import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os
import tempfile

from src.config import load_config, match_provider
from src.db import (
    add_token,
    delete_token,
    get_all_tokens,
    get_token,
    init_db,
    is_admin_token,
    record_usage,
    update_token,
)
from src.models import Provider, TokenEntry
from src.pool import extract_token, load_pool_from_db, make_token, resolve_token


def _sample_token() -> str:
    return "aaa111bbb222ccc333ddd444eee555ff"


def _make_token_full() -> str:
    return f"{_sample_token()}.Zk1pGnRvtztoeAb1"


def test_provider_model():
    p = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    assert p.name == "glm"
    assert p.prefix == "/glm"
    assert p.upstream == "https://open.bigmodel.cn"
    assert p.host == "open.bigmodel.cn"
    assert p.origin == "https://open.bigmodel.cn"
    assert p.env_token == "TOKEN_GLM"


def test_provider_custom_env_token():
    p = Provider(
        "glm", "/glm", "https://open.bigmodel.cn", True, env_token="REAL_TOKEN"
    )
    assert p.env_token == "REAL_TOKEN"


def test_provider_strip_trailing_slash():
    p = Provider("oai", "/oai", "https://api.openai.com/", True)
    assert p.upstream == "https://api.openai.com"
    assert p.host == "api.openai.com"
    assert p.origin == "https://api.openai.com"


def test_token_entry_permission_wildcard():
    entry = TokenEntry("abc.def", ["*"])
    p = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    assert entry.has_permission(p) is True


def test_token_entry_permission_specific():
    entry = TokenEntry("abc.def", ["glm", "default"])
    p1 = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    p2 = Provider("oai", "/oai", "https://api.openai.com/", True)
    assert entry.has_permission(p1) is True
    assert entry.has_permission(p2) is False


def test_token_entry_disabled():
    entry = TokenEntry("abc.def", ["*"], enabled=False)
    p = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    assert entry.has_permission(p) is False


def test_token_entry_to_from_db():
    entry = TokenEntry("abc.def", ["glm"], is_admin=True, enabled=True, created_at=1000)
    row = entry.to_db_row()
    assert row[0] == "abc.def"
    assert json.loads(row[1]) == ["glm"]
    assert row[2] == 1
    assert row[3] == 1
    assert row[4] == 1000

    restored = TokenEntry.from_db_row(row)
    assert restored.token == "abc.def"
    assert restored.providers == ["glm"]
    assert restored.is_admin is True
    assert restored.enabled is True
    assert restored.created_at == 1000


def test_match_provider_exact_prefix():
    providers = [
        Provider("oai", "/oai", "https://api.openai.com/", True),
        Provider("claude", "/claude", "https://api.anthropic.com/", True),
        Provider("glm", "/glm", "https://open.bigmodel.cn", True),
        Provider("default", "/", "https://open.bigmodel.cn", False),
    ]
    assert match_provider(providers, "/oai/v1/chat").name == "oai"
    assert match_provider(providers, "/claude/v1/messages").name == "claude"
    assert match_provider(providers, "/glm/v4/chat").name == "glm"
    assert match_provider(providers, "/anthropic/v1/messages").name == "default"
    assert match_provider(providers, "/v4/chat").name == "default"


def test_match_provider_longest_prefix():
    providers = [
        Provider("default", "/", "https://open.bigmodel.cn", False),
        Provider("oai", "/oai", "https://api.openai.com/", True),
        Provider("oai-chat", "/oai/chat", "https://api.openai.com/", True),
    ]
    assert match_provider(providers, "/oai/chat/completions").name == "oai-chat"
    assert match_provider(providers, "/oai/v1/models").name == "oai"
    assert match_provider(providers, "/other/path").name == "default"


def test_match_provider_no_providers():
    assert match_provider([], "/anything") is None


def test_make_token_format():
    token = make_token()
    parts = token.split(".")
    assert len(parts) == 2
    assert len(parts[0]) == 32
    assert len(parts[1]) == 16


def test_resolve_token_with_permission():
    token = _make_token_full()
    pool = {token: TokenEntry(token, ["glm", "default"])}
    provider = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    real_tokens = {"glm": "real-glm-token"}

    proxy_token, resolved = resolve_token(
        f"Bearer {token}", provider, pool, real_tokens
    )
    assert proxy_token == token
    assert resolved == "Bearer real-glm-token"


