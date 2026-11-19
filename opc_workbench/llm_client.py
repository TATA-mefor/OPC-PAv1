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

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self.state == "open":
                if now - self._opened_at >= self.open_s:
                    self.state = "half_open"
                    return True
                return False
            return True

    def record(self, ok: bool) -> None:
        with self._lock:
            now = time.monotonic()
            if ok:
                self._failures.clear()
                self.state = "closed"
                return
            if self.state == "half_open":
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
                time.sleep(0.8 * (attempt + 1))
        raise LLMFailure(f"llm_error:{type(last_err).__name__}") from last_err


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
