"""天气查询插件测试（修订 61，网络全部 mock）。"""

import io
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import pytest

from plugins import PluginRegistry
from plugins.base import PluginSpec
from plugins.weather_query import (
    PLUGIN_SPEC,
    WeatherQueryError,
    _request_json,
    _weather_text,
    handle_weather_query,
)


@pytest.fixture
def api_responses(monkeypatch):
    calls = []

    def fake_request_json(url):
        calls.append(url)
        if "geocoding-api" in url:
            return {
                "results": [
                    {"name": "上海", "latitude": 31.23, "longitude": 121.47}
                ]
            }
        return {
            "current": {
                "temperature_2m": 23,
                "apparent_temperature": 24.2,
                "weather_code": 2,
                "wind_speed_10m": 12,
            },
            "daily": {
                "weather_code": [2, 61],
                "temperature_2m_min": [18, 17.5],
                "temperature_2m_max": [27, 25],
                "precipitation_probability_max": [30, 70],
            },
        }

    monkeypatch.setattr("plugins.weather_query._request_json", fake_request_json)
    return calls


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("current", "上海现在多云，23度，体感24.2度，风速每小时12公里"),
        ("today", "上海今天多云，18到27度，最高降雨概率30%"),
        ("tomorrow", "上海明天有雨，17.5到25度，最高降雨概率70%"),
    ],
)
def test_weather_periods(api_responses, period, expected):
    result = handle_weather_query({"city": " 上海 ", "period": period}, False)
    assert result.success is True
    assert result.payload["message"] == expected
    assert len(api_responses) == 2


def test_request_urls_are_fixed_get_endpoints(api_responses):
    handle_weather_query({"city": "上海 浦东", "period": "today"}, False)
    geocoding = urlparse(api_responses[0])
    forecast = urlparse(api_responses[1])
    assert (geocoding.scheme, geocoding.netloc, geocoding.path) == (
        "https",
        "geocoding-api.open-meteo.com",
        "/v1/search",
    )
    assert parse_qs(geocoding.query)["name"] == ["上海 浦东"]
    assert (forecast.scheme, forecast.netloc, forecast.path) == (
        "https",
        "api.open-meteo.com",
        "/v1/forecast",
    )
    assert parse_qs(forecast.query)["forecast_days"] == ["2"]


@pytest.mark.parametrize("city", [None, "", "   ", 123])
def test_missing_city_is_rejected_without_network(monkeypatch, city):
    called = False

    def fail_if_called(url):
        nonlocal called
        called = True

    monkeypatch.setattr("plugins.weather_query._request_json", fail_if_called)
    result = handle_weather_query({"city": city, "period": "current"}, False)
    assert result.success is False
    assert result.payload["message"] == "请明确说出要查询的城市"
    assert called is False


def test_overlong_city_is_rejected():
    result = handle_weather_query({"city": "城" * 101, "period": "current"}, False)
    assert result.success is False
    assert "100" in result.payload["message"]


@pytest.mark.parametrize("period", [None, "", "week", 1, []])
def test_invalid_period_is_rejected(period):
    result = handle_weather_query({"city": "上海", "period": period}, False)
    assert result.success is False
    assert "时段" in result.payload["message"]


def test_city_not_found(monkeypatch):
    monkeypatch.setattr("plugins.weather_query._request_json", lambda url: {})
    result = handle_weather_query({"city": "不存在城", "period": "current"}, False)
    assert result.success is False
    assert result.payload["message"] == "没有找到城市：不存在城"


def test_bad_forecast_is_reported(monkeypatch):
    responses = iter(
        [
            {"results": [{"name": "上海", "latitude": 31.2, "longitude": 121.5}]},
            {"current": {"temperature_2m": 20}},
        ]
    )
    monkeypatch.setattr("plugins.weather_query._request_json", lambda url: next(responses))
    result = handle_weather_query({"city": "上海", "period": "current"}, False)
    assert result.success is False
    assert result.payload["message"] == "天气服务返回了无效数据"


def test_non_finite_weather_value_is_rejected(monkeypatch):
    responses = iter(
        [
            {"results": [{"name": "上海", "latitude": 31.2, "longitude": 121.5}]},
            {
                "current": {
                    "temperature_2m": float("nan"),
                    "apparent_temperature": 20,
                    "weather_code": 0,
                    "wind_speed_10m": 3,
                }
            },
        ]
    )
    monkeypatch.setattr("plugins.weather_query._request_json", lambda url: next(responses))
    result = handle_weather_query({"city": "上海", "period": "current"}, False)
    assert result.success is False
    assert result.payload["message"] == "天气服务返回了无效数据"


def test_request_json_uses_timeout_and_user_agent(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response(b'{"ok": true}')

    monkeypatch.setattr("plugins.weather_query.urlopen", fake_urlopen)
    assert _request_json("https://api.open-meteo.com/test") == {"ok": True}
    assert captured["request"].method == "GET"
    assert captured["request"].get_header("User-agent") == "Voxctl/1.0"
    assert captured["timeout"] == 5


def test_request_json_wraps_network_error(monkeypatch):
    def fail(request, timeout):
        raise URLError("offline")

    monkeypatch.setattr("plugins.weather_query.urlopen", fail)
    with pytest.raises(WeatherQueryError, match="暂时不可用"):
        _request_json("https://api.open-meteo.com/test")


@pytest.mark.parametrize(
    ("code", "text"),
    [(0, "晴"), (45, "有雾"), (53, "有毛毛雨"), (75, "有雪"), (95, "有雷雨")],
)
def test_wmo_code_mapping(code, text):
    assert _weather_text(code) == text


def test_plugin_spec_and_registry():
    assert isinstance(PLUGIN_SPEC, PluginSpec)
    assert PLUGIN_SPEC.intent_name == "weather_query"
    assert PLUGIN_SPEC.dangerous is False
    registry = PluginRegistry.load_from_directory(
        Path(__file__).resolve().parents[2] / "plugins"
    )
    assert registry.has("weather_query")
