import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from src.config import AppConfig
from src.db import add_token, init_db, set_site_model
from src.models import Provider, TokenEntry
from src.v1_handler import _with_identity_encoding, create_v1_handlers

_TOKEN = "aaa111bbb222ccc333ddd444eee555ff.testtoken0000000"


class _FakeChunk:
    def __init__(self, content: str):
        self.content = content
        self.choices = []

    def model_dump_json(self, exclude_none: bool = True) -> str:
        return '{"choices": [{"delta": {"content": "%s"}}]}' % self.content


class _FakeStream:
    def __init__(self, contents: list[str]):
        self._contents = contents

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for content in self._contents:
            yield _FakeChunk(content)


class _FakeCompletion:
    usage = None

    def model_dump_json(self, exclude_none: bool = True) -> str:
        return '{"id": "chatcmpl-test", "object": "chat.completion"}'


class TestV1StreamEncoding(AioHTTPTestCase):
    async def get_application(self):
        self.cfg = AppConfig(
            bind_port=0,
            usage_db=":memory:",
            providers=[
                Provider(
                    "mock",
                    "/",
                    "http://127.0.0.1:9",
                    False,
                    v1_base="http://127.0.0.1:9/v1",
                )
            ],
            real_tokens={"mock": "real-key"},
        )
        self.db_conn, _ = init_db(":memory:")
        add_token(self.db_conn, _TOKEN, ["*"])
        set_site_model(self.db_conn, "mock-model", "mock", "mock-model")
        self.token_pool = {_TOKEN: TokenEntry(_TOKEN, ["*"])}

        v1_chat_completions, _, _, _ = create_v1_handlers(
            self.cfg, self.token_pool, self.db_conn
        )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", v1_chat_completions)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.db_conn.close()

    async def _post(self, payload):
        return await self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            json=payload,
        )

    async def test_stream_forces_identity_encoding(self):
        stream = _FakeStream(["he", "llo"])
        with patch(
            "litellm.acompletion", new=AsyncMock(return_value=stream)
        ) as completion:
            resp = await self._post(
                {
                    "model": "mock-model",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
            await resp.read()
        assert resp.status == 200
        completion.assert_awaited_once()
        headers = completion.await_args.kwargs["extra_headers"]
        assert headers["Accept-Encoding"] == "identity"
        assert "accept-encoding" not in headers

    async def test_stream_preserves_client_extra_headers(self):
        self.cfg.privacy_strip_tracking = False
        stream = _FakeStream(["hi"])
        with patch(
            "litellm.acompletion", new=AsyncMock(return_value=stream)
        ) as completion:
            resp = await self._post(
                {
                    "model": "mock-model",
                    "stream": True,
                    "extra_headers": {"X-Trace": "abc", "accept-encoding": "gzip"},
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
            await resp.read()
        assert resp.status == 200
        headers = completion.await_args.kwargs["extra_headers"]
        assert headers["X-Trace"] == "abc"
        assert headers["Accept-Encoding"] == "identity"
        assert "accept-encoding" not in headers

    async def test_non_stream_does_not_inject_encoding(self):
        self.cfg.privacy_strip_tracking = False
        with patch(
            "litellm.acompletion", new=AsyncMock(return_value=_FakeCompletion())
        ) as completion:
            resp = await self._post(
                {
                    "model": "mock-model",
                    "extra_headers": {"X-Trace": "abc"},
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
            await resp.read()
        assert resp.status == 200
        kwargs = completion.await_args.kwargs
        assert kwargs["extra_headers"] == {"X-Trace": "abc"}


class TestWithIdentityEncoding(unittest.TestCase):
    def test_none_input(self):
        assert _with_identity_encoding(None) == {"Accept-Encoding": "identity"}

    def test_preserves_other_headers(self):
        out = _with_identity_encoding({"X-Trace": "abc"})
        assert out == {"X-Trace": "abc", "Accept-Encoding": "identity"}

    def test_drops_existing_encoding_case_insensitively(self):
        out = _with_identity_encoding(
            {"X-Trace": "abc", "accept-encoding": "gzip", "Accept-Encoding": "br"}
        )
        assert out == {"X-Trace": "abc", "Accept-Encoding": "identity"}

    def test_does_not_mutate_input(self):
        original = {"accept-encoding": "gzip"}
        out = _with_identity_encoding(original)
        assert original == {"accept-encoding": "gzip"}
        assert out == {"Accept-Encoding": "identity"}


if __name__ == "__main__":
    unittest.main()
