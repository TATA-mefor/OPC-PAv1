"""B1/B2/B3/B4 复审阻断修复的并发回归（M4 第二批）。

B1：旧轮次 embedding 失败，不得把已完成的新版本标成 failed。
B2：watcher 判定 missing 后，旧轮次不得把已消失的文件复活成 ready。
B3：只改 frontmatter（public→private）时正文 hash 不变，旧轮次不得用旧快照
    把 DB 权限覆盖回 public（C6 安全红线的并发分支）。
B4：显式删除后旧轮次不得重新创建已删除文档（删除代次持久化失效）。

全部用 Event 精确编排真实线程交错，**不用 sleep 猜时序**；
断言先看**实际落库状态与 chunk 正文**，再看返回标志，
保证无保护时红证直接暴露状态被污染，而不是先撞在标志位上。
"""
from __future__ import annotations

import contextlib
import pathlib
import threading

from opc_workbench import db
from opc_workbench.knowledge_service import KnowledgeService, body_hash


def _doc(rel):
    rows = db.get_conn().execute(
        "SELECT * FROM knowledge_docs WHERE stored_path=?", (rel,)).fetchall()
    return rows[0] if rows else None


def _chunks(doc_id):
    return db.get_conn().execute(
        "SELECT * FROM knowledge_chunks WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()


def _generation(rel):
    rows = db.get_conn().execute(
        "SELECT generation FROM vault_path_state WHERE stored_path=?", (rel,)).fetchall()
    return 0 if not rows else rows[0]["generation"]


@contextlib.contextmanager
def _first_read_gate(name: str):
    """在**第一次**读取指定文件时置位并阻塞（读完之后、TX1 之前停住）。

    只拦第一次：保护逻辑本身会再读一次文件复核快照，第二次必须放行，
    否则测到的就不是"旧快照覆盖"而是替身自己卡死。
    """
    original = pathlib.Path.read_text
    entered = threading.Event()
    release = threading.Event()
    state = {"hit": False}

    def gated(self, *a, **kw):
        text = original(self, *a, **kw)
        if self.name == name and not state["hit"]:
            state["hit"] = True
            entered.set()
            if not release.wait(10):
                raise RuntimeError("gate_timeout")
        return text

    pathlib.Path.read_text = gated
    try:
        yield entered, release
    finally:
        pathlib.Path.read_text = original


class _FailAfterGate:
    """进入 embed 置位；放行后抛异常（模拟旧轮次嵌入失败）。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed(self, texts, *, deadline=None):
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("gate_timeout")
        raise RuntimeError("OLD-ROUND-EMBED-FAILURE")


# --- B1 ---------------------------------------------------------------------


def test_stale_failure_cannot_mark_newer_version_failed(
        kwriter, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    fake = isolated_env["fake_llm"]
    write_md(root, "cfo/a.md", "V1-OLD")
    gate = _FailAfterGate()
    svc_a = KnowledgeService(kwriter, llm=gate, vault_root=root)
    svc_b = KnowledgeService(kwriter, llm=fake, vault_root=root)

    out: dict = {}
    errors: list = []

    def run_a():
        try:
            out["a"] = svc_a.ingest("cfo/a.md")
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run_a)
    t.start()
    try:
        assert gate.entered.wait(10), "A 未进入嵌入阶段"
        write_md(root, "cfo/a.md", "V2-NEW")
        out["b"] = svc_b.ingest("cfo/a.md")
        assert out["b"]["status"] == "ready"
    finally:
        gate.release.set()
        t.join(10)
    assert not errors, errors
    assert not t.is_alive()

    doc = _doc("cfo/a.md")
    # 先看实际状态：无保护时这里会变成 failed，新版本被挤出检索视图
    assert doc["status"] == "ready"
    assert doc["indexed_hash"] == body_hash("V2-NEW")
    assert [c["content"] for c in _chunks(doc["id"])] == ["V2-NEW"]
    assert doc["last_error"] is None
    assert out["a"]["stale"] is True
    # 新版本仍可被检索到
    assert len(svc_b.search("V2-NEW", "cfo")) == 1


def test_current_round_failure_still_marks_failed(kservice, isolated_env, write_md):
    """修复不得误伤：本轮自身失败仍要正常标 failed。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "本轮失败正文")
    isolated_env["fake_llm"].embed_responses = [RuntimeError("boom")]
    out = kservice.ingest("cfo/a.md")
    assert out["status"] == "failed" and out["stale"] is False
    doc = _doc("cfo/a.md")
    assert doc["status"] == "failed" and "boom" in doc["last_error"]


