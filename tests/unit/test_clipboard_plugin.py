"""剪贴板读取插件单元测试：monkeypatch 读取函数，不触碰真实剪贴板。"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.clipboard_read import (
    PLUGIN_SPEC,
    _CF_UNICODETEXT,
    _EMPTY_MESSAGE,
    _MAX_CHARS,
    _TRUNCATE_SUFFIX,
    _truncate,
    handle_clipboard_read,
)
from plugins import PluginRegistry
from plugins.base import PluginSpec


# ── helpers ───────────────────────────────────────────────────────────

def _patch_read(monkeypatch, value):
    """monkeypatch _read_clipboard_text 返回固定值。"""
    monkeypatch.setattr(
        "plugins.clipboard_read._read_clipboard_text",
        lambda: value,
    )


def _win(monkeypatch):
    monkeypatch.setattr("plugins.clipboard_read.sys.platform", "win32")


# ── _truncate ─────────────────────────────────────────────────────────

class TestTruncate:
    def test_short_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_exactly_max_unchanged(self):
        s = "字" * _MAX_CHARS
        assert _truncate(s) == s

    def test_over_max_truncated(self):
        s = "字" * (_MAX_CHARS + 50)
        out = _truncate(s)
        assert out == "字" * _MAX_CHARS + _TRUNCATE_SUFFIX
        assert out.startswith("字" * _MAX_CHARS)
        assert out.endswith(_TRUNCATE_SUFFIX)


# ── PLUGIN_SPEC ───────────────────────────────────────────────────────

class TestPluginSpecMeta:
    def test_spec_shape(self):
        assert isinstance(PLUGIN_SPEC, PluginSpec)
        assert PLUGIN_SPEC.intent_name == "clipboard_read"
        assert PLUGIN_SPEC.dangerous is False
        assert PLUGIN_SPEC.handler is handle_clipboard_read
        assert PLUGIN_SPEC.parameters_schema["type"] == "object"
        assert PLUGIN_SPEC.parameters_schema["required"] == []
        # description 要说明"只读"，不误导 LLM 以为能打开链接
        assert "剪贴板" in PLUGIN_SPEC.description
        assert "绝不" in PLUGIN_SPEC.description or "只是" in PLUGIN_SPEC.description

    def test_cf_unicodetext_const(self):
        assert _CF_UNICODETEXT == 13

    def test_loads_via_registry(self):
        reg = PluginRegistry.load_from_directory(
            Path(__file__).resolve().parents[2] / "plugins"
        )
        assert reg.has("clipboard_read")
        spec = reg.get("clipboard_read")
        assert spec is not None
        assert spec.dangerous is False
        # 与已有插件（volume_control）共存加载，不冲突
        assert reg.has("volume_control")


# ── handler ───────────────────────────────────────────────────────────

class TestClipboardHandler:
    def test_normal_read(self, monkeypatch):
        _win(monkeypatch)
        _patch_read(monkeypatch, "hello world")
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is True
        assert result.action_type == "clipboard_read"
        assert "hello world" in result.payload["message"]
        assert result.payload["text"] == "hello world"
        assert result.payload["truncated"] is False
        assert result.payload["length"] == len("hello world")

    def test_chinese_text(self, monkeypatch):
        _win(monkeypatch)
        _patch_read(monkeypatch, "你好，世界")
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is True
        assert "你好，世界" in result.payload["message"]

    def test_empty_string(self, monkeypatch):
        _win(monkeypatch)
        _patch_read(monkeypatch, "")
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == _EMPTY_MESSAGE
        assert result.payload["empty"] is True

    def test_none_no_text_format(self, monkeypatch):
        _win(monkeypatch)
        _patch_read(monkeypatch, None)
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == _EMPTY_MESSAGE
        assert result.payload["empty"] is True

    def test_whitespace_only_treated_empty(self, monkeypatch):
        _win(monkeypatch)
        _patch_read(monkeypatch, "   \n\t  ")
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == _EMPTY_MESSAGE

    def test_long_text_truncated(self, monkeypatch):
        _win(monkeypatch)
        long = "A" * 500
        _patch_read(monkeypatch, long)
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is True
        assert result.payload["truncated"] is True
        assert result.payload["length"] == 500
        assert result.payload["text"] == "A" * _MAX_CHARS + _TRUNCATE_SUFFIX
        assert _TRUNCATE_SUFFIX in result.payload["message"]

    def test_ignores_params(self, monkeypatch):
        # 只读插件不理会传入参数，绝不因参数执行任何内容
        _win(monkeypatch)
        _patch_read(monkeypatch, "https://example.com")
        result = handle_clipboard_read(
            {"open": True, "url": "https://example.com"}, confirmed=True
        )
        assert result.success is True
        # 仅朗读，text 原样返回，未做任何打开/执行
        assert result.payload["text"] == "https://example.com"

    def test_non_windows(self, monkeypatch):
        monkeypatch.setattr("plugins.clipboard_read.sys.platform", "linux")
        called = {"n": 0}

        def _should_not_call():
            called["n"] += 1
            return "x"

        monkeypatch.setattr(
            "plugins.clipboard_read._read_clipboard_text", _should_not_call
        )
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is False
        assert "Windows" in (result.error or "")
        assert "message" in result.payload
        assert called["n"] == 0  # 非 Windows 不应触碰剪贴板

    def test_read_raises(self, monkeypatch):
        _win(monkeypatch)

        def boom():
            raise OSError("clipboard busy")

        monkeypatch.setattr(
            "plugins.clipboard_read._read_clipboard_text", boom
        )
        result = handle_clipboard_read({}, confirmed=False)
        assert result.success is False
        assert "读取剪贴板失败" in (result.payload.get("message") or "")
        assert result.action_type == "clipboard_read"


# ── dispatch via registry（走真实插件调度链路）─────────────────────────

class TestDispatchThroughRegistry:
    def test_dispatch_reads_via_handler(self, monkeypatch):
        # 用已导入的 PLUGIN_SPEC 注册进新 registry，保证 handler 与被 patch 的
        # 模块是同一个对象（load_from_directory 会重新 exec 出一个副本模块，
        # patch 落不到那个副本上）。这里验证的是 PluginRegistry.dispatch 调度链路。
        _win(monkeypatch)
        _patch_read(monkeypatch, "clipboard payload")
        reg = PluginRegistry()
        reg.register(PLUGIN_SPEC)
        result = reg.dispatch("clipboard_read", {}, confirmed=False)
        assert result.success is True
        assert "clipboard payload" in result.payload["message"]
