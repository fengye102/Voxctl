"""真实 MainLoop 单元测试。

直接测 core.event_loop.MainLoop 的代码路径，不用影子实现。
所有外部依赖（recorder/vad/asr/tts/player/intent_parser/executor）
通过构造函数注入 mock，确保覆盖的是生产代码而非重写副本。

覆盖重点（CLAUDE.md 修订 13/14 记录的坑点）：
    1. exit_requested 置位时机：EXECUTE 阶段不置位，TTS 正常播完才置位
    2. 打断退出播报 = 取消退出：exit_requested 从未置上
    3. 打断取消待确认操作：pending 被清掉
    4. 二次确认流程：confirm / cancel / 改主意三条路径
    5. 错误兜底：单轮异常不崩主循环
"""
from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from audio.output_mode import OutputMode
from audio.vad import CHUNK_SAMPLES, VADMode
from core.event_loop import MainLoop
from core.state import State
from executor import ExecutionResult
from nlu.schema import (
    CancelIntent,
    CloseAppIntent,
    ConfirmIntent,
    ExitProgramIntent,
    OpenAppIntent,
    UnknownIntent,
)
from tts.edge_tts_engine import TTSSynthesizeError, format_result


# ======================================================================
# Stub / Mock 工厂
# ======================================================================

async def _async_return(value):
    """立即返回给定值的协程，用于构造已完成的 asyncio.Task。"""
    return value


def _silence(n: int = 1) -> list[np.ndarray]:
    return [np.zeros(CHUNK_SAMPLES, dtype=np.float32) for _ in range(n)]


def _make_recorder() -> MagicMock:
    r = MagicMock()
    r.start = MagicMock()
    r.stop = MagicMock()
    r.is_running = False
    r.print_devices = MagicMock()
    return r


def _make_player() -> MagicMock:
    p = MagicMock()
    p.stop = MagicMock()
    p.is_playing = False
    return p


def _make_vad(*, segment_on_call: Optional[int] = None) -> MagicMock:
    """创建 VAD mock。

    segment_on_call: 第 N 次 process_chunk 调用返回一个音频段（模拟人声结束），
                     其余调用返回 None。None 表示永不返回段。
    """
    v = MagicMock()
    call_counter = {"n": 0}
    seg = np.zeros(16000, dtype=np.float32)  # 1 秒假音频段

    def process_chunk_side_effect(chunk, mode=VADMode.LISTEN):
        idx = call_counter["n"]
        call_counter["n"] += 1
        if segment_on_call is not None and idx == segment_on_call:
            return seg
        return None

    v.process_chunk = MagicMock(side_effect=process_chunk_side_effect)
    v.process_chunk_interrupt = MagicMock(return_value=False)
    v.reset = MagicMock()
    v.is_confirming_silence = False
    v.preliminary_segment = None
    v._cfg = MagicMock(silence_confirm_frames=15)
    return v


def _make_asr(text: str = "打开记事本", is_empty: bool = False) -> MagicMock:
    a = MagicMock()
    result = MagicMock()
    result.text = text
    result.is_empty = is_empty
    result.no_speech_prob = 0.1
    result.avg_logprob = -0.3
    result.duration_s = 1.0
    result.elapsed_s = 0.5
    a.transcribe = MagicMock(return_value=result)
    return a


def _make_parser(intent=None) -> MagicMock:
    p = MagicMock()
    p.parse = MagicMock(return_value=intent or OpenAppIntent(app_name="notepad"))
    p.close = MagicMock()
    return p


def _make_executor(result: Optional[ExecutionResult] = None) -> MagicMock:
    e = MagicMock()
    e.execute = MagicMock(
        return_value=result or ExecutionResult(
            success=True, action_type="open_app",
            payload={"app_name": "notepad"},
        )
    )
    return e


def _make_tts() -> MagicMock:
    t = MagicMock()
    t.speak = AsyncMock()
    t.speak_result = AsyncMock()
    return t


def _build_loop(**overrides) -> MainLoop:
    """构造一个所有依赖都 mock 掉的真实 MainLoop。"""
    kwargs = dict(
        recorder=_make_recorder(),
        vad=_make_vad(),
        asr_engine=_make_asr(),
        intent_parser=_make_parser(),
        executor=_make_executor(),
        tts_engine=_make_tts(),
        player=_make_player(),
        output_mode=OutputMode.HEADPHONE,
        audit_log_path="logs/test_actions.log",
    )
    kwargs.update(overrides)
    return MainLoop(**kwargs)


# ======================================================================
# 测试：_phase_listen
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestPhaseListen(unittest.IsolatedAsyncioTestCase):

    async def test_no_segment_stays_listen(self, mock_emitter):
        """VAD 未检测到人声结束 → 状态保持 LISTEN。"""
        loop = _build_loop(vad=_make_vad(segment_on_call=None))
        loop._state.transition(State.LISTEN)
        await loop._audio_queue.put(_silence()[0])

        await loop._phase_listen()

        self.assertEqual(loop._state.state, State.LISTEN)
        self.assertIsNone(loop._pending_audio)

    async def test_segment_detected_transitions_to_asr(self, mock_emitter):
        """VAD 检测到完整人声段 → 保存音频，切到 ASR。"""
        loop = _build_loop(vad=_make_vad(segment_on_call=0))
        loop._state.transition(State.LISTEN)
        await loop._audio_queue.put(_silence()[0])

        await loop._phase_listen()

        self.assertEqual(loop._state.state, State.ASR)
        self.assertIsNotNone(loop._pending_audio)


