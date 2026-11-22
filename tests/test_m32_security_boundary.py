"""M3.2 ① 写能力获取边界负向用例。

db 侧内部 writer 获取入口已删除；DisclosureService 显式注入、fail closed、
最小暴露面。所有断言为端到端行为断言（行内容 / version / 审计）。
"""
from __future__ import annotations

import json

import pytest

from opc_workbench import app, config, db, visibility


# ---------------------------------------------------------------------------
# 探针工具
# ---------------------------------------------------------------------------

def _seed_cfo_private(conn, title="cfo 私密成本底线"):
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,detail,owner_role,visibility,created_at,updated_at) "
            "VALUES(?,?,'cfo','private',?,?)",
            (title, "私密明细-XJ9-不得外泄", db.now_iso(), db.now_iso()))
    return cur.lastrowid


def _seed_sales_public(conn, title="公开任务"):
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
            "VALUES(?,'sales','public',?,?)", (title, db.now_iso(), db.now_iso()))
    return cur.lastrowid


def _task(conn, rid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (rid,)).fetchone())


def _audit_records():
    p = config.LOG_DIR / "audit.log"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _boss_ok_audits(rid):
    """本轮最重要的负向证据：不得留下 boss 成功审计。"""
    return [r for r in _audit_records()
            if r.get("actor_role") == "boss" and r.get("result_code") == "ok"
            and r.get("target_id") == rid]




# ---------------------------------------------------------------------------
# 一、CAS writer-authorize 配对矩阵（用户给定 8 行）
# ---------------------------------------------------------------------------















def test_matrix7_no_module_level_production_writer_entry(isolated_env):
    """⑦ 普通业务模块直接获取生产 writer → 无可用入口。

    M3.2① 复审修复：原断言只验"第二次调用失败"，那恰恰证明第一次能被
    普通调用方取得。现改为断言入口本身不存在，且组合根仍能正常启动。
    """
    for name in ("_internal_writer", "_close_internal_writer", "_internal_writer_local",
                 "_open_production_writer", "_mint_writer"):
        assert not hasattr(db, name), f"db.{name} 仍存在，生产写获取旁路未封闭"
    # 借测试铸造入口指向真实生产库默认路径 → 拒绝
    with pytest.raises(PermissionError):
        db._mint_test_writer(db._PRODUCTION_DB_DEFAULT, "production")
    # 普通调用未能消耗名额：组合根照常取得生产 writer
    a = app.build_application()
    try:
        assert not hasattr(a, "writer")
    finally:
        a.close()


def test_matrix7b_composition_root_owns_disclosure(isolated_env):
    """⑦b 组合根显式持有 DisclosureService；visibility 无模块级可变服务槽。"""
    a = app.build_application()
    try:
        assert isinstance(a.disclosure, visibility.DisclosureService)
        for name in ("_service", "_disclosure_service", "_default_service", "bind"):
            assert not hasattr(visibility, name), f"visibility.{name} 构成全局绑定槽"
    finally:
        a.close()


def test_matrix8_disclosure_full_chain_regression(disclosure, db_conn):
    """⑧ M2 三披露方法保持原状态机与回归行为（全链冒烟；详细回归见 test_visibility）。"""
    rid = _seed_cfo_private(db_conn)
    aid = disclosure.request_disclosure("tasks", rid, "cfo", "sales", "报价核算")
    assert isinstance(aid, int)
    disclosure.resolve_disclosure(aid, "approved", actor="boss")
    got = disclosure.consume_grant(aid, "sales")
    assert got["id"] == rid
    # 一次性：二次消费返回占位
    assert disclosure.consume_grant(aid, "sales") == visibility.PLACEHOLDER


# ---------------------------------------------------------------------------
# 二、真实攻击复现（本轮最重要的负向验收证据）
# ---------------------------------------------------------------------------





def test_attack_b1_internal_writer_entry_is_gone(isolated_env, identity_api, db_conn):
    """B1 复现：原攻击第一步 db._internal_writer() 现已不存在。"""
    rid = _seed_cfo_private(db_conn)
    before = _task(db_conn, rid)
    with pytest.raises(AttributeError):
        db._internal_writer()
    assert _task(db_conn, rid) == before
    assert _boss_ok_audits(rid) == []




