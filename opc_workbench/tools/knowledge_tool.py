"""knowledge_tool（PRD §5.3/§8.2，M4 第二批）。

5 action：import_doc / search / list_docs / delete_doc / reindex。
**LLM 可见面只有前三个**（LLM_ACTIONS）：
- reindex 由 vault_watcher 触发，PRD 明确不暴露给 LLM；
- delete_doc 必须 UI 确认后调用，需可信上下文，同样不进 FC 注册表。

统一纪律（与 M3 工具同款）：
- 身份经 identity_api.resolve_identity（fail closed，LLM 伪造字段丢弃）；
- 检索/清单经 KnowledgeService → visibility.query_visible 治理闸门；
- owner_role **只由目录映射决定**，LLM 传入的 owner_role 一律不采信；
- 错误统一 {"error":{"code","message"}}；审计只落元数据，无正文。

本类不持有也不暴露 WriteCapability：writer 只注入给 KnowledgeService。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import audit_log, llm_client
from ..knowledge_service import DeleteDenied, KnowledgeService

LLM_ACTIONS: tuple[str, ...] = ("import_doc", "search", "list_docs")


def _err(code: str, message: str = "") -> dict:
    return {"error": {"code": code, "message": message or code}}


class ImportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")   # 未知参数一律拒绝，不静默忽略
    vault_path: str = Field(min_length=1, max_length=500)


class SearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


class ListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeTool:
    __slots__ = ("_service", "_identity_api")

    def __init__(self, *, service: KnowledgeService, identity_api):
        object.__setattr__(self, "_service", service)
        object.__setattr__(self, "_identity_api", identity_api)

    def __setattr__(self, *a):
        raise PermissionError("knowledge_tool_immutable")

    def _caller(self, frame_caller, llm_args, trusted_ctx):
        ident = self._identity_api.resolve_identity(frame_caller, llm_args, trusted_ctx)
        return self._identity_api.authorize(ident)

    # -- LLM 可见 -----------------------------------------------------------

    def import_doc(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = ImportParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        caller = self._caller(_frame_caller, _llm_args, _trusted_ctx)
        try:
            # 归属由目录映射决定；LLM 传入的 owner_role 不在 schema 中，天然被丢弃
            outcome = self._service.ingest(p.vault_path)
        except ValueError as exc:
            return _err(str(exc).split(":")[0])
        except FileNotFoundError:
            return _err("not_found")
        except llm_client.LLMFailure as exc:
            return _err("llm_unavailable", str(exc))
        audit_log.log("INFO", "knowledge_import", actor_role=caller,
                      target_table="knowledge_docs", target_id=outcome["doc_id"],
                      result_code=outcome["status"], source=p.vault_path)
        return {"doc_id": outcome["doc_id"], "status": outcome["status"],
                "vault_path": p.vault_path,
                "owner_corrected": outcome.get("owner_corrected", False)}

    def search(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            p = SearchParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        caller = self._caller(_frame_caller, _llm_args, _trusted_ctx)
        try:
            hits = self._service.search(p.query, caller, top_k=p.top_k)
        except llm_client.LLMFailure as exc:
            return _err("llm_unavailable", str(exc))
        return {"hits": [{"content": h["content"], "vault_path": h["vault_path"],
                          "seq": h["seq"], "score": h["score"]} for h in hits]}

    def list_docs(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        try:
            ListParams(**params)
        except ValidationError as exc:
            return _err("invalid_param", str(exc.errors()[0]["msg"]))
        caller = self._caller(_frame_caller, _llm_args, _trusted_ctx)
        return {"docs": self._service.list_docs(caller)}

    # -- FC schema（M5-a 消费；身份字段不在其中） -----------------------------

    @staticmethod
    def fc_schemas() -> dict:
        """只导出 LLM 可见的 3 个 action；delete_doc / reindex 不出现在 FC 面。"""
        out = {}
        for name, model in (("import_doc", ImportParams),
                            ("search", SearchParams),
                            ("list_docs", ListParams)):
            out[name] = {"parameters": model.model_json_schema()}
        return out

    # -- 非 LLM 面（UI 确认 / watcher） ---------------------------------------

    def delete_doc(self, *, _frame_caller, _llm_args, _trusted_ctx=None, **params) -> dict:
        """需 UI 确认的可信上下文；真正的授权判定在 KnowledgeService.delete 内。

        **UI 隐藏入口不是授权边界**：本层只把框架签发的 Identity 实例下传，
        不在本层用 role 字符串放行；服务层要求已登记的 Identity 且仅 boss。
        """
        if _trusted_ctx is None:
            return _err("confirmation_required")
        rel = params.get("vault_path")
        if not isinstance(rel, str) or not rel:
            return _err("invalid_param", "vault_path")
        ident = self._identity_api.resolve_identity(_frame_caller, _llm_args, _trusted_ctx)
        try:
            outcome = self._service.delete(rel, identity=ident)
        except DeleteDenied as exc:
            return _err(exc.code)
        except ValueError as exc:
            return _err(str(exc).split(":")[0])
        audit_log.log("INFO", "knowledge_delete", actor_role=outcome["role"],
                      target_table="knowledge_docs", target_id=outcome["doc_id"],
                      result_code="deleted", source=rel)
        return {"deleted": True}

    def reindex(self, *, vault_path: str) -> dict:
        """watcher 专用入口，无身份参数、不进 FC 注册表。"""
        try:
            outcome = self._service.ingest(vault_path)
        except ValueError as exc:
            return _err(str(exc).split(":")[0])
        except FileNotFoundError:
            return _err("not_found")
        return {"doc_id": outcome["doc_id"], "status": outcome["status"],
                "chunk_count": outcome.get("chunk_count", 0)}