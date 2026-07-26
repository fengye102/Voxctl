"""通过 Windows WMI 查询和调节内置显示器亮度。"""

from __future__ import annotations

import sys
import gc

from executor import ExecutionResult
from plugins.base import PluginSpec

_VALID_ACTIONS = {"query", "set", "up", "down"}
_DEFAULT_STEP = 10


class BrightnessError(Exception):
    """亮度 WMI 接口不可用或调用失败。"""


def _failure(message: str) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        action_type="brightness_control",
        error=message,
        payload={"message": message},
    )


def _apply_brightness(reading_objects, setter_objects, action, value, step) -> list[int]:
    readings = {}
    for item in reading_objects:
        current = _integer(item.CurrentBrightness, 0, 100)
        if current is not None:
            readings[str(item.InstanceName)] = current
    if not readings:
        raise BrightnessError("当前显示器不支持系统亮度控制")
    if action == "query":
        return list(readings.values())

    setters = {str(item.InstanceName): item for item in setter_objects}
    targets = []
    for instance_name, current in readings.items():
        setter = setters.get(instance_name)
        if setter is None:
            continue
        if action == "set":
            target = value
        elif action == "up":
            target = min(100, current + step)
        else:
            target = max(0, current - step)
        method = setter.Methods_("WmiSetBrightness")
        params = method.InParameters.SpawnInstance_()
        params.Properties_.Item("Timeout").Value = 0
        params.Properties_.Item("Brightness").Value = target
        setter.ExecMethod_("WmiSetBrightness", params)
        targets.append(target)
    if not targets:
        raise BrightnessError("当前显示器没有可用的亮度写入接口")
    return targets


def _run_wmi(action: str, value: int | None, step: int) -> list[int]:
    if sys.platform != "win32":
        raise BrightnessError("屏幕亮度控制仅支持 Windows")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise BrightnessError("缺少 Windows 亮度控制组件") from exc

    pythoncom.CoInitialize()
    service = None
    readings = []
    setters = []
    result = None
    error = None
    try:
        try:
            service = win32com.client.GetObject(r"winmgmts:\\.\root\WMI")
            readings = list(service.InstancesOf("WmiMonitorBrightness"))
            setters = list(service.InstancesOf("WmiMonitorBrightnessMethods"))
            result = _apply_brightness(readings, setters, action, value, step)
        except BrightnessError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            error = f"无法访问屏幕亮度接口：{exc}"
    finally:
        readings.clear()
        setters.clear()
        service = None
        gc.collect()
        pythoncom.CoUninitialize()
    if error is not None:
        raise BrightnessError(error)
    return result


def _integer(value: object, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _level_message(levels: list[int]) -> str:
    minimum, maximum = min(levels), max(levels)
    return f"{minimum}%" if minimum == maximum else f"{minimum}%到{maximum}%"


def handle_brightness_control(params: dict, confirmed: bool) -> ExecutionResult:
    """查询、设置或按步长增减 WMI 显示器亮度。"""
    del confirmed
    if not isinstance(params, dict):
        return _failure("亮度参数无效")

    action = params.get("action")
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        return _failure("亮度操作必须是查询、设置、增大或减小")

    value = None
    step = _DEFAULT_STEP
    if action == "set":
        value = _integer(params.get("value"), 0, 100)
        if value is None:
            return _failure("亮度值必须是0到100的整数")
    elif action in {"up", "down"} and "step" in params:
        parsed_step = _integer(params.get("step"), 1, 100)
        if parsed_step is None:
            return _failure("亮度步长必须是1到100的整数")
        step = parsed_step

    try:
        levels = _run_wmi(action, value, step)
    except BrightnessError as exc:
        return _failure(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _failure(f"调整屏幕亮度失败：{exc}")

    if action == "query":
        message = f"当前屏幕亮度为{_level_message(levels)}"
        return ExecutionResult(
            success=True,
            action_type="brightness_control",
            payload={"message": message, "levels": levels},
        )

    if action == "set":
        message = f"已将屏幕亮度设为{_level_message(levels)}"
    elif action == "up":
        message = f"已增大屏幕亮度，当前为{_level_message(levels)}"
    else:
        message = f"已减小屏幕亮度，当前为{_level_message(levels)}"
    return ExecutionResult(
        success=True,
        action_type="brightness_control",
        payload={"message": message, "levels": levels},
    )


PLUGIN_SPEC = PluginSpec(
    intent_name="brightness_control",
    description=(
        "查询、设置、增大或减小 Windows 内置屏幕亮度。"
        "用户问当前亮度或明确要求调整屏幕亮度时使用；不用于音量、主题或外接显示器设置。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["query", "set", "up", "down"],
                "description": "亮度操作：查询、设置、增大或减小",
            },
            "value": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "仅 set 使用，要设置的亮度百分比",
            },
            "step": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "仅 up/down 使用，调整百分比，默认10",
            },
        },
        "required": ["action"],
    },
    handler=handle_brightness_control,
    dangerous=False,
)
