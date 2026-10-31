import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import sqlite3
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from src.admin import create_admin_handlers, create_site_model_handlers
from src.config import AppConfig
from src.db import (
    add_token,
    delete_site_model,
    get_site_models,
    get_token,
    init_db,
    resolve_model,
    resolve_model_ex,
    resolve_site_model,
    set_model_map,
    set_site_model,
    update_token,
)
from src.models import Provider
from src.pool import load_pool_from_db
from src.v1_handler import _resolve_v1_mapping, create_v1_handlers

ALL_TOKEN = f"{'a' * 32}.AAAAAAAAAAAAAAAA"
LIMITED_TOKEN = f"{'b' * 32}.BBBBBBBBBBBBBBBB"
PROVIDER_TOKEN = f"{'c' * 32}.CCCCCCCCCCCCCCCC"
ADMIN_TOKEN = f"{'d' * 32}.DDDDDDDDDDDDDDDD"


def _conn() -> sqlite3.Connection:
    conn, _ = init_db(":memory:", admin_tokens=[ADMIN_TOKEN], pool_count=0)
    return conn


class TestSiteModelDB(unittest.TestCase):
    def test_migration_defaults_models_to_wildcard(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            old = sqlite3.connect(path)
            old.execute(
                "CREATE TABLE token_pool ("
                "token TEXT PRIMARY KEY,"
                "providers TEXT NOT NULL DEFAULT '[\"*\"]',"
                "is_admin INTEGER NOT NULL DEFAULT 0,"
                "enabled INTEGER NOT NULL DEFAULT 1,"
                "created_at INTEGER NOT NULL,"
                "describe TEXT NOT NULL DEFAULT ''"
                ")"
            )
            old.execute(
                "INSERT INTO token_pool "
                "(token, providers, is_admin, enabled, created_at, describe) "
                "VALUES (?, ?, 0, 1, 1000, '')",
                ("legacy-token", '["*"]'),
            )
            old.commit()
            old.close()

            conn, _ = init_db(path, admin_tokens=[ADMIN_TOKEN], pool_count=0)
            try:
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(token_pool)").fetchall()
                }
                assert "models" in cols
                entry = get_token(conn, "legacy-token")
                assert entry is not None
                assert entry.models == ["*"]
                assert entry.has_model_permission("any-model") is True
            finally:
                conn.close()

    def test_explicit_allowlist_persists_through_add_update_rename(self):
        conn = _conn()
        try:
            add_token(conn, "tok.one", ["*"], models=["public-a", "vendor-*"])
            assert get_token(conn, "tok.one").models == ["public-a", "vendor-*"]

            update_token(conn, "tok.one", models=["public-b"])
            assert get_token(conn, "tok.one").models == ["public-b"]

            update_token(conn, "tok.one", new_token="tok.two")
            assert get_token(conn, "tok.one") is None
            assert get_token(conn, "tok.two").models == ["public-b"]
        finally:
            conn.close()

    def test_token_mapping_beats_site_mapping(self):
        conn = _conn()
        try:
            set_model_map(conn, "tok.one", "gpt-*", "token-target", "glm")
            set_site_model(conn, "gpt-4", "oai", "site-target")

            target, provider, matched = resolve_model_ex(conn, "tok.one", "gpt-4")
            assert (target, provider, matched) == ("token-target", "glm", True)

            target, provider = resolve_model(conn, "tok.one", "gpt-4")
            assert (target, provider) == ("token-target", "glm")
        finally:
            conn.close()

    def test_site_mapping_used_when_no_token_rule(self):
        conn = _conn()
        try:
            set_site_model(conn, "gpt-4", "oai", "site-target")
            target, provider, matched = resolve_model_ex(conn, "tok.other", "gpt-4")
            assert (target, provider, matched) == ("gpt-4", "", False)

            row = resolve_site_model(conn, "gpt-4")
            assert row is not None
            assert row["provider"] == "oai"
            assert row["target"] == "site-target"

            cfg = AppConfig(
                providers=[
                    Provider("glm", "/glm", "https://glm.example", True),
                    Provider("oai", "/oai", "https://oai.example", True),
                ]
            )
            mapped, target_provider, original, token_matched = _resolve_v1_mapping(
                conn, cfg, "tok.other", "gpt-4"
            )
            assert mapped == "site-target"
            assert target_provider.name == "oai"
            assert original == "gpt-4"
            assert token_matched is False
        finally:
            conn.close()

    def test_site_model_filtering_and_delete(self):
        conn = _conn()
        try:
            set_site_model(conn, "b-model", "glm", "", True)
            set_site_model(conn, "a-model", "oai", "up", False)

            models = get_site_models(conn)
            assert [m["model"] for m in models] == ["a-model", "b-model"]

            enabled = get_site_models(conn, enabled_only=True)
            assert [m["model"] for m in enabled] == ["b-model"]

            assert resolve_site_model(conn, "a-model") is None
            assert delete_site_model(conn, "a-model") is True
            assert delete_site_model(conn, "a-model") is False
        finally:
            conn.close()


