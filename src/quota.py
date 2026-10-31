from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from src.config import AppConfig
from src.models import Provider

QUOTA_LIMIT_PATH = "/api/monitor/usage/quota/limit"


def _origin(url: str) -> str:
    if "://" in url:
        scheme, rest = url.split("://", 1)
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}"
    return url.split("/", 1)[0]


def resolve_quota_source(cfg: AppConfig) -> tuple[str, str]:
    if cfg.quota_base and cfg.quota_token:
        return cfg.quota_base, cfg.quota_token
    provider: Provider | None = None
    for p in cfg.providers:
        if p.v1_base:
            provider = p
            break
    if provider is None:
        for p in cfg.providers:
            if p.prefix == "/":
                provider = p
                break
    if provider is None and cfg.providers:
        provider = cfg.providers[0]
    if provider is None:
        return "", ""
    base = cfg.quota_base or _origin(provider.v1_base or provider.upstream)
    token = cfg.quota_token
    if not token:
        token = cfg.real_tokens.get(provider.name) or os.environ.get(
            provider.env_token, ""
        )
    return base, token


@dataclass
class QuotaCache:
    base: str
    token: str
    ttl: float = 60.0
    fetched_at: float = 0.0
    data: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _fresh(self) -> bool:
        return self.data is not None and time.time() - self.fetched_at < self.ttl

    async def get(self, session: aiohttp.ClientSession) -> dict:
        if self._fresh():
            return {"cache": "hit", "fetched_at": self.fetched_at, "data": self.data}
        async with self.lock:
            if self._fresh():
                return {
                    "cache": "hit",
                    "fetched_at": self.fetched_at,
                    "data": self.data,
                }
            payload = await self._fetch(session)
            self.data = payload
            self.fetched_at = time.time()
            return {"cache": "miss", "fetched_at": self.fetched_at, "data": payload}

    async def _fetch(self, session: aiohttp.ClientSession) -> dict:
        url = f"{self.base}{QUOTA_LIMIT_PATH}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Language": "en-US,en",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"upstream HTTP {resp.status}: {text[:200]}")
        value = json.loads(text)
        ok = value.get("success") is True or value.get("code") == 200
        if not ok or "data" not in value:
            raise RuntimeError(f"upstream rejected: {text[:200]}")
        return value["data"]


def create_quota_handlers(cfg: AppConfig, session: aiohttp.ClientSession):
    base, token = resolve_quota_source(cfg)
    cache = QuotaCache(base=base, token=token, ttl=max(10, cfg.quota_cache_ttl))

    async def quota_api_handler(request: web.Request) -> web.Response:
        if not base or not token:
            return web.json_response(
                {"error": "quota source not configured"}, status=503
            )
        try:
            result = await cache.get(session)
            return web.json_response(result)
        except Exception as e:
            if cache.data is not None:
                return web.json_response(
                    {
                        "cache": "stale",
                        "fetched_at": cache.fetched_at,
                        "data": cache.data,
                        "error": str(e),
                    }
                )
            return web.json_response({"error": str(e)}, status=502)

    return quota_api_handler
