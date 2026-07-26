"""基于 Open-Meteo 的只读天气查询插件。"""

from __future__ import annotations

import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from executor import ExecutionResult
from plugins.base import PluginSpec

_GEOCODING_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_SECONDS = 5
_MAX_CITY_LENGTH = 100
_VALID_PERIODS = {"current", "today", "tomorrow"}


class WeatherQueryError(Exception):
    """天气服务请求或响应无效。"""


def _failure(message: str) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        action_type="weather_query",
        error=message,
        payload={"message": message},
    )


def _request_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "Voxctl/1.0"}, method="GET")
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
            data = json.load(response)
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise WeatherQueryError("天气服务暂时不可用") from exc
    if not isinstance(data, dict):
        raise WeatherQueryError("天气服务返回了无效数据")
    return data


def _lookup_city(city: str) -> tuple[float, float, str]:
    query = urlencode({"name": city, "count": 1, "language": "zh", "format": "json"})
    data = _request_json(f"{_GEOCODING_ENDPOINT}?{query}")
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise WeatherQueryError(f"没有找到城市：{city}")

    result = results[0]
    if not isinstance(result, dict):
        raise WeatherQueryError("城市查询返回了无效数据")
    latitude = result.get("latitude")
    longitude = result.get("longitude")
    name = result.get("name")
    if not _is_number(latitude) or not _is_number(longitude) or not isinstance(name, str):
        raise WeatherQueryError("城市查询返回了无效数据")
    return float(latitude), float(longitude), name.strip() or city


def _load_forecast(latitude: float, longitude: float) -> dict:
    query = urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "forecast_days": 2,
            "timezone": "auto",
        }
    )
    return _request_json(f"{_FORECAST_ENDPOINT}?{query}")


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _format_number(value: object) -> str:
    if not _is_number(value):
        raise WeatherQueryError("天气服务返回了无效数据")
    number = round(float(value), 1)
    return str(int(number)) if number.is_integer() else str(number)


def _weather_text(code: object) -> str:
    if not isinstance(code, int) or isinstance(code, bool):
        raise WeatherQueryError("天气服务返回了无效数据")
    if code == 0:
        return "晴"
    if code in {1, 2, 3}:
        return "多云"
    if code in {45, 48}:
        return "有雾"
    if code in {51, 53, 55, 56, 57}:
        return "有毛毛雨"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "有雨"
    if code in {71, 73, 75, 77, 85, 86}:
        return "有雪"
    if code in {95, 96, 99}:
        return "有雷雨"
    return "天气状况未知"


def _daily_value(daily: dict, field: str, index: int) -> object:
    values = daily.get(field)
    if not isinstance(values, list) or len(values) <= index:
        raise WeatherQueryError("天气服务返回了无效数据")
    return values[index]


def _format_current(city: str, forecast: dict) -> str:
    current = forecast.get("current")
    if not isinstance(current, dict):
        raise WeatherQueryError("天气服务返回了无效数据")
    condition = _weather_text(current.get("weather_code"))
    temperature = _format_number(current.get("temperature_2m"))
    apparent = _format_number(current.get("apparent_temperature"))
    wind = _format_number(current.get("wind_speed_10m"))
    return f"{city}现在{condition}，{temperature}度，体感{apparent}度，风速每小时{wind}公里"


def _format_daily(city: str, forecast: dict, period: str) -> str:
    daily = forecast.get("daily")
    if not isinstance(daily, dict):
        raise WeatherQueryError("天气服务返回了无效数据")
    index = 0 if period == "today" else 1
    label = "今天" if period == "today" else "明天"
    condition = _weather_text(_daily_value(daily, "weather_code", index))
    minimum = _format_number(_daily_value(daily, "temperature_2m_min", index))
    maximum = _format_number(_daily_value(daily, "temperature_2m_max", index))
    precipitation = _format_number(
        _daily_value(daily, "precipitation_probability_max", index)
    )
    return f"{city}{label}{condition}，{minimum}到{maximum}度，最高降雨概率{precipitation}%"


def handle_weather_query(params: dict, confirmed: bool) -> ExecutionResult:
    """查询明确城市的当前、今天或明天天气。"""
    del confirmed
    params = params or {}
    city = params.get("city")
    period = params.get("period")
    if not isinstance(city, str) or not city.strip():
        return _failure("请明确说出要查询的城市")
    if len(city.strip()) > _MAX_CITY_LENGTH:
        return _failure(f"城市名称不能超过{_MAX_CITY_LENGTH}个字符")
    if not isinstance(period, str) or period not in _VALID_PERIODS:
        return _failure("天气查询时段必须是现在、今天或明天")

    city = city.strip()
    try:
        latitude, longitude, display_name = _lookup_city(city)
        forecast = _load_forecast(latitude, longitude)
        if period == "current":
            message = _format_current(display_name, forecast)
        else:
            message = _format_daily(display_name, forecast, period)
    except WeatherQueryError as exc:
        return _failure(str(exc))

    return ExecutionResult(
        success=True,
        action_type="weather_query",
        payload={"message": message},
    )


PLUGIN_SPEC = PluginSpec(
    intent_name="weather_query",
    description=(
        "查询用户明确指定城市的现在、今天或明天天气。"
        "用户未说城市时不要猜测位置，应先询问城市；不用于查询空气质量或生活指数。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_CITY_LENGTH,
                "description": "用户明确说出的城市名称，例如上海、北京、杭州",
            },
            "period": {
                "type": "string",
                "enum": ["current", "today", "tomorrow"],
                "description": "查询时段：现在、今天或明天",
            },
        },
        "required": ["city", "period"],
    },
    handler=handle_weather_query,
    dangerous=False,
)
