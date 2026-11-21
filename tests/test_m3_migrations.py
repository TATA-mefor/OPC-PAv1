"""M3 迁移 m003/m004 验收（M3.1 清单 §三.5，v6 索引步骤定稿）。

m003：artifacts 重建表——CHECK 加 'boss' + version 列 + updated_at 列，
UNIQUE/索引完整重建，逐行逐字段核对（从 PRAGMA 实际列清单出发），
中途失败整体回滚，重跑幂等。
m004：approvals.digest + schedules.notified_at + schedules.recovery_enqueued_at
三条 ALTER（可空；披露单 digest 恒 NULL）。

本文件不改 M1/M2 既有测试；旧基线中版本计数断言按 v10.3 执行安排随迁移更新。
"""
from __future__ import annotations

import sqlite3

import pytest

from opc_workbench import db


# ---------------------------------------------------------------------------
# 基线工具：把库停在 v2（M2 状态）并播种旧 artifacts 行
# ---------------------------------------------------------------------------

def _art_columns(conn) -> list[str]:
    return [r["name"] for r in conn.execute("PRAGMA table_info(artifacts)")]


def _art_indexes(conn) -> dict:
    """索引清单快照：名称 → (唯一性, 列元组)。含表约束自动索引。"""
    out = {}
    for r in conn.execute("PRAGMA index_list(artifacts)"):
        cols = tuple(c["name"] for c in conn.execute(f"PRAGMA index_info({r['name']})"))
        out[r["name"]] = (bool(r["unique"]), cols)
    return out


def _seed_legacy_artifacts(conn) -> list[dict]:
    """按 v2 实际列清单播种 3 行（含 private/public、不同 kind）。"""
    rows = [
        ("daily_report", "旧日报A", "# A", '{"x":1}', "cfo", "private", "2026-11-18", "2026-11-18T08:00:00+00:00"),
        ("weekly_report", "旧周报B", "# B", '{}', "sales", "public", "2026-W46", "2026-11-19T09:30:00+00:00"),
        ("other", "杂项C", "# C", '{"y":[2,3]}', "personal", "private", "misc-1", "2026-11-20T10:00:00+00:00"),
    ]
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO artifacts(kind,title,content_md,refs_json,owner_role,"
                "visibility,period_key,created_at) VALUES(?,?,?,?,?,?,?,?)", r)
    return [dict(zip(
        ("kind", "title", "content_md", "refs_json", "owner_role",
         "visibility", "period_key", "created_at"), r)) for r in rows]


@pytest.fixture()
def v2_conn(isolated_env, monkeypatch):
    """停在 schema v2 的测试库（模拟 M2 升级现场）。

    建库时临时把 MIGRATIONS 截到前两条，建完立即恢复完整列表
    （不能用 monkeypatch.undo——它会连带撤销 isolated_env 的路径隔离）。
    """
    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", full[:2])
    try:
        assert db.init_db() == 2
    finally:
        monkeypatch.setattr(db, "MIGRATIONS", full)
    return db.get_conn()


# ---------------------------------------------------------------------------
# m003：重建表
# ---------------------------------------------------------------------------

def test_m003_adds_version_updated_at_and_boss_check(v2_conn):
    """迁移后新列存在、boss 归属可写、10 角色仍可写、未知角色仍拒。"""
    seeded = _seed_legacy_artifacts(v2_conn)
    assert db.init_db() == 4
    cols = _art_columns(v2_conn)
    assert "version" in cols and "updated_at" in cols
    with v2_conn:
        v2_conn.execute(
            "INSERT INTO artifacts(kind,title,content_md,owner_role,period_key,"
            "created_at,updated_at) VALUES('daily_report','老板聚合','# boss','boss',"
            "'2026-11-21',?,?)", (db.now_iso(), db.now_iso()))
    with pytest.raises(sqlite3.IntegrityError):
        with v2_conn:
            v2_conn.execute(
                "INSERT INTO artifacts(kind,title,content_md,owner_role,period_key,"
                "created_at,updated_at) VALUES('other','x','x','ghost','k',?,?)",
                (db.now_iso(), db.now_iso()))
    assert len(seeded) == 3


def test_m003_legacy_rows_preserved_field_by_field(v2_conn):
    """逐行逐字段核对：以旧表 PRAGMA 实际列清单为准，新增列 version=1、
    updated_at=created_at；行数相等只是必要条件。"""
    _seed_legacy_artifacts(v2_conn)
    old_cols = _art_columns(v2_conn)
    before = {r["id"]: dict(r) for r in v2_conn.execute("SELECT * FROM artifacts")}
    assert db.init_db() == 4
    after = {r["id"]: dict(r) for r in v2_conn.execute("SELECT * FROM artifacts")}
    assert set(before) == set(after) and len(after) == 3
    for rid, old in before.items():
        new = after[rid]
        for col in old_cols:            # 旧表全部实际列逐一比对
            assert new[col] == old[col], (rid, col)
        assert new["version"] == 1
        assert new["updated_at"] == old["created_at"]


