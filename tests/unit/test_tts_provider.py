"""云端 TTS provider 单元测试（修订 45）。

全部 mock，不联网、不需真实音频设备，进 CI。覆盖：
  - build_tts_engine 工厂分发（edge / openai / 未知）
  - OpenAITTSEngine 构造（缺 key / base_url 透传 / 音色）
  - synthesize_pcm 走 mock client + mock decode_mp3
  - _extract_audio_bytes 各返回形态
  - 错误归一化 → TTSRemoteError
  - speak() 云端失败降级 pyttsx3（通过 BaseTTSEngine 默认实现）
"""

from __future__ import annotations

import asyncio
import sys
import types

import numpy as np
import pytest

from tts.tts_base import BaseTTSEngine, TTSRemoteError, build_tts_engine
from tts.providers import openai_tts
from tts.providers.openai_tts import OpenAITTSEngine, _extract_audio_bytes


# ---------------------------------------------------------------------------
# 假 openai SDK
# ---------------------------------------------------------------------------

class _FakeAPIError(Exception):
    pass


class _FakeTimeout(Exception):
    pass


class _FakeAuth(Exception):
    pass


class _FakePermission(Exception):
    pass


class _FakeRateLimit(Exception):
    pass


class _FakeConnection(Exception):
    pass


class _FakeSpeech:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeAudio:
    def __init__(self, speech):
        self.speech = speech


class _FakeClient:
    def __init__(self, speech):
        self.audio = _FakeAudio(speech)
        self.init_kwargs = {}


def _install_fake_openai(monkeypatch, speech):
    holder = {}
    fake = types.ModuleType("openai")
    fake.APITimeoutError = _FakeTimeout
    fake.AuthenticationError = _FakeAuth
    fake.PermissionDeniedError = _FakePermission
    fake.RateLimitError = _FakeRateLimit
    fake.APIConnectionError = _FakeConnection
    fake.APIError = _FakeAPIError

    def _make_client(**kwargs):
        client = _FakeClient(speech)
        client.init_kwargs = kwargs
        holder["client"] = client
        return client

    fake.OpenAI = _make_client
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake, holder


# ---------------------------------------------------------------------------
# _extract_audio_bytes
# ---------------------------------------------------------------------------

def test_extract_bytes_raw():
    assert _extract_audio_bytes(b"abc") == b"abc"


def test_extract_bytes_content_attr():
    obj = types.SimpleNamespace(content=b"xyz")
    assert _extract_audio_bytes(obj) == b"xyz"


def test_extract_bytes_read_method():
    class R:
        def read(self):
            return b"data"
    assert _extract_audio_bytes(R()) == b"data"


def test_extract_bytes_unextractable_raises():
    with pytest.raises(TTSRemoteError):
        _extract_audio_bytes(object())


# ---------------------------------------------------------------------------
# build_tts_engine 工厂
# ---------------------------------------------------------------------------

def test_build_tts_engine_edge_default(monkeypatch):
    created = {}

    class _FakeEdge:
        @classmethod
        def from_config(cls, cfg, player):
            created["cfg"] = cfg
            return "EDGE"

    import tts.edge_tts_engine as ete
    monkeypatch.setattr(ete, "TTSEngine", _FakeEdge)
    out = build_tts_engine({}, player=None)  # provider 缺省 = edge
    assert out == "EDGE"


def test_build_tts_engine_unknown():
    with pytest.raises(ValueError, match="unsupported TTS provider"):
        build_tts_engine({"provider": "azure"}, player=None)


