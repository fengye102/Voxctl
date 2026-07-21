"""TTS 模块：Edge TTS 语音合成 + 流式播放 + pyttsx3 本地后备 + 模板化播报。

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
    5. 流式播放：三阶段并发管线（edge-tts → pipe → av 解码线程 → Queue → player.play_stream），
       首个 PCM 块到达即开始出声，大幅降低长文本首字延迟。流式失败自动回退到一次性合成。
    6. pyttsx3 本地后备：edge-tts 网络异常时自动降级到 SAPI5 本地 TTS，无需联网。

依赖链：
    edge-tts（网络）→ MP3 bytes → av.decode（CPU）→ PCM float32 → sounddevice（音频设备）
    pyttsx3（本地后备）→ SAPI5（Windows 自带）→ 系统音频（不经 sounddevice）
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
from typing import AsyncIterator, Optional

import av
import edge_tts
import numpy as np

from executor import ExecutionResult
from audio.player import AudioPlayer

# 抽象层 + 共享音频工具迁移（修订 45）：异常层级迁到 tts.tts_base，
# MP3 解码 / 帧转换迁到 tts._audio；这里 re-export 保持旧 import 路径可用。
from tts.tts_base import (
    BaseTTSEngine,
    TTSDecodeError,
    TTSError,
    TTSSynthesizeError,
)
from tts._audio import decode_mp3 as _decode_mp3_shared
from tts._audio import frame_to_float32_mono as _frame_to_float32_mono

_LOG = logging.getLogger(__name__)

# 解码线程从 pipe 读取 MP3 片段用的不可 seek 文件包装
_PIPE_SENTINEL = b""  # os.read 返回空 = 写端已关闭


class _PipeReader:
    """包装 os.pipe 读端 fd，暴露 .read() 但没有 .seek()。

    PyAV 打开没有 seek 的文件对象时，自动按流式（non-seekable）
    处理 MP3 容器，不会因为无法 seek 而报错。
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def read(self, n: int = -1) -> bytes:
        if n <= 0:
            # 读到 EOF
            parts: list[bytes] = []
            while True:
                chunk = os.read(self._fd, 65536)
                if not chunk:
                    break
                parts.append(chunk)
            return b"".join(parts)
        return os.read(self._fd, n)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)


