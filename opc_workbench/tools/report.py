"""report_tool（PRD §5.5/§8.4，M3.1 清单 §三.5 v6 定稿）。

4 action：generate_daily / generate_weekly / list / get。
聚合身份：boss 缺省全角色聚合 owner='boss'；boss 显式指定角色 → 仅该角色；
员工禁指定他角色（invalid_param），只能生成本角色报告。
原子 UPSERT：ON CONFLICT(kind,period_key,owner_role) DO UPDATE，
version+1、updated_at=excluded.updated_at（v6）；并发恰好一行。
degraded 落 refs_json 结构化字段；报告恒 private（含降级模板，
resolve_visibility kind=report 强制）；零数据模板不调 LLM。
period_key：日=YYYY-MM-DD / 周=ISO YYYY-Www（Asia/Shanghai 日界）。
vault 双写留 M4（已确认）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from .. import audit_log, config, db, visibility
from ..llm_client import LLMFailure

_SH = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    """统一时钟入口（aware ISO）；测试 monkeypatch 做日界/刷新断言。"""
    return db.now_iso()


def _err(code: str, message: str = "") -> dict:
    return {"error": {"code": code, "message": message or code}}


class GenerateDailyParams(BaseModel):
    date: str | None = None
    owner_role: str | None = None
    visibility: str | None = None


class GenerateWeeklyParams(BaseModel):
    week: str | None = None
    owner_role: str | None = None
    visibility: str | None = None


class ListParams(BaseModel):
    kind: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


class GetParams(BaseModel):
    id: int


def _local_date(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(_SH).date().isoformat()


def _iso_week(iso: str) -> str:
    d = datetime.fromisoformat(iso).astimezone(_SH).date()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _week_range(week_key: str) -> tuple[str, str]:
    """ISO 周 → 本地日界 [周一, 下周一)（用于聚合窗口）。"""
    y, w = week_key.split("-W")
    monday = datetime.fromisocalendar(int(y), int(w), 1)
    start = monday.replace(tzinfo=_SH)
    end = start + timedelta(days=7)
    return start.isoformat(), end.isoformat()


def _day_range(date_key: str) -> tuple[str, str]:
    start = datetime.fromisoformat(date_key).replace(tzinfo=_SH)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


class ReportTool:
    __slots__ = ("_cas", "_identity_api", "_llm")

    def __init__(self, *, cas_service, identity_api, llm):
        object.__setattr__(self, "_cas", cas_service)
        object.__setattr__(self, "_identity_api", identity_api)
        object.__setattr__(self, "_llm", llm)

    def __setattr__(self, *a):
        raise PermissionError("report_tool_immutable")

    def dispatch(self, action: str, *, _frame_caller: str, _llm_args: dict,
                 _trusted_ctx=None, **params) -> dict:
        handler = {
            "generate_daily": self.generate_daily,
            "generate_weekly": self.generate_weekly,
            "list": self.list, "get": self.get,
        }.get(action)
        if handler is None:
            return _err("invalid_param", f"unknown_action:{action}")
        return handler(_frame_caller=_frame_caller, _llm_args=_llm_args,
                       _trusted_ctx=_trusted_ctx, **params)

    # M3.2① 复审：原 _writer() 公开方法已删除（会泄露原始 WriteCapability），
    # 改为使用点内联取用。

    # -- generate -----------------------------------------------------------

    def generate_daily(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = GenerateDailyParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        date_key = p.date or _local_date(_now_iso())
        try:
            datetime.fromisoformat(date_key)
        except ValueError:
            return _err("invalid_param", "date")
        return self._generate("daily_report", date_key, _day_range(date_key),
                              p.owner_role, p.visibility,
                              _frame_caller, _llm_args, _trusted_ctx)

    def generate_weekly(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = GenerateWeeklyParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        week_key = p.week or _iso_week(_now_iso())
        try:
            start, end = _week_range(week_key)
        except (ValueError, AttributeError):
            return _err("invalid_param", "week")
        return self._generate("weekly_report", week_key, (start, end),
                              p.owner_role, p.visibility,
                              _frame_caller, _llm_args, _trusted_ctx)

    def _generate(self, kind, period_key, window, owner_role_param, vis_param,
                  frame_caller, llm_args, trusted_ctx) -> dict:
        ident = self._identity_api.resolve_identity(frame_caller, llm_args, trusted_ctx)
        caller = self._identity_api.authorize(ident)
        # 聚合身份规则
        if caller == "boss":
            owner = owner_role_param or "boss"
            if owner != "boss" and owner not in config.ROLES:
                return _err("invalid_param", "owner_role")
            agg_roles = [owner] if owner != "boss" else list(config.ROLE_ORDER)
        else:
            if owner_role_param is not None and owner_role_param != caller:
                return _err("invalid_param", "owner_role_forbidden")
            owner = caller
            agg_roles = [caller]
        # 报告恒 private（清单 §三.5：含降级模板；explicit public 强制回落）。
        # 注：M2 resolve_visibility 的 explicit 分支返回原值（source 标记
        # forced 但值未改），与清单"强制 private"表述不一致——M2 函数零改动，
        # 工具层按清单口径兜底强制（差异已记录，供复审裁决）。
        if vis_param is not None and vis_param not in ("public", "private"):
            return _err("invalid_param", "visibility")
        vis, scope, _src = visibility.resolve_visibility(
            owner if owner != "boss" else "coordinator", "", kind="report")
        vis = "private"   # 报告恒 private，任何路径不开放 public
        stats = self._aggregate(agg_roles, window)
        degraded = False
        if stats["tasks_done"] == 0 and stats["tasks_new"] == 0 and stats["schedules_done"] == 0:
            content = f"# {period_key} 报告\n\n本期无记录。\n"
        else:
            content, degraded = self._compose(period_key, stats, owner)
        refs = {"degraded": degraded, "stats": stats,
                "agg_roles": agg_roles, "window": list(window)}
        artifact_id = self._upsert(kind, period_key, owner, content, refs, vis)
        audit_log.log("INFO", "report_generated", actor_role=caller,
                      target_table="artifacts", target_id=artifact_id,
                      result_code="degraded" if degraded else "ok")
        return {"artifact_id": artifact_id, "title": f"{period_key} 报告",
                "content_md": content, "degraded": degraded}

    def _aggregate(self, roles: list, window: tuple) -> dict:
        """期内统计：done/新增任务 + 日程完成度（读连接；聚合含 private——
        boss 全可见语义；员工仅本角色，query_visible 同口径）。"""
        start, end = window
        s = datetime.fromisoformat(start).timestamp()
        e = datetime.fromisoformat(end).timestamp()
        conn = db.get_read_conn()
        tasks_done = tasks_new = sched_done = sched_total = 0
        placeholders = ",".join("?" for _ in roles)
        for r in conn.execute(
                f"SELECT status,created_at,updated_at FROM tasks "
                f"WHERE owner_role IN ({placeholders})", roles).fetchall():
            try:
                created = datetime.fromisoformat(r["created_at"]).timestamp()
                updated = datetime.fromisoformat(r["updated_at"]).timestamp()
            except (ValueError, TypeError):
                continue
            if r["status"] == "done" and s <= updated < e:
                tasks_done += 1
            if s <= created < e:
                tasks_new += 1
        for r in conn.execute(
                f"SELECT status,remind_at FROM schedules "
                f"WHERE owner_role IN ({placeholders})", roles).fetchall():
            try:
                at = datetime.fromisoformat(r["remind_at"]).timestamp()
            except (ValueError, TypeError):
                continue
            if s <= at < e:
                sched_total += 1
                if r["status"] in ("done", "notified"):
                    sched_done += 1
        return {"tasks_done": tasks_done, "tasks_new": tasks_new,
                "schedules_done": sched_done, "schedules_total": sched_total}

    def _compose(self, period_key: str, stats: dict, owner: str) -> tuple[str, bool]:
        stats_md = (f"| 指标 | 数值 |\n|---|---|\n"
                    f"| 完成任务 | {stats['tasks_done']} |\n"
                    f"| 新增任务 | {stats['tasks_new']} |\n"
                    f"| 日程完成 | {stats['schedules_done']}/{stats['schedules_total']} |\n")
        prompt = (f"以下是 {owner} 在 {period_key} 的工作统计，请生成简洁 Markdown 报告点评：\n"
                  f"{stats_md}")
        try:
            commentary = self._llm.chat([{"role": "user", "content": prompt}])
            return f"# {period_key} 报告\n\n{stats_md}\n## AI 点评\n\n{commentary}\n", False
        except LLMFailure:
            return (f"# {period_key} 报告\n\n{stats_md}\n"
                    f"> AI 点评不可用（降级为纯统计模板）\n"), True

    def _upsert(self, kind, period_key, owner, content, refs, vis) -> int:
        writer = object.__getattribute__(object.__getattribute__(self, "_cas"), "_writer")
        now = _now_iso()
        title = f"{period_key} 报告"
        with writer.transaction() as cur:
            cur.execute(
                "INSERT INTO artifacts(kind,title,content_md,refs_json,owner_role,"
                "visibility,period_key,created_at,version,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,?) "
                "ON CONFLICT(kind,period_key,owner_role) DO UPDATE SET "
                "title=excluded.title, content_md=excluded.content_md, "
                "refs_json=excluded.refs_json, version=artifacts.version+1, "
                "updated_at=excluded.updated_at",
                (kind, title, content, json.dumps(refs, ensure_ascii=False),
                 owner, vis, period_key, now, now))
            row = cur.execute(
                "SELECT id FROM artifacts WHERE kind=? AND period_key=? AND owner_role=?",
                (kind, period_key, owner)).fetchone()
        return row["id"]

    # -- list / get ---------------------------------------------------------

    def list(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = ListParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        ident = self._identity_api.resolve_identity(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        filters = {"kind": p.kind} if p.kind else {}
        try:
            rows = visibility.query_visible("artifacts", role, **filters)
        except ValueError as exc:
            return _err("invalid_param", str(exc))
        rows.sort(key=lambda r: r["id"], reverse=True)
        items = [{"id": r["id"], "kind": r["kind"], "title": r["title"],
                  "period_key": r["period_key"], "owner_role": r["owner_role"],
                  "version": r["version"], "updated_at": r["updated_at"],
                  "degraded": json.loads(r["refs_json"]).get("degraded", False)}
                 for r in rows[:p.limit]]
        return {"items": items, "total": len(rows)}

    def get(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = GetParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        ident = self._identity_api.resolve_identity(_frame_caller, _llm_args, _trusted_ctx)
        role = self._identity_api.authorize(ident)
        rows = visibility.query_visible("artifacts", role)
        row = next((r for r in rows if r["id"] == p.id), None)
        if row is None:
            return _err("not_found")
        return {"id": row["id"], "kind": row["kind"], "title": row["title"],
                "content_md": row["content_md"], "period_key": row["period_key"],
                "owner_role": row["owner_role"], "version": row["version"],
                "refs": json.loads(row["refs_json"])}

    @staticmethod
    def fc_schemas() -> dict:
        out = {}
        for name, model in (
                ("generate_daily", GenerateDailyParams),
                ("generate_weekly", GenerateWeeklyParams),
                ("list", ListParams), ("get", GetParams)):
            out[name] = {"parameters": model.model_json_schema()}
        return out