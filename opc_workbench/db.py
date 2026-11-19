"""数据层：SQLite(WAL) 连接管理 + 内置顺序迁移框架 + 备份。

PRD §4/§14 纪律：
- 表结构变更一律新增迁移函数，禁止手改；每个迁移单事务。
- **迁移函数禁止 executescript()**：它会先隐式 COMMIT 挂起事务，破坏外层
  BEGIN/ROLLBACK 的原子性（M1 审查发现的缺口）。一律逐条 conn.execute()。
- init_db() 零播种（审计红旗：旧 seed_tasks 污染真实链路）。
- 角色枚举 CHECK 由 config.ROLES 生成，单一来源。
- 每线程独立连接；写串行由 SQLite WAL + busy_timeout 保证。
- 本模块不含业务 CRUD 与可见性逻辑（M2/M3 范围）。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

_LOCAL = threading.local()


def now_iso() -> str:
    """统一时间格式：aware UTC ISO8601（PRD §12.1，禁止 naive 入库）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _roles_sql() -> str:
    return ",".join(f"'{r}'" for r in config.ROLE_ORDER)


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """线程本地连接。db_path 缺省取 config.DB_PATH（测试经 monkeypatch 重定向）。"""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    conns: dict = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = _LOCAL.conns = {}
    key = str(path)
    conn = conns.get(key)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=config.DB_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={config.DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conns[key] = conn
    return conn


def close_thread_conns() -> None:
    """关闭当前线程全部连接（测试清理用）。"""
    conns: dict = getattr(_LOCAL, "conns", {})
    for conn in conns.values():
        conn.close()
    conns.clear()


# ---------------------------------------------------------------------------
# 迁移框架：schema_version + 顺序迁移函数列表
# ---------------------------------------------------------------------------

def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )


def current_version(conn: sqlite3.Connection) -> int:
    _ensure_version_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM schema_version").fetchone()
    return int(row["v"])


# DDL 文本内不含分号字面量，可安全按 ';' 切分为独立语句逐条执行。
_M001_SQL = """
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'todo'
                CHECK(status IN ('todo','doing','done','cancelled')),
    priority    TEXT NOT NULL DEFAULT 'mid'
                CHECK(priority IN ('low','mid','high')),
    due_at      TEXT,
    owner_role  TEXT NOT NULL CHECK(owner_role IN ({ROLES})),
    visibility  TEXT NOT NULL DEFAULT 'public'
                CHECK(visibility IN ('public','private')),
    scope       TEXT NOT NULL DEFAULT 'global'
                CHECK(scope IN ('global')),
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    remind_at   TEXT NOT NULL,
    repeat_rule TEXT NOT NULL DEFAULT 'none'
                CHECK(repeat_rule IN ('none','daily','weekly')),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','notified','done','cancelled')),
    owner_role  TEXT NOT NULL CHECK(owner_role IN ({ROLES})),
    visibility  TEXT NOT NULL DEFAULT 'public'
                CHECK(visibility IN ('public','private')),
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE knowledge_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL UNIQUE,
    file_hash   TEXT NOT NULL,
    owner_role  TEXT NOT NULL CHECK(owner_role IN ({ROLES})),
    visibility  TEXT NOT NULL DEFAULT 'public'
                CHECK(visibility IN ('public','private')),
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'ready'
                CHECK(status IN ('processing','ready','failed','missing')),
    created_at  TEXT NOT NULL
);

CREATE TABLE knowledge_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    content     TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    UNIQUE(doc_id, seq)
);

CREATE TABLE memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    summary     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'episode'
                CHECK(kind IN ('episode','preference')),
    owner_role  TEXT NOT NULL CHECK(owner_role IN ({ROLES})),
    visibility  TEXT NOT NULL DEFAULT 'private'
                CHECK(visibility IN ('public','private')),
    created_at  TEXT NOT NULL
);

CREATE TABLE artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL
                CHECK(kind IN ('daily_report','weekly_report','other')),
    title       TEXT NOT NULL,
    content_md  TEXT NOT NULL,
    refs_json   TEXT NOT NULL DEFAULT '{}',
    owner_role  TEXT NOT NULL CHECK(owner_role IN ({ROLES})),
    visibility  TEXT NOT NULL DEFAULT 'private'
                CHECK(visibility IN ('public','private')),
    period_key  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(kind, period_key, owner_role)
);

CREATE TABLE approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL
                CHECK(kind IN ('private_disclosure','bulk_delete')),
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected','expired')),
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_tasks_owner_vis_status ON tasks(owner_role, visibility, status);
CREATE INDEX idx_tasks_due ON tasks(due_at);
CREATE INDEX idx_sched_remind ON schedules(status, remind_at);
CREATE INDEX idx_chunks_doc ON knowledge_chunks(doc_id);
CREATE INDEX idx_memory_owner_vis ON memory(owner_role, visibility);
CREATE INDEX idx_approvals_status ON approvals(status);
"""


def _m001_statements() -> list[str]:
    """把 DDL 文本切为独立语句（测试可注入中途失败）。"""
    sql = _M001_SQL.replace("{ROLES}", _roles_sql())
    return [s.strip() for s in sql.split(";") if s.strip()]


