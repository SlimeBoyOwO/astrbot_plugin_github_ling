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
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from . import cards, card_render
from .github_api import GitHubAPI
from .utils import first_line, now_iso, parse_time
from .webhook_events import build_events
from .webhook_server import GitHubWebhookServer

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    _DATA_DIR = str(get_astrbot_data_path())
except Exception:  # 兼容旧版本 AstrBot
    _DATA_DIR = "data"

STATE_FILE = os.path.join(_DATA_DIR, "github_ling_state.json")
AVATAR_DIR = os.path.join(_DATA_DIR, "github_ling_avatars")
AVATAR_TTL_SECONDS = 7 * 24 * 3600
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")

REPO_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+$")
REPO_IN_TEXT_PATTERN = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)")

# 文转图服务默认会先请求网络端点，网络不佳时可能长时间阻塞；
# 超过该时间即放弃卡片、改用纯文本，避免阻塞后续通知。
CARD_RENDER_TIMEOUT = 20

# 推进水位线时回退几秒，避免 GitHub 秒级时间戳与查询快照之间的边界漏报
WATERMARK_SAFETY_SECONDS = 2


def _looks_like_image(data: bytes) -> bool:
    """粗略判断字节串是不是 PNG / JPEG / GIF / WebP 图片。"""
    if not data:
        return False
    signatures = (
        bytes((0x89, 0x50, 0x4E, 0x47)),  # PNG
        bytes((0xFF, 0xD8, 0xFF)),  # JPEG
        b"GIF8",  # GIF
        b"RIFF",  # WebP
    )
    return any(data.startswith(signature) for signature in signatures)


def _describe_issue_event(event: dict[str, Any]) -> str | None:
    """把一条 issue event（标签、指派、标题、状态等）翻译成中文描述。"""
    event_type = str(event.get("event") or "")
    actor = str((event.get("actor") or {}).get("login") or "未知")
    label = str((event.get("label") or {}).get("name") or "?")
    assignee = str((event.get("assignee") or {}).get("login") or "?")
    milestone = str((event.get("milestone") or {}).get("title") or "?")
    rename = event.get("rename") or {}

    if event_type == "labeled":
        return f"{actor} 添加了标签：{label}"
    if event_type == "unlabeled":
        return f"{actor} 移除了标签：{label}"
    if event_type == "assigned":
        return f"{actor} 指派给：{assignee}"
    if event_type == "unassigned":
        return f"{actor} 取消指派：{assignee}"
    if event_type == "renamed":
        old = rename.get("from") or "?"
        new = rename.get("to") or "?"
        return f"{actor} 修改标题：{old} → {new}"
    if event_type == "milestoned":
        return f"{actor} 设置里程碑：{milestone}"
    if event_type == "demilestoned":
        return f"{actor} 移除里程碑：{milestone}"
    if event_type == "locked":
        return f"{actor} 锁定了讨论"
    if event_type == "unlocked":
        return f"{actor} 解锁了讨论"
    if event_type == "closed":
        return f"{actor} 关闭了"
    if event_type == "reopened":
        return f"{actor} 重新打开了"
    if event_type == "committed":
        sha = str(event.get("commit_id") or event.get("sha") or "")[:7]
        return f"{actor} 推送了提交 {sha}" if sha else f"{actor} 推送了新的提交"
    if event_type == "ready_for_review":
        return f"{actor} 将 PR 标记为可审查"
    if event_type == "review_requested":
        return f"{actor} 请求了审查"
    return None


