"""可见性闸门（M2 核心交付，M2.1 清单 v3 §四）。

三场景三规则，显式分离：
- 普通读取 visible_filter：boss 全可见；coordinator 仅 public（自己的 private
  也不可见）；其他 Agent 自见 public+private、他角色仅 public。
- 跨角色交接 handoff_filter：仅 public，与 owner 是谁无关。
- Coordinator 上下文 filter_context_items：等价 coordinator 读取。

纪律：本模块不 import llm_client、不发起网络、不依赖 UI；
caller 由框架注入并经 resolve_caller 校验（fail closed）；
业务查询唯一入口 query_visible（表/列双白名单 + 参数化）；
grant_once 原子消费 = 单条条件 UPDATE + rowcount 判定，禁止先读后改。
"""
from __future__ import annotations

import json
import sqlite3
import time

from . import audit_log, config, db

# ---------------------------------------------------------------------------
# 场景A：普通读取
# ---------------------------------------------------------------------------

_VALID_CALLERS = frozenset(config.ROLE_ORDER) | {"boss"}


def visible_filter(caller_role: str, row) -> bool:
    """row 需含 owner_role / visibility 键（sqlite3.Row 或 dict）。"""
    if caller_role == "boss":
        return True
    if caller_role == "coordinator":
        return row["visibility"] == "public"      # 自己的 private 也不可见
    if row["owner_role"] == caller_role:
        return True
    return row["visibility"] == "public"


# 场景B：跨角色交接——独立规则，不复用 visible_filter
def handoff_filter(item) -> bool:
    return item["visibility"] == "public"


def filter_context_items(items: list, caller_role: str) -> list:
    """场景C：上下文净化原语（M5 context.py 消费）。"""
    return [it for it in items if visible_filter(caller_role, it)]


# ---------------------------------------------------------------------------
# 唯一合法业务查询入口（表/列双白名单，禁任意拼接）
# ---------------------------------------------------------------------------

_QUERYABLE: dict[str, frozenset[str]] = {
    "tasks": frozenset({"status", "owner_role", "visibility", "priority", "due_at"}),
    "schedules": frozenset({"status", "owner_role", "visibility", "remind_at"}),
    "knowledge_docs": frozenset({"owner_role", "visibility", "status"}),
    "memory": frozenset({"owner_role", "visibility", "kind"}),
    "artifacts": frozenset({"owner_role", "visibility", "kind", "period_key"}),
}


def query_visible(table: str, caller_role: str, **filters) -> list:
    if table not in _QUERYABLE:
        raise ValueError(f"forbidden_table:{table}")
    if caller_role not in _VALID_CALLERS:
        raise ValueError(f"invalid_caller:{caller_role}")
    allowed = _QUERYABLE[table]
    for key in filters:
        if key not in allowed:
            raise ValueError(f"forbidden_column:{table}.{key}")
    # SQL 只用常量模板 + ? 占位；表名/列名全部来自上方白名单常量
    sql = f"SELECT * FROM {table}"
    params: list = []
    if filters:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in filters)
        params = list(filters.values())
    rows = db.get_conn().execute(sql, params).fetchall()
    return [r for r in rows if visible_filter(caller_role, r)]


# ---------------------------------------------------------------------------
# caller 身份（fail closed）
# ---------------------------------------------------------------------------

_SPOOF_FIELDS = ("caller_role", "owner_role", "actor", "role")


def resolve_caller(frame_caller: str, llm_args: dict) -> str:
    if frame_caller not in _VALID_CALLERS:
        raise ValueError(f"invalid_caller:{frame_caller!r}")   # 未知/空 → fail closed
    for field in _SPOOF_FIELDS:
        if field in llm_args:
            audit_log.log("WARN", "caller_spoof_dropped",
                          actor_role=frame_caller, target_table=field)
    return frame_caller


# ---------------------------------------------------------------------------
# 写入治理：session 合法但不落库
# ---------------------------------------------------------------------------

_VISIBILITIES = frozenset({"public", "private"})
_SCOPES = frozenset({"global", "session"})


def validate_write(owner_role: str, visibility: str, scope: str) -> None:
    if owner_role not in config.ROLES:
        raise ValueError(f"unknown_role:{owner_role}")
    if visibility not in _VISIBILITIES:      # internal 及一切非法值：所有路径拒绝
        raise ValueError(f"forbidden_visibility:{visibility}")
    if scope not in _SCOPES:
        raise ValueError(f"forbidden_scope:{scope}")
    # session 合法（会话内存使用），不抛错


