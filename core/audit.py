"""审计日志写入（CLAUDE.md §6 强制）。

所有执行动作和打断事件写入 logs/actions.log，JSON lines 格式，UTF-8 编码。
executor 和 event_loop 共用此模块，避免重复的文件写入逻辑。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from nlu.schema import safe_model_dump

_LOG = logging.getLogger(__name__)


def _write_entry(log_path: Path, entry: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _LOG.warning("failed to write audit log to %s: %s", log_path, exc)


def log_action(
    log_path: Path,
    raw_text: str,
    intent,
    result,
    confirmed: bool,
) -> None:
    """记录一次执行动作（§6 强制）。"""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "raw_text": raw_text,
        "intent": getattr(intent, "intent", None),
        "intent_detail": safe_model_dump(intent),
        "confirmed": confirmed,
        "action_type": result.action_type,
        "success": result.success,
        "needs_confirmation": result.needs_confirmation,
        "should_exit": result.should_exit,
        "error": result.error,
        "payload": result.payload,
        "interrupted": False,
        "interrupt_detail": None,
    }
    _write_entry(log_path, entry)


def log_interrupt_event(
    log_path: Path,
    before_state: str,
    drained_chunks: int,
    cancelled_pending,
    cancelled_exit: bool,
    result_being_played,
) -> None:
    """记录打断事件（§6 强制：审计日志必须记录打断事件本身）。"""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "event": "interrupt",
        "before_state": before_state,
        "drained_chunks": drained_chunks,
        "cancelled_pending": (
            None if cancelled_pending is None
            else {
                "intent": getattr(cancelled_pending.intent, "intent", None),
                "intent_detail": safe_model_dump(cancelled_pending.intent),
                "raw_text": cancelled_pending.raw_text,
            }
        ),
        "cancelled_exit": cancelled_exit,
        "result_being_played": {
            "action_type": result_being_played.action_type,
            "success": result_being_played.success,
            "needs_confirmation": result_being_played.needs_confirmation,
            "should_exit": result_being_played.should_exit,
        },
    }
    _write_entry(log_path, entry)
