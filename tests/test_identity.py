"""身份链专项（M3.1 清单 v5–v10.1：身份通道/可信根/确认事件/冻结快照/签发登记）。

验收口径（用户）：真实调用链——身份 → 授权 → 工具入口 → 写能力 → 事务。
本文件覆盖身份与授权段；CAS 写入段在 test_cas.py。

测试组合根 = conftest 的 identity_api/boss_ctx fixture（与 app.main 同构装配，
不修改生产模块全局状态）。
"""
from __future__ import annotations

import threading

import pytest

from opc_workbench import config, identity


# ---------------------------------------------------------------------------
# 构造门禁：直构一律 PermissionError
# ---------------------------------------------------------------------------

def test_direct_construction_all_forbidden():
    for cls in (identity.SessionAuthority, identity.TrustedContext,
                identity.Identity, identity.AuthEvent, identity.SessionProof,
                identity.DeleteToken):
        with pytest.raises(PermissionError):
            cls()


def test_trusted_context_immutable(boss_ctx):
    with pytest.raises(PermissionError):
        boss_ctx._caller = "sales"
    with pytest.raises(PermissionError):
        boss_ctx._token = "fake"


def test_identity_handle_immutable(identity_api):
    ident = identity_api.resolve_identity("sales", {})
    with pytest.raises(PermissionError):
        ident._ident_id = "forged"


def test_no_public_assembly_entry():
    """v10.1：identity 无公开装配入口（只有内部 _build_identity）。"""
    assert not hasattr(identity, "assemble_identity")
    assert not hasattr(identity, "build_identity")
    assert callable(identity._build_identity)


# ---------------------------------------------------------------------------
# 会话凭证：登记来源 / HMAC / 一次性消费 / 职责分离
# ---------------------------------------------------------------------------

def test_register_requires_valid_auth_event(identity_api, auth_backend):
    """无认证事件/伪造事件 → 登记拒绝 → 签不出 proof（整条链断裂）。"""
    with pytest.raises(PermissionError):
        identity_api.register_session(None)
    forged = object.__new__(identity.AuthEvent)
    object.__setattr__(forged, "_event_id", "made-up")
    object.__setattr__(forged, "_session_id", "sess-x")
    with pytest.raises(PermissionError):
        identity_api.register_session(forged)
    with pytest.raises(PermissionError):
        identity_api.issue_proof("sess-x")


def test_authenticate_none_blocks_chain(identity_api, auth_backend):
    auth_backend.authenticate_enabled = False
    assert auth_backend.authenticate("s") is None
    with pytest.raises(PermissionError):
        identity_api.issue_proof("s")


def test_proof_hmac_tamper_rejected(identity_api, auth_backend):
    ev = auth_backend.authenticate("s1")
    identity_api.register_session(ev)
    proof = identity_api.issue_proof("s1")
    forged = object.__new__(identity.SessionProof)
    object.__setattr__(forged, "_session_id", proof._session_id)
    object.__setattr__(forged, "_nonce", proof._nonce)
    object.__setattr__(forged, "_issued_at", proof._issued_at)
    object.__setattr__(forged, "_mac", b"\x00" * 32)
    with pytest.raises(PermissionError):
        identity_api.mint_boss_context(forged)


def test_proof_replay_rejected(identity_api, auth_backend):
    ev = auth_backend.authenticate("s1")
    identity_api.register_session(ev)
    proof = identity_api.issue_proof("s1")
    ctx = identity_api.mint_boss_context(proof)
    assert ctx._caller == "boss"
    with pytest.raises(PermissionError):
        identity_api.mint_boss_context(proof)   # 一次性消费，重放拒绝


def test_resolve_repeated_after_mint(identity_api, auth_backend, boss_ctx):
    """v8 职责分离：铸造消费 nonce 后，同一会话仍可反复解析成功。"""
    for _ in range(3):
        ident = identity_api.resolve_identity("boss", {}, boss_ctx)
        assert identity_api.authorize(ident) == "boss"


