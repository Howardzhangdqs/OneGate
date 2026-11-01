from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

from src.client_detect import detect_client
from src.config import AppConfig, match_provider
from src.db import (
    check_rate_limit,
    compute_cost,
    get_client_usage_detail,
    get_global_client_hourly,
    get_global_client_summary,
    get_pricing_map,
    get_provider_status,
    get_rate_limit_status,
    get_token_client_hourly,
    get_token_client_usage_detail,
    get_token_usage_by_model,
    get_token_usage_detail,
    record_client_token_usage,
    record_client_usage,
    record_status,
    record_success,
    record_token_usage,
    record_usage,
    resolve_model,
)
from src.log import save_log, save_log_headers_only
from src.models import TokenEntry
from src.pool import resolve_token
from src.stream_proxy import pump_stream
from src.throughput import (
    _extract_usage_from_json,
    _extract_usage_from_sse,
    make_tracker,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
logger = logging.getLogger(__name__)

_USAGE_BUF_LIMIT = 256 * 1024

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

_STRIP_REQUEST_HEADERS = _HOP_BY_HOP | {"content-length"}

_TRACKING_HEADERS = frozenset(
    {
        "x-session-id",
        "x-zcode-trace-id",
        "x-query-id",
        "x-request-id",
        "x-title",
        "x-os-category",
        "x-os-version",
        "x-platform",
        "x-client-timezone",
        "x-client-language",
        "x-release-channel",
        "x-zcode-app-version",
        "x-zcode-agent",
        "x-zcode-session-type",
        "x-app",
        "http-referer",
        "referer",
        "x-forwarded-for",
        "x-real-ip",
        "x-forwarded-proto",
        "x-forwarded-scheme",
    }
)

_TRACKING_HEADER_PREFIXES = ("x-stainless-",)

_TRACKING_BODY_KEYS = ("metadata", "user")


def _apply_privacy_headers(headers: dict, ua_override: str) -> dict:
    out: dict[str, str] = {}
    for key, value in headers.items():
        lk = key.lower()
        if lk in _TRACKING_HEADERS or lk.startswith(_TRACKING_HEADER_PREFIXES):
            continue
        if lk == "user-agent" and ua_override:
            continue
        out[key] = value
    if ua_override:
        out["User-Agent"] = ua_override
    return out


def _strip_tracking_body(body: bytes | None) -> bytes | None:
    if not body:
        return body
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    if not isinstance(obj, dict) or not any(k in obj for k in _TRACKING_BODY_KEYS):
        return body
    for key in _TRACKING_BODY_KEYS:
        obj.pop(key, None)
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _build_forward_headers(request: web.Request, strip_lower: frozenset[str]) -> dict:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in strip_lower:
            continue
        headers[key] = value
    return headers


def _parse_request_model(body: bytes | None) -> str:
    if not body:
        return ""
    try:
        obj = json.loads(body)
        return obj.get("model", "")
    except Exception:
        return ""


def _request_wants_stream(body: bytes | None) -> bool:
    if not body:
        return False
    try:
        obj = json.loads(body)
    except Exception:
        return False
    return isinstance(obj, dict) and obj.get("stream") is True


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


async def _query_usage(token, token_pool, db_conn, cfg, start_str=None, end_str=None):
    entry = token_pool.get(token) if token else None
    if token and not entry:
        return None, "token not found in pool"

    if token:
        where = "WHERE token = ? AND ts >= ? AND ts < ?"
        where_since = "WHERE token = ? AND ts >= ?"

        def _params(start, end):
            return (token, start, end)

        def _params_since(start):
            return (token, start)

    else:
        where = "WHERE ts >= ? AND ts < ?"
        where_since = "WHERE ts >= ?"

        def _params(start, end):
            return (start, end)

        def _params_since(start):
            return (start,)

    now = datetime.now(timezone.utc)
    current_hour_ts = int(now.timestamp()) - (int(now.timestamp()) % 3600)

    if start_str:
        start_dt = _parse_iso(start_str)
    else:
        start_dt = now - timedelta(hours=24)

    if end_str:
        end_dt = _parse_iso(end_str)
    else:
        end_dt = now

    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)

    cur = db_conn.execute(
        "SELECT provider, ts - (ts % 3600) AS hour_ts, SUM(count) AS cnt "
        "FROM success_log "
        + where
        + " GROUP BY provider, hour_ts ORDER BY provider, hour_ts",
        _params(start_hour, end_hour + 3600),
    )
    rows = cur.fetchall()

    req_by_provider: dict[str, dict[int, int]] = {}
    for provider_name, hour_ts, cnt in rows:
        req_by_provider.setdefault(provider_name, {})[hour_ts] = cnt

    tok_cur = db_conn.execute(
        "SELECT provider, ts - (ts % 3600) AS hour_ts, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write) "
        "FROM token_usage_log "
        + where
        + " GROUP BY provider, hour_ts ORDER BY provider, hour_ts",
        _params(start_hour, end_hour + 3600),
    )
    tok_rows = tok_cur.fetchall()

    tok_by_provider: dict[str, dict[int, dict[str, int]]] = {}
    for provider_name, hour_ts, it, ot, cr, cw in tok_rows:
        tok_by_provider.setdefault(provider_name, {})[hour_ts] = {
            "input": it or 0,
            "output": ot or 0,
            "cache_read": cr or 0,
            "cache_write": cw or 0,
        }

    all_providers = set(tok_by_provider) | set(req_by_provider)

    hourly: dict[str, dict[str, dict]] = {}
    t = start_hour
    while t <= end_hour:
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        key = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        hourly[key] = {}
        for prov_name in all_providers:
            tok = tok_by_provider.get(prov_name, {}).get(t, {})
            req = req_by_provider.get(prov_name, {}).get(t, 0)
            hourly[key][prov_name] = {
                "input": tok.get("input", 0),
                "output": tok.get("output", 0),
                "cache_read": tok.get("cache_read", 0),
                "cache_write": tok.get("cache_write", 0),
                "requests": req,
            }
        t += 3600

    total = sum(cnt for _, _, cnt in rows)

    summary: dict[str, int] = {}
    for label, hours in [("last_24h", 24), ("last_7d", 168), ("last_30d", 720)]:
        cutoff = int(now.timestamp()) - hours * 3600
        cutoff_hour = cutoff - (cutoff % 3600)
        row = db_conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM success_log " + where_since,
            _params_since(cutoff_hour),
        ).fetchone()
        summary[label] = row[0]

    current_hour_rows = db_conn.execute(
        "SELECT provider, SUM(count) FROM success_log " + where + " GROUP BY provider",
        _params(current_hour_ts, current_hour_ts + 3600),
    ).fetchall()
    current_hour = {
        "hour": datetime.fromtimestamp(current_hour_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "by_provider": {r[0]: r[1] for r in current_hour_rows},
    }

    site_token = token if entry else None
    tokens_by_provider = get_token_usage_detail(db_conn, site_token, start_ts, end_ts)
    clients_summary = get_token_client_usage_detail(
        db_conn, site_token, start_ts, end_ts
    )
    client_hourly_raw = get_token_client_hourly(db_conn, site_token, start_ts, end_ts)

    models_breakdown = get_token_usage_by_model(db_conn, site_token, start_ts, end_ts)
    pricing_map = get_pricing_map(db_conn)
    total_cost = 0.0
    for model_name, row in models_breakdown.items():
        price_row = pricing_map.get(model_name)
        if price_row:
            cost = compute_cost(row, price_row)
            row["cost"] = round(cost, 6)
            total_cost += cost
        else:
            row["cost"] = None
    has_priced = any(
        models_breakdown[m].get("cost") is not None for m in models_breakdown
    )

    hourly_by_client: dict[str, dict[str, dict]] = {}
    all_clients = set(client_hourly_raw)
    t = start_hour
    while t <= end_hour:
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        key = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        hourly_by_client[key] = {}
        for c_name in all_clients:
            slot = client_hourly_raw.get(c_name, {}).get(t, {})
            hourly_by_client[key][c_name] = {
                "input": slot.get("input", 0),
                "output": slot.get("output", 0),
                "cache_read": slot.get("cache_read", 0),
                "cache_write": slot.get("cache_write", 0),
                "requests": slot.get("requests", 0),
            }
        t += 3600

    return {
        "token": token or "",
        "providers": (entry.providers if entry else [p.name for p in cfg.providers]),
        "describe": getattr(entry, "describe", "") if entry else "",
        "current_hour": current_hour,
        "range": {
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "total": total,
        "summary": summary,
        "hourly": hourly,
        "tokens": tokens_by_provider,
        "clients": clients_summary,
        "hourly_by_client": hourly_by_client,
        "limits": get_rate_limit_status(db_conn, entry) if entry else [],
        "models": models_breakdown,
        "total_cost": round(total_cost, 6) if has_priced else None,
    }, None


def create_handlers(
    cfg: AppConfig,
    token_pool: dict[str, TokenEntry],
    db_conn,
    public_ip: str | None,
    session: aiohttp.ClientSession,
    start_time: float,
    monitor=None,
    usage_monitor=None,
):
    async def serve_usage_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "usage.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "usage.html not found"}, status=500)
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def info_api_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        token = body.get("token")
        if token is not None and not isinstance(token, str):
            return web.json_response({"error": "invalid token"}, status=400)
        token = (token or "").strip()

        data, err = await _query_usage(
            token,
            token_pool,
            db_conn,
            cfg,
            body.get("start"),
            body.get("end"),
        )
        if err:
            return web.json_response({"error": err}, status=404)
        return web.json_response(data)

    async def proxy_handler(request: web.Request) -> web.StreamResponse:
        client_ip = request.headers.get("X-Forwarded-For", "")
        if not client_ip:
            client_ip = request.headers.get("X-Real-IP", "")
        if not client_ip:
            client_ip = request.remote or ""
        if client_ip in cfg.banned_ips:
            return web.json_response({"error": "forbidden"}, status=403)

        ua = request.headers.get("User-Agent", "")
        for pattern in cfg.banned_uas:
            if fnmatch.fnmatch(ua, pattern):
                return web.json_response({"error": "forbidden"}, status=403)

        is_v1_route = request.path.startswith("/v1/")
        provider = match_provider(cfg.providers, request.path)
        if not provider:
            return web.json_response({"error": "no provider matched"}, status=502)

        path = request.path
        if provider.strip_prefix:
            prefix = provider.prefix.rstrip("/")
            if path.startswith(prefix):
                path = path[len(prefix) :] or "/"

        proxy_token = None

        headers = _build_forward_headers(request, _STRIP_REQUEST_HEADERS)
        headers["Host"] = provider.host
        if cfg.privacy_strip_tracking:
            headers = _apply_privacy_headers(headers, cfg.privacy_ua_override)
        else:
            existing_xff = request.headers.get("X-Forwarded-For")
            headers["X-Forwarded-For"] = (
                f"{existing_xff}, {public_ip}" if existing_xff else public_ip
            )
            headers["X-Real-IP"] = public_ip

        forbidden = False
        client = detect_client(request.headers)
        auth_header = request.headers.get("Authorization", "")
        if auth_header:
            proxy_token, resolved_auth = resolve_token(
                auth_header, provider, token_pool, cfg.real_tokens
            )
            if proxy_token is not None and resolved_auth == "":
                forbidden = True
            else:
                headers["Authorization"] = resolved_auth

        api_key = request.headers.get("x-api-key", "")
        if api_key:
            key_token, resolved_key = resolve_token(
                "Bearer " + api_key,
                provider,
                token_pool,
                cfg.real_tokens,
            )
            if key_token is not None:
                proxy_token = key_token
                if resolved_key == "":
                    forbidden = True
                else:
                    headers["x-api-key"] = resolved_key[7:]

        if forbidden:
            return web.json_response(
                {"error": f"token not authorized for provider '{provider.name}'"},
                status=403,
            )

        if proxy_token:
            token_entry = token_pool.get(proxy_token)
            if token_entry and token_entry.has_limits:
                allowed, triggered, quota_info = check_rate_limit(db_conn, token_entry)
                if not allowed:
                    return web.json_response(
                        {
                            "error": "rate limit exceeded",
                            "triggered": triggered,
                            "quota": quota_info,
                        },
                        status=429,
                    )

        body = await request.read() if request.body_exists else None
        if cfg.privacy_strip_tracking:
            body = _strip_tracking_body(body)
        if _request_wants_stream(body):
            headers["Accept-Encoding"] = "identity"

        v1_override_provider = None
        effective_model = ""
        if is_v1_route and proxy_token and body:
            model = _parse_request_model(body)
            if not model:
                return web.json_response(
                    {"error": "model not found in request body"}, status=400
                )
            mapped_model, mapped_provider_name = resolve_model(
                db_conn, proxy_token, model
            )
            if mapped_model == model and not mapped_provider_name:
                return web.json_response(
                    {
                        "error": f"model '{model}' not found in mapping rules",
                        "model": model,
                    },
                    status=404,
                )
            effective_model = mapped_model
            for p in cfg.providers:
                if p.name == mapped_provider_name:
                    v1_override_provider = p
                    break
            if v1_override_provider:
                try:
                    obj = json.loads(body)
                    obj["model"] = mapped_model
                    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    headers["Content-Length"] = str(len(body))
                except Exception:
                    pass
                real_token = cfg.real_tokens.get(v1_override_provider.name, "")
                if real_token:
                    headers["Authorization"] = f"Bearer {real_token}"
                headers["Host"] = v1_override_provider.host
        elif proxy_token and body:
            model = _parse_request_model(body)
            if model:
                mapped, _ = resolve_model(db_conn, proxy_token, model)
                effective_model = mapped
                if mapped != model:
                    try:
                        obj = json.loads(body)
                        obj["model"] = mapped
                        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                        headers["Content-Length"] = str(len(body))
                    except Exception:
                        pass
            else:
                effective_model = ""

        effective_provider = v1_override_provider or provider
        if is_v1_route and v1_override_provider:
            v1_path = path
            if v1_path.startswith("/v1"):
                v1_path = v1_path[3:] or "/"
            if cfg.intercept_port:
                target_url = (
                    f"http://127.0.0.1:{cfg.intercept_port}"
                    + effective_provider.upstream_path
                    + v1_path
                )
            else:
                target_url = (
                    effective_provider.origin
                    + effective_provider.upstream_path
                    + v1_path
                )
        else:
            if cfg.intercept_port:
                target_url = (
                    f"http://127.0.0.1:{cfg.intercept_port}"
                    + effective_provider.upstream_path
                    + path
                )
            else:
                target_url = (
                    effective_provider.origin + effective_provider.upstream_path + path
                )
        if request.query_string:
            target_url += "?" + request.query_string

        tracker = make_tracker(
            monitor,
            proxy_token,
            model=_parse_request_model(body),
            provider_name=effective_provider.name,
            request_url=request.path,
            target_url=target_url,
            request_headers=headers,
            request_body=body,
        )
        if tracker is not None:
            tracker.emit_start()

        try:
            async with session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as resp:
                resp_headers = dict(resp.headers)
                resp_headers.pop("Transfer-Encoding", None)
                resp_headers.pop("Content-Length", None)
                resp_headers.pop("Content-Encoding", None)

                ct = resp_headers.get("Content-Type", "")

                response = web.StreamResponse(
                    status=resp.status,
                    headers=resp_headers,
                )
                await response.prepare(request)

                resp_chunks = []
                usage_parts: list[bytes] | None = [] if tracker is None else None
                usage_len = 0

                def _on_proxy_chunk(chunk: bytes) -> None:
                    nonlocal usage_len
                    if cfg.enable_log and cfg.log_format == "full":
                        resp_chunks.append(chunk)

                    if tracker is not None:
                        tracker.feed_chunk(chunk, ct)
                    elif usage_parts is not None and usage_len < _USAGE_BUF_LIMIT:
                        usage_parts.append(chunk)
                        usage_len += len(chunk)

                if "text/event-stream" in ct:
                    stream_started = time.monotonic()
                    pump_result = await pump_stream(
                        response,
                        resp.content.iter_any(),
                        heartbeat_interval=cfg.timeout_stream_heartbeat,
                        idle_timeout=cfg.timeout_stream_idle,
                        on_chunk=_on_proxy_chunk,
                    )
                    if pump_result == "idle_timeout":
                        logger.warning(
                            "SSE stream closed after idle timeout %.1fs: provider=%s path=%s",
                            time.monotonic() - stream_started,
                            effective_provider.name,
                            request.path,
                        )
                else:
                    async for chunk in resp.content.iter_any():
                        _on_proxy_chunk(chunk)
                        await response.write(chunk)

                    await response.write_eof()

                if tracker is not None:
                    if tracker.is_stream:
                        tracker.emit_end_stream(resp.status, resp_headers)
                    else:
                        full_resp = tracker.buffered_body
                        tracker.feed_non_stream_response(
                            full_resp, resp.status, resp_headers
                        )

                resp_body = b"".join(resp_chunks) if resp_chunks else None
                if cfg.enable_log:
                    if cfg.log_format == "headers":
                        save_log_headers_only(
                            request.method,
                            request.path,
                            request.headers,
                            resp.status,
                            resp_headers,
                            headers,
                            effective_provider.name,
                        )
                    else:
                        save_log(
                            request.method,
                            request.path,
                            request.headers,
                            body,
                            resp.status,
                            resp_headers,
                            resp_body,
                            headers,
                            target_url,
                            decompress=cfg.decompress_log,
                        )
                if proxy_token:
                    record_usage(db_conn, proxy_token, effective_provider.name)
                    record_client_usage(
                        db_conn, proxy_token, effective_provider.name, client
                    )
                    if resp.status == 200:
                        record_success(db_conn, proxy_token, effective_provider.name)
                        if tracker is not None:
                            tin, tout, tcr, tcw = tracker.final_usage
                        elif usage_parts:
                            usage_buf = b"".join(usage_parts)
                            if "text/event-stream" in ct:
                                tin, tout, tcr, tcw = _extract_usage_from_sse(
                                    usage_buf.decode("utf-8", errors="replace")
                                )
                            else:
                                tin, tout, tcr, tcw = _extract_usage_from_json(
                                    usage_buf
                                )
                        else:
                            tin, tout, tcr, tcw = 0, 0, 0, 0
                        if token_entry is not None:
                            cr_w = (
                                token_entry.cache_read_weight
                                if token_entry.cache_read_weight >= 0
                                else cfg.cache_read_weight
                            )
                            cw_w = (
                                token_entry.cache_write_weight
                                if token_entry.cache_write_weight >= 0
                                else cfg.cache_write_weight
                            )
                        else:
                            cr_w, cw_w = cfg.cache_read_weight, cfg.cache_write_weight
                        record_token_usage(
                            db_conn,
                            proxy_token,
                            effective_provider.name,
                            tin,
                            tout,
                            tcr,
                            tcw,
                            cr_w,
                            cw_w,
                            model=effective_model,
                        )
                        record_client_token_usage(
                            db_conn,
                            proxy_token,
                            effective_provider.name,
                            client,
                            tin,
                            tout,
                            tcr,
                            tcw,
                            model=effective_model,
                        )
                        if usage_monitor is not None and usage_monitor.is_active(
                            proxy_token
                        ):
                            now_ts = int(time.time())
                            hour_ts = now_ts - (now_ts % 3600)
                            hour_key = datetime.fromtimestamp(
                                hour_ts, tz=timezone.utc
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                            usage_monitor.notify_usage(
                                proxy_token,
                                {
                                    "hour": hour_key,
                                    "provider": effective_provider.name,
                                    "client": client,
                                    "model": effective_model,
                                    "input": tin,
                                    "output": tout,
                                    "cache_read": tcr,
                                    "cache_write": tcw,
                                    "requests": 1,
                                    "ts": now_ts,
                                },
                            )
                record_status(db_conn, effective_provider.name, resp.status)
                logger.debug(
                    "%s %s -> [%s] %s",
                    request.method,
                    request.path,
                    effective_provider.name,
                    resp.status,
                )

                return response

        except asyncio.TimeoutError:
            record_status(db_conn, effective_provider.name, 504)
            logger.error(
                "Timeout proxying %s %s -> [%s] %s",
                request.method,
                request.path,
                effective_provider.name,
                target_url,
            )
            return web.json_response(
                {"error": "upstream request timed out"},
                status=504,
            )
        except aiohttp.ClientError as e:
            record_status(db_conn, effective_provider.name, 502)
            logger.error(
                "Client error proxying %s %s -> [%s] %s: %s",
                request.method,
                request.path,
                effective_provider.name,
                target_url,
                e,
            )
            return web.json_response(
                {"error": "upstream connection error"},
                status=502,
            )

    async def serve_status_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "status.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "status.html not found"}, status=500)
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def status_api_handler(request: web.Request) -> web.Response:
        token = request.headers.get("X-Proxy-Token", "").strip()
        if not token:
            return web.json_response({"error": "missing X-Proxy-Token"}, status=401)
        entry = token_pool.get(token)
        if not entry or not entry.enabled:
            return web.json_response({"error": "invalid or disabled token"}, status=403)

        visible = [p for p in cfg.providers if entry.has_permission(p)]
        uptime = int(time.time() - start_time)
        stats = get_provider_status(db_conn)

        grouped: dict[str, dict] = {}
        for p in visible:
            real_token = cfg.real_tokens.get(p.name, "")
            key = f"{p.upstream}::{real_token}"
            if key not in grouped:
                grouped[key] = {
                    "name": p.name,
                    "prefixes": [p.prefix],
                    "upstream": p.upstream,
                    "original_names": [p.name],
                }
            else:
                grouped[key]["prefixes"].append(p.prefix)
                grouped[key]["original_names"].append(p.name)

        providers = [
            {
                "name": v["name"],
                "prefix": ", ".join(v["prefixes"]),
                "upstream": v["upstream"],
            }
            for v in grouped.values()
        ]

        merged_hourly: dict[str, list] = {}
        for pname, hourly_data in stats.get("hourly", {}).items():
            for p in visible:
                real_token = cfg.real_tokens.get(p.name, "")
                key = f"{p.upstream}::{real_token}"
                if p.name == pname and key in grouped:
                    group_name = grouped[key]["name"]
                    if group_name not in merged_hourly:
                        merged_hourly[group_name] = hourly_data
                    else:
                        for i, h in enumerate(hourly_data):
                            if i < len(merged_hourly[group_name]):
                                merged_hourly[group_name][i]["total"] += h["total"]
                                merged_hourly[group_name][i]["success"] += h["success"]
                                if merged_hourly[group_name][i]["total"] > 0:
                                    merged_hourly[group_name][i]["success_rate"] = (
                                        round(
                                            merged_hourly[group_name][i]["success"]
                                            / merged_hourly[group_name][i]["total"]
                                            * 100,
                                            2,
                                        )
                                    )
                                for sc, cnt in h.get("error_distribution", {}).items():
                                    if (
                                        sc
                                        not in merged_hourly[group_name][i][
                                            "error_distribution"
                                        ]
                                    ):
                                        merged_hourly[group_name][i][
                                            "error_distribution"
                                        ][sc] = 0
                                    merged_hourly[group_name][i]["error_distribution"][
                                        sc
                                    ] += cnt
                    break

        merged_summary: dict[str, dict] = {}
        for pname, period_data in stats.items():
            if pname == "hourly":
                continue
            for p in visible:
                real_token = cfg.real_tokens.get(p.name, "")
                key = f"{p.upstream}::{real_token}"
                if p.name == pname and key in grouped:
                    group_name = grouped[key]["name"]
                    if group_name not in merged_summary:
                        merged_summary[group_name] = period_data
                    else:
                        for period, data in period_data.items():
                            if period in merged_summary[group_name]:
                                merged_summary[group_name][period]["total"] += data[
                                    "total"
                                ]
                                merged_summary[group_name][period]["success"] += data[
                                    "success"
                                ]
                                if merged_summary[group_name][period]["total"] > 0:
                                    merged_summary[group_name][period][
                                        "success_rate"
                                    ] = round(
                                        merged_summary[group_name][period]["success"]
                                        / merged_summary[group_name][period]["total"]
                                        * 100,
                                        2,
                                    )
                                for sc, cnt in data.get(
                                    "error_distribution", {}
                                ).items():
                                    if (
                                        sc
                                        not in merged_summary[group_name][period][
                                            "error_distribution"
                                        ]
                                    ):
                                        merged_summary[group_name][period][
                                            "error_distribution"
                                        ][sc] = 0
                                    merged_summary[group_name][period][
                                        "error_distribution"
                                    ][sc] += cnt
                    break

        return web.json_response(
            {
                "uptime": uptime,
                "providers": providers,
                "stats": merged_hourly,
                "summary": merged_summary,
            }
        )

    async def serve_clients_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "clients.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "clients.html not found"}, status=500)
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def clients_api_handler(request: web.Request) -> web.Response:
        now = datetime.now(timezone.utc)
        start_str = request.query.get("start")
        end_str = request.query.get("end")
        if start_str:
            start_dt = _parse_iso(start_str)
        else:
            start_dt = now - timedelta(hours=24)
        if end_str:
            end_dt = _parse_iso(end_str)
        else:
            end_dt = now
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        summary = get_global_client_summary(db_conn, start_ts, end_ts)
        hourly = get_global_client_hourly(db_conn, start_ts, end_ts)
        detail = get_client_usage_detail(db_conn, start_ts, end_ts)

        start_hour = start_ts - (start_ts % 3600)
        end_hour = end_ts - (end_ts % 3600)

        timeseries: list[dict] = []
        t = start_hour
        while t <= end_hour:
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
            key = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            point: dict[str, int | str] = {"ts": key}
            for client_name in hourly:
                point[client_name] = hourly[client_name].get(t, 0)
            timeseries.append(point)
            t += 3600

        total = sum(v["requests"] for v in summary.values())
        unknown = summary.get("unknown", {}).get("requests", 0)
        identified = total - unknown

        return web.json_response(
            {
                "range": {
                    "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "total": total,
                "identified": identified,
                "unknown": unknown,
                "recognition_rate": round(identified / total * 100, 2)
                if total > 0
                else None,
                "summary": summary,
                "detail": detail,
                "timeseries": timeseries,
            }
        )

    async def usage_stream_api(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        token = (
            body.get("token", "").strip() if isinstance(body.get("token"), str) else ""
        )
        if not token:
            return web.json_response({"error": "missing token"}, status=400)
        entry = token_pool.get(token)
        if not entry:
            return web.json_response({"error": "token not found in pool"}, status=404)
        start = body.get("start")
        end = body.get("end")
        session_id = usage_monitor.create_session(token, start, end)
        if not session_id:
            return web.json_response({"error": "token not found in pool"}, status=404)
        return web.json_response({"session_id": session_id})

    async def usage_sse_handler(request: web.Request) -> web.StreamResponse:
        sid = request.query.get("sid", "")
        session = usage_monitor.sessions.get(sid) if usage_monitor else None
        if not session:
            return web.json_response({"error": "invalid session"}, status=403)
        session.attached = True

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        data, err = await _query_usage(
            session.token, token_pool, db_conn, cfg, session.start, session.end
        )
        snapshot = (
            f"event: snapshot\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if not err
            else f"event: error\ndata: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"
        )
        try:
            await resp.write(snapshot.encode("utf-8"))
        except ConnectionError:
            usage_monitor.remove_session(sid)
            return resp

        try:
            await resp.write(b"event: connected\ndata: {}\n\n")
        except ConnectionError:
            usage_monitor.remove_session(sid)
            return resp

        async def keepalive():
            while True:
                await asyncio.sleep(15)
                try:
                    await resp.write(b": keepalive\n\n")
                except ConnectionError:
                    break

        task = asyncio.ensure_future(keepalive())

        try:
            while True:
                if task.done():
                    break
                try:
                    chunk = await asyncio.wait_for(session.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await resp.write(chunk)
                except ConnectionError:
                    break
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            usage_monitor.remove_session(sid)

        return resp

    return (
        serve_usage_page,
        info_api_handler,
        proxy_handler,
        serve_status_page,
        status_api_handler,
        serve_clients_page,
        clients_api_handler,
        usage_stream_api,
        usage_sse_handler,
    )
