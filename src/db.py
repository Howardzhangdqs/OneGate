from __future__ import annotations

import fnmatch
import re
import json
import os
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

from src.models import TokenEntry


def _get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _get_primary_key_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall() if row[5] > 0}


def _migrate_old_schema(conn: sqlite3.Connection):
    cols = _get_columns(conn, "request_log")
    if "provider" not in cols:
        print("[DB] Migrating: adding 'provider' column to request_log")
        conn.execute(
            "ALTER TABLE request_log ADD COLUMN provider TEXT NOT NULL DEFAULT 'default'"
        )
        if _get_columns(conn, "request_log") & {"token", "ts"}:
            conn.execute(
                "UPDATE request_log SET provider = 'default' WHERE provider = '' OR provider IS NULL"
            )
        conn.commit()

    pk_cols = _get_primary_key_columns(conn, "request_log")
    if pk_cols == {"token", "ts"}:
        print(
            "[DB] Migrating: rebuilding request_log with PRIMARY KEY (token, provider, ts)"
        )
        conn.execute(
            "CREATE TABLE request_log_new ("
            "  token TEXT NOT NULL,"
            "  provider TEXT NOT NULL DEFAULT 'default',"
            "  ts INTEGER NOT NULL,"
            "  count INTEGER NOT NULL DEFAULT 1,"
            "  PRIMARY KEY (token, provider, ts)"
            ")"
        )
        conn.execute(
            "INSERT INTO request_log_new (token, provider, ts, count) "
            "SELECT token, provider, ts, count FROM request_log"
        )
        conn.execute("DROP TABLE request_log")
        conn.execute("ALTER TABLE request_log_new RENAME TO request_log")
        conn.commit()
        print("[DB] Migration complete: request_log primary key updated")

    pool_cols = _get_columns(conn, "token_pool")
    if pool_cols and "describe" not in pool_cols:
        print("[DB] Migrating: adding 'describe' column to token_pool")
        conn.execute(
            "ALTER TABLE token_pool ADD COLUMN describe TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()


def _migrate_rate_limit_columns(conn: sqlite3.Connection):
    cols = _get_columns(conn, "token_pool")
    # Rate-limit columns default to -1 (unlimited); 0 means "blocked".
    rate_cols = [
        ("rpm", "INTEGER", -1),
        ("rph", "INTEGER", -1),
        ("rpd", "INTEGER", -1),
        ("rpt", "INTEGER", -1),
        ("success_count", "INTEGER", 0),
        ("rpw", "INTEGER", -1),
        ("rpmo", "INTEGER", -1),
        ("tpm", "INTEGER", -1),
        ("tph", "INTEGER", -1),
        ("tpd", "INTEGER", -1),
        ("tpw", "INTEGER", -1),
        ("tpmo", "INTEGER", -1),
        ("tpt", "INTEGER", -1),
        ("token_count", "INTEGER", 0),
        # Cache-token weights: -1 = inherit global default, >=0 = explicit override.
        ("cache_read_weight", "REAL", -1),
        ("cache_write_weight", "REAL", -1),
    ]
    for col, col_type, default in rate_cols:
        if col not in cols:
            print(f"[DB] Migrating: adding '{col}' column to token_pool")
            conn.execute(
                f"ALTER TABLE token_pool ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default}"
            )
    conn.commit()

    existing_tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "success_log" not in existing_tables:
        print("[DB] Creating success_log table")
        conn.execute(
            "CREATE TABLE success_log ("
            "  token    TEXT NOT NULL,"
            "  provider TEXT NOT NULL DEFAULT 'default',"
            "  ts       INTEGER NOT NULL,"
            "  count    INTEGER NOT NULL DEFAULT 1,"
            "  PRIMARY KEY (token, provider, ts)"
            ")"
        )
        conn.commit()


def _migrate_token_usage_log(conn: sqlite3.Connection):
    existing_tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "token_usage_log" not in existing_tables:
        print("[DB] Creating token_usage_log table")
        conn.execute(
            "CREATE TABLE token_usage_log ("
            "  token         TEXT NOT NULL,"
            "  provider      TEXT NOT NULL DEFAULT 'default',"
            "  ts            INTEGER NOT NULL,"
            "  input_tokens  INTEGER NOT NULL DEFAULT 0,"
            "  output_tokens INTEGER NOT NULL DEFAULT 0,"
            "  cache_read    INTEGER NOT NULL DEFAULT 0,"
            "  cache_write   INTEGER NOT NULL DEFAULT 0,"
            "  PRIMARY KEY (token, provider, ts)"
            ")"
        )
        conn.commit()


def _migrate_usage_model_column(conn: sqlite3.Connection):
    # Add a `model` column to the per-token and per-client usage logs and fold
    # it into the primary key so usage can be broken down by model. SQLite can't
    # ALTER a primary key in place, so rebuild via the _new pattern used elsewhere.
    rebuilds = [
        (
            "token_usage_log",
            "token, provider, model, ts",
            "token         TEXT NOT NULL,"
            "  provider      TEXT NOT NULL DEFAULT 'default',"
            "  model         TEXT NOT NULL DEFAULT '',"
            "  ts            INTEGER NOT NULL,"
            "  input_tokens  INTEGER NOT NULL DEFAULT 0,"
            "  output_tokens INTEGER NOT NULL DEFAULT 0,"
            "  cache_read    INTEGER NOT NULL DEFAULT 0,"
            "  cache_write   INTEGER NOT NULL DEFAULT 0,"
            "  count         INTEGER NOT NULL DEFAULT 0",
            [
                "token",
                "provider",
                "ts",
                "input_tokens",
                "output_tokens",
                "cache_read",
                "cache_write",
            ],
        ),
        (
            "client_token_usage_log",
            "token, provider, client, model, ts",
            "token         TEXT NOT NULL,"
            "  provider      TEXT NOT NULL DEFAULT 'default',"
            "  client        TEXT NOT NULL,"
            "  model         TEXT NOT NULL DEFAULT '',"
            "  ts            INTEGER NOT NULL,"
            "  input_tokens  INTEGER NOT NULL DEFAULT 0,"
            "  output_tokens INTEGER NOT NULL DEFAULT 0,"
            "  cache_read    INTEGER NOT NULL DEFAULT 0,"
            "  cache_write   INTEGER NOT NULL DEFAULT 0,"
            "  count         INTEGER NOT NULL DEFAULT 1",
            [
                "token",
                "provider",
                "client",
                "ts",
                "input_tokens",
                "output_tokens",
                "cache_read",
                "cache_write",
                "count",
            ],
        ),
    ]
    for table, new_pk, cols_def, old_cols in rebuilds:
        cols = _get_columns(conn, table)
        if not cols or "model" in cols:
            continue
        print(f"[DB] Migrating: adding 'model' column to {table}")
        conn.execute(f"CREATE TABLE {table}_new ({cols_def}, PRIMARY KEY ({new_pk}))")
        select_cols = ", ".join(old_cols)
        conn.execute(
            f"INSERT INTO {table}_new ({select_cols}) SELECT {select_cols} FROM {table}"
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        conn.commit()


_SEED_PRICING = [
    ("deepseek-v4-flash", 0.02, 1.0, 2.0, 2.0),
    ("deepseek-v4-pro", 0.025, 3.0, 6.0, 6.0),
]


def _seed_default_pricing(conn: sqlite3.Connection):
    count = conn.execute("SELECT COUNT(*) FROM model_pricing").fetchone()[0]
    if count > 0:
        return
    for model, ih, im, oh, om in _SEED_PRICING:
        conn.execute(
            "INSERT OR IGNORE INTO model_pricing "
            "(model, input_cache_hit, input_cache_miss, output_cache_hit, output_cache_miss) "
            "VALUES (?, ?, ?, ?, ?)",
            (model, ih, im, oh, om),
        )
    conn.commit()


def _migrate_pool_json(
    conn: sqlite3.Connection, pool_file: str, admin_tokens: list[str]
):
    if not os.path.exists(pool_file):
        return

    existing_count = conn.execute("SELECT COUNT(*) FROM token_pool").fetchone()[0]
    if existing_count > 0:
        backup = pool_file + ".bak"
        if not os.path.exists(backup):
            shutil.move(pool_file, backup)
            print(f"[DB] pool.json migrated to DB, backed up to {backup}")
        return

    with open(pool_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw_tokens = data.get("tokens", [])
    now = int(time.time())
    imported = 0
    for entry in raw_tokens:
        if isinstance(entry, str):
            t = entry
            provs = ["*"]
        elif isinstance(entry, dict):
            t = entry.get("token", "")
            provs = entry.get("providers", ["*"])
        else:
            continue
        if not t:
            continue
        is_admin = t in admin_tokens
        conn.execute(
            "INSERT OR IGNORE INTO token_pool "
            "(token, providers, is_admin, enabled, created_at, describe, "
            "rpm, rph, rpd, rpt, success_count) "
            "VALUES (?, ?, ?, ?, ?, '', -1, -1, -1, -1, 0)",
            (t, json.dumps(provs), 1 if is_admin else 0, 1, now),
        )
        imported += 1

    conn.commit()
    backup = pool_file + ".bak"
    if not os.path.exists(backup):
        shutil.move(pool_file, backup)
        print(f"[DB] Imported {imported} tokens from pool.json, backed up to {backup}")


def init_db(
    usage_db: str,
    pool_file: str = "",
    admin_tokens: list[str] | None = None,
    pool_count: int = 5,
) -> tuple[sqlite3.Connection, list[str]]:
    db_dir = os.path.dirname(usage_db)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(usage_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS request_log ("
        "  token TEXT NOT NULL,"
        "  provider TEXT NOT NULL DEFAULT 'default',"
        "  ts    INTEGER NOT NULL,"
        "  count INTEGER NOT NULL DEFAULT 1,"
        "  PRIMARY KEY (token, provider, ts)"
        ")"
    )
    _migrate_old_schema(conn)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS token_pool ("
        "  token       TEXT PRIMARY KEY,"
        "  providers   TEXT NOT NULL DEFAULT '[\"*\"]',"
        "  is_admin    INTEGER NOT NULL DEFAULT 0,"
        "  enabled     INTEGER NOT NULL DEFAULT 1,"
        "  created_at  INTEGER NOT NULL,"
        "  describe    TEXT NOT NULL DEFAULT ''"
        ")"
    )

    _migrate_rate_limit_columns(conn)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_success_token_ts ON success_log(token, ts)"
    )

    pool_cols = _get_columns(conn, "token_pool")
    if "models" not in pool_cols:
        print("[DB] Migrating: adding 'models' column to token_pool")
        conn.execute(
            "ALTER TABLE token_pool ADD COLUMN models TEXT NOT NULL DEFAULT '[\"*\"]'"
        )
        conn.commit()

    # One-time backfill: legacy auto-created tokens inherited a rate-limit
    # default of 0 (= blocked). Flip the all-zero signature back to unlimited.
    db_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if db_version < 1:
        flipped = conn.execute(
            "UPDATE token_pool SET rpm=-1, rph=-1, rpd=-1, rpt=-1 "
            "WHERE rpm=0 AND rph=0 AND rpd=0 AND rpt=0"
        ).rowcount
        if flipped:
            print(
                f"[DB] Backfill: set {flipped} legacy token(s) to unlimited rate limits"
            )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS status_log ("
        "  provider    TEXT NOT NULL,"
        "  status_code INTEGER NOT NULL,"
        "  ts          INTEGER NOT NULL,"
        "  count       INTEGER NOT NULL DEFAULT 1,"
        "  PRIMARY KEY (provider, status_code, ts)"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS model_map ("
        "  token    TEXT NOT NULL,"
        "  pattern  TEXT NOT NULL,"
        "  target   TEXT NOT NULL,"
        "  provider TEXT NOT NULL DEFAULT '',"
        "  PRIMARY KEY (token, pattern)"
        ")"
    )

    model_map_cols = _get_columns(conn, "model_map")
    if model_map_cols and "provider" not in model_map_cols:
        print("[DB] Migrating: adding 'provider' column to model_map")
        conn.execute(
            "ALTER TABLE model_map ADD COLUMN provider TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS site_model ("
        "  model    TEXT PRIMARY KEY,"
        "  provider TEXT NOT NULL DEFAULT '',"
        "  target   TEXT NOT NULL DEFAULT '',"
        "  enabled  INTEGER NOT NULL DEFAULT 1"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS token_usage_log ("
        "  token         TEXT NOT NULL,"
        "  provider      TEXT NOT NULL DEFAULT 'default',"
        "  model         TEXT NOT NULL DEFAULT '',"
        "  ts            INTEGER NOT NULL,"
        "  input_tokens  INTEGER NOT NULL DEFAULT 0,"
        "  output_tokens INTEGER NOT NULL DEFAULT 0,"
        "  cache_read    INTEGER NOT NULL DEFAULT 0,"
        "  cache_write   INTEGER NOT NULL DEFAULT 0,"
        "  count         INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (token, provider, model, ts)"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS token_rate_usage ("
        "  token  TEXT NOT NULL,"
        "  ts     INTEGER NOT NULL,"
        "  tokens INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (token, ts)"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS client_usage_log ("
        "  token    TEXT NOT NULL,"
        "  provider TEXT NOT NULL DEFAULT 'default',"
        "  client   TEXT NOT NULL,"
        "  ts       INTEGER NOT NULL,"
        "  count    INTEGER NOT NULL DEFAULT 1,"
        "  PRIMARY KEY (token, provider, client, ts)"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS client_token_usage_log ("
        "  token         TEXT NOT NULL,"
        "  provider      TEXT NOT NULL DEFAULT 'default',"
        "  client        TEXT NOT NULL,"
        "  model         TEXT NOT NULL DEFAULT '',"
        "  ts            INTEGER NOT NULL,"
        "  input_tokens  INTEGER NOT NULL DEFAULT 0,"
        "  output_tokens INTEGER NOT NULL DEFAULT 0,"
        "  cache_read    INTEGER NOT NULL DEFAULT 0,"
        "  cache_write   INTEGER NOT NULL DEFAULT 0,"
        "  count         INTEGER NOT NULL DEFAULT 1,"
        "  PRIMARY KEY (token, provider, client, model, ts)"
        ")"
    )

    _migrate_usage_model_column(conn)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS model_pricing ("
        "  model              TEXT PRIMARY KEY,"
        "  input_cache_hit    REAL NOT NULL DEFAULT 0,"
        "  input_cache_miss   REAL NOT NULL DEFAULT 0,"
        "  output_cache_hit   REAL NOT NULL DEFAULT 0,"
        "  output_cache_miss  REAL NOT NULL DEFAULT 0"
        ")"
    )
    _seed_default_pricing(conn)

    generated_admins: list[str] = []

    has_any_admin = conn.execute(
        "SELECT COUNT(*) FROM token_pool WHERE is_admin = 1"
    ).fetchone()[0]

    admin_tokens = admin_tokens or []
    if not admin_tokens and has_any_admin == 0:
        from src.pool import make_token

        new_admin = make_token()
        admin_tokens = [new_admin]
        generated_admins.append(new_admin)

    _migrate_pool_json(conn, pool_file, admin_tokens)

    if admin_tokens:
        placeholders = ",".join("?" for _ in admin_tokens)
        conn.execute(
            f"UPDATE token_pool SET is_admin = 1 WHERE token IN ({placeholders})",
            admin_tokens,
        )
        for t in admin_tokens:
            exists = conn.execute(
                "SELECT 1 FROM token_pool WHERE token = ?", (t,)
            ).fetchone()
            if not exists:
                now = int(time.time())
                conn.execute(
                    "INSERT OR IGNORE INTO token_pool "
                    "(token, providers, is_admin, enabled, created_at, describe, "
                    "rpm, rph, rpd, rpt, success_count) "
                    "VALUES (?, ?, 1, 1, ?, '', -1, -1, -1, -1, 0)",
                    (t, '["*"]', now),
                )
        conn.commit()

    current_count = conn.execute("SELECT COUNT(*) FROM token_pool").fetchone()[0]
    if current_count < pool_count:
        need = pool_count - current_count
        from src.pool import make_token

        now = int(time.time())
        for _ in range(need):
            t = make_token()
            conn.execute(
                "INSERT OR IGNORE INTO token_pool "
                "(token, providers, is_admin, enabled, created_at, describe, "
                "rpm, rph, rpd, rpt, success_count) "
                "VALUES (?, ?, 0, 1, ?, '', -1, -1, -1, -1, 0)",
                (t, '["*"]', now),
            )
        conn.commit()
        print(f"[DB] Added {need} tokens to pool, now {pool_count} total")

    conn.execute(
        "DELETE FROM request_log WHERE ts < ?", (int(time.time()) - 35 * 86400,)
    )
    conn.execute(
        "DELETE FROM status_log WHERE ts < ?", (int(time.time()) - 35 * 86400,)
    )
    conn.execute(
        "DELETE FROM success_log WHERE ts < ?", (int(time.time()) - 35 * 86400,)
    )
    conn.execute(
        "DELETE FROM token_usage_log WHERE ts < ?", (int(time.time()) - 35 * 86400,)
    )
    conn.execute(
        "DELETE FROM token_rate_usage WHERE ts < ?", (int(time.time()) - 35 * 86400,)
    )
    conn.execute(
        "DELETE FROM client_usage_log WHERE ts < ?", (int(time.time()) - 35 * 86400,)
    )
    conn.execute(
        "DELETE FROM client_token_usage_log WHERE ts < ?",
        (int(time.time()) - 35 * 86400,),
    )
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM token_pool").fetchone()[0]
    print(f"[DB] {usage_db} initialized, {total} tokens in pool")
    return conn, generated_admins


def record_usage(conn: sqlite3.Connection, token: str, provider_name: str):
    now = int(time.time())
    hour_ts = now - (now % 3600)
    conn.execute(
        "INSERT INTO request_log (token, provider, ts, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(token, provider, ts) DO UPDATE SET count = count + 1",
        (token, provider_name, hour_ts),
    )
    conn.commit()


def record_client_usage(
    conn: sqlite3.Connection,
    token: str,
    provider_name: str,
    client: str,
):
    if not client:
        client = "unknown"
    now = int(time.time())
    hour_ts = now - (now % 3600)
    conn.execute(
        "INSERT INTO client_usage_log (token, provider, client, ts, count) "
        "VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(token, provider, client, ts) DO UPDATE SET count = count + 1",
        (token, provider_name, client, hour_ts),
    )
    conn.commit()


def record_client_token_usage(
    conn: sqlite3.Connection,
    token: str,
    provider_name: str,
    client: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    model: str = "",
):
    if not client:
        client = "unknown"
    if not model:
        model = ""
    if (
        input_tokens <= 0
        and output_tokens <= 0
        and cache_read <= 0
        and cache_write <= 0
    ):
        return
    now = int(time.time())
    hour_ts = now - (now % 3600)
    conn.execute(
        "INSERT INTO client_token_usage_log "
        "(token, provider, client, model, ts, input_tokens, output_tokens, "
        "cache_read, cache_write, count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(token, provider, client, model, ts) DO UPDATE SET "
        "input_tokens = input_tokens + excluded.input_tokens, "
        "output_tokens = output_tokens + excluded.output_tokens, "
        "cache_read = cache_read + excluded.cache_read, "
        "cache_write = cache_write + excluded.cache_write, "
        "count = count + 1",
        (
            token,
            provider_name,
            client,
            model,
            hour_ts,
            max(0, input_tokens),
            max(0, output_tokens),
            max(0, cache_read),
            max(0, cache_write),
        ),
    )
    conn.commit()


def get_client_usage_summary(
    conn: sqlite3.Connection,
    token: str,
    start_ts: int,
    end_ts: int,
) -> dict[str, int]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)
    rows = conn.execute(
        "SELECT client, SUM(count) FROM client_usage_log "
        "WHERE token = ? AND ts >= ? AND ts < ? "
        "GROUP BY client ORDER BY SUM(count) DESC",
        (token, start_hour, end_hour + 3600),
    ).fetchall()
    return {client: (count or 0) for client, count in rows}


def get_global_client_summary(
    conn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)
    rows = conn.execute(
        "SELECT client, SUM(count), COUNT(DISTINCT token) "
        "FROM client_usage_log "
        "WHERE ts >= ? AND ts < ? "
        "GROUP BY client ORDER BY SUM(count) DESC",
        (start_hour, end_hour + 3600),
    ).fetchall()
    result: dict[str, dict] = {}
    for client, count, active_tokens in rows:
        result[client] = {
            "requests": count or 0,
            "active_tokens": active_tokens or 0,
        }
    return result


def get_global_client_hourly(
    conn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict[int, int]]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)
    rows = conn.execute(
        "SELECT client, ts, SUM(count) FROM client_usage_log "
        "WHERE ts >= ? AND ts < ? "
        "GROUP BY client, ts ORDER BY client, ts",
        (start_hour, end_hour + 3600),
    ).fetchall()
    by_client: dict[str, dict[int, int]] = {}
    for client, ts, count in rows:
        by_client.setdefault(client, {})[ts] = count or 0
    return by_client


def get_token_clients(
    conn: sqlite3.Connection,
    token: str,
    days: int = 30,
) -> list[dict]:
    cutoff = int(time.time()) - days * 86400
    cutoff_hour = cutoff - (cutoff % 3600)
    rows = conn.execute(
        "SELECT client, SUM(count) FROM client_usage_log "
        "WHERE token = ? AND ts >= ? "
        "GROUP BY client ORDER BY SUM(count) DESC",
        (token, cutoff_hour),
    ).fetchall()
    return [{"client": client, "requests": count or 0} for client, count in rows]


def get_client_usage_detail(
    conn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict[str, int]]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)

    req_rows = conn.execute(
        "SELECT client, SUM(count) FROM client_usage_log "
        "WHERE ts >= ? AND ts < ? GROUP BY client",
        (start_hour, end_hour + 3600),
    ).fetchall()

    tok_rows = conn.execute(
        "SELECT client, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write), SUM(count) "
        "FROM client_token_usage_log WHERE ts >= ? AND ts < ? "
        "GROUP BY client",
        (start_hour, end_hour + 3600),
    ).fetchall()

    result: dict[str, dict[str, int]] = {}
    clients = {r[0] for r in req_rows} | {r[0] for r in tok_rows}
    for client in clients:
        result[client] = {
            "requests": 0,
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "success_requests": 0,
        }
    for client, count in req_rows:
        result[client]["requests"] = count or 0
    for client, it, ot, cr, cw, sc in tok_rows:
        result[client]["input"] = it or 0
        result[client]["output"] = ot or 0
        result[client]["cache_read"] = cr or 0
        result[client]["cache_write"] = cw or 0
        result[client]["success_requests"] = sc or 0
    return result


def get_token_client_usage_detail(
    conn: sqlite3.Connection,
    token: str | None,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict[str, int]]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)

    if token is None:
        where = "WHERE ts >= ? AND ts < ?"
        params: tuple = (start_hour, end_hour + 3600)
    else:
        where = "WHERE token = ? AND ts >= ? AND ts < ?"
        params = (token, start_hour, end_hour + 3600)

    req_rows = conn.execute(
        "SELECT client, SUM(count) FROM client_usage_log " + where + " GROUP BY client",
        params,
    ).fetchall()

    tok_rows = conn.execute(
        "SELECT client, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write), SUM(count) "
        "FROM client_token_usage_log " + where + " GROUP BY client",
        params,
    ).fetchall()

    result: dict[str, dict[str, int]] = {}
    clients = {r[0] for r in req_rows} | {r[0] for r in tok_rows}
    for client in clients:
        result[client] = {
            "requests": 0,
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "success_requests": 0,
        }
    for client, count in req_rows:
        result[client]["requests"] = count or 0
    for client, it, ot, cr, cw, sc in tok_rows:
        result[client]["input"] = it or 0
        result[client]["output"] = ot or 0
        result[client]["cache_read"] = cr or 0
        result[client]["cache_write"] = cw or 0
        result[client]["success_requests"] = sc or 0
    return result


def get_token_client_hourly(
    conn: sqlite3.Connection,
    token: str | None,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict[int, dict[str, int]]]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)

    if token is None:
        where = "WHERE ts >= ? AND ts < ?"
        params: tuple = (start_hour, end_hour + 3600)
    else:
        where = "WHERE token = ? AND ts >= ? AND ts < ?"
        params = (token, start_hour, end_hour + 3600)

    req_rows = conn.execute(
        "SELECT client, ts - (ts % 3600) AS hour_ts, SUM(count) "
        "FROM client_usage_log "
        + where
        + " GROUP BY client, hour_ts ORDER BY client, hour_ts",
        params,
    ).fetchall()

    tok_rows = conn.execute(
        "SELECT client, ts - (ts % 3600) AS hour_ts, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write) "
        "FROM client_token_usage_log "
        + where
        + " GROUP BY client, hour_ts ORDER BY client, hour_ts",
        params,
    ).fetchall()

    by_client: dict[str, dict[int, dict[str, int]]] = {}
    for client, hour_ts, count in req_rows:
        by_client.setdefault(client, {}).setdefault(
            hour_ts,
            {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "requests": 0,
            },
        )["requests"] = count or 0
    for client, hour_ts, it, ot, cr, cw in tok_rows:
        slot = by_client.setdefault(client, {}).setdefault(
            hour_ts,
            {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "requests": 0,
            },
        )
        slot["input"] = it or 0
        slot["output"] = ot or 0
        slot["cache_read"] = cr or 0
        slot["cache_write"] = cw or 0
    return by_client


def record_success(conn: sqlite3.Connection, token: str, provider_name: str):
    now = int(time.time())
    min_ts = now - (now % 60)
    conn.execute(
        "INSERT INTO success_log (token, provider, ts, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(token, provider, ts) DO UPDATE SET count = count + 1",
        (token, provider_name, min_ts),
    )
    conn.execute(
        "UPDATE token_pool SET success_count = success_count + 1 WHERE token = ?",
        (token,),
    )
    conn.commit()


def record_token_usage(
    conn: sqlite3.Connection,
    token: str,
    provider_name: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    cache_read_weight: float = 0.0,
    cache_write_weight: float = 0.0,
    model: str = "",
):
    if (
        input_tokens <= 0
        and output_tokens <= 0
        and cache_read <= 0
        and cache_write <= 0
    ):
        return
    if not model:
        model = ""
    now = int(time.time())
    hour_ts = now - (now % 3600)
    conn.execute(
        "INSERT INTO token_usage_log "
        "(token, provider, model, ts, input_tokens, output_tokens, cache_read, cache_write) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(token, provider, model, ts) DO UPDATE SET "
        "input_tokens = input_tokens + excluded.input_tokens, "
        "output_tokens = output_tokens + excluded.output_tokens, "
        "cache_read = cache_read + excluded.cache_read, "
        "cache_write = cache_write + excluded.cache_write",
        (
            token,
            provider_name,
            model,
            hour_ts,
            max(0, input_tokens),
            max(0, output_tokens),
            max(0, cache_read),
            max(0, cache_write),
        ),
    )
    min_ts = now - (now % 60)
    rate_tokens = max(0, input_tokens) + max(0, output_tokens)
    cache_part = cache_read_weight * max(0, cache_read) + cache_write_weight * max(
        0, cache_write
    )
    if cache_part:
        rate_tokens = int(round(rate_tokens + cache_part))
    if rate_tokens > 0:
        conn.execute(
            "INSERT INTO token_rate_usage (token, ts, tokens) VALUES (?, ?, ?) "
            "ON CONFLICT(token, ts) DO UPDATE SET tokens = tokens + excluded.tokens",
            (token, min_ts, rate_tokens),
        )
        conn.execute(
            "UPDATE token_pool SET token_count = token_count + ? WHERE token = ?",
            (rate_tokens, token),
        )
    conn.commit()


_LIMIT_META: dict[str, dict] = {
    "rpm": {
        "label": "RPM",
        "unit": "requests",
        "window": "min",
        "desc": "requests per minute",
        "scale": 1,
    },
    "rph": {
        "label": "RPH",
        "unit": "requests",
        "window": "hour",
        "desc": "requests per hour",
        "scale": 1,
    },
    "rpd": {
        "label": "RPD",
        "unit": "requests",
        "window": "day",
        "desc": "requests per day",
        "scale": 1,
    },
    "rpw": {
        "label": "RPW",
        "unit": "requests",
        "window": "week",
        "desc": "requests per week",
        "scale": 1,
    },
    "rpmo": {
        "label": "RP月",
        "unit": "requests",
        "window": "month",
        "desc": "requests per month",
        "scale": 1,
    },
    "rpt": {
        "label": "RPT",
        "unit": "requests",
        "window": "total",
        "desc": "requests per total",
        "scale": 1,
    },
    "tpm": {
        "label": "TPM",
        "unit": "M tokens",
        "window": "min",
        "desc": "tokens per minute",
        "scale": 1_000_000,
    },
    "tph": {
        "label": "TPH",
        "unit": "M tokens",
        "window": "hour",
        "desc": "tokens per hour",
        "scale": 1_000_000,
    },
    "tpd": {
        "label": "TPD",
        "unit": "M tokens",
        "window": "day",
        "desc": "tokens per day",
        "scale": 1_000_000,
    },
    "tpw": {
        "label": "TPW",
        "unit": "M tokens",
        "window": "week",
        "desc": "tokens per week",
        "scale": 1_000_000,
    },
    "tpmo": {
        "label": "TP月",
        "unit": "M tokens",
        "window": "month",
        "desc": "tokens per month",
        "scale": 1_000_000,
    },
    "tpt": {
        "label": "TPT",
        "unit": "M tokens",
        "window": "total",
        "desc": "tokens per total",
        "scale": 1_000_000,
    },
}


def _window_starts(now: int) -> dict:
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    iso = now_dt.isocalendar()
    monday = date.fromisocalendar(iso.year, iso.week, 1)
    return {
        "min": now - (now % 60),
        "hour": now - (now % 3600),
        "day": now - (now % 86400),
        "week": int(
            datetime(
                monday.year, monday.month, monday.day, tzinfo=timezone.utc
            ).timestamp()
        ),
        "month": int(
            datetime(now_dt.year, now_dt.month, 1, tzinfo=timezone.utc).timestamp()
        ),
    }


def _reset_in(window: str, now: int) -> int | None:
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if window == "min":
        return 60 - (now % 60)
    if window == "hour":
        return 3600 - (now % 3600)
    if window == "day":
        return 86400 - (now % 86400)
    if window == "week":
        iso = now_dt.isocalendar()
        nxt = date.fromisocalendar(iso.year, iso.week, 1) + timedelta(days=7)
        return (
            int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc).timestamp())
            - now
        )
    if window == "month":
        year = now_dt.year + (1 if now_dt.month == 12 else 0)
        month = 1 if now_dt.month == 12 else now_dt.month + 1
        nxt = datetime(year, month, 1, tzinfo=timezone.utc)
        return int(nxt.timestamp()) - now
    return None


_USAGE_WINDOWS = ("min", "hour", "day", "week", "month")


def _needed_windows(entry) -> set[str]:
    needed: set[str] = set()
    for key in entry.RATE_LIMIT_FIELDS:
        if getattr(entry, key) == -1:
            continue
        window = _LIMIT_META[key]["window"]
        if window in _USAGE_WINDOWS:
            needed.add(window)
    return needed


def _sum_windows(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    token: str,
    starts: dict,
    wanted: list[str],
) -> dict:
    lo = min(starts[w] for w in wanted)
    cols = ", ".join(
        "COALESCE(SUM(CASE WHEN ts >= ? THEN " + column + " ELSE 0 END), 0)"
        for _ in wanted
    )
    params = tuple(starts[w] for w in wanted) + (token, lo)
    row = conn.execute(
        f"SELECT {cols} FROM {table} WHERE token = ? AND ts >= ?", params
    ).fetchone()
    return dict(zip(wanted, row)) if row else {}


def _compute_used(
    conn: sqlite3.Connection, token: str, windows: set[str] | None = None
) -> dict:
    now = int(time.time())
    starts = _window_starts(now)
    wanted = [w for w in _USAGE_WINDOWS if windows is None or w in windows]

    pool_row = conn.execute(
        "SELECT COALESCE(success_count, 0), COALESCE(token_count, 0) "
        "FROM token_pool WHERE token = ?",
        (token,),
    ).fetchone()
    success_total = pool_row[0] if pool_row else 0
    token_total = pool_row[1] if pool_row else 0

    req_by_window = {w: 0 for w in _USAGE_WINDOWS}
    tok_by_window = {w: 0 for w in _USAGE_WINDOWS}
    if wanted:
        req_by_window.update(
            _sum_windows(conn, "success_log", "count", token, starts, wanted)
        )
        tok_by_window.update(
            _sum_windows(conn, "token_rate_usage", "tokens", token, starts, wanted)
        )

    return {
        "rpm": req_by_window["min"],
        "rph": req_by_window["hour"],
        "rpd": req_by_window["day"],
        "rpw": req_by_window["week"],
        "rpmo": req_by_window["month"],
        "rpt": success_total,
        "tpm": tok_by_window["min"],
        "tph": tok_by_window["hour"],
        "tpd": tok_by_window["day"],
        "tpw": tok_by_window["week"],
        "tpmo": tok_by_window["month"],
        "tpt": token_total,
    }


def _limit_view(key: str, limit_raw: int, used_raw: int, now: int) -> dict:
    meta = _LIMIT_META[key]
    scale = meta["scale"]
    if limit_raw > 0:
        percent = round(used_raw / limit_raw * 100, 1)
    elif limit_raw == 0:
        percent = 100.0 if used_raw > 0 else 0.0
    else:
        percent = 0.0
    return {
        "key": key,
        "label": meta["label"],
        "description": meta["desc"],
        "unit": meta["unit"],
        "limit": round(limit_raw / scale, 2),
        "used": round(used_raw / scale, 2),
        "percent": percent,
        "reset_in": _reset_in(meta["window"], now),
    }


def check_rate_limit(
    conn: sqlite3.Connection,
    entry: TokenEntry,
) -> tuple[bool, str | None, dict]:
    if not entry.has_limits:
        return True, None, {}
    used = _compute_used(conn, entry.token, _needed_windows(entry))
    now = int(time.time())
    quota: dict = {}
    for key in TokenEntry.RATE_LIMIT_FIELDS:
        limit = getattr(entry, key)
        if limit == -1:
            continue
        quota[key] = _limit_view(key, limit, used[key], now)
    for key in TokenEntry.RATE_LIMIT_FIELDS:
        limit = getattr(entry, key)
        if limit == -1:
            continue
        if limit == 0 or (limit > 0 and used[key] >= limit):
            return False, key, quota
    return True, None, quota


def get_rate_limit_status(conn: sqlite3.Connection, entry: TokenEntry) -> list[dict]:
    if not entry.has_limits:
        return []
    used = _compute_used(conn, entry.token, _needed_windows(entry))
    now = int(time.time())
    out: list[dict] = []
    for key in TokenEntry.RATE_LIMIT_FIELDS:
        limit = getattr(entry, key)
        if limit == -1:
            continue
        out.append(_limit_view(key, limit, used[key], now))
    return out


def get_all_tokens(conn: sqlite3.Connection) -> list[TokenEntry]:
    cur = conn.execute(
        "SELECT token, providers, is_admin, enabled, created_at, describe, "
        "rpm, rph, rpd, rpw, rpmo, rpt, tpm, tph, tpd, tpw, tpmo, tpt, "
        "success_count, token_count, cache_read_weight, cache_write_weight, models "
        "FROM token_pool "
        "ORDER BY created_at DESC"
    )
    return [TokenEntry.from_db_row(row) for row in cur.fetchall()]


def get_token(conn: sqlite3.Connection, token: str) -> TokenEntry | None:
    cur = conn.execute(
        "SELECT token, providers, is_admin, enabled, created_at, describe, "
        "rpm, rph, rpd, rpw, rpmo, rpt, tpm, tph, tpd, tpw, tpmo, tpt, "
        "success_count, token_count, cache_read_weight, cache_write_weight, models "
        "FROM token_pool WHERE token = ?",
        (token,),
    )
    row = cur.fetchone()
    return TokenEntry.from_db_row(row) if row else None


def is_admin_token(conn: sqlite3.Connection, token: str) -> bool:
    row = conn.execute(
        "SELECT is_admin, enabled FROM token_pool WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return False
    return bool(row[0]) and bool(row[1])


def add_token(
    conn: sqlite3.Connection,
    token: str,
    providers: list[str],
    is_admin: bool = False,
    describe: str = "",
    rpm: int = -1,
    rph: int = -1,
    rpd: int = -1,
    rpw: int = -1,
    rpmo: int = -1,
    rpt: int = -1,
    tpm: int = -1,
    tph: int = -1,
    tpd: int = -1,
    tpw: int = -1,
    tpmo: int = -1,
    tpt: int = -1,
    cache_read_weight: float = -1,
    cache_write_weight: float = -1,
    models: list[str] | None = None,
) -> TokenEntry:
    entry = TokenEntry(
        token=token,
        providers=providers,
        is_admin=is_admin,
        describe=describe,
        rpm=rpm,
        rph=rph,
        rpd=rpd,
        rpw=rpw,
        rpmo=rpmo,
        rpt=rpt,
        tpm=tpm,
        tph=tph,
        tpd=tpd,
        tpw=tpw,
        tpmo=tpmo,
        tpt=tpt,
        cache_read_weight=cache_read_weight,
        cache_write_weight=cache_write_weight,
        models=models,
    )
    conn.execute(
        "INSERT OR IGNORE INTO token_pool (token, providers, is_admin, enabled, created_at, describe, "
        "rpm, rph, rpd, rpw, rpmo, rpt, tpm, tph, tpd, tpw, tpmo, tpt, "
        "success_count, token_count, cache_read_weight, cache_write_weight, models) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token,
            json.dumps(providers),
            1 if is_admin else 0,
            1,
            entry.created_at,
            describe,
            rpm,
            rph,
            rpd,
            rpw,
            rpmo,
            rpt,
            tpm,
            tph,
            tpd,
            tpw,
            tpmo,
            tpt,
            0,
            0,
            cache_read_weight,
            cache_write_weight,
            json.dumps(entry.models),
        ),
    )
    conn.commit()
    return entry


def update_token(
    conn: sqlite3.Connection,
    token: str,
    providers: list[str] | None = None,
    is_admin: bool | None = None,
    enabled: bool | None = None,
    describe: str | None = None,
    rpm: int | None = None,
    rph: int | None = None,
    rpd: int | None = None,
    rpw: int | None = None,
    rpmo: int | None = None,
    rpt: int | None = None,
    tpm: int | None = None,
    tph: int | None = None,
    tpd: int | None = None,
    tpw: int | None = None,
    tpmo: int | None = None,
    tpt: int | None = None,
    cache_read_weight: float | None = None,
    cache_write_weight: float | None = None,
    models: list[str] | None = None,
    new_token: str | None = None,
) -> TokenEntry | None:
    entry = get_token(conn, token)
    if not entry:
        return None

    # Resolve an optional token rename. ``new_token`` semantics:
    #   None  -> not provided, keep current token (e.g. toggleEnabled path)
    #   ""    -> empty, auto-generate a fresh token (regenerate)
    #   ==token -> no-op
    #   else  -> rename to this value (must be unique)
    rename_to: str | None = None
    if new_token is not None:
        nt = new_token.strip()
        if not nt:
            from src.pool import make_token

            nt = make_token()
        if nt != token:
            exists = conn.execute(
                "SELECT 1 FROM token_pool WHERE token = ?", (nt,)
            ).fetchone()
            if exists:
                raise ValueError("token already exists")
            rename_to = nt
            entry.token = nt

    if providers is not None:
        entry.providers = providers
    if is_admin is not None:
        entry.is_admin = is_admin
    if enabled is not None:
        entry.enabled = enabled
    if describe is not None:
        entry.describe = describe
    if rpm is not None:
        entry.rpm = rpm
    if rph is not None:
        entry.rph = rph
    if rpd is not None:
        entry.rpd = rpd
    if rpw is not None:
        entry.rpw = rpw
    if rpmo is not None:
        entry.rpmo = rpmo
    if rpt is not None:
        entry.rpt = rpt
    if tpm is not None:
        entry.tpm = tpm
    if tph is not None:
        entry.tph = tph
    if tpd is not None:
        entry.tpd = tpd
    if tpw is not None:
        entry.tpw = tpw
    if tpmo is not None:
        entry.tpmo = tpmo
    if tpt is not None:
        entry.tpt = tpt
    if cache_read_weight is not None:
        entry.cache_read_weight = cache_read_weight
    if cache_write_weight is not None:
        entry.cache_write_weight = cache_write_weight
    if models is not None:
        entry.models = models

    if rename_to:
        # token is the PRIMARY KEY of token_pool -> update it explicitly.
        conn.execute(
            "UPDATE token_pool SET token=?, providers=?, is_admin=?, enabled=?, describe=?, "
            "rpm=?, rph=?, rpd=?, rpw=?, rpmo=?, rpt=?, "
            "tpm=?, tph=?, tpd=?, tpw=?, tpmo=?, tpt=?, "
            "cache_read_weight=?, cache_write_weight=?, models=? WHERE token=?",
            (
                rename_to,
                json.dumps(entry.providers),
                1 if entry.is_admin else 0,
                1 if entry.enabled else 0,
                entry.describe,
                entry.rpm,
                entry.rph,
                entry.rpd,
                entry.rpw,
                entry.rpmo,
                entry.rpt,
                entry.tpm,
                entry.tph,
                entry.tpd,
                entry.tpw,
                entry.tpmo,
                entry.tpt,
                entry.cache_read_weight,
                entry.cache_write_weight,
                json.dumps(entry.models),
                token,
            ),
        )
        # Keep usage history and model mappings attached to the renamed token.
        for tbl in (
            "request_log",
            "success_log",
            "token_usage_log",
            "token_rate_usage",
        ):
            conn.execute(f"UPDATE {tbl} SET token=? WHERE token=?", (rename_to, token))
        conn.execute("UPDATE model_map SET token=? WHERE token=?", (rename_to, token))
    else:
        conn.execute(
            "UPDATE token_pool SET providers=?, is_admin=?, enabled=?, describe=?, "
            "rpm=?, rph=?, rpd=?, rpw=?, rpmo=?, rpt=?, "
            "tpm=?, tph=?, tpd=?, tpw=?, tpmo=?, tpt=?, "
            "cache_read_weight=?, cache_write_weight=?, models=? WHERE token=?",
            (
                json.dumps(entry.providers),
                1 if entry.is_admin else 0,
                1 if entry.enabled else 0,
                entry.describe,
                entry.rpm,
                entry.rph,
                entry.rpd,
                entry.rpw,
                entry.rpmo,
                entry.rpt,
                entry.tpm,
                entry.tph,
                entry.tpd,
                entry.tpw,
                entry.tpmo,
                entry.tpt,
                entry.cache_read_weight,
                entry.cache_write_weight,
                json.dumps(entry.models),
                token,
            ),
        )
    conn.commit()
    return entry


def delete_token(conn: sqlite3.Connection, token: str) -> bool:
    cur = conn.execute("DELETE FROM token_pool WHERE token = ?", (token,))
    conn.commit()
    return cur.rowcount > 0


def get_tokens_usage_summary(conn: sqlite3.Connection) -> dict[str, dict]:
    now = int(time.time())
    thresholds = {
        "5h": now - 5 * 3600,
        "1d": now - 1 * 86400,
        "7d": now - 7 * 86400,
    }
    result: dict[str, dict] = {}
    for key, ts_threshold in thresholds.items():
        req_rows = conn.execute(
            "SELECT token, SUM(count) FROM request_log WHERE ts >= ? GROUP BY token",
            (ts_threshold,),
        ).fetchall()
        for token, count in req_rows:
            result.setdefault(token, {})
            result[token].setdefault(key, {"requests": 0, "tokens": {}})
            result[token][key]["requests"] = count or 0

        tok_rows = conn.execute(
            "SELECT token, provider, "
            "SUM(input_tokens), SUM(output_tokens), "
            "SUM(cache_read), SUM(cache_write) "
            "FROM token_usage_log WHERE ts >= ? "
            "GROUP BY token, provider",
            (ts_threshold,),
        ).fetchall()
        for token, provider, it, ot, cr, cw in tok_rows:
            result.setdefault(token, {})
            result[token].setdefault(key, {"requests": 0, "tokens": {}})
            result[token][key]["tokens"][provider] = {
                "input": it or 0,
                "output": ot or 0,
                "cache_read": cr or 0,
                "cache_write": cw or 0,
            }
    return result


def get_token_usage_detail(
    conn: sqlite3.Connection,
    token: str | None,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict[str, int]]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)

    if token is None:
        where = "WHERE ts >= ? AND ts < ?"
        params: tuple = (start_hour, end_hour + 3600)
    else:
        where = "WHERE token = ? AND ts >= ? AND ts < ?"
        params = (token, start_hour, end_hour + 3600)

    req_rows = conn.execute(
        "SELECT provider, SUM(count) FROM success_log " + where + " GROUP BY provider",
        params,
    ).fetchall()

    tok_rows = conn.execute(
        "SELECT provider, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write) "
        "FROM token_usage_log " + where + " GROUP BY provider",
        params,
    ).fetchall()

    result: dict[str, dict[str, int]] = {}
    providers = {r[0] for r in req_rows} | {r[0] for r in tok_rows}
    for provider in providers:
        result[provider] = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "requests": 0,
        }
    for provider, count in req_rows:
        result[provider]["requests"] = count or 0
    for provider, it, ot, cr, cw in tok_rows:
        result[provider]["input"] = it or 0
        result[provider]["output"] = ot or 0
        result[provider]["cache_read"] = cr or 0
        result[provider]["cache_write"] = cw or 0
    return result


