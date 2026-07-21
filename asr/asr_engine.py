"""ASR 抽象层（修订 45）。

本模块是 ASR 的共享基础，与具体引擎（本地 faster-whisper / 云端 OpenAI）无关：
  - `ASREngine`：抽象基类，定义统一接口 transcribe / warmup / close
  - `ASRResult` 数据类 + 异常层级（ASRError / ModelLoadError / TranscribeError / ASRRemoteError）
  - `apply_opencc()`：繁→简共享 helper（本地和云端 Whisper 都可能吐繁体）
  - `finalize_result()`：置信度过滤共享 helper，两条路径拿到 text + 指标后统一判 is_empty
  - `build_asr_engine()`：按 config 的 `provider` 字段实例化具体引擎（CLAUDE.md §2.2）

设计动机（镜像 nlu/ 的多 provider 结构）：业务层（core/factory.py / event_loop.py）
只依赖 `ASREngine` 抽象接口和 `build_asr_engine` 工厂，不感知底层是本地模型还是云端 API。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

_LOG = logging.getLogger(__name__)

# 置信度过滤阈值（经验默认值，可通过 config 覆盖）
_DEFAULT_NO_SPEECH_THRESHOLD = 0.6   # no_speech_prob > 此值 → 视为无人声/噪声
_DEFAULT_LOG_PROB_THRESHOLD = -1.0   # avg_logprob < 此值 → 转录质量太差，丢弃


class ASRError(Exception):
    """ASR 模块错误基类。"""


class ModelLoadError(ASRError):
    """模型加载失败（网络问题、磁盘空间不足等）。"""


class TranscribeError(ASRError):
    """转录执行阶段的错误。"""


class ASRRemoteError(ASRError):
    """云端 ASR 调用失败（鉴权 / 超时 / HTTP 错误）。消息带分类信息。"""


@dataclass
class ASRResult:
    """转录结果。

    Attributes:
        text:            识别出的文本（空字符串表示静音/噪声/置信度不足）。
        is_empty:        True 表示没有有效语音内容（静音、噪声、置信度过滤）。
        no_speech_prob:  Whisper 认为这段音频不含人声的概率（0–1）。
        avg_logprob:     所有 token 的平均对数概率（越接近 0 越好，< -1 通常质量差）。
        duration_s:      音频时长（秒）。
        elapsed_s:       转录耗时（秒），用于性能监控。
        segments:        各 segment 的原始文本列表（调试用）。
    """
    text: str
    is_empty: bool
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0
    duration_s: float = 0.0
    elapsed_s: float = 0.0
    segments: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls, reason: str = "", **kwargs) -> "ASRResult":
        """构造一个空结果（静音/噪声/过滤）。"""
        _LOG.debug("ASR empty result: %s", reason)
        return cls(text="", is_empty=True, **kwargs)


class ASREngine(ABC):
    """ASR 引擎抽象基类。

    具体实现：
      - asr.whisper_engine.WhisperEngine（本地 faster-whisper，默认）
      - asr.providers.openai_asr.OpenAIASREngine（云端 OpenAI 兼容）
    """

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> ASRResult:
        """把音频片段转录为文本。audio: float32 mono [-1,1]。"""
        raise NotImplementedError

    def warmup(self) -> None:
        """提前加载模型 / 建立连接（可选）。默认 no-op。"""
        return None

    def close(self) -> None:
        """释放底层资源（可选）。默认 no-op。"""
        return None


# ---------------------------------------------------------------------------
# 共享 helper
# ---------------------------------------------------------------------------

def apply_opencc(text: str, converter) -> str:
    """繁→简转换（CLAUDE.md §2：Whisper 中文模型对大陆普通话有时输出繁体字符）。

    converter 为 None（opencc 未安装/未启用）时原样返回。
    """
    if not text or converter is None:
        return text
    converted = converter.convert(text)
    if converted != text:
        _LOG.debug("opencc t2s: %r -> %r", text, converted)
    return converted


def load_opencc_converter(enabled: bool = True):
    """加载 opencc t2s 转换器。未安装时静默降级返回 None（不阻断启动）。

    选 t2s 而不是 tw2sp：用户说大陆普通话，繁体是字符变体（開/来/览）而非台湾词汇，
    tw2sp 词组级替换会把「退出程序」错转成「退出进程」，t2s 只做字符 1:1 映射零副作用。
    """
    if not enabled:
        return None
    try:
        import opencc
        converter = opencc.OpenCC("t2s")
        _LOG.debug("opencc t2s converter loaded")
        return converter
    except ImportError:
        _LOG.warning(
            "opencc-python-reimplemented 未安装，繁简转换已禁用；"
            "如遇繁体输出请运行：pip install opencc-python-reimplemented"
        )
        return None


def finalize_result(
    text: str,
    *,
    no_speech_prob: float,
    avg_logprob: float,
    duration_s: float,
    elapsed_s: float,
    segments: Optional[list[str]] = None,
    no_speech_threshold: float = _DEFAULT_NO_SPEECH_THRESHOLD,
    log_prob_threshold: float = _DEFAULT_LOG_PROB_THRESHOLD,
    has_confidence: bool = True,
) -> ASRResult:
    """根据文本 + 置信度指标统一判 is_empty，构造 ASRResult。

    本地 faster-whisper 与云端 Whisper（verbose_json 有 segment 置信度时）共用同一套过滤。
    云端中转站不返回置信度（has_confidence=False）时，退化为仅按空文本判 is_empty
    （CLAUDE.md §8.12：无 no_speech_prob/avg_logprob，静音/噪声可能漏过滤）。
    """
    metrics = dict(
        no_speech_prob=no_speech_prob,
        avg_logprob=avg_logprob,
        duration_s=duration_s,
        elapsed_s=elapsed_s,
    )

    if has_confidence:
        if no_speech_prob > no_speech_threshold:
            return ASRResult.empty(
                reason=f"no_speech_prob={no_speech_prob:.3f} > threshold={no_speech_threshold}",
                **metrics,
            )
        if avg_logprob < log_prob_threshold and text:
            return ASRResult.empty(
                reason=f"avg_logprob={avg_logprob:.3f} < threshold={log_prob_threshold}",
                **metrics,
            )

    if not text:
        return ASRResult.empty(reason="empty transcription", **metrics)

    return ASRResult(
        text=text,
        is_empty=False,
        segments=segments or [],
        **metrics,
    )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_asr_engine(asr_cfg: dict) -> ASREngine:
    """按 config 的 asr.provider 字段实例化具体 ASR 引擎（CLAUDE.md §2.2）。

    provider 取值：
      - "local"（默认）：本地 faster-whisper（asr.whisper_engine.WhisperEngine）
      - "openai"：云端 OpenAI 兼容（asr.providers.openai_asr.OpenAIASREngine）
    未知 provider 抛 ValueError（暴露配置错误，不静默兜底）。
    """
    provider = str((asr_cfg or {}).get("provider", "local")).strip().lower()

    if provider == "local":
        from asr.whisper_engine import WhisperEngine
        return WhisperEngine.from_config(asr_cfg or {})

    if provider == "openai":
        from asr.providers.openai_asr import OpenAIASREngine
        return OpenAIASREngine.from_config(asr_cfg or {})

    raise ValueError(
        f"unsupported ASR provider: {provider!r} (expected 'local' or 'openai')"
    )


__all__ = [
    "ASREngine",
    "ASRResult",
    "ASRError",
    "ModelLoadError",
    "TranscribeError",
    "ASRRemoteError",
    "apply_opencc",
    "load_opencc_converter",
    "finalize_result",
    "build_asr_engine",
    "_DEFAULT_NO_SPEECH_THRESHOLD",
    "_DEFAULT_LOG_PROB_THRESHOLD",
]
