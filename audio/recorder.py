"""录音模块：基于 sounddevice 的持续音频采集。

职责：
  - 列出并打印所有可用的输入设备（CLAUDE.md §8.2：不依赖默认设备）
  - 以 CHUNK_SAMPLES 为单位持续采集麦克风 PCM，投入 asyncio.Queue
  - sounddevice 回调运行在 C 回调线程，通过 run_coroutine_threadsafe 桥接 asyncio

常量：
  SAMPLE_RATE    = 16000   silero-vad 要求的采样率
  CHUNK_SAMPLES  = 512     silero-vad v5 在 16kHz 下要求的最小帧大小（32ms）

用法（asyncio 上下文）：
  queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
  recorder = AudioRecorder(device_index=1)
  recorder.start(queue, asyncio.get_event_loop())
  chunk = await queue.get()   # shape=(512,), dtype=float32, range≈[-1, 1]
  recorder.stop()
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import numpy as np
import sounddevice as sd

_LOG = logging.getLogger(__name__)

SAMPLE_RATE = 16000      # Hz，与 silero-vad 要求对齐
CHUNK_SAMPLES = 512      # 每帧样本数 = 32ms @ 16kHz（silero-vad v5 要求）


class AudioRecorderError(Exception):
    """录音相关错误的基类。"""


class DeviceNotFoundError(AudioRecorderError):
    """指定的设备索引不存在或不支持录音。"""


class AudioRecorder:
    """sounddevice 录音封装，持续采集 → asyncio Queue。

    线程安全说明：
        sounddevice 的音频回调运行在单独的实时线程。
        _callback 只通过 asyncio.run_coroutine_threadsafe 向 Queue 投递数据，
        不直接访问 asyncio 对象，避免竞态。
    """

    def __init__(
        self,
        device_index: Optional[int] = None,
        sample_rate: int = SAMPLE_RATE,
        chunk_samples: int = CHUNK_SAMPLES,
    ) -> None:
        """
        Args:
            device_index: sounddevice 设备索引；None 则用系统默认输入设备。
                          首次运行建议先调用 list_devices() 查看可用设备，
                          再在 config.yaml 里写死索引（CLAUDE.md §8.2）。
            sample_rate:  采样率，必须与 VADProcessor 保持一致，默认 16000。
            chunk_samples: 每次回调投递的样本数，必须与 silero-vad 帧大小一致，默认 512。
        """
        self._device = device_index
        self._sample_rate = sample_rate
        self._chunk_samples = chunk_samples

        self._stream: Optional[sd.InputStream] = None
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def start(
        self,
        queue: "asyncio.Queue[np.ndarray]",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """打开音频流，开始向 queue 投递 PCM 块。

        Raises:
            DeviceNotFoundError: 设备索引无效。
            AudioRecorderError: sounddevice 打开失败。
        """
        if self._stream is not None:
            raise AudioRecorderError("recorder already started; call stop() first")

        self._queue = queue
        self._loop = loop

        try:
            self._stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self._chunk_samples,
                device=self._device,
                callback=self._callback,
                # latency='low' 减少回调延迟，避免 VAD 判断滞后
                latency="low",
            )
            self._stream.start()
        except sd.PortAudioError as exc:
            self._stream = None
            if "Invalid device" in str(exc) or "No Default Input" in str(exc):
                raise DeviceNotFoundError(
                    f"audio device {self._device!r} not found or has no input channels; "
                    "run list_devices() to see available devices"
                ) from exc
            raise AudioRecorderError(str(exc)) from exc

        _LOG.info(
            "AudioRecorder started: device=%s, rate=%d, chunk=%d samples (%.0fms)",
            self._device, self._sample_rate, self._chunk_samples,
            self._chunk_samples / self._sample_rate * 1000,
        )

    def stop(self) -> None:
        """停止录音，关闭音频流。可重复调用，幂等。"""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("error closing audio stream: %s", exc)
        finally:
            self._stream = None
        _LOG.info("AudioRecorder stopped")

    @property
    def is_running(self) -> bool:
        return self._stream is not None and self._stream.active

    # ------------------------------------------------------------------
    # 设备工具
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices() -> list[dict]:
        """返回所有可用输入设备的信息列表（只含有输入声道的设备）。

        每个 dict 包含：index, name, max_input_channels, default_samplerate。
        """
        result = []
        for dev in sd.query_devices():
            if dev["max_input_channels"] < 1:
                continue
            result.append(
                {
                    "index": dev["index"],
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "default_samplerate": int(dev["default_samplerate"]),
                }
            )
        return result

    @staticmethod
    def print_devices() -> None:
        """把输入设备列表打印到 stdout（CLAUDE.md §8.2：引导用户选择设备）。"""
        # Windows 控制台编码修正（CLAUDE.md §8.6）
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

        devices = AudioRecorder.list_devices()
        if not devices:
            print("[AudioRecorder] 未找到任何输入设备。请检查麦克风连接。")
            return
        print("[AudioRecorder] 可用输入设备：")
        for d in devices:
            print(
                f"  [{d['index']:2d}] {d['name']}  "
                f"(channels={d['channels']}, default_rate={d['default_samplerate']}Hz)"
            )
        default_idx = sd.default.device[0]
        print(f"  当前系统默认输入设备索引：{default_idx}")
        print("  在 config.yaml 的 audio.input_device_index 里填入你想用的索引。")

    # ------------------------------------------------------------------
    # 内部回调（运行在 sounddevice 的 C 回调线程，不能调 await）
    # ------------------------------------------------------------------

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            # input overflow 等状态在高 CPU 负载时会出现；只打日志，不抛异常。
            # 在 C 回调里抛异常会 crash sounddevice 的内部线程。
            _LOG.debug("sounddevice status: %s", status)

        # indata shape = (chunk_samples, 1)，取 mono 通道并复制（回调结束后 indata 内存会被回收）
        chunk = indata[:, 0].copy()

        if self._loop is not None and self._queue is not None:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(chunk), self._loop
            )
