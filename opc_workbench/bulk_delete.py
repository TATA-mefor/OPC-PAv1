"""bulk_delete 治理（M3.1 清单 §三.1 方案 A 定稿实现）。

独立三函数 + 工具入口，M2 披露函数零改动：
- request_bulk_delete：Agent ≥5 → 审批单（payload 冻结 + digest 独立列）；
  同 requester+table+ids 的 pending 单去重复用。
- resolve_bulk_delete：仅 boss 决议（Agent 自批/他批 PermissionError）。
- consume_bulk_delete：requester 或 boss 消费；摘要校验 → 条件 UPDATE →
  逐条版本 + write_where 校验 → 同事务全删；任一失败整体 ROLLBACK。
- bulk_delete_action：工具入口阈值分流（≥5 Agent 审批 / boss 直执需确认
  令牌 / <5 任何 caller 直删）。

原子性六约束（v2 定稿）：消费与删除同事务 / 全删或全不删 / kind 进所有
最终 UPDATE / 并发恰好一次 / 过期两态清扫 / 非法 expires_at 类型 fail closed。

身份纪律（v7）：三入口签名无裸 requester/actor；_frame_caller/_llm_args/
_trusted_ctx 经注入的 identity_api.resolve_identity 解析，任何调用路径
（含普通 Python 直调）同等校验。

写能力纪律（v10.2）：全部写 SQL 只经注入的 CASService writer；
读快照经 query_visible / 只读连接。本模块不 import db 取写连接。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import audit_log, db, visibility
from .identity import canonical_json

APPROVAL_TTL_S = 24 * 3600
_DECISIONS = frozenset({"approved", "rejected"})
_TABLES = frozenset({"tasks", "schedules", "artifacts"})
BULK_THRESHOLD = 5

PLACEHOLDER = visibility.PLACEHOLDER   # 失败同构（与 M2 一致）


def _now() -> float:
    return db_time()


def db_time() -> float:
    import time
    return time.time()


def _has_json1(conn) -> bool:
    try:
        conn.execute("SELECT json_extract('{\"a\":1}','$.a')").fetchone()
        return True
    except sqlite3.OperationalError:
        return False


_JSON_NUM_GUARD = "json_type(payload,'$.expires_at') IN ('integer','real')"


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _core_digest(payload: dict) -> str:
    core = {k: payload[k] for k in ("table", "ids", "versions", "requester", "expires_at")}
    return hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()


class BulkDeleteContext:
    """治理函数的装配依赖（组合根注入；无模块级全局）。"""

    __slots__ = ("identity_api", "cas_service")

    def __init__(self, *, identity_api, cas_service):
        object.__setattr__(self, "identity_api", identity_api)
        object.__setattr__(self, "cas_service", cas_service)


def _writer_of(ctx: BulkDeleteContext):
    return object.__getattribute__(ctx.cas_service, "_writer")


def _read_conn():
    return db.get_read_conn()


# ---------------------------------------------------------------------------
# request：Agent 审批单创建（boss 经工具入口走直执，不进此函数的审批分支）
# ---------------------------------------------------------------------------

def request_bulk_delete(table: str, ids, *, _frame_caller: str, _llm_args: dict,
                        _trusted_ctx=None, _ctx: BulkDeleteContext) -> int | dict:
    """签名无 requester 参数（直传 requester="boss" → TypeError）。

    requester 唯一来源 = resolve_identity；versions 快照系统内部读取
    （query_visible 逐条），调用方无入口。
    """
    if table not in _TABLES:
        raise ValueError(f"forbidden_table:{table}")
    ident = _ctx.identity_api.resolve_identity(_frame_caller, _llm_args, _trusted_ctx)
    requester = _ctx.identity_api.authorize(ident)
    ids_sorted = sorted({int(i) for i in ids})
    if not ids_sorted:
        raise ValueError("empty_ids")
    if requester == "boss":
        # boss 申请 → 直执方向（工具入口处理令牌门槛）；治理函数不产生审批单
        return {"error": {"code": "confirmation_required"}}
    # 去重：同 requester+table+ids 的未过期 pending 单复用
    conn = _read_conn()
    for row in conn.execute(
            "SELECT id,payload,digest FROM approvals "
            "WHERE kind='bulk_delete' AND status='pending'").fetchall():
        try:
            p = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        if (p.get("table") == table and p.get("requester") == requester
                and p.get("ids") == ids_sorted
                and _is_num(p.get("expires_at")) and p["expires_at"] > _now()):
            return row["id"]
    # 冻结版本快照：逐条经读连接读取当前 version（系统内部，非调用方传入）
    versions = {}
    for tid in ids_sorted:
        row = conn.execute(f"SELECT version FROM {table} WHERE id=?", (tid,)).fetchone()
        if row is None:
            return dict(PLACEHOLDER)   # 目标缺失：不产生审批单
        versions[str(tid)] = int(row["version"])
    payload = {"table": table, "ids": ids_sorted, "versions": versions,
               "requester": requester, "expires_at": _now() + APPROVAL_TTL_S}
    digest = _core_digest(payload)
    writer = _writer_of(_ctx)
    with writer.transaction() as cur:
        cur.execute(
            "INSERT INTO approvals(kind,payload,status,created_at,digest) "
            "VALUES('bulk_delete',?,'pending',?,?)",
            (json.dumps(payload, ensure_ascii=False), db.now_iso(), digest))
        aid = cur.lastrowid
    audit_log.log("INFO", "bulk_delete_requested", actor_role=requester,
                  target_table=table, approval_id=aid)
    return aid


# ---------------------------------------------------------------------------
# resolve：仅 boss 决议
# ---------------------------------------------------------------------------

def _sweep_expired(conn, approval_id: int) -> None:
    """惰性原子过期转换（写事务内，先于决议/消费）；pending+approved 两态。"""
    if _has_json1(conn):
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE id=? AND kind='bulk_delete' AND status IN ('pending','approved') "
            f"AND {_JSON_NUM_GUARD} "
            "AND json_extract(payload,'$.expires_at') <= ?",
            (db.now_iso(), approval_id, _now()))
    else:
        row = conn.execute(
            "SELECT payload FROM approvals WHERE id=? AND kind='bulk_delete' "
            "AND status IN ('pending','approved')", (approval_id,)).fetchone()
        if row is not None:
            try:
                expires = json.loads(row["payload"]).get("expires_at")
            except (ValueError, TypeError):
                expires = None
            if _is_num(expires) and expires <= _now():
                conn.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND kind='bulk_delete' "
                    "AND status IN ('pending','approved')",
                    (db.now_iso(), approval_id))


def resolve_bulk_delete(approval_id: int, decision: str, *, _frame_caller: str,
                        _llm_args: dict, _trusted_ctx=None,
                        _ctx: BulkDeleteContext) -> None:
    ident = _ctx.identity_api.resolve_identity(_frame_caller, _llm_args, _trusted_ctx)
    actor = _ctx.identity_api.authorize(ident)
    if actor != "boss":
        audit_log.log("WARN", "bulk_delete_resolve_denied",
                      actor_role=actor, approval_id=approval_id)
        raise PermissionError("boss_only")
    if decision not in _DECISIONS:
        raise ValueError(f"forbidden_decision:{decision}")
    writer = _writer_of(_ctx)
    with writer.transaction() as cur:
        _sweep_expired(cur, approval_id)
        if _has_json1(cur):
            cur.execute(
                "UPDATE approvals SET status=?, resolved_at=? "
                "WHERE id=? AND kind='bulk_delete' AND status='pending' "
                f"AND {_JSON_NUM_GUARD} "
                "AND json_extract(payload,'$.expires_at') > ?",
                (decision, db.now_iso(), approval_id, _now()))
        else:
            row = cur.execute(
                "SELECT payload FROM approvals WHERE id=? AND kind='bulk_delete' "
                "AND status='pending'", (approval_id,)).fetchone()
            if row is not None:
                try:
                    expires = json.loads(row["payload"]).get("expires_at")
                except (ValueError, TypeError):
                    expires = None
                if _is_num(expires) and expires > _now():
                    cur.execute(
                        "UPDATE approvals SET status=?, resolved_at=? "
                        "WHERE id=? AND kind='bulk_delete' AND status='pending'",
                        (decision, db.now_iso(), approval_id))
    audit_log.log("INFO", "bulk_delete_resolved", actor_role=actor,
                  approval_id=approval_id, result_code=decision)


# ---------------------------------------------------------------------------
# consume：摘要校验 + 条件消费 + 同事务全删
# ---------------------------------------------------------------------------

def _delete_targets(cur, table: str, ids, versions: dict, role: str) -> int:
    """逐条版本 + write_where 校验后 DELETE；任一不符抛错 → 外层整体回滚。"""
    from .cas import write_where
    frag, params = write_where(role)
    deleted = 0
    for tid in ids:
        expected = int(versions[str(tid)])
        cur.execute(
            f"DELETE FROM {table} WHERE id=? AND version=? AND {frag}",
            [int(tid), expected] + params)
        if cur.rowcount != 1:
            raise RuntimeError("bulk_delete_target_mismatch")
        deleted += 1
    return deleted


def consume_bulk_delete(approval_id: int, *, _frame_caller: str, _llm_args: dict,
                        _trusted_ctx=None, _ctx: BulkDeleteContext) -> dict:
    ident = _ctx.identity_api.resolve_identity(_frame_caller, _llm_args, _trusted_ctx)
    actor = _ctx.identity_api.authorize(ident)
    writer = _writer_of(_ctx)
    try:
        with writer.transaction() as cur:
            _sweep_expired(cur, approval_id)
            row = cur.execute(
                "SELECT payload,digest,status FROM approvals "
                "WHERE id=? AND kind='bulk_delete'", (approval_id,)).fetchone()
            if row is None or row["status"] != "approved":
                return dict(PLACEHOLDER)
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                raise RuntimeError("payload_corrupt")
            # actor ∈ {requester, boss}
            if actor != payload.get("requester") and actor != "boss":
                return dict(PLACEHOLDER)
            # expires_at 类型守卫 + 有效期（两路径一致 fail closed）
            expires = payload.get("expires_at")
            if not _is_num(expires) or expires <= _now():
                return dict(PLACEHOLDER)
            # 摘要校验：重算核心字段摘要与独立 digest 列比对
            if row["digest"] is None or _core_digest(payload) != row["digest"]:
                audit_log.log("ERROR", "bulk_delete_digest_mismatch",
                              actor_role=actor, approval_id=approval_id)
                raise RuntimeError("digest_mismatch")
            table = payload["table"]
            if table not in _TABLES:
                raise RuntimeError("forbidden_table")
            # 条件 UPDATE 消费（kind + status + json_type 守卫；并发恰好一次）
            if _has_json1(cur):
                cur.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND kind='bulk_delete' AND status='approved' "
                    f"AND {_JSON_NUM_GUARD} "
                    "AND json_extract(payload,'$.expires_at') > ?",
                    (db.now_iso(), approval_id, _now()))
            else:
                cur.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND kind='bulk_delete' AND status='approved'",
                    (db.now_iso(), approval_id))
            if cur.rowcount != 1:
                return dict(PLACEHOLDER)   # 并发已被消费
            deleted = _delete_targets(cur, table, payload["ids"],
                                      payload["versions"], actor)
        audit_log.log("INFO", "bulk_delete_consumed", actor_role=actor,
                      approval_id=approval_id, target_table=table,
                      result_code="ok")
        return {"deleted_n": deleted}
    except Exception:
        audit_log.log("ERROR", "bulk_delete_consume_failed",
                      actor_role=actor, approval_id=approval_id, result_code="error")
        return dict(PLACEHOLDER)


# ---------------------------------------------------------------------------
# 工具入口：阈值分流
# ---------------------------------------------------------------------------

def bulk_delete_action(table: str, ids, *, _frame_caller: str, _llm_args: dict,
                       _trusted_ctx=None, _ctx: BulkDeleteContext) -> dict:
    if table not in _TABLES:
        raise ValueError(f"forbidden_table:{table}")
    ident = _ctx.identity_api.resolve_identity(_frame_caller, _llm_args, _trusted_ctx)
    role = _ctx.identity_api.authorize(ident)
    ids_sorted = sorted({int(i) for i in ids})
    if not ids_sorted:
        raise ValueError("empty_ids")
    if len(ids_sorted) < BULK_THRESHOLD:
        return _direct_delete(_ctx, table, ids_sorted, role)
    if role == "boss":
        return _boss_direct(_ctx, table, ids_sorted, ident, _trusted_ctx)
    return request_bulk_delete(table, ids_sorted, _frame_caller=_frame_caller,
                               _llm_args=_llm_args, _trusted_ctx=_trusted_ctx,
                               _ctx=_ctx)


def _direct_delete(_ctx, table, ids_sorted, role) -> dict:
    """<5 直删：单事务逐条 write_where + 当前版本校验，全删或全不删。"""
    from .cas import write_where
    frag, params = write_where(role)
    writer = _writer_of(_ctx)
    try:
        with writer.transaction() as cur:
            deleted = 0
            for tid in ids_sorted:
                row = cur.execute(
                    f"SELECT version FROM {table} WHERE id=? AND {frag}",
                    [tid] + params).fetchone()
                if row is None:
                    raise RuntimeError("target_not_writable")
                cur.execute(
                    f"DELETE FROM {table} WHERE id=? AND version=? AND {frag}",
                    [tid, row["version"]] + params)
                if cur.rowcount != 1:
                    raise RuntimeError("delete_race")
                deleted += 1
        return {"deleted_n": deleted}
    except Exception:
        return dict(PLACEHOLDER)


def _boss_direct(_ctx, table, ids_sorted, ident, trusted_ctx) -> dict:
    """boss ≥5 直执：需确认令牌（M3 无生产构造点 → confirmation_required）。

    六步：claim 冻结快照 → BEGIN IMMEDIATE → 读当前版本 → 逐条比对 →
    不符整批拒绝回退 issued → 全匹配才删 → COMMIT → mark_consumed。
    """
    tok = getattr(trusted_ctx, "_token", None)
    if tok is None:
        return {"error": {"code": "confirmation_required"}}
    api = _ctx.identity_api
    snap = api.claim_token(tok.token_id, table, ids_sorted, _ctx=trusted_ctx)
    if snap is None:
        return {"error": {"code": "confirmation_required"}}
    frozen_ids, frozen_versions = snap
    from .cas import write_where
    role = api.authorize(ident)
    frag, params = write_where(role)
    writer = _writer_of(_ctx)
    try:
        with writer.transaction() as cur:
            for tid in frozen_ids:
                row = cur.execute(
                    f"SELECT version FROM {table} WHERE id=? AND {frag}",
                    [int(tid)] + params).fetchone()
                if row is None or row["version"] != int(frozen_versions[int(tid)]):
                    raise _Stale()
            for tid in frozen_ids:
                cur.execute(
                    f"DELETE FROM {table} WHERE id=? AND version=? AND {frag}",
                    [int(tid), int(frozen_versions[int(tid)])] + params)
                if cur.rowcount != 1:
                    raise _Stale()
    except _Stale:
        api.release_token(tok.token_id)
        return {"error": {"code": "confirmation_stale"}}
    except Exception:
        api.release_token(tok.token_id)
        return dict(PLACEHOLDER)
    api.mark_consumed(tok.token_id)
    audit_log.log("INFO", "bulk_delete_direct", actor_role="boss",
                  target_table=table, result_code="ok")
    return {"deleted_n": len(frozen_ids)}


class _Stale(Exception):
    pass