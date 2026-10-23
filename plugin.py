"""MaiBot 插件：通过 ChatGPT 账号登录，使用 ChatGPT Plus/Pro（Codex）订阅额度。

设计参考 WSL043/dsh-codex-subscription（v2.5）：
* 账号由插件自己管理和登录（内置 OAuth 设备码流程），不依赖本机安装 Codex CLI；
* 凭证保存在插件自己的账户库（多账号、可切换、可单独刷新），而不是把 ~/.codex/auth.json
  当成唯一来源（只在账户库为空时可选导入一次）；
* 模型目录从 ``/codex/models`` 实时发现并缓存；
* 请求走 Codex 使用的 Responses API（与 pi-ai 的 openai-codex-responses 同一套 wire）。

与 DSH 版的差异：MaiBot 的插件 Provider 不支持 Host 侧流式处理器，因此这里在插件内部
聚合 SSE，只把最终结果交给 Host。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal

try:  # MaiBot Runner 会按 manifest 的依赖声明安装 httpx
    import httpx
except ImportError:  # pragma: no cover - 仅在依赖缺失时触发
    httpx = None  # type: ignore[assignment]

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    LLMProvider,
    MaiBotPlugin,
    PluginConfigBase,
)

__version__ = "0.3.9"

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

CLIENT_TYPE = "codex_subscription"

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_CHATGPT_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CLIENT_VERSION = "0.160.1"

MAX_CALL_ID_LENGTH = 64
CALL_ID_PATTERN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

#: 订阅后端拒绝的参数（官方 API Key 模式不受影响）。
CHATGPT_UNSUPPORTED_KEYS = ("max_output_tokens", "temperature", "top_p", "stop", "response_format")

DEVICE_LOGIN_TIMEOUT = 15 * 60

#: 历史插件 ID；升级到 com.chenxi.maibot-codex-subscription 后用于迁移账号库。
LEGACY_PLUGIN_IDS = ("com.maibot.codex-subscription", "com.chenxi.maibot-codex-subscription")

#: 最近一次拉到的模型目录（供配置页 Schema 生成下拉/多选选项）。
_LAST_CATALOG: list[dict[str, str]] = []

logger = logging.getLogger(__name__)


class CodexError(RuntimeError):
    """可读错误，消息会直接回给 Host / 用户。"""


# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """插件基础设置（Host 依赖 [plugin] 节跟踪配置版本与总开关）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "plug"
    __ui_order__ = 0

    config_version: str = Field(
        default=__version__,
        description="配置 schema 版本，请勿手动修改。",
        json_schema_extra={"hidden": True, "disabled": True, "order": 99},
    )
    enabled: bool = Field(default=True, description="总开关。关闭后插件停止工作。")


class AccountConfig(PluginConfigBase):
    """账号库与账号选择。"""

    __ui_label__ = "账号"
    __ui_icon__ = "users"
    __ui_order__ = 1

    accounts_file: str = Field(
        default="",
        description="账号库文件；留空使用插件数据目录下的 accounts.json",
        json_schema_extra={"label": "账号库文件", "placeholder": "data/plugins/.../accounts.json"},
    )
    active_account: str = Field(
        default="",
        description="要使用的账号（account_id 或邮箱）；留空自动选第一个可用账号",
        json_schema_extra={"label": "当前账号"},
    )
    import_codex_auth: bool = Field(
        default=False,
        description="账号库为空时自动从 CODEX_HOME/auth.json 导入一次；建议保持关闭，需要时用 /codex import 手动导入",
        json_schema_extra={"label": "启动时自动导入 Codex 登录"},
    )
    auto_refresh: bool = Field(
        default=True,
        description="access_token 快过期或收到 HTTP 401 时自动刷新",
        json_schema_extra={"label": "自动刷新令牌"},
    )
    prefer_api_key: bool = Field(
        default=False,
        description="账号里同时存在 API Key 时优先使用（按量计费）",
        json_schema_extra={"label": "优先使用 API Key"},
    )
    issuer: str = Field(
        default=DEFAULT_ISSUER,
        description="OAuth 签发地址。请勿指向不可信地址：账号令牌会被发往该地址换取/刷新凭证",
        json_schema_extra={"label": "OAuth 签发地址", "hint": "保持默认 auth.openai.com；改成其它地址等于把令牌交给它"},
    )


class NetworkConfig(PluginConfigBase):
    """网络。"""

    __ui_label__ = "网络"
    __ui_icon__ = "wifi"
    __ui_order__ = 2

    proxy: str = Field(
        default="",
        description="HTTP 代理，例如 http://127.0.0.1:7890；留空读取 HTTPS_PROXY / HTTP_PROXY",
        json_schema_extra={"label": "代理地址", "placeholder": "http://127.0.0.1:7890"},
    )
    base_url: str = Field(
        default=DEFAULT_CHATGPT_BASE_URL,
        description="订阅后端根地址。请勿指向不可信地址：访问令牌会作为 Bearer 发往该地址",
        json_schema_extra={"label": "订阅后端地址", "hint": "默认 chatgpt.com；改成内网/不明地址会泄露访问令牌"},
    )
    api_base_url: str = Field(
        default=DEFAULT_OPENAI_BASE_URL,
        description="API Key 模式后端根地址。请勿指向不可信地址：API Key 会作为 Bearer 发往该地址",
        json_schema_extra={"label": "API Key 后端地址", "hint": "默认 api.openai.com；改成内网/不明地址会泄露 API Key"},
    )
    timeout: float = Field(default=300.0, description="请求总超时（秒）", json_schema_extra={"label": "请求超时"})
    connect_timeout: float = Field(default=20.0, description="连接超时（秒）", json_schema_extra={"label": "连接超时"})


class ModelConfig(PluginConfigBase):
    """模型与协议。"""

    __ui_label__ = "模型与协议"
    __ui_icon__ = "tune"
    __ui_order__ = 3

    default_model: str = Field(
        default="",
        description="默认模型（下拉选择）；模型配置里 model_identifier 留空时也用它",
        json_schema_extra={"label": "默认模型", "x-widget": "select", "placeholder": "gpt-6-luna"},
    )
    enabled_models: list[str] = Field(
        default_factory=list,
        description="勾选要在麦麦模型选择里出现的模型；留空表示全部同步",
        json_schema_extra={"label": "可选模型（多选）", "x-widget": "select"},
    )
    sync_model_config: bool = Field(
        default=True,
        description="把勾选的模型写入 MaiBot 的 model_config.toml（自动创建 Codex 订阅 provider 条目）",
        json_schema_extra={"label": "同步到模型配置"},
    )
    provider_name: str = Field(
        default="Codex订阅",
        description="写入 model_config.toml 的 api_providers 名称",
        json_schema_extra={"label": "Provider 名称"},
    )
    model_name_prefix: str = Field(
        default="codex-",
        description="同步到模型选择器时给模型名加的前缀，避免与已有模型重名",
        json_schema_extra={"label": "模型名前缀"},
    )
    reasoning_effort: Literal["default", "low", "medium", "high", "xhigh", "max", "ultra"] = Field(
        default="medium",
        description="推理强度；default 表示不发送，由后端决定",
        json_schema_extra={"label": "推理强度"},
    )
    reasoning_summary: Literal["default", "auto", "concise", "detailed"] = Field(
        default="auto",
        description="推理摘要；default 表示不请求",
        json_schema_extra={"label": "推理摘要"},
    )
    max_output_tokens: int = Field(
        default=0,
        description="仅 API Key 模式生效；订阅后端会拒绝该参数",
        json_schema_extra={"label": "输出上限（API Key 模式）"},
    )
    client_version: str = Field(
        default="0.160.1",
        description="发给后端的 Codex 客户端版本号；模型目录接口要求必填，按 codex --version 调整",
        json_schema_extra={"label": "客户端版本号", "placeholder": "0.160.1"},
    )
    originator: str = Field(
        default="codex_cli_rs",
        description="originator 请求头",
        json_schema_extra={"label": "originator"},
    )
    catalog_ttl: int = Field(default=60, description="模型目录缓存秒数", json_schema_extra={"label": "目录缓存秒数"})


class CodexPluginConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    account: AccountConfig = Field(default_factory=AccountConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)


