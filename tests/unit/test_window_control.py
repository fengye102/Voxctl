"""窗口控制插件单元测试：mock 键盘事件，不依赖真实桌面。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

from plugins.window_control import (
    PLUGIN_SPEC,
    _KEYEVENTF_KEYUP,
    _VK_D,
    _VK_LWIN,
    _VK_M,
    _VK_MENU,
    _VK_SHIFT,
    _VK_TAB,
    handle_window_control,
)
from plugins import PluginRegistry
from plugins.base import PluginSpec


# ── helpers ───────────────────────────────────────────────────────────

def _mock_keybd(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(
        "plugins.window_control._get_keybd_event",
        lambda: mock,
    )
    return mock


def _combo_calls(keys):
    """组合键的完整事件序列：先按下（正序），再抬起（逆序）。"""
    expected = [call(vk, 0, 0, 0) for vk in keys]
    expected += [call(vk, 0, _KEYEVENTF_KEYUP, 0) for vk in reversed(keys)]
    return expected


# ── PLUGIN_SPEC ───────────────────────────────────────────────────────

class TestPluginSpecMeta:
    def test_spec_shape(self):
        assert isinstance(PLUGIN_SPEC, PluginSpec)
        assert PLUGIN_SPEC.intent_name == "window_control"
        assert PLUGIN_SPEC.dangerous is False
        assert PLUGIN_SPEC.parameters_schema["type"] == "object"
        assert "action" in PLUGIN_SPEC.parameters_schema["properties"]
        assert PLUGIN_SPEC.handler is handle_window_control
        assert "窗口" in PLUGIN_SPEC.description or "桌面" in PLUGIN_SPEC.description
        assert "关闭应用" in PLUGIN_SPEC.description or "退出" in PLUGIN_SPEC.description

    def test_loads_via_registry(self):
        reg = PluginRegistry.load_from_directory(
            Path(__file__).resolve().parents[2] / "plugins"
        )
        assert reg.has("window_control")
        spec = reg.get("window_control")
        assert spec is not None
        assert spec.dangerous is False


# ── handler: actions ──────────────────────────────────────────────────

class TestWindowHandler:
    def test_show_desktop(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_window_control({"action": "show_desktop"}, confirmed=False)
        assert result.success is True
        assert result.action_type == "window_control"
        assert result.payload["message"] == "已显示桌面"
        assert result.payload["action"] == "show_desktop"
        assert mock.call_args_list == _combo_calls([_VK_LWIN, _VK_D])

    def test_minimize_all(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_window_control({"action": "minimize_all"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已最小化所有窗口"
        assert mock.call_args_list == _combo_calls([_VK_LWIN, _VK_M])

    def test_restore_windows(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_window_control({"action": "restore_windows"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已恢复窗口"
        # 三键组合：down 正序 Win,Shift,M；up 逆序 M,Shift,Win
        assert mock.call_args_list == _combo_calls([_VK_LWIN, _VK_SHIFT, _VK_M])
        # 显式验证抬起为逆序
        up_calls = mock.call_args_list[3:]
        assert up_calls == [
            call(_VK_M, 0, _KEYEVENTF_KEYUP, 0),
            call(_VK_SHIFT, 0, _KEYEVENTF_KEYUP, 0),
            call(_VK_LWIN, 0, _KEYEVENTF_KEYUP, 0),
        ]

    def test_switch_window(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_window_control({"action": "switch_window"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已切换窗口"
        assert mock.call_args_list == _combo_calls([_VK_MENU, _VK_TAB])

    def test_invalid_action(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_window_control({"action": "explode"}, confirmed=False)
        assert result.success is False
        assert result.action_type == "window_control"
        assert "无效" in (result.error or "")
        assert "message" in result.payload
        mock.assert_not_called()

    def test_missing_action(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")
        mock = _mock_keybd(monkeypatch)
        result = handle_window_control({}, confirmed=False)
        assert result.success is False
        mock.assert_not_called()

    def test_non_windows(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "linux")
        mock = _mock_keybd(monkeypatch)
        result = handle_window_control({"action": "show_desktop"}, confirmed=False)
        assert result.success is False
        assert "Windows" in (result.error or "")
        assert "message" in result.payload
        mock.assert_not_called()

    def test_keybd_raises(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")

        def boom():
            raise OSError("no user32")

        monkeypatch.setattr("plugins.window_control._get_keybd_event", boom)
        result = handle_window_control({"action": "show_desktop"}, confirmed=False)
        assert result.success is False
        assert "窗口" in result.payload.get("message", "")

    def test_tap_combo_raises_mid_way(self, monkeypatch):
        monkeypatch.setattr("plugins.window_control.sys.platform", "win32")
        mock = MagicMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(
            "plugins.window_control._get_keybd_event",
            lambda: mock,
        )
        result = handle_window_control({"action": "switch_window"}, confirmed=False)
        assert result.success is False
        assert "message" in result.payload
