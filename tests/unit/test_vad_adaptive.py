"""VAD 自适应阈值单元测试（P2 #12）。

mock 掉 onnx 模型加载和推理，直接注入概率值，
验证自适应阈值在各种噪声环境下的行为。
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from audio.vad import (
    CHUNK_SAMPLES,
    VADConfig,
    VADMode,
    VADProcessor,
    _NOISE_MIN_FRAMES,
    _NOISE_WINDOW_FRAMES,
)


# ── helpers ──────────────────────────────────────────────────────────────

def _make_processor(
    *,
    silence_threshold: float = 0.5,
    adaptive: bool = True,
    margin: float = 0.10,
) -> VADProcessor:
    """构造 VADProcessor，mock 掉模型加载。"""
    cfg = VADConfig(
        silence_threshold=silence_threshold,
        adaptive_threshold=adaptive,
        adaptive_margin=margin,
    )
    with patch.object(VADProcessor, "_load_model"):
        proc = VADProcessor(cfg)
    return proc


def _feed_silence_frames(proc: VADProcessor, prob: float, n: int) -> None:
    """喂 n 帧静默（概率 < 阈值），直接操控 _get_speech_prob 返回值。"""
    chunk = np.zeros(CHUNK_SAMPLES, dtype=np.float32)
    with patch.object(proc, "_get_speech_prob", return_value=prob):
        for _ in range(n):
            proc.process_chunk(chunk, mode=VADMode.LISTEN)


# ── TestAdaptiveThreshold ────────────────────────────────────────────────

class TestAdaptiveThreshold:

    def test_quiet_room_no_change(self) -> None:
        """安静环境（噪声概率 ~0.05）：自适应阈值不高于静态阈值。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)
        _feed_silence_frames(proc, prob=0.05, n=50)

        assert proc.effective_listen_threshold == 0.5
        assert proc.noise_floor == pytest.approx(0.05, abs=0.01)

    def test_noisy_room_raises_threshold(self) -> None:
        """噪声环境（概率 ~0.45）：自适应阈值高于静态 0.5。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)
        _feed_silence_frames(proc, prob=0.45, n=50)

        expected = 0.45 + 0.10  # 0.55
        assert proc.effective_listen_threshold == pytest.approx(expected, abs=0.01)
        assert proc.noise_floor == pytest.approx(0.45, abs=0.01)

    def test_very_noisy_room(self) -> None:
        """高噪声环境（概率 ~0.48，接近阈值）：阈值被抬高到噪声上方。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)
        _feed_silence_frames(proc, prob=0.48, n=50)

        expected = 0.48 + 0.10  # 0.58
        assert proc.effective_listen_threshold == pytest.approx(expected, abs=0.01)

    def test_adaptive_disabled_uses_static(self) -> None:
        """自适应关闭时，始终用静态阈值。"""
        proc = _make_processor(silence_threshold=0.5, adaptive=False)
        _feed_silence_frames(proc, prob=0.45, n=50)

        assert proc.effective_listen_threshold == 0.5
        assert proc.noise_floor == 0.0

    def test_not_enough_samples_uses_static(self) -> None:
        """样本不足 _NOISE_MIN_FRAMES 时，回退到静态阈值。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)
        _feed_silence_frames(proc, prob=0.45, n=_NOISE_MIN_FRAMES - 1)

        assert proc.effective_listen_threshold == 0.5
        assert proc.noise_floor == 0.0

    def test_exactly_min_frames_activates(self) -> None:
        """刚好达到 _NOISE_MIN_FRAMES 时自适应激活。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)
        _feed_silence_frames(proc, prob=0.45, n=_NOISE_MIN_FRAMES)

        assert proc.effective_listen_threshold > 0.5

    def test_noise_floor_property(self) -> None:
        """noise_floor 正确反映窗口 p95。"""
        proc = _make_processor()
        assert proc.noise_floor == 0.0

        _feed_silence_frames(proc, prob=0.10, n=50)
        assert proc.noise_floor == pytest.approx(0.10, abs=0.01)

    def test_reset_clears_noise_window(self) -> None:
        """reset() 清空噪声窗口，回退到静态阈值。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)
        _feed_silence_frames(proc, prob=0.45, n=50)
        assert proc.effective_listen_threshold > 0.5

        proc.reset()
        assert proc.effective_listen_threshold == 0.5
        assert proc.noise_floor == 0.0
        assert len(proc._noise_probs) == 0

    def test_interrupt_mode_unaffected(self) -> None:
        """INTERRUPT 模式阈值不受自适应影响。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)
        proc._cfg.interrupt_threshold_headphone = 0.6
        proc._cfg.interrupt_threshold_speaker = 0.85

        _feed_silence_frames(proc, prob=0.45, n=50)

        proc._use_headphone = True
        assert proc._get_threshold(VADMode.INTERRUPT) == 0.6
        proc._use_headphone = False
        assert proc._get_threshold(VADMode.INTERRUPT) == 0.85

    def test_sliding_window_bounded(self) -> None:
        """噪声窗口大小不超过 _NOISE_WINDOW_FRAMES。"""
        proc = _make_processor()
        _feed_silence_frames(proc, prob=0.10, n=_NOISE_WINDOW_FRAMES + 50)
        assert len(proc._noise_probs) == _NOISE_WINDOW_FRAMES

    def test_window_adapts_to_changing_noise(self) -> None:
        """噪声水平变化时，窗口逐步适应新水平。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.10)

        _feed_silence_frames(proc, prob=0.10, n=_NOISE_WINDOW_FRAMES)
        assert proc.effective_listen_threshold == 0.5

        _feed_silence_frames(proc, prob=0.45, n=_NOISE_WINDOW_FRAMES)
        assert proc.effective_listen_threshold == pytest.approx(0.55, abs=0.01)

    def test_custom_margin(self) -> None:
        """自定义 margin 生效。"""
        proc = _make_processor(silence_threshold=0.5, margin=0.05)
        _feed_silence_frames(proc, prob=0.45, n=50)

        expected = 0.45 + 0.05  # 0.50 — 与静态相等
        assert proc.effective_listen_threshold == pytest.approx(expected, abs=0.01)

    def test_only_silence_frames_tracked(self) -> None:
        """只有 SILENCE 状态下未超阈值的帧进入噪声窗口。"""
        proc = _make_processor(silence_threshold=0.5, adaptive=True, margin=0.10)
        chunk = np.zeros(CHUNK_SAMPLES, dtype=np.float32)

        # 先喂一些低概率帧建立基线
        with patch.object(proc, "_get_speech_prob", return_value=0.10):
            for _ in range(40):
                proc.process_chunk(chunk, mode=VADMode.LISTEN)

        initial_len = len(proc._noise_probs)

        # 喂高概率帧（超阈值 → 进入 CONFIRMING_SPEECH，不应追加到噪声窗口）
        with patch.object(proc, "_get_speech_prob", return_value=0.80):
            proc.process_chunk(chunk, mode=VADMode.LISTEN)

        assert len(proc._noise_probs) == initial_len

    def test_config_from_dict_adaptive_fields(self) -> None:
        """VADConfig.from_dict 正确解析自适应字段。"""
        cfg = VADConfig.from_dict({
            "adaptive_threshold": False,
            "adaptive_margin": 0.15,
        })
        assert cfg.adaptive_threshold is False
        assert cfg.adaptive_margin == 0.15

    def test_config_from_dict_defaults(self) -> None:
        """VADConfig.from_dict 自适应字段缺省值正确。"""
        cfg = VADConfig.from_dict({})
        assert cfg.adaptive_threshold is True
        assert cfg.adaptive_margin == 0.10
