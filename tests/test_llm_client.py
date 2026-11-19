"""M1 审查补强：熔断器并发单探针 + deadline 临界 + 网络隔离边界。"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from opc_workbench import config, llm_client


class _FakeStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class _StubCompletions:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        result = self.behavior(self.calls, kwargs)
        if isinstance(result, str):
            # 模拟 OpenAI SDK 响应结构：resp.choices[0].message.content
            from types import SimpleNamespace
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=result))])
        return result


class _StubClient:
    """替身 SDK 客户端：零网络，可编程行为。"""

    def __init__(self, behavior):
        self.chat = type("Chat", (), {"completions": _StubCompletions(behavior)})()


def _make_client(behavior) -> llm_client.LLMClient:
    c = llm_client.LLMClient(api_key="")  # 不构造真 SDK
    c._client = _StubClient(behavior)
    return c


# ---------------------------------------------------------------------------
# 熔断器：严格单探针
# ---------------------------------------------------------------------------

def test_half_open_single_probe_under_concurrency():
    """M1 审查修复：HALF-OPEN 只放行一个探针，其余并发请求拒绝。"""
    cb = llm_client.CircuitBreaker(fail_threshold=2, window_s=30, open_s=0.05)
    cb.record(False)
    cb.record(False)
    assert cb.state == "open"
    assert cb.allow() is False
    time.sleep(0.06)

    results: list[bool] = []
    barrier = threading.Barrier(8)

    def racer():
        barrier.wait()
        results.append(cb.allow())

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sum(results) == 1, f"应恰好 1 个探针，实际 {sum(results)}"
    assert cb.state == "half_open"
    # 探针未归位前继续拒绝
    assert cb.allow() is False


def test_half_open_probe_success_closes_and_failure_reopens():
    cb = llm_client.CircuitBreaker(fail_threshold=1, open_s=0.01)
    cb.record(False)
    time.sleep(0.02)
    assert cb.allow() is True          # 探针放行
    cb.record(True)
    assert cb.state == "closed"
    assert cb.allow() is True          # 恢复后正常放行

    cb.record(False)                   # 再次熔断
    time.sleep(0.02)
    assert cb.allow() is True
    cb.record(False)                   # 探针失败 → 重新 OPEN
    assert cb.state == "open"
    assert cb.allow() is False


def test_breaker_open_blocks_chat(monkeypatch):
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    c = _make_client(lambda n, kw: (_ for _ in ()).throw(_FakeStatusError(500)))
    for _ in range(config.LLM_ATTEMPTS):
        with pytest.raises(llm_client.LLMFailure):
            c.chat([{"role": "user", "content": "x"}])
    # 5 次失败阈值：两轮×2 次=4 次，再补一次触发
    c.breaker.record(False)
    assert c.breaker.state == "open"
    with pytest.raises(llm_client.LLMFailure, match="circuit_open"):
        c.chat([{"role": "user", "content": "x"}])
    assert c._client.chat.completions.calls == 4  # 熔断后未再发请求


# ---------------------------------------------------------------------------
# deadline 临界
# ---------------------------------------------------------------------------

def test_deadline_already_passed_no_request():
    c = _make_client(lambda n, kw: "ok")
    with pytest.raises(llm_client.LLMFailure, match="deadline_exceeded"):
        c.chat([{"role": "user", "content": "x"}],
               deadline=time.monotonic() - 1)
    assert c._client.chat.completions.calls == 0


def test_retry_backoff_must_not_exceed_deadline(monkeypatch):
    """M1 审查修复：退避 0.8s 会越过剩余 0.2s 的 deadline → 立即失败，不 sleep 不重试。"""
    slept: list[float] = []
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: slept.append(s))

    c = _make_client(lambda n, kw: (_ for _ in ()).throw(_FakeStatusError(429)))
    start = time.monotonic()
    with pytest.raises(llm_client.LLMFailure, match="deadline_exceeded"):
        c.chat([{"role": "user", "content": "x"}], deadline=start + 0.2)
    assert c._client.chat.completions.calls == 1  # 未发起第二次
    assert slept == []                            # 未等待退避
    assert time.monotonic() - start < 0.5         # 未越界耗时


def test_retry_within_deadline_still_works(monkeypatch):
    """deadline 充裕时重试机制保持原语义。"""
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    c = _make_client(lambda n, kw: "第二次成功" if n == 2
                     else (_ for _ in ()).throw(_FakeStatusError(429)))
    out = c.chat([{"role": "user", "content": "x"}],
                 deadline=time.monotonic() + 60)
    assert out == "第二次成功"
    assert c._client.chat.completions.calls == 2


def test_non_retryable_fails_fast(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: slept.append(s))
    c = _make_client(lambda n, kw: (_ for _ in ()).throw(_FakeStatusError(401)))
    with pytest.raises(llm_client.LLMFailure):
        c.chat([{"role": "user", "content": "x"}])
    assert c._client.chat.completions.calls == 1
    assert slept == []


# ---------------------------------------------------------------------------
# 网络隔离边界（普通测试零外网）
# ---------------------------------------------------------------------------

def test_connect_ex_external_blocked():
    """M1 审查修复：connect_ex 同样被拦截。"""
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="禁止真实网络"):
            s.connect_ex(("8.8.8.8", 53))
    finally:
        s.close()


def test_connect_external_blocked():
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="禁止真实网络"):
            s.connect(("8.8.8.8", 53))
    finally:
        s.close()


def test_create_connection_external_blocked():
    with pytest.raises(RuntimeError, match="禁止真实网络"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_wildcard_address_not_whitelisted():
    """0.0.0.0 不是回环：连接它必须被拦截（M1 审查修复）。"""
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="禁止真实网络"):
            s.connect(("0.0.0.0", 9))
    finally:
        s.close()


@pytest.mark.live
def test_live_marker_lifts_network_guard():
    """@live 解除禁网的机制验证（零网络）：socket 方法应为原生实现，
    而非夹具的 _guard_* 包装函数。不向外网发任何包。"""
    assert socket.socket.connect.__name__ == "connect"
    assert socket.socket.connect_ex.__name__ == "connect_ex"
    assert socket.create_connection.__name__ == "create_connection"
