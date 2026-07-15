"""白名单校验模块（CLAUDE.md §6 安全注意事项——风险最高的部分）。

职责：
    1. 加载 config.yaml 的 whitelist 块（apps / commands / paths）
    2. 提供三类校验：应用名、命令名、文件路径
    3. 路径白名单用 pathlib.Path 比较，环境变量用 os.path.expandvars 展开
       （CLAUDE.md §8.8：统一 pathlib，不硬编码 C:\\Users\\xxx）

强制规则（CLAUDE.md §6 白名单机制）：
    - 任何不在白名单内的操作，一律拒绝
    - commands 必须用列表形式（subprocess 不传 shell=True），禁止任意命令拼接
    - dangerous: true 的命令由 shell_runner 负责触发二次确认，本模块只标记不拦截

路径白名单比较逻辑：
    用户给的文件路径 resolve() 后，必须 is_relative_to 某个白名单目录（同样 resolve()）。
    用 resolve() 而不是原样比较，是为了防符号链接、相对路径、.. 绕过。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)


class WhitelistError(Exception):
    """白名单模块错误基类。"""


@dataclass(frozen=True)
class CommandEntry:
    """白名单命令条目。command 是列表形式，直接传 subprocess.run，禁止 shell 拼接。"""

    name: str
    command: list[str]
    dangerous: bool


class Whitelist:
    """白名单校验器。实例化后只读，线程安全。

    用法：
        wl = Whitelist.from_config(config["whitelist"])
        app_path = wl.get_app_path("chrome")          # Path 或 None
        cmd = wl.get_command("lock_screen")            # CommandEntry 或 None
        if wl.is_path_allowed(some_path): ...
    """

    def __init__(
        self,
        apps: dict[str, Path],
        commands: dict[str, CommandEntry],
        paths: list[Path],
    ) -> None:
        self._apps = dict(apps)
        self._commands = dict(commands)
        # 白名单目录一律 resolve()，避免相对路径/符号链接绕过
        self._paths = [p.resolve() for p in paths]

    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, whitelist_cfg: dict) -> "Whitelist":
        """从 config.yaml 的 whitelist 块构造。

        缺字段按空处理（允许 commands/paths 为空），但 apps 必须存在——
        没有 apps 白名单等于不能开关任何应用，是配置错误。
        """
        apps_raw = whitelist_cfg.get("apps") or {}
        if not apps_raw:
            raise WhitelistError("whitelist.apps is empty: no applications can be controlled")

        apps: dict[str, Path] = {}
        for name, exe_path in apps_raw.items():
            # 应用 exe 路径原样存，不 resolve（程序可能尚未安装，resolve 会失败）
            apps[name] = Path(exe_path)

        commands: dict[str, CommandEntry] = {}
        for i, entry in enumerate(whitelist_cfg.get("commands") or []):
            try:
                name = entry["name"]
                command = list(entry["command"])
                dangerous = bool(entry.get("dangerous", False))
            except (KeyError, TypeError) as exc:
                raise WhitelistError(
                    f"whitelist.commands[{i}] malformed: missing key ({exc})"
                ) from exc
            if not isinstance(command, list) or not command:
                raise WhitelistError(
                    f"whitelist.commands[{i}] ({name}): command must be a non-empty list"
                )
            commands[name] = CommandEntry(name=name, command=command, dangerous=dangerous)

        paths: list[Path] = []
        for raw_path in whitelist_cfg.get("paths") or []:
            # 展开 %USERPROFILE% 等环境变量（CLAUDE.md §8.8）
            expanded = os.path.expandvars(raw_path)
            paths.append(Path(expanded))

        return cls(apps=apps, commands=commands, paths=paths)

    # ------------------------------------------------------------------
    def get_app_path(self, app_name: str) -> Optional[Path]:
        """返回应用 exe 路径，不在白名单返回 None。"""
        return self._apps.get(app_name)

    def list_app_names(self) -> list[str]:
        """白名单内全部应用名（调试/播报用）。"""
        return list(self._apps.keys())

    # ------------------------------------------------------------------
    def get_command(self, name: str) -> Optional[CommandEntry]:
        """返回命令条目，不在白名单返回 None。"""
        return self._commands.get(name)

    def list_command_names(self) -> list[str]:
        return list(self._commands.keys())

    # ------------------------------------------------------------------
    def is_path_allowed(self, path: Path) -> bool:
        """检查路径是否在白名单目录内。

        resolve() 后比较，防止 .. / 符号链接绕过。白名单为空时一律拒绝
        （CLAUDE.md §6：禁止系统目录，空白名单=全拒绝，保守）。
        """
        if not self._paths:
            return False
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            # 路径不存在或无法 resolve（如盘符无效），直接拒绝
            return False
        for allowed_dir in self._paths:
            try:
                if resolved.is_relative_to(allowed_dir):
                    return True
            except TypeError:
                # Python <3.9 is_relative_to 对跨盘符会抛 TypeError；3.10+ 返回 False
                continue
        return False

    # ------------------------------------------------------------------
    def list_allowed_paths(self) -> list[Path]:
        """白名单目录列表（resolve 后，调试/路径解析用）。"""
        return list(self._paths)
