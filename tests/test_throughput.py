import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

from src.throughput import ThroughputTracker


class FakeMonitor:
    def __init__(self):
        self.events = []

    def is_active(self, token):
        return True

    def broadcast(self, token, event, data):
        self.events.append((event, data))


def _make_tracker():
    monitor = FakeMonitor()
    tracker = ThroughputTracker(
        monitor,
        "proxy-token",
        req_id="req-1",
        model="deepseek-reasoner",
        provider="gjzq-deepseek",
        request_url="/deepseek/v1/chat/completions",
        target_url="https://gpt.gjzq.cn/prod-api/v1/chat/completions",
        request_headers={},
        request_body=b'{"stream":true}',
        start_time=time.monotonic(),
    )
    return monitor, tracker


class TestParseStreamChunk(unittest.TestCase):
    def test_reasoning_content_delta_counts(self):
        _, tracker = _make_tracker()
        tracker.feed_chunk(
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n',
            "text/event-stream",
        )
        assert tracker.first_token_time > 0
        assert tracker.data_line_count == 1

    def test_reasoning_delta_counts(self):
        _, tracker = _make_tracker()
        tracker.feed_chunk(
            b'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}\n\n',
            "text/event-stream",
        )
        assert tracker.first_token_time > 0
        assert tracker.data_line_count == 1

    def test_content_delta_still_counts(self):
        _, tracker = _make_tracker()
        tracker.feed_chunk(
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            "text/event-stream",
        )
        assert tracker.first_token_time > 0
        assert tracker.data_line_count == 1

    def test_delta_with_content_and_reasoning_counts_once(self):
        _, tracker = _make_tracker()
        tracker.feed_chunk(
            b'data: {"choices":[{"delta":{"content":"hi",'
            b'"reasoning_content":"think"}}]}\n\n',
            "text/event-stream",
        )
        assert tracker.first_token_time > 0
        assert tracker.data_line_count == 1

    def test_top_level_thinking_not_double_counted_with_choices(self):
        _, tracker = _make_tracker()
        tracker.feed_chunk(
            b'data: {"thinking":"think","choices":[{"delta":{"content":"hi"}}]}\n\n',
            "text/event-stream",
        )
        assert tracker.first_token_time > 0
        assert tracker.data_line_count == 1

    def test_anthropic_thinking_delta_counts(self):
        _, tracker = _make_tracker()
        tracker.feed_chunk(
            b'data: {"type":"content_block_delta",'
            b'"delta":{"type":"thinking_delta","thinking":"x"}}\n\n',
            "text/event-stream",
        )
        assert tracker.first_token_time > 0
        assert tracker.data_line_count == 1

    def test_anthropic_text_delta_still_counts(self):
        _, tracker = _make_tracker()
        tracker.feed_chunk(
            b'data: {"type":"content_block_delta",'
            b'"delta":{"type":"text_delta","text":"hi"}}\n\n',
            "text/event-stream",
        )
        assert tracker.first_token_time > 0
        assert tracker.data_line_count == 1

    def test_end_event_ttft_smaller_than_duration(self):
        async def scenario():
            monitor, tracker = _make_tracker()
            tracker.feed_chunk(
                b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n',
                "text/event-stream",
            )
            await asyncio.sleep(0.05)
            tracker.emit_end_stream(200, {})
            return monitor

        monitor = asyncio.run(scenario())
        end = next(data for event, data in monitor.events if event == "end")
        assert end["ttft_ms"] < end["duration_ms"]
        assert end["duration_ms"] - end["ttft_ms"] >= 30


if __name__ == "__main__":
    unittest.main()
