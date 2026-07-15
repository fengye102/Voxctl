"""Step 5 验收脚本：TTS 模板化播报 + 真实合成播放端到端检测。

对应 CLAUDE.md §5 Step 5 验收标准：
    - 执行结果、错误提示都能正确播报
    - 播报文本用模板化拼接（不让 LLM 生成）

两部分测试：
    1. 模板化拼接（format_result）：纯函数，不需要网络/音频设备，
       覆盖所有 action_type + success/fail/needs_confirmation 组合
    2. 真实合成 + 播放（TTSEngine.speak / speak_result）：
       需要网络（edge-tts）+ 音频设备（sounddevice），只跑几条验证

用法（PowerShell，已激活 venv）：

    # 仅跑模板化测试（快，无需网络）
    python -m tests.test_tts --templates-only

    # 全量验收（含真实播放，需联网 + 音频设备）
    python -m tests.test_tts

全部通过退出码 0，否则 1。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Optional

from executor import ExecutionResult
from tts import TTSEngine, format_result
from audio.player import AudioPlayer
import yaml
from pathlib import Path

# Windows 控制台默认非 UTF-8（CLAUDE.md §8.6）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ===========================================================================
# Part 1: 模板化拼接测试（纯函数，无副作用）
# ===========================================================================

@dataclass
class TemplateCase:
    name: str
    result: ExecutionResult
    expected: Optional[str] = None
    expected_contains: Optional[list[str]] = None  # 任一包含即通过（灵活匹配）


TEMPLATE_CASES: list[TemplateCase] = [
    # --- open_app ---
    TemplateCase(
        name="open_app 成功",
        result=ExecutionResult(
            success=True, action_type="open_app",
            payload={"app_name": "chrome", "app_path": "C:\\...\\chrome.exe"},
        ),
        expected="已为你打开 chrome",
    ),
    TemplateCase(
        name="open_app 失败（白名单外）",
        result=ExecutionResult(
            success=False, action_type="open_app",
            error="应用 'word' 不在允许范围内",
            payload={"app_name": "word"},
        ),
        expected="应用 'word' 不在允许范围内",
    ),
    TemplateCase(
        name="open_app 失败（路径不存在）",
        result=ExecutionResult(
            success=False, action_type="open_app",
            error="应用 'xyz' 的路径不存在: ...",
            payload={"app_name": "xyz"},
        ),
        expected="应用 'xyz' 的路径不存在: ...",
    ),

    # --- close_app ---
    TemplateCase(
        name="close_app 待确认",
        result=ExecutionResult(
            success=False, action_type="close_app", needs_confirmation=True,
            payload={"app_name": "notepad", "app_path": "..."},
        ),
        expected="确定要关闭 notepad 吗？",
    ),
    TemplateCase(
        name="close_app 成功（已关闭）",
        result=ExecutionResult(
            success=True, action_type="close_app",
            payload={"app_name": "notepad", "terminated_pids": [1234], "terminated_count": 1},
        ),
        expected="已关闭 notepad",
    ),
    TemplateCase(
        name="close_app 成功（未运行）",
        result=ExecutionResult(
            success=True, action_type="close_app",
            payload={"app_name": "notepad", "terminated_pids": [], "message": "..."},
        ),
        expected="notepad 当前未在运行",
    ),
    TemplateCase(
        name="close_app 失败（白名单外）",
        result=ExecutionResult(
            success=False, action_type="close_app",
            error="应用 'word' 不在允许范围内",
            payload={"app_name": "word"},
        ),
        expected="应用 'word' 不在允许范围内",
    ),

    # --- query_system ---
    TemplateCase(
        name="query_system memory 成功",
        result=ExecutionResult(
            success=True, action_type="query_system",
            payload={"resource": "memory", "total_gb": 16.0, "available_gb": 8.5, "percent_used": 46.9},
        ),
        expected="当前内存剩余 8.5 GB，共 16.0 GB，使用率 46.9%",
    ),
    TemplateCase(
        name="query_system disk 成功（单盘）",
        result=ExecutionResult(
            success=True, action_type="query_system",
            payload={"resource": "disk", "drives": [
                {"drive": "C:\\", "total_gb": 500.0, "free_gb": 200.0, "used_gb": 300.0, "percent_used": 60.0},
            ]},
        ),
        expected="C 盘共 500.0 GB，剩余 200.0 GB",
    ),
    TemplateCase(
        name="query_system disk 成功（多盘）",
        result=ExecutionResult(
            success=True, action_type="query_system",
            payload={"resource": "disk", "drives": [
                {"drive": "C:\\", "total_gb": 500.0, "free_gb": 200.0, "used_gb": 300.0, "percent_used": 60.0},
                {"drive": "D:\\", "total_gb": 1000.0, "free_gb": 800.0, "used_gb": 200.0, "percent_used": 20.0},
            ]},
        ),
        expected_contains=["C 盘共 500.0 GB", "D 盘共 1000.0 GB"],
    ),
    TemplateCase(
        name="query_system cpu 成功",
        result=ExecutionResult(
            success=True, action_type="query_system",
            payload={"resource": "cpu", "percent_used": 23.5},
        ),
        expected="当前 CPU 占用率 23.5%",
    ),
    TemplateCase(
        name="query_system 失败",
        result=ExecutionResult(
            success=False, action_type="query_system",
            error="查询内存失败: ...",
            payload={"resource": "memory"},
        ),
        expected="查询内存失败: ...",
    ),

    # --- open_file ---
    TemplateCase(
        name="open_file 成功",
        result=ExecutionResult(
            success=True, action_type="open_file",
            payload={"raw_path": "桌面上的 test.txt", "resolved_path": "...", "filename": "test.txt"},
        ),
        expected="已打开文件 test.txt",
    ),
    TemplateCase(
        name="open_file 失败（白名单外）",
        result=ExecutionResult(
            success=False, action_type="open_file",
            error="路径不在允许范围内: C:\\Windows\\...",
            payload={"raw_path": "..."},
        ),
        expected="路径不在允许范围内: C:\\Windows\\...",
    ),
    TemplateCase(
        name="open_file 失败（不存在）",
        result=ExecutionResult(
            success=False, action_type="open_file",
            error="文件不存在: ...",
            payload={"raw_path": "..."},
        ),
        expected="文件不存在: ...",
    ),

    # --- run_command ---
    TemplateCase(
        name="run_command 待确认（dangerous）",
        result=ExecutionResult(
            success=False, action_type="run_command", needs_confirmation=True,
            payload={"command_name": "empty_recycle_bin", "dangerous": True, "command": [...]},
        ),
        expected="确定要执行 empty_recycle_bin 吗？",
    ),
    TemplateCase(
        name="run_command 成功（无输出）",
        result=ExecutionResult(
            success=True, action_type="run_command",
            payload={"command_name": "lock_screen", "returncode": 0, "stdout": None},
        ),
        expected="命令 lock_screen 已执行完成",
    ),
    TemplateCase(
        name="run_command 成功（有短输出）",
        result=ExecutionResult(
            success=True, action_type="run_command",
            payload={"command_name": "show_ip", "returncode": 0, "stdout": "192.168.1.100"},
        ),
        expected_contains=["show_ip", "192.168.1.100"],
    ),
    TemplateCase(
        name="run_command 失败",
        result=ExecutionResult(
            success=False, action_type="run_command",
            error="命令退出码 1: ...",
            payload={"command_name": "show_ip", "returncode": 1},
        ),
        expected="命令退出码 1: ...",
    ),

    # --- exit_program ---
    TemplateCase(
        name="exit_program",
        result=ExecutionResult(
            success=True, action_type="exit_program", should_exit=True,
            payload={"message": "退出语音助手"},
        ),
        expected="再见",
    ),

    # --- unknown ---
    TemplateCase(
        name="unknown",
        result=ExecutionResult(
            success=False, action_type="unknown",
            error="empty input",
        ),
        expected="不确定你想做什么",
    ),

    # --- cancel ---
    TemplateCase(
        name="cancel",
        result=ExecutionResult(
            success=True, action_type="cancel",
            payload={"message": "已取消"},
        ),
        expected="已取消",
    ),
]


def run_template_tests() -> tuple[int, int]:
    """运行模板化拼接测试，返回 (passed, total)。"""
    print("=== Part 1: 模板化拼接测试（纯函数）===")
    passed = 0
    total = len(TEMPLATE_CASES)
    for i, case in enumerate(TEMPLATE_CASES, 1):
        got = format_result(case.result)
        ok = _check_template(case, got)
        status = "PASS" if ok else "FAIL"
        print(f"[{i:2d}] {status}  {case.name}")
        print(f"     期望: {case.expected!r}" if case.expected else
              f"     期望包含: {case.expected_contains!r}")
        print(f"     实际: {got!r}")
        if ok:
            passed += 1
        print()
    return passed, total


def _check_template(case: TemplateCase, got: str) -> bool:
    if case.expected is not None:
        return got == case.expected
    if case.expected_contains:
        return all(s in got for s in case.expected_contains)
    return False


# ===========================================================================
# Part 2: 真实 TTS 合成 + 播放测试（需网络 + 音频设备）
# ===========================================================================

async def run_real_playback_tests() -> tuple[int, int]:
    """真实合成 + 播放验收，返回 (passed, total)。"""
    print("=== Part 2: 真实 TTS 合成 + 播放测试（需网络 + 音频设备）===")

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    player = AudioPlayer()
    engine = TTSEngine.from_config(config["tts"], player)

    cases = [
        ("简单文本播报", "你好，这是语音助手测试"),
        ("模板化结果播报（open_app 成功）", format_result(ExecutionResult(
            success=True, action_type="open_app",
            payload={"app_name": "记事本", "app_path": "..."},
        ))),
        ("模板化结果播报（query_system memory）", format_result(ExecutionResult(
            success=True, action_type="query_system",
            payload={"resource": "memory", "total_gb": 16.0, "available_gb": 8.5, "percent_used": 46.9},
        ))),
        ("错误提示播报（白名单外）", format_result(ExecutionResult(
            success=False, action_type="open_app",
            error="应用 'word' 不在允许范围内",
            payload={"app_name": "word"},
        ))),
    ]

    passed = 0
    total = len(cases)
    for i, (name, text) in enumerate(cases, 1):
        print(f"[{i}] {name}")
        print(f"    文本: {text!r}")
        try:
            await engine.speak(text)
            print(f"    PASS  播放完成")
            passed += 1
        except Exception as exc:
            print(f"    FAIL  异常: {type(exc).__name__}: {exc}")
        print()

    return passed, total


# ===========================================================================
# main
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Step 5 TTS acceptance test")
    ap.add_argument(
        "--templates-only",
        action="store_true",
        help="只跑模板化测试，跳过真实播放（无需网络/音频设备）",
    )
    args = ap.parse_args()

    total_passed = 0
    total_count = 0

    # Part 1: 模板化（必跑）
    p1_passed, p1_total = run_template_tests()
    total_passed += p1_passed
    total_count += p1_total

    # Part 2: 真实播放（可选）
    if not args.templates_only:
        print()
        p2_passed, p2_total = asyncio.run(run_real_playback_tests())
        total_passed += p2_passed
        total_count += p2_total

    print(f"=== 总计 {total_passed}/{total_count} 通过 ===")
    return 0 if total_passed == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
