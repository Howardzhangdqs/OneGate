from __future__ import annotations

UA_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("claude-code", ("claude-cli", "claude code", "claudecode")),
    ("codex", ("codex_cli", "codex-cli", "openai-codex")),
    ("opencode", ("opencode",)),
    ("zcode", ("zcode",)),
    ("cursor", ("cursor",)),
    ("aider", ("aider",)),
    ("cline", ("cline",)),
    ("roo-code", ("roo code", "roo-code", "roocli")),
    ("continue", ("continue",)),
    ("github-copilot", ("copilot", "github-copilot")),
    ("gemini-cli", ("gemini-cli", "gemini cli")),
    ("cody", ("cody", "sourcegraph")),
    ("windsurf", ("windsurf",)),
    ("kilo", ("kilo",)),
    ("kilo-code", ("kilo-code",)),
    ("qwen-code", ("qwen-code", "qwen code")),
    ("crush", ("crush-cli", "crush")),
    ("goose", ("goose",)),
    ("serena", ("serena",)),
    ("ampcode", ("ampcode",)),
    ("python-openai", ("openai-python",)),
    ("node-openai", ("openai-node",)),
]

HEADER_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("claude-code", ("anthropic-version",)),
    (
        "python-openai",
        ("x-stainless-lang", "x-stainless-os", "x-stainless-runtime-version"),
    ),
    ("node-openai", ("x-stainless-retry-count",)),
]


def detect_client(headers) -> str:
    ua = ""
    lower_map: dict[str, str] = {}
    for key, value in headers.items():
        lk = key.lower()
        lower_map[lk] = value
        if lk == "user-agent":
            ua = value or ""

    ua_lower = ua.lower()
    for name, patterns in UA_RULES:
        for p in patterns:
            if p in ua_lower:
                return name.strip()

    if "anthropic-version" in lower_map:
        return "claude-code"
    has_stainless = any(k.startswith("x-stainless") for k in lower_map)
    if has_stainless:
        return "python-openai"

    return "unknown"


_CLIENT_LABELS: dict[str, str] = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "zcode": "ZCode",
    "cursor": "Cursor",
    "aider": "Aider",
    "cline": "Cline",
    "roo-code": "Roo Code",
    "continue": "Continue",
    "github-copilot": "GitHub Copilot",
    "gemini-cli": "Gemini CLI",
    "cody": "Cody",
    "windsurf": "Windsurf",
    "kilo": "Kilo",
    "kilo-code": "Kilo Code",
    "qwen-code": "Qwen Code",
    "crush": "Crush",
    "goose": "Goose",
    "serena": "Serena",
    "python-openai": "OpenAI SDK (Python)",
    "node-openai": "OpenAI SDK (Node)",
    "unknown": "Unknown",
}


def client_label(client: str) -> str:
    return _CLIENT_LABELS.get(client, client)
