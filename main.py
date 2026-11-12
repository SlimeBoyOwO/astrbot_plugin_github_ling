"""GitHub 读写插件 - 灵（AstrBot 入口）。

这个文件只做三件事：

1. 组装各模块（配置 / 状态 / 轮询 / 发送 / Issue 助手）；
2. 注册命令并把参数转交给对应模块；
3. 处理生命周期（启动轮询或 Webhook、卸载时清理）。

具体逻辑都在子包里：

* ``core/``   配置、状态、推送目标、文本与时间工具
* ``github/`` REST 客户端、Webhook、轮询
* ``render/`` 卡片渲染
* ``issue/``  引用/合并转发解析、Issue 模板、大模型生成、截图上传
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlparse

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import (
    AstrMessageEvent,
    MessageChain,
    MessageEventResult,
    filter,
)
from astrbot.api.star import Context, Star, register

from .core import targets
from .core.config import PluginConfig
from .core.paths import AVATAR_DIR
from .core.state import StateStore
from .core.timeutil import now_iso, parse_time
from .github import events as webhook_events
from .github.client import GitHubAPI
from .github.poller import Poller, server_now_iso
from .github.webhook import GitHubWebhookServer
from .issue.agent import IssueAgent
from .issue.assets import AssetUploader
from .issue.context import (
    ChatContextCollector,
    collect_comment_images,
    extract_quoted_message_text,
)
from .issue.draft import DraftStore, format_preview, preview_sections
from .issue.templates import TemplateCatalog
from .render import card as card_render
from .render import markdown as cards

try:  # AstrBot >= 4.2x 才有 GreedyStr
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover
    GreedyStr = str  # type: ignore[assignment,misc]

try:  # 全局管理员列表
    from astrbot.core.config import astrbot_config
except Exception:  # pragma: no cover
    astrbot_config = None  # type: ignore[assignment]

PLUGIN_VERSION = "1.6.7"

AVATAR_TTL_SECONDS = 7 * 24 * 3600
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")

# 群主 / 群管理员在 OneBot 事件里的 role 取值
GROUP_ADMIN_ROLES = {"owner", "admin"}
# 查询群成员身份的结果缓存时间（秒）
ADMIN_CACHE_TTL = 300

REPO_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+$")
REPO_IN_TEXT_PATTERN = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)")

# /ghcomment 的目标写法：完整链接、owner/repo#12、#12 或 12
COMMENT_URL_PATTERN = re.compile(
    r"github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)", re.IGNORECASE
)
COMMENT_SHORT_PATTERN = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")
COMMENT_NUMBER_PATTERN = re.compile(r"^#?(\d+)$")

COMMENT_USAGE = (
    "用法：/ghcomment 编号 [评论内容]\n"
    "也可以写完整链接或 owner/repo#编号：\n"
    "  /ghcomment https://github.com/owner/repo/issues/12 已修复\n"
    "  /ghcomment owner/repo#12 已修复\n"
    "只写编号时，会使用本会话（或你绑定的）默认仓库。\n"
    "引用一条消息再发送，可以把引用内容作为引用块附在评论末尾。"
)

# 文转图服务默认会先请求网络端点，网络不佳时可能长时间阻塞；
# 超过该时间即放弃卡片、改用纯文本，避免阻塞后续通知。
CARD_RENDER_TIMEOUT = 20

CONFIRM_WORDS = {
    "ok",
    "confirm",
    "yes",
    "y",
    "发",
    "确认",
    "创建",
    "提交",
    "submit",
    "create",
}
CANCEL_WORDS = {"cancel", "no", "n", "取消", "放弃", "abort", "discard"}
TEMPLATE_WORDS = {"tpl", "template", "模板", "换模板"}
LIST_WORDS = {"list", "ls", "模板列表", "templates"}


def _looks_like_image(data: bytes) -> bool:
    """粗略判断字节串是不是常见的图片格式。"""
    if not data:
        return False
    signatures = (
        bytes((0x89, 0x50, 0x4E, 0x47)),  # PNG
        bytes((0xFF, 0xD8, 0xFF)),  # JPEG
        b"GIF8",  # GIF
        b"RIFF",  # WebP
    )
    return any(data.startswith(signature) for signature in signatures)


@register(
    "astrbot_plugin_github_ling",
    "SlimeBoyOwO",
    "绑定 GitHub 仓库，用卡片推送 Commit、PR 与 Issue 更新；支持大模型自动发 Issue",
    PLUGIN_VERSION,
)
class GitHubLingPlugin(Star):
    """GitHub 读写插件 - 灵

    绑定仓库后，插件会推送以下更新：

    - Commit：卡片展示短哈希与提交信息（如 feat: xxx）
    - PR / Issue：大标题 + 评论（或描述）的前 10 行

    还可以引用一条消息（日志 / 合并转发的聊天记录 / 截图），
    让大模型按仓库的 Issue 模板整理成 Issue 草稿，确认后发送到 GitHub。

    命令：
    /ghbind 用户名/仓库名    绑定仓库到当前会话（也可直接粘贴仓库链接）
    /ghbind 用户名/仓库名 force  跳过 API 校验直接绑定（服务器连不上 api.github.com 时使用）
    /ghunbind [仓库]         取消绑定，省略参数则取消当前会话全部绑定
    /ghbindlist              查看当前会话绑定的仓库
    /ghswitch [仓库]         切换当前会话默认使用的仓库（/ghissue 用它）
    /ghpush on|off|list      把当前会话（例如某个群）加入/移出额外推送目标
    /ghissue [说明|ok|cancel|tpl N|list]  根据引用的聊天记录自动发 Issue
    /ghcomment 编号 [内容]   在已有 Issue / PR 下发表评论（可附带引用内容）
    /ghcheck                 立即检查一次更新（轮询模式）
    /ghrewind [分钟]         把水位线回退若干分钟，用于找回漏掉的事件
    /ghstatus                查看运行状态与 API 连通性
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.raw_config = config or {}
        self.cfg = PluginConfig.load(self.raw_config)

        store = StateStore()
        state = store.load()
        self._store = store
        self.bindings: dict[str, list[str]] = {
            str(repo): [str(item) for item in subscribers]
            for repo, subscribers in (state.get("bindings") or {}).items()
            if isinstance(subscribers, list)
        }
        self.watermarks: dict[str, str] = {
            str(repo): str(value)
            for repo, value in (state.get("watermarks") or {}).items()
        }
        # 额外推送目标（群聊 / 私聊），与具体仓库无关，所有已绑定仓库的更新都会推送过去
        self.broadcasts: list[str] = [
            str(item).strip()
            for item in (state.get("broadcasts") or [])
            if str(item).strip()
        ]
        # 每个会话当前默认使用的仓库（/ghissue 优先用它）
        self.active: dict[str, str] = {
            str(session): str(repo)
            for session, repo in (state.get("active") or {}).items()
        }
        # 仓库 → {会话: 绑定者用户 ID}，用于「私聊绑定、群里发 Issue」的回退
        self.binding_users: dict[str, dict[str, str]] = {}
        for repo, mapping in (state.get("binding_users") or {}).items():
            if isinstance(mapping, dict):
                self.binding_users[str(repo)] = {
                    str(session): str(user) for session, user in mapping.items()
                }
        self._prune_state()

        # 配置文件热加载（手工改 JSON 后无需重载插件）
        self._config_cache: dict[str, Any] | None = None
        self._config_stamp: tuple[int, int] | None = None
        # 群管理员身份查询缓存：(群号, 用户) -> (时间戳, 是否管理员)
        self._admin_cache: dict[tuple[str, str], tuple[float, bool]] = {}

        self.api = GitHubAPI(
            self.cfg.github_token,
            proxy=self.cfg.github_proxy,
            ca_bundle=self.cfg.github_ca_bundle,
            ssl_verify=self.cfg.github_ssl_verify,
        )

        self.poller = Poller(
            api=self.api,
            config=self.cfg,
            bindings=self.bindings,
            watermarks=self.watermarks,
            deliver=self._deliver,
            save_state=self._save_state,
            server_now_iso=self._server_now_iso,
        )

        self.webhook_server: GitHubWebhookServer | None = None
        if self.cfg.enable_webhook:
            self.webhook_server = GitHubWebhookServer(
                plugin=self,
                host=self.cfg.webhook_host,
                port=self.cfg.webhook_port,
                path=self.cfg.webhook_path,
                secret=self.cfg.webhook_secret,
            )

        # 自动发 Issue
        self.templates = TemplateCatalog(self.api)
        self.assets = AssetUploader(self.api, self.cfg)
        self.drafts = DraftStore()
        self.issue_agent = IssueAgent(
            context=self.context,
            api=self.api,
            config=self.cfg,
            catalog=self.templates,
            uploader=self.assets,
        )

    # ------------------------------------------------------------------ 配置

    def _sync_config(self) -> None:
        """每次命令 / 推送前同步配置，避免改了配置却未重载插件。

        面板里改配置时 AstrBot 会就地更新 ``AstrBotConfig``；如果是手工编辑
        ``data/config/astrbot_plugin_github_ling_config.json``，这里会把磁盘上的
        新值合并进来，省得再重启一次机器人。
        """
        source: Any = self.raw_config
        disk = self._read_config_file()
        if disk:
            merged = dict(self.raw_config)
            merged.update(disk)
            source = merged
        self.cfg.refresh(source)
        self.api.token = self.cfg.github_token
        self.api.proxy = self.cfg.github_proxy
        self.api.ca_bundle = self.cfg.github_ca_bundle
        self.api.ssl_verify = self.cfg.github_ssl_verify

    @property
    def config_path(self) -> str:
        return str(getattr(self.raw_config, "config_path", "") or "")

    def _read_config_file(self) -> dict[str, Any] | None:
        """读取插件配置文件（按 mtime/size 缓存，避免每次命令都读盘）。"""
        path = self.config_path
        if not path:
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._config_stamp:
            return self._config_cache
        try:
            with open(path, encoding="utf-8-sig") as file:
                data = json.load(file)
        except Exception as exc:
            logger.warning("读取插件配置 %s 失败: %s", path, exc)
            return self._config_cache
        self._config_stamp = stamp
        self._config_cache = data if isinstance(data, dict) else None
        return self._config_cache

    def _server_now_iso(self, safety_seconds: int = 0) -> str:
        return server_now_iso(self.api, safety_seconds)

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        self._sync_config()
        if self.cfg.enable_webhook:
            assert self.webhook_server is not None
            try:
                await self.webhook_server.start()
            except Exception as exc:
                logger.error("启动 GitHub Ling Webhook 服务失败: %s", exc)
        else:
            self.poller.start()
            logger.info(
                "GitHub Ling 启动轮询模式：间隔 %s 分钟，已绑定 %s 个仓库",
                self.cfg.check_interval,
                len(self.bindings),
            )

    async def terminate(self) -> None:
        await self.poller.stop()
        if self.webhook_server:
            await self.webhook_server.stop()
        await self.api.close()
        self.drafts.clear()
        self._save_state()
        logger.info("GitHub Ling 插件已卸载")

    # ------------------------------------------------------------------ 状态

    def _save_state(self) -> None:
        self._prune_state()
        self._store.save(
            bindings=self.bindings,
            watermarks=self.watermarks,
            broadcasts=self.broadcasts,
            active=self.active,
            binding_users=self.binding_users,
        )

    def _prune_state(self) -> None:
        """清掉已经失效的默认仓库 / 绑定者记录（例如解绑之后的残留）。"""
        for repo in list(self.binding_users):
            subscribers = self.bindings.get(repo)
            if not subscribers:
                self.binding_users.pop(repo, None)
                continue
            mapping = self.binding_users[repo]
            for session in list(mapping):
                if session not in subscribers:
                    mapping.pop(session, None)
            if not mapping:
                self.binding_users.pop(repo, None)
        for session, repo in list(self.active.items()):
            if session not in self.bindings.get(repo, []):
                self.active.pop(session, None)

    # ------------------------------------------------------------------ 绑定关系

    def _session_repos(self, umo: str) -> list[str]:
        """当前会话绑定的仓库（按绑定顺序）。"""
        return [
            repo for repo, subscribers in self.bindings.items() if umo in subscribers
        ]

    def _user_repos(self, umo: str, user_id: str) -> list[str]:
        """同一用户在其它会话里绑定过的仓库。

        既看 ``binding_users`` 里的显式记录，也兼容旧版状态文件：
        私聊会话的 session_id 就是用户 ID（``aiocqhttp:FriendMessage:123``）。
        """
        found: list[str] = []
        for repo, subscribers in self.bindings.items():
            for session in subscribers:
                if session == umo:
                    continue
                owner = (self.binding_users.get(repo) or {}).get(session)
                if owner == user_id or self._session_is_private_of(session, user_id):
                    if repo not in found:
                        found.append(repo)
                    break
        return found

    @staticmethod
    def _session_is_private_of(session: str, user_id: str) -> bool:
        if not user_id:
            return False
        parts = str(session).split(":")
        if len(parts) < 3 or parts[-1] != user_id:
            return False
        return "friend" in parts[1].lower() or "private" in parts[1].lower()

    def _set_active_repo(self, umo: str, repo: str) -> None:
        self.active[umo] = repo

    def _record_binder(self, repo: str, umo: str, user_id: str) -> None:
        if user_id:
            self.binding_users.setdefault(repo, {})[umo] = user_id

    def _forget_binding(self, repo: str, umo: str) -> None:
        (self.binding_users.get(repo) or {}).pop(umo, None)
        if self.binding_users.get(repo) == {}:
            self.binding_users.pop(repo, None)
        if self.active.get(umo) == repo:
            self.active.pop(umo, None)

    # ------------------------------------------------------------------ 推送目标

    def _platform_ids(self) -> list[str]:
        try:
            insts = self.context.platform_manager.platform_insts
        except Exception:
            return []
        ids: list[str] = []
        for inst in insts:
            try:
                platform_id = str(inst.meta().id or "")
            except Exception:
                continue
            if platform_id:
                ids.append(platform_id)
        return ids

    def _platform_hint(self) -> str:
        return targets.platform_hint(
            self.cfg.push_platform, self.bindings, self._platform_ids()
        )

    def _normalize_target(self, raw: Any) -> str | None:
        return targets.normalize_target(raw, self._platform_hint())

    def _configured_targets(self) -> list[str]:
        return targets.configured_targets(
            self.cfg.push_targets or [], self._platform_hint()
        )

    def _broadcast_targets(self) -> list[str]:
        return targets.broadcast_targets(
            self.broadcasts, self.cfg.push_targets or [], self._platform_hint()
        )

    def _delivery_targets(self, subscribers: list[str]) -> list[str]:
        return targets.merge_targets(subscribers, self._broadcast_targets())

    def _admin_ids(self) -> set[str]:
        return {
            str(item).strip()
            for item in (self.cfg.issue_admin_users or [])
            if str(item).strip()
        }

    def _global_admin_ids(self) -> set[str]:
        if astrbot_config is None:
            return set()
        try:
            admins = astrbot_config.get("admins_id", []) or []
        except Exception:
            return set()
        return {str(item).strip() for item in admins if str(item).strip()}

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        allowed, _ = await self._admin_check(event)
        return allowed

    async def _permission_error(
        self, event: AstrMessageEvent, *, action_name: str = "自动发 Issue"
    ) -> str | None:
        """返回无权限时给用户的提示，有权限时返回 None。"""
        if not self.cfg.issue_admin_only:
            return None
        allowed, _ = await self._admin_check(event)
        if allowed:
            return None
        sender = self._user_id(event)
        admins = "、".join(sorted(self._admin_ids())) or "（空）"
        return (
            f"{action_name}需要管理员权限。\n"
            f"你的用户 ID：{sender or '未知'}\n"
            f"当前 issue_admin_users：{admins}\n"
            "以下任一种方式都可以获得权限：\n"
            "1. 群主 / 群管理员直接使用（插件会自动识别）；\n"
            "2. 在插件配置 issue_admin_users 里加上你的 QQ 号（私聊也有效）；\n"
            "3. 把账号加入 AstrBot 的 admins_id（管理员 ID）；\n"
            "4. 关闭 issue_admin_only 让所有人使用。\n"
            f"配置文件：{self.config_path or '未获取到路径'}\n"
            "发送 /ghstatus 可以查看插件读取到的配置路径与权限判定结果。"
        )

    async def _admin_check(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """判断发送者能否使用 /ghissue，并返回依据（方便 /ghstatus 排查）。"""
        sender = self._user_id(event)

        # 1) 插件配置里显式指定的用户（群聊 / 私聊都有效）
        if sender and sender in self._admin_ids():
            return True, "issue_admin_users"

        # 2) 平台事件直接给出的身份（部分适配器会填 event.role / sender.role）
        role = self._event_role(event)
        if role in GROUP_ADMIN_ROLES:
            return True, "群主/群管理员"

        # 3) 平台适配器认为的管理员
        try:
            if event.is_admin():
                return True, "平台管理员"
        except Exception:
            pass

        # 4) AstrBot 全局 admins_id
        if sender and sender in self._global_admin_ids():
            return True, "admins_id"

        # 5) 兜底：向平台查询群成员身份（aiocqhttp 的 event.role 恒为 member）
        if await self._remote_group_admin(event, sender):
            return True, "群主/群管理员"
        return False, "未授权"

    @staticmethod
    def _event_role(event: AstrMessageEvent) -> str:
        try:
            role = str(getattr(event, "role", "") or "").lower()
        except Exception:
            role = ""
        if role in GROUP_ADMIN_ROLES:
            return role
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            sender = raw.get("sender")
        else:
            sender = getattr(raw, "sender", None)
        if isinstance(sender, dict):
            role = str(sender.get("role") or "").lower()
        else:
            role = str(getattr(sender, "role", "") or "").lower()
        return role if role in GROUP_ADMIN_ROLES else ""

    def _platform_client(self, event: AstrMessageEvent):
        """拿到当前消息所在平台适配器的客户端（用于查询群成员身份）。"""
        try:
            name = str(event.platform_meta.name or "")
            platform_id = str(event.platform_meta.id or "")
        except Exception:
            name, platform_id = "", ""
        try:
            insts = self.context.platform_manager.platform_insts
        except Exception:
            return None
        for inst in insts:
            try:
                meta = inst.meta()
                if name and str(meta.name) != name and str(meta.id) != platform_id:
                    continue
                client = inst.get_client()
            except Exception:
                continue
            if hasattr(client, "call_action"):
                return client
        return None

    async def _remote_group_admin(self, event: AstrMessageEvent, sender: str) -> bool:
        if not sender:
            return False
        try:
            if event.is_private_chat():
                return False
        except Exception:
            pass

        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        if not group_id:
            group = getattr(getattr(event, "message_obj", None), "group", None)
            group_id = str(getattr(group, "group_id", "") or "")
        if not group_id.isdigit() or not sender.isdigit():
            return False

        key = (group_id, sender)
        cached = self._admin_cache.get(key)
        if cached and time.time() - cached[0] < ADMIN_CACHE_TTL:
            return cached[1]

        client = self._platform_client(event)
        if client is None:
            return False
        try:
            info = await asyncio.wait_for(
                client.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(sender),
                    no_cache=True,
                ),
                timeout=5,
            )
        except Exception as exc:
            logger.debug("查询群成员身份失败: %s", exc)
            return False

        is_admin = str((info or {}).get("role", "")).lower() in GROUP_ADMIN_ROLES
        self._admin_cache[key] = (time.time(), is_admin)
        return is_admin

    # ------------------------------------------------------------------ 仓库参数

    @staticmethod
    def _extract_repo(text: str | None) -> str | None:
        """从参数中提取 owner/repo，支持完整链接。"""
        value = (text or "").strip()
        if not value:
            return None

        match = REPO_IN_TEXT_PATTERN.search(value)
        if match:
            return f"{match.group(1)}/{match.group(2)}"

        value = value.rstrip("/")
        if value.endswith(".git"):
            value = value[:-4]
        if REPO_PATTERN.match(value):
            return value
        return None

    def _find_binding_key(self, repo: str | None) -> str | None:
        """在已绑定仓库中做大小写不敏感匹配。"""
        if not repo:
            return None
        if repo in self.bindings:
            return repo
        lowered = repo.lower()
        for key in self.bindings:
            if key.lower() == lowered:
                return key
        return None

    # ------------------------------------------------------------------ 命令：绑定

    @filter.command("ghbind")
    async def bind_repo(
        self,
        event: AstrMessageEvent,
        repo: str | None = None,
        force: str | None = None,
    ):
        """绑定 GitHub 仓库。用法: /ghbind 用户名/仓库名 [force]"""
        self._sync_config()
        target = self._extract_repo(repo)
        if not target:
            yield event.plain_result(
                "请提供有效的仓库，格式：/ghbind 用户名/仓库名\n"
                "也可以直接粘贴链接：/ghbind https://github.com/用户名/仓库名"
            )
            return

        skip_check = str(force or "").strip().lower() in {
            "force",
            "--force",
            "-f",
            "跳过",
        }
        info: dict[str, Any] | None = None
        if not skip_check:
            info = await self.api.get_repo(target)
            if not info:
                reason = self.api.last_error or "未知原因"
                yield event.plain_result(
                    f"无法访问仓库 {target}：{reason}\n\n"
                    "排查建议：\n"
                    "1. 在插件配置中填写 GitHub Token（未认证只有 60 次/小时，"
                    "云服务器共享出口 IP 很容易被限流）；若已填写，确认 Token 未过期、"
                    "有 public repo 读取权限（fine-grained token 需允许 Public repositories "
                    "或勾选对应仓库）\n"
                    "2. 若服务器无法直连 api.github.com，请配置 github_proxy"
                    "（例如 http://127.0.0.1:7890）\n"
                    "3. 发送 /ghstatus 查看实时连通性与剩余额度\n"
                    "4. 如果只想先绑定（例如改用 Webhook 模式，由 GitHub 主动推送），"
                    "发送：\n"
                    f"   /ghbind {target} force"
                )
                return

        full_name = str((info or {}).get("full_name") or target)
        key = self._find_binding_key(full_name) or full_name
        subscriber = event.unified_msg_origin
        subscribers = self.bindings.setdefault(key, [])

        already = subscriber in subscribers
        if not already:
            subscribers.append(subscriber)
        if key not in self.watermarks:
            self.watermarks[key] = self._server_now_iso()
        self._record_binder(key, subscriber, self._user_id(event))
        self._set_active_repo(subscriber, key)
        self._save_state()

        if already:
            yield event.plain_result(
                f"默认仓库已切换为 {key}\n"
                "之后 /ghissue 会直接使用这个仓库。"
            )
            return

        description = str((info or {}).get("description") or "").strip()
        mode = (
            "Webhook 推送"
            if self.cfg.enable_webhook
            else f"每 {self.cfg.check_interval} 分钟轮询"
        )
        note = (
            "\n! 本次为 force 绑定，未校验仓库是否存在；仓库名写错将收不到推送。"
            if skip_check
            else ""
        )
        yield event.plain_result(
            f"已绑定仓库 {full_name}\n"
            f"{description or '（未获取仓库描述）'}\n"
            f"推送方式：{mode}；Commit、PR、Issue 更新都会以卡片推送。\n"
            f"默认仓库：/ghissue 会直接使用它（绑定多个仓库后用 /ghswitch 切换）。"
            f"{note}"
        )

    @filter.command("ghunbind")
    async def unbind_repo(self, event: AstrMessageEvent, repo: str | None = None):
        """取消绑定。用法: /ghunbind 用户名/仓库名；省略参数则取消全部"""
        subscriber = event.unified_msg_origin

        if repo is None:
            removed = [
                key
                for key, subscribers in self.bindings.items()
                if subscriber in subscribers
            ]
            if not removed:
                yield event.plain_result("当前会话还没有绑定任何仓库")
                return

            for key in removed:
                self.bindings[key].remove(subscriber)
                self._forget_binding(key, subscriber)
                if not self.bindings[key]:
                    del self.bindings[key]
                    self.watermarks.pop(key, None)
            self._save_state()
            yield event.plain_result("已取消绑定：" + "、".join(removed))
            return

        target = self._extract_repo(repo)
        key = self._find_binding_key(target)
        if not key or subscriber not in self.bindings.get(key, []):
            yield event.plain_result(f"当前会话没有绑定仓库 {repo}")
            return

        self.bindings[key].remove(subscriber)
        self._forget_binding(key, subscriber)
        if not self.bindings[key]:
            del self.bindings[key]
            self.watermarks.pop(key, None)
        self._save_state()
        yield event.plain_result(f"已取消绑定 {key}")

    @filter.command("ghbindlist", alias={"ghbinds"})
    async def list_bindings(self, event: AstrMessageEvent):
        """查看当前会话绑定的仓库"""
        subscriber = event.unified_msg_origin
        repos = self._session_repos(subscriber)
        if not repos:
            yield event.plain_result(
                "当前会话还没有绑定任何仓库，使用 /ghbind 用户名/仓库名 绑定"
            )
            return
        active = self.active.get(subscriber)
        lines = ["当前会话绑定的仓库", ""]
        for repo in repos:
            mark = "  [默认]" if repo == active else ""
            lines.append(f"  · {repo}{mark}")
        if len(repos) > 1 or not active:
            lines.append("")
            lines.append("切换默认仓库：/ghswitch 用户名/仓库名")
        yield event.plain_result("\n".join(lines))

    @filter.command("ghswitch", alias={"ghuse", "gh切换", "ghselect"})
    async def switch_repo(self, event: AstrMessageEvent, repo: str | None = None):
        """切换当前会话默认使用的仓库。用法: /ghswitch 用户名/仓库名"""
        subscriber = event.unified_msg_origin
        repos = self._session_repos(subscriber)
        active = self.active.get(subscriber)

        if repo is None:
            if not repos:
                yield event.plain_result(
                    "当前会话还没有绑定任何仓库。\n"
                    "先发送 /ghbind 用户名/仓库名 绑定，再回来切换。"
                )
                return
            lines = ["当前会话可切换的仓库", ""]
            for index, item in enumerate(repos, start=1):
                mark = "  [默认]" if item == active else ""
                lines.append(f"  {index}. {item}{mark}")
            lines.append("")
            lines.append("切换：/ghswitch 用户名/仓库名（也可以直接写序号）")
            yield event.plain_result("\n".join(lines))
            return

        raw = repo.strip()
        target = self._extract_repo(raw) or (
            repos[int(raw) - 1] if raw.isdigit() and 0 < int(raw) <= len(repos) else None
        )
        if not target:
            yield event.plain_result(
                "请提供有效的仓库，例如：/ghswitch owner/repo"
            )
            return

        key = self._find_binding_key(target)
        if not key:
            yield event.plain_result(
                f"仓库 {target} 还没有绑定过。\n"
                f"先发送 /ghbind {target} 绑定，之后就能用 /ghswitch 切换。"
            )
            return

        subscribers = self.bindings.setdefault(key, [])
        already = subscriber in subscribers
        if not already:
            subscribers.append(subscriber)
            if key not in self.watermarks:
                self.watermarks[key] = self._server_now_iso()
        self._record_binder(key, subscriber, self._user_id(event))
        self._set_active_repo(subscriber, key)
        self._save_state()

        if already:
            yield event.plain_result(
                f"已切换默认仓库为 {key}\n之后 /ghissue 会直接使用它。"
            )
            return
        yield event.plain_result(
            f"已切换默认仓库为 {key}\n"
            "本会话也加入了它的推送列表（Commit / PR / Issue 更新会推到这里）。\n"
            "取消推送：/ghunbind 当前仓库名"
        )

    @filter.command("ghpush", alias={"gh推送"})
    async def manage_push_targets(
        self,
        event: AstrMessageEvent,
        action: str | None = None,
    ):
        """把当前会话加入额外推送目标。用法: /ghpush on|off|list"""
        self._sync_config()
        value = str(action or "").strip().lower()
        umo = event.unified_msg_origin

        if value in {"on", "add", "开", "开启", "加入", "enable", "yes"}:
            if umo in self.broadcasts:
                yield event.plain_result("当前会话已经在推送列表里了")
                return
            self.broadcasts.append(umo)
            self._save_state()
            lines = ["已把当前会话加入推送列表"]
            if self.bindings:
                lines.append(
                    f"之后 {len(self.bindings)} 个已绑定仓库的更新都会推送到这里。"
                )
            else:
                lines.append(
                    "! 目前还没有绑定任何仓库，请先发送 /ghbind 用户名/仓库名"
                )
            lines.append("取消：/ghpush off")
            yield event.plain_result("\n".join(lines))
            return

        if value in {"off", "del", "remove", "关", "关闭", "取消", "退出", "disable"}:
            if umo not in self.broadcasts:
                yield event.plain_result("当前会话不在推送列表里")
                return
            self.broadcasts.remove(umo)
            self._save_state()
            yield event.plain_result("已把当前会话移出推送列表")
            return

        lines = ["额外推送目标（所有已绑定仓库的更新都会推到这里）", ""]
        if self.broadcasts:
            lines.extend(f"  · {item}  (/ghpush 添加)" for item in self.broadcasts)
        if self._configured_targets():
            lines.extend(
                f"  · {item}  (配置项 push_targets)"
                for item in self._configured_targets()
            )
        if not self.broadcasts and not self._configured_targets():
            lines.append("  （暂无）")
            lines.append("在群里发送 /ghpush on，本群就会开始接收推送")
        lines.append(f"当前会话：{umo}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ 命令：检查

    @filter.command("ghcheck")
    async def check_now(self, event: AstrMessageEvent):
        """立即检查一次更新（轮询模式）"""
        self._sync_config()
        if self.cfg.enable_webhook:
            yield event.plain_result(
                "当前为 Webhook 模式，GitHub 会主动推送，无需手动检查"
            )
            return

        repos, sent, failed = await self.poller.poll_once()
        message = f"已检查 {repos} 个仓库，发现并推送 {sent} 条更新"
        if failed:
            message += (
                f"\n! {failed} 个仓库本轮检查失败，已保留水位线，下轮会重试"
                f"（{self.api.last_error or '网络/API 错误'}）"
            )
        yield event.plain_result(message)

    @filter.command("ghrewind")
    async def rewind_watermark(
        self, event: AstrMessageEvent, minutes: str | None = None
    ):
        """把检查水位线回退若干分钟，找回漏掉的更新。用法: /ghrewind 10"""
        if self.cfg.enable_webhook:
            yield event.plain_result("Webhook 模式不使用水位线，无需回退")
            return

        try:
            delta = int(minutes) if minutes else 10
        except (TypeError, ValueError):
            delta = 10
        delta = max(1, min(delta, 24 * 60))

        subscriber = event.unified_msg_origin
        repos = [
            key
            for key, subscribers in self.bindings.items()
            if subscriber in subscribers
        ]
        if not repos:
            yield event.plain_result("当前会话还没有绑定任何仓库")
            return

        local_now = parse_time(now_iso())
        for repo in repos:
            current = parse_time(self.watermarks.get(repo))
            anchor = current or local_now
            if local_now is not None and anchor is not None and anchor > local_now:
                # 水位线可能因旧版本使用了偏快的本机时钟而跑到未来
                anchor = local_now
            if anchor is None:
                continue
            target = anchor - timedelta(minutes=delta)
            self.watermarks[repo] = target.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._save_state()

        yield event.plain_result(
            f"已将 {len(repos)} 个仓库的检查水位线回退 {delta} 分钟\n"
            + "\n".join(f"  · {repo}" for repo in repos)
            + "\n下一轮轮询会重新检查这段时间；也可以发送 /ghcheck 立即触发。"
        )

    @filter.command("ghstatus")
    async def show_status(self, event: AstrMessageEvent):
        """查看插件运行状态与 API 连通性"""
        self._sync_config()
        mode = "Webhook" if self.cfg.enable_webhook else "轮询"
        if not self.cfg.enable_webhook:
            mode += f"（每 {self.cfg.check_interval} 分钟）"
        lines = [
            "GitHub Ling · 运行状态",
            "────────────────────",
            f"版本：{PLUGIN_VERSION}",
            f"模式：{mode}",
            f"Token：{'已配置' if self.cfg.github_token else '未配置（60 次/小时限额）'}",
            f"代理：{self.cfg.github_proxy or '未配置'}",
            f"TLS：{'自定义 CA' if self.cfg.github_ca_bundle else ('校验开启' if self.cfg.github_ssl_verify else '校验已关闭（不安全）')}",
            f"已绑定仓库：{len(self.bindings)} 个",
            f"额外推送目标：{len(self._broadcast_targets())} 个",
            f"卡片图片：{'开启' if self.cfg.send_card_image else '关闭'}"
            f"（{self.cfg.card_style}）",
        ]
        if self.cfg.issue_enabled:
            try:
                label = await self.issue_agent.provider_label(event)
            except Exception:
                label = "未配置"
            lines.append(
                f"Issue 助手：开启（模型：{label}；"
                f"{'需确认' if self.cfg.issue_require_confirm else '直接发送'}；"
                f"{'仅管理员' if self.cfg.issue_admin_only else '所有人可用'}）"
            )
            if self.cfg.issue_admin_only:
                allowed, reason = await self._admin_check(event)
                lines.append(
                    f"你的 ID：{self._user_id(event) or '未知'}；"
                    f"Issue 权限：{'可用' if allowed else '不可用'}（{reason}）"
                )
        else:
            lines.append("Issue 助手：关闭")

        lines.append(f"配置文件：{self.config_path or '未获取到路径'}")
        lines.append(f"状态文件：{self._store.path}")
        session_repos = self._session_repos(event.unified_msg_origin)
        if session_repos:
            active = self.active.get(event.unified_msg_origin)
            lines.append(
                "本会话默认仓库：" + (active or session_repos[0])
                + (f"（共绑定 {len(session_repos)} 个）" if len(session_repos) > 1 else "")
            )
        else:
            lines.append("本会话默认仓库：（未绑定）")
        if self.cfg.enable_webhook:
            lines.append(
                f"监听地址：http://{self.cfg.webhook_host}:{self.cfg.webhook_port}{self.cfg.webhook_path}"
            )

        # /rate_limit 不消耗配额，适合用来判断服务器能否访问 GitHub API
        rate = await self.api.get_rate_limit()
        if rate:
            core = (rate.get("resources") or {}).get("core") or {}
            remaining = core.get("remaining", "?")
            limit = core.get("limit", "?")
            reset = core.get("reset")
            reset_text = ""
            if isinstance(reset, (int, float)) and reset:
                reset_text = f"，{datetime.fromtimestamp(reset).strftime('%H:%M:%S')} 重置"
            lines.append(
                f"API 连通性：正常（核心额度 {remaining}/{limit}{reset_text}）"
            )
        else:
            lines.append(
                f"API 连通性：失败 —— {self.api.last_error or '未知原因'}"
            )

        server_time = self.api.server_now()
        local_time = parse_time(now_iso())
        if server_time is not None and local_time is not None:
            skew = (server_time - local_time).total_seconds()
            if abs(skew) >= 30:
                lines.append(
                    f"时钟偏差：{skew:+.0f} 秒（水位线已使用 GitHub 时间）"
                )

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ 命令：评论

    @filter.command("ghcomment", alias={"gh评论", "ghc"})
    async def comment_command(self, event: AstrMessageEvent, action: GreedyStr):
        """在已有的 Issue / PR 下发表评论。

        用法：
        /ghcomment 12 评论内容
        /ghcomment https://github.com/owner/repo/issues/12 评论内容
        /ghcomment owner/repo#12 评论内容
        /ghcomment 12            （引用一条消息，把引用内容作为引用块附上）
        引用消息里的截图会一起上传，以图片形式附在评论末尾。
        """
        self._sync_config()
        error = await self._permission_error(event, action_name="发表评论")
        if error:
            yield event.plain_result(error)
            return

        repo_arg, number, text, parse_error = self._parse_comment_args(
            str(action or "")
        )
        if parse_error:
            yield event.plain_result(parse_error)
            return

        repo, repo_error, _ = self._issue_repo(event, repo_arg)
        if not repo:
            yield event.plain_result(repo_error)
            return

        body = await self._compose_comment(event, text)
        images = await collect_comment_images(event, self.cfg.issue_max_images)
        if not body and not images:
            yield event.plain_result("评论内容是空的。\n" + COMMENT_USAGE)
            return

        if images:
            yield event.plain_result(f"正在上传 {len(images)} 张截图…")
            result = await self.assets.upload(repo, images)
            if result.urls:
                lines = ["### 截图", ""]
                for index, url in enumerate(result.urls, start=1):
                    lines.append(f"![截图 {index}]({url})")
                body = (body + "\n\n" if body else "") + "\n".join(lines)
            if result.note:
                if not result.urls and not body:
                    yield event.plain_result(f"评论没有发出：{result.note}")
                    return
                body = (body + "\n\n" if body else "") + f"> {result.note}"

        data = await self.api.create_comment(repo, number, body)
        if not data:
            status = self.api.last_status
            reason = self.api.last_error or "未知原因"
            if status == 404:
                reason = (
                    f"{repo} 里没有 #{number}，或者 Token 没有访问该仓库的权限"
                )
            elif status == 403:
                reason = "没有评论权限，或该 Issue 已锁定（" + reason + "）"
            yield event.plain_result(f"评论失败：{reason}")
            return

        url = str(data.get("html_url") or "")
        if url:
            yield event.plain_result(f"已发表评论：{url}")
        else:
            yield event.plain_result(f"已发表评论到 {repo} #{number}")

    # ------------------------------------------------------------------ 命令：自动发 Issue

    @filter.command("ghissue", alias={"gh发issue", "ghnew"})
    async def issue_command(self, event: AstrMessageEvent, action: GreedyStr):
        """根据引用的消息 / 聊天记录自动生成 Issue。

        用法：
        /ghissue 补充说明       引用一条消息（可以是日志、合并转发、截图）后发送
        /ghissue ok            确认创建草稿
        /ghissue cancel        放弃草稿
        /ghissue tpl 2         换用第 2 个模板重新生成
        /ghissue list          查看仓库的 Issue 模板
        /ghissue -r owner/repo 指定仓库
        """
        self._sync_config()
        if not self.cfg.issue_enabled:
            yield event.plain_result(
                "自动发 Issue 已在插件配置中关闭（issue_enabled）"
            )
            return
        error = await self._permission_error(event, action_name="自动发 Issue")
        if error:
            yield event.plain_result(error)
            return

        raw = str(action or "").strip()
        tokens = raw.split()
        head = tokens[0].lower() if tokens else ""
        single = len(tokens) == 1

        if single and head in CONFIRM_WORDS:
            yield event.plain_result(await self._confirm_issue(event))
            return
        if single and head in CANCEL_WORDS:
            yield event.plain_result(self._cancel_issue(event))
            return
        if head in TEMPLATE_WORDS:
            yield event.plain_result("正在换模板重新生成…")
            yield self._wrap_result(
                event, await self._regenerate_issue(event, raw)
            )
            return
        if single and head in LIST_WORDS:
            yield event.plain_result(await self._list_templates(event, raw))
            return

        yield event.plain_result("正在阅读聊天记录并整理 Issue 草稿…")
        yield self._wrap_result(event, await self._start_issue(event, raw))

    async def _start_issue(
        self, event: AstrMessageEvent, raw: str
    ) -> str | MessageEventResult:
        repo, option_error = self._issue_options(raw)
        if option_error:
            return option_error

        repo_name, error, note = self._issue_repo(event, repo)
        if not repo_name:
            return error

        instruction, template_index, template_name = self._parse_issue_args(raw)
        umo = event.unified_msg_origin
        user_id = self._user_id(event)

        if not instruction and not self._has_material(event):
            draft = self.drafts.get(umo, user_id)
            if draft:
                return self._draft_result(event, draft, note=note)

        collector = ChatContextCollector(
            max_chars=self.cfg.issue_max_context_chars,
            max_images=self.cfg.issue_max_images,
        )
        chat = await collector.collect(event, instruction)
        if chat.is_empty:
            return (
                "没有找到可用的聊天内容。\n"
                "请引用一条消息（日志 / 报错 / 合并转发的聊天记录）后再发送 /ghissue，"
                "或在命令后面写一句说明。"
            )

        draft, error = await self.issue_agent.generate(
            event=event,
            repo=repo_name,
            chat=chat,
            instruction=instruction,
            template_index=template_index,
            template_name=template_name,
            user_id=user_id,
        )
        if not draft:
            return f"生成失败：{error}"

        self.drafts.put(draft)
        prefix = f"{note}\n" if note else ""
        if self.cfg.issue_require_confirm:
            return self._draft_result(event, draft, note=note)
        return prefix + await self._publish_draft(event, draft)

    async def _regenerate_issue(
        self, event: AstrMessageEvent, raw: str
    ) -> str | MessageEventResult:
        draft = self.drafts.get(event.unified_msg_origin, self._user_id(event))
        if not draft or draft.chat is None:
            return "没有待确认的草稿。请先引用一条消息发送 /ghissue 生成草稿。"

        tokens = raw.split()
        argument = tokens[1] if len(tokens) > 1 else ""
        index: int | None = None
        name: str | None = None
        if argument.isdigit():
            index = int(argument)
        elif argument:
            name = argument

        new_draft, error = await self.issue_agent.generate(
            event=event,
            repo=draft.repo,
            chat=draft.chat,
            instruction=draft.instruction,
            template_index=index,
            template_name=name,
            user_id=self._user_id(event),
        )
        if not new_draft:
            return f"重新生成失败：{error}"
        self.drafts.put(new_draft)
        return self._draft_result(event, new_draft)

    def _cancel_issue(self, event: AstrMessageEvent) -> str:
        draft = self.drafts.pop(event.unified_msg_origin, self._user_id(event))
        return "已放弃这份草稿" if draft else "当前没有待确认的草稿"

    async def _confirm_issue(self, event: AstrMessageEvent) -> str:
        draft = self.drafts.get(event.unified_msg_origin, self._user_id(event))
        if not draft:
            return (
                "当前没有待确认的草稿。\n"
                "请先引用一条消息发送 /ghissue 生成草稿。"
            )
        return await self._publish_draft(event, draft)

    async def _publish_draft(self, event: AstrMessageEvent, draft) -> str:
        url, error = await self.issue_agent.publish(draft)
        if not url:
            return f"创建失败：{error}\n草稿已保留，可以稍后重试 /ghissue ok"
        self.drafts.pop(event.unified_msg_origin, self._user_id(event))
        return f"已创建 Issue：{url}"

    # ------------------------------------------------------------------ Issue 预览

    @staticmethod
    def _wrap_result(
        event: AstrMessageEvent, value: str | MessageEventResult
    ) -> MessageEventResult:
        if isinstance(value, MessageEventResult):
            return value
        return event.plain_result(str(value))

    @staticmethod
    def _supports_forward(event: AstrMessageEvent) -> bool:
        """当前平台是否支持合并转发（QQ / Satori）。"""
        if not (hasattr(Comp, "Node") and hasattr(Comp, "Nodes")):
            return False
        try:
            name = str(event.get_platform_name() or "").lower()
        except Exception:
            return False
        return name in {"aiocqhttp", "satori"}

    @staticmethod
    def _forward_node(event: AstrMessageEvent, title: str, body: str) -> Any:
        uin = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "0")
        text = f"{title}\n{body}" if title else body
        return Comp.Node(content=[Comp.Plain(text)], name="GitHub Ling", uin=uin)

    def _draft_result(
        self,
        event: AstrMessageEvent,
        draft: Any,
        *,
        note: str = "",
    ) -> str | MessageEventResult:
        """把草稿渲染成合并转发消息；平台不支持时退回纯文本。"""
        require_confirm = self.cfg.issue_require_confirm
        if not self._supports_forward(event):
            text = format_preview(draft, require_confirm=require_confirm)
            return f"{note}\n{text}" if note else text

        sections = preview_sections(draft, require_confirm=require_confirm)
        if note:
            sections.insert(0, ("提示", note))
        nodes = [
            self._forward_node(event, title, body) for title, body in sections
        ]
        return event.chain_result([Comp.Nodes(nodes)])

    async def _list_templates(self, event: AstrMessageEvent, raw: str) -> str:
        repo_arg, option_error = self._issue_options(raw)
        if option_error:
            return option_error
        repo, error, note = self._issue_repo(event, repo_arg)
        if not repo:
            return error
        prefix = f"{note}\n" if note else ""

        templates = await self.templates.get(repo)
        if not templates:
            return prefix + (
                f"{repo} 没有配置 Issue 模板（.github/ISSUE_TEMPLATE），"
                "生成时会使用内置的通用结构。"
            )
        lines = [f"{repo} 的 Issue 模板", ""]
        for index, item in enumerate(templates, start=1):
            suffix = f"  —— {item.description}" if item.description else ""
            lines.append(f"  {index}. {item.name}{suffix}")
        lines.append("")
        lines.append("用 /ghissue tpl 序号 换模板重新生成草稿")
        return prefix + "\n".join(lines)

    # ------------------------------------------------------------------ Issue 参数

    def _parse_comment_args(
        self, raw: str
    ) -> tuple[str | None, int | None, str, str]:
        """解析 ``/ghcomment``：返回 ``(仓库, 编号, 评论内容, 错误)``。"""
        tokens = raw.split()
        repo_arg: str | None = None
        position = 0
        while position < len(tokens):
            if tokens[position] in {"-r", "--repo"}:
                if position + 1 >= len(tokens):
                    return (
                        None,
                        None,
                        "",
                        "参数 -r 后面需要写仓库名，例如 -r owner/repo",
                    )
                repo_arg = tokens[position + 1]
                position += 2
                continue
            break

        if position >= len(tokens):
            return None, None, "", COMMENT_USAGE

        target = tokens[position]
        position += 1
        target_repo: str | None = None
        number: int | None = None

        match = COMMENT_URL_PATTERN.search(target)
        if match:
            target_repo = f"{match.group(1)}/{match.group(2)}"
            number = int(match.group(3))
        else:
            short = COMMENT_SHORT_PATTERN.match(target)
            if short:
                target_repo = short.group("repo")
                number = int(short.group("number"))
            else:
                plain_number = COMMENT_NUMBER_PATTERN.match(target)
                if plain_number:
                    number = int(plain_number.group(1))

        if number is None or number <= 0:
            return None, None, "", "没有看懂要评论哪个 Issue。\n" + COMMENT_USAGE

        body = " ".join(tokens[position:]).strip()
        return repo_arg or target_repo, number, body, ""

    async def _compose_comment(self, event: AstrMessageEvent, text: str) -> str:
        """评论正文：用户写的文字 + 引用消息（Markdown 引用块）。"""
        parts: list[str] = []
        instruction = (text or "").strip()
        if instruction:
            parts.append(instruction)

        quoted = ""
        if extract_quoted_message_text is not None:
            try:
                quoted = str(await extract_quoted_message_text(event) or "")
            except Exception as exc:
                logger.debug("解析引用消息失败: %s", exc)
                quoted = ""
        quoted = quoted.strip()
        if quoted:
            limit = self.cfg.issue_max_context_chars
            if len(quoted) > limit:
                quoted = quoted[:limit] + "\n…（内容过长，已截断）"
            lines = ["> 【引用的消息】"]
            for line in quoted.splitlines():
                lines.append(f"> {line}" if line.strip() else ">")
            parts.append("\n".join(lines))

        return "\n\n".join(parts).strip()

    def _parse_issue_args(
        self, raw: str
    ) -> tuple[str, int | None, str | None]:
        """解析 ``-r owner/repo``、``-t 2`` 与剩余说明文字。"""
        tokens = raw.split()
        index: int | None = None
        name: str | None = None
        position = 0
        while position < len(tokens):
            token = tokens[position]
            if token in {"-t", "--template"} and position + 1 < len(tokens):
                value = tokens[position + 1]
                if value.isdigit():
                    index = int(value)
                else:
                    name = value
                position += 2
                continue
            if token in {"-r", "--repo"} and position + 1 < len(tokens):
                position += 2
                continue
            break
        instruction = " ".join(tokens[position:]).strip()
        return instruction, index, name

    def _issue_options(self, raw: str) -> tuple[str | None, str]:
        """只解析命令开头的选项，取出 ``-r/--repo`` 的值。

        只认开头的参数，避免把补充说明里的「-r」当成选项。
        """
        tokens = raw.split()
        position = 0
        while position < len(tokens):
            token = tokens[position]
            if token in {"-r", "--repo"}:
                if position + 1 < len(tokens):
                    return tokens[position + 1], ""
                return None, "参数 -r 后面需要写仓库名，例如 -r owner/repo"
            if token in {"-t", "--template"} and position + 1 < len(tokens):
                position += 2
                continue
            break
        return None, ""

    def _issue_repo(
        self,
        event: AstrMessageEvent,
        explicit: str | None,
    ) -> tuple[str | None, str, str]:
        """决定这次 Issue 发到哪个仓库。

        返回 ``(仓库, 错误信息, 提示)``。查找顺序：

        1. ``-r owner/repo`` 明确指定（只要该仓库被任何会话绑定过即可）；
        2. 当前会话的默认仓库（``/ghbind`` / ``/ghswitch`` 设置）；
        3. 当前会话唯一绑定的仓库；
        4. 同一用户在其它会话（例如私聊）绑定过的仓库；
        5. 全局唯一绑定的仓库。

        第 4、5 步解决「私聊绑过了，群里还要再绑一次」的问题。
        """
        umo = event.unified_msg_origin
        user_id = self._user_id(event)

        if explicit:
            target = self._extract_repo(explicit)
            key = self._find_binding_key(target)
            if not key:
                return (
                    None,
                    f"仓库 {explicit} 还没有绑定过。\n"
                    f"请先发送 /ghbind {explicit} 绑定。",
                    "",
                )
            if umo in self.bindings.get(key, []):
                return key, "", ""
            return (
                key,
                "",
                f"本次使用其他会话绑定的 {key}；"
                f"发送 /ghbind {key} 可让本会话也接收推送。",
            )

        session_repos = self._session_repos(umo)
        active = self.active.get(umo)
        if active and active in session_repos:
            return active, "", ""
        if len(session_repos) == 1:
            return session_repos[0], "", ""
        if len(session_repos) > 1:
            listing = "\n".join(
                f"- {item}{'（默认）' if item == active else ''}"
                for item in session_repos
            )
            return (
                None,
                "当前会话绑定了多个仓库，请先选一个默认仓库：\n"
                f"{listing}\n"
                f"发送 /ghswitch {session_repos[0]} 切换，"
                "或本次用 /ghissue -r 仓库名 指定。",
                "",
            )

        # 当前会话没有绑定：看是不是同一个人在别处绑过（私聊 ↔ 群聊）
        user_repos = self._user_repos(umo, user_id)
        if len(user_repos) == 1:
            repo = user_repos[0]
            return (
                repo,
                "",
                f"本会话还没绑定仓库，本次使用你在其他会话绑定的 {repo}；"
                f"发送 /ghbind {repo} 可让本会话也接收推送。",
            )
        if len(user_repos) > 1:
            listing = "\n".join(f"- {item}" for item in user_repos)
            return (
                None,
                "你在其他会话绑定了多个仓库，请指定这次发到哪一个：\n"
                f"{listing}\n"
                f"例如：/ghissue -r {user_repos[0]} 这个报错了\n"
                "也可以先 /ghswitch 仓库名，把本群固定到这个仓库。",
                "",
            )

        if len(self.bindings) == 1:
            repo = next(iter(self.bindings))
            return (
                repo,
                "",
                f"本会话还没绑定仓库，本次使用全局唯一绑定的 {repo}；"
                f"发送 /ghbind {repo} 可让本会话也接收推送。",
            )
        if not self.bindings:
            return (
                None,
                "还没有绑定任何仓库。\n"
                "请先发送 /ghbind 用户名/仓库名 绑定一个仓库。",
                "",
            )

        listing = "\n".join(f"- {item}" for item in list(self.bindings)[:10])
        return (
            None,
            "本会话还没有绑定仓库，而全局有多个仓库可选，请指定一个：\n"
            f"{listing}\n"
            "先在本会话 /ghbind 一个仓库，或本次用 /ghissue -r 仓库名 指定。",
            "",
        )

    @staticmethod
    def _has_material(event: AstrMessageEvent) -> bool:
        """消息里是否带了引用 / 合并转发 / 图片。"""
        try:
            components = list(event.get_messages() or [])
        except Exception:
            return False
        material_types = tuple(
            item
            for item in (
                getattr(Comp, name, None)
                for name in ("Reply", "Forward", "Nodes", "Node", "Image")
            )
            if isinstance(item, type)
        )
        return any(isinstance(comp, material_types) for comp in components)

    @staticmethod
    def _user_id(event: AstrMessageEvent) -> str:
        """取发送者 ID。

        ``event.get_sender_id()`` 只在 ``user_id`` 是字符串时返回内容
        （aiocqhttp 是 int），所以这里直接从 message_obj 里取并转成字符串，
        避免同一群里的所有用户共用一份草稿。
        """
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        uid = getattr(sender, "user_id", None)
        if uid is not None:
            text = str(uid).strip()
            if text:
                return text
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------ Webhook 入口

    async def handle_webhook_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        if event_type == "ping":
            logger.info("收到 GitHub Webhook ping 事件，配置正常")
            return

        repository = payload.get("repository")
        repo = ""
        if isinstance(repository, dict):
            repo = str(repository.get("full_name") or "")

        key = self._find_binding_key(repo)
        if not key:
            return

        events = webhook_events.build_events(event_type, payload)
        if not events:
            return

        subscribers = list(self.bindings.get(key, []))
        for event in events:
            await self._deliver(event, subscribers)

    # ------------------------------------------------------------------ 发送

    async def _deliver(
        self, event: dict[str, Any], subscribers: list[str]
    ) -> None:
        subscribers = self._delivery_targets(subscribers)
        if not subscribers:
            return

        image: str | None = None
        if self.cfg.send_card_image:
            image = await self._render_card_image(event)

        plain = None
        if not image:
            plain = cards.render_event(
                event,
                markdown=False,
                line_limit=self.cfg.comment_line_limit,
                max_commits=self.cfg.max_commits_per_card,
            )

        for subscriber in subscribers:
            try:
                if image:
                    chain = MessageChain([self._image_component(image)])
                else:
                    chain = MessageChain([Comp.Plain(plain or "")])
                await self.context.send_message(subscriber, chain)
            except Exception as exc:
                logger.error("向 %s 推送 GitHub 更新失败: %s", subscriber, exc)
            await asyncio.sleep(0.5)

    async def _avatar_path(self, login: str) -> str | None:
        """返回操作人头像的本地缓存路径（没有则尝试下载一次）。"""
        login = (login or "").strip()
        if not LOGIN_PATTERN.match(login):
            return None

        cache = os.path.join(AVATAR_DIR, f"{login.lower()}.png")
        try:
            if os.path.exists(cache) and (
                time.time() - os.path.getmtime(cache) < AVATAR_TTL_SECONDS
            ):
                return cache
        except OSError:
            pass

        data = await self.api.fetch_avatar(login)
        # 只有确实是图片才落盘，避免把错误页当成头像缓存 7 天
        if data and _looks_like_image(data):
            try:
                os.makedirs(AVATAR_DIR, exist_ok=True)
                tmp = cache + ".tmp"
                with open(tmp, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, cache)
                return cache
            except OSError as exc:
                logger.debug("缓存头像失败 %s: %s", login, exc)

        # 下载失败时退回旧缓存（哪怕过期），总比没有头像好
        return cache if os.path.exists(cache) else None

    async def _render_card_image(self, event: dict[str, Any]) -> str | None:
        """渲染卡片图片，返回文件路径或 URL；全部失败时返回 None（改用纯文本）。

        优先使用插件内置的 Pillow 渲染器（米白毛玻璃风格，不依赖网络）；
        失败或配置为 markdown 时退回 AstrBot 的文转图服务。
        """
        if self.cfg.card_style == "ling":
            avatar_path = None
            target_avatar_path = None
            try:
                avatar_path = await asyncio.wait_for(
                    self._avatar_path(str(event.get("actor") or "")), timeout=12
                )
            except Exception as exc:
                logger.debug("获取操作人头像失败，使用占位图: %s", exc)

            if event.get("target"):
                try:
                    target_avatar_path = await asyncio.wait_for(
                        self._avatar_path(str(event.get("target"))), timeout=12
                    )
                except Exception as exc:
                    logger.debug("获取对方头像失败，使用占位图: %s", exc)

            try:
                path = await asyncio.wait_for(
                    asyncio.to_thread(
                        card_render.render_card_file,
                        event,
                        line_limit=self.cfg.comment_line_limit,
                        max_commits=self.cfg.max_commits_per_card,
                        width=self.cfg.card_width,
                        avatar_path=avatar_path,
                        target_avatar_path=target_avatar_path,
                    ),
                    timeout=CARD_RENDER_TIMEOUT,
                )
                if path and os.path.exists(path):
                    return path
                raise RuntimeError("渲染器没有返回图片文件")
            except Exception as exc:
                logger.warning("内置卡片渲染失败，尝试文转图: %s", exc)

        try:
            markdown = cards.render_event(
                event,
                markdown=True,
                line_limit=self.cfg.comment_line_limit,
                max_commits=self.cfg.max_commits_per_card,
            )
            image = await asyncio.wait_for(
                self.text_to_image(markdown), timeout=CARD_RENDER_TIMEOUT
            )
            if not image:
                raise RuntimeError("文转图返回空结果")
            return image
        except Exception as exc:
            logger.warning("卡片渲染失败，改用纯文本发送: %s", exc)
            return None

    @staticmethod
    def _image_component(value: str) -> Comp.Image:
        text = str(value)
        if text.startswith("file://"):
            path = unquote(urlparse(text).path)
            if re.match(r"^/[A-Za-z]:[/\\]", path):
                path = path[1:]
            return Comp.Image.fromFileSystem(path)
        if text.startswith("http://") or text.startswith("https://"):
            return Comp.Image.fromURL(text)
        return Comp.Image.fromFileSystem(text)
