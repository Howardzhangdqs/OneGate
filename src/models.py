from __future__ import annotations

import json
import time
from urllib.parse import urlparse


class Provider:
    def __init__(
        self,
        name: str,
        prefix: str,
        upstream: str,
        strip_prefix: bool,
        env_token: str = "",
        v1_base: str = "",
    ):
        self.name = name
        self.prefix = prefix
        self.upstream = upstream.rstrip("/")
        self.strip_prefix = strip_prefix
        self.env_token = env_token or f"TOKEN_{name.upper()}"
        self.v1_base = v1_base
        parsed = urlparse(self.upstream)
        self.host: str = parsed.netloc or ""
        self.origin: str = f"{parsed.scheme}://{parsed.netloc}"
        self.upstream_path: str = parsed.path.rstrip("/")


class TokenEntry:
    def __init__(
        self,
        token: str,
        providers: list[str] | None = None,
        is_admin: bool = False,
        enabled: bool = True,
        created_at: int | None = None,
        describe: str = "",
        rpm: int = -1,
        rph: int = -1,
        rpd: int = -1,
        rpt: int = -1,
        success_count: int = 0,
    ):
        self.token = token
        self.providers: list[str] = providers if providers is not None else ["*"]
        self.is_admin = is_admin
        self.enabled = enabled
        self.created_at = created_at or int(time.time())
        self.describe = describe
        self.rpm = rpm
        self.rph = rph
        self.rpd = rpd
        self.rpt = rpt
        self.success_count = success_count

    def has_permission(self, provider: Provider) -> bool:
        if not self.enabled:
            return False
        if "*" in self.providers:
            return True
        return provider.name in self.providers

    def to_db_row(self) -> tuple:
        return (
            self.token,
            json.dumps(self.providers),
            1 if self.is_admin else 0,
            1 if self.enabled else 0,
            self.created_at,
            self.describe,
            self.rpm,
            self.rph,
            self.rpd,
            self.rpt,
            self.success_count,
        )

    @staticmethod
    def from_db_row(row: tuple) -> TokenEntry:
        return TokenEntry(
            token=row[0],
            providers=json.loads(row[1]),
            is_admin=bool(row[2]),
            enabled=bool(row[3]),
            created_at=row[4],
            describe=row[5] if len(row) > 5 else "",
            rpm=row[6] if len(row) > 6 else 0,
            rph=row[7] if len(row) > 7 else 0,
            rpd=row[8] if len(row) > 8 else 0,
            rpt=row[9] if len(row) > 9 else 0,
            success_count=row[10] if len(row) > 10 else 0,
        )

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "providers": self.providers,
            "is_admin": self.is_admin,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "describe": self.describe,
            "rpm": self.rpm,
            "rph": self.rph,
            "rpd": self.rpd,
            "rpt": self.rpt,
            "success_count": self.success_count,
        }
