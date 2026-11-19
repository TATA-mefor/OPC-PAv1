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
def isolated_env(tmp_path, monkeypatch):
    """每个测试独立数据目录/库/vault + mock LLM + 禁网。"""
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

    # socket 层禁外网：回环（127.0.0.1/::1/localhost）放行——asyncio/Gradio
    # 内部 socketpair 依赖它；任何非回环连接直接失败（@live 用例自行解除）。
    _orig_connect = socket.socket.connect
    _orig_create = socket.create_connection
    _LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

    def _addr_of(address):
        if isinstance(address, tuple) and address:
            return str(address[0])
        return str(address)

    def _guard_connect(self, address):
        if _addr_of(address) not in _LOOPBACK:
            raise RuntimeError(f"测试禁止真实网络调用（目标 {address!r}，@live 除外）")
        return _orig_connect(self, address)

    def _guard_create(address, *args, **kwargs):
        if _addr_of(address) not in _LOOPBACK:
            raise RuntimeError(f"测试禁止真实网络调用（目标 {address!r}，@live 除外）")
        return _orig_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
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
