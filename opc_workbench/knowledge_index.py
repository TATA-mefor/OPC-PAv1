"""向量编解码与检索打分（M4 第二批）。

纪律：
- 只依赖已锁定的 numpy，零新依赖；不触网、不读库、不做 IO。
- 存储统一 float32 且**入库前 L2 归一化**，检索用点积即余弦。
- 维度/模型不一致的向量**判为不可比并跳过**（记 WARN），绝不静默混算。
- 本模块不落任何正文到日志。
"""
from __future__ import annotations

import numpy as np

from . import audit_log


def encode_vector(vec) -> bytes:
    """校验 + L2 归一化 + float32 序列化。非法向量一律拒绝，不入库。"""
    arr = np.asarray(vec, dtype=np.float32)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("vector_invalid")
    if not np.all(np.isfinite(arr)):
        raise ValueError("vector_not_finite")
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        raise ValueError("vector_zero_norm")
    return (arr / norm).tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size == 0:
        raise ValueError("vector_empty")
    return arr


def normalize_query(vec) -> np.ndarray:
    """查询向量归一化（与入库同口径，保证点积=余弦）。"""
    return np.frombuffer(encode_vector(vec), dtype=np.float32)


def rank_chunks(rows: list, query_vec, *, top_k: int) -> list[dict]:
    """对**已经过可见性过滤**的行打分排序。

    入参 rows 必须由 visibility.query_visible 产出（含 owner_role/visibility）。
    本函数不做任何权限判断——权限在上游闸门完成，此处只负责相似度。
    维度与查询不一致的行跳过并记 WARN（不含正文）。
    """
    if top_k <= 0:
        raise ValueError("top_k_invalid")
    q = normalize_query(query_vec)
    scored: list[dict] = []
    for row in rows:
        vec = decode_vector(row["embedding"])
        if vec.size != q.size:
            audit_log.log("WARN", "chunk_dim_mismatch", target_table="knowledge_chunks",
                          target_id=row["doc_id"], result_code="chunk_dim_mismatch")
            continue
        score = float(np.dot(q, vec))
        scored.append({
            "doc_id": row["doc_id"], "seq": row["seq"], "content": row["content"],
            "stored_path": row["stored_path"], "vault_path": row["stored_path"],
            "filename": row["filename"],
            "owner_role": row["owner_role"], "visibility": row["visibility"],
            "score": score,
        })
    scored.sort(key=lambda h: (-h["score"], h["doc_id"], h["seq"]))
    return scored[:top_k]