class TTSEngine(BaseTTSEngine):
    """Edge TTS 语音合成引擎，含流式播放 + pyttsx3 本地后备。

    主路径：edge-tts（云端）流式合成 MP3 → pipe → PyAV 解码线程 → Queue → player.play_stream()。
    一次性回退：流式失败时 → 完整合成 MP3 → PyAV 解码 → player.play()。
    后备路径：edge-tts 网络异常时自动降级到 pyttsx3（SAPI5 本地引擎），
    无需联网，Windows 自带语音包即可用。降级对调用方透明。

    打断支持：pyttsx3 播报期间，interrupt coordinator 调用 player.stop() 时
    会通过 player 的 stop callback 机制触发 engine.stop()，中断本地播报。

    用法：
        player = AudioPlayer()
        engine = TTSEngine.from_config(config["tts"], player)
        await engine.speak("你好")
        await engine.speak_result(execution_result)
    """

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        player: Optional[AudioPlayer] = None,
        local_fallback: bool = True,
    ) -> None:
        super().__init__(player=player, local_fallback=local_fallback)
        self._voice = voice

    @classmethod
    def from_config(cls, tts_cfg: dict, player: AudioPlayer) -> "TTSEngine":
        """从 config.yaml 的 tts 块构造。

        Args:
            tts_cfg: config["tts"] 字典，含 voice / local_fallback 字段
            player: AudioPlayer 实例（Step 5 实现，负责实际播放）
        """
        voice = tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural")
        local_fallback = tts_cfg.get("local_fallback", True)
        return cls(voice=voice, player=player, local_fallback=local_fallback)

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
        """用 PyAV 解码 MP3 → PCM float32 mono（委托 tts._audio 共享实现，修订 45）。"""
        return _decode_mp3_shared(mp3_bytes)

    # ------------------------------------------------------------------
    async def speak(self, text: str) -> None:
        """合成文本并播放。三级降级：流式 → 一次性 → pyttsx3 本地。

        如果没设置 player，只打日志（测试/调试用）。
        """
        if self._player is None:
            _LOG.info("[TTS] no player set, skip playback: %r", text)
            return

        if not text or not text.strip():
            _LOG.debug("[TTS] empty text, skip")
            return

        # 1. 尝试流式播放（最低延迟）
        try:
            await self._speak_streaming(text)
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[TTS] streaming failed (%s), falling back to one-shot", exc)

        # 2. 回退：一次性合成 + 播放
        try:
            pcm, sample_rate = await self.synthesize_pcm(text)
            if pcm.size > 0:
                await self._player.play(pcm, sample_rate)
            return
        except TTSError as exc:
            _LOG.warning("[TTS] one-shot also failed, trying local fallback: %s", exc)

        # 3. 最终回退：pyttsx3 本地 TTS
        if self._local_fallback:
            await self._speak_local(text)

    # ------------------------------------------------------------------
    async def _speak_streaming(self, text: str) -> None:
        """三阶段流式管线：edge-tts 异步生产 → pipe → 解码线程 → Queue → 播放。

        阶段 1（async）：edge-tts stream() 产出 MP3 chunk，写入 pipe 写端
        阶段 2（thread）：av 从 pipe 读端流式解码 MP3 → PCM，放入 asyncio.Queue
        阶段 3（async）：player.play_stream() 从 Queue 逐块播放

        stop() 可随时中断：player.play_stream 检查 is_playing 退出，
        解码线程读到 pipe 关闭也会退出。
        """
        read_fd, write_fd = os.pipe()
        pcm_queue: asyncio.Queue[Optional[np.ndarray]] = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()

        # 阶段 1：异步 feed edge-tts MP3 块到 pipe 写端
        async def _feed_mp3() -> None:
            try:
                communicate = edge_tts.Communicate(text, self._voice)
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        data = chunk["data"]
                        if data:
                            os.write(write_fd, data)
            except Exception as exc:
                _LOG.warning("[TTS] edge-tts feed error: %s", exc)
                raise
            finally:
                # 关闭写端，让解码线程的 read() 拿到 EOF
                with contextlib.suppress(OSError):
                    os.close(write_fd)

        # 阶段 2：在线程池中用 av 从 pipe 流式解码
        def _decode_thread() -> None:
            reader = _PipeReader(read_fd)
            container = None
            try:
                container = av.open(reader, format="mp3")
                audio_stream = container.streams.audio[0]
                for frame in container.decode(audio_stream):
                    arr = _frame_to_float32_mono(frame)
                    if arr.size > 0:
                        # 线程安全地把 PCM 放入异步队列
                        future = asyncio.run_coroutine_threadsafe(
                            pcm_queue.put(arr), loop,
                        )
                        future.result(timeout=5.0)  # 阻塞等待放入
            except Exception as exc:
                _LOG.debug("[TTS] decode thread: %s", exc)
            finally:
                if container is not None:
                    with contextlib.suppress(Exception):
                        container.close()
                reader.close()
                # 发送结束哨兵
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        pcm_queue.put(None), loop,
                    )
                    future.result(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass

        # 阶段 3：异步迭代器包装 Queue，供 player.play_stream 消费
        async def _pcm_chunks() -> AsyncIterator[np.ndarray]:
            while True:
                try:
                    chunk = await asyncio.wait_for(pcm_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # 检查播放器是否被 stop() 了
                    if not self._player.is_playing:
                        return
                    continue
                if chunk is None:
                    # 解码结束
                    return
                yield chunk

        # 启动三个阶段并发
        feed_task = asyncio.create_task(_feed_mp3(), name="tts_feed")
        decode_future = loop.run_in_executor(None, _decode_thread)
        sample_rate = 24000  # edge-tts 默认 24kHz

        try:
            await self._player.play_stream(_pcm_chunks(), sample_rate=sample_rate)
        finally:
            # 确保 feed 和 decode 都结束
            if not feed_task.done():
                feed_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await feed_task
            # 关闭写端以防 feed 被 cancel 时还没关
            with contextlib.suppress(OSError):
                os.close(write_fd)
            # 等 decode 线程退出（pipe 读端关闭后它会 EOF）
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.shield(decode_future), timeout=3.0,
                )

    # ------------------------------------------------------------------
    # speak_result 不重写：复用 BaseTTSEngine.speak_result（format_result + speak），
    # 子类实现与 base 完全一致，重写是冗余。pyttsx3 本地后备也已上移到 BaseTTSEngine（修订 45）。


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
        return ""
    if action == "llm_error":
        return _format_llm_error(result)
    if action == "asr_error":
        return "语音识别出了问题，请再说一次"
    if action == "intent_error":
        return "理解指令时出了问题，请再试一次"

    # 未知的 action_type，返回通用错误播报
    if result.success:
        return "操作完成"
    return result.error or "操作失败"


def _format_llm_error(result: ExecutionResult) -> str:
    """LLM 基础设施故障的分类播报（区别于"不确定你想做什么"）。"""
    reason = result.error or ""
    if reason.startswith("timeout"):
        return "网络连接超时，请稍后再试"
    if reason.startswith("rate limited"):
        return "请求过于频繁，请稍后再试"
    if reason.startswith("parse error") or reason.startswith("validation failed"):
        return "理解指令时出了问题，请再说一次"
    return "服务出了问题，请稍后再试"


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
# PyAV 帧解码工具（_frame_to_float32_mono 已迁到 tts._audio，此处 import 复用，修订 45）
# ===========================================================================


__all__ = [
    "TTSEngine",
    "TTSError",
    "TTSSynthesizeError",
    "TTSDecodeError",
    "format_result",
]
