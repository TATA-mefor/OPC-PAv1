"""集成与全量回归（顺序 5）：真实调用链端到端。

用户验收口径：身份 → 授权 → 工具入口（注册表 handler）→ 数据库写能力 →
事务 → 状态变化 → 异常与回滚。全部经组合根装配的注册表入口触达，
不直调工具内部方法。
"""
from __future__ import annotations

import json

import pytest

from opc_workbench import app, config, db


@pytest.fixture()
def application(isolated_env):
    a = app.build_application()
    yield a
    a.close()


def _agent_call(application, action, role="sales", **params):
    """经调度器 Agent 通道调用：角色由框架注册表决定，调用方写不进 _frame_caller。"""
    channel = application.dispatcher.agent_channel(f"test-agent-{role}")
    return channel.submit(action, params)


def test_chain_task_create_update_delete(application, db_conn):
    """正向全链：create → update_status → delete，库状态逐步变化。"""
    r = _agent_call(application, "task.create", title="链路任务")
    tid = r["id"]
    row = db_conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "todo" and row["version"] == 1
    r2 = _agent_call(application, "task.update_status", id=tid, status="doing", version=1)
    assert r2["version"] == 2
    assert db_conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "doing"
    r3 = _agent_call(application, "task.delete", id=tid, version=2)
    assert r3["deleted"] is True
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


def test_chain_version_conflict_rollback(application, db_conn):
    """异常路径：过期版本更新 → version_conflict，库内容不变。"""
    r = _agent_call(application, "task.create", title="冲突链")
    _agent_call(application, "task.update", id=r["id"], version=1, title="已改")
    bad = _agent_call(application, "task.update", id=r["id"], version=1, title="过期写")
    assert bad["error"]["code"] == "version_conflict"
    row = db_conn.execute("SELECT title,version FROM tasks WHERE id=?", (r["id"],)).fetchone()
    assert row["title"] == "已改" and row["version"] == 2


def test_chain_bulk_delete_request_via_registry(application, db_conn):
    """审批链入口段：注册表 bulk_delete ≥5 → 审批单产生、目标未删。
    boss 决议/消费段生产不可达（M3 无 UI），已在 test_bulk_delete.py
    以测试组合根全量覆盖（含并发/篡改/回滚）。"""
    ids = [_agent_call(application, "task.create", title=f"批{i}")["id"] for i in range(6)]
    r = _agent_call(application, "task.bulk_delete", ids=ids)
    aid = r["approval_id"]
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 6
    row = db_conn.execute("SELECT kind,status,digest FROM approvals WHERE id=?",
                          (aid,)).fetchone()
    assert row["kind"] == "bulk_delete" and row["status"] == "pending"
    assert row["digest"] is not None


def test_chain_schedule_create_notify(application, db_conn, monkeypatch):
    """日程链：create → 提醒引擎 process_due → notified + 队列通知。"""
    from opc_workbench import reminder
    from opc_workbench.tools import schedule as schedule_tool
    from datetime import datetime
    fixed = datetime.fromisoformat("2026-11-21T09:00:00+08:00").timestamp()
    monkeypatch.setattr(reminder, "_now", lambda: fixed)
    monkeypatch.setattr(schedule_tool, "_now", lambda: fixed)
    r = _agent_call(application, "schedule.create", role="personal",
                    title="链路提醒", remind_at="2026-11-21T09:00:00+08:00")
    assert r["status"] == "pending"
    import queue
    # M3.2①：Application 不再暴露原始 writer；提醒引擎用同库测试写能力
    _w = db._open_test_writer(config.DB_PATH)
    eng = reminder.ReminderEngine(writer=_w, notify_queue=queue.Queue(50))
    assert eng.process_due() == 1
    row = db_conn.execute("SELECT * FROM schedules WHERE id=?", (r["id"],)).fetchone()
    assert row["status"] == "notified" and row["recovery_enqueued_at"] is not None
    note = eng.notify_queue.get_nowait()
    assert note["schedule_id"] == r["id"]


def test_chain_report_generate_private(application, db_conn):
    """报告链：Agent 生成 → artifacts private + degraded 字段落库。"""
    _agent_call(application, "task.create", title="报告素材")
    _agent_call(application, "task.update_status", id=1, status="doing", version=1)
    _agent_call(application, "task.update_status", id=1, status="done", version=2)
    r = _agent_call(application, "report.generate_daily", date="2026-11-21")
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (r["artifact_id"],)).fetchone()
    assert row["visibility"] == "private" and row["owner_role"] == "sales"
    assert json.loads(row["refs_json"])["degraded"] is False


def test_chain_cross_role_write_denied(application, db_conn):
    """越权链：marketing 改 sales public 任务 → not_found 同构，库不变。"""
    r = _agent_call(application, "task.create", title="他人任务")
    bad = _agent_call(application, "task.update", role="marketing",
                      id=r["id"], version=1, title="越权")
    assert bad["error"]["code"] == "not_found"
    assert db_conn.execute("SELECT title FROM tasks WHERE id=?",
                           (r["id"],)).fetchone()["title"] == "他人任务"


def test_chain_llm_spoof_via_registry(application, db_conn):
    """LLM 在参数里伪造身份字段经调度入口 → 不提升权限，归属仍按真实 caller。"""
    spoof = _agent_call(application, "task.create", title="伪造链",
                        owner_role="cfo", caller_role="boss")
    row = db_conn.execute("SELECT owner_role FROM tasks WHERE id=?",
                          (spoof["id"],)).fetchone()
    assert row["owner_role"] == "sales"


def test_registry_completeness(application):
    """注册表完整性：6+5+4+3=18 个 LLM action；治理与破坏性动作不在其中。"""
    names = set(application.dispatcher.schemas())
    assert len(names) == 18
    assert sum(n.startswith("task.") for n in names) == 6
    assert sum(n.startswith("schedule.") for n in names) == 5
    assert sum(n.startswith("report.") for n in names) == 4
    assert sum(n.startswith("knowledge.") for n in names) == 3
    assert "knowledge.delete_doc" not in names
    assert "knowledge.reindex" not in names
    for n in names:
        assert "resolve" not in n and "consume" not in n and "request" not in n