"""打断协调器（CLAUDE.md §5 Step 7）。

职责：
    在 TTS 播报期间，并发监听音频队列，用 VAD INTERRUPT 模式快速判定
    "用户是否在打断"。一旦判定为打断：
        1. 立即调 player.stop() 终止播报（sd.stop() 会让 await sd.wait() 返回）
        2. 保存触发前的短预滚，让主循环回到 LISTEN 时继续收集本句话

设计要点：
    - 与 TTS 播报并发跑（asyncio.create_task），TTS 结束后取消监听任务
    - 打断判定用快速路径（process_chunk_interrupt）：连续 N 帧概率超阈值即触发，
      不等完整 segment 结束，响应更快
    - 进入监听前必须 reset VAD，避免把 TTS 之前的尾音/键盘声误判为打断
    - 只保留有界预滚，不回放较早的 TTS 泄漏；耳机模式由 LISTEN 继续处理
      触发后的队列，外放模式仍丢弃（回声误判风险更高）

CLAUDE.md §5 Step 7 打断语义：
    - 打断发生时如果 state.pending is not None（待二次确认）：视为取消该操作，
      清掉 pending，不进入 AWAIT_CONFIRM
    - 如果操作已经开始执行（EXECUTE 阶段已发出系统调用），打断不影响该操作
      完成——EXECUTE 是 await to_thread，主循环不会响应打断，本协调器只在 TTS 期间工作
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import numpy as np

from audio.player import AudioPlayer
from audio.vad import PRE_ROLL_CHUNKS, VADProcessor

_LOG = logging.getLogger(__name__)

# VAD 默认需要两帧确认打断；再留正常 LISTEN 的预滚窗口，避免软字头丢失。
# 有界窗口不把整段播报泄漏移交给 ASR。
_HANDOFF_CHUNKS = PRE_ROLL_CHUNKS + 2


class InterruptCoordinator:
    """TTS 播报期间的打断判定协调器。

    用法：
        coord = InterruptCoordinator(vad, player, audio_queue)
        # TTS 阶段开始时：
        monitor_task = asyncio.create_task(coord.monitor())
        try:
            await tts_engine.speak_result(result)
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
        if coord.interrupted:
            # 处理打断：移交 handoff_chunks、清 pending、回 LISTEN
            ...
    """

    def __init__(
        self,
        vad: VADProcessor,
        player: AudioPlayer,
        audio_queue: "asyncio.Queue[np.ndarray]",
    ) -> None:
        self._vad = vad
        self._player = player
        self._queue = audio_queue
        self._interrupted: bool = False
        self._handoff_chunks: tuple[np.ndarray, ...] = ()

    # ------------------------------------------------------------------
    @property
    def interrupted(self) -> bool:
        """是否检测到打断信号。"""
        return self._interrupted

    @property
    def handoff_chunks(self) -> tuple[np.ndarray, ...]:
        """触发打断时截取的短预滚（按录音时间顺序）。"""
        return self._handoff_chunks

    def reset(self) -> None:
        """开始新一轮监听前重置状态。"""
        self._interrupted = False
        self._handoff_chunks = ()
        self._vad.reset()

    # ------------------------------------------------------------------
    async def monitor(self) -> None:
        """TTS 播报期间并发监听音频队列，检测到打断立即 stop player。

        本方法设计为通过 asyncio.create_task 启动，TTS 结束后由调用方 cancel。
        正常返回（检测到打断）或被 cancel 都安全。
        """
        # 进入监听前清空 VAD 状态，避免 TTS 之前积压的尾音误判
        self._vad.reset()
        _LOG.debug("interrupt monitor started")
        recent_chunks: deque[np.ndarray] = deque(maxlen=_HANDOFF_CHUNKS)

        try:
            while True:
                # 用 wait_for + 小超时，避免队列长时间无数据时 cancel 响应迟
                try:
                    chunk = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                recent_chunks.append(chunk)
                if self._vad.process_chunk_interrupt(chunk):
                    # 检测到打断：立即停止播放
                    self._handoff_chunks = tuple(recent_chunks)
                    self._interrupted = True
                    _LOG.info("interrupt detected, stopping TTS playback")
                    self._player.stop()
                    return
        except asyncio.CancelledError:
            _LOG.debug("interrupt monitor cancelled (TTS finished normally)")
            raise

    # ------------------------------------------------------------------
    @staticmethod
    def drain_queue(audio_queue: "asyncio.Queue[np.ndarray]") -> int:
        """清空音频队列里剩余的音频块。

        仅用于外放模式：关闭打断时清理整段 TTS 泄漏；显式启用打断时，
        为避免误判的回声进入 ASR，仍按旧行为丢弃队列。耳机模式打断
        不可调用，否则会截断新指令。

        Returns:
            被丢弃的音频块数量（用于日志）。
        """
        count = 0
        while True:
            try:
                audio_queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        if count > 0:
            _LOG.info("drained %d stale audio chunks after interrupt", count)
        return count


__all__ = ["InterruptCoordinator"]