def record_status(conn: sqlite3.Connection, provider: str, status_code: int):
    now = int(time.time())
    hour_ts = now - (now % 3600)
    conn.execute(
        "INSERT INTO status_log (provider, status_code, ts, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(provider, status_code, ts) DO UPDATE SET count = count + 1",
        (provider, status_code, hour_ts),
    )
    conn.commit()


def get_provider_status(conn: sqlite3.Connection, hours: int = 168) -> dict:
    now = int(time.time())
    current_hour_ts = now - (now % 3600)
    start_hour_ts = current_hour_ts - (hours - 1) * 3600
    thresholds = {
        "1h": now - 3600,
        "24h": now - 86400,
        "7d": now - 7 * 86400,
        "30d": now - 30 * 86400,
    }

    all_providers_row = conn.execute(
        "SELECT DISTINCT provider FROM status_log WHERE ts >= ?", (start_hour_ts,)
    ).fetchall()
    provider_names = [r[0] for r in all_providers_row]

    all_providers_for_summary = conn.execute(
        "SELECT DISTINCT provider FROM status_log"
    ).fetchall()
    all_provider_names = [r[0] for r in all_providers_for_summary]

    result: dict[str, dict] = {"hourly": {}}
    for pname in provider_names:
        rows = conn.execute(
            "SELECT ts, status_code, count FROM status_log "
            "WHERE provider = ? AND ts >= ? ORDER BY ts, status_code",
            (pname, start_hour_ts),
        ).fetchall()

        by_hour: dict[int, list[tuple[int, int]]] = {}
        for ts, sc, count in rows:
            by_hour.setdefault(ts, []).append((sc, count))

        hourly_data = []
        t = start_hour_ts
        while t <= current_hour_ts:
            entries = by_hour.get(t, [])
            total = sum(c for _, c in entries)
            success = sum(c for sc, c in entries if 200 <= sc < 300)
            errors = {str(sc): c for sc, c in entries if sc >= 400}
            hourly_data.append(
                {
                    "ts": t,
                    "total": total,
                    "success": success,
                    "success_rate": round(success / total * 100, 2)
                    if total > 0
                    else None,
                    "error_distribution": errors,
                }
            )
            t += 3600
        result["hourly"][pname] = hourly_data

    for pname in all_provider_names:
        result[pname] = {}
        for period, ts_cutoff in thresholds.items():
            rows = conn.execute(
                "SELECT status_code, SUM(count) FROM status_log "
                "WHERE provider = ? AND ts >= ? GROUP BY status_code "
                "ORDER BY status_code",
                (pname, ts_cutoff),
            ).fetchall()
            total = sum(c for _, c in rows)
            success = sum(c for sc, c in rows if 200 <= sc < 300)
            error_dist = {str(sc): c for sc, c in rows if sc >= 400}
            result[pname][period] = {
                "total": total,
                "success": success,
                "success_rate": round(success / total * 100, 2) if total > 0 else None,
                "error_distribution": error_dist,
            }
    return result