# --------------------------------------------------------------------------------------
# JWT 与账号
# --------------------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def jwt_auth_claims(token: str) -> dict[str, Any]:
    claims = jwt_payload(token).get("https://api.openai.com/auth")
    return claims if isinstance(claims, dict) else {}


def account_identity(access_token: str, fallback: str = "") -> tuple[str, str, str, float]:
    """从令牌里读出 (account_id, email, plan, expires_at)。"""
    payload = jwt_payload(access_token)
    claims = jwt_auth_claims(access_token)
    account_id = str(claims.get("chatgpt_account_id") or "").strip()
    email = str(payload.get("email") or "").strip()
    plan = str(claims.get("chatgpt_plan_type") or "").strip()
    try:
        expires_at = float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if not account_id:
        account_id = fallback or hashlib.sha256(str(access_token).encode()).hexdigest()[:16]
    return account_id, email, plan, expires_at


def mask_email(value: str) -> str:
    """邮箱脱敏：a***@domain，避免在群聊里直接暴露账号。"""
    email = str(value or "").strip()
    if not email or "@" not in email:
        return email or "(未知)"
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"


def mask_id(value: str) -> str:
    """账号 ID 只保留前 8 位，避免把完整标识递出去。"""
    text = str(value or "")
    return f"{text[:8]}…" if len(text) > 8 else text


@dataclass(slots=True)
class Account:
    """账号库中的一条账号。"""

    account_id: str
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    email: str = ""
    plan: str = ""
    expires_at: float = 0.0
    added_at: str = ""
    source: str = "login"
    api_key: str = ""

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at <= time.time() + 60

    @property
    def label(self) -> str:
        return self.email or self.account_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "email": self.email,
            "plan": self.plan,
            "expires_at": self.expires_at,
            "added_at": self.added_at,
            "source": self.source,
            "api_key": self.api_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        access_token = str(data.get("access_token") or "")
        account_id = str(data.get("account_id") or "")
        email = str(data.get("email") or "")
        plan = str(data.get("plan") or "")
        expires_at = float(data.get("expires_at") or 0)
        if access_token:
            derived_id, derived_email, derived_plan, derived_exp = account_identity(access_token, account_id)
            account_id = account_id or derived_id
            email = email or derived_email
            plan = plan or derived_plan
            expires_at = expires_at or derived_exp
        return cls(
            account_id=account_id or hashlib.sha256(str(access_token).encode()).hexdigest()[:16],
            access_token=access_token,
            refresh_token=str(data.get("refresh_token") or ""),
            id_token=str(data.get("id_token") or ""),
            email=email,
            plan=plan,
            expires_at=expires_at,
            added_at=str(data.get("added_at") or ""),
            source=str(data.get("source") or "login"),
            api_key=str(data.get("api_key") or ""),
        )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON，并且从创建那一刻起就是 0600（不经过 umask 的窗口期）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


