"""系统托盘状态测试（修订 59）。"""

from unittest.mock import patch

from ui.tray import StatusTray


def test_non_windows_start_is_noop():
    tray = StatusTray()
    with patch("ui.tray.sys.platform", "linux"):
        assert tray.start() == "系统托盘仅 Windows 支持"
        tray.stop()


def test_update_normalizes_unknown_status():
    tray = StatusTray()
    tray.update("listen")
    assert tray._status == "listen"
    tray.update("not-a-state")
    assert tray._status == "idle"
