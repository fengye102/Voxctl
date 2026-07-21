"""插件注册表（CLAUDE.md §6 修订 42）。

PluginRegistry 负责：
  1. 扫描 plugins/ 目录下的 .py 文件，加载 PLUGIN_SPEC
  2. 校验 intent_name 不与内置意图冲突、不重复
  3. 提供 tool_specs() 给 build_intent_schema() 合并
  4. 提供 dispatch(intent_name, params, confirmed) 给 Executor 分发

加载失败的插件跳过并记日志，不阻断启动。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Optional

from executor import ExecutionResult
from nlu.schema import IntentType
from plugins.base import (
    PluginError,
    PluginHandlerError,
    PluginLoadError,
    PluginSpec,
)

_LOG = logging.getLogger(__name__)

_BUILTIN_INTENT_NAMES = frozenset(e.value for e in IntentType)


def _scan_plugin_source_safety(source: str, filename: str) -> None:
    """AST 扫描插件源码，发现危险调用模式则抛 PluginLoadError（修订 47 §6）。

    检测：
    - import subprocess / from subprocess import ...
    - __import__('subprocess') / __import__('os')
    - os.system() / os.popen()（追踪 os 模块别名）
    - from os import system / popen 后直接调用

    深度防御，非绝对屏障——getattr(os, 'system') 等动态绕过仍可能。能拦住
    无意违规即可，有意绕过等同本地代码执行权限，不在威胁模型内。
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise PluginLoadError(f"source parse failed: {exc}") from exc

    os_aliases: set[str] = set()  # os 模块的别名（import os / import os as o）
    os_dangerous_funcs: set[str] = set()  # from os import system/popen 的别名

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    raise PluginLoadError(
                        f"禁止 import subprocess（§6 修订 47）：插件不得直接执行系统命令"
                    )
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                raise PluginLoadError(
                    f"禁止 from subprocess import ...（§6 修订 47）"
                )
            if node.module == "os":
                for alias in node.names:
                    if alias.name in ("system", "popen"):
                        os_dangerous_funcs.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            func = node.func
            # __import__('subprocess'/'os')
            if isinstance(func, ast.Name) and func.id == "__import__":
                if node.args and isinstance(node.args[0], ast.Constant):
                    mod = node.args[0].value
                    if mod in ("subprocess", "os"):
                        raise PluginLoadError(
                            f"禁止 __import__({mod!r})（§6 修订 47）"
                        )
            # os.system() / os.popen() via alias
            elif isinstance(func, ast.Attribute):
                if (isinstance(func.value, ast.Name)
                        and func.value.id in os_aliases
                        and func.attr in ("system", "popen")):
                    raise PluginLoadError(
                        f"禁止 {func.value.id}.{func.attr}()（§6 修订 47）"
                    )
            # 直接调用 from os import system 的 system()
            elif isinstance(func, ast.Name) and func.id in os_dangerous_funcs:
                raise PluginLoadError(
                    f"禁止调用 {func.id}()（§6 修订 47：from os import 的危险函数）"
                )


