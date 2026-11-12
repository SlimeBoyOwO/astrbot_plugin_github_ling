"""状态文件读写：绑定关系、轮询水位线、额外推送目标、默认仓库。

文件结构见 README「数据」一节。读取失败时返回空状态而不是抛异常，
避免插件因为一个坏掉的 JSON 无法启动。
"""

from __future__ import annotations

import json
import os
from typing import Any

from astrbot.api import logger

from .paths import STATE_FILE


class StateStore:
    def __init__(self, path: str = STATE_FILE) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                return data
            logger.warning("GitHub Ling 状态文件内容不是对象，已忽略: %s", self.path)
        except Exception as exc:
            logger.error("读取 GitHub Ling 状态文件失败: %s", exc)
        return {}

    def save(
        self,
        *,
        bindings: dict[str, list[str]],
        watermarks: dict[str, str],
        broadcasts: list[str],
        active: dict[str, str] | None = None,
        binding_users: dict[str, dict[str, str]] | None = None,
    ) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "bindings": bindings,
                        "watermarks": watermarks,
                        "broadcasts": broadcasts,
                        # 会话 → 当前默认仓库（/ghissue 优先使用）
                        "active": active or {},
                        # 仓库 → {会话: 绑定者用户 ID}，用于跨会话（私聊 ↔ 群聊）回退
                        "binding_users": binding_users or {},
                    },
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as exc:
            logger.error("保存 GitHub Ling 状态文件失败: %s", exc)
