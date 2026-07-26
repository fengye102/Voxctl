"""电池状态插件单元测试：monkeypatch _get_battery，不依赖真实硬件。"""

from __future__ import annotations

import types
from pathlib import Path

from plugins.power_status import (
    PLUGIN_SPEC,
    handle_power_status,
)
from plugins import PluginRegistry
from plugins.base import PluginSpec


# ── helpers ───────────────────────────────────────────────────────────

def _fake_battery(percent, plugged, secsleft=-1):
    return types.SimpleNamespace(
        percent=percent,
        power_plugged=plugged,
        secsleft=secsleft,
    )


def _patch_battery(monkeypatch, value):
    monkeypatch.setattr(
        "plugins.power_status._get_battery",
        lambda: value,
    )


# ── PLUGIN_SPEC ───────────────────────────────────────────────────────

class TestPluginSpecMeta:
    def test_spec_shape(self):
        assert isinstance(PLUGIN_SPEC, PluginSpec)
        assert PLUGIN_SPEC.intent_name == "power_status"
        assert PLUGIN_SPEC.dangerous is False
        assert PLUGIN_SPEC.parameters_schema["type"] == "object"
        assert PLUGIN_SPEC.parameters_schema["properties"] == {}
        assert PLUGIN_SPEC.parameters_schema["required"] == []
        assert PLUGIN_SPEC.handler is handle_power_status
        assert "电" in PLUGIN_SPEC.description

    def test_loads_via_registry(self):
        reg = PluginRegistry.load_from_directory(
            Path(__file__).resolve().parents[2] / "plugins"
        )
        assert reg.has("power_status")
        spec = reg.get("power_status")
        assert spec is not None
        assert spec.dangerous is False


# ── handler ───────────────────────────────────────────────────────────

class TestPowerStatusHandler:
    def test_charging(self, monkeypatch):
        _patch_battery(monkeypatch, _fake_battery(85.0, True))
        result = handle_power_status({}, confirmed=False)
        assert result.success is True
        assert result.action_type == "power_status"
        assert result.payload["message"] == "电量还有85%，正在充电"
        assert result.payload["has_battery"] is True
        assert result.payload["percent"] == 85
        assert result.payload["power_plugged"] is True

    def test_not_charging(self, monkeypatch):
        _patch_battery(monkeypatch, _fake_battery(42.0, False))
        result = handle_power_status({}, confirmed=False)
        assert result.success is True
        assert result.payload["message"] == "电量还有42%，未充电"
        assert result.payload["percent"] == 42
        assert result.payload["power_plugged"] is False

    def test_percent_rounds(self, monkeypatch):
        _patch_battery(monkeypatch, _fake_battery(66.7, False))
        result = handle_power_status({}, confirmed=False)
        assert result.success is True
        assert result.payload["percent"] == 67
        assert "67%" in result.payload["message"]

    def test_percent_rounds_down(self, monkeypatch):
        _patch_battery(monkeypatch, _fake_battery(50.2, True))
        result = handle_power_status({}, confirmed=False)
        assert result.payload["percent"] == 50
        assert result.payload["message"] == "电量还有50%，正在充电"

    def test_no_battery(self, monkeypatch):
        _patch_battery(monkeypatch, None)
        result = handle_power_status({}, confirmed=False)
        assert result.success is True
        assert result.action_type == "power_status"
        assert result.payload["has_battery"] is False
        assert "台式机" in result.payload["message"]
        assert "percent" not in result.payload

    def test_get_battery_raises(self, monkeypatch):
        def boom():
            raise OSError("no battery sensor")

        monkeypatch.setattr("plugins.power_status._get_battery", boom)
        result = handle_power_status({}, confirmed=False)
        assert result.success is False
        assert result.action_type == "power_status"
        assert "失败" in (result.error or "")
        assert "message" in result.payload

    def test_params_ignored(self, monkeypatch):
        # 传入无关 params 不应影响结果（handler 忽略 params）
        _patch_battery(monkeypatch, _fake_battery(30.0, False))
        result = handle_power_status({"garbage": 1}, confirmed=True)
        assert result.success is True
        assert result.payload["percent"] == 30