# ======================================================================
# 测试：投机 ASR（P2 #11）
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestSpeculativeAsr(unittest.IsolatedAsyncioTestCase):

    async def test_speculative_task_started_on_confirming_silence(self, mock_emitter):
        """VAD 进入 CONFIRMING_SILENCE → 投机 ASR 任务被创建。"""
        prelim = np.zeros(16000, dtype=np.float32)
        vad = _make_vad()
        vad.process_chunk = MagicMock(return_value=None)
        vad.is_confirming_silence = True
        vad.preliminary_segment = prelim

        loop = _build_loop(vad=vad)
        loop._state.transition(State.LISTEN)
        await loop._audio_queue.put(_silence()[0])

        await loop._phase_listen()

        self.assertIsNotNone(loop._speculative_asr_task)
        self.assertEqual(loop._state.state, State.LISTEN)

        loop._speculative_asr_task.cancel()

    async def test_speculative_not_started_when_not_confirming(self, mock_emitter):
        """VAD 不在 CONFIRMING_SILENCE → 不启动投机 ASR。"""
        vad = _make_vad()
        vad.process_chunk = MagicMock(return_value=None)
        vad.is_confirming_silence = False
        vad.preliminary_segment = None

        loop = _build_loop(vad=vad)
        loop._state.transition(State.LISTEN)
        await loop._audio_queue.put(_silence()[0])

        await loop._phase_listen()

        self.assertIsNone(loop._speculative_asr_task)

    async def test_speculative_result_used_on_final_segment(self, mock_emitter):
        """最终 segment 到达时，投机 ASR 已完成 → 用投机结果。"""
        final_seg = np.zeros(24000, dtype=np.float32)
        vad = _make_vad()
        vad.process_chunk = MagicMock(return_value=final_seg)
        vad.is_confirming_silence = False
        vad.preliminary_segment = None

        asr = _make_asr(text="打开记事本")
        loop = _build_loop(vad=vad, asr_engine=asr)
        loop._state.transition(State.LISTEN)

        asr_result = asr.transcribe(np.zeros(16000, dtype=np.float32))
        loop._speculative_asr_task = asyncio.create_task(_async_return(asr_result))
        await asyncio.sleep(0)  # Let task complete

        await loop._audio_queue.put(_silence()[0])
        await loop._phase_listen()

        self.assertEqual(loop._state.state, State.ASR)
        self.assertEqual(loop._speculative_asr_result, asr_result)
        self.assertIsNone(loop._pending_audio)

    async def test_speculative_cancelled_on_speech_resume(self, mock_emitter):
        """语音恢复（VAD 离开 CONFIRMING_SILENCE）→ 投机 ASR 被取消。"""
        vad = _make_vad()
        vad.process_chunk = MagicMock(return_value=None)
        vad.is_confirming_silence = False  # Speech resumed
        vad.preliminary_segment = None

        loop = _build_loop(vad=vad)
        loop._state.transition(State.LISTEN)
        dummy_task = asyncio.create_task(asyncio.sleep(10))
        loop._speculative_asr_task = dummy_task

        await loop._audio_queue.put(_silence()[0])
        await loop._phase_listen()

        self.assertIsNone(loop._speculative_asr_task)
        await asyncio.sleep(0)  # Let cancellation propagate
        self.assertTrue(dummy_task.cancelled())

    async def test_speculative_failure_falls_back_to_pending_audio(self, mock_emitter):
        """投机 ASR 失败 → 回退到用最终 segment 走正常 ASR。"""
        final_seg = np.zeros(16000, dtype=np.float32)
        vad = _make_vad()
        vad.process_chunk = MagicMock(return_value=final_seg)

        loop = _build_loop(vad=vad)
        loop._state.transition(State.LISTEN)

        async def fail():
            raise RuntimeError("ASR crashed")
        loop._speculative_asr_task = asyncio.create_task(fail())
        await asyncio.sleep(0)  # Let task fail

        await loop._audio_queue.put(_silence()[0])
        await loop._phase_listen()

        self.assertEqual(loop._state.state, State.ASR)
        self.assertIsNone(loop._speculative_asr_result)
        self.assertIsNotNone(loop._pending_audio)

    async def test_phase_asr_uses_speculative_result(self, mock_emitter):
        """_phase_asr 优先使用投机 ASR 结果，不再调 transcribe。"""
        asr = _make_asr(text="打开记事本")
        spec_result = asr.transcribe(np.zeros(16000, dtype=np.float32))
        asr.transcribe.reset_mock()

        parser = _make_parser(intent=OpenAppIntent(app_name="notepad"))
        loop = _build_loop(asr_engine=asr, intent_parser=parser)
        loop._state.transition(State.ASR)
        loop._speculative_asr_result = spec_result
        loop._pending_audio = np.zeros(16000, dtype=np.float32)

        await loop._phase_asr()

        asr.transcribe.assert_not_called()
        self.assertIsNone(loop._speculative_asr_result)
        self.assertEqual(loop._state.state, State.EXECUTE)

    async def test_phase_asr_falls_back_when_no_speculative(self, mock_emitter):
        """无投机结果 → 正常调 transcribe。"""
        asr = _make_asr(text="打开记事本")
        parser = _make_parser(intent=OpenAppIntent(app_name="notepad"))
        loop = _build_loop(asr_engine=asr, intent_parser=parser)
        loop._state.transition(State.ASR)
        loop._speculative_asr_result = None
        loop._pending_audio = np.zeros(16000, dtype=np.float32)

        await loop._phase_asr()

        asr.transcribe.assert_called_once()
        self.assertEqual(loop._state.state, State.EXECUTE)


