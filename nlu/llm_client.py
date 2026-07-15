"""LLM 抽象基类与统一异常。

`intent_parser.py` 只依赖本模块，不 import 任何厂商 SDK。
每个 provider 负责把 `build_intent_schema()` 生成的统一 schema 转成自己协议的格式，
并把厂商特有的错误码归一化为下面几种自定义异常。

CLAUDE.md 第 2.1 节要求：屏蔽厂商差异；第 5 节要求：只用协议自带的结构化输出能力，
不依赖 prompt 里"请输出 JSON"这类软约束。
"""

from __future__ import annotations

import abc
from typing import Optional


class LLMError(Exception):
    """所有 LLM 相关错误的基类。业务层只需 except LLMError。"""


class LLMTimeoutError(LLMError):
    """请求超时（网络层或 SDK 层报告 timeout）。"""


class LLMAuthError(LLMError):
    """鉴权失败：API key 无效、被撤销、权限不足（HTTP 401/403 等）。"""


class LLMRateLimitError(LLMError):
    """被限流（HTTP 429）。业务层可选择重试或降级。"""


class LLMAPIError(LLMError):
    """其他所有 API 层错误：5xx、网络中断、中转站行为异常等。

    CLAUDE.md 第 8.9 节：中转站错误码语义不一定与官方一致，本层不做细分。
    """


class LLMParseError(LLMError):
    """LLM 返回了内容，但结构化解析失败：

    - 没有调用任何工具（模型选择输出纯文本）
    - 工具名不在允许列表内
    - 参数无法通过 pydantic 校验

    这类错误说明 LLM 或协议行为异常，重试通常无益，业务层应该走 unknown 兜底。
    """


class LLMClient(abc.ABC):
    """所有 provider 必须实现的最小接口。

    实现约定：
        - 无论用户输入什么，返回值必须是可被 `nlu.schema.validate_intent()` 校验通过的 dict
        - 解析不出意图时返回 {"intent": "unknown", "reason": "..."}，不抛 LLMParseError
        - 只有当协议层真的出错（模型不认工具、返回纯文本、参数校验失败）才抛 LLMParseError
        - 超时、鉴权、限流分别抛对应异常，其他 API 错误抛 LLMAPIError
    """

    @abc.abstractmethod
    def parse_intent(self, text: str, schema: dict) -> dict:
        """把用户文本解析为结构化意图 dict。

        Args:
            text: 用户说的话（ASR 转文字后的原始文本，可能含标点、语气词）
            schema: 由 `nlu.schema.build_intent_schema()` 生成的意图工具规格

        Returns:
            形如 {"intent": "open_app", "app_name": "chrome"} 的 dict，
            能被 `nlu.schema.validate_intent()` 校验通过。

        Raises:
            LLMTimeoutError, LLMAuthError, LLMRateLimitError, LLMAPIError, LLMParseError
        """
        raise NotImplementedError

    def close(self) -> None:
        """释放底层客户端资源。默认空实现，provider 需要时覆盖。"""
        return None


# --- provider 工厂 -------------------------------------------------------------
#
# `intent_parser.py` 只调 `build_client()`，不直接 import provider 模块——避免在只用
# 一个 provider 的场景下白导入另一个 SDK。真正的 import 延迟到工厂里做。

def build_client(
    provider: str,
    model: str,
    api_key: str,
    base_url: Optional[str] = None,
    timeout: float = 15.0,
) -> LLMClient:
    """按 provider 字段实例化对应的 LLMClient。

    Args:
        provider: "anthropic" 或 "openai"
        model: 模型标识，直接透传给对应 SDK，不做跨协议映射
        api_key: 已从环境变量解析出来的实际 key
        base_url: 中转站 URL，留空则走对应厂商官方端点
        timeout: 单次请求超时秒数

    Raises:
        ValueError: provider 不受支持
    """
    key = provider.strip().lower()
    if key == "anthropic":
        from nlu.providers.anthropic_provider import AnthropicClient

        return AnthropicClient(
            model=model, api_key=api_key, base_url=base_url, timeout=timeout
        )
    if key == "openai":
        from nlu.providers.openai_provider import OpenAIClient

        return OpenAIClient(
            model=model, api_key=api_key, base_url=base_url, timeout=timeout
        )
    raise ValueError(
        f"unsupported LLM provider: {provider!r} (only 'anthropic' or 'openai' allowed)"
    )