def get_model_maps(conn: sqlite3.Connection, token: str) -> list[dict]:
    cur = conn.execute(
        "SELECT pattern, target, provider FROM model_map WHERE token = ? ORDER BY pattern",
        (token,),
    )
    return [
        {"pattern": row[0], "target": row[1], "provider": row[2]}
        for row in cur.fetchall()
    ]


def set_model_map(
    conn: sqlite3.Connection,
    token: str,
    pattern: str,
    target: str,
    provider: str = "",
):
    conn.execute(
        "INSERT INTO model_map (token, pattern, target, provider) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(token, pattern) DO UPDATE SET target = excluded.target, provider = excluded.provider",
        (token, pattern, target, provider),
    )
    conn.commit()


def delete_model_map(conn: sqlite3.Connection, token: str, pattern: str) -> bool:
    cur = conn.execute(
        "DELETE FROM model_map WHERE token = ? AND pattern = ?",
        (token, pattern),
    )
    conn.commit()
    return cur.rowcount > 0


def resolve_model_ex(
    conn: sqlite3.Connection, token: str, model: str
) -> tuple[str, str, bool]:
    if not model:
        return model, "", False
    rows = conn.execute(
        "SELECT pattern, target, provider FROM model_map WHERE token = ?",
        (token,),
    ).fetchall()
    for pattern, target, provider in rows:
        try:
            m = re.fullmatch(pattern, model)
        except re.error:
            m = None
        if m:
            result = target
            for i in range(len(m.groups()), 0, -1):
                result = result.replace(f"${i}", m.group(i) if m.group(i) else "")
            return result, provider, True
        if fnmatch.fnmatch(model, pattern):
            return target, provider, True
    return model, "", False


