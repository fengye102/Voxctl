"""全局热键解析单元测试（CLAUDE.md §8.11 / §6 / 修订 33）。

只测能脱离 Win32 独立验证的纯逻辑：热键字符串解析（parse_hotkey）。
不测 HotkeyListener 的 RegisterHotKey / 消息循环——那需要真 Windows 消息队列 +
被占用/未占用的实际热键，属 frozen 冒烟范畴（同 overlay_window 的定位）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ui.hotkey import HotkeyError, parse_hotkey

# 与 ui/hotkey.py 内部常量对齐（测试独立复刻，避免依赖私有名）
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000


class TestParseHotkey(unittest.TestCase):
    def test_basic_ctrl_alt_letter(self):
        mods, vk = parse_hotkey("ctrl+alt+v")
        self.assertEqual(mods, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT)
        self.assertEqual(vk, ord("V"))

    def test_case_and_space_insensitive(self):
        mods, vk = parse_hotkey("  Ctrl + ALT + V ")
        self.assertEqual(mods, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT)
        self.assertEqual(vk, ord("V"))

    def test_shift_win_aliases(self):
        # super / cmd 都映射 WIN；control 同 ctrl
        mods, vk = parse_hotkey("control+shift+space")
        self.assertEqual(mods, _MOD_CONTROL | _MOD_SHIFT | _MOD_NOREPEAT)
        self.assertEqual(vk, 0x20)
        mods2, _ = parse_hotkey("win+shift+a")
        self.assertTrue(mods2 & _MOD_WIN)
        mods3, _ = parse_hotkey("super+alt+a")
        self.assertTrue(mods3 & _MOD_WIN)

    def test_function_keys(self):
        _, vk = parse_hotkey("ctrl+f8")
        self.assertEqual(vk, 0x77)
        _, vk12 = parse_hotkey("alt+f12")
        self.assertEqual(vk12, 0x7B)

    def test_digit_main_key(self):
        _, vk = parse_hotkey("ctrl+alt+1")
        self.assertEqual(vk, ord("1"))

    def test_named_special_keys(self):
        for name, code in (("enter", 0x0D), ("esc", 0x1B), ("up", 0x26),
                           ("delete", 0x2E), ("tab", 0x09)):
            _, vk = parse_hotkey(f"ctrl+{name}")
            self.assertEqual(vk, code, name)

    # ── 拒绝的非法输入 ──────────────────────────────────────────────
    def test_reject_no_modifier(self):
        # 无修饰键太容易误触发，必须拒绝
        with self.assertRaises(HotkeyError):
            parse_hotkey("v")
        with self.assertRaises(HotkeyError):
            parse_hotkey("f8")

    def test_reject_empty(self):
        for bad in ("", "   ", "+", "++"):
            with self.assertRaises(HotkeyError):
                parse_hotkey(bad)

    def test_reject_no_main_key(self):
        with self.assertRaises(HotkeyError):
            parse_hotkey("ctrl+alt")

    def test_reject_two_main_keys(self):
        with self.assertRaises(HotkeyError):
            parse_hotkey("ctrl+a+b")

    def test_reject_unknown_key(self):
        with self.assertRaises(HotkeyError):
            parse_hotkey("ctrl+nope")

    def test_reject_non_string(self):
        with self.assertRaises(HotkeyError):
            parse_hotkey(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
