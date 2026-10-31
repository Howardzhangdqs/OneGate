import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os
import tempfile

from src.config import load_config, match_provider
from src.db import (
    add_token,
    check_rate_limit,
    delete_token,
    get_all_tokens,
    get_rate_limit_status,
    get_token,
    init_db,
    is_admin_token,
    record_success,
    record_token_usage,
    record_usage,
    update_token,
)
from src.models import Provider, TokenEntry
from src.pool import extract_token, load_pool_from_db, make_token, resolve_token
from src.admin import _read_limit, _read_limit_opt


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
    entry = TokenEntry(
        "abc.def",
        ["glm"],
        is_admin=True,
        enabled=True,
        created_at=1000,
        rpm=10,
        rph=100,
        rpd=1000,
        rpw=7000,
        rpmo=30000,
        rpt=99999,
        tpm=5_000_000,
        tph=50_000_000,
        tpd=500_000_000,
        tpw=3_500_000_000,
        tpmo=15_000_000_000,
        tpt=99_999_999,
        success_count=42,
        token_count=123_456,
        cache_read_weight=0.1,
        cache_write_weight=1.25,
    )
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
    assert restored.rpm == 10
    assert restored.rpt == 99999
    assert restored.rpw == 7000
    assert restored.rpmo == 30000
    assert restored.tpm == 5_000_000
    assert restored.tpt == 99_999_999
    assert restored.token_count == 123_456
    assert restored.cache_read_weight == 0.1
    assert restored.cache_write_weight == 1.25
    assert restored.has_limits is True


def test_token_entry_has_limits_default():
    entry = TokenEntry("abc.def", ["*"])
    assert entry.has_limits is False
    assert all(getattr(entry, f) == -1 for f in TokenEntry.RATE_LIMIT_FIELDS)


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


def test_match_provider_multi_segment_prefix():
    providers = [
        Provider("default", "/", "https://open.bigmodel.cn", False),
        Provider("gjzq-glm", "/gjzq/glm", "https://gpt.example.cn/prod-api", True),
        Provider("gjzq-ds", "/gjzq/ds", "https://gpt.example.cn/prod-api", True),
    ]
    assert match_provider(providers, "/gjzq/glm/v1/chat/completions").name == "gjzq-glm"
    assert match_provider(providers, "/gjzq/ds/v1/chat/completions").name == "gjzq-ds"
    assert match_provider(providers, "/gjzq/glmx/v1/chat").name == "default"
    assert match_provider(providers, "/gjzq").name == "default"

    p = match_provider(providers, "/gjzq/glm/v1/chat/completions")
    prefix = p.prefix.rstrip("/")
    assert "/gjzq/glm/v1/chat/completions"[len(prefix) :] == "/v1/chat/completions"
    assert p.origin + p.upstream_path == "https://gpt.example.cn/prod-api"


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


def test_record_token_usage_tracks_rate_buckets():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        add_token(conn, "tok.rate", ["*"])
        record_token_usage(conn, "tok.rate", "glm", input_tokens=300, output_tokens=200)
        row = conn.execute(
            "SELECT tokens FROM token_rate_usage WHERE token=?", ("tok.rate",)
        ).fetchone()
        assert row[0] == 500
        tc = conn.execute(
            "SELECT token_count FROM token_pool WHERE token=?", ("tok.rate",)
        ).fetchone()
        assert tc[0] == 500
        conn.close()
    finally:
        os.unlink(db_file)


def test_record_token_usage_with_cache_weights():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        add_token(conn, "tok.cw", ["*"])
        # input 300 + output 200 + 0.1*cache_read 1000 + 1.25*cache_write 400
        # = 500 + 100 + 500 = 1100
        record_token_usage(
            conn,
            "tok.cw",
            "glm",
            input_tokens=300,
            output_tokens=200,
            cache_read=1000,
            cache_write=400,
            cache_read_weight=0.1,
            cache_write_weight=1.25,
        )
        row = conn.execute(
            "SELECT tokens FROM token_rate_usage WHERE token=?", ("tok.cw",)
        ).fetchone()
        assert row[0] == 1100
        # zero weights (default) -> cache ignored, only input+output
        record_token_usage(
            conn,
            "tok.cw",
            "glm",
            input_tokens=10,
            output_tokens=5,
            cache_read=9999,
            cache_write=9999,
        )
        row = conn.execute(
            "SELECT tokens FROM token_rate_usage WHERE token=?", ("tok.cw",)
        ).fetchone()
        assert row[0] == 1100 + 15
        conn.close()
    finally:
        os.unlink(db_file)


def test_check_rate_limit_blocks_and_reports():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        entry = add_token(conn, "tok.lim", ["*"], rpm=2, tpm=1_000_000)
        allowed, triggered, quota = check_rate_limit(conn, entry)
        assert allowed is True
        assert triggered is None
        assert "rpm" in quota and "tpm" in quota
        record_success(conn, "tok.lim", "glm")
        record_success(conn, "tok.lim", "glm")
        record_token_usage(
            conn, "tok.lim", "glm", input_tokens=400_000, output_tokens=400_000
        )
        allowed, triggered, quota = check_rate_limit(conn, entry)
        assert allowed is False
        assert triggered == "rpm"
        assert "rpm" in quota and "tpm" in quota
        assert quota["rpm"]["used"] == 2
        assert quota["tpm"]["limit"] == 1
        assert quota["tpm"]["used"] == 0.8
        conn.close()
    finally:
        os.unlink(db_file)


def test_get_rate_limit_status_only_configured():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        conn, _ = init_db(db_file)
        entry = add_token(conn, "tok.status", ["*"], rph=10, tph=2_000_000)
        status = get_rate_limit_status(conn, entry)
        keys = {d["key"] for d in status}
        assert keys == {"rph", "tph"}
        by_key = {d["key"]: d for d in status}
        assert by_key["rph"]["limit"] == 10
        assert by_key["tph"]["limit"] == 2
        assert by_key["tph"]["unit"] == "M tokens"
        unlimited = add_token(conn, "tok.free", ["*"])
        assert get_rate_limit_status(conn, unlimited) == []
        conn.close()
    finally:
        os.unlink(db_file)


def test_read_limit_scales_tokens_in_millions():
    assert _read_limit({"rpm": 10}, "rpm") == 10
    assert _read_limit({"tpm": 5}, "tpm") == 5_000_000
    assert _read_limit({"tpm": 0.5}, "tpm") == 500_000
    assert _read_limit({"tpm": -1}, "tpm") == -1
    assert _read_limit({}, "tpm") == -1
    assert _read_limit({"tpt": ""}, "tpt") == -1
    assert _read_limit_opt({}, "tpm") is None
    assert _read_limit_opt({"tpm": 3}, "tpm") == 3_000_000


def test_read_weight_parses_floats():
    from src.admin import _read_weight, _read_weight_opt

    assert _read_weight({"cache_read_weight": 0.1}, "cache_read_weight") == 0.1
    assert _read_weight({"cache_write_weight": "1.25"}, "cache_write_weight") == 1.25
    assert _read_weight({}, "cache_read_weight") == -1
    assert _read_weight({"cache_read_weight": ""}, "cache_read_weight") == -1
    assert _read_weight_opt({}, "cache_read_weight") is None
    assert _read_weight_opt({"cache_read_weight": 0}, "cache_read_weight") == 0.0


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
cache_read_weight = 0.1
cache_write_weight = 1.25

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
        assert cfg.cache_read_weight == 0.1
        assert cfg.cache_write_weight == 1.25
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
