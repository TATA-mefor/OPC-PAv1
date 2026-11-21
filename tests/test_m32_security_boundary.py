"""M3.2 安全边界负向矩阵（用户 2026-11-22 裁决必测项）。

覆盖两条已实证的越权路径：
- B1：非 production writer（原 internal:m2_disclosure）跳过配对校验 +
      伪造 boss lambda → 改写 cfo 私有行。修复：取消全部豁免 + 删除内部 writer 入口。
- B2：`forged._pa_owner = api.owner_token` 贴属性伪造归属标记 + **生产 writer**
      → 构造成功并以 boss 身份改写 cfo 私有行。修复：信任凭据改为 authority
      签发登记表（identity._verify_authorize），而非对象形状/属性。

以及：写能力获取边界、DisclosureService fail closed、Report 三条契约。
所有断言均为端到端行为断言（行内容 / version / 审计），不是对象同一性断言。
"""
from __future__ import annotations

import json

import pytest

from opc_workbench import app, cas, config, db, identity, visibility


# ---------------------------------------------------------------------------
# 探针工具
# ---------------------------------------------------------------------------

def _seed_cfo_private(conn, title="cfo 私密成本底线"):
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,detail,owner_role,visibility,created_at,updated_at) "
            "VALUES(?,?,'cfo','private',?,?)",
            (title, "私密明细-XJ9-不得外泄", db.now_iso(), db.now_iso()))
    return cur.lastrowid


def _seed_sales_public(conn, title="公开任务"):
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
            "VALUES(?,'sales','public',?,?)", (title, db.now_iso(), db.now_iso()))
    return cur.lastrowid


def _task(conn, rid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (rid,)).fetchone())


def _audit_records():
    p = config.LOG_DIR / "audit.log"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _boss_ok_audits(rid):
    """本轮最重要的负向证据：不得留下 boss 成功审计。"""
    return [r for r in _audit_records()
            if r.get("actor_role") == "boss" and r.get("result_code") == "ok"
            and r.get("target_id") == rid]


def _forged_authorize_instance(real_az, *, api=None, authority=None):
    """绕过构造门禁伪造 Authorize 实例（未登记）——验证登记表才是信任凭据。"""
    fake = object.__new__(identity.Authorize)
    object.__setattr__(fake, "_api", api if api is not None
                       else object.__getattribute__(real_az, "_api"))
    object.__setattr__(fake, "_authority", authority if authority is not None
                       else object.__getattribute__(real_az, "_authority"))
    return fake


# ---------------------------------------------------------------------------
# 一、CAS writer-authorize 配对矩阵（用户给定 8 行）
# ---------------------------------------------------------------------------

def test_matrix1_production_writer_real_identity_allowed(isolated_env, identity_api, db_conn):
    """① 生产 writer + 组合根提供的真实 Identity → 允许合法授权操作。"""
    rid = _seed_sales_public(db_conn)
    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)
    svc = cas.CASService(authorize=identity_api.make_authorize(), writer=w)
    ident = identity_api.resolve_identity("sales", {})
    row = svc.cas_update("tasks", rid, 1, ident=ident, title="合法更新")
    assert row["version"] == 2
    assert _task(db_conn, rid)["title"] == "合法更新"
    w.close()


def test_matrix2_bare_lambda_rejected(isolated_env, identity_api, db_conn):
    """② 非测试 writer + lambda ident: "boss" → 拒绝构造。"""
    rid = _seed_cfo_private(db_conn)
    before = _task(db_conn, rid)
    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)
    with pytest.raises(PermissionError, match="authorize_object_required"):
        cas.CASService(authorize=lambda ident: "boss", writer=w)
    assert _task(db_conn, rid) == before
    assert _boss_ok_audits(rid) == []
    w.close()


def test_matrix3_mismatched_authorize_rejected(isolated_env, identity_api,
                                               auth_backend, db_conn):
    """③ 非测试 writer + 不匹配的 authorize（平行链正规签发）→ 拒绝。"""
    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)
    other_api = identity._build_identity(auth_backend)
    with pytest.raises(PermissionError, match="writer_authorize_pairing_mismatch"):
        cas.CASService(authorize=other_api.make_authorize(), writer=w)
    w.close()


def test_matrix4_untrusted_source_authorize_rejected(isolated_env, identity_api, db_conn):
    """④ 非测试 writer + 形状正确但来源不可信的 authorize → 拒绝。

    伪造实例携带**真实** api 与 authority 引用（形状完全正确），
    但不在 authority._authorizes 签发登记表中 → 拒绝。
    这正是"__slots__ + isinstance 不足以构成不可伪造机制"的对策验证。
    """
    real_az = identity_api.make_authorize()
    fake = _forged_authorize_instance(real_az)
    assert isinstance(fake, identity.Authorize)          # 形状检查会通过
    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)
    with pytest.raises(PermissionError, match="authorize_not_registered"):
        cas.CASService(authorize=fake, writer=w)
    w.close()


