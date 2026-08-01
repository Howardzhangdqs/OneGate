from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from aiohttp import web

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

CHUNK_INTERVAL = 0.3
BODY_LIMIT = 100 * 1024
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "www-authenticate",
    }
)


def filter_sensitive_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}


@dataclass
class StreamSession:
    session_id: str
    token: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    created_at: float = field(default_factory=time.time)


class ThroughputMonitor:
    def __init__(self, token_pool: dict):
        self.token_pool = token_pool
        self.sessions: dict[str, StreamSession] = {}
        self.token_sessions: dict[str, set[str]] = {}

    def is_active(self, token: str | None) -> bool:
        if not token:
            return False
        return bool(self.token_sessions.get(token))

    def create_session(self, token: str) -> str | None:
        entry = self.token_pool.get(token)
        if not entry:
            return None
        session_id = secrets.token_hex(16)
        session = StreamSession(session_id=session_id, token=token)
        self.sessions[session_id] = session
        self.token_sessions.setdefault(token, set()).add(session_id)
        logger.info(
            "throughput session created: %s... for token %s...",
            session_id[:8],
            token[:8],
        )
        return session_id

    def remove_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session:
            sids = self.token_sessions.get(session.token)
            if sids:
                sids.discard(session_id)
                if not sids:
                    del self.token_sessions[session.token]
            logger.info("throughput session removed: %s...", session_id[:8])

    def broadcast(self, token: str, event_type: str, data: dict) -> None:
        sids = self.token_sessions.get(token)
        if not sids:
            return
        payload = (
            f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        )
        encoded = payload.encode("utf-8")
        dead = []
        for sid in list(sids):
            session = self.sessions.get(sid)
            if not session:
                dead.append(sid)
                continue
            try:
                session.queue.put_nowait(encoded)
            except asyncio.QueueFull:
                dead.append(sid)
        for sid in dead:
            self.remove_session(sid)


