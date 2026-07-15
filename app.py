"""Voxctl 统一入口（CLAUDE.md 修订 19）。

用法：
    python app.py            # 默认：启动 PyWebView 原生 GUI 控制台
    python app.py --voice    # 跑语音主循环（委托 main.main()）
    voxctl.exe               # frozen：默认 GUI（--windowed，无黑窗）
    voxctl.exe --voice       # frozen：语音子进程（GUI 用它拉起 / 杀掉）

设计：单 exe 用 `--voice` 参数复用两种角色——GUI 进程默认走 run_gui()，永不 import
语音模块（不加载 torch/whisper）；语音角色 import main 委托现有 main.main()。GUI 用
subprocess 拉起 `sys.executable --voice` 作为独立子进程（§4/§16：语音主循环不依赖 GUI）。

frozen --windowed 下无控制台，sys.stdout/stderr 为 None——先重定向到 devnull，避免
print()/日志在 emit 时崩；真实日志走 logs/ 文件，GUI 从文件回显。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _redirect_null_std() -> None:
    """frozen --windowed：sys.stdout/stderr 为 None 时重定向到 devnull，避免 print 崩。"""
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
    _redirect_null_std()
    _chdir_exe_dir()

    argv = sys.argv[1:]
    if "--voice" in argv:
        # 语音角色：委托现有 main.main()（剥掉 --voice，其余参数如 --config 透传）
        sys.argv = [sys.argv[0]] + [a for a in argv if a != "--voice"]
        import main as voice_main
        return voice_main.main()

    # 默认角色：GUI 控制台
    from webui.gui import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
