"""收集 QQ 消息里的「证据」：引用内容、合并转发的聊天记录、截图。

QQ（OneBot）里一条消息可能长这样：

* 引用别人的消息（可能是一条日志 / 报错截图 / 合并转发的聊天记录）
* 直接把合并转发的聊天记录发出来
* 直接带几张截图

AstrBot 自带的 ``extract_quoted_message_*`` 已经处理好了「引用」这一层，
包括引用里再套合并转发的递归拉取；本模块补充：

* 当前消息里直接出现的合并转发（``Forward`` 组件）
* 当前消息里直接的图片
* 文本清洗与长度限制
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import astrbot.api.message_components as Comp
from astrbot.api import logger

from ..core.texts import normalize_spaces, squeeze_blank_lines, strip_control

try:  # AstrBot >= 4.2x
    from astrbot.core.utils.quoted_message import (
        extract_quoted_message_images,
        extract_quoted_message_text,
    )
except Exception:  # pragma: no cover - 老版本 AstrBot 没有这组工具
    extract_quoted_message_text = None  # type: ignore[assignment]
    extract_quoted_message_images = None  # type: ignore[assignment]

try:
    from astrbot.core.utils.quoted_message.chain_parser import OneBotPayloadParser
    from astrbot.core.utils.quoted_message.onebot_client import OneBotClient
except Exception:  # pragma: no cover
    OneBotPayloadParser = None  # type: ignore[assignment]
    OneBotClient = None  # type: ignore[assignment]


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


@dataclass(slots=True)
class ChatContext:
    """一次 Issue 生成用到的聊天素材。"""

    text: str = ""
    images: list[str] = field(default_factory=list)
    quoted: bool = False
    forwarded: bool = False
    truncated: bool = False
    image_limit_hit: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.images


def _is_image_ref(value: Any) -> bool:
    """判断字符串是否可能指向一张图片。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    lower = text.lower()
    if lower.startswith(("http://", "https://", "base64://", "data:image/")):
        return True
    if lower.startswith("file://"):
        return True
    if lower.endswith(_IMAGE_SUFFIXES):
        return True
    try:
        return os.path.exists(text)
    except OSError:
        return False