class AccountVault:
    """插件自己的账号库：多账号、可切换、原子落盘。"""

    def __init__(self, path: Callable[[], Path]) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path()

    def _load_raw(self) -> dict[str, Any]:
        path = self.path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "active": "", "accounts": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexError(f"账号库读取失败：{path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise CodexError(f"账号库格式不对：{path}")
        return data

    def accounts(self) -> list[Account]:
        raw = self._load_raw()
        entries = raw.get("accounts")
        if not isinstance(entries, list):
            return []
        accounts = []
        for entry in entries:
            if isinstance(entry, dict):
                try:
                    accounts.append(Account.from_dict(entry))
                except Exception:  # pragma: no cover - 跳过坏条目
                    continue
        return accounts

    def active_id(self) -> str:
        return str(self._load_raw().get("active") or "")

    def resolve(self, desired: str = "") -> Account:
        accounts = self.accounts()
        if not accounts:
            raise CodexError(
                "还没有登录任何 ChatGPT 账号。\n"
                "请发送 `/codex login` 用设备码登录，或先在服务器上执行 `codex login` 后重启插件导入。"
            )
        target = (desired or self.active_id()).strip()
        if target:
            for account in accounts:
                if target in (account.account_id, account.email, account.label):
                    return account
        return accounts[0]

    def save(self, accounts: list[Account], active: str = "") -> None:
        payload = {
            "version": 1,
            "active": active or (accounts[0].account_id if accounts else ""),
            "accounts": [account.to_dict() for account in accounts],
        }
        _atomic_write_json(self.path, payload)

    def upsert(self, account: Account, *, make_active: bool = True) -> Account:
        accounts = self.accounts()
        for index, existing in enumerate(accounts):
            if existing.account_id == account.account_id:
                account.added_at = existing.added_at or account.added_at
                accounts[index] = account
                break
        else:
            accounts.append(account)
        active = account.account_id if make_active else self.active_id()
        self.save(accounts, active)
        return account

    def remove(self, target: str) -> bool:
        accounts = self.accounts()
        remaining = [a for a in accounts if target not in (a.account_id, a.email, a.label)]
        if len(remaining) == len(accounts):
            return False
        active = self.active_id()
        if active == target:
            active = remaining[0].account_id if remaining else ""
        self.save(remaining, active)
        return True


def import_codex_auth(path: Path, issuer: str = DEFAULT_ISSUER) -> Account | None:
    """从 Codex CLI 的 auth.json 导入一次（只读，不改写）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access_token = str((tokens or {}).get("access_token") or "")
    refresh_token = str((tokens or {}).get("refresh_token") or "")
    if not access_token:
        return None
    account_id, email, plan, expires_at = account_identity(access_token)
    return Account(
        account_id=account_id,
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=str((tokens or {}).get("id_token") or ""),
        email=email,
        plan=plan,
        expires_at=expires_at,
        added_at=datetime.now(timezone.utc).isoformat(),
        source="import",
        api_key=str(data.get("OPENAI_API_KEY") or ""),
    )


# --------------------------------------------------------------------------------------
# OAuth（设备码）
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class DeviceCode:
    verification_url: str
    user_code: str
    device_auth_id: str
    interval: float


def _auth_headers() -> dict[str, str]:
    return {"User-Agent": f"maibot-codex-subscription/{__version__}", "originator": "codex_cli_rs"}


async def request_device_code(client: Any, issuer: str) -> DeviceCode:
    base = f"{issuer.rstrip('/')}/api/accounts"
    try:
        resp = await client.post(
            f"{base}/deviceauth/usercode", json={"client_id": CODEX_CLIENT_ID}, headers=_auth_headers()
        )
    except Exception as exc:  # httpx.HTTPError
        raise CodexError(f"申请设备码失败（网络错误）：{exc}") from exc
    if resp.status_code == 404:
        raise CodexError("当前 OAuth 服务未启用设备码登录，请检查 auth.issuer 配置。")
    if resp.status_code != 200:
        raise CodexError(f"申请设备码失败（HTTP {resp.status_code}）：{resp.text[:200]}")

    payload = resp.json()
    try:
        interval = float(str(payload.get("interval", 5)).strip() or 5)
    except ValueError:
        interval = 5.0
    return DeviceCode(
        verification_url=f"{issuer.rstrip('/')}/codex/device",
        user_code=str(payload.get("user_code") or ""),
        device_auth_id=str(payload.get("device_auth_id") or ""),
        interval=max(1.0, interval),
    )


async def poll_device_token(
    client: Any, issuer: str, device: DeviceCode, *, deadline: float
) -> dict[str, Any]:
    base = f"{issuer.rstrip('/')}/api/accounts"
    while time.time() < deadline:
        try:
            resp = await client.post(
                f"{base}/deviceauth/token",
                json={"device_auth_id": device.device_auth_id, "user_code": device.user_code},
                headers=_auth_headers(),
            )
        except Exception:
            await asyncio.sleep(device.interval)
            continue
        if resp.status_code == 200:
            payload = resp.json()
            if not payload.get("authorization_code"):
                raise CodexError("设备码登录返回内容缺少 authorization_code。")
            return payload
        if resp.status_code in (403, 404):
            await asyncio.sleep(device.interval)
            continue
        raise CodexError(f"设备码登录失败（HTTP {resp.status_code}）：{resp.text[:200]}")
    raise CodexError("设备码登录超时（15 分钟），请重新发送 `/codex login`。")


async def exchange_device_code(client: Any, issuer: str, token_payload: dict[str, Any]) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": str(token_payload.get("authorization_code") or ""),
        "redirect_uri": f"{issuer.rstrip('/')}/deviceauth/callback",
        "client_id": CODEX_CLIENT_ID,
        "code_verifier": str(token_payload.get("code_verifier") or ""),
    }
    try:
        resp = await client.post(
            f"{issuer.rstrip('/')}/oauth/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", **_auth_headers()},
        )
    except Exception as exc:
        raise CodexError(f"换取令牌失败（网络错误）：{exc}") from exc
    if resp.status_code != 200:
        raise CodexError(f"换取令牌失败（HTTP {resp.status_code}）：{resp.text[:300]}")
    return resp.json()


async def refresh_account(client: Any, issuer: str, account: Account) -> Account:
    """用 refresh_token 换新 access_token；失败抛 CodexError。"""
    if not account.refresh_token:
        raise CodexError(f"账号 {account.label} 没有 refresh_token，请重新登录。")
    data = {"grant_type": "refresh_token", "refresh_token": account.refresh_token, "client_id": CODEX_CLIENT_ID}
    try:
        resp = await client.post(
            f"{issuer.rstrip('/')}/oauth/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except Exception as exc:
        raise CodexError(f"刷新令牌失败（网络错误）：{exc}") from exc
    if resp.status_code != 200:
        raise CodexError(
            f"刷新令牌失败（HTTP {resp.status_code}）：{resp.text[:300]}\n"
            "订阅可能已过期或被限制，请重新执行 `/codex login`。"
        )
    payload = resp.json()
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise CodexError("刷新接口没有返回 access_token。")
    account.access_token = access_token
    if payload.get("refresh_token"):
        account.refresh_token = str(payload["refresh_token"])
    if payload.get("id_token"):
        account.id_token = str(payload["id_token"])
    account_id, email, plan, expires_at = account_identity(access_token, account.account_id)
    account.account_id = account_id
    account.email = email or account.email
    account.plan = plan or account.plan
    account.expires_at = expires_at
    return account


def build_account_from_tokens(tokens: dict[str, Any]) -> Account:
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        raise CodexError("登录成功但响应里没有 access_token。")
    account_id, email, plan, expires_at = account_identity(access_token)
    return Account(
        account_id=account_id,
        access_token=access_token,
        refresh_token=str(tokens.get("refresh_token") or ""),
        id_token=str(tokens.get("id_token") or ""),
        email=email,
        plan=plan,
        expires_at=expires_at,
        added_at=datetime.now(timezone.utc).isoformat(),
        source="login",
    )


# --------------------------------------------------------------------------------------
# 协议转换：MaiBot Context Items -> Responses API
# --------------------------------------------------------------------------------------


def normalize_call_id(value: Any) -> str:
    text = str(value or "")
    if text and len(text) <= MAX_CALL_ID_LENGTH and set(text) <= CALL_ID_PATTERN_CHARS:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return "call_" + digest[: MAX_CALL_ID_LENGTH - len("call_")]


def _parts_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
            chunks.append(part["refusal"])
    return "".join(chunks)


def _image_part_base64(part: dict[str, Any]) -> tuple[str, str] | None:
    image_format = str(part.get("image_format") or "png").lower()
    raw = part.get("image_base64")
    if isinstance(raw, str) and raw:
        return ("jpeg" if image_format in {"jpg", "jpeg"} else image_format), raw
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    if isinstance(image_url, str) and image_url.startswith("data:"):
        header, _, payload = image_url.partition(",")
        fmt = header.split("/")[-1].split(";")[0] or "png"
        return ("jpeg" if fmt in {"jpg", "jpeg"} else fmt), payload
    return None


def _content_from_parts(parts: Any) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if not isinstance(parts, list):
        return content
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text") and isinstance(part.get("text"), str):
            content.append({"type": "input_text", "text": part["text"]})
        elif kind == "refusal" and isinstance(part.get("refusal"), str):
            content.append({"type": "input_text", "text": part["refusal"]})
        elif kind in ("image", "image_url", "input_image"):
            image = _image_part_base64(part)
            if image is not None:
                fmt, data = image
                content.append({"type": "input_image", "image_url": f"data:image/{fmt};base64,{data}"})
    return content


def serialize_context_items(items: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Host Item 协议 -> instructions + input。"""
    instructions: list[str] = []
    wire: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return instructions, wire

    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("item_type")
        if item_type == "SystemMessageItem":
            text = _parts_text(item.get("parts"))
            if text.strip():
                instructions.append(text)
        elif item_type == "UserMessageItem":
            content = _content_from_parts(item.get("parts"))
            if content:
                wire.append({"type": "message", "role": "user", "content": content})
        elif item_type == "AssistantMessageItem":
            text = _parts_text(item.get("parts"))
            if text:
                wire.append(
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
                )
        elif item_type == "FunctionCallItem":
            call = item.get("tool_call") if isinstance(item.get("tool_call"), dict) else {}
            name = str(call.get("func_name") or "")
            if not name:
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            wire.append(
                {
                    "type": "function_call",
                    "call_id": normalize_call_id(call.get("call_id")),
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            )
        elif item_type == "FunctionCallOutputItem":
            wire.append(
                {
                    "type": "function_call_output",
                    "call_id": normalize_call_id(item.get("call_id")),
                    "output": str(item.get("output") or ""),
                }
            )
    return instructions, wire


def serialize_legacy_messages(messages: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """旧版 message_list（Chat 风格）-> instructions + input。"""
    instructions: list[str] = []
    wire: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return instructions, wire

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        raw_content = message.get("content")
        if isinstance(raw_content, str):
            content_parts: list[dict[str, Any]] = [{"type": "input_text", "text": raw_content}]
            text = raw_content
        else:
            content_parts = _content_from_parts(raw_content)
            text = _parts_text(raw_content)

        if role == "system":
            if text.strip():
                instructions.append(text)
        elif role == "tool":
            wire.append(
                {
                    "type": "function_call_output",
                    "call_id": normalize_call_id(message.get("tool_call_id") or message.get("call_id")),
                    "output": text or "(no output)",
                }
            )
        elif role == "assistant":
            if text:
                wire.append(
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
                )
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or "")
                if not name:
                    continue
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                wire.append(
                    {
                        "type": "function_call",
                        "call_id": normalize_call_id(call.get("id")),
                        "name": name,
                        "arguments": arguments,
                    }
                )
        elif content_parts:
            wire.append({"type": "message", "role": "user", "content": content_parts})
    return instructions, wire


def serialize_tools(tool_options: Any) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if not isinstance(tool_options, list):
        return tools
    for option in tool_options:
        if not isinstance(option, dict):
            continue
        function = option.get("function") if isinstance(option.get("function"), dict) else option
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}, "required": []}
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": parameters,
            }
        )
    return tools


