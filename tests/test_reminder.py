"""S 组提醒专项（M3.1 清单 §三.4 v6 定稿：三职责拆分 + 六时点语义）。

select_due 纯函数（零 DB 零队列）/ process_due 事务（先库后队）/
reminder_loop 线程壳；_deliver_and_mark 统一投递原语（正常与补扫共用）；
补偿窗口 10min；标记提交后崩溃即使窗口内也可能永久丢失（如实声明）。
repeat 新建一行、Asia/Shanghai 墙钟保持。
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from opc_workbench import config, db, reminder

SH = ZoneInfo("Asia/Shanghai")


def _iso(s: str) -> str:
    return s  # 测试直接给 aware ISO 字面量


def _epoch(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _row(rid, remind_at, status="pending", repeat="none", notified_at=None,
         recovery=None):
    return {"id": rid, "title": f"r{rid}", "remind_at": remind_at,
            "repeat_rule": repeat, "status": status, "owner_role": "personal",
            "visibility": "private", "notified_at": notified_at,
            "recovery_enqueued_at": recovery}


# ---------------------------------------------------------------------------
# select_due：纯函数时间边界
# ---------------------------------------------------------------------------

def test_select_due_exact_boundary():
    now = _epoch("2026-11-21T09:00:00+08:00")
    rows = [
        _row(1, "2026-11-21T09:00:00+08:00"),   # 恰等 → 命中
        _row(2, "2026-11-21T09:00:01+08:00"),   # 晚 1s → 不命中
        _row(3, "2026-11-21T08:59:59+08:00"),   # 早 1s → 命中
        _row(4, "2026-11-21T09:00:00+08:00", status="notified"),  # 非 pending
        _row(5, "2026-11-21T09:00:00+08:00", status="cancelled"),
    ]
    due = reminder.select_due(now, rows)
    assert [d["id"] for d in due] == [1, 3]


def test_select_due_cross_day_and_timezone():
    """UTC 与本地墙钟混排：比较统一 UTC epoch，本地 9:00 不因换算漂移。"""
    now = _epoch("2026-11-21T01:00:00+00:00")   # = 本地 09:00
    rows = [
        _row(1, "2026-11-21T09:00:00+08:00"),   # 同一时刻 → 命中
        _row(2, "2026-11-21T00:59:00+00:00"),   # 早 1min → 命中
        _row(3, "2026-11-21T01:01:00+00:00"),   # 晚 1min → 不命中
    ]
    assert [d["id"] for d in reminder.select_due(now, rows)] == [1, 2]


def test_select_due_repeat_next_instance_wallclock():
    """daily/weekly 下次实例：Asia/Shanghai 墙钟保持（本地 9:00 永远 9:00）。"""
    now = _epoch("2026-11-21T09:00:00+08:00")
    rows = [
        _row(1, "2026-11-21T09:00:00+08:00", repeat="daily"),
        _row(2, "2026-11-21T09:00:00+08:00", repeat="weekly"),
        _row(3, "2026-11-21T09:00:00+08:00", repeat="none"),
    ]
    due = reminder.select_due(now, rows)
    by_id = {d["id"]: d for d in due}
    assert by_id[1]["next_remind_at"] == "2026-11-22T09:00:00+08:00"
    assert by_id[2]["next_remind_at"] == "2026-11-28T09:00:00+08:00"
    assert by_id[3]["next_remind_at"] is None


def test_select_due_weekly_cross_month():
    now = _epoch("2026-11-28T09:00:00+08:00")
    rows = [_row(1, "2026-11-28T09:00:00+08:00", repeat="weekly")]
    due = reminder.select_due(now, rows)
    assert due[0]["next_remind_at"] == "2026-12-05T09:00:00+08:00"


def test_select_due_daily_cross_year():
    now = _epoch("2026-12-31T09:00:00+08:00")
    rows = [_row(1, "2026-12-31T09:00:00+08:00", repeat="daily")]
    assert reminder.select_due(now, rows)[0]["next_remind_at"] == "2027-01-01T09:00:00+08:00"


def test_select_due_pure_no_side_effects(db_conn):
    """纯函数断言：调用前后库与队列零变化。"""
    now = _epoch("2026-11-21T09:00:00+08:00")
    rows = [_row(1, "2026-11-21T09:00:00+08:00", repeat="daily")]
    before = db_conn.execute("SELECT COUNT(*) c FROM schedules").fetchone()["c"]
    reminder.select_due(now, rows)
    assert db_conn.execute("SELECT COUNT(*) c FROM schedules").fetchone()["c"] == before


# ---------------------------------------------------------------------------
# process_due + _deliver_and_mark：六时点语义
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine(isolated_env):
    w = db._open_test_writer(config.DB_PATH)
    q = queue.Queue(maxsize=50)
    eng = reminder.ReminderEngine(writer=w, notify_queue=q)
    yield eng
    w.close()


def _seed_schedule(conn, remind_at="2026-11-21T09:00:00+08:00", repeat="none"):
    with conn:
        cur = conn.execute(
            "INSERT INTO schedules(title,remind_at,repeat_rule,owner_role,"
            "created_at,updated_at) VALUES('提醒',?,?, 'personal',?,?)",
            (remind_at, repeat, db.now_iso(), db.now_iso()))
    return cur.lastrowid


def _now_fixed(monkeypatch, s="2026-11-21T09:00:00+08:00"):
    monkeypatch.setattr(reminder, "_now", lambda: _epoch(s))


def test_normal_delivery_marks_and_no_rescan(engine, db_conn, monkeypatch):
    """v6 核心：正常入队成功 → recovery_enqueued_at 非空 → 重启补扫不重复。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)
    n = engine.process_due()
    assert n == 1
    row = db_conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "notified"
    assert row["notified_at"] is not None
    assert row["recovery_enqueued_at"] is not None   # 正常路径同样写标记
    assert engine.notify_queue.get_nowait()["schedule_id"] == sid
    # 重启补扫：候选集为空
    assert engine.recovery_scan() == 0
    assert engine.notify_queue.empty()


