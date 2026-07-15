"""TTS 模块：Edge TTS 语音合成 + 模板化播报（CLAUDE.md §5 Step 5）。

设计要点：
    1. Edge TTS 输出 MP3 流，用 PyAV（av 库，faster-whisper 已依赖）解码成
       PCM float32，再走 audio/player.py 的 sounddevice 播放。
       不依赖系统 ffmpeg（av 自带解码器），避免环境配置负担。
    2. 播报文本用模板化拼接（CLAUDE.md §5 Step 5 强制）：把 ExecutionResult
       套固定模板生成中文播报文本，不让 LLM 生成自然语言。这样播报内容
       可控、可测试、不会语言跑偏。
    3. format_result() 是纯函数，不依赖网络/音频设备，可单独测试。
    4. speak() 是 async 的，因为 edge-tts 是异步库。播放部分用 asyncio.to_thread
       包装 sounddevice 的阻塞调用。

依赖链：
    edge-tts（网络）→ MP3 bytes → av.decode（CPU）→ PCM float32 → sounddevice（音频设备）
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import av
import edge_tts
import numpy as np

from executor import ExecutionResult
from audio.player import AudioPlayer

_LOG = logging.getLogger(__name__)


class TTSError(Exception):
    """TTS 模块错误基类。"""


class TTSSynthesizeError(TTSError):
    """Edge TTS 合成失败（网络/服务问题）。"""


class TTSDecodeError(TTSError):
    """MP3 解码失败（av/PyAV 问题）。"""


class TTSEngine:
    """Edge TTS 语音合成引擎。

    用法：
        player = AudioPlayer()
        engine = TTSEngine.from_config(config["tts"], player)
        await engine.speak("你好")
        await engine.speak_result(execution_result)

    播报文本生成（不联网，纯函数）：
        text = format_result(execution_result)
        # 或在 Step 6 主循环里单独调
    """

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        player: Optional[AudioPlayer] = None,
    ) -> None:
        self._voice = voice
        self._player = player

    @classmethod
    def from_config(cls, tts_cfg: dict, player: AudioPlayer) -> "TTSEngine":
        """从 config.yaml 的 tts 块构造。

        Args:
            tts_cfg: config["tts"] 字典，含 voice 字段
            player: AudioPlayer 实例（Step 5 实现，负责实际播放）
        """
        voice = tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural")
        return cls(voice=voice, player=player)

    # ------------------------------------------------------------------
    async def synthesize_pcm(self, text: str) -> tuple[np.ndarray, int]:
        """文本 → PCM float32 数组（mono，[-1, 1]）+ sample_rate。

        内部流程：edge-tts 合成 MP3 → av 解码 PCM。
        合成失败抛 TTSSynthesizeError，解码失败抛 TTSDecodeError。
        """
        if not text or not text.strip():
            return np.array([], dtype=np.float32), 24000

        mp3_bytes = await self._synthesize_mp3(text)
        if not mp3_bytes:
            raise TTSSynthesizeError(f"Edge TTS 返回空音频，文本: {text!r}")

        pcm, sample_rate = self._decode_mp3(mp3_bytes)
        return pcm, sample_rate

    # ------------------------------------------------------------------
    async def _synthesize_mp3(self, text: str) -> bytes:
        """调用 Edge TTS 合成 MP3，返回完整字节流。"""
        try:
            communicate = edge_tts.Communicate(text, self._voice)
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
        except Exception as exc:
            _LOG.warning("edge-tts synthesize failed for %r: %s", text, exc)
            raise TTSSynthesizeError(f"Edge TTS 合成失败: {exc}") from exc
        return b"".join(chunks)

    # ------------------------------------------------------------------
    @staticmethod
    def _decode_mp3(mp3_bytes: bytes) -> tuple[np.ndarray, int]:
        """用 PyAV 解码 MP3 → PCM float32 mono。

        av（PyAv）是 FFmpeg 的 Python 绑定，faster-whisper 已依赖它，
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
                arr = _frame_to_float32_mono(frame)
                if arr.size > 0:
                    samples.append(arr)
        except av.error.FFmpegError as exc:
            raise TTSDecodeError(f"av 解码失败: {exc}") from exc
        finally:
            container.close()

        if not samples:
            raise TTSDecodeError("av 解码后无音频帧")

        pcm = np.concatenate(samples)
        # edge-tts 默认 24kHz，兜底用 24000
        return pcm, sample_rate or 24000

    # ------------------------------------------------------------------
    async def speak(self, text: str) -> None:
        """合成文本并播放。如果没设置 player，只打日志（测试/调试用）。"""
        if self._player is None:
            _LOG.info("[TTS] no player set, skip playback: %r", text)
            return

        if not text or not text.strip():
            _LOG.debug("[TTS] empty text, skip")
            return

        try:
            pcm, sample_rate = await self.synthesize_pcm(text)
        except TTSError as exc:
            _LOG.warning("[TTS] synthesize failed, cannot speak: %s", exc)
            return

        if pcm.size == 0:
            _LOG.warning("[TTS] empty PCM for text: %r", text)
            return

        await self._player.play(pcm, sample_rate)

    # ------------------------------------------------------------------
    async def speak_result(self, result: ExecutionResult) -> None:
        """把 ExecutionResult 套模板生成播报文本，再合成播放。"""
        text = format_result(result)
        await self.speak(text)


