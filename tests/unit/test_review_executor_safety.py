"""Regression coverage for executor path allowlists and process identity."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil

from executor.app_control import AppController
from executor.file_ops import FileOps
from executor.whitelist import Whitelist
from nlu.schema import CloseAppIntent, OpenFileIntent


def _controller(app_path: Path) -> AppController:
    whitelist = Whitelist(apps={"editor": app_path}, commands={}, paths=[])
    return AppController(whitelist)


def _process(pid: int, exe_path: Path, name: str = "editor.exe") -> MagicMock:
    proc = MagicMock()
    proc.info = {"pid": pid, "name": name}
    proc.exe.return_value = str(exe_path)
    return proc


def test_close_does_not_kill_same_basename_at_another_path(tmp_path: Path) -> None:
    allowed_exe = tmp_path / "installed" / "editor.exe"
    other_exe = tmp_path / "portable" / "editor.exe"
    proc = _process(100, other_exe)

    with patch("executor.app_control.psutil.process_iter", return_value=[proc]):
        result = _controller(allowed_exe).close_app(CloseAppIntent(app_name="editor"), confirmed=True)

    assert result.success
    assert result.payload["terminated_pids"] == []
    assert result.payload["matched_via"] == "none"
    proc.terminate.assert_not_called()


def test_close_does_not_kill_same_name_when_exe_is_unreadable(tmp_path: Path) -> None:
    allowed_exe = tmp_path / "installed" / "editor.exe"
    proc = _process(100, allowed_exe)
    proc.exe.side_effect = psutil.AccessDenied

    with patch("executor.app_control.psutil.process_iter", return_value=[proc]):
        result = _controller(allowed_exe).close_app(CloseAppIntent(app_name="editor"), confirmed=True)

    assert result.success
    assert result.payload["terminated_pids"] == []
    proc.terminate.assert_not_called()


def test_close_resolves_whitelisted_shortcut_to_process_exe(tmp_path: Path) -> None:
    shortcut = tmp_path / "Editor.lnk"
    shortcut.write_bytes(b"fixture")
    target_exe = tmp_path / "installed" / "editor.exe"
    proc = _process(100, target_exe)

    with patch("executor.file_ops._resolve_lnk_target", return_value=target_exe), \
         patch("executor.app_control.psutil.process_iter", return_value=[proc]):
        result = _controller(shortcut).close_app(CloseAppIntent(app_name="editor"), confirmed=True)

    assert result.success
    assert result.payload["terminated_pids"] == [100]
    assert result.payload["matched_via"] == "path"
    proc.terminate.assert_called_once_with()


def test_close_rejects_unresolvable_whitelisted_shortcut(tmp_path: Path) -> None:
    shortcut = tmp_path / "Editor.lnk"
    shortcut.write_bytes(b"fixture")
    proc = _process(100, tmp_path / "installed" / "editor.exe")

    with patch("executor.file_ops._resolve_lnk_target", return_value=None), \
         patch("executor.app_control.psutil.process_iter", return_value=[proc]):
        result = _controller(shortcut).close_app(CloseAppIntent(app_name="editor"), confirmed=True)

    assert not result.success
    assert "无法解析" in result.error
    proc.terminate.assert_not_called()


def test_close_still_requires_confirmation_for_matching_path(tmp_path: Path) -> None:
    allowed_exe = tmp_path / "installed" / "editor.exe"
    proc = _process(100, allowed_exe)

    with patch("executor.app_control.psutil.process_iter", return_value=[proc]):
        result = _controller(allowed_exe).close_app(CloseAppIntent(app_name="editor"), confirmed=False)

    assert result.needs_confirmation
    proc.terminate.assert_not_called()


def test_open_shortcut_rejects_document_outside_allowed_directory(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    shortcut = allowed_dir / "report.lnk"
    shortcut.write_bytes(b"fixture")
    outside_file = tmp_path / "outside" / "report.txt"
    outside_file.parent.mkdir()
    outside_file.write_text("private", encoding="utf-8")
    ops = FileOps(Whitelist(apps={}, commands={}, paths=[allowed_dir]))

    with patch("executor.file_ops._resolve_lnk_target", return_value=outside_file), \
         patch("executor.file_ops.os.startfile") as startfile:
        result = ops.open_file(OpenFileIntent(path=str(shortcut)))

    assert not result.success
    assert "允许范围" in result.error
    startfile.assert_not_called()


def test_open_shortcut_allows_document_inside_allowed_directory(tmp_path: Path) -> None:
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    shortcut = allowed_dir / "report.lnk"
    shortcut.write_bytes(b"fixture")
    target = allowed_dir / "report.txt"
    target.write_text("public", encoding="utf-8")
    ops = FileOps(Whitelist(apps={}, commands={}, paths=[allowed_dir]))

    with patch("executor.file_ops._resolve_lnk_target", return_value=target), \
         patch("executor.file_ops.os.startfile") as startfile:
        result = ops.open_file(OpenFileIntent(path=str(shortcut)))

    assert result.success
    startfile.assert_called_once_with(str(shortcut.resolve()))
