"""播放模块：sounddevice 音频输出封装（CLAUDE.md §5 Step 5 实现）。

实现内容：
    - play(np.ndarray, sample_rate)：播放 float32 PCM（一次性，完整 PCM）
    - play_stream(AsyncIterator, sample_rate)：流式播放 PCM 块序列（边解码边放）
    - stop()：立即停止（Step 7 打断协调器调用）
    - is_playing 属性
    - add_stop_callback / remove_stop_callback：供 pyttsx3 本地 TTS 打断用

设计说明：
    1. play() 用 sd.play(blocking=False) + sd.wait()，一次性播放完整 PCM
    2. play_stream() 用 sd.OutputStream + stream.write()，逐块流式播放，
       首个 PCM 块到达即开始出声，大幅降低长文本首字延迟
    3. stop() 调 stream.abort()（流式）或 sd.stop()（一次性），立即终止播放，
       然后触发所有已注册的 stop callback（如 pyttsx3 本地 TTS 停止）
    4. Step 7 的 InterruptCoordinator 只需调 player.stop() 即可打断播报

CLAUDE.md §1（架构图）：播报通道按需运行，可随时被打断信号停止。
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import numpy as np
import sounddevice as sd

_LOG = logging.getLogger(__name__)


class AudioPlayerError(Exception):
    """播放模块错误基类。"""


class AudioPlayer:
    """音频播放封装（sounddevice）。线程安全：play/stop 可从不同协程调用。

    用法：
        player = AudioPlayer()
        await player.play(pcm_float32, sample_rate=24000)
        # 流式播放：
        await player.play_stream(pcm_chunks_async_iter, sample_rate=24000)
        # 打断：
        player.stop()
    """

    def __init__(
        self,
        device_index: Optional[int] = None,
        sample_rate: int = 24000,  # Edge TTS 默认 24kHz
    ) -> None:
        self._device = device_index
        self._default_sample_rate = sample_rate
        self._playing = False
        self._play_lock = asyncio.Lock()
        self._active_stream: Optional[sd.OutputStream] = None
        self._stop_callbacks: list = []

    # ------------------------------------------------------------------
    async def play(self, audio: np.ndarray, sample_rate: Optional[int] = None) -> None:
        """播放一段 PCM 音频，await 直到播放完成或被 stop 打断。

        Args:
            audio: float32 numpy 数组，mono，值域 [-1, 1]。
            sample_rate: 采样率，None 用构造时的默认值。
        """
        if audio is None or audio.size == 0:
            _LOG.debug("[AudioPlayer] empty audio, skip")
            return

        sr = sample_rate or self._default_sample_rate
        # 确保 float32 + contiguous，sounddevice 要求
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        audio = np.ascontiguousarray(audio)

        # 同一时刻只允许一个 play 在跑，避免叠加播放
        async with self._play_lock:
            self._playing = True
            try:
                sd.play(audio, samplerate=sr, device=self._device, blocking=False)
                _LOG.debug("[AudioPlayer] playing %.2fs @ %dHz", len(audio) / sr, sr)
                # sd.wait() 阻塞直到播放完成；stop() 调 sd.stop() 会让它返回
                await asyncio.to_thread(sd.wait)
            except sd.PortAudioError as exc:
                _LOG.warning("[AudioPlayer] sounddevice error: %s", exc)
                raise AudioPlayerError(f"播放失败: {exc}") from exc
            finally:
                self._playing = False

    # ------------------------------------------------------------------
    async def play_stream(
        self,
        pcm_iter: AsyncIterator[np.ndarray],
        sample_rate: Optional[int] = None,
    ) -> None:
        """流式播放 PCM 块序列。每个 chunk 到达后立即送入 OutputStream。

        与 play() 的区别：play() 需要完整 PCM 才能开始播放；play_stream()
        在首个 chunk 到达时即开始出声，适合边解码边播放场景。

        Args:
            pcm_iter: 异步迭代器，yield float32 mono PCM numpy 数组。
                      迭代结束 = 播放自然结束。
            sample_rate: 采样率，None 用构造时的默认值。

        stop() 可随时中断：abort 底层 stream，write() 抛 PortAudioError，
        play_stream 捕获后安静退出。
        """
        sr = sample_rate or self._default_sample_rate

        async with self._play_lock:
            self._playing = True
            stream = sd.OutputStream(
                samplerate=sr, channels=1, dtype="float32", device=self._device,
            )
            self._active_stream = stream
            try:
                stream.start()
                _LOG.debug("[AudioPlayer] stream started @ %dHz", sr)
                async for chunk in pcm_iter:
                    if not self._playing:
                        break
                    if chunk.dtype != np.float32:
                        chunk = chunk.astype(np.float32)
                    chunk = np.ascontiguousarray(chunk)
                    if chunk.ndim == 1:
                        chunk = chunk.reshape(-1, 1)
                    await asyncio.to_thread(stream.write, chunk)
                # Flush remaining buffered samples
                if self._playing:
                    await asyncio.to_thread(stream.stop)
            except sd.PortAudioError as exc:
                if self._playing:
                    _LOG.warning("[AudioPlayer] stream playback error: %s", exc)
                    raise AudioPlayerError(
                        f"streaming playback failed: {exc}"
                    ) from exc
                # If not playing, the error is from stop() -> abort(), expected
            finally:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
                self._active_stream = None
                self._playing = False

    # ------------------------------------------------------------------
    def add_stop_callback(self, callback) -> None:
        """Register a callback to be invoked when stop() is called.

        Used by TTSEngine to stop pyttsx3 local fallback when the interrupt
        coordinator triggers player.stop() during TTS playback.
        """
        self._stop_callbacks.append(callback)

    def remove_stop_callback(self, callback) -> None:
        """Unregister a previously added stop callback."""
        try:
            self._stop_callbacks.remove(callback)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """立即停止播放（打断信号触发，CLAUDE.md §1 架构图）。

        同时处理两种播放模式：
        - 流式（play_stream）：abort OutputStream，让 write() 抛异常返回
        - 一次性（play）：sd.stop() 终止 sd.play()，让 sd.wait() 返回

        最后触发所有已注册的 stop callback（如 pyttsx3 本地 TTS 停止）。
        """
        if self._playing:
            _LOG.info("[AudioPlayer] stop requested")
        self._playing = False
        # Stop streaming OutputStream if active
        if self._active_stream is not None:
            try:
                self._active_stream.abort()
            except Exception:  # noqa: BLE001
                pass
        # Also stop sd.play() streams (one-shot play via play())
        # sd.stop() 一般不抛，但 PortAudio 未初始化/设备异常时可能抛 PortAudioError；
        # 打断路径绝不能再反噬主循环（§8.11 失败不崩主循环同样适用于此）
        try:
            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        # Fire stop callbacks (e.g. pyttsx3 engine.stop())
        for cb in self._stop_callbacks:
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    @property
    def is_playing(self) -> bool:
        return self._playing
