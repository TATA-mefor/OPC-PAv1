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
    assert db.current_version(db_conn) == len(db.MIGRATIONS) == 4  # M3: +m003/m004


def test_init_idempotent_rerun(db_conn):
    """重复 init_db 不重复建表、不重复记版本、不丢数据。"""
    with db_conn:
        db_conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) VALUES(?,?,?,?)",
            ("保留我", "sales", db.now_iso(), db.now_iso()),
        )
    v = db.init_db()
    assert v == 4
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 1
    assert db_conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 4


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
    # M3 m003 后 artifacts 新增 updated_at NOT NULL（version 有默认值）
    args = ("INSERT INTO artifacts(kind,title,content_md,owner_role,period_key,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)")
    with db_conn:
        db_conn.execute(args, ("daily_report", "d", "x", "cfo", "2026-11-19",
                               db.now_iso(), db.now_iso()))
    with pytest.raises(sqlite3.IntegrityError):
        with db_conn:
            db_conn.execute(args, ("daily_report", "d2", "y", "cfo", "2026-11-19",
                                   db.now_iso(), db.now_iso()))


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


def test_m001_midway_failure_full_rollback(isolated_env, monkeypatch):
    """M1 审查修复：初始建表迁移中途失败 → 所有业务表/索引/版本记录全部回滚。"""
    real_stmts = db._m001_statements()
    assert len(real_stmts) >= 13  # 7 表 + 6 索引
    cut = len(real_stmts) // 2    # 在 tasks/schedules 等已建、其余未建时炸

    def failing_statements():
        return real_stmts[:cut] + ["CREATE TABLE boom (id INTEGER"]  # 故意语法错误

    monkeypatch.setattr(db, "_m001_statements", failing_statements)
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()

    conn = db.get_conn()
    assert not conn.in_transaction
    # 前半截已执行的建表也必须回滚干净
    assert _tables(conn) == {"schema_version"}
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    assert idx == set()
    assert db.current_version(conn) == 0


def test_m001_rerun_after_failure_succeeds(isolated_env, monkeypatch):
    """失败回滚后 init_db 可重新完整执行（审查要求的复跑验证）。"""
    real_stmts = db._m001_statements()
    calls = {"n": 0}

    def flaky_statements():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_stmts[:3] + ["SELECT * FROM no_such_table"]
        return real_stmts

    monkeypatch.setattr(db, "_m001_statements", flaky_statements)
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
    assert db.current_version(db.get_conn()) == 0

    v = db.init_db()  # 第二次完整成功
    assert v == 4
    conn = db.get_conn()
    assert EXPECTED_TABLES <= _tables(conn)
    for table in EXPECTED_TABLES - {"schema_version"}:
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0


def test_executescript_rejected_before_execution(isolated_env, monkeypatch):
    """M1 复审修复（第一道防线）：executescript 在执行前被拒绝。

    断言：脚本未执行（无 sneaky 表）、外层事务完整回滚、版本不推进、
    数据库处于可安全重试状态。
    """
    executed = {"script_ran": False}

    def sneaky(conn):
        # 受控接口应在属性访问时即抛错，脚本根本没有机会执行
        conn.executescript("CREATE TABLE sneaky (id INTEGER);")
        executed["script_ran"] = True  # 不应到达

    monkeypatch.setattr(db, "MIGRATIONS", [db.m001_init, sneaky])
    with pytest.raises(RuntimeError, match="migration_forbidden_api:executescript"):
        db.init_db()

    conn = db.get_conn()
    assert executed["script_ran"] is False
    assert not conn.in_transaction
    assert "sneaky" not in _tables(conn)          # 无残留
    assert db.current_version(conn) == 1           # m001 成功，坏迁移未记录
    # 可安全重试：修复迁移后 init_db 正常完成
    monkeypatch.setattr(db, "MIGRATIONS", [db.m001_init, db.m002_task_idempotency])
    assert db.init_db() == 2
    assert "sneaky" not in _tables(conn)


