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
import os
import sys
from pathlib import Path

from core.factory import create_from_config_file
from core.logger import configure_root_logging


def setup_logging() -> None:
    """配置日志：控制台 INFO + 文件轮转 DEBUG（CLAUDE.md §6 审计日志是单独的 actions.log）。

    共用逻辑（建目录 / RotatingFileHandler / windowless-safe StreamHandler）已收敛到
    core/logger.py（修订 25），这里只传本进程的参数。
    """
    configure_root_logging(
        Path("logs") / "voxctl.log",
        file_level=logging.DEBUG,
        console_level=logging.INFO,
        max_bytes=10 * 1024 * 1024,
        backup_count=3,
    )


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

    from core.version import get_version_info
    _LOG.info("Voxctl %s", get_version_info())

    try:
        loop = create_from_config_file(args.config)
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
