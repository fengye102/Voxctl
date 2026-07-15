"""执行模块：把意图解析结果落到实际系统操作（CLAUDE.md §5 Step 4）。

设计要点：
    1. Executor 是无状态分发器，按意图类型委托给各子模块。二次确认状态由
       主循环（Step 6）管理，executor 只通过 ExecutionResult.needs_confirmation
       告知"这个操作需要先确认"，不自己持有"等待确认"状态。
    2. 审计日志（CLAUDE.md §6 强制）：每次 execute 调用都写 logs/actions.log，
       JSON lines 格式，UTF-8 编码（§8.6）。字段含时间戳、原始文本、意图、
       操作、结果；打断相关字段预留给 Step 7。
    3. 白名单外的操作 100% 拒绝（§6），返回 success=False + error 说明，
       由调用方 TTS 播报"不在允许范围内"。
    4. 危险操作（close_app、dangerous 命令、删除文件、关机重启）需要二次确认：
       confirmed=False → 返回 needs_confirmation=True，不执行
       confirmed=True  → 实际执行
    5. ExitProgramIntent 返回 should_exit=True，主循环负责真正退出（解耦）。

接口约定：
    executor = Executor.from_config(config["whitelist"], log_path="logs/actions.log")
    result = executor.execute(intent, raw_text="打开记事本")
    if result.needs_confirmation:
        # 主循环切到"等待确认"状态，保存 intent + raw_text
        # 收到 ConfirmIntent 后：
        result = executor.execute(saved_intent, saved_raw_text, confirmed=True)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import yaml

# nlu.schema 和 whitelist 不依赖 executor 包，可先导入
from executor.whitelist import Whitelist
from nlu.schema import (
    CancelIntent,
    CloseAppIntent,
    ConfirmIntent,
    ExitProgramIntent,
    OpenAppIntent,
    OpenFileIntent,
    ParsedIntent,
    QuerySystemIntent,
    RunCommandIntent,
    UnknownIntent,
)

_LOG = logging.getLogger(__name__)


class ExecutorError(Exception):
    """执行模块错误基类。"""


# webui 控制台维护的用户别名扩展表（修订 16）。独立于 config.yaml 是因为
# config.yaml 有大量手写注释，机器写入会毁掉注释；此文件是纯机器读写。
USER_APPS_FILE = "user_apps.yaml"


def load_user_apps(path: Union[str, Path, None] = None) -> dict[str, str]:
    """读取 user_apps.yaml 的 apps 块（应用名 → 路径）。文件不存在/损坏返回空 dict。

    path=None 时用模块常量 USER_APPS_FILE（调用时解析，方便测试替换）。
    """
    p = Path(path) if path is not None else Path(USER_APPS_FILE)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("failed to load %s, ignoring user aliases: %s", p, exc)
        return {}
    apps = data.get("apps") or {}
    if not isinstance(apps, dict):
        _LOG.warning("%s apps block is not a mapping, ignoring", p)
        return {}
    return {str(k): str(v) for k, v in apps.items()}


@dataclass
class ExecutionResult:
    """执行结果。字段供主循环决策 + Step 5 TTS 模板化播报使用。

    payload 是结构化数据，按 action_type 不同含不同字段，例如：
        open_app 成功: {"app_name": "chrome", "app_path": "..."}
        query_system memory: {"total_gb": 16.0, "available_gb": 8.5, "percent_used": 46.9}
    Step 5 的 TTS 引擎根据 action_type + success + payload 套模板拼接播报文本。
    """

    success: bool
    action_type: str
    needs_confirmation: bool = False
    should_exit: bool = False
    payload: dict = field(default_factory=dict)
    error: Optional[str] = None


# 子模块从 executor 导入 ExecutionResult，必须在 ExecutionResult 定义之后再导入，
# 否则触发循环导入（app_control/file_ops/shell_runner/system_query 都 import 它）
from executor.app_control import AppController  # noqa: E402
from executor.app_index import AppIndex  # noqa: E402
from executor.file_ops import FileOps  # noqa: E402
from executor.shell_runner import ShellRunner  # noqa: E402
from executor.system_query import SystemQuery  # noqa: E402


class Executor:
    """执行模块统一入口。无状态，线程安全（只读白名单 + append 写日志）。"""

    def __init__(
        self,
        whitelist: Whitelist,
        log_path: Path,
        app_index: Optional[AppIndex] = None,
    ) -> None:
        self._whitelist = whitelist
        self._log_path = log_path
        # 子模块共享同一份白名单实例（只读）；app_index 为 None 时打开只限白名单
        self._app_control = AppController(whitelist, app_index=app_index)
        self._system_query = SystemQuery()
        self._file_ops = FileOps(whitelist)
        self._shell_runner = ShellRunner(whitelist)

    @classmethod
    def from_config(
        cls,
        whitelist_cfg: dict,
        log_path: str = "logs/actions.log",
        user_apps_path: Union[str, Path, None] = None,
    ) -> "Executor":
        """从 config.yaml 的 whitelist 块构造。

        Args:
            whitelist_cfg: config["whitelist"] 字典
            log_path: 审计日志路径，默认 logs/actions.log（CLAUDE.md §3 文件结构）
            user_apps_path: webui 用户别名表路径（修订 16），None = 模块常量
                USER_APPS_FILE；启动时合并进 apps，config.yaml 同名别名优先

        §6 修订 15：whitelist.allow_open_any_app（默认 true）控制是否构建
        应用索引放开"打开"操作；false 恢复"打开也只限白名单"旧行为。
        """
        # 合并 webui 用户别名表（修订 16）
        user_apps = load_user_apps(user_apps_path)
        if user_apps:
            merged = dict(whitelist_cfg.get("apps") or {})
            added = 0
            for name, path in user_apps.items():
                if name not in merged:  # config.yaml 优先，不覆盖
                    merged[name] = path
                    added += 1
            whitelist_cfg = {**whitelist_cfg, "apps": merged}
            _LOG.info("merged %d user aliases from %s",
                      added, user_apps_path or USER_APPS_FILE)

        wl = Whitelist.from_config(whitelist_cfg)
        log_path_obj = Path(log_path)
        # 确保日志目录存在（logs/ 不进 git，但运行时必须存在）
        log_path_obj.parent.mkdir(parents=True, exist_ok=True)

        app_index: Optional[AppIndex] = None
        if bool(whitelist_cfg.get("allow_open_any_app", True)):
            try:
                app_index = AppIndex.build()
            except Exception as exc:  # noqa: BLE001
                # 索引构建失败不阻断启动，退化为仅白名单（保守方向）
                _LOG.warning("AppIndex build failed, open_app falls back to whitelist only: %s", exc)

        return cls(whitelist=wl, log_path=log_path_obj, app_index=app_index)

    # ------------------------------------------------------------------
    def execute(
        self,
        intent: ParsedIntent,
        raw_text: str = "",
        confirmed: bool = False,
    ) -> ExecutionResult:
        """执行意图，返回结果。

        Args:
            intent: nlu.intent_parser 解析出的意图模型
            raw_text: 原始语音转文字内容（审计日志用，§6 强制）
            confirmed: 是否已二次确认。True 时危险操作直接执行，不返回 needs_confirmation。

        Returns:
            ExecutionResult。需确认的操作返回 needs_confirmation=True 且不实际执行。
        """
        try:
            result = self._dispatch(intent, confirmed)
        except Exception as exc:
            # 子模块未捕获的异常兜底成失败结果，不让主循环崩
            _LOG.exception("executor dispatch failed for intent=%r", intent)
            result = ExecutionResult(
                success=False,
                action_type=getattr(intent, "intent", "unknown"),
                error=f"执行异常: {type(exc).__name__}: {exc}",
            )

        # 无论成功失败都写审计日志（CLAUDE.md §6 强制）
        self._log_action(raw_text, intent, result, confirmed)
        return result

    # ------------------------------------------------------------------
    def _dispatch(self, intent: ParsedIntent, confirmed: bool) -> ExecutionResult:
        """按意图类型分发到对应子模块。"""
        if isinstance(intent, OpenAppIntent):
            return self._app_control.open_app(intent)

        if isinstance(intent, CloseAppIntent):
            return self._app_control.close_app(intent, confirmed)

        if isinstance(intent, QuerySystemIntent):
            return self._system_query.query(intent)

        if isinstance(intent, OpenFileIntent):
            return self._file_ops.open_file(intent)

        if isinstance(intent, RunCommandIntent):
            return self._shell_runner.run(intent, confirmed)

        if isinstance(intent, ExitProgramIntent):
            # 退出不是破坏性操作（CLAUDE.md §2 启动与退出方式），不需要二次确认
            return ExecutionResult(
                success=True,
                action_type="exit_program",
                should_exit=True,
                payload={"message": "退出语音助手"},
            )

        if isinstance(intent, ConfirmIntent):
            # ConfirmIntent 不应路由到 executor——它是状态机层面的信号，
            # 主循环应保存原 intent 并用 confirmed=True 重调 execute。
            # 收到这里说明主循环逻辑有 bug，返回错误暴露问题。
            return ExecutionResult(
                success=False,
                action_type="confirm",
                error="confirm intent 被错误地路由到 executor；应由主循环处理",
            )

        if isinstance(intent, CancelIntent):
            # 同上，CancelIntent 是状态机信号，executor 收到等于"无操作"
            return ExecutionResult(
                success=True,
                action_type="cancel",
                payload={"message": "已取消"},
            )

        if isinstance(intent, UnknownIntent):
            return ExecutionResult(
                success=False,
                action_type="unknown",
                error=intent.reason or "无法理解意图",
            )

        # 理论上不会走到（schema 校验保证），防御性兜底
        return ExecutionResult(
            success=False,
            action_type="unknown",
            error=f"未支持的意图类型: {type(intent).__name__}",
        )

    # ------------------------------------------------------------------
    def _log_action(
        self,
        raw_text: str,
        intent: ParsedIntent,
        result: ExecutionResult,
        confirmed: bool,
    ) -> None:
        """写审计日志（CLAUDE.md §6 强制）。JSON lines 格式，UTF-8 编码（§8.6）。

        打断相关字段（interrupted / interrupt_detail）预留给 Step 7，当前留空。
        """
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "raw_text": raw_text,
            "intent": getattr(intent, "intent", None),
            "intent_detail": _safe_model_dump(intent),
            "confirmed": confirmed,
            "action_type": result.action_type,
            "success": result.success,
            "needs_confirmation": result.needs_confirmation,
            "should_exit": result.should_exit,
            "error": result.error,
            "payload": result.payload,
            # Step 7 预留：
            "interrupted": False,
            "interrupt_detail": None,
        }
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            # 日志写入失败不能影响主流程，但必须打 warning 暴露问题
            _LOG.warning("failed to write audit log to %s: %s", self._log_path, exc)


def _safe_model_dump(intent: ParsedIntent) -> dict:
    """安全地把 intent 转 dict（pydantic 模型用 model_dump，其他返回空）。"""
    if hasattr(intent, "model_dump"):
        try:
            return intent.model_dump()
        except Exception:
            return {}
    return {}


__all__ = [
    "ExecutionResult",
    "Executor",
    "ExecutorError",
    "load_user_apps",
    "USER_APPS_FILE",
]
