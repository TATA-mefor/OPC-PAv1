"""组合根装配专项（M3.1 清单 v10.1 验收表十项 + 架构断言）。

验收口径：普通模块即便导入 identity/cas、调内部构造函数、自建认证后端
与 CAS 服务，也不能替换/占用/影响应用组合根实际持有的实例；
且自建服务即便配伪造 authorize 也无法写生产库（端到端见 test_cas.py）。
"""
from __future__ import annotations

import pytest

from opc_workbench import app, cas, config, db, identity


@pytest.fixture()
def application(isolated_env):
    a = app.build_application()
    yield a
    a.close()


def test_10_normal_startup_full_chain(application, db_conn):
    """⑩ 正向端到端：组合根启动 → Agent 身份解析 → CAS 写入正常。"""
    with db_conn:
        cur = db_conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('启动验证','sales',?,?)", (db.now_iso(), db.now_iso()))
    ident = application.identity_api.resolve_identity("sales", {})
    row = application.cas_service.cas_update(
        "tasks", cur.lastrowid, 1, ident=ident, title="组合根写入")
    assert row["version"] == 2


def test_1_early_build_identity_parallel_only(application, auth_backend):
    """① 普通模块提前/另行调 _build_identity → 只得独立对象，应用不受影响。"""
    parallel = identity._build_identity(auth_backend)
    assert parallel is not application.identity_api
    assert parallel.owner_token is not application.identity_api.owner_token


def test_2_self_built_service_cannot_replace_application(application, auth_backend):
    """② 自建 CASService → 应用持有的实例不变（同一性断言）。"""
    before = application.cas_service
    other_api = identity._build_identity(auth_backend)
    w = db._open_test_writer(config.DATA_DIR / "parallel.db")
    cas.CASService(authorize=other_api.make_authorize(), writer=w)
    assert application.cas_service is before
    w.close()


def test_3_no_production_bind_interface():
    """③ 不存在可调用的生产绑定接口（cas 无 bind 属性断言）。"""
    assert not hasattr(cas, "bind")
    assert not hasattr(cas, "rebind")
    assert not hasattr(db, "bind_writer")


def test_4_forged_authorize_cannot_enter_application_service(application):
    """④ 注入伪造 authorize → 进不了应用 CASService（无 setter/rebind）。"""
    with pytest.raises((AttributeError, PermissionError)):
        application.cas_service._authorize = lambda ident: "boss"
    with pytest.raises(PermissionError):
        application.cas_service._writer = None


def test_5_parallel_identity_rejected_by_application(application, auth_backend, db_conn):
    """⑤ 自建 authority 签发 Identity → 应用 CASService 拒绝。"""
    with db_conn:
        cur = db_conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('x','sales',?,?)", (db.now_iso(), db.now_iso()))
    other_api = identity._build_identity(auth_backend)
    ident = other_api.resolve_identity("sales", {})
    with pytest.raises(PermissionError):
        application.cas_service.cas_update("tasks", cur.lastrowid, 1,
                                           ident=ident, title="平行链")


def test_6_repeated_build_does_not_replace(isolated_env):
    """⑥ 启动器内重复装配幂等：同一实例，不重复铸造、不替换既有服务。
    幂等状态在启动器**实例**上，不在模块全局。"""
    launcher = app.ApplicationLauncher()
    application = launcher.build()
    try:
        before = application.cas_service
        assert launcher.build() is application
        assert application.cas_service is before
    finally:
        launcher.close()


def test_7_cross_instance_mix_rejected(application, auth_backend, db_conn):
    """⑦ CASService 与 IdentityAPI 跨实例混用 → 拒绝。"""
    with db_conn:
        cur = db_conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('x','sales',?,?)", (db.now_iso(), db.now_iso()))
    other_api = identity._build_identity(auth_backend)
    ident = other_api.resolve_identity("sales", {})
    with pytest.raises(PermissionError):
        application.cas_service.cas_update("tasks", cur.lastrowid, 1,
                                           ident=ident, title="混用")


def test_8_agent_identity_forgery_expired_revoked(application, db_conn, monkeypatch):
    """⑧ Agent 身份伪造/过期/撤销 → 拒绝。"""
    with db_conn:
        cur = db_conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('x','sales',?,?)", (db.now_iso(), db.now_iso()))
    rid = cur.lastrowid
    fake = object.__new__(identity.Identity)
    object.__setattr__(fake, "_ident_id", "nope")
    with pytest.raises(PermissionError):
        application.cas_service.cas_update("tasks", rid, 1, ident=fake, title="x")
    t0 = 5_000_000.0
    monkeypatch.setattr(identity, "_now", lambda: t0)
    ident = application.identity_api.resolve_identity("sales", {})
    monkeypatch.setattr(identity, "_now", lambda: t0 + identity.IDENTITY_TTL_S + 1)
    with pytest.raises(PermissionError):
        application.cas_service.cas_update("tasks", rid, 1, ident=ident, title="x")
    monkeypatch.setattr(identity, "_now", lambda: t0)
    ident2 = application.identity_api.resolve_identity("sales", {})
    application.identity_api.revoke_identity(ident2)
    with pytest.raises(PermissionError):
        application.cas_service.cas_update("tasks", rid, 1, ident=ident2, title="x")


def test_9_boss_production_channel_unreachable(application):
    """⑨ M3 生产无 UI：authenticate 恒 None → boss 会话登记不了 →
    boss 身份生产不可达（端到端断言）。"""
    backend = app.ProductionAuthBackend()
    assert backend.authenticate("any") is None
    assert backend.confirm_action("any", {}) is None
    with pytest.raises(PermissionError):
        application.identity_api.register_session(None)
    with pytest.raises(PermissionError):
        application.identity_api.resolve_identity("boss", {})


def test_architecture_no_module_level_mutable_service():
    """架构断言（回归检查，非安全边界）：无模块级可变服务变量。"""
    for mod in (app, cas, identity):
        for name in vars(mod):
            if name.startswith("__"):
                continue
            val = getattr(mod, name)
            assert not isinstance(val, (cas.CASService, identity.IdentityAPI)), (
                mod.__name__, name)