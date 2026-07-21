"""云端 TTS provider：OpenAI 兼容 /v1/audio/speech（修订 45）。

CLAUDE.md §2.2：Anthropic 协议无音频端点，云端 TTS 只做 OpenAI 格式，支持自定义 base_url。
复用意图解析层已装的 openai SDK（无新依赖）。

流程：
  text → client.audio.speech.create(model, voice, input=text, response_format="mp3")
  → MP3 bytes → tts._audio.decode_mp3 → PCM float32 → player.play()
  云端失败 → BaseTTSEngine.speak() 默认降级 pyttsx3（SAPI5 本地，无需联网）

安全：api_key 从环境变量读（§6 铁律，不进 config 明文）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np

from audio.player import AudioPlayer
from tts.tts_base import BaseTTSEngine, TTSRemoteError
from tts._audio import decode_mp3

_LOG = logging.getLogger(__name__)

_DEFAULT_MODEL = "tts-1"
_DEFAULT_VOICE = "alloy"
_DEFAULT_TIMEOUT = 30.0


class OpenAITTSEngine(BaseTTSEngine):
    """OpenAI 兼容云端 TTS 引擎。

    继承 BaseTTSEngine 的 speak()（一次性合成 → 播放 → pyttsx3 降级）和 speak_result()，
    只实现 synthesize_pcm()。错误归一化：SDK 各类异常 → TTSRemoteError，
    speak() 会 catch TTSError 走本地 pyttsx3 降级。
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        voice: str = _DEFAULT_VOICE,
        api_key: str = "",
        base_url: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
        player: Optional[AudioPlayer] = None,
        local_fallback: bool = True,
    ) -> None:
        super().__init__(player=player, local_fallback=local_fallback)
        if not api_key:
            raise TTSRemoteError(
                "云端 TTS 缺少 API key；请设置 config 里 tts.api_key_env 指向的环境变量"
            )
        # 延迟 import：只有真的用云端 TTS 才把 SDK 拉进内存。
        import openai

        self._openai = openai
        self._model = model
        self._voice = voice

        client_kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)

    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, tts_cfg: dict, player: AudioPlayer) -> "OpenAITTSEngine":
        """从 config.yaml 的 tts 块构造（provider=openai 时）。

        api_key 从 tts.api_key_env 指向的环境变量读取（§6 铁律：不进 config 明文）。
        云端音色用 tts.tts_voice（与本地 edge-tts 的 tts.voice 分开，取值域不同）。
        """
        import os

        api_key_env = str(tts_cfg.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env, "")
        base_url = str(tts_cfg.get("base_url", "") or "") or None
        return cls(
            model=str(tts_cfg.get("model", _DEFAULT_MODEL)),
            voice=str(tts_cfg.get("tts_voice", _DEFAULT_VOICE)),
            api_key=api_key,
            base_url=base_url,
            timeout=float(tts_cfg.get("timeout", _DEFAULT_TIMEOUT)),
            player=player,
            local_fallback=bool(tts_cfg.get("local_fallback", True)),
        )

    # ------------------------------------------------------------------
    async def synthesize_pcm(self, text: str) -> tuple[np.ndarray, int]:
        if not text or not text.strip():
            return np.array([], dtype=np.float32), 24000

        mp3_bytes = await asyncio.to_thread(self._synthesize_mp3, text)
        if not mp3_bytes:
            raise TTSRemoteError(f"云端 TTS 返回空音频，文本: {text!r}")
        return decode_mp3(mp3_bytes)

    # ------------------------------------------------------------------
    def _synthesize_mp3(self, text: str) -> bytes:
        """同步调用 /v1/audio/speech，返回 MP3 字节。异常归一化为 TTSRemoteError。"""
        try:
            resp = self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="mp3",
            )
        except self._openai.APITimeoutError as exc:
            raise TTSRemoteError(f"timeout: {exc}") from exc
        except self._openai.AuthenticationError as exc:
            raise TTSRemoteError(f"auth: {exc}") from exc
        except self._openai.PermissionDeniedError as exc:
            raise TTSRemoteError(f"auth: {exc}") from exc
        except self._openai.RateLimitError as exc:
            raise TTSRemoteError(f"rate_limit: {exc}") from exc
        except (self._openai.APIConnectionError, self._openai.APIError) as exc:
            raise TTSRemoteError(f"api: {exc}") from exc
        except Exception as exc:  # noqa: BLE001  中转站可能抛非 SDK 异常
            raise TTSRemoteError(f"unexpected: {exc}") from exc

        return _extract_audio_bytes(resp)


def _extract_audio_bytes(resp) -> bytes:
    """从 speech 响应对象取 MP3 字节（不同 SDK 版本返回形态不一）。"""
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)
    for attr in ("content", "read"):
        val = getattr(resp, attr, None)
        if val is None:
            continue
        if callable(val):
            try:
                out = val()
                if isinstance(out, (bytes, bytearray)):
                    return bytes(out)
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(val, (bytes, bytearray)):
            return bytes(val)
    raise TTSRemoteError(f"云端 TTS 响应无法提取音频字节: {type(resp).__name__}")


__all__ = ["OpenAITTSEngine"]
