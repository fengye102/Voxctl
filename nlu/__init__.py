"""NLU 层：把 ASR 转出的文本解析为结构化意图。

对外暴露：
    - IntentParser（业务层入口）
    - schema 里的意图模型和 `validate_intent()`（供业务层做类型判断）
    - LLMError 系列异常（供业务层统一 except）
"""

from nlu.intent_parser import IntentParser, is_fallback_unknown
from nlu.llm_client import (
    LLMAPIError,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from nlu.schema import (
    IntentType,
    ParsedIntent,
    build_intent_schema,
    validate_intent,
)

__all__ = [
    "IntentParser",
    "IntentType",
    "ParsedIntent",
    "LLMClient",
    "LLMError",
    "LLMTimeoutError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMAPIError",
    "LLMParseError",
    "build_intent_schema",
    "validate_intent",
]
