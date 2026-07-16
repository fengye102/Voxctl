"""AppIndex 单元测试（CLAUDE.md §6 修订 15：打开应用放开）。

测试策略：
    用临时目录伪造开始菜单结构（建真实 .lnk 文件即可，AppIndex 只读文件名
    不解析快捷方式目标），关掉注册表扫描（scan_registry=False）保证跨机稳定。
    覆盖匹配规则：精确 / 唯一子串 / 多命中歧义 / 零命中 / 卸载项排除。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from executor.app_index import AppIndex, MatchStatus


def _make_start_menu(tmp: Path, names: list[str]) -> Path:
    """在 tmp 下建一个 Programs 目录，为每个名字建一个空 .lnk 文件。"""
    programs = tmp / "Programs"
    programs.mkdir(parents=True, exist_ok=True)
    for name in names:
        (programs / f"{name}.lnk").write_bytes(b"")  # 内容不重要，只看文件名
    return programs


class TestAppIndexBuild(unittest.TestCase):

    def test_build_collects_lnk_recursively(self):
        """递归收集 .lnk，子目录也算。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            programs = _make_start_menu(tmp, ["微信", "Google Chrome"])
            # 子目录里再放一个
            sub = programs / "网易"
            sub.mkdir()
            (sub / "网易云音乐.lnk").write_bytes(b"")

            index = AppIndex.build(start_menu_dirs=[programs], scan_registry=False)

            self.assertEqual(len(index), 3)
            names = index.list_names()
            self.assertIn("微信", names)
            self.assertIn("网易云音乐", names)

    def test_build_excludes_uninstall_shortcuts(self):
        """卸载类快捷方式不进索引（防'打开 XX'命中'卸载 XX'）。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            programs = _make_start_menu(
                tmp, ["某软件", "Uninstall 某软件", "卸载某软件"]
            )
            index = AppIndex.build(start_menu_dirs=[programs], scan_registry=False)

            self.assertEqual(len(index), 1)
            self.assertEqual(index.list_names(), ["某软件"])

    def test_build_missing_dir_is_ok(self):
        """目录不存在不报错，返回空索引。"""
        index = AppIndex.build(
            start_menu_dirs=[Path("Z:/no/such/dir")], scan_registry=False
        )
        self.assertEqual(len(index), 0)


class TestAppIndexResolve(unittest.TestCase):

    def _index(self, names: list[str]) -> AppIndex:
        self._tmpdir = tempfile.TemporaryDirectory()
        programs = _make_start_menu(Path(self._tmpdir.name), names)
        return AppIndex.build(start_menu_dirs=[programs], scan_registry=False)

    def tearDown(self):
        if hasattr(self, "_tmpdir"):
            self._tmpdir.cleanup()

    def test_exact_match(self):
        index = self._index(["微信", "QQ"])
        m = index.resolve("微信")
        self.assertEqual(m.status, MatchStatus.FOUND)
        self.assertEqual(m.entry.display_name, "微信")

    def test_exact_match_case_insensitive_and_spaces(self):
        """归一化：大小写 + 空格无关。"""
        index = self._index(["Google Chrome"])
        m = index.resolve("google chrome")
        self.assertEqual(m.status, MatchStatus.FOUND)
        m2 = index.resolve("GoogleChrome")
        self.assertEqual(m2.status, MatchStatus.FOUND)

    def test_unique_substring_match(self):
        """查询词是唯一候选的子串 → 命中。"""
        index = self._index(["Google Chrome", "微信"])
        m = index.resolve("chrome")
        self.assertEqual(m.status, MatchStatus.FOUND)
        self.assertEqual(m.entry.display_name, "Google Chrome")

    def test_ambiguous_multiple_substring_hits(self):
        """命中多个 → AMBIGUOUS，带候选名，不猜测执行。"""
        index = self._index(["微信", "微信开发者工具"])
        m = index.resolve("微信")
        # "微信" 精确命中第一个，不歧义
        self.assertEqual(m.status, MatchStatus.FOUND)
        # "微" 是两个的子串 → 歧义
        m2 = index.resolve("微")
        self.assertEqual(m2.status, MatchStatus.AMBIGUOUS)
        self.assertEqual(len(m2.candidates), 2)
        self.assertIn("微信", m2.candidates)

    def test_not_found(self):
        index = self._index(["微信"])
        m = index.resolve("不存在的应用")
        self.assertEqual(m.status, MatchStatus.NOT_FOUND)

    def test_empty_query_not_found(self):
        index = self._index(["微信"])
        m = index.resolve("")
        self.assertEqual(m.status, MatchStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main(verbosity=2)