# --- B2 ---------------------------------------------------------------------


def test_stale_success_cannot_resurrect_missing_doc(
        kwriter, isolated_env, write_md, gated_embedder_cls):
    root = isolated_env["vault_dir"]
    fake = isolated_env["fake_llm"]
    write_md(root, "sales/s.md", "GHOST-BODY")
    gate = gated_embedder_cls(fake)
    svc_a = KnowledgeService(kwriter, llm=gate, vault_root=root)
    svc_b = KnowledgeService(kwriter, llm=fake, vault_root=root)

    out: dict = {}
    errors: list = []

    def run_a():
        try:
            out["a"] = svc_a.ingest("sales/s.md")
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run_a)
    t.start()
    try:
        assert gate.entered.wait(10), "A 未进入嵌入阶段"
        (root / "sales" / "s.md").unlink()
        svc_b.mark_missing("sales/s.md")
        assert _doc("sales/s.md")["status"] == "missing"
    finally:
        gate.release.set()
        t.join(10)
    assert not errors, errors
    assert not t.is_alive()

    doc = _doc("sales/s.md")
    # 先看实际状态：无保护时旧任务会把它写回 ready
    assert doc["status"] == "missing"
    assert out["a"]["stale"] is True
    # 已消失的文件不得重新出现在检索结果里
    assert svc_b.search("GHOST-BODY", "boss") == []


def test_stale_failure_cannot_overwrite_missing_status(
        kwriter, isolated_env, write_md):
    """B1+B2 交叉：旧轮次失败也不得把 missing 改成 failed。"""
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "GHOST-FAIL-BODY")
    gate = _FailAfterGate()
    svc_a = KnowledgeService(kwriter, llm=gate, vault_root=root)
    svc_b = KnowledgeService(kwriter, llm=isolated_env["fake_llm"], vault_root=root)
    out: dict = {}

    t = threading.Thread(target=lambda: out.update(a=svc_a.ingest("sales/s.md")))
    t.start()
    try:
        assert gate.entered.wait(10)
        (root / "sales" / "s.md").unlink()
        svc_b.mark_missing("sales/s.md")
    finally:
        gate.release.set()
        t.join(10)
    assert _doc("sales/s.md")["status"] == "missing"
    assert out["a"]["stale"] is True


def test_missing_doc_revives_when_file_reappears(kservice, isolated_env, write_md):
    """修复不得误伤：文件真的回来了，必须能正常复活并重建索引。"""
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "复活验证正文")
    kservice.ingest("sales/s.md")
    (root / "sales" / "s.md").unlink()
    kservice.mark_missing("sales/s.md")
    assert _doc("sales/s.md")["status"] == "missing"

    write_md(root, "sales/s.md", "复活验证正文")
    out = kservice.ingest("sales/s.md")
    assert out["stale"] is False
    assert _doc("sales/s.md")["status"] == "ready"
    assert len(kservice.search("复活验证正文", "sales")) == 1


def test_late_tx1_cannot_resurrect_missing_doc(kwriter, isolated_env, write_md,
                                               gated_embedder_cls):
    """同源缺口：TX1 本身也是状态转换，晚到的 TX1 同样不得复活 missing。

    构造：先让文档处于 missing 且文件不存在，再用一个"读到旧内容"的轮次
    直接驱动 ingest——通过替身在读取后、TX1 前删除文件来精确制造该交错。
    """
    root = isolated_env["vault_dir"]
    fake = isolated_env["fake_llm"]
    write_md(root, "sales/s.md", "LATE-TX1-BODY")
    svc = KnowledgeService(kwriter, llm=fake, vault_root=root)
    svc.ingest("sales/s.md")

    entered = threading.Event()
    release = threading.Event()
    original_read = type(root).read_text

    def gated_read(self, *a, **kw):
        text = original_read(self, *a, **kw)
        if self.name == "s.md":
            entered.set()
            release.wait(10)          # 读完之后、TX1 之前停住
        return text

    import pathlib
    pathlib.Path.read_text = gated_read
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(r=svc.ingest("sales/s.md")))
    t.start()
    try:
        assert entered.wait(10)
        (root / "sales" / "s.md").unlink()
        svc.mark_missing("sales/s.md")
    finally:
        release.set()
        t.join(10)
        pathlib.Path.read_text = original_read

    doc = _doc("sales/s.md")
    assert doc["status"] == "missing"
    assert out["r"]["stale"] is True
    assert svc.search("LATE-TX1-BODY", "boss") == []


