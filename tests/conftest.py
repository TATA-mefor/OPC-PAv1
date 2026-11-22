"""测试隔离（PRD §12.4/§13.1，先于一切测试就位）。

autouse fixture 保证：
- 独立 tmp_path 测试库与临时 vault，绝不触碰开发库/真实 vault；
- LLM 全 mock（FakeLLM 注入），socket 层禁真实网络（@live 除外）；
- 线程本地连接与全局缺省实例用后清理。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# M5-a B2：角色快照在 config 导入时从部署配置固化，测试必须在导入前注入。
# 这里是测试专用的部署配置替身（生产不设置该变量 → 空快照）。
if "OPC_AGENT_ROLES" not in os.environ:
    os.environ["OPC_AGENT_ROLES"] = json.dumps(
        {f"test-agent-{role}": role for role in (
            "customer_service", "sales", "cfo", "legal", "product",
            "research", "marketing", "hr", "personal", "coordinator")})

from opc_workbench import app, config, db, llm_client, cas, visibility  # noqa: E402


class FakeLLM:
    """确定性替身：记录调用、可编排响应/异常，零网络。"""

    def __init__(self, responses=None, embed_responses=None, embed_dim=8):
        self.responses = list(responses or ["fake-ok"])
        self.embed_responses = list(embed_responses or [])
        self.embed_dim = embed_dim
        self.calls: list[dict] = []
        self.available = True
        self.breaker = llm_client.CircuitBreaker()
        self.embed_breaker = llm_client.CircuitBreaker()   # C3：与 chat 熔断隔离

    def chat(self, messages, *, max_tokens=1500, deadline=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        item = self.responses.pop(0) if self.responses else "fake-ok"
        if isinstance(item, Exception):
            raise item
        return item

    def embed(self, texts, *, deadline=None):
        """M4 第一批测试替身：确定性、已归一化、零网络；可编排异常/异常向量。

        生产 embed() 属第二批，本替身仅用于纯函数与分块/检索地基测试。
        """
        self.calls.append({"kind": "embed", "texts": list(texts)})
        if self.embed_responses:
            item = self.embed_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(self.embed_dim)]
        norm = math.sqrt(sum(v * v for v in raw))
        if norm == 0.0:                       # 零向量兜底为单位向量，绝不返回全零
            raw = [1.0] + [0.0] * (self.embed_dim - 1)
            norm = 1.0
        return [v / norm for v in raw]


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
    # M5-a B2：角色快照已在导入期固化（见文件头部）；生产默认空快照。

    fake = FakeLLM()
    llm_client.set_llm(fake)

    # 审计日志器重置：确保每个测试的日志落在各自 tmp 目录（M2）
    from opc_workbench import audit_log
    monkeypatch.setattr(audit_log, "_logger", None, raising=False)
    monkeypatch.setattr(audit_log, "_logger_dir", None, raising=False)

    # M3.2①：app 侧已无模块级应用槽/名额，无需重置；
    # 每个测试自行 build_application() 或持有 ApplicationLauncher 实例。

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
    # 测试卫生：用例中途失败时防止 owner 会话名额跨用例泄漏
    # （正常路径由持有名额的 Application.close() 释放）。
    from opc_workbench import _owner_session
    _owner_session.release()


@pytest.fixture()
def db_conn(isolated_env):
    """已迁移的测试库**可写**连接（测试播种专用）。

    get_conn 已只读化（用户裁决 2026-11-21），测试播种不再经兼容符号；
    本夹具直接打开 tmp 测试库连接（测试工具代码，非生产模块路径）。
    """
    import sqlite3
    db.init_db()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# M3 身份链测试组合根（M3.1 清单 v10.1：fixture 与 app.main 同构装配）
# ---------------------------------------------------------------------------

class TestAuthBackend:
    """受控认证后端替身（测试专用，不进生产模块）。

    - authenticate：可编排返回 None（模拟认证失败）或签发 AuthEvent；
    - validate_event：只认本后端登记签发的事件（伪造事件拒绝）；
    - confirm_action：按会话注入受控确认处理器；未注入 → None（无确认）。
    """

    def __init__(self):
        self._events: dict = {}
        self._confirmations: dict = {}
        self.authenticate_enabled = True

    def authenticate(self, session_id: str):
        from opc_workbench import identity
        if not self.authenticate_enabled:
            return None
        ev = object.__new__(identity.AuthEvent)
        import uuid
        object.__setattr__(ev, "_event_id", uuid.uuid4().hex)
        object.__setattr__(ev, "_session_id", session_id)
        self._events[ev._event_id] = session_id
        return ev

    def validate_event(self, ev) -> bool:
        from opc_workbench import identity
        return (isinstance(ev, identity.AuthEvent)
                and self._events.get(ev._event_id) == ev._session_id)

    def confirm_action(self, session_id: str, action_spec: dict):
        handler = self._confirmations.get(session_id)
        if handler is None:
            return None
        return handler(action_spec)

    def allow_confirm(self, session_id: str, handler=None):
        """注入受控确认：默认 handler 校验 request 标记后返回确认结果。"""
        if handler is None:
            def handler(spec):
                req = spec.get("request")
                if getattr(req, "is_ui_confirmation", False) is not True:
                    return None
                if getattr(req, "session_id", None) != session_id:
                    return None
                return {"confirmed": True}
        self._confirmations[session_id] = handler


class UIConfirmationRequest:
    """模拟真实 UI 确认回调请求对象（M6 由 Gradio 事件上下文构造）。"""

    def __init__(self, session_id: str):
        self.is_ui_confirmation = True
        self.session_id = session_id


@pytest.fixture()
def ui_confirmation_request_cls():
    """注入请求对象类（避免 from tests.conftest 与 site-packages 同名包冲突）。"""
    return UIConfirmationRequest


@pytest.fixture()
def auth_backend():
    return TestAuthBackend()


@pytest.fixture()
def identity_api(auth_backend):
    from opc_workbench import identity
    return identity._build_identity(auth_backend)


@pytest.fixture()
def disclosure(isolated_env):
    """M2 披露服务：写能力显式注入，隔离测试库（M3.2 修正版 A）。"""
    w = db._open_test_writer(config.DB_PATH)
    yield visibility.DisclosureService(w)
    w.close()


@pytest.fixture()
def boss_ctx(identity_api, auth_backend):
    """完整 boss 身份链：认证事件 → 登记 → proof → 铸造 ctx₀。"""
    ev = auth_backend.authenticate("sess-boss-1")
    identity_api.register_session(ev)
    proof = identity_api.issue_proof("sess-boss-1")
    return identity_api.mint_boss_context(proof)


def pytest_collection_modifyitems(config, items):
    """缺省跳过 @live；-m live 显式选择时才跑。"""
    if "live" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="live 用例需显式 -m live 且真实 Key")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# M4 第二批：知识链路夹具（tests 目录非包，共享工具一律以 fixture 暴露，
# 不用 from tests.xxx import，避免与 site-packages 同名包冲突）
# ---------------------------------------------------------------------------

@pytest.fixture()
def write_md():
    """在临时 vault 播种一篇合法 frontmatter 文档，返回正文 hash。"""
    from datetime import datetime, timezone

    from opc_workbench import vault
    from opc_workbench.knowledge_service import body_hash

    def _write(root, rel, body, *, visibility="public", owner_role=None):
        owner = owner_role or vault.owner_for_relpath(rel)
        meta = {
            "owner_role": owner, "visibility": visibility, "scope": "global",
            "file_hash": body_hash(body),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(vault.serialize_frontmatter(meta, body), encoding="utf-8")
        return meta["file_hash"]

    return _write


@pytest.fixture()
def kdim(monkeypatch):
    """测试用 8 维模型：与 FakeLLM.embed_dim 对齐，避免维度混算。"""
    monkeypatch.setattr(config, "EMBEDDING_DIM", 8)
    return 8


@pytest.fixture()
def kwriter(isolated_env):
    w = db._open_test_writer(config.DB_PATH)
    yield w
    w.close()


@pytest.fixture()
def kservice(kwriter, isolated_env, kdim, identity_api):
    from opc_workbench.knowledge_service import KnowledgeService
    return KnowledgeService(kwriter, llm=isolated_env["fake_llm"],
                            vault_root=isolated_env["vault_dir"],
                            identity_api=identity_api)


@pytest.fixture()
def kboss_ident(identity_api, boss_ctx):
    """框架签发的 boss Identity 实例（删除服务的唯一可接受身份形态）。"""
    return identity_api.resolve_identity("boss", {}, boss_ctx)


@pytest.fixture()
def ksales_ident(identity_api):
    return identity_api.resolve_identity("sales", {})


@pytest.fixture()
def gated_embedder_cls():
    """并发编排替身类：进入 embed 置位，放行后才返回（不用 sleep 猜时序）。"""
    import threading

    class GatedEmbedder:
        def __init__(self, inner):
            self.inner = inner
            self.entered = threading.Event()
            self.release = threading.Event()

        def embed(self, texts, *, deadline=None):
            self.entered.set()
            if not self.release.wait(10):
                raise RuntimeError("gate_timeout")
            return self.inner.embed(texts, deadline=deadline)

    return GatedEmbedder


def _run_module_entry():
    """以标准库 runpy 真实执行可信入口（等价 `python -m opc_workbench.app`）。

    runpy 就是 `-m` 的实现：以真实模块 spec + 临时 `__main__` 命名空间
    **重新执行入口模块源码**，不能注入适配器、不能跳过入口代码；与
    exec 伪造调用帧（只改 `__name__`，不执行入口模块）有本质区别。
    返回 (application, adapter)，adapter 由入口流程内部创建。
    """
    import runpy
    import warnings
    with warnings.catch_warnings():
        # 模块已被正常导入过时的预期提示（双实例正是入口语义）
        warnings.simplefilter("ignore", RuntimeWarning)
        ns = runpy.run_module("opc_workbench.app", run_name="__main__",
                              alter_sys=True)
    return ns["_entry_application"], ns["_entry_adapter"]


@pytest.fixture()
def module_entry():
    """真实入口执行器：返回 (application, adapter)。"""
    return _run_module_entry


@pytest.fixture()
def owner_entry(isolated_env):
    """可信入口工厂：(application, submit_port) 成对返回，测试结束自动关停。"""
    opened = []

    def run():
        application, adapter = _run_module_entry()
        opened.append(application)
        return application, adapter.send

    yield run
    for application in opened:
        application.close()

