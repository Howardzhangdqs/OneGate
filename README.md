# OneGate

Multi-provider AI reverse proxy with token management and admin panel.

Route requests to different upstream AI providers by URL prefix, with per-token provider permission control, a token pool backed by SQLite, and a web-based admin UI.

## Features

- **Multi-provider routing** -- `/oai/` -> OpenAI, `/glm/` -> BigModel, or any custom provider
- **Longest-prefix matching** -- most specific prefix wins; `/` is the fallback
- **Token pool** -- SQLite-backed token storage with auto-migration from legacy `pool.json`
- **Per-token permissions** -- restrict each token to specific providers; `["*"]` for all
- **Admin panel** -- web UI at `/admin?token=<admin_token>` for CRUD on tokens
- **Admin self-protection** -- cannot demote, disable, or delete your own admin account
- **Request logging** -- optional full request/response logging to SQLite

## Prerequisites

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Quick Start

```bash
cp .env.example .env
cp config.toml.example config.toml
```

Edit `.env` and `config.toml` with your real API keys and admin tokens.

If no admin tokens are configured, one will be auto-generated on first startup and printed to the console.

```bash
uv sync
uv run python proxy.py
```

The proxy starts on `http://0.0.0.0:5678` by default.

## Configuration

### `.env`

| Variable | Description |
|---|---|
| `TOKEN_OPENAI` | API key for OpenAI provider |
| `TOKEN_ANTHROPIC` | API key for Anthropic provider |
| `TOKEN_GROK` | API key for Grok (x.ai) provider |
| `POOL_COUNT` | Number of tokens to generate in pool (overrides `config.toml`) |
| `ENABLE_LOG` | Set to `1` to enable request/response logging |

### `config.toml`

```toml
bind_port = 5678
enable_log = false

[pool]
count = 10
usage_db = "data/onegate.db"
# admin_tokens = []  # Auto-generated on first startup if empty

[providers.openai]
prefix = "/oai"
upstream = "https://api.openai.com"
strip_prefix = true
env_token = "TOKEN_OPENAI"

[providers.anthropic]
prefix = "/claude"
upstream = "https://api.anthropic.com"
strip_prefix = true
env_token = "TOKEN_ANTHROPIC"

[providers.grok]
prefix = "/grok"
upstream = "https://api.x.ai"
strip_prefix = true
env_token = "TOKEN_GROK"

[providers.default]
prefix = "/"
upstream = "https://api.openai.com"
strip_prefix = false
env_token = "TOKEN_OPENAI"
```

Each provider entry:

| Field | Description |
|---|---|
| `prefix` | URL path prefix to match (longest match wins) |
| `upstream` | Upstream API base URL |
| `strip_prefix` | Remove the prefix before forwarding |
| `env_token` | `.env` variable name holding the real API key |

## Admin Panel

Access at `/admin?token=<admin_token>`.

From the admin panel you can:

- View all tokens and their provider permissions
- Create new tokens with specific provider access
- Toggle admin / enabled status
- Delete tokens (with self-protection for your own admin)

## Project Structure

```
proxy.py                  # Entry point
config.toml               # Provider routing config (gitignored)
.env                      # API keys (gitignored)
src/
  config.py               # Config loading and provider matching
  models.py               # Provider / TokenEntry data models
  pool.py                 # Token generation, resolution, extraction
  db.py                   # SQLite init, CRUD, usage logging
  handlers.py             # Info and proxy request handlers
  admin.py                # Admin panel API
  log.py                  # Full request/response logging
  network.py              # Public IP detection
static/
  admin.html              # Self-contained admin panel (dark theme)
tests/
  test_proxy.py           # Unit tests
  test_handlers.py        # Integration tests
```

## Testing

```bash
uv sync --extra dev
uv run pytest tests/ -v
```

## Systemd Deployment

```bash
cp onegate.service.example onegate.service
# Edit paths and username in the service file
sudo cp onegate.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now onegate
```

After code changes, clear `__pycache__` before restarting:

```bash
find /path/to/onegate -type d -name __pycache__ -exec rm -rf {} +
sudo systemctl restart onegate
```

## License

MIT
