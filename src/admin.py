from __future__ import annotations

import os

from aiohttp import web

from src.config import AppConfig
from src.db import (
    add_token,
    delete_model_map,
    delete_token,
    get_all_tokens,
    get_model_maps,
    get_tokens_usage_summary,
    is_admin_token,
    set_model_map,
    update_token,
)
from src.models import TokenEntry
from src.pool import make_token

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


def _get_admin_token(request: web.Request) -> str:
    return request.headers.get("X-Admin-Token", "")


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
        rpm = body.get("rpm", -1)
        rph = body.get("rph", -1)
        rpd = body.get("rpd", -1)
        rpt = body.get("rpt", -1)

        token = custom_token if custom_token else make_token()
        entry = add_token(
            db_conn,
            token,
            providers,
            is_admin,
            describe=describe,
            rpm=rpm,
            rph=rph,
            rpd=rpd,
            rpt=rpt,
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
        rpm = body.get("rpm")
        rph = body.get("rph")
        rpd = body.get("rpd")
        rpt = body.get("rpt")
        new_token = body.get("new_token")

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
                rpm=rpm,
                rph=rph,
                rpd=rpd,
                rpt=rpt,
                new_token=new_token,
            )
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=409)

        if not entry:
            return web.json_response({"error": "token not found"}, status=404)

        # If the token was renamed, drop the old key from the in-memory auth
        # pool so the previous value stops authenticating immediately.
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
