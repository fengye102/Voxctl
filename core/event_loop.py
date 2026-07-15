"""主循环：单向串行版（CLAUDE.md §5 Step 6）。

把 Step 1-5 的模块串成一条完整流程：
    麦克风采集 → VAD 切片 → ASR 转写 → 意图解析 → 执行 → TTS 播报 → 回到采集

Step 6 明确不做打断（CLAUDE.md §5）：
    - TTS 播报期间监听通道照常采集（recorder 一直在跑），但音频块直接丢弃，
      不进入 VAD 处理 → 不会触发新一轮 ASR
    - 等 TTS 播报 await 返回后，恢复 VAD 处理，重新进入 LISTEN
    Step 7 才叠加全双工打断（TTS 期间用 INTERRUPT 阈值检测打断信号）

二次确认流程：
    1. executor.execute(intent) 返回 needs_confirmation=True
    2. 主循环播报"确定要关闭 X 吗？"，状态切到 AWAIT_CONFIRM
    3. 下一轮语音走 ASR → 意图解析，期望得到 ConfirmIntent / CancelIntent
    4. ConfirmIntent：用 saved intent + confirmed=True 重新调 execute → TTS 播报结果
    5. CancelIntent：播报"已取消"，回到 LISTEN
    6. 其他意图：视为用户改主意，丢弃 pending，按新意图正常走流程

退出机制（CLAUDE.md §2 启动与退出）：
    - 语音说"退出程序" → ExitProgramIntent → executor 返回 should_exit=True
      → TTS 播报"再见" → 主循环 break
    - Ctrl+C：main.py 的 try/except KeyboardInterrupt 捕获，清理资源退出
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from asr.whisper_engine import WhisperEngine
from audio.player import AudioPlayer
from audio.recorder import AudioRecorder, AudioRecorderError
from audio.vad import VADMode, VADProcessor
from audio.output_mode import OutputMode, detect_output_mode
from core.interrupt_coordinator import InterruptCoordinator
from core.state import LoopState, PendingConfirmation, State
from executor import Executor, ExecutionResult
from nlu.intent_parser import IntentParser
from nlu.schema import (
    CancelIntent,
    ConfirmIntent,
    ExitProgramIntent,
    ParsedIntent,
    UnknownIntent,
)
from tts.edge_tts_engine import TTSEngine, format_result

_LOG = logging.getLogger(__name__)


class MainLoopError(Exception):
    """主循环错误基类。"""


class MainLoop:
    """Step 6 单向串行主循环。

    用法：
        loop = MainLoop.from_config_file("config.yaml")
        await loop.run()
    """

    def __init__(
        self,
        recorder: AudioRecorder,
        vad: VADProcessor,
        asr_engine: WhisperEngine,
        intent_parser: IntentParser,
        executor: Executor,
        tts_engine: TTSEngine,
        player: AudioPlayer,
        output_mode: OutputMode,
        audit_log_path: str = "logs/actions.log",
    ) -> None:
        self._recorder = recorder
        self._vad = vad
        self._asr = asr_engine
        self._intent_parser = intent_parser
        self._executor = executor
        self._tts_engine = tts_engine
        self._player = player
        self._output_mode = output_mode

        self._state = LoopState()
        self._audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Step 7 打断协调器（复用 VAD + player + queue）
        self._interrupt_coord = InterruptCoordinator(
            vad=vad, player=player, audio_queue=self._audio_queue,
        )

        # 审计日志路径（§6 强制：打断事件也要写审计日志）
        self._audit_log_path = Path(audit_log_path)

    # ------------------------------------------------------------------
    @classmethod
    def from_config_file(cls, config_path: str = "config.yaml") -> "MainLoop":
        """从 config.yaml 构造完整主循环（含所有子模块初始化）。"""
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        # GUI 控制台改的 LLM 覆盖（修订 19）：叠加 provider/model/base_url，
        # 并把 api_key 注入到环境变量（intent_parser 读取路径不变，仍读 env var）。
        # 语音无论由 GUI 拉起还是 run.bat 独立启动都走这条，自己从 voxctl.local.yaml
        # 读 key；无本地文件时 no-op（纯用系统环境变量的老行为不变）。
        from core.local_settings import apply_local_llm_overrides
        apply_local_llm_overrides(config)

        return cls.from_config(config)

    @classmethod
    def from_config(cls, config: dict) -> "MainLoop":
        """从已加载的 config dict 构造。"""
        # 输出模式检测（Step 7 会用，Step 6 先记录）
        output_mode = detect_output_mode(
            config.get("audio", {}).get("output_mode", "auto")
        )
        _LOG.info("output mode: %s", output_mode.value)

        # 录音
        audio_cfg = config.get("audio", {})
        recorder = AudioRecorder(
            device_index=audio_cfg.get("input_device_index"),
        )

        # VAD（耳机/外放阈值差异在 Step 7 才生效，Step 6 先传进去）
        vad = VADProcessor.from_config(
            config.get("vad", {}),
            use_headphone_threshold=(output_mode == OutputMode.HEADPHONE),
        )

        # ASR（首次构造不加载模型，延迟到第一次 transcribe）
        asr_engine = WhisperEngine.from_config(config.get("asr", {}))

        # 意图解析
        intent_parser = IntentParser.from_config(config["llm"])

        # 执行器
        executor = Executor.from_config(
            config["whitelist"],
            log_path="logs/actions.log",
        )

        # TTS + 播放器
        player = AudioPlayer()
        tts_engine = TTSEngine.from_config(config.get("tts", {}), player)

        return cls(
            recorder=recorder,
            vad=vad,
            asr_engine=asr_engine,
            intent_parser=intent_parser,
            executor=executor,
            tts_engine=tts_engine,
            player=player,
            output_mode=output_mode,
        )

    # ------------------------------------------------------------------
    async def run(self) -> None:
        """启动主循环。阻塞调用，直到 ExitProgramIntent 或 Ctrl+C。"""
        self._loop = asyncio.get_event_loop()
        self._state.transition(State.LISTEN)

        # 启动录音（sounddevice 回调线程 → asyncio.Queue）
        try:
            self._recorder.start(self._audio_queue, self._loop)
        except AudioRecorderError as exc:
            self._print_user_error(f"无法启动录音：{exc}")
            self._recorder.print_devices()
            return

        _LOG.info("=== Voxctl 主循环启动（Step 6 单向串行版）===")
        print("=== Voxctl 已启动，对着麦克风说话即可（说\"退出程序\"结束）===")

        try:
            await self._main_loop()
        finally:
            self._recorder.stop()
            self._intent_parser.close()
            _LOG.info("=== Voxctl 主循环已退出 ===")
            print("=== Voxctl 已退出 ===")

    # ------------------------------------------------------------------
    async def _main_loop(self) -> None:
        """核心循环：按状态机分发。"""
        while not self._state.exit_requested:
            try:
                if self._state.state == State.LISTEN:
                    await self._phase_listen()
                elif self._state.state == State.ASR:
                    await self._phase_asr()
                elif self._state.state == State.EXECUTE:
                    await self._phase_execute()
                elif self._state.state == State.TTS:
                    await self._phase_tts()
                elif self._state.state == State.AWAIT_CONFIRM:
                    await self._phase_await_confirm()
                else:
                    # IDLE 或未知状态，回到 LISTEN
                    self._state.transition(State.LISTEN)
            except asyncio.CancelledError:
                _LOG.info("main loop cancelled")
                break
            except Exception as exc:
                _LOG.exception("main loop iteration failed (state=%s)", self._state.state.value)
                # 出错时播报一句简短提示，然后回到 LISTEN，不让循环崩
                try:
                    await self._tts_engine.speak("出错了，请再说一次")
                except Exception:
                    pass  # noqa: BLE001 TTS 也挂了就只能日志
                self._state.transition(State.LISTEN)

    # ==================================================================
    # 各阶段实现
    # ==================================================================

    async def _phase_listen(self) -> None:
        """LISTEN：从队列取音频块 → VAD 处理 → emit 语音片段 → 切到 ASR。"""
        chunk = await self._audio_queue.get()
        segment = self._vad.process_chunk(chunk, mode=VADMode.LISTEN)
        if segment is not None:
            # 保存语音片段供 ASR 阶段使用
            self._pending_audio = segment
            self._state.transition(State.ASR)

    # 用实例属性暂存阶段间数据（只在单线程 asyncio 里访问，不需要锁）
    _pending_audio: Optional[np.ndarray] = None
    _pending_text: Optional[str] = None
    _pending_intent: Optional[ParsedIntent] = None
    _pending_result: Optional[ExecutionResult] = None

    async def _phase_asr(self) -> None:
        """ASR：把语音片段转写为文本，然后做意图解析，切到 EXECUTE。"""
        if self._pending_audio is None:
            self._state.transition(State.LISTEN)
            return

        audio = self._pending_audio
        self._pending_audio = None

        # ASR 是 CPU 密集同步调用，丢到线程池避免阻塞 asyncio
        try:
            result = await asyncio.to_thread(self._asr.transcribe, audio)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("ASR transcribe failed: %s", exc)
            self._pending_result = ExecutionResult(
                success=False, action_type="asr_error",
                error=f"语音识别失败: {type(exc).__name__}",
            )
            self._state.transition(State.TTS)
            return

        if result.is_empty or not result.text.strip():
            _LOG.info("ASR 空结果（静音/噪声/置信度低），回到 LISTEN")
            # 不播报，静默回到监听（避免每次键盘声都说"没听清"）
            self._state.transition(State.LISTEN)
            return

        text = result.text.strip()
        _LOG.info("ASR: %r (no_speech_prob=%.2f, logprob=%.2f, %.2fs/%.2fs)",
                  text, result.no_speech_prob, result.avg_logprob,
                  result.duration_s, result.elapsed_s)
        print(f"[你] {text}")

        # 意图解析（同步 LLM 调用，丢线程池）
        try:
            intent = await asyncio.to_thread(self._intent_parser.parse, text)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("intent parse failed (LLM error): %s", exc)
            self._pending_result = ExecutionResult(
                success=False, action_type="intent_error",
                error=f"意图解析失败: {type(exc).__name__}",
            )
            self._state.transition(State.TTS)
            return

        # AWAIT_CONFIRM 状态下收到的意图需要特殊处理
        if self._state.pending is not None:
            await self._handle_confirm_response(intent, text)
            return

        self._pending_text = text
        self._pending_intent = intent
        self._state.transition(State.EXECUTE)

    async def _phase_execute(self) -> None:
        """EXECUTE：执行意图，根据结果决定下一步。"""
        intent = self._pending_intent
        raw_text = self._pending_text or ""
        self._pending_intent = None
        self._pending_text = None

        if intent is None:
            _LOG.warning("EXECUTE phase with None intent")
            self._state.transition(State.LISTEN)
            return

        # UnknownIntent 不执行，直接播报"没听懂"
        if isinstance(intent, UnknownIntent):
            _LOG.info("UnknownIntent: %s", intent.reason)
            self._pending_result = ExecutionResult(
                success=False, action_type="unknown",
                error=intent.reason or "无法理解意图",
            )
            self._state.transition(State.TTS)
            return

        # 执行（executor 内部同步，但 IO 较少，直接 await to_thread）
        try:
            result = await asyncio.to_thread(
                self._executor.execute, intent, raw_text, False
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("executor failed for intent=%r: %s", intent, exc)
            result = ExecutionResult(
                success=False, action_type=getattr(intent, "intent", "unknown"),
                error=f"执行异常: {type(exc).__name__}: {exc}",
            )
        self._pending_result = result

        _LOG.info("EXECUTE intent=%s success=%s needs_confirm=%s should_exit=%s",
                  getattr(intent, "intent", type(intent).__name__),
                  result.success, result.needs_confirmation, result.should_exit)

        # 注意：should_exit 不在这里置 exit_requested（修订 14）。
        # 主循环 while not exit_requested 在阶段之间检查条件，此处置位会导致
        # 循环在 _phase_tts 执行前就 break，"再见"永远播不出来。
        # 置位挪到 _phase_tts 正常播完之后，见该方法末尾。

        if result.needs_confirmation:
            # 保存上下文，切到 AWAIT_CONFIRM，等用户说"是/取消"
            self._state.set_pending_confirmation(intent, raw_text, result)
            self._state.transition(State.TTS)  # 先播报"确定要...吗？"
            return

        self._state.transition(State.TTS)

    async def _phase_tts(self) -> None:
        """TTS：把执行结果套模板播报。Step 7 起并发跑打断监听。

        流程：
            1. 启动 InterruptCoordinator.monitor 任务（从队列取音频块用 INTERRUPT 阈值判定）
            2. await speak_result（被 player.stop() 打断会立即返回）
            3. cancel monitor 任务
            4. 如果检测到打断：
               - drain_queue 清空带 TTS 泄漏的剩余音频块
               - 如果 pending 存在（待二次确认），视为取消，清掉 pending
               - VAD reset，回到 LISTEN
            5. 如果正常播完：按原流程切状态（AWAIT_CONFIRM / LISTEN / 退出）
        """
        result = self._pending_result
        self._pending_result = None

        if result is None:
            self._state.transition(State.LISTEN)
            return

        # 启动打断监听（与 speak_result 并发）
        self._interrupt_coord.reset()
        monitor_task = asyncio.create_task(
            self._interrupt_coord.monitor(), name="interrupt_monitor"
        )

        try:
            await self._tts_engine.speak_result(result)
        finally:
            if not monitor_task.done():
                monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await monitor_task

        # 处理打断
        if self._interrupt_coord.interrupted:
            _LOG.info("TTS 被打断，清理状态回到 LISTEN")
            print("[打断] 停止播报，准备听新指令")

            # 清空队列里带 TTS 泄漏的音频块，避免回到 LISTEN 后误判
            drained = InterruptCoordinator.drain_queue(self._audio_queue)
            self._vad.reset()

            # AWAIT_CONFIRM 之前的播报被打断：视为取消待确认操作
            cancelled_pending = None
            if self._state.pending is not None:
                _LOG.info("打断取消待确认操作: %s", self._state.pending.intent)
                cancelled_pending = self._state.clear_pending_confirmation()

            # 退出确认音（"再见"）被打断：视为用户反悔，取消退出（§6 打断边界）。
            # 修订 14 后 exit_requested 延迟到播完才置位，此刻标志位尚未置上，
            # 只需按 result.should_exit 判定"取消了退出"用于审计，无需清除动作
            # （防御性地清一下，兼容未来其他路径提前置位的情况）。
            cancelled_exit = False
            if result.should_exit or self._state.exit_requested:
                _LOG.info("打断取消退出：退出播报被打断，exit_requested 不置位，回到监听")
                self._state.exit_requested = False
                cancelled_exit = True

            # §6 强制：审计日志必须记录打断事件本身
            self._log_interrupt_event(
                before_state=self._state.state.value,
                drained_chunks=drained,
                cancelled_pending=cancelled_pending,
                cancelled_exit=cancelled_exit,
                result_being_played=result,
            )

            self._state.rounds_completed += 1
            self._state.transition(State.LISTEN)
            return

        # 正常播完，决定下一步
        if result.should_exit:
            # 修订 14：exit_requested 延迟到"再见"正常播完才置位。
            # EXECUTE 阶段置位会让主循环 while 在 TTS 前 break，播报被跳过。
            _LOG.info("退出播报正常完成，置 exit_requested，主循环退出")
            self._state.exit_requested = True
            return  # run() 的 while 会 break

        if self._state.pending is not None:
            # 刚播完"确定要...吗？"，切到等待确认
            self._state.transition(State.AWAIT_CONFIRM)
        else:
            self._state.rounds_completed += 1
            self._state.transition(State.LISTEN)

    async def _phase_await_confirm(self) -> None:
        """AWAIT_CONFIRM：等待用户说"是"或"取消"。

        实际处理在 _handle_confirm_response 里（当 ASR 阶段收到意图时判断）。
        这里只是阻塞在音频队列上等下一句话，VAD 切片后切到 ASR。
        """
        # 和 _phase_listen 一样，只是状态标记不同（让 _handle_confirm_response 知道现在是确认阶段）
        chunk = await self._audio_queue.get()
        segment = self._vad.process_chunk(chunk, mode=VADMode.LISTEN)
        if segment is not None:
            self._pending_audio = segment
            self._state.transition(State.ASR)

    # ==================================================================
    # 二次确认处理
    # ==================================================================

    async def _handle_confirm_response(self, intent: ParsedIntent, text: str) -> None:
        """AWAIT_CONFIRM 状态下收到新意图的处理。

        - ConfirmIntent：用 saved intent + confirmed=True 重新执行
        - CancelIntent：播报"已取消"，丢弃 pending
        - 其他意图：用户改主意了，丢弃 pending，按新意图正常走流程
        """
        pending = self._state.clear_pending_confirmation()
        if pending is None:
            # 不应该发生，防御性处理
            self._state.transition(State.LISTEN)
            return

        if isinstance(intent, ConfirmIntent):
            _LOG.info("confirm: 重新执行 %s (confirmed=True)", pending.intent)
            result = await asyncio.to_thread(
                self._executor.execute, pending.intent, pending.raw_text, True
            )
            self._pending_result = result
            # should_exit 的置位同样延迟到 _phase_tts 正常播完（修订 14）
            self._state.transition(State.TTS)
            return

        if isinstance(intent, CancelIntent):
            _LOG.info("cancel: 丢弃 pending %s", pending.intent)
            self._pending_result = ExecutionResult(
                success=True, action_type="cancel",
                payload={"message": "已取消"},
            )
            self._state.transition(State.TTS)
            return

        # 其他意图：用户改主意，按新意图正常处理
        _LOG.info("AWAIT_CONFIRM 收到非确认意图 %s，视为改主意，按新意图处理", intent)
        self._pending_intent = intent
        self._pending_text = text
        self._state.transition(State.EXECUTE)

    # ==================================================================
    # 审计日志（§6 强制：打断事件也要记录）
    # ==================================================================

    def _log_interrupt_event(
        self,
        before_state: str,
        drained_chunks: int,
        cancelled_pending: Optional[PendingConfirmation],
        cancelled_exit: bool,
        result_being_played: ExecutionResult,
    ) -> None:
        """记录打断事件到审计日志（CLAUDE.md §6 强制）。

        打断事件可能不对应任何 execute 调用（如普通播报被打断），
        但 §6 明确要求："审计日志必须记录打断事件本身：什么时候发生了打断、
        打断前处于什么状态、打断取消了什么（如果有，含'取消了退出'）"。
        """
        import json
        from datetime import datetime

        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": "interrupt",
            "before_state": before_state,
            "drained_chunks": drained_chunks,
            "cancelled_pending": (
                None if cancelled_pending is None
                else {
                    "intent": getattr(cancelled_pending.intent, "intent", None),
                    "intent_detail": _safe_model_dump(cancelled_pending.intent),
                    "raw_text": cancelled_pending.raw_text,
                }
            ),
            "cancelled_exit": cancelled_exit,
            "result_being_played": {
                "action_type": result_being_played.action_type,
                "success": result_being_played.success,
                "needs_confirmation": result_being_played.needs_confirmation,
                "should_exit": result_being_played.should_exit,
            },
        }
        try:
            # 确保目录存在
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            _LOG.warning("failed to write interrupt audit log: %s", exc)

    # ==================================================================
    # 工具
    # ==================================================================

    @staticmethod
    def _print_user_error(msg: str) -> None:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        print(f"[错误] {msg}", file=sys.stderr)


__all__ = ["MainLoop", "MainLoopError"]


def _safe_model_dump(intent: ParsedIntent) -> dict:
    """安全地把 intent 转 dict（pydantic 模型用 model_dump，其他返回空）。"""
    if hasattr(intent, "model_dump"):
        try:
            return intent.model_dump()
        except Exception:  # noqa: BLE001
            return {}
    return {}
