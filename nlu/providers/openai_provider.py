"""OpenAI provider：走 Chat Completions + function calling 完成结构化意图解析。

CLAUDE.md 第 2.1 节：OpenAI 兼容端点（中转站）也走这条路。
第 5 节：只用协议自带的结构化输出能力（tool_choice=required + strict function schema），
不依赖 prompt 里"请输出 JSON"这类软约束。

注意：中转站对 function calling 的兼容度参差不齐（第 8.9 节），换 base_url 前
必须用一次性脚本单独验证 tool_choice=required 真的能触发 tool_calls 返回，否则
本 provider 会持续抛 LLMParseError（模型走了纯文本路径）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from nlu.llm_client import (
    LLMAPIError,
    LLMAuthError,
    LLMClient,
    LLMParseError,
    LLMRateLimitError,
    LLMTimeoutError,
)

_SYSTEM_PROMPT = (
    "你是本地语音助手的意图解析器。用户会说一句中文指令，你必须从提供的工具列表里"
    "选择恰好一个工具来表达用户意图。宁可选 unknown，不要为了填工具而猜测。"
    "永远只调用工具，不要输出自然语言。"
)


class OpenAIClient(LLMClient):
    """基于 openai SDK 的 provider 实现。

    错误码归一化（与 Anthropic 版本对齐）：
        - APITimeoutError → LLMTimeoutError
        - AuthenticationError / PermissionDeniedError → LLMAuthError
        - RateLimitError → LLMRateLimitError
        - 其余 APIConnectionError / APIError → LLMAPIError
        - 模型不 call tool、工具名未知、arguments 不是合法 JSON → LLMParseError
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        # 延迟 import：只有真的实例化本 provider 才把 SDK 拉进内存。
        import openai

        self._openai = openai
        self._model = model
        client_kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)

    # ------------------------------------------------------------------
    def parse_intent(self, text: str, schema: dict) -> dict:
        tools = self._build_tools(schema)
        allowed_names = {t["function"]["name"] for t in tools}

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                tools=tools,
                # tool_choice=required 强制模型必须挑一个 function，杜绝纯文本回复。
                # 中转站不兼容此字段时，会在这一步或下面 _extract_tool_call 处暴露。
                tool_choice="required",
            )
        except self._openai.APITimeoutError as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except self._openai.AuthenticationError as exc:
            raise LLMAuthError(str(exc)) from exc
        except self._openai.PermissionDeniedError as exc:
            raise LLMAuthError(str(exc)) from exc
        except self._openai.RateLimitError as exc:
            raise LLMRateLimitError(str(exc)) from exc
        except (self._openai.APIConnectionError, self._openai.APIError) as exc:
            # 中转站错误码语义可能与官方不一致（第 8.9 节），最终都汇聚到 LLMAPIError。
            raise LLMAPIError(str(exc)) from exc

        return self._extract_tool_call(response, allowed_names)

    # ------------------------------------------------------------------
    @staticmethod
    def _build_tools(schema: dict) -> list[dict]:
        """把统一 schema 转成 OpenAI function calling 工具定义。"""
        tools: list[dict] = []
        for intent in schema.get("intents", []):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": intent["name"],
                        "description": intent["description"],
                        # OpenAI 用 parameters 字段承载 JSON schema。
                        "parameters": intent["parameters"],
                    },
                }
            )
        return tools

    @staticmethod
    def _resolve_name(returned: str, allowed: set[str]) -> str | None:
        """把中转站改写过的工具名映射回原始名（与 AnthropicClient 同逻辑）。"""
        if returned in allowed:
            return returned
        def to_pascal(s: str) -> str:
            return "".join(w.capitalize() for w in s.split("_"))
        pascal_map = {to_pascal(n): n for n in allowed}
        candidate = returned
        if candidate.startswith("Compat"):
            candidate = candidate[6:]
        candidate = re.sub(r"[0-9a-f]{6}$", "", candidate)
        return pascal_map.get(candidate)

    @staticmethod
    def _extract_tool_call(response: Any, allowed_names: set[str]) -> dict:
        """从 OpenAI 响应里挑出第一个 tool_call，转成统一 dict。"""
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMParseError("no choices in OpenAI response")

        choice = choices[0]
        message = getattr(choice, "message", None)
        tool_calls = getattr(message, "tool_calls", None) if message else None
        if not tool_calls:
            finish_reason = getattr(choice, "finish_reason", "?")
            content = getattr(message, "content", None) if message else None
            raise LLMParseError(
                f"no tool_calls in OpenAI response; "
                f"finish_reason={finish_reason!r}, "
                f"message.content={content!r}"
            )

        first = tool_calls[0]
        fn = getattr(first, "function", None)
        raw_name = getattr(fn, "name", None) if fn else None
        name = OpenAIClient._resolve_name(raw_name, allowed_names)
        if name is None:
            raise LLMParseError(f"model returned unknown tool name: {raw_name!r}")

        raw_args = getattr(fn, "arguments", None)
        # OpenAI 的 arguments 一律是 JSON 字符串，即使参数对象是空的也是 "{}"，
        # 中转站偶尔返回 None 或空串，做兼容处理。
        if raw_args in (None, ""):
            parsed_args: dict = {}
        elif isinstance(raw_args, dict):
            # 极少数中转站直接返回 dict，跟着容忍。
            parsed_args = raw_args
        else:
            try:
                parsed_args = json.loads(raw_args)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LLMParseError(
                    f"tool_call.arguments is not valid JSON: {raw_args!r}"
                ) from exc
            if not isinstance(parsed_args, dict):
                raise LLMParseError(
                    f"tool_call.arguments decoded to non-object: {type(parsed_args).__name__}"
                )

        result = dict(parsed_args)
        # intent 字段由工具名承载，回填给业务层。
        result["intent"] = name
        return result
