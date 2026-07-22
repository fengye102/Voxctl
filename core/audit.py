"""审计日志写入（CLAUDE.md §6 强制）。

所有执行动作和打断事件写入 logs/actions.log，JSON lines 格式，UTF-8 编码。
executor 和 event_loop 共用此模块，避免重复的文件写入逻辑。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def log_metrics(
    log_path: Path,
    asr_ms: Optional[float],
    intent_ms: Optional[float],
    exec_ms: Optional[float],
    speech_to_tts_ms: Optional[float],
    action_type: Optional[str] = None,
) -> None:
    """记录一轮指令的各阶段延迟（修订 56，度量埋点 §7）。

    毫秒值可为 None（该阶段未发生，如空 ASR 直接兜底的轮次）。写入独立
    event="metrics" 的审计行，便于事后核对 §7 的延迟类验收指标，
    替代此前只能人工感觉的做法。
    """
    def _round(v):
        return None if v is None else round(v, 1)

    entry = {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "event": "metrics",
        "action_type": action_type,
        "asr_ms": _round(asr_ms),
        "intent_ms": _round(intent_ms),
        "exec_ms": _round(exec_ms),
        # "说完话 → 开始播报" 端到端延迟（§7 端到端延迟 < 5s 的量化依据）
        "speech_to_tts_ms": _round(speech_to_tts_ms),
    }
    _write_entry(log_path, entry)


def log_interrupt_event(
    log_path: Path,
    before_state: str,
    drained_chunks: int,
    cancelled_pending,
    cancelled_exit: bool,
    result_being_played,
    interrupt_latency_ms: Optional[float] = None,
) -> None:
    """记录打断事件（§6 强制：审计日志必须记录打断事件本身）。

    interrupt_latency_ms（修订 56，可选）：从播报开始到判定打断的毫秒数，作为
    §7"打断响应延迟"的量化代理（非精确"用户开口→停止"，是"播报开始→判定"）。
    """
    entry = {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "event": "interrupt",
        "before_state": before_state,
        "drained_chunks": drained_chunks,
        "interrupt_latency_ms": (
            None if interrupt_latency_ms is None else round(interrupt_latency_ms, 1)
        ),
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
