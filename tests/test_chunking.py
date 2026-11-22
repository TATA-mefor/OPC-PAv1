"""M4 第一批：分块纯函数（字符预算近似，断言边界与重叠，不断言精确 token）。"""
from __future__ import annotations

import pytest

from opc_workbench import chunking


def _est(text: str) -> float:
    """独立事实源：测试自行估算，不复用被测模块的私有函数。"""
    return sum(1.0 if ord(c) >= 0x2E80 else 0.25 for c in text)


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t  \n"])
def test_empty_text_yields_no_chunks(text):
    assert chunking.chunk_text(text) == []


def test_short_text_single_chunk_verbatim():
    text = "短文本，无需切分。"
    assert chunking.chunk_text(text) == [text]


def test_long_cjk_respects_budget():
    text = "财" * 2000
    chunks = chunking.chunk_text(text, target=100, overlap=10)
    assert len(chunks) > 1
    assert all(_est(c) <= 100 for c in chunks)
    assert all(c for c in chunks)


def test_every_chunk_is_substring_and_ends_match():
    text = "段落内容。" * 400
    chunks = chunking.chunk_text(text, target=120, overlap=20)
    assert text.startswith(chunks[0])
    assert text.endswith(chunks[-1])
    assert all(c in text for c in chunks)


def test_overlap_present_between_consecutive_chunks():
    text = "甲" * 1000
    chunks = chunking.chunk_text(text, target=100, overlap=20)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.endswith(nxt[:10])          # 后块开头来自前块尾部
        assert len(nxt) > 0


def test_heading_boundary_starts_new_chunk():
    body = "引子" * 60
    text = body + "\n# 第二章\n" + "正文" * 200
    chunks = chunking.chunk_text(text, target=150, overlap=10)
    assert any(c.startswith("# 第二章") for c in chunks)


def test_unbroken_long_paragraph_hard_cut_terminates():
    text = "x" * 20000                            # 无任何语义边界
    chunks = chunking.chunk_text(text, target=200, overlap=20)
    assert len(chunks) > 1
    assert all(_est(c) <= 200 for c in chunks)
    assert "".join(chunks[:1]) == text[:len(chunks[0])]


def test_deterministic():
    text = "混合 content 中文与 English words。" * 100
    assert chunking.chunk_text(text) == chunking.chunk_text(text)


def test_default_budget_matches_prd_approximation():
    text = "中" * 1200
    chunks = chunking.chunk_text(text)          # 默认 512 / 64
    assert all(_est(c) <= 512 for c in chunks)
    assert len(chunks) >= 2


@pytest.mark.parametrize("kw", [
    {"target": 0}, {"target": -1}, {"overlap": -1}, {"overlap": 100, "target": 100},
])
def test_invalid_params_rejected(kw):
    with pytest.raises(ValueError):
        chunking.chunk_text("some text", **kw)

# --- 已批准的分块规则：标题不重叠，其余边界与硬切按 64 重叠 ------------------

def test_heading_chunk_has_no_overlap_from_previous():
    """审查批准规则：标题完整地位于新块开头，不带入前块尾部内容。"""
    text = "引子" * 60 + "\n# 第二章\n" + "正文" * 200
    chunks = chunking.chunk_text(text, target=150, overlap=10)
    idx = next(i for i, c in enumerate(chunks) if c.startswith("# 第二章"))
    assert idx > 0
    prev = chunks[idx - 1]
    assert prev.endswith("\n")                       # 前块止于标题前的换行
    assert not prev.endswith(chunks[idx][:5])        # 无重叠带入


def test_hard_cut_uses_default_64_overlap():
    text = "甲" * 3000                                # 无边界 → 硬切
    chunks = chunking.chunk_text(text)                # 默认 target=512 / overlap=64
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.endswith(nxt[:64])                # 恰好 64 字符重叠
        assert len(nxt) > 64


def test_paragraph_boundary_keeps_overlap():
    text = ("段落内容若干。\n\n" * 300)
    chunks = chunking.chunk_text(text, target=120, overlap=20)
    assert len(chunks) > 2
    shared = sum(1 for p, n in zip(chunks, chunks[1:]) if p.endswith(n[:5]))
    assert shared > 0                                 # 普通边界仍保留重叠
