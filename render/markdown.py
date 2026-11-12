"""把规范化后的 GitHub 事件渲染成卡片文本。

图片卡片默认由 ``render/card.py`` 手绘（见 ``card_style=ling``）；
本模块负责两种文本输出，既是 ``card_style=markdown`` 的正文，也是兜底：

* ``markdown=True``  —— 交给 AstrBot 的 ``text_to_image`` 渲染成图片卡片；
* ``markdown=False`` —— 兜底的纯文本，图片渲染不可用时直接发消息。

本模块不依赖 AstrBot，便于离线测试。
"""

from __future__ import annotations

from typing import Any

from . import rich_body
from ..core.texts import first_lines, short_sha

ACTION_LABELS = {
    "opened": "新建",
    "reopened": "重新打开",
    "closed": "关闭",
    "merged": "已合并",
    "edited": "已编辑",
    "comment": "新评论",
    "comment_edited": "评论已编辑",
    "review": "新审查",
    "review_comment": "新审查评论",
    "push": "代码推送",
    "updated": "有更新",
}

KIND_LABELS = {"issue": "Issue", "pr": "PR"}

RULE_MARKDOWN = "---"
RULE_PLAIN = "-" * 40


def _one_line(value: Any) -> str:
    """把可能带换行的标题压成一行。"""
    return " ".join(str(value or "").replace("\r", "\n").split("\n")).strip()


def _action_label(action: str | None) -> str:
    return ACTION_LABELS.get(action or "", action or "更新")


def _quote_body(body: str) -> list[str]:
    """把正文转成 Markdown 引用块，空行也保留在引用内。"""
    # 避免正文里不成对的代码围栏破坏整张卡片的排版
    safe = body.replace("```", "'''")
    lines = safe.split("\n")
    return [f"> {line}" if line else ">" for line in lines]


def _render_commit_event(
    event: dict[str, Any],
    *,
    markdown: bool,
    max_commits: int,
) -> str:
    repo = event.get("repo") or "未知仓库"
    commits = list(event.get("commits") or [])
    branch = event.get("branch")
    actor = event.get("actor")

    shown = commits[: max(1, max_commits)]
    hidden = len(commits) - len(shown)

    if markdown:
        # 卡片图片由 AstrBot 文转图渲染，本地字体未必包含 emoji，
        # 因此 Markdown 版本只用文字，emoji 仅保留在纯文本兜底里。
        lines = [f"# {repo} · 新提交", ""]

        meta = [f"**提交** {len(commits)} 个"]
        if branch:
            meta.insert(0, f"**分支** {branch}")
        if actor:
            meta.append(f"**作者** {actor}")
        lines.append("　".join(meta))
        lines.append("")
        lines.append(RULE_MARKDOWN)
        lines.append("")

        for commit in shown:
            sha = short_sha(commit.get("sha"))
            subject = _one_line(commit.get("subject")) or "(无提交信息)"
            lines.append(f"### {sha} · {subject}")
            lines.append("")

        if hidden > 0:
            lines.append(f"…… 以及另外 {hidden} 个提交")
            lines.append("")

        url = event.get("url") or (commits[0].get("url") if commits else "")
        if url:
            lines.append(RULE_MARKDOWN)
            lines.append("")
            lines.append(f"链接：{url}")
        return "\n".join(lines).strip()

    lines = [f"{repo} · 提交更新"]
    meta_plain = [f"提交: {len(commits)} 个"]
    if branch:
        meta_plain.insert(0, f"分支: {branch}")
    if actor:
        meta_plain.append(f"作者: {actor}")
    lines.append(" | ".join(meta_plain))
    lines.append(RULE_PLAIN)

    for commit in shown:
        sha = short_sha(commit.get("sha"))
        subject = _one_line(commit.get("subject")) or "(无提交信息)"
        lines.append(f"{sha}  {subject}")

    if hidden > 0:
        lines.append(f"…… 以及另外 {hidden} 个提交")

    url = event.get("url") or (commits[0].get("url") if commits else "")
    if url:
        lines.append(RULE_PLAIN)
        lines.append(url)
    return "\n".join(lines).strip()


def _render_thread_event(
    event: dict[str, Any],
    *,
    markdown: bool,
    line_limit: int,
) -> str:
    repo = event.get("repo") or "未知仓库"
    kind = KIND_LABELS.get(event.get("kind") or "", "Issue")
    number = event.get("number")
    title = _one_line(event.get("title")) or "(无标题)"
    action = _action_label(event.get("action"))
    actor = event.get("actor") or "未知"
    body_label = event.get("body_label") or "内容"
    url = event.get("url") or ""

    body_source = event.get("body")
    if not markdown and body_source:
        # 纯文本兜底时把 Markdown 记号去掉，避免出现 ** 和反引号
        body_source = rich_body.plain_text(str(body_source))

    excerpt, total_lines, shown_lines = first_lines(body_source, limit=line_limit)

    if markdown:
        lines = [f"# {title}", ""]
        type_text = f"**类型** {kind} #{number}" if number else f"**类型** {kind}"
        lines.append(f"**仓库** {repo}　{type_text}")
        lines.append(f"**事件** {action}　**操作人** {actor}")
        lines.append("")
        lines.append(RULE_MARKDOWN)
        lines.append("")

        if excerpt:
            if total_lines > shown_lines:
                label = f"**{body_label}**（前 {shown_lines} 行，共 {total_lines} 行）"
            else:
                label = f"**{body_label}**"
            lines.append(label)
            lines.append("")
            lines.extend(_quote_body(excerpt))
            if total_lines > shown_lines:
                lines.append(">")
                lines.append(f"> …… 其余 {total_lines - shown_lines} 行已省略")
        else:
            lines.append("（暂无内容）")

        if url:
            lines.append("")
            lines.append(RULE_MARKDOWN)
            lines.append("")
            lines.append(f"链接：{url}")
        return "\n".join(lines).strip()

    head = f"【{kind} #{number}】{title}" if number else f"【{kind}】{title}"
    lines = [head]
    lines.append(f"仓库: {repo} | 事件: {action} | 操作人: {actor}")
    lines.append(RULE_PLAIN)

    if excerpt:
        if total_lines > shown_lines:
            lines.append(
                f"{body_label}（前 {shown_lines} 行，共 {total_lines} 行，其余已省略）:"
            )
        else:
            lines.append(f"{body_label}:")
        lines.append(excerpt)
    else:
        lines.append("（暂无内容）")

    if url:
        lines.append(RULE_PLAIN)
        lines.append(url)
    return "\n".join(lines).strip()


def render_event(
    event: dict[str, Any],
    *,
    markdown: bool = True,
    line_limit: int = 10,
    max_commits: int = 5,
) -> str:
    """把事件渲染成卡片文本。

    Args:
        event: ``webhook_events`` / 轮询逻辑产出的规范化事件。
        markdown: True 输出 Markdown（用于文转图），False 输出纯文本兜底。
        line_limit: PR/Issue 正文最多展示的行数。
        max_commits: 一张提交卡片最多展示的提交数。
    """
    if (event.get("kind") or "") == "commit":
        return _render_commit_event(
            event, markdown=markdown, max_commits=max_commits
        )
    return _render_thread_event(
        event, markdown=markdown, line_limit=line_limit
    )
