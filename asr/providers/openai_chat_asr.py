"""云端 ASR provider：chat/completions `input_audio` 多模态格式（修订 73）。

CLAUDE.md §2.2：部分中转站的 ASR 模型不实现标准 `/v1/audio/transcriptions`（404），
只经 chat/completions 的 `input_audio` 多模态格式暴露（实测 2026-07-26，探针
`tests/tools/probe_cloud_asr.py` 可复现）。本 provider 对接该格式。

流程：
  numpy float32 mono [-1,1] → 内存 WAV（复用 openai_asr._encode_wav）→ base64
  → client.chat.completions.create(model, messages=[{user, content:[input_audio]}])
  → choices[0].message.content 即转录文本
  → opencc t2s 繁→简（与其它 ASR 路径一致）

已知退化（§8.12）：该格式协议上就不返回 no_speech_prob / avg_logprob，
恒按 has_confidence=False 走"仅空文本判 is_empty"，静音/噪声过滤只能依赖
上游 silero-vad + 唤醒词前缀两层兜底。language 字段该格式不支持传递，忽略。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Optional

import numpy as np

from asr.asr_engine import (
    ASREngine,
    ASRRemoteError,
    ASRResult,
    apply_opencc,
    finalize_result,
    load_opencc_converter,
)
from asr.providers.openai_asr import _encode_wav

_LOG = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


class OpenAIChatASREngine(ASREngine):
    """chat/completions `input_audio` 格式的云端 ASR 引擎。

    错误归一化与 OpenAIASREngine 一致：SDK 各类异常 → ASRRemoteError。
    """

    def __init__(
        self,
        model: str,
        api_key: str = "",
        base_url: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
        opencc_enabled: bool = True,
    ) -> None:
        if not model:
            raise ASRRemoteError(
                "openai_chat provider 必须显式指定 asr.model（中转站的 ASR 模型标识）"
            )
        if not api_key:
            raise ASRRemoteError(
                "云端 ASR 缺少 API key；请设置 config 里 asr.api_key_env 指向的环境变量"
            )
        # 延迟 import：只有真的用云端 ASR 才把 SDK 拉进内存。
        import openai

        self._openai = openai
        self._model = model
        self._opencc_enabled = opencc_enabled
        self._converter = load_opencc_converter(opencc_enabled)

        client_kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)

    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, asr_config: dict) -> "OpenAIChatASREngine":
        """从 config.yaml 的 asr 块构造（provider=openai_chat 时）。

        api_key 从 asr.api_key_env 指向的环境变量读取（§6 铁律：不进 config 明文）。
        """
        import os

        api_key_env = str(asr_config.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env, "")
        base_url = str(asr_config.get("base_url", "") or "") or None
        return cls(
            model=str(asr_config.get("model", "") or ""),
            api_key=api_key,
            base_url=base_url,
            timeout=float(asr_config.get("timeout", _DEFAULT_TIMEOUT)),
            opencc_enabled=bool(asr_config.get("opencc_enabled", True)),
        )

    # ------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> ASRResult:
        duration_s = len(audio) / sample_rate if sample_rate else 0.0
        wav_b64 = base64.b64encode(_encode_wav(audio, sample_rate)).decode("ascii")

        t0 = time.monotonic()
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": wav_b64, "format": "wav"},
                            },
                        ],
                    },
                ],
            )
        except self._openai.APITimeoutError as exc:
            raise ASRRemoteError(f"timeout: {exc}") from exc
        except self._openai.AuthenticationError as exc:
            raise ASRRemoteError(f"auth: {exc}") from exc
        except self._openai.PermissionDeniedError as exc:
            raise ASRRemoteError(f"auth: {exc}") from exc
        except self._openai.RateLimitError as exc:
            raise ASRRemoteError(f"rate_limit: {exc}") from exc
        except (self._openai.APIConnectionError, self._openai.APIError) as exc:
            raise ASRRemoteError(f"api: {exc}") from exc
        except Exception as exc:  # noqa: BLE001  中转站可能抛非 SDK 异常
            raise ASRRemoteError(f"unexpected: {exc}") from exc

        elapsed_s = time.monotonic() - t0

        text = _extract_text(resp)
        text = apply_opencc(text, self._converter) if self._opencc_enabled else text

        _LOG.debug(
            "chat ASR: text=%r elapsed=%.2fs (no confidence fields, §8.12)",
            text, elapsed_s,
        )

        # §8.12：该格式恒无置信度，只按空文本判 is_empty，不猜测、不伪造。
        return finalize_result(
            text,
            no_speech_prob=0.0,
            avg_logprob=0.0,
            duration_s=duration_s,
            elapsed_s=elapsed_s,
            has_confidence=False,
        )

    def warmup(self) -> None:
        # 云端无模型可预热；跳过（避免无谓的一次计费调用）。
        return None


# ---------------------------------------------------------------------------
# helper（模块级纯函数，便于测试）
# ---------------------------------------------------------------------------

def _extract_text(resp) -> str:
    """从 chat.completions 响应提取转录文本。

    正常形态：choices[0].message.content 为 str；个别中转可能返回分块 list
    （[{type: text, text: ...}]），拼接其中的 text 字段。取不到时返回空串
    （交给 finalize_result 判 is_empty），不抛异常。
    """
    try:
        choices = getattr(resp, "choices", None) or (
            resp.get("choices") if isinstance(resp, dict) else None
        )
        if not choices:
            return ""
        message = getattr(choices[0], "message", None) or (
            choices[0].get("message") if isinstance(choices[0], dict) else None
        )
        if message is None:
            return ""
        content = getattr(message, "content", None) or (
            message.get("content") if isinstance(message, dict) else None
        )
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                blk = block if isinstance(block, dict) else getattr(block, "__dict__", {})
                if blk.get("type") == "text" and blk.get("text"):
                    parts.append(str(blk["text"]))
            return "".join(parts).strip()
        return ""
    except Exception:  # noqa: BLE001  响应形态不可控，解析失败按空文本处理
        _LOG.warning("chat ASR: unexpected response shape, treating as empty", exc_info=True)
        return ""


__all__ = ["OpenAIChatASREngine"]
