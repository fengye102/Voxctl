"""指令数据结构定义。

所有意图类型集中定义于此。`build_intent_schema()` 生成给 LLM 的工具规格列表，
`validate_intent()` 把 LLM 返回的原始 dict 校验为具体的 Pydantic 模型。

注意：discriminator 字段 `intent` 用 `Literal[str]` 而不是 `Literal[IntentType.X]`，
规避 pydantic v2 在 Literal[Enum] 场景下的边界行为。IntentType 保留供外部代码
作常量引用（IntentType 是 str Enum，等值比较对 str 依然生效）。
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, List, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter


class IntentType(str, Enum):
    """意图分类常量。业务层用这个做等值比较，schema 内部用字符串字面量做 discriminator。"""

    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    QUERY_SYSTEM = "query_system"
    OPEN_FILE = "open_file"
    RUN_COMMAND = "run_command"
    EXIT_PROGRAM = "exit_program"
    CONFIRM = "confirm"
    CANCEL = "cancel"
    UNKNOWN = "unknown"


# --- 各意图对应的 pydantic 模型 ------------------------------------------------

class OpenAppIntent(BaseModel):
    intent: Literal["open_app"] = "open_app"
    app_name: str = Field(
        ...,
        description="用户想打开的应用名称。优先照抄用户说出的名字（如 微信、网易云音乐、chrome），"
        "不要翻译或改写；名称到实际程序的匹配由执行层完成（§6 修订 15）",
    )


class CloseAppIntent(BaseModel):
    intent: Literal["close_app"] = "close_app"
    app_name: str = Field(..., description="要关闭的应用别名")


class QuerySystemIntent(BaseModel):
    intent: Literal["query_system"] = "query_system"
    resource: Literal["memory", "disk", "cpu"] = Field(
        ..., description="要查询的资源类型"
    )
    disk_drive: Optional[str] = Field(
        None,
        description="仅 resource=disk 时使用，盘符字母如 C、D；不填代表查询全部盘",
    )


class OpenFileIntent(BaseModel):
    intent: Literal["open_file"] = "open_file"
    path: str = Field(
        ...,
        description="文件路径。可以是自然语言描述如 '桌面上的 test.txt'，也可以是完整路径；"
        "路径解析交给执行层，本层原样透传",
    )


class RunCommandIntent(BaseModel):
    intent: Literal["run_command"] = "run_command"
    command_name: str = Field(
        ...,
        description="白名单命令名称，需与 config.whitelist.commands 中的键一致",
    )
    args: List[str] = Field(default_factory=list, description="命令参数列表")


class ExitProgramIntent(BaseModel):
    """退出语音助手本身。CLAUDE.md 第 2 节：语音退出走正常意图解析→执行流程。"""

    intent: Literal["exit_program"] = "exit_program"


class ConfirmIntent(BaseModel):
    """对二次确认问题的肯定回答（是/确定/对/好）。业务层根据上下文决定是否消费。"""

    intent: Literal["confirm"] = "confirm"


class CancelIntent(BaseModel):
    """对二次确认问题的否定回答（不/取消/算了）。"""

    intent: Literal["cancel"] = "cancel"


class UnknownIntent(BaseModel):
    """无法路由到任何明确意图。CLAUDE.md 第 4/5 节：不猜测，宁可 unknown。"""

    intent: Literal["unknown"] = "unknown"
    reason: Optional[str] = Field(None, description="判定为 unknown 的简要原因，可选")


class PluginIntent(BaseModel):
    """插件注册的自定义意图（修订 42）。intent 字段 = 插件的 intent_name。"""

    intent: str
    params: dict = Field(default_factory=dict)


# --- 判别联合类型 --------------------------------------------------------------

ParsedIntent = Annotated[
    Union[
        OpenAppIntent,
        CloseAppIntent,
        QuerySystemIntent,
        OpenFileIntent,
        RunCommandIntent,
        ExitProgramIntent,
        ConfirmIntent,
        CancelIntent,
        UnknownIntent,
    ],
    Field(discriminator="intent"),
]

# AnyIntent（修订 47 C2）：ParsedIntent + PluginIntent 的联合，用于 parse() 返回类型注解。
# PluginIntent 的 intent 字段是动态的（插件 intent_name），不能放进带 discriminator
# 的 ParsedIntent 联合；intent_parser.parse() 在 validate_intent 前拦截插件意图名
# 构造 PluginIntent，不走 _ADAPTER 校验。原 parse() -> ParsedIntent 注解与实际行为
# 矛盾（可能返回 PluginIntent），此别名让类型注解诚实。
AnyIntent = Union[ParsedIntent, PluginIntent]

_ADAPTER: TypeAdapter[ParsedIntent] = TypeAdapter(ParsedIntent)


def validate_intent(raw: dict) -> ParsedIntent:
    """把 LLM 返回的原始 dict 校验为具体 Pydantic 模型；失败抛 pydantic.ValidationError。"""
    return _ADAPTER.validate_python(raw)


# --- 意图工具规格（给 LLMClient 用） -------------------------------------------
#
# 每个意图 = 一个 tool / function，LLM 通过挑选工具完成意图分类；
# 强制 tool_choice = any/required，即使命中不了任何真实意图，也必须选 unknown。
# 描述用中文写清楚使用场景，让 LLM 在语义边界上判断更稳。

_INTENT_TOOL_SPECS: List[dict] = [
    {
        "name": "open_app",
        "description": "打开一个应用程序。用户说'打开 XX'、'启动 XX'、'开一下 XX' 等意图时选此工具。"
        "app_name 直接填用户说的应用名（如 微信、网易云音乐、chrome、记事本），"
        "不需要是完整可执行文件路径，也不要自行翻译成英文；名称匹配由执行层完成。",
        "model": OpenAppIntent,
    },
    {
        "name": "close_app",
        "description": "关闭一个应用程序。用户说'关掉 XX'、'关闭 XX'、'退出 XX'（指 XX 应用而非本助手）等时选此工具。",
        "model": CloseAppIntent,
    },
    {
        "name": "query_system",
        "description": "查询系统资源使用情况：内存、磁盘、CPU。"
        "用户问 '内存剩多少'、'C 盘还有多少空间'、'CPU 占用率' 等时选此工具。"
        "resource 只能是 memory / disk / cpu 三选一；如果问磁盘且提到具体盘符，"
        "把盘符字母填入 disk_drive。",
        "model": QuerySystemIntent,
    },
    {
        "name": "open_file",
        "description": "打开一个具体文件（不是启动应用）。"
        "用户说'打开桌面上的 test.txt'、'开一下 xxx 文档'时选此工具。"
        "path 原样透传用户描述即可，不需要展开成绝对路径。",
        "model": OpenFileIntent,
    },
    {
        "name": "run_command",
        "description": "执行白名单中预先注册的自定义命令。"
        "只有当用户明确说的短语能对应到某个已知命令别名时才选此工具；模糊的、开放式的请求用 unknown。",
        "model": RunCommandIntent,
    },
    {
        "name": "exit_program",
        "description": "退出本语音助手程序。用户说'退出程序'、'关闭助手'、'再见'、'退出' 等明确终止本助手意图时选此工具。"
        "注意区别于 close_app（关闭其他应用）。",
        "model": ExitProgramIntent,
    },
    {
        "name": "confirm",
        "description": "对二次确认问题的肯定回答，如'是'、'对'、'确定'、'好的'。孤立场景下也可以选。",
        "model": ConfirmIntent,
    },
    {
        "name": "cancel",
        "description": "对二次确认问题的否定回答，如'不'、'取消'、'算了'、'不用了'。",
        "model": CancelIntent,
    },
    {
        "name": "unknown",
        "description": "以上任何工具都不能明确对应用户意图时使用。"
        "宁可返回 unknown，不要为了填一个工具而猜测。reason 可以简要说明原因。",
        "model": UnknownIntent,
    },
]


def build_intent_schema(plugin_tool_specs: Optional[List[dict]] = None) -> dict:
    """生成给 LLMClient 使用的意图工具规格。

    返回结构：
        {
            "intents": [
                {"name": "open_app", "description": "...", "parameters": {json schema}},
                ...
            ]
        }
    各 provider 内部将此结构转为自己协议要求的格式（Anthropic tool 或 OpenAI function）。

    Args:
        plugin_tool_specs: 插件注册的额外 tool specs（来自 PluginRegistry.tool_specs()）。
            每项格式与内置 spec 的输出一致：{"name", "description", "parameters"}。
    """
    intents: List[dict] = []
    for spec in _INTENT_TOOL_SPECS:
        params_schema: dict = spec["model"].model_json_schema()
        _strip_intent_field(params_schema)
        _ensure_object_schema(params_schema)
        intents.append(
            {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": params_schema,
            }
        )
    if plugin_tool_specs:
        intents.extend(plugin_tool_specs)
    return {"intents": intents}


def _strip_intent_field(schema_dict: Any) -> None:
    """从 pydantic 生成的 JSON schema 中移除 intent 字段——它由工具名承载，不需 LLM 再填一次。"""
    if not isinstance(schema_dict, dict):
        return
    props = schema_dict.get("properties")
    if isinstance(props, dict) and "intent" in props:
        del props["intent"]
    required = schema_dict.get("required")
    if isinstance(required, list) and "intent" in required:
        required.remove("intent")
    # 深入 $defs，保持一致（一般不会有 intent 出现在 $defs 里，保守处理）
    for def_schema in schema_dict.get("$defs", {}).values():
        _strip_intent_field(def_schema)


def _ensure_object_schema(schema_dict: dict) -> None:
    """保证参数 schema 是一个合法的 JSON schema object，避免空 tool 的 schema 缺字段。"""
    schema_dict.setdefault("type", "object")
    schema_dict.setdefault("properties", {})
    schema_dict.setdefault("required", [])


def safe_model_dump(intent: ParsedIntent) -> dict:
    """安全地把 intent 转 dict（pydantic 模型用 model_dump，其他返回空）。"""
    if hasattr(intent, "model_dump"):
        try:
            return intent.model_dump()
        except Exception:  # noqa: BLE001
            return {}
    return {}