def m001_init(conn: sqlite3.Connection) -> None:
    """PRD §4.2 全部核心表。逐条 execute，保持在外层迁移事务内。

    scope 仅允许 global（session 不落库）；internal 预留不启用。
    """
    for stmt in _m001_statements():
        conn.execute(stmt)


def m002_task_idempotency(conn: sqlite3.Connection) -> None:
    """PRD §5.2：task.create 幂等键（可空，UNIQUE 允许多个 NULL）。"""
    conn.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX idx_tasks_idem ON tasks(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


MIGRATIONS = [m001_init, m002_task_idempotency]


# 属性白名单：正常接口只允许这些名字，其余（含 _conn/_cur 内部槽、
# executescript/commit/rollback/close、游标 .connection）在访问时即拒绝。
# 注意（复审共识）：这不是抵御恶意代码的安全沙箱——object.__getattribute__
# 等刻意内省仍可绕过；目标是让迁移函数的正常接口无法意外取得裸连接。
_MIGRATION_ALLOWED = frozenset({"execute"})
_CURSOR_ALLOWED = frozenset({"fetchone", "fetchall", "fetchmany", "rowcount", "lastrowid"})


class MigrationCursor:
    """受控游标代理：不原样返回 sqlite3.Cursor，封死 .connection 通路。"""

    __slots__ = ("_cur",)

    def __init__(self, cur: sqlite3.Cursor):
        object.__setattr__(self, "_cur", cur)

    def __getattribute__(self, name: str):
        if name not in _CURSOR_ALLOWED:
            raise RuntimeError(
                f"migration_forbidden_cursor_api:{name}"
                "（游标仅允许 fetchone/fetchall/fetchmany/rowcount/lastrowid）"
            )
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value):
        raise RuntimeError(f"migration_forbidden_attr_write:{name}")

    def fetchone(self):
        return object.__getattribute__(self, "_cur").fetchone()

    def fetchall(self):
        return object.__getattribute__(self, "_cur").fetchall()

    def fetchmany(self, size=None):
        cur = object.__getattribute__(self, "_cur")
        return cur.fetchmany() if size is None else cur.fetchmany(size)

    @property
    def rowcount(self) -> int:
        return object.__getattribute__(self, "_cur").rowcount

    @property
    def lastrowid(self):
        return object.__getattribute__(self, "_cur").lastrowid


class MigrationConnection:
    """迁移函数的受控连接接口（M1 两轮复审修复：执行前拒绝 + 白名单封装）。

    - __getattribute__ 白名单：_conn 等内部属性与 executescript/commit 等
      危险接口在**访问时**即抛错，脚本没有机会执行，外层事务不被隐式提交，
      ROLLBACK 可完整撤销，数据库保持可安全重试状态。
    - execute() 返回 MigrationCursor 代理，杜绝经游标 .connection 取裸连接。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, "_conn", conn)

    def __getattribute__(self, name: str):
        if name not in _MIGRATION_ALLOWED:
            raise RuntimeError(
                f"migration_forbidden_api:{name}"
                "（迁移内仅允许 execute；executescript/commit/rollback 等被禁止）"
            )
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value):
        raise RuntimeError(f"migration_forbidden_attr_write:{name}")

    def execute(self, sql: str, parameters=()) -> MigrationCursor:
        cur = object.__getattribute__(self, "_conn").execute(sql, parameters)
        return MigrationCursor(cur)


def init_db(db_path: Path | str | None = None) -> int:
    """建库并按序应用未执行的迁移。零播种。返回当前 schema 版本。

    原子性保障（M1 两轮审查修复）：
    - 第一道防线：迁移函数拿到的是 MigrationConnection 受控接口（属性白名单），
      executescript 等危险调用在**执行前**被拒绝；_conn 内部属性与 execute()
      返回游标的 .connection 同样不可访问（MigrationCursor 代理），事务可完整
      回滚，失败后数据库无残留、可安全重试。
    - 显式 BEGIN/COMMIT/ROLLBACK（Python 3.12+ sqlite3 legacy 模式下 DDL
      不在隐式事务内，`with conn` 无法回滚建表）。
    - 第二道防线：迁移执行后复查 in_transaction，任何绕过受控接口破坏
      事务边界的行为仍会被拦截并阻止版本推进。
    """
    conn = get_conn(db_path)
    _ensure_version_table(conn)
    applied = current_version(conn)
    for idx in range(applied, len(MIGRATIONS)):
        fn = MIGRATIONS[idx]
        conn.execute("BEGIN")
        if not conn.in_transaction:
            raise RuntimeError("migration_transaction_not_started")
        try:
            fn(MigrationConnection(conn))
            if not conn.in_transaction:
                raise RuntimeError(
                    f"migration_broke_transaction_boundary:{fn.__name__}"
                )
            conn.execute(
                "INSERT INTO schema_version(version,name,applied_at) VALUES(?,?,?)",
                (idx + 1, fn.__name__, now_iso()),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return current_version(conn)


def backup(dest_path: Path | str, db_path: Path | str | None = None) -> Path:
    """在线备份（sqlite3 backup API，WAL 安全）。"""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = get_conn(db_path)
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
    finally:
        out.close()
    return dest
