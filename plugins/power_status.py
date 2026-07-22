"""电池/电源状态查询插件。

纯信息查询：读取本机电池电量与充电状态并交给 TTS 播报，不触碰任何
系统按键、不改动任何设置。用 psutil.sensors_battery()（psutil 已是项目
依赖，见 executor/system_query.py），不引入其它库，不调用 subprocess/os.system。

台式机 / 无电池设备：sensors_battery() 返回 None，友好提示而非报错。
"""

from __future__ import annotations

from executor import ExecutionResult
from plugins.base import PluginSpec


def _get_battery():
    """懒获取 psutil.sensors_battery() 的返回值。

    返回：
      - 有电池：一个带 .percent / .power_plugged / .secsleft 属性的对象
      - None：无电池（台式机等）

    独立成函数便于测试 monkeypatch，不依赖真实硬件；异常向上抛，
    由 handler 兜底成失败的 ExecutionResult。
    """
    import psutil

    return psutil.sensors_battery()


def handle_power_status(params: dict, confirmed: bool) -> ExecutionResult:
    """处理电池/电源状态查询意图。

    只读、无参数（params 保留以兼容统一 handler 签名），非 dangerous。
    """
    del params, confirmed  # 只读、非 dangerous，均不使用

    try:
        battery = _get_battery()
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="power_status",
            error=f"读取电池状态失败: {type(exc).__name__}: {exc}",
            payload={"message": "读取电池状态失败"},
        )

    # 无电池（台式机等）：算成功，友好提示
    if battery is None:
        return ExecutionResult(
            success=True,
            action_type="power_status",
            payload={
                "message": "这台电脑没有电池，可能是台式机",
                "has_battery": False,
            },
        )

    try:
        percent = round(float(battery.percent))
        plugged = bool(battery.power_plugged)
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="power_status",
            error=f"解析电池状态失败: {type(exc).__name__}: {exc}",
            payload={"message": "读取电池状态失败"},
        )

    charge_text = "正在充电" if plugged else "未充电"
    return ExecutionResult(
        success=True,
        action_type="power_status",
        payload={
            "message": f"电量还有{percent}%，{charge_text}",
            "has_battery": True,
            "percent": percent,
            "power_plugged": plugged,
        },
    )


PLUGIN_SPEC = PluginSpec(
    intent_name="power_status",
    description=(
        "查询本机电池电量和充电状态。用户说「还有多少电」「电量多少」"
        "「电池状态」「剩余电量」「在充电吗」「插着电吗」「电池还剩多少」"
        "等时选此工具。本工具只是把电池信息读出来播报，"
        "不要用于调节音量、关闭应用、运行命令或任何与查电量无关的操作。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    handler=handle_power_status,
    dangerous=False,
)
