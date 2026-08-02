import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.client_detect import detect_client, client_label


class FakeHeaders:
    def __init__(self, pairs):
        self._pairs = pairs

    def items(self):
        return self._pairs


def H(**kw):
    pairs = [(k, v) for k, v in kw.items()]
    return FakeHeaders(pairs)


def test_claude_code_by_ua():
    assert detect_client(H(**{"User-Agent": "claude-cli/1.0.23"})) == "claude-code"
    assert detect_client(H(**{"User-Agent": "Claude Code v1.2"})) == "claude-code"


def test_codex_by_ua():
    assert detect_client(H(**{"User-Agent": "codex_cli_rs/0.1.0"})) == "codex"
    assert detect_client(H(**{"User-Agent": "openai-codex/1.0"})) == "codex"


def test_opencode_by_ua():
    assert detect_client(H(**{"User-Agent": "opencode/0.5.0"})) == "opencode"


def test_zcode_by_ua():
    assert detect_client(H(**{"User-Agent": "zcode-agent/2.0"})) == "zcode"


def test_cursor_by_ua():
    assert detect_client(H(**{"User-Agent": "Cursor/0.42.0"})) == "cursor"


def test_aider_by_ua():
    assert detect_client(H(**{"User-Agent": "aider 0.65.0"})) == "aider"


def test_cline_by_ua():
    assert detect_client(H(**{"User-Agent": "cline/2.1"})) == "cline"


def test_github_copilot_by_ua():
    assert detect_client(H(**{"User-Agent": "Github-copilot/1.0"})) == "github-copilot"


def test_gemini_cli_by_ua():
    assert detect_client(H(**{"User-Agent": "gemini-cli/0.1"})) == "gemini-cli"


def test_claude_code_by_header_fallback():
    assert detect_client(H(**{"anthropic-version": "2023-06-01"})) == "claude-code"


def test_openai_sdk_by_stainless_header():
    h = H(**{"x-stainless-lang": "python", "x-stainless-os": "Linux"})
    assert detect_client(h) == "python-openai"


def test_unknown_when_no_signals():
    assert detect_client(H(**{"User-Agent": "python-requests/2.31"})) == "unknown"
    assert detect_client(H(**{})) == "unknown"


def test_unknown_when_empty_ua():
    assert detect_client(H(**{"User-Agent": ""})) == "unknown"


def test_ua_case_insensitive():
    assert detect_client(H(**{"User-Agent": "CLAUDE-CLI/1.0"})) == "claude-code"
    assert detect_client(H(**{"User-Agent": "OPENCODE/x"})) == "opencode"


def test_client_label_known_and_unknown():
    assert client_label("claude-code") == "Claude Code"
    assert client_label("codex") == "Codex"
    assert client_label("something-new") == "something-new"
    assert client_label("unknown") == "Unknown"
