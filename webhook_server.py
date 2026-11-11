"""接收 GitHub Webhook 的轻量 HTTP 服务（基于 aiohttp，AstrBot 自带依赖）。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

from aiohttp import web

from astrbot.api import logger


class GitHubWebhookServer:
    def __init__(
        self,
        plugin: Any,
        host: str,
        port: int,
        path: str,
        secret: str = "",
    ) -> None:
        self.plugin = plugin
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith("/") else f"/{path}"
        self.secret = secret.encode("utf-8") if secret else None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        if self._runner is not None:
            return

        app = web.Application()
        app.router.add_post(self.path, self._handle_webhook)
        app.router.add_get(self.path, self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._runner = runner

        logger.info(
            "GitHub Ling Webhook 服务已启动: http://%s:%s%s",
            self.host,
            self.port,
            self.path,
        )
        if not self.secret:
            logger.warning(
                "未设置 Webhook Secret，建议配置以校验请求来源（X-Hub-Signature-256）"
            )

    async def stop(self) -> None:
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        logger.info("GitHub Ling Webhook 服务已关闭")

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="github ling webhook ok")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        raw = await request.read()

        if self.secret:
            signature = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                self.secret, raw, hashlib.sha256
            ).hexdigest()
            if not signature or not hmac.compare_digest(signature, expected):
                logger.warning("收到签名无效的 GitHub Webhook 请求")
                return web.Response(status=401, text="invalid signature")

        event_type = request.headers.get("X-GitHub-Event", "")
        if not event_type:
            return web.Response(status=400, text="missing event")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("GitHub Webhook payload 不是合法 JSON")
            return web.Response(status=400, text="invalid payload")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="invalid payload")

        # GitHub 要求 3 秒内响应，先回 200 再异步处理
        asyncio.create_task(self._dispatch(event_type, payload))
        return web.Response(text="ok")

    async def _dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            await self.plugin.handle_webhook_event(event_type, payload)
        except Exception:
            logger.exception("处理 GitHub Webhook 事件时出错")