class TestV1ModelsEndpoint(AioHTTPTestCase):
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
        add_token(
            self.db_conn,
            LIMITED_TOKEN,
            ["*"],
            models=["glm-model", "shared-model"],
        )
        add_token(self.db_conn, PROVIDER_TOKEN, ["glm"], models=["*"])

        set_site_model(self.db_conn, "glm-model", "glm", "upstream-glm")
        set_site_model(self.db_conn, "shared-model", "oai", "upstream-oai")
        set_site_model(self.db_conn, "oai-model", "oai", "upstream-oai-2")

        self.token_pool = load_pool_from_db(self.db_conn)

        (
            _,
            _,
            v1_list_models,
            v1_get_model,
        ) = create_v1_handlers(self.cfg, self.token_pool, self.db_conn)
        (
            _,
            admin_list_models,
            admin_set_model,
            admin_delete_model,
        ) = create_site_model_handlers(self.cfg, self.db_conn)
        (
            _,
            _,
            admin_create_token,
            admin_update_token,
            _,
            _,
        ) = create_admin_handlers(self.cfg, self.db_conn, self.token_pool)

        app = web.Application()
        app.router.add_get("/v1/models", v1_list_models)
        app.router.add_get("/v1/models/{model}", v1_get_model)
        app.router.add_get("/admin/api/models", admin_list_models)
        app.router.add_post("/admin/api/models", admin_set_model)
        app.router.add_delete("/admin/api/models", admin_delete_model)
        app.router.add_post("/admin/api/tokens", admin_create_token)
        app.router.add_put("/admin/api/tokens", admin_update_token)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.db_conn.close()

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _admin(self) -> dict:
        return {"X-Admin-Token": ADMIN_TOKEN}

    async def test_list_models_requires_auth(self):
        resp = await self.client.get("/v1/models")
        assert resp.status == 401

        resp = await self.client.get(
            "/v1/models", headers={"Authorization": "Bearer bogus"}
        )
        assert resp.status == 401

    async def test_list_models_wildcard_returns_all(self):
        resp = await self.client.get("/v1/models", headers=self._auth(ALL_TOKEN))
        assert resp.status == 200
        data = await resp.json()
        assert data["object"] == "list"
        assert {m["id"] for m in data["data"]} == {
            "glm-model",
            "shared-model",
            "oai-model",
        }
        for model in data["data"]:
            assert model["object"] == "model"
            assert model["created"] == 0
            assert model["owned_by"] in {"glm", "oai"}

    async def test_list_models_filters_by_model_allowlist(self):
        resp = await self.client.get("/v1/models", headers=self._auth(LIMITED_TOKEN))
        assert resp.status == 200
        data = await resp.json()
        assert {m["id"] for m in data["data"]} == {"glm-model", "shared-model"}

    async def test_list_models_filters_by_provider_permission(self):
        resp = await self.client.get("/v1/models", headers=self._auth(PROVIDER_TOKEN))
        assert resp.status == 200
        data = await resp.json()
        assert {m["id"] for m in data["data"]} == {"glm-model"}

    async def test_get_model_visible(self):
        resp = await self.client.get(
            "/v1/models/glm-model", headers=self._auth(LIMITED_TOKEN)
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["id"] == "glm-model"
        assert data["owned_by"] == "glm"

    async def test_get_model_invisible_returns_404(self):
        resp = await self.client.get(
            "/v1/models/oai-model", headers=self._auth(LIMITED_TOKEN)
        )
        assert resp.status == 404
        data = await resp.json()
        assert data["error"] == "model 'oai-model' not found"

    async def test_get_model_provider_invisible_returns_404(self):
        resp = await self.client.get(
            "/v1/models/shared-model", headers=self._auth(PROVIDER_TOKEN)
        )
        assert resp.status == 404

    async def test_get_model_unknown_returns_404(self):
        resp = await self.client.get(
            "/v1/models/missing-model", headers=self._auth(ALL_TOKEN)
        )
        assert resp.status == 404

    async def test_admin_models_requires_admin(self):
        resp = await self.client.get("/admin/api/models")
        assert resp.status == 403

        resp = await self.client.get(
            "/admin/api/models", headers={"X-Admin-Token": "bad"}
        )
        assert resp.status == 403

    async def test_admin_models_crud(self):
        resp = await self.client.post(
            "/admin/api/models",
            headers=self._admin(),
            json={"model": "new-model", "provider": "glm", "target": "up"},
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

        resp = await self.client.get("/admin/api/models", headers=self._admin())
        assert resp.status == 200
        models = (await resp.json())["models"]
        assert any(m["model"] == "new-model" for m in models)

        resp = await self.client.delete(
            "/admin/api/models",
            headers=self._admin(),
            json={"model": "new-model"},
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

        resp = await self.client.delete(
            "/admin/api/models",
            headers=self._admin(),
            json={"model": "new-model"},
        )
        assert resp.status == 404

    async def test_admin_model_unknown_provider_returns_400(self):
        resp = await self.client.post(
            "/admin/api/models",
            headers=self._admin(),
            json={"model": "x-model", "provider": "nope"},
        )
        assert resp.status == 400

    async def test_admin_token_models_create_and_update(self):
        resp = await self.client.post(
            "/admin/api/tokens",
            headers=self._admin(),
            json={"providers": ["*"], "models": ["m1", "m2"]},
        )
        assert resp.status == 200
        data = await resp.json()
        token = data["token"]["token"]
        assert data["token"]["models"] == ["m1", "m2"]

        resp = await self.client.put(
            "/admin/api/tokens",
            headers=self._admin(),
            json={"token": token, "models": ["m3"]},
        )
        assert resp.status == 200
        assert (await resp.json())["token"]["models"] == ["m3"]

    async def test_admin_token_models_validation(self):
        resp = await self.client.post(
            "/admin/api/tokens",
            headers=self._admin(),
            json={"providers": ["*"], "models": "nope"},
        )
        assert resp.status == 400

        resp = await self.client.post(
            "/admin/api/tokens",
            headers=self._admin(),
            json={"providers": ["*"], "models": [""]},
        )
        assert resp.status == 400


if __name__ == "__main__":
    unittest.main()
