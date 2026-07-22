"""剪贴板读取插件（迭代 B）。

只读取 Windows 剪贴板的**文本内容**并交给 TTS 播报，绝不执行、绝不打开
剪贴板里的任何内容（哪怕是 URL 或看起来像命令的文本）——只是把文字"读出来"。

实现用纯 ctypes（user32 OpenClipboard/GetClipboardData/CloseClipboard
+ kernel32 GlobalLock/GlobalUnlock，CF_UNICODETEXT=13），不引入
pywin32/tkinter，不调用 subprocess/os.system。

非 Windows 平台返回失败说明。
"""

from __future__ import annotations

import sys
from typing import Optional

from executor import ExecutionResult
from plugins.base import PluginSpec

# Clipboard format：CF_UNICODETEXT（Winuser.h）
_CF_UNICODETEXT = 13

# 播报文本上限：超出则截断，避免 TTS 念几分钟
_MAX_CHARS = 200
_TRUNCATE_SUFFIX = "……后面还有内容"

_EMPTY_MESSAGE = "剪贴板是空的"


def _read_clipboard_text() -> Optional[str]:
    """读取剪贴板文本内容（CF_UNICODETEXT）。

    返回：
      - str：剪贴板里的文本（可能为空串）
      - None：剪贴板无文本格式 / 无内容

    仅在 Windows 上有意义。测试可 monkeypatch 此函数，不触碰真实剪贴板。
    异常向上抛，由 handler 兜底成失败的 ExecutionResult。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # 显式声明返回/参数类型，避免 64 位下句柄被截断成 32 位 int
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.OpenClipboard(None):
        raise OSError("无法打开剪贴板（可能被其它程序占用）")
    try:
        if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            # ptr 指向以 NUL 结尾的宽字符（UTF-16）串
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _truncate(text: str) -> str:
    """超长文本截断到 _MAX_CHARS，并追加省略提示。"""
    if len(text) <= _MAX_CHARS:
        return text
    return text[:_MAX_CHARS] + _TRUNCATE_SUFFIX


def handle_clipboard_read(params: dict, confirmed: bool) -> ExecutionResult:
    """处理剪贴板读取意图。

    只读，无参数（params 保留以兼容统一 handler 签名）。绝不执行剪贴板内容。
    """
    del params, confirmed  # 只读、非 dangerous，均不使用

    if sys.platform != "win32":
        return ExecutionResult(
            success=False,
            action_type="clipboard_read",
            error="剪贴板读取仅支持 Windows",
            payload={"message": "当前系统不支持读取剪贴板，仅 Windows 可用"},
        )

    try:
        text = _read_clipboard_text()
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(
            success=False,
            action_type="clipboard_read",
            error=f"读取剪贴板失败: {type(exc).__name__}: {exc}",
            payload={"message": "读取剪贴板失败"},
        )

    # 无文本格式 / 空内容 → 友好提示，仍算成功（不是错误）
    if not text or not text.strip():
        return ExecutionResult(
            success=True,
            action_type="clipboard_read",
            payload={"message": _EMPTY_MESSAGE, "empty": True},
        )

    full_len = len(text)
    spoken = _truncate(text)
    return ExecutionResult(
        success=True,
        action_type="clipboard_read",
        payload={
            "message": f"剪贴板内容是：{spoken}",
            "text": spoken,
            "truncated": full_len > _MAX_CHARS,
            "length": full_len,
        },
    )


PLUGIN_SPEC = PluginSpec(
    intent_name="clipboard_read",
    description=(
        "读出剪贴板里的文字内容。用户说「剪贴板里是什么」「读一下剪贴板」"
        "「我刚复制了什么」「念一下我复制的内容」等时选此工具。"
        "本工具只是把剪贴板里的文字读出来朗读，"
        "绝不会打开、执行或访问其中的链接/命令；"
        "不要用于打开网址、运行命令、打开文件或任何与朗读无关的操作。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    handler=handle_clipboard_read,
    dangerous=False,
)