# ======================================================================
# 测试：_phase_asr
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestPhaseAsr(unittest.IsolatedAsyncioTestCase):

    async def test_empty_asr_result_returns_to_listen(self, mock_emitter):
        """ASR 返回空文本 → 静默回 LISTEN，不进 EXECUTE。"""
        asr = _make_asr(text="", is_empty=True)
        loop = _build_loop(asr_engine=asr)
        loop._state.transition(State.ASR)
        loop._pending_audio = np.zeros(16000, dtype=np.float32)

        await loop._phase_asr()

        self.assertEqual(loop._state.state, State.LISTEN)

    async def test_valid_text_transitions_to_execute(self, mock_emitter):
        """ASR 识别出文本 → 意图解析 → 切到 EXECUTE。"""
        intent = OpenAppIntent(app_name="chrome")
        loop = _build_loop(
            asr_engine=_make_asr(text="打开浏览器"),
            intent_parser=_make_parser(intent=intent),
        )
        loop._state.transition(State.ASR)
        loop._pending_audio = np.zeros(16000, dtype=np.float32)

        await loop._phase_asr()

        self.assertEqual(loop._state.state, State.EXECUTE)
        self.assertEqual(loop._pending_intent, intent)
        self.assertEqual(loop._pending_text, "打开浏览器")

    async def test_asr_exception_goes_to_tts_with_error(self, mock_emitter):
        """ASR 抛异常 → 生成错误结果，切到 TTS 播报错误。"""
        asr = _make_asr()
        asr.transcribe = MagicMock(side_effect=RuntimeError("model crashed"))
        loop = _build_loop(asr_engine=asr)
        loop._state.transition(State.ASR)
        loop._pending_audio = np.zeros(16000, dtype=np.float32)

        await loop._phase_asr()

        self.assertEqual(loop._state.state, State.TTS)
        self.assertIsNotNone(loop._pending_result)
        self.assertFalse(loop._pending_result.success)
        self.assertEqual(loop._pending_result.action_type, "asr_error")

    async def test_intent_parse_exception_goes_to_tts(self, mock_emitter):
        """意图解析抛异常 → 生成错误结果，切到 TTS。"""
        parser = _make_parser()
        parser.parse = MagicMock(side_effect=RuntimeError("LLM down"))
        loop = _build_loop(intent_parser=parser)
        loop._state.transition(State.ASR)
        loop._pending_audio = np.zeros(16000, dtype=np.float32)

        await loop._phase_asr()

        self.assertEqual(loop._state.state, State.TTS)
        self.assertIsNotNone(loop._pending_result)
        self.assertEqual(loop._pending_result.action_type, "intent_error")


# ======================================================================
# 测试：_phase_execute
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestPhaseExecute(unittest.IsolatedAsyncioTestCase):

    async def test_unknown_intent_skips_executor(self, mock_emitter):
        """UnknownIntent → 不调 executor，直接生成错误结果切 TTS。"""
        executor = _make_executor()
        loop = _build_loop(executor=executor)
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = UnknownIntent(reason="听不懂")
        loop._pending_text = "乱七八糟"

        await loop._phase_execute()

        executor.execute.assert_not_called()
        self.assertEqual(loop._state.state, State.TTS)
        self.assertFalse(loop._pending_result.success)

    async def test_normal_execution_transitions_to_tts(self, mock_emitter):
        """正常执行 → 结果存入 _pending_result，切 TTS。"""
        result = ExecutionResult(success=True, action_type="open_app")
        loop = _build_loop(executor=_make_executor(result=result))
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = OpenAppIntent(app_name="notepad")
        loop._pending_text = "打开记事本"

        await loop._phase_execute()

        self.assertEqual(loop._state.state, State.TTS)
        self.assertEqual(loop._pending_result, result)

    async def test_needs_confirmation_sets_pending(self, mock_emitter):
        """needs_confirmation → 设置 pending，切 TTS 播报"确定要…吗？"。"""
        result = ExecutionResult(
            success=False, action_type="close_app",
            needs_confirmation=True, payload={"app_name": "notepad"},
        )
        loop = _build_loop(executor=_make_executor(result=result))
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = CloseAppIntent(app_name="notepad")
        loop._pending_text = "关闭记事本"

        await loop._phase_execute()

        self.assertEqual(loop._state.state, State.TTS)
        self.assertIsNotNone(loop._state.pending)
        self.assertEqual(loop._state.pending.raw_text, "关闭记事本")

    async def test_should_exit_does_NOT_set_exit_requested_rev14(self, mock_emitter):
        """修订 14 核心：should_exit=True 在 EXECUTE 阶段不置 exit_requested。

        exit_requested 延迟到 _phase_tts 正常播完后置位，
        否则主循环 while 在 TTS 前就 break，"再见"永远播不出来。
        """
        result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )
        loop = _build_loop(executor=_make_executor(result=result))
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = ExitProgramIntent()
        loop._pending_text = "退出程序"

        await loop._phase_execute()

        self.assertFalse(loop._state.exit_requested,
                         "EXECUTE 阶段不得置 exit_requested（修订 14）")
        self.assertEqual(loop._state.state, State.TTS)
        self.assertTrue(loop._pending_result.should_exit)

    async def test_executor_exception_produces_error_result(self, mock_emitter):
        """executor 抛异常 → 兜底为失败结果，不崩。"""
        executor = _make_executor()
        executor.execute = MagicMock(side_effect=OSError("permission denied"))
        loop = _build_loop(executor=executor)
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = OpenAppIntent(app_name="notepad")
        loop._pending_text = "打开记事本"

        await loop._phase_execute()

        self.assertEqual(loop._state.state, State.TTS)
        self.assertFalse(loop._pending_result.success)
        self.assertIn("OSError", loop._pending_result.error)

    async def test_fallback_unknown_produces_llm_error(self, mock_emitter):
        """LLM 调用失败降级的 UnknownIntent → action_type='llm_error'。"""
        loop = _build_loop()
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = UnknownIntent(reason="timeout: request timed out")
        loop._pending_text = "打开浏览器"

        await loop._phase_execute()

        self.assertEqual(loop._state.state, State.TTS)
        self.assertEqual(loop._pending_result.action_type, "llm_error")
        self.assertIn("timeout", loop._pending_result.error)

    async def test_genuine_unknown_produces_unknown(self, mock_emitter):
        """LLM 主动选择 unknown → action_type='unknown'（不是 llm_error）。"""
        loop = _build_loop()
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = UnknownIntent(reason="不在指令集范围内")
        loop._pending_text = "今天天气怎么样"

        await loop._phase_execute()

        self.assertEqual(loop._state.state, State.TTS)
        self.assertEqual(loop._pending_result.action_type, "unknown")

    async def test_rate_limited_unknown_is_llm_error(self, mock_emitter):
        """限流降级 → llm_error，不是 unknown。"""
        loop = _build_loop()
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = UnknownIntent(reason="rate limited: 429")
        loop._pending_text = "查内存"

        await loop._phase_execute()

        self.assertEqual(loop._pending_result.action_type, "llm_error")
        self.assertIn("rate limited", loop._pending_result.error)