class PluginRegistry:
    """插件注册表。启动时构造一次，进程内常驻。"""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginSpec] = {}

    @classmethod
    def load_from_directory(cls, plugins_dir: str | Path = "plugins") -> "PluginRegistry":
        registry = cls()
        plugins_path = Path(plugins_dir)
        if not plugins_path.is_dir():
            _LOG.debug("plugins directory %s not found, no plugins loaded", plugins_path)
            return registry

        for py_file in sorted(plugins_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                registry._load_file(py_file)
            except PluginLoadError as exc:
                _LOG.warning("skip plugin %s: %s", py_file.name, exc)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("skip plugin %s: unexpected error: %s", py_file.name, exc)

        if registry._plugins:
            _LOG.info(
                "loaded %d plugin(s): %s",
                len(registry._plugins),
                ", ".join(sorted(registry._plugins)),
            )
        return registry

    def _load_file(self, py_file: Path) -> None:
        module_name = f"plugins.{py_file.stem}"
        try:
            # 修订 47 §6：exec_module 前先 AST 扫描源码，拦截危险调用模式
            # （import subprocess / os.system / __import__ 等），防止插件绕过权限边界
            source = py_file.read_text(encoding="utf-8")
            _scan_plugin_source_safety(source, str(py_file))

            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot create module spec for {py_file}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
        except PluginLoadError:
            # AST 扫描抛的 PluginLoadError 直接传播，不被下面 Exception 包装
            sys.modules.pop(module_name, None)
            raise
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginLoadError(f"import failed: {exc}") from exc

        plugin_spec = getattr(mod, "PLUGIN_SPEC", None)
        if plugin_spec is None:
            raise PluginLoadError("no PLUGIN_SPEC found")
        if not isinstance(plugin_spec, PluginSpec):
            raise PluginLoadError(
                f"PLUGIN_SPEC is {type(plugin_spec).__name__}, expected PluginSpec"
            )

        self.register(plugin_spec)

    def register(self, spec: PluginSpec) -> None:
        name = spec.intent_name
        if not name or not name.replace("_", "").isalnum():
            raise PluginLoadError(
                f"invalid intent_name {name!r}: must be non-empty lowercase alphanumeric with underscores"
            )
        if name in _BUILTIN_INTENT_NAMES:
            raise PluginLoadError(
                f"intent_name {name!r} conflicts with built-in intent"
            )
        if name in self._plugins:
            raise PluginLoadError(
                f"intent_name {name!r} already registered by another plugin"
            )
        if not callable(spec.handler):
            raise PluginLoadError(f"handler for {name!r} is not callable")

        schema = spec.parameters_schema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise PluginLoadError(
                f"parameters_schema for {name!r} must be a dict with type='object'"
            )

        self._plugins[name] = spec
        _LOG.debug("registered plugin intent: %s", name)

    def has(self, intent_name: str) -> bool:
        return intent_name in self._plugins

    def get(self, intent_name: str) -> Optional[PluginSpec]:
        return self._plugins.get(intent_name)

    def tool_specs(self) -> list[dict]:
        specs = []
        for name in sorted(self._plugins):
            plugin = self._plugins[name]
            specs.append({
                "name": name,
                "description": plugin.description,
                "parameters": plugin.parameters_schema,
            })
        return specs

    def dispatch(
        self, intent_name: str, params: dict, confirmed: bool
    ) -> ExecutionResult:
        plugin = self._plugins.get(intent_name)
        if plugin is None:
            return ExecutionResult(
                success=False,
                action_type=intent_name,
                error=f"no plugin registered for intent {intent_name!r}",
            )

        if plugin.dangerous and not confirmed:
            return ExecutionResult(
                success=False,
                action_type=intent_name,
                needs_confirmation=True,
                payload={"message": f"插件 {intent_name} 需要确认执行"},
            )

        try:
            result = plugin.handler(params, confirmed)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("plugin %s handler failed", intent_name)
            return ExecutionResult(
                success=False,
                action_type=intent_name,
                error=f"插件执行异常: {type(exc).__name__}: {exc}",
            )

        if not isinstance(result, ExecutionResult):
            _LOG.error(
                "plugin %s handler returned %s instead of ExecutionResult",
                intent_name, type(result).__name__,
            )
            return ExecutionResult(
                success=False,
                action_type=intent_name,
                error=f"插件返回类型错误: {type(result).__name__}",
            )

        return result

    @property
    def intent_names(self) -> frozenset[str]:
        return frozenset(self._plugins)

    def __len__(self) -> int:
        return len(self._plugins)

    def __bool__(self) -> bool:
        return bool(self._plugins)


__all__ = ["PluginRegistry", "PluginSpec", "PluginError", "PluginLoadError", "PluginHandlerError"]
