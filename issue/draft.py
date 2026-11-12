"""Issue 草稿：先生成、再确认、最后发布。

草稿只放在内存里（含原始聊天素材，方便换模板重新生成），默认 15 分钟过期。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.texts import head_lines, truncate
from .context import ChatContext

DRAFT_TTL_SECONDS = 15 * 60
MAX_DRAFTS = 128


@dataclass(slots=True)
class IssueDraft:
    repo: str
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    template_name: str = ""
    template_index: int = 1
    images: list[str] = field(default_factory=list)
    # 保留原始素材，支持 /ghissue tpl N 重新生成
    chat: ChatContext | None = None
    instruction: str = ""
    umo: str = ""
    user_id: str = ""
    created_at: float = field(default_factory=time.time)
    model_note: str = ""

    def is_expired(self, ttl: int = DRAFT_TTL_SECONDS) -> bool:
        return time.time() - self.created_at > ttl


class DraftStore:
    """按 (会话, 用户) 保存待确认草稿。"""

    def __init__(self, ttl: int = DRAFT_TTL_SECONDS, max_items: int = MAX_DRAFTS):
        self.ttl = ttl
        self.max_items = max_items
        self._items: dict[tuple[str, str], IssueDraft] = {}

    def _purge(self) -> None:
        expired = [
            key for key, draft in self._items.items() if draft.is_expired(self.ttl)
        ]
        for key in expired:
            self._items.pop(key, None)
        if len(self._items) > self.max_items:
            oldest = sorted(self._items.items(), key=lambda pair: pair[1].created_at)
            for key, _ in oldest[: len(self._items) - self.max_items]:
                self._items.pop(key, None)

    def put(self, draft: IssueDraft) -> None:
        self._purge()
        self._items[(draft.umo, draft.user_id)] = draft

    def get(self, umo: str, user_id: str) -> IssueDraft | None:
        self._purge()
        draft = self._items.get((umo, user_id))
        if draft and draft.is_expired(self.ttl):
            self._items.pop((umo, user_id), None)
            return None
        return draft

    def pop(self, umo: str, user_id: str) -> IssueDraft | None:
        self._purge()
        return self._items.pop((umo, user_id), None)

    def clear(self) -> None:
        self._items.clear()


def format_preview(
    draft: IssueDraft,
    *,
    require_confirm: bool = True,
    max_lines: int = 40,
) -> str:
    """把草稿渲染成一条预览消息。"""
    body = draft.body.strip() or "（正文为空）"
    preview_lines = head_lines(body, max_lines)
    preview = "\n".join(preview_lines)
    omitted = len(head_lines(body, 10**6)) - len(preview_lines)
    if omitted > 0:
        preview += f"\n…（其余 {omitted} 行在创建后可见）"

    lines = [
        "🧾 待确认的 Issue",
        f"仓库：{draft.repo}",
        f"模板：{draft.template_name or '（无）'}",
        f"标题：{draft.title}",
        f"标签：{', '.join(draft.labels) if draft.labels else '（无）'}",
        "────────────",
        preview,
        "────────────",
    ]
    if draft.images:
        lines.append(f"📎 截图：{len(draft.images)} 张（创建时一并上传）")
    if draft.model_note:
        lines.append(f"💡 {draft.model_note}")
    if require_confirm:
        lines.append("发送 /ghissue ok 创建；/ghissue cancel 取消")
        index = draft.template_index or 1
        lines.append(
            f"换模板重新生成：/ghissue tpl 序号（当前 {index}，/ghissue list 查看全部）"
        )
    return "\n".join(lines)


def short_title(title: str, limit: int = 80) -> str:
    value, _ = truncate(" ".join(str(title or "").split()), limit)
    return value
