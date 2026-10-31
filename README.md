# OneGate

Multi-provider AI reverse proxy with token management and admin panel.

Route requests to different upstream AI providers by URL prefix, with per-token provider permission control, a token pool backed by SQLite, and a web-based admin UI.

## Features

- **Multi-provider routing** -- `/oai/` -> OpenAI, `/glm/` -> BigModel, or any custom provider
- **Longest-prefix matching** -- most specific prefix wins; `/` is the fallback
- **Token pool** -- SQLite-backed token storage with auto-migration from legacy `pool.json`
- **Per-token permissions** -- restrict each token to specific providers; `["*"]` for all
- **Model catalog** -- site-wide public model ids mapped to a provider and upstream model name
- **Per-token model visibility** -- restrict each token to an allowlist of catalog model ids; `["*"]` for all
- **OpenAI-compatible model listing** -- `GET /v1/models` returns the catalog entries the calling token may use
- **Admin panel** -- web UI at `/admin?token=<admin_token>` for CRUD on tokens
- **Status dashboard** -- token-scoped provider uptime and success stats at `/status`
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

### Example: one upstream, one key per model family

When an OpenAI-compatible gateway issues a different key per model, register one
provider block per key with the same `upstream` but distinct `prefix`. Each key
then gets its own usage/status bucket, and the model name is forwarded untouched.

```toml
[providers.gjzq-glm]
prefix = "/gjzq/glm"
upstream = "https://gpt.gjzq.cn/prod-api"
strip_prefix = true
env_token = "TOKEN_GJZQ_GLM"

[providers.gjzq-deepseek]
prefix = "/gjzq/ds"
upstream = "https://gpt.gjzq.cn/prod-api"
strip_prefix = true
env_token = "TOKEN_GJZQ_DS"
```

Clients keep the standard OpenAI shape — the prefix only replaces the host part:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://<onegate-host>:5678/gjzq/glm",
    api_key="<onegate proxy token>",
)
client.chat.completions.create(
    model="glm-5.3-flash",
    messages=[{"role": "user", "content": "hi"}],
)
```

`POST /gjzq/glm/v1/chat/completions` -> `https://gpt.gjzq.cn/prod-api/v1/chat/completions`.
The proxy token is validated against the pool, then swapped for `TOKEN_GJZQ_GLM`
before forwarding. Note that some upstreams sit behind a WAF that rejects
scripting User-Agents (e.g. `curl/*`); clients normally send their own UA, and
`privacy.ua_override` is the global escape hatch if one is blocked.

## Admin Panel

Access at `/admin?token=<admin_token>`.

From the admin panel you can:

- View all tokens and their provider permissions
- Create new tokens with specific provider access
- Toggle admin / enabled status
- Delete tokens (with self-protection for your own admin)

### Model catalog

The `/models` page (admin token required) maintains a site-wide catalog of
public model ids. Each entry maps a model id to a configured provider and an
optional upstream model name (`target`), and can be enabled or disabled.

A catalog entry may also be a wildcard or regex rule, e.g. `glm-*` or
`^claude-(.+)$`. Incoming model names are matched in this order: exact id
first, then pattern entries (longest first) tried as a regex full match and
then as an `fnmatch` wildcard. `target` may reference regex capture groups with
`$1`..`$n`; an empty target forwards the requested name unchanged. Pattern
rules route requests but are not returned by `/v1/models`, which only lists
concrete model ids.

`GET /v1/models` (also `/v1/model`, plus `/v1/models/{model}` for one entry) is authenticated with the normal
proxy Bearer token and returns only catalog entries the token may use, based on
its provider permissions and model allowlist. Chat and responses requests
resolve a model in this order: per-token mapping rules, then the site catalog
(exact, then pattern rules), then the requested model unchanged on the default
provider.

## Status Page

The `/status` page shows per-provider uptime, hourly success rates, and summary
stats. Its data endpoint, `GET /status/api`, requires a proxy token in the
`X-Proxy-Token` header and returns only the providers that token is allowed to
use, so each visitor sees their own slice of the dashboard. A token with
`providers = ["*"]` sees every configured provider group.

```bash
curl -H "X-Proxy-Token: <proxy token>" http://<onegate-host>:5678/status/api
```

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
