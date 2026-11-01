from __future__ import annotations

import os

from aiohttp import web

from src.client_detect import client_label
from src.config import AppConfig
from src.db import (
    add_token,
    delete_model_map,
    delete_pricing,
    delete_site_model,
    delete_token,
    get_all_pricing,
    get_all_tokens,
    get_model_maps,
    get_site_models,
    get_token_clients,
    get_tokens_usage_summary,
    is_admin_token,
    set_model_map,
    set_pricing,
    set_site_model,
    update_token,
)
from src.models import TokenEntry
from src.pool import make_token

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

_TOKEN_LIMIT_FIELDS = ("rpm", "rph", "rpd", "rpw", "rpmo", "rpt")
_TOKEN_TP_FIELDS = ("tpm", "tph", "tpd", "tpw", "tpmo", "tpt")


def _read_limit(body: dict, field: str) -> int:
    raw = body.get(field, -1)
    if raw is None or raw == "":
        return -1
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return -1
    if val == -1:
        return -1
    if field in _TOKEN_TP_FIELDS:
        val = val * 1_000_000
    return int(val)


def _read_limit_opt(body: dict, field: str) -> int | None:
    if field not in body:
        return None
    return _read_limit(body, field)


def _read_weight(body: dict, field: str) -> float:
    raw = body.get(field, -1)
    if raw is None or raw == "":
        return -1
    try:
        return float(raw)
    except (TypeError, ValueError):
        return -1


def _read_weight_opt(body: dict, field: str) -> float | None:
    if field not in body:
        return None
    return _read_weight(body, field)


def _get_admin_token(request: web.Request) -> str:
    return request.headers.get("X-Admin-Token", "")


def _read_models(body: dict) -> list[str] | None:
    if "models" not in body:
        return None
    raw = body.get("models")
    if not isinstance(raw, list):
        raise ValueError("models must be a list of non-empty strings")
    models: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("models must be a list of non-empty strings")
        models.append(item.strip())
    return models


