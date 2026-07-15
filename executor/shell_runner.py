"""Shell 命令执行模块（CLAUDE.md §5 Step 4：run_command 意图）。

安全规则（CLAUDE.md §6 强制）：
    1. 白名单外命令 100% 拒绝，不做模糊匹配
    2. 命令用列表形式传 subprocess，shell=False，禁止 shell=True 任意拼接
    3. dangerous: true 的命令必须二次确认（confirmed=True 才执行）
    4. 超时保护：单条命令最长 30 秒，防止挂死主循环

args 安全说明：
    RunCommandIntent.args 会追加到白名单 command 列表末尾传给 subprocess。
    因为 shell=False，args 不会被 shell 解释（&、|、; 等是字面参数）。
    但对于 powershell -Command "xxx" 类白名单条目，args 追加后会被
    powershell 自身解释——这是白名单配置者的责任，配置 dangerous 命令
    时需评估 args 注入风险。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

from executor import ExecutionResult
from executor.whitelist import CommandEntry, Whitelist
from nlu.schema import RunCommandIntent

_LOG = logging.getLogger(__name__)

# 单条命令最长执行时间（秒），防止挂死主循环
_COMMAND_TIMEOUT = 30

# stdout/stderr 截断长度，避免日志/播报被超长输出撑爆
_OUTPUT_LIMIT = 500


class ShellRunnerError(Exception):
    """Shell 命令执行模块错误基类。"""


class ShellRunner:
    """执行白名单内的自定义命令。共享 Whitelist 实例（只读）。"""

    def __init__(self, whitelist: Whitelist) -> None:
        self._whitelist = whitelist

    # ------------------------------------------------------------------
    def run(self, intent: RunCommandIntent, confirmed: bool) -> ExecutionResult:
        command_name = intent.command_name
        entry = self._whitelist.get_command(command_name)

        if entry is None:
            # CLAUDE.md §6：白名单外操作 100% 拒绝
            return ExecutionResult(
                success=False,
                action_type="run_command",
                error=f"命令 {command_name!r} 不在允许范围内",
                payload={"command_name": command_name},
            )

        # dangerous 命令需要二次确认（CLAUDE.md §6）
        if entry.dangerous and not confirmed:
            return ExecutionResult(
                success=False,
                action_type="run_command",
                needs_confirmation=True,
                payload={
                    "command_name": command_name,
                    "dangerous": True,
                    "command": entry.command,
                },
                error=None,  # needs_confirmation 不是错误，是待确认状态
            )

        # 构造最终命令：白名单 command + 用户 args
        full_command = list(entry.command) + list(intent.args)

        try:
            # shell=False：full_command 是列表，元素作为独立参数传给程序，
            # 不经过 shell 解释，杜绝命令注入（CLAUDE.md §6 强制）
            proc = subprocess.run(
                full_command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            _LOG.warning("command %r timed out after %ds", full_command, _COMMAND_TIMEOUT)
            return ExecutionResult(
                success=False,
                action_type="run_command",
                error=f"命令执行超时（{_COMMAND_TIMEOUT}秒）: {exc}",
                payload={
                    "command_name": command_name,
                    "command": full_command,
                    "timeout": _COMMAND_TIMEOUT,
                },
            )
        except FileNotFoundError as exc:
            return ExecutionResult(
                success=False,
                action_type="run_command",
                error=f"命令程序不存在: {exc}",
                payload={
                    "command_name": command_name,
                    "command": full_command,
                },
            )
        except OSError as exc:
            _LOG.warning("subprocess.run(%r) failed: %s", full_command, exc)
            return ExecutionResult(
                success=False,
                action_type="run_command",
                error=f"命令执行失败: {exc}",
                payload={
                    "command_name": command_name,
                    "command": full_command,
                },
            )

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        success = proc.returncode == 0

        return ExecutionResult(
            success=success,
            action_type="run_command",
            payload={
                "command_name": command_name,
                "command": full_command,
                "dangerous": entry.dangerous,
                "returncode": proc.returncode,
                "stdout": _truncate(stdout),
                "stderr": _truncate(stderr) if stderr else None,
            },
            error=None if success else f"命令退出码 {proc.returncode}: {stderr or '无错误输出'}",
        )


def _truncate(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    """截断超长输出，保留前 limit 字符 + 截断标记。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(截断，共 {len(text)} 字符)"