def test_enqueue_failure_leaves_null_mark_and_rescan(engine, db_conn, monkeypatch):
    """入队失败（注入队列异常）→ 标记 NULL → 窗口内补投。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)

    class BrokenQueue:
        def put_nowait(self, item):
            raise RuntimeError("queue broken")
        def get_nowait(self):
            raise queue.Empty
        def full(self):
            return False
        def qsize(self):
            return 0

    engine.notify_queue = BrokenQueue()
    engine.process_due()
    row = db_conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "notified"             # 库状态已提交
    assert row["recovery_enqueued_at"] is None     # 未入队不写标记
    # 恢复队列后补扫投出
    engine.notify_queue = queue.Queue(maxsize=50)
    assert engine.recovery_scan() == 1
    assert engine.notify_queue.get_nowait()["schedule_id"] == sid


def test_mark_db_failure_keeps_notified(engine, db_conn, monkeypatch):
    """标记写入失败（注入 DB 错）→ 标记 NULL + notified 完好 + ERROR。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)
    def broken_mark(ids):
        raise RuntimeError("db mark failure")

    monkeypatch.setattr(reminder.ReminderEngine, "_mark_enqueued", lambda self, ids: broken_mark(ids))
    engine.process_due()
    row = db_conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "notified"
    assert row["recovery_enqueued_at"] is None
    assert not engine.notify_queue.empty()   # 已入队（可能重复，best-effort）


def test_queue_full_drops_oldest_warns(engine, db_conn, monkeypatch):
    """队列满 → 丢最旧腾位 + WARN；新条入队并标记；被丢条不再补投。"""
    _now_fixed(monkeypatch)
    # 预填 50 条旧通知（模拟已标记的历史条目）
    old_ids = []
    for i in range(50):
        sid = _seed_schedule(db_conn, f"2026-11-21T08:{i:02d}:00+08:00")
        old_ids.append(sid)
    with db_conn:
        db_conn.execute("UPDATE schedules SET status='notified', notified_at=?, "
                        "recovery_enqueued_at=?", (db.now_iso(), db.now_iso()))
    for sid in old_ids:
        engine.notify_queue.put_nowait({"schedule_id": sid, "title": "old"})
    new_sid = _seed_schedule(db_conn, "2026-11-21T09:00:00+08:00")
    engine.process_due()
    items = []
    while not engine.notify_queue.empty():
        items.append(engine.notify_queue.get_nowait())
    assert len(items) == 50
    assert items[-1]["schedule_id"] == new_sid     # 新条在队尾
    assert old_ids[0] not in [it["schedule_id"] for it in items]  # 最旧被丢
    row = db_conn.execute("SELECT * FROM schedules WHERE id=?", (new_sid,)).fetchone()
    assert row["recovery_enqueued_at"] is not None
    # 被丢条标记早已非空 → 补扫不投
    assert engine.recovery_scan() == 0


def test_crash_after_mark_commit_permanent_loss(engine, db_conn, monkeypatch):
    """标记提交后崩溃 → 第二次启动不补投（窗口内永久丢失语义实证）。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)
    engine.process_due()
    # 模拟崩溃：队列内容丢失（进程重启队列清空），标记已提交
    engine.notify_queue = queue.Queue(maxsize=50)
    assert engine.recovery_scan() == 0   # 标记非空 → 不补投（丢失已声明）
    row = db_conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    assert row["recovery_enqueued_at"] is not None


def test_crash_before_mark_rescan_redelivers_once(engine, db_conn, monkeypatch):
    """入队后标记前崩溃 → 重启重复投递一次；连续 ≥2 次重启不重复。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)
    # 模拟：process_due 提交 notified 但标记前崩溃
    with db_conn:
        db_conn.execute("UPDATE schedules SET status='notified', notified_at=? "
                        "WHERE id=?", (db.now_iso(), sid))
    engine.notify_queue = queue.Queue(maxsize=50)   # 崩溃后队列空
    assert engine.recovery_scan() == 1
    assert engine.notify_queue.get_nowait()["schedule_id"] == sid
    # 第二次重启：标记已写 → 不补投
    engine.notify_queue = queue.Queue(maxsize=50)
    assert engine.recovery_scan() == 0
    # 第三次重启同样不补投
    assert engine.recovery_scan() == 0


