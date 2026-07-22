"""媒体播放控制插件（媒体键版）。

通过 Windows 虚拟媒体键（ctypes + keybd_event）控制播放暂停/停止/切歌，
不引入 pycaw/comtypes，不调用 subprocess/os.system。

非 Windows 平台返回失败说明。
"""

from __future__ import annotations

import sys
from typing import Callable

from executor import ExecutionResult
from plugins.base import PluginSpec

# Virtual-Key codes (Winuser.h)
_VK_MEDIA_PLAY_PAUSE = 0xB3
_VK_MEDIA_STOP = 0xB2
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1

_KEYEVENTF_KEYUP = 0x0002

# action -> (虚拟键码, 播报文本)
_ACTION_MAP = {
    "play_pause": (_VK_MEDIA_PLAY_PAUSE, "已切换播放暂停"),
    "stop": (_VK_MEDIA_STOP, "已停止播放"),
    "next": (_VK_MEDIA_NEXT_TRACK, "已切到下一首"),
    "prev": (_VK_MEDIA_PREV_TRACK, "已切到上一首"),
}

_VALID_ACTIONS = frozenset(_ACTION_MAP.keys())


def _tap_key(vk: int, keybd_event: Callable) -> None:
    """按下并抬起一个虚拟键。"""
    keybd_event(vk, 0, 0, 0)
    keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def _get_keybd_event() -> Callable:
    """懒获取 user32.keybd_event；测试可 monkeypatch 此函数返回值路径。"""
    import ctypes

    return ctypes.windll.user32.keybd_event


def handle_media_control(params: dict, confirmed: bool) -> ExecutionResult:
    """处理媒体播放控制意图。

    params:
      action: play_pause | stop | next | prev
              每个动作就是 tap 一次对应媒体键（down+up），无 steps 参数
    """
    del confirmed  # 本插件非 dangerous，不使用二次确认

    action = params.get("action")
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        return ExecutionResult(
            success=False,
            action_type="media_control",
            error=f"无效的媒体操作: {action!r}，可选 play_pause/stop/next/prev",
            payload={"message": "媒体操作无效，请再说一次"},
        )

    if sys.platform != "win32":
        return ExecutionResult(
            success=False,
            action_type="media_control",
            error="媒体播放控制仅支持 Windows",
            payload={"message": "当前系统不支持媒体控制，仅 Windows 可用"},
        )

    try:
        keybd_event = _get_keybd_event()
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="media_control",
            error=f"无法调用键盘媒体键接口: {exc}",
            payload={"message": "媒体控制失败"},
        )

    try:
        vk, message = _ACTION_MAP[action]
        _tap_key(vk, keybd_event)
        return ExecutionResult(
            success=True,
            action_type="media_control",
            payload={
                "message": message,
                "action": action,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="media_control",
            error=f"媒体按键发送失败: {exc}",
            payload={"message": "媒体控制失败"},
        )


PLUGIN_SPEC = PluginSpec(
    intent_name="media_control",
    description=(
        "控制媒体播放。用户说「暂停」「播放」「继续播放」「停止播放」、"
        "「下一首」「下一个」「上一首」「上一个」等时选此工具。"
        "不要用于关闭应用、退出助手、或其它与媒体播放无关的操作。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["play_pause", "stop", "next", "prev"],
                "description": (
                    "媒体操作类型："
                    "play_pause=暂停/播放/继续播放（切换）；stop=停止播放；"
                    "next=下一首/下一个；prev=上一首/上一个"
                ),
            },
        },
        "required": ["action"],
    },
    handler=handle_media_control,
    dangerous=False,
)
