import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import json
import os
import ssl
import tempfile
import time
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from src.admin import create_admin_handlers
from src.config import AppConfig, load_config
from src.db import add_token, init_db, set_model_map, set_site_model
from src.handlers import (
    _apply_privacy_headers,
    _request_wants_stream,
    _resolve_v1_target,
    _strip_tracking_body,
    _v1_target_url,
    create_handlers,
)
from src.models import Provider, TokenEntry
from src.usage_monitor import UsageMonitor


def _sample_token() -> str:
    return "aaa111bbb222ccc333ddd444eee555ff"


def _make_token_full() -> str:
    return f"{_sample_token()}.testtoken0000000"


def _make_admin_token() -> str:
    return f"{_sample_token()}00.NDt4aLrzPFb4a1A1"


class TestHandlers(AioHTTPTestCase):
    async def get_application(self):
        self.cfg = AppConfig(
            bind_port=0,
            intercept_port=0,
            enable_log=False,
            pool_count=3,
            pool_file="pool.json",
            usage_db=":memory:",
            providers=[
                Provider("glm", "/glm", "https://open.bigmodel.cn", True),
                Provider("oai", "/oai", "https://api.openai.com/", True),
                Provider("default", "/", "https://open.bigmodel.cn", False),
            ],
            real_tokens={
                "glm": "real-glm-key",
                "oai": "real-oai-key",
                "default": "real-default-key",
            },
        )
        self.db_conn, _ = init_db(":memory:")

        add_token(self.db_conn, _make_token_full(), ["glm", "default"])
        add_token(self.db_conn, f"{_sample_token()}00.NDt4aLrzPFb4a1A1", ["*"])
        add_token(self.db_conn, _make_admin_token(), ["*"], is_admin=True)

        self.token_pool = {
            _make_token_full(): TokenEntry(_make_token_full(), ["glm", "default"]),
            f"{_sample_token()}00.NDt4aLrzPFb4a1A1": TokenEntry(
                f"{_sample_token()}00.NDt4aLrzPFb4a1A1", ["*"]
            ),
            _make_admin_token(): TokenEntry(_make_admin_token(), ["*"], is_admin=True),
        }
        self.public_ip = "1.2.3.4"

        timeout = aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=10)
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            skip_auto_headers={"User-Agent"},
        )

        (
            serve_usage_page,
            info_api_handler,
            proxy_handler,
            _,
            _,
            _,
            _,
            usage_stream_api,
            usage_sse_handler,
            _,
        ) = create_handlers(
            self.cfg,
            self.token_pool,
            self.db_conn,
            self.public_ip,
            self.session,
            time.time(),
            None,
            UsageMonitor(self.token_pool),
        )

        (
            serve_admin_page,
            api_list_tokens,
            api_create_token,
            api_update_token,
            api_delete_token,
            api_tokens_usage,
        ) = create_admin_handlers(self.cfg, self.db_conn, self.token_pool)

        app = web.Application()
        app.router.add_get("/usage", serve_usage_page)
        app.router.add_post("/usage/api", info_api_handler)
        app.router.add_post("/usage/api/stream", usage_stream_api)
        app.router.add_get("/usage/events", usage_sse_handler)
        app.router.add_get("/admin", serve_admin_page)
        app.router.add_get("/admin/api/tokens", api_list_tokens)
        app.router.add_post("/admin/api/tokens", api_create_token)
        app.router.add_put("/admin/api/tokens", api_update_token)
        app.router.add_delete("/admin/api/tokens", api_delete_token)
        app.router.add_route("*", "/{path:.*}", proxy_handler)
        return app

    async def asyncTearDown(self):
        await self.session.close()

    async def test_info_api_missing_token(self):
        resp = await self.client.post("/usage/api", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["token"] == ""
        assert data["limits"] == []

    async def test_info_api_invalid_json(self):
        resp = await self.client.post(
            "/usage/api",
            data="not json",
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status == 400

    async def test_info_api_unknown_token(self):
        resp = await self.client.post("/usage/api", json={"token": "unknown"})
        assert resp.status == 404

    async def test_info_api_known_token(self):
        resp = await self.client.post("/usage/api", json={"token": _make_token_full()})
        assert resp.status == 200
        data = await resp.json()
        assert data["token"] == _make_token_full()

    async def test_usage_stream_missing_token(self):
        resp = await self.client.post("/usage/api/stream", json={})
        assert resp.status == 400

    async def test_usage_stream_unknown_token(self):
        resp = await self.client.post("/usage/api/stream", json={"token": "unknown"})
        assert resp.status == 404

    async def test_usage_stream_known_token(self):
        resp = await self.client.post(
            "/usage/api/stream", json={"token": _make_token_full()}
        )
        assert resp.status == 200
        data = await resp.json()
        assert "session_id" in data

    async def test_usage_sse_invalid_sid(self):
        resp = await self.client.get("/usage/events?sid=bogus")
        assert resp.status == 403

    async def test_proxy_forbidden_provider(self):
        token = _make_token_full()
        resp = await self.client.post(
            "/oai/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status == 403
        data = await resp.json()
        assert "not authorized" in data["error"]
        assert "oai" in data["error"]

    async def test_proxy_authorized_wildcard(self):
        token = f"{_sample_token()}00.NDt4aLrzPFb4a1A1"
        resp = await self.client.post(
            "/oai/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status != 403

    async def test_proxy_no_auth_passthrough(self):
        resp = await self.client.post(
            "/glm/v4/chat/completions",
            json={"model": "glm-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status != 403

    async def test_proxy_default_provider(self):
        token = _make_token_full()
        resp = await self.client.post(
            "/v4/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "glm-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status != 403


class TestAdminAPI(AioHTTPTestCase):
    async def get_application(self):
        self.cfg = AppConfig(
            bind_port=0,
            providers=[
                Provider("glm", "/glm", "https://open.bigmodel.cn", True),
                Provider("oai", "/oai", "https://api.openai.com/", True),
            ],
        )
        self.db_file = tempfile.mktemp(suffix=".db")
        self.db_conn, _ = init_db(self.db_file)
        self.admin_token = _make_admin_token()
        add_token(self.db_conn, self.admin_token, ["*"], is_admin=True)
        self.token_pool = {
            self.admin_token: TokenEntry(self.admin_token, ["*"], is_admin=True),
        }
        self.public_ip = "1.2.3.4"

        timeout = aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=10)
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            skip_auto_headers={"User-Agent"},
        )

        (
            serve_usage_page,
            info_api_handler,
            proxy_handler,
            _,
            _,
            _,
            _,
            _,
            _,
            _,
        ) = create_handlers(
            self.cfg,
            self.token_pool,
            self.db_conn,
            self.public_ip,
            self.session,
            time.time(),
        )
        (
            serve_admin_page,
            api_list_tokens,
            api_create_token,
            api_update_token,
            api_delete_token,
            api_tokens_usage,
        ) = create_admin_handlers(self.cfg, self.db_conn, self.token_pool)

        app = web.Application()
        app.router.add_get("/usage", serve_usage_page)
        app.router.add_post("/usage/api", info_api_handler)
        app.router.add_get("/admin", serve_admin_page)
        app.router.add_get("/admin/api/tokens", api_list_tokens)
        app.router.add_post("/admin/api/tokens", api_create_token)
        app.router.add_put("/admin/api/tokens", api_update_token)
        app.router.add_delete("/admin/api/tokens", api_delete_token)
        app.router.add_route("*", "/{path:.*}", proxy_handler)
        return app

    async def asyncTearDown(self):
        await self.session.close()
        self.db_conn.close()
        if os.path.exists(self.db_file):
            os.unlink(self.db_file)

    def _admin_headers(self):
        return {"X-Admin-Token": self.admin_token}

    async def test_admin_unauthorized(self):
        resp = await self.client.get(
            "/admin/api/tokens", headers={"X-Admin-Token": "bad"}
        )
        assert resp.status == 403

    async def test_admin_unauthorized_no_header(self):
        resp = await self.client.get("/admin/api/tokens")
        assert resp.status == 403

    async def test_admin_page_no_auth(self):
        resp = await self.client.get("/admin")
        assert resp.status == 200

    async def test_admin_list_tokens(self):
        resp = await self.client.get("/admin/api/tokens", headers=self._admin_headers())
        assert resp.status == 200
        data = await resp.json()
        assert len(data["tokens"]) >= 1
        admin = next(t for t in data["tokens"] if t["token"] == self.admin_token)
        assert admin["is_admin"] is True

    async def test_admin_create_token(self):
        resp = await self.client.post(
            "/admin/api/tokens",
            headers=self._admin_headers(),
            json={"providers": ["glm"], "is_admin": False},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["token"]["providers"] == ["glm"]
        assert data["token"]["is_admin"] is False
        assert data["token"]["enabled"] is True
        assert data["token"]["token"] in self.token_pool

    async def test_admin_update_token(self):
        create_resp = await self.client.post(
            "/admin/api/tokens",
            headers=self._admin_headers(),
            json={"providers": ["glm"]},
        )
        new_token = (await create_resp.json())["token"]["token"]

        resp = await self.client.put(
            "/admin/api/tokens",
            headers=self._admin_headers(),
            json={"token": new_token, "providers": ["oai", "glm"], "is_admin": True},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["token"]["providers"] == ["oai", "glm"]
        assert data["token"]["is_admin"] is True

    async def test_admin_toggle_enabled(self):
        create_resp = await self.client.post(
            "/admin/api/tokens",
            headers=self._admin_headers(),
            json={"providers": ["*"]},
        )
        new_token = (await create_resp.json())["token"]["token"]

        resp = await self.client.put(
            "/admin/api/tokens",
            headers=self._admin_headers(),
            json={"token": new_token, "enabled": False},
        )
        assert resp.status == 200
        assert (await resp.json())["token"]["enabled"] is False

    async def test_admin_delete_token(self):
        create_resp = await self.client.post(
            "/admin/api/tokens",
            headers=self._admin_headers(),
            json={"providers": ["*"]},
        )
        new_token = (await create_resp.json())["token"]["token"]

        resp = await self.client.delete(
            "/admin/api/tokens",
            headers=self._admin_headers(),
            json={"token": new_token},
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True


_TRACKING_HEADERS_SENT = {
    "x-session-id": "sess-1",
    "x-zcode-trace-id": "trace-1",
    "x-query-id": "query-1",
    "x-request-id": "req-1",
    "x-title": "Z Code@electron",
    "x-os-category": "linux",
    "x-os-version": "6.8.0-134-generic",
    "x-platform": "linux-x64",
    "x-client-timezone": "Asia/Shanghai",
    "x-client-language": "en-US",
    "x-release-channel": "production",
    "x-zcode-app-version": "3.9.1",
    "x-zcode-agent": "glm",
    "x-zcode-session-type": "main",
    "http-referer": "https://zcode.z.ai",
    "x-stainless-os": "Linux",
    "x-stainless-runtime-version": "3.12",
    "X-Forwarded-For": "10.0.0.1",
    "X-Real-IP": "10.0.0.1",
}

_TRACKING_BODY = {
    "model": "glm-4",
    "metadata": {"user_id": '{"device_id":"d-1","session_id":"sess-1"}'},
    "user": "u-1",
    "messages": [{"role": "user", "content": "hi"}],
}


class TestPrivacyUnits(unittest.TestCase):
    def test_apply_privacy_headers(self):
        headers = {
            "Host": "upstream",
            "User-Agent": "ZCode/3.9.1 ai-sdk runtime/node.js/22",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "sec-fetch-mode": "cors",
            "Authorization": "Bearer real",
            **_TRACKING_HEADERS_SENT,
        }
        out = _apply_privacy_headers(headers, "OneGate")
        assert set(out) == {
            "Host",
            "User-Agent",
            "anthropic-version",
            "Content-Type",
            "sec-fetch-mode",
            "Authorization",
        }
        assert out["User-Agent"] == "OneGate"
        assert out["Authorization"] == "Bearer real"

    def test_apply_privacy_headers_keeps_ua_when_override_empty(self):
        out = _apply_privacy_headers({"User-Agent": "ZCode/1", "x-title": "t"}, "")
        assert out == {"User-Agent": "ZCode/1"}

    def test_strip_tracking_body(self):
        out = json.loads(_strip_tracking_body(json.dumps(_TRACKING_BODY).encode()))
        assert "metadata" not in out
        assert "user" not in out
        assert out["model"] == "glm-4"
        assert out["messages"] == [{"role": "user", "content": "hi"}]

    def test_strip_tracking_body_passthrough(self):
        assert _strip_tracking_body(None) is None
        raw = b"not json"
        assert _strip_tracking_body(raw) is raw
        clean = json.dumps({"model": "glm-4"}).encode()
        assert _strip_tracking_body(clean) is clean

    def test_load_config_privacy(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write('[privacy]\nstrip_tracking = false\nua_override = "X"\n')
            cfg = load_config(d)
            assert cfg.privacy_strip_tracking is False
            assert cfg.privacy_ua_override == "X"
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            cfg = load_config(d)
            assert cfg.privacy_strip_tracking is True
            assert cfg.privacy_ua_override == ""


class TestRequestWantsStream(unittest.TestCase):
    def test_literal_true(self):
        assert _request_wants_stream(b'{"model":"m","stream":true}') is True

    def test_stream_false(self):
        assert _request_wants_stream(b'{"model":"m","stream":false}') is False

    def test_stream_string_is_not_truthy(self):
        assert _request_wants_stream(b'{"model":"m","stream":"true"}') is False

    def test_missing_stream(self):
        assert _request_wants_stream(b'{"model":"m"}') is False

    def test_empty_body(self):
        assert _request_wants_stream(None) is False
        assert _request_wants_stream(b"") is False

    def test_invalid_json(self):
        assert _request_wants_stream(b"not json") is False

    def test_non_object_json(self):
        assert _request_wants_stream(b'["stream", true]') is False
        assert _request_wants_stream(b"true") is False


class TestPrivacyProxyE2E(AioHTTPTestCase):
    async def get_application(self):
        self.captured = {}

        async def upstream_handler(request):
            self.captured["headers"] = dict(request.headers)
            self.captured["body"] = await request.read()
            return web.json_response({"ok": True})

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/messages", upstream_handler)
        self.upstream_runner = web.AppRunner(upstream_app)
        await self.upstream_runner.setup()
        site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await site.start()
        port = self.upstream_runner.addresses[0][1]

        self.cfg = AppConfig(
            bind_port=0,
            usage_db=":memory:",
            providers=[Provider("mock", "/", f"http://127.0.0.1:{port}", False)],
            real_tokens={"mock": "real-key"},
        )
        self.db_conn, _ = init_db(":memory:")
        self.token = _make_token_full()
        add_token(self.db_conn, self.token, ["*"])
        self.token_pool = {self.token: TokenEntry(self.token, ["*"])}
        self.public_ip = "1.2.3.4"

        timeout = aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=10)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            skip_auto_headers={"User-Agent"},
        )

        (_, _, proxy_handler, _, _, _, _, _, _, _) = create_handlers(
            self.cfg,
            self.token_pool,
            self.db_conn,
            self.public_ip,
            self.session,
            time.time(),
            None,
            UsageMonitor(self.token_pool),
        )

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", proxy_handler)
        return app

    async def asyncTearDown(self):
        await self.session.close()
        await self.upstream_runner.cleanup()

    async def _post(self):
        return await self.client.post(
            "/api/messages",
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "ZCode/3.9.1 ai-sdk runtime/node.js/22",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "sec-fetch-mode": "cors",
                **_TRACKING_HEADERS_SENT,
            },
            json=_TRACKING_BODY,
        )

    async def test_privacy_strip_on(self):
        resp = await self._post()
        assert resp.status == 200
        h = {k.lower(): v for k, v in self.captured["headers"].items()}
        assert h.get("user-agent", "").startswith("ZCode/")
        assert h.get("authorization") == "Bearer real-key"
        assert h.get("sec-fetch-mode") == "cors"
        assert h.get("anthropic-version") == "2023-06-01"
        for banned in (k.lower() for k in _TRACKING_HEADERS_SENT):
            assert banned not in h
        body = json.loads(self.captured["body"])
        assert "metadata" not in body
        assert "user" not in body
        assert body["model"] == "glm-4"

    async def test_privacy_strip_off(self):
        self.cfg.privacy_strip_tracking = False
        resp = await self._post()
        assert resp.status == 200
        h = {k.lower(): v for k, v in self.captured["headers"].items()}
        assert h.get("x-session-id") == "sess-1"
        assert h.get("user-agent", "").startswith("ZCode/")
        assert h.get("x-forwarded-for") == "10.0.0.1, 1.2.3.4"
        assert h.get("x-real-ip") == "1.2.3.4"
        body = json.loads(self.captured["body"])
        assert body["metadata"]["user_id"].startswith('{"device_id"')
        assert body["user"] == "u-1"


class TestStreamAcceptEncodingProxy(AioHTTPTestCase):
    async def get_application(self):
        self.captured = {}

        async def upstream_handler(request):
            self.captured["headers"] = dict(request.headers)
            self.captured["body"] = await request.read()
            return web.json_response({"ok": True})

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/chat", upstream_handler)
        self.upstream_runner = web.AppRunner(upstream_app)
        await self.upstream_runner.setup()
        site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await site.start()
        port = self.upstream_runner.addresses[0][1]

        self.cfg = AppConfig(
            bind_port=0,
            usage_db=":memory:",
            providers=[Provider("mock", "/", f"http://127.0.0.1:{port}", False)],
            real_tokens={"mock": "real-key"},
        )
        self.db_conn, _ = init_db(":memory:")
        self.token = _make_token_full()
        add_token(self.db_conn, self.token, ["*"])
        self.token_pool = {self.token: TokenEntry(self.token, ["*"])}
        self.public_ip = "1.2.3.4"

        timeout = aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=10)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            skip_auto_headers={"User-Agent"},
        )

        (_, _, proxy_handler, _, _, _, _, _, _, _) = create_handlers(
            self.cfg,
            self.token_pool,
            self.db_conn,
            self.public_ip,
            self.session,
            time.time(),
        )

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", proxy_handler)
        return app

    async def asyncTearDown(self):
        await self.session.close()
        await self.upstream_runner.cleanup()

    async def _post(self, payload):
        return await self.client.post(
            "/api/chat",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept-Encoding": "gzip",
            },
            json=payload,
        )

    async def test_stream_request_forces_identity_encoding(self):
        resp = await self._post(
            {
                "model": "deepseek-reasoner",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        assert resp.status == 200
        h = {k.lower(): v for k, v in self.captured["headers"].items()}
        assert h.get("accept-encoding") == "identity"

    async def test_non_stream_request_preserves_accept_encoding(self):
        resp = await self._post(
            {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert resp.status == 200
        h = {k.lower(): v for k, v in self.captured["headers"].items()}
        assert h.get("accept-encoding") == "gzip"


class TestStreamKeepaliveProxy(AioHTTPTestCase):
    async def get_application(self):
        async def upstream_handler(request):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await resp.prepare(request)
            try:
                await resp.write(b"data: hello\n\n")
            except ConnectionResetError:
                return resp
            await asyncio.sleep(2)
            try:
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
            except ConnectionResetError:
                pass
            return resp

        upstream_app = web.Application()
        upstream_app.router.add_post("/api/chat", upstream_handler)
        self.upstream_runner = web.AppRunner(upstream_app)
        await self.upstream_runner.setup()
        site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await site.start()
        port = self.upstream_runner.addresses[0][1]

        self.cfg = AppConfig(
            bind_port=0,
            usage_db=":memory:",
            providers=[Provider("mock", "/", f"http://127.0.0.1:{port}", False)],
            real_tokens={"mock": "real-key"},
            timeout_stream_heartbeat=0.3,
            timeout_stream_idle=0.9,
        )
        self.db_conn, _ = init_db(":memory:")
        self.token = _make_token_full()
        add_token(self.db_conn, self.token, ["*"])
        self.token_pool = {self.token: TokenEntry(self.token, ["*"])}
        self.public_ip = "1.2.3.4"

        timeout = aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=10)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            skip_auto_headers={"User-Agent"},
        )

        (_, _, proxy_handler, _, _, _, _, _, _, _) = create_handlers(
            self.cfg,
            self.token_pool,
            self.db_conn,
            self.public_ip,
            self.session,
            time.time(),
        )

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", proxy_handler)
        return app

    async def asyncTearDown(self):
        await self.session.close()
        await self.upstream_runner.cleanup()

    async def test_sse_idle_stream_gets_pings_and_closes(self):
        async def collect():
            resp = await self.client.post(
                "/api/chat",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status == 200
            assert "text/event-stream" in resp.headers.get("Content-Type", "")
            chunks = []
            async for chunk in resp.content.iter_any():
                chunks.append(chunk)
            return b"".join(chunks)

        started = time.monotonic()
        body = await asyncio.wait_for(collect(), timeout=5)
        elapsed = time.monotonic() - started

        assert b"data: hello" in body
        assert body.count(b": ping\n\n") >= 1
        assert b"data: [DONE]" not in body
        assert elapsed < 1.9


class TestV1TargetUrl(unittest.TestCase):
    def _big_provider(self) -> Provider:
        return Provider(
            "big",
            "/",
            "https://open.bigmodel.cn",
            False,
            v1_base="https://open.bigmodel.cn/api/paas/v4",
        )

    def test_v1_base_used(self):
        provider = self._big_provider()
        assert (
            _v1_target_url(provider, "/v1/chat/completions", 0)
            == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        )

    def test_upstream_without_v1_appends_v1(self):
        provider = Provider("kimi", "/kimi", "https://api.kimi.com", True)
        assert (
            _v1_target_url(provider, "/v1/chat/completions", 0)
            == "https://api.kimi.com/v1/chat/completions"
        )

    def test_upstream_ending_with_v1_not_duplicated(self):
        provider = Provider("x", "/x", "https://x.example/v1", True)
        assert (
            _v1_target_url(provider, "/v1/chat/completions", 0)
            == "https://x.example/v1/chat/completions"
        )

    def test_litellm_prefix_same_result(self):
        provider = self._big_provider()
        assert (
            _v1_target_url(provider, "/litellm/v1/chat/completions", 0)
            == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        )

    def test_v1_base_trailing_slash(self):
        provider = Provider(
            "big",
            "/",
            "https://open.bigmodel.cn",
            False,
            v1_base="https://open.bigmodel.cn/api/paas/v4/",
        )
        assert (
            _v1_target_url(provider, "/v1/chat/completions", 0)
            == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        )


class TestV1ModelMapping(unittest.TestCase):
    def setUp(self):
        self.conn, _ = init_db(":memory:")
        self.cfg = AppConfig(
            providers=[
                Provider("first", "/fx", "https://first.example", True),
                Provider(
                    "default",
                    "/",
                    "https://open.bigmodel.cn",
                    False,
                    v1_base="https://open.bigmodel.cn/api/paas/v4",
                ),
                Provider("ds", "/ds", "https://gpt.gjzq.cn/prod-api", True),
            ]
        )
        self.first = self.cfg.providers[0]
        self.big = self.cfg.providers[1]
        self.ds = self.cfg.providers[2]
        self.token = _make_token_full()
        add_token(self.conn, self.token, ["*"])

    def tearDown(self):
        self.conn.close()

    def test_token_model_map_hit(self):
        set_model_map(self.conn, self.token, "gpt-4", "glm-4-plus", "ds")
        mapped, provider = _resolve_v1_target(self.conn, self.cfg, self.token, "gpt-4")
        assert mapped == "glm-4-plus"
        assert provider is self.ds

    def test_site_model_pattern_fallback(self):
        set_site_model(self.conn, "glm-*", "default", "glm-5.3")
        mapped, provider = _resolve_v1_target(
            self.conn, self.cfg, self.token, "glm-5.2"
        )
        assert mapped == "glm-5.3"
        assert provider is self.big

    def test_no_match_falls_back_to_default_provider(self):
        mapped, provider = _resolve_v1_target(
            self.conn, self.cfg, self.token, "totally-unknown"
        )
        assert mapped == "totally-unknown"
        assert provider is self.big
        assert provider is not self.first


if __name__ == "__main__":
    import unittest

    unittest.main()
