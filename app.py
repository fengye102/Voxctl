"""Voxctl 统一入口（CLAUDE.md 修订 19）。

用法：
    python app.py            # 默认：启动 PyWebView 原生 GUI 控制台
    python app.py --voice    # 跑语音主循环（委托 main.main()）
    voxctl.exe               # frozen：默认 GUI（--windowed，无黑窗）
    voxctl.exe --voice       # frozen：语音子进程（GUI 用它拉起 / 杀掉）

设计：单 exe 用 `--voice` 参数复用两种角色——GUI 进程默认走 run_gui()，永不 import
语音模块（不加载 torch/whisper）；语音角色 import main 委托现有 main.main()。GUI 用
subprocess 拉起 `sys.executable --voice` 作为独立子进程（§4/§16：语音主循环不依赖 GUI）。

frozen --windowed 下无控制台，sys.stdout/stderr 为 None——需重定向避免 print/日志崩。
但两种角色的重定向目标不同（修订 28，修订 50 起浮窗已删，stdout 仅供崩溃回读）：
    - GUI 角色：无人读它的 stdout，扔 devnull 即可。
    - 语音角色：父进程（GUI voice_start）已把子进程 OS 层 fd 1/2 指向
      logs/voxctl-voice.out，若这里再把 sys.stdout 换成 devnull，子进程崩溃
      时的早期 traceback 就进了黑洞、GUI _read_voice_failure_reason 读不到
      失败原因。必须用 os.fdopen(1) 把 Python stdout **接回**那个真实 fd，
      崩溃日志才能落进文件供 GUI 回读。
真实日志仍走 logs/ 文件，GUI 从文件回显。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _reopen_fd(fd: int):
    """把某个 OS 文件描述符包成带 UTF-8 编码的 text stream。失败返回 None。"""
    try:
        return os.fdopen(fd, "w", encoding="utf-8", errors="replace", closefd=False)
    except Exception:  # noqa: BLE001
        return None


def _redirect_std(voice_role: bool) -> None:
    """frozen --windowed：sys.stdout/stderr 为 None 时按角色重定向。

    voice_role=True：接回 fd 1/2（父进程已指向 voxctl-voice.out），让子进程崩溃
      时的早期 traceback 写进文件——GUI _read_voice_failure_reason 才能回读原因。
    voice_role=False（GUI）：无人读，扔 devnull。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    if voice_role:
        if sys.stdout is None:
            sys.stdout = _reopen_fd(1)
        if sys.stderr is None:
            sys.stderr = _reopen_fd(2)
        # fdopen 失败（极少数情况 fd 无效）时兜底 devnull，保证 print 不崩
        if sys.stdout is not None and sys.stderr is not None:
            return

    try:
        null = open(os.devnull, "w", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return
    if sys.stdout is None:
        sys.stdout = null
    if sys.stderr is None:
        sys.stderr = null


def _chdir_exe_dir() -> None:
    """frozen：项目根 = exe 所在目录，chdir 过去（同 main.py 修订 18），
    让 config.yaml / voxctl.local.yaml / user_apps.yaml / logs 按相对路径解析。"""
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)


def main() -> int:
    argv = sys.argv[1:]

    # --version：打印版本信息后退出（不需要重定向或 chdir）
    if "--version" in argv or "-V" in argv:
        from core.version import get_version_info
        print(f"Voxctl {get_version_info()}")
        return 0

    voice_role = "--voice" in argv

    _redirect_std(voice_role)
    _chdir_exe_dir()

    if voice_role:
        # 语音角色：委托现有 main.main()（剥掉 --voice，其余参数如 --config 透传）
        sys.argv = [sys.argv[0]] + [a for a in argv if a != "--voice"]
        import main as voice_main
        return voice_main.main()

    # 默认角色：GUI 控制台
    from webui.gui import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
