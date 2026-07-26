"""Step 2 验收脚本：ASR 接入准确率测试。

对应 CLAUDE.md 第 5 节 Step 2 验收标准：
  "Step 1 截出的人声片段，能正确转成文字，误差在可接受范围（人工核对 10 句）"

两种用法：

  1. 实时录音模式（推荐，完整闭环验证）：
       python -m tests.acceptance.test_asr --live
     程序自动录音 + VAD 截取 + ASR 转录，打印识别结果。
     说够 10 句后 Ctrl+C，人工核对准确率是否 < 10% 字错误率。

  2. 离线 wav 文件模式（验证已录好的片段）：
       python -m tests.acceptance.test_asr --wav-dir ./test_audio
     批量转录指定目录下所有 wav 文件，打印文件名和识别结果。

参数：
  --device    sounddevice 输入设备索引（实时模式，不填用默认）
  --model     whisper 模型大小（默认 base，CLAUDE.md §2 说明）
  --live      实时录音+VAD 模式
  --wav-dir   离线 wav 目录模式
  --debug     打印 VAD/ASR 内部详情
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import wave
from pathlib import Path
from typing import Optional

import numpy as np

# Windows 控制台编码（CLAUDE.md §8.6）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ------------------------------------------------------------------
# 离线模式：批量转录 wav 文件
# ------------------------------------------------------------------

def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """读取 wav 文件，返回 (float32 numpy mono, sample_rate)。"""
    with wave.open(str(path), "rb") as wf:
        n_frames = wf.getnframes()
        sample_rate = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(n_frames)

    dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
    dtype = dtype_map.get(sampwidth, np.int16)
    audio = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    # 多声道取平均
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)
    # 归一化到 [-1, 1]
    max_val = float(np.iinfo(dtype).max) if np.issubdtype(dtype, np.integer) else 1.0
    audio /= max_val
    return audio, sample_rate


def run_offline(wav_dir: Path, model_size: str) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from asr import WhisperEngine

    wav_files = sorted(wav_dir.glob("*.wav"))
    if not wav_files:
        print(f"[ERROR] {wav_dir} 里没有 wav 文件")
        return

    print("\n=== Step 2 验收：ASR 离线模式 ===")
    print(f"模型：{model_size}  文件数：{len(wav_files)}\n")

    engine = WhisperEngine(model_size=model_size)

    for i, wav_path in enumerate(wav_files, 1):
        audio, sr = read_wav(wav_path)
        result = engine.transcribe(audio, sample_rate=sr)

        status = "    " if not result.is_empty else "[空]"
        print(
            f"[{i:02d}] {status} {wav_path.name:<30s}  "
            f"({result.duration_s:.2f}s → {result.elapsed_s:.2f}s)  "
            f"nsp={result.no_speech_prob:.2f}  logp={result.avg_logprob:.2f}"
        )
        print(f"       识别结果：{result.text or '(空)'}")
        print()

    print("── 人工核对说明 ──")
    print("对照录音音频和识别结果，统计字错误率（CER）。")
    print("CER < 10% → Step 2 验收通过（CLAUDE.md §7）。")
    print("CER > 10% → 考虑升级模型到 small（CLAUDE.md §2 GPU 加速说明）。")


# ------------------------------------------------------------------
# 实时模式：录音 + VAD + ASR 全流程
# ------------------------------------------------------------------

async def run_live(device_index: Optional[int], model_size: str) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from audio.recorder import AudioRecorder, SAMPLE_RATE
    from audio.vad import VADProcessor, VADMode
    from asr import WhisperEngine

    print("\n=== Step 2 验收：ASR 实时模式 ===")
    print(f"模型：{model_size}\n")

    AudioRecorder.print_devices()
    print()

    # VAD 默认配置
    vad_config = {
        "silence_threshold": 0.5,
        "speech_confirm_frames": 2,
        "silence_confirm_frames": 15,
    }

    print("加载 VAD 模型...")
    vad = VADProcessor.from_config(vad_config)

    print(f"加载 Whisper {model_size} 模型（首次运行会下载，请稍候）...")
    engine = WhisperEngine(model_size=model_size)
    engine.warmup()
    print("准备就绪。说一句话，识别结果会立刻打印。Ctrl+C 退出。\n")

    queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=200)
    recorder = AudioRecorder(device_index=device_index)
    loop = asyncio.get_running_loop()
    recorder.start(queue, loop)

    seg_count = 0
    try:
        while True:
            chunk: np.ndarray = await queue.get()
            segment = vad.process_chunk(chunk, mode=VADMode.LISTEN)
            if segment is None:
                continue

            seg_count += 1
            duration = len(segment) / SAMPLE_RATE
            print(f"[片段 {seg_count:02d}] 检测到语音 {duration:.2f}s，正在识别...", flush=True)

            result = engine.transcribe(segment, sample_rate=SAMPLE_RATE)

            if result.is_empty:
                print(f"           → (空/噪声  nsp={result.no_speech_prob:.2f})\n")
            else:
                print(
                    f"           → 【{result.text}】\n"
                    f"             (耗时 {result.elapsed_s:.2f}s, "
                    f"rtf={result.elapsed_s/max(duration,0.001):.2f}x, "
                    f"nsp={result.no_speech_prob:.2f}, logp={result.avg_logprob:.2f})\n"
                )

    except KeyboardInterrupt:
        print(f"\n已退出。共识别 {seg_count} 个片段。")
    finally:
        recorder.stop()


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Step 2 验收：ASR 准确率测试")
    ap.add_argument("--device", "-d", type=int, default=None,
                    help="sounddevice 输入设备索引（实时模式）")
    ap.add_argument("--model", "-m", default="base",
                    help="Whisper 模型大小：base / small / medium（默认 base）")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true",
                      help="实时录音 + VAD + ASR 全流程")
    mode.add_argument("--wav-dir", type=Path, metavar="DIR",
                      help="离线模式：批量转录 DIR 下所有 wav 文件")
    ap.add_argument("--debug", action="store_true",
                    help="打印 VAD / ASR 内部详情")
    args = ap.parse_args()

    _setup_logging(args.debug)

    if args.live:
        asyncio.run(run_live(args.device, args.model))
    else:
        run_offline(args.wav_dir, args.model)


if __name__ == "__main__":
    main()
