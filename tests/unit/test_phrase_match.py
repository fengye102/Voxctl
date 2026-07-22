"""core/phrase_match.py 单元测试（纯函数，无设备/网络依赖）。"""

from __future__ import annotations

from core.phrase_match import match_exit_phrase, match_wake_prefix, normalize


# --- normalize -----------------------------------------------------------

def test_normalize_strips_and_removes_spaces():
    assert normalize("  你好 世界  ") == "你好世界"


def test_normalize_lowercases():
    assert normalize("Hello World") == "helloworld"


def test_normalize_fullwidth_letters_and_digits():
    # 全角字母数字 → 半角，并小写
    assert normalize("ＡＢ１２") == "ab12"


def test_normalize_fullwidth_space():
    assert normalize("你好　世界") == "你好世界"


def test_normalize_chinese_punctuation_removed():
    assert normalize("小助手，打开！浏览器？") == "小助手打开浏览器"


def test_normalize_english_punctuation_removed():
    assert normalize("open, the-browser.") == "openthebrowser"


def test_normalize_empty():
    assert normalize("") == ""
    assert normalize("   ") == ""


# --- match_wake_prefix ----------------------------------------------------

WAKE = ["小助手", "你好助手"]


def test_wake_prefix_basic_strip():
    assert match_wake_prefix("小助手打开记事本", WAKE) == (True, "打开记事本")


def test_wake_prefix_with_punctuation_between():
    # 唤醒词后带标点/空格，remainder 从原文切出后 strip（此处逗号归属剩余部分）
    matched, remainder = match_wake_prefix("小助手，打开浏览器", WAKE)
    assert matched is True
    assert remainder == "，打开浏览器"


def test_wake_prefix_only_wake_word():
    assert match_wake_prefix("小助手", WAKE) == (True, "")


def test_wake_prefix_only_wake_word_with_trailing_space():
    assert match_wake_prefix("小助手  ", WAKE) == (True, "")


def test_wake_prefix_no_match():
    assert match_wake_prefix("打开记事本", WAKE) is None


def test_wake_prefix_empty_phrases():
    assert match_wake_prefix("小助手打开记事本", []) is None


def test_wake_prefix_all_whitespace_phrases():
    assert match_wake_prefix("小助手打开记事本", ["", "   "]) is None


def test_wake_prefix_picks_the_matching_one():
    assert match_wake_prefix("你好助手查内存", WAKE) == (True, "查内存")


def test_wake_prefix_remainder_preserves_original_case():
    # 归一化用于匹配，但 remainder 保留原始大小写
    assert match_wake_prefix("小助手Open Chrome", WAKE) == (True, "Open Chrome")


def test_wake_prefix_normalized_match_fullwidth():
    # 原文含全角/标点，归一化后仍能命中前缀，切点映射回原文
    matched, remainder = match_wake_prefix("小助手，打开ＣＨＲＯＭＥ", WAKE)
    assert matched is True
    assert remainder == "，打开ＣＨＲＯＭＥ"


def test_wake_prefix_english_wake_word():
    assert match_wake_prefix("Hey Jarvis, open notepad", ["hey jarvis"]) == (
        True,
        ", open notepad",
    )


# --- match_exit_phrase ----------------------------------------------------

EXIT = ["退出听写", "结束听写"]


def test_exit_exact_match():
    assert match_exit_phrase("退出听写", EXIT) is True


def test_exit_contains_match():
    assert match_exit_phrase("请帮我退出听写吧", EXIT) is True


def test_exit_no_match():
    assert match_exit_phrase("打开记事本", EXIT) is False


def test_exit_empty_phrases():
    assert match_exit_phrase("退出听写", []) is False


def test_exit_all_whitespace_phrases():
    assert match_exit_phrase("退出听写", ["", "  "]) is False


def test_exit_normalized_match_with_punctuation():
    assert match_exit_phrase("退出听写。", EXIT) is True


def test_exit_normalized_match_second_phrase():
    assert match_exit_phrase("好的，结束听写！", EXIT) is True


def test_exit_english_case_insensitive():
    assert match_exit_phrase("Stop Dictation", ["stop dictation"]) is True


def test_exit_number_and_fullwidth():
    assert match_exit_phrase("退出模式２", ["退出模式2"]) is True