def test_session_revoke_invalidates_ctx_immediately(identity_api, auth_backend, boss_ctx):
    ident = identity_api.resolve_identity("boss", {}, boss_ctx)
    identity_api.revoke_session("sess-boss-1")
    assert not identity_api.is_session_active("sess-boss-1")
    with pytest.raises(PermissionError):
        identity_api.resolve_identity("boss", {}, boss_ctx)
    with pytest.raises(PermissionError):
        identity_api.authorize(ident)   # 已签发身份级联失效


def test_session_ttl_expiry(identity_api, auth_backend, monkeypatch):
    t0 = 1_000_000.0
    monkeypatch.setattr(identity, "_now", lambda: t0)
    ev = auth_backend.authenticate("s-ttl")
    identity_api.register_session(ev)
    proof = identity_api.issue_proof("s-ttl")
    ctx = identity_api.mint_boss_context(proof)
    monkeypatch.setattr(identity, "_now", lambda: t0 + identity.SESSION_TTL_S + 1)
    assert not identity_api.is_session_active("s-ttl")
    with pytest.raises(PermissionError):
        identity_api.resolve_identity("boss", {}, ctx)


def test_cross_authority_proof_rejected(auth_backend):
    """跨实例混用：A 签发的 proof 到 B 铸造 → HMAC 校验失败。"""
    api_a = identity._build_identity(auth_backend)
    api_b = identity._build_identity(auth_backend)
    ev = auth_backend.authenticate("s-cross")
    api_a.register_session(ev)
    proof = api_a.issue_proof("s-cross")
    with pytest.raises(PermissionError):
        api_b.mint_boss_context(proof)


# ---------------------------------------------------------------------------
# 双通道解析：Agent 沿用 M2 语义 / boss 必须可信上下文
# ---------------------------------------------------------------------------

def test_agent_channel_resolves_and_registers(identity_api):
    for role in config.ROLE_ORDER:
        ident = identity_api.resolve_identity(role, {})
        assert identity_api.authorize(ident) == role


def test_boss_without_ctx_rejected(identity_api):
    with pytest.raises(PermissionError):
        identity_api.resolve_identity("boss", {})
    with pytest.raises(PermissionError):
        identity_api.resolve_identity("boss", {"caller_role": "boss"})


def test_forged_ctx_dict_rejected(identity_api):
    fake = {"_caller": "boss", "_minted": True, "_session_id": "s"}
    with pytest.raises(PermissionError):
        identity_api.resolve_identity("boss", {}, fake)


def test_llm_spoof_fields_dropped(identity_api, boss_ctx):
    """LLM 参数伪造身份字段 → 丢弃，按真实通道解析（M2 语义不变）。"""
    ident = identity_api.resolve_identity(
        "sales", {"caller_role": "boss", "actor": "boss", "owner_role": "cfo"})
    assert identity_api.authorize(ident) == "sales"
    ident2 = identity_api.resolve_identity("boss", {"caller_role": "sales"}, boss_ctx)
    assert identity_api.authorize(ident2) == "boss"


def test_invalid_frame_caller_fail_closed(identity_api):
    for bad in ("ghost", "", None):
        with pytest.raises(ValueError):
            identity_api.resolve_identity(bad, {})


def test_agent_channel_cannot_claim_boss(identity_api, boss_ctx):
    """执行器只注入角色档案身份：Agent frame_caller 配 boss ctx 仍解析为 Agent。"""
    ident = identity_api.resolve_identity("sales", {}, boss_ctx)
    assert identity_api.authorize(ident) == "sales"


# ---------------------------------------------------------------------------
# Identity 签发登记（v10 核心）：对象形状不是信任凭据
# ---------------------------------------------------------------------------

def test_forged_identity_object_rejected(identity_api):
    """object.__new__ + __setattr__ 造真实例填 ident_id → 登记未命中拒绝。"""
    fake = object.__new__(identity.Identity)
    object.__setattr__(fake, "_ident_id", "not-registered")
    with pytest.raises(PermissionError):
        identity_api.authorize(fake)


