"""固定文件夹插件测试（修订 62）。"""

from pathlib import Path

import pytest

import plugins.known_folder as known_folder
from plugins import PluginRegistry
from plugins.base import PluginSpec
from plugins.known_folder import (
    PLUGIN_SPEC,
    KnownFolderError,
    _resolve_known_folder,
    handle_open_known_folder,
)


@pytest.mark.parametrize(
    ("folder", "label"),
    [("desktop", "桌面"), ("documents", "文档"), ("downloads", "下载")],
)
def test_allowed_folder_is_opened(monkeypatch, tmp_path, folder, label):
    target = tmp_path / folder
    target.mkdir()
    opened = []
    monkeypatch.setattr(known_folder, "_resolve_known_folder", lambda value: target)
    monkeypatch.setattr(known_folder.os, "startfile", opened.append, raising=False)

    result = handle_open_known_folder({"folder": folder}, False)

    assert result.success is True
    assert result.payload["message"] == f"已打开{label}文件夹"
    assert result.payload["resolved_path"] == str(target)
    assert opened == [str(target)]


@pytest.mark.parametrize("folder", [None, "", "pictures", "C:\\Windows", 1, []])
def test_unknown_folder_is_rejected_without_resolution(monkeypatch, folder):
    called = False

    def resolve(value):
        nonlocal called
        called = True

    monkeypatch.setattr(known_folder, "_resolve_known_folder", resolve)
    result = handle_open_known_folder({"folder": folder}, False)

    assert result.success is False
    assert result.payload["message"] == "只能打开桌面、文档或下载文件夹"
    assert called is False


def test_non_windows_is_rejected(monkeypatch):
    monkeypatch.setattr(known_folder.sys, "platform", "linux")
    with pytest.raises(KnownFolderError, match="仅支持 Windows"):
        _resolve_known_folder("desktop")


def test_missing_known_folder_is_reported(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    monkeypatch.setattr(known_folder, "_resolve_known_folder", lambda value: missing)
    result = handle_open_known_folder({"folder": "downloads"}, False)
    assert result.success is False
    assert result.payload["message"] == "下载文件夹不存在"


def test_resolution_failure_is_reported(monkeypatch):
    def fail(value):
        raise KnownFolderError("系统无法解析固定文件夹")

    monkeypatch.setattr(known_folder, "_resolve_known_folder", fail)
    result = handle_open_known_folder({"folder": "documents"}, False)
    assert result.success is False
    assert result.payload["message"] == "系统无法解析固定文件夹"


def test_startfile_failure_is_reported(monkeypatch, tmp_path):
    target = tmp_path / "desktop"
    target.mkdir()
    monkeypatch.setattr(known_folder, "_resolve_known_folder", lambda value: target)

    def fail(path):
        raise OSError("shell unavailable")

    monkeypatch.setattr(known_folder.os, "startfile", fail, raising=False)
    result = handle_open_known_folder({"folder": "desktop"}, False)
    assert result.success is False
    assert result.payload["message"] == "打开桌面文件夹失败：shell unavailable"


def test_plugin_spec_and_registry():
    assert isinstance(PLUGIN_SPEC, PluginSpec)
    assert PLUGIN_SPEC.intent_name == "open_known_folder"
    assert PLUGIN_SPEC.dangerous is False
    assert PLUGIN_SPEC.parameters_schema["properties"]["folder"]["enum"] == [
        "desktop",
        "documents",
        "downloads",
    ]

    registry = PluginRegistry.load_from_directory(
        Path(__file__).resolve().parents[2] / "plugins"
    )
    assert registry.has("open_known_folder")
