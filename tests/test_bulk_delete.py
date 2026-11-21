"""bulk_delete 治理专项（M3.1 清单 §三.1 v2–v9 定稿）。

方案 A：独立三函数（M2 披露函数零改动）；digest 独立列；仅 boss 决议；
消费与删除同一写事务；全删或全不删；kind 进所有最终 UPDATE；并发恰好一次；
过期两态清扫；expires_at json_type 守卫；阈值 ≥5 Agent 审批 / boss 直执需
确认令牌 / <5 直删；三入口签名无裸 requester/actor。
"""
from __future__ import annotations

import json
import threading

import pytest

from opc_workbench import bulk_delete as bd
from opc_workbench import cas, config, db, identity, visibility


@pytest.fixture()
def bulk_ctx(identity_api, isolated_env):
    w = db._open_test_writer(config.DB_PATH)
    service = cas._build_cas_service(authorize=identity_api.make_authorize(), writer=w)
    ctx = bd.BulkDeleteContext(identity_api=identity_api, cas_service=service)
    yield ctx
    w.close()


def _seed(conn, n=6, owner="sales", vis="public"):
    ids, versions = [], {}
    with conn:
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (f"t{i}", owner, vis, db.now_iso(), db.now_iso()))
            ids.append(cur.lastrowid)
            versions[cur.lastrowid] = 1
    return ids, versions


def _approval(conn, aid):
    return conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()


# ---------------------------------------------------------------------------
# 签名纪律：三入口无裸 requester/actor
# ---------------------------------------------------------------------------

def test_signatures_have_no_bare_role_params():
    import inspect
    for fn in (bd.request_bulk_delete, bd.resolve_bulk_delete, bd.consume_bulk_delete):
        params = inspect.signature(fn).parameters
        assert "requester" not in params and "actor" not in params
        assert "_frame_caller" in params and "_llm_args" in params


def test_bare_requester_kwarg_typeerror(db_conn, identity_api, bulk_ctx):
    ids, _ = _seed(db_conn)
    with pytest.raises(TypeError):
        bd.request_bulk_delete("tasks", ids, requester="boss", _ctx=bulk_ctx)


def test_frame_caller_boss_without_ctx_rejected(db_conn, identity_api, bulk_ctx):
    ids, _ = _seed(db_conn)
    with pytest.raises(PermissionError):
        bd.request_bulk_delete("tasks", ids, _frame_caller="boss", _llm_args={}, _ctx=bulk_ctx)


# ---------------------------------------------------------------------------
# Agent 审批流：request → boss resolve → consume
# ---------------------------------------------------------------------------

def test_agent_request_creates_approval_with_digest(db_conn, identity_api, bulk_ctx):
    ids, versions = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert isinstance(aid, int)
    row = _approval(db_conn, aid)
    assert row["kind"] == "bulk_delete" and row["status"] == "pending"
    payload = json.loads(row["payload"])
    assert payload["table"] == "tasks"
    assert payload["ids"] == sorted(ids)
    assert payload["versions"] == {str(k): v for k, v in versions.items()}
    assert payload["requester"] == "sales"
    assert row["digest"] is not None and len(row["digest"]) == 64
    # 目标行未被删除（审批中）
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6


def test_llm_forged_requester_dropped(db_conn, identity_api, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales",
                                 _llm_args={"requester": "boss", "actor": "boss"}, _ctx=bulk_ctx)
    payload = json.loads(_approval(db_conn, aid)["payload"])
    assert payload["requester"] == "sales"   # 按真实 caller 走审批流


