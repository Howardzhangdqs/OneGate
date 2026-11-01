from __future__ import annotations

import logging

from aiohttp import web

from src.config import AppConfig
from src.db import get_site_models, is_site_model_pattern, resolve_site_model_exact
from src.models import Provider
from src.pool import resolve_token

logger = logging.getLogger(__name__)


def _get_provider_by_name(cfg: AppConfig, name: str) -> Provider | None:
    for p in cfg.providers:
        if p.name == name:
            return p
    return None


def create_catalog_handlers(
    cfg: AppConfig,
    token_pool: dict,
    db_conn,
):
    def _authenticate(request: web.Request):
        auth_header = request.headers.get("Authorization", "")
        proxy_token, _ = resolve_token(
            auth_header, cfg.providers[0], token_pool, cfg.real_tokens
        )
        if not proxy_token:
            logger.warning(
                "catalog auth failed: %s",
                auth_header[:40] if auth_header else "(none)",
            )
            return None, web.json_response({"error": "unauthorized"}, status=401)
        return proxy_token, None

    async def list_models(request: web.Request) -> web.StreamResponse:
        proxy_token, auth_err = _authenticate(request)
        if auth_err:
            return auth_err
        entry = token_pool.get(proxy_token)
        data = []
        for row in get_site_models(db_conn, enabled_only=True):
            if is_site_model_pattern(row["model"]):
                continue
            provider = _get_provider_by_name(cfg, row["provider"])
            if provider is None:
                continue
            if entry is None or not entry.has_permission(provider):
                continue
            if not entry.has_model_permission(row["model"]):
                continue
            data.append(
                {
                    "id": row["model"],
                    "object": "model",
                    "created": 0,
                    "owned_by": row["provider"],
                }
            )
        return web.json_response({"object": "list", "data": data})

    async def get_model(request: web.Request) -> web.StreamResponse:
        proxy_token, auth_err = _authenticate(request)
        if auth_err:
            return auth_err
        name = request.match_info.get("model", "")
        row = (
            resolve_site_model_exact(db_conn, name)
            if not is_site_model_pattern(name)
            else None
        )
        provider = _get_provider_by_name(cfg, row["provider"]) if row else None
        entry = token_pool.get(proxy_token)
        visible = (
            row is not None
            and provider is not None
            and entry is not None
            and entry.has_permission(provider)
            and entry.has_model_permission(row["model"])
        )
        if not visible:
            return web.json_response({"error": f"model '{name}' not found"}, status=404)
        return web.json_response(
            {
                "id": row["model"],
                "object": "model",
                "created": 0,
                "owned_by": row["provider"],
            }
        )

    return list_models, get_model
