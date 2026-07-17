"""版本信息（CLAUDE.md 版本管理）。

提供 __version__（语义版本号）和 get_version_info()（版本 + git commit hash）。

git commit hash 获取优先级：
  1. subprocess 调 git rev-parse --short HEAD（源码模式正常工作）
  2. 读 core/_build_hash 静态文件（frozen exe 模式，打包脚本写入）
  3. 都失败返回 "unknown"
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

__version__ = "0.1.0"


def _git_hash() -> str:
    """尝试用 git 命令获取短 commit hash。失败返回空串。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _static_hash() -> str:
    """读打包时写入的静态 hash 文件。失败返回空串。"""
    # frozen：_build_hash 在 _MEIPASS 或 exe 同目录的 core/ 下
    if getattr(sys, "frozen", False):
        candidates = [
            Path(getattr(sys, "_MEIPASS", ".")) / "core" / "_build_hash",
            Path(sys.executable).resolve().parent / "core" / "_build_hash",
        ]
    else:
        candidates = [Path(__file__).resolve().parent / "_build_hash"]

    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue
    return ""


def get_commit_hash() -> str:
    """返回 git 短 commit hash，不可用时返回 "unknown"。"""
    return _git_hash() or _static_hash() or "unknown"


def get_version_info() -> str:
    """返回 "版本号+commit hash" 格式的完整版本信息字符串。"""
    commit = get_commit_hash()
    return f"{__version__}+{commit}"
