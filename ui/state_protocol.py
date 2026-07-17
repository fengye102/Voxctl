"""状态推送协议：语音子进程 → GUI 的 stdout 行 JSON（CLAUDE.md §8.11 / 修订 26）。

单向、无状态、纯函数。每行一个 JSON 对象，字段：
    event   必填，"state" | "asr_text" | "tts_text" | "error" | "exit"
    state   event=="state" 时必填，State 枚举的 value 字符串
            （idle/listen/asr/execute/tts/await_confirm）
    ts      浮点时间戳（time.time()）
    text?   可选，识别文本 / 播报文本；**不含敏感信息**（无 api_key / 无绝对路径）

设计约束：本模块**不 import core.state**（那会拖入 executor/nlu 重依赖）——
GUI 端的 parse_line 只需处理字符串，语音端的 encode 对 State 枚举鸭子类型取 .value。
这样 GUI 进程解析这条协议时零重依赖。

容错：voxctl-voice.out 里混着语音子进程别的 stdout（print 的中文提示等），
parse_line 对非 JSON / 非本协议行一律返回 None，调用方据此跳过。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

# 前缀标记：只有以此开头的行才是状态协议行，快速筛掉子进程别的 stdout，
# 避免对每一行普通中文提示都尝试 json.loads（既省事又降低误判）。
LINE_PREFIX = "@@VOXCTL_STATE@@"

# 合法的 event 取值（parse_line 校验用）
_VALID_EVENTS = frozenset(
    {"state", "asr_text", "tts_text", "error", "exit"}
)


def _state_value(state: Any) -> str:
    """把 State 枚举 / 字符串统一成 value 字符串。鸭子类型，不 import State。"""
    val = getattr(state, "value", state)
    return str(val)


def encode_state_event(state: Any, text: Optional[str] = None) -> str:
    """编码一条 state 事件为协议行（不含结尾换行，调用方负责 print）。

    Args:
        state: State 枚举或其 value 字符串
        text:  可选附带文本（识别文本等），None 则不带
    Returns:
        形如 '@@VOXCTL_STATE@@{"event":"state","state":"asr","ts":...}' 的一行
    """
    payload: dict = {"event": "state", "state": _state_value(state), "ts": time.time()}
    if text:
        payload["text"] = str(text)
    return LINE_PREFIX + json.dumps(payload, ensure_ascii=False)


def encode_event(event: str, **fields: Any) -> str:
    """编码一条非 state 事件（error / exit / asr_text / tts_text）。

    Example:
        encode_event("error", text="识别失败")
        encode_event("exit")
    """
    payload: dict = {"event": event, "ts": time.time()}
    for k, v in fields.items():
        if v is not None:
            payload[k] = v
    return LINE_PREFIX + json.dumps(payload, ensure_ascii=False)


def parse_line(line: str) -> Optional[dict]:
    """解析一行输出。非协议行 / 非法 JSON / 缺 event / event 非法 一律返回 None。

    容错设计（§8.11）：voxctl-voice.out 混着别的 stdout，本函数必须对任何
    脏输入安全返回 None，绝不抛异常（tailer 线程逐行调，不能被一行坏数据搞崩）。
    """
    if not line:
        return None
    if not isinstance(line, str):
        return None
    s = line.strip()
    idx = s.find(LINE_PREFIX)
    if idx < 0:
        return None
    body = s[idx + len(LINE_PREFIX):].strip()
    if not body:
        return None
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    event = obj.get("event")
    if event not in _VALID_EVENTS:
        return None
    if event == "state" and not obj.get("state"):
        return None
    return obj


__all__ = [
    "LINE_PREFIX",
    "encode_state_event",
    "encode_event",
    "parse_line",
]
