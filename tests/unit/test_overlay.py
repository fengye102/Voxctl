"""状态浮窗 ui 包单元测试（CLAUDE.md §8.11 / 修订 26）。

测试策略——只测能脱离 pywebview / 音频栈独立验证的纯逻辑：
    1. state_protocol：encode/parse 往返、脏输入不抛异常返回 None
    2. state_emitter：env 闸门、同态 200ms 节流、stdout=None / 未启用时静默
    3. state_tailer：文件不存在等待、tail 到新行推给假 window、rotate 重打开
不测 overlay_window（需真起 pywebview 窗口 + Windows HWND，属 frozen 冒烟范畴）。
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ui import state_emitter, state_protocol
from ui.state_protocol import LINE_PREFIX, encode_state_event, parse_line
from ui.state_tailer import StateTailer


# ===========================================================================
# 1. state_protocol：编码 / 解析往返 + 脏输入健壮性
# ===========================================================================

class _FakeState:
    """鸭子类型 State：只需有 .value（协议不 import core.state）。"""
    def __init__(self, value):
        self.value = value


class TestStateProtocol(unittest.TestCase):

    def test_encode_has_prefix_and_parses_back(self):
        line = encode_state_event(_FakeState("listen"))
        self.assertIn(LINE_PREFIX, line)
        obj = parse_line(line)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["event"], "state")
        self.assertEqual(obj["state"], "listen")
        self.assertIn("ts", obj)

    def test_encode_accepts_plain_string_state(self):
        # State.value 是字符串，直接传字符串也应当工作
        obj = parse_line(encode_state_event("asr"))
        self.assertEqual(obj["state"], "asr")

    def test_encode_with_text(self):
        obj = parse_line(encode_state_event(_FakeState("execute"), text="打开浏览器"))
        self.assertEqual(obj["text"], "打开浏览器")

    def test_parse_line_with_leading_log_noise(self):
        # tail 到的行前面可能有 logging 前缀，parse 要能从中定位 prefix
        raw = "2026-07-15 12:00:00 INFO " + encode_state_event(_FakeState("tts"))
        obj = parse_line(raw)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["state"], "tts")

    def test_parse_line_ignores_non_protocol_lines(self):
        for junk in ["", "   ", "just a normal log line\n",
                     LINE_PREFIX + "not-json", LINE_PREFIX + "{bad json"]:
            self.assertIsNone(parse_line(junk), f"should ignore: {junk!r}")

    def test_parse_line_rejects_unknown_event(self):
        line = state_protocol.encode_event("bogus_event", foo=1)
        self.assertIsNone(parse_line(line))

    def test_parse_line_never_raises(self):
        # 各种畸形输入都不许抛
        for junk in [None, 123, b"bytes", LINE_PREFIX, LINE_PREFIX + "null"]:
            try:
                parse_line(junk)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                self.fail(f"parse_line raised on {junk!r}: {exc}")


# ===========================================================================
# 2. state_emitter：env 闸门 + 节流 + 静默容错
# ===========================================================================

class TestStateEmitter(unittest.TestCase):

    def setUp(self):
        self._orig_stdout = sys.stdout
        self._orig_env = state_emitter.os.environ.get(state_emitter._ENABLE_ENV)
        state_emitter.reset_throttle()

    def tearDown(self):
        sys.stdout = self._orig_stdout
        if self._orig_env is None:
            state_emitter.os.environ.pop(state_emitter._ENABLE_ENV, None)
        else:
            state_emitter.os.environ[state_emitter._ENABLE_ENV] = self._orig_env
        state_emitter.reset_throttle()

    def _enable(self, on: bool):
        if on:
            state_emitter.os.environ[state_emitter._ENABLE_ENV] = "1"
        else:
            state_emitter.os.environ.pop(state_emitter._ENABLE_ENV, None)

    def test_disabled_emits_nothing(self):
        self._enable(False)
        buf = io.StringIO()
        sys.stdout = buf
        state_emitter.emit_state(_FakeState("listen"))
        self.assertEqual(buf.getvalue(), "")

    def test_enabled_writes_protocol_line(self):
        self._enable(True)
        buf = io.StringIO()
        sys.stdout = buf
        state_emitter.emit_state(_FakeState("listen"))
        self.assertIn(LINE_PREFIX, buf.getvalue())
        self.assertIsNotNone(parse_line(buf.getvalue()))

    def test_state_change_always_immediate(self):
        self._enable(True)
        buf = io.StringIO()
        sys.stdout = buf
        state_emitter.emit_state(_FakeState("listen"))
        state_emitter.emit_state(_FakeState("asr"))
        state_emitter.emit_state(_FakeState("execute"))
        self.assertEqual(buf.getvalue().count(LINE_PREFIX), 3)

    def test_same_state_throttled_within_window(self):
        self._enable(True)
        buf = io.StringIO()
        sys.stdout = buf
        state_emitter.emit_state(_FakeState("listen"))
        state_emitter.emit_state(_FakeState("listen"))  # 立刻重复 → 被节流
        self.assertEqual(buf.getvalue().count(LINE_PREFIX), 1)

    def test_stdout_none_is_silent(self):
        self._enable(True)
        sys.stdout = None  # windowed 子进程可能 stdout=None
        try:
            state_emitter.emit_state(_FakeState("tts"))  # 不许抛
        except Exception as exc:  # noqa: BLE001
            self.fail(f"emit_state raised with stdout=None: {exc}")

    def test_emit_error_and_exit(self):
        self._enable(True)
        buf = io.StringIO()
        sys.stdout = buf
        state_emitter.emit_error("出错了")
        state_emitter.emit_exit()
        lines = [ln for ln in buf.getvalue().splitlines() if LINE_PREFIX in ln]
        self.assertEqual(len(lines), 2)
        self.assertEqual(parse_line(lines[0])["event"], "error")
        self.assertEqual(parse_line(lines[1])["event"], "exit")


# ===========================================================================
# 3. state_tailer：文件跟随 + 推送 + rotate
# ===========================================================================

class _FakeWindow:
    """假 pywebview window：记录 evaluate_js 收到的脚本。"""
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class TestStateTailer(unittest.TestCase):

    def test_tails_appended_protocol_lines(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "voxctl-voice.out"
            out.write_text("", encoding="utf-8")   # 先建空文件
            win = _FakeWindow()
            tailer = StateTailer(out, win)
            tailer.start()
            try:
                # 等 tailer 线程打开空文件并 seek(0,2)（pos=0），再写新内容；
                # 否则 tailer 会 seek 到已写内容的末尾，永远读不到这些行。
                time.sleep(0.3)
                # 追加一行协议行 + 一行普通日志
                with open(out, "a", encoding="utf-8") as f:
                    f.write("normal log line\n")
                    f.write(encode_state_event(_FakeState("listen")) + "\n")
                    f.flush()
                self.assertTrue(_wait_until(lambda: len(win.scripts) >= 1),
                                "tailer should have pushed the state line")
                self.assertIn("setState", win.scripts[-1])
            finally:
                tailer.stop()

    def test_waits_for_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "not-yet.out"   # 尚不存在
            win = _FakeWindow()
            tailer = StateTailer(out, win)
            tailer.start()
            try:
                time.sleep(0.3)  # 文件不存在，线程应在等待，不崩
                self.assertEqual(win.scripts, [])
                # 现在创建并写入
                out.write_text(encode_state_event(_FakeState("asr")) + "\n",
                               encoding="utf-8")
                self.assertTrue(_wait_until(lambda: len(win.scripts) >= 1))
            finally:
                tailer.stop()

    def test_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.out"
            out.write_text("", encoding="utf-8")
            tailer = StateTailer(out, _FakeWindow())
            tailer.start()
            tailer.stop()
            tailer.stop()  # 二次 stop 不许抛


if __name__ == "__main__":
    unittest.main()
