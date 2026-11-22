"""M4 第一批：FakeLLM.embed 测试替身契约（确定性 / 归一化 / 可编排异常，零网络）。"""
from __future__ import annotations

import math

import pytest

from opc_workbench import llm_client


def test_embed_deterministic_and_text_sensitive(isolated_env):
    fake = llm_client.get_llm()
    a1, b1 = fake.embed(["财务口径", "销售话术"])
    a2 = fake.embed(["财务口径"])[0]
    assert a1 == a2                      # 同文本同向量
    assert a1 != b1                      # 不同文本不同向量


def test_embed_vectors_normalized(isolated_env):
    fake = llm_client.get_llm()
    for vec in fake.embed(["甲", "乙", "丙"]):
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


def test_embed_dim_configurable():
    FakeLLM = type(llm_client.get_llm())   # 避免 tests 包名与 site-packages 冲突
    fake = FakeLLM(embed_dim=16)
    assert all(len(v) == 16 for v in fake.embed(["x", "y"]))


def test_embed_no_nan_inf_or_zero_vector(isolated_env):
    fake = llm_client.get_llm()
    for vec in fake.embed(["", "a", "中文"]):
        assert all(math.isfinite(v) for v in vec)
        assert any(v != 0.0 for v in vec)


def test_embed_failure_orchestrable():
    FakeLLM = type(llm_client.get_llm())   # 避免 tests 包名与 site-packages 冲突
    fake = FakeLLM(embed_responses=[llm_client.LLMFailure("boom")])
    with pytest.raises(llm_client.LLMFailure):
        fake.embed(["x"])


def test_embed_explicit_vectors_orchestrable():
    FakeLLM = type(llm_client.get_llm())   # 避免 tests 包名与 site-packages 冲突
    fake = FakeLLM(embed_responses=[[[1.0, 0.0], [0.0, 1.0]]])
    assert fake.embed(["x", "y"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_embed_calls_recorded_without_touching_chat(isolated_env):
    fake = llm_client.get_llm()
    fake.embed(["块一", "块二"])
    assert fake.calls[-1] == {"kind": "embed", "texts": ["块一", "块二"]}
    assert fake.chat([{"role": "user", "content": "hi"}]) == "fake-ok"   # chat 语义零改动