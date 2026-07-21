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


# ── S4：插件源码 AST 安全扫描（修订 47 §6） ──────────────────────────

class TestPluginSourceSafety:
    """S4：插件源码静态扫描，检测危险调用模式并拒绝加载。

    深度防御——能拦住无意违规即可，有意动态绕过不在威胁模型内。
    """

    def test_import_subprocess_rejected(self, tmp_path: Path):
        plugin_code = textwrap.dedent("""\
            import subprocess
            from plugins.base import PluginSpec
            from executor import ExecutionResult

            def handle(params, confirmed):
                return ExecutionResult(success=True, action_type="t")

            PLUGIN_SPEC = PluginSpec(
                intent_name="t",
                description="t",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=handle,
            )
        """)
        (tmp_path / "bad.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert not reg.has("t"), "import subprocess 必须被拦截"

    def test_from_subprocess_import_rejected(self, tmp_path: Path):
        plugin_code = textwrap.dedent("""\
            from subprocess import Popen
            from plugins.base import PluginSpec
            PLUGIN_SPEC = PluginSpec(
                intent_name="t",
                description="t",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=lambda p, c: None,
            )
        """)
        (tmp_path / "bad2.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert len(reg) == 0

    def test_dunder_import_subprocess_rejected(self, tmp_path: Path):
        plugin_code = textwrap.dedent("""\
            sp = __import__('subprocess')
            from plugins.base import PluginSpec
            PLUGIN_SPEC = PluginSpec(
                intent_name="t",
                description="t",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=lambda p, c: None,
            )
        """)
        (tmp_path / "bad3.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert len(reg) == 0

    def test_dunder_import_os_rejected(self, tmp_path: Path):
        plugin_code = "__import__('os').system('echo hi')"
        (tmp_path / "bad4.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert len(reg) == 0

    def test_os_system_via_alias_rejected(self, tmp_path: Path):
        """import os + os.system() 调用必须被拦截。"""
        plugin_code = textwrap.dedent("""\
            import os
            from plugins.base import PluginSpec
            from executor import ExecutionResult

            def handle(params, confirmed):
                os.system('echo hi')
                return ExecutionResult(success=True, action_type="t")

            PLUGIN_SPEC = PluginSpec(
                intent_name="t",
                description="t",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=handle,
            )
        """)
        (tmp_path / "bad5.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert not reg.has("t")

    def test_os_popen_via_asname_rejected(self, tmp_path: Path):
        """import os as o + o.popen() 也要被拦截（追踪别名）。"""
        plugin_code = textwrap.dedent("""\
            import os as o
            from plugins.base import PluginSpec
            PLUGIN_SPEC = PluginSpec(
                intent_name="t",
                description="t",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=lambda p, c: o.popen('dir'),
            )
        """)
        (tmp_path / "bad6.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert len(reg) == 0

    def test_from_os_import_system_rejected(self, tmp_path: Path):
        """from os import system 后直接调用 system() 必须拦截。"""
        plugin_code = textwrap.dedent("""\
            from os import system
            from plugins.base import PluginSpec
            PLUGIN_SPEC = PluginSpec(
                intent_name="t",
                description="t",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=lambda p, c: system('echo hi'),
            )
        """)
        (tmp_path / "bad7.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert len(reg) == 0

    def test_safe_os_path_usage_allowed(self, tmp_path: Path):
        """合法插件只用 os.path / pathlib 等安全 API → 正常加载。"""
        plugin_code = textwrap.dedent("""\
            import os
            from pathlib import Path
            from plugins.base import PluginSpec
            from executor import ExecutionResult

            def handle(params, confirmed):
                # 只用 os.path 安全 API，不应触发 AST 扫描拦截
                p = os.path.join('a', 'b')
                return ExecutionResult(success=True, action_type="t",
                                       payload={"joined": p})

            PLUGIN_SPEC = PluginSpec(
                intent_name="safe_path",
                description="safe path plugin",
                parameters_schema={"type": "object", "properties": {}, "required": []},
                handler=handle,
            )
        """)
        (tmp_path / "safe.py").write_text(plugin_code, encoding="utf-8")
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        reg = PluginRegistry.load_from_directory(tmp_path)
        assert reg.has("safe_path"), "合法 os.path 使用不应被拦截"

    def test_scan_function_directly(self):
        """直接调 _scan_plugin_source_safety 验证错误消息。"""
        from plugins import _scan_plugin_source_safety
        from plugins.base import PluginLoadError

        with pytest.raises(PluginLoadError, match="subprocess"):
            _scan_plugin_source_safety("import subprocess", "test.py")

        with pytest.raises(PluginLoadError, match="subprocess"):
            _scan_plugin_source_safety(
                "from subprocess import Popen", "test.py"
            )

    def test_scan_function_allows_safe_code(self):
        """安全代码不抛异常。"""
        from plugins import _scan_plugin_source_safety
        # 不抛即通过
        _scan_plugin_source_safety(
            "import os\nx = os.path.join('a','b')", "safe.py"
        )
        _scan_plugin_source_safety("x = 1 + 2", "trivial.py")

    def test_scan_function_syntax_error_raises(self):
        """语法错误应抛 PluginLoadError（不掩盖）。"""
        from plugins import _scan_plugin_source_safety
        from plugins.base import PluginLoadError
        with pytest.raises(PluginLoadError, match="source parse failed"):
            _scan_plugin_source_safety("def broken(:", "bad.py")
