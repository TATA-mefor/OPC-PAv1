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

# 生产库默认路径快照（模块加载时固定）：测试写工厂守卫基准。
# 测试 fixture 重定向 config.DB_PATH 不影响本常量——守卫始终拒绝真实生产库。
_PRODUCTION_DB_DEFAULT = Path(config.DB_PATH)


def now_iso() -> str:
    """统一时间格式：aware UTC ISO8601（PRD §12.1，禁止 naive 入库）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _roles_sql() -> str:
    return ",".join(f"'{r}'" for r in config.ROLE_ORDER)


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """兼容符号：**物理只读**连接（用户裁决 2026-11-21：安全边界优先）。

    历史语义为可写线程本地连接；v10.2 复审裁定该入口构成绕过 CASService
    与 Identity 的生产写旁路，与自建服务伪造授权属同一安全边界，不可作为
    兼容方案保留。现降级为 query_only 只读（与 get_read_conn 同缓存槽）：
    任何 INSERT/UPDATE/DELETE/DDL/BEGIN IMMEDIATE 在执行时被 SQLite 拒绝。
    写操作唯一路径 = 组合根注入的 WriteCapability（M3.2 起 M2 披露写经
    显式注入的 DisclosureService，db 不再提供任何内部 writer 获取函数）。
    """
    return get_read_conn(db_path)


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
# - WriteCapability 不可直构；生产 writer 只由组合根在 build_application
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

    @property
    def bound_authorize(self):
        """组合根绑定的可信 authority（未绑定为 None）。"""
        return object.__getattribute__(self, "_bound_authorize")

    def _bind_authorize(self, authority) -> None:
        """组合根取得 writer 后**立即**一次性绑定可信 authority。

        M3.2 复审：绑定前移到组合根，CASService 构造期只校验不绑定，
        消除"谁先构造谁赢"的先到先得窗口。已绑定后任何再次绑定一律拒绝。
        """
        if object.__getattribute__(self, "_bound_authorize") is not None:
            raise PermissionError("write_capability_already_paired")
        object.__setattr__(self, "_bound_authorize", authority)

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


def apply_write_pragmas(conn: sqlite3.Connection) -> None:
    """为**调用方已自行打开**的写连接施加统一 PRAGMA。

    纯配置函数：不打开连接、不铸造句柄、不发放任何能力。
    组合根自行打开生产连接后调用它，避免 PRAGMA 策略在两处重复。
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={config.DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")


def _mint_test_writer(path: Path | str, cap_id: str) -> WriteCapability:
    """**测试专用**铸造：拒绝真实生产库默认路径。

    M3.2① 复审修复：原 _mint_writer 无任何守卫、无一次性限制、cap_id 可
    任意传入，是比 _open_production_writer 更短的生产写获取入口（已实证
    可反复铸造指向生产库的 writer 并绕过 CAS 直接写入）。现统一加生产
    路径守卫，且只供 _open_test_writer 使用。
    """
    p = Path(path)
    if p.resolve() == _PRODUCTION_DB_DEFAULT.resolve():
        raise PermissionError("test_writer_cannot_target_production_db")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=config.DB_BUSY_TIMEOUT_MS / 1000,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_write_pragmas(conn)
    w = object.__new__(WriteCapability)
    object.__setattr__(w, "_conn", conn)
    object.__setattr__(w, "_db_path", str(p))
    object.__setattr__(w, "_cap_id", cap_id)
    object.__setattr__(w, "_lock", threading.Lock())
    object.__setattr__(w, "_bound_authorize", None)
    return w


# ---------------------------------------------------------------------------
# 生产写能力获取入口：**本模块不提供任何此类函数**（M3.2① 复审修复）
#
# 原 _open_production_writer() 已整体删除。它虽有一次性锁，但只拦得住第二个
# 调用者，不能证明第一个调用者就是组合根——普通业务模块可在 app 启动前抢先
# 调用它取得 production WriteCapability，再用公开的 transaction() 绕过 CAS
# 直接写生产库（已实证）。一次性标志不是访问控制。
#
# 现行边界：生产 writer 由 app.build_application() **在函数体内部**自行打开
# 连接并铸造，db 侧不存在任何返回生产写能力的模块级可调用对象。
# 威胁模型（用户既定口径）：不要求进程内绝对不可绕过的沙箱——直接
# sqlite3.connect(config.DB_PATH) 或 object.__new__(WriteCapability) 属于
# 特权逃逸，不在防护范围；要求的是项目 API 中不存在普通模块可直接调用的
# 生产写能力获取入口。
# ---------------------------------------------------------------------------


def _open_test_writer(db_path: Path | str) -> WriteCapability:
    """测试组合根专用：打开独立测试库写能力，与生产库物理隔离。

    对新库自动应用全部迁移（零播种），开箱即有完整 schema。
    可多次创建（并发用例需要）。
    **拒绝指向真实生产库默认路径**（防借测试工厂取生产写能力，用户裁决
    第4点）。判定基准是未被测试重定向的默认路径 config.DATA_DIR/personal_agent.db
    的原始值——测试经 fixture 把 config.DB_PATH 重定向到 tmp 后，
    _open_test_writer(config.DB_PATH) 合法（写的就是隔离测试库）。
    调用方负责 close()（fixture 清理）。
    """
    if Path(db_path).resolve() == _PRODUCTION_DB_DEFAULT.resolve():
        raise PermissionError("test_writer_cannot_target_production_db")
    init_db(db_path)
    return _mint_test_writer(db_path, cap_id=f"test:{db_path}")


# ---------------------------------------------------------------------------
# M2 披露写能力（M3.2 复审：全局获取入口已删除）
#
# 原 _internal_writer() / _close_internal_writer() / _internal_writer_local
# 已整体移除：它们是普通模块可直接调用的生产写能力获取入口，且其 cap_id
# 不等于 "production"，会跳过 CAS 配对校验（已实证可伪造 boss 越权写入）。
#
# 现行边界：生产写能力由组合根在 build_application() 内部铸造并立即绑定
# 可信 authority；M2 披露写经组合根显式注入
# visibility.DisclosureService(writer)，本模块不再提供任何披露写获取函数。
# ---------------------------------------------------------------------------


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
    # 专用引导连接（get_conn 已只读化，用户裁决 2026-11-21）：
    # 仅在本函数内部打开/关闭，不进入线程缓存、不对外暴露。
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=config.DB_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={config.DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
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
    finally:
        conn.close()


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
