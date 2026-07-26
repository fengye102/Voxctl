"""本地提醒插件测试（修订 58）。"""

from pathlib import Path

import pytest

from plugins import PluginRegistry
from plugins.base import PluginSpec
from plugins.reminder import PLUGIN_SPEC, handle_set_reminder


def test_valid_reminder_request():
    result = handle_set_reminder(
        {"delay_seconds": 300, "message": " 开会 "}, confirmed=False
    )
    assert result.success is True
    assert result.payload["message"] == "好的，5分钟后提醒你：开会"
    assert result.payload["reminder"] == {
        "delay_seconds": 300,
        "message": "开会",
    }


@pytest.mark.parametrize("delay", [None, True, 0, 86401, 1.5])
def test_invalid_delay_is_rejected(delay):
    result = handle_set_reminder(
        {"delay_seconds": delay, "message": "休息"}, confirmed=False
    )
    assert result.success is False
    assert result.payload["message"]


@pytest.mark.parametrize("message", [None, "", "   ", "x" * 201])
def test_invalid_message_is_rejected(message):
    result = handle_set_reminder(
        {"delay_seconds": 60, "message": message}, confirmed=False
    )
    assert result.success is False
    assert result.payload["message"]


def test_plugin_spec_and_registry():
    assert isinstance(PLUGIN_SPEC, PluginSpec)
    assert PLUGIN_SPEC.intent_name == "set_reminder"
    assert PLUGIN_SPEC.dangerous is False

    registry = PluginRegistry.load_from_directory(
        Path(__file__).resolve().parents[2] / "plugins"
    )
    assert registry.has("set_reminder")

