"""用大模型把聊天记录整理成 Issue。

流程：

1. 取仓库的 Issue 模板（没有就用内置骨架）；
2. 把模板结构 + 聊天记录 + 截图交给 AstrBot 当前使用的对话模型；
3. 模型返回 JSON（标题 / 正文 / 标签），解析成 :class:`IssueDraft`；
4. 用户确认后，上传截图并调用 GitHub API 创建 Issue。

模型不按要求返回 JSON 时不会报错，而是退化成「全文当正文」，
保证聊天里的有用信息不会丢。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from astrbot.api import logger

from ..core.config import PluginConfig
from ..core.texts import normalize_spaces, truncate
from ..github.client import GitHubAPI
from .assets import AssetUploader
from .context import ChatContext
from .draft import IssueDraft, short_title
from .templates import (
    IssueTemplate,
    TemplateCatalog,
    choose_template,
    default_template,
)

MAX_BODY_CHARS = 60_000
MAX_PROMPT_TEMPLATE_CHARS = 2600

SYSTEM_PROMPT = """你是一个 GitHub Issue 助手，负责把 QQ 群里的聊天记录整理成规范的 Issue。

写作要求：
1. 只依据聊天记录和截图中的信息，严禁编造版本号、复现步骤、环境、链接或他人反馈。
2. 标题用一句话概括问题，中文优先，保留必要的英文术语，不超过 60 个字。
3. 正文必须严格套用给定模板的标题层级（### 开头的字段名原样保留），逐项填写；
   模板要求但聊天中没有的信息，写“未提供”，不要删除该字段。
