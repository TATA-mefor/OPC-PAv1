"""M1 迁移框架与 DDL 验收（PRD §4）。"""
from __future__ import annotations

import sqlite3

import pytest

from opc_workbench import config, db

EXPECTED_TABLES = {
    "schema_version", "tasks", "schedules", "knowledge_docs",
    "knowledge_chunks", "memory", "artifacts", "approvals",
}


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def test_init_creates_all_tables(db_conn):
    assert EXPECTED_TABLES <= _tables(db_conn)
    assert db.current_version(db_conn) == len(db.MIGRATIONS) == 2


def test_init_idempotent_rerun(db_conn):
    """重复 init_db 不重复建表、不重复记版本、不丢数据。"""
    with db_conn:
        db_conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) VALUES(?,?,?,?)",
            ("保留我", "sales", db.now_iso(), db.now_iso()),
        )
    v = db.init_db()
    assert v == 2
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 1
    assert db_conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 2


def test_zero_seeding(db_conn):
    """审计红线：init_db 零播种，任何业务表初始必须为空。"""
    for table in EXPECTED_TABLES - {"schema_version"}:
        assert db_conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0, table


def test_role_check_constraint(db_conn):
    """角色枚举由 config 生成：未知角色拒写，10 个合法角色可写。"""
    with pytest.raises(sqlite3.IntegrityError):
        with db_conn:
            db_conn.execute(
                "INSERT INTO tasks(title,owner_role,created_at,updated_at) VALUES(?,?,?,?)",
                ("x", "work", db.now_iso(), db.now_iso()),  # 旧三角色残留，必须拒绝
            )
    for role in config.ROLE_ORDER:
        with db_conn:
            db_conn.execute(
                "INSERT INTO tasks(title,owner_role,created_at,updated_at) VALUES(?,?,?,?)",
                (f"ok-{role}", role, db.now_iso(), db.now_iso()),
            )
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 10


def test_visibility_internal_rejected(db_conn):
    """internal 枚举预留：V1 写入必须被 CHECK 拒绝（PRD §3.1）。"""
    with pytest.raises(sqlite3.IntegrityError):
        with db_conn:
            db_conn.execute(
                "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                ("x", "sales", "internal", db.now_iso(), db.now_iso()),
            )


def test_scope_session_rejected(db_conn):
    """session 数据不落库：scope CHECK 仅允许 global（PRD §3.1）。"""
    with pytest.raises(sqlite3.IntegrityError):
        with db_conn:
            db_conn.execute(
                "INSERT INTO tasks(title,owner_role,scope,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                ("x", "sales", "session", db.now_iso(), db.now_iso()),
            )


def test_idempotency_key_unique(db_conn):
    """m002：幂等键唯一，NULL 可多条。"""
    args = ("INSERT INTO tasks(title,owner_role,idempotency_key,created_at,updated_at) "
            "VALUES(?,?,?,?,?)")
    with db_conn:
        db_conn.execute(args, ("a", "sales", "k1", db.now_iso(), db.now_iso()))
    with pytest.raises(sqlite3.IntegrityError):
        with db_conn:
            db_conn.execute(args, ("b", "sales", "k1", db.now_iso(), db.now_iso()))
    for i in range(3):
        with db_conn:
            db_conn.execute(args, (f"n{i}", "sales", None, db.now_iso(), db.now_iso()))


def test_artifact_period_unique(db_conn):
    args = ("INSERT INTO artifacts(kind,title,content_md,owner_role,period_key,created_at) "
            "VALUES(?,?,?,?,?,?)")
    with db_conn:
        db_conn.execute(args, ("daily_report", "d", "x", "cfo", "2026-11-19", db.now_iso()))
    with pytest.raises(sqlite3.IntegrityError):
        with db_conn:
            db_conn.execute(args, ("daily_report", "d2", "y", "cfo", "2026-11-19", db.now_iso()))


def test_migration_failure_rolls_back(isolated_env, monkeypatch):
    """迁移单事务：中途失败不留半截表、版本不推进。"""
    def bad_migration(conn):
        conn.execute("CREATE TABLE half_done (id INTEGER)")
        raise RuntimeError("模拟迁移中途失败")

    monkeypatch.setattr(db, "MIGRATIONS", [db.m001_init, bad_migration])
    with pytest.raises(RuntimeError):
        db.init_db()
    conn = db.get_conn()
    assert "half_done" not in _tables(conn)
    assert db.current_version(conn) == 1  # m001 已成功独立提交，坏迁移未记录


def test_backup_readable(isolated_env, tmp_path):
    db.init_db()
    conn = db.get_conn()
    with conn:
        conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) VALUES(?,?,?,?)",
            ("备份验证", "personal", db.now_iso(), db.now_iso()),
        )
    dest = db.backup(tmp_path / "backups" / "b.db")
    check = sqlite3.connect(str(dest))
    try:
        assert check.execute("SELECT title FROM tasks").fetchone()[0] == "备份验证"
    finally:
        check.close()