def test_agent_self_approve_rejected(db_conn, identity_api, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    with pytest.raises(PermissionError):
        bd.resolve_bulk_delete(aid, "approved", _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert _approval(db_conn, aid)["status"] == "pending"


def test_boss_resolve_and_requester_consume(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    assert _approval(db_conn, aid)["status"] == "approved"
    result = bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert result["deleted_n"] == 6
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0
    assert _approval(db_conn, aid)["status"] == "expired"   # 消费即终态


def test_boss_can_also_consume(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    result = bd.consume_bulk_delete(aid, _frame_caller="boss",
                                    _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    assert result["deleted_n"] == 6


def test_other_agent_consume_rejected(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    result = bd.consume_bulk_delete(aid, _frame_caller="marketing", _llm_args={}, _ctx=bulk_ctx)
    assert result == visibility.PLACEHOLDER
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6


def test_consume_unapproved_placeholder(db_conn, identity_api, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx) == visibility.PLACEHOLDER


# ---------------------------------------------------------------------------
# 原子性六约束
# ---------------------------------------------------------------------------

def test_target_version_changed_all_or_nothing(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    with db_conn:
        db_conn.execute("UPDATE tasks SET version=version+1 WHERE id=?", (ids[2],))
    result = bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert result == visibility.PLACEHOLDER
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6
    assert _approval(db_conn, aid)["status"] == "approved"   # 回滚可重试


def test_target_missing_all_or_nothing(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    with db_conn:
        db_conn.execute("DELETE FROM tasks WHERE id=?", (ids[0],))
    assert bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx) == visibility.PLACEHOLDER
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 5


def test_write_where_in_consume_other_owner_public(db_conn, identity_api, boss_ctx, bulk_ctx):
    """消费逐条 write_where：requester=sales 的审批单含 cfo public 行 →
    该条写不可达 → 全不删（审批单目标必须 requester 可写）。"""
    ids_sales, _ = _seed(db_conn, 3, "sales")
    ids_cfo, _ = _seed(db_conn, 3, "cfo")
    aid = bd.request_bulk_delete("tasks", ids_sales + ids_cfo,
                                 _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    assert bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx) == visibility.PLACEHOLDER
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6


def test_digest_tamper_single_field_rejected(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    with db_conn:
        db_conn.execute(
            "UPDATE approvals SET payload=json_set(payload,'$.requester','boss') "
            "WHERE id=?", (aid,))
    assert bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx) == visibility.PLACEHOLDER
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6
    assert _approval(db_conn, aid)["status"] == "approved"


def test_digest_tamper_ids_and_versions_rejected(db_conn, identity_api, boss_ctx, bulk_ctx):
    """同时篡改 ids+versions 不动 digest 列 → 摘要不匹配拒绝。"""
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    row = _approval(db_conn, aid)
    payload = json.loads(row["payload"])
    payload["ids"] = payload["ids"][:-1]
    payload["versions"] = {k: 99 for k in payload["versions"]}
    with db_conn:
        db_conn.execute("UPDATE approvals SET payload=? WHERE id=?",
                        (json.dumps(payload), aid))
    assert bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx) == visibility.PLACEHOLDER


def test_concurrent_consume_exactly_once(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    results, barrier = [], threading.Barrier(2)

    def racer():
        db.get_conn()
        barrier.wait()
        results.append(bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx))
        db.close_thread_conns()

    ts = [threading.Thread(target=racer) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)
    ok = [r for r in results if r != visibility.PLACEHOLDER]
    assert len(ok) == 1 and ok[0]["deleted_n"] == 6


def test_kind_isolation_both_directions(db_conn, identity_api, boss_ctx, bulk_ctx):
    """bulk_delete 函数不触碰披露单；披露函数不触碰 bulk_delete 单。"""
    ids, _ = _seed(db_conn)
    aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    # 披露决议入口对 bulk_delete 单无效（kind 隔离）
    visibility.resolve_disclosure(aid, "approved", actor="boss")
    assert _approval(db_conn, aid)["status"] == "pending"
    # bulk_delete 决议对披露单无效
    with db_conn:
        cur = db_conn.execute(
            "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
            "VALUES('p','cfo','private',?,?)", (db.now_iso(), db.now_iso()))
    d_aid = visibility.request_disclosure("tasks", cur.lastrowid, "cfo", "sales", "x")
    bd.resolve_bulk_delete(d_aid, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    assert _approval(db_conn, d_aid)["status"] == "pending"


def test_expired_two_state_sweep(db_conn, identity_api, boss_ctx, monkeypatch, bulk_ctx):
    t0 = 4_000_000.0
    monkeypatch.setattr(bd, "_now", lambda: t0)
    ids, _ = _seed(db_conn)
    a1 = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    a2 = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    bd.resolve_bulk_delete(a2, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    monkeypatch.setattr(bd, "_now", lambda: t0 + bd.APPROVAL_TTL_S + 1)
    # pending 过期：决议 noop 且转 expired
    bd.resolve_bulk_delete(a1, "approved", _frame_caller="boss",
                           _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    assert _approval(db_conn, a1)["status"] == "expired"
    # approved 过期：消费 PLACEHOLDER 且转 expired
    assert bd.consume_bulk_delete(a2, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx) == visibility.PLACEHOLDER
    assert _approval(db_conn, a2)["status"] == "expired"


def test_invalid_expires_at_type_fail_closed(db_conn, identity_api, boss_ctx, bulk_ctx):
    """非法 expires_at 类型（字符串/布尔/null）→ 决议与消费两路径 fail closed。

    篡改在 request 之前布置不可行（digest 会重算），故 request 后篡改并
    同步重算 digest——隔离类型守卫本身（摘要篡改另有专项用例）。
    """
    ids, _ = _seed(db_conn)
    for bad in ('"2026-11-21"', "true", "null"):
        aid = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
        row = _approval(db_conn, aid)
        payload = json.loads(row["payload"])
        payload["expires_at"] = json.loads(bad)   # str / True / None
        import hashlib
        from opc_workbench.identity import canonical_json
        core = {k: payload[k] for k in ("table","ids","versions","requester","expires_at")}
        digest = hashlib.sha256(canonical_json(core).encode()).hexdigest()
        with db_conn:
            db_conn.execute("UPDATE approvals SET payload=?, digest=? WHERE id=?",
                            (json.dumps(payload), digest, aid))
        bd.resolve_bulk_delete(aid, "approved", _frame_caller="boss",
                               _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
        assert _approval(db_conn, aid)["status"] == "pending"   # 类型守卫拒绝决议
        assert bd.consume_bulk_delete(aid, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx) == visibility.PLACEHOLDER
        assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6


def test_request_dedup_same_target(db_conn, identity_api, bulk_ctx):
    """审批单去重（PRD §8.1）：同 requester+table+ids 的 pending 单复用。"""
    ids, _ = _seed(db_conn)
    a1 = bd.request_bulk_delete("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    a2 = bd.request_bulk_delete("tasks", list(reversed(ids)),
                                _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert a1 == a2


# ---------------------------------------------------------------------------
# boss 直执路径（≥5 需确认令牌）与 <5 直删
# ---------------------------------------------------------------------------

def test_boss_direct_without_token_confirmation_required(db_conn, identity_api, boss_ctx, bulk_ctx):
    ids, _ = _seed(db_conn)
    result = bd.bulk_delete_action("tasks", ids, _frame_caller="boss",
                                   _llm_args={}, _trusted_ctx=boss_ctx, _ctx=bulk_ctx)
    assert result == {"error": {"code": "confirmation_required"}}
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6
    # 无审批单产生
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM approvals WHERE kind='bulk_delete'").fetchone()["c"] == 0


def test_boss_direct_with_token_full_chain(db_conn, identity_api, auth_backend,
                                           boss_ctx, ui_confirmation_request_cls, bulk_ctx):
    ids, versions = _seed(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions, _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    result = bd.bulk_delete_action("tasks", ids, _frame_caller="boss",
                                   _llm_args={}, _trusted_ctx=ctx1, _ctx=bulk_ctx)
    assert result["deleted_n"] == 6
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


def test_boss_direct_token_version_drift_stale(db_conn, identity_api, auth_backend,
                                               boss_ctx, ui_confirmation_request_cls, bulk_ctx):
    ids, versions = _seed(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions, _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    with db_conn:
        db_conn.execute("UPDATE tasks SET version=version+1 WHERE id=?", (ids[1],))
    result = bd.bulk_delete_action("tasks", ids, _frame_caller="boss",
                                   _llm_args={}, _trusted_ctx=ctx1, _ctx=bulk_ctx)
    assert result == {"error": {"code": "confirmation_stale"}}
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6
    # 令牌回退 issued：修复版本后可重试（重新确认不需要——快照仍匹配原值？
    # 不：漂移后快照与当前不符，重试仍 stale；此处断言令牌未被烧掉即可）
    snap = identity_api.claim_token(ctx1._token.token_id, "tasks", ids, _ctx=ctx1)
    assert snap is not None   # 回退 issued，可再次 claim


def test_boss_direct_token_reuse_rejected(db_conn, identity_api, auth_backend,
                                          boss_ctx, ui_confirmation_request_cls, bulk_ctx):
    ids, versions = _seed(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions, _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    assert bd.bulk_delete_action("tasks", ids, _frame_caller="boss",
                                 _llm_args={}, _trusted_ctx=ctx1, _ctx=bulk_ctx)["deleted_n"] == 6
    ids2, versions2 = _seed(db_conn, 6)
    result = bd.bulk_delete_action("tasks", ids2, _frame_caller="boss",
                                   _llm_args={}, _trusted_ctx=ctx1, _ctx=bulk_ctx)
    assert result["error"]["code"] in ("confirmation_required", "confirmation_stale")


def test_under_threshold_any_caller_direct(db_conn, identity_api, bulk_ctx):
    """<5 直删：任何 caller 逐条 write_where + 版本校验，无审批单。"""
    ids, _ = _seed(db_conn, 4)
    result = bd.bulk_delete_action("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert result["deleted_n"] == 4
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM approvals WHERE kind='bulk_delete'").fetchone()["c"] == 0


def test_under_threshold_other_owner_not_found(db_conn, identity_api, bulk_ctx):
    ids, _ = _seed(db_conn, 3, "cfo")
    result = bd.bulk_delete_action("tasks", ids, _frame_caller="sales", _llm_args={}, _ctx=bulk_ctx)
    assert result == visibility.PLACEHOLDER   # 全不删（write_where 不可达）
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 3


def test_governance_functions_not_in_registry():
    from opc_workbench.tools import build_tools
    from opc_workbench import cas, identity
    # 注册表断言（减少暴露面；身份约束不依赖它）
    registry = build_tools(cas_service=None, identity_api=None)
    for name in registry:
        assert "resolve_bulk_delete" not in name
        assert "consume_bulk_delete" not in name
        assert "request_bulk_delete" not in name