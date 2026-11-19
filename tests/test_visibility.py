"""V 组可见性红线测试（PRD §13.2 + M2.1 清单 v3 §五/§六）。

全部桩级验证治理原语；M5 接真实链路时升级集成回归。
种子数据仅测试用（经 db 直插），不违反生产零播种。
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from opc_workbench import audit_log, config, db, visibility as vis

PRIVATE_SAMPLE = "私密样本串-XJ9-成本底线"
SESSION_SAMPLE = "会话样本串-HK2-草稿"
KEY_SAMPLE = "sk-FAKEKEY-000000"


def _seed_task(conn, owner, visibility, title="t"):
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (title, owner, visibility, db.now_iso(), db.now_iso()),
        )
    return cur.lastrowid


def _seed_memory(conn, owner, visibility, summary):
    with conn:
        cur = conn.execute(
            "INSERT INTO memory(summary,owner_role,visibility,created_at) VALUES(?,?,?,?)",
            (summary, owner, visibility, db.now_iso()),
        )
    return cur.lastrowid


@pytest.fixture()
def seeded(db_conn):
    """多角色种子：cfo private+public、personal private、sales public。"""
    ids = {
        "cfo_private": _seed_task(db_conn, "cfo", "private", PRIVATE_SAMPLE),
        "cfo_public": _seed_task(db_conn, "cfo", "public", "公开成本口径"),
        "personal_private": _seed_task(db_conn, "personal", "private", "老板体检安排"),
        "sales_public": _seed_task(db_conn, "sales", "public", "客户线索"),
        "cfo_private_mem": _seed_memory(db_conn, "cfo", "private", PRIVATE_SAMPLE),
        "coordinator_private": _seed_task(db_conn, "coordinator", "private", "总管家私密"),
    }
    return ids


# ---------------------------------------------------------------------------
# V1 员工不得读他角色 private + coordinator 仅 public
# ---------------------------------------------------------------------------

def test_v1_sales_cannot_see_cfo_private(db_conn, seeded):
    rows = vis.query_visible("tasks", "sales")
    titles = [r["title"] for r in rows]
    assert PRIVATE_SAMPLE not in titles            # 结果集断言，非文案
    assert "公开成本口径" in titles


def test_v1_coordinator_cannot_see_own_private(db_conn, seeded):
    rows = vis.query_visible("tasks", "coordinator")
    assert all(r["visibility"] == "public" for r in rows)
    assert "总管家私密" not in [r["title"] for r in rows]
    mems = vis.query_visible("memory", "coordinator")
    assert all(m["visibility"] == "public" for m in mems)


def test_v1_owner_sees_own_private_and_boss_sees_all(db_conn, seeded):
    assert PRIVATE_SAMPLE in [r["title"] for r in vis.query_visible("tasks", "cfo")]
    boss_titles = [r["title"] for r in vis.query_visible("tasks", "boss")]
    assert PRIVATE_SAMPLE in boss_titles and "老板体检安排" in boss_titles


# ---------------------------------------------------------------------------
# V2 personal private 对全部员工不可见
# ---------------------------------------------------------------------------

def test_v2_personal_private_invisible_to_all_employees(db_conn, seeded):
    _seed_memory(db_conn, "personal", "private", "私事备忘")
    for role in config.ROLE_ORDER:
        if role == "personal":
            continue
        for table in ("tasks", "memory"):
            rows = vis.query_visible(table, role)
            assert all(r["owner_role"] != "personal" or r["visibility"] == "public"
                       for r in rows), f"{role} 泄露 personal private ({table})"


# ---------------------------------------------------------------------------
# V3 coordinator 上下文无 private
# ---------------------------------------------------------------------------

def test_v3_context_items_public_only():
    items = [
        {"owner_role": "cfo", "visibility": "private", "text": PRIVATE_SAMPLE},
        {"owner_role": "sales", "visibility": "public", "text": "公开"},
        {"owner_role": "coordinator", "visibility": "private", "text": "自有私密"},
    ]
    out = vis.filter_context_items(items, "coordinator")
    assert len(out) == 1 and out[0]["text"] == "公开"
    assert PRIVATE_SAMPLE not in json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# V4 交接包不带 private（含 to_role 自有 private 反例）
# ---------------------------------------------------------------------------

def test_v4_handoff_public_only_including_target_own_private():
    pack = {"items": [
        {"owner_role": "cfo", "visibility": "private", "text": PRIVATE_SAMPLE},
        {"owner_role": "sales", "visibility": "private", "text": "sales自有私密"},  # 反例
        {"owner_role": "sales", "visibility": "public", "text": "公开线索"},
    ]}
    out = [it for it in pack["items"] if vis.handoff_filter(it)]
    assert len(out) == 1 and out[0]["text"] == "公开线索"
    serialized = json.dumps(out, ensure_ascii=False)
    assert PRIVATE_SAMPLE not in serialized and "sales自有私密" not in serialized


# ---------------------------------------------------------------------------
# V5 披露审批：原子消费 / 过期临界 / 同构占位 / 仅 boss 决议
# ---------------------------------------------------------------------------

def _request(db_conn, seeded, to_role="sales"):
    return vis.request_disclosure("tasks", seeded["cfo_private"], "cfo", to_role, "报价核算")


def test_v5_pending_consume_placeholder(db_conn, seeded):
    aid = _request(db_conn, seeded)
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER


def test_v5_approve_consume_once(db_conn, seeded):
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    got = vis.consume_grant(aid, "sales")
    assert got["title"] == PRIVATE_SAMPLE
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER   # 二次消费失败
    status = db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()[0]
    assert status == "expired"


def test_v5_concurrent_double_consume_exactly_one(db_conn, seeded):
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    results, barrier = [], threading.Barrier(2)

    def racer():
        conn = db.get_conn()  # 每线程独立连接
        barrier.wait()
        results.append(vis.consume_grant(aid, "sales"))
        db.close_thread_conns()

    ts = [threading.Thread(target=racer) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)
    ok = [r for r in results if r != vis.PLACEHOLDER]
    assert len(ok) == 1 and ok[0]["title"] == PRIVATE_SAMPLE


def test_v5_placeholder_isomorphic_all_failure_modes(db_conn, seeded):
    """未授权/过期/拒绝/角色不符/目标不存在/审批单不存在 → 完全同构。"""
    modes = []
    modes.append(vis.consume_grant(_request(db_conn, seeded), "sales"))        # pending
    a = _request(db_conn, seeded); vis.resolve_disclosure(a, "rejected", actor="boss")
    modes.append(vis.consume_grant(a, "sales"))                                 # rejected
    modes.append(vis.consume_grant(_request(db_conn, seeded), "marketing"))     # 角色不符(pending)
    a = _request(db_conn, seeded); vis.resolve_disclosure(a, "approved", actor="boss")
    modes.append(vis.consume_grant(a, "marketing"))                             # 角色不符(approved)
    a = _request(db_conn, seeded, to_role="sales")
    vis.resolve_disclosure(a, "approved", actor="boss")
    with db_conn:
        db_conn.execute("DELETE FROM tasks WHERE id=?", (seeded["cfo_private"],))
    modes.append(vis.consume_grant(a, "sales"))                                 # 目标不存在
    modes.append(vis.consume_grant(99999, "sales"))                             # 审批单不存在
    for m in modes:
        assert m == vis.PLACEHOLDER
    assert all(json.dumps(m, sort_keys=True) == json.dumps(vis.PLACEHOLDER, sort_keys=True)
               for m in modes)


def test_v5_only_boss_resolves(db_conn, seeded):
    aid = _request(db_conn, seeded)
    for actor in ("cfo", "sales", "coordinator", "personal"):
        with pytest.raises(PermissionError):
            vis.resolve_disclosure(aid, "approved", actor=actor)
    assert db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()[0] == "pending"


def test_v5_decision_whitelist(db_conn, seeded):
    aid = _request(db_conn, seeded)
    for bad in ("hacked", "APPROVED", "", "expired"):
        with pytest.raises(ValueError):
            vis.resolve_disclosure(aid, bad, actor="boss")
    assert db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()[0] == "pending"


def test_v5_repeat_resolve_idempotent(db_conn, seeded):
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    vis.resolve_disclosure(aid, "rejected", actor="boss")   # 不覆盖既有决议
    assert db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()[0] == "approved"


def test_v5_expiry_critical_resolve(db_conn, seeded, monkeypatch):
    """临界：expires_at-1s 可决议；+1s 拒绝且 pending 已转 expired；恰等判过期。

    三个审批单均在同一基准时刻 t0 申请（expires_at 相同），再分别拨钟验证。
    """
    t0 = 1_000_000.0
    monkeypatch.setattr(vis, "_now", lambda: t0)
    aid1 = _request(db_conn, seeded)
    aid2 = _request(db_conn, seeded)
    aid3 = _request(db_conn, seeded)
    expires = t0 + vis.APPROVAL_TTL_S

    # -1s：仍可决议
    monkeypatch.setattr(vis, "_now", lambda: expires - 1)
    vis.resolve_disclosure(aid1, "approved", actor="boss")
    assert db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid1,)).fetchone()[0] == "approved"

    # +1s：决议被拒（幂等无操作），且 pending 已被 _sweep_expired 转为 expired
    monkeypatch.setattr(vis, "_now", lambda: expires + 1)
    vis.resolve_disclosure(aid2, "approved", actor="boss")
    assert db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid2,)).fetchone()[0] == "expired"

    # 恰等 expires_at：<= 语义判过期
    monkeypatch.setattr(vis, "_now", lambda: expires)
    vis.resolve_disclosure(aid3, "approved", actor="boss")
    assert db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid3,)).fetchone()[0] == "expired"


def test_v5_expiry_critical_consume(db_conn, seeded, monkeypatch):
    t0 = 2_000_000.0
    monkeypatch.setattr(vis, "_now", lambda: t0)
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    expires = t0 + vis.APPROVAL_TTL_S
    monkeypatch.setattr(vis, "_now", lambda: expires + 1)   # 授权超期
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER


def test_v5_corrupt_payload_fail_closed(db_conn, seeded):
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    with db_conn:
        db_conn.execute("UPDATE approvals SET payload='{broken json' WHERE id=?", (aid,))
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER   # 不泄露、不崩溃


# ---------------------------------------------------------------------------
# V6 可见性优先级链
# ---------------------------------------------------------------------------

def test_v6_role_default_and_sensitive():
    assert vis.resolve_visibility("cfo", "普通文本") == ("private", "global", "role_default")
    assert vis.resolve_visibility("sales", "普通文本") == ("public", "global", "role_default")
    assert vis.resolve_visibility("sales", "他的存款情况") == ("private", "global", "sensitive")


def test_v6_explicit_overrides_role_and_sensitive():
    assert vis.resolve_visibility("cfo", "普通", explicit="public") == ("public", "global", "explicit")
    assert vis.resolve_visibility("sales", "密码", explicit="public") == ("public", "global", "explicit")
    assert vis.resolve_visibility("sales", "普通", explicit="private") == ("private", "global", "explicit")


def test_v6_report_forced_private():
    assert vis.resolve_visibility("sales", "无任何敏感词") == ("public", "global", "role_default")
    assert vis.resolve_visibility("sales", "无任何敏感词", kind="report") == ("private", "global", "forced_report")
    v, s, src = vis.resolve_visibility("sales", "x", kind="report", explicit="public")
    assert (v, s, src) == ("public", "global", "explicit_override_forced")


def test_v6_preference_public_global():
    assert vis.resolve_visibility("personal", "老板喜欢简洁汇报", kind="preference") == \
        ("public", "global", "preference")


def test_v6_invalid_kind_and_explicit_internal():
    with pytest.raises(ValueError):
        vis.resolve_visibility("sales", "x", kind="secret")
    with pytest.raises(ValueError):
        vis.resolve_visibility("sales", "x", explicit="internal")
    with pytest.raises(ValueError):
        vis.resolve_visibility("ghost", "x")


# ---------------------------------------------------------------------------
# V7 caller 伪造与非法
# ---------------------------------------------------------------------------

def test_v7_spoof_dropped_and_logged(isolated_env):
    assert vis.resolve_caller("sales", {"caller_role": "boss", "owner_role": "cfo"}) == "sales"
    log_text = (config.LOG_DIR / "audit.log").read_text(encoding="utf-8")
    assert "caller_spoof_dropped" in log_text


def test_v7_unknown_frame_caller_fail_closed():
    for bad in ("ghost", "", None, "BOSS"):
        with pytest.raises(ValueError):
            vis.resolve_caller(bad, {})


# ---------------------------------------------------------------------------
# V8 internal 拒写 / session 合法但不落库
# ---------------------------------------------------------------------------

def test_v8_internal_rejected_all_paths(db_conn):
    with pytest.raises(ValueError):
        vis.validate_write("sales", "internal", "global")
    with pytest.raises(ValueError):
        vis.validate_persist("sales", "internal", "global")
    with pytest.raises(sqlite3.IntegrityError):
        with db_conn:
            db_conn.execute(
                "INSERT INTO tasks(title,owner_role,visibility,created_at,updated_at) "
                "VALUES('x','sales','internal',?,?)", (db.now_iso(), db.now_iso()))


def test_v8_session_legal_in_memory_never_in_sqlite(db_conn):
    vis.validate_write("personal", "private", "session")     # 合法，不抛错
    buf = vis.SessionBuffer()
    buf.put("s1", "draft", SESSION_SAMPLE)
    assert buf.get("s1", "draft") == SESSION_SAMPLE
    with pytest.raises(ValueError):
        vis.validate_persist("personal", "private", "session")
    buf.clear("s1")
    assert buf.get("s1", "draft") is None
    # 全库逐表扫描：session 样本零命中（证明未进 SQLite，而非仅拒写）
    tables = [r[0] for r in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = [c[1] for c in db_conn.execute(f"PRAGMA table_info({t})")]
        for row in db_conn.execute(f"SELECT * FROM {t}"):
            for c in cols:
                val = row[c]
                if isinstance(val, (str, bytes)):
                    assert SESSION_SAMPLE not in str(val), f"session 数据落入 {t}.{c}"


# ---------------------------------------------------------------------------
# V9 日志全文无 private/Key + 注入清洗
# ---------------------------------------------------------------------------

def test_v9_log_scan_no_private_no_key(db_conn, seeded):
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    vis.consume_grant(aid, "sales")
    vis.resolve_caller("sales", {"caller_role": "boss"})
    vis.request_disclosure("tasks", seeded["cfo_private"], "cfo", "hr", PRIVATE_SAMPLE)
    log_text = (config.LOG_DIR / "audit.log").read_text(encoding="utf-8")
    assert PRIVATE_SAMPLE not in log_text     # reason 含私密样本也不落盘
    assert KEY_SAMPLE not in log_text
    assert "老板体检安排" not in log_text


def test_v9_log_injection_sanitized(isolated_env):
    audit_log.log("INFO", "evil\n{\"level\":\"ERROR\"}", target_id="1\r\n2",
                  content="白名单外字段应被丢弃", api_key=KEY_SAMPLE)
    lines = (config.LOG_DIR / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1                    # 换行被清洗，未伪造出第二行
    rec = json.loads(lines[0])
    assert "content" not in rec and "api_key" not in rec
    assert KEY_SAMPLE not in lines[0]


# ---------------------------------------------------------------------------
# 补充：query_visible 防注入
# ---------------------------------------------------------------------------

def test_query_whitelist_rejects_bad_table_and_column(db_conn, seeded):
    with pytest.raises(ValueError):
        vis.query_visible("approvals", "boss")
    with pytest.raises(ValueError):
        vis.query_visible("tasks; DROP TABLE tasks", "boss")
    with pytest.raises(ValueError):
        vis.query_visible("tasks", "sales", title="x")       # title 不在过滤白名单
    with pytest.raises(ValueError):
        vis.query_visible("tasks", "ghost")


def test_query_filter_value_parameterized(db_conn, seeded):
    rows = vis.query_visible("tasks", "boss", status="' OR 1=1 --")
    assert rows == []                          # 注入串作为普通参数值，无副作用
    assert db_conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] >= 4


# ---------------------------------------------------------------------------
# M2 复审修复回归（阻断项 1/2/3 + 两项建议）
# ---------------------------------------------------------------------------

def _status(db_conn, aid):
    return db_conn.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()[0]


def test_fix1_consume_sweeps_expired_pending(db_conn, seeded, monkeypatch):
    """阻断1：过期 pending 在 consume 后转为 expired（不再滞留 pending）。"""
    t0 = 3_000_000.0
    monkeypatch.setattr(vis, "_now", lambda: t0)
    aid = _request(db_conn, seeded)
    monkeypatch.setattr(vis, "_now", lambda: t0 + vis.APPROVAL_TTL_S + 1)
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, aid) == "expired"


def test_fix1_consume_sweeps_expired_approved(db_conn, seeded, monkeypatch):
    """阻断1：过期 approved 在 consume 后转为 expired。"""
    t0 = 3_100_000.0
    monkeypatch.setattr(vis, "_now", lambda: t0)
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    monkeypatch.setattr(vis, "_now", lambda: t0 + vis.APPROVAL_TTL_S + 1)
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, aid) == "expired"


def test_fix2_target_missing_rolls_back_grant(db_conn, seeded):
    """阻断2：目标不存在 → 整体回滚，审批恢复 approved 可重试，无半消费。"""
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    with db_conn:
        db_conn.execute("DELETE FROM tasks WHERE id=?", (seeded["cfo_private"],))
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, aid) == "approved"      # 未烧掉，可重试
    # 目标恢复后重试成功（证明回滚完整、授权仍有效）
    new_id = _seed_task(db_conn, "cfo", "private", PRIVATE_SAMPLE)
    with db_conn:
        db_conn.execute(
            "UPDATE approvals SET payload=json_set(payload,'$.item_id',?) WHERE id=?",
            (new_id, aid))
    got = vis.consume_grant(aid, "sales")
    assert got["title"] == PRIVATE_SAMPLE
    assert _status(db_conn, aid) == "expired"


def test_fix2_corrupt_payload_rolls_back(db_conn, seeded):
    """阻断2：payload 损坏 → 回滚，审批状态不变（approved），fail closed。"""
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    with db_conn:
        db_conn.execute("UPDATE approvals SET payload='{broken' WHERE id=?", (aid,))
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, aid) == "approved"      # 回滚，未半消费
    assert not db.get_conn().in_transaction


def test_fix2_read_failure_rolls_back(db_conn, seeded, monkeypatch):
    """阻断2：目标读取异常 → 整体回滚（连接代理注入读取故障）。"""
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    real_conn = db.get_conn()

    class FlakyConn:
        """代理真实连接：仅目标读取抛错，其余透传（sqlite3 属性只读，不能直接 patch）。"""

        def execute(self, sql, *a, **kw):
            if sql.startswith("SELECT * FROM tasks WHERE id="):
                raise sqlite3.OperationalError("simulated read failure")
            return real_conn.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    monkeypatch.setattr(db, "get_conn", lambda *a, **kw: FlakyConn())
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER
    monkeypatch.undo()
    assert _status(db_conn, aid) == "approved"
    assert not real_conn.in_transaction


def test_fix3_nonjson1_paths_semantics_match(db_conn, seeded, monkeypatch):
    """阻断3：JSON1 不可用时，resolve/consume 过期与消费语义与主路径一致。"""
    monkeypatch.setattr(vis, "_has_json1", lambda conn: False)
    t0 = 3_200_000.0
    monkeypatch.setattr(vis, "_now", lambda: t0)
    # 过期 pending：resolve 拒绝且转 expired
    a1 = _request(db_conn, seeded)
    monkeypatch.setattr(vis, "_now", lambda: t0 + vis.APPROVAL_TTL_S + 1)
    vis.resolve_disclosure(a1, "approved", actor="boss")
    assert _status(db_conn, a1) == "expired"
    # 未过期：正常批准 + 单次消费
    monkeypatch.setattr(vis, "_now", lambda: t0)
    a2 = _request(db_conn, seeded)
    vis.resolve_disclosure(a2, "approved", actor="boss")
    assert _status(db_conn, a2) == "approved"
    got = vis.consume_grant(a2, "sales")
    assert got["title"] == PRIVATE_SAMPLE
    assert vis.consume_grant(a2, "sales") == vis.PLACEHOLDER
    # 过期 approved：consume 清扫转 expired
    monkeypatch.setattr(vis, "_now", lambda: t0)
    a3 = _request(db_conn, seeded)
    vis.resolve_disclosure(a3, "approved", actor="boss")
    monkeypatch.setattr(vis, "_now", lambda: t0 + vis.APPROVAL_TTL_S + 1)
    assert vis.consume_grant(a3, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, a3) == "expired"
    # 目标缺失：备选路径同样回滚到 approved
    monkeypatch.setattr(vis, "_now", lambda: t0)
    a4 = _request(db_conn, seeded)
    vis.resolve_disclosure(a4, "approved", actor="boss")
    with db_conn:
        db_conn.execute("DELETE FROM tasks WHERE id=?", (seeded["cfo_private"],))
    assert vis.consume_grant(a4, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, a4) == "approved"


def test_fix3_nonjson1_concurrent_exactly_one(db_conn, seeded, monkeypatch):
    """备选路径并发双消费同样恰好一次成功。"""
    monkeypatch.setattr(vis, "_has_json1", lambda conn: False)
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    results, barrier = [], threading.Barrier(2)

    def racer():
        db.get_conn()
        barrier.wait()
        results.append(vis.consume_grant(aid, "sales"))
        db.close_thread_conns()

    ts = [threading.Thread(target=racer) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)
    ok = [r for r in results if r != vis.PLACEHOLDER]
    assert len(ok) == 1 and ok[0]["title"] == PRIVATE_SAMPLE


def test_suggestion_context_filter_rejects_invalid_caller():
    """建议1：filter_context_items 入口自身校验 caller，非法角色 fail closed。"""
    for bad in ("ghost", "", None):
        with pytest.raises(ValueError):
            vis.filter_context_items([], bad)


# ---------------------------------------------------------------------------
# M2 第三轮复审修复回归：expires_at JSON 类型守卫 + 审批类型隔离
# ---------------------------------------------------------------------------

_BAD_EXPIRES = ["garbage", "9999999999", True, False, None, [1], {"a": 1}]


def _set_expires(db_conn, aid, value):
    # json_set 按绑定值类型决定 JSON 类型：必须传原始 Python 值
    #（int→integer、float→real、str→text、bool→true/false、None→null）；
    # 传 json.dumps 文本会把数值存成字符串，反而制造类型非法。
    # list/dict 无对应 SQL 绑定类型，经 json() 包装写入。
    with db_conn:
        if isinstance(value, (list, dict, bool)):
            # bool 直接绑定会被 SQLite 转成 0/1 整数，必须经 json() 保留布尔类型
            db_conn.execute(
                "UPDATE approvals SET payload=json_set(payload,'$.expires_at',json(?)) "
                "WHERE id=?", (json.dumps(value), aid))
        else:
            db_conn.execute(
                "UPDATE approvals SET payload=json_set(payload,'$.expires_at',?) "
                "WHERE id=?", (value, aid))


@pytest.mark.parametrize("bad", _BAD_EXPIRES)
@pytest.mark.parametrize("use_json1", [True, False], ids=["json1", "fallback"])
def test_fix4_bad_expires_type_fail_closed(db_conn, seeded, monkeypatch, bad, use_json1):
    """阻断1：非法 expires_at 类型（字符串/布尔/null/数组/对象）两路径一律拒绝。"""
    monkeypatch.setattr(vis, "_has_json1", lambda conn: use_json1)
    aid = _request(db_conn, seeded)
    _set_expires(db_conn, aid, bad)
    # 决议被拒：状态保持 pending（类型非法不被清扫转换，也不被批准）
    vis.resolve_disclosure(aid, "approved", actor="boss")
    assert _status(db_conn, aid) == "pending"
    # 消费被拒：同构占位，状态不变
    assert vis.consume_grant(aid, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, aid) == "pending"


def test_fix4_numeric_string_expires_not_greater_than_now(db_conn, seeded):
    """复审实证场景：字符串 'garbage' 在裸比较下 > now 恒真，守卫后必须拒绝。"""
    aid = _request(db_conn, seeded)
    _set_expires(db_conn, aid, "garbage")
    # 直接验证守卫 SQL 语义
    row = db_conn.execute(
        "SELECT json_extract(payload,'$.expires_at') > 100 AS bare, "
        "json_type(payload,'$.expires_at') IN ('integer','real') AS guarded "
        "FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["bare"] == 1 and row["guarded"] == 0   # 缺口存在，守卫拦截
    vis.resolve_disclosure(aid, "approved", actor="boss")
    assert _status(db_conn, aid) == "pending"


def test_fix4_valid_numeric_types_still_work(db_conn, seeded):
    """integer 与 real 两种合法数值类型不受守卫误伤。"""
    for value in (int(vis._now() + 3600), vis._now() + 3600.5):
        aid = _request(db_conn, seeded)
        _set_expires(db_conn, aid, value)
        vis.resolve_disclosure(aid, "approved", actor="boss")
        assert _status(db_conn, aid) == "approved"
        got = vis.consume_grant(aid, "sales")
        assert got["title"] == PRIVATE_SAMPLE


def _seed_bulk_delete(db_conn):
    with db_conn:
        cur = db_conn.execute(
            "INSERT INTO approvals(kind,payload,status,created_at) "
            "VALUES('bulk_delete',?,'pending',?)",
            (json.dumps({"ids": [1, 2], "to_role": "sales",
                         "expires_at": vis._now() + 99999}), db.now_iso()))
    return cur.lastrowid


@pytest.mark.parametrize("use_json1", [True, False], ids=["json1", "fallback"])
def test_fix5_bulk_delete_not_resolvable_or_consumable(db_conn, seeded, monkeypatch, use_json1):
    """阻断2：bulk_delete 审批单不能被披露接口批准或消费（两路径一致）。"""
    monkeypatch.setattr(vis, "_has_json1", lambda conn: use_json1)
    bid = _seed_bulk_delete(db_conn)
    vis.resolve_disclosure(bid, "approved", actor="boss")   # 幂等无操作
    assert _status(db_conn, bid) == "pending"               # 未被批准
    assert vis.consume_grant(bid, "sales") == vis.PLACEHOLDER
    assert _status(db_conn, bid) == "pending"               # 未被消费/清扫
    # 对照：同库的披露单不受影响，正常批准消费
    aid = _request(db_conn, seeded)
    vis.resolve_disclosure(aid, "approved", actor="boss")
    assert vis.consume_grant(aid, "sales")["title"] == PRIVATE_SAMPLE


def test_suggestion_log_reserved_fields(isolated_env):
    """建议2：timestamp/level 为内部保留字段，调用参数不得伪造。"""
    # timestamp 在白名单内但属内部保留字段：经 fields 传入不得覆盖自动值
    # （action/level 是签名参数，经 fields 传会 TypeError，故只伪造 timestamp）
    audit_log.log("INFO", "probe", **{"timestamp": "1999-01-01T00:00:00+00:00",
                                      "target_id": "42"})
    rec = json.loads((config.LOG_DIR / "audit.log").read_text(encoding="utf-8").strip())
    assert rec["level"] == "INFO"                    # 未被覆盖
    assert not rec["timestamp"].startswith("1999")   # 未被覆盖
    assert rec["action"] == "probe"                  # action 也不被 fields 覆盖
    assert rec["target_id"] == "42"                  # 正常白名单字段不受影响
