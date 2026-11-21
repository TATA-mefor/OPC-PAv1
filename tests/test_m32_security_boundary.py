"""M3.2 ① 写能力获取边界负向用例。

db 侧内部 writer 获取入口已删除；DisclosureService 显式注入、fail closed、
最小暴露面。所有断言为端到端行为断言（行内容 / version / 审计）。
"""
from __future__ import annotations

import json

import pytest

from opc_workbench import app, config, db, visibility


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




# ---------------------------------------------------------------------------
# 一、CAS writer-authorize 配对矩阵（用户给定 8 行）
# ---------------------------------------------------------------------------















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





def test_attack_b1_internal_writer_entry_is_gone(isolated_env, identity_api, db_conn):
    """B1 复现：原攻击第一步 db._internal_writer() 现已不存在。"""
    rid = _seed_cfo_private(db_conn)
    before = _task(db_conn, rid)
    with pytest.raises(AttributeError):
        db._internal_writer()
    assert _task(db_conn, rid) == before
    assert _boss_ok_audits(rid) == []




# ---------------------------------------------------------------------------
# 三、Authorize 不可伪造（普通调用方路径）
# ---------------------------------------------------------------------------











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