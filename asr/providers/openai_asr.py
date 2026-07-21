"""云端 ASR provider：OpenAI 兼容 /v1/audio/transcriptions（修订 45）。

CLAUDE.md §2.2：Anthropic 协议无音频端点，云端 ASR 只做 OpenAI 格式，支持自定义 base_url。
复用意图解析层已装的 openai SDK（无新依赖）。

流程：
  numpy float32 mono [-1,1] → 内存 WAV（stdlib wave，16-bit PCM）
  → client.audio.transcriptions.create(model, file, language, response_format="verbose_json")
  → 有 segment 置信度（no_speech_prob/avg_logprob）就沿用同一套过滤，无则按空文本判 is_empty
  → opencc t2s 繁→简（与本地 Whisper 一致）

已知退化（§8.12）：中转站可能只返回纯文本或 verbose_json 缺置信度字段，此时无法做
no_speech_prob/avg_logprob 过滤，静音/噪声可能漏过滤——要可靠过滤请用本地 provider。
"""

from __future__ import annotations

import io
import logging
import time
import wave
from typing import Optional

import numpy as np

from asr.asr_engine import (
    ASREngine,
    ASRRemoteError,
    ASRResult,
    _DEFAULT_LOG_PROB_THRESHOLD,
    _DEFAULT_NO_SPEECH_THRESHOLD,
    apply_opencc,
    finalize_result,
    load_opencc_converter,
)

_LOG = logging.getLogger(__name__)

_DEFAULT_MODEL = "whisper-1"
_DEFAULT_TIMEOUT = 30.0


class OpenAIASREngine(ASREngine):
    """OpenAI 兼容云端 ASR 引擎。

    错误归一化：SDK 各类异常 → ASRRemoteError（消息带分类前缀，屏蔽厂商差异）。
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        api_key: str = "",
        base_url: Optional[str] = None,
        language: str = "zh",
        timeout: float = _DEFAULT_TIMEOUT,
        no_speech_threshold: float = _DEFAULT_NO_SPEECH_THRESHOLD,
        log_prob_threshold: float = _DEFAULT_LOG_PROB_THRESHOLD,
        opencc_enabled: bool = True,
    ) -> None:
        if not api_key:
            raise ASRRemoteError(
                "云端 ASR 缺少 API key；请设置 config 里 asr.api_key_env 指向的环境变量"
            )
        # 延迟 import：只有真的用云端 ASR 才把 SDK 拉进内存。
        import openai

        self._openai = openai
        self._model = model
        self._language = language
        self._no_speech_threshold = no_speech_threshold
        self._log_prob_threshold = log_prob_threshold
        self._opencc_enabled = opencc_enabled
        self._converter = load_opencc_converter(opencc_enabled)

        client_kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)

    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, asr_config: dict) -> "OpenAIASREngine":
        """从 config.yaml 的 asr 块构造（provider=openai 时）。

        api_key 从 asr.api_key_env 指向的环境变量读取（§6 铁律：不进 config 明文）。
        """
        import os

        api_key_env = str(asr_config.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env, "")
        base_url = str(asr_config.get("base_url", "") or "") or None
        return cls(
            model=str(asr_config.get("model", _DEFAULT_MODEL)),
            api_key=api_key,
            base_url=base_url,
            language=str(asr_config.get("language", "zh")),
            timeout=float(asr_config.get("timeout", _DEFAULT_TIMEOUT)),
            no_speech_threshold=float(
                asr_config.get("no_speech_threshold", _DEFAULT_NO_SPEECH_THRESHOLD)
            ),
            log_prob_threshold=float(
                asr_config.get("log_prob_threshold", _DEFAULT_LOG_PROB_THRESHOLD)
            ),
            opencc_enabled=bool(asr_config.get("opencc_enabled", True)),
        )

    # ------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> ASRResult:
        duration_s = len(audio) / sample_rate if sample_rate else 0.0
        wav_bytes = _encode_wav(audio, sample_rate)

        t0 = time.monotonic()
        try:
            resp = self._client.audio.transcriptions.create(
                model=self._model,
                file=("audio.wav", wav_bytes, "audio/wav"),
                language=self._language,
                response_format="verbose_json",
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

        text, segments, no_speech_prob, avg_logprob, has_confidence = _parse_response(resp)
        text = apply_opencc(text, self._converter) if self._opencc_enabled else text

        _LOG.debug(
            "cloud ASR: text=%r has_conf=%s no_speech_prob=%.3f avg_logprob=%.3f elapsed=%.2fs",
            text, has_confidence, no_speech_prob, avg_logprob, elapsed_s,
        )

        return finalize_result(
            text,
            no_speech_prob=no_speech_prob,
            avg_logprob=avg_logprob,
            duration_s=duration_s,
            elapsed_s=elapsed_s,
            segments=segments,
            no_speech_threshold=self._no_speech_threshold,
            log_prob_threshold=self._log_prob_threshold,
            has_confidence=has_confidence,
        )

    def warmup(self) -> None:
        # 云端无模型可预热；建连成本低，跳过（避免无谓的一次计费调用）。
        return None


# ---------------------------------------------------------------------------
# helper：编码 / 解析（模块级纯函数，便于测试）
# ---------------------------------------------------------------------------

def _encode_wav(audio: np.ndarray, sample_rate: int) -> bytes:
    """float32 mono [-1,1] → 16-bit PCM WAV 字节（stdlib wave，无第三方依赖）。"""
    arr = np.asarray(audio, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm16 = (arr * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _to_dict(resp) -> dict:
    """把 SDK 响应对象归一成 dict（不同 SDK 版本/中转站返回形态不一）。"""
    if isinstance(resp, dict):
        return resp
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:  # noqa: BLE001
                pass
    # 退而求其次：从对象属性拼一个最小 dict
    return {
        "text": getattr(resp, "text", "") or "",
        "segments": getattr(resp, "segments", None),
    }


def _parse_response(resp):
    """解析 transcriptions 响应，返回 (text, segments, no_speech_prob, avg_logprob, has_confidence)。

    verbose_json 带 segments 且含 no_speech_prob/avg_logprob 时 has_confidence=True，
    走与本地 Whisper 相同的过滤；否则退化为 has_confidence=False（§8.12）。
    """
    data = _to_dict(resp)
    text = (data.get("text") or "").strip()

    raw_segments = data.get("segments") or []
    segments_text: list[str] = []
    logprob_sum = 0.0
    logprob_count = 0
    no_speech_prob = 0.0
    saw_confidence = False

    for seg in raw_segments:
        seg_dict = seg if isinstance(seg, dict) else _to_dict(seg)
        seg_text = (seg_dict.get("text") or "").strip()
        if seg_text:
            segments_text.append(seg_text)
        if "avg_logprob" in seg_dict and seg_dict.get("avg_logprob") is not None:
            saw_confidence = True
            logprob_sum += float(seg_dict.get("avg_logprob") or 0.0)
            logprob_count += 1
        if "no_speech_prob" in seg_dict and seg_dict.get("no_speech_prob") is not None:
            saw_confidence = True
            nsp = float(seg_dict.get("no_speech_prob") or 0.0)
            if nsp > no_speech_prob:
                no_speech_prob = nsp

    avg_logprob = (logprob_sum / logprob_count) if logprob_count > 0 else 0.0
    return text, segments_text, no_speech_prob, avg_logprob, saw_confidence


__all__ = ["OpenAIASREngine"]
