"""生成 GitHub Ling 卡片样例（PNG / Markdown / 纯文本）。

- 默认卡片由 `render/card.py` 用 Pillow 直接绘制（米白毛玻璃风格，离线可用）
- 兜底的 Markdown / 纯文本由 `render/markdown.py` 生成

用法::

    # 只打印 Markdown 与纯文本
    python tools/preview_cards.py

    # 生成内置渲染器的 PNG 卡片
    python tools/preview_cards.py --render

    # 额外渲染旧版 Markdown 卡片（走 AstrBot 本地文转图）做对比
    python tools/preview_cards.py --render --render-markdown

生成的图片保存在 tools/preview/ 下。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from astrbot_plugin_github_ling.render import markdown as cards  # noqa: E402


def sample_events() -> list[tuple[str, dict]]:
    commit = {
        "kind": "commit",
        "repo": "SlimeBoyOwO/LingChat",
        "branch": "main",
        "action": "push",
        "actor": "钦灵",
        "commits": [
            {
                "sha": "a1b2c3d4e5f6",
                "subject": "feat: 支持自定义卡片模板",
                "url": "https://github.com/SlimeBoyOwO/LingChat/commit/a1b2c3d",
            },
            {
                "sha": "d4e5f6a7b8c9",
                "subject": "fix: 修复启动时崩溃的问题",
                "url": "https://github.com/SlimeBoyOwO/LingChat/commit/d4e5f6a",
            },
            {
                "sha": "998877665544",
                "subject": "docs: 更新 README 说明",
                "url": "https://github.com/SlimeBoyOwO/LingChat/commit/9988776",
            },
        ],
        "url": "https://github.com/SlimeBoyOwO/LingChat/compare/aaa...bbb",
    }

    issue = {
        "kind": "issue",
        "repo": "SlimeBoyOwO/LingChat",
        "action": "comment",
        "actor": "忻椒",
        "title": "登录状态下发送图片会崩溃",
        "number": 123,
        "body": (
            "我在最新版本遇到了这个问题：\n\n"
            "复现步骤：\n"
            "1. 登录\n"
            "2. 发送一张图片\n"
            "3. 机器人直接退出\n\n"
            "日志如下：\n"
            "Traceback (most recent call last):\n"
            '  File "main.py", line 1\n'
            "    boom\n\n"
            "环境：Windows 11 / Python 3.12\n"
            "版本：v4.28.1\n"
            "补充：只有群聊里会出现，私聊正常。"
        ),
        "body_label": "评论",
        "url": "https://github.com/SlimeBoyOwO/LingChat/issues/123#issuecomment-1",
    }

    pr = {
        "kind": "pr",
        "repo": "SlimeBoyOwO/LingChat",
        "action": "review_comment",
        "actor": "钦灵",
        "title": "feat: 新增 GitHub 卡片渲染器",
        "number": 456,
        "body": (
            "这里建议把卡片生成拆成纯函数，方便加测试。\n\n"
            "- cards.py 只负责 Markdown\n"
            "- main.py 只负责取数和发送\n"
            "- 渲染失败时退回纯文本\n\n"
            "其他部分我看没问题。"
        ),
        "body_label": "审查评论",
        "url": "https://github.com/SlimeBoyOwO/LingChat/pull/456#discussion_r1",
    }

    return [("commit", commit), ("issue_comment", issue), ("pr_comment", pr)]


def render_all(
    events: list[tuple[str, dict]],
    images: bool,
    markdown_images: bool,
    avatar: str | None = None,
) -> None:
    out_dir = Path(__file__).resolve().parent / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    if images:
        from astrbot_plugin_github_ling.render import card as card_render

        for name, event in events:
            image = card_render.render_card(event, avatar_path=avatar)
            path = out_dir / f"{name}.png"
            tmp = out_dir / f"~{name}.png"
            image.save(tmp)
            os.replace(tmp, path)
            print(f"已生成: {path}")

    if markdown_images:
        import asyncio

        from astrbot.core.utils.t2i.local_strategy import MarkdownRenderer

        async def _run() -> None:
            renderer = MarkdownRenderer(font_size=25, width=800)
            for name, event in events:
                markdown = cards.render_event(event, markdown=True)
                image = await renderer.render(markdown)
                path = out_dir / f"{name}_markdown.png"
                tmp = out_dir / f"~{name}_markdown.png"
                image.save(tmp)
                os.replace(tmp, path)
                print(f"已生成: {path}")

        asyncio.run(_run())


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 GitHub Ling 卡片样例")
    parser.add_argument(
        "--render",
        action="store_true",
        help="用插件自带的 Pillow 渲染器生成 PNG 卡片（推荐）",
    )
    parser.add_argument(
        "--render-markdown",
        action="store_true",
        help="额外用 AstrBot 本地文转图渲染旧版 Markdown 卡片，便于对比",
    )
    parser.add_argument(
        "--avatar",
        default=None,
        help="可选：本地头像图片路径，用于预览头像效果（不给则画首字母占位图）",
    )
    args = parser.parse_args()

    events = sample_events()

    for name, event in events:
        print("=" * 70)
        print(f"[{name}] Markdown（这里是卡片内容与排版，视觉样式由 t2i 模板决定）")
        print("=" * 70)
        print(cards.render_event(event, markdown=True))
        print("-" * 70)
        print(f"[{name}] 纯文本兜底")
        print("-" * 70)
        print(cards.render_event(event, markdown=False))
        print()

    if not (args.render or args.render_markdown):
        print("提示：加 --render 可生成 PNG 样例到 tools/preview/")
        return

    try:
        render_all(events, args.render, args.render_markdown, args.avatar)
    except Exception as exc:  # pragma: no cover
        print(f"渲染失败: {exc}")
        raise


if __name__ == "__main__":
    main()
