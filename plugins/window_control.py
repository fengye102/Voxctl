"""窗口控制插件。

通过 Windows 虚拟键组合键（ctypes + keybd_event）控制桌面窗口——
显示桌面、最小化所有窗口、恢复窗口、切换窗口，
不引入 pycaw/comtypes，不调用 subprocess/os.system。

非 Windows 平台返回失败说明。
"""

from __future__ import annotations

import sys
from typing import Callable

from executor import ExecutionResult
from plugins.base import PluginSpec

# Virtual-Key codes (Winuser.h)
_VK_LWIN = 0x5B
_VK_SHIFT = 0x10
_VK_MENU = 0x12  # Alt
_VK_D = 0x44
_VK_M = 0x4D
_VK_TAB = 0x09

_KEYEVENTF_KEYUP = 0x0002

# action → (组合键序列, 播报文案)。序列按「先按下、后逆序抬起」处理。
_ACTIONS = {
    "show_desktop": ([_VK_LWIN, _VK_D], "已显示桌面"),
    "minimize_all": ([_VK_LWIN, _VK_M], "已最小化所有窗口"),
    "restore_windows": ([_VK_LWIN, _VK_SHIFT, _VK_M], "已恢复窗口"),
    "switch_window": ([_VK_MENU, _VK_TAB], "已切换窗口"),
}


def _tap_combo(keys: list[int], keybd_event: Callable) -> None:
    """按下一组组合键：依次按下所有键，再逆序抬起。

    逆序抬起是组合键的正确释放顺序（后按下的先松开），
    例如 Win+Shift+M 的 keys=[VK_LWIN, VK_SHIFT, VK_M]。
    """
    for vk in keys:
        keybd_event(vk, 0, 0, 0)
    for vk in reversed(keys):
        keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def _get_keybd_event() -> Callable:
    """懒获取 user32.keybd_event；测试可 monkeypatch 此函数返回值路径。"""
    import ctypes

    return ctypes.windll.user32.keybd_event


def handle_window_control(params: dict, confirmed: bool) -> ExecutionResult:
    """处理窗口控制意图。

    params:
      action: show_desktop | minimize_all | restore_windows | switch_window
    """
    del confirmed  # 本插件非 dangerous，不使用二次确认

    action = params.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        return ExecutionResult(
            success=False,
            action_type="window_control",
            error=(
                f"无效的窗口操作: {action!r}，可选 "
                "show_desktop/minimize_all/restore_windows/switch_window"
            ),
            payload={"message": "窗口操作无效，请再说一次"},
        )

    if sys.platform != "win32":
        return ExecutionResult(
            success=False,
            action_type="window_control",
            error="窗口控制仅支持 Windows",
            payload={"message": "当前系统不支持窗口控制，仅 Windows 可用"},
        )

    try:
        keybd_event = _get_keybd_event()
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="window_control",
            error=f"无法调用键盘接口: {exc}",
            payload={"message": "窗口控制失败"},
        )

    keys, message = _ACTIONS[action]
    try:
        _tap_combo(keys, keybd_event)
        return ExecutionResult(
            success=True,
            action_type="window_control",
            payload={
                "message": message,
                "action": action,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="window_control",
            error=f"组合键发送失败: {exc}",
            payload={"message": "窗口控制失败"},
        )


PLUGIN_SPEC = PluginSpec(
    intent_name="window_control",
    description=(
        "控制桌面窗口布局。用户说「显示桌面」「回到桌面」、"
        "「最小化所有窗口」「最小化窗口」、「恢复窗口」、"
        "「切换窗口」「切到下一个窗口」等时选此工具。"
        "不要用于关闭应用、退出助手、或其它无关操作。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "show_desktop",
                    "minimize_all",
                    "restore_windows",
                    "switch_window",
                ],
                "description": (
                    "窗口操作类型："
                    "show_desktop=显示桌面/回到桌面；"
                    "minimize_all=最小化所有窗口/最小化窗口；"
                    "restore_windows=恢复窗口；"
                    "switch_window=切换窗口/切到下一个窗口"
                ),
            },
        },
        "required": ["action"],
    },
    handler=handle_window_control,
    dangerous=False,
)
