"""Anthropic provider：走 Messages API + tool use 完成结构化意图解析。

CLAUDE.md 第 5 节：LLM 在意图解析这一步只输出结构化数据，不生成自然语言，
用协议自带的工具调用能力做硬约束（tool_choice=any），不依赖 prompt 软约束。
"""

from __future__ import annotations

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

# 极简系统提示：只交代任务，格式约束由 tool_choice 兜底，不指望这段 prompt 起决定性作用。
_SYSTEM_PROMPT = (
    "你是本地语音助手的意图解析器。用户会说一句中文指令，你必须从提供的工具列表里"
    "选择恰好一个工具来表达用户意图。宁可选 unknown，不要为了填工具而猜测。"
    "永远只调用工具，不要输出自然语言。"
)


class AnthropicClient(LLMClient):
    """基于 anthropic SDK 的 provider 实现。

    错误码归一化：
        - APITimeoutError → LLMTimeoutError
        - AuthenticationError / PermissionDeniedError → LLMAuthError
        - RateLimitError → LLMRateLimitError
        - 其余 APIError / APIConnectionError → LLMAPIError
        - 模型不选工具、工具名未知 → LLMParseError
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        max_tokens: int = 1024,
    ) -> None:
        # 延迟 import：只有真的实例化本 provider 才把 SDK 拉进内存。
        import anthropic

        self._anthropic = anthropic
        self._model = model
        self._max_tokens = max_tokens
        client_kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**client_kwargs)

    # ------------------------------------------------------------------
    def parse_intent(self, text: str, schema: dict) -> dict:
        tools = self._build_tools(schema)
        allowed_names = {t["name"] for t in tools}

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM_PROMPT,
                # tool_choice=any 强制模型必须选一个工具，杜绝纯文本回复的可能。
                tool_choice={"type": "any"},
                tools=tools,
                messages=[{"role": "user", "content": text}],
            )
        except self._anthropic.APITimeoutError as exc:
            raise LLMTimeoutError(str(exc)) from exc
        except self._anthropic.AuthenticationError as exc:
            raise LLMAuthError(str(exc)) from exc
        except self._anthropic.PermissionDeniedError as exc:
            raise LLMAuthError(str(exc)) from exc
        except self._anthropic.RateLimitError as exc:
            raise LLMRateLimitError(str(exc)) from exc
        except (self._anthropic.APIConnectionError, self._anthropic.APIError) as exc:
            # 中转站可能抛出各种非标错误，最终都汇聚到 LLMAPIError。
            raise LLMAPIError(str(exc)) from exc

        return self._extract_tool_call(response, allowed_names)

    # ------------------------------------------------------------------
    @staticmethod
    def _build_tools(schema: dict) -> list[dict]:
        """把统一 schema 转成 Anthropic tool 定义。"""
        tools: list[dict] = []
        for intent in schema.get("intents", []):
            tools.append(
                {
                    "name": intent["name"],
                    "description": intent["description"],
                    # Anthropic 用 input_schema 而不是 parameters。
                    "input_schema": intent["parameters"],
                }
            )
        return tools

    @staticmethod
    def _resolve_name(returned: str, allowed: set[str]) -> str | None:
        """把中转站改写过的工具名映射回原始名。

        部分中转站会把工具名重写成 CompatXxxYyy{6hex} 格式（如 CompatOpenApp566c2f），
        这里做逆映射：strip 前缀 + strip 6位hex后缀 + PascalCase→snake_case 还原。
        如果返回的名字本来就合法，直接返回。
        """
        if returned in allowed:
            return returned
        # 构建 PascalCase → 原名 的查找表
        def to_pascal(s: str) -> str:
            return "".join(w.capitalize() for w in s.split("_"))
        pascal_map = {to_pascal(n): n for n in allowed}
        candidate = returned
        if candidate.startswith("Compat"):
            candidate = candidate[6:]
        # 去掉末尾 6 个小写十六进制字符
        candidate = re.sub(r"[0-9a-f]{6}$", "", candidate)
        return pascal_map.get(candidate)  # None if still unrecognized

    @staticmethod
    def _extract_tool_call(response: Any, allowed_names: set[str]) -> dict:
        """从 Anthropic 响应里挑出第一个 tool_use block，转成统一 dict。"""
        content_blocks = getattr(response, "content", []) or []
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type != "tool_use":
                continue
            raw_name = getattr(block, "name", None)
            name = AnthropicClient._resolve_name(raw_name, allowed_names)
            if name is None:
                raise LLMParseError(
                    f"model returned unknown tool name: {raw_name!r}"
                )
            raw_input = getattr(block, "input", None) or {}
            if not isinstance(raw_input, dict):
                raise LLMParseError(
                    f"tool_use.input is not a dict: {type(raw_input).__name__}"
                )
            result = dict(raw_input)
            # intent 字段由工具名承载，回填给业务层。
            result["intent"] = name
            return result

        # 把实际收到的 content 一起带出，方便诊断中转站返回了什么
        content_repr = []
        for block in content_blocks:
            btype = getattr(block, "type", "?")
            if btype == "text":
                content_repr.append(f"text={getattr(block, 'text', '')!r}")
            else:
                content_repr.append(f"type={btype!r}")
        stop_reason = getattr(response, "stop_reason", "?")
        raise LLMParseError(
            f"no tool_use block found in Anthropic response; "
            f"stop_reason={stop_reason!r}, "
            f"content=[{', '.join(content_repr) or 'empty'}]"
        )