# ---------------------------------------------------------------------------
# 三、Authorize 不可伪造（普通调用方路径）
# ---------------------------------------------------------------------------











# ---------------------------------------------------------------------------
# 四、DisclosureService fail closed 与最小暴露面
# ---------------------------------------------------------------------------

def test_disclosure_module_functions_fail_closed(db_conn):
    """未注入 service → 拒绝，绝不回退到任何全局写能力。"""
    rid = _seed_cfo_private(db_conn)
    for bad in (None, object(), lambda: None):
        with pytest.raises(PermissionError, match="disclosure_service_required"):
            visibility.request_disclosure(bad, "tasks", rid, "cfo", "sales", "x")
        with pytest.raises(PermissionError, match="disclosure_service_required"):
            visibility.resolve_disclosure(bad, 1, "approved", actor="boss")
        with pytest.raises(PermissionError, match="disclosure_service_required"):
            visibility.consume_grant(bad, 1, "sales")


def test_disclosure_module_functions_accept_explicit_service(disclosure, db_conn):
    """显式传入合法 service → 薄委托正常工作（无全局槽）。"""
    rid = _seed_cfo_private(db_conn)
    aid = visibility.request_disclosure(disclosure, "tasks", rid, "cfo", "sales", "交接")
    visibility.resolve_disclosure(disclosure, aid, "approved", actor="boss")
    assert visibility.consume_grant(disclosure, aid, "sales")["id"] == rid


def test_disclosure_service_requires_write_capability():
    for bad in (None, object(), "writer"):
        with pytest.raises(TypeError, match="disclosure_writer_required"):
            visibility.DisclosureService(bad)


def test_disclosure_service_minimal_surface(disclosure):
    """只暴露三个业务方法：不暴露 writer / cursor / 通用事务接口。"""
    with pytest.raises(PermissionError, match="disclosure_service_writer_not_exposed"):
        disclosure._writer
    for name in ("transaction", "cursor", "execute", "conn", "writer"):
        assert not hasattr(disclosure, name), f"DisclosureService 暴露了 {name}"
    with pytest.raises(PermissionError, match="disclosure_service_immutable"):
        disclosure._writer = None


# ---------------------------------------------------------------------------
# 生产写能力抢先获取：两条已实证路径的负向复现（M3.2① 复审阻断项）
#
# 威胁模型（用户既定口径）：不要求进程内绝对不可绕过的沙箱。
# object.__getattribute__(svc, "_writer") / object.__new__(WriteCapability) /
# 直接 sqlite3.connect(config.DB_PATH) 属特权逃逸，不在防护范围；
# 要求的是项目 API 中不存在普通模块可直接调用的生产写能力获取入口。
# ---------------------------------------------------------------------------

def test_attack_preempt_one_shot_factory_entry_gone(isolated_env, db_conn):
    """路径A 复现：普通模块抢先调用一次性工厂取生产 writer。

    修复前实测：首个调用者即取得 cap_id="production" 的 writer，
    随后 writer.transaction() 绕过 CAS 直接写入成功；一次性锁只拦第二个
    调用者，不能证明第一个是组合根。修复后该函数不存在。
    """
    with pytest.raises(AttributeError):
        db._open_production_writer()
    assert db_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    # 普通调用未消耗名额，组合根照常启动
    a = app.build_application()
    try:
        assert not hasattr(a, "writer")
    finally:
        a.close()


