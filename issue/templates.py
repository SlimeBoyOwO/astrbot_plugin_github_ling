"""读取仓库的 Issue 模板（``.github/ISSUE_TEMPLATE``），并转换成可填写的骨架。

支持两种官方模板：

* Markdown 模板（``.md``）：YAML front matter + 正文
* 表单模板（``.yml`` / ``.yaml``）：``body`` 里是一串 ``textarea`` / ``input`` /
  ``dropdown`` / ``checkboxes`` / ``markdown`` 字段

无论哪种，最终都会变成一个「带标题层级的 Markdown 骨架」交给大模型填写：
表单模板由我们拼出 ``### 字段名``，Markdown 模板直接用原文。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..github.client import GitHubAPI

try:
    import yaml
except Exception:  # pragma: no cover - AstrBot 正常会带 PyYAML
    yaml = None  # type: ignore[assignment]

TEMPLATE_DIR = ".github/ISSUE_TEMPLATE"
MAX_TEMPLATES = 8
CACHE_TTL_SECONDS = 10 * 60

_FRONT_MATTER_RE = re.compile(r"\A﻿?---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.S)

GENERIC_TEMPLATE = """### 发生了什么

（描述遇到的问题）

### 复现步骤

1.
2.

### 期望行为

### 环境信息

- 系统 / 版本：
- 相关日志：

### 补充说明

（截图或其他信息）
"""


@dataclass(slots=True)
class IssueTemplate:
    name: str
    description: str = ""
    title_prefix: str = ""
    labels: list[str] = field(default_factory=list)
    filename: str = ""
    kind: str = "markdown"  # markdown | form
    skeleton: str = ""

    def as_prompt_block(self, index: int) -> str:
        head = f"[{index}] {self.name}"
        if self.description:
            head += f" —— {self.description}"
        return f"{head}\n{self.skeleton.strip()}"


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    body = text[match.end() :]
    if yaml is None:
        return {}, body
    try:
        data = yaml.safe_load(raw) or {}
    except Exception as exc:
        logger.debug("解析 Issue 模板 front matter 失败: %s", exc)
        return {}, body
    return (data if isinstance(data, dict) else {}), body


def _as_labels(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _form_skeleton(items: Any) -> str:
    """把 YAML 表单的 ``body`` 转成 Markdown 骨架。"""
    lines: list[str] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        attributes = item.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}

        if item_type == "markdown":
            value = str(attributes.get("value") or "").strip()
            if value:
                lines.append(value)
            continue

        label = _clean_text(attributes.get("label"))
        if not label:
            continue
        lines.append(f"### {label}")

        hint_parts = [
            _clean_text(attributes.get("description")),
            _clean_text(attributes.get("placeholder")),
        ]
        hint = " ".join(part for part in hint_parts if part)
        if hint:
            lines.append(f"<!-- 提示：{hint} -->")

        if item_type == "dropdown":
            options = [
                _clean_text(option)
                for option in list(attributes.get("options") or [])
                if _clean_text(option)
            ]
            if options:
                lines.append("可选值：" + " / ".join(options) + "（选择最合适的一项）")
        elif item_type == "checkboxes":
            for option in list(attributes.get("options") or []):
                text = option.get("label") if isinstance(option, dict) else option
                text = _clean_text(text)
                if text:
                    lines.append(f"- [ ] {text}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def parse_markdown_template(text: str, filename: str = "") -> IssueTemplate:
    meta, body = _front_matter(text)
    name = _clean_text(meta.get("name")) or filename.rsplit(".", 1)[0] or "模板"
    return IssueTemplate(
        name=name,
        description=_clean_text(meta.get("about") or meta.get("description")),
        title_prefix=_clean_text(meta.get("title")),
        labels=_as_labels(meta.get("labels")),
        filename=filename,
        kind="markdown",
        skeleton=body.strip() + "\n",
    )


def parse_form_template(data: dict[str, Any], filename: str = "") -> IssueTemplate:
    name = _clean_text(data.get("name")) or filename.rsplit(".", 1)[0] or "模板"
    return IssueTemplate(
        name=name,
        description=_clean_text(data.get("description") or data.get("about")),
        title_prefix=_clean_text(data.get("title")),
        labels=_as_labels(data.get("labels")),
        filename=filename,
        kind="form",
        skeleton=_form_skeleton(data.get("body")),
    )


class TemplateCatalog:
    """按仓库缓存 Issue 模板，避免每次生成都请求 GitHub。"""

    def __init__(self, api: GitHubAPI, ttl: int = CACHE_TTL_SECONDS) -> None:
        self.api = api
        self.ttl = ttl
        self._cache: dict[str, tuple[float, list[IssueTemplate]]] = {}

    def invalidate(self, repo: str | None = None) -> None:
        if repo is None:
            self._cache.clear()
        else:
            self._cache.pop(repo, None)

    async def get(self, repo: str) -> list[IssueTemplate]:
        cached = self._cache.get(repo)
        now = time.time()
        if cached and now - cached[0] < self.ttl:
            return cached[1]

        templates = await self._fetch(repo)
        self._cache[repo] = (now, templates)
        return templates

    async def _fetch(self, repo: str) -> list[IssueTemplate]:
        listing = await self.api.get_contents(repo, TEMPLATE_DIR)
        if not isinstance(listing, list):
            return []

        templates: list[IssueTemplate] = []
        for entry in listing:
            if len(templates) >= MAX_TEMPLATES:
                break
            if not isinstance(entry, dict) or entry.get("type") != "file":
                continue
            filename = str(entry.get("name") or "")
            lower = filename.lower()
            if not lower.endswith((".md", ".yml", ".yaml")):
                continue
            if lower == "config.yml" or lower == "config.yaml":
                continue

            content = await self.api.get_text_file(repo, str(entry.get("path") or f"{TEMPLATE_DIR}/{filename}"))
            if not content:
                continue
            try:
                if lower.endswith(".md"):
                    template = parse_markdown_template(content, filename)
                else:
                    if yaml is None:
                        continue
                    data = yaml.safe_load(content) or {}
                    if not isinstance(data, dict):
                        continue
                    template = parse_form_template(data, filename)
            except Exception as exc:
                logger.warning("解析 Issue 模板 %s 失败: %s", filename, exc)
                continue

            if not template.skeleton.strip():
                continue
            templates.append(template)

        # 按文件名排序，保证每次生成的顺序一致
        templates.sort(key=lambda item: item.filename)
        return templates


def default_template() -> IssueTemplate:
    """仓库没有模板时使用的内置骨架。"""
    return IssueTemplate(
        name="通用 Issue",
        description="仓库没有提供模板，使用内置的通用结构",
        filename="",
        kind="generic",
        skeleton=GENERIC_TEMPLATE,
    )


def choose_template(
    templates: list[IssueTemplate],
    index: int | None = None,
    name: str | None = None,
) -> IssueTemplate | None:
    """按序号（从 1 开始）或名称挑选模板。"""
    if not templates:
        return None
    if name:
        lowered = name.strip().lower()
        for template in templates:
            if template.name.strip().lower() == lowered:
                return template
        for template in templates:
            if lowered and lowered in template.name.strip().lower():
                return template
    if index is not None:
        if 1 <= index <= len(templates):
            return templates[index - 1]
        return None
    return templates[0]