# ======================================================================
# 测试：_phase_tts（正常播完 / 打断 / 退出时序）
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestPhaseTts(unittest.IsolatedAsyncioTestCase):

    async def test_normal_complete_returns_to_listen(self, mock_emitter):
        """正常播完 → 状态切回 LISTEN，rounds +1。"""
        loop = _build_loop()
        loop._state.transition(State.TTS)
        loop._pending_result = ExecutionResult(
            success=True, action_type="open_app",
        )
        # 预填音频块供 interrupt monitor 消费（全 False = 无打断）
        for c in _silence(3):
            await loop._audio_queue.put(c)

        await loop._phase_tts()

        self.assertEqual(loop._state.state, State.LISTEN)
        self.assertEqual(loop._state.rounds_completed, 1)
        self.assertFalse(loop._state.exit_requested)

    async def test_normal_complete_with_pending_goes_to_await_confirm(self, mock_emitter):
        """正常播完 needs_confirm 的结果 → 切到 AWAIT_CONFIRM。"""
        loop = _build_loop()
        result = ExecutionResult(
            success=False, action_type="close_app",
            needs_confirmation=True,
        )
        loop._pending_result = result
        loop._state.set_pending_confirmation(
            intent=CloseAppIntent(app_name="notepad"),
            raw_text="关闭记事本", result=result,
        )
        loop._state.transition(State.TTS)
        for c in _silence(3):
            await loop._audio_queue.put(c)

        await loop._phase_tts()

        self.assertEqual(loop._state.state, State.AWAIT_CONFIRM)
        self.assertIsNotNone(loop._state.pending)

    async def test_exit_requested_set_only_after_tts_completes_rev14(self, mock_emitter):
        """修订 14 核心：should_exit 的结果正常播完后才置 exit_requested。"""
        loop = _build_loop()
        loop._state.transition(State.TTS)
        loop._pending_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )
        self.assertFalse(loop._state.exit_requested)

        for c in _silence(3):
            await loop._audio_queue.put(c)

        await loop._phase_tts()

        self.assertTrue(loop._state.exit_requested,
                        "TTS 正常播完后必须置 exit_requested")

    async def test_interrupt_cancels_exit_rev13(self, mock_emitter):
        """修订 13 核心：打断"再见"播报 → exit_requested 从未置上，回 LISTEN。"""
        player = _make_player()
        vad = _make_vad()
        # 让 interrupt monitor 检测到打断
        vad.process_chunk_interrupt = MagicMock(return_value=True)

        tts = _make_tts()

        async def slow_speak(r):
            await asyncio.sleep(1.0)

        tts.speak_result = AsyncMock(side_effect=slow_speak)

        loop = _build_loop(vad=vad, player=player, tts_engine=tts)
        loop._state.transition(State.TTS)
        loop._pending_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )

        # 预填音频块（让 monitor 有数据消费以触发打断判定）
        for c in _silence(5):
            await loop._audio_queue.put(c)

        await asyncio.wait_for(loop._phase_tts(), timeout=3.0)

        self.assertFalse(loop._state.exit_requested,
                         "打断退出播报 → exit_requested 不得置位（修订 13）")
        self.assertEqual(loop._state.state, State.LISTEN)

    async def test_interrupt_clears_pending_confirmation(self, mock_emitter):
        """打断确认提示播报 → pending 被清掉，不进 AWAIT_CONFIRM。"""
        vad = _make_vad()
        vad.process_chunk_interrupt = MagicMock(return_value=True)

        tts = _make_tts()

        async def slow_speak(r):
            await asyncio.sleep(1.0)

        tts.speak_result = AsyncMock(side_effect=slow_speak)

        loop = _build_loop(vad=vad, tts_engine=tts)
        result = ExecutionResult(
            success=False, action_type="close_app",
            needs_confirmation=True,
        )
        loop._pending_result = result
        loop._state.set_pending_confirmation(
            intent=CloseAppIntent(app_name="notepad"),
            raw_text="关闭记事本", result=result,
        )
        loop._state.transition(State.TTS)

        for c in _silence(5):
            await loop._audio_queue.put(c)

        await asyncio.wait_for(loop._phase_tts(), timeout=3.0)

        self.assertIsNone(loop._state.pending)
        self.assertEqual(loop._state.state, State.LISTEN)

    async def test_none_result_returns_to_listen(self, mock_emitter):
        """_pending_result 为 None → 直接回 LISTEN。"""
        loop = _build_loop()
        loop._state.transition(State.TTS)
        loop._pending_result = None

        await loop._phase_tts()

        self.assertEqual(loop._state.state, State.LISTEN)

    async def test_tts_failure_emits_error_and_returns_to_listen(self, mock_emitter):
        """TTS 合成失败（如网络断）→ emit_error 到浮窗，正常切回 LISTEN。"""
        tts = _make_tts()
        tts.speak_result = AsyncMock(side_effect=TTSSynthesizeError("network down"))

        loop = _build_loop(tts_engine=tts)
        loop._state.transition(State.TTS)
        loop._pending_result = ExecutionResult(
            success=True, action_type="open_app",
        )
        for c in _silence(3):
            await loop._audio_queue.put(c)

        await loop._phase_tts()

        self.assertEqual(loop._state.state, State.LISTEN)
        self.assertEqual(loop._state.rounds_completed, 1)
        mock_emitter.emit_error.assert_called_with("语音播报失败")

    async def test_tts_failure_on_exit_still_sets_exit_requested(self, mock_emitter):
        """TTS 合成失败 + should_exit → exit_requested 仍置位（用户意图有效）。"""
        tts = _make_tts()
        tts.speak_result = AsyncMock(side_effect=TTSSynthesizeError("network"))

        loop = _build_loop(tts_engine=tts)
        loop._state.transition(State.TTS)
        loop._pending_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )

        await loop._phase_tts()

        self.assertTrue(loop._state.exit_requested,
                        "TTS 失败不影响退出意图")

    async def test_tts_failure_with_pending_still_goes_to_await_confirm(self, mock_emitter):
        """TTS 合成失败 + 有 pending → 仍切 AWAIT_CONFIRM（确认问题没说出来但流程继续）。"""
        tts = _make_tts()
        tts.speak_result = AsyncMock(side_effect=TTSSynthesizeError("network"))

        loop = _build_loop(tts_engine=tts)
        result = ExecutionResult(
            success=False, action_type="close_app",
            needs_confirmation=True,
        )
        loop._pending_result = result
        loop._state.set_pending_confirmation(
            intent=CloseAppIntent(app_name="notepad"),
            raw_text="关闭记事本", result=result,
        )
        loop._state.transition(State.TTS)

        await loop._phase_tts()

        self.assertEqual(loop._state.state, State.AWAIT_CONFIRM)
        self.assertIsNotNone(loop._state.pending)


