from __future__ import annotations

import re
import secrets
import string

import sqlite3

from src.models import Provider, TokenEntry

TOKEN_PATTERN = re.compile(r"^Bearer ([0-9a-f]{32})\.([A-Za-z0-9]{16})$")
_ALPHABET = string.ascii_letters + string.digits


def make_token() -> str:
    part1 = secrets.token_hex(16)
    part2 = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return f"{part1}.{part2}"


def load_pool_from_db(conn: sqlite3.Connection) -> dict[str, TokenEntry]:
    cur = conn.execute(
        "SELECT token, providers, is_admin, enabled, created_at, describe, "
        "rpm, rph, rpd, rpt, success_count FROM token_pool"
    )
    pool: dict[str, TokenEntry] = {}
    for row in cur.fetchall():
        entry = TokenEntry.from_db_row(row)
        pool[entry.token] = entry
    return pool


def resolve_token(
    auth_value: str,
    provider: Provider,
    token_pool: dict[str, TokenEntry],
    real_tokens: dict[str, str],
) -> tuple[str | None, str]:
    m = TOKEN_PATTERN.match(auth_value)
    if not m:
        return None, auth_value

    proxy_token = m.group(0)[7:]
    entry = token_pool.get(proxy_token)
    if not entry:
        return None, auth_value

    if not entry.has_permission(provider):
        return proxy_token, ""

    real_token = real_tokens.get(provider.name, "")
    if real_token:
        return proxy_token, f"Bearer {real_token}"

    return proxy_token, auth_value


def extract_token(auth_value: str) -> str | None:
    m = TOKEN_PATTERN.match(auth_value)
    if m:
        return m.group(0)[7:]
    return None
