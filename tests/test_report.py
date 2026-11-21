"""R 组：report_tool（PRD §5.5/§8.4，M3.1 清单 §三.5 v6 定稿）。

boss 缺省全聚合 owner='boss'；员工禁指定他角色；原子 UPSERT（version+1、
updated_at=excluded.updated_at）；degraded 落 refs_json；报告恒 private
（含降级模板）；period_key 日/ISO 周（Asia/Shanghai 日界）；vault 双写 M4。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest

from opc_workbench import cas, config, db, llm_client
from opc_workbench.tools import report as report_tool


@pytest.fixture()
def tool(isolated_env, identity_api):
    w = db._open_test_writer(config.DB_PATH)
    service = cas._build_cas_service(authorize=identity_api.make_authorize(), writer=w)
    t = report_tool.ReportTool(cas_service=service, identity_api=identity_api,
                               llm=isolated_env["fake_llm"])
    yield t
    w.close()


def _boss_ctx(tool):
    api = tool._identity_api
    backend = api._authority._auth_backend
    ev = backend.authenticate("sess-report-boss")
    api.register_session(ev)
    return api.mint_boss_context(api.issue_proof("sess-report-boss"))


def _call(tool, action, frame_caller="sales", llm_args=None, trusted_ctx=None, **params):
    return tool.dispatch(action, _frame_caller=frame_caller,
                         _llm_args=llm_args or {}, _trusted_ctx=trusted_ctx, **params)


def _seed_task(conn, owner, status="done", created="2026-11-21T08:00:00+08:00"):
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,status,owner_role,created_at,updated_at) "
            "VALUES(?,?,?,?,?)", (f"{owner}任务", status, owner, created, created))
    return cur.lastrowid


# ---------------------------------------------------------------------------
# 聚合身份
# ---------------------------------------------------------------------------

def test_boss_default_aggregates_all_owner_boss(tool, db_conn):
    _seed_task(db_conn, "sales")
    _seed_task(db_conn, "cfo")
    ctx = _boss_ctx(tool)
    res = _call(tool, "generate_daily", frame_caller="boss", trusted_ctx=ctx,
                date="2026-11-21")
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert row["owner_role"] == "boss"
    assert row["kind"] == "daily_report" and row["period_key"] == "2026-11-21"
    assert row["visibility"] == "private"


def test_boss_explicit_role_aggregates_only_that_role(tool, db_conn):
    _seed_task(db_conn, "sales")
    _seed_task(db_conn, "cfo")
    ctx = _boss_ctx(tool)
    res = _call(tool, "generate_daily", frame_caller="boss", trusted_ctx=ctx,
                date="2026-11-21", owner_role="cfo")
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert row["owner_role"] == "cfo"
    refs = json.loads(row["refs_json"])
    assert refs["stats"]["tasks_done"] == 1   # 仅 cfo 一条


def test_employee_other_role_rejected(tool, db_conn):
    res = _call(tool, "generate_daily", date="2026-11-21", owner_role="cfo")
    assert res["error"]["code"] == "invalid_param"


def test_employee_own_role_report(tool, db_conn):
    _seed_task(db_conn, "sales")
    res = _call(tool, "generate_daily", date="2026-11-21")
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert row["owner_role"] == "sales" and row["visibility"] == "private"


def test_llm_forged_owner_role_dropped(tool, db_conn):
    _seed_task(db_conn, "sales")
    res = _call(tool, "generate_daily", date="2026-11-21",
                llm_args={"owner_role": "boss", "caller_role": "boss"})
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert row["owner_role"] == "sales"


# ---------------------------------------------------------------------------
# UPSERT 幂等 + version + updated_at
# ---------------------------------------------------------------------------

def test_upsert_same_key_one_row_version_increments(tool, db_conn):
    _seed_task(db_conn, "sales")
    r1 = _call(tool, "generate_daily", date="2026-11-21")
    r2 = _call(tool, "generate_daily", date="2026-11-21")
    assert r1["artifact_id"] == r2["artifact_id"]
    rows = db_conn.execute(
        "SELECT * FROM artifacts WHERE kind='daily_report' AND period_key='2026-11-21' "
        "AND owner_role='sales'").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == 2


def test_upsert_updated_at_refreshes(tool, db_conn, monkeypatch):
    """v6：重复生成 updated_at > 首次值（时钟注入断言刷新，非保留旧值）。"""
    _seed_task(db_conn, "sales")
    t1 = "2026-11-21T10:00:00+00:00"
    t2 = "2026-11-21T11:00:00+00:00"
    monkeypatch.setattr(report_tool, "_now_iso", lambda: t1)
    r1 = _call(tool, "generate_daily", date="2026-11-21")
    first = db_conn.execute("SELECT updated_at FROM artifacts WHERE id=?",
                            (r1["artifact_id"],)).fetchone()["updated_at"]
    monkeypatch.setattr(report_tool, "_now_iso", lambda: t2)
    _call(tool, "generate_daily", date="2026-11-21")
    second = db_conn.execute("SELECT updated_at FROM artifacts WHERE id=?",
                             (r1["artifact_id"],)).fetchone()["updated_at"]
    assert first == t1 and second == t2 and second > first


def test_concurrent_upsert_exactly_one_row(tool, db_conn):
    _seed_task(db_conn, "sales")
    results, barrier = [], threading.Barrier(2)

    def racer():
        barrier.wait()
        results.append(_call(tool, "generate_daily", date="2026-11-21"))

    ts = [threading.Thread(target=racer) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)
    rows = db_conn.execute(
        "SELECT version FROM artifacts WHERE kind='daily_report' "
        "AND period_key='2026-11-21' AND owner_role='sales'").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == 2
    assert all("artifact_id" in r for r in results)


# ---------------------------------------------------------------------------
# 降级与零数据
# ---------------------------------------------------------------------------

def test_llm_failure_degraded_template(tool, db_conn, isolated_env):
    _seed_task(db_conn, "sales")
    isolated_env["fake_llm"].responses = [llm_client.LLMFailure("boom")]
    res = _call(tool, "generate_daily", date="2026-11-21")
    assert res["degraded"] is True
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    refs = json.loads(row["refs_json"])
    assert refs["degraded"] is True
    assert row["visibility"] == "private"   # 降级模板同样 private
    assert refs["stats"]["tasks_done"] == 1  # 纯统计照常


def test_no_data_template_skips_llm(tool, db_conn, isolated_env):
    res = _call(tool, "generate_daily", date="2026-11-21")
    assert "本期无记录" in res["content_md"]
    assert isolated_env["fake_llm"].calls == []   # 零数据不调 LLM
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert json.loads(row["refs_json"])["degraded"] is False


def test_normal_report_calls_llm(tool, db_conn, isolated_env):
    _seed_task(db_conn, "sales")
    isolated_env["fake_llm"].responses = ["# AI 点评\n不错"]
    res = _call(tool, "generate_daily", date="2026-11-21")
    assert res["degraded"] is False
    assert len(isolated_env["fake_llm"].calls) == 1
    assert "AI 点评" in res["content_md"]


# ---------------------------------------------------------------------------
# period_key 与可见性
# ---------------------------------------------------------------------------

def test_weekly_iso_period_key(tool, db_conn):
    _seed_task(db_conn, "sales")
    res = _call(tool, "generate_weekly", week="2026-W47")
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert row["kind"] == "weekly_report" and row["period_key"] == "2026-W47"


def test_weekly_default_current_iso_week(tool, db_conn, monkeypatch):
    monkeypatch.setattr(report_tool, "_now_iso", lambda: "2026-11-21T10:00:00+08:00")
    res = _call(tool, "generate_weekly")
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    # 2026-11-21 是周六，ISO 周 2026-W47
    assert row["period_key"] == "2026-W47"


def test_daily_default_today_shanghai(tool, db_conn, monkeypatch):
    # UTC 2026-11-21 23:00 = 本地 2026-11-22 07:00 → 日界取本地
    monkeypatch.setattr(report_tool, "_now_iso", lambda: "2026-11-21T23:00:00+00:00")
    res = _call(tool, "generate_daily")
    row = db_conn.execute("SELECT * FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert row["period_key"] == "2026-11-22"


def test_list_get_visibility_filter(tool, db_conn):
    _seed_task(db_conn, "sales")
    _call(tool, "generate_daily", date="2026-11-21")               # sales private
    ctx = _boss_ctx(tool)
    _call(tool, "generate_daily", frame_caller="boss", trusted_ctx=ctx,
          date="2026-11-21", owner_role="cfo")                    # cfo private
    res_sales = _call(tool, "list")
    assert len(res_sales["items"]) == 1   # 只见自有
    res_boss = _call(tool, "list", frame_caller="boss", trusted_ctx=ctx)
    assert len(res_boss["items"]) == 2
    got = _call(tool, "get", id=res_sales["items"][0]["id"])
    assert got["refs"]["degraded"] is False
    other_id = [it["id"] for it in res_boss["items"]
                if it["id"] != res_sales["items"][0]["id"]][0]
    denied = _call(tool, "get", id=other_id)
    assert denied["error"]["code"] == "not_found"


def test_report_explicit_public_forced_private(tool, db_conn):
    """resolve_visibility kind=report：explicit public 强制回落 private。"""
    _seed_task(db_conn, "sales")
    res = _call(tool, "generate_daily", date="2026-11-21", visibility="public")
    row = db_conn.execute("SELECT visibility FROM artifacts WHERE id=?",
                          (res["artifact_id"],)).fetchone()
    assert row["visibility"] == "private"


def test_fc_schema_excludes_identity_fields(tool):
    schema = tool.fc_schemas()
    assert set(schema) == {"generate_daily", "generate_weekly", "list", "get"}
    for name, spec in schema.items():
        props = spec["parameters"]["properties"]
        for forbidden in ("caller_role", "actor", "_frame_caller", "_llm_args",
                          "_trusted_ctx"):
            assert forbidden not in props, (name, forbidden)
    # owner_role 仅 boss 可用但仍是业务参数（员工传他角色被拒）——保留在 schema
    assert "owner_role" in schema["generate_daily"]["parameters"]["properties"]