def test_outside_window_no_rescan(engine, db_conn, monkeypatch):
    """窗口外（>10min）不补扫。"""
    _now_fixed(monkeypatch, "2026-11-21T09:00:00+08:00")
    sid = _seed_schedule(db_conn)
    with db_conn:
        db_conn.execute("UPDATE schedules SET status='notified', notified_at=? "
                        "WHERE id=?", ("2026-11-21T08:49:00+08:00", sid))
    _now_fixed(monkeypatch, "2026-11-21T09:00:01+08:00")   # 超窗 1s
    assert engine.recovery_scan() == 0


def test_repeat_creates_new_row_original_untouched(engine, db_conn, monkeypatch):
    """repeat 新建一行：原行 notified 不动，新行 pending + 下次墙钟时间。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn, repeat="daily")
    engine.process_due()
    rows = db_conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    assert len(rows) == 2
    orig, new = rows[0], rows[1]
    assert orig["id"] == sid and orig["status"] == "notified"
    assert orig["remind_at"] == "2026-11-21T09:00:00+08:00"   # 不被覆盖
    assert new["status"] == "pending"
    assert new["remind_at"] == "2026-11-22T09:00:00+08:00"
    assert new["repeat_rule"] == "daily"


def test_process_due_atomic_midway_failure(engine, db_conn, monkeypatch):
    """事务原子：注入中途失败 → 全回滚（无半 notified）。"""
    _now_fixed(monkeypatch)
    s1 = _seed_schedule(db_conn, "2026-11-21T08:59:00+08:00")
    s2 = _seed_schedule(db_conn, "2026-11-21T08:59:30+08:00", repeat="daily")
    def broken_insert(item, **kw):
        raise RuntimeError("midway failure")

    monkeypatch.setattr(reminder.ReminderEngine, "_insert_next_instance", lambda self, item, **kw: broken_insert(item, **kw))
    with pytest.raises(RuntimeError):
        engine.process_due()
    for sid in (s1, s2):
        row = db_conn.execute("SELECT status FROM schedules WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "pending"   # 全回滚
    assert engine.notify_queue.empty()      # 先库后队：库未提交不投递


def test_db_before_queue_order(engine, db_conn, monkeypatch):
    """先库后队：投递时库内已是 notified（队列消费者读库可见）。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)
    seen = {}

    class ObservingQueue(queue.Queue):
        def put_nowait(self, item):
            row = db.get_read_conn().execute(
                "SELECT status FROM schedules WHERE id=?",
                (item["schedule_id"],)).fetchone()
            seen["status_at_enqueue"] = row["status"]
            super().put_nowait(item)

    engine.notify_queue = ObservingQueue(maxsize=50)
    engine.process_due()
    assert seen["status_at_enqueue"] == "notified"


# ---------------------------------------------------------------------------
# reminder_loop：线程壳（停线程纪律）
# ---------------------------------------------------------------------------

def test_reminder_loop_stops_cleanly(engine, db_conn, monkeypatch):
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)
    stop = threading.Event()
    t = threading.Thread(target=engine.reminder_loop, args=(stop,), daemon=True)
    t.start()
    # 等待首轮处理完成
    import time
    for _ in range(100):
        if not engine.notify_queue.empty():
            break
        time.sleep(0.02)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert engine.notify_queue.get_nowait()["schedule_id"] == sid


def test_loop_exception_does_not_kill_thread(engine, db_conn, monkeypatch):
    """异常记 ERROR 不击穿线程：首轮注入异常，次轮仍处理。"""
    _now_fixed(monkeypatch)
    sid = _seed_schedule(db_conn)
    calls = {"n": 0}
    real_process = engine.process_due

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real_process()

    monkeypatch.setattr(reminder.ReminderEngine, "process_due", lambda self: flaky())
    stop = threading.Event()
    monkeypatch.setattr(config, "REMINDER_SCAN_INTERVAL_S", 0)
    t = threading.Thread(target=engine.reminder_loop, args=(stop,), daemon=True)
    t.start()
    import time
    for _ in range(200):
        if not engine.notify_queue.empty():
            break
        time.sleep(0.02)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert calls["n"] >= 2
    assert engine.notify_queue.get_nowait()["schedule_id"] == sid