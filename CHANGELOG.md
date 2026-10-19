# CHANGELOG

## 0.3.5

第四轮审核的三条非阻断建议：

- 迁移旧 ID 账号库后补 `os.chmod(target, 0o600)`，`copy2` 不会再继承 0644 之类的源权限。
- `/codex models`（public）只读内存快照：不联网、不刷新令牌、不写盘；目录为空时提示由管理员
  `/codex sync` 拉取。
- `issuer` / `base_url` / `api_base_url` 的字段说明和 README 增加警告：这些地址会收到
  `Bearer` 令牌，不要指向不可信地址。

## 0.3.4

第三轮审核的三条非阻断建议：

- `/codex models`（public）改为读取目录缓存（TTL 内不再发请求），不会让群里任何人反复触发
  带鉴权的上游请求。
- 同步只保留最近 10 份 `config/old/model_config_codex_*.toml` 备份；README 明确说明
  「手动加到 `Codex订阅` 下的模型也会被同步移除」。
- 账号库写入改为 `os.open(..., 0o600)` 直接创建临时文件，去掉 write-then-chmod 的权限窗口期。

## 0.3.3

第二轮审核意见：

- `/codex models`（public）只做展示，不再触发 `model_config.toml` 同步；同步只由启动流程、
  登录成功后的内部流程和 operator 的 `/codex sync` 触发。
- `/codex test` 也改为 operator（会消耗订阅额度）。
- 登录成功回复不再输出账号库绝对路径。
- `_parse_command` 改用 Host 文档承诺的 `matched_groups`（命名捕获组 `action`/`arg`），
  旧 Host 只有原始文本时退化为文本解析；命令模式改用命名捕获组。
- `host_application.max_version` 从 `999.999.999` 收到 `1.3.99`。
- `accounts_file` 只允许插件数据目录内的路径，绝对路径/`..` 越界会回退到默认位置。
- manifest 与 `requirements.txt` 补上 `tomlkit` 依赖声明。

## 0.3.2

按插件中心审核意见收紧权限与信息暴露：

- `/codex` 拆成两个组件：只读的 `models` / `test` 保持 `public`；
  `login` / `accounts` / `use` / `status` / `sync` / `import` / `logout` 标 `permission="operator"`，
  由主程序统一鉴权，群里普通成员不能再切号、删号或拿走一次性设备码。
- `/codex accounts`、`/codex status` 与登录/切换/移除的回复全部脱敏：邮箱变 `a***@domain`，
  账号 ID 只保留前 8 位，不再输出账号库绝对路径。
- `import_codex_auth` 默认值改为 `false`；新增 `/codex import` 手动导入 `~/.codex/auth.json`。
- `model_config.toml` 的定位增加校验：同目录必须存在 `bot_config.toml`，否则不再自动改写。

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
