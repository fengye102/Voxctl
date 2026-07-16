"""webui 控制台单元测试（CLAUDE.md 修订 16 / 24）。

测试策略：
    - 直接测 executor 的 user_apps.yaml 合并逻辑（合并优先级）
    - 测 webui.gui 的纯函数（validate_alias / read/write_user_apps）
      （修订 24 删掉浏览器版后，这些底层从 server.py 内联进了 gui.py）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml

from executor import Executor, load_user_apps


# ===========================================================================
# 1. user_apps.yaml 合并优先级（executor 层）
# ===========================================================================

class TestUserAppsMerge(unittest.TestCase):

    def _write_yaml(self, tmp: Path, apps: dict) -> Path:
        p = tmp / "user_apps.yaml"
        p.write_text(yaml.safe_dump({"apps": apps}, allow_unicode=True), encoding="utf-8")
        return p

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(load_user_apps(Path("Z:/nope/user_apps.yaml")), {})

    def test_merge_adds_user_alias(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ua = self._write_yaml(tmp, {"我的工具": r"C:\Windows\System32\notepad.exe"})
            wl_cfg = {"apps": {"notepad": r"C:\Windows\System32\notepad.exe"},
                      "allow_open_any_app": False}
            ex = Executor.from_config(wl_cfg, log_path=str(tmp / "a.log"),
                                      user_apps_path=ua)
            # 用户别名进入白名单
            self.assertIsNotNone(ex._whitelist.get_app_path("我的工具"))
            self.assertIsNotNone(ex._whitelist.get_app_path("notepad"))

    def test_config_wins_on_conflict(self):
        """同名时 config.yaml 优先，user_apps 不覆盖。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ua = self._write_yaml(tmp, {"notepad": r"D:\fake\evil.exe"})
            wl_cfg = {"apps": {"notepad": r"C:\Windows\System32\notepad.exe"},
                      "allow_open_any_app": False}
            ex = Executor.from_config(wl_cfg, log_path=str(tmp / "a.log"),
                                      user_apps_path=ua)
            path = ex._whitelist.get_app_path("notepad")
            self.assertEqual(str(path).lower(), r"c:\windows\system32\notepad.exe".lower())


# ===========================================================================
# 2. webui 纯函数（修订 24：从 webui.gui 导入，浏览器版 server.py 已删）
# ===========================================================================

class TestWebuiHelpers(unittest.TestCase):

    def test_validate_alias_rejects_empty_name(self):
        from webui.gui import validate_alias
        self.assertIsNotNone(validate_alias("", r"C:\Windows\System32\notepad.exe"))

    def test_validate_alias_rejects_relative_path(self):
        from webui.gui import validate_alias
        self.assertIsNotNone(validate_alias("x", "notepad.exe"))

    def test_validate_alias_rejects_wrong_suffix(self):
        from webui.gui import validate_alias
        # 存在但不是 exe/lnk
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            txt = f.name
        try:
            self.assertIsNotNone(validate_alias("x", txt))
        finally:
            Path(txt).unlink(missing_ok=True)

    def test_validate_alias_rejects_missing_file(self):
        from webui.gui import validate_alias
        self.assertIsNotNone(validate_alias("x", r"C:\no\such\thing.exe"))

    def test_validate_alias_accepts_real_exe(self):
        from webui.gui import validate_alias
        notepad = r"C:\Windows\System32\notepad.exe"
        if not Path(notepad).is_file():
            self.skipTest("notepad.exe not present")
        self.assertIsNone(validate_alias("记事本", notepad))

    def test_write_then_read_roundtrip(self):
        from webui.gui import write_user_apps, read_user_apps
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "user_apps.yaml"
            write_user_apps({"甲": r"C:\a.exe", "乙": r"C:\b.exe"}, p)
            got = read_user_apps(p)
            self.assertEqual(got, {"甲": r"C:\a.exe", "乙": r"C:\b.exe"})
            # 注释头写进去了但不影响解析
            self.assertIn("修订", p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