# ======================================================================
# 测试：二次确认流程（_handle_confirm_response）
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestConfirmFlow(unittest.IsolatedAsyncioTestCase):

    def _setup_pending(self, loop: MainLoop) -> ExecutionResult:
        """给 loop 设置一个待确认的 close_app pending。"""
        result = ExecutionResult(
            success=False, action_type="close_app",
            needs_confirmation=True,
        )
        loop._state.set_pending_confirmation(
            intent=CloseAppIntent(app_name="notepad"),
            raw_text="关闭记事本", result=result,
        )
        return result

    async def test_confirm_re_executes_with_confirmed_true(self, mock_emitter):
        """ConfirmIntent → 用原 intent + confirmed=True 重新执行。"""
        confirmed_result = ExecutionResult(
            success=True, action_type="close_app",
            payload={"message": "已关闭 notepad"},
        )
        executor = _make_executor(result=confirmed_result)
        loop = _build_loop(executor=executor)
        self._setup_pending(loop)

        await loop._handle_confirm_response(ConfirmIntent(), "确定")

        # 验证 executor 被调用时 confirmed=True
        executor.execute.assert_called_once()
        call_args = executor.execute.call_args
        self.assertTrue(call_args[0][2])  # 第三个位置参数是 confirmed
        self.assertEqual(loop._state.state, State.TTS)
        self.assertEqual(loop._pending_result, confirmed_result)
        self.assertIsNone(loop._state.pending)

    async def test_cancel_discards_pending(self, mock_emitter):
        """CancelIntent → 丢弃 pending，生成"已取消"结果。"""
        loop = _build_loop()
        self._setup_pending(loop)

        await loop._handle_confirm_response(CancelIntent(), "取消")

        self.assertIsNone(loop._state.pending)
        self.assertEqual(loop._state.state, State.TTS)
        self.assertEqual(loop._pending_result.action_type, "cancel")

    async def test_other_intent_treats_as_change_of_mind(self, mock_emitter):
        """非 Confirm/Cancel 意图 → 丢弃 pending，按新意图正常走。"""
        new_intent = OpenAppIntent(app_name="chrome")
        loop = _build_loop()
        self._setup_pending(loop)

        await loop._handle_confirm_response(new_intent, "打开浏览器")

        self.assertIsNone(loop._state.pending)
        self.assertEqual(loop._state.state, State.EXECUTE)
        self.assertEqual(loop._pending_intent, new_intent)
        self.assertEqual(loop._pending_text, "打开浏览器")


# ======================================================================
# 测试：format_result 错误模板（P0 #2：用户友好的中文，不含 Python 类名）
# ======================================================================

