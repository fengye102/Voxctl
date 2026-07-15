"""系统状态查询模块（CLAUDE.md §5 Step 4：query_system 意图）。

用 psutil 查询内存/磁盘/CPU 三类资源。纯查询操作，无副作用，不需要二次确认。

payload 结构（供 Step 5 TTS 模板拼接）：
    memory: {"total_gb": 16.0, "available_gb": 8.5, "used_gb": 7.5, "percent_used": 46.9}
    disk:   {"drives": [{"drive": "C:\\", "total_gb": 500.0, "free_gb": 200.0, "percent_used": 60.0}, ...]}
    cpu:    {"percent_used": 23.5}
"""

from __future__ import annotations

import logging
import string
from typing import Optional

import psutil

from nlu.schema import QuerySystemIntent
from executor import ExecutionResult

_LOG = logging.getLogger(__name__)


class SystemQueryError(Exception):
    """系统查询模块错误基类。"""


class SystemQuery:
    """查询系统资源状态。无状态，可直接复用。"""

    def query(self, intent: QuerySystemIntent) -> ExecutionResult:
        """按 intent.resource 分发查询。"""
        resource = intent.resource
        if resource == "memory":
            return self._query_memory(intent)
        if resource == "disk":
            return self._query_disk(intent)
        if resource == "cpu":
            return self._query_cpu(intent)
        # schema 已用 Literal 约束，这里防御性兜底
        return ExecutionResult(
            success=False,
            action_type="query_system",
            error=f"不支持的资源类型: {resource}",
            payload={"resource": resource},
        )

    # ------------------------------------------------------------------
    def _query_memory(self, intent: QuerySystemIntent) -> ExecutionResult:
        try:
            mem = psutil.virtual_memory()
        except Exception as exc:
            _LOG.warning("psutil.virtual_memory failed: %s", exc)
            return ExecutionResult(
                success=False,
                action_type="query_system",
                error=f"查询内存失败: {exc}",
                payload={"resource": "memory"},
            )
        total_gb = mem.total / (1024 ** 3)
        available_gb = mem.available / (1024 ** 3)
        used_gb = (mem.total - mem.available) / (1024 ** 3)
        return ExecutionResult(
            success=True,
            action_type="query_system",
            payload={
                "resource": "memory",
                "total_gb": round(total_gb, 2),
                "available_gb": round(available_gb, 2),
                "used_gb": round(used_gb, 2),
                "percent_used": round(mem.percent, 1),
            },
        )

    # ------------------------------------------------------------------
    def _query_disk(self, intent: QuerySystemIntent) -> ExecutionResult:
        # disk_drive 是盘符字母如 "C" / "D"，None 表示查所有盘
        drive_letter = intent.disk_drive
        drives_to_query: list[str] = []

        if drive_letter:
            # 规范化：取首字母，大写，拼成 "C:\\"
            letter = drive_letter.strip()[:1].upper()
            if letter not in string.ascii_uppercase:
                return ExecutionResult(
                    success=False,
                    action_type="query_system",
                    error=f"无效的盘符: {drive_letter!r}",
                    payload={"resource": "disk", "requested_drive": drive_letter},
                )
            drives_to_query = [f"{letter}:\\"]
        else:
            # 查所有物理盘（只取固定/可移动，排除光驱等无法访问的）
            try:
                partitions = psutil.disk_partitions(all=False)
            except Exception as exc:
                _LOG.warning("psutil.disk_partitions failed: %s", exc)
                return ExecutionResult(
                    success=False,
                    action_type="query_system",
                    error=f"枚举磁盘分区失败: {exc}",
                    payload={"resource": "disk"},
                )
            for part in partitions:
                # 只查能访问的盘（光驱/空槽会抛 OSError）
                if self._is_partition_readable(part.mountpoint):
                    drives_to_query.append(part.mountpoint)

        if not drives_to_query:
            return ExecutionResult(
                success=False,
                action_type="query_system",
                error="没有可查询的磁盘分区",
                payload={"resource": "disk"},
            )

        drives_info: list[dict] = []
        errors: list[str] = []
        for mountpoint in drives_to_query:
            try:
                usage = psutil.disk_usage(mountpoint)
            except OSError as exc:
                _LOG.debug("disk_usage(%s) failed: %s", mountpoint, exc)
                errors.append(f"{mountpoint}: {exc}")
                continue
            drives_info.append({
                "drive": mountpoint,
                "total_gb": round(usage.total / (1024 ** 3), 2),
                "free_gb": round(usage.free / (1024 ** 3), 2),
                "used_gb": round(usage.used / (1024 ** 3), 2),
                "percent_used": round(usage.percent, 1),
            })

        if not drives_info:
            return ExecutionResult(
                success=False,
                action_type="query_system",
                error=f"所有磁盘分区都无法查询: {'; '.join(errors)}",
                payload={"resource": "disk"},
            )

        return ExecutionResult(
            success=True,
            action_type="query_system",
            payload={
                "resource": "disk",
                "drives": drives_info,
            },
        )

    # ------------------------------------------------------------------
    def _query_cpu(self, intent: QuerySystemIntent) -> ExecutionResult:
        try:
            # interval=1 阻塞 1 秒采样，首次调用返回 0.0 才有意义
            percent = psutil.cpu_percent(interval=1)
        except Exception as exc:
            _LOG.warning("psutil.cpu_percent failed: %s", exc)
            return ExecutionResult(
                success=False,
                action_type="query_system",
                error=f"查询 CPU 失败: {exc}",
                payload={"resource": "cpu"},
            )
        return ExecutionResult(
            success=True,
            action_type="query_system",
            payload={
                "resource": "cpu",
                "percent_used": round(percent, 1),
            },
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _is_partition_readable(mountpoint: str) -> bool:
        """检查磁盘分区是否可读（光驱/空槽会抛 OSError，静默跳过）。"""
        try:
            psutil.disk_usage(mountpoint)
            return True
        except OSError:
            return False