def normalize_effort(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "" if text in {"", "default", "none", "off", "disabled"} else text


def normalize_summary(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "" if text in {"", "default", "none", "off"} else text


def build_responses_body(
    request: dict[str, Any],
    *,
    model: str,
    effort: str = "",
    summary: str = "",
    max_output_tokens: int = 0,
    include_temperature: bool = True,
) -> dict[str, Any]:
    if not model:
        raise CodexError("没有可用的模型名：请在模型配置里填写 model_identifier，或设置插件 default_model。")

    if isinstance(request.get("context_items"), list):
        instructions, wire_input = serialize_context_items(request.get("context_items"))
    else:
        instructions, wire_input = serialize_legacy_messages(request.get("message_list") or request.get("messages"))

    body: dict[str, Any] = {"model": model, "input": wire_input, "stream": True, "store": False}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)

    tools = serialize_tools(request.get("tool_options"))
    if tools:
        body["tools"] = tools
    if effort or summary:
        reasoning: dict[str, Any] = {}
        if effort:
            reasoning["effort"] = effort
        if summary:
            reasoning["summary"] = summary
        body["reasoning"] = reasoning

    if include_temperature:
        temperature = request.get("temperature")
        if isinstance(temperature, (int, float)):
            body["temperature"] = float(temperature)
        response_format = request.get("response_format")
        if isinstance(response_format, dict) and response_format.get("format_type") == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": response_format.get("schema") or {}}
    if max_output_tokens:
        body["max_output_tokens"] = int(max_output_tokens)
    return body


def drop_unsupported(body: dict[str, Any]) -> list[str]:
    removed = [key for key in CHATGPT_UNSUPPORTED_KEYS if key in body]
    for key in removed:
        body.pop(key, None)
    return removed


# --------------------------------------------------------------------------------------
# SSE -> 结果
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ToolCallData:
    call_id: str
    name: str
    arguments: str = ""

    def args_dict(self) -> dict[str, Any]:
        text = self.arguments.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass(slots=True)
class StreamResult:
    text: str = ""
    reasoning: str = ""
    reasoning_kind: str = "raw_text"
    tool_calls: list[ToolCallData] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    response_id: str = ""
    status: str = "completed"


def map_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached = details.get("cached_tokens")
    cached_tokens = int(cached) if isinstance(cached, (int, float)) else 0
    return {
        "prompt_tokens": max(input_tokens - cached_tokens, 0),
        "completion_tokens": output_tokens,
        "total_tokens": int(usage.get("total_tokens") or (input_tokens + output_tokens)),
        "prompt_cache_hit_tokens": cached_tokens,
        "prompt_cache_miss_tokens": max(input_tokens - cached_tokens, 0),
    }


class SseCollector:
    def __init__(self) -> None:
        self.result = StreamResult()
        self._calls: dict[str, ToolCallData] = {}

    def handle(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._ensure_call(item)
        elif event_type in ("response.output_text.delta", "response.refusal.delta"):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self.result.text += delta
        elif event_type == "response.reasoning_summary_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self.result.reasoning += delta
                self.result.reasoning_kind = "summary"
        elif event_type == "response.reasoning_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self.result.reasoning += delta
                self.result.reasoning_kind = "raw_text"
        elif event_type == "response.function_call_arguments.delta":
            item_id = str(event.get("item_id") or "")
            call = self._calls.get(item_id)
            if call is None:
                call = ToolCallData(call_id=normalize_call_id(item_id), name="")
                self._calls[item_id] = call
                self.result.tool_calls.append(call)
            delta = event.get("delta")
            if isinstance(delta, str):
                call.arguments += delta
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                call = self._ensure_call(item)
                arguments = item.get("arguments")
                if isinstance(arguments, str) and arguments and not call.arguments:
                    call.arguments = arguments
        elif event_type in ("response.completed", "response.done"):
            response = event.get("response") if isinstance(event.get("response"), dict) else {}
            mapped = map_usage(event.get("usage") or response.get("usage"))
            if mapped:
                self.result.usage = mapped
            self.result.response_id = str(response.get("id") or event.get("response_id") or "")
            self.result.status = str(response.get("status") or "completed")
        elif event_type == "response.failed":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            raise CodexError(f"模型返回失败：{error.get('message') or 'unknown error'}")
        elif event_type == "error":
            raise CodexError(f"后端返回错误：{event.get('message') or 'provider error'}")

    def _ensure_call(self, item: dict[str, Any]) -> ToolCallData:
        item_id = str(item.get("id") or "")
        call = self._calls.get(item_id)
        if call is None:
            call = ToolCallData(
                call_id=normalize_call_id(item.get("call_id") or item_id), name=str(item.get("name") or "")
            )
            self._calls[item_id] = call
            self.result.tool_calls.append(call)
        else:
            if not call.name and item.get("name"):
                call.name = str(item["name"])
            if item.get("call_id"):
                call.call_id = normalize_call_id(item["call_id"])
        return call


def looks_like_sse(text: str) -> bool:
    """按内容判断是不是 SSE：先出现 data:/event: 就算，先出现 { 就当 JSON。"""
    for line in text[:8000].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("data:", "event:")):
            return True
        if stripped.startswith("{"):
            return False
    return False


async def iterate_lines(text: str) -> AsyncIterator[str]:
    for line in text.splitlines():
        yield line


async def collect_sse(lines: AsyncIterator[str]) -> StreamResult:
    collector = SseCollector()
    async for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        if not data:
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            collector.handle(event)
    return collector.result


def parse_json_response(raw: bytes) -> StreamResult:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexError(f"无法解析后端响应：{raw[:200]!r}") from exc
    result = StreamResult()
    result.response_id = str(payload.get("id") or "")
    result.status = str(payload.get("status") or "completed")
    result.usage = map_usage(payload.get("usage"))
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    result.text += str(part.get("text") or "")
        elif item.get("type") == "reasoning":
            for part in item.get("summary") or []:
                if isinstance(part, dict):
                    result.reasoning += str(part.get("text") or "")
                    result.reasoning_kind = "summary"
        elif item.get("type") == "function_call":
            result.tool_calls.append(
                ToolCallData(
                    call_id=normalize_call_id(item.get("call_id")),
                    name=str(item.get("name") or ""),
                    arguments=str(item.get("arguments") or ""),
                )
            )
    return result


def build_output_items(result: StreamResult, schema_version: Any) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if result.reasoning:
        if result.reasoning_kind == "summary":
            items.append(
                {
                    "item_type": "ReasoningItem",
                    "representation": "summary",
                    "summary_parts": [result.reasoning],
                    "text_parts": [],
                }
            )
        else:
            items.append(
                {
                    "item_type": "ReasoningItem",
                    "representation": "raw_text",
                    "summary_parts": [],
                    "text_parts": [result.reasoning],
                }
            )
    if result.text:
        items.append({"item_type": "AssistantMessageItem", "parts": [{"type": "text", "text": result.text}]})
    for call in result.tool_calls:
        if not call.name:
            continue
        items.append(
            {
                "item_type": "FunctionCallItem",
                "tool_call": {
                    "call_id": call.call_id,
                    "func_name": call.name,
                    "args": call.args_dict(),
                    "extra_content": None,
                },
            }
        )
    return {"output_items": items, "item_schema_version": schema_version}


def build_legacy_response(result: StreamResult) -> dict[str, Any]:
    return {
        "content": result.text,
        "reasoning_content": result.reasoning,
        "tool_calls": [
            {"id": call.call_id, "function": {"name": call.name, "arguments": call.args_dict()}}
            for call in result.tool_calls
            if call.name
        ],
        "usage": result.usage,
    }


def extract_text(payload: dict[str, Any]) -> str:
    items = payload.get("output_items")
    if isinstance(items, list):
        chunks: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("item_type") == "AssistantMessageItem":
                chunks.append(_parts_text(item.get("parts")))
        return "".join(chunks)
    return str(payload.get("content") or "")


# --------------------------------------------------------------------------------------
# 模型目录与 model_config.toml 同步
# --------------------------------------------------------------------------------------


def normalize_catalog(entries: Any) -> list[dict[str, str]]:
    """把后端目录整理成 [{slug, name}]，去重并保留顺序。"""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or entry.get("id") or "").strip()
        if not slug or slug in seen:
            continue
        name = str(entry.get("display_name") or entry.get("name") or slug).strip() or slug
        seen.add(slug)
        result.append({"slug": slug, "name": name})
    return result


def selected_slugs(catalog: list[dict[str, str]], enabled: list[str] | None) -> list[str]:
    """勾选为空时返回全部；否则只保留目录里真实存在的项。"""
    available = [item["slug"] for item in catalog]
    chosen = [str(item).strip() for item in (enabled or []) if str(item).strip()]
    if not chosen:
        return available
    available_set = set(available)
    if not available_set:
        return chosen
    return [slug for slug in chosen if slug in available_set]


def inject_catalog_choices(schema: Any, slugs: list[str]) -> Any:
    """把模型目录注入配置 Schema：默认模型下拉、可选模型多选。"""
    if not isinstance(schema, dict) or not slugs:
        return schema
    sections = schema.get("sections")
    fields = sections.get("model", {}).get("fields") if isinstance(sections, dict) else None
    if not isinstance(fields, dict):
        return schema
    for key in ("default_model", "enabled_models"):
        field = fields.get(key)
        if not isinstance(field, dict):
            continue
        field["type"] = "select"
        field["ui_type"] = "select"
        field["choices"] = list(slugs)
    if isinstance(fields.get("enabled_models"), dict):
        fields["enabled_models"]["multiple"] = True
    return schema


