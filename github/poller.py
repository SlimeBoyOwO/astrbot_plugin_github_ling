"""轮询模式：定期检查已绑定仓库，把新事件交给发送层。

水位线（watermark）按仓库保存。**请求失败的一轮绝不推进水位线**，
下一轮从旧位置重试，避免网络抖动期间漏掉评论 / 审查。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from ..core.config import PluginConfig
from ..core.texts import first_line
from ..core.timeutil import now_iso, parse_time
from .client import GitHubAPI

# 推进水位线时回退几秒，避免 GitHub 秒级时间戳与查询快照之间的边界漏报
WATERMARK_SAFETY_SECONDS = 2

EventDict = dict[str, Any]
DeliverFn = Callable[[EventDict, list[str]], Awaitable[None]]


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


class Poller:
    """仓库更新轮询器。"""

    def __init__(
        self,
        *,
        api: GitHubAPI,
        config: PluginConfig,
        bindings: dict[str, list[str]],
        watermarks: dict[str, str],
        deliver: DeliverFn,
        save_state: Callable[[], None],
        server_now_iso: Callable[[int], str],
    ) -> None:
        self.api = api
        self.config = config
        self.bindings = bindings
        self.watermarks = watermarks
        self._deliver = deliver
        self._save_state = save_state
        self._server_now_iso = server_now_iso
        self._lock = asyncio.Lock()
        self.task: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        if not self.task:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.task = None

    async def run_forever(self) -> None:
        # 稍等插件加载完成，避免与初始化流程抢资源
        await asyncio.sleep(5)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("轮询 GitHub 更新时出错")
            await asyncio.sleep(max(1, self.config.check_interval) * 60)

    # ------------------------------------------------------------------ 主流程

    async def poll_once(self) -> tuple[int, int, int]:
        """检查所有已绑定仓库，返回 ``(检查数, 推送数, 失败数)``。"""
        async with self._lock:
            checked = 0
            sent = 0
            failed = 0

            for repo in list(self.bindings.keys()):
                subscribers = list(self.bindings.get(repo) or [])
                if not subscribers:
                    continue

                checked += 1
                try:
                    events, ok = await self.collect(repo)
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

    async def collect(self, repo: str) -> tuple[list[EventDict], bool]:
        """收集仓库更新。

        Returns:
            ``(events, ok)``。``ok=False`` 表示本轮有请求失败，调用方必须保留
            水位线，下一轮从旧水位线重新检查，避免漏报。
        """
        since = parse_time(self.watermarks.get(repo))
        if since is None:
            self.watermarks[repo] = self._server_now_iso(0)
            return [], True

        since_param = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        events: list[EventDict] = []

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
        config = self.config
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
                and config.poll_comments
                and review_fetches < config.max_items_per_check
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
            if (
                config.poll_comments
                and comment_fetches < config.max_items_per_check
            ):
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
            if (
                state_action is not None
                and event_fetches < config.max_items_per_check
            ):
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
                if event_fetches < config.max_items_per_check:
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

    # ------------------------------------------------------------------ 事件描述

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


def server_now_iso(
    api: GitHubAPI,
    safety_seconds: int = 0,
) -> str:
    """优先使用 GitHub 服务器时间作为水位线，避免本机时钟偏差。

    这是与 ``GitHubAPI.server_now()`` 配套的小工具，放在这里避免主类过于臃肿。
    """
    server_time = api.server_now()
    if server_time is not None:
        target = server_time - timedelta(seconds=max(0, safety_seconds))
        return target.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if safety_seconds:
        local_time = parse_time(now_iso())
        if local_time is not None:
            local_time -= timedelta(seconds=safety_seconds)
            return local_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    return now_iso()