@register(
    "astrbot_plugin_github_ling",
    "SlimeBoyOwO",
    "绑定 GitHub 仓库，用卡片推送 Commit、PR 与 Issue 更新",
    "1.4.7",
)
class GitHubLingPlugin(Star):
    """GitHub 仓库消息推送

    绑定仓库后，插件会推送以下更新：

    - Commit：卡片展示短哈希与提交信息（如 feat: xxx）
    - PR / Issue：大标题 + 评论（或描述）的前 10 行

    命令：
    /ghbind 用户名/仓库名    绑定仓库到当前会话（也可直接粘贴仓库链接）
    /ghbind 用户名/仓库名 force  跳过 API 校验直接绑定（服务器连不上 api.github.com 时使用）
    /ghunbind [仓库]         取消绑定，省略参数则取消当前会话全部绑定
    /ghbindlist              查看当前会话绑定的仓库
    /ghcheck                 立即检查一次更新（轮询模式）
    /ghrewind [分钟]         把水位线回退若干分钟，用于找回漏掉的事件
    /ghstatus                查看运行状态与 API 连通性
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}

        self.github_token = str(self.config.get("github_token", "") or "").strip()
        self.github_proxy = str(self.config.get("github_proxy", "") or "").strip()
        self.github_ca_bundle = str(
            self.config.get("github_ca_bundle", "") or ""
        ).strip()
        self.github_ssl_verify = self._cfg_bool("github_ssl_verify", True)
        self.check_interval = self._cfg_int("check_interval", 10, minimum=1)
        self.send_card_image = self._cfg_bool("send_card_image", True)
        self.card_style = str(self.config.get("card_style", "ling") or "ling").strip().lower()
        if self.card_style not in ("ling", "markdown"):
            self.card_style = "ling"
        self.card_width = self._cfg_int("card_width", 960, minimum=480)
        self.comment_line_limit = self._cfg_int(
            "comment_line_limit", 10, minimum=1
        )
        self.max_commits_per_card = self._cfg_int(
            "max_commits_per_card", 5, minimum=1
        )
        self.poll_comments = self._cfg_bool("poll_comments", True)
        self.max_items_per_check = self._cfg_int(
            "max_items_per_check", 3, minimum=1
        )

        self.enable_webhook = self._cfg_bool("enable_webhook", False)
        self.webhook_host = str(self.config.get("webhook_host", "0.0.0.0"))
        self.webhook_port = self._cfg_int("webhook_port", 6193, minimum=1)
        self.webhook_path = str(
            self.config.get("webhook_path", "/github/webhook")
        )
        self.webhook_secret = str(self.config.get("webhook_secret", "") or "")

        state = self._load_state()
        self.bindings: dict[str, list[str]] = {
            str(repo): [str(item) for item in subscribers]
            for repo, subscribers in (state.get("bindings") or {}).items()
            if isinstance(subscribers, list)
        }
        self.watermarks: dict[str, str] = {
            str(repo): str(value)
            for repo, value in (state.get("watermarks") or {}).items()
        }

        self.api = GitHubAPI(
            self.github_token,
            proxy=self.github_proxy,
            ca_bundle=self.github_ca_bundle,
            ssl_verify=self.github_ssl_verify,
        )
        self.webhook_server: GitHubWebhookServer | None = None
        if self.enable_webhook:
            self.webhook_server = GitHubWebhookServer(
                plugin=self,
                host=self.webhook_host,
                port=self.webhook_port,
                path=self.webhook_path,
                secret=self.webhook_secret,
            )

        self.task: asyncio.Task[Any] | None = None
        self._poll_lock = asyncio.Lock()

    def _cfg_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off", ""}
        return bool(value)

    def _sync_api_credentials(self) -> None:
        """每次调用 API 前同步配置，避免改了 Token/代理却未重载插件。"""
        token = str(self.config.get("github_token", "") or "").strip()
        proxy = str(self.config.get("github_proxy", "") or "").strip()
        self.github_token = token
        self.github_proxy = proxy
        self.api.token = token
        self.api.proxy = proxy

    def _server_now_iso(self, safety_seconds: int = 0) -> str:
        """优先使用 GitHub 服务器时间作为水位线，避免本机时钟偏差。"""
        server_time = self.api.server_now()
        if server_time is not None:
            target = server_time - timedelta(seconds=max(0, safety_seconds))
            return target.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if safety_seconds:
            local_time = parse_time(now_iso())
            if local_time is not None:
                local_time -= timedelta(seconds=safety_seconds)
                return local_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        return now_iso()

    @staticmethod
    def _find_closer(
        issue_events: list[dict[str, Any]],
        since: datetime,
    ) -> str | None:
        """找出水位线之后是谁关闭/合并了这条 Issue/PR。"""
        latest: tuple[datetime, str] | None = None
        for event in issue_events:
            if not isinstance(event, dict):
                continue
            if str(event.get("event") or "") not in {"closed", "merged"}:
                continue
            event_time = parse_time(event.get("created_at"))
            if event_time is None or event_time <= since:
                continue
            login = str((event.get("actor") or {}).get("login") or "")
            if not login:
                continue
            if latest is None or event_time > latest[0]:
                latest = (event_time, login)
        return latest[1] if latest else None

    def _describe_recent_events(
        self,
        issue_events: list[dict[str, Any]],
        since: datetime,
    ) -> str:
        """取水位线之后的最新几条 issue event，拼成“变更内容”。"""
        described: list[tuple[datetime, str]] = []
        for event in issue_events:
            if not isinstance(event, dict):
                continue
            event_time = parse_time(event.get("created_at"))
            if event_time is None or event_time <= since:
                continue
            text = _describe_issue_event(event)
            if text:
                described.append((event_time, text))
        described.sort(key=lambda pair: pair[0], reverse=True)
        return "\n".join(text for _, text in described[:3])

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        if self.enable_webhook:
            assert self.webhook_server is not None
            try:
                await self.webhook_server.start()
            except Exception as exc:
                logger.error("启动 GitHub Ling Webhook 服务失败: %s", exc)
        else:
            self.task = asyncio.create_task(self._poll_loop())
            logger.info(
                "GitHub Ling 启动轮询模式：间隔 %s 分钟，已绑定 %s 个仓库",
                self.check_interval,
                len(self.bindings),
            )

    async def terminate(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

        if self.webhook_server:
            await self.webhook_server.stop()

        await self.api.close()
        self._save_state()
        logger.info("GitHub Ling 插件已卸载")

    # ------------------------------------------------------------------ 状态存取

    def _load_state(self) -> dict[str, Any]:
        if not os.path.exists(STATE_FILE):
            return {}
        try:
            with open(STATE_FILE, encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.error("读取 GitHub Ling 状态文件失败: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "bindings": self.bindings,
                        "watermarks": self.watermarks,
                    },
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as exc:
            logger.error("保存 GitHub Ling 状态文件失败: %s", exc)

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

    # ------------------------------------------------------------------ 命令

    @filter.command("ghbind")
    async def bind_repo(
        self,
        event: AstrMessageEvent,
        repo: str | None = None,
        force: str | None = None,
    ):
        """绑定 GitHub 仓库。用法: /ghbind 用户名/仓库名 [force]"""
        self._sync_api_credentials()
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
            if self.enable_webhook
            else f"每 {self.check_interval} 分钟轮询"
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

    @filter.command("ghcheck")
    async def check_now(self, event: AstrMessageEvent):
        """立即检查一次更新（轮询模式）"""
        if self.enable_webhook:
            yield event.plain_result(
                "当前为 Webhook 模式，GitHub 会主动推送，无需手动检查"
            )
            return

        repos, sent, failed = await self._poll_once()
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
        if self.enable_webhook:
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
        self._sync_api_credentials()
        mode = "Webhook" if self.enable_webhook else "轮询"
        if not self.enable_webhook:
            mode += f"（每 {self.check_interval} 分钟）"
        lines = [
            "📊 GitHub Ling 状态",
            f"模式：{mode}",
            f"Token：{'已配置' if self.github_token else '未配置（60 次/小时限额）'}",
            f"代理：{self.github_proxy or '未配置'}",
            f"TLS：{'自定义 CA' if self.github_ca_bundle else ('校验开启' if self.github_ssl_verify else '校验已关闭（不安全）')}",
            f"已绑定仓库：{len(self.bindings)} 个",
            f"卡片图片：{'开启' if self.send_card_image else '关闭'}"
            f"（{self.card_style}）",
        ]
        if self.enable_webhook:
            lines.append(
                f"监听地址：http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"
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

        events = build_events(event_type, payload)
        if not events:
            return

        subscribers = list(self.bindings.get(key, []))
        for event in events:
            await self._deliver(event, subscribers)

    # ------------------------------------------------------------------ 轮询

    async def _poll_loop(self) -> None:
        # 稍等插件加载完成，避免与初始化流程抢资源
        await asyncio.sleep(5)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("轮询 GitHub 更新时出错")
            await asyncio.sleep(max(1, self.check_interval) * 60)

    async def _poll_once(self) -> tuple[int, int, int]:
        self._sync_api_credentials()
        async with self._poll_lock:
            checked = 0
            sent = 0
            failed = 0

            for repo in list(self.bindings.keys()):
                subscribers = list(self.bindings.get(repo) or [])
                if not subscribers:
                    continue

                checked += 1
                try:
                    events, ok = await self._collect_updates(repo)
                except Exception:
                    logger.exception("检查仓库 %s 更新时出错", repo)
                    events, ok = [], False

                if not ok:
                    # 关键：请求失败时绝不推进水位线，否则失败窗口内的事件会被永久跳过
                    failed += 1
                    logger.warning(
                        "仓库 %s 本轮检查未完成（%s），保留水位线以便下轮重试",
                        repo,
                        self.api.last_error or "未知原因",
                    )
                    continue

                for event in events:
                    await self._deliver(event, subscribers)
                    sent += 1

                self.watermarks[repo] = self._server_now_iso(
                    WATERMARK_SAFETY_SECONDS
                )
                logger.info(
                    "仓库 %s 本轮检查完成，发现 %s 条更新", repo, len(events)
                )

            if checked:
                self._save_state()
            return checked, sent, failed

    async def _collect_updates(
        self, repo: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """收集仓库更新。

        Returns:
            ``(events, ok)``。``ok=False`` 表示本轮有请求失败，调用方必须保留
            水位线，下一轮从旧水位线重新检查，避免漏报。
        """
        since = parse_time(self.watermarks.get(repo))
        if since is None:
            self.watermarks[repo] = self._server_now_iso()
            return [], True

        since_param = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        events: list[dict[str, Any]] = []

        # 1) 提交（GitHub 默认分支，按时间倒序返回）
        commits = await self.api.list_commits(repo, since_param)
        if commits is None:
            return [], False

        commit_items = []
        for commit in commits:
            commit_data = commit.get("commit") or {}
            commit_time = parse_time(
                (commit_data.get("committer") or {}).get("date")
            )
            if commit_time is None:
                continue
            if commit_time <= since:
                break
            commit_items.append(
                {
                    "sha": str(commit.get("sha") or ""),
                    "subject": first_line(commit_data.get("message")),
                    "url": str(commit.get("html_url") or ""),
                    "author": (commit_data.get("author") or {}).get("name")
                    or "未知",
                }
            )

        if commit_items:
            events.append(
                {
                    "kind": "commit",
                    "repo": repo,
                    "branch": None,
                    "action": "push",
                    "actor": commit_items[0].get("author") or "未知",
                    "commits": commit_items,
                    "url": commit_items[0].get("url") or "",
                }
            )

        # 2) Issue / PR（按 updated_at 倒序）
        issues = await self.api.list_issues(repo, since_param)
        if issues is None:
            return [], False

        comment_fetches = 0
        event_fetches = 0
        review_fetches = 0
        for item in issues:
            updated = parse_time(item.get("updated_at"))
            if updated is None or updated <= since:
                break

            kind = "pr" if "pull_request" in item else "issue"
            number = item.get("number")
            author = str((item.get("user") or {}).get("login") or "未知")
            actor = author
            body = str(item.get("body") or "")
            body_label = "描述"
            action = "updated"
            url = str(item.get("html_url") or "")

            created = parse_time(item.get("created_at"))
            is_new = created is not None and created > since
            state_action = None
            if item.get("state") == "closed":
                merged_at = (item.get("pull_request") or {}).get("merged_at")
                state_action = "merged" if merged_at else "closed"

            # 1) 优先展示“这次更新”的内容，而不是 Issue/PR 的原始描述
            new_review = None
            if (
                kind == "pr"
                and self.poll_comments
                and review_fetches < self.max_items_per_check
            ):
                review_fetches += 1
                reviews = await self.api.list_reviews(repo, number)
                if reviews is None and self.api.last_error:
                    logger.warning(
                        "获取 %s#%s 的审查记录失败（%s），本轮保留水位线",
                        repo,
                        number,
                        self.api.last_error,
                    )
                    return [], False
                for review in reviews or []:
                    submitted = parse_time(review.get("submitted_at"))
                    if submitted is not None and submitted > since:
                        new_review = review
                        break

            new_comment = None
            if self.poll_comments and comment_fetches < self.max_items_per_check:
                comment_fetches += 1
                comment = await self.api.get_latest_comment(
                    repo, number, since_param
                )
                if comment is None and self.api.last_error:
                    # 评论请求失败：整轮作废，保留水位线下轮重试，避免漏掉这条评论
                    logger.warning(
                        "获取 %s#%s 的评论失败（%s），本轮保留水位线",
                        repo,
                        number,
                        self.api.last_error,
                    )
                    return [], False
                comment_time = parse_time((comment or {}).get("created_at"))
                if comment and comment_time is not None and comment_time > since:
                    new_comment = comment

            # 关闭 / 合并时先查出“是谁关的”，评论 + 关闭同时发生也能用上
            closer = None
            if state_action is not None and event_fetches < self.max_items_per_check:
                event_fetches += 1
                issue_events = await self.api.list_issue_events(repo, number)
                if issue_events:
                    closer = self._find_closer(issue_events, since)

            if new_review is not None:
                action = "review"
                body = str(new_review.get("body") or "").strip()
                state = str(new_review.get("state") or "").upper()
                if not body:
                    body = {
                        "APPROVED": "审查通过，没有留下评论。",
                        "CHANGES_REQUESTED": "请求修改，没有留下评论。",
                        "COMMENTED": "留下了审查意见。",
                    }.get(state, "提交了一次审查。")
                body_label = "审查"
                actor = str(
                    (new_review.get("user") or {}).get("login") or actor
                )
                url = str(new_review.get("html_url") or url)
            elif new_comment is not None:
                comment_author = str(
                    (new_comment.get("user") or {}).get("login") or actor
                )
                body = str(new_comment.get("body") or "")
                body_label = "评论"
                actor = comment_author
                url = str(new_comment.get("html_url") or url)
                # 同一个窗口里既评论又关闭/合并：状态优先，评论作为正文一起带上
                if state_action is not None:
                    action = state_action
                    if closer:
                        actor = closer
                        if comment_author != closer:
                            body_label = f"评论（{comment_author}）"
                else:
                    action = "comment"
            elif is_new:
                action = "opened"
            elif state_action is not None:
                # 关闭 / 合并本身就是要推送的信息，不再重复贴原始描述
                action = state_action
                body = ""
                body_label = "状态"
                if closer:
                    actor = closer
            else:
                # 2) 其它更新：用 issue events 描述具体变化；
                #    拿不到具体变化就不推，避免把原始描述当成“本次更新”
                description = None
                if event_fetches < self.max_items_per_check:
                    event_fetches += 1
                    issue_events = await self.api.list_issue_events(repo, number)
                    if issue_events is None:
                        logger.warning(
                            "获取 %s#%s 的事件失败（%s），跳过本条更新",
                            repo,
                            number,
                            self.api.last_error or "未知原因",
                        )
                    else:
                        description = self._describe_recent_events(issue_events, since)
                if not description:
                    logger.debug("跳过 %s#%s 的更新：无法确定具体变化", repo, number)
                    continue
                body = description
                body_label = "变更内容"

            events.append(
                {
                    "kind": kind,
                    "repo": repo,
                    "action": action,
                    "actor": actor,
                    # 评论 / 审查 / 关闭类事件：操作人 → 原作者
                    # （操作人就是原作者时，渲染层会自动退化成单头像）
                    "target": (
                        author
                        if action
                        in {
                            "review",
                            "review_comment",
                            "comment",
                            "comment_edited",
                            "closed",
                            "merged",
                            "reopened",
                        }
                        else ""
                    ),
                    "title": str(item.get("title") or ""),
                    "number": number,
                    "body": body,
                    "body_label": body_label,
                    "url": url,
                }
            )

        return events, True

    # ------------------------------------------------------------------ 发送

    async def _deliver(
        self, event: dict[str, Any], subscribers: list[str]
    ) -> None:
        if not subscribers:
            return

        image: str | None = None
        if self.send_card_image:
            image = await self._render_card_image(event)

        plain = None
        if not image:
            plain = cards.render_event(
                event,
                markdown=False,
                line_limit=self.comment_line_limit,
                max_commits=self.max_commits_per_card,
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
        if self.card_style == "ling":
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
                        line_limit=self.comment_line_limit,
                        max_commits=self.max_commits_per_card,
                        width=self.card_width,
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
                line_limit=self.comment_line_limit,
                max_commits=self.max_commits_per_card,
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
