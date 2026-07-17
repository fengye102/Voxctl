"""core/local_settings.py 单元测试（P2 #8）。

覆盖：load / save / apply_local_overrides / effective_* / keyring 路径。
全部用 tmp_path 隔离文件系统，monkeypatch 隔离环境变量和 keyring。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from core import local_settings as ls


# ── helpers ──────────────────────────────────────────────────────────────

def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def _make_config() -> dict:
    """最小可用 config 骨架（模拟 config.yaml 被 yaml.safe_load 后的 dict）。"""
    return {
        "llm": {
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "base_url": "",
            "api_key_env": "TEST_API_KEY",
        },
        "asr": {"model_size": "small"},
        "tts": {"voice": "zh-CN-XiaoxiaoNeural"},
    }


# ── load_local_settings ─────────────────────────────────────────────────

class TestLoadLocalSettings:
    def test_file_not_found(self, tmp_path: Path) -> None:
        assert ls.load_local_settings(tmp_path / "no.yaml") == {}

    def test_corrupted_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(":::\n  - [invalid", encoding="utf-8")
        assert ls.load_local_settings(p) == {}

    def test_non_mapping(self, tmp_path: Path) -> None:
        p = tmp_path / "list.yaml"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        assert ls.load_local_settings(p) == {}

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        assert ls.load_local_settings(p) == {}

    def test_normal(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.yaml"
        _write_yaml(p, {"llm": {"model": "gpt-4o"}})
        result = ls.load_local_settings(p)
        assert result == {"llm": {"model": "gpt-4o"}}


# ── load_local_section ───────────────────────────────────────────────────

class TestLoadLocalSection:
    def test_missing_section(self, tmp_path: Path) -> None:
        p = tmp_path / "s.yaml"
        _write_yaml(p, {"llm": {"model": "x"}})
        assert ls.load_local_section("asr", p) == {}

    def test_non_dict_section(self, tmp_path: Path) -> None:
        p = tmp_path / "s.yaml"
        _write_yaml(p, {"asr": "not_a_dict"})
        assert ls.load_local_section("asr", p) == {}

    def test_normal_section(self, tmp_path: Path) -> None:
        p = tmp_path / "s.yaml"
        _write_yaml(p, {"asr": {"model_size": "medium"}})
        assert ls.load_local_section("asr", p) == {"model_size": "medium"}


# ── save_local_settings ──────────────────────────────────────────────────

class TestSaveLocalSettings:
    def test_basic_write(self, tmp_path: Path) -> None:
        p = tmp_path / "out.yaml"
        ls.save_local_settings({"llm": {"model": "test"}}, p)
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert data == {"llm": {"model": "test"}}

    def test_header_present(self, tmp_path: Path) -> None:
        p = tmp_path / "out.yaml"
        ls.save_local_settings({"llm": {"model": "test"}}, p)
        text = p.read_text(encoding="utf-8")
        assert text.startswith("# Voxctl")

    def test_strips_api_key_from_llm(self, tmp_path: Path) -> None:
        p = tmp_path / "out.yaml"
        ls.save_local_settings(
            {"llm": {"model": "test", "api_key": "sk-secret"}}, p
        )
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert "api_key" not in data.get("llm", {})

    def test_strips_api_key_leaves_empty_llm(self, tmp_path: Path) -> None:
        """llm 块只有 api_key → 剥掉后整块消失。"""
        p = tmp_path / "out.yaml"
        ls.save_local_settings({"llm": {"api_key": "sk-secret"}}, p)
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert data is None or "llm" not in data

    def test_empty_blocks_stripped(self, tmp_path: Path) -> None:
        p = tmp_path / "out.yaml"
        ls.save_local_settings({"asr": {}}, p)
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert data is None or "asr" not in data

    def test_non_dict_blocks_stripped(self, tmp_path: Path) -> None:
        p = tmp_path / "out.yaml"
        ls.save_local_settings({"asr": "bad"}, p)
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert data is None or "asr" not in data

    def test_preserves_other_sections(self, tmp_path: Path) -> None:
        p = tmp_path / "out.yaml"
        ls.save_local_settings(
            {"llm": {"model": "a"}, "asr": {"model_size": "medium"}, "tts": {"voice": "v"}},
            p,
        )
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert data["asr"]["model_size"] == "medium"
        assert data["tts"]["voice"] == "v"


# ── save_local_llm ───────────────────────────────────────────────────────

class TestSaveLocalLlm:
    def test_preserves_existing_asr_tts(self, tmp_path: Path) -> None:
        p = tmp_path / "out.yaml"
        _write_yaml(p, {"asr": {"model_size": "large"}, "tts": {"voice": "v2"}})
        ls.save_local_llm({"model": "new-model"}, p)
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert data["llm"]["model"] == "new-model"
        assert data["asr"]["model_size"] == "large"
        assert data["tts"]["voice"] == "v2"

    def test_no_existing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "new.yaml"
        ls.save_local_llm({"provider": "openai"}, p)
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert data["llm"]["provider"] == "openai"


# ── apply_local_overrides ────────────────────────────────────────────────

class TestApplyLocalOverrides:
    def test_no_local_file_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_config()
        original = dict(config["llm"])
        ls.apply_local_overrides(config, tmp_path / "missing.yaml")
        assert config["llm"]["model"] == original["model"]

    def test_llm_fields_overridden(self, tmp_path: Path) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"llm": {"model": "gpt-4o", "provider": "openai"}})
        config = _make_config()
        ls.apply_local_overrides(config, p)
        assert config["llm"]["model"] == "gpt-4o"
        assert config["llm"]["provider"] == "openai"

    def test_empty_string_not_overridden(self, tmp_path: Path) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"llm": {"model": "", "base_url": "  "}})
        config = _make_config()
        original_model = config["llm"]["model"]
        ls.apply_local_overrides(config, p)
        assert config["llm"]["model"] == original_model

    def test_asr_tts_overridden(self, tmp_path: Path) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"asr": {"model_size": "large"}, "tts": {"voice": "v2"}})
        config = _make_config()
        ls.apply_local_overrides(config, p)
        assert config["asr"]["model_size"] == "large"
        assert config["tts"]["voice"] == "v2"

    def test_api_key_from_keyring_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"llm": {}})
        config = _make_config()
        env_name = config["llm"]["api_key_env"]
        monkeypatch.delenv(env_name, raising=False)

        mock_kr = MagicMock()
        mock_kr.get_password.return_value = "kr-secret-key"
        with patch.object(ls, "_keyring", mock_kr):
            ls.apply_local_overrides(config, p)

        assert os.environ.get(env_name) == "kr-secret-key"
        monkeypatch.delenv(env_name, raising=False)

    def test_api_key_yaml_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"llm": {"api_key": "yaml-key"}})
        config = _make_config()
        env_name = config["llm"]["api_key_env"]
        monkeypatch.delenv(env_name, raising=False)

        with patch.object(ls, "_keyring", None):
            ls.apply_local_overrides(config, p)

        assert os.environ.get(env_name) == "yaml-key"
        monkeypatch.delenv(env_name, raising=False)

    def test_no_key_anywhere_env_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"llm": {}})
        config = _make_config()
        env_name = config["llm"]["api_key_env"]
        monkeypatch.delenv(env_name, raising=False)

        with patch.object(ls, "_keyring", None):
            ls.apply_local_overrides(config, p)

        assert env_name not in os.environ

    def test_returns_config(self, tmp_path: Path) -> None:
        config = _make_config()
        ret = ls.apply_local_overrides(config, tmp_path / "nope.yaml")
        assert ret is config


# ── keyring save / load / delete ─────────────────────────────────────────

class TestKeyring:
    def test_save_and_load(self) -> None:
        mock_kr = MagicMock()
        mock_kr.get_password.return_value = "my-key"
        with patch.object(ls, "_keyring", mock_kr):
            assert ls.save_api_key("my-key", "ENV") is True
            mock_kr.set_password.assert_called_once_with(ls.KEYRING_SERVICE, "ENV", "my-key")
            assert ls.load_api_key("ENV") == "my-key"

    def test_save_empty_key(self) -> None:
        assert ls.save_api_key("", "ENV") is False
        assert ls.save_api_key("  ", "ENV") is False

    def test_save_no_keyring(self) -> None:
        with patch.object(ls, "_keyring", None):
            assert ls.save_api_key("key", "ENV") is False

    def test_load_no_keyring_no_yaml(self, tmp_path: Path) -> None:
        with patch.object(ls, "_keyring", None):
            assert ls.load_api_key("ENV", tmp_path / "no.yaml") == ""

    def test_load_keyring_fails_yaml_fallback(self, tmp_path: Path) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"llm": {"api_key": "yaml-fb"}})
        mock_kr = MagicMock()
        mock_kr.get_password.side_effect = Exception("keyring broke")
        with patch.object(ls, "_keyring", mock_kr):
            assert ls.load_api_key("ENV", p) == "yaml-fb"

    def test_delete_success(self) -> None:
        mock_kr = MagicMock()
        with patch.object(ls, "_keyring", mock_kr):
            assert ls.delete_api_key("ENV") is True
            mock_kr.delete_password.assert_called_once()

    def test_delete_no_keyring(self) -> None:
        with patch.object(ls, "_keyring", None):
            assert ls.delete_api_key("ENV") is False


# ── effective_llm_settings ───────────────────────────────────────────────

class TestEffectiveLlmSettings:
    def test_no_local_file(self, tmp_path: Path) -> None:
        config_llm = {"provider": "anthropic", "model": "h", "base_url": "", "api_key_env": "K"}
        with patch.object(ls, "_keyring", None):
            result = ls.effective_llm_settings(config_llm, tmp_path / "no.yaml")
        assert result["provider"] == "anthropic"
        assert result["overridden"] == []
        assert result["has_local_key"] is False

    def test_with_local_overrides(self, tmp_path: Path) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"llm": {"model": "gpt-4o"}})
        config_llm = {"provider": "anthropic", "model": "h", "base_url": "", "api_key_env": "K"}
        with patch.object(ls, "_keyring", None):
            result = ls.effective_llm_settings(config_llm, p)
        assert result["model"] == "gpt-4o"
        assert "model" in result["overridden"]

    def test_has_local_key_via_keyring(self, tmp_path: Path) -> None:
        config_llm = {"provider": "a", "model": "m", "base_url": "", "api_key_env": "K"}
        mock_kr = MagicMock()
        mock_kr.get_password.return_value = "secret"
        with patch.object(ls, "_keyring", mock_kr):
            result = ls.effective_llm_settings(config_llm, tmp_path / "no.yaml")
        assert result["has_local_key"] is True


# ── effective_section_settings ───────────────────────────────────────────

class TestEffectiveSectionSettings:
    def test_no_override(self, tmp_path: Path) -> None:
        result = ls.effective_section_settings(
            {"model_size": "small"}, "asr", ls._ASR_OVERRIDE_FIELDS,
            tmp_path / "no.yaml",
        )
        assert result["model_size"] == "small"
        assert result["overridden"] == []

    def test_with_override(self, tmp_path: Path) -> None:
        p = tmp_path / "local.yaml"
        _write_yaml(p, {"asr": {"model_size": "large"}})
        result = ls.effective_section_settings(
            {"model_size": "small"}, "asr", ls._ASR_OVERRIDE_FIELDS, p,
        )
        assert result["model_size"] == "large"
        assert "model_size" in result["overridden"]
