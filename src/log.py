from __future__ import annotations

import gzip
import json
import os
import zlib
from datetime import datetime, timezone

try:
    import brotli

    _HAS_BROTLI = True
except ImportError:
    _HAS_BROTLI = False

try:
    import zstandard

    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False

LOG_DIR = "logs"

_REDACT_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "www-authenticate",
    }
)


def redact_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _REDACT_HEADERS}


def _decompress_body(encoding: str, body: bytes) -> bytes:
    if not encoding:
        return body
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    if encoding == "br":
        if _HAS_BROTLI:
            return brotli.decompress(body)
    if encoding == "zstd":
        if _HAS_ZSTD:
            return zstandard.ZstdDecompressor().decompress(body)
    return body


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _try_parse_json_body(
    headers, body: bytes | None, decompress: bool = False
) -> bytes | dict | list | str | None:
    if not body:
        return None
    if decompress:
        encoding = headers.get("Content-Encoding", "") if headers else ""
        body = _decompress_body(encoding, body)
    content_type = headers.get("Content-Type", "") if headers else ""
    if "application/json" in content_type:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            pass
    return body.decode("utf-8", errors="replace")


def save_log(
    request_method: str,
    request_path: str,
    req_headers,
    req_body: bytes | None,
    resp_status: int,
    resp_headers: dict,
    resp_body: bytes | None,
    forward_headers: dict,
    target_url: str,
    decompress: bool = False,
):
    _ensure_log_dir()
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    log_entry = {
        "timestamp": now,
        "request": {
            "method": request_method,
            "path": request_path,
            "headers": redact_headers(req_headers),
            "body": _try_parse_json_body(req_headers, req_body, decompress=decompress),
        },
        "forward": {
            "url": target_url,
            "headers": redact_headers(forward_headers),
            "body": _try_parse_json_body(
                forward_headers, req_body, decompress=decompress
            ),
        },
        "response": {
            "status": resp_status,
            "headers": redact_headers(resp_headers),
            "body": _try_parse_json_body(
                resp_headers, resp_body, decompress=decompress
            ),
        },
    }
    log_file = os.path.join(LOG_DIR, f"{now}.json")
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(log_entry, f, ensure_ascii=False, indent=2)


def save_log_headers_only(
    request_method: str,
    request_path: str,
    req_headers,
    resp_status: int,
    resp_headers: dict,
    forward_headers: dict,
    provider_name: str,
):
    _ensure_log_dir()
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    ts_str = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}Z"
    log_entry = {
        "ts": ts_str,
        "method": request_method,
        "path": request_path,
        "provider": provider_name,
        "status": resp_status,
        "req_headers": redact_headers(req_headers),
        "forward_headers": redact_headers(forward_headers),
        "resp_headers": redact_headers(resp_headers),
    }
    log_file = os.path.join(LOG_DIR, f"{date_str}.jsonl")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
