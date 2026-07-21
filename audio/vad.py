"""VAD 封装：基于 silero-vad 的人声检测状态机。

核心设计：
  - 用 onnxruntime + numpy 直跑 vendor 的 silero_vad.onnx 模型（修订 21：不再依赖
    torch / silero_vad 包，自行复刻 OnnxWrapper 的逐帧状态机），逐帧打概率，
    自行维护上层状态机。这样可以在运行时动态切换阈值（监听模式 vs 打断判定模式），
    不需要重建模型。
  - 两套阈值（CLAUDE.md §2 VAD 选型，第 4 节全双工需求）：
    * 监听模式（LISTEN）：标准阈值，判断"有没有人在说话"
    * 打断模式（INTERRUPT）：更高阈值，播报期间判断"用户是否主动打断"
      打断阈值又分耳机/外放两组，见 audio/output_mode.py

状态机（每帧）：
  SILENCE → [prob >= threshold 且连续 min_speech_frames 帧] → SPEAKING
  SPEAKING → [prob < threshold 且连续 min_silence_frames 帧] → SILENCE，emit segment

预滚（pre-roll）：
  进入 SPEAKING 时，把前 PRE_ROLL_CHUNKS 帧也并入输出，避免截掉字头。

用法：
  proc = VADProcessor.from_config(config["vad"])
  for chunk in audio_stream:
      segment = proc.process_chunk(chunk, mode=VADMode.LISTEN)
      if segment is not None:
          # segment 是完整人声片段，shape=(N,), dtype=float32
          ...
  proc.reset()   # 切换说话人或场景时重置内部状态
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np

_LOG = logging.getLogger(__name__)

# silero-vad 在 16kHz 下要求的帧大小（512 samples = 32ms）
CHUNK_SAMPLES = 512
SAMPLE_RATE = 16000
# 单帧时长（毫秒），供外部估算 VAD 静默期等时长用
CHUNK_DURATION_MS = CHUNK_SAMPLES * 1000 // SAMPLE_RATE  # 32

# silero-vad onnx 模型的上下文窗口（16kHz=64 samples）与 LSTM 状态形状（修订 21）。
# 逐帧推理时 input = concat(context, chunk)，context 滚动保留上一帧尾部 64 samples。
_ONNX_CONTEXT = 64
_ONNX_STATE_SHAPE = (2, 1, 128)
_ONNX_MODEL_NAME = "silero_vad.onnx"

# 预滚帧数：进入 SPEAKING 时额外保留的前置帧，避免字头被截断（~160ms）
PRE_ROLL_CHUNKS = 5

# 最短有效语音帧数：短于此的触发视为误触发（512 * 4 = 约128ms，过滤键盘声等）
MIN_SPEECH_CHUNKS = 4

# 最长单次语音片段帧数：超出则强制截断（约30秒），防止内存无限增长
MAX_SPEECH_CHUNKS = 30 * (SAMPLE_RATE // CHUNK_SAMPLES)  # ~937帧

# 自适应阈值：噪声概率滑动窗口帧数（~3 秒静默期数据）
_NOISE_WINDOW_FRAMES = 94
# 自适应阈值：窗口内最少帧数（~1 秒），低于此用静态阈值
_NOISE_MIN_FRAMES = 30


class VADError(Exception):
    """VAD 模块错误基类。"""


class VADMode(Enum):
    """VAD 工作模式，决定使用哪套阈值。"""
    LISTEN = "listen"        # 常规监听：判断"有人说话了"
    INTERRUPT = "interrupt"  # 播报中：判断"用户在打断"


@dataclass
class VADConfig:
    """VAD 配置，从 config.yaml 的 vad 块读取。

    字段对应 CLAUDE.md 第 3 节 config.yaml 骨架：
      vad.silence_threshold
      vad.interrupt_threshold_headphone
      vad.interrupt_threshold_speaker
    """
    # 监听模式阈值（0–1，越高越保守）
    silence_threshold: float = 0.5
    # 打断模式：耳机阈值（播报声音基本不泄漏，可以适当低一点）
    interrupt_threshold_headphone: float = 0.6
    # 打断模式：外放阈值（播报声漏入麦克风多，必须更保守）
    interrupt_threshold_speaker: float = 0.8
    # 连续多少帧概率 >= threshold 才算"语音开始"（过滤瞬间噪音）
    speech_confirm_frames: int = 2
    # 连续多少帧概率 < threshold 才算"语音结束"（结束时的静默窗口）
    silence_confirm_frames: int = 15   # 约 480ms @ 32ms/frame
    # 自适应阈值：根据环境噪声自动抬高 silence_threshold，仅影响 LISTEN 模式
    adaptive_threshold: bool = True
    # 自适应余量：噪声基线 p95 上方的安全间距
    adaptive_margin: float = 0.10

    @classmethod
    def from_dict(cls, raw: dict) -> "VADConfig":
        return cls(
            silence_threshold=float(raw.get("silence_threshold", 0.5)),
            interrupt_threshold_headphone=float(
                raw.get("interrupt_threshold_headphone", 0.6)
            ),
            interrupt_threshold_speaker=float(
                raw.get("interrupt_threshold_speaker", 0.8)
            ),
            speech_confirm_frames=int(raw.get("speech_confirm_frames", 2)),
            silence_confirm_frames=int(raw.get("silence_confirm_frames", 15)),
            adaptive_threshold=bool(raw.get("adaptive_threshold", True)),
            adaptive_margin=float(raw.get("adaptive_margin", 0.10)),
        )


class _State(Enum):
    SILENCE = "silence"
    CONFIRMING_SPEECH = "confirming_speech"   # 概率超阈值，等待确认
    SPEAKING = "speaking"
    CONFIRMING_SILENCE = "confirming_silence"  # 概率回落，等待确认结束


class VADProcessor:
    """silero-vad 人声检测状态机。

    单实例对象，供主循环复用，不需要每次说话都重建。
    非线程安全，所有调用必须在同一个线程（asyncio 事件循环）里进行。
    """

    def __init__(self, config: VADConfig, use_headphone_threshold: bool = False) -> None:
        """
        Args:
            config: VAD 配置（阈值等）。
            use_headphone_threshold: 当前输出模式是否为耳机；影响打断模式下的阈值选取。
        """
        self._cfg = config
        self._use_headphone = use_headphone_threshold

        # 延迟加载 silero-vad onnx 模型（onnxruntime 会话 + LSTM 状态/上下文缓冲）
        self._session = None
        self._onnx_state = np.zeros(_ONNX_STATE_SHAPE, dtype=np.float32)
        self._onnx_context = np.zeros((1, _ONNX_CONTEXT), dtype=np.float32)
        self._load_model()

        # 状态机
        self._state = _State.SILENCE
        self._confirm_counter = 0   # 当前状态的连续帧计数

        # 打断判定计数器（process_chunk_interrupt 用，独立于主状态机）
        self._interrupt_confirm_counter = 0

        # 自适应阈值：SILENCE 期间的概率滑动窗口
        self._noise_probs: deque[float] = deque(maxlen=_NOISE_WINDOW_FRAMES)

        # 音频缓冲
        self._pre_roll: list[np.ndarray] = []   # 最近 PRE_ROLL_CHUNKS 帧（静默期间维护）
        self._speech_chunks: list[np.ndarray] = []  # 当前语音片段的帧

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        vad_config: dict,
        use_headphone_threshold: bool = False,
    ) -> "VADProcessor":
        return cls(VADConfig.from_dict(vad_config), use_headphone_threshold)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def silence_confirm_frames(self) -> int:
        """静默确认帧数（只读）。供外部估算 VAD 静默期时长（frames * 32ms）。"""
        return self._cfg.silence_confirm_frames

    def process_chunk(
        self,
        chunk: np.ndarray,
        mode: VADMode = VADMode.LISTEN,
    ) -> Optional[np.ndarray]:
        """处理一帧音频（512 samples @ 16kHz），返回完整语音片段或 None。

        Args:
            chunk: float32 numpy 数组，shape=(512,)，值域 [-1, 1]。
            mode:  当前 VAD 工作模式（监听 / 打断判定）。

        Returns:
            完整语音片段（numpy float32 array，已包含预滚），或 None（语音未结束）。

        Note:
            返回值不为 None 时，内部缓冲已清空，可立即开始处理下一段语音。
        """
        if len(chunk) != CHUNK_SAMPLES:
            _LOG.warning(
                "unexpected chunk size %d (expected %d), skipping", len(chunk), CHUNK_SAMPLES
            )
            return None

        threshold = self._get_threshold(mode)
        prob = self._get_speech_prob(chunk)

        return self._step(chunk, prob, threshold)

    def update_output_mode(self, use_headphone: bool) -> None:
        """运行时切换耳机/外放模式（影响打断阈值，不影响监听阈值）。"""
        self._use_headphone = use_headphone

    def process_chunk_interrupt(self, chunk: np.ndarray) -> bool:
        """快速打断判定（Step 7）：连续 speech_confirm_frames 帧概率超阈值即触发。

        与 process_chunk 不同，这里不等完整语音片段结束，一旦判定"有人开始说话"
        立即返回 True，让协调器尽快 stop 播放流。

        Args:
            chunk: float32 numpy 数组，shape=(512,)。

        Returns:
            True 表示检测到打断信号；False 表示当前帧未达打断阈值。

        Note:
            内部维护独立的打断计数器，不影响 process_chunk 的状态机。
            调用方应在进入 TTS 前 reset() 一次，清掉之前的尾音状态。
        """
        if len(chunk) != CHUNK_SAMPLES:
            return False

        threshold = self._get_threshold(VADMode.INTERRUPT)
        prob = self._get_speech_prob(chunk)
        above = prob >= threshold

        if above:
            self._interrupt_confirm_counter += 1
            if self._interrupt_confirm_counter >= self._cfg.speech_confirm_frames:
                _LOG.info(
                    "VAD interrupt triggered (prob=%.3f, threshold=%.3f, consecutive=%d)",
                    prob, threshold, self._interrupt_confirm_counter,
                )
                return True
        else:
            if self._interrupt_confirm_counter > 0:
                _LOG.debug(
                    "VAD interrupt counter reset (prob=%.3f < %.3f, was %d)",
                    prob, threshold, self._interrupt_confirm_counter,
                )
            self._interrupt_confirm_counter = 0

        return False

    def reset(self) -> None:
        """重置状态机，丢弃当前积累的语音片段。

        用于：打断发生后清空未完成的收集缓冲；切换说话场景。
        """
        if self._state != _State.SILENCE:
            _LOG.debug(
                "VADProcessor reset: discarding %d speech chunks (state=%s)",
                len(self._speech_chunks), self._state.value
            )
        self._state = _State.SILENCE
        self._confirm_counter = 0
        self._interrupt_confirm_counter = 0
        self._noise_probs.clear()
        self._pre_roll.clear()
        self._speech_chunks.clear()

    @property
    def is_speaking(self) -> bool:
        """当前是否处于语音收集状态（含确认阶段）。"""
        return self._state in (_State.SPEAKING, _State.CONFIRMING_SPEECH, _State.CONFIRMING_SILENCE)

    @property
    def is_confirming_silence(self) -> bool:
        """当前是否正在等待静默确认（CONFIRMING_SILENCE）。"""
        return self._state == _State.CONFIRMING_SILENCE

    @property
    def preliminary_segment(self) -> Optional[np.ndarray]:
        """若正在静默确认且语音够长，返回已收集的语音片段（不重置状态）。

        供主循环在静默确认期间抢先启动 ASR，省掉 ~480ms 等待。
        返回 None 表示不处于合适状态或语音太短。
        """
        if self._state != _State.CONFIRMING_SILENCE:
            return None
        if len(self._speech_chunks) < MIN_SPEECH_CHUNKS:
            return None
        return np.concatenate(self._speech_chunks)

    @property
    def noise_floor(self) -> float:
        """当前环境噪声基线（p95），用于自适应阈值和诊断。

        返回 0.0 表示样本不足或自适应未启用。
        """
        if not self._cfg.adaptive_threshold:
            return 0.0
        if len(self._noise_probs) < _NOISE_MIN_FRAMES:
            return 0.0
        sorted_probs = sorted(self._noise_probs)
        return sorted_probs[int(len(sorted_probs) * 0.95)]

    @property
    def effective_listen_threshold(self) -> float:
        """当前 LISTEN 模式的生效阈值（含自适应调整）。"""
        return self._get_threshold(VADMode.LISTEN)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """加载 vendor 的 silero_vad.onnx 到 onnxruntime 会话（修订 21，无 torch）。"""
        try:
            import onnxruntime
        except ImportError as exc:
            raise VADError(
                "onnxruntime 未安装；请运行：pip install onnxruntime"
            ) from exc

        model_path = self._resolve_model_path()
        if not model_path.is_file():
            raise VADError(f"找不到 VAD 模型文件：{model_path}")

        _LOG.info("loading silero-vad onnx model from %s ...", model_path)
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        _LOG.info("silero-vad onnx model loaded")

    @staticmethod
    def _resolve_model_path() -> Path:
        """定位 silero_vad.onnx：frozen 从 _MEIPASS，源码从本文件同目录（修订 21）。"""
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
            return base / _ONNX_MODEL_NAME
        return Path(__file__).resolve().parent / _ONNX_MODEL_NAME

    def _get_speech_prob(self, chunk: np.ndarray) -> float:
        """用 silero-vad onnx 推断当前帧的语音概率（0–1）。

        复刻官方 OnnxWrapper 的逐帧逻辑（16kHz）：input = concat(context, chunk)，
        session 输出概率 + 新 LSTM 状态，context 滚动为本次 input 的尾部 64 samples。
        """
        x = chunk.astype(np.float32).reshape(1, CHUNK_SAMPLES)
        inp = np.concatenate([self._onnx_context, x], axis=1)  # (1, 64+512)
        out, self._onnx_state = self._session.run(
            None,
            {
                "input": inp,
                "state": self._onnx_state,
                "sr": np.array(SAMPLE_RATE, dtype="int64"),
            },
        )
        self._onnx_context = inp[:, -_ONNX_CONTEXT:]
        return float(out[0, 0])

    def _get_threshold(self, mode: VADMode) -> float:
        if mode == VADMode.LISTEN:
            base = self._cfg.silence_threshold
            if (
                self._cfg.adaptive_threshold
                and len(self._noise_probs) >= _NOISE_MIN_FRAMES
            ):
                sorted_probs = sorted(self._noise_probs)
                p95 = sorted_probs[int(len(sorted_probs) * 0.95)]
                adaptive = p95 + self._cfg.adaptive_margin
                if adaptive > base:
                    return adaptive
            return base
        # INTERRUPT 模式按输出设备类型选阈值
        if self._use_headphone:
            return self._cfg.interrupt_threshold_headphone
        return self._cfg.interrupt_threshold_speaker

    def _step(
        self, chunk: np.ndarray, prob: float, threshold: float
    ) -> Optional[np.ndarray]:
        """状态机单步推进。返回完整语音片段或 None。"""
        above = prob >= threshold

        if self._state == _State.SILENCE:
            # 预滚维护（静默期间保留最近 PRE_ROLL_CHUNKS 帧）
            self._pre_roll.append(chunk)
            if len(self._pre_roll) > PRE_ROLL_CHUNKS:
                self._pre_roll.pop(0)

            if above:
                self._state = _State.CONFIRMING_SPEECH
                self._confirm_counter = 1
                _LOG.debug("VAD: SILENCE → CONFIRMING_SPEECH (prob=%.3f)", prob)
            elif self._cfg.adaptive_threshold:
                self._noise_probs.append(prob)

        elif self._state == _State.CONFIRMING_SPEECH:
            self._pre_roll.append(chunk)
            if len(self._pre_roll) > PRE_ROLL_CHUNKS:
                self._pre_roll.pop(0)

            if above:
                self._confirm_counter += 1
                if self._confirm_counter >= self._cfg.speech_confirm_frames:
                    # 确认为语音：把预滚并入语音片段，进入 SPEAKING
                    self._speech_chunks = list(self._pre_roll)
                    self._pre_roll.clear()
                    self._state = _State.SPEAKING
                    self._confirm_counter = 0
                    _LOG.debug("VAD: SPEAKING (confirmed, speech_frames=%d so far)", len(self._speech_chunks))
            else:
                # 概率回落，误触发，回到静默
                self._state = _State.SILENCE
                self._confirm_counter = 0
                _LOG.debug("VAD: CONFIRMING_SPEECH → SILENCE (prob=%.3f, false trigger)", prob)

        elif self._state == _State.SPEAKING:
            self._speech_chunks.append(chunk)

            # 防止语音片段无限增长（超过 MAX_SPEECH_CHUNKS 强制截断）
            if len(self._speech_chunks) > MAX_SPEECH_CHUNKS:
                _LOG.warning(
                    "VAD: speech segment exceeded %d frames, force-emitting", MAX_SPEECH_CHUNKS
                )
                return self._emit_segment()

            if not above:
                self._state = _State.CONFIRMING_SILENCE
                self._confirm_counter = 1
                _LOG.debug("VAD: SPEAKING → CONFIRMING_SILENCE (prob=%.3f)", prob)

        elif self._state == _State.CONFIRMING_SILENCE:
            self._speech_chunks.append(chunk)

            if not above:
                self._confirm_counter += 1
                if self._confirm_counter >= self._cfg.silence_confirm_frames:
                    # 确认静默，语音结束
                    _LOG.debug(
                        "VAD: SPEAKING ended, segment=%d frames (%.1fs)",
                        len(self._speech_chunks),
                        len(self._speech_chunks) * CHUNK_SAMPLES / SAMPLE_RATE,
                    )
                    return self._emit_segment()
            else:
                # 又出现人声，重回 SPEAKING
                self._state = _State.SPEAKING
                self._confirm_counter = 0
                _LOG.debug("VAD: CONFIRMING_SILENCE → SPEAKING (prob=%.3f)", prob)

        return None

    def _emit_segment(self) -> Optional[np.ndarray]:
        """输出当前语音片段并重置缓冲。若片段过短则丢弃（误触发过滤）。"""
        chunks = self._speech_chunks
        self._speech_chunks = []
        self._pre_roll.clear()
        self._state = _State.SILENCE
        self._confirm_counter = 0

        if len(chunks) < MIN_SPEECH_CHUNKS:
            _LOG.debug(
                "VAD: segment too short (%d frames < %d), discarding",
                len(chunks), MIN_SPEECH_CHUNKS
            )
            return None

        segment = np.concatenate(chunks)
        _LOG.info(
            "VAD: emitting segment %.2fs (%d samples)",
            len(segment) / SAMPLE_RATE, len(segment)
        )
        return segment
