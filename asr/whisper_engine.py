"""ASR 模块：faster-whisper 封装。

职责：
  - 加载 Whisper 模型（延迟加载，首次 transcribe 时初始化）
  - 接收 numpy float32 音频片段（16kHz mono），返回中文转录文本
  - 封装置信度过滤：no_speech_prob 高 → 判定为空/噪声，返回空文本
  - 封装幻觉过滤：avg_logprob 低 → 转录结果不可信，返回空文本

模型选择（CLAUDE.md §2）：
  - 默认 base（约 150MB），CPU 模式加载快，准确率够用
  - CER > 10% 时升级到 small（约 500MB），这是唯一升级触发条件
  - GPU 加速不在 MVP 范围，device 固定 cpu

关键参数（CLAUDE.md §7 验收标准：CER < 10%）：
  - language='zh'：固定中文，避免语言检测耗时和误判
  - beam_size=5：默认值，准确率与速度的平衡点
  - vad_filter=False：我们已经用 silero-vad 做了分段，不需要 whisper 内置 VAD 再过滤

用法：
  engine = WhisperEngine.from_config(config["asr"])
  result = engine.transcribe(audio_np)   # audio_np: float32 numpy, 16kHz
  if result.text:
      print(result.text)   # "打开浏览器"
  else:
      # 静音或噪声，走"没听清"流程
      ...
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import numpy as np

# 抽象层迁移（修订 45）：ASRResult / 异常层级 / 阈值常量迁到 asr.asr_engine，
# 这里 re-export 保持 `from asr.whisper_engine import ASRResult, ASRError` 等旧路径可用。
from asr.asr_engine import (
    ASREngine,
    ASRError,  # noqa: F401 - 保留旧导入路径兼容性
    ASRResult,
    ModelLoadError,
    TranscribeError,
    _DEFAULT_LOG_PROB_THRESHOLD,
    _DEFAULT_NO_SPEECH_THRESHOLD,
    apply_opencc,
    finalize_result,  # noqa: F401 - 保留旧导入路径兼容性
    load_opencc_converter,
)

_LOG = logging.getLogger(__name__)

_BUNDLED_MODEL_FILES = (
    "model.bin", "config.json", "tokenizer.json", "vocabulary.txt",
)


def find_bundled_model_dir(bundle_root: str | Path | None = None) -> Path | None:
    """返回 PyInstaller 包内 Whisper 目录；不存在则保持联网加载旧行为。"""
    if bundle_root is None:
        bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None

    candidate = Path(bundle_root) / "models" / "whisper"
    if not candidate.exists():
        return None
    missing = [name for name in _BUNDLED_MODEL_FILES if not (candidate / name).is_file()]
    if missing:
        raise ModelLoadError(
            f"bundled Whisper model is incomplete; missing: {', '.join(missing)}"
        )
    return candidate


class WhisperEngine(ASREngine):
    """faster-whisper ASR 引擎。

    单实例，进程内复用。模型在首次调用 transcribe() 时加载，之后常驻内存。
    非线程安全（asyncio 单线程使用，不需要锁）。
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",           # CPU 下 int8 速度最快，质量损失可忽略
        language: str = "zh",
        beam_size: int = 5,
        no_speech_threshold: float = _DEFAULT_NO_SPEECH_THRESHOLD,
        log_prob_threshold: float = _DEFAULT_LOG_PROB_THRESHOLD,
        cpu_threads: int = 0,                 # 0 = 自动（使用所有核心）
        opencc_enabled: bool = True,          # 繁→简转换（见 CLAUDE.md §2 ASR 说明）
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._beam_size = beam_size
        self._no_speech_threshold = no_speech_threshold
        self._log_prob_threshold = log_prob_threshold
        self._cpu_threads = cpu_threads
        self._model = None       # 延迟加载
        self._converter = None   # opencc 转换器，_ensure_converter_loaded() 初始化
        self._opencc_enabled = opencc_enabled
        if opencc_enabled:
            self._ensure_converter_loaded()

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, asr_config: dict) -> "WhisperEngine":
        """从 config.yaml 的 asr 块构造。

        支持的字段（均可选，有默认值）：
          model_size, device, compute_type, language, beam_size,
          no_speech_threshold, log_prob_threshold, cpu_threads, opencc_enabled
        """
        return cls(
            model_size=str(asr_config.get("model_size", "small")),
            device=str(asr_config.get("device", "cpu")),
            compute_type=str(asr_config.get("compute_type", "int8")),
            language=str(asr_config.get("language", "zh")),
            beam_size=int(asr_config.get("beam_size", 5)),
            no_speech_threshold=float(
                asr_config.get("no_speech_threshold", _DEFAULT_NO_SPEECH_THRESHOLD)
            ),
            log_prob_threshold=float(
                asr_config.get("log_prob_threshold", _DEFAULT_LOG_PROB_THRESHOLD)
            ),
            cpu_threads=int(asr_config.get("cpu_threads", 0)),
            opencc_enabled=bool(asr_config.get("opencc_enabled", True)),
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> ASRResult:
        """把音频片段转录为文本。

        Args:
            audio:       float32 numpy 数组，mono，值域 [-1, 1]，建议 16kHz。
            sample_rate: 音频采样率，faster-whisper 内部会重采样，但 16kHz 最快。

        Returns:
            ASRResult。text 为空字符串表示静音/噪声/置信度不足，业务层按
            CLAUDE.md §4 边界情况处理（"没听清，请再说一次"）。

        Raises:
            TranscribeError: 底层调用抛出非预期异常。
        """
        self._ensure_model_loaded()

        duration_s = len(audio) / sample_rate
        t0 = time.monotonic()

        # faster-whisper 的 transcribe 接受 numpy float32 数组（16kHz）
        # 返回 (segments_generator, TranscriptionInfo)；segments 是惰性迭代器，
        # 必须消费完才能拿到 info 里汇总的 no_speech_prob 等字段。
        try:
            segments_iter, info = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=self._beam_size,
                # 我们已经用 silero-vad 切好片段，不需要 whisper 内置 VAD 再处理
                vad_filter=False,
                # 不需要字级时间戳，节省计算
                word_timestamps=False,
                # condition_on_previous_text=False 避免上下文污染（每次都是独立指令）
                condition_on_previous_text=False,
            )
        except Exception as exc:
            raise TranscribeError(f"transcribe call failed: {exc}") from exc

        # 消费 segments 生成器，拼接文本，同时汇总置信度指标
        # 注意：segments_iter 是惰性生成器，只能消费一次，所以在这里一并收集所有需要的字段
        segments_text: list[str] = []
        logprob_sum = 0.0
        logprob_count = 0
        no_speech_prob = 0.0
        try:
            for seg in segments_iter:
                segments_text.append(seg.text.strip())
                # avg_logprob 和 no_speech_prob 在每个 Segment 上都有
                logprob_sum += getattr(seg, "avg_logprob", 0.0) or 0.0
                logprob_count += 1
                # no_speech_prob 取所有 segment 的最大值（最悲观估计）
                seg_nsp = getattr(seg, "no_speech_prob", 0.0) or 0.0
                if seg_nsp > no_speech_prob:
                    no_speech_prob = seg_nsp
        except Exception as exc:
            raise TranscribeError(f"segment iteration failed: {exc}") from exc

        elapsed_s = time.monotonic() - t0
        full_text = "".join(segments_text).strip()
        avg_logprob = (logprob_sum / logprob_count) if logprob_count > 0 else 0.0

        # 繁→简转换（CLAUDE.md §2 ASR 补充：Whisper 中文模型对大陆普通话有时输出繁体字符）
        if self._opencc_enabled:
            full_text = apply_opencc(full_text, self._converter)

        _LOG.debug(
            "ASR raw: text=%r no_speech_prob=%.3f avg_logprob=%.3f elapsed=%.2fs",
            full_text, no_speech_prob, avg_logprob, elapsed_s,
        )

        if no_speech_prob > self._no_speech_threshold:
            return ASRResult.empty(
                reason=f"no_speech_prob={no_speech_prob:.3f} > threshold={self._no_speech_threshold}",
                no_speech_prob=no_speech_prob,
                avg_logprob=avg_logprob,
                duration_s=duration_s,
                elapsed_s=elapsed_s,
            )

        if avg_logprob < self._log_prob_threshold and full_text:
            return ASRResult.empty(
                reason=f"avg_logprob={avg_logprob:.3f} < threshold={self._log_prob_threshold}",
                no_speech_prob=no_speech_prob,
                avg_logprob=avg_logprob,
                duration_s=duration_s,
                elapsed_s=elapsed_s,
            )

        if not full_text:
            return ASRResult.empty(
                reason="empty transcription",
                no_speech_prob=no_speech_prob,
                avg_logprob=avg_logprob,
                duration_s=duration_s,
                elapsed_s=elapsed_s,
            )

        _LOG.info(
            "ASR: %r  (%.2fs audio → %.2fs, rtf=%.2f)",
            full_text, duration_s, elapsed_s, elapsed_s / max(duration_s, 0.001),
        )

        return ASRResult(
            text=full_text,
            is_empty=False,
            no_speech_prob=no_speech_prob,
            avg_logprob=avg_logprob,
            duration_s=duration_s,
            elapsed_s=elapsed_s,
            segments=segments_text,
        )

    def warmup(self) -> None:
        """提前加载模型（可选）。

        主循环启动时调用，避免第一次说话时有加载延迟。
        """
        self._ensure_model_loaded()
        # 用一段极短的静音跑一次推理，确保 ONNX/CTranslate2 的 JIT 已编译完
        dummy = np.zeros(16000, dtype=np.float32)  # 1 秒静音
        try:
            self.transcribe(dummy)
        except Exception:  # noqa: BLE001
            pass  # warmup 失败不影响后续使用
        _LOG.info("WhisperEngine warmup complete")

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _ensure_model_loaded(self) -> None:
        if self._model is not None:
            return

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ModelLoadError(
                "faster-whisper 未安装；请运行：pip install faster-whisper"
            ) from exc

        bundled_dir = find_bundled_model_dir()
        model_ref = str(bundled_dir) if bundled_dir else self._model_size
        if bundled_dir:
            _LOG.info("loading bundled faster-whisper model: %s", bundled_dir)
        else:
            _LOG.info(
                "loading faster-whisper model=%r device=%s compute_type=%s "
                "（首次运行会下载模型，请稍候…）",
                self._model_size, self._device, self._compute_type,
            )
        t0 = time.monotonic()
        try:
            self._model = WhisperModel(
                model_ref,
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
            )
        except Exception as exc:
            raise ModelLoadError(
                f"failed to load faster-whisper model {model_ref!r}: {exc}"
            ) from exc

        elapsed = time.monotonic() - t0
        _LOG.info("faster-whisper model loaded in %.1fs", elapsed)

    def _ensure_converter_loaded(self) -> None:
        """加载 opencc 繁简转换器（t2s 模式：繁体→简体，字符级映射）。见 asr.asr_engine。"""
        self._converter = load_opencc_converter(self._opencc_enabled)
