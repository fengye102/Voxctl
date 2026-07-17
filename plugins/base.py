"""插件基础定义（CLAUDE.md §6 修订 42）。

一个插件 = plugins/ 下的一个 .py 文件，模块级暴露 PLUGIN_SPEC 变量。

安全约束（§6 强制）：
  - handler 禁止直接调 subprocess/os.system/os.popen
  - handler 返回 ExecutionResult，走与内置意图相同的 TTS 模板链路
  - handler 异常不能崩主循环——PluginRegistry.dispatch() 内部兜底

用法示例（plugins/hello.py）::

    from plugins.base import PluginSpec
    from executor import ExecutionResult

    def handle_hello(params: dict, confirmed: bool) -> ExecutionResult:
        name = params.get("name", "世界")
        return ExecutionResult(
            success=True,
            action_type="hello",
            payload={"message": f"你好，{name}！"},
        )

    PLUGIN_SPEC = PluginSpec(
        intent_name="hello",
        description="打招呼。用户说'你好'、'问个好'等时选此工具。",
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "打招呼对象的名字，不填则默认'世界'",
                }
            },
            "required": [],
        },
        handler=handle_hello,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from executor import ExecutionResult


class PluginError(Exception):
    """插件机制错误基类。"""


class PluginLoadError(PluginError):
    """插件加载失败（import 错误、缺少 PLUGIN_SPEC、spec 校验失败）。"""


class PluginHandlerError(PluginError):
    """插件 handler 运行时异常。"""


@dataclass(frozen=True)
class PluginSpec:
    """插件规格声明。

    Attributes:
        intent_name:       唯一意图名（小写下划线），不能与内置意图名冲突。
        description:       给 LLM 的中文工具描述，决定 LLM 何时选此意图。
        parameters_schema: JSON Schema dict，描述 handler 的 params 结构。
                           至少是 {"type": "object", "properties": {}, "required": []}.
        handler:           (params: dict, confirmed: bool) -> ExecutionResult.
        dangerous:         True 时需二次确认（同 §6 whitelist.commands 的 dangerous 语义）。
    """

    intent_name: str
    description: str
    parameters_schema: dict
    handler: Callable[[dict, bool], ExecutionResult]
    dangerous: bool = False
