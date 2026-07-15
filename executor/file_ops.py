"""文件操作模块（CLAUDE.md §5 Step 4：open_file 意图）。

核心难点：用户口头说的路径是自然语言（"桌面上的 test.txt"），需要解析成
实际 Path 并校验白名单（CLAUDE.md §6 paths 白名单 + §8.8 pathlib 规范）。

解析策略（按优先级）：
    1. 完整路径：path 是绝对路径或能直接 resolve 的相对路径 → 校验白名单
    2. 位置关键词：识别"桌面/文档/下载"等中文位置词 + 文件名 → 拼接对应白名单目录
    3. 全局搜索：在所有白名单目录里按文件名查找（仅当文件名无路径分隔符时）

白名单校验（CLAUDE.md §6 强制）：
    resolve() 后必须 is_relative_to 某个白名单目录，防止 ../ 绕过。
    系统目录（C:\\Windows 等）不在白名单内，天然被拦。

打开方式：os.startfile，走 Windows 文件关联（.txt 用记事本，.docx 用 Word 等）。

MVP 范围（CLAUDE.md §4）：只做"打开文件"，不做删除/修改/移动。
删除文件虽不在 MVP，但 §6 二次确认规则已立，本模块不实现删除接口。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from executor import ExecutionResult
from executor.whitelist import Whitelist
from nlu.schema import OpenFileIntent

_LOG = logging.getLogger(__name__)


class FileOpsError(Exception):
    """文件操作模块错误基类。"""


# 位置关键词 → 目录名匹配规则。键是用户口头可能说的中文词，
# 值是白名单目录 Path.name 需要匹配的关键词（小写比较）。
_LOCATION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "桌面": ("desktop", "桌面"),
    "桌面上": ("desktop", "桌面"),
    "桌面的": ("desktop", "桌面"),
    "文档": ("documents", "文档", "my documents"),
    "文档的": ("documents", "文档", "my documents"),
    "下载": ("downloads", "下载"),
    "下载的": ("downloads", "下载"),
}

# 英文位置词（大小写不敏感）
_LOCATION_KEYWORDS_EN: dict[str, tuple[str, ...]] = {
    "desktop": ("desktop",),
    "documents": ("documents", "my documents"),
    "downloads": ("downloads",),
}


class FileOps:
    """文件操作。共享 Whitelist 实例（只读）。"""

    def __init__(self, whitelist: Whitelist) -> None:
        self._whitelist = whitelist

    # ------------------------------------------------------------------
    def open_file(self, intent: OpenFileIntent) -> ExecutionResult:
        raw_path = intent.path
        if not raw_path or not raw_path.strip():
            return ExecutionResult(
                success=False,
                action_type="open_file",
                error="文件路径为空",
                payload={"raw_path": raw_path},
            )

        resolved, error = self._resolve_path(raw_path)
        if resolved is None:
            return ExecutionResult(
                success=False,
                action_type="open_file",
                error=error,
                payload={"raw_path": raw_path},
            )

        if not resolved.exists():
            return ExecutionResult(
                success=False,
                action_type="open_file",
                error=f"文件不存在: {resolved}",
                payload={"raw_path": raw_path, "resolved_path": str(resolved)},
            )

        if not resolved.is_file():
            return ExecutionResult(
                success=False,
                action_type="open_file",
                error=f"路径不是文件: {resolved}",
                payload={"raw_path": raw_path, "resolved_path": str(resolved)},
            )

        try:
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        except OSError as exc:
            _LOG.warning("os.startfile(%s) failed: %s", resolved, exc)
            return ExecutionResult(
                success=False,
                action_type="open_file",
                error=f"打开文件失败: {exc}",
                payload={"raw_path": raw_path, "resolved_path": str(resolved)},
            )

        return ExecutionResult(
            success=True,
            action_type="open_file",
            payload={
                "raw_path": raw_path,
                "resolved_path": str(resolved),
                "filename": resolved.name,
            },
        )

    # ------------------------------------------------------------------
    def _resolve_path(self, raw_path: str) -> tuple[Optional[Path], Optional[str]]:
        """把用户说的路径解析成实际 Path，并校验白名单。

        Returns:
            (resolved_path, None) 成功
            (None, error_message) 失败
        """
        raw_path = raw_path.strip()

        # 策略 1：完整路径（含盘符或路径分隔符）
        if "\\" in raw_path or "/" in raw_path or re.match(r"^[A-Za-z]:", raw_path):
            try:
                candidate = Path(raw_path).resolve()
            except (OSError, RuntimeError) as exc:
                return None, f"路径解析失败: {exc}"
            if self._whitelist.is_path_allowed(candidate):
                return candidate, None
            return None, f"路径不在允许范围内: {candidate}"

        # 策略 2：位置关键词 + 文件名（"桌面上的 test.txt"）
        location_dir, filename = self._parse_location_and_filename(raw_path)
        if location_dir is not None and filename:
            candidate = location_dir / filename
            try:
                candidate_resolved = candidate.resolve()
            except (OSError, RuntimeError) as exc:
                return None, f"路径解析失败: {exc}"
            if self._whitelist.is_path_allowed(candidate_resolved):
                return candidate_resolved, None
            return None, f"路径不在允许范围内: {candidate}"

        # 策略 3：纯文件名，在所有白名单目录里找
        if "\\" not in raw_path and "/" not in raw_path:
            for base_dir in self._whitelist.list_allowed_paths():
                candidate = base_dir / raw_path
                if candidate.exists() and candidate.is_file():
                    try:
                        candidate_resolved = candidate.resolve()
                    except (OSError, RuntimeError):
                        continue
                    if self._whitelist.is_path_allowed(candidate_resolved):
                        return candidate_resolved, None

        return None, f"无法解析路径或路径不在允许范围内: {raw_path!r}"

    # ------------------------------------------------------------------
    def _parse_location_and_filename(self, raw_path: str) -> tuple[Optional[Path], str]:
        """从自然语言描述里提取位置目录和文件名。

        "桌面上的 test.txt" → (Desktop_dir, "test.txt")
        "文档里的 报告.docx" → (Documents_dir, "报告.docx")
        "test.txt"          → (None, "test.txt")
        """
        raw_lower = raw_path.lower()

        # 先匹配中文位置词（精确匹配前缀，最长优先）
        for keyword in sorted(_LOCATION_KEYWORDS.keys(), key=len, reverse=True):
            if raw_path.startswith(keyword):
                filename = raw_path[len(keyword):].strip()
                # 去掉连接词"的/里/中"等
                filename = re.sub(r"^[的里中上]+\s*", "", filename)
                if not filename:
                    continue
                dir_path = self._find_location_dir(_LOCATION_KEYWORDS[keyword])
                return dir_path, filename

        # 再匹配英文位置词
        for keyword in sorted(_LOCATION_KEYWORDS_EN.keys(), key=len, reverse=True):
            if raw_lower.startswith(keyword):
                filename = raw_path[len(keyword):].strip()
                filename = re.sub(r"^[\s'on]+\s*", "", filename, flags=re.IGNORECASE)
                # 去掉 "on"/"in" 等英文连接词
                filename = re.sub(r"^(on|in)\s+", "", filename, flags=re.IGNORECASE)
                if not filename:
                    continue
                dir_path = self._find_location_dir(_LOCATION_KEYWORDS_EN[keyword])
                return dir_path, filename

        return None, raw_path

    # ------------------------------------------------------------------
    def _find_location_dir(self, name_keywords: tuple[str, ...]) -> Optional[Path]:
        """在白名单目录里找名字匹配的目录。

        name_keywords 是小写关键词，白名单目录 Path.name 小写匹配任一即命中。
        """
        for base_dir in self._whitelist.list_allowed_paths():
            dir_name_lower = base_dir.name.lower()
            if any(kw in dir_name_lower for kw in name_keywords):
                return base_dir
        return None
