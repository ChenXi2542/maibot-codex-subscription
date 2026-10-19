# CHANGELOG

## 0.3.1

- 插件 ID 定为 `chenxi2542.maibot-codex-subscription`；首次加载会自动从旧 ID 的数据目录
  （`com.maibot.codex-subscription` / `com.chenxi.maibot-codex-subscription`）迁移 `accounts.json`。
- 修复：模型配置同步写回前增加自校验，`extra_params` 改用内联表，避免生成非法 TOML 导致麦麦无法启动。
- 修复：`/codex/models` 要求 `client_version` 查询参数，未配置时返回 HTTP 400。
  现在默认 `0.160.1` 并始终随请求发送（`version` 头同步）。
- 修复：Host 生成配置 Schema 走类方法时拿不到实例目录，改为同时覆盖
  `build_config_schema`，确保「默认模型」下拉与「可选模型」多选始终有选项。

## 0.3.0

- 新增：启动/登录/`/codex models` 时拉取完整模型目录，并把选中的模型**自动同步**进
  MaiBot 的 `model_config.toml`（自动创建 provider 条目，其余 provider 不动）。
- 新增：插件配置页「默认模型」为下拉框、「可选模型」为多选，选项来自实时目录。
- 新增：`/codex sync` 手动同步；同步后会触发 MaiBot 的文件监听热重载。
- 同步时保证：被任务配置引用的旧模型不删除，同名外部模型跳过，写前备份到 `config/old/`。

## 0.2.1

- 修复：后端返回 SSE 但 `content-type` 被网关改写时，插件误按 JSON 解析导致
  `无法解析后端响应：b'event: response.created...'`。改成按响应内容嗅探
  （先出现 `data:`/`event:` 即按 SSE 聚合），不再依赖响应头。

## 0.2.0

按 `WSL043/dsh-codex-subscription` 的思路重写：

- 新增插件自有**账号库**（`accounts.json`，0600）：多账号、切换、单独刷新；
  `~/.codex/auth.json` 仅在账号库为空时导入一次，之后不再依赖 Codex CLI。
- 新增命令：`/codex accounts`、`/codex use`、`/codex logout`；`/codex status` 改为显示账号库状态。
- 新增模型目录实时发现 + TTL 缓存（`GET /codex/models`）。
- 新增 `client_version` / `originator` / `catalog_ttl` 配置。
- 修复：订阅模式下 400 重试时异常被错误包装成“无法连接后端”。
- 配置节增加中文 label，WebUI 表单可读性更好。

## 0.1.2

- 修复配置下拉框崩溃 `A <Select.Item /> must have a value prop that is not an empty string`：
  `Literal` 选项不再包含空字符串，“不发送”改用 `default` 并在运行时归一化。

## 0.1.1

- 修复：补齐 `[plugin]` 配置节（`config_version` / `enabled`），解决 MaiBot 1.3.6 上
  配置生成失败、命令与 LLM Provider 不注册的问题。
- 新增 `display.icon`、CHANGELOG。

## 0.1.0

- 首个版本：`codex_subscription` LLM Provider、设备码登录、令牌刷新、`/codex` 命令。