def test_resolve_token_forbidden():
    token = _make_token_full()
    pool = {token: TokenEntry(token, ["glm"])}
    provider = Provider("oai", "/oai", "https://api.openai.com/", True)
    real_tokens = {"oai": "real-oai-token"}

    proxy_token, resolved = resolve_token(
        f"Bearer {token}", provider, pool, real_tokens
    )
    assert proxy_token == token
    assert resolved == ""


def test_resolve_token_disabled():
    token = _make_token_full()
    pool = {token: TokenEntry(token, ["*"], enabled=False)}
    provider = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    real_tokens = {"glm": "real-glm-token"}

    proxy_token, resolved = resolve_token(
        f"Bearer {token}", provider, pool, real_tokens
    )
    assert proxy_token == token
    assert resolved == ""


def test_resolve_token_not_in_pool():
    pool: dict[str, TokenEntry] = {}
    provider = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    real_tokens = {"glm": "real-glm-token"}
    auth = f"Bearer {_sample_token()}.Zk1pGnRvtztoeAb1"

    proxy_token, resolved = resolve_token(auth, provider, pool, real_tokens)
    assert proxy_token is None
    assert resolved == auth


def test_resolve_token_passthrough():
    pool: dict[str, TokenEntry] = {}
    provider = Provider("glm", "/glm", "https://open.bigmodel.cn", True)
    real_tokens = {}

    proxy_token, resolved = resolve_token(
        "Bearer some-other-token-format", provider, pool, real_tokens
    )
    assert proxy_token is None
    assert resolved == "Bearer some-other-token-format"


def test_extract_token():
    token = _make_token_full()
    assert extract_token(f"Bearer {token}") == token
    assert extract_token("Bearer invalid") is None
    assert extract_token("NoBearer prefix") is None


def test_init_db_creates_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='request_log'"
        )
        assert cur.fetchone() is not None
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='token_pool'"
        )
        assert cur.fetchone() is not None
        conn.close()
    finally:
        os.unlink(db_file)


def test_init_db_migrates_pool_json():
    token = _make_token_full()
    pool_data = {"tokens": [{"token": token, "providers": ["glm"]}]}
    with tempfile.TemporaryDirectory() as tmpdir:
        pool_file = os.path.join(tmpdir, "pool.json")
        db_file = os.path.join(tmpdir, "test.db")
        with open(pool_file, "w") as f:
            json.dump(pool_data, f)

        conn, _ = init_db(db_file, pool_file=pool_file, pool_count=1)
        pool = load_pool_from_db(conn)
        assert token in pool
        assert pool[token].providers == ["glm"]
        assert os.path.exists(pool_file + ".bak")
        conn.close()


def test_record_usage():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        record_usage(conn, "test_token", "glm")

        cur = conn.execute(
            "SELECT count FROM request_log WHERE token=? AND provider=?",
            ("test_token", "glm"),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1

        record_usage(conn, "test_token", "glm")
        cur = conn.execute(
            "SELECT count FROM request_log WHERE token=? AND provider=?",
            ("test_token", "glm"),
        )
        row = cur.fetchone()
        assert row[0] == 2
        conn.close()
    finally:
        os.unlink(db_file)


def test_db_token_crud():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)

        entry = add_token(conn, _make_token_full(), ["glm"], is_admin=True)
        assert entry.token == _make_token_full()
        assert entry.providers == ["glm"]
        assert entry.is_admin is True

        fetched = get_token(conn, _make_token_full())
        assert fetched is not None
        assert fetched.is_admin is True

        all_tokens = get_all_tokens(conn)
        assert any(t.token == _make_token_full() for t in all_tokens)

        updated = update_token(
            conn, _make_token_full(), providers=["*"], is_admin=False
        )
        assert updated is not None
        assert updated.providers == ["*"]
        assert updated.is_admin is False

        deleted = delete_token(conn, _make_token_full())
        assert deleted is True
        assert get_token(conn, _make_token_full()) is None

        deleted_again = delete_token(conn, _make_token_full())
        assert deleted_again is False

        conn.close()
    finally:
        os.unlink(db_file)


def test_is_admin_token():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        add_token(conn, _make_token_full(), ["*"], is_admin=True)
        add_token(
            conn, f"{_sample_token()}00.NDt4aLrzPFb4a1A1", ["glm"], is_admin=False
        )

        assert is_admin_token(conn, _make_token_full()) is True
        assert is_admin_token(conn, f"{_sample_token()}00.NDt4aLrzPFb4a1A1") is False
        assert is_admin_token(conn, "nonexistent") is False
        conn.close()
    finally:
        os.unlink(db_file)


