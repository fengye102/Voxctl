"""Step 3 验收脚本：意图解析准确率端到端检测。

对应 CLAUDE.md 第 5 节 Step 3 验收标准 + 第 7 节测试表"意图解析准确率 100%"。
这是打真实 LLM 的系统级验收脚本，不是 unit test——不要往里加 mock，
本项目对 LLM 的信任建立在"真调一次能不能拿到结构化输出"上。

用法（PowerShell，已激活 venv、已设好 API key 环境变量）：

    python -m tests.acceptance.test_intent_parser --provider anthropic
    python -m tests.acceptance.test_intent_parser --provider openai

    # 走中转站：
    python -m tests.acceptance.test_intent_parser --provider openai --base-url https://xxx/v1

10/10 通过退出码 0，否则退出码 1。CLAUDE.md 明确要求两个 provider 各独立跑一遍，
只测其中一个不算完成 Step 3 验收。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Optional

from nlu import IntentParser, is_fallback_unknown


# Windows 控制台默认非 UTF-8，中文输出容易乱码（CLAUDE.md §8.6）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class Case:
    text: str
    expected_intent: str
    # 需要额外核对的字段（比如 query_system 的 resource 具体是 memory / disk / cpu）
    expected_fields: Optional[dict] = None


# 10 条典型指令，覆盖全部意图类型。CLAUDE.md：固定小范围指令集不应有歧义。
CASES: list[Case] = [
    Case("打开浏览器", "open_app"),
    Case("打开记事本", "open_app"),
    Case("关掉记事本", "close_app"),
    Case("关闭 chrome", "close_app"),
    Case("现在内存还剩多少", "query_system", {"resource": "memory"}),
    Case("C 盘还有多少空间", "query_system", {"resource": "disk"}),
    Case("CPU 占用率是多少", "query_system", {"resource": "cpu"}),
    Case("打开桌面上的 test.txt", "open_file"),
    Case("退出程序", "exit_program"),
    Case("帮我叫辆出租车", "unknown"),  # 明确在指令集外的开放请求，应走 unknown 兜底
]


# 各 provider 的默认模型 / 环境变量名。CLAUDE.md §2.1：model 直接透传，不做跨协议映射。
DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {
        "model": "claude-haiku-4-5-20251001",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
}


def run(provider: str, model: Optional[str], base_url: Optional[str], api_key_env: Optional[str]) -> int:
    defaults = DEFAULTS[provider]
    llm_cfg = {
        "provider": provider,
        "model": model or defaults["model"],
        "api_key_env": api_key_env or defaults["api_key_env"],
        "base_url": base_url or "",
    }
    parser = IntentParser.from_config(llm_cfg)

    print(f"=== Step 3 acceptance ===")
    print(f"provider = {provider}")
    print(f"model    = {llm_cfg['model']}")
    print(f"base_url = {llm_cfg['base_url'] or '(default)'}")
    print()

    passed = 0
    for i, case in enumerate(CASES, 1):
        try:
            intent = parser.parse(case.text)
        except Exception as exc:  # 鉴权错等由 IntentParser 上抛，视为整体不通过
            print(f"[{i:2d}] FAIL  {case.text!r} → exception: {type(exc).__name__}: {exc}")
            continue

        got = intent.intent
        ok = got == case.expected_intent
        mismatched_field = None
        fallback_note = None

        # 关键：期望 unknown 时，只有"LLM 主动选了 unknown 工具"才算真通过；
        # 底层调用/校验失败被兜底成 unknown 的算 FAIL，避免中转站砍掉 tool use
        # 时所有请求都掉进同一个 unknown、恰好让 unknown 用例假通过。
        if ok and case.expected_intent == "unknown" and is_fallback_unknown(intent):
            ok = False
            fallback_note = "fallback unknown (LLM didn't actually pick unknown tool)"

        if ok and case.expected_fields:
            for k, v in case.expected_fields.items():
                if getattr(intent, k, None) != v:
                    ok = False
                    mismatched_field = (k, getattr(intent, k, None), v)
                    break

        status = "PASS" if ok else "FAIL"
        detail = intent.model_dump()
        line = f"[{i:2d}] {status}  {case.text!r} → {detail}"
        if mismatched_field is not None:
            k, got_v, want_v = mismatched_field
            line += f"  (field {k}: got {got_v!r}, want {want_v!r})"
        if fallback_note:
            line += f"  ({fallback_note})"
        print(line)
        if ok:
            passed += 1

    parser.close()
    total = len(CASES)
    print()
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 3 intent parsing acceptance test")
    ap.add_argument("--provider", choices=["anthropic", "openai"], required=True)
    ap.add_argument("--model", default=None, help="override provider default model")
    ap.add_argument("--base-url", default=None, help="LLM endpoint (中转站 URL)")
    ap.add_argument(
        "--api-key-env",
        default=None,
        help="env var name holding the API key (defaults per provider)",
    )
    args = ap.parse_args()
    return run(args.provider, args.model, args.base_url, args.api_key_env)


if __name__ == "__main__":
    sys.exit(main())
