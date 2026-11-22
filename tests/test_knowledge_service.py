"""KnowledgeService 闭环（M4 第二批）：导入/更新/检索/删除/恢复 + B8 竞争保护。

验收重点（用户明确）：
- 检索隔离必须检查**实际 chunk 正文、出处、日志与错误信息**，不只比 doc_id；
- B8 必须证明**旧 embedding 任务无法覆盖新文件版本**，不是普通顺序执行。
"""
from __future__ import annotations

import threading

import pytest

from opc_workbench import config, db, vault
from opc_workbench.knowledge_service import (DeleteDenied as KnowledgeServiceDeleteDenied,
                                             KnowledgeService, body_hash)

SECRET = "SECRET-CFO-42"


def _rows(sql, params=()):
    return db.get_conn().execute(sql, params).fetchall()


def _doc(rel):
    rows = _rows("SELECT * FROM knowledge_docs WHERE stored_path=?", (rel,))
    return rows[0] if rows else None


def _chunks(doc_id):
    return _rows("SELECT * FROM knowledge_chunks WHERE doc_id=? ORDER BY seq", (doc_id,))


# --- 导入基础 ---------------------------------------------------------------


def test_ingest_creates_doc_and_chunks(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "季度成本结构说明")
    out = kservice.ingest("cfo/a.md")
    assert out["status"] == "ready" and out["reindexed"] is True
    doc = _doc("cfo/a.md")
    assert doc["owner_role"] == "cfo"
    assert doc["chunk_count"] == len(_chunks(doc["id"])) > 0
    assert doc["indexed_hash"] == body_hash("季度成本结构说明")
    assert doc["embed_dim"] == config.EMBEDDING_DIM


def test_ingest_is_idempotent_for_unchanged_content(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "同一份内容")
    first = kservice.ingest("cfo/a.md")
    calls_before = len(isolated_env["fake_llm"].calls)
    second = kservice.ingest("cfo/a.md")
    assert second["reindexed"] is False
    assert second["doc_id"] == first["doc_id"]
    assert len(isolated_env["fake_llm"].calls) == calls_before   # 未重复嵌入


def test_owner_comes_from_directory_not_frontmatter(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "内容", owner_role="sales")   # frontmatter 谎报
    out = kservice.ingest("cfo/a.md")
    assert out["owner_corrected"] is True
    assert _doc("cfo/a.md")["owner_role"] == "cfo"


def test_shared_dir_maps_to_coordinator_and_public(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "shared/s.md", "共享制度", visibility="private")
    kservice.ingest("shared/s.md")
    doc = _doc("shared/s.md")
    assert doc["owner_role"] == "coordinator"
    assert doc["visibility"] == "public"


def test_same_content_different_paths_both_indexed(kservice, isolated_env, write_md):
    """C2：不做内容级去重，两个合法副本各自入库、互不覆盖。"""
    root = isolated_env["vault_dir"]
    body = "完全相同的正文"
    write_md(root, "cfo/a.md", body)
    write_md(root, "cfo/b.md", body)
    a = kservice.ingest("cfo/a.md")
    b = kservice.ingest("cfo/b.md")
    assert a["doc_id"] != b["doc_id"]
    assert _doc("cfo/a.md")["indexed_hash"] == _doc("cfo/b.md")["indexed_hash"]
    assert len(_chunks(a["doc_id"])) > 0 and len(_chunks(b["doc_id"])) > 0


def test_file_move_is_new_doc_not_merge(kservice, isolated_env, write_md):
    """C2 修订：移动按路径身份处理，旧路径 missing、新路径新文档。"""
    root = isolated_env["vault_dir"]
    body = "被移动的正文"
    write_md(root, "cfo/a.md", body)
    old = kservice.ingest("cfo/a.md")
    (root / "cfo" / "a.md").unlink()
    write_md(root, "sales/a.md", body)
    new = kservice.ingest("sales/a.md")
    kservice.mark_missing("cfo/a.md")
    assert new["doc_id"] != old["doc_id"]
    assert _doc("cfo/a.md")["status"] == "missing"
    assert _doc("sales/a.md")["owner_role"] == "sales"
    assert len(_chunks(old["doc_id"])) > 0        # 旧索引保留供恢复


# --- 红线1：先校验路径再读取 --------------------------------------------------


@pytest.mark.parametrize("rel", ["../evil.md", "cfo/../../evil.md", "C:\\evil.md",
                                 "/etc/passwd", "cfo/a.md:stream"])
