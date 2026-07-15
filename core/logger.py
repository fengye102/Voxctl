"""共用日志配置（CLAUDE.md §3 / 修订 25）。

修订 11 起目录树一直写有 `core/logger.py`，但日志逻辑实际散在 `main.py` 的
`setup_logging()` 和 `webui/gui.py` 的 `_setup_gui_logging()` 里——两处结构完全
同构（建 logs 目录 → RotatingFileHandler(UTF-8) → windowless-safe StreamHandler），
只有文件名/级别/大小参数不同。修订 25 把这套共用逻辑收敛到这里，两处改为传参调用，
消除重复、让目录树名实相符。

审计日志（`logs/actions.log`，CLAUDE.md §6）是另一条独立的记录路径，不走这里。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Union


def configure_root_logging(
    log_path: Union[str, Path],
    *,
    file_level: int = logging.DEBUG,
    console_level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """配置 root logger：文件轮转 + （有控制台时）控制台输出。

    参数：
        log_path:      日志文件路径（其父目录不存在会自动创建）
        file_level:    文件 handler 级别（默认 DEBUG）
        console_level: 控制台 handler 级别（默认 INFO）
        max_bytes:     单个日志文件大小上限，超过即轮转
        backup_count:  轮转保留的历史文件份数

    windowless 保护（修订 19）：frozen `--windowed` 场景下 `sys.stdout` 可能为
    None（GUI exe 无控制台），此时跳过控制台 handler，只写文件，避免 StreamHandler 崩。
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(min(file_level, console_level))

    # 控制台：仅在有 stdout 时加。Windows 控制台默认非 UTF-8（§8.6），强制重配。
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(console_level)
        console.setFormatter(fmt)
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        root.addHandler(console)

    # 文件：UTF-8 轮转
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