def sync_model_config(
    path: Path,
    *,
    provider_name: str,
    prefix: str,
    slugs: list[str],
    base_url: str,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """把选中的模型同步进 MaiBot 的 model_config.toml。

    使用 tomlkit 保留注释与原有排版；只重建 ``api_provider == provider_name`` 的模型条目，
    其它 provider 的模型一律不动。被任务配置引用的旧模型不会删除，避免配置校验失败。
    """
    result: dict[str, Any] = {"changed": False, "added": 0, "removed": 0, "kept": 0, "path": str(path)}
    if not path.is_file():
        result["reason"] = f"找不到 {path}"
        return result
    try:
        import tomlkit
    except ImportError:
        result["reason"] = "当前环境缺少 tomlkit，已跳过自动同步"
        return result

    text = path.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:  # pragma: no cover - 配置文件损坏时交回给用户
        result["reason"] = f"model_config.toml 解析失败：{exc}"
        return result

    providers = doc.get("api_providers")
    if providers is None:
        doc["api_providers"] = tomlkit.aot()
        providers = doc["api_providers"]

    provider = None
    for item in providers:
        if str(item.get("name") or "") != provider_name:
            continue
        # 同名但类型不是本插件的 provider，说明是别人家的（比如手写的 openai），一律不碰
        if str(item.get("client_type") or "") != CLIENT_TYPE:
            result["reason"] = (
                f"已存在同名 provider「{provider_name}」"
                f"（client_type={str(item.get('client_type') or '') or '未设置'}），已跳过同步以免误删其模型"
            )
            logger.warning("%s", result["reason"])
            return result
        provider = item
        break
    if provider is None:
        provider = tomlkit.table()
        provider["name"] = provider_name
        provider["base_url"] = base_url
        provider["api_key"] = ""
        provider["auth_type"] = "none"
        provider["client_type"] = CLIENT_TYPE
        provider["timeout"] = float(timeout)
        provider["max_retry"] = 1
        providers.append(provider)

    referenced: set[str] = set()
    tasks = doc.get("model_task_config")
    if tasks is not None:
        for _key, section in tasks.items():
            if hasattr(section, "get"):
                names = section.get("model_list")
                if names:
                    referenced.update(str(name) for name in names)

    models = doc.get("models")
    if models is None:
        doc["models"] = tomlkit.aot()
        models = doc["models"]

    foreign_names = {str(item.get("name") or "") for item in models if str(item.get("api_provider") or "") != provider_name}
    existing_managed: dict[str, Any] = {
        str(item.get("name") or ""): item
        for item in models
        if str(item.get("api_provider") or "") == provider_name
    }
    kept: list[Any] = []
    kept_managed_names: set[str] = set()
    for item in list(models):
        if str(item.get("api_provider") or "") == provider_name:
            name = str(item.get("name") or "")
            if name in referenced:
                if "model_type" not in item:
                    item["model_type"] = "llm"
                kept.append(item)
                kept_managed_names.add(name)
                result["kept"] += 1
            else:
                result["removed"] += 1
            continue
        kept.append(item)

    managed: list[Any] = []
    for slug in slugs:
        name = f"{prefix}{slug}"
        if name in foreign_names or name in kept_managed_names:
            # 已被任务配置引用而保留的同名条目不再重复添加，否则模型名重复会导致麦麦启动失败
            result.setdefault("skipped", []).append(name)
            continue
        table = tomlkit.table()
        table["model_identifier"] = slug
        table["name"] = name
        table["api_provider"] = provider_name
        # MaiBot 1.3.14+ 的 ModelInfo 把 model_type 变成必填字段，缺了会导致启动/刷新失败
        table["model_type"] = "llm"
        table["price_in"] = 0.0
        table["price_out"] = 0.0
        table["cache_price_in"] = 0.0
        table["send_temperature"] = True
        table["visual"] = False
        table["force_stream_mode"] = False
        table["extra_params"] = tomlkit.inline_table()

        # 保留旧条目里我们模板没覆盖的字段（例如宿主新加的 model_type、用户手调的 temperature 等）
        previous = existing_managed.get(name)
        if previous is not None:
            for key, value in previous.items():
                if key in table:
                    continue
                try:
                    table[key] = value.unwrap() if hasattr(value, "unwrap") else value
                except Exception:  # pragma: no cover - 单个字段复制失败不影响整体
                    continue
        managed.append(table)
        result["added"] += 1

    new_models = tomlkit.aot()
    used_names: set[str] = set()
    dropped = 0
    for item in list(kept) + list(managed):
        name = str(item.get("name") or "")
        if name and name in used_names:
            dropped += 1
            continue
        if name:
            used_names.add(name)
        new_models.append(item)
    if dropped:
        result["dropped_duplicates"] = dropped
    doc["models"] = new_models

    rendered = tomlkit.dumps(doc)
    if rendered == text:
        result["reason"] = "无变化"
        return result

    # 写回前先自校验：能被重新解析、条目数一致、且没有重名，才落盘。
    try:
        check = tomlkit.parse(rendered)
        check_models = check.get("models")
        if not isinstance(check_models, list) or len(check_models) != len(used_names):
            raise ValueError("模型条目数量不一致")
        check_names = [str(item.get("name") or "") for item in check_models]
        if len(check_names) != len(set(check_names)):
            raise ValueError("模型名称存在重复")
    except Exception as exc:
        result["reason"] = f"生成的 TOML 校验失败，已放弃写入：{exc}"
        logger.error("模型配置同步被中止：%s", exc)
        return result

    backup_dir = path.parent / "old"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (backup_dir / f"{path.stem}_codex_{stamp}.toml").write_text(text, encoding="utf-8")
        # 只保留最近 10 份本插件写的备份，避免长期运行攒一堆文件
        backups = sorted(backup_dir.glob(f"{path.stem}_codex_*.toml"))
        for stale in backups[:-10]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:  # pragma: no cover - 备份失败不阻塞主流程
        logger.warning("备份 model_config.toml 失败", exc_info=True)

    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    try:
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        # 中途失败不要留下 .tmp-* 垃圾文件
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    result["changed"] = True
    return result


# --------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------


class _HttpStatusError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class CodexProvider:
    def __init__(self, plugin: "CodexSubscriptionPlugin") -> None:
        self._plugin = plugin

    @property
    def _config(self) -> CodexPluginConfig:
        return self._plugin.cfg

    async def respond(self, request: dict[str, Any]) -> dict[str, Any]:
        config = self._config
        model_info = request.get("model_info") if isinstance(request.get("model_info"), dict) else {}
        model = str(model_info.get("model_identifier") or config.model.default_model or "").strip()

        effort = normalize_effort(config.model.reasoning_effort)
        summary = normalize_summary(config.model.reasoning_summary)
        extra_params = model_info.get("extra_params")
        if isinstance(extra_params, dict):
            effort = normalize_effort(extra_params.get("reasoning_effort") or effort)
            summary = normalize_summary(extra_params.get("reasoning_summary") or summary)

        client = await self._plugin.http_client()
        account = await self._plugin.ensure_account(client)

        body = build_responses_body(
            request,
            model=model,
            effort=effort,
            summary=summary,
            max_output_tokens=0 if not account.api_key else config.model.max_output_tokens,
            include_temperature=bool(account.api_key),
        )
        if not account.api_key:
            drop_unsupported(body)

        result = await self._send(body, account, client)
        schema_version = request.get("item_schema_version")
        if schema_version is not None:
            return build_output_items(result, schema_version)
        return build_legacy_response(result)

    def _headers(self, account: Account) -> dict[str, str]:
        config = self._config
        headers = {
            "Authorization": f"Bearer {account.api_key or account.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": f"maibot-codex-subscription/{__version__}",
        }
        if not account.api_key:
            headers["OpenAI-Beta"] = "responses=experimental"
            headers["originator"] = config.model.originator or "codex_cli_rs"
            headers["version"] = config.model.client_version.strip() or DEFAULT_CLIENT_VERSION
            if account.account_id:
                headers["chatgpt-account-id"] = account.account_id
            headers["session_id"] = uuid.uuid4().hex
        return headers

    def _url(self, account: Account) -> str:
        config = self._config
        if account.api_key:
            return f"{config.network.api_base_url.rstrip('/')}/responses"
        return f"{config.network.base_url.rstrip('/')}/codex/responses"

    async def _send(self, body: dict[str, Any], account: Account, client: Any) -> StreamResult:
        refreshed = False
        stripped = False
        for _attempt in range(3):
            try:
                return await self._stream_once(client, self._url(account), self._headers(account), body)
            except _HttpStatusError as exc:
                if exc.status == 401 and not account.api_key and not refreshed:
                    refreshed = True
                    account = await self._plugin.refresh_and_store(client, account)
                    continue
                if exc.status == 400 and not account.api_key and not stripped:
                    lowered = exc.body.lower()
                    if "unsupported" in lowered or "not supported" in lowered:
                        stripped = True
                        drop_unsupported(body)
                        continue
                raise self._explain(exc) from exc
        raise CodexError("请求 ChatGPT 后端失败：重试次数已用尽。")

    async def _stream_once(self, client: Any, url: str, headers: dict[str, str], body: dict[str, Any]) -> StreamResult:
        try:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code != 200:
                    raw = await response.aread()
                    raise _HttpStatusError(response.status_code, raw.decode("utf-8", "replace")[:800])
                # 后端可能返回 SSE，也可能返回整段 JSON；这里按内容判断，不依赖 content-type，
                # 因为不同网关会把 text/event-stream 改写掉。
                raw = await response.aread()
        except (CodexError, _HttpStatusError):
            raise
        except Exception as exc:
            raise CodexError(
                f"无法连接 ChatGPT 后端：{exc}\n"
                "如果服务器需要代理，请在插件配置 network.proxy 里填写（例如 http://127.0.0.1:7890）。"
            ) from exc

        text = raw.decode("utf-8", "replace")
        if looks_like_sse(text):
            return await collect_sse(iterate_lines(text))
        return parse_json_response(raw)

    @staticmethod
    def _explain(exc: _HttpStatusError) -> CodexError:
        detail = exc.body
        try:
            parsed = json.loads(exc.body)
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict) and error.get("message"):
                    detail = str(error["message"])
                elif parsed.get("detail"):
                    detail = str(parsed["detail"])
                elif parsed.get("message"):
                    detail = str(parsed["message"])
        except json.JSONDecodeError:
            pass

        if exc.status == 401:
            hint = "登录已失效：请重新 `/codex login`。"
        elif exc.status == 403:
            hint = "账号没有 Codex 权限，或该模型不在订阅范围内。"
        elif exc.status == 429:
            hint = "订阅额度或频率超限，请稍后重试（与 Codex 共用同一份配额）。"
        elif exc.status == 400 and "not supported" in detail.lower():
            hint = "模型不被订阅后端支持，可用 `/codex models` 查看可用模型。"
        elif exc.status >= 500:
            hint = "ChatGPT 后端暂时不可用，请稍后重试。"
        else:
            hint = ""
        return CodexError(f"ChatGPT 后端返回 HTTP {exc.status}：{detail[:400]}" + (f"\n{hint}" if hint else ""))


