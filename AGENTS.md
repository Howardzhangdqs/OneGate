# AGENTS.md

## 项目概述

OneGate 是一个基于 Python/aiohttp 构建的多提供商 AI 反向代理。它位于多个 AI API
提供商（GLM、OpenAI、Anthropic、Grok 等）之前，通过基于 SQLite 的 token 池管理
认证、路由、速率限制与用量追踪。

## 技术栈

- Python 3.12+
- aiohttp（异步 HTTP 服务端 + 客户端）
- SQLite（stdlib sqlite3）
- TOML 配置 + .env（python-dotenv）
- uv 包管理器
- Ruff（linter/格式化工具，默认配置）

## 常用命令

```bash
uv sync --extra dev          # Install all deps including dev tools
uv run python proxy.py       # Run the proxy server
uv run pytest tests/ -v      # Run tests
uv run ruff check src/ tests/ # Lint
uv run ruff format src/ tests/ # Format
```

## 项目结构

- `proxy.py` - 主入口，装配 aiohttp 服务、路由与 session
- `intercept.py` - 调试代理，将完整请求/响应捕获到文件
- `src/config.py` - 从 TOML/env 加载配置，提供商匹配（`AppConfig`、`match_provider`）
- `src/models.py` - 数据模型（`Provider`、`TokenEntry`）
- `src/handlers.py` - 请求处理器：proxy、usage、status API
- `src/admin.py` - 用于 token 管理的管理员 CRUD API
- `src/pool.py` - Token 池：生成、查找、认证解析
- `src/db.py` - SQLite 层：schema、迁移、CRUD、速率限制、用量追踪
- `src/log.py` - 请求/响应日志（完整 JSON 或仅头部 JSONL）
- `src/network.py` - 公网 IP 探测
- `static/` - 管理员、用量、状态、定价 HTML 页面 + 共享 CSS
- `config.toml` - 运行时配置（已 gitignore，包含密钥）
- `config.toml.example` - 配置模板
- `onegate.service` - 用于生产环境的 Systemd unit 文件

## 架构

客户端使用 proxy token（`{32 hex}.{16 alphanum}`）认证。代理会按提供商将其解析为真实的上游
API key。请求按路径上的最长前缀匹配进行路由。代理以流式方式返回响应，并可选地记录完整的
请求/响应对。

关键请求流程：IP/UA 封禁检查 -> 提供商匹配 -> 去除前缀 -> token 解析 -> 速率限制检查 -> 转发上游 -> 记录用量。

## 配置（config.toml）

关键字段：
- `bind_port` - 服务端口（默认 5678）
- `banned_ips` - 要封禁的 IP 列表
- `banned_uas` - 要封禁的 UA 通配模式列表（fnmatch 语法，例如 `Bun*`）
- `[timeout]` - total、connect、sock_read
- `[privacy]` - strip_tracking（转发前丢弃客户端追踪头以及 body 中的 metadata/user 字段）、
  ua_override（重写上游 User-Agent；默认 "" 保留客户端原值）
- `[pool]` - count、usage_db、admin_tokens
- `[providers.*]` - prefix、upstream、strip_prefix、env_token

## 服务管理

```bash
sudo systemctl restart onegate.service   # Restart
sudo systemctl status onegate.service    # Status
journalctl -u onegate.service -f         # Logs
```

## 代码约定

- 所有源文件使用 `from __future__ import annotations`
- 除非明确要求，否则不写注释
- 配置/模型使用 dataclass，不使用 Pydantic
- 通过 stdlib 使用 SQLite，不使用 ORM
- 全面异步（aiohttp handler 均为异步）
- 静态 HTML 页面为自包含的单文件
- 配置变更需要重启服务（无热重载）
