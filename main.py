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
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlparse

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
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
from .issue.context import ChatContextCollector
from .issue.draft import DraftStore, format_preview
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

AVATAR_TTL_SECONDS = 7 * 24 * 3600
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")

REPO_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+$")
REPO_IN_TEXT_PATTERN = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)")

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
    "1.6.0",
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
    /ghpush on|off|list      把当前会话（例如某个群）加入/移出额外推送目标
    /ghissue [说明|ok|cancel|tpl N|list]  根据引用的聊天记录自动发 Issue
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
        """每次命令 / 推送前同步配置，避免改了配置却未重载插件。"""
        self.cfg.refresh(self.raw_config)
        self.api.token = self.cfg.github_token
        self.api.proxy = self.cfg.github_proxy
        self.api.ca_bundle = self.cfg.github_ca_bundle
        self.api.ssl_verify = self.cfg.github_ssl_verify

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
        self._store.save(
            bindings=self.bindings,
            watermarks=self.watermarks,
            broadcasts=self.broadcasts,
        )

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

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender = self._user_id(event)
        if not sender or astrbot_config is None:
            return False
        try:
            admins = astrbot_config.get("admins_id", []) or []
        except Exception:
            return False
        return sender in {str(item) for item in admins}

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

        if subscriber in subscribers:
            yield event.plain_result(f"当前会话已经绑定了 {key}")
            return

        subscribers.append(subscriber)
        if key not in self.watermarks:
            self.watermarks[key] = self._server_now_iso()
        self._save_state()

        description = str((info or {}).get("description") or "").strip()
        mode = (
            "Webhook 推送"
            if self.cfg.enable_webhook
            else f"每 {self.cfg.check_interval} 分钟轮询"
        )
        note = (
            "\n⚠️ 本次为 force 绑定，未校验仓库是否存在；仓库名写错将收不到推送。"
            if skip_check
            else ""
        )
        yield event.plain_result(
            f"✅ 已绑定仓库 {full_name}\n"
            f"{description or '（未获取仓库描述）'}\n"
            f"推送方式：{mode}；Commit、PR、Issue 更新都会以卡片推送。{note}"
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
        if not self.bindings[key]:
            del self.bindings[key]
            self.watermarks.pop(key, None)
        self._save_state()
        yield event.plain_result(f"已取消绑定 {key}")

    @filter.command("ghbindlist", alias={"ghbinds"})
    async def list_bindings(self, event: AstrMessageEvent):
        """查看当前会话绑定的仓库"""
        subscriber = event.unified_msg_origin
        repos = [
            key for key, subscribers in self.bindings.items() if subscriber in subscribers
        ]
        if not repos:
            yield event.plain_result(
                "当前会话还没有绑定任何仓库，使用 /ghbind 用户名/仓库名 绑定"
            )
            return
        yield event.plain_result(
            "当前会话绑定的仓库：\n" + "\n".join(f"- {repo}" for repo in repos)
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
            lines = ["✅ 已把当前会话加入推送列表"]
            if self.bindings:
                lines.append(
                    f"之后 {len(self.bindings)} 个已绑定仓库的更新都会推送到这里。"
                )
            else:
                lines.append(
                    "⚠️ 目前还没有绑定任何仓库，请先发送 /ghbind 用户名/仓库名"
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

        lines = ["📢 额外推送目标（所有已绑定仓库的更新都会推到这里）"]
        if self.broadcasts:
            lines.extend(f"- {item}（/ghpush 添加）" for item in self.broadcasts)
        if self._configured_targets():
            lines.extend(
                f"- {item}（配置项 push_targets）"
                for item in self._configured_targets()
            )
        if not self.broadcasts and not self._configured_targets():
            lines.append("- （暂无）")
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
        message = f"🔍 已检查 {repos} 个仓库，发现并推送 {sent} 条更新"
        if failed:
            message += (
                f"\n⚠️ {failed} 个仓库本轮检查失败，已保留水位线，下轮会重试"
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
            f"⏪ 已将 {len(repos)} 个仓库的检查水位线回退 {delta} 分钟：\n"
            + "\n".join(f"- {repo}" for repo in repos)
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
            "📊 GitHub Ling 状态",
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
                f"{'需确认' if self.cfg.issue_require_confirm else '直接发送'}）"
            )
        else:
            lines.append("Issue 助手：关闭")
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
        if self.cfg.issue_admin_only and not self._is_admin(event):
            yield event.plain_result(
                "自动发 Issue 需要管理员权限。\n"
                "可以在 AstrBot 的 admins_id 中加入你的账号，"
                "或在插件配置中关闭 issue_admin_only。"
            )
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
            yield event.plain_result("🧩 正在换模板重新生成…")
            yield event.plain_result(await self._regenerate_issue(event, raw))
            return
        if single and head in LIST_WORDS:
            yield event.plain_result(await self._list_templates(event, raw))
            return

        yield event.plain_result("🤔 正在阅读聊天记录并整理 Issue 草稿…")
        yield event.plain_result(await self._start_issue(event, raw))

    async def _start_issue(self, event: AstrMessageEvent, raw: str) -> str:
        repo, option_error = self._issue_options(raw)
        if option_error:
            return option_error

        repo_name, error = self._issue_repo(event, repo)
        if not repo_name:
            return error

        instruction, template_index, template_name = self._parse_issue_args(raw)
        umo = event.unified_msg_origin
        user_id = self._user_id(event)

        if not instruction and not self._has_material(event):
            draft = self.drafts.get(umo, user_id)
            if draft:
                return format_preview(
                    draft, require_confirm=self.cfg.issue_require_confirm
                )

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
            return f"❌ 生成失败：{error}"

        self.drafts.put(draft)
        if self.cfg.issue_require_confirm:
            return format_preview(draft, require_confirm=True)
        return await self._publish_draft(event, draft)

    async def _regenerate_issue(self, event: AstrMessageEvent, raw: str) -> str:
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
            return f"❌ 重新生成失败：{error}"
        self.drafts.put(new_draft)
        return format_preview(
            new_draft, require_confirm=self.cfg.issue_require_confirm
        )

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
            return f"❌ 创建失败：{error}\n草稿已保留，可以稍后重试 /ghissue ok"
        self.drafts.pop(event.unified_msg_origin, self._user_id(event))
        return f"✅ 已创建 Issue：{url}"

    async def _list_templates(self, event: AstrMessageEvent, raw: str) -> str:
        repo_arg, option_error = self._issue_options(raw)
        if option_error:
            return option_error
        repo, error = self._issue_repo(event, repo_arg)
        if not repo:
            return error

        templates = await self.templates.get(repo)
        if not templates:
            return (
                f"{repo} 没有配置 Issue 模板（.github/ISSUE_TEMPLATE），"
                "生成时会使用内置的通用结构。"
            )
        lines = [f"📋 {repo} 的 Issue 模板："]
        for index, item in enumerate(templates, start=1):
            suffix = f" —— {item.description}" if item.description else ""
            lines.append(f"{index}. {item.name}{suffix}")
        lines.append("")
        lines.append("用 /ghissue tpl 序号 换模板重新生成草稿")
        return "\n".join(lines)

    # ------------------------------------------------------------------ Issue 参数

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
    ) -> tuple[str | None, str]:
        subscribed = [
            key
            for key, subscribers in self.bindings.items()
            if event.unified_msg_origin in subscribers
        ]
        if explicit:
            target = self._extract_repo(explicit)
            key = self._find_binding_key(target)
            if key and event.unified_msg_origin in self.bindings.get(key, []):
                return key, ""
            return (
                None,
                f"当前会话没有绑定仓库 {explicit}。\n"
                "自动发 Issue 只允许操作当前会话已绑定的仓库，"
                "请先发送 /ghbind 用户名/仓库名。",
            )

        if len(subscribed) == 1:
            return subscribed[0], ""
        if not subscribed:
            return (
                None,
                "当前会话还没有绑定任何仓库。\n"
                "请先发送 /ghbind 用户名/仓库名 绑定一个仓库。",
            )
        listing = "\n".join(f"- {item}" for item in subscribed)
        return (
            None,
            "当前会话绑定了多个仓库，请用 -r 指定其中一个：\n"
            f"{listing}\n例如：/ghissue -r {subscribed[0]} 这个报错了",
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
