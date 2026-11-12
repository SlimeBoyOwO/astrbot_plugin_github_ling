"""时间处理：统一使用带时区的 UTC，避免本地时区与 GitHub 时间混用。

本模块不依赖 AstrBot，便于离线测试。
"""

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


def format_iso(value: datetime) -> str:
    """把 datetime 格式化成 GitHub 风格的 UTC 时间字符串。"""
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