def create_admin_handlers(
    cfg: AppConfig,
    db_conn,
    token_pool: dict[str, TokenEntry],
):
    async def serve_admin_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "admin.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "admin.html not found"}, status=500)

        providers = [p.name for p in cfg.providers]
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("/*__PROVIDERS__*/", repr(providers))
        return web.Response(text=html, content_type="text/html")

    async def api_list_tokens(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)

        tokens = get_all_tokens(db_conn)
        return web.json_response({"tokens": [t.to_dict() for t in tokens]})

    async def api_create_token(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        providers = body.get("providers", ["*"])
        is_admin = body.get("is_admin", False)
        describe = body.get("describe", "")
        custom_token = body.get("token")
        limits = {
            f: _read_limit(body, f) for f in _TOKEN_LIMIT_FIELDS + _TOKEN_TP_FIELDS
        }
        cr_w = _read_weight(body, "cache_read_weight")
        cw_w = _read_weight(body, "cache_write_weight")
        try:
            models = _read_models(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        token = custom_token if custom_token else make_token()
        entry = add_token(
            db_conn,
            token,
            providers,
            is_admin,
            describe=describe,
            cache_read_weight=cr_w,
            cache_write_weight=cw_w,
            models=models,
            **limits,
        )
        token_pool[entry.token] = entry
        return web.json_response({"token": entry.to_dict()})

    async def api_update_token(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        target = body.get("token", "")
        if not target:
            return web.json_response({"error": "missing token"}, status=400)

        providers = body.get("providers")
        is_admin = body.get("is_admin")
        enabled = body.get("enabled")
        describe = body.get("describe")
        limits = {
            f: _read_limit_opt(body, f) for f in _TOKEN_LIMIT_FIELDS + _TOKEN_TP_FIELDS
        }
        new_token = body.get("new_token")
        try:
            models = _read_models(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        if is_admin is False and target == admin_token:
            return web.json_response(
                {"error": "cannot remove your own admin privilege"}, status=400
            )
        if enabled is False and target == admin_token:
            return web.json_response(
                {"error": "cannot disable your own token"}, status=400
            )

        try:
            entry = update_token(
                db_conn,
                target,
                providers=providers,
                is_admin=is_admin,
                enabled=enabled,
                describe=describe,
                new_token=new_token,
                cache_read_weight=_read_weight_opt(body, "cache_read_weight"),
                cache_write_weight=_read_weight_opt(body, "cache_write_weight"),
                models=models,
                **limits,
            )
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=409)

        if not entry:
            return web.json_response({"error": "token not found"}, status=404)

        # 如果 token 被重命名，从内存中的认证池移除旧键，
        # 使旧值立即停止通过认证。
        if entry.token != target:
            token_pool.pop(target, None)
        token_pool[entry.token] = entry
        return web.json_response({"token": entry.to_dict()})

    async def api_delete_token(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        target = body.get("token", "")
        if not target:
            return web.json_response({"error": "missing token"}, status=400)

        if target == admin_token:
            return web.json_response(
                {"error": "cannot delete your own token"}, status=400
            )

        if not delete_token(db_conn, target):
            return web.json_response({"error": "token not found"}, status=404)

        token_pool.pop(target, None)
        return web.json_response({"ok": True})

    async def api_tokens_usage(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)

        usage = get_tokens_usage_summary(db_conn)
        for token, data in usage.items():
            clients = get_token_clients(db_conn, token)
            top = clients[0] if clients else None
            data["clients"] = [
                {
                    "client": c["client"],
                    "label": client_label(c["client"]),
                    "requests": c["requests"],
                }
                for c in clients
            ]
            data["top_client"] = (
                {
                    "client": top["client"],
                    "label": client_label(top["client"]),
                    "requests": top["requests"],
                }
                if top
                else None
            )
        return web.json_response({"usage": usage})

    return (
        serve_admin_page,
        api_list_tokens,
        api_create_token,
        api_update_token,
        api_delete_token,
        api_tokens_usage,
    )


def _validate_proxy_token(
    request: web.Request, db_conn, token_pool: dict[str, TokenEntry]
) -> tuple[str | None, web.Response | None]:
    token = request.headers.get("X-Proxy-Token", "").strip()
    if not token:
        return None, web.json_response({"error": "missing X-Proxy-Token"}, status=401)
    entry = token_pool.get(token)
    if not entry or not entry.enabled:
        return None, web.json_response(
            {"error": "invalid or disabled token"}, status=403
        )
    return token, None


def create_mapping_handlers(cfg: AppConfig, db_conn, token_pool: dict[str, TokenEntry]):
    providers = [p.name for p in cfg.providers]

    async def serve_mapping_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "mapping.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "mapping.html not found"}, status=500)
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("/*__PROVIDERS__*/[]", repr(providers))
        return web.Response(text=html, content_type="text/html")

    async def api_list_mappings(request: web.Request) -> web.Response:
        token, err = _validate_proxy_token(request, db_conn, token_pool)
        if err:
            return err
        maps = get_model_maps(db_conn, token)
        return web.json_response({"mappings": maps})

    async def api_set_mapping(request: web.Request) -> web.Response:
        token, err = _validate_proxy_token(request, db_conn, token_pool)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        pattern = body.get("pattern", "").strip()
        target = body.get("target", "").strip()
        provider = body.get("provider", "").strip()
        if not pattern or not target:
            return web.json_response(
                {"error": "pattern and target are required"}, status=400
            )
        if provider:
            if provider not in providers:
                return web.json_response(
                    {"error": f"unknown provider '{provider}'"}, status=400
                )
            entry = token_pool.get(token)
            allowed = entry is not None and (
                "*" in entry.providers or provider in entry.providers
            )
            if not allowed:
                return web.json_response(
                    {"error": f"token not authorized for provider '{provider}'"},
                    status=403,
                )
        set_model_map(db_conn, token, pattern, target, provider)
        return web.json_response({"ok": True})

    async def api_delete_mapping(request: web.Request) -> web.Response:
        token, err = _validate_proxy_token(request, db_conn, token_pool)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        pattern = body.get("pattern", "").strip()
        if not pattern:
            return web.json_response({"error": "pattern is required"}, status=400)
        if not delete_model_map(db_conn, token, pattern):
            return web.json_response({"error": "mapping not found"}, status=404)
        return web.json_response({"ok": True})

    return serve_mapping_page, api_list_mappings, api_set_mapping, api_delete_mapping


def create_pricing_handlers(cfg: AppConfig, db_conn):
    async def serve_pricing_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "pricing.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "pricing.html not found"}, status=500)
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("/*__PROVIDERS__*/", repr([p.name for p in cfg.providers]))
        return web.Response(text=html, content_type="text/html")

    async def api_list_pricing(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        is_admin = bool(admin_token) and is_admin_token(db_conn, admin_token)
        return web.json_response(
            {"pricing": get_all_pricing(db_conn), "is_admin": is_admin}
        )

    async def api_set_pricing(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        model = (body.get("model") or "").strip()
        if not model:
            return web.json_response({"error": "model is required"}, status=400)

        def _read_price(field: str) -> float:
            try:
                v = float(body.get(field, 0))
            except (TypeError, ValueError):
                raise ValueError(field)
            if v < 0:
                raise ValueError(field)
            return v

        try:
            ih = _read_price("input_cache_hit")
            im = _read_price("input_cache_miss")
            oh = _read_price("output_cache_hit")
            om = _read_price("output_cache_miss")
        except ValueError as e:
            return web.json_response({"error": f"invalid price for {e}"}, status=400)
        set_pricing(db_conn, model, ih, im, oh, om)
        return web.json_response({"ok": True})

    async def api_delete_pricing(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        model = (body.get("model") or "").strip()
        if not model:
            return web.json_response({"error": "model is required"}, status=400)
        if not delete_pricing(db_conn, model):
            return web.json_response({"error": "pricing not found"}, status=404)
        return web.json_response({"ok": True})

    return serve_pricing_page, api_list_pricing, api_set_pricing, api_delete_pricing


def create_site_model_handlers(cfg: AppConfig, db_conn):
    providers = [p.name for p in cfg.providers]

    async def serve_models_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "models.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "models.html not found"}, status=500)
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("/*__PROVIDERS__*/[]", repr(providers))
        return web.Response(text=html, content_type="text/html")

    async def api_list_models(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)
        return web.json_response({"models": get_site_models(db_conn)})

    async def api_set_model(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        model = (body.get("model") or "").strip()
        provider = (body.get("provider") or "").strip()
        if not model:
            return web.json_response({"error": "model is required"}, status=400)
        if not provider:
            return web.json_response({"error": "provider is required"}, status=400)
        if provider not in providers:
            return web.json_response(
                {"error": f"unknown provider '{provider}'"}, status=400
            )
        target = (body.get("target") or "").strip()
        enabled = body.get("enabled", True)
        enabled = bool(enabled) if not isinstance(enabled, bool) else enabled
        set_site_model(db_conn, model, provider, target, enabled)
        return web.json_response({"ok": True})

    async def api_delete_model(request: web.Request) -> web.Response:
        admin_token = _get_admin_token(request)
        if not admin_token or not is_admin_token(db_conn, admin_token):
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        model = (body.get("model") or "").strip()
        if not model:
            return web.json_response({"error": "model is required"}, status=400)
        if not delete_site_model(db_conn, model):
            return web.json_response({"error": "model not found"}, status=404)
        return web.json_response({"ok": True})

    return serve_models_page, api_list_models, api_set_model, api_delete_model