def validate_persist(owner_role: str, visibility: str, scope: str) -> None:
    validate_write(owner_role, visibility, scope)
    if scope != "global":
        raise ValueError("session_not_persistable")


class SessionBuffer:
    """session 数据的合法内存居所：按 session_id 隔离，无任何 DB 写入路径。"""

    def __init__(self):
        self._data: dict[str, dict] = {}

    def put(self, session_id: str, key: str, value) -> None:
        self._data.setdefault(session_id, {})[key] = value

    def get(self, session_id: str, key: str, default=None):
        return self._data.get(session_id, {}).get(key, default)

    def clear(self, session_id: str) -> None:
        self._data.pop(session_id, None)


# ---------------------------------------------------------------------------
# 可见性判定：显式优先级链（v3 §四.7）
#   ① 用户显式/UI ② 强制规则(report→private / preference→public+global)
#   ③ 敏感词升级 ④ 角色默认
# ---------------------------------------------------------------------------

_KINDS = frozenset({"item", "report", "preference"})


def resolve_visibility(owner_role: str, text: str, *, kind: str = "item",
                       explicit: str | None = None) -> tuple[str, str, str]:
    """返回 (visibility, scope, source)。写入路径统一经此判定，禁止工具自判。"""
    if kind not in _KINDS:
        raise ValueError(f"forbidden_kind:{kind}")
    if owner_role not in config.ROLES:
        raise ValueError(f"unknown_role:{owner_role}")
    if explicit is not None:
        if explicit not in _VISIBILITIES:    # explicit 传 internal 等一律拒绝
            raise ValueError(f"forbidden_visibility:{explicit}")
        forced = (kind == "report" and explicit == "public")
        source = "explicit_override_forced" if forced else "explicit"
        return (explicit, "global", source)  # ① 最高优先级
    if kind == "report":
        return ("private", "global", "forced_report")     # ② 不靠敏感词判断
    if kind == "preference":
        return ("public", "global", "preference")         # ② 全员共享全局配置
    if any(h in text for h in config.SENSITIVE_HINTS):
        return ("private", "global", "sensitive")         # ③
    return (config.ROLES[owner_role]["default_visibility"], "global", "role_default")  # ④


# ---------------------------------------------------------------------------
# friend 式单次披露（原子消费 + 过期语义 + 占位符同构）
# ---------------------------------------------------------------------------

PLACEHOLDER = {"redacted": True}   # 所有失败模式返回同一结构；错误信息/日志对外同构
APPROVAL_TTL_S = 24 * 3600
_DECISIONS = frozenset({"approved", "rejected"})
_GRANTABLE_TABLES = frozenset({"tasks", "schedules", "knowledge_docs", "memory", "artifacts"})


def _now() -> float:
    """唯一时钟入口（epoch 秒）；测试 monkeypatch 做临界验证。"""
    return time.time()


def _has_json1(conn) -> bool:
    try:
        conn.execute("SELECT json_extract('{\"a\":1}','$.a')").fetchone()
        return True
    except sqlite3.OperationalError:
        return False


def request_disclosure(item_table: str, item_id: int, from_role: str,
                       to_role: str, reason: str) -> int:
    if item_table not in _GRANTABLE_TABLES:
        raise ValueError(f"forbidden_table:{item_table}")
    if from_role not in config.ROLES or to_role not in config.ROLES:
        raise ValueError("unknown_role")
    if from_role == to_role:
        raise ValueError("same_role_no_disclosure_needed")
    payload = {
        "item_table": item_table, "item_id": int(item_id),
        "from_role": from_role, "to_role": to_role,
        "reason": str(reason)[:200], "grant_once": True,
        "expires_at": _now() + APPROVAL_TTL_S,
    }
    conn = db.get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO approvals(kind,payload,status,created_at) "
            "VALUES('private_disclosure',?,'pending',?)",
            (json.dumps(payload, ensure_ascii=False), db.now_iso()),
        )
    audit_log.log("INFO", "disclosure_requested", actor_role=from_role,
                  target_table=item_table, target_id=item_id, approval_id=cur.lastrowid)
    return cur.lastrowid


