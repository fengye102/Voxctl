"""Windows 系统托盘状态图标（纯 ctypes，无第三方依赖）。"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

_LOG = logging.getLogger(__name__)

_STATUS_TEXT = {
    "idle": "已停止",
    "listen": "正在监听",
    "asr": "正在识别",
    "execute": "正在执行",
    "tts": "正在播报",
    "await_confirm": "等待确认",
}


class StatusTray:
    """只读状态托盘。所有 Win32 调用都在独立消息线程中执行。"""

    def __init__(self) -> None:
        self._status = "idle"
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._lock = threading.Lock()

    def start(self) -> Optional[str]:
        """启动托盘线程；返回错误信息或 None。非 Windows 安全降级。"""
        if sys.platform != "win32":
            return "系统托盘仅 Windows 支持"
        with self._lock:
            if self._thread and self._thread.is_alive():
                return None
            self._ready.clear()
            self._error = None
            self._thread = threading.Thread(
                target=self._run, name="status-tray", daemon=True,
            )
            self._thread.start()
        self._ready.wait(timeout=2.0)
        return self._error

    def update(self, status: str) -> None:
        """更新 tooltip；未知状态按 idle 处理。"""
        status = status if status in _STATUS_TEXT else "idle"
        with self._lock:
            if status == self._status:
                return
            self._status = status
            hwnd = self._hwnd
        if hwnd and sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                post = ctypes.windll.user32.PostMessageW
                post.argtypes = [
                    wintypes.HWND, wintypes.UINT,
                    wintypes.WPARAM, wintypes.LPARAM,
                ]
                post.restype = wintypes.BOOL
                post(hwnd, 0x8001, 0, 0)
            except Exception:  # noqa: BLE001
                _LOG.exception("更新托盘状态失败")

    def stop(self) -> None:
        """删除图标并停止消息线程。"""
        with self._lock:
            thread = self._thread
            hwnd = self._hwnd
        if thread is None:
            return
        if hwnd and sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                post = ctypes.windll.user32.PostMessageW
                post.argtypes = [
                    wintypes.HWND, wintypes.UINT,
                    wintypes.WPARAM, wintypes.LPARAM,
                ]
                post.restype = wintypes.BOOL
                post(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:  # noqa: BLE001
                _LOG.exception("停止托盘线程失败")
        if thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            self._thread = None
            self._hwnd = None

    def _run(self) -> None:
        try:
            self._run_windows()
        except Exception as exc:  # noqa: BLE001
            self._error = f"系统托盘启动失败: {type(exc).__name__}: {exc}"
            _LOG.exception("status tray thread crashed")
            self._ready.set()

    def _run_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        wm_update = 0x8001  # WM_APP + 1
        wm_tray = 0x8002
        nim_add, nim_modify, nim_delete = 0, 1, 2
        nif_message, nif_icon, nif_tip = 1, 2, 4

        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        )

        class WndClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NotifyIconData(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
            ]

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
        user32.RegisterClassW.restype = wintypes.WORD
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD, ctypes.POINTER(NotifyIconData),
        ]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        holder = {}

        def apply_status() -> None:
            nid = holder.get("nid")
            if nid is None:
                return
            with self._lock:
                text = _STATUS_TEXT[self._status]
            nid.szTip = f"Voxctl - {text}"
            shell32.Shell_NotifyIconW(nim_modify, ctypes.byref(nid))

        @wndproc_type
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == wm_update:
                apply_status()
                return 0
            if msg == 0x0002:  # WM_DESTROY
                nid = holder.get("nid")
                if nid is not None:
                    shell32.Shell_NotifyIconW(nim_delete, ctypes.byref(nid))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        instance = kernel32.GetModuleHandleW(None)
        class_name = f"VoxctlStatusTray_{id(self)}"
        wc = WndClass()
        wc.lpfnWndProc = wndproc
        wc.hInstance = instance
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError("RegisterClassW failed")

        hwnd = user32.CreateWindowExW(
            0, class_name, "Voxctl Status", 0,
            0, 0, 0, 0, None, None, instance, None,
        )
        if not hwnd:
            user32.UnregisterClassW(class_name, instance)
            raise OSError("CreateWindowExW failed")

        icon_resource = ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR)
        icon = user32.LoadIconW(None, icon_resource)  # IDI_APPLICATION
        nid = NotifyIconData()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = nif_message | nif_icon | nif_tip
        nid.uCallbackMessage = wm_tray
        nid.hIcon = icon
        with self._lock:
            nid.szTip = f"Voxctl - {_STATUS_TEXT[self._status]}"
            self._hwnd = hwnd
        holder["nid"] = nid

        if not shell32.Shell_NotifyIconW(nim_add, ctypes.byref(nid)):
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, instance)
            raise OSError("Shell_NotifyIconW(NIM_ADD) failed")

        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        with self._lock:
            self._hwnd = None
        user32.UnregisterClassW(class_name, instance)


__all__ = ["StatusTray"]