def test_unsafe_path_rejected_before_any_read(kservice, isolated_env, monkeypatch, rel):
    """读取桩一旦被调用即失败：证明校验发生在读取之前。"""
    reads: list[str] = []

    def _read(self, *a, **kw):
        reads.append(str(self))
        raise AssertionError("read_before_path_validation")

    monkeypatch.setattr(type(isolated_env["vault_dir"]), "read_text", _read)
    with pytest.raises(ValueError, match="vault_path_unsafe"):
        kservice.ingest(rel)
    assert reads == []


def test_attachments_dir_not_ingestable(kservice, isolated_env):
    root = isolated_env["vault_dir"]
    (root / "attachments").mkdir(exist_ok=True)
    (root / "attachments" / "x.md").write_text("---\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError):
        kservice.ingest("attachments/x.md")


# --- C6：元数据变更不重嵌入但权限即时生效 --------------------------------------


def test_visibility_change_updates_db_without_reembed(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    body = "老板的私密笔记"
    write_md(root, "cfo/a.md", body, visibility="public")
    kservice.ingest("cfo/a.md")
    before = len(isolated_env["fake_llm"].calls)

    write_md(root, "cfo/a.md", body, visibility="private")   # 只改可见性
    out = kservice.ingest("cfo/a.md")
    assert out["reindexed"] is False
    assert len(isolated_env["fake_llm"].calls) == before      # 未重新嵌入
    assert _doc("cfo/a.md")["visibility"] == "private"        # DB 立即更新
    # 检索立即生效：sales 再也搜不到
    hits = kservice.search(body, "sales")
    assert all(SECRET not in h["content"] for h in hits)
    assert hits == []


def test_visibility_change_both_directions(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    body = "双向可见性测试正文"
    write_md(root, "cfo/a.md", body, visibility="private")
    kservice.ingest("cfo/a.md")
    assert kservice.search(body, "sales") == []
    write_md(root, "cfo/a.md", body, visibility="public")
    kservice.ingest("cfo/a.md")
    assert len(kservice.search(body, "sales")) == 1
    write_md(root, "cfo/a.md", body, visibility="private")
    kservice.ingest("cfo/a.md")
    assert kservice.search(body, "sales") == []


# --- 检索与权限隔离 ----------------------------------------------------------


def test_search_returns_content_and_provenance(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    body = "检索出处验证正文"
    write_md(root, "sales/s.md", body)
    kservice.ingest("sales/s.md")
    hits = kservice.search(body, "sales")
    assert len(hits) == 1
    assert hits[0]["content"] == body
    assert hits[0]["vault_path"] == "sales/s.md"
    assert hits[0]["seq"] == 0
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-5)


def test_search_isolation_checks_content_path_logs_and_errors(
        kservice, isolated_env, write_md):
    """他角色 private 的正文/出处/日志/错误信息都不得泄露。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/secret.md", SECRET, visibility="private")
    write_md(root, "sales/open.md", "公开销售素材")
    kservice.ingest("cfo/secret.md")
    kservice.ingest("sales/open.md")

    hits = kservice.search(SECRET, "sales")
    assert hits == []                                  # 不是"数据不存在"造成的空
    assert len(kservice.search(SECRET, "boss")) == 1   # 同一查询 boss 能命中
    assert all(SECRET not in h["content"] for h in hits)
    assert all("cfo/" not in h["vault_path"] for h in hits)
    assert all(h["owner_role"] != "cfo" for h in hits)

    # 非空结果下的正文/出处校验：sales 只能拿到自己那条
    open_hits = kservice.search("公开销售素材", "sales")
    assert len(open_hits) == 1
    assert open_hits[0]["vault_path"] == "sales/open.md"
    assert SECRET not in open_hits[0]["content"]

    docs = kservice.list_docs("sales")
    assert all(d["vault_path"] != "cfo/secret.md" for d in docs)

    log_path = config.LOG_DIR / "audit.log"
    if log_path.exists():
        assert SECRET not in log_path.read_text(encoding="utf-8")


def test_coordinator_cannot_see_any_private(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/p.md", SECRET, visibility="private")
    write_md(root, "sales/s.md", "公开材料")
    kservice.ingest("cfo/p.md")
    kservice.ingest("sales/s.md")
    hits = kservice.search(SECRET, "coordinator")
    assert hits == []
    assert all(h["visibility"] == "public"
               for h in kservice.search("公开材料", "coordinator"))


def test_boss_sees_all(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/p.md", SECRET, visibility="private")
    kservice.ingest("cfo/p.md")
    hits = kservice.search(SECRET, "boss")
    assert len(hits) == 1 and hits[0]["content"] == SECRET


def test_owner_sees_own_private(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/p.md", SECRET, visibility="private")
    kservice.ingest("cfo/p.md")
    assert len(kservice.search(SECRET, "cfo")) == 1


def test_missing_doc_excluded_from_search(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "消失的文档正文")
    kservice.ingest("sales/s.md")
    (root / "sales" / "s.md").unlink()
    kservice.mark_missing("sales/s.md")
    assert kservice.search("消失的文档正文", "boss") == []


def test_search_threshold_filters_low_scores(kservice, isolated_env, write_md, monkeypatch):
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "阈值测试正文")
    kservice.ingest("sales/s.md")
    monkeypatch.setattr(config, "KNOWLEDGE_MIN_SCORE", 1.1)
    assert kservice.search("阈值测试正文", "sales") == []
    monkeypatch.setattr(config, "KNOWLEDGE_MIN_SCORE", 0.0)
    assert len(kservice.search("阈值测试正文", "sales")) == 1


def test_search_invalid_caller_fails_closed(kservice):
    with pytest.raises(ValueError, match="invalid_caller"):
        kservice.search("x", "not-a-role")


def test_search_top_k_invalid_rejected(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "topk 测试")
    kservice.ingest("sales/s.md")
    with pytest.raises(ValueError, match="top_k_invalid"):
        kservice.search("topk 测试", "sales", top_k=0)


# --- 失败路径 ---------------------------------------------------------------


def test_embed_failure_marks_failed_and_keeps_indexed_hash(
        kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "第一版正文")
    kservice.ingest("cfo/a.md")
    good_hash = _doc("cfo/a.md")["indexed_hash"]

    write_md(root, "cfo/a.md", "第二版正文")
    isolated_env["fake_llm"].embed_responses = [RuntimeError("boom")]
    out = kservice.ingest("cfo/a.md")
    assert out["status"] == "failed"
    doc = _doc("cfo/a.md")
    assert doc["indexed_hash"] == good_hash          # 失败不前进 indexed_hash
    assert doc["last_error"] and "boom" in doc["last_error"]
    assert "第二版正文" not in doc["last_error"]      # 错误信息不含正文
    # 旧索引仍在，且内容仍是第一版
    assert _chunks(doc["id"])[0]["content"] == "第一版正文"


def test_failure_log_has_no_body(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", SECRET)
    isolated_env["fake_llm"].embed_responses = [RuntimeError("boom")]
    kservice.ingest("cfo/a.md")
    log_path = config.LOG_DIR / "audit.log"
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "cfo/a.md" in text                        # 出处必须可追溯


# --- B8：版本竞争保护（真并发交错，非顺序执行） --------------------------------


def test_stale_embedding_cannot_overwrite_newer_version(
        kwriter, isolated_env, write_md, gated_embedder_cls):
    """A 轮读 v1 后阻塞在嵌入；B 轮完成 v2；A 醒来必须被丢弃。

    这是真正的并发交错：A 在独立线程中执行，用 Event 精确编排，不用 sleep。
    """
    root = isolated_env["vault_dir"]
    fake = isolated_env["fake_llm"]
    write_md(root, "cfo/a.md", "V1-OLD-CONTENT")
    gate = gated_embedder_cls(fake)
    svc_a = KnowledgeService(kwriter, llm=gate, vault_root=root)
    svc_b = KnowledgeService(kwriter, llm=fake, vault_root=root)

    results: dict = {}
    errors: list = []

    def run_a():
        try:
            results["a"] = svc_a.ingest("cfo/a.md")
        except BaseException as exc:      # noqa: BLE001 — 线程内异常需带回主线程
            errors.append(exc)

    t = threading.Thread(target=run_a)
    t.start()
    try:
        assert gate.entered.wait(10), "A 未进入嵌入阶段"
        # A 已读到 v1 并写入 source_hash=h1，此刻文件被改成 v2
        write_md(root, "cfo/a.md", "V2-NEW-CONTENT")
        results["b"] = svc_b.ingest("cfo/a.md")
        assert results["b"]["status"] == "ready"
    finally:
        gate.release.set()
        t.join(10)
    assert not errors, errors
    assert not t.is_alive()

    # 先验**实际落库内容**：无保护时这里会直接暴露 V1 覆盖 V2
    doc = _doc("cfo/a.md")
    contents = [c["content"] for c in _chunks(doc["id"])]
    assert contents == ["V2-NEW-CONTENT"]
    assert "V1-OLD-CONTENT" not in contents
    assert doc["indexed_hash"] == body_hash("V2-NEW-CONTENT")
    assert results["a"]["stale"] is True


def test_stale_discard_writes_no_partial_chunks(
        kwriter, isolated_env, write_md, gated_embedder_cls):
    """首次导入即过期时，不得留下任何半截 chunk。"""
    root = isolated_env["vault_dir"]
    fake = isolated_env["fake_llm"]
    write_md(root, "sales/s.md", "OLD-BODY")
    gate = gated_embedder_cls(fake)
    svc_a = KnowledgeService(kwriter, llm=gate, vault_root=root)
    svc_b = KnowledgeService(kwriter, llm=fake, vault_root=root)
    out: dict = {}

    t = threading.Thread(target=lambda: out.update(a=svc_a.ingest("sales/s.md")))
    t.start()
    try:
        assert gate.entered.wait(10)
        write_md(root, "sales/s.md", "NEW-BODY")
        svc_b.ingest("sales/s.md")
    finally:
        gate.release.set()
        t.join(10)
    doc = _doc("sales/s.md")
    assert [c["content"] for c in _chunks(doc["id"])] == ["NEW-BODY"]
    assert doc["chunk_count"] == len(_chunks(doc["id"]))
    assert out["a"]["stale"] is True


def test_sequential_reindex_advances_indexed_hash(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "版本一")
    kservice.ingest("cfo/a.md")
    write_md(root, "cfo/a.md", "版本二")
    out = kservice.ingest("cfo/a.md")
    assert out["reindexed"] is True and out["stale"] is False
    doc = _doc("cfo/a.md")
    assert doc["indexed_hash"] == body_hash("版本二")
    assert [c["content"] for c in _chunks(doc["id"])] == ["版本二"]


def test_reindex_keeps_status_ready(kservice, isolated_env, write_md):
    """reindex 期间旧索引继续可搜（不置 processing）。"""
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "旧正文")
    kservice.ingest("sales/s.md")
    write_md(root, "sales/s.md", "新正文")
    kservice.ingest("sales/s.md")
    assert _doc("sales/s.md")["status"] == "ready"


# --- 删除与恢复 -------------------------------------------------------------


def test_delete_removes_file_rows_and_chunks(kservice, isolated_env, write_md, kboss_ident):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "待删除正文")
    out = kservice.ingest("cfo/a.md")
    kservice.delete("cfo/a.md", identity=kboss_ident)
    assert not (root / "cfo" / "a.md").exists()
    assert _doc("cfo/a.md") is None
    assert _chunks(out["doc_id"]) == []
    assert _rows("SELECT * FROM pending_deletions") == []


def test_delete_is_idempotent(kservice, isolated_env, write_md, kboss_ident):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "幂等删除")
    kservice.ingest("cfo/a.md")
    kservice.delete("cfo/a.md", identity=kboss_ident)
    kservice.delete("cfo/a.md", identity=kboss_ident)
    assert _doc("cfo/a.md") is None


def test_delete_rejects_unsafe_path(kservice, kboss_ident):
    with pytest.raises(ValueError, match="vault_path_unsafe"):
        kservice.delete("../evil.md", identity=kboss_ident)


# --- 删除鉴权（服务层是唯一授权落点）----------------------------------------


def _no_effect(root, rel="cfo/a.md"):
    """拒绝必须发生在动作之前：文件与文档行都还在。"""
    assert (root / rel).exists()
    assert _doc(rel) is not None


def test_delete_without_identity_fails_closed(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "无身份")
    kservice.ingest("cfo/a.md")
    with pytest.raises(KnowledgeServiceDeleteDenied) as exc:
        kservice.delete("cfo/a.md")
    assert exc.value.code == "identity_required"
    _no_effect(root)


def test_delete_rejects_non_boss_identity(kservice, isolated_env, write_md, ksales_ident):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "非 boss")
    kservice.ingest("cfo/a.md")
    with pytest.raises(KnowledgeServiceDeleteDenied) as exc:
        kservice.delete("cfo/a.md", identity=ksales_ident)
    assert exc.value.code == "forbidden_role"
    _no_effect(root)
    log = (config.LOG_DIR / "audit.log").read_text(encoding="utf-8")
    assert "knowledge_delete_denied" in log and "forbidden_role" in log


def test_delete_rejects_unregistered_identity(kservice, isolated_env, write_md):
    """裸字典自报 boss / 伪造对象都必须被拒（不做 role 字符串信任）。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "伪造身份")
    kservice.ingest("cfo/a.md")
    for forged in ({"role": "boss"}, object(), "boss"):
        with pytest.raises(KnowledgeServiceDeleteDenied) as exc:
            kservice.delete("cfo/a.md", identity=forged)
        assert exc.value.code == "identity_not_authorized"
    _no_effect(root)


def test_recover_replays_pending_deletion(kservice, kwriter, isolated_env, write_md):
    """崩溃点：意图已写、文件与 DB 行尚未删 → 重启幂等重放。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "崩溃前正文")
    out = kservice.ingest("cfo/a.md")
    with kwriter.transaction() as cur:
        cur.execute("INSERT INTO pending_deletions(doc_id,stored_path,created_at) "
                    "VALUES(?,?,?)", (out["doc_id"], "cfo/a.md", db.now_iso()))
    report = kservice.recover()
    assert report["deletions_replayed"] == 1
    assert not (root / "cfo" / "a.md").exists()
    assert _doc("cfo/a.md") is None
    assert _chunks(out["doc_id"]) == []


def test_recover_replays_pending_vault_write(kservice, kwriter, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "原始正文")
    kservice.ingest("cfo/a.md")
    pending_text = (root / "cfo" / "a.md").read_text(encoding="utf-8").replace(
        "原始正文", "崩溃后应恢复的正文")
    (root / "cfo" / "a.md").unlink()
    with kwriter.transaction() as cur:
        cur.execute("INSERT INTO pending_vault_writes(stored_path,content,created_at) "
                    "VALUES(?,?,?)", ("cfo/a.md", pending_text, db.now_iso()))
    report = kservice.recover()
    assert report["writes_replayed"] == 1
    assert (root / "cfo" / "a.md").exists()
    assert "崩溃后应恢复的正文" in (root / "cfo" / "a.md").read_text(encoding="utf-8")
    assert _rows("SELECT * FROM pending_vault_writes") == []


def test_recover_resets_interrupted_processing(kservice, kwriter, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "中断文档")
    with kwriter.transaction() as cur:
        cur.execute(
            "INSERT INTO knowledge_docs(filename,stored_path,file_hash,owner_role,"
            "created_at,status) VALUES('a.md','cfo/a.md','sha256:x','cfo',?,'processing')",
            (db.now_iso(),))
    report = kservice.recover()
    assert report["processing_reset"] == 1
    doc = _doc("cfo/a.md")
    assert doc["status"] == "failed" and doc["last_error"] == "interrupted"


def test_write_doc_roundtrip(kservice, isolated_env):
    from datetime import datetime, timezone
    meta = {"owner_role": "sales", "visibility": "public", "scope": "global",
            "file_hash": body_hash("经 API 写入的正文"),
            "updated_at": datetime.now(timezone.utc).isoformat()}
    out = kservice.write_doc("sales/new.md", meta, "经 API 写入的正文")
    assert out["status"] == "ready"
    assert _doc("sales/new.md")["owner_role"] == "sales"
    assert _rows("SELECT * FROM pending_vault_writes") == []


def test_write_doc_rejects_unsafe_path(kservice):
    with pytest.raises(ValueError, match="vault_path_unsafe"):
        kservice.write_doc("../evil.md", {}, "x")


def test_purge_missing_respects_retention(kservice, isolated_env, write_md):
    from datetime import datetime, timedelta, timezone
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "过期待清理")
    out = kservice.ingest("sales/s.md")
    (root / "sales" / "s.md").unlink()
    kservice.mark_missing("sales/s.md")
    now = datetime.now(timezone.utc)
    assert kservice.purge_missing(now=now)["purged"] == 0        # 未到保留期
    later = now + timedelta(days=config.MISSING_RETENTION_DAYS + 1)
    assert kservice.purge_missing(now=later)["purged"] == 1
    assert _doc("sales/s.md") is None
    assert _chunks(out["doc_id"]) == []


# --- 快照 -------------------------------------------------------------------


def test_snapshot_skips_unknown_and_attachment_dirs(kservice, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "合法")
    (root / "attachments").mkdir(exist_ok=True)
    (root / "attachments" / "x.md").write_text("x", encoding="utf-8")
    (root / "unknown").mkdir(exist_ok=True)
    (root / "unknown" / "y.md").write_text("y", encoding="utf-8")
    assert set(kservice.snapshot()) == {"cfo/a.md"}