def test_attack_unguarded_mint_entry_gone(isolated_env, db_conn):
    """路径B 复现：_mint_writer 无守卫、无一次性限制、cap_id 可任意伪造。

    修复前实测：可反复铸造指向生产库的 writer（比路径A 更短），
    直接 transaction() 写入成功。修复后该函数不存在，替代的测试铸造
    入口对真实生产库默认路径一律拒绝。
    """
    with pytest.raises(AttributeError):
        db._mint_writer(config.DB_PATH, "test:evil")
    for cap in ("production", "anything", "test:evil"):
        with pytest.raises(PermissionError, match="test_writer_cannot_target_production_db"):
            db._mint_test_writer(db._PRODUCTION_DB_DEFAULT, cap)
    with pytest.raises(PermissionError):
        db._open_test_writer(db._PRODUCTION_DB_DEFAULT)
    assert db_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_composition_root_mint_not_exposed_as_callable(isolated_env):
    """铸造代码只在 build_application 函数体内部，app 模块无生产写工厂。"""
    for name in ("_open_production_writer", "_mint_production_writer",
                 "_mint_production_writer_inline", "open_writer", "get_writer"):
        assert not hasattr(app, name), f"app.{name} 构成可调用的生产写获取入口"
    a = app.build_application()
    try:
        assert not hasattr(a, "writer")
    finally:
        a.close()

# ---------------------------------------------------------------------------
# M3.2① 第三轮复审：普通 API 可达面全封闭 + 抢先调用不抢占启动名额
# 威胁模型：object.__getattribute__ / object.__new__ / 裸 sqlite3.connect 属
# 特权逃逸，不在防护范围；下列全部走**普通属性访问与公开方法**。
# ---------------------------------------------------------------------------


def test_public_surface_sweep_yields_no_write_capability(isolated_env):
    """遍历组合根产物的全部公开属性，任一都不得是原始 WriteCapability。"""
    from opc_workbench.tools.task import TaskTool
    from opc_workbench.tools.schedule import ScheduleTool
    from opc_workbench.tools.report import ReportTool

    a = app.build_application()
    try:
        assert "writer" not in app.Application.__slots__
        for obj in (a, a.cas_service, a.disclosure, a.identity_api,
                    a.dispatcher,
                    a.dispatcher.agent_channel("test-agent-sales")):
            for name in dir(obj):
                if name.startswith("__"):
                    continue
                try:
                    val = getattr(obj, name)
                except PermissionError:
                    continue          # 显式拒绝即达标
                assert not isinstance(val, db.WriteCapability), (
                    f"{type(obj).__name__}.{name} 泄露原始写能力")
        # 原始工具注册表不在公开面；调度器内部同样取得不到
        assert not hasattr(a, "tools")
        with pytest.raises(PermissionError, match="dispatcher_internal_not_exposed"):
            a.dispatcher._registry
        # 工具类不得再提供公开的 writer 取用方法
        for cls in (TaskTool, ScheduleTool, ReportTool):
            assert not hasattr(cls, "_writer"), f"{cls.__name__}._writer 仍是公开入口"
            assert not hasattr(cls, "writer")
    finally:
        a.close()


def test_attack_ordinary_call_does_not_preempt_startup(isolated_env, db_conn):
    """攻击：普通业务模块抢先调用公开工厂。

    断言一：拿不到原始生产 writer（任何公开属性）。
    断言二：不影响真正启动——随后 app.main() 的启动器仍装配出可用应用。
    断言三：修复后真实调用链（工具注册 / CAS / 披露）端到端仍正常。
    """
    # ① 普通模块抢先调用（修复前此处即可取得生产 writer）
    grabbed = app.build_application()
    assert not hasattr(grabbed, "writer")
    with pytest.raises(PermissionError, match="cas_service_writer_not_exposed"):
        grabbed.cas_service._writer
    with pytest.raises(PermissionError, match="disclosure_service_writer_not_exposed"):
        grabbed.disclosure._writer

    # ② 真正启动不受影响：启动器由入口持有，装配出独立可用应用
    launcher = app.main()
    started = launcher.build()
    assert started is not grabbed
    assert not hasattr(started, "writer")

    # ③ 端到端回归：调度入口 → CAS 版本推进 → 披露审批链
    channel = started.dispatcher.agent_channel("test-agent-sales")

    def call(action, **params):
        return channel.submit(action, params)

    r = call("task.create", title="启动后链路")
    assert r["version"] == 1
    r2 = call("task.update_status", id=r["id"], status="doing", version=1)
    assert r2["version"] == 2
    assert db_conn.execute("SELECT status FROM tasks WHERE id=?",
                           (r["id"],)).fetchone()["status"] == "doing"

    with db_conn:
        cur = db_conn.execute(
            "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
            "VALUES('cfo私有','cfo','private',0,0)")
        pid = cur.lastrowid
    ap = visibility.request_disclosure(started.disclosure, "tasks", pid,
                                       from_role="cfo", to_role="sales", reason="链路核验")
    visibility.resolve_disclosure(started.disclosure, ap, "approved", actor="boss")
    grant = visibility.consume_grant(started.disclosure, ap, to_role="sales")
    assert grant["id"] == pid and grant["owner_role"] == "cfo"
    launcher.close()
    grabbed.close()


