"""ASR 层：把音频片段转成中文文本。

对外暴露：
    WhisperEngine   — faster-whisper 封装，输入 numpy 音频，输出转录文本
    ASRResult       — 转录结果（文本 + 置信度元信息）
    ASRError        — ASR 模块错误基类
"""

from asr.whisper_engine import ASRError, ASRResult, WhisperEngine

__all__ = ["WhisperEngine", "ASRResult", "ASRError"]