# --- B3（只改 frontmatter 的并发覆盖） ---------------------------------------


def test_stale_frontmatter_cannot_overwrite_newer_visibility(
        kservice, isolated_env, write_md):
    """A 读到 public 后暂停；B 把文件改成 private 并完成 ingest；放行 A。

    正文 hash 不变（C6 口径），单靠 source_hash 无法拦截；快照必须拦截。
    最终 DB 必须保持 private：sales 搜不到，boss 能搜到。
    """
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/secret.md", "SENSITIVE-BODY", visibility="public")
    kservice.ingest("cfo/secret.md")
    assert _doc("cfo/secret.md")["visibility"] == "public"

    out: dict = {}
    errors: list = []

    def run_a():
        try:
            out["a"] = kservice.ingest("cfo/secret.md")
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    with _first_read_gate("secret.md") as (entered, release):
        t = threading.Thread(target=run_a)
        t.start()
        try:
            assert entered.wait(10), "A 未停在读完文件之后"
            write_md(root, "cfo/secret.md", "SENSITIVE-BODY", visibility="private")
            out["b"] = kservice.ingest("cfo/secret.md")
            # 正文未变 → 不应重新嵌入，但权限必须立即落库
            assert out["b"]["reindexed"] is False
            assert _doc("cfo/secret.md")["visibility"] == "private"
        finally:
            release.set()
            t.join(10)
    assert not errors, errors
    assert not t.is_alive()

    doc = _doc("cfo/secret.md")
    # 红证点：无快照保护时旧轮次会把这里写回 public
    assert doc["visibility"] == "private"
    assert doc["indexed_hash"] == body_hash("SENSITIVE-BODY")
    assert out["a"]["stale"] is True
    assert kservice.search("SENSITIVE-BODY", "sales") == []
    assert len(kservice.search("SENSITIVE-BODY", "boss")) == 1