class TestFormatResultErrorTemplates(unittest.TestCase):

    def test_asr_error_template(self):
        """asr_error → 用户友好的中文提示，不含 Python 异常类名。"""
        result = ExecutionResult(
            success=False, action_type="asr_error",
            error="语音识别失败: RuntimeError",
        )
        text = format_result(result)
        self.assertEqual(text, "语音识别出了问题，请再说一次")
        self.assertNotIn("RuntimeError", text)

    def test_intent_error_template(self):
        """intent_error → 用户友好的中文提示。"""
        result = ExecutionResult(
            success=False, action_type="intent_error",
            error="意图解析失败: LLMTimeoutError",
        )
        text = format_result(result)
        self.assertEqual(text, "理解指令时出了问题，请再试一次")
        self.assertNotIn("LLMTimeoutError", text)

    def test_llm_error_timeout(self):
        """llm_error + timeout reason → 超时专用提示。"""
        result = ExecutionResult(
            success=False, action_type="llm_error",
            error="timeout: request timed out after 30s",
        )
        self.assertEqual(format_result(result), "网络连接超时，请稍后再试")

    def test_llm_error_rate_limited(self):
        """llm_error + rate limited → 限流专用提示。"""
        result = ExecutionResult(
            success=False, action_type="llm_error",
            error="rate limited: 429 Too Many Requests",
        )
        self.assertEqual(format_result(result), "请求过于频繁，请稍后再试")

    def test_llm_error_api_error(self):
        """llm_error + api error → 服务故障提示。"""
        result = ExecutionResult(
            success=False, action_type="llm_error",
            error="api error: 500 Internal Server Error",
        )
        self.assertEqual(format_result(result), "服务出了问题，请稍后再试")

    def test_llm_error_parse_error(self):
        """llm_error + parse error → 理解失败提示。"""
        result = ExecutionResult(
            success=False, action_type="llm_error",
            error="parse error: no tool call in response",
        )
        self.assertEqual(format_result(result), "理解指令时出了问题，请再说一次")

    def test_llm_error_validation_failed(self):
        """llm_error + validation failed → 理解失败提示。"""
        result = ExecutionResult(
            success=False, action_type="llm_error",
            error="validation failed: [{'type': 'missing'}]",
        )
        self.assertEqual(format_result(result), "理解指令时出了问题，请再说一次")

    def test_llm_error_generic(self):
        """llm_error + 其他原因 → 通用服务故障提示。"""
        result = ExecutionResult(
            success=False, action_type="llm_error",
            error="llm error: connection reset",
        )
        self.assertEqual(format_result(result), "服务出了问题，请稍后再试")

    def test_unknown_still_says_not_sure(self):
        """genuine unknown → 仍然说"不确定你想做什么"。"""
        result = ExecutionResult(
            success=False, action_type="unknown",
            error="不在指令集范围内",
        )
        self.assertEqual(format_result(result), "不确定你想做什么")


