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
    get_client_usage_detail,
    get_global_client_hourly,
    get_global_client_summary,
    get_provider_status,
    get_token_client_usage_detail,
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


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


async def _query_usage(token, token_pool, db_conn, start_str=None, end_str=None):
    entry = token_pool.get(token)
    if not entry:
        return None, "token not found in pool"

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
        "FROM success_log WHERE token = ? AND ts >= ? AND ts < ? "
        "GROUP BY provider, hour_ts ORDER BY provider, hour_ts",
        (token, start_hour, end_hour + 3600),
    )
    rows = cur.fetchall()

    req_by_provider: dict[str, dict[int, int]] = {}
    for provider_name, hour_ts, cnt in rows:
        req_by_provider.setdefault(provider_name, {})[hour_ts] = cnt

    tok_cur = db_conn.execute(
        "SELECT provider, ts - (ts % 3600) AS hour_ts, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write) "
        "FROM token_usage_log WHERE token = ? AND ts >= ? AND ts < ? "
        "GROUP BY provider, hour_ts ORDER BY provider, hour_ts",
        (token, start_hour, end_hour + 3600),
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
            "SELECT COALESCE(SUM(count), 0) FROM success_log "
            "WHERE token = ? AND ts >= ?",
            (token, cutoff_hour),
        ).fetchone()
        summary[label] = row[0]

    current_hour_rows = db_conn.execute(
        "SELECT provider, SUM(count) FROM success_log "
        "WHERE token = ? AND ts >= ? AND ts < ? "
        "GROUP BY provider",
        (token, current_hour_ts, current_hour_ts + 3600),
    ).fetchall()
    current_hour = {
        "hour": datetime.fromtimestamp(current_hour_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "by_provider": {r[0]: r[1] for r in current_hour_rows},
    }

    tokens_by_provider = get_token_usage_detail(db_conn, token, start_ts, end_ts)
    clients_summary = get_token_client_usage_detail(db_conn, token, start_ts, end_ts)

    return {
        "token": token,
        "providers": entry.providers,
        "describe": getattr(entry, "describe", ""),
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
    }, None


def create_handlers(
    cfg: AppConfig,
    token_pool: dict[str, TokenEntry],
    db_conn,
    public_ip: str | None,
    session: aiohttp.ClientSession,
    start_time: float,
    monitor=None,
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
        if not token:
            return web.json_response({"error": "missing token"}, status=400)

        data, err = await _query_usage(
            token,
            token_pool,
            db_conn,
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
            if token_entry and (
                token_entry.rpm != -1
                or token_entry.rph != -1
                or token_entry.rpd != -1
                or token_entry.rpt != -1
            ):
                allowed, triggered, quota_info = check_rate_limit(
                    db_conn,
                    proxy_token,
                    token_entry.rpm,
                    token_entry.rph,
                    token_entry.rpd,
                    token_entry.rpt,
                )
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

        v1_override_provider = None
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
                if mapped != model:
                    try:
                        obj = json.loads(body)
                        obj["model"] = mapped
                        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                        headers["Content-Length"] = str(len(body))
                    except Exception:
                        pass

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
                usage_buf = b"" if tracker is None else None
                async for chunk in resp.content.iter_any():
                    if cfg.enable_log and cfg.log_format == "full":
                        resp_chunks.append(chunk)

                    if tracker is not None:
                        tracker.feed_chunk(chunk, ct)
                    elif usage_buf is not None and len(usage_buf) < _USAGE_BUF_LIMIT:
                        usage_buf += chunk

                    await response.write(chunk)

                await response.write_eof()

                if tracker is not None:
                    if tracker.is_stream:
                        tracker.emit_end_stream(resp.status, resp_headers)
                    else:
                        full_resp = b"".join(tracker.chunks)
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
                        elif usage_buf:
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
                        record_token_usage(
                            db_conn,
                            proxy_token,
                            effective_provider.name,
                            tin,
                            tout,
                            tcr,
                            tcw,
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
                        )
                record_status(db_conn, effective_provider.name, resp.status)
                logger.info(
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
        uptime = int(time.time() - start_time)
        stats = get_provider_status(db_conn)

        grouped: dict[str, dict] = {}
        for p in cfg.providers:
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
            for p in cfg.providers:
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
            for p in cfg.providers:
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

    return (
        serve_usage_page,
        info_api_handler,
        proxy_handler,
        serve_status_page,
        status_api_handler,
        serve_clients_page,
        clients_api_handler,
    )
