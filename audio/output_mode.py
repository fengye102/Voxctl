"""耳机 / 外放输出模式检测。

CLAUDE.md §2（VAD 选型）、§4（MVP）、§8.5（坑说明）：
  - 打断判定的可靠度与输出设备类型强相关：耳机场景播报声基本不泄漏进麦克风，
    外放场景泄漏明显，需要两套不同的 VAD 阈值。
  - 自动检测基于设备名关键词匹配，不保证 100% 准确（已知且接受的取舍）。
  - 检测失败或不确定时默认按外放处理（更保守，见 CLAUDE.md §8.5）。
  - config.yaml 里 output_mode: headphone / speaker / auto 可手动覆盖检测结果。

关键词表有意不追求穷尽（见 §8.5）；遇到检测错误时用 config.yaml 手动覆盖即可。
"""

from __future__ import annotations

import logging
from enum import Enum

_LOG = logging.getLogger(__name__)

# 关键词表：设备名包含任意一个词（忽略大小写）→ 判为耳机模式
_HEADPHONE_KEYWORDS = (
    "headphone",
    "headset",
    "earphone",
    "airpods",
    "buds",
    "earbuds",
    "耳机",
    "耳麦",
    "头戴",
)


class OutputMode(Enum):
    """输出设备类型，决定 VAD 打断模式用哪套阈值。"""
    HEADPHONE = "headphone"   # 耳机：播报不泄漏，打断阈值可以低一点
    SPEAKER = "speaker"       # 外放：泄漏严重，打断阈值必须保守


def detect_output_mode(override: str = "auto") -> OutputMode:
    """检测当前输出设备类型。

    Args:
        override: "auto"（自动检测）、"headphone"、"speaker"。
                  显式指定时直接返回，跳过自动检测。

    Returns:
        OutputMode.HEADPHONE 或 OutputMode.SPEAKER。
        检测失败或 auto 下无法判断，默认返回 SPEAKER（保守策略）。
    """
    if override == "headphone":
        _LOG.info("output_mode: manual override → HEADPHONE")
        return OutputMode.HEADPHONE
    if override == "speaker":
        _LOG.info("output_mode: manual override → SPEAKER")
        return OutputMode.SPEAKER

    # auto：尝试从 sounddevice 获取当前默认输出设备名
    try:
        import sounddevice as sd

        default_output_idx = sd.default.device[1]
        if default_output_idx < 0:
            _LOG.warning("output_mode: no default output device, assuming SPEAKER")
            return OutputMode.SPEAKER

        dev_info = sd.query_devices(default_output_idx)
        dev_name: str = dev_info.get("name", "") or ""

        _LOG.debug("output_mode: default output device = %r", dev_name)

        name_lower = dev_name.lower()
        if any(kw in name_lower for kw in _HEADPHONE_KEYWORDS):
            _LOG.info("output_mode: detected HEADPHONE (device=%r)", dev_name)
            return OutputMode.HEADPHONE

        _LOG.info(
            "output_mode: no headphone keyword matched, assuming SPEAKER (device=%r)", dev_name
        )
        return OutputMode.SPEAKER

    except Exception as exc:  # noqa: BLE001
        # sounddevice 未安装或查询失败，保守处理
        _LOG.warning("output_mode: auto-detect failed (%s), defaulting to SPEAKER", exc)
        return OutputMode.SPEAKER