def _file_uri_to_path(value: str) -> str:
    path = unquote(urlparse(value).path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return path


def _normalize_image_ref(value: str) -> str:
    """把 file:// 换成普通路径，模型与上传逻辑都更容易处理。"""
    text = str(value or "").strip()
    if text.lower().startswith("file://"):
        return _file_uri_to_path(text)
    return text


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


async def collect_comment_images(event: Any, max_images: int = 4) -> list[str]:
    """只收集图片：引用消息、当前消息里的图片与合并转发中的图片。

    评论不需要整理成 Issue 正文，所以不走完整的长文本收集，只提取图片引用。
    """
    images: list[str] = []

    if extract_quoted_message_images is not None and max_images:
        try:
            images.extend(await extract_quoted_message_images(event) or [])
        except Exception as exc:
            logger.debug("解析引用消息中的图片失败: %s", exc)

    collector = ChatContextCollector(max_chars=500, max_images=max_images)
    for comp in list(event.get_messages() or []):
        if isinstance(comp, Comp.Image):
            ref = await collector._component_image_ref(comp)
            if ref:
                images.append(ref)
        elif isinstance(comp, Comp.Forward):
            _, forward_images = await collector._read_forward(event, comp)
            images.extend(forward_images)
        elif isinstance(comp, Comp.Nodes):
            _, node_images = collector._read_nodes(comp.nodes)
            images.extend(node_images)
        elif isinstance(comp, Comp.Node):
            _, node_images = collector._read_nodes([comp])
            images.extend(node_images)

    unique = _dedupe(
        [
            _normalize_image_ref(item)
            for item in images
            if _is_image_ref(item)
        ]
    )
    if max_images:
        return unique[:max_images]
    return []


class ChatContextCollector:
    """从消息事件里抽取文本与图片。"""

    def __init__(
        self,
        max_chars: int = 6000,
        max_images: int = 4,
    ) -> None:
        self.max_chars = max(500, int(max_chars))
        self.max_images = max(0, int(max_images))

    async def collect(
        self,
        event: Any,
        instruction: str = "",
    ) -> ChatContext:
        texts: list[str] = []
        images: list[str] = []
        quoted = False
        forwarded = False

        # 1) 引用的消息（AstrBot 内置解析，含引用里的合并转发）
        if extract_quoted_message_text is not None:
            try:
                quoted_text = await extract_quoted_message_text(event)
            except Exception as exc:
                logger.debug("解析引用消息失败: %s", exc)
                quoted_text = None
            if quoted_text and quoted_text.strip():
                quoted = True
                texts.append("【引用的消息】\n" + quoted_text.strip())

        if extract_quoted_message_images is not None and self.max_images:
            try:
                quoted_images = await extract_quoted_message_images(event)
            except Exception as exc:
                logger.debug("解析引用消息中的图片失败: %s", exc)
                quoted_images = []
            for ref in quoted_images or []:
                if _is_image_ref(ref):
                    images.append(ref)

        # 2) 当前消息本身的组件
        own_text_parts: list[str] = []
        for comp in list(event.get_messages() or []):
            if isinstance(comp, Comp.Plain):
                if comp.text:
                    own_text_parts.append(comp.text)
            elif isinstance(comp, Comp.Image):
                ref = await self._component_image_ref(comp)
                if ref:
                    images.append(ref)
            elif isinstance(comp, Comp.Forward):
                forward_text, forward_images = await self._read_forward(event, comp)
                if forward_text:
                    forwarded = True
                    texts.append("【聊天记录】\n" + forward_text)
                images.extend(forward_images)
            elif isinstance(comp, Comp.Nodes):
                node_text, node_images = self._read_nodes(comp.nodes)
                if node_text:
                    forwarded = True
                    texts.append("【聊天记录】\n" + node_text)
                images.extend(node_images)
            elif isinstance(comp, Comp.Node):
                node_text, node_images = self._read_nodes([comp])
                if node_text:
                    forwarded = True
                    texts.append("【聊天记录】\n" + node_text)
                images.extend(node_images)

        own_text = self._clean_own_text("".join(own_text_parts), instruction)
        if own_text:
            sender = ""
            try:
                sender = normalize_spaces(event.get_sender_name())
            except Exception:
                sender = ""
            prefix = f"【发送者{('：' + sender) if sender else ''}】"
            texts.append(f"{prefix}\n{own_text}")

        # 3) 合并、清洗、截断
        text = squeeze_blank_lines(strip_control("\n\n".join(texts)))
        truncated = False
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + "\n…（内容过长，已截断）"
            truncated = True

        unique_images = _dedupe(
            [
                _normalize_image_ref(item)
                for item in images
                if _is_image_ref(item)
            ]
        )
        image_limit_hit = len(unique_images) > self.max_images
        if self.max_images:
            unique_images = unique_images[: self.max_images]
        else:
            unique_images = []

        return ChatContext(
            text=text,
            images=unique_images,
            quoted=quoted,
            forwarded=forwarded,
            truncated=truncated,
            image_limit_hit=image_limit_hit,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _clean_own_text(text: str, instruction: str) -> str:
        """去掉命令本身与用户已经单独作为「补充说明」传入的部分。"""
        value = (text or "").strip()
        for command in ("/ghissue", "ghissue", "/gh发issue", "gh发issue"):
            if value.lower().startswith(command.lower()):
                value = value[len(command) :].strip()
                break
        if instruction and value.endswith(instruction):
            value = value[: -len(instruction)].strip()
        return value

    @staticmethod
    async def _component_image_ref(comp: Any) -> str | None:
        for candidate in (comp.url, comp.file, comp.path):
            if _is_image_ref(candidate):
                return str(candidate).strip()
        convert = getattr(comp, "convert_to_file_path", None)
        if callable(convert):
            try:
                path = await convert()
                if path and _is_image_ref(path):
                    return str(path)
            except Exception as exc:
                logger.debug("图片转存失败，已跳过: %s", exc)
        return None

    async def _read_forward(
        self,
        event: Any,
        comp: Any,
    ) -> tuple[str, list[str]]:
        """读取当前消息里直接出现的合并转发。"""
        if OneBotClient is None or OneBotPayloadParser is None:
            return "", []
        forward_id = str(getattr(comp, "id", "") or "").strip()
        if not forward_id:
            return "", []
        try:
            client = OneBotClient(event)
            payload = await client.get_forward_msg(forward_id)
        except Exception as exc:
            logger.debug("拉取合并转发消息失败: %s", exc)
            return "", []
        if not payload:
            return "", []
        try:
            parsed = OneBotPayloadParser().parse_get_forward_payload(payload)
        except Exception as exc:
            logger.debug("解析合并转发消息失败: %s", exc)
            return "", []
        return str(parsed.get("text") or ""), [
            ref for ref in parsed.get("image_refs") or [] if _is_image_ref(ref)
        ]

    def _read_nodes(self, nodes: Any) -> tuple[str, list[str]]:
        """读取 Nodes / Node 组件（部分适配器会直接给出结构化的聊天记录）。"""
        lines: list[str] = []
        images: list[str] = []

        def walk(node_list: Any) -> None:
            for node in list(node_list or []):
                name = (
                    getattr(node, "name", "")
                    or getattr(node, "uin", "")
                    or "未知成员"
                )
                content = getattr(node, "content", None) or []
                parts: list[str] = []
                for item in content:
                    if isinstance(item, Comp.Plain):
                        if item.text:
                            parts.append(item.text)
                    elif isinstance(item, Comp.Image):
                        parts.append("[图片]")
                        for candidate in (item.url, item.file, item.path):
                            if _is_image_ref(candidate):
                                images.append(str(candidate).strip())
                                break
                    elif isinstance(item, Comp.Reply):
                        nested = getattr(item, "message_str", "") or ""
                        if nested:
                            parts.append(nested)
                    elif isinstance(item, Comp.Forward):
                        parts.append("[嵌套的聊天记录]")
                text = " ".join(parts).strip()
                if text:
                    lines.append(f"{name}: {text}")
                if isinstance(node, Comp.Node) and isinstance(
                    getattr(node, "content", None), list
                ):
                    # Node 内部还可能再嵌 Node
                    nested_nodes = [
                        child
                        for child in getattr(node, "content", [])
                        if isinstance(child, Comp.Node)
                    ]
                    if nested_nodes:
                        walk(nested_nodes)

        walk(nodes)
        return "\n".join(lines), images