def create_throughput_handlers(monitor: ThroughputMonitor, token_pool: dict):
    async def serve_throughput_page(request: web.Request) -> web.Response:
        html_path = os.path.join(STATIC_DIR, "throughput.html")
        if not os.path.exists(html_path):
            return web.json_response({"error": "throughput.html not found"}, status=500)
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def create_session_api(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        token = body.get("token", "").strip()
        if not token:
            return web.json_response({"error": "missing token"}, status=400)
        session_id = monitor.create_session(token)
        if not session_id:
            return web.json_response({"error": "token not found in pool"}, status=404)
        return web.json_response({"session_id": session_id})

    async def sse_handler(request: web.Request) -> web.StreamResponse:
        sid = request.query.get("sid", "")
        session = monitor.sessions.get(sid)
        if not session:
            return web.json_response({"error": "invalid session"}, status=403)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        try:
            await resp.write(b"event: connected\ndata: {}\n\n")
        except ConnectionError:
            monitor.remove_session(sid)
            return resp

        async def keepalive():
            while True:
                await asyncio.sleep(15)
                try:
                    await resp.write(b": keepalive\n\n")
                except ConnectionError:
                    break

        task = asyncio.ensure_future(keepalive())

        try:
            while True:
                if task.done():
                    break
                try:
                    chunk = await asyncio.wait_for(session.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await resp.write(chunk)
                except ConnectionError:
                    break
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            monitor.remove_session(sid)

        return resp

    return serve_throughput_page, create_session_api, sse_handler


class ThroughputTracker:
    """Per-request throughput tracking state.

    Tracks input/output token estimates, TTFT, duration, and TPS for a single
    proxied request, broadcasting progress and final stats to SSE monitor sessions.
    """

    def __init__(
        self,
        monitor: ThroughputMonitor,
        proxy_token: str | None,
        *,
        req_id: str,
        model: str,
        provider: str,
        request_url: str,
        target_url: str,
        request_headers: dict,
        request_body: bytes | None,
        start_time: float,
    ):
        self.monitor = monitor
        self.proxy_token = proxy_token
        self.req_id = req_id
        self.model = model
        self.provider = provider
        self.request_url = request_url
        self.target_url = target_url

        try:
            self.input_est = _estimate_input_tokens(request_body)
        except Exception:
            self.input_est = 0
        self.start_mono = start_time
        self.last_broadcast = start_time

        self.is_stream = False
        self.line_buf = ""
        self.data_line_count = 0
        self.exact_input = 0
        self.exact_output = 0
        self.exact_cache_read = 0
        self.exact_cache_write = 0
        self.has_exact = False
        self.first_token_time = 0.0

        self.chunks: list[bytes] = []

        self.req_body = ""
        self.req_body_truncated = False
        if request_body:
            decoded = request_body.decode("utf-8", errors="replace")
            if len(decoded) > BODY_LIMIT:
                self.req_body = decoded[:BODY_LIMIT]
                self.req_body_truncated = True
            else:
                self.req_body = decoded

    @property
    def active(self) -> bool:
        return bool(self.proxy_token) and self.monitor.is_active(self.proxy_token)

    @property
    def final_usage(self) -> tuple[int, int, int, int]:
        return (
            self.exact_input,
            self.exact_output,
            self.exact_cache_read,
            self.exact_cache_write,
        )

    def emit_start(self) -> None:
        if not self.active:
            return
        start_data = {
            "id": self.req_id,
            "time": time.time() * 1000,
            "model": self.model,
            "provider": self.provider,
            "input_tokens": self.input_est,
            "request_url": self.request_url,
            "target_url": self.target_url,
            "request_headers": filter_sensitive_headers(self.request_headers),
            "request_body": self.req_body,
        }
        if self.req_body_truncated:
            start_data["request_body_truncated"] = True
        self.monitor.broadcast(self.proxy_token, "start", start_data)

    def feed_chunk(self, chunk: bytes, content_type: str = "") -> None:
        if not self.active:
            return
        self.chunks.append(chunk)
        self._parse_stream_chunk(chunk, content_type)
        if not self.is_stream:
            return

        now_mono = time.monotonic()
        if now_mono - self.last_broadcast < CHUNK_INTERVAL:
            return
        elapsed_ms = (now_mono - self.start_mono) * 1000
        est_out = self.exact_output if self.has_exact else self.data_line_count
        tps = est_out / (elapsed_ms / 1000) if elapsed_ms > 0 else 0
        chunk_data = {
            "id": self.req_id,
            "output_tokens": est_out,
            "duration_ms": round(elapsed_ms),
            "tps": round(tps, 1),
        }
        if self.first_token_time > 0:
            chunk_data["ttft_ms"] = round(
                (self.first_token_time - self.start_mono) * 1000
            )
        self.monitor.broadcast(self.proxy_token, "chunk", chunk_data)
        self.last_broadcast = now_mono

    def feed_non_stream_response(
        self, body: bytes, status: int, resp_headers: dict
    ) -> None:
        if not self.active:
            return
        self.chunks = [body] if body else []
        elapsed_ms = (time.monotonic() - self.start_mono) * 1000
        pi, po, cr, cw = _extract_usage_from_json(body)
        if pi:
            self.exact_input = pi
        if po:
            self.exact_output = po
            self.has_exact = True
        if cr:
            self.exact_cache_read = cr
        if cw:
            self.exact_cache_write = cw
        final_input = max(self.exact_input, self.input_est)
        input_estimated = final_input == self.input_est or final_input == 0
        final_output = self.exact_output
        tps = final_output / (elapsed_ms / 1000) if elapsed_ms > 0 else 0
        self._emit_end(
            status,
            resp_headers,
            final_input,
            input_estimated,
            final_output,
            elapsed_ms,
            tps,
            round(elapsed_ms),
        )

    def emit_end_stream(self, status: int, resp_headers: dict) -> None:
        if not self.active:
            return
        elapsed_ms = (time.monotonic() - self.start_mono) * 1000
        if self.line_buf.strip():
            pi, po, cr, cw = _extract_usage_from_sse(self.line_buf)
            if pi:
                self.exact_input = pi
            if po and not self.has_exact:
                self.exact_output = po
                self.has_exact = True
            if cr:
                self.exact_cache_read = cr
            if cw:
                self.exact_cache_write = cw
        final_input = max(self.exact_input, self.input_est)
        input_estimated = final_input == self.input_est or final_input == 0
        final_output = self.exact_output if self.has_exact else self.data_line_count
        tps = final_output / (elapsed_ms / 1000) if elapsed_ms > 0 else 0
        ttft_ms = round(elapsed_ms)
        if self.first_token_time > 0:
            ttft_ms = round((self.first_token_time - self.start_mono) * 1000)
        self._emit_end(
            status,
            resp_headers,
            final_input,
            input_estimated,
            final_output,
            elapsed_ms,
            tps,
            ttft_ms,
        )

    def _emit_end(
        self,
        status: int,
        resp_headers: dict,
        final_input: int,
        input_estimated: bool,
        final_output: int,
        elapsed_ms: float,
        tps: float,
        ttft_ms: int,
    ) -> None:
        decoded_resp = b"".join(self.chunks).decode("utf-8", errors="replace")
        tp_resp_body = ""
        tp_resp_body_truncated = False
        if len(decoded_resp) > BODY_LIMIT:
            tp_resp_body = decoded_resp[:BODY_LIMIT]
            tp_resp_body_truncated = True
        else:
            tp_resp_body = decoded_resp
        end_data = {
            "id": self.req_id,
            "input_tokens": final_input,
            "input_estimated": input_estimated,
            "output_tokens": final_output,
            "duration_ms": round(elapsed_ms),
            "ttft_ms": ttft_ms,
            "tps": round(tps, 1),
            "status": status,
            "response_headers": dict(resp_headers),
            "response_body": tp_resp_body,
        }
        if tp_resp_body_truncated:
            end_data["response_body_truncated"] = True
        self.monitor.broadcast(self.proxy_token, "end", end_data)

    def _parse_stream_chunk(self, chunk: bytes, content_type: str) -> None:
        if content_type and "text/event-stream" in content_type:
            self.is_stream = True
        if not self.is_stream:
            return
        self.line_buf += chunk.decode("utf-8", errors="replace")
        while "\n" in self.line_buf:
            line, self.line_buf = self.line_buf.split("\n", 1)
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            usage = obj.get("usage")
            if usage:
                pt = usage.get("prompt_tokens")
                it = usage.get("input_tokens")
                ct = usage.get("completion_tokens")
                ot = usage.get("output_tokens")
                if pt:
                    self.exact_input = pt
                if it:
                    self.exact_input = it
                if ct:
                    self.exact_output = ct
                    self.has_exact = True
                    if self.first_token_time == 0.0:
                        self.first_token_time = time.monotonic()
                if ot:
                    self.exact_output = ot
                    self.has_exact = True
                    if self.first_token_time == 0.0:
                        self.first_token_time = time.monotonic()
                cr, cw = _extract_cache_from_usage(usage)
                if cr:
                    self.exact_cache_read = cr
                if cw:
                    self.exact_cache_write = cw
            else:
                choices = obj.get("choices", [])
                for ch in choices:
                    delta = ch.get("delta", {})
                    if isinstance(delta.get("content"), str) and delta["content"]:
                        self.data_line_count += 1
                        if self.first_token_time == 0.0:
                            self.first_token_time = time.monotonic()
                msg_type = obj.get("type", "")
                if msg_type == "content_block_delta":
                    delta = obj.get("delta", {})
                    if isinstance(delta.get("text"), str) and delta["text"]:
                        self.data_line_count += 1
                        if self.first_token_time == 0.0:
                            self.first_token_time = time.monotonic()


def _estimate_input_tokens(body: bytes | None) -> int:
    if not body:
        return 0
    try:
        obj = json.loads(body)
        messages = obj.get("messages", [])
        total_chars = sum(
            len(m.get("content", ""))
            for m in messages
            if isinstance(m.get("content"), str)
        )
        if not total_chars:
            prompt = obj.get("prompt", "")
            if isinstance(prompt, str):
                total_chars = len(prompt)
            elif isinstance(prompt, list):
                total_chars = sum(len(p) for p in prompt if isinstance(p, str))
        return max(1, total_chars // 3)
    except Exception:
        return 0


def _extract_cache_from_usage(usage: dict) -> tuple[int, int]:
    cache_read = 0
    cache_write = 0
    cr = usage.get("cache_read_input_tokens")
    if isinstance(cr, int):
        cache_read = cr
    cw = usage.get("cache_creation_input_tokens")
    if isinstance(cw, int):
        cache_write = cw
    if not cache_read:
        cr = usage.get("cached_tokens")
        if isinstance(cr, int):
            cache_read = cr
    if not cache_read:
        cr = usage.get("cache_read_tokens")
        if isinstance(cr, int):
            cache_read = cr
    if not cache_write:
        cw = usage.get("cache_write_tokens")
        if isinstance(cw, int):
            cache_write = cw
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict):
        if not cache_read:
            cr = ptd.get("cached_tokens")
            if isinstance(cr, int):
                cache_read = cr
        if not cache_write:
            cw = ptd.get("cache_creation_tokens")
            if isinstance(cw, int):
                cache_write = cw
    return cache_read, cache_write


def _extract_usage_from_sse(line_buffer: str) -> tuple[int, int, int, int]:
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    for line in line_buffer.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        usage = obj.get("usage")
        if not usage:
            choices = obj.get("choices", [])
            for ch in choices:
                delta = ch.get("delta", {})
                if isinstance(delta.get("content"), str):
                    output_tokens += 1
        else:
            if "prompt_tokens" in usage:
                input_tokens = usage.get("prompt_tokens", input_tokens)
                ct = usage.get("completion_tokens", 0)
                if ct:
                    output_tokens = ct
            elif "input_tokens" in usage:
                it = usage.get("input_tokens", 0)
                if it:
                    input_tokens = it
                ot = usage.get("output_tokens", 0)
                if ot:
                    output_tokens = ot
            cr, cw = _extract_cache_from_usage(usage)
            if cr:
                cache_read = cr
            if cw:
                cache_write = cw
    return input_tokens, output_tokens, cache_read, cache_write


def _extract_usage_from_json(body: bytes | None) -> tuple[int, int, int, int]:
    if not body:
        return 0, 0, 0, 0
    try:
        obj = json.loads(body)
        usage = obj.get("usage", {})
        if usage:
            input_tokens = 0
            output_tokens = 0
            if "prompt_tokens" in usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
            elif "input_tokens" in usage:
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
            cache_read, cache_write = _extract_cache_from_usage(usage)
            return input_tokens, output_tokens, cache_read, cache_write
        return 0, 0, 0, 0
    except Exception:
        return 0, 0, 0, 0


def make_tracker(
    monitor,
    proxy_token: str | None,
    *,
    model: str,
    provider_name: str,
    request_url: str,
    target_url: str,
    request_headers: dict,
    request_body: bytes | None,
) -> ThroughputTracker | None:
    if monitor is None or not proxy_token or not monitor.is_active(proxy_token):
        return None
    import uuid

    req_id = uuid.uuid4().hex[:12]
    return ThroughputTracker(
        monitor,
        proxy_token,
        req_id=req_id,
        model=model,
        provider=provider_name,
        request_url=request_url,
        target_url=target_url,
        request_headers=request_headers,
        request_body=request_body,
        start_time=time.monotonic(),
    )
