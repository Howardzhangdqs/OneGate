from __future__ import annotations

import fnmatch
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
        rpw: int = -1,
        rpmo: int = -1,
        rpt: int = -1,
        tpm: int = -1,
        tph: int = -1,
        tpd: int = -1,
        tpw: int = -1,
        tpmo: int = -1,
        tpt: int = -1,
        success_count: int = 0,
        token_count: int = 0,
        cache_read_weight: float = -1,
        cache_write_weight: float = -1,
        models: list[str] | None = None,
    ):
        self.token = token
        self.providers: list[str] = providers if providers is not None else ["*"]
        self.models: list[str] = models if models is not None else ["*"]
        self.is_admin = is_admin
        self.enabled = enabled
        self.created_at = created_at or int(time.time())
        self.describe = describe
        self.rpm = rpm
        self.rph = rph
        self.rpd = rpd
        self.rpw = rpw
        self.rpmo = rpmo
        self.rpt = rpt
        self.tpm = tpm
        self.tph = tph
        self.tpd = tpd
        self.tpw = tpw
        self.tpmo = tpmo
        self.tpt = tpt
        self.success_count = success_count
        self.token_count = token_count
        self.cache_read_weight = cache_read_weight
        self.cache_write_weight = cache_write_weight

    RATE_LIMIT_FIELDS = (
        "rpm",
        "rph",
        "rpd",
        "rpw",
        "rpmo",
        "rpt",
        "tpm",
        "tph",
        "tpd",
        "tpw",
        "tpmo",
        "tpt",
    )

    @property
    def has_limits(self) -> bool:
        return any(getattr(self, f) != -1 for f in self.RATE_LIMIT_FIELDS)

    def has_permission(self, provider: Provider) -> bool:
        if not self.enabled:
            return False
        if "*" in self.providers:
            return True
        return provider.name in self.providers

    def has_model_permission(self, model: str) -> bool:
        if not self.enabled:
            return False
        if "*" in self.models:
            return True
        return any(m == model or fnmatch.fnmatch(model, m) for m in self.models)

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
            self.rpw,
            self.rpmo,
            self.rpt,
            self.tpm,
            self.tph,
            self.tpd,
            self.tpw,
            self.tpmo,
            self.tpt,
            self.success_count,
            self.token_count,
            self.cache_read_weight,
            self.cache_write_weight,
            json.dumps(self.models),
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
            rpm=row[6] if len(row) > 6 else -1,
            rph=row[7] if len(row) > 7 else -1,
            rpd=row[8] if len(row) > 8 else -1,
            rpw=row[9] if len(row) > 9 else -1,
            rpmo=row[10] if len(row) > 10 else -1,
            rpt=row[11] if len(row) > 11 else -1,
            tpm=row[12] if len(row) > 12 else -1,
            tph=row[13] if len(row) > 13 else -1,
            tpd=row[14] if len(row) > 14 else -1,
            tpw=row[15] if len(row) > 15 else -1,
            tpmo=row[16] if len(row) > 16 else -1,
            tpt=row[17] if len(row) > 17 else -1,
            success_count=row[18] if len(row) > 18 else 0,
            token_count=row[19] if len(row) > 19 else 0,
            cache_read_weight=row[20] if len(row) > 20 else -1,
            cache_write_weight=row[21] if len(row) > 21 else -1,
            models=json.loads(row[22]) if len(row) > 22 and row[22] else None,
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
            "rpw": self.rpw,
            "rpmo": self.rpmo,
            "rpt": self.rpt,
            "tpm": self.tpm,
            "tph": self.tph,
            "tpd": self.tpd,
            "tpw": self.tpw,
            "tpmo": self.tpmo,
            "tpt": self.tpt,
            "success_count": self.success_count,
            "token_count": self.token_count,
            "cache_read_weight": self.cache_read_weight,
            "cache_write_weight": self.cache_write_weight,
            "models": self.models,
        }
