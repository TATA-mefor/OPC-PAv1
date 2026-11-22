"""LLM 客户端骨架（M1）：DashScope OpenAI 兼容端点 + 超时/重试/熔断/降级。

设计移植自旧 llm_client 的防失控语义（审计确认为高价值复用件），重写为无全局单例：
- LLMClient 实例化使用；get_llm() 提供进程级缺省实例（测试经 fixture 替换）。
- 无 Key → LLMFailure（上层走 L1 降级，UI 照常启动）。
- 熔断：连续 5 次失败或 30s 窗口失败率>50% → OPEN 30s → HALF-OPEN 试探（PRD §15.4）。
- Function Calling 的 tools 参数透传接口在 M5 扩展；M1 仅纯 chat。
- 本模块不打印对话原文与 Key（日志红线，PRD §10.4）。
"""
from __future__ import annotations

import threading
import time

from . import config


class LLMFailure(RuntimeError):
    """LLM 不可用（无 Key/熔断/重试耗尽）。上层据此降级，不得让异常击穿 UI。"""


class CircuitBreaker:
    """CLOSED → OPEN → HALF-OPEN 状态机（线程安全）。"""

    def __init__(self, fail_threshold: int = 5, window_s: float = 30.0,
                 open_s: float = 30.0):
        self._lock = threading.Lock()
        self.fail_threshold = fail_threshold
        self.window_s = window_s
        self.open_s = open_s
        self.state = "closed"
        self._failures: list[float] = []
        self._opened_at = 0.0
        self._probe_inflight = False  # 严格单探针（M1 审查修复）

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self.state == "open":
                if now - self._opened_at >= self.open_s:
                    self.state = "half_open"
                    self._probe_inflight = True
                    return True
                return False
            if self.state == "half_open":
                # 半开态只放行一个探针，其余并发请求直接拒绝
                if self._probe_inflight:
                    return False
                self._probe_inflight = True
                return True
            return True

    def record(self, ok: bool) -> None:
        with self._lock:
            now = time.monotonic()
            if ok:
                self._failures.clear()
                self._probe_inflight = False
                self.state = "closed"
                return
            if self.state == "half_open":
                self._probe_inflight = False
                self._trip(now)
                return
            self._failures = [t for t in self._failures if now - t <= self.window_s]
            self._failures.append(now)
            if len(self._failures) >= self.fail_threshold:
                self._trip(now)

    def _trip(self, now: float) -> None:
        self.state = "open"
        self._opened_at = now


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        self.api_key = api_key if api_key is not None else config.DASHSCOPE_API_KEY
        self.base_url = base_url or config.DASHSCOPE_BASE_URL
        self.model = model or config.DASHSCOPE_MODEL
        self.breaker = CircuitBreaker()
        # C3（用户裁决 2026-11-22）：embed 独立熔断器。批量索引失败不得把
        # 日常对话一起熔断；chat 仍只用 self.breaker，行为零改动。
        self.embed_breaker = CircuitBreaker()
        self._client = None
        if self.api_key:
            from openai import OpenAI  # 延迟导入：无 Key/测试环境不触碰 SDK
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                  timeout=config.LLM_TIMEOUT_S, max_retries=0)

    @property
    def available(self) -> bool:
        return self._client is not None

    def chat(self, messages: list[dict], *, max_tokens: int = 1500,
             deadline: float | None = None) -> str:
        """纯文本对话。deadline 为 monotonic 截止时刻；超时/熔断抛 LLMFailure。"""
        if not self.available:
            raise LLMFailure("no_api_key")
        if not self.breaker.allow():
            raise LLMFailure("circuit_open")
        last_err: Exception | None = None
        for attempt in range(config.LLM_ATTEMPTS):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise LLMFailure("deadline_exceeded")
            timeout = config.LLM_TIMEOUT_S if remaining is None else min(
                config.LLM_TIMEOUT_S, remaining)
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, messages=messages,
                    max_tokens=max_tokens, timeout=timeout,
                )
                self.breaker.record(True)
                return resp.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001 — 分类后决定是否重试
                last_err = exc
                status = getattr(exc, "status_code", None)
                retryable = status in config.RETRYABLE_STATUS
                self.breaker.record(False)
                if not retryable or attempt + 1 >= config.LLM_ATTEMPTS:
                    break
                backoff = 0.8 * (attempt + 1)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= backoff:
                        # 退避会越过 deadline：不再等待/重试（M1 审查修复）
                        raise LLMFailure("deadline_exceeded") from exc
                time.sleep(backoff)
        raise LLMFailure(f"llm_error:{type(last_err).__name__}") from last_err

    def embed(self, texts: list[str], *, deadline: float | None = None) -> list[list[float]]:
        """文本向量化（M4 第二批）。返回顺序与输入严格对齐。

        纪律与 chat 同款：无 Key → LLMFailure；独立 embed_breaker 熔断；
        仅对 RETRYABLE_STATUS 重试，退避 0.8*(n+1)，越过 deadline 立即停。
        分批上限 10 条/请求（text-embedding-v4 规格），维度固定 config.EMBEDDING_DIM。
        **不信任服务端返回顺序**：按 item.index 复位；缺项/多项/维度不符一律失败。
        本方法不打印文本原文与 Key（日志红线）。
        """
        if not isinstance(texts, list) or any(not isinstance(t, str) for t in texts):
            raise LLMFailure("embed_invalid_input")
        if not texts:
            return []
        if not self.available:
            raise LLMFailure("no_api_key")
        out: list[list[float]] = []
        size = config.EMBED_BATCH_SIZE
        for start in range(0, len(texts), size):
            batch = texts[start:start + size]
            out.extend(self._embed_batch(batch, deadline=deadline))
        return out

    def _embed_batch(self, batch: list[str], *, deadline: float | None) -> list[list[float]]:
        if not self.embed_breaker.allow():
            raise LLMFailure("circuit_open")
        last_err: Exception | None = None
        for attempt in range(config.LLM_ATTEMPTS):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise LLMFailure("deadline_exceeded")
            timeout = config.LLM_TIMEOUT_S if remaining is None else min(
                config.LLM_TIMEOUT_S, remaining)
            try:
                resp = self._client.embeddings.create(
                    model=config.EMBEDDING_MODEL, input=batch,
                    dimensions=config.EMBEDDING_DIM, timeout=timeout,
                )
                vectors = self._order_vectors(resp, len(batch))
                self.embed_breaker.record(True)
                return vectors
            except LLMFailure:
                self.embed_breaker.record(False)
                raise
            except Exception as exc:  # noqa: BLE001 — 分类后决定是否重试
                last_err = exc
                status = getattr(exc, "status_code", None)
                retryable = status in config.RETRYABLE_STATUS
                self.embed_breaker.record(False)
                if not retryable or attempt + 1 >= config.LLM_ATTEMPTS:
                    break
                backoff = 0.8 * (attempt + 1)
                if deadline is not None and deadline - time.monotonic() <= backoff:
                    raise LLMFailure("deadline_exceeded") from exc
                time.sleep(backoff)
        raise LLMFailure(f"embed_error:{type(last_err).__name__}") from last_err

    @staticmethod
    def _order_vectors(resp, expected: int) -> list[list[float]]:
        """按 index 复位并校验完整性/维度；任何缺口都不得静默补位。"""
        items = list(resp.data)
        if len(items) != expected:
            raise LLMFailure("embed_count_mismatch")
        ordered: list[list[float] | None] = [None] * expected
        for item in items:
            idx = item.index
            if not isinstance(idx, int) or not 0 <= idx < expected:
                raise LLMFailure("embed_index_invalid")
            if ordered[idx] is not None:
                raise LLMFailure("embed_index_duplicate")
            vec = list(item.embedding)
            if len(vec) != config.EMBEDDING_DIM:
                raise LLMFailure("embed_dim_mismatch")
            ordered[idx] = vec
        if any(v is None for v in ordered):
            raise LLMFailure("embed_index_missing")
        return [v for v in ordered if v is not None]


_default: LLMClient | None = None
_default_lock = threading.Lock()


def get_llm() -> LLMClient:
    global _default
    with _default_lock:
        if _default is None:
            _default = LLMClient()
        return _default


def set_llm(client: LLMClient | None) -> None:
    """测试/降级注入点。"""
    global _default
    with _default_lock:
        _default = client
