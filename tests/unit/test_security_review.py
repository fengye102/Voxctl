"""修订 47 安全审查单元测试（S1/S2/S3/C1/H3）。

集中验证本次安全修复的新分支，避免拆散到多个文件：
    - S1 file_ops 扩展名白名单 + .lnk 解析拒绝
    - S2 app_control 完整路径匹配优先（避免误杀同名进程）
    - S3 shell_runner allow_args=false 拒绝带 args 调用
    - C1 whitelist apps 键归一化存储与查询
    - H3 allow_open_any_app=true 时 apps 允许为空
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from executor.app_control import AppController
from executor.file_ops import FileOps, _ALLOWED_FILE_EXTENSIONS
from executor.shell_runner import ShellRunner
from executor.whitelist import Whitelist, WhitelistError
from nlu.schema import CloseAppIntent, OpenFileIntent, RunCommandIntent


# ======================================================================
# C1 + H3：Whitelist apps 归一化 + apps 允许为空
# ======================================================================

class TestWhitelistAppsNormalization(unittest.TestCase):
    """C1：apps 键归一化存储（casefold + 去空白），Chrome/chrome/记 事 本 都命中。"""

    def test_casefold_match(self):
        wl = Whitelist.from_config({
            "apps": {"Chrome": "C:\\Program Files\\Chrome\\chrome.exe"},
        })
        self.assertEqual(
            str(wl.get_app_path("chrome")),
            "C:\\Program Files\\Chrome\\chrome.exe",
        )
        self.assertEqual(
            str(wl.get_app_path("CHROME")),
            "C:\\Program Files\\Chrome\\chrome.exe",
        )

    def test_whitespace_stripped_in_lookup(self):
        """归一化去掉所有空白，'记 事 本' 也命中 '记事本'。"""
        wl = Whitelist.from_config({
            "apps": {"记事本": "C:\\Windows\\System32\\notepad.exe"},
        })
        self.assertEqual(
            str(wl.get_app_path("记 事 本")),
            "C:\\Windows\\System32\\notepad.exe",
        )
        self.assertEqual(
            str(wl.get_app_path(" 记事本 ")),
            "C:\\Windows\\System32\\notepad.exe",
        )

    def test_list_app_names_returns_original(self):
        """list_app_names 返回原始未归一化名（用于播报展示）。"""
        wl = Whitelist.from_config({
            "apps": {
                "Chrome": "C:\\chrome.exe",
                "notepad": "C:\\notepad.exe",
            },
        })
        names = wl.list_app_names()
        self.assertIn("Chrome", names)
        self.assertIn("notepad", names)
        # 归一化键不应出现在展示列表
        self.assertNotIn("chrome", names)


class TestWhitelistAppsEmptyH3(unittest.TestCase):
    """H3：allow_open_any_app=true 时 apps 允许为空。"""

    def test_empty_apps_allowed_when_any_app_true(self):
        wl = Whitelist.from_config({
            "apps": {},
            "allow_open_any_app": True,
        })
        self.assertIsNone(wl.get_app_path("chrome"))
        self.assertEqual(wl.list_app_names(), [])

    def test_empty_apps_rejected_when_any_app_false(self):
        with self.assertRaises(WhitelistError) as ctx:
            Whitelist.from_config({
                "apps": {},
                "allow_open_any_app": False,
            })
        self.assertIn("allow_open_any_app=false", str(ctx.exception))

    def test_missing_apps_treated_as_empty(self):
        """apps 字段缺失按空处理，同样受 allow_open_any_app 控制。"""
        wl = Whitelist.from_config({
            "allow_open_any_app": True,
        })
        self.assertEqual(wl.list_app_names(), [])


# ======================================================================
# S3：CommandEntry allow_args 字段 + dangerous 强制 false
# ======================================================================

class TestCommandEntryAllowArgs(unittest.TestCase):

    def test_default_allow_args_false(self):
        wl = Whitelist.from_config({
            "apps": {},
            "commands": [
                {"name": "show_ip", "command": ["cmd", "/c", "ipconfig"]},
            ],
        })
        entry = wl.get_command("show_ip")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.allow_args)
        self.assertFalse(entry.dangerous)

    def test_explicit_allow_args_true_preserved_for_safe_command(self):
        wl = Whitelist.from_config({
            "apps": {},
            "commands": [
                {
                    "name": "echo_args",
                    "command": ["echo"],
                    "dangerous": False,
                    "allow_args": True,
                },
            ],
        })
        entry = wl.get_command("echo_args")
        self.assertTrue(entry.allow_args)

    def test_dangerous_forces_allow_args_false(self):
        """dangerous=true 时即使显式 allow_args=true 也会被覆盖为 false。"""
        wl = Whitelist.from_config({
            "apps": {},
            "commands": [
                {
                    "name": "dangerous_cmd",
                    "command": ["cmd", "/c", "del"],
                    "dangerous": True,
                    "allow_args": True,  # 显式开启，应被强制覆盖
                },
            ],
        })
        entry = wl.get_command("dangerous_cmd")
        self.assertTrue(entry.dangerous)
        self.assertFalse(entry.allow_args, "dangerous=True 必须强制 allow_args=False")


# ======================================================================
# S3：ShellRunner allow_args=false 拒绝带 args 调用
# ======================================================================

class TestShellRunnerAllowArgs(unittest.TestCase):

    def _make_runner(self, *, allow_args: bool, dangerous: bool = False) -> ShellRunner:
        wl = Whitelist.from_config({
            "apps": {},
            "commands": [
                {
                    "name": "test_cmd",
                    "command": ["echo"],
                    "dangerous": dangerous,
                    "allow_args": allow_args,
                },
            ],
        })
        return ShellRunner(wl)

    def test_args_rejected_when_allow_args_false(self):
        runner = self._make_runner(allow_args=False)
        result = runner.run(
            RunCommandIntent(command_name="test_cmd", args=["hello"]),
            confirmed=True,
        )
        self.assertFalse(result.success)
        self.assertIn("不支持参数", result.error)
        self.assertEqual(result.payload["rejected_args"], ["hello"])

    def test_args_allowed_when_allow_args_true(self):
        runner = self._make_runner(allow_args=True)
        # mock subprocess.run 避免真实执行
        with patch("executor.shell_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="hello\n", stderr=""
            )
            result = runner.run(
                RunCommandIntent(command_name="test_cmd", args=["hello"]),
                confirmed=True,
            )
        self.assertTrue(result.success)
        # full_command = 白名单 command + args
        called_args = mock_run.call_args[0][0]
        self.assertEqual(called_args, ["echo", "hello"])

    def test_dangerous_command_rejects_args_even_if_config_says_true(self):
        """dangerous 命令配置 allow_args=true，Whitelist 层已强制 false，
        ShellRunner 应据此拒绝带 args 调用（dangerous + 确认了仍然拒绝 args）。"""
        runner = self._make_runner(allow_args=True, dangerous=True)
        result = runner.run(
            RunCommandIntent(command_name="test_cmd", args=["sneaky"]),
            confirmed=True,  # 用户已确认 dangerous
        )
        self.assertFalse(result.success)
        self.assertIn("不支持参数", result.error)

    def test_no_args_passes_when_allow_args_false(self):
        """allow_args=false 但 intent.args 为空 → 正常执行。"""
        runner = self._make_runner(allow_args=False)
        with patch("executor.shell_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ok", stderr=""
            )
            result = runner.run(
                RunCommandIntent(command_name="test_cmd", args=[]),
                confirmed=True,
            )
        self.assertTrue(result.success)


# ======================================================================
# S1：FileOps 扩展名白名单
# ======================================================================

class TestFileOpsExtensionWhitelist(unittest.TestCase):
    """S1：open_file 拒绝可执行类扩展名，只允许文档/图片/音视频等。"""

    def _make_file_ops(self, tmp_path: Path) -> FileOps:
        wl = Whitelist.from_config({
            "apps": {},
            "allow_open_any_app": True,
            "paths": [str(tmp_path)],
        })
        return FileOps(wl)

    def test_allowed_extension_passes_check(self):
        """白名单内 .txt 文件通过扩展名校验（os.startfile 用 mock 避免真实打开）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            f = tmp / "notes.txt"
            f.write_text("hi", encoding="utf-8")
            ops = self._make_file_ops(tmp)

            with patch("executor.file_ops.os.startfile") as mock_start:
                result = ops.open_file(OpenFileIntent(path=str(f)))
            self.assertTrue(result.success, msg=result.error)
            mock_start.assert_called_once()

    def test_executable_extension_rejected(self):
        """.bat 文件即使在白名单目录也拒绝（防文件关联绕过 whitelist.commands）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            f = tmp / "evil.bat"
            f.write_text("@echo off", encoding="utf-8")
            ops = self._make_file_ops(tmp)

            with patch("executor.file_ops.os.startfile") as mock_start:
                result = ops.open_file(OpenFileIntent(path=str(f)))
            self.assertFalse(result.success)
            self.assertIn("不支持打开此类型文件", result.error)
            self.assertIn(".bat", result.error)
            mock_start.assert_not_called()

    def test_exe_extension_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            f = tmp / "fake.exe"
            f.write_bytes(b"MZ")
            ops = self._make_file_ops(tmp)

            with patch("executor.file_ops.os.startfile"):
                result = ops.open_file(OpenFileIntent(path=str(f)))
            self.assertFalse(result.success)
            self.assertIn(".exe", result.error)

    def test_lnk_rejected_when_target_unresolvable(self):
        """.lnk 解析失败（pywin32 不可用或解析返回 None）→ 拒绝打开。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            f = tmp / "shortcut.lnk"
            f.write_bytes(b"fake lnk content")
            ops = self._make_file_ops(tmp)

            # 强制 _resolve_lnk_target 返回 None（模拟 pywin32 不可用/解析失败）
            with patch("executor.file_ops._resolve_lnk_target", return_value=None), \
                 patch("executor.file_ops.os.startfile") as mock_start:
                result = ops.open_file(OpenFileIntent(path=str(f)))
            self.assertFalse(result.success)
            self.assertIn(".lnk", result.error)
            mock_start.assert_not_called()

    def test_lnk_rejected_when_target_is_executable(self):
        """.lnk 指向 .exe → 拒绝（不允许通过快捷方式绕过扩展名白名单）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            lnk = tmp / "evil_shortcut.lnk"
            lnk.write_bytes(b"fake lnk")
            ops = self._make_file_ops(tmp)

            fake_target = Path("C:\\Windows\\System32\\cmd.exe")
            with patch("executor.file_ops._resolve_lnk_target", return_value=fake_target), \
                 patch("executor.file_ops.os.startfile") as mock_start:
                result = ops.open_file(OpenFileIntent(path=str(lnk)))
            self.assertFalse(result.success)
            self.assertIn("快捷方式指向的文件类型不支持打开", result.error)
            mock_start.assert_not_called()

    def test_lnk_allowed_when_target_is_document(self):
        """.lnk 指向 .txt → 允许打开（解析出 target 后按 target 扩展名校验）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            real_txt = tmp / "real.txt"
            real_txt.write_text("hi", encoding="utf-8")
            lnk = tmp / "doc_shortcut.lnk"
            lnk.write_bytes(b"fake lnk")
            ops = self._make_file_ops(tmp)

            with patch("executor.file_ops._resolve_lnk_target", return_value=real_txt), \
                 patch("executor.file_ops.os.startfile") as mock_start:
                result = ops.open_file(OpenFileIntent(path=str(lnk)))
            self.assertTrue(result.success, msg=result.error)
            mock_start.assert_called_once_with(str(lnk))

    def test_extension_whitelist_does_not_include_executable_types(self):
        """直接校验白名单集合不含 .bat/.cmd/.ps1/.exe/.scr/.js 等可执行扩展名。"""
        forbidden = {".bat", ".cmd", ".ps1", ".exe", ".scr", ".js", ".vbs", ".wsf"}
        self.assertFalse(forbidden & _ALLOWED_FILE_EXTENSIONS,
                         f"可执行扩展名不应在白名单内: {forbidden & _ALLOWED_FILE_EXTENSIONS}")


