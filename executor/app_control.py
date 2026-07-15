"""应用控制模块（CLAUDE.md §5 Step 4：open_app / close_app 意图）。

open_app（§6 修订 15）：先查 whitelist.apps 别名表（用户自定义优先），
    未命中再查 AppIndex（开始菜单 + App Paths 索引），都没有才拒绝。
    索引未启用（allow_open_any_app: false）时恢复旧行为：仅白名单。
close_app：查白名单 → 二次确认 → psutil 找进程并 terminate。
    关闭 = 杀进程可能丢未保存数据，不放开白名单（§6 修订 15 明确保留）。

二次确认策略（CLAUDE.md §6）：
    "关闭正在运行且可能有未保存内容的应用"需要确认。MVP 采用保守策略——
    所有 close_app 一律二次确认，不区分应用类型。理由：任何 GUI 应用都
    可能有未保存内容（notepad 有未保存文本，chrome 有未提交表单），
    保守策略实现简单且安全，误判代价（多问一句）远低于漏判代价（丢数据）。

进程匹配：
    白名单里存的是 exe 路径（如 chrome.exe 的完整路径），关闭时按 exe
    文件名匹配进程（process.name() == "chrome.exe"）。chrome 等多进程
    应用会关闭所有匹配进程。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import psutil

from executor import ExecutionResult
from executor.app_index import AppIndex, MatchStatus
from executor.whitelist import Whitelist

from nlu.schema import CloseAppIntent, OpenAppIntent

_LOG = logging.getLogger(__name__)


class AppControlError(Exception):
    """应用控制模块错误基类。"""


class AppController:
    """打开/关闭应用程序。共享 Whitelist 实例（只读）。

    app_index 为 None 时打开只限白名单（allow_open_any_app: false 的旧行为）。
    """

    def __init__(self, whitelist: Whitelist, app_index: Optional[AppIndex] = None) -> None:
        self._whitelist = whitelist
        self._app_index = app_index

    # ------------------------------------------------------------------
    def open_app(self, intent: OpenAppIntent) -> ExecutionResult:
        app_name = intent.app_name

        # 1. 白名单别名优先（用户自定义映射，如 chrome → 指定路径）
        app_path = self._whitelist.get_app_path(app_name)
        if app_path is not None:
            return self._launch(app_name, app_path, resolved_via="whitelist")

        # 2. 应用索引兜底（§6 修订 15：打开放开为全机已安装应用）
        if self._app_index is not None:
            match = self._app_index.resolve(app_name)
            if match.status == MatchStatus.FOUND:
                return self._launch(
                    app_name, match.entry.target,
                    resolved_via="index",
                    matched_name=match.entry.display_name,
                )
            if match.status == MatchStatus.AMBIGUOUS:
                # 确定性拒绝：命中多个不猜，播报候选让用户说具体点
                names = "、".join(match.candidates)
                return ExecutionResult(
                    success=False,
                    action_type="open_app",
                    error=f"找到多个匹配 {app_name!r} 的应用：{names}，请说得更具体一些",
                    payload={"app_name": app_name, "candidates": list(match.candidates)},
                )
            # NOT_FOUND 落到统一的"没找到"
            return ExecutionResult(
                success=False,
                action_type="open_app",
                error=f"没有找到应用 {app_name!r}",
                payload={"app_name": app_name},
            )

        # 索引未启用：保持旧行为，白名单外拒绝
        return ExecutionResult(
            success=False,
            action_type="open_app",
            error=f"应用 {app_name!r} 不在允许范围内",
            payload={"app_name": app_name},
        )

    # ------------------------------------------------------------------
    def _launch(
        self,
        app_name: str,
        target: Path,
        resolved_via: str,
        matched_name: Optional[str] = None,
    ) -> ExecutionResult:
        """启动 target（exe 或 .lnk），payload 记录解析来源（§6 审计要求）。"""
        if not target.exists():
            return ExecutionResult(
                success=False,
                action_type="open_app",
                error=f"应用 {app_name!r} 的路径不存在: {target}",
                payload={"app_name": app_name, "app_path": str(target),
                         "resolved_via": resolved_via},
            )

        try:
            # os.startfile 是 Windows 上启动应用的最简方式（走关联打开，.lnk 可直接启动）
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            _LOG.warning("os.startfile(%s) failed: %s", target, exc)
            return ExecutionResult(
                success=False,
                action_type="open_app",
                error=f"启动应用 {app_name!r} 失败: {exc}",
                payload={"app_name": app_name, "app_path": str(target),
                         "resolved_via": resolved_via},
            )

        payload = {
            "app_name": matched_name or app_name,
            "app_path": str(target),
            "resolved_via": resolved_via,
        }
        return ExecutionResult(success=True, action_type="open_app", payload=payload)

    # ------------------------------------------------------------------
    def close_app(self, intent: CloseAppIntent, confirmed: bool) -> ExecutionResult:
        app_name = intent.app_name
        app_path = self._whitelist.get_app_path(app_name)
        if app_path is None:
            # 白名单外的应用不关——既不能关，也不该关（不知道会关掉什么）
            return ExecutionResult(
                success=False,
                action_type="close_app",
                error=f"应用 {app_name!r} 不在允许范围内",
                payload={"app_name": app_name},
            )

        # CLAUDE.md §6：关闭可能有未保存内容的应用需二次确认
        if not confirmed:
            return ExecutionResult(
                success=False,
                action_type="close_app",
                needs_confirmation=True,
                payload={"app_name": app_name, "app_path": str(app_path)},
                error=None,  # needs_confirmation 不是错误，是待确认状态
            )

        # confirmed=True，实际执行关闭
        exe_filename = app_path.name  # e.g. "chrome.exe"
        terminated_pids: list[int] = []
        errors: list[str] = []

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc_name = proc.info.get("name") or ""
                if proc_name.lower() == exe_filename.lower():
                    proc.terminate()  # 优雅关闭，发 SIGTERM
                    terminated_pids.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                # 进程已退出或权限不足，记录但继续
                _LOG.debug("terminate pid=%s failed: %s", proc.info.get("pid"), exc)
                errors.append(str(exc))

        if not terminated_pids:
            return ExecutionResult(
                success=True,  # 没找到进程也算"成功"——应用本来就没开
                action_type="close_app",
                payload={
                    "app_name": app_name,
                    "app_path": str(app_path),
                    "terminated_pids": [],
                    "message": f"应用 {app_name!r} 当前未在运行",
                },
            )

        return ExecutionResult(
            success=True,
            action_type="close_app",
            payload={
                "app_name": app_name,
                "app_path": str(app_path),
                "terminated_pids": terminated_pids,
                "terminated_count": len(terminated_pids),
                "errors": errors or None,
            },
        )
