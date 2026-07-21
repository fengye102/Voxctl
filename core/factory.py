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
from asr.asr_engine import build_asr_engine
from executor import Executor
from nlu.intent_parser import IntentParser
from tts.tts_base import build_tts_engine

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

    vad_cfg = config.get("vad", {})
    vad = VADProcessor.from_config(
        vad_cfg,
        use_headphone_threshold=(output_mode == OutputMode.HEADPHONE),
    )
    # 修订 47 P1：speaker 模式打断开关（默认 false，§8.4 实测 TTS 泄漏概率 0.88-0.97）
    speaker_interrupt_enabled = bool(vad_cfg.get("speaker_interrupt_enabled", False))

    asr_engine = build_asr_engine(config.get("asr", {}))

    plugin_registry = None
    plugin_tool_specs = None
    plugin_intent_names = None
    plugins_cfg = config.get("plugins", {})
    if plugins_cfg.get("enabled", True):
        from plugins import PluginRegistry
        plugin_registry = PluginRegistry.load_from_directory()
        if plugin_registry:
            plugin_tool_specs = plugin_registry.tool_specs()
            plugin_intent_names = plugin_registry.intent_names

    intent_parser = IntentParser.from_config(
        config["llm"],
        plugin_tool_specs=plugin_tool_specs,
        plugin_intent_names=plugin_intent_names,
    )

    executor = Executor.from_config(
        config["whitelist"],
        log_path="logs/actions.log",
        plugin_registry=plugin_registry,
    )

    player = AudioPlayer()
    tts_engine = build_tts_engine(config.get("tts", {}), player)

    return MainLoop(
        recorder=recorder,
        vad=vad,
        asr_engine=asr_engine,
        intent_parser=intent_parser,
        executor=executor,
        tts_engine=tts_engine,
        player=player,
        output_mode=output_mode,
        speaker_interrupt_enabled=speaker_interrupt_enabled,
    )


__all__ = ["create_from_config_file", "create_from_config"]
