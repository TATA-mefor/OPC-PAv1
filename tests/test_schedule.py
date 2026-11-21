"""S 组：schedule_tool 五 action（PRD §5.4/§8.3）。

create（remind_at 早于 now 拒绝）/ list / reschedule（CAS）/ cancel /
mark_done（状态机）。提醒投递与补扫专项见 test_reminder.py。
"""
from __future__ import annotations

import pytest

from opc_workbench import cas, config, db
from opc_workbench.tools import schedule as schedule_tool

FUTURE = "2026-12-01T09:00:00+08:00"


@pytest.fixture()
def tool(isolated_env, identity_api, monkeypatch):
    # 固定时钟：2026-11-21 09:00 本地（FUTURE 在其后）
    from datetime import datetime
    fixed = datetime.fromisoformat("2026-11-21T09:00:00+08:00").timestamp()
    monkeypatch.setattr(schedule_tool, "_now", lambda: fixed)
    w = db._open_test_writer(config.DB_PATH)
    service = cas._build_cas_service(authorize=identity_api.make_authorize(), writer=w)
    t = schedule_tool.ScheduleTool(cas_service=service, identity_api=identity_api)
    yield t
    w.close()


def _call(tool, action, frame_caller="personal", llm_args=None, trusted_ctx=None, **params):
    return tool.dispatch(action, _frame_caller=frame_caller,
                         _llm_args=llm_args or {}, _trusted_ctx=trusted_ctx, **params)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_default_private_personal(tool, db_conn):
    res = _call(tool, "create", title="体检提醒", remind_at=FUTURE)
    assert res["visibility"] == "private" and res["status"] == "pending"
    row = db_conn.execute("SELECT * FROM schedules WHERE id=?", (res["id"],)).fetchone()
    assert row["owner_role"] == "personal" and row["repeat_rule"] == "none"


def test_create_past_remind_at_rejected(tool):
    res = _call(tool, "create", title="过期", remind_at="2026-11-20T09:00:00+08:00")
    assert res["error"]["code"] == "invalid_param"


def test_create_naive_remind_at_rejected(tool):
    res = _call(tool, "create", title="无时区", remind_at="2026-12-01T09:00:00")
    assert res["error"]["code"] == "invalid_param"


def test_create_repeat_rule_whitelist(tool):
    ok = _call(tool, "create", title="每日", remind_at=FUTURE, repeat_rule="daily")
    assert "id" in ok
    bad = _call(tool, "create", title="非法", remind_at=FUTURE, repeat_rule="hourly")
    assert bad["error"]["code"] == "invalid_param"


def test_create_sensitive_hint_private(tool):
    res = _call(tool, "create", frame_caller="sales", title="提醒我体检", remind_at=FUTURE)
    assert res["visibility"] == "private"


def test_create_boss_without_ctx_rejected(tool):
    with pytest.raises(PermissionError):
        _call(tool, "create", frame_caller="boss", title="x", remind_at=FUTURE)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_visibility_and_date_range(tool):
    _call(tool, "create", title="本人私密", remind_at=FUTURE)
    _call(tool, "create", frame_caller="sales", title="销售公开",
          remind_at="2026-12-02T09:00:00+08:00")
    _call(tool, "create", frame_caller="cfo", title="财务私密",
          remind_at="2026-12-03T09:00:00+08:00")
    res = _call(tool, "list")
    titles = {it["title"] for it in res["items"]}
    assert titles == {"本人私密", "销售公开"}   # 他角色 private 不可见
    ranged = _call(tool, "list", date_from="2026-12-02T00:00:00+08:00",
                   date_to="2026-12-02T23:59:59+08:00")
    assert {it["title"] for it in ranged["items"]} == {"销售公开"}


def test_list_coordinator_only_public(tool):
    _call(tool, "create", title="私密")
    _call(tool, "create", frame_caller="sales", title="公开", remind_at=FUTURE)
    res = _call(tool, "list", frame_caller="coordinator")
    assert {it["title"] for it in res["items"]} == {"公开"}


# ---------------------------------------------------------------------------
# reschedule / cancel / mark_done
# ---------------------------------------------------------------------------

def test_reschedule_cas_and_past_rejected(tool):
    r = _call(tool, "create", title="改期", remind_at=FUTURE)
    ok = _call(tool, "reschedule", id=r["id"], remind_at="2026-12-05T10:00:00+08:00",
               version=1)
    assert ok["version"] == 2
    conflict = _call(tool, "reschedule", id=r["id"],
                     remind_at="2026-12-06T10:00:00+08:00", version=1)
    assert conflict["error"]["code"] == "version_conflict"
    past = _call(tool, "reschedule", id=r["id"],
                 remind_at="2026-11-20T10:00:00+08:00", version=2)
    assert past["error"]["code"] == "invalid_param"


def test_reschedule_other_role_not_found(tool):
    r = _call(tool, "create", frame_caller="sales", title="他角色", remind_at=FUTURE)
    res = _call(tool, "reschedule", id=r["id"], remind_at="2026-12-05T10:00:00+08:00",
                version=1)
    assert res["error"]["code"] == "not_found"


def test_cancel_state_machine(tool):
    r = _call(tool, "create", title="取消", remind_at=FUTURE)
    ok = _call(tool, "cancel", id=r["id"], version=1)
    assert ok["status"] == "cancelled"
    again = _call(tool, "cancel", id=r["id"], version=2)
    assert again["error"]["code"] == "invalid_param"   # cancelled 终态


def test_mark_done_state_machine(tool):
    r = _call(tool, "create", title="完成", remind_at=FUTURE)
    ok = _call(tool, "mark_done", id=r["id"], version=1)
    assert ok["status"] == "done"
    cancel_done = _call(tool, "cancel", id=r["id"], version=2)
    assert cancel_done["error"]["code"] == "invalid_param"   # done 终态


def test_mark_done_notified_allowed(tool, db_conn):
    """notified → done 合法（提醒后确认完成）。"""
    r = _call(tool, "create", title="已提醒", remind_at=FUTURE)
    with db_conn:
        db_conn.execute("UPDATE schedules SET status='notified', version=2 WHERE id=?",
                        (r["id"],))
    ok = _call(tool, "mark_done", id=r["id"], version=2)
    assert ok["status"] == "done"


def test_cancel_notified_allowed(tool, db_conn):
    r = _call(tool, "create", title="已提醒取消", remind_at=FUTURE)
    with db_conn:
        db_conn.execute("UPDATE schedules SET status='notified', version=2 WHERE id=?",
                        (r["id"],))
    ok = _call(tool, "cancel", id=r["id"], version=2)
    assert ok["status"] == "cancelled"


# ---------------------------------------------------------------------------
# FC schema 纪律
# ---------------------------------------------------------------------------

def test_fc_schema_excludes_identity_fields(tool):
    schema = tool.fc_schemas()
    assert set(schema) == {"create", "list", "reschedule", "cancel", "mark_done"}
    for name, spec in schema.items():
        props = spec["parameters"]["properties"]
        for forbidden in ("caller_role", "owner_role", "actor", "requester",
                          "_frame_caller", "_llm_args", "_trusted_ctx"):
            assert forbidden not in props, (name, forbidden)