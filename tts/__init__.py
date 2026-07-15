"""TTS 模块入口。"""

from tts.edge_tts_engine import (
    TTSError,
    TTSSynthesizeError,
    TTSDecodeError,
    TTSEngine,
    format_result,
)

__all__ = [
    "TTSEngine",
    "TTSError",
    "TTSSynthesizeError",
    "TTSDecodeError",
    "format_result",
]
