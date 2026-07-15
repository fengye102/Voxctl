"""音频层：录音、VAD、播放、输出模式检测。

对外暴露：
    AudioRecorder   — sounddevice 录音封装（持续采集 → asyncio Queue）
    VADProcessor    — silero-vad 人声检测状态机
    VADMode         — 监听模式 / 打断判定模式
    AudioPlayer     — 播放封装（Step 5 实现，此处为接口声明）
    OutputMode      — 耳机 / 外放
    detect_output_mode — 自动检测输出设备类型
"""

from audio.recorder import AudioRecorder
from audio.vad import VADMode, VADProcessor
from audio.output_mode import OutputMode, detect_output_mode
from audio.player import AudioPlayer

__all__ = [
    "AudioRecorder",
    "VADProcessor",
    "VADMode",
    "AudioPlayer",
    "OutputMode",
    "detect_output_mode",
]
