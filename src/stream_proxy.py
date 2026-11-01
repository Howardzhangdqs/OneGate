from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

from aiohttp import web

_EOF = object()

_CLIENT_GONE_ERRORS = (ConnectionResetError, RuntimeError)


class _UpstreamError:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


async def _read_upstream(
    upstream_iter: AsyncIterator[bytes], queue: asyncio.Queue
) -> None:
    try:
        async for chunk in upstream_iter:
            await queue.put(chunk)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        await queue.put(_UpstreamError(exc))
    else:
        await queue.put(_EOF)


async def _finish_idle_timeout(
    resp: web.StreamResponse, on_idle_timeout: Callable[[], bytes] | None
) -> str:
    if on_idle_timeout is not None:
        try:
            await resp.write(on_idle_timeout())
        except _CLIENT_GONE_ERRORS:
            return "client_gone"
    try:
        await resp.write_eof()
    except _CLIENT_GONE_ERRORS:
        return "client_gone"
    return "idle_timeout"


async def pump_stream(
    resp: web.StreamResponse,
    upstream_iter: AsyncIterator[bytes],
    *,
    heartbeat_interval: float,
    idle_timeout: float,
    on_idle_timeout: Callable[[], bytes] | None = None,
    on_chunk: Callable[[bytes], None] | None = None,
) -> str:
    queue: asyncio.Queue = asyncio.Queue()
    reader = asyncio.ensure_future(_read_upstream(upstream_iter, queue))
    last_chunk_ts = time.monotonic()
    read_timeout = heartbeat_interval if heartbeat_interval > 0 else idle_timeout
    try:
        while True:
            if read_timeout > 0:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    if (
                        idle_timeout > 0
                        and time.monotonic() - last_chunk_ts >= idle_timeout
                    ):
                        return await _finish_idle_timeout(resp, on_idle_timeout)
                    if heartbeat_interval > 0:
                        try:
                            await resp.write(b": ping\n\n")
                        except _CLIENT_GONE_ERRORS:
                            return "client_gone"
                    continue
            else:
                item = await queue.get()
            if isinstance(item, _UpstreamError):
                raise item.exc
            if item is _EOF:
                try:
                    await resp.write_eof()
                except _CLIENT_GONE_ERRORS:
                    return "client_gone"
                return "done"
            last_chunk_ts = time.monotonic()
            if on_chunk is not None:
                on_chunk(item)
            try:
                await resp.write(item)
            except _CLIENT_GONE_ERRORS:
                return "client_gone"
    finally:
        if not reader.done():
            reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
