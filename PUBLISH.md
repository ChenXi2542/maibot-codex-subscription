# 发布到 MaiBot 插件商店

插件中心的索引仓库是 [Mai-with-u/plugin-repo](https://github.com/Mai-with-u/plugin-repo)，
插件本身必须是**独立的公开 GitHub 仓库**。本目录已经按提交清单准备好了，
只差把它推到你的 GitHub 账号下。

## 0. 先改三处身份信息

`_manifest.json` 里现在是占位值，推送前必须换成你自己的：

```json
"author": { "name": "你的名字", "url": "https://github.com/你的用户名" },
"urls": { "repository": "https://github.com/你的用户名/maibot-codex-subscription", ... }
```

不想手改就跑一次脚本（会同时初始化 git、提交并打 tag）：

```powershell
pwsh ./publish.ps1 -Owner 你的GitHub用户名
```

## 1. 建仓库并推送

在 GitHub 上新建一个**公开**空仓库 `maibot-codex-subscription`（不要勾选初始化 README），然后：

```bash
git remote add origin https://github.com/<你的用户名>/maibot-codex-subscription.git
git push -u origin main
git push --tags
```

## 2. 建 Release（版本号必须和 manifest 一致）

manifest 的 `version` 是 `0.3.1`，Tag 必须是 `v0.3.1` 或 `0.3.1`：

```bash
git tag v0.3.1
git push origin v0.3.1
```

然后在 GitHub 仓库 → Releases → Draft a new release → 选择 tag `v0.3.1`，
标题写 `v0.3.1`，发布说明可直接用 `CHANGELOG.md` 里 0.3.1 那一段。

> 以后每次发版：改 `_manifest.json` 的 `version` → 提交 → 打同号 tag → 建 Release。
> Tag / manifest / Release 三者版本号不一致，该版本会进 `rejected_releases`。

## 3. 提交到插件中心

打开 <https://github.com/Mai-with-u/plugin-repo/issues/new/choose>，选 **「Add Plugin / 添加插件」**，填：

- **插件 ID**：`com.maibot.codex-subscription`
- **仓库地址**：`https://github.com/<你的用户名>/maibot-codex-subscription`

CI 会自动读仓库根目录的 `_manifest.json` 校验，结果评论在 Issue 里；
标签流转 `pending-validation` → `validated` → 维护者 `/approve` → `approved`。
失败就按提示改仓库，然后在 Issue 里评论 `/recheck`。

## 提交清单（已核对）

- [x] 根目录有 `_manifest.json`（`manifest_version: 2`）、`plugin.py`、`LICENSE`、`README.md`
- [x] `id` 稳定：`com.maibot.codex-subscription`
- [x] 版本号三段式：`0.3.1`
- [x] `host_application` / `sdk` 上界 `999.999.999`，只约束 `min_version`
- [x] `author` 是 `{ name, url }` 对象
- [x] `capabilities` 只声明实际用到的 `send.text`
- [x] 已在真实 MaiBot 1.3.6 上加载、注册 Provider、跑通真实对话

## 商店里的隐私与合规提示

插件描述里建议保留 README 末尾「合规与安全」一节的口径：

- 只使用用户自己账号的登录令牌，不绕过 OpenAI 授权；
- 订阅额度与 Codex 共用，是否符合账号条款需使用者自行确认。

这样能减少审核时对「第三方客户端复用订阅额度」的疑问。
