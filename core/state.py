"""主循环状态机（CLAUDE.md §5 Step 6 / Step 7）。

状态定义：
    IDLE            启动后的初始状态，进入 LISTEN 前的过渡（实际几乎不驻留）
    LISTEN          正在监听用户说话（VAD 收集语音片段）
    ASR             VAD 已 emit 一段语音，正在做 ASR 转写
    EXECUTE         ASR 出文本，意图解析 + 执行中
    TTS             执行完成，正在 TTS 合成 + 播报
    AWAIT_CONFIRM   执行返回 needs_confirmation=True，等待用户说"是/取消"

Step 6（单向串行）：
    IDLE → LISTEN → ASR → EXECUTE → TTS → LISTEN → ...
    危险操作：EXECUTE(needs_confirm) → AWAIT_CONFIRM → ASR(是/取消) → EXECUTE(confirmed=True) → TTS
    播报期间监听通道照常采集但不处理（Step 6 不做打断），所以实际是
    "TTS 期间音频块直接丢弃，TTS 结束后回到 LISTEN"。

Step 7（全双工打断）：
    TTS 状态下监听通道用 INTERRUPT 阈值判定打断；本文件不实现打断逻辑，
    只暴露状态供 InterruptCoordinator 查询（Step 7 在 event_loop.py 加协调）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from executor import ExecutionResult
from nlu.schema import ParsedIntent


class State(Enum):
    """主循环状态。"""
    IDLE = "idle"
    LISTEN = "listen"
    ASR = "asr"
    EXECUTE = "execute"
    TTS = "tts"
    AWAIT_CONFIRM = "await_confirm"


@dataclass
class PendingConfirmation:
    """等待二次确认时保存的上下文。

    AWAIT_CONFIRM 状态下，用户下一句话应该被解析为 ConfirmIntent / CancelIntent，
    主循环用这个上下文里的 intent + raw_text 以 confirmed=True 重新调 executor.execute。
    """
    intent: ParsedIntent
    raw_text: str
    result: ExecutionResult  # 原始 needs_confirmation=True 的结果，用于播报确认提示


@dataclass
class LoopState:
    """主循环可变状态容器。非线程安全，只在 asyncio 单线程访问。"""
    state: State = State.IDLE
    pending: Optional[PendingConfirmation] = None
    # 累计处理轮次（用于日志/调试）
    rounds_completed: int = 0
    # 是否收到退出信号（ExitProgramIntent 执行成功后置 True，主循环 break）
    exit_requested: bool = False

    # ------------------------------------------------------------------
    # 状态转换助手（带日志，方便调试）
    # ------------------------------------------------------------------
    def transition(self, new_state: State) -> None:
        import logging
        _LOG = logging.getLogger(__name__)
        _LOG.debug("state: %s → %s", self.state.value, new_state.value)
        self.state = new_state

    def set_pending_confirmation(
        self, intent: ParsedIntent, raw_text: str, result: ExecutionResult
    ) -> None:
        self.pending = PendingConfirmation(
            intent=intent, raw_text=raw_text, result=result,
        )

    def clear_pending_confirmation(self) -> Optional[PendingConfirmation]:
        p = self.pending
        self.pending = None
        return p


__all__ = ["State", "LoopState", "PendingConfirmation"]