def test_forged_identity_with_existing_id_cross_authority_rejected(identity_api, auth_backend):
    """伪造已存在 ident_id 但跨 authority → 拒绝（登记归属校验）。"""
    ident = identity_api.resolve_identity("sales", {})
    other_api = identity._build_identity(auth_backend)
    fake = object.__new__(identity.Identity)
    object.__setattr__(fake, "_ident_id", ident._ident_id)
    with pytest.raises(PermissionError):
        other_api.authorize(fake)


def test_identity_ttl_expiry(identity_api, monkeypatch):
    t0 = 2_000_000.0
    monkeypatch.setattr(identity, "_now", lambda: t0)
    ident = identity_api.resolve_identity("sales", {})
    monkeypatch.setattr(identity, "_now", lambda: t0 + identity.IDENTITY_TTL_S + 1)
    with pytest.raises(PermissionError):
        identity_api.authorize(ident)


def test_identity_revoke(identity_api):
    ident = identity_api.resolve_identity("sales", {})
    identity_api.revoke_identity(ident)
    with pytest.raises(PermissionError):
        identity_api.authorize(ident)


def test_role_taken_from_registry_not_object(identity_api):
    """篡改对象字段不影响判定：role 取自登记（Identity 句柄本无 role 字段）。"""
    ident = identity_api.resolve_identity("cfo", {})
    # 句柄只有 _ident_id；即便注入伪造 role 属性也不改变授权结果
    with pytest.raises(PermissionError):
        ident.role = "boss"
    assert identity_api.authorize(ident) == "cfo"


def test_authorize_non_identity_rejected(identity_api):
    for bad in ("boss", {"role": "boss"}, None, 42):
        with pytest.raises(PermissionError):
            identity_api.authorize(bad)


# ---------------------------------------------------------------------------
# 确认事件 + 冻结快照 + 令牌传递链（v8/v9）
# ---------------------------------------------------------------------------

def _seed_tasks(conn, n=6):
    ids, versions = [], {}
    with conn:
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO tasks(title,owner_role,created_at,updated_at) "
                "VALUES(?,?,?,?)", (f"t{i}", "sales", "2026-11-21T00:00:00+00:00",
                                   "2026-11-21T00:00:00+00:00"))
            ids.append(cur.lastrowid)
            versions[cur.lastrowid] = 1
    return ids, versions


def test_confirm_mint_requires_confirmation(identity_api, boss_ctx, db_conn):
    """v9：有效 ctx₀ 但无确认事件 → confirmation_required 拒绝。"""
    ids, versions = _seed_tasks(db_conn)
    with pytest.raises(PermissionError, match="confirmation_required"):
        identity_api.confirm_mint("tasks", ids, versions,
                                  _ctx=boss_ctx, _confirmation_request=None)


