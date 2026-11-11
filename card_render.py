"""用 Pillow 手绘 GitHub 通知卡片（Ling 风格）。

视觉参考 LingChat 的 ``CharacterCard.vue``：

* 米白背景 + 毛玻璃面板（半透明白 + 背景模糊 + 细描边）
* 左侧竖栏：操作人头像 → 作者名 → 类型（Commit / Issue / PR）→ 操作胶囊
* 右侧：类型图标 + 仓库名 + 副标题、大标题、Markdown 渲染后的正文

本模块只依赖 Pillow，不联网、不需要 AstrBot 的文转图服务，
因此在任何服务器上都能稳定出图。字符串内容由 ``cards.py`` 的语义提供。
"""

from __future__ import annotations

import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import rich_body
from .utils import short_sha

# ---------------------------------------------------------------- 版式常量

WIDTH = 960
"""卡片画布宽度（1x 逻辑像素）。"""

SCALE = 2
"""超采样倍率；绘制后缩回 1x，得到抗锯齿边缘。"""

MARGIN = 30
INNER = 26
LEFT_W = 150
LEFT_W_PAIR = 206
RADIUS = 26

# ---------------------------------------------------------------- 配色

BG_TOP = (252, 249, 243)  # 米白
BG_BOTTOM = (243, 236, 226)

PANEL_ALPHA = 208  # 白色毛玻璃
BORDER = (28, 33, 48, 24)

BRAND = (14, 116, 144)  # cyan-700，深青蓝
BRAND_DEEP = (21, 94, 117)  # cyan-800

TEXT = (27, 31, 43)
TEXT_SOFT = (58, 66, 84)
TEXT_MUTED = (123, 133, 152)

ACTION_COLORS = {
    "opened": (14, 116, 144),  # cyan-700
    "reopened": (14, 116, 144),
    "merged": (5, 150, 105),  # emerald-600
    "closed": (225, 29, 72),  # rose-600
    "push": (3, 105, 161),  # sky-700
    "comment": (15, 118, 110),  # teal-700
    "comment_edited": (15, 118, 110),
    "review": (15, 118, 110),
    "review_comment": (15, 118, 110),
    "updated": (100, 116, 139),  # slate-500
    "edited": (100, 116, 139),
}

TYPE_LABELS = {"commit": "Commit", "issue": "Issue", "pr": "PR"}

ACTION_LABELS_EN = {
    "opened": "Open",
    "reopened": "Reopened",
    "closed": "Closed",
    "merged": "Merged",
    "edited": "Edited",
    "comment": "Comment",
    "comment_edited": "Edited",
    "review": "Review",
    "review_comment": "Review",
    "push": "Push",
    "updated": "Update",
}

# ---------------------------------------------------------------- 字体

_FONT_CACHE: dict[tuple[int, bool, bool], Any] = {}

_REGULAR_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "NotoSansCJK-Regular.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "DejaVuSans.ttf",
]
_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.otf",
    "NotoSansCJK-Bold.ttc",
    "DejaVuSans-Bold.ttf",
] + _REGULAR_CANDIDATES
_MONO_CANDIDATES = [
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/SFNSMono.ttf",
    "DejaVuSansMono.ttf",
] + _REGULAR_CANDIDATES


def _data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path())
    except Exception:
        return Path("data")


def _font(size: int, *, bold: bool = False, mono: bool = False):
    """按字号取字体，优先使用系统 CJK 字体。"""
    key = (size, bold, mono)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    data = _data_dir()
    custom = [
        data / ("font-mono.ttf" if mono else "font-bold.ttf" if bold else "font.ttf")
    ]
    candidates = custom + (
        _MONO_CANDIDATES if mono else _BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES
    )
    for path in candidates:
        try:
            font = ImageFont.truetype(str(path), size)
        except (OSError, TypeError, ValueError):
            continue
        _FONT_CACHE[key] = font
        return font

    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# ---------------------------------------------------------------- 绘制工具


def _u(value: float) -> int:
    return int(round(value * SCALE))


def _text_width(text: str, font) -> float:
    try:
        return font.getlength(text)
    except AttributeError:
        return font.getsize(text)[0]


def _line_height(font, size: int) -> int:
    try:
        ascent, descent = font.getmetrics()
        return int(ascent + descent)
    except AttributeError:
        return int(size * 1.35)


