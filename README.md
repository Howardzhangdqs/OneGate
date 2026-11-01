# OneGate

多提供商 AI 反向代理，内置 token 管理与管理员面板。

按 URL 前缀将请求路由到不同的上游 AI 提供商，支持按 token 的提供商权限控制、基于 SQLite 的 token 池，以及基于 Web 的管理界面。

## 功能特性

- **多提供商路由** -- `/oai/` -> OpenAI、`/glm/` -> BigModel，或任意自定义提供商
- **最长前缀匹配** -- 最具体的前缀优先；`/` 作为兜底
- **Token 池** -- 基于 SQLite 的 token 存储，可从旧版 `pool.json` 自动迁移
- **按 token 权限** -- 将每个 token 限制到指定提供商；`["*"]` 表示全部
- **模型目录** -- 站点级的公共 model id，映射到某个提供商及上游模型名
- **按 token 的模型可见性** -- 将每个 token 限制到目录 model id 白名单；`["*"]` 表示全部
- **OpenAI 兼容的模型列表** -- `GET /v1/models` 返回调用 token 可使用的目录条目
- **管理面板** -- 位于 `/admin?token=<admin_token>` 的 Web 界面，用于 token 增删改查
- **状态看板** -- 位于 `/status` 的按 token 划分的提供商可用性与成功率统计
- **管理员自我保护** -- 无法降级、禁用或删除自己的管理员账号
- **请求日志** -- 可选的完整请求/响应日志，写入 SQLite

## 前置要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

## 快速开始

```bash
cp .env.example .env
cp config.toml.example config.toml
```

编辑 `.env` 与 `config.toml`，填入真实的 API key 与管理员 token。

若未配置管理员 token，首次启动时会自动生成一个并打印到控制台。

```bash
uv sync
uv run python proxy.py
```

默认情况下，代理监听 `http://0.0.0.0:5678`。

## 配置

### `.env`

| 变量 | 说明 |
|---|---|
| `TOKEN_OPENAI` | OpenAI 提供商的 API key |
| `TOKEN_ANTHROPIC` | Anthropic 提供商的 API key |
| `TOKEN_GROK` | Grok（x.ai）提供商的 API key |
| `POOL_COUNT` | 池中生成的 token 数量（覆盖 `config.toml`） |
| `ENABLE_LOG` | 设为 `1` 以启用请求/响应日志 |

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

每个提供商条目：

| 字段 | 说明 |
|---|---|
| `prefix` | 要匹配的 URL 路径前缀（最长匹配优先） |
| `upstream` | 上游 API 基础 URL |
| `strip_prefix` | 转发前移除前缀 |
| `env_token` | 保存真实 API key 的 `.env` 变量名 |

### 示例：一个上游，每个模型系列一把 key

当某个 OpenAI 兼容网关为每个模型签发不同的 key 时，为每把 key 注册一个
提供商块，使用相同的 `upstream` 但不同的 `prefix`。这样每把 key 都有各自的
用量/状态分桶，模型名原样转发。

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

客户端保持标准 OpenAI 形态 —— 前缀只替换 host 部分：

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

`POST /gjzq/glm/v1/chat/completions` -> `https://gpt.gjzq.cn/prod-api/v1/chat/completions`。
proxy token 先经 token 池校验，再换成 `TOKEN_GJZQ_GLM` 后转发。注意部分上游
位于 WAF 之后，会拒绝脚本类 User-Agent（如 `curl/*`）；客户端通常发送自己的
UA，若被拦截，`privacy.ua_override` 是全局的兜底开关。

## 管理面板

访问 `/admin?token=<admin_token>`。

在管理面板中你可以：

- 查看所有 token 及其提供商权限
- 创建带有指定提供商访问权限的新 token
- 切换管理员/启用状态
- 删除 token（对自己的管理员账号有自我保护）

### 模型目录

`/models` 页面（需要管理员 token）维护一个站点级的公共 model id 目录。每个条目
将 model id 映射到一个已配置的提供商以及可选的上游模型名（`target`），并可启用
或禁用。

目录条目也可以是通配符或正则规则，例如 `glm-*` 或 `^claude-(.+)$`。传入的模型名
按以下顺序匹配：先精确匹配 id，再依次尝试模式条目（最长者优先），先作正则完整
匹配，再作 `fnmatch` 通配匹配。`target` 可以用 `$1`..`$n` 引用正则捕获组；`target`
为空时原样转发请求的模型名。模式规则只用于路由请求，不会由 `/v1/models` 返回，
后者只列出具体的 model id。

`GET /v1/models`（以及 `/v1/model`，单个条目用 `/v1/models/{model}`）使用普通
proxy Bearer token 认证，仅返回该 token 有权使用的目录条目，依据是其提供商权限
与模型白名单。chat 与 responses 请求按以下顺序解析模型：先按 token 级映射规则，
再查站点目录（精确匹配，然后模式规则），最后在默认提供商上原样使用请求的模型。

## 状态页

`/status` 页面展示各提供商的可用性、每小时成功率与汇总统计。其数据端点
`GET /status/api` 要求在 `X-Proxy-Token` 头中携带 proxy token，只返回该 token
有权使用的提供商，因此每位访问者只看到自己那份看板。带有 `providers = ["*"]`
的 token 可看到全部已配置的提供商分组。

```bash
curl -H "X-Proxy-Token: <proxy token>" http://<onegate-host>:5678/status/api
```

## 项目结构

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

## 测试

```bash
uv sync --extra dev
uv run pytest tests/ -v
```

## Systemd 部署

```bash
cp onegate.service.example onegate.service
# Edit paths and username in the service file
sudo cp onegate.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now onegate
```

代码改动后，重启前先清理 `__pycache__`：

```bash
find /path/to/onegate -type d -name __pycache__ -exec rm -rf {} +
sudo systemctl restart onegate
```

## 许可证

MIT
