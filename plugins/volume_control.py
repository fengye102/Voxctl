"""系统音量控制插件（迭代 B）。

通过 Windows 虚拟音量键（ctypes + keybd_event）调节音量/静音，
不引入 pycaw/comtypes，不调用 subprocess/os.system。

非 Windows 平台返回失败说明。
"""

from __future__ import annotations

import sys
from typing import Callable

from executor import ExecutionResult
from plugins.base import PluginSpec

# Virtual-Key codes (Winuser.h)
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF

_KEYEVENTF_KEYUP = 0x0002

_DEFAULT_STEPS = 4
_MIN_STEPS = 1
_MAX_STEPS = 15

_VALID_ACTIONS = frozenset({"mute", "unmute", "toggle_mute", "up", "down"})


def _clamp_steps(raw) -> int:
    """将 steps 夹紧到 [1, 15]；非法/缺省时用默认 4。"""
    if raw is None:
        return _DEFAULT_STEPS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_STEPS
    if n < _MIN_STEPS:
        return _MIN_STEPS
    if n > _MAX_STEPS:
        return _MAX_STEPS
    return n


def _tap_key(vk: int, keybd_event: Callable) -> None:
    """按下并抬起一个虚拟键。"""
    keybd_event(vk, 0, 0, 0)
    keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def _get_keybd_event() -> Callable:
    """懒获取 user32.keybd_event；测试可 monkeypatch 此函数返回值路径。"""
    import ctypes

    return ctypes.windll.user32.keybd_event


def handle_volume_control(params: dict, confirmed: bool) -> ExecutionResult:
    """处理音量/静音意图。

    params:
      action: mute | unmute | toggle_mute | up | down
      steps:  up/down 时按键次数，默认 4，夹紧到 1-15
    """
    del confirmed  # 本插件非 dangerous，不使用二次确认

    action = params.get("action")
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        return ExecutionResult(
            success=False,
            action_type="volume_control",
            error=f"无效的音量操作: {action!r}，可选 mute/unmute/toggle_mute/up/down",
            payload={"message": "音量操作无效，请再说一次"},
        )

    if sys.platform != "win32":
        return ExecutionResult(
            success=False,
            action_type="volume_control",
            error="系统音量控制仅支持 Windows",
            payload={"message": "当前系统不支持音量控制，仅 Windows 可用"},
        )

    try:
        keybd_event = _get_keybd_event()
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="volume_control",
            error=f"无法调用键盘音量键接口: {exc}",
            payload={"message": "音量控制失败"},
        )

    try:
        if action in ("mute", "unmute", "toggle_mute"):
            # Windows 只有 mute toggle 键，无独立 mute/unmute API（无额外依赖时）
            _tap_key(_VK_VOLUME_MUTE, keybd_event)
            if action == "mute":
                message = "已静音"
            elif action == "unmute":
                message = "已取消静音"
            else:
                message = "已切换静音"
            return ExecutionResult(
                success=True,
                action_type="volume_control",
                payload={
                    "message": message,
                    "action": action,
                },
            )

        steps = _clamp_steps(params.get("steps"))
        vk = _VK_VOLUME_UP if action == "up" else _VK_VOLUME_DOWN
        for _ in range(steps):
            _tap_key(vk, keybd_event)
        message = "已增大音量" if action == "up" else "已减小音量"
        return ExecutionResult(
            success=True,
            action_type="volume_control",
            payload={
                "message": message,
                "action": action,
                "steps": steps,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="volume_control",
            error=f"音量按键发送失败: {exc}",
            payload={"message": "音量控制失败"},
        )


PLUGIN_SPEC = PluginSpec(
    intent_name="volume_control",
    description=(
        "控制系统音量或静音。用户说「静音」「关闭声音」「取消静音」「打开声音」、"
        "「音量加大」「大声一点」「音量减小」「小声一点」等时选此工具。"
        "不要用于关闭应用、退出助手、或其它与音量无关的操作。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["mute", "unmute", "toggle_mute", "up", "down"],
                "description": (
                    "音量操作类型："
                    "mute=静音/关闭声音；unmute=取消静音/打开声音；"
                    "toggle_mute=切换静音；up=音量加大；down=音量减小"
                ),
            },
            "steps": {
                "type": "integer",
                "description": "up/down 时按键次数，默认 4，范围 1-15",
            },
        },
        "required": ["action"],
    },
    handler=handle_volume_control,
    dangerous=False,
)