def _wrap(text: str, font, max_width: float) -> list[str]:
    """按宽度折行；中文逐字断行，英文尽量在空格处断开。"""
    if not text:
        return []
    if max_width <= 0:
        return [text]

    lines: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        raw = raw.rstrip()
        if not raw:
            lines.append("")
            continue
        remaining = raw
        while remaining:
            if _text_width(remaining, font) <= max_width:
                lines.append(remaining.rstrip())
                break
            low, high, best = 1, len(remaining), 1
            while low <= high:
                mid = (low + high) // 2
                if _text_width(remaining[:mid], font) <= max_width:
                    best = mid
                    low = mid + 1
                else:
                    high = mid - 1
            cut = max(1, best)
            if cut < len(remaining):
                # 英文尽量在单词边界断开
                probe = remaining[:cut]
                space = max(probe.rfind(" "), probe.rfind("\t"))
                if space >= max(1, cut // 2):
                    cut = space + 1
            lines.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
    return lines


def _clamp(lines: list[str], limit: int) -> tuple[list[str], bool]:
    if limit <= 0 or len(lines) <= limit:
        return lines, False
    kept = lines[:limit]
    if kept:
        kept[-1] = kept[-1].rstrip(" 　.,;、") + "…"
    return kept, True


def _ellipsize(text: str, font, max_width: float) -> str:
    if _text_width(text, font) <= max_width:
        return text
    while text and _text_width(text + "…", font) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _draw_tracked_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    center_x: float,
    y: float,
    fill,
    *,
    tracking: float = 1.4,
) -> float:
    """绘制带字间距的居中小字（Pillow 没有 letter-spacing，手动排）。"""
    if not text:
        return 0.0
    widths = [font.getlength(ch) for ch in text]
    gap = tracking * SCALE
    total = sum(widths) + gap * (len(text) - 1)
    x = center_x * SCALE - total / 2
    for char, width in zip(text, widths):
        draw.text((x, y * SCALE), char, font=font, fill=fill)
        x += width + gap
    return total / SCALE


def _rounded(draw: ImageDraw.ImageDraw, box, radius: float, **kwargs) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(
        (_u(x0), _u(y0), _u(x1), _u(y1)), radius=_u(radius), **kwargs
    )


def _rgba(color: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (color[0], color[1], color[2], max(0, min(255, alpha)))


# ---------------------------------------------------------------- 背景


def _build_background(size: tuple[int, int]) -> Image.Image:
    """深色渐变 + 两处品牌色光晕，作为毛玻璃后面的底。"""
    w, h = size
    base = Image.new("RGB", size, BG_TOP)
    draw = ImageDraw.Draw(base)
    for y in range(h):
        ratio = y / max(1, h - 1)
        color = tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3)
        )
        draw.line((0, y, w, y), fill=color)

    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse(
        (-w * 0.35, -h * 0.55, w * 0.62, h * 0.75), fill=_rgba(BRAND, 34)
    )
    gdraw.ellipse(
        (w * 0.62, h * 0.45, w * 1.35, h * 1.5), fill=_rgba((56, 189, 248), 30)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(_u(60)))
    base = Image.alpha_composite(base.convert("RGBA"), glow)
    return base


# ---------------------------------------------------------------- 图标


def _draw_icon(draw: ImageDraw.ImageDraw, kind: str, cx: float, cy: float, r: float, color):
    """在圆形头像里画一个简单的矢量图标（入参均为 1x 逻辑坐标）。"""
    lw = max(2, _u(r * 0.14))

    def _line(a, b):
        draw.line((_u(a[0]), _u(a[1]), _u(b[0]), _u(b[1])), fill=color, width=lw)

    def _circle(x, y, radius, *, fill=None):
        # 小尺寸节点用实心，否则描边会糊成一团
        if fill is None and radius <= 4:
            fill = color
        draw.ellipse(
            (_u(x - radius), _u(y - radius), _u(x + radius), _u(y + radius)),
            outline=None if fill else color,
            fill=fill,
            width=lw,
        )

    if kind == "commit":
        _line((cx - r * 0.85, cy), (cx - r * 0.3, cy))
        _line((cx + r * 0.3, cy), (cx + r * 0.85, cy))
        _circle(cx, cy, r * 0.3)
        return

    if kind == "issue":
        _circle(cx, cy, r * 0.62)
        _circle(cx, cy, r * 0.15, fill=color)
        return

    # PR：分支合并示意图 —— 顶部横线连接两条竖线，底端各一个节点
    left_x, right_x = cx - r * 0.42, cx + r * 0.42
    top_y, bottom_y = cy - r * 0.42, cy + r * 0.52
    node = r * 0.16
    _line((left_x, top_y), (right_x, top_y))
    _line((left_x, top_y), (left_x, bottom_y - node))
    _line((right_x, top_y), (right_x, bottom_y - node))
    _circle(left_x, top_y, node)
    _circle(left_x, bottom_y, node)
    _circle(right_x, bottom_y, node)


