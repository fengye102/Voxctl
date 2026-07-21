"""TTS 模块入口。"""

from tts.tts_base import (
    BaseTTSEngine,
    TTSError,
    TTSSynthesizeError,
    TTSDecodeError,
    TTSRemoteError,
    build_tts_engine,
)
from tts.edge_tts_engine import (
    TTSEngine,
    format_result,
)

__all__ = [
    "BaseTTSEngine",
    "build_tts_engine",
    "TTSEngine",
    "TTSError",
    "TTSSynthesizeError",
    "TTSDecodeError",
    "TTSRemoteError",
    "format_result",
]
