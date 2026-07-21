"""TTS 共享音频工具（修订 45）。

MP3 解码 + PyAV 帧转换从 edge_tts_engine.py 抽出共享：
edge-tts 和云端 OpenAI TTS 都输出 MP3，都要解码成 PCM float32 走 sounddevice 播放。
"""

from __future__ import annotations

import io
import logging
from typing import Optional

import av
import numpy as np

from tts.tts_base import TTSDecodeError

_LOG = logging.getLogger(__name__)


def decode_mp3(mp3_bytes: bytes) -> tuple[np.ndarray, int]:
    """用 PyAV 解码 MP3 → PCM float32 mono + sample_rate。

    av（PyAV）是 FFmpeg 的 Python 绑定，faster-whisper 已依赖它，
    不需要额外装系统 ffmpeg。解码后统一转 float32 mono [-1, 1]。
    """
    try:
        container = av.open(io.BytesIO(mp3_bytes))
    except av.error.InvalidDataError as exc:
        raise TTSDecodeError(f"av.open 失败（非有效 MP3）: {exc}") from exc

    samples: list[np.ndarray] = []
    sample_rate: Optional[int] = None

    try:
        audio_stream = container.streams.audio[0]
        for frame in container.decode(audio_stream):
            if sample_rate is None:
                sample_rate = frame.sample_rate
            arr = frame_to_float32_mono(frame)
            if arr.size > 0:
                samples.append(arr)
    except av.error.FFmpegError as exc:
        raise TTSDecodeError(f"av 解码失败: {exc}") from exc
    finally:
        container.close()

    if not samples:
        raise TTSDecodeError("av 解码后无音频帧")

    pcm = np.concatenate(samples)
    # edge-tts / OpenAI TTS 默认 24kHz，兜底用 24000
    return pcm, sample_rate or 24000


def frame_to_float32_mono(frame: "av.AudioFrame") -> np.ndarray:
    """把 av.AudioFrame 转成 float32 mono numpy 数组。

    av 的 frame.to_ndarray() 返回格式取决于 frame.format：
        s16 交错: shape (samples, 1) int16
        s16p 平面: shape (channels, samples) int16
        fltp 平面: shape (channels, samples) float32
    统一转 float32 [-1, 1]，多通道 downmix 到 mono。
    """
    arr = frame.to_ndarray()

    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) / 32768.0
    elif arr.dtype == np.int32:
        arr = arr.astype(np.float32) / 2147483648.0
    elif arr.dtype == np.float32:
        pass
    else:
        arr = arr.astype(np.float32)

    # 多通道 → mono（取均值）
    # av 平面格式 shape=(channels, samples)，交错格式 shape=(samples, channels)
    if arr.ndim == 2:
        if arr.shape[0] <= arr.shape[1]:
            arr = arr.mean(axis=0)
        else:
            arr = arr.mean(axis=1)

    return np.ascontiguousarray(arr, dtype=np.float32)


__all__ = ["decode_mp3", "frame_to_float32_mono"]
