"""知识导入/更新/检索/删除的状态机（M4 第二批）。

冻结裁决（用户 2026-11-22，C1–C6）：
- C1：检索经 visibility.query_visible("knowledge_chunks_visible", caller_role)，
  治理列由 knowledge_docs 单一真源 JOIN 取得；视图只读，写入只经注入 writer。
- C2：文档身份是 stored_path，**不做内容级去重**；不同路径同内容各自入库。
  文件移动按路径身份处理：旧路径删除，新路径作为新文档导入，绝不合并。
- C3：embed 走 LLMClient 独立 embed_breaker。
- C5：维度固定 config.EMBEDDING_DIM；模型/维度变化不自动全量重嵌入。
- C6：hash = sha256(正文 UTF-8)，不含 frontmatter；只改 visibility 不重嵌入，
  但**必须立即更新 DB 权限**，检索即时生效。

两条硬红线：
1. **先校验路径再读文件**：resolve_safe_path → read → load_doc。
   load_doc 校验的是路径字符串而非读取行为，先读后验等于没校验。
2. **B8 版本保护**：嵌入在事务外做；提交时用
   `UPDATE ... WHERE id=? AND source_hash=? AND status<>'missing'` 条件更新，
   rowcount==0 即判定本轮结果过期，整事务回滚，一个 chunk 都不写。
   indexed_hash 只在成功后前进。
   **失败路径同样受这两个条件保护**（B1）：旧轮次失败不得把新版本标成 failed。
   **missing 不可被旧轮次复活**（B2）：mark_missing 不改 source_hash，因此
   提交、失败标记、TX1 状态转换三处都必须显式排除 missing。
3. **B3 快照保护**：body_hash 排除 frontmatter，只改 visibility 时正文 hash 不变，
   单靠 source_hash 会让"读到 public 的旧轮次"在别人把文件改成 private 之后
   用旧快照把 DB 权限覆盖回 public。因此另设 `snapshot_hash`（原文全文 hash，
   含 frontmatter）作为**文件快照标识**：嵌入口径仍只用 body_hash，但 TX1
   提交前必须在写事务内重新读取文件、确认快照仍是本轮读到的那个，否则整轮
   判 stale（权限写入、chunks 写入、失败标记一律不落库）。
4. **B4 删除代次保护**：显式 delete 会推进 `vault_path_state.generation`（持久
   墓碑，文档行删除后仍保留）。ingest 在**读文件之前**取得代次，TX1/TX2 在
   写事务内比对该代次；代次被推进即表示该路径已被显式删除，旧轮次不得重新
   创建文档、不得写 chunks。删除后真正重新导入由新轮次读取新代次完成。
   `purge_missing` 同样清除文档身份，因此在同一个写事务内**先推进代次**再删
   行删块，防"purge 之前启动的旧轮次"在文件后来出现时重建旧记录。
5. **路径身份唯一**（B5）：DB 键一律是 `vault.normalize_relpath()` 的结果
   （规范 POSIX 相对路径）。`cfo\\a.md` 与 `cfo/a.md` 是同一个身份，否则
   反斜杠导入、正斜杠删除会删掉盘上文件却查不到文档行，造成行与 chunks
   残留，代次墓碑也会分裂成两条。
6. **删除鉴权在方法内部**：UI 隐藏入口不是授权边界。delete 只接受
   identity_api 签发、经登记校验的 `Identity` 实例（裸 role 字符串无效），
   角色非 boss 或无法认证一律 fail closed。

读写边界：对外检索一律经 query_visible 治理闸门；写入事务内读取**自身索引状态**
（doc 行/hash）用注入 writer 的游标，属写路径内部状态，不构成 Agent 业务查询。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import (audit_log, chunking, config, db, knowledge_index, llm_client,
               vault, visibility)


def body_hash(body: str) -> str:
    """C6 统一口径：正文 UTF-8 字节的 sha256，不含 frontmatter。"""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def snapshot_hash(text: str) -> str:
    """B3：文件快照标识 = 原文（frontmatter + 正文）UTF-8 字节的 sha256。

    与 body_hash 分工：body_hash 决定"是否需要重新 embedding"（C6 冻结口径，
    不含 frontmatter）；snapshot_hash 只判定"本轮读到的文件是否仍是最新快照"，
    因此**只改 frontmatter（如 public→private）也会得到不同快照**，可被保护识别。
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class KnowledgeService:
    """知识库应用服务。writer 由组合根注入，**不依赖 Application.writer**。"""

    def __init__(self, writer, *, llm=None, vault_root=None, identity_api=None):
        self._writer = writer
        self._llm = llm
        self._root = Path(vault_root) if vault_root is not None else config.VAULT_PATH
        # 删除鉴权唯一可信来源；未注入即"无人可被认证"，删除一律 fail closed
        self._identity_api = identity_api

    # -- 内部 ---------------------------------------------------------------

    def _embedder(self):
        return self._llm if self._llm is not None else llm_client.get_llm()

    def _read_validated(self, rel: str) -> tuple[str, dict, Path]:
        """红线1：路径校验必须发生在读取之前。"""
        path = vault.resolve_safe_path(self._root, rel)   # ① 先校验
        text = path.read_text(encoding="utf-8")           # ② 再读取
        return text, vault.load_doc(self._root, rel, text), path  # ③ 组合入口定归属
    @staticmethod
    def _effective_visibility(meta: dict) -> str:
        # shared/ 归 coordinator 且强制 public（目录映射既定语义）
        if meta["owner_role"] == vault.SHARED_OWNER:
            return "public"
        return meta["visibility"]

    @staticmethod
    def _doc_row(cur, rel: str):
        return cur.execute(
            "SELECT id, indexed_hash, source_hash, status, embed_model, embed_dim "
            "FROM knowledge_docs WHERE stored_path=?", (rel,)).fetchone()

    # -- B3 / B4 并发保护状态 -------------------------------------------------

    @staticmethod
    def _generation_row(cur, rel: str) -> int:
        """写事务内读取删除代次（不存在即 0）。"""
        row = cur.execute(
            "SELECT generation FROM vault_path_state WHERE stored_path=?",
            (rel,)).fetchone()
        return 0 if row is None else int(row["generation"])

    @staticmethod
    def _generation_at_read(rel: str) -> int:
        """B4：代次必须**在读文件之前**取得，删除之后推进代次才能让本轮失效。"""
        row = db.get_read_conn().execute(
            "SELECT generation FROM vault_path_state WHERE stored_path=?",
            (rel,)).fetchone()
        return 0 if row is None else int(row["generation"])

    @staticmethod
    def _current_snapshot(path: Path):
        """B3：重新读取当前文件快照；文件已不在返回 None（判为过期）。"""
        try:
            return snapshot_hash(path.read_text(encoding="utf-8"))
        except OSError:
            return None

    @staticmethod
    def _current_doc_id(rel: str):
        row = db.get_read_conn().execute(
            "SELECT id FROM knowledge_docs WHERE stored_path=?", (rel,)).fetchone()
        return None if row is None else row["id"]

    # -- 导入 / 更新 ---------------------------------------------------------

    def ingest(self, rel: str) -> dict:
        """导入或更新单个文档。返回处理结果元数据（不含正文）。"""
        # B5：先归一为规范 POSIX 相对路径（DB 身份键），再做任何读/写
        rel = vault.normalize_relpath(rel)
        # B4：删除代次在读文件之前取得（删除发生在读取之后才能让本轮失效）
        generation = self._generation_at_read(rel)
        text, doc, path = self._read_validated(rel)
        snapshot = snapshot_hash(text)
        meta, body = doc["meta"], doc["body"]
        owner = meta["owner_role"]
        vis = self._effective_visibility(meta)
        digest = body_hash(body)
        now = db.now_iso()
        filename = Path(rel).name

        # TX1：登记/更新身份与权限（C6：即使正文未变也立即更新 visibility/owner）
        # B3/B4：进入写事务后重新核对两件事，任一不成立即整轮判过期、不落库：
        #   ① 删除代次未被显式删除推进（否则本轮属于已删除的旧路径身份）；
        #   ② 文件当前快照仍是本轮读到的那个（否则旧快照会覆盖新权限）。
        # 校验与写入同在 BEGIN IMMEDIATE 事务内，避免"检查完再被改"的窗口。
        try:
            with self._writer.transaction() as cur:
                if self._generation_row(cur, rel) != generation:
                    raise _StaleRound("delete_generation_advanced")
                if self._current_snapshot(path) != snapshot:
                    raise _StaleRound("snapshot_changed")

                row = self._doc_row(cur, rel)
                if row is None:
                    cur.execute(
                        "INSERT INTO knowledge_docs(filename,stored_path,file_hash,"
                        "owner_role,visibility,chunk_count,status,created_at,"
                        "source_hash,snapshot_hash,updated_at) "
                        "VALUES(?,?,?,?,?,0,'processing',?,?,?,?)",
                        (filename, rel, digest, owner, vis, now, digest, snapshot, now))
                    doc_id = cur.lastrowid
                    already_indexed = False
                else:
                    doc_id = row["id"]
                    already_indexed = (row["indexed_hash"] == digest
                                       and row["embed_model"] == config.EMBEDDING_MODEL
                                       and row["embed_dim"] == config.EMBEDDING_DIM
                                       and row["status"] == "ready")
                    # reindex 期间保持 ready（旧索引继续可搜）；首次导入才是 processing
                    status = "ready" if row["indexed_hash"] is not None else "processing"
                    cur.execute(
                        "UPDATE knowledge_docs SET filename=?,file_hash=?,owner_role=?,"
                        "visibility=?,source_hash=?,snapshot_hash=?,status=?,updated_at=? "
                        "WHERE id=?",
                        (filename, digest, owner, vis, digest, snapshot, status, now, doc_id))
        except _StaleRound as stale:
            audit_log.log("INFO", "index_stale_round_discarded",
                          target_table="knowledge_docs",
                          target_id=self._current_doc_id(rel),
                          result_code=stale.code, source=rel)
            return {"doc_id": self._current_doc_id(rel), "status": "stale",
                    "reindexed": False, "stale": True,
                    "owner_corrected": doc["owner_corrected"]}

        if already_indexed:
            return {"doc_id": doc_id, "status": "ready", "reindexed": False,
                    "stale": False, "owner_corrected": doc["owner_corrected"]}

        try:
            pieces = chunking.chunk_text(body)
            vectors = self._embedder().embed(pieces) if pieces else []
            if len(vectors) != len(pieces):
                raise ValueError("embed_count_mismatch")
            blobs = [knowledge_index.encode_vector(v) for v in vectors]
        except Exception as exc:  # noqa: BLE001 — 失败只记错误码，不落正文
            code = f"{type(exc).__name__}:{exc}"[:200]
            # B1 修复：失败状态同样受版本与 missing 保护。旧轮次失败不得把
            # 新版本（或 watcher 判定的 missing）改成 failed，否则新索引会被
            # 挤出检索视图。rowcount==0 即本轮已过期，丢弃失败标记。
            with self._writer.transaction() as cur:
                cur.execute(
                    "UPDATE knowledge_docs SET status='failed',last_error=?,updated_at=? "
                    "WHERE id=? AND source_hash=? AND snapshot_hash=? AND status<>'missing'",
                    (code, db.now_iso(), doc_id, digest, snapshot))
                stale_failure = cur.rowcount == 0
            if stale_failure:
                audit_log.log("INFO", "index_stale_failure_discarded",
                              target_table="knowledge_docs", target_id=doc_id,
                              result_code="index_stale_failure_discarded", source=rel)
                return {"doc_id": doc_id, "status": "stale", "reindexed": False,
                        "stale": True, "owner_corrected": doc["owner_corrected"]}
            audit_log.log("ERROR", "knowledge_index_failed", target_table="knowledge_docs",
                          target_id=doc_id, result_code="index_failed", source=rel)
            return {"doc_id": doc_id, "status": "failed", "reindexed": False,
                    "stale": False, "error": code,
                    "owner_corrected": doc["owner_corrected"]}

        # TX2：B8 条件提交——source_hash 仍是本轮读到的值，且文档未进入 missing
        # （B2 修复：mark_missing 不改 source_hash，只靠 hash 条件会让旧任务
        #  把已消失的文件复活成 ready 并重新插入 chunks）
        # B3/B4 追加：快照未被更新轮次推进、删除代次未被显式删除推进，
        # 否则本轮 chunks 属于过期身份，整事务回滚。
        dim = len(vectors[0]) if vectors else config.EMBEDDING_DIM
        try:
            with self._writer.transaction() as cur:
                cur.execute(
                    "UPDATE knowledge_docs SET indexed_hash=?,chunk_count=?,embed_model=?,"
                    "embed_dim=?,status='ready',last_error=NULL,updated_at=? "
                    "WHERE id=? AND source_hash=? AND snapshot_hash=? AND status<>'missing' "
                    "AND COALESCE((SELECT generation FROM vault_path_state "
                    "WHERE stored_path=?),0)=?",
                    (digest, len(pieces), config.EMBEDDING_MODEL, dim,
                     db.now_iso(), doc_id, digest, snapshot, rel, generation))
                if cur.rowcount == 0:
                    # 本轮结果已被更新版本取代：整事务回滚，一个 chunk 都不写
                    audit_log.log("INFO", "index_stale_discarded",
                                  target_table="knowledge_docs", target_id=doc_id,
                                  result_code="index_stale_discarded", source=rel)
                    raise _StaleIndex()
                cur.execute("DELETE FROM knowledge_chunks WHERE doc_id=?", (doc_id,))
                for seq, (piece, blob) in enumerate(zip(pieces, blobs)):
                    cur.execute(
                        "INSERT INTO knowledge_chunks(doc_id,seq,content,embedding) "
                        "VALUES(?,?,?,?)", (doc_id, seq, piece, blob))
        except _StaleIndex:
            return {"doc_id": doc_id, "status": "stale", "reindexed": False,
                    "stale": True, "owner_corrected": doc["owner_corrected"]}

        return {"doc_id": doc_id, "status": "ready", "reindexed": True, "stale": False,
                "chunk_count": len(pieces), "owner_corrected": doc["owner_corrected"]}

    def write_doc(self, rel: str, meta: dict, body: str) -> dict:
        """老板写盘路径：意图表 → 原子写 → 清意图 → 索引。崩溃可幂等重放。"""
        # B5：意图表键、落盘路径、索引身份必须同一个规范串
        rel = vault.normalize_relpath(rel)
        vault.resolve_safe_path(self._root, rel)          # 先校验，后落盘
        owner = vault.owner_for_relpath(rel)
        meta = dict(meta, owner_role=owner)
        if owner == vault.SHARED_OWNER:
            meta["visibility"] = "public"
        text = vault.serialize_frontmatter(meta, body)
        with self._writer.transaction() as cur:
            cur.execute(
                "INSERT INTO pending_vault_writes(stored_path,content,created_at) "
                "VALUES(?,?,?) ON CONFLICT(stored_path) DO UPDATE SET "
                "content=excluded.content,created_at=excluded.created_at",
                (rel, text, db.now_iso()))
        vault.atomic_write(vault.resolve_safe_path(self._root, rel), text)
        with self._writer.transaction() as cur:
            cur.execute("DELETE FROM pending_vault_writes WHERE stored_path=?", (rel,))
        return self.ingest(rel)

    # -- 检索 ---------------------------------------------------------------

    def search(self, query: str, caller_role: str, *, top_k: int | None = None) -> list[dict]:
        """Agent 检索。**先经 M2 治理闸门过滤，再打分**。

        caller_role 由框架注入（上游经 resolve_caller），本服务不采信 LLM 身份字段。
        无 Key/熔断时抛 LLMFailure，由上层降级，不返回未过滤结果。
        """
        k = config.KNOWLEDGE_TOP_K if top_k is None else top_k
        rows = visibility.query_visible("knowledge_chunks_visible", caller_role,
                                        status="ready")
        if not rows:
            return []
        qvec = self._embedder().embed([query])[0]
        hits = knowledge_index.rank_chunks(rows, qvec, top_k=k)
        # PRD §5.3：低于阈值视为未命中，Agent 据此明确回答"没有相关内容"
        return [h for h in hits if h["score"] >= config.KNOWLEDGE_MIN_SCORE]

    def list_docs(self, caller_role: str, *, owner_role: str | None = None) -> list[dict]:
        """文档清单，经治理闸门过滤（不含正文与向量）。"""
        filters = {} if owner_role is None else {"owner_role": owner_role}
        rows = visibility.query_visible("knowledge_docs", caller_role, **filters)
        return [{"id": r["id"], "filename": r["filename"], "vault_path": r["stored_path"],
                 "owner_role": r["owner_role"], "visibility": r["visibility"],
                 "status": r["status"], "chunk_count": r["chunk_count"]} for r in rows]

    # -- 删除与恢复 ----------------------------------------------------------

    def delete(self, rel: str, *, identity=None) -> dict:
        """删除协议：鉴权 → 意图 → 删盘 → 删库 → 清意图。任一步崩溃可幂等重放。

        绝不采用"DB 先删 → 失败仅 WARN → watcher 猜孤儿"（用户已否决）。

        授权边界在**本方法内部**：调用方不得只靠"没暴露入口"或自报 role。
        身份必须是 identity_api 签发并已登记校验的 Identity 实例（裸
        caller_role 字符串一律视为无可信身份），角色非 boss 即拒绝，且拒绝
        先于任何路径解析与磁盘动作。
        """
        role = self._authorize_delete(identity)
        # B5：删除与导入必须命中同一个身份（反斜杠/正斜杠不算两个路径）
        rel = vault.normalize_relpath(rel)
        vault.resolve_safe_path(self._root, rel)   # 先校验路径，再动盘
        with self._writer.transaction() as cur:
            row = self._doc_row(cur, rel)
            doc_id = row["id"] if row is not None else None
            cur.execute(
                "INSERT INTO pending_deletions(doc_id,stored_path,created_at) "
                "VALUES(?,?,?) ON CONFLICT(stored_path) DO NOTHING",
                (doc_id, rel, db.now_iso()))
        self._replay_deletion(rel, doc_id)
        return {"deleted": True, "doc_id": doc_id, "role": role}

    def _authorize_delete(self, identity) -> str:
        """唯一删除授权判定：登记校验 + boss 限定；任何不确定都 fail closed。"""

        def _deny(code: str, role: str = "unknown") -> None:
            audit_log.log("WARN", "knowledge_delete_denied", actor_role=role,
                          target_table="knowledge_docs",
                          result_code=code, source="knowledge_service.delete")
            raise DeleteDenied(code)

        if self._identity_api is None:
            _deny("identity_api_unavailable")
        if identity is None:
            _deny("identity_required")
        try:
            role = self._identity_api.authorize(identity)
        except PermissionError:
            _deny("identity_not_authorized")
        if role != "boss":
            _deny("forbidden_role", role)
        return role

    def _replay_deletion(self, rel: str, doc_id) -> None:
        rel = vault.normalize_relpath(rel)      # B5：墓碑/文档行同一身份
        path = vault.resolve_safe_path(self._root, rel)
        if path.exists():
            path.unlink()
        with self._writer.transaction() as cur:
            # B4：显式删除推进持久代次（文档行删掉后本表仍保留墓碑）。
            # 已读取旧文件的 ingest 轮次因此失效，不得重新创建该路径文档。
            cur.execute(
                "INSERT INTO vault_path_state(stored_path,generation,updated_at) "
                "VALUES(?,1,?) ON CONFLICT(stored_path) DO UPDATE SET "
                "generation=generation+1, updated_at=excluded.updated_at",
                (rel, db.now_iso()))
            if doc_id is None:
                row = self._doc_row(cur, rel)
                doc_id = row["id"] if row is not None else None
            if doc_id is not None:
                cur.execute("DELETE FROM knowledge_chunks WHERE doc_id=?", (doc_id,))
                cur.execute("DELETE FROM knowledge_docs WHERE id=?", (doc_id,))
            cur.execute("DELETE FROM knowledge_docs WHERE stored_path=?", (rel,))
            cur.execute("DELETE FROM pending_deletions WHERE stored_path=?", (rel,))

    def mark_missing(self, rel: str) -> dict:
        """watcher 判定文件消失：标记 missing，**保留索引供恢复**（PRD §5.3）。

        与 delete() 的区别：delete 是老板显式确认的彻底删除（连带文件与索引）；
        missing 只是文件不在了，索引保留 MISSING_RETENTION_DAYS 天。
        missing 文档不参与检索（视图按 status='ready' 过滤）。
        """
        rel = vault.normalize_relpath(rel)      # B5：与 ingest 同一身份键
        with self._writer.transaction() as cur:
            cur.execute(
                "UPDATE knowledge_docs SET status='missing',updated_at=? "
                "WHERE stored_path=? AND status<>'missing'", (db.now_iso(), rel))
            changed = cur.rowcount
        if changed:
            audit_log.log("WARN", "knowledge_doc_missing", target_table="knowledge_docs",
                          result_code="missing", source=rel)
        return {"missing": bool(changed), "vault_path": rel}

    def purge_missing(self, *, now=None) -> dict:
        """清理超过保留期的 missing 文档（索引与元数据一并删除）。

        purge 与显式 delete 一样**清除文档身份**：必须在同一个写事务内先推进
        该路径的删除代次，再删 chunks 与文档行。否则 purge 之前已读到文件、
        还在 embedding 的旧轮次，可在文件后来原样重现时凭"快照+旧代次都匹配"
        重新插入一条已清理的文档（用户第三轮裁决）。

        **候选复核（第四轮阻断）**：候选列表是在写事务**之外**查到的，期间文档
        可能已被 ingest 恢复（status → ready、updated_at 前进）或已被 delete
        清行。因此每条候选进入写事务后必须重新确认"仍是 missing 且仍早于
        cutoff"，条件不成立就跳过：不得推进代次、不得删 chunks、不得删行，
        否则一份已成功恢复并重建索引的文档会被过期清理任务永久删除。
        复核与清理同在 BEGIN IMMEDIATE 事务内，复核后不存在被抢写的窗口。
        """
        from datetime import datetime, timedelta, timezone
        moment = now or datetime.now(timezone.utc)
        cutoff = (moment - timedelta(days=config.MISSING_RETENTION_DAYS)).isoformat()
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT id,stored_path FROM knowledge_docs WHERE status='missing' "
            "AND updated_at IS NOT NULL AND updated_at<? ORDER BY id", (cutoff,)).fetchall()
        purged = 0
        for row in rows:
            rel = vault.normalize_relpath(row["stored_path"])   # B5：历史行兜底归一
            with self._writer.transaction() as cur:
                still = cur.execute(
                    "SELECT stored_path FROM knowledge_docs "
                    "WHERE id=? AND status='missing' "
                    "AND updated_at IS NOT NULL AND updated_at<?",
                    (row["id"], cutoff)).fetchone()
                if still is None:
                    audit_log.log("INFO", "knowledge_purge_skip_restored",
                                  target_table="knowledge_docs", target_id=row["id"],
                                  result_code="candidate_no_longer_missing", source=rel)
                    continue
                rel = vault.normalize_relpath(still["stored_path"])
                cur.execute(
                    "INSERT INTO vault_path_state(stored_path,generation,updated_at) "
                    "VALUES(?,1,?) ON CONFLICT(stored_path) DO UPDATE SET "
                    "generation=generation+1, updated_at=excluded.updated_at",
                    (rel, db.now_iso()))
                cur.execute("DELETE FROM knowledge_chunks WHERE doc_id=?", (row["id"],))
                cur.execute("DELETE FROM knowledge_docs WHERE id=?", (row["id"],))
                purged += 1
        return {"purged": purged, "skipped": len(rows) - purged}

    def recover(self) -> dict:
        """启动恢复：重放未完成意图 + 复位中断的 processing。"""
        conn = db.get_conn()
        deletions = conn.execute(
            "SELECT doc_id,stored_path FROM pending_deletions ORDER BY id").fetchall()
        writes = conn.execute(
            "SELECT stored_path,content FROM pending_vault_writes ORDER BY id").fetchall()
        stuck = conn.execute(
            "SELECT id,stored_path FROM knowledge_docs WHERE status='processing' "
            "ORDER BY id").fetchall()
        for row in deletions:
            self._replay_deletion(row["stored_path"], row["doc_id"])
        for row in writes:
            rel = vault.normalize_relpath(row["stored_path"])   # B5：与 ingest 同一身份
            vault.atomic_write(vault.resolve_safe_path(self._root, rel), row["content"])
            with self._writer.transaction() as cur:
                cur.execute("DELETE FROM pending_vault_writes WHERE stored_path=?", (rel,))
        for row in stuck:
            with self._writer.transaction() as cur:
                cur.execute(
                    "UPDATE knowledge_docs SET status='failed',last_error=?,updated_at=? "
                    "WHERE id=? AND status='processing'",
                    ("interrupted", db.now_iso(), row["id"]))
            audit_log.log("WARN", "knowledge_processing_reset",
                          target_table="knowledge_docs", target_id=row["id"],
                          result_code="interrupted", source=row["stored_path"])
        return {"deletions_replayed": len(deletions), "writes_replayed": len(writes),
                "processing_reset": len(stuck)}

    # -- 快照（watcher 用） ---------------------------------------------------

    def snapshot(self) -> dict:
        """扫描 vault 内可归属的 .md 文件 → {rel: (mtime, size)}。不做索引。"""
        snap: dict = {}
        for path in sorted(self._root.rglob("*.md")):
            rel = path.relative_to(self._root).as_posix()
            try:
                rel = vault.normalize_relpath(rel)      # B5：watcher 与 ingest 同一身份
                vault.resolve_safe_path(self._root, rel)
                vault.owner_for_relpath(rel)
            except ValueError:
                continue                      # attachments/未知目录/非法路径不纳入
            st = path.stat()
            snap[rel] = (st.st_mtime, st.st_size)
        return snap


class _StaleIndex(Exception):
    """内部信号：本轮索引结果过期，用于触发事务回滚（不外泄）。"""


class DeleteDenied(Exception):
    """删除鉴权失败（fail closed）。code 是稳定错误码，可直接回给 UI。

    服务层是授权边界的**唯一落点**：工具层只负责传入 Identity 实例，
    不得自行以 role 字符串放行。
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _StaleRound(Exception):
    """B3/B4 内部信号：TX1 复核不通过（快照已变 / 删除代次已推进）。

    携带稳定错误码，只进审计，不出现在 Agent 可见错误里。
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code