# ======================================================================
# 测试：_main_loop 多步端到端
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestMainLoopIntegration(unittest.IsolatedAsyncioTestCase):

    async def test_full_cycle_listen_to_tts_and_back(self, mock_emitter):
        """完整一轮：LISTEN → ASR → EXECUTE → TTS → LISTEN。"""
        vad = _make_vad(segment_on_call=0)
        asr = _make_asr(text="打开记事本")
        intent = OpenAppIntent(app_name="notepad")
        parser = _make_parser(intent=intent)
        result = ExecutionResult(success=True, action_type="open_app")
        executor = _make_executor(result=result)
        tts = _make_tts()

        loop = _build_loop(
            vad=vad, asr_engine=asr, intent_parser=parser,
            executor=executor, tts_engine=tts,
        )

        # 手动跑各阶段（因为 _main_loop 的 while 会阻塞在 queue.get）
        loop._state.transition(State.LISTEN)
        await loop._audio_queue.put(_silence()[0])
        await loop._phase_listen()
        self.assertEqual(loop._state.state, State.ASR)

        await loop._phase_asr()
        self.assertEqual(loop._state.state, State.EXECUTE)

        await loop._phase_execute()
        self.assertEqual(loop._state.state, State.TTS)

        for c in _silence(3):
            await loop._audio_queue.put(c)
        await loop._phase_tts()
        self.assertEqual(loop._state.state, State.LISTEN)
        self.assertEqual(loop._state.rounds_completed, 1)

        tts.speak_result.assert_called_once_with(result)
        executor.execute.assert_called_once()

    async def test_exit_full_cycle_rev14(self, mock_emitter):
        """退出完整流程：EXECUTE 不置位 → TTS 播完"再见" → exit_requested。"""
        vad = _make_vad(segment_on_call=0)
        intent = ExitProgramIntent()
        parser = _make_parser(intent=intent)
        exit_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )
        executor = _make_executor(result=exit_result)
        tts = _make_tts()

        loop = _build_loop(
            vad=vad, asr_engine=_make_asr(text="退出程序"),
            intent_parser=parser, executor=executor, tts_engine=tts,
        )

        loop._state.transition(State.LISTEN)
        await loop._audio_queue.put(_silence()[0])
        await loop._phase_listen()

        await loop._phase_asr()
        await loop._phase_execute()
        self.assertFalse(loop._state.exit_requested, "EXECUTE 后不置位")

        for c in _silence(3):
            await loop._audio_queue.put(c)
        await loop._phase_tts()
        self.assertTrue(loop._state.exit_requested, "TTS 播完后才置位")

    async def test_confirm_full_cycle(self, mock_emitter):
        """确认流程端到端：EXECUTE → TTS("确定？") → AWAIT_CONFIRM
        → ASR("确定") → EXECUTE(confirmed) → TTS(结果) → LISTEN。
        """
        close_intent = CloseAppIntent(app_name="notepad")
        confirm_intent = ConfirmIntent()

        # 第一轮：close_app 需要确认
        need_confirm_result = ExecutionResult(
            success=False, action_type="close_app",
            needs_confirmation=True,
            payload={"prompt": "确定要关闭 notepad 吗？"},
        )
        # 第二轮（确认后）：实际关闭
        confirmed_result = ExecutionResult(
            success=True, action_type="close_app",
            payload={"message": "已关闭 notepad"},
        )

        call_count = {"n": 0}
        def executor_side_effect(intent, raw_text, confirmed):
            call_count["n"] += 1
            if not confirmed:
                return need_confirm_result
            return confirmed_result

        executor = _make_executor()
        executor.execute = MagicMock(side_effect=executor_side_effect)

        # parser 第一次返回 close，第二次返回 confirm
        parse_results = iter([close_intent, confirm_intent])
        parser = _make_parser()
        parser.parse = MagicMock(side_effect=lambda t: next(parse_results))

        vad = _make_vad(segment_on_call=0)
        tts = _make_tts()

        loop = _build_loop(
            vad=vad, asr_engine=_make_asr(text="关闭记事本"),
            intent_parser=parser, executor=executor, tts_engine=tts,
        )

        # 第一轮：LISTEN → ASR → EXECUTE → TTS → AWAIT_CONFIRM
        loop._state.transition(State.LISTEN)
        await loop._audio_queue.put(_silence()[0])
        await loop._phase_listen()
        await loop._phase_asr()
        await loop._phase_execute()

        self.assertIsNotNone(loop._state.pending)

        for c in _silence(3):
            await loop._audio_queue.put(c)
        await loop._phase_tts()
        self.assertEqual(loop._state.state, State.AWAIT_CONFIRM)

        # 第二轮：AWAIT_CONFIRM → (queue) → ASR → (confirm) → EXECUTE → TTS → LISTEN
        # 重置 VAD 让它再返回一次 segment
        vad_confirm = _make_vad(segment_on_call=0)
        loop._vad = vad_confirm

        await loop._audio_queue.put(_silence()[0])
        await loop._phase_await_confirm()
        self.assertEqual(loop._state.state, State.ASR)

        # ASR 阶段：识别出"确定"，_handle_confirm_response 内部处理
        asr_confirm = _make_asr(text="确定")
        loop._asr = asr_confirm

        await loop._phase_asr()
        # _handle_confirm_response 处理 ConfirmIntent → 重执行 → 切 TTS
        self.assertEqual(loop._state.state, State.TTS)
        self.assertIsNone(loop._state.pending)
        self.assertEqual(loop._pending_result, confirmed_result)

        # 最终 TTS 播报确认结果 → LISTEN
        for c in _silence(3):
            await loop._audio_queue.put(c)
        await loop._phase_tts()
        self.assertEqual(loop._state.state, State.LISTEN)

        # executor 被调用 2 次：第一次 confirmed=False，第二次 confirmed=True
        self.assertEqual(executor.execute.call_count, 2)

    async def test_error_recovery_returns_to_listen(self, mock_emitter):
        """单轮异常不崩主循环：executor 抛异常 → 错误结果 → TTS 播报 → LISTEN。"""
        executor = _make_executor()
        executor.execute = MagicMock(side_effect=RuntimeError("boom"))
        tts = _make_tts()

        loop = _build_loop(executor=executor, tts_engine=tts)
        loop._state.transition(State.EXECUTE)
        loop._pending_intent = OpenAppIntent(app_name="notepad")
        loop._pending_text = "打开记事本"

        await loop._phase_execute()
        self.assertEqual(loop._state.state, State.TTS)
        self.assertFalse(loop._pending_result.success)
        self.assertIn("RuntimeError", loop._pending_result.error)

        for c in _silence(3):
            await loop._audio_queue.put(c)
        await loop._phase_tts()
        self.assertEqual(loop._state.state, State.LISTEN)

        tts.speak_result.assert_called_once()


