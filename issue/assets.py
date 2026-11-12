"""把聊天里的截图放进 Issue。

GitHub 的 Issue 正文只接受图片 URL，没有“上传附件”的公开 API。这里采用
**Git Data API 提交到独立分支**的方式：

1. 把图片作为 blob 上传；
2. 在 ``ling-assets``（可配置）分支上创建一个只包含新图片的提交；
3. 用 ``raw.githubusercontent.com`` 的链接放进 Issue 正文。

这样做的好处是只依赖 ``api.github.com``，不需要第三方图床，也不需要
``github.com`` 网页上传接口（那个接口只认 Cookie）。代价是目标仓库需要一个
有写权限的 Token，并且会多出一个 ``ling-assets`` 分支。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from astrbot.api import logger

from ..core.config import PluginConfig
from ..github.client import GitHubAPI

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024


@dataclass(slots=True)
class UploadResult:
    urls: list[str] = field(default_factory=list)
    failed: int = 0
    note: str = ""


def image_extension(data: bytes) -> str | None:
    """按文件头判断图片格式，返回不带点的扩展名。"""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF8"):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    return None


def git_blob_sha(data: bytes) -> str:
    """计算 Git blob 的 SHA-1，用作文件名以实现内容去重。"""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def _file_uri_to_path(value: str) -> str:
    path = unquote(urlparse(value).path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return path


class AssetUploader:
    def __init__(self, api: GitHubAPI, config: PluginConfig) -> None:
        self.api = api
        self.config = config
        self._lock = asyncio.Lock()
        self._default_branch: dict[str, str] = {}

    # ------------------------------------------------------------------ 读取

    async def read_image(self, ref: str) -> bytes | None:
        """把各种图片引用读成字节。"""
        value = str(ref or "").strip()
        if not value:
            return None

        lower = value.lower()
        try:
            if lower.startswith(("http://", "https://")):
                return await self.api.download(
                    value, timeout=20, max_bytes=MAX_IMAGE_BYTES
                )
            if lower.startswith("base64://"):
                return base64.b64decode(value[len("base64://") :], validate=False)
            if lower.startswith("data:image/"):
                _, _, payload = value.partition(",")
                return base64.b64decode(payload, validate=False)
            if lower.startswith("file://"):
                value = _file_uri_to_path(value)
            if os.path.exists(value):
                size = os.path.getsize(value)
                if size <= 0 or size > MAX_IMAGE_BYTES:
                    return None
                with open(value, "rb") as handle:
                    return handle.read()
        except (binascii.Error, OSError, ValueError) as exc:
            logger.debug("读取图片失败 %s: %s", value, exc)
        return None

    # ------------------------------------------------------------------ 上传

    async def upload(self, repo: str, images: list[str]) -> UploadResult:
        """上传截图，返回可访问的 raw 链接列表。"""
        if not images:
            return UploadResult()
        if self.config.issue_asset_mode == "none":
            return UploadResult(
                failed=len(images),
                note="图片上传已在配置中关闭",
            )

        target_repo = self._target_repo(repo)
        branch = self._branch_name()

        # 1) 读取并筛选图片
        payloads: list[tuple[str, bytes, str]] = []
        failed = 0
        total = 0
        for ref in images:
            data = await self.read_image(ref)
            extension = image_extension(data or b"")
            if not data or not extension:
                failed += 1
                continue
            if total + len(data) > MAX_TOTAL_BYTES:
                failed += 1
                continue
            total += len(data)
            payloads.append((git_blob_sha(data), data, extension))
        if not payloads:
            return UploadResult(
                failed=failed,
                note="没有可上传的有效图片（可能是格式不支持或体积过大）",
            )

        # 2) 去重：同一张图只提交一次
        unique: dict[str, tuple[bytes, str]] = {}
        for sha, data, extension in payloads:
            unique.setdefault(sha, (data, extension))

        async with self._lock:
            try:
                urls = await self._commit_batch(target_repo, branch, unique)
            except Exception as exc:
                logger.warning("上传 Issue 截图失败: %s", exc)
                return UploadResult(
                    urls=[],
                    failed=len(images),
                    note=f"截图上传失败：{self.api.last_error or exc}",
                )
            if not urls:
                return UploadResult(
                    urls=[],
                    failed=len(images),
                    note=(
                        "截图上传失败："
                        f"{self.api.last_error or '目标仓库没有写入权限'}"
                    ),
                )

        # 保持调用方顺序
        ordered: list[str] = []
        for sha, _, extension in payloads:
            url = urls.get(sha)
            if url and url not in ordered:
                ordered.append(url)
        return UploadResult(urls=ordered, failed=failed, note="")

    def _target_repo(self, repo: str) -> str:
        """截图存放到哪个仓库：配置合法时用配置，否则退回目标仓库。"""
        configured = (self.config.issue_asset_repo or "").strip()
        if configured and re.fullmatch(r"[\w.-]+/[\w.-]+", configured):
            return configured
        if configured:
            logger.warning(
                "issue_asset_repo 格式不正确（%s），已退回目标仓库", configured
            )
        return repo

    def _branch_name(self) -> str:
        branch = re.sub(r"[^A-Za-z0-9._/-]", "-", self.config.issue_asset_branch or "")
        branch = branch.strip("/") or "ling-assets"
        if branch in {"main", "master"}:
            # 不要往主分支上提交截图
            branch = "ling-assets"
        return branch

    async def _commit_batch(
        self,
        repo: str,
        branch: str,
        items: dict[str, tuple[bytes, str]],
    ) -> dict[str, str]:
        ref = await self.api.get_ref(repo, f"heads/{branch}")
        branch_exists = False
        head_sha = ""
        if isinstance(ref, dict):
            obj = ref.get("object") or {}
            head_sha = str(obj.get("sha") or "")
            branch_exists = bool(head_sha)

        if not head_sha:
            base_commit = await self._default_commit(repo)
            if not base_commit:
                return {}
            head_sha = str(base_commit.get("sha") or "")

        head_commit = await self.api.get_commit(repo, head_sha) if head_sha else None
        base_tree = ""
        if isinstance(head_commit, dict):
            base_tree = str((head_commit.get("tree") or {}).get("sha") or "")

        # 1) 逐张上传 blob（已存在的文件直接复用链接）
        urls: dict[str, str] = {}
        entries: list[dict[str, str]] = []
        month = self._asset_prefix()
        for sha, (data, extension) in items.items():
            path = f"{month}/{sha}.{extension}"
            url = self._raw_url(repo, branch, path)
            if await self.api.file_exists(repo, path, branch):
                urls[sha] = url
                continue
            blob_sha = await self.api.create_blob(repo, data)
            if not blob_sha:
                logger.warning("上传截图 blob 失败: %s", self.api.last_error)
                continue
            entries.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            )
            urls[sha] = url

        if not entries:
            return urls

        # 2) 一次性提交
        tree_sha = await self.api.create_tree(repo, entries, base_tree or None)
        if not tree_sha:
            logger.warning("创建截图 tree 失败: %s", self.api.last_error)
            return {}
        commit_sha = await self.api.create_commit(
            repo,
            "chore: 上传 Issue 截图（GitHub Ling）",
            tree_sha,
            [head_sha] if head_sha else [],
        )
        if not commit_sha:
            logger.warning("创建截图 commit 失败: %s", self.api.last_error)
            return {}

        if branch_exists:
            ok = await self.api.update_ref(repo, branch, commit_sha)
        else:
            ok = await self.api.create_ref(repo, branch, commit_sha)
        if not ok:
            logger.warning("更新截图分支失败: %s", self.api.last_error)
            return {}
        return urls

    async def _default_commit(self, repo: str) -> dict | None:
        branch = self._default_branch.get(repo)
        if not branch:
            info = await self.api.get_repo(repo)
            if isinstance(info, dict):
                branch = str(info.get("default_branch") or "")
                if branch:
                    self._default_branch[repo] = branch
        if not branch:
            return None
        return await self.api.get_base_commit(repo, branch)

    @staticmethod
    def _asset_prefix() -> str:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return now.strftime("issue-assets/%Y%m")

    @staticmethod
    def _raw_url(repo: str, branch: str, path: str) -> str:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
