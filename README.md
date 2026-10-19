# ChatGPT 订阅 (Codex) — MaiBot 插件

用 **ChatGPT Plus / Pro 订阅额度**给麦麦当模型后端：插件自己完成 ChatGPT 账号登录，
不需要 OpenAI API Key，也不需要在服务器上安装 Codex CLI。

> v0.2.0 起按 [WSL043/dsh-codex-subscription](https://github.com/WSL043/dsh-codex-subscription) 的思路重写：
> 账号由插件自己管理（账号库 / 多账号 / 单独刷新），模型目录实时发现，
> 请求走 Codex 使用的 Responses API（与 pi-ai 的 openai-codex-responses 同一套 wire）。

---

## 设计

| 环节 | 做法 |
| --- | --- |
| 登录 | 内置 OpenAI OAuth **设备码**流程（`/codex login`），浏览器输入一次性代码即可 |
| 凭证 | 插件自己的账号库 `accounts.json`（0600），支持多个账号、切换、单独刷新 |
| 兼容 | 账号库为空时可选择从 `~/.codex/auth.json` **导入一次**（只读，不改写） |
| 续期 | 快过期或 401 时用 refresh_token 刷新，并写回账号库 |
| 协议 | OpenAI Responses API：`POST https://chatgpt.com/backend-api/codex/responses`（SSE） |
| 目录 | `GET /codex/models` 实时发现，带 TTL 缓存 |
| 接入 | `@LLMProvider("codex_subscription")`，模型配置里把 `client_type` 指过来 |

插件内部聚合 SSE，只把最终结果交给 Host（MaiBot 的插件 Provider 不支持 Host 侧流式处理器）。

---

## 安装

把目录放到 MaiBot 的 `plugins/` 下，或在 WebUI 插件页「从 ZIP 安装」上传打包好的 zip，
然后重启 MaiBot。

要求：MaiBot ≥ 1.3.0，插件 SDK ≥ 2.6.0（用到 `ctx.paths`），Python 依赖 `httpx`（manifest 已声明）。

---

## 登录

在聊天里发：

```
/codex login
```

麦麦会回一个 `https://auth.openai.com/codex/device` 链接和一次性代码：

1. 打开链接，登录 ChatGPT 账号；
2. 输入那串代码；
3. 插件自动轮询（最长 15 分钟），成功后把账号写进账号库并在聊天里回报邮箱与订阅类型。

> 只在你**自己**发起登录时输入代码；别人给你的代码不要输。

登录后账号库位置：`data/plugins/chenxi2542.maibot-codex-subscription/accounts.json`（可在插件配置里改）。

### 从 Codex CLI 导入（可选）

如果服务器上已经跑过 `codex login`，且账号库为空，插件启动时会自动导入一次
`~/.codex/auth.json`（`CODEX_HOME` 优先）。导入后就不再依赖该文件，Codex 登出也不影响麦麦。

---

## 命令

`models` 对所有人开放；其余命令标了 `permission="operator"`，由 MaiBot 统一鉴权，
需要在主程序里把自己的账号设为操作员才能用（`[plugin].permission = ["qq:123456789"]`，
或在 WebUI「Bot 配置 → 命令」里配置）。

| 命令 | 权限 | 作用 |
| --- | --- | --- |
| `/codex models` | public | 查看账号可用模型（只展示，不改配置） |
| `/codex test` | operator | 用 `default_model` 跑一句话验证链路（会消耗订阅额度） |
| `/codex login` | operator | 设备码登录，添加账号 |
| `/codex accounts` | operator | 列出账号库（邮箱 `a***@domain`、ID 只显示前 8 位） |
| `/codex use <序号\|邮箱>` | operator | 切换当前账号 |
| `/codex status` | operator | 当前账号、订阅类型、令牌有效期（已脱敏，不输出路径） |
| `/codex import` | operator | 从服务器上的 `~/.codex/auth.json` 手动导入一次 |
| `/codex sync` | operator | 同步模型目录到 `model_config.toml` |
| `/codex logout <序号>` | operator | 从账号库移除账号（令牌一并删除） |

> `model_config.toml` 的同步只在启动、登录成功和 `/codex sync` 时发生；`/codex models` 不会改宿主配置。


---

## 模型目录与同步（v0.3+）

插件加载、登录成功、执行 `/codex sync` 时会拉取账号的完整模型目录，并把选中的模型写进
`config/model_config.toml`：

- 自动创建（或复用）`api_providers` 里的 `Codex订阅` 条目（`client_type = codex_subscription`）；
- 每个模型生成一条 `[[models]]`，名字默认加 `codex-` 前缀（可配置），避免和已有模型重名；
- **只重建自己 provider 下的条目**：`model_task_config` 引用中的会保留，其余会被移除——
  包括你手动加到 `Codex订阅` 下面的条目，所以别把自定义模型挂在这个 provider 下；
- `provider_name`（默认 `Codex订阅`）**不要填成已有 provider 的名字**：如果同名 provider 的
  `client_type` 不是 `codex_subscription`，同步会直接跳过并记日志，不会删它的模型；
- 写前备份到 `config/old/model_config_codex_<时间>.toml`，只保留最近 10 份；
- MaiBot 有文件监听，写完自动热重载，无需重启。

`/codex models`（公开命令）只读目录缓存，不会强制联网，也不会改宿主配置；缓存过期才会
发一次带 Authorization 的请求到 `network.base_url`。

在插件配置页：

- **默认模型**：下拉框，选项来自实时目录（`/codex test` 和空 `model_identifier` 时用它）；
- **可选模型（多选）**：勾选哪些模型同步到麦麦的模型选择；留空 = 全部同步；
- **同步到模型配置**：关掉就只保留已同步的内容，不再自动改 `model_config.toml`。

取消勾选并保存后，下次同步会把对应条目移除（被任务引用的会保留）。

---

## 模型配置

```toml
[[api_providers]]
name = "codex-subscription"
base_url = "https://chatgpt.com/backend-api"
auth_type = "none"                # 凭证由插件账号库管理
client_type = "codex_subscription"
timeout = 300

[[models]]
model_identifier = "gpt-5.5"      # 填 /codex models 里的 slug
name = "gpt-5.5"
api_provider = "codex-subscription"
```

然后指给任务，例如：

```toml
[model_task_config.replyer]
model_list = ["gpt-5.5"]
max_tokens = 8192
temperature = 1.0
```

> 订阅后端不接受 `temperature` / `max_output_tokens` / `response_format`，插件在订阅模式下会自动剥离；
> 这些参数只在「账号里带 API Key 且开启 prefer_api_key」时才会发送。

---

## 插件配置

> ⚠️ `network.base_url` / `network.api_base_url` / `account.issuer` 决定令牌发往哪里。
> 插件会把账号的 access_token（或 API Key）作为 `Bearer` 发到这些地址，**不要指向 localhost、内网
> 或任何不可信地址**，除非你完全清楚自己在做什么。

| 分组 | 字段 | 默认 | 说明 |
| --- | --- | --- | --- |
| 插件 | `enabled` | `true` | 总开关 |
| 账号 | `accounts_file` | 空 | 账号库路径；空 = 插件数据目录 |
| 账号 | `active_account` | 空 | 指定账号（id 或邮箱）；空 = 自动选第一个 |
| 账号 | `import_codex_auth` | `false` | 账号库为空时自动导入 Codex 登录；建议保持关闭，用 `/codex import` 手动导入 |
| 账号 | `auto_refresh` | `true` | 自动刷新过期令牌 |
| 账号 | `prefer_api_key` | `false` | 有 API Key 时优先走按量计费 |
| 网络 | `proxy` | 空 | 例如 `http://127.0.0.1:7890`；空则读 `HTTPS_PROXY` |
| 网络 | `base_url` | `https://chatgpt.com/backend-api` | 订阅后端 |
| 网络 | `api_base_url` | `https://api.openai.com/v1` | API Key 模式后端 |
| 网络 | `timeout` / `connect_timeout` | 300 / 20 | 秒 |
| 模型 | `default_model` | 空 | `/codex test` 与空 `model_identifier` 时使用 |
| 模型 | `reasoning_effort` | `medium` | `default/low/medium/high/xhigh/max/ultra`，`default` = 不发送 |
| 模型 | `reasoning_summary` | `auto` | `default/auto/concise/detailed` |
| 模型 | `client_version` | 空 | `version` 头；模型目录不全时填 `codex --version` |
| 模型 | `originator` | `codex_cli_rs` | `originator` 头 |
| 模型 | `catalog_ttl` | 60 | 模型目录缓存秒数 |

---

## 排错

| 现象 | 处理 |
| --- | --- |
| `还没有登录任何 ChatGPT 账号` | 发 `/codex login` |
| 无法连接后端 / 超时 | 配 `network.proxy`（服务器不读系统代理） |
| HTTP 401 | 令牌失效：插件会尝试 refresh 一次，仍失败则重新登录 |
| HTTP 403 | 账号没有 Codex 权限，或模型不在订阅范围内 |
| HTTP 429 | 订阅额度/频率超限，与 Codex 共用同一份配额 |
| `model ... not supported` | 模型名不对或账号不可用：`/codex models` 看真实 slug |
| 模型列表为空 | 设置 `client_version`（`codex --version`）后重试 |
| 配置页下拉框报 `Select.Item value` | 0.1.2 已修（选项不再使用空字符串） |

日志 logger：`plugin.chenxi2542.maibot-codex-subscription`。

---

## 自测

```bash
python tests/test_protocol.py
```

覆盖：账号库读写/切换/移除、JWT 账号解析、Codex auth.json 导入、Item 协议序列化、
旧版 message_list 兼容、订阅模式参数裁剪、SSE 聚合、非流式兜底、Provider 端到端（假客户端）、
400 自动重试、HTTP 错误映射、命令解析。

---

## 合规与安全

- 只使用**你自己账号**的登录令牌，不绕过 OpenAI 的任何授权或风控，不做账号共享。
- 订阅额度按账号计量，与 Codex 共用同一份配额；请自行确认这种用法符合账号条款和所在平台规定。
- 令牌只存在本机账号库（0600），除 OpenAI 官方端点外不会发往任何地方，也不会写进日志。
- 订阅后端是「受限 Responses 实现」，OpenAI 调整协议时插件可能需要跟着更新。
