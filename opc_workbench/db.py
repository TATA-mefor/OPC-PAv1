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


def get_read_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """线程本地**物理只读**连接（M3 读路径唯一入口，v10.2 写能力隔离）。

    PRAGMA query_only=ON：任何 INSERT/UPDATE/DELETE/DDL 在执行时被 SQLite
    拒绝（OperationalError），读不构成越权写入。与 get_conn 分缓存槽。
    """
    path = Path(db_path) if db_path is not None else config.DB_PATH
    conns: dict = getattr(_LOCAL, "read_conns", None)
    if conns is None:
        conns = _LOCAL.read_conns = {}
    key = str(path)
    conn = conns.get(key)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=config.DB_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={config.DB_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conns[key] = conn
    return conn


def close_thread_conns() -> None:
    """关闭当前线程全部连接（测试清理用）。"""
    for slot in ("conns", "read_conns"):
        conns: dict = getattr(_LOCAL, slot, {})
        for conn in conns.values():
            conn.close()
        conns.clear()


# ---------------------------------------------------------------------------
# 写能力隔离（M3.1 清单 v10.2 §三.3）
#
# 安全依据 = 对象所有权与依赖注入，不是命名/一次性标志：
# - WriteCapability 不可直构；生产 writer 只由组合根经 _open_production_writer
#   取得并封入应用 CASService（构造期固定，无 setter/rebind）；
# - 一次性取得**不是安全依据**（同 bind 槽教训）——抢先到者得到的 writer
#   进不了应用服务，且应用组合根因取得失败而启动失败（fail fast）；
# - 自建服务只能配 _open_test_writer 写独立测试库。
#
# 兼容口径（v10.3 执行说明，冲突被迫解）：get_conn 保留为 M2 遗留治理连接
# （「M2 披露函数零改动」+ 既有测试 monkeypatch 依赖，物理移除会违反已定案
# 约束）；M3 读路径走 get_read_conn（物理只读）；M3 全部写操作只经注入的
# WriteCapability，零 get_conn 调用（架构回归测试看守）。
# ---------------------------------------------------------------------------

class WriteCapability:
    """数据库写能力句柄。不可直构；实例只由本模块内部工厂产出。

    持有专用写连接（check_same_thread=False + 实例锁串行），事务原语
    BEGIN IMMEDIATE / COMMIT / ROLLBACK 显式控制（与迁移框架同款纪律）。
    """

    __slots__ = ("_conn", "_db_path", "_cap_id", "_lock", "_bound_authorize")

    def __init__(self, *a, **kw):
        raise PermissionError("write_capability_direct_construction_forbidden")

    def __setattr__(self, name, value):
        raise PermissionError("write_capability_immutable")

    @property
    def db_path(self) -> str:
        return object.__getattribute__(self, "_db_path")

    @property
    def cap_id(self) -> str:
        return object.__getattribute__(self, "_cap_id")

    @property
    def in_transaction(self) -> bool:
        return object.__getattribute__(self, "_conn").in_transaction

    def _bind_authorize(self, authorize) -> None:
        """装配期一次性绑定 authorize（仅 CASService 构造内部调用）。

        配对校验依据：生产 writer 只能与同一组合根装配的 authorize 配对；
        已绑定后任何再次绑定（含同一函数）一律拒绝——伪造 authorize
        拿生产 writer 构造服务的路径在构造期即断。
        """
        if object.__getattribute__(self, "_bound_authorize") is not None:
            raise PermissionError("write_capability_already_paired")
        object.__setattr__(self, "_bound_authorize", authorize)

    def transaction(self):
        """with writer.transaction() as cur: ... —— 单事务写原语。

        进入即 BEGIN IMMEDIATE；正常退出 COMMIT；异常 ROLLBACK 后原样抛出。
        锁保证同实例跨线程串行（SQLite 写本就串行，锁防交叉 BEGIN）。
        """
        return _WriteTransaction(self)

    def close(self) -> None:
        conn = object.__getattribute__(self, "_conn")
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


class _WriteTransaction:
    """WriteCapability.transaction() 的上下文管理器（内部类）。"""

    __slots__ = ("_writer", "_cur")

    def __init__(self, writer: WriteCapability):
        object.__setattr__(self, "_writer", writer)
        object.__setattr__(self, "_cur", None)

    def __enter__(self):
        w = object.__getattribute__(self, "_writer")
        lock = object.__getattribute__(w, "_lock")
        lock.acquire()
        conn = object.__getattribute__(w, "_conn")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            lock.release()
            raise
        cur = conn.cursor()
        object.__setattr__(self, "_cur", cur)
        return cur

    def __exit__(self, exc_type, exc, tb):
        w = object.__getattribute__(self, "_writer")
        conn = object.__getattribute__(w, "_conn")
        lock = object.__getattribute__(w, "_lock")
        try:
            if exc_type is None:
                conn.execute("COMMIT")
            else:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        finally:
            cur = object.__getattribute__(self, "_cur")
            if cur is not None:
                cur.close()
            lock.release()
        return False


def _mint_writer(path: Path | str, cap_id: str) -> WriteCapability:
    """内部铸造：打开专用写连接并封入不可变句柄（失败原样抛错，fail fast）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=config.DB_BUSY_TIMEOUT_MS / 1000,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={config.DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    w = object.__new__(WriteCapability)
    object.__setattr__(w, "_conn", conn)
    object.__setattr__(w, "_db_path", str(p))
    object.__setattr__(w, "_cap_id", cap_id)
    object.__setattr__(w, "_lock", threading.Lock())
    object.__setattr__(w, "_bound_authorize", None)
    return w


_production_writer_taken = False
_production_writer_lock = threading.Lock()


def _open_production_writer() -> WriteCapability:
    """组合根专用：进程内首个调用者取得生产写能力，后续调用抛错。

    一次性不是安全依据（见上方注释）；取得失败原样抛错 → 组合根启动失败，
    不静默降级。路径取 config.DB_PATH（测试经 fixture 重定向到 tmp）。
    """
    global _production_writer_taken
    with _production_writer_lock:
        if _production_writer_taken:
            raise RuntimeError("production_writer_already_taken")
        w = _mint_writer(config.DB_PATH, cap_id="production")
        _production_writer_taken = True
        return w


def _open_test_writer(db_path: Path | str) -> WriteCapability:
    """测试组合根专用：打开独立测试库写能力，与生产库物理隔离。

    对新库自动应用全部迁移（零播种），开箱即有完整 schema。
    可多次创建（并发用例需要）；不占用生产一次性名额。
    调用方负责 close()（fixture 清理）。
    """
    init_db(db_path)
    return _mint_writer(db_path, cap_id=f"test:{db_path}")


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


# ---------------------------------------------------------------------------
# m003：artifacts 重建表（M3.1 清单 §三.5，v6 索引步骤定稿）
#   CHECK 加 'boss' + version 列 + updated_at 列；UNIQUE 作为表约束随新表
#   DDL 自动重建；独立索引按 PRAGMA 记录原名原列恢复（SQLite 无 ALTER INDEX
#   RENAME）。旧行 version 补 1、updated_at 回填 created_at（语义显式声明）。
#   核对不通过 → 抛错 → 外层 ROLLBACK，原表原数据原索引完整保留。
# ---------------------------------------------------------------------------

_M003_CREATE_SQL = """
CREATE TABLE artifacts_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL
                CHECK(kind IN ('daily_report','weekly_report','other')),
    title       TEXT NOT NULL,
    content_md  TEXT NOT NULL,
    refs_json   TEXT NOT NULL DEFAULT '{}',
    owner_role  TEXT NOT NULL CHECK(owner_role IN ({ROLES},'boss')),
    visibility  TEXT NOT NULL DEFAULT 'private'
                CHECK(visibility IN ('public','private')),
    period_key  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    UNIQUE(kind, period_key, owner_role)
)
"""


def _m003_statements() -> list[str]:
    """m003 静态语句序列（测试可注入中途失败，与 m001 同款设计）。

    仅含可静态枚举的 DDL/DML；索引记录与逐字段核对在迁移函数内动态执行。
    顺序：建新表 → 复制旧行（version=1、updated_at=created_at）→ DROP 旧表
    → 表级 RENAME。核对在复制后、DROP 前由函数完成。
    """
    create = _M003_CREATE_SQL.replace("{ROLES}", _roles_sql()).strip()
    copy = (
        "INSERT INTO artifacts_new(id,kind,title,content_md,refs_json,owner_role,"
        "visibility,period_key,created_at,version,updated_at) "
        "SELECT id,kind,title,content_md,refs_json,owner_role,visibility,"
        "period_key,created_at,1,created_at FROM artifacts"
    )
    return [create, copy, "DROP TABLE artifacts",
            "ALTER TABLE artifacts_new RENAME TO artifacts"]


def _record_indexes(mconn, table: str) -> list[dict]:
    """记录独立 CREATE INDEX 索引（排除表约束自动索引 sqlite_autoindex_*）。"""
    out = []
    for r in mconn.execute(f"PRAGMA index_list({table})").fetchall():
        name = r["name"]
        if name.startswith("sqlite_autoindex_"):
            continue
        cols = [c["name"] for c in mconn.execute(f"PRAGMA index_info({name})").fetchall()]
        out.append({"name": name, "unique": bool(r["unique"]), "cols": cols})
    return out


def _verify_artifacts_copy(mconn) -> None:
    """逐行逐字段核对（从 PRAGMA 实际列清单出发，v4 定稿）。

    调用时点：复制完成后、DROP 前——旧表仍在，可全量比对。
    行数相等是必要非充分条件；任何不符抛错，外层整体回滚。
    """
    old_cols = [r["name"] for r in mconn.execute("PRAGMA table_info(artifacts)").fetchall()]
    old_rows = {r["id"]: r for r in mconn.execute("SELECT * FROM artifacts").fetchall()}
    new_rows = {r["id"]: r for r in mconn.execute("SELECT * FROM artifacts_new").fetchall()}
    if set(old_rows) != set(new_rows):
        raise RuntimeError("m003_row_set_mismatch")
    for rid, old in old_rows.items():
        new = new_rows[rid]
        for col in old_cols:
            if new[col] != old[col]:
                raise RuntimeError(f"m003_field_mismatch:{rid}.{col}")
        if new["version"] != 1 or new["updated_at"] != old["created_at"]:
            raise RuntimeError(f"m003_new_column_backfill_mismatch:{rid}")


def m003_report_identity_version(conn) -> None:
    """artifacts 重建：boss 归属 + version + updated_at（单事务，外层控制）。"""
    indexes = _record_indexes(conn, "artifacts")     # ① 记录旧独立索引
    stmts = _m003_statements()
    conn.execute(stmts[0])                            # ② 建新表（UNIQUE 表约束）
    conn.execute(stmts[1])                            # ③ 复制旧行
    _verify_artifacts_copy(conn)                      # ④ 逐行逐字段核对（DROP 前）
    conn.execute(stmts[2])                            # ⑤ DROP 旧表
    conn.execute(stmts[3])                            # ⑥ 表级 RENAME
    for idx in indexes:                               # ⑦ 按记录恢复独立索引
        uniq = "UNIQUE " if idx["unique"] else ""
        cols = ",".join(idx["cols"])
        conn.execute(f"CREATE {uniq}INDEX {idx['name']} ON artifacts({cols})")


def m004_bulk_delete_recovery(conn) -> None:
    """三条 ALTER ADD COLUMN（可空，无需重建表）：
    approvals.digest（披露单恒 NULL，M2 函数零改动）；
    schedules.notified_at / recovery_enqueued_at（补扫时间依据与补偿标记）。
    """
    conn.execute("ALTER TABLE approvals ADD COLUMN digest TEXT")
    conn.execute("ALTER TABLE schedules ADD COLUMN notified_at TEXT")
    conn.execute("ALTER TABLE schedules ADD COLUMN recovery_enqueued_at TEXT")


MIGRATIONS = [m001_init, m002_task_idempotency,
              m003_report_identity_version, m004_bulk_delete_recovery]


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
