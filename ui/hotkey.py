"""全局键盘热键监听（CLAUDE.md §8.11 / §6 / 修订 33）。

给状态浮窗（光圈）配一个全局热键，按下即 toggle 语音子进程启停——与「点击光圈」
是同一个非破坏性生命周期操作的等价触发（§6/§19 早已许可，免二次确认）。

实现要点（纯 ctypes，stdlib，不引新依赖、非 IPC/socket/管道）：
    - Win32 `RegisterHotKey(NULL, id, mods, vk)` 注册系统级热键。NULL 窗口句柄意味着
      热键消息 `WM_HOTKEY` 投递到**注册它的线程**的消息队列，故必须有一个自己的线程
      跑 `GetMessageW` 消息循环来收——这就是 `_run` 干的事。
    - 停止：`PostThreadMessageW(tid, WM_QUIT, 0, 0)` 让 `GetMessageW` 返回 0，循环退出，
      退出前 `UnregisterHotKey` 释放热键（否则重启注册会 RegisterHotKey 失败）。
    - 改热键：`set_hotkey()` 停旧线程再起新线程重注册（RegisterHotKey 不能原地改）。
    - 热键被别的程序占用 → `RegisterHotKey` 返回 0，`register_error` 记下原因供 GUI 回报，
      不静默、也不崩（§6 修订 33：热键被占用要回报错误）。

非 Windows：整体 no-op（RegisterHotKey/WM_HOTKEY 都是 Win32 概念，本项目 Windows 专属）。

回调约定：`on_trigger` 由调用方（webui.gui.run_gui）传入，就是那个 toggle 闭包。回调在
监听线程上执行，抛异常一律吞掉——热键线程绝不能因回调出错而崩（同浮窗"失败不崩"原则）。
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

_LOG = logging.getLogger(__name__)

# ── Win32 常量 ───────────────────────────────────────────────────────────
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000  # 按住不重复触发（Win7+），避免长按刷屏
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 1

_MOD_NAMES = {
    "ctrl": _MOD_CONTROL,
    "control": _MOD_CONTROL,
    "alt": _MOD_ALT,
    "shift": _MOD_SHIFT,
    "win": _MOD_WIN,
    "super": _MOD_WIN,
    "cmd": _MOD_WIN,
}

# 主键名 → 虚拟键码（VK）。字母/数字用 ord()（VK 与 ASCII 大写一致），
# 这里只列非字母数字的特殊键。
_VK_NAMES = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


class HotkeyError(ValueError):
    """热键字符串非法（无修饰键 / 无主键 / 未知键名）。"""


def parse_hotkey(spec: str) -> tuple[int, int]:
    """把 "ctrl+alt+v" 解析成 (mods, vk)。

    规则：
        - 用 + 分隔，大小写不敏感，容忍多余空格。
        - 至少一个修饰键（ctrl/alt/shift/win）——全局热键无修饰键太容易误触发，拒绝。
        - 恰好一个主键（字母数字或 _VK_NAMES 里的特殊键）。
    Returns:
        (mods, vk)：mods 已 OR 上 MOD_NOREPEAT；vk 是虚拟键码。
    Raises:
        HotkeyError：格式不合法。
    """
    if not spec or not isinstance(spec, str):
        raise HotkeyError("热键不能为空")
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyError("热键不能为空")

    mods = 0
    main_key: Optional[str] = None
    for p in parts:
        if p in _MOD_NAMES:
            mods |= _MOD_NAMES[p]
        elif main_key is None:
            main_key = p
        else:
            raise HotkeyError(f"热键只能有一个主键，多了 {p!r}")

    if mods == 0:
        raise HotkeyError("热键至少要带一个修饰键（ctrl/alt/shift/win），否则易误触发")
    if main_key is None:
        raise HotkeyError("热键缺少主键（如 v / space / f8）")

    vk = _resolve_vk(main_key)
    if vk is None:
        raise HotkeyError(f"未知的主键 {main_key!r}")
    return mods | _MOD_NOREPEAT, vk


def _resolve_vk(key: str) -> Optional[int]:
    """主键名 → 虚拟键码。单个字母/数字用 ASCII 大写码，其余查 _VK_NAMES。"""
    if len(key) == 1:
        ch = key.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch)
        return None
    return _VK_NAMES.get(key)


class HotkeyListener:
    """全局热键监听器。线程安全启停 + 改键。非 Windows / 解析失败时安全降级。

    典型用法（webui.gui.run_gui）：
        listener = HotkeyListener(on_trigger=toggle_closure)
        err = listener.start("ctrl+alt+v")   # 返回错误信息或 None
        ...
        listener.set_hotkey("ctrl+shift+space")  # 改键，返回错误信息或 None
        listener.stop()
    """

    def __init__(self, on_trigger: Callable[[], None]) -> None:
        self._on_trigger = on_trigger
        self._thread: Optional[threading.Thread] = None
        self._tid: Optional[int] = None          # 监听线程的 Win32 线程 id（PostThreadMessage 用）
        self._tid_ready = threading.Event()
        self._register_error: Optional[str] = None
        self._lock = threading.Lock()

    # ── 对外接口 ──────────────────────────────────────────────────────
    def start(self, spec: str) -> Optional[str]:
        """注册并开始监听热键。返回错误信息（字符串）或 None（成功）。

        非 Windows 平台直接返回一句说明（不视为致命错误，调用方按需忽略）。
        """
        if sys.platform != "win32":
            _LOG.info("hotkey listener no-op on non-Windows platform")
            return "全局热键仅 Windows 支持"
        try:
            mods, vk = parse_hotkey(spec)
        except HotkeyError as exc:
            return str(exc)

        with self._lock:
            self._stop_locked()
            self._register_error = None
            self._tid_ready.clear()
            self._thread = threading.Thread(
                target=self._run, args=(mods, vk, spec), name="hotkey-listener",
                daemon=True,
            )
            self._thread.start()

        # 等线程把 RegisterHotKey 的结果填进 _register_error（或成功）
        self._tid_ready.wait(timeout=2.0)
        return self._register_error

    def set_hotkey(self, spec: str) -> Optional[str]:
        """改热键：停旧的再起新的。返回错误信息或 None。"""
        return self.start(spec)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    # ── 内部 ──────────────────────────────────────────────────────────
    def _stop_locked(self) -> None:
        """在持锁状态下停掉当前监听线程（PostThreadMessage WM_QUIT）。"""
        t = self._thread
        tid = self._tid
        if t is None:
            return
        if tid is not None and sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("PostThreadMessage WM_QUIT failed: %s", exc)
        if t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._tid = None

    def _run(self, mods: int, vk: int, spec: str) -> None:
        """监听线程：注册热键 → 跑消息循环 → 收 WM_HOTKEY 触发回调 → WM_QUIT 退出。"""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # 记下本线程 tid，供 stop() 的 PostThreadMessage 定位
            self._tid = kernel32.GetCurrentThreadId()

            # 必须先让消息队列存在：GetMessage 首次调用会建队列；RegisterHotKey 用 NULL hwnd
            # 时热键消息发到本线程队列。
            if not user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk):
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                self._register_error = (
                    f"热键 {spec!r} 注册失败（可能已被其他程序占用，错误码 {err}）"
                )
                _LOG.warning("RegisterHotKey failed for %s: err=%s", spec, err)
                self._tid_ready.set()
                return

            _LOG.info("hotkey registered: %s (mods=0x%x vk=0x%x)", spec, mods, vk)
            self._register_error = None
            self._tid_ready.set()

            msg = wintypes.MSG()
            try:
                while True:
                    ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if ret == 0:       # WM_QUIT
                        break
                    if ret == -1:      # GetMessage 出错
                        _LOG.warning("GetMessageW returned -1, stopping hotkey loop")
                        break
                    if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                        self._fire()
            finally:
                user32.UnregisterHotKey(None, _HOTKEY_ID)
                _LOG.info("hotkey unregistered: %s", spec)
        except Exception:  # noqa: BLE001 —— 监听线程绝不能把异常抛出去
            _LOG.exception("hotkey listener thread crashed")
            self._register_error = self._register_error or "热键监听线程异常"
            self._tid_ready.set()

    def _fire(self) -> None:
        """触发回调。回调抛异常一律吞掉，不让热键线程崩。"""
        try:
            self._on_trigger()
        except Exception:  # noqa: BLE001
            _LOG.exception("hotkey on_trigger callback raised (ignored)")


__all__ = ["HotkeyListener", "parse_hotkey", "HotkeyError"]
