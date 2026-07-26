"""屏幕亮度插件测试（修订 63，WMI 全 mock）。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.brightness_control as brightness
from plugins import PluginRegistry
from plugins.base import PluginSpec
from plugins.brightness_control import PLUGIN_SPEC, handle_brightness_control


class Setter:
    def __init__(self, name):
        self.InstanceName = name
        self.calls = []

    def WmiSetBrightness(self, **kwargs):
        self.calls.append(kwargs)


def _session(monkeypatch, levels=(52,), writable=True):
    readings = [
        SimpleNamespace(InstanceName=f"display-{index}", CurrentBrightness=level)
        for index, level in enumerate(levels)
    ]
    setters = [Setter(item.InstanceName) for item in readings] if writable else []
    monkeypatch.setattr(
        brightness,
        "_run_wmi",
        lambda action, value, step: brightness._apply_brightness(
            readings, setters, action, value, step
        ),
    )
    return setters


def test_query(monkeypatch):
    setters = _session(monkeypatch)
    result = handle_brightness_control({"action": "query"}, False)
    assert result.success is True
    assert result.payload == {"message": "当前屏幕亮度为52%", "levels": [52]}
    assert setters[0].calls == []


def test_query_multiple_levels(monkeypatch):
    _session(monkeypatch, levels=(30, 70))
    result = handle_brightness_control({"action": "query"}, False)
    assert result.payload["message"] == "当前屏幕亮度为30%到70%"


def test_set(monkeypatch):
    setters = _session(monkeypatch)
    result = handle_brightness_control({"action": "set", "value": 65}, False)
    assert result.success is True
    assert result.payload["message"] == "已将屏幕亮度设为65%"
    assert setters[0].calls == [{"Brightness": 65, "Timeout": 1}]


@pytest.mark.parametrize(
    ("action", "current", "step", "target"),
    [
        ("up", 52, None, 62),
        ("down", 52, None, 42),
        ("up", 95, 20, 100),
        ("down", 5, 20, 0),
    ],
)
def test_relative_adjustment(monkeypatch, action, current, step, target):
    setters = _session(monkeypatch, levels=(current,))
    params = {"action": action}
    if step is not None:
        params["step"] = step
    result = handle_brightness_control(params, False)
    assert result.success is True
    assert result.payload["levels"] == [target]
    assert setters[0].calls == [{"Brightness": target, "Timeout": 1}]


@pytest.mark.parametrize("value", [None, -1, 101, 1.5, True, "50"])
def test_invalid_set_value_is_rejected(monkeypatch, value):
    called = False

    def session():
        nonlocal called
        called = True

    monkeypatch.setattr(brightness, "_run_wmi", session)
    result = handle_brightness_control({"action": "set", "value": value}, False)
    assert result.success is False
    assert called is False


@pytest.mark.parametrize("step", [0, 101, 1.5, True, "10"])
def test_invalid_step_is_rejected(step):
    result = handle_brightness_control({"action": "up", "step": step}, False)
    assert result.success is False


@pytest.mark.parametrize("params", [{}, {"action": "auto"}, None, []])
def test_invalid_action_is_rejected(params):
    result = handle_brightness_control(params, False)
    assert result.success is False


def test_no_supported_display(monkeypatch):
    monkeypatch.setattr(
        brightness,
        "_run_wmi",
        lambda action, value, step: brightness._apply_brightness(
            [], [], action, value, step
        ),
    )
    result = handle_brightness_control({"action": "query"}, False)
    assert result.success is False
    assert result.payload["message"] == "当前显示器不支持系统亮度控制"


def test_non_windows_is_rejected(monkeypatch):
    monkeypatch.setattr(brightness.sys, "platform", "linux")
    result = handle_brightness_control({"action": "query"}, False)
    assert result.success is False
    assert result.payload["message"] == "屏幕亮度控制仅支持 Windows"


def test_no_matching_setter(monkeypatch):
    _session(monkeypatch, writable=False)
    result = handle_brightness_control({"action": "up"}, False)
    assert result.success is False
    assert result.payload["message"] == "当前显示器没有可用的亮度写入接口"


def test_wmi_failure(monkeypatch):
    def fail():
        raise brightness.BrightnessError("WMI unavailable")

    monkeypatch.setattr(brightness, "_run_wmi", lambda action, value, step: fail())
    result = handle_brightness_control({"action": "query"}, False)
    assert result.success is False
    assert result.payload["message"] == "WMI unavailable"


def test_setter_failure(monkeypatch):
    setters = _session(monkeypatch)

    def fail(**kwargs):
        raise OSError("write failed")

    setters[0].WmiSetBrightness = fail
    result = handle_brightness_control({"action": "down"}, False)
    assert result.success is False
    assert "write failed" in result.payload["message"]


def test_plugin_spec_and_registry():
    assert isinstance(PLUGIN_SPEC, PluginSpec)
    assert PLUGIN_SPEC.intent_name == "brightness_control"
    assert PLUGIN_SPEC.dangerous is False
    registry = PluginRegistry.load_from_directory(
        Path(__file__).resolve().parents[2] / "plugins"
    )
    assert registry.has("brightness_control")