# ======================================================================
# S2：AppController close_app 完整路径匹配优先
# ======================================================================

class TestAppControlClosePathMatching(unittest.TestCase):
    """S2：close_app 优先按完整路径匹配，避免误杀同名不同路径进程。"""

    def _make_controller(self, app_path: str) -> AppController:
        wl = Whitelist.from_config({
            "apps": {"notepad": app_path},
        })
        return AppController(wl, app_index=None)

    def test_terminates_only_matching_full_path(self):
        """两个同名 notepad.exe 但不同路径 → 只杀路径匹配的那个。"""
        # 白名单指向 C:\System\notepad.exe
        controller = self._make_controller("C:\\System\\notepad.exe")

        # 构造两个进程：一个路径匹配，一个同名但路径不同
        matching_proc = MagicMock()
        matching_proc.info = {"pid": 100, "name": "notepad.exe"}
        matching_proc.exe.return_value = "C:\\System\\notepad.exe"
        matching_proc.terminate = MagicMock()

        other_proc = MagicMock()
        other_proc.info = {"pid": 200, "name": "notepad.exe"}
        other_proc.exe.return_value = "C:\\Portable\\notepad.exe"
        other_proc.terminate = MagicMock()

        # 注意：第一阶段匹配到一个进程后 terminated_pids 非空，
        # 不会进入第二阶段文件名匹配，所以 other_proc 不应被杀
        with patch("executor.app_control.psutil.process_iter",
                   return_value=[matching_proc, other_proc]):
            result = controller.close_app(
                CloseAppIntent(app_name="notepad"), confirmed=True
            )

        self.assertTrue(result.success)
        self.assertEqual(result.payload["terminated_pids"], [100])
        # 修订 48 S2：审计字段记录匹配来源
        self.assertEqual(result.payload["matched_via"], "path")
        matching_proc.terminate.assert_called_once()
        other_proc.terminate.assert_not_called()

    def test_falls_back_to_filename_match_when_full_path_unavailable(self):
        """proc.exe() 全部 AccessDenied → 完整路径阶段无匹配，
        降级到文件名匹配（杀掉所有同名进程）。"""
        controller = self._make_controller("C:\\System\\notepad.exe")

        proc1 = MagicMock()
        proc1.info = {"pid": 100, "name": "notepad.exe"}
        proc1.exe.side_effect = psutil.AccessDenied
        proc1.terminate = MagicMock()

        proc2 = MagicMock()
        proc2.info = {"pid": 200, "name": "notepad.exe"}
        proc2.exe.side_effect = psutil.AccessDenied
        proc2.terminate = MagicMock()

        with patch("executor.app_control.psutil.process_iter",
                   return_value=[proc1, proc2]):
            result = controller.close_app(
                CloseAppIntent(app_name="notepad"), confirmed=True
            )

        self.assertTrue(result.success)
        self.assertEqual(sorted(result.payload["terminated_pids"]), [100, 200])
        # 修订 48 S2：降级到文件名匹配
        self.assertEqual(result.payload["matched_via"], "name")
        proc1.terminate.assert_called_once()
        proc2.terminate.assert_called_once()

    def test_falls_back_to_filename_when_no_full_path_match(self):
        """所有进程的完整路径都不匹配白名单 → 降级文件名匹配。"""
        controller = self._make_controller("C:\\System\\notepad.exe")

        # 进程名匹配但路径不匹配（便携版 notepad）
        proc = MagicMock()
        proc.info = {"pid": 100, "name": "notepad.exe"}
        proc.exe.return_value = "C:\\Portable\\notepad.exe"
        proc.terminate = MagicMock()

        with patch("executor.app_control.psutil.process_iter",
                   return_value=[proc]):
            result = controller.close_app(
                CloseAppIntent(app_name="notepad"), confirmed=True
            )

        # 完整路径阶段未匹配到，降级文件名阶段匹配并杀掉
        self.assertTrue(result.success)
        self.assertEqual(result.payload["terminated_pids"], [100])
        self.assertEqual(result.payload["matched_via"], "name")
        proc.terminate.assert_called_once()

    def test_returns_success_when_no_process_running(self):
        """没有任何进程匹配 → 返回 success=True（应用本来就没开）。"""
        controller = self._make_controller("C:\\System\\notepad.exe")

        with patch("executor.app_control.psutil.process_iter",
                   return_value=[]):
            result = controller.close_app(
                CloseAppIntent(app_name="notepad"), confirmed=True
            )

        self.assertTrue(result.success)
        self.assertEqual(result.payload["terminated_pids"], [])
        # 修订 48 S2：无进程匹配
        self.assertEqual(result.payload["matched_via"], "none")
        self.assertIn("未在运行", result.payload["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
