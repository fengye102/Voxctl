"""打开 Windows 桌面、文档或下载文件夹。"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from uuid import UUID

from executor import ExecutionResult
from plugins.base import PluginSpec

_KNOWN_FOLDERS = {
    "desktop": ("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "桌面"),
    "documents": ("FDD39AD0-238F-46AF-ADB4-6C85480369C7", "文档"),
    "downloads": ("374DE290-123F-4565-9164-39C4925E467B", "下载"),
}


class KnownFolderError(Exception):
    """Windows Known Folder 解析失败。"""


def _failure(message: str) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        action_type="open_known_folder",
        error=message,
        payload={"message": message},
    )


def _resolve_known_folder(folder: str) -> Path:
    if sys.platform != "win32":
        raise KnownFolderError("打开固定文件夹仅支持 Windows")

    guid_text, _ = _KNOWN_FOLDERS[folder]
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(UUID(guid_text).bytes_le)
    path_ptr = ctypes.c_wchar_p()

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    get_path = shell32.SHGetKnownFolderPath
    get_path.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    get_path.restype = ctypes.c_long
    result = get_path(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
    if result != 0:
        raise KnownFolderError(
            f"系统无法解析固定文件夹（错误 0x{result & 0xFFFFFFFF:08X}）"
        )

    try:
        if not path_ptr.value:
            raise KnownFolderError("系统返回了空的固定文件夹路径")
        return Path(path_ptr.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(  # type: ignore[attr-defined]
            ctypes.cast(path_ptr, ctypes.c_void_p)
        )


def handle_open_known_folder(params: dict, confirmed: bool) -> ExecutionResult:
    """打开受枚举限制的 Windows Known Folder。"""
    del confirmed
    params = params or {}
    folder = params.get("folder")
    if not isinstance(folder, str) or folder not in _KNOWN_FOLDERS:
        return _failure("只能打开桌面、文档或下载文件夹")

    _, label = _KNOWN_FOLDERS[folder]
    try:
        path = _resolve_known_folder(folder)
    except (KnownFolderError, OSError) as exc:
        return _failure(str(exc))

    if not path.is_dir():
        return _failure(f"{label}文件夹不存在")

    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError as exc:
        return _failure(f"打开{label}文件夹失败：{exc}")

    return ExecutionResult(
        success=True,
        action_type="open_known_folder",
        payload={
            "message": f"已打开{label}文件夹",
            "folder": folder,
            "resolved_path": str(path),
        },
    )


PLUGIN_SPEC = PluginSpec(
    intent_name="open_known_folder",
    description=(
        "打开 Windows 的桌面、文档或下载文件夹。"
        "只用于打开这三个固定文件夹；打开具体文件应使用 open_file，其他目录返回 unknown。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "folder": {
                "type": "string",
                "enum": ["desktop", "documents", "downloads"],
                "description": "要打开的固定文件夹：桌面、文档或下载",
            }
        },
        "required": ["folder"],
    },
    handler=handle_open_known_folder,
    dangerous=False,
)
