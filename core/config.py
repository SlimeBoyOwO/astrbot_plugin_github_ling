"""插件配置的统一门面。

AstrBot 的配置是 ``AstrBotConfig``（dict 的子类），键名写死在各处很容易手滑，
类型也常常是字符串。这里把它们一次性归一化成带类型的数据类：

* 所有模块只读 ``PluginConfig``，不直接翻 ``AstrBotConfig``；
* 配置改动后调用 :meth:`PluginConfig.refresh` 即可热更新，无需重载插件。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

_TRUE_WORDS = {"true", "1", "yes", "on", "y", "是", "开", "开启"}
_FALSE_WORDS = {"false", "0", "no", "off", "n", "", "否", "关", "关闭"}


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def as_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum:
        number = max(minimum, number)
    if maximum and number > maximum:
        number = maximum
    return number


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def as_list(value: Any) -> list[str]:
    """把配置里的列表 / 多行字符串 / 逗号分隔字符串统一成字符串列表。"""
    if isinstance(value, list | tuple | set):
        items = list(value)
    elif isinstance(value, str):
        items = value.replace("，", ",").replace(",", "\n").splitlines()
    else:
        items = []
    result: list[str] = []
    for item in items:
        text = as_str(item)
        if text and text not in result:
            result.append(text)
    return result


@dataclass(slots=True)
class PluginConfig:
    """插件全部配置项（带默认值，字段名与 ``_conf_schema.json`` 一致）。"""

    # GitHub
    github_token: str = ""
    github_proxy: str = ""
    github_ca_bundle: str = ""
    github_ssl_verify: bool = True

    # 轮询 / 卡片
    check_interval: int = 10
    send_card_image: bool = True
    card_style: str = "ling"
    card_width: int = 960
    comment_line_limit: int = 10
    max_commits_per_card: int = 5
    poll_comments: bool = True
    max_items_per_check: int = 3

    # 推送目标
    push_platform: str = ""
    push_targets: list[str] | None = None

    # Webhook
    enable_webhook: bool = False
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 6193
    webhook_path: str = "/github/webhook"
    webhook_secret: str = ""

    # 自动发 Issue
    issue_enabled: bool = True
    issue_admin_only: bool = True
    issue_admin_users: list[str] | None = None
    issue_require_confirm: bool = True
    issue_template_auto: bool = True
    issue_llm_provider: str = ""
    issue_max_context_chars: int = 6000
    issue_max_images: int = 4
    issue_asset_mode: str = "repo"
    issue_asset_branch: str = "ling-assets"
    issue_asset_repo: str = ""
    issue_timeout: int = 90

    # ------------------------------------------------------------------ 构建

    @classmethod
    def load(cls, raw: Any) -> PluginConfig:
        cfg = cls()
        cfg.refresh(raw)
        return cfg

    def refresh(self, raw: Any) -> None:
        """从 AstrBot 配置对象同步所有字段（可就地热更新）。"""
        get = raw.get if hasattr(raw, "get") else (lambda key, default=None: default)

        self.github_token = as_str(get("github_token", ""))
        self.github_proxy = as_str(get("github_proxy", ""))
        self.github_ca_bundle = as_str(get("github_ca_bundle", ""))
        self.github_ssl_verify = as_bool(get("github_ssl_verify", True), True)

        self.check_interval = as_int(get("check_interval", 10), 10, minimum=1)
        self.send_card_image = as_bool(get("send_card_image", True), True)
        style = as_str(get("card_style", "ling"), "ling").lower()
        self.card_style = style if style in {"ling", "markdown"} else "ling"
        self.card_width = as_int(get("card_width", 960), 960, minimum=480)
        self.comment_line_limit = as_int(
            get("comment_line_limit", 10), 10, minimum=1
        )
        self.max_commits_per_card = as_int(
            get("max_commits_per_card", 5), 5, minimum=1
        )
        self.poll_comments = as_bool(get("poll_comments", True), True)
        self.max_items_per_check = as_int(
            get("max_items_per_check", 3), 3, minimum=1
        )

        self.push_platform = as_str(get("push_platform", ""))
        self.push_targets = as_list(get("push_targets", []))

        self.enable_webhook = as_bool(get("enable_webhook", False), False)
        self.webhook_host = as_str(get("webhook_host", "0.0.0.0"), "0.0.0.0")
        self.webhook_port = as_int(
            get("webhook_port", 6193), 6193, minimum=1, maximum=65535
        )
        path = as_str(get("webhook_path", "/github/webhook"), "/github/webhook")
        self.webhook_path = path if path.startswith("/") else f"/{path}"
        self.webhook_secret = as_str(get("webhook_secret", ""))

        self.issue_enabled = as_bool(get("issue_enabled", True), True)
        self.issue_admin_only = as_bool(get("issue_admin_only", True), True)
        self.issue_admin_users = as_list(get("issue_admin_users", []))
        self.issue_require_confirm = as_bool(
            get("issue_require_confirm", True), True
        )
        self.issue_template_auto = as_bool(get("issue_template_auto", True), True)
        self.issue_llm_provider = as_str(get("issue_llm_provider", ""))
        self.issue_max_context_chars = as_int(
            get("issue_max_context_chars", 6000), 6000, minimum=500
        )
        self.issue_max_images = as_int(
            get("issue_max_images", 4), 4, minimum=0, maximum=10
        )
        mode = as_str(get("issue_asset_mode", "repo"), "repo").lower()
        self.issue_asset_mode = mode if mode in {"repo", "none"} else "repo"
        self.issue_asset_branch = as_str(
            get("issue_asset_branch", "ling-assets"), "ling-assets"
        )
        self.issue_asset_repo = as_str(get("issue_asset_repo", ""))
        self.issue_timeout = as_int(
            get("issue_timeout", 90), 90, minimum=20, maximum=600
        )

    # ------------------------------------------------------------------ 工具

    def as_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}
