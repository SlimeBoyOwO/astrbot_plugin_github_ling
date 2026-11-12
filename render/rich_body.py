"""极简 Markdown 富文本排版，供卡片正文使用。

通知正文常常是 Markdown：标题、列表、引用、代码块、`行内代码`、**粗体**。
直接把符号画出来很难看，所以这里做一个够用的小排版器：

* 块级：`#` 标题、`-`/`*`/`+` 无序列表、`1.` 有序列表、`>` 引用、
  代码围栏、`---` 分割线、普通段落
* 行内：`**粗体**`、`` `代码` ``、`[文字](链接)`、`~~删除线~~`

只做排版，不依赖 AstrBot，便于离线测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)\s*([\w+-]*)\s*$")
# 拉丁字母/数字按整词处理，CJK 与符号逐字处理
_ATOM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_@:/.\-+#%&]*|\s+|.", re.S)
# 这些字符不适合出现在行首，宁可让上一行稍微超出一点
_NO_LINE_START = set("。，、；：！？）》」』】〕”’,.!?:;)]}%…~")


@dataclass
class Span:
    text: str
    bold: bool = False
    code: bool = False
    muted: bool = False
    link: bool = False


@dataclass
class Block:
    kind: str  # heading / bullet / ordered / quote / code / hr / para
    spans: list[Span] = field(default_factory=list)
    level: int = 0
    marker: str = ""
    lines: list[str] = field(default_factory=list)


@dataclass
class Op:
    x: float
    w: float
    text: str
    font: Any
    color: tuple
    bg: tuple | None = None


@dataclass
class Line:
    ops: list[Op]
    height: int
    indent: float = 0.0
    bg_full: bool = False
    gap_before: int = 0
    quote_bar: bool = False


# ------------------------------------------------------------------ 解析


def parse_inline(text: str) -> list[Span]:
    """把一行文本拆成带样式的片段（粗体 / 行内代码 / 链接 / 删除线）。"""
    spans: list[Span] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            spans.append(Span("".join(buf)))
            buf.clear()

    i, n = 0, len(text)
    while i < n:
        chunk = text[i:]

        for marker, factory in (
            ("**", lambda s: Span(s, bold=True)),
            ("__", lambda s: Span(s, bold=True)),
            ("~~", lambda s: Span(s, muted=True)),
        ):
            if chunk.startswith(marker):
                end = text.find(marker, i + 2)
                if end > i + 2:
                    flush()
                    spans.append(factory(text[i + 2 : end]))
                    i = end + 2
                    break
        else:
            if chunk[0] == "`":
                end = text.find("`", i + 1)
                if end > i + 1:
                    flush()
                    spans.append(Span(text[i + 1 : end], code=True))
                    i = end + 1
                    continue

            if chunk[0] == "[":
                match = _LINK_RE.match(text, i)
                if match:
                    flush()
                    spans.append(Span(match.group(1) or match.group(2), link=True))
                    i = match.end()
                    continue

            buf.append(chunk[0])
            i += 1
            continue
        continue

    flush()
    return spans


def parse_blocks(text: str) -> list[Block]:
    """把正文拆成块：标题 / 列表 / 引用 / 代码 / 分割线 / 段落。"""
    blocks: list[Block] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    after_blank = True
    while i < len(lines):
        raw = lines[i]

        fence = _FENCE_RE.match(raw)
        if fence:
            marker = fence.group(1)
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过结束围栏
            blocks.append(Block(kind="code", lines=code_lines))
            continue

        if not raw.strip():
            after_blank = True
            i += 1
            continue

        if _HR_RE.match(raw):
            blocks.append(Block(kind="hr"))
            i += 1
            continue

        heading = _HEADING_RE.match(raw)
        if heading:
            blocks.append(
                Block(
                    kind="heading",
                    level=len(heading.group(1)),
                    spans=parse_inline(heading.group(2)),
                )
            )
            i += 1
            continue

        if _QUOTE_RE.match(raw):
            quote_lines: list[str] = []
            while i < len(lines):
                quote = _QUOTE_RE.match(lines[i])
                if not quote:
                    break
                quote_lines.append(quote.group(1))
                i += 1
            blocks.append(
                Block(kind="quote", spans=parse_inline(" ".join(quote_lines).strip()))
            )
            continue

        ordered = _ORDERED_RE.match(raw)
        if ordered:
            blocks.append(
                Block(
                    kind="ordered",
                    marker=f"{ordered.group(1)}.",
                    spans=parse_inline(ordered.group(2)),
                )
            )
            i += 1
            continue

        bullet = _BULLET_RE.match(raw)
        if bullet:
            blocks.append(Block(kind="bullet", spans=parse_inline(bullet.group(1))))
            i += 1
            continue

        # level 记下“前面是否有空行”，排版时用它决定段间距
        blocks.append(
            Block(kind="para", spans=parse_inline(raw.strip()), level=1 if after_blank else 0)
        )
        after_blank = False
        i += 1

    return blocks


# ------------------------------------------------------------------ 排版


class RichLayout:
    """把 Markdown 排成一行行可直接绘制的文本。"""

    def __init__(
        self,
        card,
        *,
        width: float,
        text_color: tuple,
        muted_color: tuple,
        code_color: tuple,
        code_bg: tuple,
        link_color: tuple,
        base_size: float = 16,
    ) -> None:
        self.card = card
        self.width = width
        self.text_color = text_color
        self.muted_color = muted_color
        self.code_color = code_color
        self.code_bg = code_bg
        self.link_color = link_color
        self.base_size = base_size
        self.line_h = int(base_size + 10)
        # 卡片字体按超采样倍率创建，量出来的宽度要换算回 1x 逻辑坐标
        self.scale = getattr(card, "scale", 1) or 1

    def _advance(self, text: str, font) -> float:
        return font.getlength(text) / self.scale

    # -- 字体与颜色 -------------------------------------------------
    def _style(self, span: Span, size: float, *, bold: bool = False) -> tuple:
        if span.code:
            return (
                self.card.font(size - 1.5, mono=True),
                self.code_color,
                self.code_bg,
            )
        font = self.card.font(size, bold=bold or span.bold)
        if span.muted:
            return font, self.muted_color, None
        if span.link:
            return font, self.link_color, None
        return font, self.text_color, None

    # -- 折行 -------------------------------------------------------
    def _wrap(
        self,
        spans: list[Span],
        size: float,
        *,
        bold: bool = False,
        indent: float = 0.0,
    ) -> list[Line]:
        height = int(size + 10)
        lines: list[Line] = []
        ops: list[Op] = []
        cursor = indent
        started = False

        for span in spans:
            font, color, bg = self._style(span, size, bold=bold)
            for atom in _ATOM_RE.findall(span.text):
                if not atom:
                    continue
                width = self._advance(atom, font)
                if atom.isspace():
                    if not started:
                        continue
                    ops.append(Op(cursor, width, atom, font, color, bg))
                    cursor += width
                    continue
                needs_break = started and cursor + width > self.width
                if needs_break and atom in _NO_LINE_START and cursor + width <= self.width + 18:
                    needs_break = False
                if needs_break:
                    lines.append(Line(self._trim(ops), height, indent))
                    ops, cursor, started = [], indent, False
                ops.append(Op(cursor, width, atom, font, color, bg))
                cursor += width
                started = True

        if ops:
            lines.append(Line(self._trim(ops), height, indent))
        elif not lines:
            lines.append(Line([], height, indent))
        return lines

    @staticmethod
    def _trim(ops: list[Op]) -> list[Op]:
        """去掉行尾空格，并合并同样式的相邻片段，减少绘制次数。"""
        while ops and ops[-1].text.isspace():
            ops.pop()
        # 行内代码前后留一点空隙，避免和正文粘在一起
        merged: list[Op] = []
        for op in ops:
            if (
                merged
                and op.bg == merged[-1].bg
                and merged[-1].font is op.font
                and merged[-1].color == op.color
            ):
                merged[-1].text += op.text
                merged[-1].w += op.w
            else:
                merged.append(op)
        return merged

    # -- 入口 -------------------------------------------------------
    def layout(self, text: str) -> list[Line]:
        result: list[Line] = []
        for index, block in enumerate(parse_blocks(text)):
            if block.kind == "hr":
                result.append(Line([], 14, 0, bg_full=True))
                continue

            if block.kind == "code":
                font = self.card.font(self.base_size - 1.5, mono=True)
                for raw in block.lines:
                    ops: list[Op] = []
                    cursor = 12.0
                    for char in raw.replace("\t", "    "):
                        width = self._advance(char, font)
                        ops.append(Op(cursor, width, char, font, self.code_color))
                        cursor += width
                    result.append(
                        Line(ops, int(self.base_size + 7), 0, bg_full=True)
                    )
                continue

            if block.kind == "heading":
                size = self.base_size + (4 if block.level <= 2 else 2)
                lines = self._wrap(block.spans, size, bold=True)
                lines[0].gap_before = 10 if index else 2
                result.extend(lines)
                continue

            if block.kind in ("bullet", "ordered"):
                prefix = "•" if block.kind == "bullet" else block.marker
                font = self.card.font(self.base_size, bold=True)
                lines = self._wrap(block.spans, self.base_size, indent=20.0)
                lines[0].ops = [
                    Op(0.0, self._advance(prefix, font), prefix, font, self.text_color)
                ] + lines[0].ops
                result.extend(lines)
                continue

            if block.kind == "quote":
                lines = self._wrap(
                    [Span(s.text, muted=True) for s in block.spans],
                    self.base_size,
                    indent=18.0,
                )
                for line in lines:
                    line.quote_bar = True
                result.extend(lines)
                continue

            lines = self._wrap(block.spans, self.base_size)
            lines[0].gap_before = (8 if block.level else 0) if index else 0
            result.extend(lines)
        return result

    # -- 绘制 -------------------------------------------------------
    def draw(self, draw, lines: list[Line], x: float, y: float) -> float:
        card = self.card
        for line in lines:
            y += line.gap_before
            if line.bg_full:
                draw.rounded_rectangle(
                    (
                        card.u(x),
                        card.u(y + 1),
                        card.u(x + self.width),
                        card.u(y + line.height - 5),
                    ),
                    radius=card.u(8),
                    fill=self.code_bg,
                )
            if line.quote_bar:
                draw.rounded_rectangle(
                    (
                        card.u(x),
                        card.u(y + 1),
                        card.u(x + 3),
                        card.u(y + line.height - 6),
                    ),
                    radius=card.u(1.5),
                    fill=(28, 33, 48, 46),
                )
            for op in line.ops:
                if op.bg:
                    draw.rounded_rectangle(
                        (
                            card.u(x + op.x - 2),
                            card.u(y + 2),
                            card.u(x + op.x + op.w + 2),
                            card.u(y + line.height - 7),
                        ),
                        radius=card.u(5),
                        fill=op.bg,
                    )
                draw.text(
                    (card.u(x + op.x), card.u(y)),
                    op.text,
                    font=op.font,
                    fill=op.color,
                )
            y += line.height
        return y

    @staticmethod
    def height(lines: list[Line]) -> float:
        return sum(line.gap_before + line.height for line in lines)


def plain_text(text: str) -> str:
    """把 Markdown 压成纯文本（不画图时使用）。"""
    return " ".join(
        span.text for block in parse_blocks(text) for span in block.spans
    ).strip()
