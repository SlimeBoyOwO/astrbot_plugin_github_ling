"""把「额外推送目标」的三种写法统一成 AstrBot 的会话标识。

会话标识（``unified_msg_origin``，下称 UMO）的格式是
``platform_id:消息类型:会话ID``，例如 ``aiocqhttp:GroupMessage:123456``。

支持的配置写法：

* 完整 UMO：``aiocqhttp:GroupMessage:123456789``
* 简写：``group:123456789`` / ``private:123456789``（也认「群 / 群聊 / 私聊 / 好友」）
* 纯数字：``123456789``，按群聊处理

本模块是纯函数，不依赖 AstrBot，方便离线测试。
"""

from __future__ import annotations

from typing import Any, Iterable

MESSAGE_TYPES = {"GroupMessage", "FriendMessage", "OtherMessage"}

TARGET_KIND_PREFIXES = {
    "group": "GroupMessage",
    "群": "GroupMessage",
    "群聊": "GroupMessage",
    "private": "FriendMessage",
    "friend": "FriendMessage",
    "私聊": "FriendMessage",
    "好友": "FriendMessage",
}


def platform_hint(
    configured: str,
    bindings: dict[str, list[str]],
    platform_ids: Iterable[str] = (),
) -> str:
    """推断简写目标应该用哪个平台适配器。

    优先使用配置项 ``push_platform``，其次取已绑定会话出现过的平台，
    最后退回第一个已加载的平台适配器。
    """
    if configured:
        return configured.strip()
    for subscribers in bindings.values():
        for umo in subscribers:
            parts = str(umo).split(":", 2)
            if len(parts) == 3 and parts[0].strip():
                return parts[0].strip()
    for platform_id in platform_ids:
        value = str(platform_id or "").strip()
        if value:
            return value
    return ""


def normalize_target(raw: Any, platform: str) -> str | None:
    """把一条目标解析成 UMO；无法解析时返回 ``None``。"""
    value = str(raw or "").strip().strip("\"'").strip()
    if not value:
        return None

    if value.count(":") >= 2:
        parts = value.split(":", 2)
        if all(part.strip() for part in parts) and parts[1] in MESSAGE_TYPES:
            return value
        # 不是标准会话标识，继续按简写解析（例如 group:123 只有两段）

    head, _, rest = value.partition(":")
    kind = TARGET_KIND_PREFIXES.get(head.strip().lower())
    if kind:
        rest = rest.strip()
        if not rest or not platform:
            return None
        return f"{platform}:{kind}:{rest}"

    if value.isdigit() and platform:
        return f"{platform}:GroupMessage:{value}"
    return None


def configured_targets(raw_items: Iterable[Any], platform: str) -> list[str]:
    """把配置项 ``push_targets`` 的每一项解析成 UMO（去重、保序）。"""
    targets: list[str] = []
    for item in raw_items:
        target = normalize_target(item, platform)
        if target and target not in targets:
            targets.append(target)
    return targets


def broadcast_targets(
    stored: Iterable[Any],
    configured: Iterable[Any],
    platform: str,
) -> list[str]:
    """所有额外目标：``/ghpush`` 添加的 + 配置里写的。"""
    targets: list[str] = []
    for item in [*stored, *configured]:
        target = normalize_target(item, platform) or str(item or "").strip()
        if target and target not in targets:
            targets.append(target)
    return targets


def merge_targets(subscribers: Iterable[str], extra: Iterable[str]) -> list[str]:
    """本次推送的实际接收者：绑定会话 + 额外目标（去重、保序）。"""
    targets: list[str] = []
    for item in [*subscribers, *extra]:
        if item and item not in targets:
            targets.append(item)
    return targets
