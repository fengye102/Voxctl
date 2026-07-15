"""Voxctl 入口：启动 asyncio 主循环（CLAUDE.md §2 启动与退出方式）。

用法：
    python main.py                # 用默认 config.yaml
    python main.py --config xxx.yaml

退出方式（CLAUDE.md §2）：
    - Ctrl+C：本文件捕获 KeyboardInterrupt，清理资源退出
    - 语音"退出程序"：走正常意图流程，ExitProgramIntent → 主循环 break
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from core import MainLoop


def setup_logging() -> None:
    """配置日志：控制台 INFO + 文件轮转 DEBUG（CLAUDE.md §6 审计日志是单独的 actions.log）。"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 控制台：INFO，UTF-8（§8.6）。frozen --windowed 场景下 sys.stdout 可能为 None
    # （GUI exe 无控制台），此时跳过控制台 handler，只写文件，避免 StreamHandler 崩（修订 19）。
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        # Windows 控制台默认非 UTF-8（§8.6），强制重配
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        root.addHandler(console)

    # 文件：DEBUG，10MB 轮转保留 3 份
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "voxctl.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)


def main() -> int:
    # PyInstaller frozen 模式（修订 18）：项目根 = exe 所在目录，chdir 过去，
    # 让 config.yaml / user_apps.yaml / logs/ 都按 exe 所在目录相对解析。
    # 源码模式（python main.py）下 sys.frozen 不存在，保持在项目根运行，行为不变。
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)

    ap = argparse.ArgumentParser(description="Voxctl 语音助手")
    ap.add_argument(
        "--config", default="config.yaml",
        help="配置文件路径（默认 config.yaml）",
    )
    args = ap.parse_args()

    setup_logging()
    _LOG = logging.getLogger("main")

    try:
        loop = MainLoop.from_config_file(args.config)
    except FileNotFoundError as exc:
        print(f"[错误] 配置文件不存在：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("初始化失败")
        print(f"[错误] 初始化失败：{exc}", file=sys.stderr)
        return 1

    try:
        asyncio.run(loop.run())
    except KeyboardInterrupt:
        _LOG.info("收到 Ctrl+C，退出")
        print("\n=== 收到 Ctrl+C，正在退出 ===")
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("主循环异常退出")
        print(f"[错误] 主循环异常：{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
