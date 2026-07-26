"""主循环：状态机 + 各阶段处理（CLAUDE.md §5 Step 6/7）。

构造逻辑（from_config / from_config_file）已拆到 core/factory.py，
本文件只包含 MainLoop 的运行时行为。

状态机流程：
    LISTEN → ASR → EXECUTE → TTS → LISTEN（含 AWAIT_CONFIRM 分支）

全双工打断（Step 7）：
    TTS 播报期间 InterruptCoordinator 并发监听音频队列，
    检测到用户打断立即停止播放、清理状态、回到 LISTEN。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

from audio.output_mode import OutputMode
from audio.recorder import AudioRecorderError
from audio.vad import CHUNK_DURATION_MS, VADMode
from core.audit import log_interrupt_event, log_metrics
from core.interrupt_coordinator import InterruptCoordinator
from core.phrase_match import match_exit_phrase, match_wake_prefix
from core.state import LoopState, State
from executor import ExecutionResult
from executor.text_input import TextInputError, inject_text
from nlu.intent_parser import is_fallback_unknown
from nlu.schema import (
    AnyIntent,
    CancelIntent,
    CloseAppIntent,
    ConfirmIntent,
    OpenAppIntent,
    UnknownIntent,
)

_LOG = logging.getLogger(__name__)


class MainLoopError(Exception):
    """主循环错误基类。"""


class MainLoop:
    """主循环状态机（CLAUDE.md §5 Step 6/7）。

    构造方式：
        from core.factory import create_from_config_file
        loop = create_from_config_file("config.yaml")
        await loop.run()

    所有外部依赖通过构造函数注入，便于测试。
    """

    def __init__(
        self,
        recorder,
        vad,
        asr_engine,
        intent_parser,
        executor,
        tts_engine,
        player,
        output_mode: OutputMode,
        audit_log_path: str = "logs/actions.log",
        speaker_interrupt_enabled: bool = False,
        wake_word_enabled: bool = False,
        wake_word_phrases: Optional[list[str]] = None,
        wake_word_ack: bool = True,
        dictation_exit_phrases: Optional[list[str]] = None,
        multi_turn: bool = False,
        metrics_enabled: bool = False,
    ) -> None:
        self._recorder = recorder
        self._vad = vad
        self._asr = asr_engine
        self._intent_parser = intent_parser
        self._executor = executor
        self._tts_engine = tts_engine
        self._player = player
        self._output_mode = output_mode
        # 修订 47 P1：speaker 模式打断开关（默认 false，§8.4 实测 TTS 泄漏 0.88-0.97）
        self._speaker_interrupt_enabled = speaker_interrupt_enabled

        # 修订 56 唤醒词（默认关，opt-in）：开启时指令须以唤醒词前缀开头才处理
        self._wake_word_enabled = wake_word_enabled
        self._wake_word_phrases = list(wake_word_phrases or [])
        self._wake_word_ack = wake_word_ack
        # 修订 56 听写模式：进入后识别文本直接注入光标；exit_phrases 关键词退出
        self._dictation_exit_phrases = list(dictation_exit_phrases or [])
        self._dictation_mode = False
        # 修订 56 多轮上下文：维护上一条成功指令摘要，供下轮指代消解
        self._multi_turn = multi_turn
        self._last_context: Optional[str] = None
        # 修订 56 度量埋点：各阶段 perf_counter 时间戳（每轮 LISTEN emit 时重置）
        self._metrics_enabled = metrics_enabled
        self._t_speech_end: Optional[float] = None
        self._t_asr_done: Optional[float] = None
        self._t_intent_done: Optional[float] = None
        self._t_exec_done: Optional[float] = None
        self._t_tts_start: Optional[float] = None

        self._state = LoopState()
        self._audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._reminder_queue: asyncio.Queue[ExecutionResult] = asyncio.Queue()
        self._reminder_tasks: set[asyncio.Task] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Step 7 打断协调器（复用 VAD + player + queue）
        self._interrupt_coord = InterruptCoordinator(
            vad=vad, player=player, audio_queue=self._audio_queue,
        )

        # 审计日志路径（§6 强制：打断事件也要写审计日志）
        self._audit_log_path = Path(audit_log_path)

        # 阶段间数据暂存（只在单线程 asyncio 里访问，不需要锁）
        self._pending_audio: Optional[np.ndarray] = None
        self._pending_text: Optional[str] = None
        # 修订 48 C2：类型从 ParsedIntent 扩展为 AnyIntent（插件意图可进 pending 路径）
        self._pending_intent: Optional[AnyIntent] = None
        self._pending_result: Optional[ExecutionResult] = None

        # 投机 ASR（P2 #11）：VAD 静默确认期间抢先启动 ASR 以重叠延迟
        self._speculative_asr_task: Optional[asyncio.Task] = None
        self._speculative_asr_result = None  # ASRResult or None

        # 连续 ASR 空结果计数：≥N 次时轻提示「没听清」（默认 N=3）
        self._consecutive_empty_asr: int = 0
        self._empty_asr_hint_threshold: int = 3

    # ------------------------------------------------------------------
    async def run(self) -> None:
        """启动主循环。阻塞调用，直到 ExitProgramIntent 或 Ctrl+C。"""
        self._loop = asyncio.get_running_loop()
        self._state.transition(State.LISTEN)

        # 启动录音（sounddevice 回调线程 → asyncio.Queue）
        try:
            self._recorder.start(self._audio_queue, self._loop)
        except AudioRecorderError as exc:
            self._print_user_error(f"无法启动录音：{exc}")
            self._recorder.print_devices()
            return

        asyncio.create_task(asyncio.to_thread(self._asr.warmup))

        mode_cn = "耳机" if self._output_mode == OutputMode.HEADPHONE else "外放"
        if self._output_mode == OutputMode.HEADPHONE:
            interrupt_cn = "开启（播报期间可语音打断）"
        elif self._speaker_interrupt_enabled:
            interrupt_cn = "开启（外放打断已启用，可能误触发）"
        else:
            interrupt_cn = "关闭（播报期间请等播完；全双工请用耳机）"

        banner = (
            f"=== Voxctl 已启动，对着麦克风说话即可（说\"退出程序\"结束）===\n"
            f"[模式] {mode_cn} | 打断：{interrupt_cn}"
        )
        _LOG.info(
            "=== Voxctl 主循环启动（Step 7 全双工版）mode=%s speaker_interrupt=%s ===",
            self._output_mode.value,
            self._speaker_interrupt_enabled,
        )
        _LOG.info("[模式] %s | 打断：%s", mode_cn, interrupt_cn)
        print(banner)

        try:
            await self._main_loop()
        finally:
            await self._cancel_reminders()
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
            except Exception:
                _LOG.exception("main loop iteration failed (state=%s)", self._state.state.value)
                # 出错时播报一句简短提示，然后回到 LISTEN，不让循环崩
                # 若异常发生在 TTS 阶段，不再调 TTS（避免递归异常 / 浪费资源）
                if self._state.state != State.TTS:
                    try:
                        await self._tts_engine.speak("出错了，请再说一次")
                    except Exception:
                        pass  # noqa: BLE001 TTS 也挂了就只能日志
                self._state.transition(State.LISTEN)

    # ==================================================================
    # 各阶段实现
    # ==================================================================

    async def _phase_listen(self) -> None:
        """LISTEN：从队列取音频块 → VAD 处理 → emit 语音片段 → 切到 ASR。

        投机 ASR（P2 #11）：VAD 进入 CONFIRMING_SILENCE 时已收集完全部
        有意义语音，剩余 ~480ms 纯等静默确认。此期间抢先启动 ASR 转录，
        最终 segment 到达时直接用结果，省掉等待时间。
        """
        try:
            self._pending_result = self._reminder_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        else:
            self._state.transition(State.TTS)
            return

        chunk = await self._audio_queue.get()
        segment = self._vad.process_chunk(chunk, mode=VADMode.LISTEN)

        if segment is not None:
            # 度量埋点（修订 56）：一段语音确认结束——本轮计时基准，重置各阶段时间戳
            self._t_speech_end = time.perf_counter()
            self._t_asr_done = None
            self._t_intent_done = None
            self._t_exec_done = None
            # 最终 segment 确认——尝试用投机 ASR 结果
            if self._speculative_asr_task is not None:
                task = self._speculative_asr_task
                self._speculative_asr_task = None
                try:
                    spec_result = await task
                    # 修订 47 C3 / 修订 48：投机 ASR 质量校验——满足任一条件即丢弃，
                    # 回退到最终 segment（投机用的是 preliminary_segment，较短，
                    # 质量可能差于最终 segment）：
                    #   - is_empty：空文本
                    #   - no_speech_prob > 0.8：高静音概率
                    #   - avg_logprob < -1.0：低平均对数概率（幻觉短文本，修订 48 新增）
                    if (spec_result.is_empty
                            or spec_result.no_speech_prob > 0.8
                            or spec_result.avg_logprob < -1.0):
                        _LOG.debug(
                            "speculative ASR low quality "
                            "(empty=%s/no_speech=%.2f/logprob=%.2f), fallback",
                            spec_result.is_empty,
                            spec_result.no_speech_prob,
                            spec_result.avg_logprob,
                        )
                        self._pending_audio = segment
                    else:
                        self._speculative_asr_result = spec_result
                        _LOG.info("speculative ASR hit — saved ~%dms",
                                  self._vad.silence_confirm_frames * CHUNK_DURATION_MS)
                # 仅吞普通异常（ASR 失败/任务抛错），让 CancelledError 正常传播
                # （当前协程被外部 cancel 时不应被吞掉，破坏 asyncio 取消机制）
                except Exception:
                    _LOG.debug("speculative ASR unusable, falling back to final segment")
                    self._pending_audio = segment
            else:
                self._pending_audio = segment
            self._state.transition(State.ASR)
            return

        # 未出最终 segment——管理投机 ASR 生命周期
        if self._speculative_asr_task is not None:
            if not self._vad.is_confirming_silence:
                _LOG.debug("speech resumed / too short, cancelling speculative ASR")
                self._speculative_asr_task.cancel()
                self._speculative_asr_task = None
        elif self._vad.is_confirming_silence:
            prelim = self._vad.preliminary_segment
            if prelim is not None:
                _LOG.debug("starting speculative ASR (%.2fs preliminary)",
                           len(prelim) / 16000)
                self._speculative_asr_task = asyncio.create_task(
                    asyncio.to_thread(self._asr.transcribe, prelim),
                    name="speculative_asr",
                )

    async def _phase_asr(self) -> None:
        """ASR：把语音片段转写为文本，然后做意图解析，切到 EXECUTE。"""

        # 优先使用投机 ASR 结果（P2 #11 投机 ASR）
        if self._speculative_asr_result is not None:
            result = self._speculative_asr_result
            self._speculative_asr_result = None
            self._pending_audio = None
        elif self._pending_audio is not None:
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
        else:
            self._state.transition(State.LISTEN)
            return

        if result.is_empty or not result.text.strip():
            self._consecutive_empty_asr += 1
            n = self._consecutive_empty_asr
            threshold = self._empty_asr_hint_threshold
            if n >= threshold:
                _LOG.info(
                    "ASR 连续空结果 %d 次（≥%d），轻提示「没听清」",
                    n, threshold,
                )
                self._consecutive_empty_asr = 0
                self._pending_result = ExecutionResult(
                    success=True,
                    action_type="asr_empty_hint",
                    payload={"message": "刚才没听清，请再说一次"},
                )
                self._state.transition(State.TTS)
                return
            _LOG.info(
                "ASR 空结果（静音/噪声/置信度低，连续 %d/%d），回到 LISTEN",
                n, threshold,
            )
            # 未达阈值：静默回到监听（避免每次键盘声都说"没听清"）
            self._state.transition(State.LISTEN)
            return

        # 成功识别非空文本：重置空结果计数
        self._consecutive_empty_asr = 0
        text = result.text.strip()
        # ASR 完成时间戳（度量埋点，修订 56）
        self._t_asr_done = time.perf_counter()
        _LOG.info("ASR: %r (no_speech_prob=%.2f, logprob=%.2f, %.2fs/%.2fs)",
                  text, result.no_speech_prob, result.avg_logprob,
                  result.duration_s, result.elapsed_s)
        print(f"[你] {text}")

        # 听写模式短路（修订 56）：识别文本直接注入光标或匹配退出短语，
        # 不走唤醒词过滤、不走多轮上下文、不走 LLM 意图解析。仅非确认阶段生效。
        if self._dictation_mode and self._state.pending is None:
            await self._handle_dictation(text)
            return

        # 唤醒词过滤（修订 56，默认关）：开启且非确认阶段时，指令须以唤醒词前缀开头；
        # 未命中直接丢弃（不 TTS、不执行）。确认阶段（等"是/取消"）不要求唤醒词。
        if self._wake_word_enabled and self._state.pending is None:
            matched = match_wake_prefix(text, self._wake_word_phrases)
            if matched is None:
                _LOG.info("唤醒词未命中，丢弃指令：%r", text)
                self._state.transition(State.LISTEN)
                return
            _ok, remainder = matched
            remainder = remainder.strip()
            if not remainder:
                # 只说了唤醒词、无后续指令
                if self._wake_word_ack:
                    self._pending_result = ExecutionResult(
                        success=True, action_type="wake_ack",
                        payload={"message": "在"},
                    )
                    self._state.transition(State.TTS)
                else:
                    self._state.transition(State.LISTEN)
                return
            text = remainder

        # 意图解析（同步 LLM 调用，丢线程池）。多轮上下文（修订 56）：非确认阶段
        # 且 multi_turn 开启时把上一条摘要传入，辅助 LLM 消解"它/这个"等指代。
        context = (
            self._last_context
            if (self._multi_turn and self._state.pending is None)
            else None
        )
        try:
            intent = await asyncio.to_thread(
                self._intent_parser.parse, text, context
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("intent parse failed (LLM error): %s", exc)
            self._pending_result = ExecutionResult(
                success=False, action_type="intent_error",
                error=f"意图解析失败: {type(exc).__name__}",
            )
            self._state.transition(State.TTS)
            return
        # 意图解析完成时间戳（度量埋点）
        self._t_intent_done = time.perf_counter()

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

        # UnknownIntent 不执行，直接播报
        if isinstance(intent, UnknownIntent):
            if is_fallback_unknown(intent):
                _LOG.warning("LLM 调用失败降级为 unknown: %s", intent.reason)
                self._pending_result = ExecutionResult(
                    success=False, action_type="llm_error",
                    error=intent.reason or "llm error",
                )
            else:
                _LOG.info("UnknownIntent: %s", intent.reason)
                self._pending_result = ExecutionResult(
                    success=False, action_type="unknown",
                    error=intent.reason or "无法理解意图",
                )
            self._t_exec_done = time.perf_counter()
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
        self._register_reminder(result)

        self._t_exec_done = time.perf_counter()
        # 多轮上下文（修订 56）：成功的可执行指令更新"上一条摘要"，供下轮指代消解。
        # 失败/unknown 不更新，避免把错误上下文带进下一轮。
        if self._multi_turn and result.success:
            summary = self._build_context_summary(intent, result)
            if summary:
                self._last_context = summary

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

        # 度量埋点（修订 56）：播报开始 = "说完→开始播报"终点，记一条 metrics 审计行。
        # 只在本轮有 speech_end 基准时记（wake_ack / 空提示等无命令轮次不记）。
        self._t_tts_start = time.perf_counter()
        if self._metrics_enabled and self._t_speech_end is not None:
            speech_to_tts = (self._t_tts_start - self._t_speech_end) * 1000.0
            asr_ms = ((self._t_asr_done - self._t_speech_end) * 1000.0
                      if self._t_asr_done is not None else None)
            intent_ms = ((self._t_intent_done - self._t_asr_done) * 1000.0
                         if (self._t_asr_done is not None
                             and self._t_intent_done is not None) else None)
            exec_ms = ((self._t_exec_done - self._t_intent_done) * 1000.0
                       if (self._t_intent_done is not None
                           and self._t_exec_done is not None) else None)
            log_metrics(self._audit_log_path, asr_ms, intent_ms, exec_ms,
                        speech_to_tts, action_type=result.action_type)
            self._t_speech_end = None  # 已记录，清基准防重复

        # 启动打断监听（与 speak_result 并发）
        # 修订 47 P1：speaker 模式默认关闭打断（§8.4 实测 TTS 泄漏概率 0.88-0.97，
        # monitor 会把泄漏的播报声误判为用户打断）。关闭时跳过 monitor，
        # 但播完后仍需 drain_queue 清掉 TTS 期间累积的泄漏音频块，否则下一轮
        # LISTEN 会立即被泄漏音频误触发
        interrupt_enabled = not (
            self._output_mode == OutputMode.SPEAKER
            and not self._speaker_interrupt_enabled
        )
        monitor_task: Optional[asyncio.Task] = None
        if interrupt_enabled:
            self._interrupt_coord.reset()
            monitor_task = asyncio.create_task(
                self._interrupt_coord.monitor(), name="interrupt_monitor"
            )

        try:
            await self._tts_engine.speak_result(result)
        except Exception as exc:  # noqa: BLE001 TTS 网络/解码失败不能崩主循环
            _LOG.warning("TTS 播报失败: %s", exc)
        finally:
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await monitor_task

        # 处理打断
        if interrupt_enabled and self._interrupt_coord.interrupted:
            self._handle_tts_interrupt(result)
            return

        # speaker 模式关闭打断时，仍要清空 TTS 期间累积的泄漏音频块 + VAD reset，
        # 否则下一轮 LISTEN 立即被泄漏音频误触发
        if not interrupt_enabled:
            drained = InterruptCoordinator.drain_queue(self._audio_queue)
            if drained > 0:
                _LOG.debug(
                    "speaker mode (interrupt disabled): drained %d TTS-leak chunks",
                    drained,
                )
            self._vad.reset()

        # 进入听写模式（修订 56）：确认音正常播完后翻转模式（与 should_exit 同构的
        # 延迟置位——避免在 EXECUTE 阶段提前置位导致确认音时序错乱）。
        if result.enter_dictation:
            self._dictation_mode = True
            _LOG.info("已进入听写模式")
            print("[听写] 已进入听写模式，说的话将被打字输入；说\"退出听写\"结束")

        # 正常播完，决定下一步
        if result.should_exit:
            # 修订 14：exit_requested 延迟到"再见"正常播完才置位。
            # EXECUTE 阶段置位会让主循环 while 在 TTS 前 break，播报被跳过。
            _LOG.info("退出播报正常完成，置 exit_requested，主循环退出")
            self._last_context = None  # 退出时清空多轮上下文（修订 56）
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
    # 本地提醒（修订 58）
    # ==================================================================

    def _register_reminder(self, result: ExecutionResult) -> None:
        """把提醒插件返回的结构化请求注册为进程内 asyncio 定时任务。"""
        if not result.success or result.action_type != "set_reminder":
            return
        reminder = result.payload.get("reminder")
        if not isinstance(reminder, dict):
            return
        delay = reminder.get("delay_seconds")
        message = reminder.get("message")
        if (isinstance(delay, bool) or not isinstance(delay, int)
                or not 1 <= delay <= 86400
                or not isinstance(message, str) or not message.strip()):
            _LOG.warning("忽略无效提醒请求: %r", reminder)
            return

        task = asyncio.create_task(
            self._enqueue_reminder(delay, message.strip()),
            name="local_reminder",
        )
        self._reminder_tasks.add(task)
        task.add_done_callback(self._reminder_tasks.discard)

    async def _enqueue_reminder(self, delay_seconds: int, message: str) -> None:
        await asyncio.sleep(delay_seconds)
        await self._reminder_queue.put(ExecutionResult(
            success=True,
            action_type="reminder_due",
            payload={"message": f"提醒：{message}"},
        ))

    async def _cancel_reminders(self) -> None:
        tasks = list(self._reminder_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reminder_tasks.clear()

    # ==================================================================
    # 打断处理
    # ==================================================================

    def _handle_tts_interrupt(self, result: ExecutionResult) -> None:
        """TTS 播报期间检测到打断后的清理逻辑。

        - 清空队列中带 TTS 泄漏的音频块
        - 如果有待确认操作，视为取消
        - 如果是退出播报被打断，视为用户反悔（§6 打断边界）
        - 写审计日志（§6 强制）
        - 状态回到 LISTEN
        """
        _LOG.info("TTS 被打断，清理状态回到 LISTEN")
        print("[打断] 停止播报，准备听新指令")

        drained = InterruptCoordinator.drain_queue(self._audio_queue)
        self._vad.reset()

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

        log_interrupt_event(
            log_path=self._audit_log_path,
            before_state=self._state.state.value,
            drained_chunks=drained,
            cancelled_pending=cancelled_pending,
            cancelled_exit=cancelled_exit,
            result_being_played=result,
            interrupt_latency_ms=(
                (time.perf_counter() - self._t_tts_start) * 1000.0
                if (self._metrics_enabled and self._t_tts_start is not None)
                else None
            ),
        )

        self._state.rounds_completed += 1
        self._state.transition(State.LISTEN)

    # ==================================================================
    # 二次确认处理
    # ==================================================================

    async def _handle_confirm_response(self, intent: AnyIntent, text: str) -> None:
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
    # 听写模式（修订 56）
    # ==================================================================

    async def _handle_dictation(self, text: str) -> None:
        """听写模式下处理识别文本（§4 / §6 修订 56）。

        - 命中退出短语 → 退出听写，播报"已退出听写模式"
        - 否则 → 逐字符注入当前光标窗口（只注入字面字符，不发回车/Tab/修饰键），
          注入成功不播报（读回听写内容既慢又吵），直接回 LISTEN 听下一句
        - 注入失败（非 Windows / SendInput 出错）→ 退出听写并播报失败原因，
          避免用户卡在一个不断失败的模式里
        审计：退出记原文（退出短语不敏感）；注入只记字符数，不记明文（§6 隐私）
        """
        from core.audit import log_action

        if match_exit_phrase(text, self._dictation_exit_phrases):
            self._dictation_mode = False
            result = ExecutionResult(
                success=True, action_type="exit_dictation",
                payload={"message": "已退出听写模式"},
            )
            log_action(self._audit_log_path, text, None, result, False)
            self._pending_result = result
            self._state.transition(State.TTS)
            return

        try:
            injected = await asyncio.to_thread(inject_text, text)
        except TextInputError as exc:
            _LOG.warning("听写注入失败，退出听写模式：%s", exc)
            self._dictation_mode = False
            result = ExecutionResult(
                success=False, action_type="dictation_input",
                error=str(exc),
                payload={"message": f"听写注入失败：{exc}，已退出听写模式"},
            )
            log_action(self._audit_log_path, "<听写注入失败>", None, result, False)
            self._pending_result = result
            self._state.transition(State.TTS)
            return

        # 注入成功：只记字符数，不落明文（§6 隐私）；不播报，直接回 LISTEN
        _LOG.info("听写注入 %d 字符", injected)
        redacted = ExecutionResult(
            success=True, action_type="dictation_input",
            payload={"chars": injected},
        )
        log_action(self._audit_log_path, f"<听写 {injected} 字>", None, redacted, False)
        self._state.rounds_completed += 1
        self._state.transition(State.LISTEN)

    @staticmethod
    def _build_context_summary(intent: AnyIntent, result: ExecutionResult) -> Optional[str]:
        """构造"上一条成功指令"摘要，供多轮指代消解（修订 56）。

        只覆盖有明确"目标"的意图（开关应用），其它返回 None（不值得作上下文）。
        摘要是给 LLM 看的自然语言提示，不是结构化数据。
        """
        if isinstance(intent, OpenAppIntent):
            return f"刚刚打开了应用：{intent.app_name}"
        if isinstance(intent, CloseAppIntent):
            return f"刚刚关闭了应用：{intent.app_name}"
        return None

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
