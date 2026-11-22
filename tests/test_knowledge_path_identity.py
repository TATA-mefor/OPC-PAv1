"""B5：路径身份唯一（M4 第二批第三轮复审阻断）。

问题：`resolve_safe_path` 只回绝对 Path，DB 却以相对串为身份。若直接把调用方
原始串落库，`cfo\\a.md` 与 `cfo/a.md` 会解析到同一文件、却是两个 DB 身份：
反斜杠导入后用正斜杠删除，盘上文件被删、文档行与 chunks 查不到而残留，
删除代次墓碑也会分裂成两条（B4 的保护因此可被路径别名绕过）。

回归口径：所有公开入口先经 `vault.normalize_relpath` 归一，再读写文件/落库；
反斜杠导入 → 正斜杠删除必须命中同一条身份，文件、文档行、chunks、墓碑全部
落在同一个规范路径上。
"""
from __future__ import annotations

import pytest

from opc_workbench import db, vault


def _rows(sql, params=()):
    return db.get_conn().execute(sql, params).fetchall()


def _doc(rel):
    rows = _rows("SELECT * FROM knowledge_docs WHERE stored_path=?", (rel,))
    return rows[0] if rows else None


def _chunks(doc_id):
    return _rows("SELECT * FROM knowledge_chunks WHERE doc_id=? ORDER BY seq", (doc_id,))


def _paths():
    return {r["stored_path"] for r in
            _rows("SELECT stored_path FROM knowledge_docs")}


@pytest.mark.parametrize("raw,canon", [
    ("cfo/a.md", "cfo/a.md"),
    ("cfo\\a.md", "cfo/a.md"),
    ("cfo\\sub\\a.md", "cfo/sub/a.md"),
    ("./cfo/a.md", "cfo/a.md"),
    (".\\cfo\\a.md", "cfo/a.md"),
    ("cfo//a.md", "cfo/a.md"),
    ("cfo/./a.md", "cfo/a.md"),
])
def test_normalize_relpath_is_canonical(raw, canon):
    assert vault.normalize_relpath(raw) == canon
    assert vault.resolve_safe_path(vault_placeholder_root(), raw).name == "a.md"


@pytest.mark.parametrize("bad", [
    "", "..", "cfo/../a.md", "cfo\\..\\a.md", "/abs.md", "\\abs.md",
    "C:\\a.md", "C:a.md", "\\\\server\\share\\a.md", "cfo/a.md:stream", "cfo/\x00.md",
])
def test_normalize_relpath_rejects_unsafe(bad):
    with pytest.raises(ValueError, match="vault_path_unsafe"):
        vault.normalize_relpath(bad)


def vault_placeholder_root():
    """归一化是纯字符串操作；这里的根只用于跑通 resolve_safe_path。"""
    from opc_workbench import config
    return config.VAULT_PATH


# --- 服务层：同一身份 -------------------------------------------------------


def test_backslash_ingest_then_posix_delete_clears_everything(
        kservice, isolated_env, write_md, kboss_ident):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "路径身份正文")

    out = kservice.ingest("cfo\\a.md")                 # Windows 写法导入
    assert out["status"] == "ready"
    assert _doc("cfo/a.md") is not None                 # 只落规范键
    assert _paths() == {"cfo/a.md"}
    assert len(_chunks(out["doc_id"])) >= 1

    kservice.delete("cfo/a.md", identity=kboss_ident)  # POSIX 写法删除
    assert not (root / "cfo" / "a.md").exists()
    assert _doc("cfo/a.md") is None
    assert _chunks(out["doc_id"]) == []
    assert _paths() == set()
    assert _rows("SELECT * FROM pending_deletions") == []

    # 墓碑落在同一规范路径（否则下一条 B4 保护会被别名绕过）
    tombstones = _rows("SELECT stored_path,generation FROM vault_path_state")
    assert [(r["stored_path"], r["generation"]) for r in tombstones] == [("cfo/a.md", 1)]


def test_dot_segment_ingest_does_not_duplicate_doc(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "同一文档")
    first = kservice.ingest("cfo/a.md")
    second = kservice.ingest("./cfo//a.md")
    assert second["doc_id"] == first["doc_id"]
    assert second["reindexed"] is False
    assert _paths() == {"cfo/a.md"}


def test_write_doc_backslash_then_delete_posix(kservice, isolated_env, kboss_ident):
    """写盘路径同样归一：意图表键、文件、索引身份必须一致。"""
    from opc_workbench.knowledge_service import body_hash

    root = isolated_env["vault_dir"]
    meta = {"visibility": "private", "scope": "global", "owner_role": "cfo",
            "file_hash": body_hash("写盘正文"),
            "updated_at": "2026-01-01T00:00:00+00:00"}
    out = kservice.write_doc("cfo\\w.md", meta, "写盘正文")
    assert out["status"] == "ready"
    assert (root / "cfo" / "w.md").exists()
    assert _paths() == {"cfo/w.md"}
    assert _rows("SELECT * FROM pending_vault_writes") == []

    kservice.delete("cfo/w.md", identity=kboss_ident)
    assert not (root / "cfo" / "w.md").exists()
    assert _paths() == set()


def test_mark_missing_by_alias_hits_canonical_row(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "missing 别名")
    kservice.ingest("sales/s.md")
    (root / "sales" / "s.md").unlink()
    assert kservice.mark_missing("sales\\s.md")["missing"] is True
    assert _doc("sales/s.md")["status"] == "missing"


def test_watcher_snapshot_uses_canonical_keys(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "watcher 键")
    snap = kservice.snapshot()
    assert set(snap) == {"cfo/a.md"}
