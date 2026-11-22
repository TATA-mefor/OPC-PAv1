"""分块纯函数（M4 第一批）。

PRD §5.3 目标 512 token/块、重叠 64、标题/段落边界优先。
**显式工程近似**（M4.1 §五.2 已获审查通过）：不引入 tokenizer，按字符权重
估算 token —— CJK 等宽字符计 1，其余计 0.25；断言块数边界与重叠语义，
不断言精确 token 数。

纯函数：无 IO、无外部依赖、确定性；ingest（第二批）import 使用。
"""
from __future__ import annotations

DEFAULT_TARGET = 512
DEFAULT_OVERLAP = 64

# 边界优先级：标题 > 空行段落 > 换行 > 句末
_BOUNDARIES = ("\n#", "\n\n", "\n", "。", "！", "？", ". ", "! ", "? ")


def _weight(ch: str) -> float:
    """CJK 及全角符号按 1 token 估，其余按 4 字符≈1 token 估。"""
    return 1.0 if ord(ch) >= 0x2E80 else 0.25


def _estimate(text: str) -> float:
    return sum(_weight(c) for c in text)


def _find_cut(text: str, start: int, window: int, hi: int) -> tuple[int, bool] | None:
    """找语义边界 → (切点, 是否标题边界)。

    标题在**整块范围**内搜索（标题是天然块首，宁可块短也要对齐）；
    段落/换行/句末只在尾部窗口内搜索，避免块被切得过短。
    """
    idx = text.rfind("\n#", start + 1, hi)
    if idx >= 0:
        cut = idx + 1
        if start < cut < hi:
            return cut, True
    for marker in _BOUNDARIES[1:]:
        idx = text.rfind(marker, window, hi)
        if idx < 0:
            continue
        cut = idx + len(marker)
        if window <= cut < hi and cut > start:
            return cut, False
    return None


def chunk_text(text: str, *, target: int = DEFAULT_TARGET,
               overlap: int = DEFAULT_OVERLAP) -> list[str]:
    """按预算切块，块间保留重叠；空文本返回 []。"""
    if target <= 0:
        raise ValueError("chunk_target_invalid")
    if overlap < 0 or overlap >= target:
        raise ValueError("chunk_overlap_invalid")
    if not text or not text.strip():
        return []

    chunks: list[str] = []
    n = len(text)
    start = 0
    while start < n:
        acc = 0.0
        end = start
        while end < n and acc + _weight(text[end]) <= target:
            acc += _weight(text[end])
            end += 1
        if end >= n:                                   # 尾块
            chunks.append(text[start:])
            break
        window = max(start + 1, end - max(1, target // 5))
        found = _find_cut(text, start, window, end)
        cut, at_heading = found if found else (end, False)
        if cut <= start:
            cut, at_heading = end, False
        chunks.append(text[start:cut])

        if at_heading:
            # 标题是天然块首：不回退重叠，避免把标题裹进上一块尾部
            start = cut
            continue
        back, acc2 = cut, 0.0                          # 回退 overlap 个估算 token
        while back > start and acc2 + _weight(text[back - 1]) <= overlap:
            acc2 += _weight(text[back - 1])
            back -= 1
        start = back if back > start else cut          # 保证前进，绝不空转
    return chunks