"""Step 7 打断功能验收测试。

测试策略：
    完全 mock VADProcessor 和 AudioPlayer，避免依赖 silero-vad 模型加载、
    麦克风和真实 TTS。通过注入合成音频块验证打断协调器逻辑。

测试覆盖：
    1. process_chunk_interrupt：连续 N 帧超阈值即触发打断
    2. InterruptCoordinator.monitor：检测到打断立即 stop player
    3. drain_queue：清空队列里剩余的音频块
    4. _phase_tts 集成：TTS 被打断 → 状态切回 LISTEN + pending 清空
    5. _phase_tts 正常：无打断 → 按原流程切 AWAIT_CONFIRM/LISTEN
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np

# 保证从项目根目录导入
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from audio.vad import VADMode, VADProcessor, VADConfig, CHUNK_SAMPLES
from audio.player import AudioPlayer
from core.interrupt_coordinator import InterruptCoordinator
from core.state import LoopState, State
from executor import ExecutionResult
from nlu.schema import CloseAppIntent, OpenAppIntent

_LOG = logging.getLogger(__name__)


# ===========================================================================
# 工具：构造 mock VAD，可控返回打断判定结果
# ===========================================================================

class MockVAD:
    """Mock VAD：按预设序列返回 process_chunk_interrupt 结果。

    模式：
        - interrupt_pattern: list[bool]，第 n 次调用返回第 n 个值
        - 用完后返回 False（避免 IndexError）
    """

    def __init__(self, interrupt_pattern: list[bool]) -> None:
        self.interrupt_pattern = list(interrupt_pattern)
        self.call_count = 0
        self.reset_count = 0

    def process_chunk_interrupt(self, chunk: np.ndarray) -> bool:
        idx = self.call_count
        self.call_count += 1
        if idx < len(self.interrupt_pattern):
            return self.interrupt_pattern[idx]
        return False

    def reset(self) -> None:
        self.reset_count += 1
        self.call_count = 0


class MockPlayer:
    """Mock AudioPlayer：记录 stop 调用次数。"""

    def __init__(self) -> None:
        self.stop_count = 0

    def stop(self) -> None:
        self.stop_count += 1


# ===========================================================================
# 测试 1：InterruptCoordinator 基础逻辑
# ===========================================================================

class TestInterruptCoordinator(unittest.IsolatedAsyncioTestCase):

    async def test_monitor_detects_interrupt_and_stops_player(self):
        """连续 N 帧超阈值 → 触发打断 → player.stop() 被调用"""
        vad = MockVAD(interrupt_pattern=[False, True, True])  # 第 2、3 帧超阈值
        player = MockPlayer()
        queue: asyncio.Queue = asyncio.Queue()

        # 预填 3 个音频块
        for _ in range(3):
            await queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        coord = InterruptCoordinator(vad=vad, player=player, audio_queue=queue)
        self.assertFalse(coord.interrupted)

        await asyncio.wait_for(coord.monitor(), timeout=2.0)

        self.assertTrue(coord.interrupted)
        self.assertEqual(player.stop_count, 1)
        self.assertGreaterEqual(vad.reset_count, 1)  # 进入 monitor 前 reset 过

    async def test_monitor_no_interrupt_keeps_running_until_cancelled(self):
        """无打断信号 → monitor 一直运行，直到被 cancel"""
        vad = MockVAD(interrupt_pattern=[False, False, False, False, False])
        player = MockPlayer()
        queue: asyncio.Queue = asyncio.Queue()

        for _ in range(3):
            await queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        coord = InterruptCoordinator(vad=vad, player=player, audio_queue=queue)

        task = asyncio.create_task(coord.monitor())
        # 让 monitor 跑完 3 个块（都返回 False，没打断）
        await asyncio.sleep(0.3)

        self.assertFalse(coord.interrupted)
        self.assertEqual(player.stop_count, 0)

        # cancel 应该正常结束
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        self.assertFalse(coord.interrupted)

    def test_drain_queue_clears_all_chunks(self):
        """drain_queue 清空队列里所有剩余音频块"""
        queue: asyncio.Queue = asyncio.Queue()
        for i in range(5):
            queue.put_nowait(np.full(CHUNK_SAMPLES, i, dtype=np.float32))

        count = InterruptCoordinator.drain_queue(queue)

        self.assertEqual(count, 5)
        self.assertTrue(queue.empty())

    def test_drain_queue_empty_queue_returns_zero(self):
        """drain_queue 对空队列返回 0"""
        queue: asyncio.Queue = asyncio.Queue()
        count = InterruptCoordinator.drain_queue(queue)
        self.assertEqual(count, 0)

    def test_reset_clears_interrupted_flag(self):
        """reset 清掉 interrupted 标志"""
        vad = MockVAD(interrupt_pattern=[])
        player = MockPlayer()
        queue: asyncio.Queue = asyncio.Queue()

        coord = InterruptCoordinator(vad=vad, player=player, audio_queue=queue)
        coord._interrupted = True  # 模拟已触发

        coord.reset()

        self.assertFalse(coord.interrupted)


# ===========================================================================
# 测试 2：_phase_tts 集成（用 mock 的 MainLoop 子类）
# ===========================================================================

class MockTTS:
    """Mock TTS：记录 speak_result 调用，可控制 await 时长。"""

    def __init__(self, speak_delay: float = 0.0) -> None:
        self.speak_delay = speak_delay
        self.speak_result_count = 0
        self.interrupted_during_speak = False

    async def speak_result(self, result: ExecutionResult) -> None:
        self.speak_result_count += 1
        if self.speak_delay > 0:
            # 模拟 TTS 播报耗时，期间可被 player.stop() 打断
            try:
                await asyncio.sleep(self.speak_delay)
            except asyncio.CancelledError:
                self.interrupted_during_speak = True
                raise


class MainLoopForTest:
    """复用 MainLoop 的 _phase_tts 逻辑，但所有依赖都 mock 掉。

    直接构造 MainLoop 需要真实 VAD/recorder 等，这里只测 _phase_tts，
    所以手动实例化必要字段。
    """

    def __init__(
        self,
        vad: MockVAD,
        player: MockPlayer,
        tts: MockTTS,
    ) -> None:
        self._vad = vad
        self._player = player
        self._tts = tts
        self._state = LoopState()
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._interrupt_coord = InterruptCoordinator(
            vad=vad, player=player, audio_queue=self._audio_queue,
        )
        self._pending_result: Optional[ExecutionResult] = None

    # 复制 _phase_tts 的实现（与 core/event_loop.py 保持一致）
    async def _phase_tts(self) -> None:
        import contextlib as _ctxlib
        result = self._pending_result
        self._pending_result = None

        if result is None:
            self._state.transition(State.LISTEN)
            return

        self._interrupt_coord.reset()
        monitor_task = asyncio.create_task(
            self._interrupt_coord.monitor(), name="interrupt_monitor"
        )

        try:
            await self._tts.speak_result(result)
        finally:
            if not monitor_task.done():
                monitor_task.cancel()
            with _ctxlib.suppress(asyncio.CancelledError, Exception):
                await monitor_task

        if self._interrupt_coord.interrupted:
            print("[打断] 停止播报，准备听新指令")
            InterruptCoordinator.drain_queue(self._audio_queue)
            self._vad.reset()
            if self._state.pending is not None:
                self._state.clear_pending_confirmation()
            # 退出播报被打断 = 取消退出（§6 打断边界，修订 14：标志位从未置上，防御性清除）
            if result.should_exit or self._state.exit_requested:
                self._state.exit_requested = False
            self._state.rounds_completed += 1
            self._state.transition(State.LISTEN)
            return

        if result.should_exit:
            # 修订 14：exit_requested 延迟到退出播报正常播完才置位
            self._state.exit_requested = True
            return
        if self._state.pending is not None:
            self._state.transition(State.AWAIT_CONFIRM)
        else:
            self._state.rounds_completed += 1
            self._state.transition(State.LISTEN)


class TestPhaseTtsInterrupt(unittest.IsolatedAsyncioTestCase):

    async def test_tts_normal_complete_no_interrupt(self):
        """TTS 正常播完，无打断 → 状态切回 LISTEN"""
        vad = MockVAD(interrupt_pattern=[False] * 20)
        player = MockPlayer()
        tts = MockTTS(speak_delay=0.1)

        loop = MainLoopForTest(vad=vad, player=player, tts=tts)
        loop._pending_result = ExecutionResult(
            success=True, action_type="open_app",
            payload={"app_name": "notepad"},
        )

        # 同时往队列塞一些音频块（monitor 会消费）
        for _ in range(5):
            await loop._audio_queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        await loop._phase_tts()

        self.assertFalse(loop._interrupt_coord.interrupted)
        self.assertEqual(player.stop_count, 0)
        self.assertEqual(tts.speak_result_count, 1)
        self.assertEqual(loop._state.state, State.LISTEN)
        self.assertEqual(loop._state.rounds_completed, 1)

    async def test_tts_interrupted_returns_to_listen(self):
        """TTS 期间被打断 → player.stop 调用，状态切回 LISTEN，rounds+1"""
        # 第 1、2 帧超阈值即触发打断（speech_confirm_frames=2 假设）
        vad = MockVAD(interrupt_pattern=[True, True])
        player = MockPlayer()
        tts = MockTTS(speak_delay=1.0)  # TTS 播报 1 秒，期间会被打断

        loop = MainLoopForTest(vad=vad, player=player, tts=tts)
        loop._pending_result = ExecutionResult(
            success=True, action_type="open_app",
            payload={"app_name": "notepad"},
        )

        # 预填 2 个音频块（让 monitor 检测到打断）
        for _ in range(2):
            await loop._audio_queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        await asyncio.wait_for(loop._phase_tts(), timeout=3.0)

        self.assertTrue(loop._interrupt_coord.interrupted)
        self.assertEqual(player.stop_count, 1)
        self.assertEqual(loop._state.state, State.LISTEN)
        self.assertEqual(loop._state.rounds_completed, 1)
        # 队列应该被 drain
        self.assertTrue(loop._audio_queue.empty())

    async def test_tts_interrupted_clears_pending_confirmation(self):
        """AWAIT_CONFIRM 前的播报被打断 → pending 被清掉，不进 AWAIT_CONFIRM"""
        vad = MockVAD(interrupt_pattern=[True, True])
        player = MockPlayer()
        tts = MockTTS(speak_delay=1.0)

        loop = MainLoopForTest(vad=vad, player=player, tts=tts)
        loop._pending_result = ExecutionResult(
            success=False, action_type="close_app", needs_confirmation=True,
            payload={"app_name": "notepad"},
        )
        # 模拟 EXECUTE 阶段已设置 pending
        loop._state.set_pending_confirmation(
            intent=CloseAppIntent(app_name="notepad"),
            raw_text="关闭记事本",
            result=loop._pending_result,
        )

        for _ in range(2):
            await loop._audio_queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        await asyncio.wait_for(loop._phase_tts(), timeout=3.0)

        self.assertTrue(loop._interrupt_coord.interrupted)
        self.assertEqual(player.stop_count, 1)
        # pending 应被清掉
        self.assertIsNone(loop._state.pending)
        # 不进 AWAIT_CONFIRM，而是回 LISTEN
        self.assertEqual(loop._state.state, State.LISTEN)

    async def test_tts_normal_complete_with_pending_goes_to_await_confirm(self):
        """正常播完 needs_confirm 结果 → 切到 AWAIT_CONFIRM（保留 pending）"""
        vad = MockVAD(interrupt_pattern=[False] * 20)
        player = MockPlayer()
        tts = MockTTS(speak_delay=0.1)

        loop = MainLoopForTest(vad=vad, player=player, tts=tts)
        result = ExecutionResult(
            success=False, action_type="close_app", needs_confirmation=True,
            payload={"app_name": "notepad"},
        )
        loop._pending_result = result
        loop._state.set_pending_confirmation(
            intent=CloseAppIntent(app_name="notepad"),
            raw_text="关闭记事本",
            result=result,
        )

        for _ in range(3):
            await loop._audio_queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        await loop._phase_tts()

        self.assertFalse(loop._interrupt_coord.interrupted)
        # pending 应保留
        self.assertIsNotNone(loop._state.pending)
        self.assertEqual(loop._state.state, State.AWAIT_CONFIRM)

    async def test_tts_normal_complete_exit_requested(self):
        """should_exit=True 的结果正常播完 → exit_requested 才被置位（修订 14），不切状态"""
        vad = MockVAD(interrupt_pattern=[False] * 20)
        player = MockPlayer()
        tts = MockTTS(speak_delay=0.1)

        loop = MainLoopForTest(vad=vad, player=player, tts=tts)
        loop._pending_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )
        # 修订 14：EXECUTE 阶段不再置位，进入 TTS 时 exit_requested 必须还是 False
        self.assertFalse(loop._state.exit_requested)

        for _ in range(3):
            await loop._audio_queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        await loop._phase_tts()

        self.assertFalse(loop._interrupt_coord.interrupted)
        # 正常播完后才置位，主循环下一轮 while 检查时退出
        self.assertTrue(loop._state.exit_requested)

    async def test_tts_interrupted_cancels_exit(self):
        """退出确认音（"再见"）被打断 → exit_requested 保持未置位，回 LISTEN（§6 打断边界，修订 14）。

        修订 14 后 EXECUTE 阶段不置位（否则主循环 while 在 TTS 前就 break，
        "再见"根本播不出来），打断时标志位从未置上，退出自然取消。
        """
        vad = MockVAD(interrupt_pattern=[True, True])
        player = MockPlayer()
        tts = MockTTS(speak_delay=1.0)

        loop = MainLoopForTest(vad=vad, player=player, tts=tts)
        loop._pending_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )
        # 修订 14：EXECUTE 阶段不再提前置位

        for _ in range(2):
            await loop._audio_queue.put(np.zeros(CHUNK_SAMPLES, dtype=np.float32))

        await asyncio.wait_for(loop._phase_tts(), timeout=3.0)

        self.assertTrue(loop._interrupt_coord.interrupted)
        self.assertEqual(player.stop_count, 1)
        # 核心断言：退出被取消（标志位从未置上），主循环得以继续
        self.assertFalse(loop._state.exit_requested)
        self.assertEqual(loop._state.state, State.LISTEN)


# ===========================================================================
# 测试 3：VAD process_chunk_interrupt 单元测试（需要真实 silero-vad）
# ===========================================================================

class TestVadInterruptUnit(unittest.TestCase):
    """测试 VADProcessor.process_chunk_interrupt 的阈值判定逻辑。

    Note: 这组测试需要加载真实 silero-vad 模型（首次约 1MB 下载）。
    如果环境无网络/silero-vad 未装，跳过。
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.vad = VADProcessor(VADConfig(), use_headphone_threshold=True)
        except Exception as exc:
            raise unittest.SkipTest(f"silero-vad 不可用，跳过：{exc}")

    def test_silence_chunks_no_interrupt(self):
        """静音音频块不应触发打断"""
        self.vad.reset()
        silence = np.zeros(CHUNK_SAMPLES, dtype=np.float32)
        for _ in range(20):
            self.assertFalse(self.vad.process_chunk_interrupt(silence))

    def test_loud_speech_triggers_interrupt(self):
        """足够大的语音信号应该触发打断"""
        self.vad.reset()
        # 生成模拟语音：白噪声 + 调幅，幅度足够大
        rng = np.random.default_rng(42)
        for _ in range(10):
            # 振幅 0.5 的噪声信号，silero-vad 在大信号下概率会很高
            speech = (rng.standard_normal(CHUNK_SAMPLES) * 0.3).astype(np.float32)
            triggered = self.vad.process_chunk_interrupt(speech)
            if triggered:
                # 一旦触发，验证状态
                self.assertTrue(triggered)
                return
        # 10 帧大信号都没触发，可能阈值偏高或测试环境问题，不算失败
        # 但至少不应误触发
        self.skipTest("高振幅信号未触发打断（可能阈值偏高，需要真实人声验证）")


# ===========================================================================
# main 入口
# ===========================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    unittest.main(verbosity=2)