# --------------------------------------------------------------------------------------
# 插件主体
# --------------------------------------------------------------------------------------


class CodexSubscriptionPlugin(MaiBotPlugin):
    """账号库 + 登录 + 模型目录 + LLM Provider。"""

    config_model = CodexPluginConfig
    config_reload_subscriptions = ("model",)

    def __init__(self) -> None:
        super().__init__()
        self._fallback_cfg = CodexPluginConfig()
        self._client: Any | None = None
        self._provider = CodexProvider(self)
        self._login_task: asyncio.Task[None] | None = None
        self._vault = AccountVault(self._accounts_path)
        self._catalog_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._catalog_snapshot: list[dict[str, str]] = []

    # -- 路径 ----------------------------------------------------------------

    @property
    def cfg(self) -> CodexPluginConfig:
        try:
            return self.config  # type: ignore[return-value]
        except (RuntimeError, AttributeError):
            return self._fallback_cfg

    def _accounts_path(self) -> Path:
        try:
            base = Path(self.ctx.paths.data_dir)
        except Exception:
            base = Path.home() / ".maibot" / "codex-subscription"
        default_path = base / "accounts.json"

        configured = (self.cfg.account.accounts_file or "").strip()
        if not configured:
            return default_path

        candidate = Path(os.path.expandvars(configured)).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
            base_resolved = base.resolve()
        except OSError:
            return default_path
        # 账号库里是 refresh_token，不允许落到插件数据目录之外。
        if resolved != base_resolved and base_resolved not in resolved.parents:
            logger.warning("accounts_file 指向数据目录之外，已回退到默认路径：%s", resolved)
            return default_path
        return resolved

    @property
    def vault(self) -> AccountVault:
        return self._vault

    def _model_config_path(self) -> Path | None:
        """由插件数据目录反推实例根目录下的 config/model_config.toml。

        只有同目录下同时存在 bot_config.toml 才认为是 MaiBot 实例的 config 目录，
        避免 data_dir 被自定义后误改到别处的同名文件。
        """
        try:
            data_dir = Path(self.ctx.paths.data_dir)
        except Exception:
            return None
        config_dir = data_dir.parents[2] / "config"
        if not (config_dir / "bot_config.toml").is_file():
            return None
        candidate = config_dir / "model_config.toml"
        return candidate if candidate.is_file() else None

    # -- 模型目录与同步 ------------------------------------------------------

    async def refresh_catalog(self, *, force: bool = False) -> list[dict[str, str]]:
        client = await self.http_client()
        account = await self.ensure_account(client)
        entries = await self._catalog(client, account, force=force)
        self._catalog_snapshot = normalize_catalog(entries)
        _LAST_CATALOG[:] = self._catalog_snapshot
        return self._catalog_snapshot

    def sync_models(self, *, catalog: list[dict[str, str]] | None = None) -> dict[str, Any]:
        config = self.cfg.model
        if not config.sync_model_config:
            return {"changed": False, "reason": "未开启同步"}
        snapshot = catalog if catalog is not None else self._catalog_snapshot
        if not snapshot:
            return {"changed": False, "reason": "模型目录为空，暂不同步"}
        path = self._model_config_path()
        if path is None:
            return {"changed": False, "reason": "找不到 model_config.toml"}
        slugs = selected_slugs(snapshot, list(config.enabled_models))
        stats = sync_model_config(
            path,
            provider_name=config.provider_name or "Codex订阅",
            prefix=config.model_name_prefix or "codex-",
            slugs=slugs,
            base_url=self.cfg.network.base_url,
            timeout=self.cfg.network.timeout,
        )
        logger.info("模型配置同步：%s", stats)
        return stats

    @classmethod
    def build_config_schema(cls, **kwargs: Any) -> dict[str, Any]:
        """注入模型目录：默认模型是下拉框，可选模型是多选。

        只覆盖 SDK 文档承诺的类方法；实例侧的 ``get_webui_config_schema`` 由基类
        转发到这里，不再单独覆盖。
        """
        schema = super().build_config_schema(**kwargs)
        return inject_catalog_choices(schema, [item["slug"] for item in _LAST_CATALOG])

    # -- 网络与账号 ----------------------------------------------------------

    async def http_client(self) -> Any:
        if httpx is None:
            raise CodexError("缺少 httpx 依赖：请执行 `pip install \"httpx>=0.24\"` 后重载插件。")
        if self._client is not None and not self._client.is_closed:
            return self._client
        config = self.cfg
        common: dict[str, Any] = {
            "timeout": httpx.Timeout(config.network.timeout, connect=config.network.connect_timeout),
            "follow_redirects": True,
        }
        proxy = (config.network.proxy or "").strip()
        if proxy:
            if "://" not in proxy:
                proxy = f"http://{proxy}"
            try:
                self._client = httpx.AsyncClient(proxy=proxy, **common)
            except TypeError:  # httpx < 0.26
                self._client = httpx.AsyncClient(proxies=proxy, **common)  # type: ignore[arg-type]
        else:
            self._client = httpx.AsyncClient(trust_env=True, **common)
        return self._client

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass

    def _migrate_legacy_data(self) -> None:
        """从旧插件 ID 的数据目录迁移账号库（仅默认路径、且当前库不存在时）。"""
        if (self.cfg.account.accounts_file or "").strip():
            return
        try:
            data_dir = Path(self.ctx.paths.data_dir)
        except Exception:
            return
        target = data_dir / "accounts.json"
        if target.exists():
            return
        for legacy_id in LEGACY_PLUGIN_IDS:
            legacy = data_dir.parent / legacy_id / "accounts.json"
            if not legacy.is_file():
                continue
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, target)
                # copy2 会继承源文件权限，旧目录可能是 0644，这里强制收回 0600
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
                logger.info("已从旧插件 ID 迁移账号库：%s -> %s", legacy, target)
            except OSError:
                logger.warning("迁移旧账号库失败：%s", legacy, exc_info=True)
            return

    def _maybe_import_codex_auth(self) -> None:
        if not self.cfg.account.import_codex_auth:
            return
        if self._vault.accounts():
            return
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        base = Path(os.path.expandvars(codex_home)).expanduser() if codex_home else Path.home() / ".codex"
        imported = import_codex_auth(base / "auth.json")
        if imported is not None:
            self._vault.upsert(imported)
            logger.info("已从 %s 导入 ChatGPT 账号 %s", base / "auth.json", imported.label)

    async def ensure_account(self, client: Any) -> Account:
        """取出当前账号，必要时刷新。"""
        account = self._vault.resolve(self.cfg.account.active_account)
        if account.expired and self.cfg.account.auto_refresh and account.refresh_token:
            account = await self.refresh_and_store(client, account)
        return account

    async def refresh_and_store(self, client: Any, account: Account) -> Account:
        refreshed = await refresh_account(client, self.cfg.account.issuer, account)
        try:
            self._vault.upsert(refreshed, make_active=False)
        except OSError:
            logger.warning("账号库写回失败，本次请求继续使用内存中的新令牌", exc_info=True)
        return refreshed

    # -- 生命周期 ------------------------------------------------------------

    async def on_load(self) -> None:
        self._vault = AccountVault(self._accounts_path)
        self._migrate_legacy_data()
        self._maybe_import_codex_auth()
        try:
            accounts = self._vault.accounts()
        except CodexError as exc:
            self.ctx.logger.warning("%s", exc)
            accounts = []
        if accounts:
            labels = "、".join(account.label for account in accounts[:5])
            self.ctx.logger.info("ChatGPT 订阅插件已加载，账号库 %s：%s", self._vault.path, labels)
        else:
            self.ctx.logger.warning("账号库为空（%s）；发送 /codex login 登录", self._vault.path)

        # 启动时拉一次模型目录并同步到 model_config.toml；失败不影响插件加载
        try:
            await asyncio.wait_for(self.refresh_catalog(force=True), timeout=15)
            self.sync_models()
        except asyncio.TimeoutError:
            self.ctx.logger.warning("启动时拉取模型目录超时；稍后发送 /codex models 可重试")
        except Exception as exc:
            self.ctx.logger.warning("启动时同步模型配置失败：%s", exc)

    async def on_unload(self) -> None:
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
        await self._close_client()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self._close_client()
            self._vault = AccountVault(self._accounts_path)
            try:
                self.sync_models()
            except Exception:
                logger.warning("配置更新后同步模型失败", exc_info=True)

    # -- LLM Provider --------------------------------------------------------

    @LLMProvider(
        CLIENT_TYPE,
        name="ChatGPT 订阅 (Codex)",
        description="用 ChatGPT 账号登录复用 Codex 订阅额度（Responses API）",
        version=__version__,
    )
    async def handle_llm_provider(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        if operation != "response":
            raise ValueError(f"codex_subscription 只支持 response 操作，收到: {operation}")
        return await self._provider.respond(request)

    # -- 命令 ----------------------------------------------------------------

    @Command(
        "codex",
        description="ChatGPT 订阅只读命令：models / 帮助",
        pattern=r"^/codex(?:\s+(?P<action>models))?\s*$",
        permission="public",
    )
    async def handle_codex(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        sub, _arg = self._parse_command(kwargs)
        if sub == "models":
            return await self._reply(stream_id, await self._cmd_models())
        return await self._reply(stream_id, (True, self._help_text(), 2))

    @Command(
        "codex_admin",
        description="ChatGPT 订阅管理：login / accounts / use / status / sync / import / logout / test（仅管理员）",
        pattern=(
            r"^/codex\s+(?P<action>login|auth|accounts|list|use|status|info|sync|apply|import|logout|remove|test)"
            r"(?:\s+(?P<arg>.+))?\s*$"
        ),
        permission="operator",
    )
    async def handle_codex_admin(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        sub, arg = self._parse_command(kwargs)

        if sub in ("login", "auth"):
            result = await self._cmd_login(stream_id)
        elif sub in ("accounts", "list"):
            result = (True, self._accounts_text(), 2)
        elif sub == "use":
            result = (True, self._use_account(arg), 2)
        elif sub in ("status", "info"):
            result = (True, self._status_text(), 2)
        elif sub in ("sync", "apply"):
            result = await self._cmd_sync()
        elif sub == "import":
            result = await self._cmd_import()
        elif sub == "test":
            result = await self._cmd_test()
        elif sub in ("logout", "remove"):
            result = (True, self._logout(arg), 2)
        else:
            result = (True, self._help_text(), 2)
        return await self._reply(stream_id, result)

    async def _reply(self, stream_id: str, result: tuple[bool, str, int]) -> tuple[bool, str, int]:
        """命令回复要自己发：Host 只接收返回值，不会替插件发送文本。"""
        ok, text, weight = result
        if stream_id and text:
            try:
                await self.ctx.send.text(text, stream_id)
            except Exception:  # pragma: no cover - 发送失败不影响命令结果
                logger.exception("发送命令回复失败")
        return ok, text, weight

    @staticmethod
    def _parse_command(kwargs: dict[str, Any]) -> tuple[str, str]:
        """按 Host 承诺的 matched_groups 解析；旧 Host 只有原文时退化为文本解析。"""
        matched = kwargs.get("matched_groups")
        if isinstance(matched, dict) and matched:
            action = str(matched.get("action") or "").strip().lower()
            arg = str(matched.get("arg") or "").strip()
            if action:
                return action, arg
        elif isinstance(matched, (list, tuple)) and matched:
            action = str(matched[0] or "").strip().lower()
            arg = str(matched[1] or "").strip() if len(matched) > 1 else ""
            if action:
                return action, arg

        text = str(kwargs.get("raw_message") or kwargs.get("plain_text") or kwargs.get("text") or "").strip()
        parts = text.split(maxsplit=2)
        return (parts[1].lower() if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")

    def _help_text(self) -> str:
        return (
            "ChatGPT 订阅（Codex）插件\n"
            "只读（所有人）：\n"
            "/codex models         — 查看账号可用模型\n"
            "管理（需要操作员权限）：\n"
            "/codex test           — 用默认模型跑一次，消耗订阅额度\n"
            "/codex login          — 设备码登录，添加一个 ChatGPT 账号\n"
            "/codex accounts       — 列出账号库（邮箱/ID 已脱敏）\n"
            "/codex use <序号|邮箱> — 切换当前账号\n"
            "/codex status         — 当前账号与令牌状态（已脱敏）\n"
            "/codex import         — 从服务器上的 ~/.codex/auth.json 导入一次\n"
            "/codex sync           — 同步模型目录到 model_config.toml\n"
            "/codex logout <序号>  — 从账号库移除账号"
        )

    def _accounts_text(self) -> str:
        try:
            accounts = self._vault.accounts()
            active = self._vault.active_id()
        except CodexError as exc:
            return str(exc)
        if not accounts:
            return "账号库为空。发送 `/codex login` 添加账号。"

        lines = ["账号库（已脱敏）", ""]
        for index, account in enumerate(accounts, start=1):
            mark = "*" if account.account_id == active else " "
            state = "已过期" if account.expired else "有效"
            lines.append(
                f"{mark} {index}. {mask_email(account.email) or mask_id(account.account_id)}"
                f"{f'（{account.plan}）' if account.plan else ''} [{state}]"
                f"  {mask_id(account.account_id)}"
            )
        return "\n".join(lines)

    def _use_account(self, arg: str) -> str:
        if not arg:
            return "用法：`/codex use <序号|邮箱>`"
        try:
            accounts = self._vault.accounts()
        except CodexError as exc:
            return str(exc)
        target = arg.strip()
        chosen: Account | None = None
        if target.isdigit() and 1 <= int(target) <= len(accounts):
            chosen = accounts[int(target) - 1]
        else:
            for account in accounts:
                if target.lower() in (account.email.lower(), account.account_id.lower()):
                    chosen = account
                    break
        if chosen is None:
            return f"没找到账号：{target}"
        self._vault.save(accounts, chosen.account_id)
        return f"已切换到 {mask_email(chosen.email) or mask_id(chosen.account_id)}" + (
            f"（{chosen.plan}）" if chosen.plan else ""
        )

    def _logout(self, arg: str) -> str:
        try:
            accounts = self._vault.accounts()
        except CodexError as exc:
            return str(exc)
        if not arg:
            return "用法：`/codex logout <序号|邮箱>`"
        target = arg.strip()
        account: Account | None = None
        if target.isdigit() and 1 <= int(target) <= len(accounts):
            account = accounts[int(target) - 1]
        else:
            for candidate in accounts:
                if target.lower() in (candidate.email.lower(), candidate.account_id.lower()):
                    account = candidate
                    break
        if account is None:
            return f"没找到账号：{target}"
        self._vault.remove(account.account_id)
        return f"已移除 {mask_email(account.email) or mask_id(account.account_id)}"

    def _status_text(self) -> str:
        try:
            account = self._vault.resolve(self.cfg.account.active_account)
        except CodexError as exc:
            return str(exc)

        lines = [
            f"账号：{mask_email(account.email) or mask_id(account.account_id)}",
            f"账号 ID：{mask_id(account.account_id)}",
        ]
        if account.plan:
            lines.append(f"订阅：{account.plan}")
        if account.expires_at:
            remaining = int(account.expires_at - time.time())
            when = datetime.fromtimestamp(account.expires_at).strftime("%Y-%m-%d %H:%M:%S")
            state = "已过期（下次请求会刷新）" if remaining <= 0 else f"约 {remaining // 60} 分钟后过期"
            lines.append(f"access_token：{when}（{state}）")
        lines.append(f"refresh_token：{'有' if account.refresh_token else '无'}")
        lines.append(f"来源：{account.source}")
        return "\n".join(lines)

    async def _cmd_login(self, stream_id: str) -> tuple[bool, str, int]:
        if self._login_task is not None and not self._login_task.done():
            return True, "已经有一个登录流程在进行中，请先在浏览器完成它。", 2

        client = await self.http_client()
        try:
            device = await request_device_code(client, self.cfg.account.issuer)
        except CodexError as exc:
            return True, str(exc), 2

        intro = (
            "请打开下面的链接并用 ChatGPT 账号登录：\n"
            f"{device.verification_url}\n\n"
            f"一次性代码：{device.user_code}\n"
            "（15 分钟内有效；只在你自己发起登录时输入这个代码。）"
        )
        self._login_task = asyncio.create_task(self._finish_login(device, stream_id))
        return True, intro, 2

    async def _finish_login(self, device: DeviceCode, stream_id: str) -> None:
        issuer = self.cfg.account.issuer
        try:
            client = await self.http_client()
            token_payload = await poll_device_token(
                client, issuer, device, deadline=time.time() + DEVICE_LOGIN_TIMEOUT
            )
            tokens = await exchange_device_code(client, issuer, token_payload)
            account = build_account_from_tokens(tokens)
            self._vault.upsert(account)
            try:
                await self.refresh_catalog(force=True)
                self.sync_models()
            except Exception:
                logger.warning("登录后同步模型目录失败", exc_info=True)
            message = f"登录成功：{mask_email(account.email) or mask_id(account.account_id)}"
            if account.plan:
                message += f"（{account.plan}）"
            message += "\n现在可以在模型配置里使用 client_type = codex_subscription 了。"
        except asyncio.CancelledError:
            raise
        except CodexError as exc:
            message = f"登录失败：{exc}"
        except Exception as exc:  # pragma: no cover
            logger.exception("设备码登录未预期失败")
            message = f"登录失败（未预期错误）：{exc}"
        finally:
            self._login_task = None

        if stream_id:
            try:
                await self.ctx.send.text(message, stream_id)
            except Exception:  # pragma: no cover
                logger.exception("发送登录结果失败")

    async def _catalog(self, client: Any, account: Account, *, force: bool = False) -> list[dict[str, Any]]:
        ttl = max(0, int(self.cfg.model.catalog_ttl))
        cached_at, cached = self._catalog_cache
        if cached and not force and time.time() - cached_at < ttl:
            return cached

        config = self.cfg
        if account.api_key:
            url = f"{config.network.api_base_url.rstrip('/')}/models"
            headers = {"Authorization": f"Bearer {account.api_key}"}
        else:
            version = config.model.client_version.strip() or DEFAULT_CLIENT_VERSION
            url = f"{config.network.base_url.rstrip('/')}/codex/models?client_version={version}"
            headers = {
                "Authorization": f"Bearer {account.access_token}",
                "OpenAI-Beta": "responses=experimental",
                "originator": config.model.originator or "codex_cli_rs",
                "version": version,
            }
            if account.account_id:
                headers["chatgpt-account-id"] = account.account_id

        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise CodexError(f"获取模型目录失败（HTTP {resp.status_code}）：{resp.text[:300]}")
        payload = resp.json()
        entries = payload.get("models") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            entries = payload.get("data") if isinstance(payload, dict) else []
        models = [entry for entry in (entries or []) if isinstance(entry, dict)]
        self._catalog_cache = (time.time(), models)
        return models

    async def _cmd_models(self) -> tuple[bool, str, int]:
        """公开命令：只读内存里的目录快照，不联网、不刷新令牌、不写盘。"""
        snapshot = list(self._catalog_snapshot)
        if not snapshot:
            return (
                True,
                "模型目录尚未缓存（插件启动或管理员执行 `/codex sync` 时会拉取）。",
                2,
            )

        lines = [f"{item['slug']}  ({item['name']})" if item["name"] != item["slug"] else item["slug"] for item in snapshot[:40]]
        return True, "可用模型：\n" + "\n".join(lines), 2

    async def _cmd_sync(self) -> tuple[bool, str, int]:
        try:
            catalog = await self.refresh_catalog(force=True)
        except CodexError as exc:
            return True, str(exc), 2
        stats = self.sync_models(catalog=catalog)
        return True, self._describe_sync(stats), 2

    async def _cmd_import(self) -> tuple[bool, str, int]:
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        base = Path(os.path.expandvars(codex_home)).expanduser() if codex_home else Path.home() / ".codex"
        source = base / "auth.json"
        account = import_codex_auth(source)
        if account is None:
            return True, f"没有从 {source} 读到可用的 ChatGPT 令牌（文件不存在或格式不对）。", 2
        self._vault.upsert(account)
        return True, f"已导入账号 {mask_email(account.email) or mask_id(account.account_id)}", 2

    def _describe_sync(self, stats: dict[str, Any]) -> str:
        if not stats.get("changed") and stats.get("reason"):
            return f"模型配置未改动：{stats['reason']}"
        skipped = stats.get("skipped")
        text = f"模型配置已同步：新增 {stats.get('added', 0)}，移除 {stats.get('removed', 0)}"
        if stats.get("kept"):
            text += f"，保留（被任务引用）{stats['kept']}"
        if skipped:
            text += f"，跳过重名 {len(skipped)}"
        text += "\n✅ 麦麦会自动热重载 model_config.toml，模型选择里刷新即可看到。"
        return text

    async def _cmd_test(self) -> tuple[bool, str, int]:
        model = self.cfg.model.default_model.strip()
        if not model:
            return True, "请先在插件配置 model.default_model 里填一个模型名（可用 /codex models 查看）。", 2
        request = {
            "item_schema_version": 1,
            "context_items": [
                {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "回复两个字：你好"}]}
            ],
            "model_info": {"model_identifier": model},
        }
        try:
            result = await self._provider.respond(request)
        except CodexError as exc:
            return True, f"测试失败：{exc}", 2
        return True, f"测试成功（{model}）：{extract_text(result)[:200] or '（没有文本输出）'}", 2


def create_plugin() -> CodexSubscriptionPlugin:
    return CodexSubscriptionPlugin()
