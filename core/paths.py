"""插件用到的磁盘路径，集中一处方便排查。"""

from __future__ import annotations

import os

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    DATA_DIR = str(get_astrbot_data_path())
except Exception:  # 兼容旧版本 AstrBot / 离线测试
    DATA_DIR = "data"

STATE_FILE = os.path.join(DATA_DIR, "github_ling_state.json")
AVATAR_DIR = os.path.join(DATA_DIR, "github_ling_avatars")
__all__ = ["DATA_DIR", "STATE_FILE", "AVATAR_DIR"]
