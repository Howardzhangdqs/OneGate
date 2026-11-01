import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import unittest

from src.stream_proxy import pump_stream


class FakeResp:
    def __init__(self, fail_after: int | None = None):
        self.written: list[bytes] = []
        self.eof = False
        self.fail_after = fail_after

    async def write(self, data: bytes) -> None:
        if self.fail_after is not None and len(self.written) >= self.fail_after:
            raise ConnectionResetError("client gone")
        self.written.append(data)

    async def write_eof(self) -> None:
        self.eof = True


async def _steady_iter():
    for i in range(3):
        yield f"chunk-{i}".encode()


async def _stall_iter(delay: float):
    yield b"first"
    await asyncio.sleep(delay)
    yield b"second"


async def _stuck_iter():
    yield b"first"
    await asyncio.sleep(60)


class TestPumpStream(unittest.IsolatedAsyncioTestCase):
    async def test_steady_chunks_done(self):
        resp = FakeResp()
        seen = []
        result = await pump_stream(
            resp,
            _steady_iter(),
            heartbeat_interval=0.2,
            idle_timeout=5,
            on_chunk=seen.append,
        )
        assert result == "done"
        assert resp.eof is True
        assert resp.written == [b"chunk-0", b"chunk-1", b"chunk-2"]
        assert seen == [b"chunk-0", b"chunk-1", b"chunk-2"]

    async def test_stall_emits_pings_then_done(self):
        resp = FakeResp()
        result = await pump_stream(
            resp,
            _stall_iter(2),
            heartbeat_interval=0.3,
            idle_timeout=5,
        )
        assert result == "done"
        assert resp.eof is True
        assert resp.written[0] == b"first"
        assert resp.written[-1] == b"second"
        assert resp.written.count(b": ping\n\n") >= 2

    async def test_idle_timeout_writes_error_and_eof(self):
        resp = FakeResp()
        result = await pump_stream(
            resp,
            _stuck_iter(),
            heartbeat_interval=0.3,
            idle_timeout=1,
            on_idle_timeout=lambda: b"data: error-frame\n\n",
        )
        assert result == "idle_timeout"
        assert resp.eof is True
        assert resp.written[0] == b"first"
        assert resp.written[-1] == b"data: error-frame\n\n"
        assert b": ping\n\n" in resp.written

    async def test_client_gone_on_write(self):
        resp = FakeResp(fail_after=1)
        result = await pump_stream(
            resp,
            _stall_iter(2),
            heartbeat_interval=0.3,
            idle_timeout=5,
        )
        assert result == "client_gone"

    async def test_heartbeat_disabled_no_pings(self):
        resp = FakeResp()
        result = await pump_stream(
            resp,
            _steady_iter(),
            heartbeat_interval=0,
            idle_timeout=0,
        )
        assert result == "done"
        assert resp.eof is True
        assert b": ping\n\n" not in resp.written

    async def test_heartbeat_disabled_still_bounds_idle(self):
        resp = FakeResp()
        result = await pump_stream(
            resp,
            _stuck_iter(),
            heartbeat_interval=0,
            idle_timeout=0.5,
            on_idle_timeout=lambda: b"data: error-frame\n\n",
        )
        assert result == "idle_timeout"
        assert resp.eof is True
        assert resp.written[-1] == b"data: error-frame\n\n"
        assert b": ping\n\n" not in resp.written


if __name__ == "__main__":
    import unittest

    unittest.main()
