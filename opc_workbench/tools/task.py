"""task_tool（PRD §5.2/§8.1，M3.1 清单 §三.3 治理纪律）。

6 action：create / list / update_status / update / delete / bulk_delete。
统一纪律：
- 身份：identity_api.resolve_identity（fail closed；LLM 伪造字段丢弃）；
- 写入：resolve_visibility → validate_persist → 经注入 writer INSERT；
- 查询：query_visible 唯一入口（表/列白名单 + 可见性过滤）；
- 更新/删除：CASService（write_where 纳入最终 WHERE + 乐观锁）；
- 审计：元数据（id/操作/角色），无内容原文。

错误统一 {"error": {"code", "message"}}；not_found 同构（含被过滤条目）。
身份字段不在 FC schema；治理函数不进注册表。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from .. import audit_log, bulk_delete, db, visibility
from ..cas import CASNotFound, CASVersionConflict


def _err(code: str, message: str = "") -> dict:
    return {"error": {"code": code, "message": message or code}}


# ---------------------------------------------------------------------------
# pydantic 参数模型（M5 FC schema 来源；身份字段不在其中）
# ---------------------------------------------------------------------------

class CreateParams(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    detail: str = ""
    due_at: str | None = None
    priority: str = "mid"
    visibility: str | None = None
    idempotency_key: str | None = None


class ListParams(BaseModel):
    # owner_role 过滤不开放给 LLM（与身份 spoof 字段同名，防伪造面）；
    # boss UI 按角色查看走 M6 治理接口，不经 FC schema。
    status: str | None = None
    visibility: str | None = None
    priority: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


class UpdateStatusParams(BaseModel):
    id: int
    status: str
    version: int


class UpdateParams(BaseModel):
    id: int
    version: int
    title: str | None = None
    detail: str | None = None
    status: str | None = None
    priority: str | None = None
    due_at: str | None = None
    visibility: str | None = None


class DeleteParams(BaseModel):
    id: int
    version: int


class BulkDeleteParams(BaseModel):
    ids: list[int] = Field(min_length=1)


_STATUS_FLOW = {
    "todo": {"doing", "cancelled"},
    "doing": {"done", "cancelled"},
    "done": {"cancelled"},
    "cancelled": set(),
}
_PRIORITIES = frozenset({"low", "mid", "high"})


def _aware_iso(value: str) -> bool:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return dt.tzinfo is not None


class TaskTool:
    """任务工具实例：构造期固定注入依赖，无模块级全局。"""

    __slots__ = ("_cas", "_identity_api", "_bulk_ctx")

    def __init__(self, *, cas_service, identity_api):
        object.__setattr__(self, "_cas", cas_service)
        object.__setattr__(self, "_identity_api", identity_api)
        object.__setattr__(self, "_bulk_ctx", bulk_delete.BulkDeleteContext(
            identity_api=identity_api, cas_service=cas_service))

    def __setattr__(self, *a):
        raise PermissionError("task_tool_immutable")

    # -- 分发入口 -----------------------------------------------------------

    def dispatch(self, action: str, *, _frame_caller: str, _llm_args: dict,
                 _trusted_ctx=None, **params) -> dict:
        handler = {
            "create": self.create, "list": self.list,
            "update_status": self.update_status, "update": self.update,
            "delete": self.delete, "bulk_delete": self.bulk_delete,
        }.get(action)
        if handler is None:
            return _err("invalid_param", f"unknown_action:{action}")
        return handler(_frame_caller=_frame_caller, _llm_args=_llm_args,
                       _trusted_ctx=_trusted_ctx, **params)

    def _ident(self, frame_caller, llm_args, trusted_ctx):
        return self._identity_api.resolve_identity(frame_caller, llm_args, trusted_ctx)

    # M3.2① 复审：原 _writer() 是公开方法，任何拿到工具实例的调用方都能
    # 取出原始 WriteCapability 绕过 CAS。已删除，改为使用点内联取用。

    # -- create -------------------------------------------------------------

    def create(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = CreateParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        if p.priority not in _PRIORITIES:
            return _err("invalid_param", "priority")
        if p.due_at is not None and not _aware_iso(p.due_at):
            return _err("invalid_param", "due_at_requires_timezone")
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        if role == "boss":
            return _err("invalid_param", "boss_owns_no_tasks")  # boss 不是 Agent
        try:
            vis, scope, _src = visibility.resolve_visibility(
                role, p.title + " " + p.detail, kind="item", explicit=p.visibility)
            visibility.validate_persist(role, vis, scope)
        except ValueError as exc:
            return _err("invalid_param", str(exc))
        writer = object.__getattribute__(object.__getattribute__(self, "_cas"), "_writer")
        now = db.now_iso()
        with writer.transaction() as cur:
            if p.idempotency_key is not None:
                exist = cur.execute(
                    "SELECT id,title,status,visibility,version FROM tasks "
                    "WHERE idempotency_key=?", (p.idempotency_key,)).fetchone()
                if exist is not None:
                    return dict(exist)
            cur.execute(
                "INSERT INTO tasks(title,detail,status,priority,due_at,owner_role,"
                "visibility,scope,version,created_at,updated_at,idempotency_key) "
                "VALUES(?,?,?,?,?,?,?,?,1,?,?,?)",
                (p.title, p.detail, "todo", p.priority, p.due_at, role,
                 vis, scope, now, now, p.idempotency_key))
            tid = cur.lastrowid
        audit_log.log("INFO", "task_created", actor_role=role,
                      target_table="tasks", target_id=tid)
        return {"id": tid, "title": p.title, "status": "todo",
                "visibility": vis, "version": 1}

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
        filters = {k: v for k, v in
                   (("status", p.status),
                    ("visibility", p.visibility), ("priority", p.priority))
                   if v is not None}
        try:
            rows = visibility.query_visible("tasks", role, **filters)
        except ValueError as exc:
            return _err("invalid_param", str(exc))
        rows.sort(key=lambda r: r["id"], reverse=True)
        total = len(rows)
        items = [dict(r) for r in rows[:p.limit]]
        return {"items": items, "total": total}

    # -- update_status ------------------------------------------------------

    def update_status(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = UpdateStatusParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        if p.status not in _STATUS_FLOW:
            return _err("invalid_param", "status")
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        # 业务层先校验流转：读当前状态经可见性（他角色 private → not_found 同构）
        rows = visibility.query_visible("tasks", role)
        current = next((r for r in rows if r["id"] == p.id), None)
        if current is None:
            return _err("not_found")
        if p.status not in _STATUS_FLOW[current["status"]]:
            return _err("invalid_param", "status_transition")
        return self._cas_write(ident, p.id, p.version, status=p.status)

    # -- update -------------------------------------------------------------

    def update(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = UpdateParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        fields = {k: v for k, v in
                  (("title", p.title), ("detail", p.detail), ("status", p.status),
                   ("priority", p.priority), ("due_at", p.due_at),
                   ("visibility", p.visibility)) if v is not None}
        if not fields:
            return _err("invalid_param", "empty_update")
        if "priority" in fields and fields["priority"] not in _PRIORITIES:
            return _err("invalid_param", "priority")
        if "due_at" in fields and not _aware_iso(fields["due_at"]):
            return _err("invalid_param", "due_at_requires_timezone")
        if "visibility" in fields and fields["visibility"] not in ("public", "private"):
            return _err("invalid_param", "visibility")
        if "status" in fields and fields["status"] not in _STATUS_FLOW:
            return _err("invalid_param", "status")
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        if "status" in fields:
            role = self._identity_api.authorize(ident)
            rows = visibility.query_visible("tasks", role)
            current = next((r for r in rows if r["id"] == p.id), None)
            if current is None:
                return _err("not_found")
            if fields["status"] not in _STATUS_FLOW[current["status"]]:
                return _err("invalid_param", "status_transition")
        return self._cas_write(ident, p.id, p.version, **fields)

    def _cas_write(self, ident, tid, version, **fields) -> dict:
        try:
            row = self._cas.cas_update("tasks", tid, version, ident=ident, **fields)
        except CASNotFound:
            return _err("not_found")
        except CASVersionConflict:
            return _err("version_conflict", "数据已变化，请刷新")
        except ValueError as exc:
            return _err("invalid_param", str(exc))
        return {k: row[k] for k in ("id", "title", "status", "visibility", "version")}

    # -- delete -------------------------------------------------------------

    def delete(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = DeleteParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        ident = self._ident(_frame_caller, _llm_args, _trusted_ctx)
        try:
            return self._cas.cas_delete("tasks", p.id, p.version, ident=ident)
        except CASNotFound:
            return _err("not_found")
        except CASVersionConflict:
            return _err("version_conflict", "数据已变化，请刷新")

    # -- bulk_delete --------------------------------------------------------

    def bulk_delete(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = BulkDeleteParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        result = bulk_delete.bulk_delete_action(
            "tasks", p.ids, _frame_caller=_frame_caller, _llm_args=_llm_args,
            _trusted_ctx=_trusted_ctx, _ctx=self._bulk_ctx)
        if isinstance(result, int):
            return {"approval_id": result}
        return result

    # -- FC schema（M5 消费；身份字段不在其中） ------------------------------

    @staticmethod
    def fc_schemas() -> dict:
        out = {}
        for name, model in (
                ("create", CreateParams), ("list", ListParams),
                ("update_status", UpdateStatusParams), ("update", UpdateParams),
                ("delete", DeleteParams), ("bulk_delete", BulkDeleteParams)):
            out[name] = {"parameters": model.model_json_schema()}
        return out