def _sweep_expired(conn, approval_id: int) -> None:
    """惰性原子过期转换：过期 pending → expired（同写事务内，先于决议/消费）。"""
    if _has_json1(conn):
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE id=? AND status='pending' "
            "AND json_extract(payload,'$.expires_at') <= ?",
            (db.now_iso(), approval_id, _now()),
        )
    else:
        row = conn.execute(
            "SELECT payload FROM approvals WHERE id=? AND status='pending'",
            (approval_id,),
        ).fetchone()
        if row is not None:
            try:
                expires = json.loads(row["payload"]).get("expires_at")
            except (ValueError, TypeError):
                expires = None
            if isinstance(expires, (int, float)) and expires <= _now():
                conn.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND status='pending'",
                    (db.now_iso(), approval_id),
                )


def resolve_disclosure(approval_id: int, decision: str, *, actor: str) -> None:
    if actor != "boss":                       # 仅 boss；申请者不得自批
        audit_log.log("WARN", "disclosure_resolve_denied",
                      actor_role=actor, approval_id=approval_id)
        raise PermissionError("boss_only")
    if decision not in _DECISIONS:            # 白名单先校验，任意串不进状态字段
        raise ValueError(f"forbidden_decision:{decision}")
    conn = db.get_conn()
    with conn:
        _sweep_expired(conn, approval_id)
        if _has_json1(conn):
            cur = conn.execute(
                "UPDATE approvals SET status=?, resolved_at=? "
                "WHERE id=? AND status='pending' "
                "AND json_extract(payload,'$.expires_at') > ?",
                (decision, db.now_iso(), approval_id, _now()),
            )
        else:
            cur = conn.execute(
                "UPDATE approvals SET status=?, resolved_at=? "
                "WHERE id=? AND status='pending'",
                (decision, db.now_iso(), approval_id),
            )
    # rowcount=0：已决议/已过期/不存在 → 幂等无操作，对外同构不区分原因
    audit_log.log("INFO", "disclosure_resolved", actor_role=actor,
                  approval_id=approval_id, result_code=decision if cur.rowcount else "noop")


def consume_grant(approval_id: int, to_role: str) -> dict:
    """原子消费：恰好一个并发成功者；一切失败模式返回同构 PLACEHOLDER。"""
    if to_role not in config.ROLES:
        return dict(PLACEHOLDER)
    conn = db.get_conn()
    payload = None
    try:
        if _has_json1(conn):
            with conn:
                cur = conn.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND kind='private_disclosure' AND status='approved' "
                    "AND json_extract(payload,'$.to_role')=? "
                    "AND json_extract(payload,'$.expires_at') > ?",
                    (db.now_iso(), approval_id, to_role, _now()),
                )
            if cur.rowcount != 1:
                return dict(PLACEHOLDER)
            row = conn.execute(
                "SELECT payload FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            payload = json.loads(row["payload"])
        else:
            # 备选路径（v3 §八 六步事务边界）：BEGIN IMMEDIATE 先取写锁
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload,status FROM approvals "
                    "WHERE id=? AND kind='private_disclosure'",
                    (approval_id,),
                ).fetchone()
                if row is None or row["status"] != "approved":
                    conn.execute("ROLLBACK")
                    return dict(PLACEHOLDER)
                data = json.loads(row["payload"])
                if data.get("to_role") != to_role or data.get("expires_at", 0) <= _now():
                    conn.execute("ROLLBACK")
                    return dict(PLACEHOLDER)
                cur = conn.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND status='approved'",
                    (db.now_iso(), approval_id),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return dict(PLACEHOLDER)
                conn.execute("COMMIT")
                payload = data
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except Exception:
        # fail closed：payload 损坏/DB 异常 → 占位，绝不转成数据泄露
        audit_log.log("ERROR", "grant_consume_failed",
                      actor_role=to_role, approval_id=approval_id, result_code="error")
        return dict(PLACEHOLDER)

    try:
        table = payload["item_table"]
        if table not in _GRANTABLE_TABLES:
            raise ValueError("forbidden_table")
        item = conn.execute(
            f"SELECT * FROM {table} WHERE id=?", (int(payload["item_id"]),)
        ).fetchone()
        if item is None:
            raise ValueError("target_missing")
        result = dict(item)
    except Exception:
        # 目标缺失/读取失败：grant 已烧掉（安全方向），同构占位
        audit_log.log("ERROR", "grant_target_unavailable",
                      actor_role=to_role, approval_id=approval_id, result_code="missing")
        return dict(PLACEHOLDER)

    audit_log.log("INFO", "grant_consumed", actor_role=to_role,
                  approval_id=approval_id, target_table=payload["item_table"],
                  target_id=payload["item_id"], result_code="ok")
    return result
