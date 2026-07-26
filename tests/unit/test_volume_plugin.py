"""系统音量插件单元测试：mock 键盘事件，不依赖真实声卡。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

from plugins.volume_control import (
    PLUGIN_SPEC,
    _KEYEVENTF_KEYUP,
    _VK_VOLUME_DOWN,
    _VK_VOLUME_MUTE,
    _VK_VOLUME_UP,
    _clamp_steps,
    handle_volume_control,
)
from plugins import PluginRegistry
from plugins.base import PluginSpec


# ── helpers ───────────────────────────────────────────────────────────

def _mock_keybd(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(
        "plugins.volume_control._get_keybd_event",
        lambda: mock,
    )
    return mock


def _tap_calls(vk: int):
    """一对 key-down + key-up call。"""
    return [
        call(vk, 0, 0, 0),
        call(vk, 0, _KEYEVENTF_KEYUP, 0),
    ]


# ── clamp steps ───────────────────────────────────────────────────────

class TestClampSteps:
    def test_default_none(self):
        assert _clamp_steps(None) == 4

    def test_default_invalid(self):
        assert _clamp_steps("x") == 4
        assert _clamp_steps({}) == 4

    def test_clamp_low(self):
        assert _clamp_steps(0) == 1
        assert _clamp_steps(-5) == 1

    def test_clamp_high(self):
        assert _clamp_steps(16) == 15
        assert _clamp_steps(100) == 15

    def test_in_range(self):
        assert _clamp_steps(1) == 1
        assert _clamp_steps(8) == 8
        assert _clamp_steps(15) == 15


# ── PLUGIN_SPEC ───────────────────────────────────────────────────────

class TestPluginSpecMeta:
    def test_spec_shape(self):
        assert isinstance(PLUGIN_SPEC, PluginSpec)
        assert PLUGIN_SPEC.intent_name == "volume_control"
        assert PLUGIN_SPEC.dangerous is False
        assert PLUGIN_SPEC.parameters_schema["type"] == "object"
        assert "action" in PLUGIN_SPEC.parameters_schema["properties"]
        assert PLUGIN_SPEC.handler is handle_volume_control
        assert "音量" in PLUGIN_SPEC.description or "静音" in PLUGIN_SPEC.description
        assert "关闭应用" in PLUGIN_SPEC.description or "退出" in PLUGIN_SPEC.description

    def test_loads_via_registry(self):
        reg = PluginRegistry.load_from_directory(
            Path(__file__).resolve().parents[2] / "plugins"
        )
        assert reg.has("volume_control")
        spec = reg.get("volume_control")
        assert spec is not None
        assert spec.dangerous is False


# ── handler: actions ──────────────────────────────────────────────────

class TestVolumeHandler:
    def test_up_default_steps(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "up"}, confirmed=False)
        assert result.success is True
        assert result.action_type == "volume_control"
        assert result.payload["message"] == "已增大音量"
        assert result.payload["steps"] == 4
        # 4 taps × 2 events
        assert mock.call_count == 8
        expected = []
        for _ in range(4):
            expected.extend(_tap_calls(_VK_VOLUME_UP))
        assert mock.call_args_list == expected

    def test_down_custom_steps(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "down", "steps": 2}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已减小音量"
        assert result.payload["steps"] == 2
        assert mock.call_count == 4
        expected = []
        for _ in range(2):
            expected.extend(_tap_calls(_VK_VOLUME_DOWN))
        assert mock.call_args_list == expected

    def test_up_steps_clamped_high(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "up", "steps": 99}, confirmed=False)
        assert result.success is True
        assert result.payload["steps"] == 15
        assert mock.call_count == 30  # 15 * 2

    def test_down_steps_clamped_low(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "down", "steps": 0}, confirmed=False)
        assert result.success is True
        assert result.payload["steps"] == 1
        assert mock.call_count == 2

    def test_mute(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "mute"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已静音"
        assert mock.call_args_list == _tap_calls(_VK_VOLUME_MUTE)

    def test_unmute(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "unmute"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已取消静音"
        assert mock.call_args_list == _tap_calls(_VK_VOLUME_MUTE)

    def test_toggle_mute(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "toggle_mute"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已切换静音"
        assert mock.call_args_list == _tap_calls(_VK_VOLUME_MUTE)

    def test_invalid_action(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "explode"}, confirmed=False)
        assert result.success is False
        assert result.action_type == "volume_control"
        assert "无效" in (result.error or "")
        assert "message" in result.payload
        mock.assert_not_called()

    def test_missing_action(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({}, confirmed=False)
        assert result.success is False
        mock.assert_not_called()

    def test_non_windows(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "linux")
        mock = _mock_keybd(monkeypatch)
        result = handle_volume_control({"action": "up"}, confirmed=False)
        assert result.success is False
        assert "Windows" in (result.error or "")
        assert "message" in result.payload
        mock.assert_not_called()

    def test_keybd_raises(self, monkeypatch):
        monkeypatch.setattr("plugins.volume_control.sys.platform", "win32")

        def boom():
            raise OSError("no user32")

        monkeypatch.setattr("plugins.volume_control._get_keybd_event", boom)
        result = handle_volume_control({"action": "mute"}, confirmed=False)
        assert result.success is False
        assert "音量" in result.payload.get("message", "")
