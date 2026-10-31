import asyncio
import time

from src.config import AppConfig
from src.models import Provider
from src.quota import QuotaCache, _origin, resolve_quota_source


def test_origin():
    assert (
        _origin("https://open.bigmodel.cn/api/coding/paas/v4")
        == "https://open.bigmodel.cn"
    )
    assert _origin("https://example.com:8443/path?q=1") == "https://example.com:8443"
    assert _origin("https://example.com") == "https://example.com"


def test_resolve_quota_source_from_providers(monkeypatch):
    cfg = AppConfig()
    cfg.providers = [
        Provider(
            name="default",
            prefix="/",
            upstream="https://open.bigmodel.cn",
            strip_prefix=False,
            env_token="TOKEN_ZHIPUAI",
        ),
        Provider(
            name="litellm",
            prefix="/litellm/v1",
            upstream="https://open.bigmodel.cn/api/coding/paas/v4",
            strip_prefix=True,
            env_token="TOKEN_ZHIPUAI",
            v1_base="https://open.bigmodel.cn/api/coding/paas/v4",
        ),
    ]
    monkeypatch.setenv("TOKEN_ZHIPUAI", "tok-1")
    base, token = resolve_quota_source(cfg)
    assert base == "https://open.bigmodel.cn"
    assert token == "tok-1"


def test_resolve_quota_source_explicit_wins(monkeypatch):
    cfg = AppConfig()
    cfg.quota_base = "https://relay.example.com:6443"
    cfg.quota_token = "tok-2"
    cfg.providers = [
        Provider(
            name="default",
            prefix="/",
            upstream="https://open.bigmodel.cn",
            strip_prefix=False,
            env_token="TOKEN_ZHIPUAI",
        ),
    ]
    monkeypatch.setenv("TOKEN_ZHIPUAI", "tok-1")
    base, token = resolve_quota_source(cfg)
    assert (base, token) == ("https://relay.example.com:6443", "tok-2")


def test_resolve_quota_source_unconfigured():
    assert resolve_quota_source(AppConfig()) == ("", "")


def test_cache_ttl_and_single_fetch():
    async def run():
        calls = []

        async def fake_fetch(_session):
            calls.append(time.time())
            return {"limits": [], "level": "max"}

        cache = QuotaCache(base="https://x", token="t", ttl=60)
        cache._fetch = fake_fetch
        r1 = await cache.get(None)
        r2 = await cache.get(None)
        assert r1["cache"] == "miss"
        assert r2["cache"] == "hit"
        assert r2["fetched_at"] == r1["fetched_at"]
        assert len(calls) == 1
        cache.fetched_at = time.time() - 61
        r3 = await cache.get(None)
        assert r3["cache"] == "miss"
        assert len(calls) == 2

    asyncio.run(run())


def test_cache_error_keeps_no_data():
    async def run():
        async def bad_fetch(_session):
            raise RuntimeError("upstream HTTP 401")

        cache = QuotaCache(base="https://x", token="t", ttl=60)
        cache._fetch = bad_fetch
        try:
            await cache.get(None)
            assert False, "should raise"
        except RuntimeError:
            pass
        assert cache.data is None

    asyncio.run(run())