# ===========================================================================
# 模板化播报文本生成（CLAUDE.md §5 Step 5 强制：模板拼接，不让 LLM 生成）
# ===========================================================================

def format_result(result: ExecutionResult) -> str:
    """把 ExecutionResult 转成中文播报文本（纯函数，不联网）。

    按 action_type + success + needs_confirmation 分发到对应模板。
    所有错误情况都生成可播报的中文文本，不静默失败（CLAUDE.md §4 边界情况）。
    """
    action = result.action_type

    if action == "open_app":
        return _format_open_app(result)
    if action == "close_app":
        return _format_close_app(result)
    if action == "query_system":
        return _format_query_system(result)
    if action == "open_file":
        return _format_open_file(result)
    if action == "run_command":
        return _format_run_command(result)
    if action == "exit_program":
        return "再见"
    if action == "unknown":
        return "不确定你想做什么"
    if action == "cancel":
        return "已取消"
    if action == "confirm":
        # 不应走到 TTS，防御性返回空
        return ""

    # 未知的 action_type，返回通用错误播报
    if result.success:
        return "操作完成"
    return result.error or "操作失败"


def _format_open_app(result: ExecutionResult) -> str:
    p = result.payload
    app_name = p.get("app_name", "")
    if result.success:
        return f"已为你打开 {app_name}"
    return result.error or f"无法打开 {app_name}"


def _format_close_app(result: ExecutionResult) -> str:
    p = result.payload
    app_name = p.get("app_name", "")
    if result.needs_confirmation:
        return f"确定要关闭 {app_name} 吗？"
    if result.success:
        terminated = p.get("terminated_pids") or []
        if not terminated:
            return f"{app_name} 当前未在运行"
        return f"已关闭 {app_name}"
    return result.error or f"无法关闭 {app_name}"


def _format_query_system(result: ExecutionResult) -> str:
    p = result.payload
    resource = p.get("resource", "")

    if not result.success:
        return result.error or f"查询{resource}失败"

    if resource == "memory":
        avail = p.get("available_gb", 0)
        total = p.get("total_gb", 0)
        pct = p.get("percent_used", 0)
        return f"当前内存剩余 {avail} GB，共 {total} GB，使用率 {pct}%"

    if resource == "disk":
        drives = p.get("drives", [])
        if not drives:
            return "没有可查询的磁盘"
        # 多盘符逐个播报，用分号分隔
        parts: list[str] = []
        for d in drives:
            drive_label = d.get("drive", "")
            # "C:\\" → "C 盘"
            letter = drive_label[:1] if drive_label else ""
            total = d.get("total_gb", 0)
            free = d.get("free_gb", 0)
            parts.append(f"{letter} 盘共 {total} GB，剩余 {free} GB")
        return "；".join(parts)

    if resource == "cpu":
        pct = p.get("percent_used", 0)
        return f"当前 CPU 占用率 {pct}%"

    return result.error or "查询失败"


def _format_open_file(result: ExecutionResult) -> str:
    p = result.payload
    if result.success:
        filename = p.get("filename", "")
        return f"已打开文件 {filename}"
    return result.error or "无法打开文件"


def _format_run_command(result: ExecutionResult) -> str:
    p = result.payload
    cmd_name = p.get("command_name", "")
    if result.needs_confirmation:
        return f"确定要执行 {cmd_name} 吗？"
    if result.success:
        stdout = p.get("stdout") or ""
        # stdout 短且非空时播报输出，否则只播报"已完成"
        if stdout and len(stdout) <= 50:
            return f"命令 {cmd_name} 已执行完成，输出：{stdout}"
        return f"命令 {cmd_name} 已执行完成"
    return result.error or f"命令 {cmd_name} 执行失败"


# ===========================================================================
# PyAV 帧解码工具
# ===========================================================================

def _frame_to_float32_mono(frame: av.AudioFrame) -> np.ndarray:
    """把 av.AudioFrame 转成 float32 mono numpy 数组。

    av 的 frame.to_ndarray() 返回格式取决于 frame.format：
        s16 交错: shape (samples, 1) int16
        s16p 平面: shape (channels, samples) int16
        fltp 平面: shape (channels, samples) float32
    统一转 float32 [-1, 1]，多通道 downmix 到 mono。
    """
    arr = frame.to_ndarray()

    # int16 → float32
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
            # 平面格式 (channels, samples)
            arr = arr.mean(axis=0)
        else:
            # 交错格式 (samples, channels)
            arr = arr.mean(axis=1)

    return np.ascontiguousarray(arr, dtype=np.float32)


__all__ = [
    "TTSEngine",
    "TTSError",
    "TTSSynthesizeError",
    "TTSDecodeError",
    "format_result",
]
