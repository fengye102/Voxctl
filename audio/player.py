"""播放模块：sounddevice 音频输出封装（CLAUDE.md §5 Step 5 实现）。

实现内容：
    - play(np.ndarray, sample_rate)：播放 float32 PCM，非阻塞启动 + await 完成等待
    - stop()：立即停止（Step 7 打断协调器调用，sd.stop() 会让 sd.wait() 返回）
    - is_playing 属性

设计说明：
    1. sounddevice.play(blocking=False) 立即返回，播放在 PortAudio 后台线程进行
    2. sd.wait() 阻塞直到播放完成；在 asyncio 里用 asyncio.to_thread 包装
    3. stop() 调 sd.stop()，会让正在阻塞的 sd.wait() 立即返回，从而结束 play()
    4. 这样 Step 7 的 InterruptCoordinator 只需调 player.stop() 即可打断播报

非流式说明（MVP）：
    Step 5 先做"合成完整 PCM → 一次性播放"。流式播放（边合成边放）留到
    Step 8 加固阶段或后续优化——MVP 的端到端延迟目标 5 秒（CLAUDE.md §7），
    短句合成 + 播放的延迟可接受。流式播放的复杂度（分块解码 + 播放队列
    同步）不在 Step 5 验收范围内。

CLAUDE.md §1（架构图）：播报通道按需运行，可随时被打断信号停止。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

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
    def stop(self) -> None:
        """立即停止播放（打断信号触发，CLAUDE.md §1 架构图）。

        sd.stop() 会终止后台播放线程，并让正在 await 的 sd.wait() 返回，
        从而让 play() 协程结束。这是 Step 7 打断能力的底层支撑。
        """
        if self._playing:
            _LOG.info("[AudioPlayer] stop requested")
        sd.stop()
        self._playing = False

    # ------------------------------------------------------------------
    @property
    def is_playing(self) -> bool:
        return self._playing