def test_confirm_mint_forged_request_rejected(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")   # 默认 handler 校验 UI 标记
    forged = {"is_ui_confirmation": True, "session_id": "sess-boss-1"}  # 字典非请求对象
    with pytest.raises(PermissionError, match="confirmation_required"):
        identity_api.confirm_mint("tasks", ids, versions,
                                  _ctx=boss_ctx, _confirmation_request=forged)


def test_confirm_mint_wrong_session_rejected(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    req = ui_confirmation_request_cls("sess-other")   # 确认事件绑定他会话
    with pytest.raises(PermissionError, match="confirmation_required"):
        identity_api.confirm_mint("tasks", ids, versions,
                                  _ctx=boss_ctx, _confirmation_request=req)


def test_confirm_mint_version_keyset_mismatch(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    bad_versions = dict(versions)
    bad_versions.pop(ids[0])                    # 键集与 ids 不符
    with pytest.raises(ValueError):
        identity_api.confirm_mint("tasks", ids, bad_versions,
                                  _ctx=boss_ctx,
                                  _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))


def test_confirm_mint_full_chain_and_frozen_snapshot(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    """正向全链：确认 → 铸造 ctx₁ 携令牌；Registry 存完整冻结快照；
    claim 不接收 versions、返回冻结快照；旧 ctx₀._token 恒 None。"""
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions,
                                     _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    assert ctx1 is not boss_ctx
    assert boss_ctx._token is None
    assert ctx1._token is not None
    # 确认后目标被改动 → 冻结快照仍是确认时刻的值（不重读当前版本）
    with db_conn:
        db_conn.execute("UPDATE tasks SET version=version+1 WHERE id=?", (ids[0],))
    snap = identity_api.claim_token(ctx1._token.token_id, "tasks", ids, _ctx=ctx1)
    assert snap is not None
    frozen_ids, frozen_versions = snap
    assert tuple(frozen_ids) == tuple(sorted(ids))
    assert dict(frozen_versions) == versions    # 冻结值，非当前值


def test_claim_token_signature_has_no_versions():
    """v9 签名断言：claim_token 不接收 versions 入参。"""
    import inspect
    params = inspect.signature(identity.IdentityAPI.claim_token).parameters
    assert "versions" not in params


def test_claim_token_atomic_exactly_one(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    """并发双 claim → 恰好一个成功（barrier 同步，锁内检查+置位原子）。"""
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions,
                                     _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    results, barrier = [], threading.Barrier(2)

    def racer():
        barrier.wait()
        results.append(identity_api.claim_token(
            ctx1._token.token_id, "tasks", ids, _ctx=ctx1))

    ts = [threading.Thread(target=racer) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert sum(r is not None for r in results) == 1


def test_token_release_retry_then_consumed(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    """回滚回退 issued → TTL 内重试成功；mark_consumed 后复用拒绝。"""
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions,
                                     _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    tid = ctx1._token.token_id
    assert identity_api.claim_token(tid, "tasks", ids, _ctx=ctx1) is not None
    identity_api.release_token(tid)             # 模拟事务回滚
    assert identity_api.claim_token(tid, "tasks", ids, _ctx=ctx1) is not None
    identity_api.mark_consumed(tid)
    assert identity_api.claim_token(tid, "tasks", ids, _ctx=ctx1) is None


def test_token_ttl_expiry(identity_api, auth_backend, boss_ctx, db_conn, monkeypatch, ui_confirmation_request_cls):
    t0 = 3_000_000.0
    monkeypatch.setattr(identity, "_now", lambda: t0)
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions,
                                     _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    monkeypatch.setattr(identity, "_now", lambda: t0 + identity.DELETE_TOKEN_TTL_S + 1)
    assert identity_api.claim_token(ctx1._token.token_id, "tasks", ids, _ctx=ctx1) is None


def test_token_misappropriation_rejected(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    """挪用拒绝：他组 table/ids；仅 token_id 字符串无 ctx 携带；他会话 ctx。"""
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    ctx1 = identity_api.confirm_mint("tasks", ids, versions,
                                     _ctx=boss_ctx,
                                     _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))
    tid = ctx1._token.token_id
    assert identity_api.claim_token(tid, "schedules", ids, _ctx=ctx1) is None
    assert identity_api.claim_token(tid, "tasks", ids[1:], _ctx=ctx1) is None
    assert identity_api.claim_token(tid, "tasks", ids, _ctx=boss_ctx) is None  # ctx₀ 无令牌
    # 另一会话铸 ctx 携同 token_id → 拒绝
    ev2 = auth_backend.authenticate("sess-boss-2")
    identity_api.register_session(ev2)
    ctx_other = identity_api.mint_boss_context(identity_api.issue_proof("sess-boss-2"))
    assert identity_api.claim_token(tid, "tasks", ids, _ctx=ctx_other) is None


def test_confirm_mint_session_revoked_rejected(identity_api, auth_backend, boss_ctx, db_conn, ui_confirmation_request_cls):
    ids, versions = _seed_tasks(db_conn)
    auth_backend.allow_confirm("sess-boss-1")
    identity_api.revoke_session("sess-boss-1")
    with pytest.raises(PermissionError):
        identity_api.confirm_mint("tasks", ids, versions,
                                  _ctx=boss_ctx,
                                  _confirmation_request=ui_confirmation_request_cls("sess-boss-1"))