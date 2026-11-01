import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ssl
import time
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from src.config import AppConfig
from src.db import add_token, init_db, record_status
from src.handlers import create_handlers
from src.models import Provider, TokenEntry
from src.usage_monitor import UsageMonitor


def _sample_token() -> str:
    return "aaa111bbb222ccc333ddd444eee555ff"


def _make_token(suffix: str) -> str:
    return f"{_sample_token()}.{suffix}"


WILDCARD_TOKEN = _make_token("testtoken0000000")
RESTRICTED_TOKEN = _make_token("restricted000000")
DISABLED_TOKEN = _make_token("disabled00000000")


class TestStatusAuth(AioHTTPTestCase):
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
                Provider("glm2", "/glm2", "https://open.bigmodel.cn", True),
                Provider("oai", "/oai", "https://api.openai.com/", True),
            ],
            real_tokens={
                "glm": "real-glm-key",
                "glm2": "real-glm-key",
                "oai": "real-oai-key",
            },
        )
        self.db_conn, _ = init_db(":memory:")

        add_token(self.db_conn, WILDCARD_TOKEN, ["*"])
        add_token(self.db_conn, RESTRICTED_TOKEN, ["glm"])

        self.token_pool = {
            WILDCARD_TOKEN: TokenEntry(WILDCARD_TOKEN, ["*"]),
            RESTRICTED_TOKEN: TokenEntry(RESTRICTED_TOKEN, ["glm"]),
            DISABLED_TOKEN: TokenEntry(DISABLED_TOKEN, ["*"], enabled=False),
        }

        record_status(self.db_conn, "glm", 200)
        record_status(self.db_conn, "glm2", 500)
        record_status(self.db_conn, "oai", 200)

        self.public_ip = "1.2.3.4"

        timeout = aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=10)
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            skip_auto_headers={"User-Agent"},
        )

        (
            _,
            _,
            _,
            _,
            status_api_handler,
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
            None,
            UsageMonitor(self.token_pool),
        )

        app = web.Application()
        app.router.add_get("/status/api", status_api_handler)
        return app

    async def asyncTearDown(self):
        await self.session.close()

    async def test_missing_header(self):
        resp = await self.client.get("/status/api")
        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "missing X-Proxy-Token"

    async def test_garbage_token(self):
        resp = await self.client.get(
            "/status/api", headers={"X-Proxy-Token": "garbage"}
        )
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "invalid or disabled token"

    async def test_disabled_token(self):
        resp = await self.client.get(
            "/status/api", headers={"X-Proxy-Token": DISABLED_TOKEN}
        )
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "invalid or disabled token"

    async def test_wildcard_sees_all_groups(self):
        resp = await self.client.get(
            "/status/api", headers={"X-Proxy-Token": WILDCARD_TOKEN}
        )
        assert resp.status == 200
        data = await resp.json()
        assert "uptime" in data
        names = [p["name"] for p in data["providers"]]
        assert names == ["glm", "oai"]
        glm_group = next(p for p in data["providers"] if p["name"] == "glm")
        assert glm_group["prefix"] == "/glm, /glm2"
        assert set(data["stats"]) == {"glm", "oai"}
        assert set(data["summary"]) == {"glm", "oai"}
        assert data["stats"]["glm"][-1]["total"] == 2
        assert data["stats"]["glm"][-1]["success"] == 1

    async def test_restricted_token_only_sees_glm(self):
        resp = await self.client.get(
            "/status/api", headers={"X-Proxy-Token": RESTRICTED_TOKEN}
        )
        assert resp.status == 200
        data = await resp.json()
        assert [p["name"] for p in data["providers"]] == ["glm"]
        assert data["providers"][0]["prefix"] == "/glm"
        assert set(data["stats"]) == {"glm"}
        assert set(data["summary"]) == {"glm"}
        assert data["stats"]["glm"][-1]["total"] == 1
        assert "oai" not in data["stats"]
        assert "oai" not in data["summary"]
        assert "glm2" not in data["stats"]
        assert "glm2" not in data["summary"]

    async def test_restricted_token_cannot_see_other_summary_periods(self):
        resp = await self.client.get(
            "/status/api", headers={"X-Proxy-Token": RESTRICTED_TOKEN}
        )
        assert resp.status == 200
        data = await resp.json()
        assert list(data["summary"]) == ["glm"]
        assert data["summary"]["glm"]["24h"]["total"] == 1


if __name__ == "__main__":
    unittest.main()
