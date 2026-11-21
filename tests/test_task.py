"""T 组：task_tool 六 action（PRD §5.2/§8.1，M3.1 清单 §三.3 治理纪律）。

create 幂等键 / list 可见性 / status 流转 / update 乐观锁 / delete 单条 /
bulk_delete 阈值分流（治理专项见 test_bulk_delete.py）。
统一纪律：身份经 resolve_identity；写入经 resolve_visibility + validate_persist；
查询经 query_visible；更新/删除经 CAS（write_where）；审计无内容原文。
"""
from __future__ import annotations

import pytest

from opc_workbench import cas, config, db, visibility
from opc_workbench.tools import task as task_tool


@pytest.fixture()
def tool(identity_api, cas_service):
    return task_tool.TaskTool(cas_service=cas_service, identity_api=identity_api)


def _call(tool, action, frame_caller="sales", llm_args=None, trusted_ctx=None, **params):
    return tool.dispatch(action, _frame_caller=frame_caller,
                         _llm_args=llm_args or {}, _trusted_ctx=trusted_ctx, **params)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_defaults_and_visibility(tool, db_conn):
    res = _call(tool, "create", title="普通任务")
    assert res["status"] == "todo" and res["visibility"] == "public"  # sales 默认
    row = db_conn.execute("SELECT * FROM tasks WHERE id=?", (res["id"],)).fetchone()
    assert row["owner_role"] == "sales" and row["version"] == 1


def test_create_personal_default_private(tool, db_conn):
    res = _call(tool, "create", frame_caller="personal", title="预约体检")
    assert res["visibility"] == "private"


def test_create_sensitive_hint_upgrades_private(tool):
    res = _call(tool, "create", title="下周体检安排")
    assert res["visibility"] == "private"   # 敏感词升级


def test_create_explicit_visibility(tool):
    res = _call(tool, "create", title="x", visibility="private")
    assert res["visibility"] == "private"
    res2 = _call(tool, "create", title="y", visibility="internal")
    assert res2["error"]["code"] == "invalid_param"


def test_create_idempotency_key_returns_existing(tool, db_conn):
    r1 = _call(tool, "create", title="幂等", idempotency_key="k-1")
    r2 = _call(tool, "create", title="幂等重复", idempotency_key="k-1")
    assert r1["id"] == r2["id"]
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 1


def test_create_due_at_requires_timezone(tool):
    res = _call(tool, "create", title="x", due_at="2026-11-25T09:00:00")
    assert res["error"]["code"] == "invalid_param"
    ok = _call(tool, "create", title="x", due_at="2026-11-25T09:00:00+08:00")
    assert "id" in ok


def test_create_llm_owner_role_spoof_dropped(tool, db_conn):
    res = _call(tool, "create", llm_args={"owner_role": "cfo", "caller_role": "boss"},
                title="伪造归属")
    row = db_conn.execute("SELECT owner_role FROM tasks WHERE id=?", (res["id"],)).fetchone()
    assert row["owner_role"] == "sales"   # 按真实 caller 归属


def test_create_boss_without_ctx_rejected(tool):
    with pytest.raises(PermissionError):
        _call(tool, "create", frame_caller="boss", title="x")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_visibility_filter(tool, db_conn):
    _call(tool, "create", title="sales公开")
    _call(tool, "create", frame_caller="cfo", title="cfo私密")   # cfo 默认 private
    _call(tool, "create", frame_caller="cfo", title="cfo公开", visibility="public")
    res = _call(tool, "list")
    titles = {it["title"] for it in res["items"]}
    assert titles == {"sales公开", "cfo公开"}   # 他角色 private 不可见
    res_boss = _call(tool, "list", frame_caller="boss",
                     trusted_ctx=_boss_ctx(tool))
    assert len(res_boss["items"]) == 3


def _boss_ctx(tool):
    api = tool._identity_api
    backend = api._authority._auth_backend
    ev = backend.authenticate("sess-task-boss")
    api.register_session(ev)
    return api.mint_boss_context(api.issue_proof("sess-task-boss"))


def test_list_filters_and_limit(tool):
    for i in range(5):
        _call(tool, "create", title=f"t{i}")
    res = _call(tool, "list", status="todo", limit=3)
    assert len(res["items"]) == 3 and res["total"] == 5
    bad = _call(tool, "list", version=1)   # 白名单外过滤列
    assert bad["error"]["code"] == "invalid_param"


def test_list_coordinator_only_public(tool):
    _call(tool, "create", title="pub")
    _call(tool, "create", frame_caller="personal", title="pri")
    res = _call(tool, "list", frame_caller="coordinator")
    assert {it["title"] for it in res["items"]} == {"pub"}


# ---------------------------------------------------------------------------
# update_status / update
# ---------------------------------------------------------------------------

