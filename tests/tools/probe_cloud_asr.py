"""云端音频端点探针（CLAUDE.md §2.2 / §8.12，修订 72）。

换云端 ASR / TTS 端点前手动跑，验证目标中转是否实现本项目依赖的标准 OpenAI 音频端点：
  - ASR：POST /v1/audio/transcriptions（OpenAIASREngine 的唯一对接方式）
  - TTS：POST /v1/audio/speech（OpenAITTSEngine 的唯一对接方式）

同时探测非标准的 chat/completions `input_audio` 多模态格式——部分中转（实测 2026-07-26）
只经该格式暴露 ASR 模型，标准端点 404；这种端点现有 provider **接不上**，对接需新增
provider 实现（新能力面，须走修订流程），本探针只负责把事实探清楚。

用法（key 走环境变量，不进命令行历史）：
  $env:VOXCTL_PROBE_KEY = 'sk-...'
  python -m tests.tools.probe_cloud_asr --base-url https://relay.example.com/v1 `
      --asr-model whisper-1 --tts-model tts-1 --wav test_audio\\xxx\\segment_001.wav
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Cloudflare 会拦 urllib 默认 UA（error 1010），伪装成 openai SDK 的 UA
_UA = "OpenAI/Python 1.99.1"


def _post_json(base_url: str, path: str, key: str, payload: dict, timeout: float = 30.0):
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), time.perf_counter() - t0


def probe_models(base_url: str, key: str) -> None:
    print("── [0] GET /models ──")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}", "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        print("  可用模型：" + ", ".join(m.get("id", "?") for m in data.get("data", [])))
    except Exception as e:
        print(f"  失败：{type(e).__name__}: {e}")


def probe_std_asr(base_url: str, key: str, model: str, wav: Path) -> None:
    print(f"── [1] 标准 /audio/transcriptions（model={model}）──")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from asr.providers.openai_asr import OpenAIASREngine
    from tests.acceptance.test_asr import read_wav

    try:
        engine = OpenAIASREngine(model=model, api_key=key, base_url=base_url, language="zh")
        audio, sr = read_wav(wav)
        r = engine.transcribe(audio, sample_rate=sr)
        print(f"  ✓ 可用  {r.elapsed_s:.2f}s  nsp={r.no_speech_prob:.2f}  文本：{r.text or '(空)'}")
        print("  （nsp/logp 全为 0 时注意 §8.12 置信度退化）")
    except Exception as e:
        print(f"  ✗ 不可用：{type(e).__name__}: {e}")


def probe_chat_audio(base_url: str, key: str, model: str, wav: Path) -> None:
    print(f"── [2] 非标准 chat/completions + input_audio（model={model}）──")
    b64 = base64.b64encode(wav.read_bytes()).decode()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
        ]}],
    }
    try:
        _, body, dt = _post_json(base_url, "/chat/completions", key, payload)
        text = json.loads(body.decode())["choices"][0]["message"]["content"]
        print(f"  响应 200  {dt:.2f}s  文本：{text}")
        print("  （注意：此格式现有 OpenAIASREngine 接不上，仅说明模型走多模态暴露）")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}：{e.read().decode()[:200]}")
    except Exception as e:
        print(f"  失败：{type(e).__name__}: {e}")


def probe_std_tts(base_url: str, key: str, model: str) -> None:
    print(f"── [3] 标准 /audio/speech（model={model}）──")
    payload = {"model": model, "input": "你好", "voice": "alloy"}
    try:
        status, body, dt = _post_json(base_url, "/audio/speech", key, payload)
        print(f"  ✓ 可用  {status}  {dt:.2f}s  返回 {len(body)} bytes")
    except urllib.error.HTTPError as e:
        print(f"  ✗ 不可用：HTTP {e.code}：{e.read().decode()[:200]}")
    except Exception as e:
        print(f"  失败：{type(e).__name__}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="云端音频端点探针（§2.2/§8.12）")
    ap.add_argument("--base-url", required=True, help="中转 base_url，如 https://xx/v1")
    ap.add_argument("--asr-model", default="whisper-1")
    ap.add_argument("--tts-model", default="", help="留空则跳过 TTS 探测")
    ap.add_argument("--wav", type=Path, default=None, help="用于 ASR 探测的 wav；不给则跳过 ASR")
    ap.add_argument("--api-key-env", default="VOXCTL_PROBE_KEY",
                    help="API key 所在环境变量名（默认 VOXCTL_PROBE_KEY）")
    args = ap.parse_args()

    key = os.environ.get(args.api_key_env, "")
    if not key:
        print(f"[ERROR] 环境变量 {args.api_key_env} 未设置（key 不走命令行参数，避免进 shell 历史）")
        sys.exit(1)

    probe_models(args.base_url, key)
    if args.wav:
        probe_std_asr(args.base_url, key, args.asr_model, args.wav)
        probe_chat_audio(args.base_url, key, args.asr_model, args.wav)
    if args.tts_model:
        probe_std_tts(args.base_url, key, args.tts_model)


if __name__ == "__main__":
    main()