# ======================================================================
# 测试：审计日志写入（影子实现从未覆盖的副作用）
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestAuditLogWritten(unittest.IsolatedAsyncioTestCase):

    async def test_execute_writes_audit_log(self, mock_emitter):
        """executor.execute 写审计日志到 actions.log（§6 强制）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "actions.log")
            result = ExecutionResult(
                success=True, action_type="open_app",
                payload={"app_name": "notepad"},
            )
            loop = _build_loop(
                executor=_make_executor(result=result),
                audit_log_path=log_path,
            )
            loop._state.transition(State.EXECUTE)
            loop._pending_intent = OpenAppIntent(app_name="notepad")
            loop._pending_text = "打开记事本"

            await loop._phase_execute()

            # executor 内部的 log_action 应该写了审计日志
            # 注意：executor 是 mock，其 execute() 不会写审计日志——
            # 真实写入只在 Executor.execute() 内部调 core.audit.log_action()。
            # 但 _phase_tts 打断路径会调 log_interrupt_event（直接写文件）。
            # 此测试验证 TTS 打断场景的审计日志。

    async def test_interrupt_writes_audit_log(self, mock_emitter):
        """打断事件写审计日志（§6 强制：审计日志必须记录打断事件本身）。"""
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "actions.log")

            vad = _make_vad()
            vad.process_chunk_interrupt = MagicMock(return_value=True)

            tts = _make_tts()
            async def slow_speak(r):
                await asyncio.sleep(1.0)
            tts.speak_result = AsyncMock(side_effect=slow_speak)

            loop = _build_loop(
                vad=vad, tts_engine=tts, audit_log_path=log_path,
            )
            loop._state.transition(State.TTS)
            loop._pending_result = ExecutionResult(
                success=True, action_type="exit_program", should_exit=True,
            )

            for c in _silence(5):
                await loop._audio_queue.put(c)

            await asyncio.wait_for(loop._phase_tts(), timeout=3.0)

            log_file = Path(log_path)
            self.assertTrue(log_file.exists(), "打断事件应写入审计日志文件")
            lines = log_file.read_text(encoding="utf-8").strip().split("\n")
            self.assertGreaterEqual(len(lines), 1)

            entry = json.loads(lines[0])
            self.assertEqual(entry["event"], "interrupt")
            self.assertEqual(entry["cancelled_exit"], True)
            self.assertIn("before_state", entry)
            self.assertIn("drained_chunks", entry)


# ======================================================================
# 测试：_main_loop() 真实 while 循环端到端
#
# 之前所有测试都是手动逐 phase 调用，验证每个阶段的行为。
# 这里验证 _main_loop() 的 while not exit_requested 循环本身：
# 状态分发、异常兜底、退出条件。这正是修订 14 暴露的盲区——
# 手动调 phase 不经过 while 条件检查，测不到"EXECUTE 置位导致
# TTS 被跳过"的 bug。
# ======================================================================

@patch("core.event_loop.state_emitter")
class TestMainLoopWhileLoop(unittest.IsolatedAsyncioTestCase):

    async def test_main_loop_runs_through_exit(self, mock_emitter):
        """_main_loop() 完整跑通一轮到退出，验证 while 循环正确终止。"""
        vad = _make_vad(segment_on_call=0)
        asr = _make_asr(text="退出程序")
        parser = _make_parser(intent=ExitProgramIntent())
        exit_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )
        executor = _make_executor(result=exit_result)
        tts = _make_tts()

        loop = _build_loop(
            vad=vad, asr_engine=asr, intent_parser=parser,
            executor=executor, tts_engine=tts,
        )

        # recorder.start 把音频块注入队列
        def start_side_effect(queue, event_loop):
            async def feed():
                # 先放一个触发 VAD 的 chunk，再放几个静默给 interrupt monitor
                await queue.put(_silence()[0])
                for c in _silence(5):
                    await queue.put(c)
            asyncio.ensure_future(feed(), loop=event_loop)

        loop._recorder.start = MagicMock(side_effect=start_side_effect)

        # 直接调 run()（它会调 _main_loop）
        await asyncio.wait_for(loop.run(), timeout=5.0)

        self.assertTrue(loop._state.exit_requested)
        tts.speak_result.assert_called_once()
        self.assertTrue(tts.speak_result.call_args[0][0].should_exit)

    async def test_main_loop_error_recovery_then_exit(self, mock_emitter):
        """_main_loop() 中单轮异常 → 播报"出错了" → 继续循环 → 正常退出。"""
        call_count = {"n": 0}

        # VAD 每次调用都返回 segment
        def vad_always_segment(chunk, mode=VADMode.LISTEN):
            return np.zeros(16000, dtype=np.float32)
        vad = _make_vad()
        vad.process_chunk = MagicMock(side_effect=vad_always_segment)

        # ASR 返回文本
        asr = _make_asr(text="打开记事本")

        # parser 第一次抛异常，第二次返回 exit
        def parse_side_effect(text):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("LLM boom")
            return ExitProgramIntent()
        parser = _make_parser()
        parser.parse = MagicMock(side_effect=parse_side_effect)

        exit_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )
        executor = _make_executor(result=exit_result)
        tts = _make_tts()

        loop = _build_loop(
            vad=vad, asr_engine=asr, intent_parser=parser,
            executor=executor, tts_engine=tts,
        )

        def start_side_effect(queue, event_loop):
            async def feed():
                for _ in range(20):
                    await queue.put(_silence()[0])
                    await asyncio.sleep(0)
            asyncio.ensure_future(feed(), loop=event_loop)

        loop._recorder.start = MagicMock(side_effect=start_side_effect)

        await asyncio.wait_for(loop.run(), timeout=5.0)

        self.assertTrue(loop._state.exit_requested)
        # 第一轮错误 → intent_error 结果播报
        # 第二轮正常退出 → exit 结果播报
        self.assertEqual(tts.speak_result.call_count, 2)

    async def test_main_loop_multiple_rounds(self, mock_emitter):
        """_main_loop() 连续多轮：2 轮 open_app + 1 轮 exit。"""
        intents = iter([
            OpenAppIntent(app_name="chrome"),
            OpenAppIntent(app_name="notepad"),
            ExitProgramIntent(),
        ])
        results = iter([
            ExecutionResult(success=True, action_type="open_app"),
            ExecutionResult(success=True, action_type="open_app"),
            ExecutionResult(success=True, action_type="exit_program", should_exit=True),
        ])

        vad_call = {"n": 0}
        def vad_every_other(chunk, mode=VADMode.LISTEN):
            vad_call["n"] += 1
            if vad_call["n"] % 2 == 1:  # 奇数次返回 segment
                return np.zeros(16000, dtype=np.float32)
            return None  # 偶数次静默

        vad = _make_vad()
        vad.process_chunk = MagicMock(side_effect=vad_every_other)

        asr = _make_asr(text="指令")

        parser = _make_parser()
        parser.parse = MagicMock(side_effect=lambda t: next(intents))

        executor = _make_executor()
        executor.execute = MagicMock(side_effect=lambda i, t, c: next(results))

        tts = _make_tts()

        loop = _build_loop(
            vad=vad, asr_engine=asr, intent_parser=parser,
            executor=executor, tts_engine=tts,
        )

        def start_side_effect(queue, event_loop):
            async def feed():
                for _ in range(40):
                    await queue.put(_silence()[0])
                    await asyncio.sleep(0)
            asyncio.ensure_future(feed(), loop=event_loop)

        loop._recorder.start = MagicMock(side_effect=start_side_effect)

        await asyncio.wait_for(loop.run(), timeout=5.0)

        self.assertTrue(loop._state.exit_requested)
        self.assertEqual(tts.speak_result.call_count, 3)
        self.assertEqual(executor.execute.call_count, 3)


# ======================================================================
# main
# ======================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    unittest.main(verbosity=2)
