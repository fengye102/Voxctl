"""修订 56 特性集成测试：唤醒词 / 听写模式 / 多轮上下文 / 度量埋点 / enter_dictation 意图。

隔离模块（core.phrase_match、executor.text_input）各有独立单测，本文件测的是它们
接进 MainLoop / executor / intent_parser 后的**接线行为**，直接跑生产代码路径。
复用 test_event_loop 的 mock 工厂，避免重复造轮子。
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import numpy as np

from core.state import State
from executor import ExecutionResult, Executor
from executor.text_input import TextInputError
from nlu.schema import (
    EnterDictationIntent,
    OpenAppIntent,
    build_intent_schema,
    validate_intent,
)
from tts.edge_tts_engine import format_result

from tests.unit.test_event_loop import (
    _build_loop,
    _make_asr,
    _make_executor,
    _make_parser,
)


def _seg() -> np.ndarray:
    return np.zeros(16000, dtype=np.float32)


# ======================================================================
# enter_dictation 意图（schema + executor + TTS 模板）
# ======================================================================

class TestEnterDictationIntent(unittest.TestCase):
    def test_schema_validates(self):
        intent = validate_intent({"intent": "enter_dictation"})
        self.assertIsInstance(intent, EnterDictationIntent)

    def test_in_tool_schema(self):
        names = [i["name"] for i in build_intent_schema()["intents"]]
        self.assertIn("enter_dictation", names)

    def test_executor_sets_flag(self):
        with TemporaryDirectory() as d:
            ex = Executor.from_config(
                {"apps": {"x": "C:\\x.exe"}, "commands": [], "paths": [],
                 "allow_open_any_app": False},
                log_path=str(Path(d) / "a.log"),
            )
            result = ex.execute(EnterDictationIntent(), "进入听写")
        self.assertTrue(result.success)
        self.assertEqual(result.action_type, "enter_dictation")
        self.assertTrue(result.enter_dictation)
        self.assertIn("听写", result.payload["message"])

    def test_tts_templates(self):
        self.assertEqual(
            format_result(ExecutionResult(True, "wake_ack", payload={"message": "在"})),
            "在",
        )
        self.assertIn(
            "听写",
            format_result(ExecutionResult(
                True, "enter_dictation", payload={"message": "进入听写模式，说的话会被打字输入"},
            )),
        )
        self.assertEqual(
            format_result(ExecutionResult(True, "exit_dictation", payload={"message": "已退出听写模式"})),
            "已退出听写模式",
        )


# ======================================================================
# intent_parser 多轮上下文透传
# ======================================================================

class TestIntentParserContext(unittest.TestCase):
    def _parser(self):
        from nlu.intent_parser import IntentParser
        client = MagicMock()
        client.parse_intent = MagicMock(return_value={"intent": "unknown", "reason": "x"})
        return IntentParser(client), client

    def test_context_passed_through(self):
        parser, client = self._parser()
        parser.parse("关闭它", context="刚刚打开了应用：记事本")
        args = client.parse_intent.call_args
        self.assertEqual(args.args[0], "关闭它")
        self.assertEqual(args.args[2], "刚刚打开了应用：记事本")

    def test_no_context_is_none(self):
        parser, client = self._parser()
        parser.parse("打开浏览器")
        self.assertIsNone(client.parse_intent.call_args.args[2])


# ======================================================================
# 唤醒词过滤（_phase_asr）
# ======================================================================

class TestWakeWord(unittest.IsolatedAsyncioTestCase):
    def _loop(self, text, **kw):
        parser = _make_parser()
        loop = _build_loop(
            asr_engine=_make_asr(text=text),
            intent_parser=parser,
            wake_word_enabled=True,
            wake_word_phrases=["小助手"],
            **kw,
        )
        loop._pending_audio = _seg()
        return loop, parser

    async def test_miss_dropped(self):
        loop, parser = self._loop("打开记事本")
        await loop._phase_asr()
        self.assertEqual(loop._state.state, State.LISTEN)
        parser.parse.assert_not_called()

    async def test_hit_strips_and_parses(self):
        loop, parser = self._loop("小助手打开记事本")
        await loop._phase_asr()
        self.assertEqual(loop._state.state, State.EXECUTE)
        self.assertEqual(parser.parse.call_args.args[0], "打开记事本")

    async def test_bare_wake_word_acks(self):
        loop, parser = self._loop("小助手")
        await loop._phase_asr()
        self.assertEqual(loop._state.state, State.TTS)
        self.assertEqual(loop._pending_result.action_type, "wake_ack")
        parser.parse.assert_not_called()

    async def test_bare_wake_word_no_ack_listens(self):
        loop, parser = self._loop("小助手", wake_word_ack=False)
        await loop._phase_asr()
        self.assertEqual(loop._state.state, State.LISTEN)
        parser.parse.assert_not_called()

    async def test_disabled_passes_through(self):
        parser = _make_parser()
        loop = _build_loop(asr_engine=_make_asr(text="打开记事本"), intent_parser=parser)
        loop._pending_audio = _seg()
        await loop._phase_asr()
        self.assertEqual(loop._state.state, State.EXECUTE)
        self.assertEqual(parser.parse.call_args.args[0], "打开记事本")


# ======================================================================
# 听写模式（_phase_asr → _handle_dictation）
# ======================================================================

class TestDictation(unittest.IsolatedAsyncioTestCase):
    def _loop(self, text):
        parser = _make_parser()
        loop = _build_loop(
            asr_engine=_make_asr(text=text),
            intent_parser=parser,
            dictation_exit_phrases=["退出听写"],
        )
        loop._dictation_mode = True
        loop._pending_audio = _seg()
        return loop, parser

    async def test_inject_then_listen(self):
        loop, parser = self._loop("你好世界")
        with patch("core.event_loop.inject_text", MagicMock(return_value=4)) as inj:
            await loop._phase_asr()
        inj.assert_called_once_with("你好世界")
        parser.parse.assert_not_called()
        self.assertEqual(loop._state.state, State.LISTEN)
        self.assertTrue(loop._dictation_mode)  # 仍在听写模式

    async def test_exit_phrase(self):
        loop, parser = self._loop("退出听写")
        with patch("core.event_loop.inject_text", MagicMock()) as inj:
            await loop._phase_asr()
        inj.assert_not_called()
        self.assertFalse(loop._dictation_mode)
        self.assertEqual(loop._state.state, State.TTS)
        self.assertEqual(loop._pending_result.action_type, "exit_dictation")

    async def test_inject_failure_exits(self):
        loop, parser = self._loop("你好")
        with patch("core.event_loop.inject_text",
                   MagicMock(side_effect=TextInputError("no win32"))):
            await loop._phase_asr()
        self.assertFalse(loop._dictation_mode)  # 失败退出，不困在模式里
        self.assertEqual(loop._state.state, State.TTS)
        self.assertFalse(loop._pending_result.success)

    async def test_audit_redacts_content(self):
        """§6 隐私：听写注入只记字符数，不落明文。"""
        with TemporaryDirectory() as d:
            audit = Path(d) / "actions.log"
            loop, _ = self._loop("我的银行密码是一二三")
            loop._audit_log_path = audit
            with patch("core.event_loop.inject_text", MagicMock(return_value=9)):
                await loop._phase_asr()
            content = audit.read_text(encoding="utf-8")
        self.assertNotIn("银行密码", content)  # 明文不落盘
        self.assertIn("听写", content)


# ======================================================================
# 多轮上下文（_phase_asr 透传 + _phase_execute 更新摘要）
# ======================================================================

class TestMultiTurn(unittest.IsolatedAsyncioTestCase):
    async def test_context_passed_when_enabled(self):
        parser = _make_parser()
        loop = _build_loop(asr_engine=_make_asr(text="关闭它"),
                           intent_parser=parser, multi_turn=True)
        loop._last_context = "刚刚打开了应用：记事本"
        loop._pending_audio = _seg()
        await loop._phase_asr()
        self.assertEqual(parser.parse.call_args.args[1], "刚刚打开了应用：记事本")

    async def test_context_none_when_disabled(self):
        parser = _make_parser()
        loop = _build_loop(asr_engine=_make_asr(text="关闭它"),
                           intent_parser=parser, multi_turn=False)
        loop._last_context = "刚刚打开了应用：记事本"
        loop._pending_audio = _seg()
        await loop._phase_asr()
        self.assertIsNone(parser.parse.call_args.args[1])

    async def test_summary_updated_on_success(self):
        loop = _build_loop(
            intent_parser=_make_parser(intent=OpenAppIntent(app_name="记事本")),
            executor=_make_executor(ExecutionResult(True, "open_app", payload={})),
            multi_turn=True,
        )
        loop._pending_intent = OpenAppIntent(app_name="记事本")
        loop._pending_text = "打开记事本"
        await loop._phase_execute()
        self.assertIn("记事本", loop._last_context or "")

    async def test_summary_not_updated_when_disabled(self):
        loop = _build_loop(
            executor=_make_executor(ExecutionResult(True, "open_app", payload={})),
            multi_turn=False,
        )
        loop._pending_intent = OpenAppIntent(app_name="记事本")
        loop._pending_text = "打开记事本"
        await loop._phase_execute()
        self.assertIsNone(loop._last_context)


# ======================================================================
# 度量埋点（_phase_tts 记 metrics 行）
# ======================================================================

class TestMetrics(unittest.IsolatedAsyncioTestCase):
    def _prime_timing(self, loop):
        now = time.perf_counter()
        loop._t_speech_end = now - 0.5
        loop._t_asr_done = now - 0.3
        loop._t_intent_done = now - 0.1
        loop._t_exec_done = now - 0.05

    async def test_metrics_logged_when_enabled(self):
        loop = _build_loop(metrics_enabled=True)
        self._prime_timing(loop)
        loop._pending_result = ExecutionResult(True, "open_app", payload={})
        with patch("core.event_loop.log_metrics", MagicMock()) as lm:
            await loop._phase_tts()
        lm.assert_called_once()
        # speech_to_tts_ms（第 4 位置参）非空且为正
        self.assertGreater(lm.call_args.args[4], 0)
        self.assertEqual(lm.call_args.kwargs.get("action_type"), "open_app")

    async def test_metrics_skipped_when_disabled(self):
        loop = _build_loop(metrics_enabled=False)
        self._prime_timing(loop)
        loop._pending_result = ExecutionResult(True, "open_app", payload={})
        with patch("core.event_loop.log_metrics", MagicMock()) as lm:
            await loop._phase_tts()
        lm.assert_not_called()

    async def test_metrics_row_written_to_audit(self):
        from core.audit import log_metrics
        with TemporaryDirectory() as d:
            p = Path(d) / "actions.log"
            log_metrics(p, 200.0, 120.5, 30.0, 350.7, action_type="open_app")
            entry = json.loads(p.read_text(encoding="utf-8").strip())
        self.assertEqual(entry["event"], "metrics")
        self.assertEqual(entry["speech_to_tts_ms"], 350.7)
        self.assertEqual(entry["action_type"], "open_app")


if __name__ == "__main__":
    unittest.main()
