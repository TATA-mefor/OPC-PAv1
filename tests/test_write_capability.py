"""生产写能力隔离专项（M3.1 清单 v10.2 §三.3，九项验收）。

安全依据 = 对象所有权与依赖注入（非命名/导入顺序/一次性标志）：
- WriteCapability 不可直构；生产 writer 只由组合根在 build_application 内部铸造；
- db.py 写路径无模块级 get_conn()（读连接 query_only，物理拒绝写 SQL）；
- 自建 CASService + 伪造 authorize 也没有生产库可写（端到端负向实测）。

本阶段 CASService 仅交付构造契约与 write_where 纯函数；
cas_update/cas_delete 写入行为在身份链落地后实现（顺序 1 后半段）。
"""
from __future__ import annotations

import sqlite3

import pytest

from opc_workbench import app, config, db


def _make_writer(db_path):
    return db._open_test_writer(db_path)


# ---------------------------------------------------------------------------
# ⑦ WriteCapability 直构 → PermissionError
# ---------------------------------------------------------------------------

def test_write_capability_direct_construction_forbidden():
    with pytest.raises(PermissionError):
        db.WriteCapability()
    with pytest.raises(PermissionError):
        db.WriteCapability(sqlite3.connect(":memory:"))


def test_write_capability_attribute_tamper_forbidden():
    w = _make_writer(config.DB_PATH)
    with pytest.raises((AttributeError, PermissionError)):
        w._conn = sqlite3.connect(":memory:")
    w.close()


# ---------------------------------------------------------------------------
# ② 无公开工厂：db 写路径无模块级 get_conn；生产 writer 一次性
# ---------------------------------------------------------------------------

def test_db_module_has_no_public_writer_factory():
    """无公开写能力工厂；生产/测试 writer 仅内部入口。

    兼容口径（v10.3 执行说明）：get_conn 保留为 M2 遗留治理连接
    （M2 披露函数零改动 + 既有测试 monkeypatch 依赖）；M3 读路径走
    query_only 的 get_read_conn；M3 写路径只经注入的 WriteCapability，
    零 get_conn 调用（由架构回归测试看守）。
    """
    assert callable(db.get_read_conn)
    assert not hasattr(db, "open_writer")          # 无公开写工厂
    assert not hasattr(db, "get_write_conn")
    assert not hasattr(db, "_open_production_writer")   # M3.2①：入口已删除
    assert not hasattr(db, "_mint_writer")              # 无守卫铸造入口已删除
    assert callable(db._open_test_writer)


def test_production_writer_only_via_composition_root(isolated_env):
    """M3.2①：生产 writer 只能经组合根取得；重复装配 fail fast。

    原 _open_production_writer 的"首个调用者赢"语义已废除——一次性锁
    只拦第二个调用者，不能证明第一个是组合根。
    """
    a = app.build_application()
    assert isinstance(a.writer, db.WriteCapability)
    assert a.writer.cap_id == "production"
    with pytest.raises(RuntimeError, match="production_writer_already_taken"):
        app.build_application()
    a.writer.close()


def test_production_writer_failure_fail_fast(isolated_env, monkeypatch):
    """⑨ 组合根取得 writer 失败 → 抛错，不静默降级返回 None。"""
    def boom(*a, **kw):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(app.sqlite3, "connect", boom)
    with pytest.raises(sqlite3.OperationalError):
        app.build_application()


# ---------------------------------------------------------------------------
# ⑤ 读连接物理只读：业务模块经读路径无法执行写 SQL
# ---------------------------------------------------------------------------

def test_read_conn_rejects_write_sql(db_conn):
    """query_only 读连接：INSERT/UPDATE/DELETE/DDL 一律 OperationalError，
    生产库内容不变（实际调用断言，非仅属性断言）。"""
    conn = db.get_read_conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('越权','sales',?,?)", (db.now_iso(), db.now_iso()))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE tasks SET title='篡改'")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM tasks")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE sneaky (id INTEGER)")
    assert conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


def test_read_conn_reads_committed_writes(db_conn):
    """读连接可见 writer 已提交的数据（WAL 正常读路径不受隔离影响）。"""
    w = _make_writer(config.DB_PATH)
    with w.transaction() as cur:
        cur.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('经writer写入','sales',?,?)", (db.now_iso(), db.now_iso()))
    rows = db.get_read_conn().execute("SELECT title FROM tasks").fetchall()
    assert [r["title"] for r in rows] == ["经writer写入"]
    w.close()


# ---------------------------------------------------------------------------
# writer 事务原语：BEGIN IMMEDIATE / COMMIT / ROLLBACK
# ---------------------------------------------------------------------------

def test_writer_transaction_commit_and_rollback(db_conn):
    w = _make_writer(config.DB_PATH)
    with w.transaction() as cur:
        cur.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('保留','sales',?,?)", (db.now_iso(), db.now_iso()))
    try:
        with w.transaction() as cur:
            cur.execute(
                "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
                "VALUES('回滚','sales',?,?)", (db.now_iso(), db.now_iso()))
            raise RuntimeError("中途失败")
    except RuntimeError:
        pass
    titles = [r["title"] for r in
              db.get_read_conn().execute("SELECT title FROM tasks ORDER BY id")]
    assert titles == ["保留"]
    assert not w.in_transaction
    w.close()


def test_test_writer_physically_isolated_from_production(isolated_env, tmp_path):
    """④ _open_test_writer 打开独立库：写测试库，生产库文件零变化。"""
    db.init_db()   # 生产（tmp 重定向）库就位
    prod_path = config.DB_PATH
    prod_bytes = prod_path.read_bytes()
    other = tmp_path / "elsewhere" / "isolated.db"
    w = db._open_test_writer(other)
    with w.transaction() as cur:
        cur.execute(
            "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
            "VALUES('独立库','sales',?,?)", (db.now_iso(), db.now_iso()))
    w.close()
    assert other.exists()
    check = sqlite3.connect(str(other))
    try:
        assert check.execute("SELECT COUNT(*) c FROM tasks").fetchone()[0] == 1
    finally:
        check.close()
    assert prod_path.read_bytes() == prod_bytes   # 生产库字节级零变化


def test_test_writer_unlimited_instances(isolated_env, tmp_path):
    """测试 writer 可多次创建（并发用例需要），互不影响。"""
    w1 = db._open_test_writer(tmp_path / "a.db")
    w2 = db._open_test_writer(tmp_path / "b.db")
    assert w1 is not w2
    w1.close()
    w2.close()