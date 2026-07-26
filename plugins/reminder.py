"""本地一次性提醒插件。"""

from __future__ import annotations

from executor import ExecutionResult
from plugins.base import PluginSpec

_MAX_DELAY_SECONDS = 24 * 60 * 60
_MAX_MESSAGE_LENGTH = 200


def _failure(message: str) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        action_type="set_reminder",
        error=message,
        payload={"message": message},
    )


def handle_set_reminder(params: dict, confirmed: bool) -> ExecutionResult:
    """校验提醒参数；实际定时和播报由主循环负责。"""
    del confirmed
    params = params or {}
    delay = params.get("delay_seconds")
    message = params.get("message")

    if isinstance(delay, bool) or not isinstance(delay, int):
        return _failure("提醒时间必须是整数秒")
    if not 1 <= delay <= _MAX_DELAY_SECONDS:
        return _failure("提醒时间必须在1秒到24小时之间")
    if not isinstance(message, str) or not message.strip():
        return _failure("提醒内容不能为空")

    message = message.strip()
    if len(message) > _MAX_MESSAGE_LENGTH:
        return _failure(f"提醒内容不能超过{_MAX_MESSAGE_LENGTH}个字符")

    delay_text = f"{delay // 60}分钟" if delay % 60 == 0 else f"{delay}秒"
    return ExecutionResult(
        success=True,
        action_type="set_reminder",
        payload={
            "message": f"好的，{delay_text}后提醒你：{message}",
            "reminder": {"delay_seconds": delay, "message": message},
        },
    )


PLUGIN_SPEC = PluginSpec(
    intent_name="set_reminder",
    description=(
        "设置当前程序运行期间的一次性本地提醒。用户说‘10分钟后提醒我休息’"
        "‘30秒后提醒我关火’时选此工具；把分钟、小时换算成整数秒。"
        "不要用于查询当前时间、闹钟、重复提醒或超过24小时的提醒。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "delay_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_DELAY_SECONDS,
                "description": "从现在起等待的秒数，须把分钟或小时换算成秒",
            },
            "message": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_MESSAGE_LENGTH,
                "description": "到期时要播报的提醒内容",
            },
        },
        "required": ["delay_seconds", "message"],
    },
    handler=handle_set_reminder,
    dangerous=False,
)
