"""云端 ASR provider 单元测试（修订 45）。

全部 mock，不联网、不需真实设备，进 CI。覆盖：
  - build_asr_engine 工厂分发（local / openai / 未知）
  - _encode_wav 编码正确性（stdlib wave 回读）
  - _parse_response 解析（verbose_json 带/不带置信度）
  - OpenAIASREngine.transcribe 走 mock client、置信度过滤、opencc、错误归一化
  - finalize_result 的置信度过滤 + 退化行为
"""

from __future__ import annotations

import io
import sys
import types
import wave

import numpy as np
import pytest

from asr.asr_engine import (
    ASRRemoteError,
    build_asr_engine,
    finalize_result,
)
from asr.providers.openai_asr import (
    OpenAIASREngine,
    _encode_wav,
    _parse_response,
)


# ---------------------------------------------------------------------------
# 假 openai SDK：注入 sys.modules，让 OpenAIASREngine 的延迟 import 拿到它
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


class _FakeTranscriptions:
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
    def __init__(self, transcriptions):
        self.transcriptions = transcriptions


class _FakeOpenAIClient:
    def __init__(self, audio):
        self.audio = audio
        self.init_kwargs = {}


def _install_fake_openai(monkeypatch, transcriptions):
    """注入一个假的 openai 模块，返回 (module, created_client_holder)。"""
    holder = {}

    fake = types.ModuleType("openai")
    fake.APITimeoutError = _FakeTimeout
    fake.AuthenticationError = _FakeAuth
    fake.PermissionDeniedError = _FakePermission
    fake.RateLimitError = _FakeRateLimit
    fake.APIConnectionError = _FakeConnection
    fake.APIError = _FakeAPIError

    def _make_client(**kwargs):
        client = _FakeOpenAIClient(_FakeAudio(transcriptions))
        client.init_kwargs = kwargs
        holder["client"] = client
        return client

    fake.OpenAI = _make_client
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake, holder


# ---------------------------------------------------------------------------
# _encode_wav
# ---------------------------------------------------------------------------

def test_encode_wav_roundtrip():
    audio = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    wav_bytes = _encode_wav(audio, 16000)

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        frames = wf.readframes(wf.getnframes())

    decoded = np.frombuffer(frames, dtype="<i2")
    assert len(decoded) == 5
    # 1.0 → 32767, -1.0 → -32767
    assert decoded[3] == 32767
    assert decoded[4] == -32767


def test_encode_wav_clips_out_of_range():
    audio = np.array([2.0, -2.0], dtype=np.float32)
    wav_bytes = _encode_wav(audio, 16000)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        decoded = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    assert decoded[0] == 32767
    assert decoded[1] == -32767


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

def test_parse_response_with_confidence():
    resp = {
        "text": "打开浏览器",
        "segments": [
            {"text": "打开浏览器", "avg_logprob": -0.3, "no_speech_prob": 0.05},
        ],
    }
    text, segs, nsp, alp, has_conf = _parse_response(resp)
    assert text == "打开浏览器"
    assert segs == ["打开浏览器"]
    assert nsp == pytest.approx(0.05)
    assert alp == pytest.approx(-0.3)
    assert has_conf is True


def test_parse_response_plain_text_no_confidence():
    resp = {"text": "打开浏览器"}
    text, segs, nsp, alp, has_conf = _parse_response(resp)
    assert text == "打开浏览器"
    assert segs == []
    assert has_conf is False


def test_parse_response_takes_max_no_speech_prob():
    resp = {
        "text": "ab",
        "segments": [
            {"text": "a", "no_speech_prob": 0.1, "avg_logprob": -0.2},
            {"text": "b", "no_speech_prob": 0.7, "avg_logprob": -0.4},
        ],
    }
    _, _, nsp, alp, has_conf = _parse_response(resp)
    assert nsp == pytest.approx(0.7)
    assert alp == pytest.approx(-0.3)  # (-0.2 + -0.4) / 2
    assert has_conf is True


# ---------------------------------------------------------------------------
# finalize_result 过滤 / 退化
# ---------------------------------------------------------------------------

def test_finalize_filters_high_no_speech_prob():
    r = finalize_result(
        "噪声", no_speech_prob=0.9, avg_logprob=-0.2,
        duration_s=1.0, elapsed_s=0.5, has_confidence=True,
    )
    assert r.is_empty
    assert r.text == ""


def test_finalize_filters_low_logprob():
    r = finalize_result(
        "幻觉", no_speech_prob=0.1, avg_logprob=-2.0,
        duration_s=1.0, elapsed_s=0.5, has_confidence=True,
    )
    assert r.is_empty


