"""TTS 抽象层（修订 45）。

本模块是 TTS 的共享基础，与具体引擎（本地 edge-tts / 云端 OpenAI）无关：
  - `BaseTTSEngine`：抽象基类，定义统一接口 speak / speak_result / synthesize_pcm；
    内置 pyttsx3 本地后备（SAPI5，两条云路径失败时共用的最后一道降级）
  - 异常层级（TTSError / TTSSynthesizeError / TTSDecodeError / TTSRemoteError）
  - `build_tts_engine()`：按 config 的 `provider` 字段实例化具体引擎（CLAUDE.md §2.2）

设计动机（镜像 nlu/ 的多 provider 结构）：业务层只依赖 `BaseTTSEngine` 抽象接口
和 `build_tts_engine` 工厂，不感知底层是 edge-tts 还是云端 OpenAI TTS。

注意：`format_result()` 仍留在 tts.edge_tts_engine（多处测试从那里 import），不迁到本模块。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np

from audio.player import AudioPlayer

_LOG = logging.getLogger(__name__)


class TTSError(Exception):
    """TTS 模块错误基类。"""


class TTSSynthesizeError(TTSError):
    """TTS 合成失败（网络/服务问题）。"""


class TTSDecodeError(TTSError):
    """MP3 解码失败（av/PyAV 问题）。"""


class TTSRemoteError(TTSError):
    """云端 TTS 调用失败（鉴权 / 超时 / HTTP 错误）。消息带分类信息。"""


class BaseTTSEngine(ABC):
    """TTS 引擎抽象基类，内置 pyttsx3 本地后备。

    具体实现：
      - tts.edge_tts_engine.TTSEngine（本地 edge-tts + 流式，默认）
      - tts.providers.openai_tts.OpenAITTSEngine（云端 OpenAI 兼容）

    子类必须实现 `synthesize_pcm()`（文本→PCM）。`speak()` 的默认实现：
    合成 PCM → player.play() → 失败降级 pyttsx3。edge-tts 子类会重写 speak()
    额外插入流式路径。云端子类直接用默认 speak()（合成后一次性播放）。
    """

    def __init__(
        self,
        player: Optional[AudioPlayer] = None,
        local_fallback: bool = True,
    ) -> None:
        self._player = player
        self._local_fallback = local_fallback
        self._pyttsx3_engine = None  # lazy init

    # ------------------------------------------------------------------
    @abstractmethod
    async def synthesize_pcm(self, text: str) -> tuple[np.ndarray, int]:
        """文本 → PCM float32 数组（mono，[-1,1]）+ sample_rate。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    async def speak(self, text: str) -> None:
        """合成文本并播放。默认：一次性合成 → 播放 → pyttsx3 本地降级。

        edge-tts 子类重写本方法以插入流式路径；云端子类用此默认实现。
        """
        if self._player is None:
            _LOG.info("[TTS] no player set, skip playback: %r", text)
            return
        if not text or not text.strip():
            _LOG.debug("[TTS] empty text, skip")
            return

        try:
            pcm, sample_rate = await self.synthesize_pcm(text)
            if pcm.size > 0:
                await self._player.play(pcm, sample_rate)
            return
        except TTSError as exc:
            _LOG.warning("[TTS] synth/play failed, trying local fallback: %s", exc)

        if self._local_fallback:
            await self._speak_local(text)

    # ------------------------------------------------------------------
    async def speak_result(self, result) -> None:
        """把 ExecutionResult 套模板生成播报文本，再合成播放。"""
        # 延迟 import 避免与 edge_tts_engine 循环依赖（format_result 留在那里）。
        from tts.edge_tts_engine import format_result

        text = format_result(result)
        await self.speak(text)

    # ------------------------------------------------------------------
    # pyttsx3 本地后备 TTS（两条云路径失败时共用）
    # ------------------------------------------------------------------

    def _get_pyttsx3(self):
        """Lazy-init pyttsx3 engine（SAPI5）。失败返回 None。"""
        if self._pyttsx3_engine is not None:
            return self._pyttsx3_engine
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 150)
            self._pyttsx3_engine = engine
            _LOG.info("[TTS] pyttsx3 local engine initialized (SAPI5)")
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[TTS] pyttsx3 init failed, no local fallback: %s", exc)
            return None
        return self._pyttsx3_engine

    async def _speak_local(self, text: str) -> None:
        """Fallback: speak via pyttsx3 (SAPI5)。pyttsx3 自管音频输出（不经 player）。

        打断支持：注册 player stop callback，interrupt coordinator 调 player.stop()
        时触发 engine.stop() 中断本地播报。
        """
        engine = self._get_pyttsx3()
        if engine is None:
            _LOG.warning("[TTS] pyttsx3 unavailable, speech skipped: %r", text)
            return
        if self._player is None:
            return

        _LOG.info("[TTS] pyttsx3 fallback speaking: %r", text)

        def _on_player_stop():
            try:
                engine.stop()
            except RuntimeError:
                pass
            except Exception:  # noqa: BLE001
                pass

        self._player.add_stop_callback(_on_player_stop)
        try:
            def _run_pyttsx3():
                engine.say(text)
                engine.runAndWait()

            await asyncio.to_thread(_run_pyttsx3)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[TTS] pyttsx3 fallback failed: %s", exc)
        finally:
            self._player.remove_stop_callback(_on_player_stop)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_tts_engine(tts_cfg: dict, player: AudioPlayer) -> BaseTTSEngine:
    """按 config 的 tts.provider 字段实例化具体 TTS 引擎（CLAUDE.md §2.2）。

    provider 取值：
      - "edge"（默认）：本地 edge-tts（tts.edge_tts_engine.TTSEngine）
      - "openai"：云端 OpenAI 兼容（tts.providers.openai_tts.OpenAITTSEngine）
    未知 provider 抛 ValueError（暴露配置错误，不静默兜底）。
    """
    provider = str((tts_cfg or {}).get("provider", "edge")).strip().lower()

    if provider == "edge":
        from tts.edge_tts_engine import TTSEngine
        return TTSEngine.from_config(tts_cfg or {}, player)

    if provider == "openai":
        from tts.providers.openai_tts import OpenAITTSEngine
        return OpenAITTSEngine.from_config(tts_cfg or {}, player)

    raise ValueError(
        f"unsupported TTS provider: {provider!r} (expected 'edge' or 'openai')"
    )


__all__ = [
    "BaseTTSEngine",
    "TTSError",
    "TTSSynthesizeError",
    "TTSDecodeError",
    "TTSRemoteError",
    "build_tts_engine",
]
