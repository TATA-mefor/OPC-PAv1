"""CAS 专项 + 生产写能力隔离端到端实测（M3.1 清单 v4/v9/v10.1/v10.2）。

- read_where ≡ M2 visible_filter（读 44 格）；write_where ≡ PRD §3.2（写 44 格）；
- 治理字段禁改 / 版本冲突 / 权限失效窗口（public→private 并发）/ not_found 同构；
- 身份闭环：裸 caller 拒绝、伪造 Identity 拒绝、会话撤销后写拒绝；
- v10.2 唯一阻断：自建 CASService + 伪造 authorize 实际尝试改生产库 →
  内容与 version 均未变（端到端负向实测，非对象同一性断言）。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from opc_workbench import app, cas, config, db, identity, visibility


# ---------------------------------------------------------------------------
# 夹具：测试组合根（与 app.main 同构装配）
# ---------------------------------------------------------------------------

@pytest.fixture()
def writer(isolated_env):
    w = db._open_test_writer(config.DB_PATH)
    yield w
    w.close()


@pytest.fixture()
def cas_service(identity_api, writer):
    return cas._build_cas_service(authorize=identity_api.authorize, writer=writer)


def _seed_row(conn, table, owner, vis, title="目标"):
    if table == "tasks":
        sql = ("INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
               "VALUES(?,?,?,?,?)")
        args = (title, owner, vis, db.now_iso(), db.now_iso())
    elif table == "schedules":
        sql = ("INSERT INTO schedules(title,remind_at,owner_role,visibility,"
               "created_at,updated_at) VALUES(?,?,?,?,?,?)")
        args = (title, "2026-11-22T09:00:00+08:00", owner, vis,
                db.now_iso(), db.now_iso())
    else:
        sql = ("INSERT INTO artifacts(kind,title,content_md,owner_role,visibility,"
               "period_key,created_at,updated_at) VALUES('other',?,?,?,?,?,?,?)")
        args = (title, "# c", owner, vis, f"k-{owner}-{vis}-{title}",
                db.now_iso(), db.now_iso())
    with conn:
        cur = conn.execute(sql, args)
    return cur.lastrowid


def _row(conn, table, rid):
    return conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()


# ---------------------------------------------------------------------------
# 读等价矩阵：read_where SQL ≡ visible_filter Python（44 格）
# ---------------------------------------------------------------------------

_MATRIX_TABLES = ("tasks", "schedules", "artifacts", "memory")
_CALLERS = tuple(config.ROLE_ORDER) + ("boss",)


@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize("table", _MATRIX_TABLES)
def test_read_where_equals_visible_filter(caller, table, db_conn):
    """每格：SQL 过滤结果集 == Python visible_filter 结果集（逐 id 比对）。

    数据布置：每个角色各一条 public + 一条 private（含 caller 自有）。
    memory 表无 visibility 过滤列差异，同样适用。
    """
    conn = db_conn
    ids = []
    for owner in config.ROLE_ORDER:
        for vis in ("public", "private"):
            if table == "memory":
                with conn:
                    cur = conn.execute(
                        "INSERT INTO memory(summary,owner_role,visibility,created_at) "
                        "VALUES(?,?,?,?)", (f"m-{owner}-{vis}", owner, vis, db.now_iso()))
                ids.append(cur.lastrowid)
            else:
                ids.append(_seed_row(conn, table, owner, vis, f"r-{owner}-{vis}"))
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    py_expected = {r["id"] for r in rows if visibility.visible_filter(caller, r)}
    frag, params = cas.read_where(caller)
    sql_rows = conn.execute(f"SELECT id FROM {table} WHERE {frag}", params).fetchall()
    sql_got = {r["id"] for r in sql_rows}
    assert sql_got == py_expected


# ---------------------------------------------------------------------------
# 写权限矩阵：write_where ≡ PRD §3.2（44 格）
# ---------------------------------------------------------------------------

def _expected_write(caller: str, owner: str) -> bool:
    """PRD §3.2 期望值（手工推导，不复用被测代码）：
    boss 全读写；coordinator 全只读（含自有）；员工仅自有。"""
    if caller == "boss":
        return True
    if caller == "coordinator":
        return False
    return caller == owner


@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize("table", ("tasks", "schedules", "artifacts", "memory"))
def test_write_where_matches_prd_matrix(caller, table, db_conn):
    conn = db_conn
    for owner in config.ROLE_ORDER:
        for vis in ("public", "private"):
            if table == "memory":
                with conn:
                    cur = conn.execute(
                        "INSERT INTO memory(summary,owner_role,visibility,created_at) "
                        "VALUES(?,?,?,?)", ("m", owner, vis, db.now_iso()))
                rid = cur.lastrowid
            else:
                rid = _seed_row(conn, table, owner, vis)
            frag, params = cas.write_where(caller)
            got = conn.execute(
                f"SELECT id FROM {table} WHERE id=? AND {frag}",
                [rid] + params).fetchone()
            assert (got is not None) == _expected_write(caller, owner), (
                caller, table, owner, vis)


def test_write_where_key_cells(cas_service, identity_api, db_conn, boss_ctx):
    """重点格端到端：sales 改 cfo public 拒；coordinator 改自有拒；boss 全允许。"""
    rid_cfo = _seed_row(db_conn, "tasks", "cfo", "public")
    rid_coord = _seed_row(db_conn, "tasks", "coordinator", "public")
    rid_coord_pri = _seed_row(db_conn, "tasks", "coordinator", "private")
    sales = identity_api.resolve_identity("sales", {})
    coord = identity_api.resolve_identity("coordinator", {})
    boss = identity_api.resolve_identity("boss", {}, boss_ctx)
    with pytest.raises(cas.CASNotFound):
        cas_service.cas_update("tasks", rid_cfo, 1, ident=sales, title="越权")
    with pytest.raises(cas.CASNotFound):
        cas_service.cas_update("tasks", rid_coord, 1, ident=coord, title="越权")
    with pytest.raises(cas.CASNotFound):
        cas_service.cas_update("tasks", rid_coord_pri, 1, ident=coord, title="越权")
    row = cas_service.cas_update("tasks", rid_cfo, 1, ident=boss, title="老板改")
    assert row["title"] == "老板改" and row["version"] == 2


# ---------------------------------------------------------------------------
# CAS 写入行为：版本 / 治理字段 / 白名单 / 权限失效窗口
# ---------------------------------------------------------------------------

def test_cas_update_increments_version_and_updated_at(cas_service, identity_api, db_conn):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    old = _row(db_conn, "tasks", rid)
    ident = identity_api.resolve_identity("sales", {})
    row = cas_service.cas_update("tasks", rid, 1, ident=ident, title="新标题")
    assert row["version"] == 2 and row["title"] == "新标题"
    assert row["updated_at"] >= old["updated_at"]
    assert _row(db_conn, "tasks", rid)["version"] == 2


def test_cas_version_conflict(cas_service, identity_api, db_conn):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    ident = identity_api.resolve_identity("sales", {})
    cas_service.cas_update("tasks", rid, 1, ident=ident, title="第一次")
    with pytest.raises(cas.CASVersionConflict):
        cas_service.cas_update("tasks", rid, 1, ident=ident, title="过期版本")
    assert _row(db_conn, "tasks", rid)["title"] == "第一次"


def test_cas_governance_fields_forbidden(cas_service, identity_api, db_conn, boss_ctx):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    boss = identity_api.resolve_identity("boss", {}, boss_ctx)
    for field in ("id", "version", "owner_role", "scope", "created_at"):
        with pytest.raises(ValueError):
            cas_service.cas_update("tasks", rid, 1, ident=boss, **{field: "x"})
    assert _row(db_conn, "tasks", rid)["version"] == 1


def test_cas_table_column_whitelist(cas_service, identity_api, db_conn):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    ident = identity_api.resolve_identity("sales", {})
    with pytest.raises(ValueError):
        cas_service.cas_update("approvals", rid, 1, ident=ident, status="approved")
    with pytest.raises(ValueError):
        cas_service.cas_update("tasks", rid, 1, ident=ident,
                               **{"title='x'--": "注入"})
    with pytest.raises(ValueError):
        cas_service.cas_update("tasks", rid, 1, ident=ident, idempotency_key="k")


def test_cas_not_found_isomorphic(cas_service, identity_api, db_conn):
    """不存在 / 他角色私有 / 他角色 public（员工写）→ 同一 CASNotFound。"""
    ident = identity_api.resolve_identity("sales", {})
    rid_other_pub = _seed_row(db_conn, "tasks", "cfo", "public")
    rid_other_pri = _seed_row(db_conn, "tasks", "cfo", "private")
    for rid in (99999, rid_other_pub, rid_other_pri):
        with pytest.raises(cas.CASNotFound):
            cas_service.cas_update("tasks", rid, 1, ident=ident, title="x")
        with pytest.raises(cas.CASNotFound):
            cas_service.cas_delete("tasks", rid, 1, ident=ident)


def test_cas_permission_window_public_to_private(cas_service, identity_api, db_conn):
    """权限失效窗口：查询时 public，写事务前被 owner 改 private →
    最终 UPDATE WHERE 重新求值 → not_found 同构（封堵并发窗口）。"""
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    attacker = identity_api.resolve_identity("marketing", {})
    # marketing 读得到（public），随后 owner 改 private
    assert visibility.visible_filter("marketing", _row(db_conn, "tasks", rid))
    with db_conn:
        db_conn.execute("UPDATE tasks SET visibility='private' WHERE id=?", (rid,))
    with pytest.raises(cas.CASNotFound):
        cas_service.cas_update("tasks", rid, 1, ident=attacker, title="窗口越权")
    assert _row(db_conn, "tasks", rid)["title"] == "目标"


def test_cas_delete_write_where_and_version(cas_service, identity_api, db_conn):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    other = identity_api.resolve_identity("marketing", {})
    with pytest.raises(cas.CASNotFound):
        cas_service.cas_delete("tasks", rid, 1, ident=other)
    ident = identity_api.resolve_identity("sales", {})
    with pytest.raises(cas.CASVersionConflict):
        cas_service.cas_delete("tasks", rid, 99, ident=ident)
    result = cas_service.cas_delete("tasks", rid, 1, ident=ident)
    assert result["deleted"] is True
    assert _row(db_conn, "tasks", rid) is None


def test_cas_concurrent_update_exactly_one(cas_service, identity_api, db_conn):
    """同版本并发双更新 → 恰好一个成功，另一个 version_conflict。"""
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    ident = identity_api.resolve_identity("sales", {})
    results, barrier = [], threading.Barrier(2)

    def racer(n):
        barrier.wait()
        try:
            cas_service.cas_update("tasks", rid, 1, ident=ident, title=f"t{n}")
            results.append("ok")
        except cas.CASVersionConflict:
            results.append("conflict")

    ts = [threading.Thread(target=racer, args=(n,)) for n in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)
    assert sorted(results) == ["conflict", "ok"]
    assert _row(db_conn, "tasks", rid)["version"] == 2


def test_cas_artifacts_updated_at_refresh(cas_service, identity_api, db_conn, boss_ctx):
    """m003 CAS 兼容断言：artifacts 可 cas_update 且 updated_at 刷新。"""
    rid = _seed_row(db_conn, "artifacts", "boss", "private")
    boss = identity_api.resolve_identity("boss", {}, boss_ctx)
    row = cas_service.cas_update("artifacts", rid, 1, ident=boss, title="改报告")
    assert row["version"] == 2 and row["updated_at"] is not None


# ---------------------------------------------------------------------------
# 身份闭环：裸 caller / 伪造 Identity / 会话撤销
# ---------------------------------------------------------------------------

def test_cas_rejects_bare_caller_string(cas_service, db_conn):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    with pytest.raises(TypeError):
        cas_service.cas_update("tasks", rid, 1, caller="boss", title="x")
    with pytest.raises(TypeError):
        cas_service.cas_delete("tasks", rid, 1, caller="boss")


def test_cas_rejects_forged_identity(cas_service, db_conn):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    fake = object.__new__(identity.Identity)
    object.__setattr__(fake, "_ident_id", "forged")
    with pytest.raises(PermissionError):
        cas_service.cas_update("tasks", rid, 1, ident=fake, title="x")
    assert _row(db_conn, "tasks", rid)["version"] == 1


def test_cas_rejects_cross_authority_identity(cas_service, db_conn, auth_backend):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    other_api = identity._build_identity(auth_backend)
    ident = other_api.resolve_identity("sales", {})
    with pytest.raises(PermissionError):
        cas_service.cas_update("tasks", rid, 1, ident=ident, title="x")


def test_cas_boss_session_revoked_write_rejected(cas_service, identity_api, db_conn, boss_ctx):
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    boss = identity_api.resolve_identity("boss", {}, boss_ctx)
    identity_api.revoke_session("sess-boss-1")
    with pytest.raises(PermissionError):
        cas_service.cas_update("tasks", rid, 1, ident=boss, title="撤销后写")
    assert _row(db_conn, "tasks", rid)["version"] == 1


def test_cas_no_bind_no_module_service():
    """v10.1 架构断言：cas 无 bind、无模块级可变服务变量。"""
    assert not hasattr(cas, "bind")
    assert not hasattr(cas, "_service")
    assert not hasattr(cas, "cas_update")     # 无模块级写函数
    assert not hasattr(cas, "cas_delete")


def test_cas_service_requires_writer(identity_api):
    """⑧ 构造缺 writer → TypeError（写能力不可省略）。"""
    with pytest.raises(TypeError):
        cas.CASService(authorize=identity_api.authorize)


def test_cas_service_immutable(cas_service, identity_api, writer):
    with pytest.raises((AttributeError, PermissionError)):
        cas_service._authorize = lambda ident: "boss"
    with pytest.raises((AttributeError, PermissionError)):
        cas_service._writer = writer


# ---------------------------------------------------------------------------
# v10.2 唯一阻断：生产写能力隔离端到端负向实测（九项之①②③④⑤⑥）
# ---------------------------------------------------------------------------

def test_forged_service_cannot_write_production_db(isolated_env, identity_api, db_conn):
    """① 端到端负向实测：普通模块自建 CASService + 伪造 authorize（恒 boss）
    + 自建身份链签发 Identity → 实际尝试修改生产库目标行 →
    断言该行内容与 version 均未改变。

    攻击路径复盘：自建服务拿不到生产 writer（一次性已被组合根取得）；
    即便配测试 writer 也只能写独立库；伪造 authorize 配生产 writer 的
    路径被构造期配对校验拒绝（⑥）。
    """
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    before = dict(_row(db_conn, "tasks", rid))

    # 组合根装配（生产 writer 只在其函数体内部铸造）
    application = app.build_application()
    prod_writer = application.writer
    prod_service = application.cas_service

    # 攻击者：自建整套链
    evil_api = identity._build_identity(identity_api._authority._auth_backend)
    evil_ident = evil_api.resolve_identity("sales", {})
    evil_service = cas.CASService(authorize=lambda ident: "boss",
                                  writer=db._open_test_writer(
                                      isolated_env["data_dir"] / "evil.db"))
    # 实际尝试写"生产库目标行"——evil_service 的 writer 指向独立库，
    # 其中根本没有该行 → not_found；生产库行内容与 version 不变
    with pytest.raises(cas.CASNotFound):
        evil_service.cas_update("tasks", rid, 1, ident=evil_ident, title="篡改")
    after = dict(_row(db_conn, "tasks", rid))
    assert after["title"] == before["title"]
    assert after["version"] == before["version"]

    # ② 攻击者尝试再装配一次以取得生产 writer → 拒绝（名额已占用）
    with pytest.raises(RuntimeError, match="production_writer_already_taken"):
        app.build_application()

    # ③ 正向对照：组合根服务正常写成功
    legit = application.identity_api.resolve_identity("sales", {})
    row = prod_service.cas_update("tasks", rid, 1, ident=legit, title="合法更新")
    assert row["version"] == 2
    prod_writer.close()


def test_writer_authorize_cross_pairing_rejected(isolated_env, auth_backend):
    """⑥ 跨配对拒绝：生产 writer 只接受带同源归属标记的 authorize；
    伪造 lambda（无标记）→ 构造拒绝；已绑定后平行链 authorize → 拒绝。"""
    application = app.build_application()
    prod_writer = application.writer
    with pytest.raises(PermissionError):
        cas.CASService(authorize=lambda ident: "boss", writer=prod_writer)
    # 本阶段 CAS 仍在首次构造时绑定：组合根已绑定后，任何二次配对一律拒绝
    # （②把绑定前移到组合根后，此处改为"同源可配对、平行链拒绝"）
    with pytest.raises(PermissionError):
        cas.CASService(authorize=application.identity_api.make_authorize(),
                       writer=prod_writer)
    other_api = identity._build_identity(auth_backend)
    with pytest.raises(PermissionError):
        cas.CASService(authorize=other_api.make_authorize(), writer=prod_writer)
    prod_writer.close()


def test_ordinary_module_cannot_preempt_production_writer(isolated_env, db_conn):
    """M3.2① 阻断项修复：普通模块**无法**抢先取得生产写能力。

    修复前：db._open_production_writer() 首个调用者即取得，一次性锁只拦
    第二个调用者，攻击者可在组合根启动前抢先拿到 writer 并用 transaction()
    绕过 CAS 直接写库（已实证）。修复后该入口不存在，组合根照常启动。
    """
    assert not hasattr(db, "_open_production_writer")
    assert not hasattr(db, "_mint_writer")
    # 借测试工厂指向真实生产库默认路径 → 拒绝
    with pytest.raises(PermissionError):
        db._open_test_writer(db._PRODUCTION_DB_DEFAULT)
    with pytest.raises(PermissionError):
        db._mint_test_writer(db._PRODUCTION_DB_DEFAULT, "production")
    # 抢先失败后组合根仍能正常启动（名额未被普通调用消耗）
    application = app.build_application()
    assert application.writer.cap_id == "production"
    application.writer.close()


def test_m3_write_path_no_legacy_conn(isolated_env, identity_api, db_conn, monkeypatch):
    """⑤ M3 写路径零 get_conn 调用：把遗留连接工厂换成哨兵，
    CAS 写入仍成功（证明写 SQL 只经注入 writer）。"""
    rid = _seed_row(db_conn, "tasks", "sales", "public")
    w = db._open_test_writer(config.DB_PATH)
    service = cas._build_cas_service(authorize=identity_api.authorize, writer=w)
    ident = identity_api.resolve_identity("sales", {})

    def sentinel(*a, **kw):
        raise AssertionError("M3 写路径不得调用遗留 get_conn")

    monkeypatch.setattr(db, "get_conn", sentinel)
    row = service.cas_update("tasks", rid, 1, ident=ident, title="不经遗留连接")
    assert row["version"] == 2
    w.close()