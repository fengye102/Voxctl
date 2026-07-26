"""媒体播放插件单元测试：mock 键盘事件，不依赖真实媒体播放器。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

from plugins.media_control import (
    PLUGIN_SPEC,
    _KEYEVENTF_KEYUP,
    _VK_MEDIA_NEXT_TRACK,
    _VK_MEDIA_PLAY_PAUSE,
    _VK_MEDIA_PREV_TRACK,
    _VK_MEDIA_STOP,
    handle_media_control,
)
from plugins import PluginRegistry
from plugins.base import PluginSpec


# ── helpers ───────────────────────────────────────────────────────────

def _mock_keybd(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(
        "plugins.media_control._get_keybd_event",
        lambda: mock,
    )
    return mock


def _tap_calls(vk: int):
    """一对 key-down + key-up call。"""
    return [
        call(vk, 0, 0, 0),
        call(vk, 0, _KEYEVENTF_KEYUP, 0),
    ]


# ── PLUGIN_SPEC ───────────────────────────────────────────────────────

class TestPluginSpecMeta:
    def test_spec_shape(self):
        assert isinstance(PLUGIN_SPEC, PluginSpec)
        assert PLUGIN_SPEC.intent_name == "media_control"
        assert PLUGIN_SPEC.dangerous is False
        assert PLUGIN_SPEC.parameters_schema["type"] == "object"
        assert "action" in PLUGIN_SPEC.parameters_schema["properties"]
        assert PLUGIN_SPEC.handler is handle_media_control
        assert "媒体" in PLUGIN_SPEC.description or "播放" in PLUGIN_SPEC.description
        assert "关闭应用" in PLUGIN_SPEC.description or "退出" in PLUGIN_SPEC.description

    def test_loads_via_registry(self):
        reg = PluginRegistry.load_from_directory(
            Path(__file__).resolve().parents[2] / "plugins"
        )
        assert reg.has("media_control")
        spec = reg.get("media_control")
        assert spec is not None
        assert spec.dangerous is False


# ── handler: actions ──────────────────────────────────────────────────

class TestMediaHandler:
    def test_play_pause(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_media_control({"action": "play_pause"}, confirmed=False)
        assert result.success is True
        assert result.action_type == "media_control"
        assert result.payload["message"] == "已切换播放暂停"
        assert result.payload["action"] == "play_pause"
        assert mock.call_args_list == _tap_calls(_VK_MEDIA_PLAY_PAUSE)

    def test_stop(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_media_control({"action": "stop"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已停止播放"
        assert result.payload["action"] == "stop"
        assert mock.call_args_list == _tap_calls(_VK_MEDIA_STOP)

    def test_next(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_media_control({"action": "next"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已切到下一首"
        assert result.payload["action"] == "next"
        assert mock.call_args_list == _tap_calls(_VK_MEDIA_NEXT_TRACK)

    def test_prev(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_media_control({"action": "prev"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已切到上一首"
        assert result.payload["action"] == "prev"
        assert mock.call_args_list == _tap_calls(_VK_MEDIA_PREV_TRACK)

    def test_invalid_action(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_media_control({"action": "explode"}, confirmed=False)
        assert result.success is False
        assert result.action_type == "media_control"
        assert "无效" in (result.error or "")
        assert "message" in result.payload
        mock.assert_not_called()

    def test_missing_action(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_media_control({}, confirmed=False)
        assert result.success is False
        mock.assert_not_called()

    def test_non_windows(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "linux")
        mock = _mock_keybd(monkeypatch)
        result = handle_media_control({"action": "play_pause"}, confirmed=False)
        assert result.success is False
        assert "Windows" in (result.error or "")
        assert "message" in result.payload
        mock.assert_not_called()

    def test_keybd_raises(self, monkeypatch):
        monkeypatch.setattr("plugins.media_control.sys.platform", "win32")

        def boom():
            raise OSError("no user32")

        monkeypatch.setattr("plugins.media_control._get_keybd_event", boom)
        result = handle_media_control({"action": "play_pause"}, confirmed=False)
        assert result.success is False
        assert "媒体" in result.payload.get("message", "")
