"""已安装应用索引（CLAUDE.md §6 修订 15：打开应用放开为全机已安装应用）。

索引来源（只读扫描，不修改系统）：
    1. 开始菜单快捷方式：%ProgramData% 和 %AppData% 下的
       Start Menu\\Programs 递归收集 .lnk。快捷方式名就是用户认知的应用名
       （中文软件是中文名，如"微信.lnk"），os.startfile 可直接启动 .lnk，
       不需要 COM 解析目标路径。
    2. 注册表 App Paths（HKLM/HKCU
       Software\\Microsoft\\Windows\\CurrentVersion\\App Paths）：
       键名形如 chrome.exe，默认值是 exe 完整路径。覆盖部分没有开始菜单
       快捷方式的程序。

匹配规则（§6 修订 15：确定性匹配，不是模糊放行）：
    1. 精确匹配：归一化（casefold + 去空格）后全等
    2. 唯一子串匹配：查询词是且仅是一个候选名的子串才放行
    3. 命中多个候选 → AMBIGUOUS，返回候选列表让用户说得更具体
    4. 命中零个 → NOT_FOUND
    不做"选相似度最高的"这类猜测执行。

安全约束：
    - 排除卸载类快捷方式（名称含 uninstall / 卸载），防止"打开 XX"
      唯一子串命中"卸载 XX"
    - 索引只在启动时构建一次，运行期只读
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)



# 排除的快捷方式名关键词（小写比较）：卸载器绝不能被"打开 XX"误命中
_EXCLUDE_KEYWORDS = ("uninstall", "卸载", "uninst")

# 开始菜单扫描目录（相对环境变量，运行时展开）
_START_MENU_DIRS = (
    r"%ProgramData%\Microsoft\Windows\Start Menu\Programs",
    r"%AppData%\Microsoft\Windows\Start Menu\Programs",
)

_APP_PATHS_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths"


class MatchStatus(Enum):
    FOUND = "found"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class AppEntry:
    """索引条目。target 是 os.startfile 可直接启动的路径（.lnk 或 .exe）。"""
    display_name: str   # 用户认知的名字，如 "微信"、"Google Chrome"
    target: Path        # .lnk 快捷方式路径或 exe 完整路径
    source: str         # "start_menu" / "app_paths"


@dataclass(frozen=True)
class MatchResult:
    """resolve() 的返回：status + 命中条目（FOUND 时）+ 候选名（AMBIGUOUS 时）。"""
    status: MatchStatus
    entry: Optional[AppEntry] = None
    candidates: tuple[str, ...] = ()


def _normalize(name: str) -> str:
    """匹配用归一化：casefold + 去所有空白。"""
    return "".join(name.split()).casefold()


class AppIndex:
    """已安装应用索引。启动时构建一次，运行期只读。

    用法：
        index = AppIndex.build()
        m = index.resolve("微信")
        if m.status == MatchStatus.FOUND:
            os.startfile(str(m.entry.target))
    """

    def __init__(self, entries: dict[str, AppEntry]) -> None:
        # key 是归一化后的 display_name
        self._entries = dict(entries)

    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        start_menu_dirs: Optional[list[Path]] = None,
        scan_registry: bool = True,
    ) -> "AppIndex":
        """构建索引。

        Args:
            start_menu_dirs: 覆盖默认开始菜单目录（测试注入用），None = 系统默认。
            scan_registry: 是否扫描注册表 App Paths（测试时可关）。
        """
        entries: dict[str, AppEntry] = {}

        if start_menu_dirs is None:
            start_menu_dirs = [
                Path(os.path.expandvars(d)) for d in _START_MENU_DIRS
            ]

        # 1. 开始菜单 .lnk（优先级高：名字最贴近用户口语，直接覆盖 App Paths 同名项）
        lnk_count = 0
        for base in start_menu_dirs:
            if not base.is_dir():
                continue
            try:
                for lnk in base.rglob("*.lnk"):
                    name = lnk.stem
                    if any(kw in name.casefold() for kw in _EXCLUDE_KEYWORDS):
                        continue
                    key = _normalize(name)
                    if not key:
                        continue
                    entries[key] = AppEntry(
                        display_name=name, target=lnk, source="start_menu",
                    )
                    lnk_count += 1
            except OSError as exc:
                _LOG.warning("scan start menu dir %s failed: %s", base, exc)

        # 2. 注册表 App Paths（不覆盖已有的开始菜单条目）
        reg_count = 0
        if scan_registry:
            reg_count = cls._scan_app_paths(entries)

        _LOG.info(
            "AppIndex built: %d entries (%d from start menu, %d from App Paths)",
            len(entries), lnk_count, reg_count,
        )
        return cls(entries)

    @staticmethod
    def _scan_app_paths(entries: dict[str, AppEntry]) -> int:
        """扫描注册表 App Paths，补充没有开始菜单快捷方式的程序。"""
        try:
            import winreg
        except ImportError:
            return 0  # 非 Windows（不应发生，防御）

        added = 0
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                root = winreg.OpenKey(hive, _APP_PATHS_SUBKEY)
            except OSError:
                continue
            with root:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    # 键名形如 "chrome.exe"，display_name 取 stem
                    name = Path(subkey_name).stem
                    if any(kw in name.casefold() for kw in _EXCLUDE_KEYWORDS):
                        continue
                    key = _normalize(name)
                    if not key or key in entries:
                        continue  # 开始菜单条目优先，不覆盖
                    try:
                        with winreg.OpenKey(root, subkey_name) as sk:
                            exe_path, _ = winreg.QueryValueEx(sk, "")
                    except OSError:
                        continue
                    if not exe_path or not isinstance(exe_path, str):
                        continue
                    target = Path(os.path.expandvars(exe_path.strip('"')))
                    entries[key] = AppEntry(
                        display_name=name, target=target, source="app_paths",
                    )
                    added += 1
        return added

    # ------------------------------------------------------------------
    def resolve(self, query: str) -> MatchResult:
        """按 §6 修订 15 的确定性规则解析应用名。

        精确匹配 → 唯一子串匹配 → AMBIGUOUS（多候选）/ NOT_FOUND（零候选）。
        """
        q = _normalize(query)
        if not q:
            return MatchResult(status=MatchStatus.NOT_FOUND)

        # 1. 精确匹配
        exact = self._entries.get(q)
        if exact is not None:
            return MatchResult(status=MatchStatus.FOUND, entry=exact)

        # 2. 子串匹配：查询词是候选名的子串
        hits = [e for k, e in self._entries.items() if q in k]
        if len(hits) == 1:
            return MatchResult(status=MatchStatus.FOUND, entry=hits[0])
        if len(hits) > 1:
            # 命中多个：拒绝执行，把候选名带回去播报（最多 5 个，避免播报冗长）
            names = tuple(sorted(e.display_name for e in hits)[:5])
            return MatchResult(status=MatchStatus.AMBIGUOUS, candidates=names)

        return MatchResult(status=MatchStatus.NOT_FOUND)

    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def list_names(self, limit: int = 0) -> list[str]:
        """全部应用显示名（调试用）。limit > 0 时截断。"""
        names = sorted(e.display_name for e in self._entries.values())
        return names[:limit] if limit > 0 else names


__all__ = ["AppIndex", "AppEntry", "MatchResult", "MatchStatus"]
