"""纯文本小工具（不依赖 AstrBot）。

通知卡片、Issue 草稿、聊天记录清洗都用到这些函数。
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"[ \t　]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


def head_lines(text: str | None, limit: int) -> list[str]:
    """按行切分并只保留前 ``limit`` 行。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return lines[: max(0, int(limit))]


def squeeze_blank_lines(text: str | None) -> str:
    """去掉连续 3 行以上的空行，聊天记录 / 日志里很常见。"""
    out: list[str] = []
    blank = 0
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip():
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 2:
                out.append("")
    return "\n".join(out).strip("\n")


def normalize_spaces(text: str | None) -> str:
    """把连续空白压成一个空格（标题清洗用）。"""
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def strip_control(text: str | None) -> str:
    """去掉控制字符：QQ 消息里偶尔夹带，会让卡片 / JSON 出问题。"""
    return _CONTROL_RE.sub("", str(text or ""))


def truncate(text: str | None, limit: int, suffix: str = "…") -> tuple[str, bool]:
    """按字符数截断，返回 ``(文本, 是否发生截断)``。"""
    value = str(text or "")
    limit = max(0, int(limit))
    if limit and len(value) > limit:
        return value[: max(0, limit - len(suffix))] + suffix, True
    return value, False


def human_size(size: int) -> str:
    """字节数转人类可读大小。"""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