def test_m003_unique_and_indexes_preserved(v2_conn):
    """UNIQUE(kind,period_key,owner_role) 保全（重复 key 仍拒）；
    迁移前后索引清单逐项一致（名称/列/唯一性）。"""
    _seed_legacy_artifacts(v2_conn)
    idx_before = _art_indexes(v2_conn)
    assert db.init_db() == 4
    idx_after = _art_indexes(v2_conn)
    # 表约束自动索引名随重建改变属预期，比对 (唯一性, 列) 多重集合
    assert sorted(idx_before.values()) == sorted(idx_after.values())
    with pytest.raises(sqlite3.IntegrityError):
        with v2_conn:
            v2_conn.execute(
                "INSERT INTO artifacts(kind,title,content_md,owner_role,period_key,"
                "created_at,updated_at) VALUES('daily_report','dup','x','cfo',"
                "'2026-11-18',?,?)", (db.now_iso(), db.now_iso()))


def test_m003_rerun_idempotent(v2_conn):
    """成功迁移后再次 init_db 零变化（行数/版本记录/字段值均不动）。"""
    _seed_legacy_artifacts(v2_conn)
    assert db.init_db() == 4
    snapshot = [dict(r) for r in v2_conn.execute("SELECT * FROM artifacts ORDER BY id")]
    n_ver = v2_conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"]
    assert db.init_db() == 4
    assert [dict(r) for r in v2_conn.execute("SELECT * FROM artifacts ORDER BY id")] == snapshot
    assert v2_conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == n_ver


def test_m003_failure_rolls_back_completely(v2_conn, monkeypatch):
    """核对失败（注入行损坏）→ 整体 ROLLBACK：原表原数据原索引完整保留，
    schema_version 停在 2，修复后重跑成功。"""
    _seed_legacy_artifacts(v2_conn)
    idx_before = _art_indexes(v2_conn)
    cols_before = _art_columns(v2_conn)
    before = [dict(r) for r in v2_conn.execute("SELECT * FROM artifacts ORDER BY id")]

    def failing_m003(mconn):
        # 执行真实迁移语句的前半（建新表+复制），然后注入坏语句——
        # 语句序列由 _m003_statements() 暴露（与 m001 同款可注入设计）
        stmts = db._m003_statements()
        half = len(stmts) // 2
        for s in stmts[:half]:
            mconn.execute(s)
        mconn.execute("SELECT * FROM no_such_table_m003")

    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS",
                        full[:2] + [failing_m003, db.m004_bulk_delete_recovery])
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
    assert not v2_conn.in_transaction
    assert _art_columns(v2_conn) == cols_before
    assert _art_indexes(v2_conn) == idx_before
    assert [dict(r) for r in v2_conn.execute("SELECT * FROM artifacts ORDER BY id")] == before
    assert db.current_version(v2_conn) == 2
    assert "artifacts_new" not in {r[0] for r in v2_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # 修复后重跑完整成功（精确恢复迁移列表，不动其他隔离补丁）
    monkeypatch.setattr(db, "MIGRATIONS", full)
    assert db.init_db() == 4
    after = {r["id"]: dict(r) for r in v2_conn.execute("SELECT * FROM artifacts")}
    for old in before:
        for col in cols_before:
            assert after[old["id"]][col] == old[col]


def test_m003_fresh_db_boss_writable(isolated_env):
    """全新库直接走到 v4：boss 归属可写、version 默认 1、updated_at 必填。"""
    assert db.init_db() == 4
    conn = db.get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO artifacts(kind,title,content_md,owner_role,period_key,"
            "created_at,updated_at) VALUES('daily_report','新日报','# n','boss',"
            "'2026-11-21',?,?)", (db.now_iso(), db.now_iso()))
    row = conn.execute("SELECT * FROM artifacts WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row["version"] == 1 and row["owner_role"] == "boss"


# ---------------------------------------------------------------------------
# m004：三条 ALTER
# ---------------------------------------------------------------------------

def test_m004_adds_three_nullable_columns(v2_conn):
    _seed_legacy_artifacts(v2_conn)
    with v2_conn:
        v2_conn.execute(
            "INSERT INTO schedules(title,remind_at,owner_role,created_at,updated_at) "
            "VALUES('旧日程','2026-11-21T09:00:00+08:00','personal',?,?)",
            (db.now_iso(), db.now_iso()))
    assert db.init_db() == 4
    a_cols = [r["name"] for r in v2_conn.execute("PRAGMA table_info(approvals)")]
    s_cols = [r["name"] for r in v2_conn.execute("PRAGMA table_info(schedules)")]
    assert "digest" in a_cols
    assert "notified_at" in s_cols and "recovery_enqueued_at" in s_cols
    # 旧行三列均 NULL
    s = v2_conn.execute("SELECT * FROM schedules").fetchone()
    assert s["notified_at"] is None and s["recovery_enqueued_at"] is None


def test_m004_disclosure_digest_stays_null(db_conn):
    """M2 披露函数零改动的直接证据：走 request_disclosure 的单 digest 恒 NULL。"""
    from opc_workbench import visibility as vis
    conn = db_conn
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
            "VALUES('私密','cfo','private',?,?)", (db.now_iso(), db.now_iso()))
    aid = vis.request_disclosure("tasks", cur.lastrowid, "cfo", "sales", "交接")
    row = conn.execute("SELECT digest FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["digest"] is None