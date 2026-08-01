import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import ssl
import tempfile
import time

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from src.admin import create_admin_handlers
from src.config import AppConfig
from src.db import add_token, init_db
from src.handlers import create_handlers
from src.models import Provider, TokenEntry


def _sample_token() -> str:
    return "aaa111bbb222ccc333ddd444eee555ff"


def _make_token_full() -> str:
    return f"{_sample_token()}.Zk1pGnRvtztoeAb1"


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

        serve_usage_page, info_api_handler, proxy_handler, _, _ = create_handlers(
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

    async def test_info_api_missing_token(self):
        resp = await self.client.post("/usage/api", json={})
        assert resp.status == 400

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

        serve_usage_page, info_api_handler, proxy_handler, _, _ = create_handlers(
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


if __name__ == "__main__":
    import unittest

    unittest.main()
