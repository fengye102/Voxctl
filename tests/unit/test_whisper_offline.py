"""Whisper 离线模型发现与构建脚本测试（修订 60）。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from asr.asr_engine import ModelLoadError
from asr.whisper_engine import WhisperEngine, find_bundled_model_dir

_REQUIRED = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


def _make_model(root: Path) -> Path:
    model = root / "models" / "whisper"
    model.mkdir(parents=True)
    for name in _REQUIRED:
        (model / name).write_bytes(b"test")
    return model


def test_find_complete_bundled_model(tmp_path):
    model = _make_model(tmp_path)
    assert find_bundled_model_dir(tmp_path) == model


def test_absent_bundled_model_keeps_download_behavior(tmp_path):
    assert find_bundled_model_dir(tmp_path) is None


def test_incomplete_bundled_model_fails_instead_of_downloading(tmp_path):
    model = _make_model(tmp_path)
    (model / "model.bin").unlink()
    with pytest.raises(ModelLoadError, match="model.bin"):
        find_bundled_model_dir(tmp_path)


def test_engine_passes_bundled_path_to_faster_whisper(tmp_path, monkeypatch):
    model = _make_model(tmp_path)
    calls = []

    class FakeWhisperModel:
        def __init__(self, model_ref, **kwargs):
            calls.append((model_ref, kwargs))

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    engine = WhisperEngine(opencc_enabled=False)

    engine._ensure_model_loaded()

    assert calls[0][0] == str(model)


def test_build_scripts_validate_and_bundle_offline_model():
    root = Path(__file__).resolve().parents[2]
    for name in ("build_voice.bat", "build_gui.bat"):
        script = (root / name).read_text(encoding="utf-8")
        assert "VOXCTL_OFFLINE_MODEL_DIR" in script
        assert "model.bin config.json tokenizer.json vocabulary.txt" in script
        assert "models\\whisper" in script