def test_load_pool_from_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        add_token(conn, _make_token_full(), ["glm"])
        pool = load_pool_from_db(conn)
        assert _make_token_full() in pool
        assert pool[_make_token_full()].providers == ["glm"]
        conn.close()
    finally:
        os.unlink(db_file)


def test_load_config_from_toml():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "config.toml")
        with open(cfg_path, "w") as f:
            f.write("""
bind_port = 9999
intercept_port = 1234
enable_log = true

[pool]
count = 10
file = "test_pool.json"
usage_db = "test_usage.db"
admin_tokens = ["aaa", "bbb"]

[providers.test1]
prefix = "/t1"
upstream = "https://test1.example.com/"
strip_prefix = true
env_token = "MY_TOKEN_TEST1"

[providers.test2]
prefix = "/t2"
upstream = "https://test2.example.com"
strip_prefix = false
""")

        cfg = load_config(config_dir=tmpdir)
        assert cfg.bind_port == 9999
        assert cfg.intercept_port == 1234
        assert cfg.enable_log is True
        assert cfg.pool_count == 10
        assert cfg.admin_tokens == ["aaa", "bbb"]
        assert len(cfg.providers) == 2
        by_name = {p.name: p for p in cfg.providers}
        assert by_name["test1"].env_token == "MY_TOKEN_TEST1"


def test_load_config_env_token_reads_real_token():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "config.toml")
        with open(cfg_path, "w") as f:
            f.write("""
[pool]
count = 1

[providers.myprov]
prefix = "/mp"
upstream = "https://my.example.com"
strip_prefix = true
env_token = "CUSTOM_SECRET_KEY"
""")

        os.environ["CUSTOM_SECRET_KEY"] = "sk-test-12345"
        try:
            cfg = load_config(config_dir=tmpdir)
            assert cfg.real_tokens["myprov"] == "sk-test-12345"
        finally:
            os.environ.pop("CUSTOM_SECRET_KEY", None)


def test_init_db_auto_generates_admin_when_none():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, generated = init_db(db_file, pool_count=0)
        assert len(generated) == 1
        admin_tok = generated[0]
        assert is_admin_token(conn, admin_tok) is True
        entry = get_token(conn, admin_tok)
        assert entry is not None
        assert entry.providers == ["*"]
        conn.close()
    finally:
        os.unlink(db_file)


def test_init_db_no_auto_admin_when_config_has_one():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        admin_tok = _make_token_full()
        conn, generated = init_db(db_file, admin_tokens=[admin_tok], pool_count=0)
        assert len(generated) == 0
        assert is_admin_token(conn, admin_tok) is True
        conn.close()
    finally:
        os.unlink(db_file)


def test_init_db_no_auto_admin_when_db_has_one():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file, pool_count=0)
        existing_admins = conn.execute(
            "SELECT token FROM token_pool WHERE is_admin = 1"
        ).fetchall()
        assert len(existing_admins) == 1
        conn.close()

        conn2, generated = init_db(db_file, pool_count=0)
        assert len(generated) == 0
        conn2.close()
    finally:
        os.unlink(db_file)


def test_init_db_creates_data_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "sub", "onegate.db")
        conn, _ = init_db(db_path, pool_count=0)
        assert os.path.exists(os.path.join(tmpdir, "sub"))
        conn.close()


if __name__ == "__main__":
    test_provider_model()
    test_provider_custom_env_token()
    test_provider_strip_trailing_slash()
    test_token_entry_permission_wildcard()
    test_token_entry_permission_specific()
    test_token_entry_disabled()
    test_token_entry_to_from_db()
    test_match_provider_exact_prefix()
    test_match_provider_longest_prefix()
    test_match_provider_no_providers()
    test_make_token_format()
    test_resolve_token_with_permission()
    test_resolve_token_forbidden()
    test_resolve_token_disabled()
    test_resolve_token_not_in_pool()
    test_resolve_token_passthrough()
    test_extract_token()
    test_init_db_creates_tables()
    test_init_db_migrates_pool_json()
    test_record_usage()
    test_db_token_crud()
    test_is_admin_token()
    test_load_pool_from_db()
    test_load_config_from_toml()
    test_load_config_env_token_reads_real_token()
    print("\nAll tests passed!")