def test_sequential_visibility_change_is_not_stale(kservice, isolated_env, write_md):
    """修复不得误伤：非并发顺序修改 visibility 必须正常生效、判非过期。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/secret.md", "SENSITIVE-BODY", visibility="public")
    first = kservice.ingest("cfo/secret.md")
    assert first["stale"] is False and first["reindexed"] is True

    write_md(root, "cfo/secret.md", "SENSITIVE-BODY", visibility="private")
    second = kservice.ingest("cfo/secret.md")
    assert second["stale"] is False
    assert second["reindexed"] is False           # 正文未变，不重嵌入

    doc = _doc("cfo/secret.md")
    assert doc["visibility"] == "private"
    assert doc["indexed_hash"] == body_hash("SENSITIVE-BODY")
    assert kservice.search("SENSITIVE-BODY", "sales") == []
    assert len(kservice.search("SENSITIVE-BODY", "boss")) == 1


# --- B4（显式删除与在途 ingest 竞争） ----------------------------------------


def test_inflight_ingest_after_delete_leaves_no_trace(
        kservice, isolated_env, write_md, kboss_ident):
    """A 读完文件暂停；B 完成彻底删除；放行 A 后必须没有 DB 行/chunks/文件。"""
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "DELETED-BODY")
    old = kservice.ingest("sales/s.md")
    old_id = old["doc_id"]
    assert _chunks(old_id)

    out: dict = {}
    errors: list = []

    def run_a():
        try:
            out["a"] = kservice.ingest("sales/s.md")
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    with _first_read_gate("s.md") as (entered, release):
        t = threading.Thread(target=run_a)
        t.start()
        try:
            assert entered.wait(10), "A 未停在读完文件之后"
            kservice.delete("sales/s.md", identity=kboss_ident)
            assert not (root / "sales" / "s.md").exists()
            assert _doc("sales/s.md") is None
        finally:
            release.set()
            t.join(10)
    assert not errors, errors
    assert not t.is_alive()

    assert _doc("sales/s.md") is None
    assert _chunks(old_id) == []
    assert not (root / "sales" / "s.md").exists()
    assert out["a"]["stale"] is True
    assert kservice.search("DELETED-BODY", "boss") == []


def test_ingest_after_delete_cannot_resurrect_doc(kservice, isolated_env, write_md, kboss_ident):
    """删除代次的独立作用：文件被"原样字节"恢复时快照相同，仍不得被旧轮次重建。"""
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "DELETED-BODY")
    old_id = kservice.ingest("sales/s.md")["doc_id"]
    original_text = (root / "sales" / "s.md").read_text(encoding="utf-8")

    out: dict = {}
    errors: list = []

    def run_a():
        try:
            out["a"] = kservice.ingest("sales/s.md")
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    with _first_read_gate("s.md") as (entered, release):
        t = threading.Thread(target=run_a)
        t.start()
        try:
            assert entered.wait(10), "A 未停在读完文件之后"
            kservice.delete("sales/s.md", identity=kboss_ident)
            # 原样恢复同一份字节（模拟同步盘/备份回写）：快照校验拦不住
            (root / "sales" / "s.md").write_text(original_text, encoding="utf-8")
        finally:
            release.set()
            t.join(10)
    assert not errors, errors
    assert not t.is_alive()

    # 红证点：无代次保护时旧轮次会 INSERT 一篇已删除文档并写 chunks
    assert _doc("sales/s.md") is None
    assert _chunks(old_id) == []
    assert out["a"]["stale"] is True
    # 删除必须留下持久代次，供后续新导入取得新身份
    assert _generation("sales/s.md") == 1


def test_delete_then_reimport_creates_new_identity(kservice, isolated_env, write_md, kboss_ident):
    """修复不得误伤：删除后真正重新导入必须成功，且是新文档身份。"""
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "REIMPORT-BODY")
    old_id = kservice.ingest("sales/s.md")["doc_id"]

    kservice.delete("sales/s.md", identity=kboss_ident)
    assert _doc("sales/s.md") is None
    assert _generation("sales/s.md") == 1
    assert not (root / "sales" / "s.md").exists()

    write_md(root, "sales/s.md", "REIMPORT-BODY")
    fresh = kservice.ingest("sales/s.md")
    assert fresh["stale"] is False and fresh["reindexed"] is True
    assert fresh["doc_id"] != old_id

    doc = _doc("sales/s.md")
    assert doc["status"] == "ready"
    assert [c["content"] for c in _chunks(fresh["doc_id"])] == ["REIMPORT-BODY"]
    assert len(kservice.search("REIMPORT-BODY", "sales")) == 1


# --- B4 扩展（purge_missing 与在途 ingest 竞争）-------------------------------


def test_inflight_ingest_after_purge_missing_cannot_resurrect(
        kservice, isolated_env, write_md):
    """purge 也清除文档身份：先推进代次，旧轮次不得在文件重现后重建旧记录。"""
    from datetime import datetime, timedelta, timezone

    from opc_workbench import config

    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "PURGED-BODY")
    old_id = kservice.ingest("sales/s.md")["doc_id"]
    original_text = (root / "sales" / "s.md").read_text(encoding="utf-8")

    out: dict = {}
    errors: list = []

    def run_a():
        try:
            out["a"] = kservice.ingest("sales/s.md")
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    with _first_read_gate("s.md") as (entered, release):
        t = threading.Thread(target=run_a)
        t.start()
        try:
            assert entered.wait(10), "A 未停在读完文件之后"
            # 文件消失 → missing；保留期已过 → purge（应推进代次）
            (root / "sales" / "s.md").unlink()
            assert kservice.mark_missing("sales/s.md")["missing"] is True
            later = datetime.now(timezone.utc) + timedelta(
                days=config.MISSING_RETENTION_DAYS + 1)
            assert kservice.purge_missing(now=later)["purged"] == 1
            assert _doc("sales/s.md") is None
            assert _chunks(old_id) == []
            # 文件以原样字节重新出现（快照与 A 读到的一致，只有代次能拦住）
            (root / "sales" / "s.md").write_text(original_text, encoding="utf-8")
        finally:
            release.set()
            t.join(10)
    assert not errors, errors
    assert not t.is_alive()

    # 红证点：purge 不推进代次时，A 会重新插入已 purge 的文档并写 chunks
    assert _doc("sales/s.md") is None
    assert _chunks(old_id) == []
    assert _generation("sales/s.md") == 1
    assert out["a"]["stale"] is True
    assert kservice.search("PURGED-BODY", "boss") == []

    # 误伤检查：purge 后的新导入轮次必须能正常重建
    fresh = kservice.ingest("sales/s.md")
    assert fresh["stale"] is False and fresh["reindexed"] is True
    assert fresh["doc_id"] != old_id
    assert _generation("sales/s.md") == 1
    assert len(kservice.search("PURGED-BODY", "sales")) == 1
