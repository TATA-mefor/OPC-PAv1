"""测试隔离（PRD §12.4/§13.1，先于一切测试就位）。

autouse fixture 保证：
- 独立 tmp_path 测试库与临时 vault，绝不触碰开发库/真实 vault；
- LLM 全 mock（FakeLLM 注入），socket 层禁真实网络（@live 除外）；
- 线程本地连接与全局缺省实例用后清理。
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opc_workbench import config, db, llm_client  # noqa: E402


class FakeLLM:
    """确定性替身：记录调用、可编排响应/异常，零网络。"""

    def __init__(self, responses=None):
        self.responses = list(responses or ["fake-ok"])
        self.calls: list[dict] = []
        self.available = True
        self.breaker = llm_client.CircuitBreaker()

    def chat(self, messages, *, max_tokens=1500, deadline=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        item = self.responses.pop(0) if self.responses else "fake-ok"
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def isolated_env(request, tmp_path, monkeypatch):
    """每个测试独立数据目录/库/vault + mock LLM + 禁外网。

    网络隔离（M1 审查修复）：
    - 拦截 connect / connect_ex / create_connection 三个入口；
    - 仅放行真回环 127.0.0.1 / ::1 / localhost（asyncio socketpair 依赖）；
      0.0.0.0 不是回环地址，不在白名单；
    - @pytest.mark.live 用例显式解除禁网（仍走 tmp 隔离与 mock 注入点，
      live 用例自行构造真实客户端）。
    """
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    data_dir.mkdir()
    vault_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "test.db")
    monkeypatch.setattr(config, "VAULT_PATH", vault_dir)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "")  # 默认无 Key，防真客户端

    fake = FakeLLM()
    llm_client.set_llm(fake)

    # 审计日志器重置：确保每个测试的日志落在各自 tmp 目录（M2）
    from opc_workbench import audit_log
    monkeypatch.setattr(audit_log, "_logger", None, raising=False)
    monkeypatch.setattr(audit_log, "_logger_dir", None, raising=False)

    # socket 层禁外网：仅真回环放行；@live 显式解除。
    is_live = "live" in request.keywords
    if not is_live:
        _orig_connect = socket.socket.connect
        _orig_connect_ex = socket.socket.connect_ex
        _orig_create = socket.create_connection
        _LOOPBACK = {"127.0.0.1", "::1", "localhost"}

        def _addr_of(address):
            if isinstance(address, tuple) and address:
                return str(address[0])
            return str(address)

        def _blocked(address):
            return _addr_of(address) not in _LOOPBACK

        def _guard_connect(self, address):
            if _blocked(address):
                raise RuntimeError(f"测试禁止真实网络调用（目标 {address!r}）")
            return _orig_connect(self, address)

        def _guard_connect_ex(self, address):
            if _blocked(address):
                raise RuntimeError(f"测试禁止真实网络调用（connect_ex 目标 {address!r}）")
            return _orig_connect_ex(self, address)

        def _guard_create(address, *args, **kwargs):
            if _blocked(address):
                raise RuntimeError(f"测试禁止真实网络调用（目标 {address!r}）")
            return _orig_create(address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", _guard_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", _guard_connect_ex)
        monkeypatch.setattr(socket, "create_connection", _guard_create)

    yield {"data_dir": data_dir, "vault_dir": vault_dir, "fake_llm": fake}

    db.close_thread_conns()
    llm_client.set_llm(None)


@pytest.fixture()
def db_conn(isolated_env):
    """已迁移的测试库连接。"""
    db.init_db()
    return db.get_conn()


def pytest_collection_modifyitems(config, items):
    """缺省跳过 @live；-m live 显式选择时才跑。"""
    if "live" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="live 用例需显式 -m live 且真实 Key")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
