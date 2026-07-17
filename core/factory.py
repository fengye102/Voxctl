"""MainLoop 构造工厂（CLAUDE.md §5 Step 6）。

从 config.yaml / config dict 构造 MainLoop 及其所有子模块。
event_loop.py 只保留运行时行为，构造逻辑集中在此。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml

from audio.output_mode import OutputMode, detect_output_mode
from audio.player import AudioPlayer
from audio.recorder import AudioRecorder
from audio.vad import VADProcessor
from asr.whisper_engine import WhisperEngine
from executor import Executor
from nlu.intent_parser import IntentParser
from tts.edge_tts_engine import TTSEngine

if TYPE_CHECKING:
    from core.event_loop import MainLoop

_LOG = logging.getLogger(__name__)


def create_from_config_file(config_path: str = "config.yaml") -> "MainLoop":
    """从 config.yaml 构造完整主循环（含所有子模块初始化）。"""
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    from core.local_settings import apply_local_llm_overrides
    apply_local_llm_overrides(config)

    return create_from_config(config)


def create_from_config(config: dict) -> "MainLoop":
    """从已加载的 config dict 构造 MainLoop。"""
    from core.event_loop import MainLoop

    output_mode = detect_output_mode(
        config.get("audio", {}).get("output_mode", "auto")
    )
    _LOG.info("output mode: %s", output_mode.value)

    audio_cfg = config.get("audio", {})
    recorder = AudioRecorder(
        device_index=audio_cfg.get("input_device_index"),
    )

    vad = VADProcessor.from_config(
        config.get("vad", {}),
        use_headphone_threshold=(output_mode == OutputMode.HEADPHONE),
    )

    asr_engine = WhisperEngine.from_config(config.get("asr", {}))
    intent_parser = IntentParser.from_config(config["llm"])

    executor = Executor.from_config(
        config["whitelist"],
        log_path="logs/actions.log",
    )

    player = AudioPlayer()
    tts_engine = TTSEngine.from_config(config.get("tts", {}), player)

    return MainLoop(
        recorder=recorder,
        vad=vad,
        asr_engine=asr_engine,
        intent_parser=intent_parser,
        executor=executor,
        tts_engine=tts_engine,
        player=player,
        output_mode=output_mode,
    )


__all__ = ["create_from_config_file", "create_from_config"]
