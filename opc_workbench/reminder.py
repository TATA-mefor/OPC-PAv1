"""提醒引擎（M3.1 清单 §三.4 v6 定稿：方案 A best-effort）。

三职责拆分：
- select_due(now, rows)：纯函数，零 DB 零队列零副作用；输出到期条目 +
  repeat 下次实例计算结果（Asia/Shanghai 墙钟保持）。
- process_due()：单一 BEGIN IMMEDIATE 事务置 notified + notified_at +
  repeat 新行；COMMIT 成功后经 _deliver_and_mark 投递（先库后队）。
- reminder_loop(stop_event)：线程壳，每轮落库前复查 stop_event（幽灵写
  纪律）；异常记 ERROR 不击穿线程。

统一投递原语 _deliver_and_mark：正常路径与恢复补投共用——
入队成功才写 recovery_enqueued_at；入队失败不写标记（留补扫候选）；
队列满丢最旧 + WARN；标记写入失败 ERROR、notified 不回滚。

诚实声明（不承诺可靠送达）：
- 标记提交后崩溃 → 即使仍在窗口内也可能永久丢失；
- 入队后标记前崩溃 → 重启窗口内重复投递一次（best-effort 接受）；
- 窗口外（>COMPENSATION_WINDOW）丢失不自动恢复；M6"今日已提醒"人工兜底。
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import audit_log, config, db

COMPENSATION_WINDOW_S = 10 * 60
_SH = ZoneInfo("Asia/Shanghai")


def _now() -> float:
    """唯一时钟入口（epoch 秒）；测试 monkeypatch 做临界验证。"""
    return time.time()


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _next_wallclock(remind_at: str, repeat_rule: str) -> str | None:
    """daily/weekly 下次实例：Asia/Shanghai 本地墙钟加 1/7 天（无 DST 漂移）。"""
    if repeat_rule not in ("daily", "weekly"):
        return None
    local = datetime.fromisoformat(remind_at).astimezone(_SH)
    delta = timedelta(days=1 if repeat_rule == "daily" else 7)
    nxt = (local + delta).astimezone(_SH)
    return nxt.isoformat()


# ---------------------------------------------------------------------------
# 纯函数：到期选择
# ---------------------------------------------------------------------------

def select_due(now: float, rows: list) -> list:
    """输入提醒行快照，输出到期条目（含 next_remind_at 计算结果）。

    仅 status='pending' 且 remind_at <= now 命中；比较统一 UTC epoch。
    零 DB 零队列零副作用。
    """
    due = []
    for r in rows:
        if r["status"] != "pending":
            continue
        try:
            at = _epoch(r["remind_at"])
        except (ValueError, TypeError, KeyError):
            continue
        if at <= now:
            item = dict(r)
            item["next_remind_at"] = _next_wallclock(r["remind_at"], r["repeat_rule"])
            due.append(item)
    return due


# ---------------------------------------------------------------------------
# 引擎实例：构造期固定 writer 与通知队列（无模块级全局）
# ---------------------------------------------------------------------------

class ReminderEngine:
    __slots__ = ("_writer", "notify_queue")

    def __init__(self, *, writer, notify_queue):
        object.__setattr__(self, "_writer", writer)
        object.__setattr__(self, "notify_queue", notify_queue)

    def __setattr__(self, name, value):
        if name == "notify_queue":   # 测试注入故障队列允许
            object.__setattr__(self, name, value)
            return
        raise PermissionError("reminder_engine_immutable")

    # -- 事务处理 -----------------------------------------------------------

    def process_due(self) -> int:
        """快照 → select_due → 单事务置 notified + repeat 新行 → 投递。"""
        rows = [dict(r) for r in db.get_read_conn().execute(
            "SELECT * FROM schedules WHERE status='pending'").fetchall()]
        due = select_due(_now(), rows)
        if not due:
            return 0
        writer = object.__getattribute__(self, "_writer")
        now_iso = db.now_iso()
        with writer.transaction() as cur:
            for item in due:
                cur.execute(
                    "UPDATE schedules SET status='notified', notified_at=?, "
                    "updated_at=? WHERE id=? AND status='pending'",
                    (now_iso, now_iso, item["id"]))
                if cur.rowcount != 1:
                    raise RuntimeError("reminder_state_race")
                if item["next_remind_at"] is not None:
                    self._insert_next_instance(item, cur=cur, now_iso=now_iso)
        # COMMIT 成功后投递（先库后队；投递失败不回滚已提交的 notified）
        self._deliver_and_mark(due)
        return len(due)

    def _insert_next_instance(self, item, *, cur, now_iso: str) -> None:
        """repeat 新建一行：新 id、新 remind_at；原行保持 notified 不动。"""
        cur.execute(
            "INSERT INTO schedules(title,remind_at,repeat_rule,status,owner_role,"
            "visibility,version,created_at,updated_at) "
            "VALUES(?,?,?,'pending',?,?,1,?,?)",
            (item["title"], item["next_remind_at"], item["repeat_rule"],
             item["owner_role"], item["visibility"], now_iso, now_iso))

    # -- 统一投递原语（正常与补扫共用） -------------------------------------

    def _deliver_and_mark(self, items: list) -> dict:
        enqueued, dropped = [], []
        for item in items:
            note = {"schedule_id": item["id"], "title": item["title"],
                    "remind_at": item["remind_at"], "missed": item.get("missed", False)}
            try:
                self._enqueue(note)
                enqueued.append(item["id"])
            except Exception:
                audit_log.log("ERROR", "reminder_enqueue_failed",
                              target_table="schedules", target_id=item["id"])
        if enqueued:
            try:
                self._mark_enqueued(enqueued)
            except Exception:
                audit_log.log("ERROR", "reminder_mark_failed",
                              target_table="schedules", result_code="mark_error")
        return {"enqueued": enqueued, "dropped": dropped}

    def _enqueue(self, note: dict) -> None:
        q = self.notify_queue
        try:
            q.put_nowait(note)
        except queue.Full:
            try:
                oldest = q.get_nowait()
                audit_log.log("WARN", "reminder_queue_overflow_dropped",
                              target_table="schedules",
                              target_id=oldest.get("schedule_id"))
            except queue.Empty:
                pass
            q.put_nowait(note)   # 腾位后仍失败 → 异常上抛（不写标记）

    def _mark_enqueued(self, ids: list) -> None:
        writer = object.__getattribute__(self, "_writer")
        now_iso = db.now_iso()
        with writer.transaction() as cur:
            for sid in ids:
                cur.execute(
                    "UPDATE schedules SET recovery_enqueued_at=? "
                    "WHERE id=? AND recovery_enqueued_at IS NULL",
                    (now_iso, sid))

    # -- 启动补扫 -----------------------------------------------------------

    def recovery_scan(self) -> int:
        """候选：notified + 窗口内 + 标记 NULL → 经同一原语补投（标 missed）。

        窗口比较统一 UTC epoch（ISO 字符串跨时区偏移不可字典序比较）。
        """
        cutoff = _now() - COMPENSATION_WINDOW_S
        rows = []
        for r in db.get_read_conn().execute(
                "SELECT * FROM schedules WHERE status='notified' "
                "AND recovery_enqueued_at IS NULL").fetchall():
            try:
                if r["notified_at"] is not None and _epoch(r["notified_at"]) >= cutoff:
                    rows.append(dict(r))
            except (ValueError, TypeError):
                continue
        if not rows:
            return 0
        for r in rows:
            r["missed"] = True
        self._deliver_and_mark(rows)
        return len(rows)

    # -- 线程壳 -------------------------------------------------------------

    def reminder_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                if stop_event.is_set():   # 落库前复查（幽灵写纪律）
                    break
                self.process_due()
            except Exception:
                audit_log.log("ERROR", "reminder_loop_error",
                              target_table="schedules", result_code="loop_exception")
            stop_event.wait(config.REMINDER_SCAN_INTERVAL_S)