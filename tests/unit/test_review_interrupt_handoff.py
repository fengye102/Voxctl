"""Regression tests for handing an in-progress barge-in utterance to LISTEN."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def production_modules():
    """Load the real two production files without optional audio/LLM packages."""
    class OutputMode(Enum):
        HEADPHONE = "headphone"
        SPEAKER = "speaker"

    class VADMode(Enum):
        LISTEN = "listen"

    class State(Enum):
        IDLE = "idle"
        LISTEN = "listen"
        ASR = "asr"
        EXECUTE = "execute"
        TTS = "tts"
        AWAIT_CONFIRM = "await_confirm"

    class LoopState:
        def __init__(self):
            self.state = State.IDLE
            self.pending = None
            self.exit_requested = False
            self.rounds_completed = 0

        def transition(self, state):
            self.state = state

        def clear_pending_confirmation(self):
            pending, self.pending = self.pending, None
            return pending

    @dataclass
    class ExecutionResult:
        success: bool
        action_type: str
        should_exit: bool = False
        enter_dictation: bool = False

    def noop(*args, **kwargs):
        return None
    schemas = {name: type(name, (), {}) for name in (
        "CancelIntent", "CloseAppIntent", "ConfirmIntent", "OpenAppIntent",
        "UnknownIntent",
    )}
    modules = {
        "audio": _module("audio"),
        "audio.output_mode": _module("audio.output_mode", OutputMode=OutputMode),
        "audio.player": _module("audio.player", AudioPlayer=object),
        "audio.recorder": _module(
            "audio.recorder", AudioRecorderError=type("AudioRecorderError", (Exception,), {})
        ),
        "audio.vad": _module(
            "audio.vad", VADProcessor=object, VADMode=VADMode,
            CHUNK_DURATION_MS=32, PRE_ROLL_CHUNKS=5,
        ),
        "core.audit": _module(
            "core.audit", log_interrupt_event=noop, log_metrics=noop,
        ),
        "core.phrase_match": _module(
            "core.phrase_match", match_exit_phrase=noop, match_wake_prefix=noop,
        ),
        "core.state": _module("core.state", LoopState=LoopState, State=State),
        "executor": _module("executor", ExecutionResult=ExecutionResult),
        "executor.text_input": _module(
            "executor.text_input", TextInputError=type("TextInputError", (Exception,), {}),
            inject_text=noop,
        ),
        "nlu": _module("nlu"),
        "nlu.intent_parser": _module(
            "nlu.intent_parser", is_fallback_unknown=lambda intent: False,
        ),
        "nlu.schema": _module("nlu.schema", AnyIntent=object, **schemas),
    }
    with patch.dict(sys.modules, modules):
        coordinator = _load("review_interrupt_coordinator", "core/interrupt_coordinator.py")
        with patch.dict(sys.modules, {"core.interrupt_coordinator": coordinator}):
            event_loop = _load("review_event_loop", "core/event_loop.py")
            yield coordinator, event_loop, State, OutputMode, ExecutionResult


class FakeVAD:
    def __init__(self, trigger=None, segment_end=None):
        self.trigger = trigger
        self.segment_end = segment_end
        self.listened = []
        self.is_confirming_silence = False
        self.preliminary_segment = None
        self.silence_confirm_frames = 15
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1

    def process_chunk_interrupt(self, chunk):
        return int(chunk[0]) == self.trigger

    def process_chunk(self, chunk, mode):
        self.listened.append(int(chunk[0]))
        return chunk if int(chunk[0]) == self.segment_end else None


def _chunk(value):
    return np.full(512, value, dtype=np.float32)


def test_monitor_keeps_bounded_prefix_not_old_tts_echo(production_modules):
    async def run():
        coordinator_mod, *_ = production_modules
        queue = asyncio.Queue()
        for value in range(12):
            queue.put_nowait(_chunk(value))
        player = types.SimpleNamespace(stops=0)
        player.stop = lambda: setattr(player, "stops", player.stops + 1)
        coord = coordinator_mod.InterruptCoordinator(FakeVAD(trigger=11), player, queue)

        await coord.monitor()

        assert coord.interrupted
        assert player.stops == 1
        values = [int(chunk[0]) for chunk in coord.handoff_chunks]
        assert values[-1] == 11
        assert values[0] > 0, "old TTS echo must not be replayed"
        assert len(values) >= 3, "handoff must include audio before the trigger frame"
        coord.reset()
        assert not coord.handoff_chunks

    asyncio.run(run())


def test_interrupt_replays_prefix_then_live_continuation_and_cancels_pending(
    production_modules,
):
    async def run():
        _, event_loop, State, OutputMode, ExecutionResult = production_modules
        vad = FakeVAD(trigger=2, segment_end=4)
        player = types.SimpleNamespace(stop=lambda: None)
        loop = event_loop.MainLoop(
            recorder=object(), vad=vad, asr_engine=object(), intent_parser=object(),
            executor=object(), tts_engine=object(), player=player,
            output_mode=OutputMode.HEADPHONE,
        )
        loop._state.transition(State.TTS)
        pending = types.SimpleNamespace(intent="dangerous_command")
        loop._state.pending = pending
        result = ExecutionResult(success=False, action_type="dangerous_command")
        loop._pending_result = result
        loop._audio_queue.put_nowait(_chunk(1))
        loop._audio_queue.put_nowait(_chunk(2))

        # The coordinator consumes the prefix. More audio arrives before TTS cleanup.
        await loop._interrupt_coord.monitor()
        loop._audio_queue.put_nowait(_chunk(3))
        loop._audio_queue.put_nowait(_chunk(4))
        loop._handle_tts_interrupt(result)

        assert loop._state.pending is None
        assert loop._state.state is State.LISTEN
        for _ in range(4):
            await asyncio.wait_for(loop._phase_listen(), timeout=0.2)
        assert vad.listened == [1, 2, 3, 4]
        assert loop._state.state is State.ASR

    asyncio.run(run())


def test_speaker_without_interrupt_still_discards_tts_echo(production_modules):
    async def run():
        _, event_loop, State, OutputMode, ExecutionResult = production_modules
        vad = FakeVAD()
        tts = types.SimpleNamespace()

        async def speak_result(result):
            return None

        tts.speak_result = speak_result
        loop = event_loop.MainLoop(
            recorder=object(), vad=vad, asr_engine=object(), intent_parser=object(),
            executor=object(), tts_engine=tts,
            player=types.SimpleNamespace(stop=lambda: None),
            output_mode=OutputMode.SPEAKER,
            speaker_interrupt_enabled=False,
        )
        loop._state.transition(State.TTS)
        loop._pending_result = ExecutionResult(success=True, action_type="open_app")
        loop._audio_queue.put_nowait(_chunk(7))

        await loop._phase_tts()

        assert loop._audio_queue.empty()
        assert loop._state.state is State.LISTEN
        assert not loop._handoff_audio

    asyncio.run(run())


def test_opted_in_speaker_interrupt_does_not_feed_echo_to_asr(production_modules):
    async def run():
        _, event_loop, State, OutputMode, ExecutionResult = production_modules
        vad = FakeVAD(trigger=2)
        loop = event_loop.MainLoop(
            recorder=object(), vad=vad, asr_engine=object(), intent_parser=object(),
            executor=object(), tts_engine=object(),
            player=types.SimpleNamespace(stop=lambda: None),
            output_mode=OutputMode.SPEAKER,
            speaker_interrupt_enabled=True,
        )
        loop._state.transition(State.TTS)
        loop._state.pending = types.SimpleNamespace(intent="dangerous_command")
        loop._audio_queue.put_nowait(_chunk(1))
        loop._audio_queue.put_nowait(_chunk(2))
        await loop._interrupt_coord.monitor()
        loop._audio_queue.put_nowait(_chunk(3))

        loop._handle_tts_interrupt(
            ExecutionResult(success=True, action_type="exit_program", should_exit=True)
        )

        assert loop._state.pending is None
        assert not loop._state.exit_requested
        assert loop._state.state is State.LISTEN
        assert loop._audio_queue.empty()
        assert not loop._handoff_audio
        assert not vad.listened

    asyncio.run(run())