def resolve_model(conn: sqlite3.Connection, token: str, model: str) -> tuple[str, str]:
    target, provider, _ = resolve_model_ex(conn, token, model)
    return target, provider


def get_site_models(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    query = "SELECT model, provider, target, enabled FROM site_model"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY model"
    rows = conn.execute(query).fetchall()
    return [
        {
            "model": row[0],
            "provider": row[1],
            "target": row[2],
            "enabled": bool(row[3]),
        }
        for row in rows
    ]


def set_site_model(
    conn: sqlite3.Connection,
    model: str,
    provider: str,
    target: str = "",
    enabled: bool = True,
):
    conn.execute(
        "INSERT INTO site_model (model, provider, target, enabled) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(model) DO UPDATE SET provider = excluded.provider, "
        "target = excluded.target, enabled = excluded.enabled",
        (model, provider, target, 1 if enabled else 0),
    )
    conn.commit()


def delete_site_model(conn: sqlite3.Connection, model: str) -> bool:
    cur = conn.execute("DELETE FROM site_model WHERE model = ?", (model,))
    conn.commit()
    return cur.rowcount > 0


_SITE_MODEL_PATTERN_CHARS = frozenset("*?[](){}^$|\\+")


def is_site_model_pattern(model: str) -> bool:
    return any(ch in _SITE_MODEL_PATTERN_CHARS for ch in model)


def resolve_site_model_exact(conn: sqlite3.Connection, model: str) -> dict | None:
    row = conn.execute(
        "SELECT model, provider, target, enabled FROM site_model "
        "WHERE model = ? AND enabled = 1",
        (model,),
    ).fetchone()
    if not row:
        return None
    return {
        "model": row[0],
        "provider": row[1],
        "target": row[2],
        "enabled": bool(row[3]),
    }


def resolve_site_model(conn: sqlite3.Connection, model: str) -> dict | None:
    return resolve_site_model_exact(conn, model)


def _substitute_captures(target: str, match: re.Match) -> str:
    result = target
    for i in range(len(match.groups()), 0, -1):
        result = result.replace(f"${i}", match.group(i) or "")
    return result


def resolve_site_model_pattern(conn: sqlite3.Connection, model: str) -> dict | None:
    exact = resolve_site_model_exact(conn, model)
    if exact:
        exact["target"] = exact["target"] or model
        return exact
    if not model:
        return None
    rows = conn.execute(
        "SELECT model, provider, target FROM site_model "
        "WHERE enabled = 1 ORDER BY LENGTH(model) DESC, model"
    ).fetchall()
    for pattern, provider, target in rows:
        if not is_site_model_pattern(pattern):
            continue
        try:
            match = re.fullmatch(pattern, model)
        except re.error:
            match = None
        if match:
            resolved = _substitute_captures(target, match)
            return {
                "model": pattern,
                "provider": provider,
                "target": resolved or model,
                "enabled": True,
            }
        if fnmatch.fnmatch(model, pattern):
            return {
                "model": pattern,
                "provider": provider,
                "target": target or model,
                "enabled": True,
            }
    return None


def get_all_pricing(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT model, input_cache_hit, input_cache_miss, "
        "output_cache_hit, output_cache_miss FROM model_pricing "
        "ORDER BY model"
    ).fetchall()
    return [
        {
            "model": r[0],
            "input_cache_hit": r[1],
            "input_cache_miss": r[2],
            "output_cache_hit": r[3],
            "output_cache_miss": r[4],
        }
        for r in rows
    ]


def get_pricing_map(conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in get_all_pricing(conn):
        out[p["model"]] = {
            "input_cache_hit": p["input_cache_hit"],
            "input_cache_miss": p["input_cache_miss"],
            "output_cache_hit": p["output_cache_hit"],
            "output_cache_miss": p["output_cache_miss"],
        }
    return out


def set_pricing(
    conn: sqlite3.Connection,
    model: str,
    input_cache_hit: float,
    input_cache_miss: float,
    output_cache_hit: float,
    output_cache_miss: float,
) -> None:
    conn.execute(
        "INSERT INTO model_pricing "
        "(model, input_cache_hit, input_cache_miss, output_cache_hit, output_cache_miss) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(model) DO UPDATE SET "
        "input_cache_hit = excluded.input_cache_hit, "
        "input_cache_miss = excluded.input_cache_miss, "
        "output_cache_hit = excluded.output_cache_hit, "
        "output_cache_miss = excluded.output_cache_miss",
        (model, input_cache_hit, input_cache_miss, output_cache_hit, output_cache_miss),
    )
    conn.commit()


def delete_pricing(conn: sqlite3.Connection, model: str) -> bool:
    cur = conn.execute("DELETE FROM model_pricing WHERE model = ?", (model,))
    conn.commit()
    return cur.rowcount > 0


def get_token_usage_by_model(
    conn: sqlite3.Connection,
    token: str | None,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict[str, int]]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)

    if token is None:
        where = "WHERE ts >= ? AND ts < ?"
        params: tuple = (start_hour, end_hour + 3600)
    else:
        where = "WHERE token = ? AND ts >= ? AND ts < ?"
        params = (token, start_hour, end_hour + 3600)

    rows = conn.execute(
        "SELECT model, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write) "
        "FROM token_usage_log " + where + " GROUP BY model",
        params,
    ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for model, it, ot, cr, cw in rows:
        result[model] = {
            "input": it or 0,
            "output": ot or 0,
            "cache_read": cr or 0,
            "cache_write": cw or 0,
            "requests": 0,
        }
    # token_usage_log has no request count column; derive requests from
    # client_token_usage_log counts grouped by model when available.
    req_by_model = conn.execute(
        "SELECT model, SUM(count) FROM client_token_usage_log "
        + where
        + " GROUP BY model",
        params,
    ).fetchall()
    for model, cnt in req_by_model:
        if model in result:
            result[model]["requests"] = cnt or 0
        else:
            result.setdefault(
                model,
                {
                    "input": 0,
                    "output": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "requests": cnt or 0,
                },
            )
    return result


def compute_cost(
    usage_row: dict,
    price_row: dict,
) -> float:
    input_miss = usage_row.get("input") or 0
    input_hit = usage_row.get("cache_read") or 0
    output_miss = max(
        0, (usage_row.get("output") or 0) - (usage_row.get("cache_write") or 0)
    )
    output_hit = usage_row.get("cache_write") or 0
    total = (
        input_miss * price_row["input_cache_miss"]
        + input_hit * price_row["input_cache_hit"]
        + output_miss * price_row["output_cache_miss"]
        + output_hit * price_row["output_cache_hit"]
    )
    return total / 1_000_000
