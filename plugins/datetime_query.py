"""日期时间查询插件（纯信息查询）。

用当前系统时间回答「现在几点」「今天几号」「今天星期几」等问题，
把结果拼成中文播报文本交给 TTS 朗读。纯只读查询，跨平台可用：
不碰任何系统按键、不 import subprocess/os.system、不做任何副作用。

当前时间统一经懒函数 ``_now()`` 获取，测试可 monkeypatch 成固定时刻做
确定性断言。
"""

from __future__ import annotations

import datetime

from executor import ExecutionResult
from plugins.base import PluginSpec

_VALID_FIELDS = frozenset({"time", "date", "both"})
_DEFAULT_FIELD = "both"

# weekday(): 周一=0 … 周日=6
_WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")


def _now() -> datetime.datetime:
    """懒获取当前时间。独立成函数，供测试 monkeypatch 成固定时刻。"""
    return datetime.datetime.now()


def _normalize_field(raw) -> str:
    """把 field 归一化到 time/date/both；无效/缺省一律当作 both（宽松，不报错）。"""
    if isinstance(raw, str) and raw in _VALID_FIELDS:
        return raw
    return _DEFAULT_FIELD


def _format_time(dt: datetime.datetime) -> str:
    """12 小时制中文报时，如「下午3点25分」「上午9点整」「中午12点整」。"""
    hour = dt.hour
    if hour <= 5:
        period = "凌晨"
    elif hour <= 11:
        period = "上午"
    elif hour == 12:
        period = "中午"
    else:
        period = "下午"

    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12

    minute = dt.minute
    if minute == 0:
        return f"现在是{period}{display_hour}点整"
    return f"现在是{period}{display_hour}点{minute}分"


def _format_date(dt: datetime.datetime) -> str:
    """中文日期，如「今天是2026年7月22日，星期三」。"""
    weekday = _WEEKDAY_CN[dt.weekday()]
    return f"今天是{dt.year}年{dt.month}月{dt.day}日，星期{weekday}"


def _build_message(field: str, dt: datetime.datetime) -> str:
    if field == "time":
        return _format_time(dt)
    if field == "date":
        return _format_date(dt)
    return f"{_format_date(dt)}，{_format_time(dt)}"


def handle_datetime_query(params: dict, confirmed: bool) -> ExecutionResult:
    """处理日期时间查询意图。

    params:
      field: time | date | both（可选，默认 both；无效值当作 both）
    """
    del confirmed  # 非 dangerous，不使用二次确认

    try:
        field = _normalize_field((params or {}).get("field"))
        message = _build_message(field, _now())
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="datetime_query",
            error=f"查询日期时间失败: {type(exc).__name__}: {exc}",
            payload={"message": "查询日期时间失败"},
        )

    return ExecutionResult(
        success=True,
        action_type="datetime_query",
        payload={"message": message, "field": field},
    )


PLUGIN_SPEC = PluginSpec(
    intent_name="datetime_query",
    description=(
        "查询当前日期或时间并读出来。用户说「现在几点」「几点了」「报时」"
        "「现在时间」「今天几号」「今天星期几」「今天日期」「现在是几月几日」"
        "「今天礼拜几」「现在日期时间」等时选此工具。"
        "本工具只是把当前系统的日期/时间读出来，"
        "不要用于设置闹钟、定时任务、倒计时，"
        "也不要用于任何与「报当前日期时间」无关的操作。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "enum": ["time", "date", "both"],
                "description": (
                    "要播报的内容："
                    "time=只报时间（几点几分）；date=只报日期（几月几日星期几）；"
                    "both=日期和时间都报（默认）"
                ),
            },
        },
        "required": [],
    },
    handler=handle_datetime_query,
    dangerous=False,
)
