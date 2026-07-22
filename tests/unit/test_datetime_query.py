"""日期时间查询插件单元测试：monkeypatch _now 成固定时刻做确定性断言。"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from plugins.datetime_query import (
    PLUGIN_SPEC,
    _build_message,
    _format_date,
    _format_time,
    _normalize_field,
    handle_datetime_query,
)
from plugins import PluginRegistry
from plugins.base import PluginSpec


# ── helpers ───────────────────────────────────────────────────────────

def _freeze(monkeypatch, dt: datetime.datetime):
    monkeypatch.setattr("plugins.datetime_query._now", lambda: dt)


_WED_1525 = datetime.datetime(2026, 7, 22, 15, 25)  # 周三，下午 3:25


# ── _normalize_field ──────────────────────────────────────────────────

class TestNormalizeField:
    def test_valid(self):
        assert _normalize_field("time") == "time"
        assert _normalize_field("date") == "date"
        assert _normalize_field("both") == "both"

    def test_default_none(self):
        assert _normalize_field(None) == "both"

    def test_invalid_to_both(self):
        assert _normalize_field("year") == "both"
        assert _normalize_field("") == "both"
        assert _normalize_field(123) == "both"
        assert _normalize_field({}) == "both"


# ── _format_time ──────────────────────────────────────────────────────

class TestFormatTime:
    def test_afternoon(self):
        assert _format_time(datetime.datetime(2026, 7, 22, 15, 25)) == "现在是下午3点25分"

    def test_morning_on_the_hour(self):
        assert _format_time(datetime.datetime(2026, 7, 22, 9, 0)) == "现在是上午9点整"

    def test_noon(self):
        assert _format_time(datetime.datetime(2026, 7, 22, 12, 0)) == "现在是中午12点整"

    def test_noon_with_minutes(self):
        assert _format_time(datetime.datetime(2026, 7, 22, 12, 30)) == "现在是中午12点30分"

    def test_early_morning(self):
        assert _format_time(datetime.datetime(2026, 7, 22, 3, 5)) == "现在是凌晨3点5分"

    def test_midnight(self):
        # 0 点走 12 小时制显示为 12，归入凌晨
        assert _format_time(datetime.datetime(2026, 7, 22, 0, 0)) == "现在是凌晨12点整"

    def test_evening_maps_to_afternoon(self):
        # 只有 4 个时段，晚上归入下午
        assert _format_time(datetime.datetime(2026, 7, 22, 20, 8)) == "现在是下午8点8分"


# ── _format_date ──────────────────────────────────────────────────────

class TestFormatDate:
    def test_wednesday(self):
        assert _format_date(_WED_1525) == "今天是2026年7月22日，星期三"

    def test_sunday(self):
        # 2026-07-26 是周日
        assert _format_date(datetime.datetime(2026, 7, 26, 10, 0)) == "今天是2026年7月26日，星期日"

    def test_monday(self):
        # 2026-07-20 是周一
        assert _format_date(datetime.datetime(2026, 7, 20, 10, 0)) == "今天是2026年7月20日，星期一"


# ── handler: field 分派 ───────────────────────────────────────────────

class TestHandler:
    def test_field_time(self, monkeypatch):
        _freeze(monkeypatch, _WED_1525)
        result = handle_datetime_query({"field": "time"}, confirmed=False)
        assert result.success is True
        assert result.action_type == "datetime_query"
        assert result.payload["field"] == "time"
        assert result.payload["message"] == "现在是下午3点25分"

    def test_field_date(self, monkeypatch):
        _freeze(monkeypatch, _WED_1525)
        result = handle_datetime_query({"field": "date"}, confirmed=False)
        assert result.success is True
        assert result.payload["field"] == "date"
        assert result.payload["message"] == "今天是2026年7月22日，星期三"

    def test_field_both(self, monkeypatch):
        _freeze(monkeypatch, _WED_1525)
        result = handle_datetime_query({"field": "both"}, confirmed=False)
        assert result.success is True
        assert result.payload["field"] == "both"
        assert result.payload["message"] == "今天是2026年7月22日，星期三，现在是下午3点25分"

    def test_default_field_both(self, monkeypatch):
        _freeze(monkeypatch, _WED_1525)
        result = handle_datetime_query({}, confirmed=False)
        assert result.success is True
        assert result.payload["field"] == "both"
        assert result.payload["message"] == "今天是2026年7月22日，星期三，现在是下午3点25分"

    def test_invalid_field_to_both(self, monkeypatch):
        _freeze(monkeypatch, _WED_1525)
        result = handle_datetime_query({"field": "century"}, confirmed=False)
        assert result.success is True
        assert result.payload["field"] == "both"
        assert result.payload["message"] == "今天是2026年7月22日，星期三，现在是下午3点25分"

    def test_none_params(self, monkeypatch):
        _freeze(monkeypatch, _WED_1525)
        result = handle_datetime_query(None, confirmed=False)
        assert result.success is True
        assert result.payload["field"] == "both"

    def test_exception_fallback(self, monkeypatch):
        def boom():
            raise RuntimeError("clock broke")

        monkeypatch.setattr("plugins.datetime_query._now", boom)
        result = handle_datetime_query({"field": "time"}, confirmed=False)
        assert result.success is False
        assert result.action_type == "datetime_query"
        assert "失败" in (result.error or "")
        assert result.payload.get("message")


# ── _build_message ────────────────────────────────────────────────────

class TestBuildMessage:
    def test_both_is_date_comma_time(self):
        msg = _build_message("both", _WED_1525)
        assert msg == _format_date(_WED_1525) + "，" + _format_time(_WED_1525)


# ── PLUGIN_SPEC ───────────────────────────────────────────────────────

class TestPluginSpecMeta:
    def test_spec_shape(self):
        assert isinstance(PLUGIN_SPEC, PluginSpec)
        assert PLUGIN_SPEC.intent_name == "datetime_query"
        assert PLUGIN_SPEC.dangerous is False
        assert PLUGIN_SPEC.parameters_schema["type"] == "object"
        assert "field" in PLUGIN_SPEC.parameters_schema["properties"]
        assert PLUGIN_SPEC.handler is handle_datetime_query

    def test_loads_via_registry(self):
        reg = PluginRegistry.load_from_directory(
            Path(__file__).resolve().parents[2] / "plugins"
        )
        assert reg.has("datetime_query")
        spec = reg.get("datetime_query")
        assert spec is not None
        assert spec.dangerous is False
