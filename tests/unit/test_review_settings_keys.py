"""Regressions for settings saves, legacy key scope, and bounded audit-log reads."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from core import local_settings as ls
from webui import gui


def test_hotkey_only_save_preserves_existing_llm_overrides(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "voxctl.local.yaml"
    existing_llm = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "base_url": "https://relay.example/v1",
        "api_key_env": "CUSTOM_LLM_KEY",
    }
    ls.save_local_settings({"llm": existing_llm, "asr": {"model_size": "medium"}}, settings)
    monkeypatch.setattr(ls, "LOCAL_SETTINGS_FILE", str(settings))

    # The GUI's saveHotkey path sends empty LLM/ASR/TTS positional arguments.
    result = gui.Api().save_settings("", "", "", "", "", "", "", False)

    assert result["ok"] is True
    saved = ls.load_local_settings(settings)
    assert saved["llm"] == existing_llm
    assert saved["asr"] == {"model_size": "medium"}
    assert saved["ui"] == {"hotkey_enabled": False}


def test_partial_llm_save_preserves_other_llm_overrides(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "voxctl.local.yaml"
    ls.save_local_settings({"llm": {"provider": "openai", "model": "old", "api_key_env": "CUSTOM_LLM_KEY"}}, settings)
    monkeypatch.setattr(ls, "LOCAL_SETTINGS_FILE", str(settings))

    result = gui.Api().save_settings(model="new")

    assert result["ok"] is True
    assert ls.load_local_llm(settings) == {
        "provider": "openai",
        "model": "new",
        "api_key_env": "CUSTOM_LLM_KEY",
    }


def test_legacy_llm_key_is_not_loaded_for_another_env(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "voxctl.local.yaml"
    settings.write_text(
        yaml.safe_dump({"llm": {"api_key": "legacy-llm-secret", "api_key_env": "LLM_KEY"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ls, "_keyring", None)

    assert ls.load_api_key("LLM_KEY", settings) == "legacy-llm-secret"
    assert ls.load_api_key("ASR_KEY", settings) == ""


def test_legacy_llm_key_without_env_metadata_uses_config_llm_env_only(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "voxctl.local.yaml"
    settings.write_text(yaml.safe_dump({"llm": {"api_key": "legacy-llm-secret"}}), encoding="utf-8")
    monkeypatch.setattr(ls, "_keyring", None)
    for env_name in ("LLM_KEY", "ASR_KEY", "TTS_KEY"):
        monkeypatch.delenv(env_name, raising=False)

    assert ls.load_api_key("ASR_KEY", settings) == ""
    config = {
        "llm": {"api_key_env": "LLM_KEY"},
        "asr": {"api_key_env": "ASR_KEY"},
        "tts": {"api_key_env": "TTS_KEY"},
    }
    ls.apply_local_overrides(config, settings)

    assert ls.os.environ["LLM_KEY"] == "legacy-llm-secret"
    assert "ASR_KEY" not in ls.os.environ
    assert "TTS_KEY" not in ls.os.environ


def test_get_log_reads_tail_in_bounded_chunks(tmp_path: Path, monkeypatch) -> None:
    audit_log = tmp_path / "actions.log"
    with audit_log.open("w", encoding="utf-8") as stream:
        for i in range(10000):
            stream.write(json.dumps({"id": i}) + "\n")
        stream.write("invalid json\n")
        stream.write(json.dumps({"id": 10000}) + "\n")
    monkeypatch.setattr(gui, "_AUDIT_LOG", audit_log)

    original_open = Path.open

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def seek(self, *args):
            return self.stream.seek(*args)

        def tell(self):
            return self.stream.tell()

        def read(self, size: int = -1):
            assert 0 < size <= 8192, "get_log must read bounded chunks"
            return self.stream.read(size)

    def guarded_open(self, *args, **kwargs):
        stream = original_open(self, *args, **kwargs)
        return BoundedReader(stream) if self == audit_log else stream

    monkeypatch.setattr(Path, "open", guarded_open)
    assert gui.Api().get_log(3) == {"entries": [{"id": 10000}, {"id": 9999}]}