def test_matrix4b_api_authority_cross_source_rejected(isolated_env, identity_api,
                                                      auth_backend):
    """④b api 与 authority 不同源的伪造实例 → 拒绝（登记前先查同源）。"""
    real_az = identity_api.make_authorize()
    other_api = identity._build_identity(auth_backend)
    fake = _forged_authorize_instance(
        real_az, authority=object.__getattribute__(other_api, "_authority"))
    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)
    with pytest.raises(PermissionError, match="authorize_api_mismatch"):
        cas.CASService(authorize=fake, writer=w)
    w.close()


def test_matrix5_test_writer_isolated_allowed(cas_service, identity_api, db_conn):
    """⑤ 测试 writer + 测试授权依赖 + 隔离测试库 → 允许测试。"""
    rid = _seed_sales_public(db_conn)
    ident = identity_api.resolve_identity("sales", {})
    row = cas_service.cas_update("tasks", rid, 1, ident=ident, title="测试写入")
    assert row["version"] == 2


def test_matrix6_test_writer_cannot_target_production_default(isolated_env):
    """⑥ 测试 writer 指向生产默认库 → 拒绝。"""
    db.init_db()
    with pytest.raises(PermissionError):
        db._open_test_writer(db._PRODUCTION_DB_DEFAULT)


def test_matrix7_no_module_level_production_writer_entry(isolated_env):
    """⑦ 普通业务模块直接获取生产 writer → 无可用入口。"""
    for name in ("_internal_writer", "_close_internal_writer", "_internal_writer_local"):
        assert not hasattr(db, name), f"db.{name} 仍存在，生产写获取旁路未封闭"
    # 唯一生产入口是一次性的：组合根取得后普通模块再取即失败
    db._open_production_writer()
    with pytest.raises(RuntimeError, match="production_writer_already_taken"):
        db._open_production_writer()


def test_matrix7b_composition_root_owns_disclosure(isolated_env):
    """⑦b 组合根显式持有 DisclosureService；visibility 无模块级可变服务槽。"""
    a = app.build_application()
    try:
        assert isinstance(a.disclosure, visibility.DisclosureService)
        for name in ("_service", "_disclosure_service", "_default_service", "bind"):
            assert not hasattr(visibility, name), f"visibility.{name} 构成全局绑定槽"
    finally:
        a.writer.close()


def test_matrix8_disclosure_full_chain_regression(disclosure, db_conn):
    """⑧ M2 三披露方法保持原状态机与回归行为（全链冒烟；详细回归见 test_visibility）。"""
    rid = _seed_cfo_private(db_conn)
    aid = disclosure.request_disclosure("tasks", rid, "cfo", "sales", "报价核算")
    assert isinstance(aid, int)
    disclosure.resolve_disclosure(aid, "approved", actor="boss")
    got = disclosure.consume_grant(aid, "sales")
    assert got["id"] == rid
    # 一次性：二次消费返回占位
    assert disclosure.consume_grant(aid, "sales") == visibility.PLACEHOLDER


# ---------------------------------------------------------------------------
# 二、真实攻击复现（本轮最重要的负向验收证据）
# ---------------------------------------------------------------------------

def test_attack_b2_forged_pa_owner_cannot_write_cfo_private(isolated_env, identity_api,
                                                            db_conn):
    """B2 复现：生产 writer + 贴 _pa_owner 的伪造 boss lambda → 必须拒绝。

    修复前实测：构造成功，cfo 私有行 version 1→2，审计记 boss/ok。
    """
    rid = _seed_cfo_private(db_conn)
    before = _task(db_conn, rid)

    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)

    forged = lambda ident: "boss"                      # noqa: E731
    forged._pa_owner = identity_api.owner_token        # 修复前凭此绕过配对校验

    with pytest.raises(PermissionError, match="authorize_object_required"):
        cas.CASService(authorize=forged, writer=w)

    after = _task(db_conn, rid)
    assert after["title"] == before["title"]
    assert after["detail"] == before["detail"]
    assert after["visibility"] == "private"
    assert after["version"] == before["version"]
    assert _boss_ok_audits(rid) == []
    w.close()


def test_attack_b2_forged_authorize_cannot_write_cfo_private(isolated_env, identity_api,
                                                             db_conn):
    """B2 升级变体：伪造 Authorize 实例（形状正确、未登记）+ 生产 writer。

    即便攻击者改用新机制的对象形状，仍因未登记被拒；行内容与 version 不变。
    """
    rid = _seed_cfo_private(db_conn)
    before = _task(db_conn, rid)
    fake = _forged_authorize_instance(identity_api.make_authorize())

    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)
    with pytest.raises(PermissionError, match="authorize_not_registered"):
        cas.CASService(authorize=fake, writer=w)

    assert _task(db_conn, rid) == before
    assert _boss_ok_audits(rid) == []
    w.close()


def test_attack_b1_internal_writer_entry_is_gone(isolated_env, identity_api, db_conn):
    """B1 复现：原攻击第一步 db._internal_writer() 现已不存在。"""
    rid = _seed_cfo_private(db_conn)
    before = _task(db_conn, rid)
    with pytest.raises(AttributeError):
        db._internal_writer()
    assert _task(db_conn, rid) == before
    assert _boss_ok_audits(rid) == []


