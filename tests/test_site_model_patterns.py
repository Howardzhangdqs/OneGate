import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from src.config import AppConfig
from src.db import (
    add_token,
    init_db,
    is_site_model_pattern,
    resolve_site_model_exact,
    resolve_site_model_pattern,
    set_site_model,
)
from src.models import Provider
from src.pool import load_pool_from_db
from src.model_catalog import create_catalog_handlers

ALL_TOKEN = f"{'a' * 32}.AAAAAAAAAAAAAAAA"
ADMIN_TOKEN = f"{'d' * 32}.DDDDDDDDDDDDDDDD"


def _conn() -> sqlite3.Connection:
    conn, _ = init_db(":memory:", admin_tokens=[ADMIN_TOKEN], pool_count=0)
    return conn


class TestIsSiteModelPattern(unittest.TestCase):
    def test_pattern_ids(self):
        for model in ("glm-*", "^a.+$", "*sonnet*", "[a-z]+", "a|b", "a\\b", "x?"):
            assert is_site_model_pattern(model) is True, model

    def test_concrete_ids(self):
        for model in (
            "gpt-4.1",
            "claude-3-5-sonnet",
            "openai/gpt-4o",
            "gpt-4o-mini",
            "deepseek-v4-flash",
        ):
            assert is_site_model_pattern(model) is False, model


class TestSiteModelPatternResolution(unittest.TestCase):
    def test_exact_beats_pattern(self):
        conn = _conn()
        try:
            set_site_model(conn, "vendor-special", "glm", "exact-target")
            set_site_model(conn, "^vendor-(.+)$", "oai", "re-$1")
            row = resolve_site_model_pattern(conn, "vendor-special")
            assert row["model"] == "vendor-special"
            assert row["provider"] == "glm"
            assert row["target"] == "exact-target"
        finally:
            conn.close()

    def test_fnmatch_uses_target_verbatim(self):
        conn = _conn()
        try:
            set_site_model(conn, "*sonnet*", "oai", "some-upstream-model")
            row = resolve_site_model_pattern(conn, "claude-3-5-sonnet")
            assert row["model"] == "*sonnet*"
            assert row["provider"] == "oai"
            assert row["target"] == "some-upstream-model"
        finally:
            conn.close()

    def test_fnmatch_empty_target_falls_back_to_requested(self):
        conn = _conn()
        try:
            set_site_model(conn, "glm-*", "glm", "")
            row = resolve_site_model_pattern(conn, "glm-4.5")
            assert row["model"] == "glm-*"
            assert row["provider"] == "glm"
            assert row["target"] == "glm-4.5"
        finally:
            conn.close()

    def test_regex_capture_substitution(self):
        conn = _conn()
        try:
            set_site_model(conn, "^vendor-(.+)$", "oai", "real-$1")
            row = resolve_site_model_pattern(conn, "vendor-foo")
            assert row["model"] == "^vendor-(.+)$"
            assert row["provider"] == "oai"
            assert row["target"] == "real-foo"
            assert resolve_site_model_pattern(conn, "other-foo") is None
        finally:
            conn.close()

    def test_invalid_regex_falls_back_to_fnmatch(self):
        conn = _conn()
        try:
            set_site_model(conn, "*foo(", "oai", "wild-target")
            row = resolve_site_model_pattern(conn, "xfoo(")
            assert row["model"] == "*foo("
            assert row["provider"] == "oai"
            assert row["target"] == "wild-target"
            assert resolve_site_model_pattern(conn, "xbar(") is None
        finally:
            conn.close()

    def test_disabled_pattern_ignored(self):
        conn = _conn()
        try:
            set_site_model(conn, "*sonnet*", "oai", "upstream", enabled=False)
            assert resolve_site_model_pattern(conn, "claude-3-5-sonnet") is None
            assert resolve_site_model_exact(conn, "*sonnet*") is None
        finally:
            conn.close()

    def test_more_specific_pattern_wins(self):
        conn = _conn()
        try:
            set_site_model(conn, "*sonnet*", "glm", "generic")
            set_site_model(conn, "claude-3-5-*", "oai", "specific")
            row = resolve_site_model_pattern(conn, "claude-3-5-sonnet")
            assert row["model"] == "claude-3-5-*"
            assert row["target"] == "specific"
        finally:
            conn.close()

    def test_concrete_row_not_used_as_pattern(self):
        conn = _conn()
        try:
            set_site_model(conn, "gpt-4.1", "oai", "up")
            assert resolve_site_model_pattern(conn, "gpt-4.x") is None
        finally:
            conn.close()


class TestV1SiteModelPatterns(AioHTTPTestCase):
    async def get_application(self):
        self.cfg = AppConfig(
            bind_port=0,
            providers=[
                Provider("glm", "/glm", "https://glm.example", True),
                Provider("oai", "/oai", "https://oai.example", True),
            ],
            real_tokens={"glm": "glm-key", "oai": "oai-key"},
        )
        self.db_conn, _ = init_db(":memory:", admin_tokens=[ADMIN_TOKEN], pool_count=0)
        add_token(self.db_conn, ALL_TOKEN, ["*"], models=["*"])

        set_site_model(self.db_conn, "glm-model", "glm", "upstream-glm")
        set_site_model(self.db_conn, "glm-*", "glm", "")
        set_site_model(self.db_conn, "*sonnet*", "glm", "sonnet-upstream")
        set_site_model(self.db_conn, "^claude-(.+)$", "oai", "upstream-$1")

        self.token_pool = load_pool_from_db(self.db_conn)

        v1_list_models, v1_get_model = create_catalog_handlers(
            self.cfg, self.token_pool, self.db_conn
        )

        app = web.Application()
        app.router.add_get("/v1/models", v1_list_models)
        app.router.add_get("/v1/models/{model}", v1_get_model)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.db_conn.close()

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {ALL_TOKEN}"}

    async def test_list_models_excludes_pattern_rules(self):
        resp = await self.client.get("/v1/models", headers=self._auth())
        assert resp.status == 200
        data = await resp.json()
        assert {m["id"] for m in data["data"]} == {"glm-model"}

    async def test_get_model_404_for_name_matching_rule(self):
        resp = await self.client.get(
            "/v1/models/claude-3-5-sonnet", headers=self._auth()
        )
        assert resp.status == 404

        resp = await self.client.get("/v1/models/glm-4.5", headers=self._auth())
        assert resp.status == 404

    async def test_get_model_still_serves_concrete_id(self):
        resp = await self.client.get("/v1/models/glm-model", headers=self._auth())
        assert resp.status == 200
        data = await resp.json()
        assert data["id"] == "glm-model"
        assert data["owned_by"] == "glm"


if __name__ == "__main__":
    unittest.main()
