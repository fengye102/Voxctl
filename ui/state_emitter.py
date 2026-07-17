"""语音子进程端：把状态推给 GUI 浮窗（CLAUDE.md §8.11 / 修订 26）。

emit_state() 在 core/event_loop.py 各 _phase_* 入口被调，print 一行协议 JSON 到
stdout——webui.gui.voice_start 已把语音子进程 stdout 重定向到 logs/voxctl-voice.out
（修订 20 的通道），GUI 端 state_tailer 读这个文件。

关键约束（§8.11）：
    - **失败必须静默**：stdout 挂了 / BrokenPipe / 编码错，一律吞掉，绝不能让
      浮窗把语音主循环搞崩（浮窗是可选增强，语音是主功能）。
    - **发送端节流**：同一状态 200ms 内不重复发（状态机在 LISTEN 可能空转很快，
      每次 _phase_listen 入口都发会刷屏 voxctl-voice.out）。状态**切换**总是立即发，
      只压制"同状态重复发"。
    - 不 import core.state：emit_state 接受 State 枚举或字符串（协议层鸭子类型）。

**开关（env gate）**：只有环境变量 VOXCTL_OVERLAY 为真时才真正写 stdout。GUI 的
voice_start 在 config.yaml 的 ui.status_overlay 为 true 时给语音子进程设这个 env；
standalone（run.bat / python main.py）不设，emit 全程 no-op——避免把 @@VOXCTL_STATE@@
协议行打进开发者的控制台。GUI 是浮窗的启动方也是唯一读取方，故 gate 落在启动这一处。

时间源用 time.monotonic()（节流只关心间隔，不受系统时钟调整影响）。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Optional

from ui.state_protocol import encode_event, encode_state_event

# GUI 拉起语音子进程时按 config.ui.status_overlay 设的开关环境变量
_ENABLE_ENV = "VOXCTL_OVERLAY"

# 同状态重复发送的最小间隔（秒）。状态切换不受此限，只压制"同状态连发"。
_THROTTLE_INTERVAL_S = 0.2

# 模块级节流状态（语音子进程单进程单线程访问，不需锁）
_last_state_value: Optional[str] = None
_last_emit_monotonic: float = 0.0


def _enabled() -> bool:
    """是否启用状态推送。每次调用读 env（GUI 在 Popen 前 setenv，子进程继承）。"""
    return os.environ.get(_ENABLE_ENV, "").strip() not in ("", "0", "false", "False")


def _state_value(state: Any) -> str:
    return str(getattr(state, "value", state))


def _write_line(line: str) -> None:
    """把一行写到 stdout 并 flush。任何异常静默吞掉（§8.11 失败不崩主循环）。"""
    if not _enabled():
        return
    try:
        out = sys.stdout
        if out is None:  # frozen --windowed 下可能为 None（app.py 已兜底重定向，双保险）
            return
        out.write(line + "\n")
        out.flush()
    except Exception:  # noqa: BLE001 —— 浮窗推送绝不能影响语音主流程
        return


def emit_state(state: Any, text: Optional[str] = None) -> None:
    """推送一个状态给浮窗。带节流：同状态 200ms 内不重复发，状态切换立即发。

    Args:
        state: State 枚举或其 value 字符串
        text:  可选附带文本（如 ASR 识别文本，用于浮窗展示；不传敏感信息）
    """
    global _last_state_value, _last_emit_monotonic
    val = _state_value(state)
    now = time.monotonic()

    # 状态切换总是立即发；同状态则受节流间隔限制
    if val == _last_state_value and (now - _last_emit_monotonic) < _THROTTLE_INTERVAL_S:
        return

    _last_state_value = val
    _last_emit_monotonic = now
    _write_line(encode_state_event(state, text=text))


def emit_error(text: Optional[str] = None) -> None:
    """推送一个错误事件（浮窗闪红）。text 只用简短提示，不带堆栈/路径。"""
    global _last_state_value, _last_emit_monotonic
    _last_state_value = "__error__"
    _last_emit_monotonic = time.monotonic()
    _write_line(encode_event("error", text=text))


def emit_exit() -> None:
    """推送退出事件，让浮窗关闭 / 归灰。"""
    global _last_state_value, _last_emit_monotonic
    _last_state_value = None
    _last_emit_monotonic = time.monotonic()
    _write_line(encode_event("exit"))


def reset_throttle() -> None:
    """清节流状态（测试用，或语音进程重启时）。"""
    global _last_state_value, _last_emit_monotonic
    _last_state_value = None
    _last_emit_monotonic = 0.0


__all__ = ["emit_state", "emit_error", "emit_exit", "reset_throttle"]
