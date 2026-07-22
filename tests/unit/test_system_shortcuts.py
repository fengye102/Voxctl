"""系统快捷操作插件单元测试：mock user32，不触发真实锁屏/截图。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from plugins.system_shortcuts import (
    PLUGIN_SPEC,
    _KEYEVENTF_KEYUP,
    _VK_LWIN,
    _VK_S,
    _VK_SHIFT,
    handle_system_shortcuts,
)
from plugins import PluginRegistry
from plugins.base import PluginSpec


# ── helpers ───────────────────────────────────────────────────────────

def _mock_user32(monkeypatch):
    """monkeypatch _get_user32，返回带 LockWorkStation / keybd_event 的 MagicMock。"""
    mock = MagicMock()
    monkeypatch.setattr(
        "plugins.system_shortcuts._get_user32",
        lambda: mock,
    )
    return mock


# ── PLUGIN_SPEC ───────────────────────────────────────────────────────

class TestPluginSpecMeta:
    def test_spec_shape(self):
        assert isinstance(PLUGIN_SPEC, PluginSpec)
        assert PLUGIN_SPEC.intent_name == "system_shortcuts"
        assert PLUGIN_SPEC.dangerous is False
        assert PLUGIN_SPEC.parameters_schema["type"] == "object"
        assert "action" in PLUGIN_SPEC.parameters_schema["properties"]
        assert PLUGIN_SPEC.handler is handle_system_shortcuts
        assert "锁屏" in PLUGIN_SPEC.description or "截图" in PLUGIN_SPEC.description
        assert "关闭应用" in PLUGIN_SPEC.description or "退出" in PLUGIN_SPEC.description

    def test_loads_via_registry(self):
        reg = PluginRegistry.load_from_directory(
            Path(__file__).resolve().parents[2] / "plugins"
        )
        assert reg.has("system_shortcuts")
        spec = reg.get("system_shortcuts")
        assert spec is not None
        assert spec.dangerous is False


# ── handler: actions ──────────────────────────────────────────────────

class TestSystemShortcutsHandler:
    def test_lock(self, monkeypatch):
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "win32")
        mock = _mock_user32(monkeypatch)
        result = handle_system_shortcuts({"action": "lock"}, confirmed=False)
        assert result.success is True
        assert result.action_type == "system_shortcuts"
        assert result.payload["message"] == "已锁定电脑"
        assert result.payload["action"] == "lock"
        mock.LockWorkStation.assert_called_once_with()
        # 锁屏不发按键
        mock.keybd_event.assert_not_called()

    def test_screenshot_combo_order(self, monkeypatch):
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "win32")
        mock = _mock_user32(monkeypatch)
        result = handle_system_shortcuts({"action": "screenshot"}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "已打开截图工具"
        assert result.payload["action"] == "screenshot"
        # down 顺序 [LWIN, SHIFT, S]，up 逆序 [S, SHIFT, LWIN]
        expected = [
            call(_VK_LWIN, 0, 0, 0),
            call(_VK_SHIFT, 0, 0, 0),
            call(_VK_S, 0, 0, 0),
            call(_VK_S, 0, _KEYEVENTF_KEYUP, 0),
            call(_VK_SHIFT, 0, _KEYEVENTF_KEYUP, 0),
            call(_VK_LWIN, 0, _KEYEVENTF_KEYUP, 0),
        ]
        assert mock.keybd_event.call_args_list == expected
        # 截图不锁屏
        mock.LockWorkStation.assert_not_called()

    def test_invalid_action(self, monkeypatch):
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "win32")
        mock = _mock_user32(monkeypatch)
        result = handle_system_shortcuts({"action": "explode"}, confirmed=False)
        assert result.success is False
        assert result.action_type == "system_shortcuts"
        assert "无效" in (result.error or "")
        assert "message" in result.payload
        mock.LockWorkStation.assert_not_called()
        mock.keybd_event.assert_not_called()

    def test_missing_action(self, monkeypatch):
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "win32")
        mock = _mock_user32(monkeypatch)
        result = handle_system_shortcuts({}, confirmed=False)
        assert result.success is False
        mock.LockWorkStation.assert_not_called()
        mock.keybd_event.assert_not_called()

    def test_non_windows(self, monkeypatch):
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "linux")
        mock = _mock_user32(monkeypatch)
        result = handle_system_shortcuts({"action": "lock"}, confirmed=False)
        assert result.success is False
        assert "Windows" in (result.error or "")
        assert "message" in result.payload
        mock.LockWorkStation.assert_not_called()
        mock.keybd_event.assert_not_called()

    def test_user32_raises(self, monkeypatch):
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "win32")

        def boom():
            raise OSError("no user32")

        monkeypatch.setattr("plugins.system_shortcuts._get_user32", boom)
        result = handle_system_shortcuts({"action": "lock"}, confirmed=False)
        assert result.success is False
        assert "message" in result.payload

    def test_lock_api_raises(self, monkeypatch):
        """底层 LockWorkStation 抛异常时兜底成失败结果。"""
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "win32")
        mock = _mock_user32(monkeypatch)
        mock.LockWorkStation.side_effect = OSError("boom")
        result = handle_system_shortcuts({"action": "lock"}, confirmed=False)
        assert result.success is False
        assert "失败" in result.payload.get("message", "")

    def test_screenshot_api_raises(self, monkeypatch):
        """底层 keybd_event 抛异常时兜底成失败结果。"""
        monkeypatch.setattr("plugins.system_shortcuts.sys.platform", "win32")
        mock = _mock_user32(monkeypatch)
        mock.keybd_event.side_effect = OSError("boom")
        result = handle_system_shortcuts({"action": "screenshot"}, confirmed=False)
        assert result.success is False
        assert "失败" in result.payload.get("message", "")
