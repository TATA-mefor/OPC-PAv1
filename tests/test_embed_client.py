"""LLMClient.embed 生产实现（M4 第二批 C3：独立熔断器）。

传输层用桩注入（沿用 test_llm_client 既有 seam：api_key="" 不构造真 SDK，
再替换 _client），全程零网络。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from opc_workbench import config, llm_client


class _FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def _vec(dim, seed):
    return [float((seed + i) % 7) / 7.0 for i in range(dim)]


class _StubEmbeddings:
    """记录每次请求；behavior(call_index, kwargs) 决定返回或抛错。"""

    def __init__(self, behavior, *, shuffle=False, dim=None):
        self.behavior = behavior
        self.calls: list[dict] = []
        self.shuffle = shuffle
        self.dim = config.EMBEDDING_DIM if dim is None else dim

    def create(self, **kw):
        n = len(self.calls)
        self.calls.append(kw)
        result = self.behavior(n, kw)
        if isinstance(result, Exception):
            raise result
        texts = kw["input"]
        data = [SimpleNamespace(index=i, embedding=_vec(self.dim, i))
                for i in range(len(texts))]
        if self.shuffle:
            data = list(reversed(data))
        return SimpleNamespace(data=data)


class _StubClient:
    def __init__(self, embeddings, chat_behavior=None):
        self.embeddings = embeddings
        behavior = chat_behavior or (lambda n, kw: "chat-ok")
        stub = self

        class _Completions:
            def create(self, **kw):
                stub.chat_calls = getattr(stub, "chat_calls", 0) + 1
                out = behavior(stub.chat_calls - 1, kw)
                if isinstance(out, Exception):
                    raise out
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=out))])

        self.chat = SimpleNamespace(completions=_Completions())


def _make_client(embeddings, chat_behavior=None):
    c = llm_client.LLMClient(api_key="")     # 不构造真 SDK
    c._client = _StubClient(embeddings, chat_behavior)
    return c


def _ok(n, kw):
    return None


# ---------------------------------------------------------------------------


def test_embed_without_key_fails_closed():
    c = llm_client.LLMClient(api_key="")
    with pytest.raises(llm_client.LLMFailure, match="no_api_key"):
        c.embed(["x"])


def test_embed_empty_input_makes_no_call():
    emb = _StubEmbeddings(_ok)
    c = _make_client(emb)
    assert c.embed([]) == []
    assert emb.calls == []


@pytest.mark.parametrize("bad", [None, "text", 123, [1], ["ok", None]])
def test_embed_rejects_non_string_list(bad):
    c = _make_client(_StubEmbeddings(_ok))
    with pytest.raises(llm_client.LLMFailure, match="embed_invalid_input"):
        c.embed(bad)


def test_embed_batches_at_ten_and_preserves_order():
    emb = _StubEmbeddings(_ok)
    c = _make_client(emb)
    texts = [f"t{i}" for i in range(25)]
    out = c.embed(texts)
    assert [len(call["input"]) for call in emb.calls] == [10, 10, 5]
    assert len(out) == 25
    # 第 i 条向量必须来自第 i 条文本所在的批内位置
    assert out[10] == _vec(config.EMBEDDING_DIM, 0)
    assert out[24] == _vec(config.EMBEDDING_DIM, 4)


def test_embed_passes_model_and_dimensions():
    emb = _StubEmbeddings(_ok)
    c = _make_client(emb)
    c.embed(["x"])
    assert emb.calls[0]["model"] == config.EMBEDDING_MODEL
    assert emb.calls[0]["dimensions"] == config.EMBEDDING_DIM


def test_embed_reorders_by_index_not_return_order():
    emb = _StubEmbeddings(_ok, shuffle=True)
    c = _make_client(emb)
    out = c.embed(["a", "b", "c"])
    assert out == [_vec(config.EMBEDDING_DIM, i) for i in range(3)]


def test_embed_dim_mismatch_rejected():
    emb = _StubEmbeddings(_ok, dim=config.EMBEDDING_DIM + 1)
    c = _make_client(emb)
    with pytest.raises(llm_client.LLMFailure, match="embed_dim_mismatch"):
        c.embed(["a"])


def test_embed_count_mismatch_rejected():
    class _Short(_StubEmbeddings):
        def create(self, **kw):
            self.calls.append(kw)
            return SimpleNamespace(data=[SimpleNamespace(
                index=0, embedding=_vec(config.EMBEDDING_DIM, 0))])

    c = _make_client(_Short(_ok))
    with pytest.raises(llm_client.LLMFailure, match="embed_count_mismatch"):
        c.embed(["a", "b"])


def test_embed_duplicate_index_rejected():
    class _Dup(_StubEmbeddings):
        def create(self, **kw):
            self.calls.append(kw)
            v = _vec(config.EMBEDDING_DIM, 0)
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=v),
                                         SimpleNamespace(index=0, embedding=v)])

    c = _make_client(_Dup(_ok))
    with pytest.raises(llm_client.LLMFailure, match="embed_index_duplicate"):
        c.embed(["a", "b"])


def test_embed_retries_retryable_then_succeeds(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def behavior(n, kw):
        calls["n"] += 1
        return _FakeStatusError(503) if calls["n"] == 1 else None

    emb = _StubEmbeddings(behavior)
    c = _make_client(emb)
    assert len(c.embed(["a"])) == 1
    assert calls["n"] == config.LLM_ATTEMPTS


def test_embed_does_not_retry_non_retryable(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    emb = _StubEmbeddings(lambda n, kw: _FakeStatusError(400))
    c = _make_client(emb)
    with pytest.raises(llm_client.LLMFailure, match="embed_error"):
        c.embed(["a"])
    assert len(emb.calls) == 1


def test_embed_deadline_exceeded():
    emb = _StubEmbeddings(_ok)
    c = _make_client(emb)
    with pytest.raises(llm_client.LLMFailure, match="deadline_exceeded"):
        c.embed(["a"], deadline=time.monotonic() - 1)
    assert emb.calls == []


# --- C3：两条链路熔断隔离 ----------------------------------------------------


def test_embed_breaker_open_does_not_block_chat(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    emb = _StubEmbeddings(lambda n, kw: _FakeStatusError(500))
    c = _make_client(emb)
    for _ in range(5):   # 累计失败达 fail_threshold=5 才熔断
        with pytest.raises(llm_client.LLMFailure):
            c.embed(["a"])
    assert c.embed_breaker.state == "open"
    assert c.breaker.state == "closed"
    assert c.chat([{"role": "user", "content": "hi"}]) == "chat-ok"


def test_chat_breaker_open_does_not_block_embed(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    emb = _StubEmbeddings(_ok)
    c = _make_client(emb, chat_behavior=lambda n, kw: _FakeStatusError(500))
    for _ in range(5):   # 累计失败达 fail_threshold=5 才熔断
        with pytest.raises(llm_client.LLMFailure):
            c.chat([{"role": "user", "content": "hi"}])
    assert c.breaker.state == "open"
    assert c.embed_breaker.state == "closed"
    assert len(c.embed(["a"])) == 1


def test_embed_breaker_is_distinct_instance():
    c = llm_client.LLMClient(api_key="")
    assert c.embed_breaker is not c.breaker