def test_status_flow_legal(tool):
    r = _call(tool, "create", title="流转")
    r2 = _call(tool, "update_status", id=r["id"], status="doing", version=1)
    assert r2["status"] == "doing" and r2["version"] == 2
    r3 = _call(tool, "update_status", id=r["id"], status="done", version=2)
    assert r3["status"] == "done"


def test_status_flow_illegal(tool):
    r = _call(tool, "create", title="流转")
    bad = _call(tool, "update_status", id=r["id"], status="done", version=1)
    assert bad["error"]["code"] == "invalid_param"   # todo→done 跳变拒绝
    r2 = _call(tool, "update_status", id=r["id"], status="doing", version=1)
    back = _call(tool, "update_status", id=r["id"], status="todo", version=2)
    assert back["error"]["code"] == "invalid_param"   # doing→todo 回退拒绝


def test_cancelled_reachable_from_any(tool):
    for start in ("todo", "doing", "done"):
        r = _call(tool, "create", title=f"取消{start}")
        v = 1
        if start in ("doing", "done"):
            _call(tool, "update_status", id=r["id"], status="doing", version=v); v += 1
        if start == "done":
            _call(tool, "update_status", id=r["id"], status="done", version=v); v += 1
        res = _call(tool, "update_status", id=r["id"], status="cancelled", version=v)
        assert res["status"] == "cancelled"


def test_update_version_conflict(tool):
    r = _call(tool, "create", title="冲突")
    _call(tool, "update", id=r["id"], version=1, title="第一次")
    res = _call(tool, "update", id=r["id"], version=1, title="过期")
    assert res["error"]["code"] == "version_conflict"


def test_update_other_role_not_found(tool):
    r = _call(tool, "create", frame_caller="cfo", title="他角色", visibility="public")
    res = _call(tool, "update", id=r["id"], version=1, title="越权")
    assert res["error"]["code"] == "not_found"


def test_update_governance_field_rejected(tool):
    r = _call(tool, "create", title="治理")
    res = _call(tool, "update", id=r["id"], version=1, owner_role="boss")
    assert res["error"]["code"] == "invalid_param"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_single_own(tool, db_conn):
    r = _call(tool, "create", title="待删")
    res = _call(tool, "delete", id=r["id"], version=1)
    assert res["deleted"] is True
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


def test_delete_other_role_not_found(tool, db_conn):
    r = _call(tool, "create", frame_caller="cfo", title="他角色", visibility="public")
    res = _call(tool, "delete", id=r["id"], version=1)
    assert res["error"]["code"] == "not_found"
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 1


def test_delete_missing_version_param(tool):
    r = _call(tool, "create", title="x")
    res = _call(tool, "delete", id=r["id"])
    assert res["error"]["code"] == "invalid_param"


# ---------------------------------------------------------------------------
# bulk_delete 入口分流（治理细节见 test_bulk_delete.py）
# ---------------------------------------------------------------------------

def test_bulk_delete_agent_threshold_approval(tool, db_conn):
    ids = [_call(tool, "create", title=f"b{i}")["id"] for i in range(5)]
    res = _call(tool, "bulk_delete", ids=ids)
    assert "approval_id" in res
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 5


def test_bulk_delete_boss_no_token_confirmation_required(tool, db_conn):
    ids = [_call(tool, "create", title=f"b{i}")["id"] for i in range(5)]
    ctx = _boss_ctx(tool)
    res = _call(tool, "bulk_delete", frame_caller="boss", trusted_ctx=ctx, ids=ids)
    assert res == {"error": {"code": "confirmation_required"}}


def test_bulk_delete_under_threshold_direct(tool, db_conn):
    ids = [_call(tool, "create", title=f"s{i}")["id"] for i in range(4)]
    res = _call(tool, "bulk_delete", ids=ids)
    assert res["deleted_n"] == 4


# ---------------------------------------------------------------------------
# 注册表与 FC schema 纪律
# ---------------------------------------------------------------------------

def test_fc_schema_excludes_identity_fields(tool):
    schema = tool.fc_schemas()
    assert set(schema) == {"create", "list", "update_status", "update", "delete", "bulk_delete"}
    for name, spec in schema.items():
        props = spec["parameters"]["properties"]
        for forbidden in ("caller_role", "owner_role", "actor", "requester",
                          "_frame_caller", "_llm_args", "_trusted_ctx", "versions"):
            assert forbidden not in props, (name, forbidden)


def test_audit_no_content(tool, isolated_env):
    _call(tool, "create", title="机密内容原文")
    from opc_workbench import audit_log
    audit_log._get_logger().handlers[0].flush()
    log_text = (config.LOG_DIR / "audit.log").read_text(encoding="utf-8")
    assert "机密内容原文" not in log_text