def test_build_tts_engine_openai(monkeypatch):
    _install_fake_openai(monkeypatch, _FakeSpeech(result=b"mp3"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    engine = build_tts_engine({
        "provider": "openai",
        "model": "tts-1",
        "tts_voice": "nova",
        "base_url": "https://relay.example.com",
    }, player=None)
    assert isinstance(engine, OpenAITTSEngine)


# ---------------------------------------------------------------------------
# OpenAITTSEngine 构造
# ---------------------------------------------------------------------------

def test_openai_tts_requires_key(monkeypatch):
    _install_fake_openai(monkeypatch, _FakeSpeech())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TTSRemoteError, match="API key"):
        OpenAITTSEngine.from_config({"provider": "openai"}, player=None)


def test_openai_tts_passes_base_url_and_voice(monkeypatch):
    speech = _FakeSpeech(result=b"mp3")
    _, holder = _install_fake_openai(monkeypatch, speech)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    engine = OpenAITTSEngine.from_config({
        "provider": "openai",
        "tts_voice": "nova",
        "base_url": "https://relay.example.com",
    }, player=None)
    assert holder["client"].init_kwargs.get("base_url") == "https://relay.example.com"
    assert engine._voice == "nova"


# ---------------------------------------------------------------------------
# synthesize_pcm
# ---------------------------------------------------------------------------

def test_openai_tts_synthesize_pcm_success(monkeypatch):
    speech = _FakeSpeech(result=b"fake-mp3-bytes")
    _install_fake_openai(monkeypatch, speech)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    # mock decode_mp3 避免真实 av 解码
    monkeypatch.setattr(
        openai_tts, "decode_mp3",
        lambda b: (np.array([0.1, 0.2], dtype=np.float32), 24000),
    )

    engine = OpenAITTSEngine.from_config({
        "provider": "openai", "tts_voice": "alloy", "model": "tts-1",
    }, player=None)
    pcm, sr = asyncio.run(engine.synthesize_pcm("你好"))
    assert sr == 24000
    assert len(pcm) == 2
    # 校验请求参数
    assert speech.calls[0]["model"] == "tts-1"
    assert speech.calls[0]["voice"] == "alloy"
    assert speech.calls[0]["input"] == "你好"
    assert speech.calls[0]["response_format"] == "mp3"


def test_openai_tts_synthesize_empty_text(monkeypatch):
    _install_fake_openai(monkeypatch, _FakeSpeech(result=b"x"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    engine = OpenAITTSEngine.from_config({"provider": "openai"}, player=None)
    pcm, sr = asyncio.run(engine.synthesize_pcm("   "))
    assert len(pcm) == 0


def test_openai_tts_timeout_maps_remote_error(monkeypatch):
    speech = _FakeSpeech(exc=_FakeTimeout("timed out"))
    _install_fake_openai(monkeypatch, speech)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    engine = OpenAITTSEngine.from_config({"provider": "openai"}, player=None)
    with pytest.raises(TTSRemoteError, match="timeout"):
        asyncio.run(engine.synthesize_pcm("你好"))


def test_openai_tts_auth_maps_remote_error(monkeypatch):
    speech = _FakeSpeech(exc=_FakeAuth("bad key"))
    _install_fake_openai(monkeypatch, speech)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    engine = OpenAITTSEngine.from_config({"provider": "openai"}, player=None)
    with pytest.raises(TTSRemoteError, match="auth"):
        asyncio.run(engine.synthesize_pcm("你好"))


# ---------------------------------------------------------------------------
# speak() 降级：云端失败 → pyttsx3（BaseTTSEngine 默认实现）
# ---------------------------------------------------------------------------

class _FakePlayer:
    def __init__(self):
        self.played = []
        self.stop_callbacks = []

    async def play(self, pcm, sr):
        self.played.append((pcm, sr))

    def add_stop_callback(self, cb):
        self.stop_callbacks.append(cb)

    def remove_stop_callback(self, cb):
        if cb in self.stop_callbacks:
            self.stop_callbacks.remove(cb)


def test_speak_falls_back_to_local_on_remote_error(monkeypatch):
    speech = _FakeSpeech(exc=_FakeTimeout("down"))
    _install_fake_openai(monkeypatch, speech)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    player = _FakePlayer()
    engine = OpenAITTSEngine.from_config(
        {"provider": "openai", "local_fallback": True}, player=player,
    )

    local_calls = []

    async def _fake_local(self, text):
        local_calls.append(text)

    monkeypatch.setattr(BaseTTSEngine, "_speak_local", _fake_local)
    asyncio.run(engine.speak("你好"))
    assert local_calls == ["你好"]
    assert player.played == []  # 云端失败，没有走 play


def test_speak_plays_pcm_on_success(monkeypatch):
    speech = _FakeSpeech(result=b"mp3")
    _install_fake_openai(monkeypatch, speech)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        openai_tts, "decode_mp3",
        lambda b: (np.array([0.5], dtype=np.float32), 24000),
    )

    player = _FakePlayer()
    engine = OpenAITTSEngine.from_config({"provider": "openai"}, player=player)
    asyncio.run(engine.speak("你好"))
    assert len(player.played) == 1
    assert player.played[0][1] == 24000
