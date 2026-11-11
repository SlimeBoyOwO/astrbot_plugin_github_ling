"""把 GitHub Webhook payload 规范化成插件内部的事件字典。

事件字典结构::

    {
        "kind": "commit" | "issue" | "pr",
        "repo": "owner/repo",
        "action": "opened" | "closed" | "merged" | "comment" | ...,
        "actor": "触发人 login",
        "target": "对方 login（审查类事件：PR 作者）",  # 可选
        "title": "Issue/PR 标题",          # issue / pr
        "number": 123,                       # issue / pr
        "body": "评论或描述全文",            # 渲染时再截取前 N 行
        "body_label": "评论" | "描述" | "审查内容",
        "url": "https://...",
        "branch": "main",                    # commit（push）事件
        "commits": [{"sha", "subject", "url", "author"}],
    }
"""

from __future__ import annotations

from typing import Any

from .utils import first_line

ISSUE_ACTIONS = {"opened", "closed", "reopened", "edited"}
PR_ACTIONS = {"opened", "closed", "reopened", "edited"}


def _login(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("login") or user.get("name") or "")
    return ""


def _actor(sender: Any) -> str:
    return _login(sender) or "未知"


def _review_state_text(state: Any) -> str:
    return {
        "approved": "审查通过，没有留下评论。",
        "changes_requested": "请求修改，没有留下评论。",
        "commented": "留下了审查意见。",
    }.get(str(state or "").lower(), "提交了一次审查。")


def _base_event(
    repo: str,
    kind: str,
    action: str,
    actor: str,
    *,
    title: str = "",
    number: int | None = None,
    body: str = "",
    body_label: str = "描述",
    url: str = "",
    target: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "repo": repo,
        "action": action,
        "actor": actor or "未知",
        "title": title,
        "number": number,
        "target": target,
        "body": body,
        "body_label": body_label,
        "url": url,
    }


def _build_push(repo: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    commits_payload = payload.get("commits")
    if not isinstance(commits_payload, list) or not commits_payload:
        return []

    ref = str(payload.get("ref") or "")
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None

    commits = []
    for commit in commits_payload:
        if not isinstance(commit, dict):
            continue
        commits.append(
            {
                "sha": str(commit.get("id") or ""),
                "subject": first_line(commit.get("message")),
                "url": str(commit.get("url") or ""),
                "author": _login(commit.get("author")) or "未知",
            }
        )
    if not commits:
        return []

    pusher = payload.get("pusher")
    actor = (
        (pusher or {}).get("name")
        if isinstance(pusher, dict)
        else ""
    ) or _actor(payload.get("sender"))

    event = _base_event(repo, "commit", "push", str(actor))
    event["branch"] = branch
    event["commits"] = commits
    event["url"] = str(payload.get("compare") or commits[0].get("url") or "")
    return [event]


def _build_issue(
    repo: str,
    action: Any,
    issue: Any,
    sender: Any,
) -> list[dict[str, Any]]:
    if not isinstance(issue, dict) or action not in ISSUE_ACTIONS:
        return []
    return [
        _base_event(
            repo,
            "issue",
            str(action),
            _actor(sender) if sender else _login(issue.get("user")),
            title=str(issue.get("title") or ""),
            number=issue.get("number"),
            body=str(issue.get("body") or "")
            if action in {"opened", "edited"}
            else "",
            body_label="描述",
            url=str(issue.get("html_url") or ""),
            target=_login(issue.get("user")),
        )
    ]


def _build_pull_request(
    repo: str,
    action: Any,
    pull_request: Any,
    sender: Any,
) -> list[dict[str, Any]]:
    if not isinstance(pull_request, dict) or action not in PR_ACTIONS:
        return []

    normalized_action = str(action)
    if action == "closed" and pull_request.get("merged"):
        normalized_action = "merged"

    return [
        _base_event(
            repo,
            "pr",
            normalized_action,
            _actor(sender) if sender else _login(pull_request.get("user")),
            title=str(pull_request.get("title") or ""),
            number=pull_request.get("number"),
            body=str(pull_request.get("body") or "")
            if action in {"opened", "edited"}
            else "",
            body_label="描述",
            url=str(pull_request.get("html_url") or ""),
            target=_login(pull_request.get("user")),
        )
    ]


def _build_issue_comment(
    repo: str,
    action: Any,
    issue: Any,
    comment: Any,
    sender: Any,
) -> list[dict[str, Any]]:
    if not isinstance(issue, dict) or not isinstance(comment, dict):
        return []
    if action not in {"created", "edited"}:
        return []

    kind = "pr" if "pull_request" in issue else "issue"
    label = "评论"
    return [
        _base_event(
            repo,
            kind,
            "comment" if action == "created" else "comment_edited",
            _actor(sender) if sender else _login(comment.get("user")),
            title=str(issue.get("title") or ""),
            number=issue.get("number"),
            body=str(comment.get("body") or ""),
            body_label=label,
            url=str(comment.get("html_url") or issue.get("html_url") or ""),
            # PR 上的评论：卡片要显示 作者 → 评论人
            target=_login(issue.get("user")),
        )
    ]


def _build_pr_review_comment(
    repo: str,
    action: Any,
    pull_request: Any,
    comment: Any,
    sender: Any,
) -> list[dict[str, Any]]:
    if not isinstance(pull_request, dict) or not isinstance(comment, dict):
        return []
    if action != "created":
        return []
    return [
        _base_event(
            repo,
            "pr",
            "review_comment",
            _actor(sender) if sender else _login(comment.get("user")),
            title=str(pull_request.get("title") or ""),
            number=pull_request.get("number"),
            body=str(comment.get("body") or ""),
            body_label="审查评论",
            url=str(comment.get("html_url") or pull_request.get("html_url") or ""),
            target=_login(pull_request.get("user")),
        )
    ]


def _build_pr_review(
    repo: str,
    action: Any,
    pull_request: Any,
    review: Any,
    sender: Any,
) -> list[dict[str, Any]]:
    if not isinstance(pull_request, dict) or not isinstance(review, dict):
        return []
    if action != "submitted":
        return []
    return [
        _base_event(
            repo,
            "pr",
            "review",
            _actor(sender) if sender else _login(review.get("user")),
            title=str(pull_request.get("title") or ""),
            number=pull_request.get("number"),
            body=str(review.get("body") or "")
            or _review_state_text(review.get("state")),
            body_label="审查内容",
            url=str(review.get("html_url") or pull_request.get("html_url") or ""),
            target=_login(pull_request.get("user")),
        )
    ]


def build_events(event_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """按事件类型生成零个或多个规范化事件。"""
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return []
    repo = str(repository.get("full_name") or "")
    if not repo:
        return []

    if event_type == "push":
        return _build_push(repo, payload)
    if event_type == "issues":
        return _build_issue(repo, payload.get("action"), payload.get("issue"), payload.get("sender"))
    if event_type == "issue_comment":
        return _build_issue_comment(
            repo,
            payload.get("action"),
            payload.get("issue"),
            payload.get("comment"),
            payload.get("sender"),
        )
    if event_type == "pull_request":
        return _build_pull_request(
            repo,
            payload.get("action"),
            payload.get("pull_request"),
            payload.get("sender"),
        )
    if event_type == "pull_request_review_comment":
        return _build_pr_review_comment(
            repo,
            payload.get("action"),
            payload.get("pull_request"),
            payload.get("comment"),
            payload.get("sender"),
        )
    if event_type == "pull_request_review":
        return _build_pr_review(
            repo,
            payload.get("action"),
            payload.get("pull_request"),
            payload.get("review"),
            payload.get("sender"),
        )
    return []
