"""中转站兼容性探针：跳过 SDK，直接打 HTTP，看服务端到底返回什么。

对应 CLAUDE.md §8.9：换中转前必须单独验证目标端点真的支持结构化输出。
本项目对 LLM 的唯一硬需求就是 tool use / function calling，中转站砍掉这个能力
就直接淘汰，不用等集成进 intent_parser.py 才发现。

用法（PowerShell，已设好对应环境变量）：

    # Anthropic 协议
    python -m tests.tools.probe_relay --provider anthropic --base-url https://xxx --model claude-xxx

    # OpenAI 协议
    python -m tests.tools.probe_relay --provider openai --base-url https://xxx/v1 --model gpt-4o-mini

    # 打官方端点：省掉 --base-url
    python -m tests.tools.probe_relay --provider anthropic --model claude-haiku-4-5-20251001

判定标准：
    - PASS：响应体里能看到 tool_use / tool_calls 结构，说明该端点真支持 tool use
    - FAIL (no tool):  只回了纯文本，中转站吃掉了 tools 字段 → 换端点
    - FAIL (http err): HTTP 4xx/5xx，看返回的 body（可能是 WAF 拦截页或非标错误）→ 换端点或找中转客服

不用 SDK：SDK 会做一层错误码归一化，把服务端原始信息盖掉，探针要看的就是那层原始信息。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Optional


# CLAUDE.md §8.6
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# 最小可行的 tool 定义，一个字段的 open_app 就够验证协议支持性。
# 探针不关心业务准确率，只关心"服务端会不会返回 tool_use / tool_calls"。
_ANTHROPIC_TOOL = {
    "name": "open_app",
    "description": "打开一个应用程序",
    "input_schema": {
        "type": "object",
        "properties": {"app_name": {"type": "string"}},
        "required": ["app_name"],
    },
}

_OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "open_app",
        "description": "打开一个应用程序",
        "parameters": {
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
    },
}

_PROBE_TEXT = "打开浏览器"


def _post_json(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict, str]:
    """POST JSON 并返回 (status, response_headers, body_text)。HTTPError 也当结果返回，不抛。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", errors="replace")


def _probe_anthropic(base_url: str, api_key: str, model: str, timeout: float) -> int:
    url = f"{base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "user-agent": "voxctl-probe/0.1",
    }
    body = {
        "model": model,
        "max_tokens": 256,
        "tool_choice": {"type": "any"},
        "tools": [_ANTHROPIC_TOOL],
        "messages": [{"role": "user", "content": _PROBE_TEXT}],
    }

    print(f"POST {url}")
    print(f"model = {model}")
    print()
    status, resp_headers, body_text = _post_json(url, headers, body, timeout)
    print(f"HTTP {status}")
    _print_interesting_headers(resp_headers)
    print("--- response body ---")
    print(body_text)
    print("---------------------")

    if status != 200:
        print()
        print("FAIL (http err): 中转站返回非 200，看上面的 body 判断是 WAF 拦截、模型名不对、还是余额/权限问题")
        return 1

    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError:
        print()
        print("FAIL (bad json): 服务端返回不是合法 JSON，中转站没按 Anthropic 协议实现")
        return 1

    content = parsed.get("content") or []
    tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    stop_reason = parsed.get("stop_reason")

    print()
    print(f"content block types: {[b.get('type') for b in content if isinstance(b, dict)]}")
    print(f"stop_reason: {stop_reason}")

    if tool_uses:
        print()
        print(f"PASS: 端点返回 {len(tool_uses)} 个 tool_use block，支持 Anthropic tool use")
        print(f"  first tool_use.name  = {tool_uses[0].get('name')}")
        print(f"  first tool_use.input = {tool_uses[0].get('input')}")
        return 0

    print()
    print(
        "FAIL (no tool): tool_choice=any 但响应里没有 tool_use block；\n"
        "  中转站大概率吃掉了 tools 字段，或后端接的不是真 Anthropic 实现——不能用来跑 Step 3。"
    )
    return 1


def _probe_openai(base_url: str, api_key: str, model: str, timeout: float) -> int:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "user-agent": "voxctl-probe/0.1",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _PROBE_TEXT}],
        "tools": [_OPENAI_TOOL],
        "tool_choice": "required",
    }

    print(f"POST {url}")
    print(f"model = {model}")
    print()
    status, resp_headers, body_text = _post_json(url, headers, body, timeout)
    print(f"HTTP {status}")
    _print_interesting_headers(resp_headers)
    print("--- response body ---")
    print(body_text)
    print("---------------------")

    if status != 200:
        print()
        print("FAIL (http err): 中转站返回非 200，看上面的 body 判断是 WAF 拦截、模型名不对、还是余额/权限问题")
        return 1

    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError:
        print()
        print("FAIL (bad json): 服务端返回不是合法 JSON，中转站没按 OpenAI 协议实现")
        return 1

    choices = parsed.get("choices") or []
    if not choices:
        print()
        print("FAIL (no choices): 响应里没有 choices 数组")
        return 1
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    finish_reason = choices[0].get("finish_reason")

    print()
    print(f"finish_reason: {finish_reason}")
    print(f"message keys: {list(message.keys())}")

    if tool_calls:
        first = tool_calls[0]
        fn = first.get("function") or {}
        print()
        print(f"PASS: 端点返回 {len(tool_calls)} 个 tool_call，支持 OpenAI function calling")
        print(f"  first tool_call.function.name      = {fn.get('name')}")
        print(f"  first tool_call.function.arguments = {fn.get('arguments')!r}")
        return 0

    print()
    print(
        "FAIL (no tool): tool_choice=required 但响应里没有 tool_calls；\n"
        "  中转站大概率吃掉了 tools 字段，或不支持 tool_choice=required——不能用来跑 Step 3。"
    )
    return 1


def _print_interesting_headers(headers: dict) -> None:
    """只打印排查中转/WAF 有用的响应头，避免刷屏。"""
    interesting = [
        "server", "cf-ray", "cf-cache-status", "x-cache", "via",
        "content-type", "x-request-id", "x-ratelimit-remaining-requests",
    ]
    for name in interesting:
        for k, v in headers.items():
            if k.lower() == name:
                print(f"  {k}: {v}")
                break


def main() -> int:
    ap = argparse.ArgumentParser(description="probe an LLM endpoint (or relay) for tool use support")
    ap.add_argument("--provider", choices=["anthropic", "openai"], required=True)
    ap.add_argument("--model", required=True, help="model id to probe (透传给端点，不做映射)")
    ap.add_argument(
        "--base-url",
        default=None,
        help="endpoint base url; 留空则打对应厂商官方端点",
    )
    ap.add_argument(
        "--api-key-env",
        default=None,
        help="env var name; 默认 anthropic=ANTHROPIC_API_KEY, openai=OPENAI_API_KEY",
    )
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    default_envs = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    env_name = args.api_key_env or default_envs[args.provider]
    api_key = os.environ.get(env_name, "").strip()
    if not api_key:
        print(f"ERROR: env var {env_name!r} is empty or unset")
        return 2

    default_base_urls = {
        "anthropic": "https://api.anthropic.com",
        "openai": "https://api.openai.com/v1",
    }
    base_url = args.base_url or default_base_urls[args.provider]

    if args.provider == "anthropic":
        return _probe_anthropic(base_url, api_key, args.model, args.timeout)
    return _probe_openai(base_url, api_key, args.model, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
