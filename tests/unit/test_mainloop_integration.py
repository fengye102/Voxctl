"""MainLoop 集成冒烟测试（修订 13/14 技术债补清）。

修订 13 发现 _tts 属性名 typo、修订 14 发现 exit_requested 置位时序 bug，
两者均为"手动逐 _phase_* 调用 / MockMainLoop 影子实现"覆盖不到的盲区——
这些测试都不经过 _main_loop() 的 while not exit_requested 外层循环，
无法暴露"EXECUTE 置位导致 TTS 被跳过"之类的跨阶段时序缺陷。

本文件每个测试都通过 MainLoop.run() 驱动真实的 _main_loop() while 循环，
用 stub/mock 注入所有外部依赖，验证状态机端到端行为。

覆盖场景：
    1. 端到端正常流程（一轮操作 + 退出）
    2. 退出流程时序（修订 14：TTS 播完后才置 exit_requested）
    3. 打断退出取消（修订 13：打断"再见"→ loop 继续 → 再退出）
    4. 二次确认流程（needs_confirmation → AWAIT_CONFIRM → confirm → 重执行）
    5. 连续 10 轮稳定性
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from audio.output_mode import OutputMode
from audio.vad import CHUNK_SAMPLES, VADMode
from core.event_loop import MainLoop
from executor import ExecutionResult
from nlu.schema import (
    CloseAppIntent,
    ConfirmIntent,
    ExitProgramIntent,
    OpenAppIntent,
)


# ======================================================================
# Stub / Mock 工厂
# ======================================================================

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


def _make_vad_always_segment() -> MagicMock:
    """每次 process_chunk 都返回一个 1 秒假音频段（立即切到 ASR）。"""
    v = MagicMock()
    seg = np.zeros(16000, dtype=np.float32)
    v.process_chunk = MagicMock(
        side_effect=lambda chunk, mode=VADMode.LISTEN: seg,
    )
    v.process_chunk_interrupt = MagicMock(return_value=False)
    v.reset = MagicMock()
    v.is_confirming_silence = False
    v.preliminary_segment = None
    v._cfg = MagicMock(silence_confirm_frames=15)
    return v


def _make_asr(text: str = "打开记事本") -> MagicMock:
    a = MagicMock()
    result = MagicMock()
    result.text = text
    result.is_empty = False
    result.no_speech_prob = 0.1
    result.avg_logprob = -0.3
    result.duration_s = 1.0
    result.elapsed_s = 0.5
    a.transcribe = MagicMock(return_value=result)
    a.warmup = MagicMock()
    return a


def _make_tts() -> MagicMock:
    t = MagicMock()
    t.speak = AsyncMock()
    t.speak_result = AsyncMock()
    return t


def _make_parser_from_sequence(intents: list) -> MagicMock:
    """parser.parse 按调用顺序依次返回 intents 列表中的意图。"""
    it = iter(intents)
    p = MagicMock()
    p.parse = MagicMock(side_effect=lambda text, context=None: next(it))
    p.close = MagicMock()
    return p


def _make_executor_from_sequence(results: list) -> MagicMock:
    """executor.execute 按调用顺序依次返回 results 列表中的结果。"""
    it = iter(results)
    e = MagicMock()
    e.execute = MagicMock(side_effect=lambda intent, text, confirmed: next(it))
    return e


def _setup_continuous_feeder(loop_obj: MainLoop, *, count: int = 60, interval: float = 0.02):
    """设置 recorder.start 的 side_effect：持续向队列注入音频块。

    count 个 chunk，每个间隔 interval 秒，总时长 count*interval 秒。
    足够长以覆盖多轮 LISTEN + TTS 的队列需求。
    """

    def start_side_effect(queue, event_loop):
        async def feed():
            for _ in range(count):
                await queue.put(_silence()[0])
                await asyncio.sleep(interval)

        asyncio.ensure_future(feed(), loop=event_loop)

    loop_obj._recorder.start = MagicMock(side_effect=start_side_effect)


def _build_integration_loop(
    *,
    vad=None,
    asr_engine=None,
    intent_parser=None,
    executor=None,
    tts_engine=None,
    player=None,
    audit_dir=None,
) -> MainLoop:
    """构造一个所有依赖 mock 掉的真实 MainLoop，用于集成测试。"""
    audit_path = str(Path(audit_dir) / "actions.log") if audit_dir else "logs/test_actions.log"
    return MainLoop(
        recorder=_make_recorder(),
        vad=vad or _make_vad_always_segment(),
        asr_engine=asr_engine or _make_asr(),
        intent_parser=intent_parser or _make_parser_from_sequence([ExitProgramIntent()]),
        executor=executor or _make_executor_from_sequence([
            ExecutionResult(success=True, action_type="exit_program", should_exit=True),
        ]),
        tts_engine=tts_engine or _make_tts(),
        player=player or _make_player(),
        output_mode=OutputMode.HEADPHONE,
        audit_log_path=audit_path,
    )


# ======================================================================
# 测试 1：端到端正常流程（一轮 open_app + 退出）
# ======================================================================

class TestEndToEndNormalFlow(unittest.IsolatedAsyncioTestCase):
    """通过 run() 驱动真实 _main_loop()，验证一轮正常操作 + 退出全流程。

    状态机预期路径：
        LISTEN → ASR → EXECUTE → TTS → LISTEN（一轮完成）
        → ASR → EXECUTE → TTS（退出）→ loop 结束
    """

    async def test_one_round_then_exit(self):
        tts = _make_tts()
        open_result = ExecutionResult(success=True, action_type="open_app",
                                      payload={"app_name": "notepad"})
        exit_result = ExecutionResult(success=True, action_type="exit_program",
                                      should_exit=True)

        loop = _build_integration_loop(
            intent_parser=_make_parser_from_sequence([
                OpenAppIntent(app_name="notepad"),
                ExitProgramIntent(),
            ]),
            executor=_make_executor_from_sequence([open_result, exit_result]),
            tts_engine=tts,
        )
        _setup_continuous_feeder(loop, count=40, interval=0.02)

        await asyncio.wait_for(loop.run(), timeout=5.0)

        # 状态机正确退出
        self.assertTrue(loop._state.exit_requested)
        # TTS 被调用 2 次（open_app 结果 + 退出确认音）
        self.assertEqual(tts.speak_result.call_count, 2)
        # 第一次播报 open_app 结果
        first_result = tts.speak_result.call_args_list[0][0][0]
        self.assertEqual(first_result.action_type, "open_app")
        self.assertTrue(first_result.success)
        # 第二次播报退出确认音
        second_result = tts.speak_result.call_args_list[1][0][0]
        self.assertTrue(second_result.should_exit)
        # 至少完成了 1 轮
        self.assertGreaterEqual(loop._state.rounds_completed, 1)


# ======================================================================
# 测试 2：退出流程时序（修订 14 核心：TTS 播完后才置 exit_requested）
# ======================================================================

class TestExitTimingRev14(unittest.IsolatedAsyncioTestCase):
    """修订 14 修复的 bug：exit_requested 的置位时机。

    原 bug：_phase_execute 在 TTS 前就置 exit_requested=True，
    主循环 while not exit_requested 在阶段之间检查条件，本轮迭代
    结束即 break，_phase_tts 永远执行不到——"再见"永远播不出来。

    修复后：_phase_execute 不置位，_phase_tts 正常播完后才置位。

    本测试通过 run() 驱动真实 while 循环验证：
        1. speak_result 确实被调用（"再见"播了出来）
        2. 被调用的 result.should_exit == True（确认是退出结果的播报）
        3. 循环正常退出
    """

    async def test_tts_plays_goodbye_before_exit(self):
        tts = _make_tts()
        exit_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )

        loop = _build_integration_loop(
            intent_parser=_make_parser_from_sequence([ExitProgramIntent()]),
            executor=_make_executor_from_sequence([exit_result]),
            tts_engine=tts,
        )
        _setup_continuous_feeder(loop, count=20, interval=0.02)

        await asyncio.wait_for(loop.run(), timeout=5.0)

        # 关键断言：TTS speak_result 被调用了
        tts.speak_result.assert_called_once()
        # 被调用时传入的 result 是退出结果（"再见"确实播了出来）
        played_result = tts.speak_result.call_args[0][0]
        self.assertTrue(played_result.should_exit,
                        "TTS 播报的结果必须是 should_exit=True（修订 14 的核心验证）")
        # 循环正常退出
        self.assertTrue(loop._state.exit_requested)


# ======================================================================
# 测试 3：打断退出取消（修订 13 核心：打断"再见"→ loop 继续）
# ======================================================================

class TestInterruptCancelsExitRev13(unittest.IsolatedAsyncioTestCase):
    """修订 13 修复的 bug：打断"再见"播报后程序照样退出。

    原 bug：打断分支不清除 exit_requested，主循环 while not exit_requested
    在下一轮 break，用户打断"再见"程序照样退出——打断没有真正取消退出。

    修复后：打断退出播报 = 取消退出，exit_requested 不被置位，
    主循环继续运行处理新指令。

    本测试通过 run() 驱动真实 while 循环验证：
        Round 1: ExitProgramIntent → TTS（慢）→ 打断 → exit_requested 不置位 → LISTEN
        Round 2: ExitProgramIntent → TTS（无打断）→ exit_requested=True → 正常退出
    """

    async def test_interrupt_prevents_exit_loop_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # ---- VAD: process_chunk 总是返回 segment；
            #      process_chunk_interrupt 仅第 1 次返回 True（触发打断）
            vad = _make_vad_always_segment()
            interrupt_call_count = {"n": 0}

            def interrupt_side_effect(chunk):
                interrupt_call_count["n"] += 1
                return interrupt_call_count["n"] == 1  # 仅第 1 次 True

            vad.process_chunk_interrupt = MagicMock(side_effect=interrupt_side_effect)

            # ---- TTS: 第 1 次 speak_result 慢（给 monitor 时间检测打断），
            #           第 2 次瞬间完成
            tts = _make_tts()
            speak_count = {"n": 0}

            async def speak_side_effect(result):
                speak_count["n"] += 1
                if speak_count["n"] == 1:
                    await asyncio.sleep(0.5)  # 慢到足以让 monitor 读取 chunk 并触发打断

            tts.speak_result = AsyncMock(side_effect=speak_side_effect)

            # ---- parser / executor: 两次都返回退出
            exit_result = ExecutionResult(
                success=True, action_type="exit_program", should_exit=True,
            )

            loop = _build_integration_loop(
                vad=vad,
                intent_parser=_make_parser_from_sequence([
                    ExitProgramIntent(),
                    ExitProgramIntent(),
                ]),
                executor=_make_executor_from_sequence([exit_result, exit_result]),
                tts_engine=tts,
                audit_dir=tmpdir,
            )
            _setup_continuous_feeder(loop, count=100, interval=0.02)

            await asyncio.wait_for(loop.run(), timeout=5.0)

            # 核心断言：循环跑了 2 轮而非 1 轮退出
            self.assertTrue(loop._state.exit_requested)
            self.assertEqual(tts.speak_result.call_count, 2,
                             "Round 1 被打断 + Round 2 正常播完 = 2 次 speak_result 调用")

            # 验证审计日志记录了打断事件
            import json
            audit_file = Path(tmpdir) / "actions.log"
            self.assertTrue(audit_file.exists(), "审计日志应存在")
            lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
            interrupt_entries = [
                json.loads(line) for line in lines
                if json.loads(line).get("event") == "interrupt"
            ]
            self.assertGreaterEqual(len(interrupt_entries), 1,
                                    "至少有 1 条打断审计记录")
            self.assertTrue(interrupt_entries[0]["cancelled_exit"],
                            "打断审计记录应标记 cancelled_exit=True")


# ======================================================================
# 测试 4：二次确认流程（通过 _main_loop() 端到端）
# ======================================================================

class TestConfirmationFlowE2E(unittest.IsolatedAsyncioTestCase):
    """二次确认全流程通过 run() 驱动真实 _main_loop()。

    预期路径：
        Round 1: "关闭记事本" → CloseAppIntent → executor(confirmed=False)
                 → needs_confirmation=True → TTS "确定要关闭吗？" → AWAIT_CONFIRM
        Round 2: "确定" → ConfirmIntent → _handle_confirm_response
                 → executor(confirmed=True) → 成功 → TTS 播报结果 → LISTEN
        Round 3: "退出程序" → ExitProgramIntent → TTS → 退出
    """

    async def test_confirm_then_exit(self):
        need_confirm_result = ExecutionResult(
            success=False, action_type="close_app",
            needs_confirmation=True,
            payload={"prompt": "确定要关闭 notepad 吗？"},
        )
        confirmed_result = ExecutionResult(
            success=True, action_type="close_app",
            payload={"message": "已关闭 notepad"},
        )
        exit_result = ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
        )

        # executor: 按调用顺序返回不同结果
        # call 1: close_app, confirmed=False → needs_confirmation
        # call 2: close_app, confirmed=True → success
        # call 3: exit_program → should_exit
        exec_results = iter([need_confirm_result, confirmed_result, exit_result])
        executor = MagicMock()
        executor.execute = MagicMock(
            side_effect=lambda intent, text, confirmed: next(exec_results),
        )

        # parser: 按调用顺序返回不同意图
        # call 1: CloseAppIntent（触发确认流程）
        # call 2: ConfirmIntent（确认）
        # call 3: ExitProgramIntent（退出）
        parser = _make_parser_from_sequence([
            CloseAppIntent(app_name="notepad"),
            ConfirmIntent(),
            ExitProgramIntent(),
        ])

        tts = _make_tts()

        loop = _build_integration_loop(
            intent_parser=parser,
            executor=executor,
            tts_engine=tts,
        )
        _setup_continuous_feeder(loop, count=80, interval=0.02)

        await asyncio.wait_for(loop.run(), timeout=5.0)

        # executor 被调 3 次
        self.assertEqual(executor.execute.call_count, 3)

        # 验证第 2 次调用 confirmed=True
        second_call = executor.execute.call_args_list[1]
        self.assertTrue(second_call[0][2],
                        "第二次执行必须 confirmed=True（二次确认的核心）")

        # TTS 被调 3 次（确认提示 + 确认结果 + 退出确认音）
        self.assertEqual(tts.speak_result.call_count, 3)

        # 循环正常退出
        self.assertTrue(loop._state.exit_requested)


# ======================================================================
# 测试 5：连续 10 轮稳定性
# ======================================================================

class TestMultiRoundStability(unittest.IsolatedAsyncioTestCase):
    """连续 10 轮正常操作 + 1 轮退出，验证 _main_loop() 的 while 循环
    在多轮迭代下不崩溃、不状态泄漏、不死循环。
    """

    async def test_ten_rounds_then_exit(self):
        n_rounds = 10
        intents = [OpenAppIntent(app_name=f"app{i}") for i in range(n_rounds)]
        intents.append(ExitProgramIntent())

        results = [
            ExecutionResult(success=True, action_type="open_app",
                            payload={"app_name": f"app{i}"})
            for i in range(n_rounds)
        ]
        results.append(
            ExecutionResult(success=True, action_type="exit_program",
                            should_exit=True)
        )

        tts = _make_tts()

        loop = _build_integration_loop(
            intent_parser=_make_parser_from_sequence(intents),
            executor=_make_executor_from_sequence(results),
            tts_engine=tts,
        )
        # 10 轮 + 1 退出，每轮至少需要 1 个 LISTEN chunk + 若干 TTS monitor chunks
        _setup_continuous_feeder(loop, count=200, interval=0.01)

        await asyncio.wait_for(loop.run(), timeout=10.0)

        # 核心断言
        self.assertTrue(loop._state.exit_requested)
        # executor 被调 11 次（10 轮 + 1 退出）
        self.assertEqual(
            loop._executor.execute.call_count, n_rounds + 1,
            f"应执行 {n_rounds + 1} 次（{n_rounds} 轮操作 + 1 退出）",
        )
        # TTS 播报 11 次
        self.assertEqual(tts.speak_result.call_count, n_rounds + 1)
        # 至少完成 10 轮（rounds_completed 在正常播完后 +1，退出轮不 +1）
        self.assertGreaterEqual(
            loop._state.rounds_completed, n_rounds,
            f"rounds_completed 应 >= {n_rounds}",
        )


# ======================================================================
# main
# ======================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    unittest.main(verbosity=2)
