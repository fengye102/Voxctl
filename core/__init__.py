"""core 模块：主循环 + 状态机 + 打断协调器（CLAUDE.md §5 Step 6/7）。"""

from core.event_loop import MainLoop, MainLoopError
from core.interrupt_coordinator import InterruptCoordinator
from core.state import LoopState, PendingConfirmation, State

__all__ = [
    "MainLoop",
    "MainLoopError",
    "InterruptCoordinator",
    "LoopState",
    "PendingConfirmation",
    "State",
]
