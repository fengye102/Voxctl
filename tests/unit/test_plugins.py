"""插件机制单元测试（修订 42）。"""

from __future__ import annotations

import textwrap
import types
from pathlib import Path

import pytest

from executor import ExecutionResult
from nlu.schema import IntentType, PluginIntent, build_intent_schema
from plugins import PluginRegistry
from plugins.base import PluginLoadError, PluginSpec


# ── fixtures ──────────────────────────────────────────────────────────

def _make_handler(success: bool = True, payload: dict | None = None):
    def handler(params: dict, confirmed: bool) -> ExecutionResult:
        return ExecutionResult(
            success=success,
            action_type="test_plugin",
            payload=payload or {"echo": params},
        )
    return handler


def _make_spec(
    name: str = "test_plugin",
    description: str = "测试插件",
    dangerous: bool = False,
    handler=None,
    schema: dict | None = None,
) -> PluginSpec:
    return PluginSpec(
        intent_name=name,
        description=description,
        parameters_schema=schema or {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": [],
        },
        handler=handler or _make_handler(),
        dangerous=dangerous,
    )


# ── PluginSpec ────────────────────────────────────────────────────────

class TestPluginSpec:
    def test_frozen(self):
        spec = _make_spec()
        with pytest.raises(AttributeError):
            spec.intent_name = "other"

    def test_defaults(self):
        spec = _make_spec()
        assert spec.dangerous is False


# ── PluginRegistry.register ──────────────────────────────────────────

class TestPluginRegistryRegister:
    def test_register_success(self):
        reg = PluginRegistry()
        reg.register(_make_spec("my_plugin"))
        assert reg.has("my_plugin")
        assert len(reg) == 1

    def test_reject_builtin_name(self):
        for name in IntentType:
            with pytest.raises(PluginLoadError, match="conflicts with built-in"):
                reg = PluginRegistry()
                reg.register(_make_spec(name.value))

    def test_reject_duplicate(self):
        reg = PluginRegistry()
        reg.register(_make_spec("dup"))
        with pytest.raises(PluginLoadError, match="already registered"):
            reg.register(_make_spec("dup"))

    def test_reject_empty_name(self):
        with pytest.raises(PluginLoadError, match="invalid intent_name"):
            reg = PluginRegistry()
            reg.register(_make_spec(""))

    def test_reject_bad_schema(self):
        with pytest.raises(PluginLoadError, match="type='object'"):
            reg = PluginRegistry()
            reg.register(_make_spec(schema={"type": "string"}))

    def test_reject_non_callable_handler(self):
        with pytest.raises(PluginLoadError, match="not callable"):
            reg = PluginRegistry()
            reg.register(PluginSpec(
                intent_name="bad",
                description="broken",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler="not_a_function",
            ))


# ── PluginRegistry.dispatch ──────────────────────────────────────────

class TestPluginRegistryDispatch:
    def test_dispatch_success(self):
        reg = PluginRegistry()
        reg.register(_make_spec("echo"))
        result = reg.dispatch("echo", {"msg": "hello"}, confirmed=False)
        assert result.success is True
        assert result.payload["echo"]["msg"] == "hello"

    def test_dispatch_unknown(self):
        reg = PluginRegistry()
        result = reg.dispatch("nonexistent", {}, confirmed=False)
        assert result.success is False
        assert "no plugin registered" in result.error

    def test_dispatch_dangerous_unconfirmed(self):
        reg = PluginRegistry()
        reg.register(_make_spec("risky", dangerous=True))
        result = reg.dispatch("risky", {}, confirmed=False)
        assert result.needs_confirmation is True
        assert result.success is False

    def test_dispatch_dangerous_confirmed(self):
        reg = PluginRegistry()
        reg.register(_make_spec("risky", dangerous=True))
        result = reg.dispatch("risky", {}, confirmed=True)
        assert result.success is True

    def test_dispatch_handler_exception(self):
        def bad_handler(params, confirmed):
            raise ValueError("boom")
        reg = PluginRegistry()
        reg.register(_make_spec("crashy", handler=bad_handler))
        result = reg.dispatch("crashy", {}, confirmed=False)
        assert result.success is False
        assert "boom" in result.error

    def test_dispatch_bad_return_type(self):
        def bad_return(params, confirmed):
            return {"not": "an ExecutionResult"}
        reg = PluginRegistry()
        reg.register(_make_spec("bad_ret", handler=bad_return))
        result = reg.dispatch("bad_ret", {}, confirmed=False)
        assert result.success is False
        assert "类型错误" in result.error


# ── PluginRegistry.tool_specs ────────────────────────────────────────

class TestPluginRegistryToolSpecs:
    def test_tool_specs_format(self):
        reg = PluginRegistry()
        reg.register(_make_spec("alpha", description="Alpha 插件"))
        reg.register(_make_spec("beta", description="Beta 插件"))
        specs = reg.tool_specs()
        assert len(specs) == 2
        assert specs[0]["name"] == "alpha"
        assert specs[1]["name"] == "beta"
        assert "description" in specs[0]
        assert "parameters" in specs[0]

    def test_empty_registry(self):
        reg = PluginRegistry()
        assert reg.tool_specs() == []
        assert not reg
        assert len(reg) == 0


# ── build_intent_schema with plugins ─────────────────────────────────

class TestBuildIntentSchemaWithPlugins:
    def test_without_plugins(self):
        schema = build_intent_schema()
        names = {i["name"] for i in schema["intents"]}
        assert "open_app" in names
        assert "unknown" in names

    def test_with_plugins(self):
        plugin_specs = [
            {"name": "custom_cmd", "description": "自定义", "parameters": {
                "type": "object", "properties": {}, "required": [],
            }}
        ]
        schema = build_intent_schema(plugin_tool_specs=plugin_specs)
        names = {i["name"] for i in schema["intents"]}
        assert "custom_cmd" in names
        assert "open_app" in names


# ── PluginIntent model ───────────────────────────────────────────────

class TestPluginIntentModel:
    def test_basic(self):
        pi = PluginIntent(intent="hello", params={"name": "世界"})
        assert pi.intent == "hello"
        assert pi.params["name"] == "世界"

    def test_empty_params(self):
        pi = PluginIntent(intent="noop")
        assert pi.params == {}


# ── PluginRegistry.load_from_directory ───────────────────────────────

class TestPluginRegistryLoadFromDirectory:
    def test_nonexistent_dir(self):
        reg = PluginRegistry.load_from_directory("/nonexistent/path")
        assert len(reg) == 0

    def test_load_valid_plugin(self, tmp_path: Path):
        plugin_code = textwrap.dedent("""\
            from plugins.base import PluginSpec
            from executor import ExecutionResult

            def handle(params, confirmed):
                return ExecutionResult(success=True, action_type="greet",
                                       payload={"msg": "hi"})

            PLUGIN_SPEC = PluginSpec(
                intent_name="greet",
                description="打招呼",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=handle,
            )
        """)
        (tmp_path / "greet.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert reg.has("greet")

    def test_skip_invalid_plugin(self, tmp_path: Path):
        (tmp_path / "broken.py").write_text("raise ImportError('nope')",
                                            encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert len(reg) == 0

    def test_skip_underscore_files(self, tmp_path: Path):
        (tmp_path / "_private.py").write_text("PLUGIN_SPEC = None",
                                              encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert len(reg) == 0
