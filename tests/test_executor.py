"""Step 4 验收脚本：执行模块端到端检测。

对应 CLAUDE.md §5 Step 4 验收标准 + §7 测试表：
    - 每类操作至少 3 个真实用例跑通
    - 白名单外操作拦截率 100%

这是系统级验收脚本（非 unit test），真实执行系统操作：
    - 真实打开/关闭 notepad
    - 真实查询 memory/disk/cpu
    - 真实创建并打开桌面测试文件
    - 真实执行 show_ip 命令（powershell）
    - dangerous 命令只验证 needs_confirmation 流程，不实际执行

用法（PowerShell，已激活 venv）：

    python -m tests.test_executor

全部用例通过退出码 0，否则 1。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from executor import Executor, ExecutionResult
from nlu.schema import (
    CloseAppIntent,
    OpenAppIntent,
    OpenFileIntent,
    QuerySystemIntent,
    RunCommandIntent,
    UnknownIntent,
)

# Windows 控制台默认非 UTF-8，中文输出容易乱码（CLAUDE.md §8.6）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class Case:
    """单个验收用例。"""
    name: str
    intent: object
    raw_text: str
    confirmed: bool = False
    expect_success: bool = True
    expect_needs_confirmation: bool = False
    expect_error_contains: Optional[str] = None
    expect_payload_keys: list[str] = field(default_factory=list)
    # 期望不执行真实操作（用于 dangerous 命令的 confirmed=False 验证）
    expect_no_side_effect: bool = False


def load_config() -> dict:
    """加载 config.yaml。"""
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def create_test_file() -> Path:
    """在桌面创建测试文件，返回路径。"""
    desktop = Path(os.path.expandvars("%USERPROFILE%")) / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    test_file = desktop / "voxctl_step4_test.txt"
    test_file.write_text("Voxctl Step 4 验收测试文件", encoding="utf-8")
    return test_file


def cleanup_test_file(test_file: Path) -> None:
    """清理测试文件。"""
    try:
        if test_file.exists():
            test_file.unlink()
    except OSError:
        pass


def run_cases(cases: list[Case], executor: Executor) -> tuple[int, int]:
    """运行表驱动用例，返回 (passed, total)。"""
    passed = 0
    total = len(cases)
    for i, case in enumerate(cases, 1):
        result = executor.execute(case.intent, raw_text=case.raw_text, confirmed=case.confirmed)
        ok, detail = check_case(case, result)
        status = "PASS" if ok else "FAIL"
        print(f"[{i:2d}] {status}  {case.name}")
        print(f"     raw_text={case.raw_text!r} confirmed={case.confirmed}")
        print(f"     result: success={result.success} needs_conf={result.needs_confirmation} error={result.error}")
        if not ok:
            print(f"     期望: success={case.expect_success} needs_conf={case.expect_needs_confirmation}")
            if case.expect_error_contains:
                print(f"     期望 error 含: {case.expect_error_contains!r}")
            if case.expect_payload_keys:
                print(f"     期望 payload 含键: {case.expect_payload_keys}")
        else:
            if case.expect_payload_keys:
                print(f"     payload: {{k: result.payload.get(k) for k in case.expect_payload_keys}}")
        if ok:
            passed += 1
        print()
    return passed, total


def check_case(case: Case, result: ExecutionResult) -> tuple[bool, str]:
    """检查结果是否符合期望。"""
    if result.success != case.expect_success:
        return False, f"success 不匹配: 期望 {case.expect_success}，实际 {result.success}"
    if result.needs_confirmation != case.expect_needs_confirmation:
        return False, f"needs_confirmation 不匹配: 期望 {case.expect_needs_confirmation}，实际 {result.needs_confirmation}"
    if case.expect_error_contains:
        if not result.error or case.expect_error_contains not in result.error:
            return False, f"error 不含 {case.expect_error_contains!r}，实际: {result.error!r}"
    if case.expect_payload_keys:
        for k in case.expect_payload_keys:
            if k not in result.payload:
                return False, f"payload 缺少键 {k!r}，实际 payload: {result.payload}"
    return True, ""


def run_close_app_two_phase(executor: Executor) -> bool:
    """close_app 两阶段验收：先 open notepad，再 close confirmed=True。

    这是最关键的交互流程：confirmed=False 返回待确认 → confirmed=True 真实关闭。
    """
    print("=== close_app 两阶段验收 ===")
    # 先打开 notepad
    print("[ 1] 打开 notepad 准备关闭测试")
    open_result = executor.execute(
        OpenAppIntent(app_name="notepad"), raw_text="打开记事本"
    )
    if not open_result.success:
        print(f"     FAIL  打开 notepad 失败: {open_result.error}")
        return False
    print("     PASS  notepad 已打开")
    time.sleep(1.5)  # 等待 notepad 真正启动

    # confirmed=False 应返回 needs_confirmation
    print("[ 2] close_app notepad confirmed=False（应返回待确认）")
    close_pending = executor.execute(
        CloseAppIntent(app_name="notepad"), raw_text="关闭记事本", confirmed=False
    )
    if not close_pending.needs_confirmation:
        print(f"     FAIL  期望 needs_confirmation=True，实际: {close_pending}")
        return False
    if close_pending.success:
        print("     FAIL  needs_confirmation 时不应 success=True")
        return False
    print("     PASS  返回待确认状态，未实际关闭")

    # 确认 notepad 仍在运行（没被真关）
    import psutil
    notepad_running = any(
        (p.info.get("name") or "").lower() == "notepad.exe"
        for p in psutil.process_iter(["name"])
    )
    if not notepad_running:
        print("     FAIL  notepad 在 confirmed=False 时被意外关闭")
        return False
    print("     PASS  notepad 仍在运行（未误关）")

    # confirmed=True 真实关闭
    print("[ 3] close_app notepad confirmed=True（应真实关闭）")
    close_real = executor.execute(
        CloseAppIntent(app_name="notepad"), raw_text="确定", confirmed=True
    )
    if not close_real.success:
        print(f"     FAIL  关闭失败: {close_real.error}")
        return False
    print(f"     PASS  已关闭 notepad，terminated_pids={close_real.payload.get('terminated_pids')}")
    print()
    return True


def run_open_file_real(executor: Executor, test_file: Path) -> bool:
    """open_file 真实打开验收：用自然语言解析 + 真实打开。"""
    print("=== open_file 真实打开验收 ===")
    # 用自然语言"桌面上的 voxctl_step4_test.txt"
    print("[ 1] open_file 自然语言路径解析")
    result = executor.execute(
        OpenFileIntent(path="桌面上的 voxctl_step4_test.txt"),
        raw_text="打开桌面上的 voxctl_step4_test.txt",
    )
    if not result.success:
        print(f"     FAIL  打开失败: {result.error}")
        return False
    print(f"     PASS  已打开，resolved_path={result.payload.get('resolved_path')}")

    # 关掉弹出的记事本（避免遗留窗口）
    time.sleep(1.0)
    import psutil
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == "notepad.exe":
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    print("     (已清理弹出的记事本窗口)")
    print()
    return True


def run_open_app_flag_off(config: dict) -> bool:
    """allow_open_any_app: false → 恢复旧行为，白名单外 100% 拒绝（§6 修订 15 开关验证）。"""
    print("=== open_app 开关关闭（allow_open_any_app: false）验收 ===")
    cfg = dict(config["whitelist"])
    cfg["allow_open_any_app"] = False
    executor_off = Executor.from_config(cfg)
    result = executor_off.execute(OpenAppIntent(app_name="word"), raw_text="打开 word")
    ok = (not result.success) and ("不在允许范围内" in (result.error or ""))
    status = "PASS" if ok else "FAIL"
    print(f"[ 1] {status}  白名单外应用被拦截（旧行为）: error={result.error!r}")
    print()
    return ok


def main() -> int:
    config = load_config()
    executor = Executor.from_config(config["whitelist"])

    # 准备测试文件
    test_file = create_test_file()
    print(f"已创建测试文件: {test_file}")
    print()

    # ---- 表驱动用例 ----
    cases: list[Case] = [
        # --- open_app（3 用例）---
        Case(
            name="open_app: notepad 白名单内真实打开",
            intent=OpenAppIntent(app_name="notepad"),
            raw_text="打开记事本",
            expect_success=True,
            expect_payload_keys=["app_name", "app_path"],
        ),
        Case(
            name="open_app: 索引查不到的应用播报没找到（修订 15）",
            intent=OpenAppIntent(app_name="完全不存在的应用xyzabc"),
            raw_text="打开完全不存在的应用xyzabc",
            expect_success=False,
            expect_error_contains="没有找到",
        ),
        Case(
            name="open_app: 空应用名被拒绝",
            intent=OpenAppIntent(app_name=""),
            raw_text="打开",
            expect_success=False,
            expect_error_contains="没有找到",
        ),

        # --- close_app（3 用例，两阶段单独跑，这里验证拦截）---
        Case(
            name="close_app: 白名单外应用被拦截",
            intent=CloseAppIntent(app_name="word"),
            raw_text="关闭 word",
            expect_success=False,
            expect_error_contains="不在允许范围内",
        ),
        Case(
            name="close_app: confirmed=False 返回待确认",
            intent=CloseAppIntent(app_name="notepad"),
            raw_text="关闭记事本",
            expect_success=False,
            expect_needs_confirmation=True,
            expect_no_side_effect=True,
        ),
        Case(
            name="close_app: 空应用名被拦截",
            intent=CloseAppIntent(app_name=""),
            raw_text="关闭",
            expect_success=False,
            expect_error_contains="不在允许范围内",
        ),

        # --- query_system（3 用例，memory/disk/cpu 各一）---
        Case(
            name="query_system: memory",
            intent=QuerySystemIntent(resource="memory"),
            raw_text="内存还剩多少",
            expect_success=True,
            expect_payload_keys=["resource", "total_gb", "available_gb", "percent_used"],
        ),
        Case(
            name="query_system: disk",
            intent=QuerySystemIntent(resource="disk"),
            raw_text="C 盘还有多少空间",
            expect_success=True,
            expect_payload_keys=["resource", "drives"],
        ),
        Case(
            name="query_system: cpu",
            intent=QuerySystemIntent(resource="cpu"),
            raw_text="CPU 占用率",
            expect_success=True,
            expect_payload_keys=["resource", "percent_used"],
        ),

        # --- open_file（3 用例，真实打开单独跑，这里验证拦截）---
        Case(
            name="open_file: 白名单外路径被拦截",
            intent=OpenFileIntent(path="C:\\Windows\\System32\\cmd.exe"),
            raw_text="打开 C 盘 Windows 下的 cmd",
            expect_success=False,
            expect_error_contains="不在允许范围内",
        ),
        Case(
            name="open_file: 不存在的文件报错",
            intent=OpenFileIntent(path="桌面上的 不存在的文件_xyz.txt"),
            raw_text="打开桌面上的不存在的文件",
            expect_success=False,
            expect_error_contains="不存在",
        ),
        Case(
            name="open_file: 空路径报错",
            intent=OpenFileIntent(path=""),
            raw_text="打开",
            expect_success=False,
            expect_error_contains="为空",
        ),

        # --- run_command（3 用例）---
        Case(
            name="run_command: show_ip 非危险命令真实执行",
            intent=RunCommandIntent(command_name="show_ip"),
            raw_text="显示 IP 地址",
            expect_success=True,
            expect_payload_keys=["command_name", "returncode", "stdout"],
        ),
        Case(
            name="run_command: empty_recycle_bin dangerous confirmed=False 待确认",
            intent=RunCommandIntent(command_name="empty_recycle_bin"),
            raw_text="清空回收站",
            expect_success=False,
            expect_needs_confirmation=True,
            expect_no_side_effect=True,
        ),
        Case(
            name="run_command: 白名单外命令被拦截",
            intent=RunCommandIntent(command_name="delete_everything"),
            raw_text="删除一切",
            expect_success=False,
            expect_error_contains="不在允许范围内",
        ),

        # --- exit_program（1 用例，附加验证）---
        Case(
            name="exit_program: 返回 should_exit 标志",
            intent=__import__("nlu.schema", fromlist=["ExitProgramIntent"]).ExitProgramIntent(),
            raw_text="退出程序",
            expect_success=True,
            expect_payload_keys=["message"],
        ),
    ]

    print("=== 表驱动用例 ===")
    passed, total = run_cases(cases, executor)

    # ---- 两阶段验收（close_app 真实流程）----
    print()
    two_phase_ok = run_close_app_two_phase(executor)
    if two_phase_ok:
        passed += 1
    total += 1

    # ---- open_file 真实打开 ----
    open_file_ok = run_open_file_real(executor, test_file)
    if open_file_ok:
        passed += 1
    total += 1

    # ---- open_app 开关关闭验证（§6 修订 15）----
    flag_off_ok = run_open_app_flag_off(config)
    if flag_off_ok:
        passed += 1
    total += 1

    # ---- 审计日志验证 ----
    print("=== 审计日志验证 ===")
    log_path = Path("logs/actions.log")
    if log_path.exists():
        log_lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        print(f"日志文件存在，共 {len(log_lines)} 条记录")
        # 验证最后一条是合法 JSON 且含必要字段
        import json
        try:
            last_entry = json.loads(log_lines[-1])
            required_keys = ["timestamp", "raw_text", "intent", "action_type", "success"]
            missing = [k for k in required_keys if k not in last_entry]
            if missing:
                print(f"     FAIL  日志缺少字段: {missing}")
            else:
                print(f"     PASS  最后一条日志字段完整: {last_entry['action_type']} success={last_entry['success']}")
                passed += 1
        except json.JSONDecodeError as exc:
            print(f"     FAIL  日志不是合法 JSON: {exc}")
    else:
        print(f"     FAIL  日志文件不存在: {log_path}")
    total += 1
    print()

    # 清理
    cleanup_test_file(test_file)

    print(f"=== 总计 {passed}/{total} 通过 ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
