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
    """场景C：上下文净化原语（M5 context.py 消费）。

    caller 合法性在此入口强制校验（fail closed）；调用链仍应先经 resolve_caller。
    """
    if caller_role not in _VALID_CALLERS:
        raise ValueError(f"invalid_caller:{caller_role!r}")
    return [it for it in items if visible_filter(caller_role, it)]


# ---------------------------------------------------------------------------
# 唯一合法业务查询入口（表/列双白名单，禁任意拼接）
# ---------------------------------------------------------------------------

_QUERYABLE: dict[str, frozenset[str]] = {
    "tasks": frozenset({"status", "owner_role", "visibility", "priority", "due_at"}),
    "schedules": frozenset({"status", "owner_role", "visibility", "remind_at"}),
    "knowledge_docs": frozenset({"owner_role", "visibility", "status"}),
    # M4 第二批 C1（用户裁决 2026-11-22）：受控**只读视图**接入治理。
    # owner_role/visibility 由 knowledge_docs JOIN 取得，单一真源不变；
    # visible_filter / query_visible 执行逻辑零改动。视图不可写，
    # chunk 写入仍只经组合根注入的 WriteCapability。
    "knowledge_chunks_visible": frozenset(
        {"owner_role", "visibility", "status", "doc_id", "embed_model", "embed_dim"}),
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




def _is_num(value) -> bool:
    """expires_at 合法数值判定：int/float 且排除 bool（isinstance(True,int) 为真）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# JSON1 类型守卫：SQLite 跨类型比较会让 TEXT > 数字 恒真（复审实证的缺口），
# 必须显式限定 json_type 为 integer/real；字符串/布尔/null/数组/对象一律拒绝。
_JSON_NUM_GUARD = "json_type(payload,'$.expires_at') IN ('integer','real')"


def _sweep_expired(conn, approval_id: int) -> None:
    """惰性原子过期转换（必须在写事务内调用，先于决议/消费）。

    仅处理 kind='private_disclosure'；覆盖 pending 与 approved 两态；
    expires_at 必须为合法 JSON 数值且 <= now 才转 expired（类型非法不转换，
    同时决议/消费的类型守卫会拒绝授权——fail closed）。
    """
    if _has_json1(conn):
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE id=? AND kind='private_disclosure' "
            "AND status IN ('pending','approved') "
            f"AND {_JSON_NUM_GUARD} "
            "AND json_extract(payload,'$.expires_at') <= ?",
            (db.now_iso(), approval_id, _now()),
        )
    else:
        row = conn.execute(
            "SELECT payload FROM approvals "
            "WHERE id=? AND kind='private_disclosure' "
            "AND status IN ('pending','approved')",
            (approval_id,),
        ).fetchone()
        if row is not None:
            try:
                expires = json.loads(row["payload"]).get("expires_at")
            except (ValueError, TypeError):
                expires = None
            if _is_num(expires) and expires <= _now():
                conn.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND kind='private_disclosure' "
                    "AND status IN ('pending','approved')",
                    (db.now_iso(), approval_id),
                )


def _payload_expires(conn, approval_id: int):
    """备选路径：写锁内读取 expires_at；损坏/缺失/类型非法/非披露单返回 None。"""
    row = conn.execute(
        "SELECT payload FROM approvals "
        "WHERE id=? AND kind='private_disclosure' AND status='pending'",
        (approval_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        expires = json.loads(row["payload"]).get("expires_at")
    except (ValueError, TypeError):
        return None
    return expires if _is_num(expires) else None


# ---------------------------------------------------------------------------
# M2 披露治理服务（M3.2 复审：写能力构造期显式注入）
#
# 边界：只暴露三个披露业务方法，**不暴露 writer / cursor / transaction()**，
# 调用方无法经本服务执行任意 SQL。写能力由组合根显式注入；
# db 模块已删除 _internal_writer() 等普通模块可调用的生产写获取入口。
# SQL、状态机、类型守卫、PLACEHOLDER 与回滚语义与 M2 逐字一致。
# ---------------------------------------------------------------------------

class DisclosureService:
    """披露治理服务。不可变；构造期固定注入的 WriteCapability。"""

    __slots__ = ("_writer",)

    def __init__(self, writer):
        if not isinstance(writer, db.WriteCapability):
            raise TypeError("disclosure_writer_required")
        object.__setattr__(self, "_writer", writer)

    def __setattr__(self, *a):
        raise PermissionError("disclosure_service_immutable")

    def __getattribute__(self, name):
        # 不暴露 WriteCapability：外部读 _writer 一律拒。
        # 三个业务方法内部经 object.__getattribute__ 取用，不受影响。
        if name == "_writer":
            raise PermissionError("disclosure_service_writer_not_exposed")
        return object.__getattribute__(self, name)

    def request_disclosure(self, item_table: str, item_id: int, from_role: str,
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
        writer = object.__getattribute__(self, "_writer")
        with writer.transaction() as cur:
            cur.execute(
                "INSERT INTO approvals(kind,payload,status,created_at) "
                "VALUES('private_disclosure',?,'pending',?)",
                (json.dumps(payload, ensure_ascii=False), db.now_iso()),
            )
            approval_id = cur.lastrowid
        audit_log.log("INFO", "disclosure_requested", actor_role=from_role,
                      target_table=item_table, target_id=item_id, approval_id=approval_id)
        return approval_id

    def resolve_disclosure(self, approval_id: int, decision: str, *, actor: str) -> None:
        if actor != "boss":                       # 仅 boss；申请者不得自批
            audit_log.log("WARN", "disclosure_resolve_denied",
                          actor_role=actor, approval_id=approval_id)
            raise PermissionError("boss_only")
        if decision not in _DECISIONS:            # 白名单先校验，任意串不进状态字段
            raise ValueError(f"forbidden_decision:{decision}")
        # BEGIN IMMEDIATE/COMMIT/ROLLBACK 由事务原语保证，状态机与守卫 SQL 零改动。
        writer = object.__getattribute__(self, "_writer")
        rowcount = 0
        with writer.transaction() as conn:
            _sweep_expired(conn, approval_id)
            if _has_json1(conn):
                cur = conn.execute(
                    "UPDATE approvals SET status=?, resolved_at=? "
                    "WHERE id=? AND kind='private_disclosure' AND status='pending' "
                    f"AND {_JSON_NUM_GUARD} "
                    "AND json_extract(payload,'$.expires_at') > ?",
                    (decision, db.now_iso(), approval_id, _now()),
                )
                rowcount = cur.rowcount
            else:
                # 备选：写锁内校验过期时间后才允许最终更新（与 JSON1 语义一致）
                expires = _payload_expires(conn, approval_id)
                if expires is not None and expires > _now():
                    cur = conn.execute(
                        "UPDATE approvals SET status=?, resolved_at=? "
                        "WHERE id=? AND kind='private_disclosure' AND status='pending'",
                        (decision, db.now_iso(), approval_id),
                    )
                    rowcount = cur.rowcount
        # rowcount=0：已决议/已过期/不存在 → 幂等无操作，对外同构不区分原因
        audit_log.log("INFO", "disclosure_resolved", actor_role=actor,
                      approval_id=approval_id, result_code=decision if rowcount else "noop")

    def consume_grant(self, approval_id: int, to_role: str) -> dict:
        """原子消费（M2 复审修复）：授权变更 + payload 校验 + 目标读取同一写事务。

        - BEGIN IMMEDIATE 先取写锁，并发双消费恰好一个 rowcount=1。
        - 清扫覆盖过期 pending/approved；消费未获授权时提交清扫结果（状态准确）。
        - 获授权后任何异常（payload 损坏/目标缺失/读取失败）→ 整体 ROLLBACK，
          审批恢复 approved 可重试，绝不半消费、绝不泄露。
        - 一切失败模式返回同构 PLACEHOLDER。
        """
        if to_role not in config.ROLES:
            return dict(PLACEHOLDER)
        writer = object.__getattribute__(self, "_writer")
        try:
            with writer.transaction() as conn:
                _sweep_expired(conn, approval_id)
                won = False
                payload = None
                if _has_json1(conn):
                    cur = conn.execute(
                        "UPDATE approvals SET status='expired', resolved_at=? "
                        "WHERE id=? AND kind='private_disclosure' AND status='approved' "
                        "AND json_extract(payload,'$.to_role')=? "
                        f"AND {_JSON_NUM_GUARD} "
                        "AND json_extract(payload,'$.expires_at') > ?",
                        (db.now_iso(), approval_id, to_role, _now()),
                    )
                    won = cur.rowcount == 1
                    if won:
                        row = conn.execute(
                            "SELECT payload FROM approvals WHERE id=?", (approval_id,)
                        ).fetchone()
                        payload = json.loads(row["payload"])   # 异常 → 整体回滚
                else:
                    row = conn.execute(
                        "SELECT payload,status FROM approvals "
                        "WHERE id=? AND kind='private_disclosure'",
                        (approval_id,),
                    ).fetchone()
                    if row is not None and row["status"] == "approved":
                        data = json.loads(row["payload"])      # 异常 → 整体回滚
                        expires = data.get("expires_at")
                        if (data.get("to_role") == to_role
                                and _is_num(expires) and expires > _now()):
                            cur = conn.execute(
                                "UPDATE approvals SET status='expired', resolved_at=? "
                                "WHERE id=? AND kind='private_disclosure' AND status='approved'",
                                (db.now_iso(), approval_id),
                            )
                            won = cur.rowcount == 1
                            payload = data if won else None
                if not won:
                    # 正常退出事务 → COMMIT 仅持久化清扫转换；未获授权不消费
                    return dict(PLACEHOLDER)
                # 目标读取在同一事务内：失败则授权变更一并回滚
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
            # 事务原语已 ROLLBACK（审批恢复 approved，可重试；无半消费态）
            audit_log.log("ERROR", "grant_consume_failed",
                          actor_role=to_role, approval_id=approval_id, result_code="error")
            return dict(PLACEHOLDER)

        audit_log.log("INFO", "grant_consumed", actor_role=to_role,
                      approval_id=approval_id, target_table=payload["item_table"],
                      target_id=payload["item_id"], result_code="ok")
        return result


def _require_service(service) -> DisclosureService:
    """fail closed：未注入合法服务一律拒绝，绝不回退到任何全局写能力。"""
    if not isinstance(service, DisclosureService):
        raise PermissionError("disclosure_service_required")
    return service


def request_disclosure(service, item_table: str, item_id: int, from_role: str,
                       to_role: str, reason: str) -> int:
    """显式接收 service 的薄委托（无模块级服务槽）。"""
    return _require_service(service).request_disclosure(
        item_table, item_id, from_role, to_role, reason)


def resolve_disclosure(service, approval_id: int, decision: str, *, actor: str) -> None:
    """显式接收 service 的薄委托（无模块级服务槽）。"""
    return _require_service(service).resolve_disclosure(approval_id, decision, actor=actor)


def consume_grant(service, approval_id: int, to_role: str) -> dict:
    """显式接收 service 的薄委托（无模块级服务槽）。"""
    return _require_service(service).consume_grant(approval_id, to_role)
