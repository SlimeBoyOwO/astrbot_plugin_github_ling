"""把聊天里的截图放进 Issue。

GitHub 的 Issue 正文只接受图片 URL，没有写进文档的“上传附件”API。插件支持
两种落盘方式（``issue_asset_mode``）：

``attachment``（默认）
    调用网页版评论框同款接口 ``POST https://uploads.github.com/user-attachments/assets``。
    上传成功后拿到 ``https://github.com/user-attachments/assets/<uuid>`` 链接，
    和手动往评论框里拖图片得到的是同一种地址。**不产生任何提交**，也不占
    仓库空间；代价是接口未写进官方文档（GitHub CLI 2.99 起的 ``--attach``
    也在用它），并且要求 Token 账号对目标仓库有 Write 权限。

``repo``
    用 Git Data API 把图片提交到 ``ling-assets``（可配置）分支，再用
    ``raw.githubusercontent.com`` 的链接放进正文。多一个分支和若干提交，
    但完全不依赖未公开接口。

两种模式都只依赖 API / uploads 域名，不需要第三方图床，也不需要
``github.com`` 的 Cookie 会话。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from astrbot.api import logger

from ..core.config import PluginConfig
from ..github.client import GitHubAPI

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024

# GitHub 附件存储接受的图片类型（bmp / svg 之外的冷门格式会被 422 拒掉）
CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


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
        # 仓库写权限缓存：repo -> (时间戳, 是否可写)
        self._push_cache: dict[str, tuple[float, bool]] = {}
        # 仓库数字 id 缓存：repo -> (时间戳, id)
        self._repo_cache: dict[str, tuple[float, int]] = {}

    # ------------------------------------------------------------------ 读取

    async def read_image(self, ref: str) -> bytes | None:
        """把各种图片引用读成字节。

        网络图片优先用插件自己的 HTTP 客户端下载；失败时再交给 AstrBot 的
        ``Image.convert_to_file_path()`` 落盘（它会复用 AstrBot 的下载逻辑，
        对 QQ 图床的兼容性更好）。这样即使图片链接需要特定 UA / 临时缓存，
        也更容易拿到真正的图片。
        """
        value = str(ref or "").strip()
        if not value:
            return None

        lower = value.lower()
        try:
            if lower.startswith(("http://", "https://")):
                data = await self.api.download(
                    value, timeout=20, max_bytes=MAX_IMAGE_BYTES
                )
                if data and image_extension(data):
                    return data
                cached = await self._read_via_astrbot(value)
                return cached or data
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

    @staticmethod
    async def _read_via_astrbot(url: str) -> bytes | None:
        """让 AstrBot 把网络图片缓存到本地，再读成字节。"""
        try:
            from astrbot.api.message_components import Image

            path = await Image.fromURL(url).convert_to_file_path()
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                if 0 < size <= MAX_IMAGE_BYTES:
                    with open(path, "rb") as handle:
                        return handle.read()
        except Exception as exc:  # 老版本 AstrBot / 下载失败都走这里
            logger.debug("AstrBot 缓存图片失败: %s", exc)
        return None

    # ------------------------------------------------------------------ 上传

    async def upload(self, repo: str, images: list[str]) -> UploadResult:
        """上传截图，返回可访问的图片链接列表。"""
        if not images:
            return UploadResult()
        if self.config.issue_asset_mode == "none":
            return UploadResult(
                failed=len(images),
                note="图片上传已在配置中关闭",
            )

        target_repo = self._target_repo(repo)
        branch = self._branch_name()

        if await self._cannot_push(target_repo):
            return UploadResult(
                urls=[],
                failed=len(images),
                note=self._no_write_note(target_repo),
            )

        # 1) 读取并筛选图片
        payloads: list[tuple[str, bytes, str]] = []
        failed = 0
        reasons: list[str] = []
        total = 0
        for ref in images:
            data = await self.read_image(ref)
            if not data:
                failed += 1
                reasons.append("下载失败")
                continue
            extension = image_extension(data)
            if not extension:
                failed += 1
                reasons.append("格式不支持")
                continue
            if total + len(data) > MAX_TOTAL_BYTES:
                failed += 1
                reasons.append("体积过大")
                continue
            total += len(data)
            payloads.append((git_blob_sha(data), data, extension))
        if not payloads:
            detail = "、".join(dict.fromkeys(reasons)) or "没有可用图片"
            return UploadResult(
                failed=failed,
                note=(
                    f"没有可上传的图片（{failed} 张：{detail}）。"
                    "图片链接可能已过期，或服务器无法访问该图片地址"
                ),
            )

        # 2) 去重：同一张图只提交一次
        unique: dict[str, tuple[bytes, str]] = {}
        for sha, data, extension in payloads:
            unique.setdefault(sha, (data, extension))

        if self.config.issue_asset_mode == "attachment":
            repo_id = await self._repo_id(target_repo)
            if not repo_id:
                return UploadResult(
                    urls=[],
                    failed=len(images),
                    note=(
                        "截图上传失败：读取仓库信息失败"
                        f"（{self.api.last_error or '未知错误'}）"
                    ),
                )
            urls, stage_error = await self._upload_attachments(repo_id, unique)
        else:
            async with self._lock:
                try:
                    urls, stage_error = await self._commit_batch(
                        target_repo, branch, unique
                    )
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
                note=self._failure_note(stage_error),
            )

        # 保持调用方顺序
        ordered: list[str] = []
        for sha, _, extension in payloads:
            url = urls.get(sha)
            if url and url not in ordered:
                ordered.append(url)

        note = stage_error
        if failed:
            detail = "、".join(dict.fromkeys(reasons))
            partial = f"{failed} 张图片未能上传（{detail}）"
            note = f"{note}；{partial}" if note else partial
        return UploadResult(urls=ordered, failed=failed, note=note)

    async def _cannot_push(self, repo: str) -> bool:
        """预检查写权限：明确没有 push 权限时直接给出可读的提示。"""
        cached = self._push_cache.get(repo)
        if cached and time.time() - cached[0] < 600:
            return not cached[1]
        info = await self.api.get_repo(repo)
        if not isinstance(info, dict):
            return False
        permissions = info.get("permissions")
        if not isinstance(permissions, dict):
            return False
        can_push = bool(permissions.get("push"))
        self._push_cache[repo] = (time.time(), can_push)
        return not can_push

    async def _repo_id(self, repo: str) -> int | None:
        """仓库的数字 id（附件接口只认 REST 的 id，不是 GraphQL 的 node id）。"""
        cached = self._repo_cache.get(repo)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        info = await self.api.get_repo(repo)
        if not isinstance(info, dict):
            return None
        try:
            repo_id = int(info.get("id"))
        except (TypeError, ValueError):
            return None
        self._repo_cache[repo] = (time.time(), repo_id)
        return repo_id

    async def _upload_attachments(
        self,
        repo_id: int,
        items: dict[str, tuple[bytes, str]],
    ) -> tuple[dict[str, str], str]:
        """逐张上传到 GitHub 附件存储，返回 ``(sha -> 链接, 失败原因)``。"""
        urls: dict[str, str] = {}
        errors: list[str] = []
        for sha, (data, extension) in items.items():
            content_type = CONTENT_TYPES.get(extension)
            if not content_type:
                errors.append(f"{extension} 格式不受支持")
                continue
            name = f"ling-{sha[:12]}.{extension}"
            link = await self.api.upload_attachment(
                repo_id, data, name, content_type
            )
            if link:
                urls[sha] = link
            elif self.api.last_error:
                errors.append(str(self.api.last_error))
        if not urls:
            return {}, f"上传图片失败：{errors[0] if errors else '未知错误'}"
        partial = ""
        if errors:
            detail = "、".join(dict.fromkeys(errors))
            partial = f"{len(errors)} 张图片未能上传（{detail}）"
        return urls, partial

    def _no_write_note(self, repo: str) -> str:
        action = "截图上传" if self.config.issue_asset_mode == "attachment" else "提交截图"
        return (
            f"截图上传失败：Token 所属账号对 {repo} 没有写入权限。"
            f"{action}要求该账号是仓库的协作者（Write 权限）；"
            "classic Token 需要 repo / public_repo 范围，"
            "fine-grained Token 需要 Contents: Read and write。"
            "也可以把 issue_asset_repo 指向一个该账号有写权限的仓库，"
            "或把 issue_asset_mode 设为 none"
        )

    def _failure_note(self, stage_error: str) -> str:
        note = f"截图上传失败：{stage_error or '目标仓库没有写入权限'}"
        if self.api.last_status in {401, 403, 404}:
            note += (
                "（GitHub 对无写权限的仓库可能返回 404/403："
                "请确认 Token 所属账号已被加为该仓库的协作者，"
                "classic Token 勾选 repo 或 public_repo，"
                "fine-grained Token 勾选 Contents: Read and write）"
            )
        return note

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
    ) -> tuple[dict[str, str], str]:
        """把图片提交到截图分支，返回 ``(sha -> raw 链接, 失败步骤)``。

        分支不存在时直接创建一个**只包含图片**的根提交，不复制默认分支的
        整棵树 —— 既快，也避免因为读取默认分支失败而中断上传。
        """
        ref = await self.api.get_ref(repo, f"heads/{branch}")
        branch_exists = False
        head_sha = ""
        if isinstance(ref, dict):
            head_sha = str((ref.get("object") or {}).get("sha") or "")
            branch_exists = bool(head_sha)

        base_tree = ""
        if branch_exists:
            head_commit = await self.api.get_commit(repo, head_sha)
            if not isinstance(head_commit, dict):
                return {}, self._stage_error("读取截图分支")
            base_tree = str((head_commit.get("tree") or {}).get("sha") or "")

        # 1) 逐张上传 blob（分支已存在时，已提交过的文件直接复用链接）
        existing: dict[str, str] = {}
        new_urls: dict[str, str] = {}
        entries: list[dict[str, str]] = []
        month = self._asset_prefix()
        for sha, (data, extension) in items.items():
            path = f"{month}/{sha}.{extension}"
            url = self._raw_url(repo, branch, path)
            if branch_exists and await self.api.file_exists(repo, path, branch):
                existing[sha] = url
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
            new_urls[sha] = url

        if not entries:
            if existing:
                return existing, ""
            return {}, self._stage_error("上传图片")

        # 2) 一次性提交（新分支是一个只含图片的根提交）
        tree_sha = await self.api.create_tree(repo, entries, base_tree or None)
        if not tree_sha:
            logger.warning("创建截图 tree 失败: %s", self.api.last_error)
            return existing, self._stage_error("创建文件树")
        commit_sha = await self.api.create_commit(
            repo,
            "chore: 上传 Issue 截图（GitHub Ling）",
            tree_sha,
            [head_sha] if branch_exists and head_sha else [],
        )
        if not commit_sha:
            logger.warning("创建截图 commit 失败: %s", self.api.last_error)
            return existing, self._stage_error("创建提交")

        if branch_exists:
            ok = await self.api.update_ref(repo, branch, commit_sha)
        else:
            ok = await self.api.create_ref(repo, branch, commit_sha)
        if not ok:
            logger.warning("更新截图分支失败: %s", self.api.last_error)
            return existing, self._stage_error(
                "更新截图分支" if branch_exists else "创建截图分支"
            )
        return {**existing, **new_urls}, ""

    def _stage_error(self, stage: str) -> str:
        return f"{stage}失败：{self.api.last_error or '未知错误'}"

    @staticmethod
    def _asset_prefix() -> str:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return now.strftime("issue-assets/%Y%m")

    @staticmethod
    def _raw_url(repo: str, branch: str, path: str) -> str:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