def test_migration_forbidden_apis(isolated_env, monkeypatch):
    """受控接口：commit/rollback/close/属性写入均在执行前被拒绝，事务可回滚。"""
    for api in ("commit", "rollback", "close"):
        def bad(conn, _api=api):
            getattr(conn, _api)()

        monkeypatch.setattr(db, "MIGRATIONS", [bad])
        with pytest.raises(RuntimeError, match=f"migration_forbidden_api:{api}"):
            db.init_db()
        conn = db.get_conn()
        assert db.current_version(conn) == 0
        assert _tables(conn) == {"schema_version"}

    def write_attr(conn):
        conn.row_factory = None

    monkeypatch.setattr(db, "MIGRATIONS", [write_attr])
    with pytest.raises(RuntimeError, match="migration_forbidden_attr_write"):
        db.init_db()
    assert db.current_version(db.get_conn()) == 0


def test_retry_after_forbidden_api_succeeds(isolated_env, monkeypatch):
    """拒绝后重试完整成功（复审要求的可重试性验证，从空库开始）。"""
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            conn.executescript("CREATE TABLE sneaky (id INTEGER);")
        conn.execute(
            "CREATE TABLE retry_ok (id INTEGER PRIMARY KEY, note TEXT NOT NULL)"
        )

    monkeypatch.setattr(db, "MIGRATIONS", [flaky])
    with pytest.raises(RuntimeError, match="migration_forbidden_api"):
        db.init_db()
    conn = db.get_conn()
    assert "sneaky" not in _tables(conn) and "retry_ok" not in _tables(conn)
    assert db.current_version(conn) == 0

    assert db.init_db() == 1  # 第二次完整成功
    assert "retry_ok" in _tables(conn)
    assert "sneaky" not in _tables(conn)


def test_bypass_via_private_conn_rejected(isolated_env, monkeypatch):
    """复审阻断项回归①：conn._conn 取裸连接 → 属性访问即被拒，无残留可重试。"""
    def bypass(conn):
        conn._conn.executescript("CREATE TABLE sneaky (id INTEGER);")

    monkeypatch.setattr(db, "MIGRATIONS", [db.m001_init, bypass])
    with pytest.raises(RuntimeError, match="migration_forbidden_api:_conn"):
        db.init_db()
    conn = db.get_conn()
    assert not conn.in_transaction
    assert "sneaky" not in _tables(conn)
    assert db.current_version(conn) == 1
    # 修复后可安全重试
    monkeypatch.setattr(db, "MIGRATIONS", [db.m001_init, db.m002_task_idempotency])
    assert db.init_db() == 2
    assert "sneaky" not in _tables(conn)


def test_bypass_via_cursor_connection_rejected(isolated_env, monkeypatch):
    """复审阻断项回归②：execute(...).connection 取裸连接 → 访问即被拒。"""
    def bypass(conn):
        conn.execute("SELECT 1").connection.executescript(
            "CREATE TABLE sneaky (id INTEGER);")

    monkeypatch.setattr(db, "MIGRATIONS", [db.m001_init, bypass])
    with pytest.raises(RuntimeError, match="migration_forbidden_cursor_api:connection"):
        db.init_db()
    conn = db.get_conn()
    assert not conn.in_transaction
    assert "sneaky" not in _tables(conn)
    assert db.current_version(conn) == 1
    monkeypatch.setattr(db, "MIGRATIONS", [db.m001_init, db.m002_task_idempotency])
    assert db.init_db() == 2
    assert "sneaky" not in _tables(conn)


def test_migration_cursor_proxy_usable(isolated_env, monkeypatch):
    """受控游标正常接口可用（防过度封锁）：fetchone/fetchall/rowcount/lastrowid。"""
    seen = {}

    def probe(conn):
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, v TEXT)")
        cur = conn.execute("INSERT INTO probe(v) VALUES('a')")
        seen["lastrowid"] = cur.lastrowid
        seen["rowcount"] = cur.rowcount
        seen["one"] = conn.execute("SELECT v FROM probe").fetchone()[0]
        seen["all"] = conn.execute("SELECT v FROM probe").fetchall()
        # 游标上的危险接口同样被拒（executescript 不在游标白名单）
        try:
            conn.execute("SELECT 1").executescript
            raise AssertionError("游标 executescript 应被拒绝")
        except RuntimeError:
            pass

    monkeypatch.setattr(db, "MIGRATIONS", [probe])
    assert db.init_db() == 1
    assert seen["lastrowid"] == 1
    assert seen["rowcount"] == 1
    assert seen["one"] == "a"
    assert len(seen["all"]) == 1


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
