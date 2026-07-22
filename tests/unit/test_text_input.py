"""executor.text_input 单元测试（听写文本注入，CLAUDE.md §6 修订 56）。

关键：monkeypatch 掉真实 SendInput，测试绝不真的往当前窗口注入按键。
通过替换 ``executor.text_input._get_send_input`` 返回一个 MagicMock，统计
调用次数/参数；SendInput 约定返回"实际注入的事件数"，每字符 2 个事件
（keydown + keyup），故 MagicMock 的 return_value 设为 2。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from executor import text_input
from executor.text_input import TextInputError, inject_text


def _patch_send_input(monkeypatch, *, return_value=2, side_effect=None):
    """把 _get_send_input 换成返回受控 MagicMock 的桩，并确保平台判定为 win32。"""
    mock_send = MagicMock(name="SendInput")
    if side_effect is not None:
        mock_send.side_effect = side_effect
    else:
        mock_send.return_value = return_value
    monkeypatch.setattr(text_input, "_get_send_input", lambda: mock_send)
    monkeypatch.setattr(text_input.sys, "platform", "win32")
    return mock_send


def test_inject_plain_text_counts_and_calls(monkeypatch):
    mock_send = _patch_send_input(monkeypatch)

    n = inject_text("abc")

    assert n == 3
    # 每个可见字符调用一次 SendInput
    assert mock_send.call_count == 3


def test_inject_chinese_text(monkeypatch):
    mock_send = _patch_send_input(monkeypatch)

    n = inject_text("你好世界")

    assert n == 4
    assert mock_send.call_count == 4


def test_newline_tab_control_chars_skipped(monkeypatch):
    mock_send = _patch_send_input(monkeypatch)

    # "abc\ndef" 只注入 6 个可见字符，换行不注入
    n = inject_text("abc\ndef")

    assert n == 6
    assert mock_send.call_count == 6


def test_various_control_chars_skipped(monkeypatch):
    mock_send = _patch_send_input(monkeypatch)

    # 制表、回车、换行、垂直制表、空字符全部跳过，只留 "ab"
    n = inject_text("a\tb\r\n\v\x00")

    assert n == 2
    assert mock_send.call_count == 2


def test_only_control_chars_returns_zero(monkeypatch):
    mock_send = _patch_send_input(monkeypatch)

    n = inject_text("\n\r\t")

    assert n == 0
    mock_send.assert_not_called()


def test_empty_string_returns_zero_no_call(monkeypatch):
    mock_send = _patch_send_input(monkeypatch)

    n = inject_text("")

    assert n == 0
    mock_send.assert_not_called()


def test_bmp_supplementary_chars_skipped(monkeypatch):
    mock_send = _patch_send_input(monkeypatch)

    # emoji 在 BMP 外（码点 > 0xFFFF），MVP 跳过；"a" + emoji + "b" 只注入 2 个
    n = inject_text("a\U0001F600b")

    assert n == 2
    assert mock_send.call_count == 2


def test_non_windows_raises(monkeypatch):
    monkeypatch.setattr(text_input.sys, "platform", "linux")

    with pytest.raises(TextInputError):
        inject_text("abc")


def test_send_input_returns_failure_raises(monkeypatch):
    # SendInput 返回 0（少于期望的 2 个事件）视为失败
    _patch_send_input(monkeypatch, return_value=0)

    with pytest.raises(TextInputError):
        inject_text("a")


def test_send_input_raises_wrapped(monkeypatch):
    _patch_send_input(monkeypatch, side_effect=OSError("boom"))

    with pytest.raises(TextInputError):
        inject_text("a")


def test_should_inject_boundaries():
    assert text_input._should_inject("a") is True
    assert text_input._should_inject(" ") is True          # 0x20 边界，注入
    assert text_input._should_inject("\x1f") is False       # 0x1f 控制字符
    assert text_input._should_inject("\n") is False
    assert text_input._should_inject("￿") is True      # BMP 上界，注入
