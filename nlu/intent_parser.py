"""意图解析器：业务层唯一入口。

职责：
    1. 按 config 的 `provider` 字段实例化对应 LLMClient
    2. 调用 LLMClient.parse_intent() 拿原始 dict
    3. 用 pydantic 校验为具体意图模型
    4. 校验失败/协议异常 → 统一降级为 UnknownIntent（CLAUDE.md 第 4/5 节：不猜测）

本文件只 import `llm_client` 抽象层和 `schema`，不 import 任何厂商 SDK。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError

from nlu.llm_client import (
    LLMAPIError,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    LLMTimeoutError,
    build_client,
)
from nlu.schema import (
    AnyIntent,
    ParsedIntent,
    PluginIntent,
    UnknownIntent,
    build_intent_schema,
    validate_intent,
)

_LOG = logging.getLogger(__name__)


# 所有降级 UnknownIntent 的 reason 前缀。业务/测试层用 `is_fallback_unknown()`
# 区分 "LLM 真的选了 unknown 工具" 和 "调用/校验失败被兜底成 unknown"。
# 如果下面 parse() 里新增了降级路径，务必同步更新这个元组。
_FALLBACK_REASON_PREFIXES: tuple[str, ...] = (
    "empty input",
    "timeout: ",
    "rate limited: ",
    "parse error: ",
    "api error: ",
    "llm error: ",
    "validation failed: ",
)


def is_fallback_unknown(intent: ParsedIntent) -> bool:
    """判断一个 UnknownIntent 是否来自 IntentParser 的降级兜底路径。

    - True:  调用超时 / 协议异常 / 校验失败等被兜底成 unknown，不是 LLM 主动选择
    - False: LLM 显式调用了 unknown 工具，或 intent 根本不是 unknown

    Step 3 验收用这个来区分"LLM 判定意图属于指令集外"和"根本没调通 LLM"——
    后者 10 条都会走到同一个 unknown，很容易让预期是 unknown 的用例假通过。
    """
    if not isinstance(intent, UnknownIntent):
        return False
    reason = intent.reason or ""
    return any(reason.startswith(p) for p in _FALLBACK_REASON_PREFIXES)


@dataclass(frozen=True)
class LLMConfig:
    """LLM 配置。字段与 CLAUDE.md 第 3 节 config.yaml 的 `llm` 块保持一一对应。"""

    provider: str
    model: str
    api_key_env: str
    base_url: Optional[str] = None
    # CLAUDE.md §8.9：默认 30s（中转站比官方端点慢）；config.yaml 缺字段时也用 30s
    timeout: float = 30.0

    @classmethod
    def from_dict(cls, raw: dict) -> "LLMConfig":
        """从 config.yaml 加载出的 dict 构造。缺字段直接抛 KeyError，暴露配置错误。"""
        return cls(
            provider=raw["provider"],
            model=raw["model"],
            api_key_env=raw["api_key_env"],
            base_url=(raw.get("base_url") or None),
            timeout=float(raw.get("timeout", 30.0)),
        )


class IntentParser:
    """把用户文本解析为具体的意图模型。

    使用方式：
        parser = IntentParser.from_config(config["llm"])
        intent = parser.parse("打开浏览器")
        if isinstance(intent, OpenAppIntent):
            ...
    """

    def __init__(
        self,
        client: LLMClient,
        plugin_tool_specs: Optional[list[dict]] = None,
        plugin_intent_names: Optional[frozenset[str]] = None,
    ) -> None:
        self._client = client
        self._plugin_intent_names = plugin_intent_names or frozenset()
        self._schema = build_intent_schema(plugin_tool_specs)

    # ------------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        llm_config: dict,
        plugin_tool_specs: Optional[list[dict]] = None,
        plugin_intent_names: Optional[frozenset[str]] = None,
    ) -> "IntentParser":
        """按 config 字段构造。会从环境变量读 API key，未设置则抛 RuntimeError。"""
        cfg = LLMConfig.from_dict(llm_config)
        api_key = os.environ.get(cfg.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"environment variable {cfg.api_key_env!r} is empty or unset; "
                "set your API key there (see CLAUDE.md §2 Windows install)"
            )
        client = build_client(
            provider=cfg.provider,
            model=cfg.model,
            api_key=api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
        )
        return cls(client, plugin_tool_specs, plugin_intent_names)

    # ------------------------------------------------------------------
    def parse(self, text: str) -> AnyIntent:
        """解析用户文本为具体意图模型。

        对空输入直接返回 unknown，不去打 LLM——ASR 转出空文本是常见情况
        （CLAUDE.md 第 4 节：ASR 空文本走 unknown 兜底流程）。
        """
        stripped = (text or "").strip()
        if not stripped:
            return UnknownIntent(reason="empty input")

        try:
            raw = self._client.parse_intent(stripped, self._schema)
        except LLMTimeoutError as exc:
            _LOG.warning("LLM timeout: %s", exc)
            return UnknownIntent(reason=f"timeout: {exc}")
        except LLMAuthError:
            # 鉴权错误是配置问题，不该被吞成 unknown 让用户在语音层遇到"没听懂"。
            # 上抛给业务层，让 main.py 尽早暴露。
            raise
        except LLMRateLimitError as exc:
            _LOG.warning("LLM rate limited: %s", exc)
            return UnknownIntent(reason=f"rate limited: {exc}")
        except LLMParseError as exc:
            _LOG.warning("LLM parse error: %s", exc)
            return UnknownIntent(reason=f"parse error: {exc}")
        except LLMAPIError as exc:
            _LOG.warning("LLM API error: %s", exc)
            return UnknownIntent(reason=f"api error: {exc}")
        except LLMError as exc:
            # 兜底：新增的 LLMError 子类也走 unknown，不让上层崩。
            _LOG.warning("LLM generic error: %s", exc)
            return UnknownIntent(reason=f"llm error: {exc}")

        intent_name = raw.get("intent", "")
        if intent_name in self._plugin_intent_names:
            params = {k: v for k, v in raw.items() if k != "intent"}
            return PluginIntent(intent=intent_name, params=params)

        try:
            return validate_intent(raw)
        except ValidationError as exc:
            _LOG.warning("intent validation failed: raw=%r err=%s", raw, exc)
            return UnknownIntent(reason=f"validation failed: {exc.errors()[:1]}")

    # ------------------------------------------------------------------
    def close(self) -> None:
        """释放底层 client。asyncio 主循环退出时调用。"""
        self._client.close()
