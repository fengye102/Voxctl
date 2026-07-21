"""ASR 层：把音频片段转成中文文本。

对外暴露：
    ASREngine       — ASR 引擎抽象基类（修订 45）
    build_asr_engine — 按 config 的 provider 字段实例化具体引擎（local / openai）
    WhisperEngine   — 本地 faster-whisper 封装（默认 provider）
    ASRResult       — 转录结果（文本 + 置信度元信息）
    ASRError        — ASR 模块错误基类
    ASRRemoteError  — 云端 ASR 调用失败（修订 45）
"""

from asr.asr_engine import (
    ASREngine,
    ASRError,
    ASRRemoteError,
    ASRResult,
    ModelLoadError,
    TranscribeError,
    build_asr_engine,
)
from asr.whisper_engine import WhisperEngine

__all__ = [
    "ASREngine",
    "build_asr_engine",
    "WhisperEngine",
    "ASRResult",
    "ASRError",
    "ASRRemoteError",
    "ModelLoadError",
    "TranscribeError",
]
