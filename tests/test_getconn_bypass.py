"""get_conn() 生产写旁路负向专项（用户裁决 2026-11-21：安全边界优先）。

验收口径：普通模块直接调用 db.get_conn() 取得连接后执行
INSERT/UPDATE/DELETE/DDL → 一律被拒绝，生产数据内容与 version 零变化。
get_conn 保留兼容符号但必须是物理只读；M2 披露写操作经内部受控写能力。
"""
from __future__ import annotations

import sqlite3

import pytest

from opc_workbench import config, db, visibility


def _seed(conn):
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
            "VALUES('目标行','sales','public',?,?)", (db.now_iso(), db.now_iso()))
    return cur.lastrowid


def test_getconn_insert_rejected_data_unchanged(db_conn):
    rid = _seed(db_conn)
    before = dict(db_conn.execute("SELECT * FROM tasks WHERE id=?", (rid,)).fetchone())
    conn = db.get_conn()   # 普通模块直调兼容符号
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('旁路写入','sales',?,?)", (db.now_iso(), db.now_iso()))
    after = dict(db_conn.execute("SELECT * FROM tasks WHERE id=?", (rid,)).fetchone())
    assert after == before
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 1


def test_getconn_update_delete_ddl_rejected(db_conn):
    rid = _seed(db_conn)
    before = dict(db_conn.execute("SELECT * FROM tasks WHERE id=?", (rid,)).fetchone())
    conn = db.get_conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE tasks SET title='篡改', version=version+1 WHERE id=?", (rid,))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM tasks WHERE id=?", (rid,))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE sneaky (id INTEGER)")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DROP TABLE tasks")
    after = dict(db_conn.execute("SELECT * FROM tasks WHERE id=?", (rid,)).fetchone())
    assert after == before   # 内容与 version 均未变


def test_getconn_explicit_production_path_also_readonly(db_conn):
    """显式传生产路径同样只读（不能借参数绕过）。"""
    conn = db.get_conn(config.DB_PATH)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('x','sales',?,?)", (db.now_iso(), db.now_iso()))


def test_getconn_write_transaction_cannot_begin(db_conn):
    """BEGIN IMMEDIATE 写事务在只读连接上不可开启。"""
    conn = db.get_conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("BEGIN IMMEDIATE")


def test_test_writer_refuses_production_path(isolated_env):
    """_open_test_writer 不得指向真实生产库默认路径（防借测试工厂取生产写能力）。

    守卫基准是模块加载时固定的默认路径快照；测试重定向后的 tmp 路径合法。
    """
    db.init_db()
    with pytest.raises(PermissionError):
        db._open_test_writer(db._PRODUCTION_DB_DEFAULT)
    # 对照：重定向后的测试路径可正常取得写能力
    w = db._open_test_writer(config.DB_PATH)
    w.close()


def test_m2_disclosure_still_writes_via_controlled_capability(db_conn, disclosure):
    """兼容改造后 M2 披露全链仍正常写（语义不变的直接证据）。"""
    rid = _seed(db_conn)
    with db_conn:
        db_conn.execute("UPDATE tasks SET visibility='private', owner_role='cfo' "
                        "WHERE id=?", (rid,))
    aid = disclosure.request_disclosure("tasks", rid, "cfo", "sales", "交接")
    assert isinstance(aid, int)
    disclosure.resolve_disclosure(aid, "approved", actor="boss")
    got = disclosure.consume_grant(aid, "sales")
    assert got["title"] == "目标行"
    assert db_conn.execute("SELECT status FROM approvals WHERE id=?",
                           (aid,)).fetchone()["status"] == "expired"