# ---------------------------------------------------------------------------
# M3.2① 第四轮：撤销模块级应用槽，生命周期由启动器实例持有
# ---------------------------------------------------------------------------


def test_no_module_level_mutable_application_slot(isolated_env):
    """app 模块不得存在任何模块级应用/启动器实例槽（含 _application）。"""
    assert not hasattr(app, "_application")
    assert not hasattr(app, "_production_writer_taken")
    for name in dir(app):
        if name.startswith("__"):
            continue
        val = getattr(app, name)
        assert not isinstance(val, (app.Application, app.ApplicationLauncher)), (
            f"app.{name} 是模块级应用服务槽")
        assert not isinstance(val, db.WriteCapability), f"app.{name} 泄露写能力"
    # 工厂只构造不缓存：两次调用得到彼此独立的有界应用
    a = app.build_application()
    b = app.build_application()
    try:
        assert a is not b
        assert a.cas_service is not b.cas_service
    finally:
        a.close()
        b.close()


def test_launcher_close_then_rebuild_is_usable(isolated_env, db_conn):
    """关停后重新 build() → 全新实例且真实可写（不是已关闭的旧实例）。"""
    launcher = app.ApplicationLauncher()
    first = launcher.build()
    assert launcher.build() is first
    launcher.close()

    second = launcher.build()
    assert second is not first
    channel = second.dispatcher.agent_channel("test-agent-sales")
    r = channel.submit("task.create", {"title": "重启后写入"})
    assert r["version"] == 1
    assert db_conn.execute("SELECT title FROM tasks WHERE id=?",
                           (r["id"],)).fetchone()["title"] == "重启后写入"
    launcher.close()


def test_launcher_close_idempotent_and_application_close_idempotent(isolated_env):
    """重复关停不报错；关停后再关也不抛（生命周期无悬挂状态）。"""
    launcher = app.ApplicationLauncher()
    a = launcher.build()
    launcher.close()
    launcher.close()                 # 第二次是空操作
    a.close()
    a.close()                        # Application.close 幂等


def test_build_failure_closes_production_connection(isolated_env, monkeypatch):
    """装配中途失败（writer 已创建）→ 已打开的生产连接必须被关闭。"""
    import sqlite3
    from opc_workbench import tools as tools_mod

    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*a, **kw):
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    def boom(**kw):
        raise RuntimeError("assembly_failed_after_writer")

    monkeypatch.setattr(app.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(tools_mod, "build_tools", boom)
    with pytest.raises(RuntimeError, match="assembly_failed_after_writer"):
        app.build_application()
    assert opened, "未捕获到生产连接"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].execute("SELECT 1")     # 已关闭 → 不可操作


def test_launcher_build_failure_caches_nothing_and_retry_works(isolated_env, monkeypatch):
    """启动器构造失败 → 不缓存半成品；排除故障后重试得到可用应用。"""
    from opc_workbench import tools as tools_mod

    def boom(**kw):
        raise RuntimeError("assembly_failed")

    launcher = app.ApplicationLauncher()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tools_mod, "build_tools", boom)
        with pytest.raises(RuntimeError, match="assembly_failed"):
            launcher.build()
    a = launcher.build()
    try:
        assert not hasattr(a, "writer")
        r = a.dispatcher.agent_channel("test-agent-sales").submit(
            "task.create", {"title": "重试装配"})
        assert r["version"] == 1
    finally:
        launcher.close()
