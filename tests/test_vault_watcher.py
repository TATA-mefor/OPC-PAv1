"""vault_watcher（M4 第二批）：增量扫描、启停幂等、失败隔离。"""
from __future__ import annotations

import threading

from opc_workbench import config, db
from opc_workbench.vault_watcher import VaultWatcher


def _doc(rel):
    rows = db.get_conn().execute(
        "SELECT * FROM knowledge_docs WHERE stored_path=?", (rel,)).fetchall()
    return rows[0] if rows else None


def test_constructor_does_not_start_thread(kservice):
    w = VaultWatcher(kservice, interval_s=0.01)
    assert w.running is False
    assert not any(t.name == "vault-watcher" for t in threading.enumerate())


def test_scan_once_ingests_added_and_changed(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    w = VaultWatcher(kservice, interval_s=0.01)
    write_md(root, "cfo/a.md", "第一版")
    first = w.scan_once()
    assert first["diff"]["added"] == ["cfo/a.md"]
    assert _doc("cfo/a.md")["status"] == "ready"

    write_md(root, "cfo/a.md", "第二版内容更长一些")
    hash_v1 = _doc("cfo/a.md")["indexed_hash"]
    second = w.scan_once()
    assert second["diff"]["changed"] == ["cfo/a.md"]
    assert _doc("cfo/a.md")["indexed_hash"] != hash_v1      # 已按新正文重建
    assert _doc("cfo/a.md")["chunk_count"] >= 1


def test_scan_once_marks_removed_as_missing_not_deleted(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    w = VaultWatcher(kservice, interval_s=0.01)
    write_md(root, "sales/s.md", "会被删掉的文档")
    w.scan_once()
    (root / "sales" / "s.md").unlink()
    out = w.scan_once()
    assert out["diff"]["removed"] == ["sales/s.md"]
    doc = _doc("sales/s.md")
    assert doc is not None and doc["status"] == "missing"   # 索引保留供恢复


def test_scan_once_no_change_is_noop(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    w = VaultWatcher(kservice, interval_s=0.01)
    write_md(root, "cfo/a.md", "稳定内容")
    w.scan_once()
    before = len(isolated_env["fake_llm"].calls)
    out = w.scan_once()
    assert out["diff"] == {"added": [], "removed": [], "changed": []}
    assert len(isolated_env["fake_llm"].calls) == before


def test_single_file_failure_does_not_abort_round(kservice, isolated_env, write_md,
                                                  monkeypatch):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/bad.md", "坏文件")
    write_md(root, "sales/good.md", "好文件")
    original = kservice.ingest

    def flaky(self, rel):
        if rel.endswith("bad.md"):
            raise RuntimeError("ingest_boom")
        return original(rel)

    # KnowledgeService 已 __slots__ 化（M5-a 封 writer 出口），实例属性不可改，
    # 因此在类上打补丁；替身仍走真实 ingest。
    monkeypatch.setattr(type(kservice), "ingest", flaky)
    w = VaultWatcher(kservice, interval_s=0.01)
    out = w.scan_once()
    statuses = {r["vault_path"]: r["status"] for r in out["results"]}
    assert statuses["cfo/bad.md"] == "failed"
    assert statuses["sales/good.md"] == "ready"


def test_start_is_idempotent(kservice):
    w = VaultWatcher(kservice, interval_s=0.01)
    try:
        w.start()
        first = [t for t in threading.enumerate() if t.name == "vault-watcher"]
        w.start()
        second = [t for t in threading.enumerate() if t.name == "vault-watcher"]
        assert len(first) == 1 and len(second) == 1
    finally:
        w.stop()


def test_stop_joins_and_leaves_no_thread(kservice):
    w = VaultWatcher(kservice, interval_s=0.01)
    w.start()
    assert w.running is True
    w.stop()
    assert w.running is False
    assert not any(t.name == "vault-watcher" for t in threading.enumerate())


def test_stop_is_idempotent_and_allows_restart(kservice):
    w = VaultWatcher(kservice, interval_s=0.01)
    w.start()
    w.stop()
    w.stop()
    w.start()
    try:
        assert w.running is True
    finally:
        w.stop()
    assert not any(t.name == "vault-watcher" for t in threading.enumerate())


def test_background_loop_actually_indexes(kservice, isolated_env, write_md):
    import time
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "后台扫描应导入")
    w = VaultWatcher(kservice, interval_s=0.01)
    w.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _doc("cfo/a.md") is None:
            time.sleep(0.01)
    finally:
        w.stop()
    assert _doc("cfo/a.md") is not None


def test_watcher_uses_config_interval_by_default(kservice):
    w = VaultWatcher(kservice)
    assert w._interval == config.VAULT_SCAN_INTERVAL_S