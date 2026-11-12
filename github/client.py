"""极简 GitHub REST API 客户端（只覆盖插件用到的接口）。

除了通知所需的只读接口，还包含自动发 Issue 用到的小部分写接口：

* 读写仓库文件（Issue 模板、图片去重）
* 创建 Issue
* Git Data API（blob / tree / commit / ref），用于把聊天截图提交到一个专门的分支

所有请求共用同一套 TLS / 代理配置，登录态只依赖 ``github_token``。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from astrbot.api import logger

API_BASE = "https://api.github.com"


class GitHubAPI:
    def __init__(
        self,
        token: str = "",
        proxy: str = "",
        ca_bundle: str = "",
        ssl_verify: bool = True,
    ) -> None:
        self.token = (token or "").strip()
        self.proxy = (proxy or "").strip()
        self.ca_bundle = (ca_bundle or "").strip()
        self.ssl_verify = bool(ssl_verify)
        self.last_error: str | None = None
        self.last_status: int | None = None
        self.last_server_time: datetime | None = None
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ 底层

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "astrbot-plugin-github-ling",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _build_connector(self) -> aiohttp.TCPConnector | None:
        """构建 TLS 连接器。

        优先复用 AstrBot 的“系统信任库 + certifi”上下文，解决精简系统
        （容器 / 云镜像）缺少 CA 证书导致的 CERTIFICATE_VERIFY_FAILED。
        也可以通过 github_ca_bundle 指定自定义 CA，或关闭校验临时绕过。
        """
        try:
            if not self.ssl_verify:
                logger.warning(
                    "github_ssl_verify=false：已关闭 GitHub API 的 TLS 证书校验，"
                    "存在中间人风险，建议改为配置 github_ca_bundle"
                )
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                return aiohttp.TCPConnector(ssl=context)

            if self.ca_bundle:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_verify_locations(cafile=self.ca_bundle)
                logger.info("GitHub API 使用自定义 CA: %s", self.ca_bundle)
                return aiohttp.TCPConnector(ssl=context)

            try:
                from astrbot.core.utils.http_ssl import build_tls_connector

                return build_tls_connector()
            except Exception as exc:
                logger.debug("复用 AstrBot TLS 上下文失败，回退到 certifi: %s", exc)

            import certifi

            # 不依赖系统信任库，直接用 certifi 构建，规避系统 CA 配置损坏
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=certifi.where())
            return aiohttp.TCPConnector(ssl=context)
        except Exception as exc:
            logger.warning("构建 GitHub API TLS 连接器失败，使用 aiohttp 默认配置: %s", exc)
            return None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = self._build_connector()
            if connector is None:
                self._session = aiohttp.ClientSession()
            else:
                self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        allow_404: bool = False,
        timeout: int = 20,
    ) -> Any | None:
        """发起请求并返回解析后的 JSON。

        * 2xx：返回 JSON（204 返回空字典）
        * 404 且 ``allow_404=True``：静默返回 ``None``（用于“文件不存在”这类正常情况）
        * 其它错误：记录 ``last_error`` 并返回 ``None``
        """
        session = self._get_session()
        url = f"{API_BASE}{path}"
        self.last_error = None
        self.last_status = None
        try:
            async with session.request(
                method,
                url,
                params=params,
                json=body,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
                proxy=self.proxy or None,
            ) as response:
                self.last_status = response.status
                # 记录 GitHub 服务器时间，作为轮询水位线的基准，
                # 避免本机时钟偏差导致漏报或重复。
                date_header = response.headers.get("Date")
                if date_header:
                    try:
                        server_time = parsedate_to_datetime(date_header)
                        if server_time.tzinfo is None:
                            server_time = server_time.replace(tzinfo=timezone.utc)
                        self.last_server_time = server_time.astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        pass

                if 200 <= response.status < 300:
                    if response.status == 204:
                        return {}
                    text = await response.text()
                    if not text.strip():
                        return {}
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {"raw": text}

                text = await response.text()
                message = ""
                try:
                    payload = json.loads(text)
                    if isinstance(payload, dict):
                        message = str(payload.get("message") or "")
                except (json.JSONDecodeError, TypeError):
                    pass
                detail = message or text[:160] or "无返回内容"

                if response.status == 404 and allow_404:
                    return None

                if response.status == 403 and "rate limit" in detail.lower():
                    self.last_error = f"触发 GitHub API 速率限制（HTTP 403）：{detail}"
                    logger.warning("GitHub API 速率限制: %s %s", path, detail)
                elif response.status == 401:
                    self.last_error = f"认证失败（HTTP 401）：{detail}"
                    logger.warning("GitHub API 认证失败: %s %s", path, detail)
                elif response.status == 404:
                    self.last_error = f"资源不存在（HTTP 404）：{detail}"
                    logger.warning("GitHub API 请求失败 %s: 404 %s", path, detail)
                else:
                    self.last_error = f"HTTP {response.status}：{detail}"
                    logger.warning(
                        "GitHub API 请求失败 %s %s: %s",
                        response.status,
                        path,
                        detail,
                    )
                return None
        except asyncio.TimeoutError:
            self.last_error = f"连接 api.github.com 超时（{timeout} 秒）"
            logger.warning("访问 GitHub API 超时: %s", path)
            return None
        except aiohttp.ClientError as exc:
            detail = str(exc)
            is_ssl_error = (
                isinstance(
                    exc,
                    (
                        aiohttp.ClientSSLError,
                        aiohttp.ClientConnectorCertificateError,
                    ),
                )
                or "SSL" in detail
                or "CERTIFICATE" in detail
            )
            if is_ssl_error:
                self.last_error = (
                    "TLS 证书校验失败：服务器无法验证 api.github.com 的证书链。"
                    "通常是系统缺少 CA 证书（容器/精简镜像），可安装 ca-certificates，"
                    "或在插件配置中填写 github_ca_bundle 指向代理或企业根证书，"
                    "或临时设置 github_ssl_verify=false。原始错误：" + detail
                )
            else:
                self.last_error = f"网络错误：{detail}"
            logger.warning("访问 GitHub API 出错: %s %s", path, detail)
            return None

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any | None:
        return await self._request("GET", path, params=params)

    async def _download(
        self,
        url: str,
        timeout: int = 12,
        max_bytes: int = 4_000_000,
    ) -> bytes | None:
        session = self._get_session()
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                proxy=self.proxy or None,
            ) as response:
                if response.status != 200:
                    logger.debug("下载文件失败 %s: HTTP %s", url, response.status)
                    return None
                data = await response.read()
                if not data or len(data) > max_bytes:
                    return None
                return data
        except Exception as exc:
            logger.debug("下载文件失败 %s: %s", url, exc)
            return None

    async def download(
        self,
        url: str,
        timeout: int = 20,
        max_bytes: int = 8_000_000,
    ) -> bytes | None:
        """下载任意 URL（走同一套代理 / TLS 配置），失败返回 None。"""
        return await self._download(url, timeout=timeout, max_bytes=max_bytes)

    async def fetch_avatar(self, login: str) -> bytes | None:
        """下载用户头像，失败返回 None。

        先用 ``github.com/{login}.png``（不走 API 配额），失败再退回
        ``GET /users/{login}`` 拿 ``avatar_url`` 下载 —— 新注册用户、
        改过头像的账号在第一种方式上偶尔会失败或拿到占位图。
        """
        login = (login or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            return None

        for attempt in range(2):
            data = await self._download(
                f"https://github.com/{login}.png?size=200"
            )
            if data:
                return data
            if attempt == 0:
                await asyncio.sleep(0.6)

        profile = await self._get(f"/users/{login}")
        avatar_url = ""
        if isinstance(profile, dict):
            avatar_url = str(profile.get("avatar_url") or "")
        if avatar_url:
            separator = "&" if "?" in avatar_url else "?"
            data = await self._download(f"{avatar_url}{separator}s=200")
            if data:
                return data
        return None

    def server_now(self) -> datetime | None:
        """最近一次响应中的 GitHub 服务器时间（UTC）。"""
        return self.last_server_time

    # ------------------------------------------------------------------ 读

    async def get_rate_limit(self) -> dict[str, Any] | None:
        """查询速率限制。该接口不消耗 API 配额，适合用来做连通性诊断。"""
        data = await self._get("/rate_limit")
        return data if isinstance(data, dict) else None

    async def get_repo(self, repo: str) -> dict[str, Any] | None:
        """获取仓库信息，同时用于校验仓库是否存在。"""
        data = await self._get(f"/repos/{repo}")
        return data if isinstance(data, dict) else None

    async def get_contents(
        self,
        repo: str,
        path: str,
        ref: str | None = None,
    ) -> Any | None:
        """获取仓库目录 / 文件内容（contents API）。

        返回 ``list``（目录）、``dict``（文件元信息）或 ``None``（不存在 / 出错）。
        404 属于正常情况（例如仓库没有 ``.github/ISSUE_TEMPLATE``）。
        """
        params = {"ref": ref} if ref else None
        return await self._request(
            "GET",
            f"/repos/{repo}/contents/{path.lstrip('/')}",
            params=params,
            allow_404=True,
        )

    async def get_text_file(
        self,
        repo: str,
        path: str,
        ref: str | None = None,
    ) -> str | None:
        """读取仓库中的文本文件（模板、配置）。"""
        data = await self.get_contents(repo, path, ref)
        if not isinstance(data, dict):
            return None
        if data.get("type") not in (None, "file"):
            return None
        content = data.get("content")
        encoding = str(data.get("encoding") or "base64")
        if not isinstance(content, str):
            return None
        if encoding == "base64":
            try:
                raw = base64.b64decode(content)
            except Exception:
                return None
        else:
            raw = content.encode("utf-8")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")

    async def list_commits(
        self,
        repo: str,
        since: str,
        per_page: int = 30,
    ) -> list[dict[str, Any]] | None:
        """列出默认分支上 ``since`` 之后的提交（GitHub 按时间倒序返回）。

        返回 ``None`` 表示请求失败；空列表表示请求成功但没有新提交。
        """
        data = await self._get(
            f"/repos/{repo}/commits",
            {"since": since, "per_page": per_page},
        )
        return data if isinstance(data, list) else None

    async def list_issues(
        self,
        repo: str,
        since: str,
        per_page: int = 30,
    ) -> list[dict[str, Any]] | None:
        """列出 ``since`` 之后有更新的 Issue 与 PR（按 updated_at 倒序）。

        GitHub 的 issues 接口会把 PR 一并返回，PR 条目带有 ``pull_request`` 字段。
        返回 ``None`` 表示请求失败；空列表表示请求成功但没有更新。
        """
        data = await self._get(
            f"/repos/{repo}/issues",
            {
                "since": since,
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": per_page,
            },
        )
        return data if isinstance(data, list) else None

    async def get_latest_comment(
        self,
        repo: str,
        number: int | str,
        since: str | None = None,
    ) -> dict[str, Any] | None:
        """获取 Issue/PR 在 ``since`` 之后的最新一条会话评论。

        注意：``/issues/{n}/comments`` **不支持** ``sort`` / ``direction``，
        返回顺序是旧的在前；以前用 ``per_page=1`` 拿到的其实是最早的那条评论，
        导致新评论永远检测不到。这里改为按 ``created_at`` 取最大的一条。
        """
        params: dict[str, Any] = {"per_page": 100}
        if since:
            params["since"] = since
        data = await self._get(
            f"/repos/{repo}/issues/{number}/comments",
            params,
        )
        if not isinstance(data, list) or not data:
            return None
        return max(data, key=lambda item: str(item.get("created_at") or ""))

    async def list_reviews(
        self,
        repo: str,
        number: int,
        per_page: int = 100,
    ) -> list[dict[str, Any]] | None:
        """列出 PR 的审查记录（最新在最前）。

        PR 的 review 不在 issues/{n}/comments 里，必须单独走 pulls 接口。
        """
        data = await self._get(
            f"/repos/{repo}/pulls/{number}/reviews",
            {"per_page": per_page},
        )
        if not isinstance(data, list):
            return None
        return sorted(
            data,
            key=lambda item: str(item.get("submitted_at") or ""),
            reverse=True,
        )

    async def list_issue_events(
        self,
        repo: str,
        number: int | str,
        per_page: int = 100,
    ) -> list[dict[str, Any]] | None:
        """列出 Issue/PR 的事件（标签、指派、标题、状态、提交等）。"""
        data = await self._get(
            f"/repos/{repo}/issues/{number}/events",
            {"per_page": per_page},
        )
        return data if isinstance(data, list) else None

    async def list_labels(self, repo: str, per_page: int = 100) -> list[str] | None:
        """仓库已有的标签名。用于过滤模型给出的、仓库里并不存在的标签。"""
        data = await self._get(f"/repos/{repo}/labels", {"per_page": per_page})
        if not isinstance(data, list):
            return None
        return [
            str(item.get("name"))
            for item in data
            if isinstance(item, dict) and item.get("name")
        ]

    # ------------------------------------------------------------------ 写

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """创建 Issue，成功返回 Issue 数据（含 html_url / number）。"""
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        data = await self._request(
            "POST",
            f"/repos/{repo}/issues",
            body=payload,
            timeout=30,
        )
        return data if isinstance(data, dict) and data.get("number") else None

    async def create_comment(
        self,
        repo: str,
        number: int,
        body: str,
    ) -> dict[str, Any] | None:
        """在 Issue / PR 下发表评论，成功返回评论数据（含 html_url）。"""
        data = await self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            body={"body": body},
            timeout=30,
        )
        return data if isinstance(data, dict) and data.get("id") else None

    # ------------------------------------------------------------------ Git Data
    # 用来把聊天截图提交到独立分支，从而在 Issue 里以图片形式展示。

    async def create_blob(self, repo: str, content: bytes) -> str | None:
        data = await self._request(
            "POST",
            f"/repos/{repo}/git/blobs",
            body={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
            timeout=30,
        )
        if isinstance(data, dict) and data.get("sha"):
            return str(data["sha"])
        return None

    async def get_ref(self, repo: str, ref: str) -> dict[str, Any] | None:
        """读取 ref（如 ``heads/ling-assets``）；不存在返回 None。"""
        return await self._request(
            "GET",
            f"/repos/{repo}/git/ref/{ref.lstrip('/')}",
            allow_404=True,
        )

    async def create_ref(self, repo: str, branch: str, sha: str) -> bool:
        data = await self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        return isinstance(data, dict) and bool(data.get("ref"))

    async def update_ref(self, repo: str, branch: str, sha: str) -> bool:
        data = await self._request(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{branch}",
            body={"sha": sha, "force": False},
        )
        return isinstance(data, dict) and bool(data.get("ref"))

    async def get_commit(self, repo: str, sha: str) -> dict[str, Any] | None:
        data = await self._request("GET", f"/repos/{repo}/git/commits/{sha}")
        return data if isinstance(data, dict) else None

    async def get_base_commit(self, repo: str, branch: str) -> dict[str, Any] | None:
        """取分支最新提交。"""
        ref = await self.get_ref(repo, f"heads/{branch}")
        if not isinstance(ref, dict):
            return None
        obj = ref.get("object") or {}
        sha = str(obj.get("sha") or "")
        if not sha:
            return None
        return await self.get_commit(repo, sha)

    async def create_tree(
        self,
        repo: str,
        entries: list[dict[str, Any]],
        base_tree: str | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {"tree": entries}
        if base_tree:
            payload["base_tree"] = base_tree
        data = await self._request(
            "POST",
            f"/repos/{repo}/git/trees",
            body=payload,
            timeout=30,
        )
        if isinstance(data, dict) and data.get("sha"):
            return str(data["sha"])
        return None

    async def create_commit(
        self,
        repo: str,
        message: str,
        tree: str,
        parents: list[str],
    ) -> str | None:
        data = await self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            body={"message": message, "tree": tree, "parents": parents},
            timeout=30,
        )
        if isinstance(data, dict) and data.get("sha"):
            return str(data["sha"])
        return None

    async def file_exists(self, repo: str, path: str, ref: str) -> bool:
        data = await self._request(
            "GET",
            f"/repos/{repo}/contents/{path.lstrip('/')}",
            params={"ref": ref},
            allow_404=True,
        )
        return isinstance(data, dict) and bool(data.get("sha"))
