# AGENTS.md

## Project Overview

OneGate is a multi-provider AI reverse proxy built with Python/aiohttp. It sits in front of multiple AI API providers (GLM, OpenAI, Anthropic, Grok, etc.), managing authentication, routing, rate limiting, and usage tracking through a SQLite-backed token pool.

## Tech Stack

- Python 3.12+
- aiohttp (async HTTP server + client)
- SQLite (stdlib sqlite3)
- TOML config + .env (python-dotenv)
- uv package manager
- Ruff (linter/formatter, default config)

## Common Commands

```bash
uv sync --extra dev          # Install all deps including dev tools
uv run python proxy.py       # Run the proxy server
uv run pytest tests/ -v      # Run tests
uv run ruff check src/ tests/ # Lint
uv run ruff format src/ tests/ # Format
```

## Project Structure

- `proxy.py` - Main entry point, wires up aiohttp server, routes, and session
- `intercept.py` - Debug proxy for capturing full request/response to files
- `src/config.py` - Config loading from TOML/env, provider matching (`AppConfig`, `match_provider`)
- `src/models.py` - Data models (`Provider`, `TokenEntry`)
- `src/handlers.py` - Request handlers: proxy, usage, status APIs
- `src/admin.py` - Admin CRUD API for token management
- `src/pool.py` - Token pool: generation, lookup, auth resolution
- `src/db.py` - SQLite layer: schema, migrations, CRUD, rate limits, usage tracking
- `src/log.py` - Request/response logging (full JSON or headers-only JSONL)
- `src/network.py` - Public IP detection
- `static/` - Admin, usage, status HTML pages + shared CSS
- `config.toml` - Runtime config (gitignored, contains secrets)
- `config.toml.example` - Config template
- `onegate.service` - Systemd unit file for production

## Architecture

Clients authenticate with proxy tokens (`{32 hex}.{16 alphanum}`). The proxy resolves these to real upstream API keys per provider. Requests are routed by longest-prefix match on the path. The proxy streams responses and optionally logs full request/response pairs.

Key request flow: IP/UA ban check -> provider match -> prefix strip -> token resolve -> rate limit check -> upstream forward -> usage recording.

## Configuration (config.toml)

Key fields:
- `bind_port` - Server port (default 5678)
- `banned_ips` - List of IPs to block
- `banned_uas` - List of UA wildcard patterns to block (fnmatch syntax, e.g. `Bun*`)
- `[timeout]` - total, connect, sock_read
- `[pool]` - count, usage_db, admin_tokens
- `[providers.*]` - prefix, upstream, strip_prefix, env_token

## Service Management

```bash
sudo systemctl restart onegate.service   # Restart
sudo systemctl status onegate.service    # Status
journalctl -u onegate.service -f         # Logs
```

## Code Conventions

- Use `from __future__ import annotations` in all source files
- No comments unless explicitly requested
- Dataclasses for config/models, no Pydantic
- SQLite via stdlib, no ORM
- Async everywhere (aiohttp handlers are async)
- Static HTML pages are self-contained single files
- Config changes require service restart (no hot-reload)
