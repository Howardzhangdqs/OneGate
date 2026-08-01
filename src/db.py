from __future__ import annotations

import fnmatch
import re
import json
import os
import shutil
import sqlite3
import time

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
        ("rpm", -1),
        ("rph", -1),
        ("rpd", -1),
        ("rpt", -1),
        ("success_count", 0),
    ]
    for col, default in rate_cols:
        if col not in cols:
            print(f"[DB] Migrating: adding '{col}' column to token_pool")
            conn.execute(
                f"ALTER TABLE token_pool ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}"
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
        "CREATE TABLE IF NOT EXISTS token_usage_log ("
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
):
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
        "INSERT INTO token_usage_log "
        "(token, provider, ts, input_tokens, output_tokens, cache_read, cache_write) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(token, provider, ts) DO UPDATE SET "
        "input_tokens = input_tokens + excluded.input_tokens, "
        "output_tokens = output_tokens + excluded.output_tokens, "
        "cache_read = cache_read + excluded.cache_read, "
        "cache_write = cache_write + excluded.cache_write",
        (
            token,
            provider_name,
            hour_ts,
            max(0, input_tokens),
            max(0, output_tokens),
            max(0, cache_read),
            max(0, cache_write),
        ),
    )
    conn.commit()


def check_rate_limit(
    conn: sqlite3.Connection,
    token: str,
    rpm: int,
    rph: int,
    rpd: int,
    rpt: int,
) -> tuple[bool, str | None, dict]:
    now = int(time.time())
    current_min_ts = now - (now % 60)
    current_hour_ts = now - (now % 3600)
    current_day_ts = now - (now % 86400)

    used_rpm = conn.execute(
        "SELECT COALESCE(count, 0) FROM success_log WHERE token = ? AND ts = ?",
        (token, current_min_ts),
    ).fetchone()
    used_rpm = used_rpm[0] if used_rpm else 0

    used_rph = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM success_log WHERE token = ? AND ts >= ?",
        (token, current_hour_ts),
    ).fetchone()[0]

    used_rpd = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM success_log WHERE token = ? AND ts >= ?",
        (token, current_day_ts),
    ).fetchone()[0]

    used_rpt = conn.execute(
        "SELECT COALESCE(success_count, 0) FROM token_pool WHERE token = ?",
        (token,),
    ).fetchone()[0]

    reset_in_rpm = 60 - (now % 60)
    reset_in_rph = 3600 - (now % 3600)
    reset_in_rpd = 86400 - (now % 86400)

    quota: dict = {}

    def add_quota(key: str, limit: int, used: int, reset_in: int | None, desc: str):
        if limit == -1:
            return
        quota[key] = {
            "description": desc,
            "limit": limit,
            "used": used,
            "reset_in": reset_in,
        }

    add_quota("rpm", rpm, used_rpm, reset_in_rpm, "requests per minute")
    add_quota("rph", rph, used_rph, reset_in_rph, "requests per hour")
    add_quota("rpd", rpd, used_rpd, reset_in_rpd, "requests per day")
    add_quota("rpt", rpt, used_rpt, None, "requests per total")

    if rpm == 0 or (rpm > 0 and used_rpm >= rpm):
        return False, "rpm", quota
    if rph == 0 or (rph > 0 and used_rph >= rph):
        return False, "rph", quota
    if rpd == 0 or (rpd > 0 and used_rpd >= rpd):
        return False, "rpd", quota
    if rpt == 0 or (rpt > 0 and used_rpt >= rpt):
        return False, "rpt", quota

    return True, None, quota


def get_all_tokens(conn: sqlite3.Connection) -> list[TokenEntry]:
    cur = conn.execute(
        "SELECT token, providers, is_admin, enabled, created_at, describe, "
        "rpm, rph, rpd, rpt, success_count FROM token_pool "
        "ORDER BY created_at DESC"
    )
    return [TokenEntry.from_db_row(row) for row in cur.fetchall()]


def get_token(conn: sqlite3.Connection, token: str) -> TokenEntry | None:
    cur = conn.execute(
        "SELECT token, providers, is_admin, enabled, created_at, describe, "
        "rpm, rph, rpd, rpt, success_count FROM token_pool WHERE token = ?",
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
    rpt: int = -1,
) -> TokenEntry:
    entry = TokenEntry(
        token=token,
        providers=providers,
        is_admin=is_admin,
        describe=describe,
        rpm=rpm,
        rph=rph,
        rpd=rpd,
        rpt=rpt,
    )
    conn.execute(
        "INSERT OR IGNORE INTO token_pool (token, providers, is_admin, enabled, created_at, describe, "
        "rpm, rph, rpd, rpt, success_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            rpt,
            0,
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
    rpt: int | None = None,
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
    if rpt is not None:
        entry.rpt = rpt

    if rename_to:
        # token is the PRIMARY KEY of token_pool -> update it explicitly.
        conn.execute(
            "UPDATE token_pool SET token=?, providers=?, is_admin=?, enabled=?, describe=?, "
            "rpm=?, rph=?, rpd=?, rpt=? WHERE token=?",
            (
                rename_to,
                json.dumps(entry.providers),
                1 if entry.is_admin else 0,
                1 if entry.enabled else 0,
                entry.describe,
                entry.rpm,
                entry.rph,
                entry.rpd,
                entry.rpt,
                token,
            ),
        )
        # Keep usage history and model mappings attached to the renamed token.
        for tbl in ("request_log", "success_log", "token_usage_log"):
            conn.execute(f"UPDATE {tbl} SET token=? WHERE token=?", (rename_to, token))
        conn.execute("UPDATE model_map SET token=? WHERE token=?", (rename_to, token))
    else:
        conn.execute(
            "UPDATE token_pool SET providers=?, is_admin=?, enabled=?, describe=?, "
            "rpm=?, rph=?, rpd=?, rpt=? WHERE token=?",
            (
                json.dumps(entry.providers),
                1 if entry.is_admin else 0,
                1 if entry.enabled else 0,
                entry.describe,
                entry.rpm,
                entry.rph,
                entry.rpd,
                entry.rpt,
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
        "1d": now - 1 * 86400,
        "7d": now - 7 * 86400,
        "30d": now - 30 * 86400,
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
    token: str,
    start_ts: int,
    end_ts: int,
) -> dict[str, dict[str, int]]:
    start_hour = start_ts - (start_ts % 3600)
    end_hour = end_ts - (end_ts % 3600)

    req_rows = conn.execute(
        "SELECT provider, SUM(count) FROM success_log "
        "WHERE token = ? AND ts >= ? AND ts < ? GROUP BY provider",
        (token, start_hour, end_hour + 3600),
    ).fetchall()

    tok_rows = conn.execute(
        "SELECT provider, "
        "SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_read), SUM(cache_write) "
        "FROM token_usage_log WHERE token = ? AND ts >= ? AND ts < ? "
        "GROUP BY provider",
        (token, start_hour, end_hour + 3600),
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


def resolve_model(conn: sqlite3.Connection, token: str, model: str) -> tuple[str, str]:
    if not model:
        return model, ""
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
            return result, provider
        if fnmatch.fnmatch(model, pattern):
            return target, provider
    return model, ""
