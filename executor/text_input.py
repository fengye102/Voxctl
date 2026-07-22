"""Windows 文本注入模块（听写模式用，CLAUDE.md §6 修订 56）。

把一段文本用 ctypes ``SendInput`` + ``KEYEVENTF_UNICODE`` **逐字符注入当前
焦点窗口**，用于把 ASR 识别结果直接"打"进正在编辑的文档/输入框。

**硬性安全约束**：只注入字面 Unicode 字符，**绝不注入回车(VK_RETURN)/Tab/
修饰键组合**——听写不能凭自身提交表单或执行命令。换行符 ``\\n``/``\\r``、
``\\t`` 及其它控制字符（码点 < 0x20）一律**跳过不注入**，计数不含跳过的字符。

实现用纯 ctypes（user32.SendInput + INPUT/KEYBDINPUT 结构体），不引入
pywin32，**不 import subprocess / os / os.system**（本模块根本不需要）。

非 Windows 平台抛 ``TextInputError``。异常向上抛，由上层 executor 兜底
包装成失败的 ExecutionResult（本模块只返回注入字符数或抛异常）。
"""

from __future__ import annotations

import sys

# ---- Win32 SendInput 常量 ----
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP = 0x0002
_INPUT_KEYBOARD = 1


class TextInputError(Exception):
    """文本注入失败（非 Windows、SendInput 报错等）。"""


def _get_send_input():
    """懒获取 ``user32.SendInput``，显式声明 argtypes/restype。

    拆成独立函数是为了让测试 monkeypatch 掉真实 SendInput——测试绝不能
    真的往当前窗口注入按键。仅在 Windows 上可调用。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    # SendInput(UINT cInputs, LPINPUT pInputs, int cbSize) -> UINT
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    return user32.SendInput


def _make_input_types():
    """构造并返回 (INPUT, KEYBDINPUT) ctypes 结构体类型。

    KEYBDINPUT: wVk, wScan, dwFlags, time, dwExtraInfo
    INPUT: type=INPUT_KEYBOARD(1) + union(ki)
    dwExtraInfo 用 ULONG_PTR（wintypes.WPARAM），保证 64 位下指针不被截断。
    """
    import ctypes
    from ctypes import wintypes

    ULONG_PTR = wintypes.WPARAM  # 平台相关宽度：32 位=DWORD，64 位=8 字节

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("union", _INPUTUNION),
        ]

    return INPUT, KEYBDINPUT


def _char_to_inputs(ch: str) -> list:
    """把单个字符转成一对 INPUT 事件（keydown + keyup，均带 KEYEVENTF_UNICODE）。

    wScan=ord(ch)，wVk=0——UNICODE 注入靠扫描码字段传字符，不走虚拟键，
    因此不可能触发 VK_RETURN/Tab/修饰键等按键语义（安全约束的物理保证）。
    调用方应已过滤控制字符与 BMP 外字符。
    """
    INPUT, KEYBDINPUT = _make_input_types()
    code = ord(ch)

    down = INPUT()
    down.type = _INPUT_KEYBOARD
    down.union.ki = KEYBDINPUT(
        wVk=0, wScan=code, dwFlags=_KEYEVENTF_UNICODE, time=0, dwExtraInfo=0
    )

    up = INPUT()
    up.type = _INPUT_KEYBOARD
    up.union.ki = KEYBDINPUT(
        wVk=0,
        wScan=code,
        dwFlags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP,
        time=0,
        dwExtraInfo=0,
    )
    return [down, up]


def _should_inject(ch: str) -> bool:
    """是否注入该字符。

    过滤掉：
      - 控制字符（码点 < 0x20，含 \\n \\r \\t）——安全约束，绝不注入
      - BMP 外字符（码点 > 0xFFFF）——MVP 跳过，wScan 只有 16 位
    """
    code = ord(ch)
    return 0x20 <= code <= 0xFFFF


def inject_text(text: str) -> int:
    """把 text 逐字符用 SendInput(KEYEVENTF_UNICODE) 注入当前焦点窗口。

    - 跳过 \\n \\r \\t 及其它控制字符（码点 < 0x20），以及 BMP 外字符——
      只注入可见字符，返回值不含被跳过的字符（安全约束，见模块 docstring）
    - 返回实际注入的字符数
    - 非 Windows：抛 TextInputError("文本注入仅支持 Windows")
    - SendInput 失败（返回 0 或抛异常）：抛 TextInputError

    每个字符发一对 INPUT 事件（keydown 带 KEYEVENTF_UNICODE，keyup 带
    KEYEVENTF_UNICODE|KEYEVENTF_KEYUP），wScan=ord(ch)，wVk=0。
    """
    if sys.platform != "win32":
        raise TextInputError("文本注入仅支持 Windows")

    if not text:
        return 0

    send_input = _get_send_input()

    injected = 0
    for ch in text:
        if not _should_inject(ch):
            continue
        inputs = _char_to_inputs(ch)
        n = len(inputs)
        arr = (type(inputs[0]) * n)(*inputs)
        try:
            import ctypes

            sent = send_input(n, ctypes.cast(arr, ctypes.c_void_p),
                              ctypes.sizeof(inputs[0]))
        except Exception as exc:  # noqa: BLE001
            raise TextInputError(
                f"SendInput 调用异常: {type(exc).__name__}: {exc}"
            ) from exc
        if sent != n:
            raise TextInputError(
                f"SendInput 注入失败：期望 {n} 个事件，实际 {sent}（字符 {ch!r}）"
            )
        injected += 1

    return injected


__all__ = ["TextInputError", "inject_text"]
