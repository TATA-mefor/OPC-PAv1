"""knowledge_tool Agent 面（M4 第二批）：身份、可见面、错误契约。"""
from __future__ import annotations

import pytest

from opc_workbench.tools.knowledge_tool import LLM_ACTIONS, KnowledgeTool


@pytest.fixture()
def ktool(kservice, identity_api):
    return KnowledgeTool(service=kservice, identity_api=identity_api)


def test_llm_visible_actions_exclude_destructive_and_reindex():
    assert set(LLM_ACTIONS) == {"import_doc", "search", "list_docs"}
    assert "delete_doc" not in LLM_ACTIONS
    assert "reindex" not in LLM_ACTIONS


def test_tool_is_immutable(ktool):
    with pytest.raises(PermissionError):
        ktool._service = None


def test_tool_exposes_no_writer_surface(ktool):
    assert not hasattr(ktool, "_writer")
    assert not hasattr(ktool, "writer")
    assert set(KnowledgeTool.__slots__) == {"_service", "_identity_api"}


def test_import_then_search_roundtrip(ktool, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "工具链路检索正文")
    out = ktool.import_doc(_frame_caller="sales", _llm_args={}, vault_path="sales/s.md")
    assert out["status"] == "ready" and out["vault_path"] == "sales/s.md"
    hits = ktool.search(_frame_caller="sales", _llm_args={}, query="工具链路检索正文")
    assert len(hits["hits"]) == 1
    assert hits["hits"][0]["vault_path"] == "sales/s.md"
    assert set(hits["hits"][0]) == {"content", "vault_path", "seq", "score"}


def test_import_rejects_unsafe_path(ktool):
    out = ktool.import_doc(_frame_caller="sales", _llm_args={}, vault_path="../evil.md")
    assert out["error"]["code"] == "vault_path_unsafe"


def test_import_missing_file_returns_not_found(ktool):
    out = ktool.import_doc(_frame_caller="sales", _llm_args={}, vault_path="sales/no.md")
    assert out["error"]["code"] == "not_found"


def test_llm_cannot_spoof_owner_role(ktool, isolated_env, write_md):
    """身份 spoof 字段被丢弃；归属仍由目录映射决定。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "归属 spoof 测试")
    out = ktool.import_doc(_frame_caller="cfo",
                           _llm_args={"owner_role": "boss", "caller_role": "boss"},
                           vault_path="cfo/a.md")
    assert out["status"] == "ready"
    docs = ktool.list_docs(_frame_caller="cfo", _llm_args={})["docs"]
    assert [d["owner_role"] for d in docs if d["vault_path"] == "cfo/a.md"] == ["cfo"]


def test_search_isolation_through_tool(ktool, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/p.md", "TOOL-SECRET-99", visibility="private")
    ktool.import_doc(_frame_caller="cfo", _llm_args={}, vault_path="cfo/p.md")
    out = ktool.search(_frame_caller="sales", _llm_args={}, query="TOOL-SECRET-99")
    assert out["hits"] == []
    boss_view = ktool.list_docs(_frame_caller="cfo", _llm_args={})["docs"]
    assert any(d["vault_path"] == "cfo/p.md" for d in boss_view)


def test_list_docs_invalid_param_rejected(ktool):
    out = ktool.list_docs(_frame_caller="sales", _llm_args={}, limit="x")
    assert out["error"]["code"] == "invalid_param"


def test_search_invalid_top_k_rejected(ktool):
    out = ktool.search(_frame_caller="sales", _llm_args={}, query="x", top_k=0)
    assert out["error"]["code"] == "invalid_param"


def test_delete_requires_trusted_context(ktool, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "需要确认才能删")
    ktool.import_doc(_frame_caller="cfo", _llm_args={}, vault_path="cfo/a.md")
    out = ktool.delete_doc(_frame_caller="cfo", _llm_args={}, vault_path="cfo/a.md")
    assert out["error"]["code"] == "confirmation_required"
    assert (root / "cfo" / "a.md").exists()


def test_delete_checks_role_not_just_hidden_entry(ktool, isolated_env, write_md,
                                                 boss_ctx):
    """UI 隐藏入口不是授权边界：有可信上下文但角色非 boss 仍必须拒绝。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "非 boss 不得删除")
    ktool.import_doc(_frame_caller="cfo", _llm_args={}, vault_path="cfo/a.md")
    out = ktool.delete_doc(_frame_caller="cfo", _llm_args={}, _trusted_ctx=boss_ctx,
                           vault_path="cfo/a.md")
    assert out["error"]["code"] == "forbidden_role"
    assert (root / "cfo" / "a.md").exists()


def test_delete_by_boss_through_tool(ktool, isolated_env, write_md, boss_ctx):
    """正路：UI 确认 + boss Identity（服务层登记校验通过）→ 删除生效。"""
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "boss 可删")
    ktool.import_doc(_frame_caller="cfo", _llm_args={}, vault_path="cfo/a.md")
    out = ktool.delete_doc(_frame_caller="boss", _llm_args={}, _trusted_ctx=boss_ctx,
                           vault_path="cfo/a.md")
    assert out == {"deleted": True}
    assert not (root / "cfo" / "a.md").exists()
    docs = ktool.list_docs(_frame_caller="cfo", _llm_args={})["docs"]
    assert all(d["vault_path"] != "cfo/a.md" for d in docs)


def test_reindex_entry_has_no_identity_params(ktool, isolated_env, write_md):
    root = isolated_env["vault_dir"]
    write_md(root, "cfo/a.md", "重建索引入口")
    out = ktool.reindex(vault_path="cfo/a.md")
    assert out["status"] == "ready" and out["chunk_count"] >= 1


def test_llm_unavailable_returns_error_not_exception(ktool, isolated_env, write_md):
    from opc_workbench import llm_client
    root = isolated_env["vault_dir"]
    write_md(root, "sales/s.md", "降级路径测试")
    isolated_env["fake_llm"].embed_responses = [llm_client.LLMFailure("circuit_open")]
    out = ktool.import_doc(_frame_caller="sales", _llm_args={}, vault_path="sales/s.md")
    assert out["status"] == "failed"
    assert "circuit_open" in out.get("status", "") or out["status"] == "failed"