"""系统快捷操作插件（锁屏 / 截图）。

通过纯 ctypes 调用 Win32 API：
  - lock       → user32.LockWorkStation() 直接锁屏（非按键）
  - screenshot → 发送 Win+Shift+S 组合键，唤起系统框选截图工具

不引入 pycaw/comtypes，不调用 subprocess/os.system/os.popen。
非 Windows 平台返回失败说明。
"""

from __future__ import annotations

import sys

from executor import ExecutionResult
from plugins.base import PluginSpec

# Virtual-Key codes (Winuser.h)
_VK_LWIN = 0x5B
_VK_SHIFT = 0x10
_VK_S = 0x53

_KEYEVENTF_KEYUP = 0x0002

_VALID_ACTIONS = frozenset({"lock", "screenshot"})


def _get_user32():
    """懒获取 user32；测试可 monkeypatch 此函数返回值路径。

    lock 走 _get_user32().LockWorkStation()，
    screenshot 走 _get_user32().keybd_event，共用同一入口便于测试拦截。
    """
    import ctypes

    return ctypes.windll.user32


def _send_combo(user32, vks) -> None:
    """依次按下 vks，再逆序抬起，模拟组合键。"""
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def handle_system_shortcuts(params: dict, confirmed: bool) -> ExecutionResult:
    """处理系统快捷操作意图。

    params:
      action: lock | screenshot
    """
    del confirmed  # 本插件非 dangerous，不使用二次确认

    action = params.get("action")
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        return ExecutionResult(
            success=False,
            action_type="system_shortcuts",
            error=f"无效的系统快捷操作: {action!r}，可选 lock/screenshot",
            payload={"message": "系统快捷操作无效，请再说一次"},
        )

    if sys.platform != "win32":
        return ExecutionResult(
            success=False,
            action_type="system_shortcuts",
            error="系统快捷操作仅支持 Windows",
            payload={"message": "当前系统不支持此操作，仅 Windows 可用"},
        )

    try:
        user32 = _get_user32()
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="system_shortcuts",
            error=f"无法调用 user32 接口: {exc}",
            payload={"message": "系统快捷操作失败"},
        )

    try:
        if action == "lock":
            user32.LockWorkStation()
            message = "已锁定电脑"
        else:  # screenshot
            # Win+Shift+S 唤起系统框选截图
            _send_combo(user32, [_VK_LWIN, _VK_SHIFT, _VK_S])
            message = "已打开截图工具"
        return ExecutionResult(
            success=True,
            action_type="system_shortcuts",
            payload={
                "message": message,
                "action": action,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="system_shortcuts",
            error=f"系统快捷操作执行失败: {exc}",
            payload={"message": "系统快捷操作失败"},
        )


PLUGIN_SPEC = PluginSpec(
    intent_name="system_shortcuts",
    description=(
        "执行系统快捷操作：锁屏或截图。用户说「锁屏」「锁定电脑」「锁定屏幕」时选 lock；"
        "说「截图」「截个图」「屏幕截图」「框选截图」时选 screenshot。"
        "不要用于关闭应用、退出助手、或其它无关操作。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["lock", "screenshot"],
                "description": (
                    "操作类型："
                    "lock=锁屏/锁定电脑/锁定屏幕；"
                    "screenshot=截图/截个图/屏幕截图/框选截图"
                ),
            },
        },
        "required": ["action"],
    },
    handler=handle_system_shortcuts,
    dangerous=False,
)