4. 日志、报错、命令输出使用 ``` 代码块原样保留关键行，删掉无关刷屏内容。
5. 截图里的报错信息要转写成文字放进对应字段，并保留对截图的引用。
6. 标签只从仓库已有标签或模板标签中选择，没有合适的就给空数组。

输出格式：只输出一个 JSON 对象，不要 Markdown 代码围栏，不要任何解释文字：
{"template": "模板名", "title": "标题", "body": "完整的 Markdown 正文", "labels": ["bug"]}
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class IssueAgent:
    def __init__(
        self,
        *,
        context: Any,
        api: GitHubAPI,
        config: PluginConfig,
        catalog: TemplateCatalog,
        uploader: AssetUploader,
    ) -> None:
        self.context = context
        self.api = api
        self.config = config
        self.catalog = catalog
        self.uploader = uploader

    # ------------------------------------------------------------------ 模型

    async def _resolve_provider(self, event: Any) -> Any | None:
        provider_id = self.config.issue_llm_provider.strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception as exc:
                logger.warning("获取指定模型 %s 失败: %s", provider_id, exc)
                return None
            if provider is not None:
                return provider
            logger.warning("配置的模型 %s 不存在，改用默认模型", provider_id)

        try:
            return await self.context.get_using_provider_async(
                event.unified_msg_origin
            )
        except Exception as exc:
            logger.warning("获取默认对话模型失败: %s", exc)
            return None

    @staticmethod
    def describe_provider(provider: Any) -> str:
        if provider is None:
            return "未配置"
        for attr in ("get_model",):
            getter = getattr(provider, attr, None)
            if callable(getter):
                try:
                    model = getter()
                    if model:
                        return str(model)
                except Exception:
                    pass
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            value = config.get("id") or config.get("model") or ""
            if value:
                return str(value)
        return type(provider).__name__

    async def provider_label(self, event: Any) -> str:
        return self.describe_provider(await self._resolve_provider(event))

    # ------------------------------------------------------------------ 生成

    async def generate(
        self,
        *,
        event: Any,
        repo: str,
        chat: ChatContext,
        instruction: str = "",
        template_index: int | None = None,
        template_name: str | None = None,
        user_id: str = "",
    ) -> tuple[IssueDraft | None, str]:
        """生成草稿。返回 ``(草稿, 错误说明)``，二者必有其一。"""
        provider = await self._resolve_provider(event)
        if provider is None:
            return (
                None,
                "没有可用的对话模型。请在 AstrBot 中配置一个模型，"
                "或在插件配置里填写 issue_llm_provider。",
            )

        templates = await self.catalog.get(repo)
        if not templates:
            templates = [default_template()]

        explicit = template_index is not None or bool(template_name)
        template = choose_template(templates, index=template_index, name=template_name)
        if explicit and template is None:
            listing = "\n".join(
                f"{i}. {item.name}" for i, item in enumerate(templates, start=1)
            )
            return None, f"没有找到指定的模板。可用模板：\n{listing}"

        repo_info = await self.api.get_repo(repo)
        prompt = self._build_prompt(
            repo=repo,
            repo_info=repo_info if isinstance(repo_info, dict) else {},
            templates=templates,
            template=template,
            chat=chat,
            instruction=instruction,
            allow_model_choice=not explicit and self.config.issue_template_auto,
        )

        note = ""
        response_text = ""
        try:
            response_text = await self._chat(provider, prompt, chat)
        except Exception as exc:
            logger.warning("调用大模型生成 Issue 失败: %s", exc)
            return None, f"调用模型失败：{exc}"

        parsed = _parse_model_output(response_text)
        if parsed is None:
            title, body = _fallback_from_text(response_text)
            note = "模型没有按要求返回 JSON，已把回复原文作为正文"
            labels = list(template.labels)
            chosen = template
        else:
            title = str(parsed.get("title") or "").strip()
            body = str(parsed.get("body") or "").strip()
            labels = [str(item).strip() for item in parsed.get("labels") or [] if str(item).strip()]
            chosen = self._pick_by_model_name(templates, parsed.get("template")) or template

        if not title and not body:
            return None, "模型没有生成有效内容，请补充一点描述后再试"

        if not title:
            title = _title_from_body(body) or "来自聊天记录的问题"
        title = short_title(_clean_title(title), 120)
        if not body:
            body = "### 发生了什么\n\n（模型没有生成正文）"

        body, too_long = truncate(body, MAX_BODY_CHARS, "\n\n…（正文过长，已截断）")
        if too_long:
            note = (note + "；" if note else "") + "正文超过 GitHub 长度限制，已截断"

        labels = await self._filter_labels(repo, labels, chosen)

        draft = IssueDraft(
            repo=repo,
            title=title,
            body=body,
            labels=labels,
            template_name=chosen.name,
            template_index=(
                templates.index(chosen) + 1 if chosen in templates else 1
            ),
            images=list(chat.images),
            chat=chat,
            instruction=instruction,
            umo=str(getattr(event, "unified_msg_origin", "")),
            user_id=user_id,
            model_note=note,
        )
        return draft, ""

    async def _chat(self, provider: Any, prompt: str, chat: ChatContext) -> str:
        """调用模型；模型不支持图片时自动退回纯文本。"""
        image_urls = list(chat.images) if chat.images else None
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=SYSTEM_PROMPT,
                    image_urls=image_urls,
                ),
                timeout=self.config.issue_timeout,
            )
            return str(getattr(response, "completion_text", "") or "")
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"模型 {self.config.issue_timeout} 秒内没有返回结果"
            ) from None
        except Exception as exc:
            if image_urls:
                logger.warning("带截图调用模型失败（%s），改用纯文本重试", exc)
                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        system_prompt=SYSTEM_PROMPT,
                    ),
                    timeout=self.config.issue_timeout,
                )
                return str(getattr(response, "completion_text", "") or "")
            raise

    # ------------------------------------------------------------------ 提示词

    def _build_prompt(
        self,
        *,
        repo: str,
        repo_info: dict[str, Any],
        templates: list[IssueTemplate],
        template: IssueTemplate | None,
        chat: ChatContext,
        instruction: str,
        allow_model_choice: bool,
    ) -> str:
        if template is None:
            template = templates[0]

        if allow_model_choice:
            shown = templates[:4]
        else:
            shown = [template]

        lines = [f"仓库：{repo}"]
        description = normalize_spaces(repo_info.get("description"))
        details = []
        if description:
            details.append(description)
        language = repo_info.get("language")
        if language:
            details.append(f"主要语言：{language}")
        stars = repo_info.get("stargazers_count")
        if isinstance(stars, int):
            details.append(f"Star：{stars}")
        if details:
            lines.append("仓库信息：" + "；".join(details))

        lines.append("")
        if len(shown) > 1:
            lines.append("可用模板（请选择最匹配的一个，并在 JSON 的 template 字段写模板名）：")
        else:
            lines.append("使用的模板：")
        for index, item in enumerate(shown, start=1):
            skeleton, _ = truncate(
                item.skeleton.strip(),
                MAX_PROMPT_TEMPLATE_CHARS,
                "\n…（模板过长，已截断）",
            )
            header = f"[{index}] {item.name}"
            if item.description:
                header += f" —— {item.description}"
            lines.append(header)
            lines.append(f"----- 模板 {index} 开始 -----")
            lines.append(skeleton)
            lines.append(f"----- 模板 {index} 结束 -----")
            lines.append("")

        lines.append("聊天记录（可能包含引用、合并转发的聊天记录与日志）：")
        lines.append(chat.text.strip() or "（没有文本内容）")
        lines.append("")
        if instruction.strip():
            lines.append("用户的补充说明：")
            lines.append(instruction.strip())
            lines.append("")
        if chat.images:
            lines.append(
                f"随消息附上了 {len(chat.images)} 张截图，请读取截图中的报错信息并写进正文。"
            )
        if chat.truncated:
            lines.append("（聊天记录过长，已截断）")
        lines.append("")
        lines.append("请按系统提示的 JSON 格式输出。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _pick_by_model_name(
        templates: list[IssueTemplate],
        name: Any,
    ) -> IssueTemplate | None:
        value = str(name or "").strip()
        if not value:
            return None
        return choose_template(templates, name=value)

    async def _filter_labels(
        self,
        repo: str,
        labels: list[str],
        template: IssueTemplate,
    ) -> list[str]:
        """只保留仓库里真实存在的标签，避免 GitHub 拒绝整次创建。"""
        candidates = []
        for item in [*template.labels, *labels]:
            value = str(item).strip()
            if value and value not in candidates:
                candidates.append(value)
        if not candidates:
            return []

        existing = await self.api.list_labels(repo)
        if existing is None:
            # 拿不到标签列表时，退回到模板标签（一定存在于该模板）
            return list(template.labels)[:5]

        allowed = {item.lower(): item for item in existing}
        result: list[str] = []
        for item in candidates:
            real = allowed.get(item.lower())
            if real and real not in result:
                result.append(real)
        return result[:5]

    # ------------------------------------------------------------------ 发布

    async def publish(self, draft: IssueDraft) -> tuple[str | None, str]:
        """上传截图并创建 Issue，返回 ``(链接, 错误)``。"""
        body = draft.body.strip()
        note_parts: list[str] = []

        if draft.images:
            result = await self.uploader.upload(draft.repo, draft.images)
            if result.urls:
                lines = ["", "", "---", "### 截图"]
                for index, url in enumerate(result.urls, start=1):
                    lines.append(f"![截图 {index}]({url})")
                body += "\n".join(lines)
            if result.note:
                note_parts.append(result.note)

        if note_parts:
            body += "\n\n> " + "；".join(note_parts)

        issue = await self.api.create_issue(
            repo=draft.repo,
            title=draft.title,
            body=body,
            labels=draft.labels,
        )
        if not issue:
            return None, self.api.last_error or "GitHub 没有返回创建结果"
        url = str(issue.get("html_url") or "")
        if not url and issue.get("number"):
            url = f"https://github.com/{draft.repo}/issues/{issue['number']}"
        return url, ""


# ---------------------------------------------------------------------- 工具


def _parse_model_output(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    cleaned = _FENCE_RE.sub("", raw).strip()

    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _fallback_from_text(text: str) -> tuple[str, str]:
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    lines = [line.strip() for line in raw.splitlines()]
    title = ""
    body_lines: list[str] = []
    for line in lines:
        if not line:
            body_lines.append("")
            continue
        if not title:
            title = line.lstrip("#").strip()
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return title, body or raw


def _title_from_body(body: str) -> str:
    for line in str(body or "").splitlines():
        value = line.strip().lstrip("#").strip()
        if value and value not in {"发生了什么", "问题描述", "描述"}:
            return value
    return ""


def _clean_title(title: str) -> str:
    value = normalize_spaces(title)
    value = value.strip("`*_#[]（）() \t\"'")
    return value[:120]