def test_finalize_no_confidence_skips_prob_filters():
    # 无置信度时，即便 no_speech_prob 传了高值也不过滤（退化，§8.12）
    r = finalize_result(
        "打开浏览器", no_speech_prob=0.9, avg_logprob=-2.0,
        duration_s=1.0, elapsed_s=0.5, has_confidence=False,
    )
    assert not r.is_empty
    assert r.text == "打开浏览器"


def test_finalize_empty_text_always_empty():
    r = finalize_result(
        "", no_speech_prob=0.1, avg_logprob=-0.1,
        duration_s=1.0, elapsed_s=0.5, has_confidence=False,
    )
    assert r.is_empty


# ---------------------------------------------------------------------------
# build_asr_engine 工厂
# ---------------------------------------------------------------------------

def test_build_asr_engine_local_default(monkeypatch):
    created = {}

    class _FakeWhisper:
        @classmethod
        def from_config(cls, cfg):
            created["cfg"] = cfg
            return "WHISPER"

    import asr.whisper_engine as we
    monkeypatch.setattr(we, "WhisperEngine", _FakeWhisper)
    out = build_asr_engine({})  # provider 缺省 = local
    assert out == "WHISPER"


def test_build_asr_engine_unknown_provider():
    with pytest.raises(ValueError, match="unsupported ASR provider"):
        build_asr_engine({"provider": "gemini"})


def test_build_asr_engine_openai(monkeypatch):
    _install_fake_openai(monkeypatch, _FakeTranscriptions(result={"text": "x"}))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    engine = build_asr_engine({
        "provider": "openai",
        "model": "whisper-1",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://relay.example.com",
    })
    assert isinstance(engine, OpenAIASREngine)


# ---------------------------------------------------------------------------
# OpenAIASREngine 构造 + transcribe
# ---------------------------------------------------------------------------

def test_openai_asr_requires_key(monkeypatch):
    _install_fake_openai(monkeypatch, _FakeTranscriptions())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ASRRemoteError, match="API key"):
        OpenAIASREngine.from_config({"provider": "openai"})


def test_openai_asr_passes_base_url(monkeypatch):
    _, holder = _install_fake_openai(monkeypatch, _FakeTranscriptions(result={"text": "x"}))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    OpenAIASREngine.from_config({
        "provider": "openai",
        "base_url": "https://relay.example.com",
    })
    assert holder["client"].init_kwargs.get("base_url") == "https://relay.example.com"


def test_openai_asr_transcribe_success(monkeypatch):
    resp = {
        "text": "打开浏览器",
        "segments": [{"text": "打开浏览器", "avg_logprob": -0.2, "no_speech_prob": 0.05}],
    }
    tr = _FakeTranscriptions(result=resp)
    _install_fake_openai(monkeypatch, tr)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    engine = OpenAIASREngine.from_config({
        "provider": "openai", "opencc_enabled": False,
    })
    audio = np.zeros(16000, dtype=np.float32)
    result = engine.transcribe(audio, sample_rate=16000)

    assert not result.is_empty
    assert result.text == "打开浏览器"
    # 校验请求参数
    assert tr.calls[0]["model"] == "whisper-1"
    assert tr.calls[0]["response_format"] == "verbose_json"
    assert tr.calls[0]["language"] == "zh"


def test_openai_asr_transcribe_timeout_maps_remote_error(monkeypatch):
    tr = _FakeTranscriptions(exc=_FakeTimeout("timed out"))
    _install_fake_openai(monkeypatch, tr)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    engine = OpenAIASREngine.from_config({"provider": "openai"})
    with pytest.raises(ASRRemoteError, match="timeout"):
        engine.transcribe(np.zeros(1600, dtype=np.float32))


def test_openai_asr_transcribe_auth_maps_remote_error(monkeypatch):
    tr = _FakeTranscriptions(exc=_FakeAuth("bad key"))
    _install_fake_openai(monkeypatch, tr)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    engine = OpenAIASREngine.from_config({"provider": "openai"})
    with pytest.raises(ASRRemoteError, match="auth"):
        engine.transcribe(np.zeros(1600, dtype=np.float32))


def test_openai_asr_degrades_without_confidence(monkeypatch):
    # 中转站只返回纯文本，无 segments → 不做置信度过滤，仍返回文本（§8.12）
    tr = _FakeTranscriptions(result={"text": "退出程序"})
    _install_fake_openai(monkeypatch, tr)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    engine = OpenAIASREngine.from_config({"provider": "openai", "opencc_enabled": False})
    result = engine.transcribe(np.zeros(1600, dtype=np.float32))
    assert not result.is_empty
    assert result.text == "退出程序"
