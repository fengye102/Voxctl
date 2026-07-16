"""Step 1 验收脚本：录音闭环 + VAD 人声检测。

对应 CLAUDE.md 第 5 节 Step 1 验收标准：
  "说一句话，能自动截取出干净的人声片段（掐头去尾）"

交互流程：
  1. 打印可用输入设备列表
  2. 开始录音（持续监听）
  3. 检测到人声 → 开始收集
  4. 静默结束后输出片段时长，可选保存到 wav 文件
  5. Ctrl+C 退出

用法（PowerShell，已激活 venv）：

    # 列出设备，用默认输入设备，每次检测到语音保存为 wav：
    python -m tests.acceptance.test_recorder_vad --save-dir ./test_audio

    # 指定设备索引：
    python -m tests.acceptance.test_recorder_vad --device 1

    # 只打印统计不保存：
    python -m tests.acceptance.test_recorder_vad

验收通过条件：
  - 不说话时不会误触发（静默不输出片段）
  - 说一句话后正确输出片段时长（精确度在 ±200ms 内）
  - 截取的片段没有明显的字头截断或尾部过长静默
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

# Windows 控制台编码修正（CLAUDE.md §8.6）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 在 import audio 模块之前配置日志，方便看 VAD 状态变化
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from audio.recorder import AudioRecorder, SAMPLE_RATE, CHUNK_SAMPLES
from audio.vad import VADProcessor, VADMode


def save_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """把 float32 numpy 数组存为 16-bit PCM wav 文件。"""
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


async def run(device_index: Optional[int], save_dir: Optional[Path], debug: bool) -> None:
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── 1. 列出设备 ────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Step 1 验收：录音闭环 + VAD 检测")
    print("=" * 60)
    AudioRecorder.print_devices()
    print()

    if device_index is not None:
        print(f"使用设备索引：{device_index}")
    else:
        print("使用系统默认输入设备（建议通过 --device 显式指定）")
    print()

    # ── 2. 初始化 ───────────────────────────────────────────────────────
    # 使用 config.yaml 默认值（Step 6 串联后从文件读取，这里内联写死用于测试）
    vad_config = {
        "silence_threshold": 0.5,
        "interrupt_threshold_headphone": 0.6,
        "interrupt_threshold_speaker": 0.8,
        "speech_confirm_frames": 2,
        "silence_confirm_frames": 15,   # 约 480ms 静默视为语音结束
    }

    print("正在加载 silero-vad 模型（首次运行可能需要下载 ~1MB）...")
    vad = VADProcessor.from_config(vad_config)
    print("VAD 模型加载完成。\n")

    queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=200)
    recorder = AudioRecorder(device_index=device_index)
    loop = asyncio.get_running_loop()

    recorder.start(queue, loop)
    print("开始监听麦克风。说一句话，结束后会输出片段信息。Ctrl+C 退出。\n")

    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        print(f"语音片段将保存到：{save_dir}\n")

    segment_count = 0
    total_chunks = 0

    try:
        while True:
            chunk: np.ndarray = await queue.get()
            total_chunks += 1

            segment = vad.process_chunk(chunk, mode=VADMode.LISTEN)
            if segment is None:
                continue

            # ── 检测到完整语音片段 ──────────────────────────────────────
            segment_count += 1
            duration = len(segment) / SAMPLE_RATE
            rms = float(np.sqrt(np.mean(segment ** 2)))

            print(f"[片段 {segment_count:02d}]  时长={duration:.3f}s  "
                  f"样本数={len(segment)}  RMS能量={rms:.4f}")

            if save_dir:
                wav_path = save_dir / f"segment_{segment_count:03d}.wav"
                save_wav(wav_path, segment, SAMPLE_RATE)
                print(f"          已保存：{wav_path}")

            print()

    except KeyboardInterrupt:
        print("\n\n用户中止（Ctrl+C）。")
    finally:
        recorder.stop()
        print(f"\n统计：共处理 {total_chunks} 帧（{total_chunks * CHUNK_SAMPLES / SAMPLE_RATE:.1f}s 音频），"
              f"检测到 {segment_count} 个语音片段。")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Step 1 验收：VAD 人声检测闭环测试"
    )
    ap.add_argument(
        "--device", "-d", type=int, default=None,
        help="sounddevice 输入设备索引（不填则用系统默认）"
    )
    ap.add_argument(
        "--save-dir", "-s", type=Path, default=None,
        metavar="DIR",
        help="把每个语音片段保存为 wav 文件到此目录（不填则只打印统计）"
    )
    ap.add_argument(
        "--debug", action="store_true",
        help="打印 VAD 内部状态变化（查看漏检/误触发时用）"
    )
    args = ap.parse_args()
    asyncio.run(run(args.device, args.save_dir, args.debug))


if __name__ == "__main__":
    main()
