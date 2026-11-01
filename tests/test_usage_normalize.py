import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.throughput import (
    _extract_cache_from_usage,
    _extract_usage_from_json,
    _extract_usage_from_sse,
    normalize_inclusive_input,
)


class TestNormalizeInclusiveInput:
    def test_subtracts_cache_when_inclusive(self):
        assert normalize_inclusive_input(1000, 300, inclusive=True) == 700

    def test_no_subtract_when_exclusive(self):
        assert normalize_inclusive_input(700, 300, inclusive=False) == 700

    def test_clamps_to_zero(self):
        assert normalize_inclusive_input(100, 300, inclusive=True) == 0

    def test_no_cache_no_change(self):
        assert normalize_inclusive_input(1000, 0, inclusive=True) == 1000


class TestExtractUsageFromJson:
    def test_openai_inclusive_prompt_tokens_subtracts_cache(self):
        body = (
            b'{"usage":{"prompt_tokens":1000,"completion_tokens":50,'
            b'"prompt_tokens_details":{"cached_tokens":300}}}'
        )
        inp, out, cr, cw = _extract_usage_from_json(body)
        assert inp == 700
        assert out == 50
        assert cr == 300
        assert cw == 0

    def test_anthropic_exclusive_input_tokens_keeps_cache(self):
        body = (
            b'{"usage":{"input_tokens":700,"output_tokens":50,'
            b'"cache_read_input_tokens":300,"cache_creation_input_tokens":0}}'
        )
        inp, out, cr, cw = _extract_usage_from_json(body)
        assert inp == 700
        assert out == 50
        assert cr == 300
        assert cw == 0

    def test_openai_cache_exceeds_prompt_clamps_to_zero(self):
        body = (
            b'{"usage":{"prompt_tokens":100,"completion_tokens":10,'
            b'"prompt_tokens_details":{"cached_tokens":500}}}'
        )
        inp, out, cr, cw = _extract_usage_from_json(body)
        assert inp == 0
        assert cr == 500

    def test_no_cache_returns_full_input(self):
        body = b'{"usage":{"prompt_tokens":1000,"completion_tokens":50}}'
        inp, out, cr, cw = _extract_usage_from_json(body)
        assert inp == 1000
        assert cr == 0

    def test_empty_body(self):
        assert _extract_usage_from_json(None) == (0, 0, 0, 0)
        assert _extract_usage_from_json(b"") == (0, 0, 0, 0)


class TestExtractUsageFromSse:
    def test_openai_stream_subtracts_cache(self):
        sse = (
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            'data: {"usage":{"prompt_tokens":1000,"completion_tokens":50,'
            '"prompt_tokens_details":{"cached_tokens":400}}}\n\n'
            "data: [DONE]\n\n"
        )
        inp, out, cr, cw = _extract_usage_from_sse(sse)
        assert inp == 600
        assert out == 50
        assert cr == 400

    def test_anthropic_stream_keeps_cache(self):
        sse = (
            'data: {"usage":{"input_tokens":600,"output_tokens":50,'
            '"cache_read_input_tokens":400}}\n\n'
            "data: [DONE]\n\n"
        )
        inp, out, cr, cw = _extract_usage_from_sse(sse)
        assert inp == 600
        assert cr == 400

    def test_no_usage_counts_content_lines(self):
        sse = (
            'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        inp, out, cr, cw = _extract_usage_from_sse(sse)
        assert out == 2


class TestExtractCacheFromUsage:
    def test_anthropic_native_fields(self):
        usage = {"cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}
        assert _extract_cache_from_usage(usage) == (100, 20)

    def test_openai_nested_fields(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 80}}
        assert _extract_cache_from_usage(usage) == (80, 0)

    def test_empty(self):
        assert _extract_cache_from_usage({}) == (0, 0)
