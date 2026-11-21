"""schedule_tool（PRD §5.4/§8.3，M3.1 清单治理纪律）。

5 action：create / list / reschedule / cancel / mark_done。
状态机：pending → notified（提醒引擎）→ done；pending/notified → cancelled；
done/cancelled 终态。reschedule 仅 pending（已提醒/终态改期无意义）。
remind_at 必须 aware 且不早于当前时间；repeat 墙钟规则在 reminder 模块。
"""
from __future__ import annotations

import time
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from .. import audit_log, db, visibility
from ..cas import CASNotFound, CASVersionConflict


def _now() -> float:
    return time.time()


def _err(code: str, message: str = "") -> dict:
    return {"error": {"code": code, "message": message or code}}


def _aware_future(value: str) -> bool:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return dt.tzinfo is not None and dt.timestamp() >= _now()


_REPEATS = frozenset({"none", "daily", "weekly"})
# 状态机：当前态 → 允许目标态（业务层校验；CAS 管并发与权限）
_FLOW = {
    "pending": {"cancelled", "done"},
    "notified": {"done", "cancelled"},
    "done": set(),
    "cancelled": set(),
}


class CreateParams(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    remind_at: str
    repeat_rule: str = "none"
    visibility: str | None = None


class ListParams(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    status: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


class RescheduleParams(BaseModel):
    id: int
    remind_at: str
    version: int


class IdVersionParams(BaseModel):
    id: int
    version: int


class ScheduleTool:
    __slots__ = ("_cas", "_identity_api")

    def __init__(self, *, cas_service, identity_api):
        object.__setattr__(self, "_cas", cas_service)
        object.__setattr__(self, "_identity_api", identity_api)

    def __setattr__(self, *a):
        raise PermissionError("schedule_tool_immutable")

    def dispatch(self, action: str, *, _frame_caller: str, _llm_args: dict,
                 _trusted_ctx=None, **params) -> dict:
        handler = {
            "create": self.create, "list": self.list,
            "reschedule": self.reschedule, "cancel": self.cancel,
            "mark_done": self.mark_done,
        }.get(action)
        if handler is None:
            return _err("invalid_param", f"unknown_action:{action}")
        return handler(_frame_caller=_frame_caller, _llm_args=_llm_args,
                       _trusted_ctx=_trusted_ctx, **params)

    def _ident(self, frame_caller, llm_args, trusted_ctx):
        return self._identity_api.resolve_identity(frame_caller, llm_args, trusted_ctx)

    def _writer(self):
        return object.__getattribute__(self._cas, "_writer")

    # -- create -------------------------------------------------------------

    def create(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = CreateParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        if p.repeat_rule not in _REPEATS:
            return _err("invalid_param", "repeat_rule")
        if not _aware_future(p.remind_at):
            return _err("invalid_param", "remind_at_must_aware_future")
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        if role == "boss":
            return _err("invalid_param", "boss_owns_no_schedules")
        try:
            vis, scope, _src = visibility.resolve_visibility(
                role, p.title, kind="item", explicit=p.visibility)
            visibility.validate_persist(role, vis, scope)
        except ValueError as exc:
            return _err("invalid_param", str(exc))
        writer = self._writer()
        now = db.now_iso()
        with writer.transaction() as cur:
            cur.execute(
                "INSERT INTO schedules(title,remind_at,repeat_rule,status,owner_role,"
                "visibility,version,created_at,updated_at) "
                "VALUES(?,?,?,'pending',?,?,1,?,?)",
                (p.title, p.remind_at, p.repeat_rule, role, vis, now, now))
            sid = cur.lastrowid
        audit_log.log("INFO", "schedule_created", actor_role=role,
                      target_table="schedules", target_id=sid)
        return {"id": sid, "title": p.title, "status": "pending",
                "visibility": vis, "version": 1, "remind_at": p.remind_at}

    # -- list ---------------------------------------------------------------

    def list(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        unknown = set(params) - set(ListParams.model_fields)
        if unknown:
            return _err("invalid_param", f"unknown_filter:{sorted(unknown)[0]}")
        try:
            p = ListParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        filters = {"status": p.status} if p.status else {}
        try:
            rows = visibility.query_visible("schedules", role, **filters)
        except ValueError as exc:
            return _err("invalid_param", str(exc))
        # date_range 过滤（remind_at 统一 epoch 比较；白名单外字段不进 SQL）
        def _in_range(r):
            try:
                at = datetime.fromisoformat(r["remind_at"]).timestamp()
            except (ValueError, TypeError):
                return False
            if p.date_from is not None:
                try:
                    if at < datetime.fromisoformat(p.date_from).timestamp():
                        return False
                except (ValueError, TypeError):
                    return False
            if p.date_to is not None:
                try:
                    if at > datetime.fromisoformat(p.date_to).timestamp():
                        return False
                except (ValueError, TypeError):
                    return False
            return True
        rows = [r for r in rows if _in_range(r)]
        rows.sort(key=lambda r: r["remind_at"])
        total = len(rows)
        return {"items": [dict(r) for r in rows[:p.limit]], "total": total}

    # -- reschedule ---------------------------------------------------------

    def reschedule(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = RescheduleParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        if not _aware_future(p.remind_at):
            return _err("invalid_param", "remind_at_must_aware_future")
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        current = self._current(role, p.id)
        if current is None:
            return _err("not_found")
        if current["status"] != "pending":
            return _err("invalid_param", "only_pending_reschedulable")
        return self._cas_write(ident, p.id, p.version, remind_at=p.remind_at)

    # -- cancel / mark_done -------------------------------------------------

    def cancel(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        return self._transition("cancelled", _frame_caller=_frame_caller,
                                _llm_args=_llm_args, _trusted_ctx=_trusted_ctx, **params)

    def mark_done(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        return self._transition("done", _frame_caller=_frame_caller,
                                _llm_args=_llm_args, _trusted_ctx=_trusted_ctx, **params)

    def _transition(self, target, *, _frame_caller, _llm_args, _trusted_ctx=None, **params):
        try:
            p = IdVersionParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        current = self._current(role, p.id)
        if current is None:
            return _err("not_found")
        if target not in _FLOW[current["status"]]:
            return _err("invalid_param", "status_transition")
        return self._cas_write(ident, p.id, p.version, status=target)

    def _current(self, role, sid):
        rows = visibility.query_visible("schedules", role)
        return next((r for r in rows if r["id"] == sid), None)

    def _cas_write(self, ident, sid, version, **fields) -> dict:
        try:
            row = self._cas.cas_update("schedules", sid, version, ident=ident, **fields)
        except CASNotFound:
            return _err("not_found")
        except CASVersionConflict:
            return _err("version_conflict", "数据已变化，请刷新")
        except ValueError as exc:
            return _err("invalid_param", str(exc))
        return {k: row[k] for k in ("id", "title", "status", "visibility",
                                    "version", "remind_at")}

    @staticmethod
    def fc_schemas() -> dict:
        out = {}
        for name, model in (
                ("create", CreateParams), ("list", ListParams),
                ("reschedule", RescheduleParams), ("cancel", IdVersionParams),
                ("mark_done", IdVersionParams)):
            out[name] = {"parameters": model.model_json_schema()}
        return out