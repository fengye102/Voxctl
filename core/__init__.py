"""core 模块：主循环 + 状态机 + 打断协调器 + 构造工厂（CLAUDE.md §5 Step 6/7）。"""

from core.event_loop import MainLoop, MainLoopError
from core.factory import create_from_config, create_from_config_file
from core.interrupt_coordinator import InterruptCoordinator
from core.state import LoopState, PendingConfirmation, State

__all__ = [
    "MainLoop",
    "MainLoopError",
    "create_from_config",
    "create_from_config_file",
    "InterruptCoordinator",
    "LoopState",
    "PendingConfirmation",
    "State",
]
