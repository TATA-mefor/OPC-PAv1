"""m006_knowledge_race_fix 迁移（M4 第二批 B3/B4）。

m005 保持已审定内容不变：本文件只验证独立 m006 的产物与升级/回滚边界。
"""
from __future__ import annotations

import sqlite3

import pytest

from opc_workbench import db


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn):
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_m006_artifacts(db_conn):
    """B3/B4：快照标识列与删除代次表必须存在（代次表主键是 stored_path）。"""
    assert "snapshot_hash" in _cols(db_conn, "knowledge_docs")
    assert _cols(db_conn, "vault_path_state") == {"stored_path", "generation", "updated_at"}
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute("INSERT INTO vault_path_state(stored_path,generation,updated_at) "
                        "VALUES('cfo/a.md',1,'t')")
        db_conn.execute("INSERT INTO vault_path_state(stored_path,generation,updated_at) "
                        "VALUES('cfo/a.md',2,'t')")
        db_conn.commit()


def test_m006_upgrades_v5_library(tmp_path):
    """v5 旧库（含历史行）升级到 v6：新列可空、墓碑表空、历史行不被改写。"""
    path = tmp_path / "v5.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    db._ensure_version_table(conn)
    for i, fn in enumerate(db.MIGRATIONS[:5], start=1):
        fn(db.MigrationConnection(conn))
        conn.execute("INSERT INTO schema_version(version,name,applied_at) VALUES(?,?,?)",
                     (i, fn.__name__, db.now_iso()))
    conn.execute(
        "INSERT INTO knowledge_docs(filename,stored_path,file_hash,owner_role,"
        "visibility,created_at,status,source_hash,indexed_hash) "
        "VALUES('a.md','cfo/a.md','sha256:abc','cfo','private','t','ready',"
        "'sha256:abc','sha256:abc')")
    conn.commit()
    assert db.current_version(conn) == 5
    conn.close()

    assert db.init_db(path) == 6
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    assert "snapshot_hash" in _cols(conn, "knowledge_docs")
    assert "vault_path_state" in _tables(conn)
    row = conn.execute("SELECT * FROM knowledge_docs WHERE stored_path='cfo/a.md'").fetchone()
    assert row["snapshot_hash"] is None          # 历史行不猜快照，下轮 ingest 补写
    assert row["visibility"] == "private"        # 升级不改权限
    assert conn.execute("SELECT COUNT(*) c FROM vault_path_state").fetchone()["c"] == 0
    assert db.current_version(conn) == 6
    conn.close()

    assert db.init_db(path) == 6                 # 幂等：重复初始化不再推进


def test_m006_rollback_leaves_no_partial_schema(tmp_path, monkeypatch):
    """m006 中途失败 → 整个 m006 回滚，版本停在 5，可原地重试。"""
    path = tmp_path / "broken6.db"
    reachable = list(db.MIGRATIONS)

    def broken(conn):
        conn.execute("ALTER TABLE knowledge_docs ADD COLUMN snapshot_hash TEXT")
        raise RuntimeError("boom6")

    monkeypatch.setattr(db, "MIGRATIONS", reachable[:5] + [broken])
    with pytest.raises(RuntimeError, match="boom6"):
        db.init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    assert db.current_version(conn) == 5
    assert "snapshot_hash" not in _cols(conn, "knowledge_docs")
    assert "vault_path_state" not in _tables(conn)
    conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", reachable)
    assert db.init_db(path) == 6
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    assert "snapshot_hash" in _cols(conn, "knowledge_docs")
    assert "vault_path_state" in _tables(conn)
    conn.close()