def test_legit_service_cannot_cross_role_write_cfo_private(cas_service, identity_api,
                                                           db_conn):
    """正向对照的边界：即便服务合法配对，sales 身份也改不动 cfo 私有行。"""
    rid = _seed_cfo_private(db_conn)
    before = _task(db_conn, rid)
    ident = identity_api.resolve_identity("sales", {})
    with pytest.raises(cas.CASNotFound):
        cas_service.cas_update("tasks", rid, 1, ident=ident, title="越权篡改")
    assert _task(db_conn, rid) == before
    assert _boss_ok_audits(rid) == []


# ---------------------------------------------------------------------------
# 三、Authorize 不可伪造（普通调用方路径）
# ---------------------------------------------------------------------------

def test_authorize_direct_construction_forbidden():
    with pytest.raises(PermissionError, match="authorize_direct_construction_forbidden"):
        identity.Authorize()


def test_authorize_immutable(identity_api):
    az = identity_api.make_authorize()
    with pytest.raises(PermissionError, match="authorize_immutable"):
        az._authority = object()
    with pytest.raises(PermissionError, match="authorize_immutable"):
        az._api = object()
    with pytest.raises(PermissionError, match="authorize_immutable"):
        az.__call__ = lambda ident: "boss"
    with pytest.raises(PermissionError):
        del az._authority
    assert not hasattr(az, "__dict__")


def test_authorize_behaviour_not_injectable(identity_api, db_conn):
    """即便绕过门禁造出实例，__call__ 也只委托真实 api.authorize，
    无法让它对未登记身份返回 boss。"""
    fake = _forged_authorize_instance(identity_api.make_authorize())
    with pytest.raises(PermissionError):
        fake(object())          # 未登记身份 → 真实 authorize 拒绝


def test_authorize_registry_is_trust_anchor(isolated_env, identity_api, auth_backend):
    """特权改写 _authority 指向另一 authority 后，仍因未登记被拒。"""
    az = identity_api.make_authorize()
    other_api = identity._build_identity(auth_backend)
    other_authority = object.__getattribute__(other_api, "_authority")
    object.__setattr__(az, "_authority", other_authority)
    object.__setattr__(az, "_api", other_api)
    w = db._open_production_writer()
    w._bind_authority(other_authority)
    with pytest.raises(PermissionError, match="authorize_not_registered"):
        cas.CASService(authorize=az, writer=w)
    w.close()


def test_bound_authority_not_rebindable(isolated_env, identity_api, auth_backend):
    """writer 的 authority 绑定一次后不可改（消除先到先得抢占）。"""
    w = db._open_production_writer()
    w._bind_authority(identity_api.owner_token)
    other_api = identity._build_identity(auth_backend)
    with pytest.raises(PermissionError):
        w._bind_authority(other_api.owner_token)
    with pytest.raises(PermissionError):
        w._bound_authority = other_api.owner_token
    w.close()


# ---------------------------------------------------------------------------
# 四、DisclosureService fail closed 与最小暴露面
# ---------------------------------------------------------------------------

def test_disclosure_module_functions_fail_closed(db_conn):
    """未注入 service → 拒绝，绝不回退到任何全局写能力。"""
    rid = _seed_cfo_private(db_conn)
    for bad in (None, object(), lambda: None):
        with pytest.raises(PermissionError, match="disclosure_service_required"):
            visibility.request_disclosure(bad, "tasks", rid, "cfo", "sales", "x")
        with pytest.raises(PermissionError, match="disclosure_service_required"):
            visibility.resolve_disclosure(bad, 1, "approved", actor="boss")
        with pytest.raises(PermissionError, match="disclosure_service_required"):
            visibility.consume_grant(bad, 1, "sales")


def test_disclosure_module_functions_accept_explicit_service(disclosure, db_conn):
    """显式传入合法 service → 薄委托正常工作（无全局槽）。"""
    rid = _seed_cfo_private(db_conn)
    aid = visibility.request_disclosure(disclosure, "tasks", rid, "cfo", "sales", "交接")
    visibility.resolve_disclosure(disclosure, aid, "approved", actor="boss")
    assert visibility.consume_grant(disclosure, aid, "sales")["id"] == rid


def test_disclosure_service_requires_write_capability():
    for bad in (None, object(), "writer"):
        with pytest.raises(TypeError, match="disclosure_writer_required"):
            visibility.DisclosureService(bad)


def test_disclosure_service_minimal_surface(disclosure):
    """只暴露三个业务方法：不暴露 writer / cursor / 通用事务接口。"""
    with pytest.raises(PermissionError, match="disclosure_service_writer_not_exposed"):
        disclosure._writer
    for name in ("transaction", "cursor", "execute", "conn", "writer"):
        assert not hasattr(disclosure, name), f"DisclosureService 暴露了 {name}"
    with pytest.raises(PermissionError, match="disclosure_service_immutable"):
        disclosure._writer = None