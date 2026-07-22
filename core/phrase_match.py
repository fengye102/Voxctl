"""纯函数文本匹配（唤醒词前缀 + 听写退出短语复用）。

供两处复用：
  - 唤醒词：对识别文本做前缀匹配，命中后剥离唤醒词，返回剩余原始文本继续走意图解析。
  - 听写退出：对识别文本做整句/包含匹配，命中即退出听写模式。

全部为模块级纯函数：无 IO、无 subprocess/os、无网络、跨平台。匹配统一在
normalize() 归一化后的形态上比较，避免全角/半角、标点、大小写、空白造成的漏配；
但唤醒词剥离后的 remainder 从**原始文本**切出（仅 strip），保留原样交给下游。
"""

from __future__ import annotations

import unicodedata


def _keep_char(ch: str) -> bool:
    """判断归一化后是否保留该字符：去空白、去标点（Unicode 类别 P*）。"""
    if ch.isspace():
        return False
    if unicodedata.category(ch).startswith("P"):
        return False
    return True


def normalize(text: str) -> str:
    """归一化：全角转半角（NFKC）、去中英文标点和空白、转小写。用于匹配前统一形态。"""
    nfkc = unicodedata.normalize("NFKC", text)
    return "".join(ch.lower() for ch in nfkc if _keep_char(ch))


def _build_norm_map(text: str) -> tuple[str, list[int]]:
    """构造原始文本的归一化串，以及每个归一化字符对应的「原文切点」。

    orig_after[k] = 产出第 k 个归一化字符的原文字符之后的原文索引，
    因此 text[:orig_after[k]] 归一化后恰好覆盖 norm_str[:k+1]。
    与 normalize() 保持一致（同样的 NFKC + 过滤规则，只是逐字符累积以记录切点）。
    """
    norm_chars: list[str] = []
    orig_after: list[int] = []
    for i, ch in enumerate(text):
        for nc in unicodedata.normalize("NFKC", ch):
            if _keep_char(nc):
                norm_chars.append(nc.lower())
                orig_after.append(i + 1)
    return "".join(norm_chars), orig_after


def _norm_phrases(phrases: list[str]) -> list[str]:
    """把短语表逐条归一化并丢弃空白项；返回非空归一化短语列表。"""
    result = []
    for p in phrases or []:
        if p is None:
            continue
        np_ = normalize(p)
        if np_:
            result.append(np_)
    return result


def match_wake_prefix(text: str, phrases: list[str]) -> tuple[bool, str] | None:
    """唤醒词前缀匹配。

    返回 (matched, remainder)：
      - 命中某个唤醒词前缀：matched=True，remainder=剥离唤醒词后的原始剩余文本
        （保留原始大小写/标点，从原文切出后仅 strip；用户只说唤醒词时为空串）。
      - 未命中任何唤醒词，或 phrases 为空/全空白（视为未配置）：返回 None。

    匹配在 normalize 后的形态上比较前缀，剥离时映射回原始 text 的切点。
    """
    norm_phrases = _norm_phrases(phrases)
    if not norm_phrases:
        return None

    norm_str, orig_after = _build_norm_map(text)
    for np_ in norm_phrases:
        if norm_str.startswith(np_):
            length = len(np_)
            cut = orig_after[length - 1] if length > 0 else 0
            return True, text[cut:].strip()
    return None


def match_exit_phrase(text: str, phrases: list[str]) -> bool:
    """听写退出短语匹配：normalize 后，text 等于任一短语或包含任一短语即 True。

    phrases 为空/全空白 → False。
    """
    norm_text = normalize(text)
    for np_ in _norm_phrases(phrases):
        if np_ in norm_text:
            return True
    return False
