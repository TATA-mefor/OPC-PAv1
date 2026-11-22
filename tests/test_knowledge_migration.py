"""m005 迁移与受控可见性视图（M4 第二批 C1/C2）。

m005 内容已审定、不再改动；B3/B4 的快照列与删除代次表由独立 m006 承接，
其迁移断言见 tests/test_knowledge_race_migration.py。
"""
from __future__ import annotations

import sqlite3

import pytest

from opc_workbench import config, db, visibility


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_m005_applied_and_version(db_conn):
    assert db.current_version(db_conn) == 6
    names = [r["name"] for r in db_conn.execute(
        "SELECT name FROM schema_version ORDER BY version")]
    assert names[-2:] == ["m005_knowledge_index", "m006_knowledge_race_fix"]


def test_m005_adds_index_version_columns(db_conn):
    cols = _cols(db_conn, "knowledge_docs")
    assert {"source_hash", "indexed_hash", "embed_model", "embed_dim",
            "updated_at", "last_error"} <= cols


def test_m005_keeps_status_four_states(db_conn):
    """C1/C2 边界：不重建表，status CHECK 仍是四态。"""
    sql = db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='knowledge_docs'").fetchone()["sql"]
    assert "'processing','ready','failed','missing'" in sql
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO knowledge_docs(filename,stored_path,file_hash,owner_role,"
            "created_at,status) VALUES('a','cfo/a.md','sha256:x','cfo','t','deleting')")
        db_conn.commit()


def test_m005_no_content_dedup_unique_index(db_conn):
    """C2：不得存在 (owner_role,file_hash) 唯一索引。"""
    idx = db_conn.execute("PRAGMA index_list(knowledge_docs)").fetchall()
    for row in idx:
        cols = [r["name"] for r in db_conn.execute(f"PRAGMA index_info({row['name']})")]
        assert not (row["unique"] and set(cols) == {"owner_role", "file_hash"})


def test_m005_backfills_existing_rows_as_indexed(tmp_path):
    """旧库已有行回填 indexed_hash=file_hash，绝不触发历史重嵌入。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    db._ensure_version_table(conn)
    for i, fn in enumerate(db.MIGRATIONS[:4], start=1):
        fn(db.MigrationConnection(conn))
        conn.execute("INSERT INTO schema_version(version,name,applied_at) VALUES(?,?,?)",
                     (i, fn.__name__, db.now_iso()))
    conn.execute(
        "INSERT INTO knowledge_docs(filename,stored_path,file_hash,owner_role,"
        "created_at) VALUES('a.md','cfo/a.md','sha256:abc','cfo','2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    db.init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM knowledge_docs").fetchone()
    conn.close()
    assert row["indexed_hash"] == "sha256:abc"
    assert row["source_hash"] == "sha256:abc"
    assert row["status"] == "ready"


def test_intent_tables_created(db_conn):
    tables = {r["name"] for r in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pending_deletions", "pending_vault_writes"} <= tables


def test_view_exposes_governance_columns_from_docs(db_conn):
    db_conn.execute(
        "INSERT INTO knowledge_docs(filename,stored_path,file_hash,owner_role,"
        "visibility,created_at,status) VALUES('a.md','cfo/a.md','sha256:x','cfo',"
        "'private','t','ready')")
    doc_id = db_conn.execute("SELECT id FROM knowledge_docs").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO knowledge_chunks(doc_id,seq,content,embedding) VALUES(?,0,'c',?)",
        (doc_id, b"\x00\x00\x80?"))
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM knowledge_chunks_visible").fetchone()
    assert row["owner_role"] == "cfo"
    assert row["visibility"] == "private"
    assert row["stored_path"] == "cfo/a.md"


@pytest.mark.parametrize("sql", [
    "INSERT INTO knowledge_chunks_visible(doc_id,seq,content,embedding) "
    "VALUES(1,0,'x',x'00')",
    "UPDATE knowledge_chunks_visible SET content='x'",
    "DELETE FROM knowledge_chunks_visible",
])
def test_view_is_not_writable(db_conn, sql):
    """C1 附加要求：视图不得成为写入口。"""
    with pytest.raises(sqlite3.OperationalError):
        db_conn.execute(sql)


def test_view_in_queryable_whitelist_but_base_table_not():
    assert "knowledge_chunks_visible" in visibility._QUERYABLE
    with pytest.raises(ValueError, match="forbidden_table"):
        visibility.query_visible("knowledge_chunks", "boss")


def test_query_visible_filters_chunks_by_caller(db_conn):
    """治理原语对 chunk 生效：他角色 private 不可见，boss 全可见。"""
    for owner, vis, path in (("cfo", "private", "cfo/p.md"),
                             ("sales", "public", "sales/s.md")):
        db_conn.execute(
            "INSERT INTO knowledge_docs(filename,stored_path,file_hash,owner_role,"
            "visibility,created_at,status) VALUES(?,?,?,?,?,'t','ready')",
            (path, path, "sha256:x", owner, vis))
        doc_id = db_conn.execute(
            "SELECT id FROM knowledge_docs WHERE stored_path=?", (path,)).fetchone()["id"]
        db_conn.execute(
            "INSERT INTO knowledge_chunks(doc_id,seq,content,embedding) VALUES(?,0,?,?)",
            (doc_id, f"body-{owner}", b"\x00\x00\x80?"))
    db_conn.commit()

    sales = visibility.query_visible("knowledge_chunks_visible", "sales")
    assert [r["content"] for r in sales] == ["body-sales"]
    cfo = visibility.query_visible("knowledge_chunks_visible", "cfo")
    assert {r["content"] for r in cfo} == {"body-cfo", "body-sales"}
    boss = visibility.query_visible("knowledge_chunks_visible", "boss")
    assert len(boss) == 2
    coordinator = visibility.query_visible("knowledge_chunks_visible", "coordinator")
    assert [r["content"] for r in coordinator] == ["body-sales"]


def test_migration_is_repeatable_and_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    assert db.init_db(path) == 6
    assert db.init_db(path) == 6


def test_m005_rollback_leaves_no_partial_schema(tmp_path, monkeypatch):
    """迁移中途失败 → 整事务回滚，版本不推进，可安全重试。"""
    path = tmp_path / "broken.db"
    reachable = list(db.MIGRATIONS)

    def broken(conn):
        conn.execute("ALTER TABLE knowledge_docs ADD COLUMN source_hash TEXT")
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", reachable[:4] + [broken])
    with pytest.raises(RuntimeError, match="boom"):
        db.init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    assert db.current_version(conn) == 4
    assert "source_hash" not in {r["name"] for r in
                                 conn.execute("PRAGMA table_info(knowledge_docs)")}
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS", reachable)
    assert db.init_db(path) == 6