# ---------------------------------------------------------------- 内容整理


def _thread_text(event: dict[str, Any]) -> tuple[str, str]:
    """整理 PR/Issue 的正文：返回 (标签, 原始 Markdown)。正文为空时返回空串。"""
    label = str(event.get("body_label") or "内容")
    body = str(event.get("body") or "").strip()
    return label, body


def _commit_items(event: dict[str, Any], max_commits: int) -> tuple[list[tuple[str, str]], int]:
    # GitHub 返回的顺序是旧 → 新；卡片按新 → 旧展示，与标题（最新提交）保持一致
    commits = list(reversed(list(event.get("commits") or [])))
    limit = max(1, max_commits)
    items = [
        (short_sha(c.get("sha")), " ".join(str(c.get("subject") or "(无提交信息)").split()))
        for c in commits[:limit]
    ]
    return items, max(0, len(commits) - len(items))


def _header_parts(event: dict[str, Any]) -> tuple[str, str]:
    repo = str(event.get("repo") or "未知仓库")
    kind = str(event.get("kind") or "")
    if kind == "commit":
        sub = str(event.get("branch") or "")
    else:
        number = event.get("number")
        sub = f"#{number}" if number else ""
    return repo, sub


def _headline(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    if kind == "commit":
        commits = list(event.get("commits") or [])
        if commits:
            return " ".join(str(commits[-1].get("subject") or "").split())
        return "新的提交"
    title = " ".join(str(event.get("title") or "").split())
    return title or "(无标题)"


def _type_label(event: dict[str, Any]) -> str:
    return TYPE_LABELS.get(str(event.get("kind") or ""), "Issue")


def _action_label_en(event: dict[str, Any]) -> str:
    action = str(event.get("action") or "")
    return ACTION_LABELS_EN.get(action, action.title() or "Update")


def _action_color(event: dict[str, Any]) -> tuple[int, int, int]:
    return ACTION_COLORS.get(str(event.get("action") or ""), BRAND)


def _load_avatar(path: str | None) -> Image.Image | None:
    """读取本地头像文件；失败时返回 None，由调用方画占位图。"""
    if not path:
        return None
    try:
        with Image.open(path) as raw:
            return raw.convert("RGB")
    except (OSError, ValueError):
        return None


def _paw_badge(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    """画一个猫爪图标（四个脚趾 + 一个肉垫），用于左上角品牌角标。"""
    box = size * 4
    canvas = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    def _bean(cx: float, cy: float, rx: float, ry: float) -> None:
        draw.ellipse(
            (
                (cx - rx) * box,
                (cy - ry) * box,
                (cx + rx) * box,
                (cy + ry) * box,
            ),
            fill=color,
        )

    # 肉垫
    _bean(0.5, 0.74, 0.30, 0.20)
    # 四个脚趾，按弧度排开，与肉垫留出缝隙
    _bean(0.13, 0.46, 0.105, 0.125)
    _bean(0.37, 0.30, 0.105, 0.125)
    _bean(0.63, 0.30, 0.105, 0.125)
    _bean(0.87, 0.46, 0.105, 0.125)

    return canvas.resize((size, size), Image.LANCZOS).rotate(
        -14, resample=Image.BICUBIC, expand=False
    )


# ---------------------------------------------------------------- 主渲染


class _Card:
    """一次渲染所需的全部版面与绘制状态。"""

    def __init__(self, width: int = WIDTH, scale: int = SCALE):
        self.width = width
        self.scale = scale
        self.h = 0

    # 便捷换算 --------------------------------------------------------
    def u(self, v: float) -> int:
        return int(round(v * self.scale))

    def measure(self, text: str, font) -> float:
        return _text_width(text, font)

    def font(self, size: int, *, bold: bool = False, mono: bool = False):
        return _font(int(size * self.scale), bold=bold, mono=mono)

    def wrap(self, text: str, font, max_width: float) -> list[str]:
        return _wrap(text, font, max_width * self.scale)


def render_card(
    event: dict[str, Any],
    *,
    line_limit: int = 10,
    max_commits: int = 5,
    width: int = WIDTH,
    scale: int = SCALE,
    avatar_path: str | None = None,
    target_avatar_path: str | None = None,
) -> Image.Image:
    """把一条规范化事件画成卡片图片。

    Args:
        event: 规范化事件。
        line_limit: 正文最多展示的行数。
        max_commits: 提交卡最多展示的提交数。
        width: 画布宽度（1x）。
        scale: 超采样倍率。
        avatar_path: 操作人头像文件路径；为空时画带首字母的占位圆。
        target_avatar_path: 对方（PR 作者）头像路径，仅审查类事件使用。
    """
    card = _Card(width=width, scale=scale)
    kind = str(event.get("kind") or "issue")
    action_color = _action_color(event)

    # --- 文本准备 -------------------------------------------------
    repo_text, sub_text = _header_parts(event)
    headline = _headline(event)
    type_label = _type_label(event)
    actor_name = str(event.get("actor") or "").strip()
    avatar_img = _load_avatar(avatar_path)

    # 别人评论 / 审查这条 Issue/PR 时：操作人 → 原主，两个头像并排
    target_name = str(event.get("target") or "").strip()
    if not target_name or target_name == actor_name:
        target_name = ""
    has_pair = bool(target_name)
    target_img = _load_avatar(target_avatar_path)

    if kind == "commit":
        items, hidden_commits = _commit_items(event, max_commits)
        count = len(items) + hidden_commits
        body_label = f"提交列表 · {count} 个" if count > 1 else "提交详情"
        body_text = ""
    else:
        body_label, body_text = _thread_text(event)
        items, hidden_commits = [], 0

    # --- 字号 -----------------------------------------------------
    f_repo = card.font(17, bold=True)
    f_sub = card.font(14, bold=True)
    f_headline = card.font(26, bold=True)
    f_label = card.font(13, bold=True)
    f_body = card.font(16)
    f_body_small = card.font(13.5)
    f_sha = card.font(14, mono=True)
    f_tag = card.font(12, bold=True)

    # --- 先算高度 -------------------------------------------------
    content_x0 = MARGIN + INNER
    left_w = LEFT_W_PAIR if has_pair else LEFT_W
    left_cx = content_x0 + left_w / 2
    pair_gap = 50
    sep_x = content_x0 + left_w
    right_x0 = sep_x + 26
    # 右侧留一点内边距，避免正文贴着卡片边框
    right_w = width - MARGIN - INNER - right_x0 - 12

    head_lines, _ = _clamp(card.wrap(headline, f_headline, right_w), 3)
    head_lh = 38
    body_lh = 27
    avatar_r = 37 if has_pair else 50
    actor_h = 20
    type_h = 18
    tag_h = 24

    rich = rich_body.RichLayout(
        card,
        width=right_w,
        text_color=TEXT_SOFT,
        muted_color=TEXT_MUTED,
        code_color=BRAND_DEEP,
        code_bg=(15, 23, 42, 12),
        link_color=BRAND,
        base_size=16,
    )

    truncated = False
    rich_lines: list = []
    if kind == "commit":
        body_blocks: list[tuple[str, list[str]]] = []
        for sha, subject in items:
            wrapped = card.wrap(subject, f_body, right_w - 112) or [""]
            body_blocks.append((sha, wrapped[:2]))
        body_h = sum(len(w) * body_lh + 10 for _, w in body_blocks)
        if hidden_commits:
            body_h += 24
    else:
        rich_lines = rich.layout(body_text) if body_text else []
        shown_rich = rich_lines[: max(line_limit, 1)]
        body_h = rich.height(shown_rich)
        truncated = len(rich_lines) > len(shown_rich)
        if truncated:
            body_h += 28

    has_body = kind == "commit" or bool(rich_lines)

    right_h = (
        24  # 仓库行
        + 16
        + len(head_lines) * head_lh
        + 26
        + ((18 + 14 + body_h) if has_body else 0)
    )

    # 左栏：头像 → 作者 → 类型 → 操作
    left_h = avatar_r * 2 + 12 + actor_h + 6 + type_h + 8 + tag_h

    card_h = max(right_h, left_h, 200) + INNER * 2
    img_h = card_h + MARGIN * 2

    # --- 背景 -----------------------------------------------------
    image = _build_background((card.u(width), card.u(img_h)))
    image = image.convert("RGBA")

    # 卡片投影（浅色背景上用来撑起层次）
    card_box = (MARGIN, MARGIN, width - MARGIN, MARGIN + card_h)
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        tuple(
            card.u(v)
            for v in (
                card_box[0] + 2,
                card_box[1] + 10,
                card_box[2] - 2,
                card_box[3] + 14,
            )
        ),
        radius=card.u(RADIUS),
        fill=(52, 46, 84, 46),
    )
    image = Image.alpha_composite(
        image, shadow.filter(ImageFilter.GaussianBlur(card.u(16)))
    )

    # 卡片轮廓 + 毛玻璃（对卡片区域做高斯模糊再叠加半透明白）
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        tuple(card.u(v) for v in card_box), radius=card.u(RADIUS), fill=255
    )
    blurred = image.filter(ImageFilter.GaussianBlur(card.u(26)))
    image.paste(blurred, (0, 0), mask)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        tuple(card.u(v) for v in card_box),
        radius=card.u(RADIUS),
        fill=(255, 253, 246, PANEL_ALPHA),
    )
    image = Image.alpha_composite(image, overlay)

    # 所有前景元素画在独立图层上，最后统一 alpha 合成，
    # 这样半透明填充与文字描边才会正确叠加。
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        tuple(card.u(v) for v in card_box),
        radius=card.u(RADIUS),
        outline=BORDER,
        width=max(1, card.u(1.4)),
    )

    # --- 左栏 -----------------------------------------------------
    # 左栏整体在卡片内垂直居中
    left_top = MARGIN + (card_h - left_h) / 2
    avatar_cy = left_top + avatar_r

    def _avatar(cx: float, cy: float, radius: float, image, name: str) -> None:
        """画一个圆形头像：装饰环 + 小圆点 + 图片（或首字母占位）+ 描边。"""
        nonlocal draw

        # 外圈装饰：一圈很淡的细环 + 右下角一个小圆点，不做投影
        ring = radius + 7
        draw.ellipse(
            (
                card.u(cx - ring),
                card.u(cy - ring),
                card.u(cx + ring),
                card.u(cy + ring),
            ),
            outline=_rgba(action_color, 52),
            width=max(1, card.u(1)),
        )
        dot = 4.5
        draw.ellipse(
            (
                card.u(cx + ring * 0.72 - dot),
                card.u(cy + ring * 0.72 - dot),
                card.u(cx + ring * 0.72 + dot),
                card.u(cy + ring * 0.72 + dot),
            ),
            fill=(255, 255, 255, 255),
            outline=_rgba(action_color, 210),
            width=max(1, card.u(1.6)),
        )

        box = (
            card.u(cx - radius),
            card.u(cy - radius),
            card.u(cx + radius),
            card.u(cy + radius),
        )
        draw.ellipse(box, fill=_rgba(action_color, 18))

        if image is not None:
            size = box[2] - box[0]
            avatar = image.resize((size, size), Image.LANCZOS)
            avatar_mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(avatar_mask).ellipse(
                (0, 0, size - 1, size - 1), fill=255
            )
            layer.paste(avatar, (box[0], box[1]), avatar_mask)
            draw = ImageDraw.Draw(layer)
        else:
            initial = (name or type_label or "?")[:1].upper()
            init_font = card.font(radius * 0.66, bold=True)
            iw = card.measure(initial, init_font)
            ih = _line_height(init_font, radius * 0.66) / scale
            draw.text(
                (card.u(cx) - iw / 2, card.u(cy - ih / 2 - 1)),
                initial,
                font=init_font,
                fill=_rgba(action_color, 220),
            )

        draw.ellipse(
            box,
            outline=_rgba(action_color, 150),
            width=max(1, card.u(2)),
        )

    if has_pair:
        # 双头像：左边是操作人（评论 / 审查的人），右边是 Issue/PR 原主
        _avatar(left_cx - pair_gap, avatar_cy, avatar_r, avatar_img, actor_name)
        _avatar(left_cx + pair_gap, avatar_cy, avatar_r, target_img, target_name)
        # 手绘箭头，避免不同字体下 → 太细太小
        arrow_y = avatar_cy
        arrow_x0, arrow_x1 = left_cx - 11, left_cx + 9
        draw.line(
            (card.u(arrow_x0), card.u(arrow_y), card.u(arrow_x1), card.u(arrow_y)),
            fill=_rgba(action_color, 215),
            width=max(1, card.u(2)),
        )
        for dy in (-5, 5):
            draw.line(
                (
                    card.u(arrow_x1),
                    card.u(arrow_y),
                    card.u(arrow_x1 - 7),
                    card.u(arrow_y + dy),
                ),
                fill=_rgba(action_color, 215),
                width=max(1, card.u(2)),
            )
    else:
        _avatar(left_cx, avatar_cy, avatar_r, avatar_img, actor_name)

    # 作者名
    cursor = avatar_cy + avatar_r + 12
    if has_pair:
        # 双头像：各自头像下方写自己的名字，避免两个长名字挤成一行被截断
        name_font = card.font(12.5, bold=True)
        slot = left_w / 2 - 10
        for cx, name in (
            (left_cx - pair_gap, actor_name),
            (left_cx + pair_gap, target_name),
        ):
            if not name:
                continue
            label = _ellipsize(name, name_font, slot * scale)
            nw = card.measure(label, name_font)
            draw.text(
                (card.u(cx) - nw / 2, card.u(cursor)),
                label,
                font=name_font,
                fill=TEXT,
            )
    elif actor_name:
        actor_font = card.font(15.5, bold=True)
        label = _ellipsize(actor_name, actor_font, (left_w - 8) * scale)
        nw = card.measure(label, actor_font)
        draw.text(
            (card.u(left_cx) - nw / 2, card.u(cursor)),
            label,
            font=actor_font,
            fill=TEXT,
        )
    cursor += actor_h + 6

    # 类型（Commit / Issue / PR）
    type_font = card.font(13.5, bold=True)
    _draw_tracked_centered(
        draw, type_label, type_font, left_cx, cursor, BRAND_DEEP, tracking=1.4
    )
    cursor += type_h + 8

    # 操作（Open / Closed / Comment …）
    action_label = _action_label_en(event)
    tag_font = card.font(12, bold=True)
    tag_w = card.measure(action_label, tag_font) / scale + 20
    tag_x0 = left_cx - tag_w / 2
    _rounded(
        draw,
        (tag_x0, cursor, tag_x0 + tag_w, cursor + tag_h),
        tag_h / 2,
        fill=_rgba(action_color, 22),
        outline=_rgba(action_color, 90),
        width=max(1, card.u(1)),
    )
    draw.text(
        (card.u(tag_x0 + 10), card.u(cursor + (tag_h - 15) / 2)),
        action_label,
        font=tag_font,
        fill=action_color,
    )

    # 左右分隔线
    draw.line(
        (card.u(sep_x), card.u(MARGIN + INNER - 4), card.u(sep_x), card.u(MARGIN + card_h - INNER + 4)),
        fill=(28, 33, 48, 22),
        width=max(1, card.u(1)),
    )

    # --- 右栏 -----------------------------------------------------
    y = MARGIN + INNER
    x = right_x0

    # 类型图标与仓库名同高，放在第一行最前面
    icon_kind = "commit" if kind == "commit" else "issue" if kind == "issue" else "pr"
    _draw_icon(draw, icon_kind, x + 10, y + 9, 10, BRAND_DEEP)
    x += 28

    repo_w = card.measure(repo_text, f_repo) / scale
    draw.text((card.u(x), card.u(y)), repo_text, font=f_repo, fill=BRAND_DEEP)
    if sub_text:
        sx = x + repo_w + 9
        dot_r = 1.6
        dot_y = y + 11
        draw.ellipse(
            (
                card.u(sx),
                card.u(dot_y - dot_r),
                card.u(sx + dot_r * 2),
                card.u(dot_y + dot_r),
            ),
            fill=(15, 23, 42, 90),
        )
        draw.text(
            (card.u(sx + 11), card.u(y + 1)), sub_text, font=f_sub, fill=TEXT_MUTED
        )

    y += 24 + 16
    for line in head_lines:
        draw.text((card.u(x), card.u(y)), line, font=f_headline, fill=TEXT)
        y += head_lh

    if not has_body:
        y += 0
    else:
        y += 26

        # 正文标签：小竖条 + 文字
        _rounded(draw, (x, y + 1, x + 3, y + 16), 1.5, fill=BRAND_DEEP)
        draw.text(
            (card.u(x + 10), card.u(y)),
            body_label,
            font=f_label,
            fill=TEXT_MUTED,
        )
        y += 18 + 14

    if kind == "commit":
        for sha, wrapped in body_blocks:
            sha_w = card.measure(sha, f_sha) / scale + 18
            sha_h = 22
            _rounded(
                draw,
                (x, y + 1, x + sha_w, y + 1 + sha_h),
                7,
                fill=_rgba(BRAND, 26),
            )
            draw.text(
                (card.u(x + 9), card.u(y + 3)),
                sha,
                font=f_sha,
                fill=BRAND_DEEP,
            )
            for i, line in enumerate(wrapped[:2]):
                draw.text(
                    (card.u(x + sha_w + 12), card.u(y + 3 + i * 24)),
                    line,
                    font=f_body,
                    fill=TEXT_SOFT,
                )
            y += len(wrapped[:2]) * body_lh + 10
        if hidden_commits:
            draw.text(
                (card.u(x), card.u(y - 2)),
                f"还有 {hidden_commits} 个提交未显示",
                font=f_body_small,
                fill=TEXT_MUTED,
            )
            y += 26
    else:
        y = rich.draw(draw, shown_rich, x, y)
        if truncated:
            draw.text(
                (card.u(x), card.u(y + 2)),
                f"…… 其余 {len(rich_lines) - len(shown_rich)} 行已省略",
                font=f_body_small,
                fill=TEXT_MUTED,
            )
            y += 28

    # 左上角猫爪品牌角标
    badge_cx, badge_cy = MARGIN + 4, MARGIN + 4
    badge_r = 15
    draw.ellipse(
        (
            card.u(badge_cx - badge_r),
            card.u(badge_cy - badge_r),
            card.u(badge_cx + badge_r),
            card.u(badge_cy + badge_r),
        ),
        fill=(*BRAND_DEEP, 255),
    )
    paw = _paw_badge(card.u(badge_r * 2 - 4), (255, 255, 255, 242))
    layer.paste(
        paw,
        (
            card.u(badge_cx) - paw.width // 2,
            card.u(badge_cy) - paw.height // 2,
        ),
        paw,
    )

    image = Image.alpha_composite(image, layer)
    return image.convert("RGB").resize((width, img_h), Image.LANCZOS)


# ---------------------------------------------------------------- 落盘


def _output_dir() -> Path:
    path = _data_dir() / "temp" / "github_ling_cards"
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "github_ling_cards"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _cleanup(directory: Path, keep_seconds: int = 3600) -> None:
    deadline = time.time() - keep_seconds
    try:
        for item in directory.glob("*.png"):
            if item.stat().st_mtime < deadline:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def render_card_file(
    event: dict[str, Any],
    *,
    line_limit: int = 10,
    max_commits: int = 5,
    width: int = WIDTH,
    avatar_path: str | None = None,
    target_avatar_path: str | None = None,
) -> str:
    """渲染卡片并保存为 PNG，返回文件路径。"""
    image = render_card(
        event,
        line_limit=line_limit,
        max_commits=max_commits,
        width=width,
        avatar_path=avatar_path,
        target_avatar_path=target_avatar_path,
    )
    directory = _output_dir()
    _cleanup(directory)
    path = directory / f"card_{uuid.uuid4().hex[:12]}.png"
    image.save(path, format="PNG", optimize=True)
    return str(path)
