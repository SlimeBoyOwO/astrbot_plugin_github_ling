"""与 AstrBot 无关的通用小工具，便于单独测试。"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串，形如 2026-11-06T01:23:45Z。"""
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    """把 GitHub 的时间戳解析为带 UTC 时区的 datetime。

    GitHub 返回的时间形如 ``2026-11-06T01:23:45Z``；这里统一成 aware datetime，
    避免本地时区 / UTC 混用导致的“漏报 8 小时”问题。
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def short_sha(sha: str | None) -> str:
    """提交哈希的短形式。"""
    return (str(sha or ""))[:7]


def first_line(text: str | None) -> str:
    """取多行文本的第一行（提交信息用）。"""
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        if line.strip():
            return line.strip()
    return ""


def first_lines(
    text: str | None,
    limit: int = 10,
    max_line_chars: int = 300,
) -> tuple[str, int, int]:
    """截取文本的前 ``limit`` 行。

    Returns:
        ``(excerpt, total_lines, shown_lines)``。
        ``excerpt`` 为要展示的文本，``total_lines`` 为去掉首尾空行后的总行数，
        ``shown_lines`` 为实际展示的行数（用于提示“其余 N 行已省略”）。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in raw.split("\n")]

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return "", 0, 0

    limit = max(1, int(limit or 1))
    shown = lines[:limit]
    shown = [
        line if len(line) <= max_line_chars else line[: max_line_chars - 1] + "…"
        for line in shown
    ]
    return "\n".join(shown), len(lines), len(shown)
