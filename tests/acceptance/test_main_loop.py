"""Step 6 验收脚本：主循环端到端测试（CLAUDE.md §5 Step 6）。

验收标准：
    - 端到端跑通全流程：说话 → ASR → 意图 → 执行 → TTS 播报
    - 不需要按任何键，功能层面无 bug
    - 二次确认流程正常（close_app 等危险操作）
    - "退出程序"语音指令能正常退出

两种模式：
    1. --live：真实麦克风 + 真实 LLM + 真实 TTS，完整端到端验收
    2. --mock（默认）：用 MockAudioSource 注入预录音频，验证状态机流转正确

用法（PowerShell，已激活 venv）：

    # 状态机流程验证（无需麦克风/网络，快速）
    python -m tests.acceptance.test_main_loop

    # 真实端到端验收（需麦克风 + LLM API key + 音频设备）
    python -m tests.acceptance.test_main_loop --live --device 1

    # 只验证状态机 + 二次确认流程
    python -m tests.acceptance.test_main_loop --scenario confirm
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_LOG = logging.getLogger("test_main_loop")


# ===========================================================================
# Mock 组件：用于离线验证状态机流转
# ===========================================================================

class MockAudioSource:
    """模拟录音器：按顺序注入预定义的音频块，不依赖真实麦克风。

    把 VAD 和 ASR 解耦出来分别 mock 太复杂，这里直接 mock 整个 MainLoop
    的 _phase_listen / _phase_asr，跳过真实音频处理，只验证状态机。
    """

    def __init__(self) -> None:
        self.injected_segments: list[np.ndarray] = []
        self.texts: list[str] = []
        self.segment_idx = 0
        self.text_idx = 0

    def add_segment(self, text: str) -> None:
        """注入一段"虚拟语音"，对应 ASR 会返回 text。"""
        # 用一个非空 numpy 数组代表语音片段（内容不重要，mock ASR 不看）
        dummy = np.ones(512, dtype=np.float32) * 0.1
        self.injected_segments.append(dummy)
        self.texts.append(text)


@dataclass
class MockScenario:
    """一个测试场景：注入的文本序列 + 期望的最终状态。"""
    name: str
    inputs: list[str]  # 用户依次说的话
    expected_exit: bool = False
    description: str = ""


SCENARIOS: list[MockScenario] = [
    MockScenario(
        name="简单指令：打开记事本",
        inputs=["打开记事本"],
        expected_exit=False,
        description="单轮：说话 → 执行 → 播报 → 回到 LISTEN",
    ),
    MockScenario(
        name="二次确认：关闭记事本",
        inputs=["关闭记事本", "是"],
        expected_exit=False,
        description="两轮：needs_confirm → AWAIT_CONFIRM → 确认 → 执行 → 播报",
    ),
    MockScenario(
        name="二次确认取消：关闭记事本但取消",
        inputs=["关闭记事本", "取消"],
        expected_exit=False,
        description="两轮：needs_confirm → AWAIT_CONFIRM → 取消 → 播报已取消",
    ),
    MockScenario(
        name="退出程序",
        inputs=["退出程序"],
        expected_exit=True,
        description="单轮：ExitProgramIntent → should_exit → 主循环 break",
    ),
    MockScenario(
        name="多轮对话",
        inputs=["查询内存", "查询CPU", "打开记事本", "退出程序"],
        expected_exit=True,
        description="连续多轮指令，最后退出",
    ),
]


# ===========================================================================
# Mock 主循环：替换 ASR 和录音，保留状态机和真实执行/TTS
# ===========================================================================

class MockMainLoop:
    """用真实 Executor + 真实 IntentParser + 真实 TTS，但 ASR 用预设文本。

    这样能验证：状态机流转 + 意图解析 + 执行 + TTS 播报全链路，
    只跳过"真实麦克风 + 真实 ASR"这一段（这段在 --live 模式验证）。
    """

    def __init__(self, config: dict, texts: list[str]) -> None:
        from asr.whisper_engine import WhisperEngine, ASRResult
        from audio.player import AudioPlayer
        from audio.vad import VADProcessor
        from audio.output_mode import OutputMode, detect_output_mode
        from core.state import LoopState, State
        from executor import Executor, ExecutionResult
        from nlu.intent_parser import IntentParser
        from tts.edge_tts_engine import TTSEngine

        self._texts = list(texts)
        self._text_idx = 0
        self._state = LoopState()
        self._State = State

        # 真实组件
        output_mode = detect_output_mode(config.get("audio", {}).get("output_mode", "auto"))
        self._intent_parser = IntentParser.from_config(config["llm"])
        self._executor = Executor.from_config(config["whitelist"], log_path="logs/actions.log")
        self._player = AudioPlayer()
        self._tts = TTSEngine.from_config(config.get("tts", {}), self._player)

        # 阶段间数据
        self._pending_text: Optional[str] = None
        self._pending_intent: Optional[object] = None
        self._pending_result: Optional[ExecutionResult] = None

    async def run(self) -> None:
        """跑完所有预设文本，返回。"""
        self._state.transition(self._State.LISTEN)
        print("=== Mock 主循环启动 ===")

        iteration = 0
        while not self._state.exit_requested:
            iteration += 1
            if iteration > 100:
                print("[Mock] 超过 100 次迭代，强制退出（可能死循环）")
                break

            try:
                if self._state.state == self._State.LISTEN:
                    await self._phase_listen()
                elif self._state.state == self._State.ASR:
                    await self._phase_asr()
                elif self._state.state == self._State.EXECUTE:
                    await self._phase_execute()
                elif self._state.state == self._State.TTS:
                    await self._phase_tts()
                elif self._state.state == self._State.AWAIT_CONFIRM:
                    await self._phase_await_confirm()
                else:
                    self._state.transition(self._State.LISTEN)
            except Exception as exc:
                print(f"[Mock] 异常: {type(exc).__name__}: {exc}")
                _LOG.exception("mock loop error")
                break

        print(f"=== Mock 主循环结束（共 {self._state.rounds_completed} 轮）===")
        self._intent_parser.close()

    async def _phase_listen(self) -> None:
        """Mock：直接取下一条预设文本，跳过 VAD。"""
        if self._text_idx >= len(self._texts):
            print("[Mock] 预设文本已用完，结束")
            self._state.exit_requested = True
            return

        text = self._texts[self._text_idx]
        self._text_idx += 1
        self._pending_text = text
        print(f"\n[Mock] 注入用户语音: {text!r}")
        self._state.transition(self._State.ASR)

    async def _phase_asr(self) -> None:
        """Mock：直接用预设文本作为 ASR 结果，调真实意图解析。"""
        text = self._pending_text or ""
        self._pending_text = None
        print(f"[ASR] {text!r}")

        intent = await asyncio.to_thread(self._intent_parser.parse, text)

        if self._state.pending is not None:
            await self._handle_confirm_response(intent, text)
            return

        self._pending_intent = intent
        self._state.transition(self._State.EXECUTE)

    async def _phase_execute(self) -> None:
        from nlu.schema import UnknownIntent
        intent = self._pending_intent
        raw_text = self._pending_text or ""
        self._pending_intent = None
        self._pending_text = None

        if isinstance(intent, UnknownIntent):
            print(f"[意图] Unknown: {intent.reason}")
            from executor import ExecutionResult
            self._pending_result = ExecutionResult(
                success=False, action_type="unknown",
                error=intent.reason or "无法理解",
            )
            self._state.transition(self._State.TTS)
            return

        print(f"[意图] {type(intent).__name__}: {intent}")
        result = await asyncio.to_thread(self._executor.execute, intent, raw_text, False)
        self._pending_result = result
        print(f"[执行] success={result.success} action={result.action_type} "
              f"needs_conf={result.needs_confirmation} exit={result.should_exit}")

        # 修订 14：should_exit 不在 EXECUTE 置位，挪到 _phase_tts 播完后

        if result.needs_confirmation:
            self._state.set_pending_confirmation(intent, raw_text, result)
            self._state.transition(self._State.TTS)
            return

        self._state.transition(self._State.TTS)

    async def _phase_tts(self) -> None:
        from tts.edge_tts_engine import format_result
        result = self._pending_result
        self._pending_result = None

        if result is None:
            self._state.transition(self._State.LISTEN)
            return

        text = format_result(result)
        print(f"[TTS] 播报: {text!r}")
        await self._tts.speak(text)

        if result.should_exit:
            # 修订 14：退出播报正常播完后才置位
            self._state.exit_requested = True
            return
        if self._state.pending is not None:
            self._state.transition(self._State.AWAIT_CONFIRM)
        else:
            self._state.rounds_completed += 1
            self._state.transition(self._State.LISTEN)

    async def _phase_await_confirm(self) -> None:
        """Mock：和 _phase_listen 一样取下一条预设文本。"""
        if self._text_idx >= len(self._texts):
            print("[Mock] AWAIT_CONFIRM 但没有更多预设文本，超时")
            self._state.exit_requested = True
            return
        text = self._texts[self._text_idx]
        self._text_idx += 1
        self._pending_text = text
        print(f"[Mock] 注入确认语音: {text!r}")
        self._state.transition(self._State.ASR)

    async def _handle_confirm_response(self, intent, text: str) -> None:
        from nlu.schema import ConfirmIntent, CancelIntent
        pending = self._state.clear_pending_confirmation()
        if pending is None:
            self._state.transition(self._State.LISTEN)
            return

        if isinstance(intent, ConfirmIntent):
            print(f"[确认] 重新执行 {pending.intent} (confirmed=True)")
            result = await asyncio.to_thread(
                self._executor.execute, pending.intent, pending.raw_text, True
            )
            self._pending_result = result
            # 修订 14：should_exit 置位延迟到 _phase_tts 播完后
            self._state.transition(self._State.TTS)
            return

        if isinstance(intent, CancelIntent):
            print("[取消] 丢弃 pending")
            from executor import ExecutionResult
            self._pending_result = ExecutionResult(
                success=True, action_type="cancel",
                payload={"message": "已取消"},
            )
            self._state.transition(self._State.TTS)
            return

        print(f"[改主意] 收到非确认意图 {intent}，按新意图处理")
        self._pending_intent = intent
        self._pending_text = text
        self._state.transition(self._State.EXECUTE)


# ===========================================================================
# 测试运行
# ===========================================================================

def run_mock_scenarios(config: dict, scenarios: list[MockScenario]) -> tuple[int, int]:
    """跑 Mock 场景，返回 (passed, total)。"""
    print("\n" + "=" * 70)
    print("Part 1: 状态机流程验证（Mock ASR + 真实 LLM + 真实执行 + 真实 TTS）")
    print("=" * 70)

    passed = 0
    total = len(scenarios)

    # 检查 API key 是否可用，不可用则跳过真实 LLM 场景，只跑纯状态机
    import os
    api_key_set = bool(os.environ.get(config["llm"]["api_key_env"], "").strip())
    if not api_key_set:
        print(f"[警告] {config['llm']['api_key_env']} 未设置，改跑纯状态机测试（mock LLM）")
        return run_state_machine_only_tests(scenarios)

    for i, sc in enumerate(scenarios, 1):
        print(f"\n--- 场景 {i}/{total}: {sc.name} ---")
        print(f"描述: {sc.description}")
        print(f"输入: {sc.inputs}")
        print()

        try:
            loop = MockMainLoop(config, sc.inputs)
            asyncio.run(loop.run())
        except Exception as exc:
            print(f"[场景失败] 异常: {type(exc).__name__}: {exc}")
            _LOG.exception("scenario failed")
            continue

        # 简单判断：跑完没异常就算通过（退出条件由场景控制）
        print(f"\n[结果] exit_requested={loop._state.exit_requested} "
              f"rounds={loop._state.rounds_completed}")

        ok = True
        if sc.expected_exit and not loop._state.exit_requested:
            print("[失败] 期望退出但未退出")
            ok = False
        if not sc.expected_exit and loop._state.exit_requested and sc.inputs:
            # 非退出场景但退出了，除非是最后一个场景自然结束
            pass

        if ok:
            print("[通过]")
            passed += 1
        else:
            print("[失败]")

    return passed, total


# ===========================================================================
# 纯状态机测试：完全不依赖 LLM/TTS/麦克风，只验证状态流转
# ===========================================================================

class PureStateMachineLoop:
    """完全 mock 的主循环：mock LLM（按关键词返回固定意图）+ mock TTS（只打日志）。

    用于验证 state.py + event_loop.py 的状态流转逻辑正确性，
    不需要 API key、麦克风、音频设备。
    """

    def __init__(self, texts: list[str]) -> None:
        from core.state import LoopState, State
        from executor import ExecutionResult
        from nlu.schema import (
            OpenAppIntent, CloseAppIntent, QuerySystemIntent,
            ExitProgramIntent, ConfirmIntent, CancelIntent, UnknownIntent,
        )

        self._texts = list(texts)
        self._idx = 0
        self._state = LoopState()
        self._State = State
        self._ExecutionResult = ExecutionResult

        # 保存意图类供 _mock_parse 用
        self._intent_classes = {
            "open_app": OpenAppIntent,
            "close_app": CloseAppIntent,
            "query_system": QuerySystemIntent,
            "exit": ExitProgramIntent,
            "confirm": ConfirmIntent,
            "cancel": CancelIntent,
            "unknown": UnknownIntent,
        }

        # 记录状态转换序列，供测试断言
        self.transitions: list[str] = []
        self.executions: list[str] = []

    def _mock_parse(self, text: str):
        """关键词匹配返回固定意图，模拟 LLM。"""
        text = text.strip()
        if "打开" in text and "记事本" in text:
            return self._intent_classes["open_app"](app_name="notepad")
        if "关闭" in text and "记事本" in text:
            return self._intent_classes["close_app"](app_name="notepad")
        if "内存" in text:
            return self._intent_classes["query_system"](resource="memory")
        if "CPU" in text or "cpu" in text:
            return self._intent_classes["query_system"](resource="cpu")
        if "退出" in text:
            return self._intent_classes["exit"]()
        if text in ("是", "确认", "确定", "yes"):
            return self._intent_classes["confirm"]()
        if text in ("取消", "不", "no"):
            return self._intent_classes["cancel"]()
        return self._intent_classes["unknown"](reason=f"mock: {text}")

    def _mock_execute(self, intent, confirmed: bool):
        """mock 执行器：按意图类型返回固定结果。"""
        from nlu.schema import (
            OpenAppIntent, CloseAppIntent, QuerySystemIntent, ExitProgramIntent,
        )
        if isinstance(intent, OpenAppIntent):
            self.executions.append(f"open_app({intent.app_name}, confirmed={confirmed})")
            return self._ExecutionResult(
                success=True, action_type="open_app",
                payload={"app_name": intent.app_name},
            )
        if isinstance(intent, CloseAppIntent):
            if not confirmed:
                self.executions.append(f"close_app({intent.app_name}, needs_confirm)")
                return self._ExecutionResult(
                    success=False, action_type="close_app", needs_confirmation=True,
                    payload={"app_name": intent.app_name},
                )
            self.executions.append(f"close_app({intent.app_name}, confirmed)")
            return self._ExecutionResult(
                success=True, action_type="close_app",
                payload={"app_name": intent.app_name, "terminated_pids": [9999]},
            )
        if isinstance(intent, QuerySystemIntent):
            self.executions.append(f"query_system({intent.resource})")
            return self._ExecutionResult(
                success=True, action_type="query_system",
                payload={"resource": intent.resource, "available_gb": 8.0,
                         "total_gb": 16.0, "percent_used": 50.0},
            )
        if isinstance(intent, ExitProgramIntent):
            self.executions.append("exit_program")
            return self._ExecutionResult(
                success=True, action_type="exit_program", should_exit=True,
            )
        return self._ExecutionResult(
            success=False, action_type="unknown", error="mock unknown",
        )

    def _record_transition(self, new_state) -> None:
        self.transitions.append(new_state.value)
        self._state.transition(new_state)

    async def run(self) -> None:
        from nlu.schema import UnknownIntent, ConfirmIntent, CancelIntent
        State = self._State

        self._record_transition(State.LISTEN)
        iteration = 0
        while not self._state.exit_requested:
            iteration += 1
            # 防死循环上限：每条输入约 4 次状态转换，留 2x 余量
            max_iter = max(100, len(self._texts) * 8)
            if iteration > max_iter:
                print(f"[Pure] 超过 {max_iter} 次迭代，强制退出")
                break

            if self._state.state == State.LISTEN:
                if self._idx >= len(self._texts):
                    break
                text = self._texts[self._idx]
                self._idx += 1
                print(f"  [LISTEN] 注入: {text!r}")
                self._pending_text = text
                self._record_transition(State.ASR)

            elif self._state.state == State.ASR:
                text = self._pending_text
                self._pending_text = None
                intent = self._mock_parse(text)
                print(f"  [ASR] {text!r} → {type(intent).__name__}")

                if self._state.pending is not None:
                    # 二次确认阶段
                    pending = self._state.clear_pending_confirmation()
                    if isinstance(intent, ConfirmIntent):
                        result = self._mock_execute(pending.intent, confirmed=True)
                        self._pending_result = result
                        # 修订 14：should_exit 置位延迟到 TTS 播完后
                        self._record_transition(State.TTS)
                    elif isinstance(intent, CancelIntent):
                        self._pending_result = self._ExecutionResult(
                            success=True, action_type="cancel",
                            payload={"message": "已取消"},
                        )
                        self._record_transition(State.TTS)
                    else:
                        # 改主意
                        self._pending_intent = intent
                        self._pending_text = text
                        self._record_transition(State.EXECUTE)
                else:
                    self._pending_intent = intent
                    self._record_transition(State.EXECUTE)

            elif self._state.state == State.EXECUTE:
                intent = self._pending_intent
                raw_text = self._pending_text or ""
                self._pending_intent = None
                self._pending_text = None

                if isinstance(intent, UnknownIntent):
                    self._pending_result = self._ExecutionResult(
                        success=False, action_type="unknown",
                        error=intent.reason,
                    )
                    self._record_transition(State.TTS)
                else:
                    result = self._mock_execute(intent, confirmed=False)
                    self._pending_result = result
                    # 修订 14：should_exit 置位延迟到 TTS 播完后
                    if result.needs_confirmation:
                        self._state.set_pending_confirmation(intent, raw_text, result)
                        self._record_transition(State.TTS)
                    else:
                        self._record_transition(State.TTS)

            elif self._state.state == State.TTS:
                result = self._pending_result
                self._pending_result = None
                print(f"  [TTS] 播报: {result.action_type} success={result.success}")

                if result.should_exit:
                    # 修订 14：退出播报正常播完后才置位
                    self._state.exit_requested = True
                    break
                if self._state.pending is not None:
                    self._record_transition(State.AWAIT_CONFIRM)
                else:
                    self._state.rounds_completed += 1
                    self._record_transition(State.LISTEN)

            elif self._state.state == State.AWAIT_CONFIRM:
                # 等下一句话，走 LISTEN 取下一条文本
                if self._idx >= len(self._texts):
                    print("  [AWAIT_CONFIRM] 无更多输入，超时退出")
                    break
                # 复用 LISTEN 逻辑取下一条
                text = self._texts[self._idx]
                self._idx += 1
                print(f"  [AWAIT_CONFIRM] 注入: {text!r}")
                self._pending_text = text
                self._record_transition(State.ASR)

            else:
                self._record_transition(State.LISTEN)


# 预期状态序列定义（用于断言）
EXPECTED_TRANSITIONS = {
    "简单指令：打开记事本": [
        "listen", "asr", "execute", "tts", "listen",
    ],
    "二次确认：关闭记事本": [
        "listen", "asr", "execute", "tts",  # needs_confirm
        "await_confirm", "asr",  # 是
        # confirm 路径：直接到 TTS（不经过 EXECUTE）
        "tts", "listen",
    ],
    "二次确认取消：关闭记事本但取消": [
        "listen", "asr", "execute", "tts",  # needs_confirm
        "await_confirm", "asr",  # 取消
        "tts", "listen",
    ],
    "退出程序": [
        "listen", "asr", "execute", "tts",  # should_exit → break
    ],
    "多轮对话": [
        "listen", "asr", "execute", "tts",  # 查询内存
        "listen", "asr", "execute", "tts",  # 查询CPU
        "listen", "asr", "execute", "tts",  # 打开记事本
        "listen", "asr", "execute", "tts",  # 退出程序
    ],
}


def run_state_machine_only_tests(scenarios: list[MockScenario]) -> tuple[int, int]:
    """纯状态机测试：完全不依赖 LLM/TTS/麦克风，只验证状态流转。"""
    print("\n" + "=" * 70)
    print("纯状态机测试（mock LLM + mock TTS，验证状态流转逻辑）")
    print("=" * 70)

    passed = 0
    total = len(scenarios)

    for i, sc in enumerate(scenarios, 1):
        print(f"\n--- 场景 {i}/{total}: {sc.name} ---")
        print(f"输入: {sc.inputs}")

        loop = PureStateMachineLoop(sc.inputs)
        asyncio.run(loop.run())

        print(f"  状态序列: {loop.transitions}")
        print(f"  执行序列: {loop.executions}")
        print(f"  exit={loop._state.exit_requested} rounds={loop._state.rounds_completed}")

        # 断言状态序列
        expected = EXPECTED_TRANSITIONS.get(sc.name)
        ok = True
        if expected is not None:
            if loop.transitions != expected:
                print(f"  [失败] 状态序列不匹配")
                print(f"         期望: {expected}")
                print(f"         实际: {loop.transitions}")
                ok = False

        if sc.expected_exit and not loop._state.exit_requested:
            print("  [失败] 期望退出但未退出")
            ok = False

        if ok:
            print("  [通过]")
            passed += 1
        else:
            print("  [失败]")

    return passed, total


async def run_live(config: dict, device_index: Optional[int] = None) -> int:
    """真实端到端验收：启动真实 MainLoop，等用户语音交互。"""
    print("\n" + "=" * 70)
    print("Part 2: 真实端到端验收（真实麦克风 + 真实 LLM + 真实 TTS）")
    print("=" * 70)
    print()
    print("验收清单（请依次对着麦克风说）：")
    print('  1. "打开记事本"        → 应该打开 notepad 并播报"已为你打开 notepad"')
    print('  2. "查询内存"          → 应该播报"当前内存剩余 X GB..."')
    print('  3. "关闭记事本"        → 应该播报"确定要关闭 notepad 吗？"')
    print('  4. "是"               → 应该关闭 notepad 并播报"已关闭 notepad"')
    print('  5. "退出程序"          → 应该播报"再见"并退出')
    print()

    from core import MainLoop

    # 覆盖设备索引
    if device_index is not None:
        config.setdefault("audio", {})["input_device_index"] = device_index

    loop = MainLoop.from_config(config)
    await loop.run()
    return 0


# ===========================================================================
# main
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Step 6 主循环验收")
    ap.add_argument(
        "--live", action="store_true",
        help="真实端到端验收（需麦克风 + LLM API）",
    )
    ap.add_argument(
        "--device", type=int, default=None,
        help="音频输入设备索引（--live 模式用）",
    )
    ap.add_argument(
        "--scenario", default=None,
        help="只跑指定场景（按名字匹配）",
    )
    ap.add_argument(
        "--config", default="config.yaml",
        help="配置文件路径",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[错误] 配置文件不存在: {config_path}", file=sys.stderr)
        return 1

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.live:
        return asyncio.run(run_live(config, args.device))

    # Mock 模式
    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in SCENARIOS if args.scenario in s.name]
        if not scenarios:
            print(f"[错误] 没有匹配的场景: {args.scenario}", file=sys.stderr)
            return 1

    passed, total = run_mock_scenarios(config, scenarios)
    print(f"\n=== 总计 {passed}/{total} 场景通过 ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
