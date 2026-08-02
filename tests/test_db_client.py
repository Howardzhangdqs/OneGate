import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import (
    get_client_usage_detail,
    get_client_usage_summary,
    get_global_client_hourly,
    get_global_client_summary,
    get_token_client_usage_detail,
    get_token_clients,
    init_db,
    record_client_token_usage,
    record_client_usage,
)


def _conn():
    conn, _ = init_db(":memory:", admin_tokens=["admintoken"], pool_count=0)
    return conn


def test_record_client_usage_accumulates():
    conn = _conn()
    token = "tok123"
    for _ in range(3):
        record_client_usage(conn, token, "glm", "claude-code")
    summary = get_client_usage_summary(
        conn, token, int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert summary.get("claude-code") == 3


def test_record_client_usage_separates_clients():
    conn = _conn()
    token = "tok456"
    record_client_usage(conn, token, "glm", "claude-code")
    record_client_usage(conn, token, "glm", "claude-code")
    record_client_usage(conn, token, "glm", "codex")
    summary = get_client_usage_summary(
        conn, token, int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert summary["claude-code"] == 2
    assert summary["codex"] == 1


def test_unknown_default_when_empty_client():
    conn = _conn()
    token = "tok789"
    record_client_usage(conn, token, "glm", "")
    summary = get_client_usage_summary(
        conn, token, int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert summary.get("unknown") == 1


def test_summary_time_range_filters():
    conn = _conn()
    token = "tok-range"
    record_client_usage(conn, token, "glm", "claude-code")
    future_start = int(time.time()) + 7200
    future_end = int(time.time()) + 10800
    summary = get_client_usage_summary(conn, token, future_start, future_end)
    assert summary == {} or summary.get("claude-code", 0) == 0


def test_get_token_clients_orders_by_count():
    conn = _conn()
    token = "tok-order"
    for _ in range(5):
        record_client_usage(conn, token, "glm", "codex")
    for _ in range(2):
        record_client_usage(conn, token, "glm", "claude-code")
    clients = get_token_clients(conn, token)
    assert len(clients) >= 2
    assert clients[0]["client"] == "codex"
    assert clients[0]["requests"] == 5
    assert clients[1]["client"] == "claude-code"
    assert clients[1]["requests"] == 2


def test_get_global_client_summary_aggregates_and_counts_tokens():
    conn = _conn()
    record_client_usage(conn, "tokA", "glm", "claude-code")
    record_client_usage(conn, "tokA", "glm", "claude-code")
    record_client_usage(conn, "tokB", "glm", "claude-code")
    record_client_usage(conn, "tokC", "glm", "codex")
    summary = get_global_client_summary(
        conn, int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert summary["claude-code"]["requests"] == 3
    assert summary["claude-code"]["active_tokens"] == 2
    assert summary["codex"]["requests"] == 1
    assert summary["codex"]["active_tokens"] == 1


def test_get_global_client_hourly_groups_by_client():
    conn = _conn()
    record_client_usage(conn, "tokA", "glm", "claude-code")
    record_client_usage(conn, "tokB", "glm", "codex")
    hourly = get_global_client_hourly(
        conn, int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert "claude-code" in hourly
    assert "codex" in hourly
    claude_total = sum(hourly["claude-code"].values())
    assert claude_total == 1


def test_record_client_token_usage_accumulates():
    conn = _conn()
    token = "tok-usage"
    record_client_token_usage(conn, token, "glm", "claude-code", 100, 50, 20, 10)
    record_client_token_usage(conn, token, "glm", "claude-code", 200, 60, 30, 5)
    detail = get_client_usage_detail(
        conn, int(time.time()) - 3600, int(time.time()) + 3600
    )
    d = detail["claude-code"]
    assert d["input"] == 300
    assert d["output"] == 110
    assert d["cache_read"] == 50
    assert d["cache_write"] == 15
    assert d["success_requests"] == 2


def test_record_client_token_usage_skips_all_zero():
    conn = _conn()
    token = "tok-zero"
    record_client_token_usage(conn, token, "glm", "claude-code", 0, 0, 0, 0)
    detail = get_client_usage_detail(
        conn, int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert "claude-code" not in detail


def test_get_client_usage_detail_merges_request_and_usage():
    conn = _conn()
    record_client_usage(conn, "tokX", "glm", "claude-code")
    record_client_usage(conn, "tokX", "glm", "claude-code")
    record_client_usage(conn, "tokX", "glm", "codex")
    record_client_token_usage(conn, "tokX", "glm", "claude-code", 100, 50, 80, 0)
    detail = get_client_usage_detail(
        conn, int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert detail["claude-code"]["requests"] == 2
    assert detail["claude-code"]["input"] == 100
    assert detail["claude-code"]["cache_read"] == 80
    assert detail["claude-code"]["success_requests"] == 1
    assert detail["codex"]["requests"] == 1
    assert detail["codex"]["input"] == 0


def test_get_client_usage_detail_empty_range():
    conn = _conn()
    record_client_usage(conn, "tokY", "glm", "claude-code")
    future_start = int(time.time()) + 7200
    future_end = int(time.time()) + 10800
    detail = get_client_usage_detail(conn, future_start, future_end)
    assert detail == {}


def test_get_token_client_usage_detail_scoped_to_token():
    conn = _conn()
    record_client_usage(conn, "tokOwner", "glm", "claude-code")
    record_client_usage(conn, "tokOther", "glm", "claude-code")
    record_client_token_usage(conn, "tokOwner", "glm", "claude-code", 100, 50, 30, 10)
    record_client_token_usage(
        conn, "tokOther", "glm", "claude-code", 999, 999, 999, 999
    )
    detail = get_token_client_usage_detail(
        conn, "tokOwner", int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert "claude-code" in detail
    assert detail["claude-code"]["requests"] == 1
    assert detail["claude-code"]["input"] == 100
    assert detail["claude-code"]["output"] == 50
    assert detail["claude-code"]["cache_read"] == 30
    assert detail["claude-code"]["success_requests"] == 1


def test_get_token_client_usage_detail_separates_clients():
    conn = _conn()
    record_client_usage(conn, "tokMix", "glm", "claude-code")
    record_client_usage(conn, "tokMix", "glm", "codex")
    record_client_token_usage(conn, "tokMix", "glm", "claude-code", 100, 50, 80, 0)
    detail = get_token_client_usage_detail(
        conn, "tokMix", int(time.time()) - 3600, int(time.time()) + 3600
    )
    assert detail["claude-code"]["requests"] == 1
    assert detail["claude-code"]["cache_read"] == 80
    assert detail["codex"]["requests"] == 1
    assert detail["codex"]["input"] == 0
