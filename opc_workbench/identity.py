"""身份链（M3.1 清单 v5–v10.1 定稿实现）。

双通道身份解析：
- Agent：frame_caller ∈ config.ROLE_ORDER，沿用 M2 resolve_caller 语义
  （零改动），LLM 伪造字段丢弃；登记 {role, session_id:None}。
- boss：必须持可信上下文 TrustedContext（仅由 mint_boss_context 铸造，
  凭证为 SessionAuthority 签发的 HMAC SessionProof，一次性消费）；
  每次解析经保存的 authority 引用复查会话有效性（撤销/过期立即失效）。

信任依据（v10 定稿）：
- Identity 只是含不透明 ident_id 的句柄，**对象形状不是信任凭据**；
  真实凭据是 authority._identities 签发登记表（实例私有）。
  _authorize 查登记：未登记/撤销/过期/跨实例一律拒；role 取自登记。
- 组合根内部构造（_build_identity），无公开装配入口；下划线/__all__/
  导入顺序/静态检查都不是安全边界——安全依据是对象所有权与依赖注入：
  业务模块自建整套链只得到平行对象，进不了应用实际使用的实例。

诚实边界：进程内 Python 无硬安全边界（完整解释器权限者可内省替换）。
本模块封死"导入即构造、LLM 参数构造、字典/篡改伪造、凭证重放"现实路径，
不抵御本机恶意代码（威胁模型外）。

M3 现实约束：无 UI（M6 才建），boss 通道仅测试注入可达；生产确认事件
恒不可得（auth_backend.confirm_action 无 UI 时返回 None）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid

from . import audit_log, config, visibility

SESSION_TTL_S = 8 * 3600          # 会话有效期（M6 接 Gradio 会话生命周期）
IDENTITY_TTL_S = 15 * 60          # 身份句柄有效期（到期重新解析）
DELETE_TOKEN_TTL_S = 5 * 60       # 确认令牌 TTL（清单 v6 定稿）


def _now() -> float:
    """唯一时钟入口（epoch 秒）；测试 monkeypatch 做临界验证。"""
    return time.time()


def canonical_json(obj) -> str:
    """规范化 JSON：键排序、无多余空白、UTF-8（摘要计算唯一序列化口径）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_of(table: str, ids, versions: dict) -> str:
    core = {"table": table, "ids": sorted(int(i) for i in ids),
            "versions": {str(int(k)): int(v) for k, v in versions.items()}}
    return hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 不可直构对象（构造门禁；真实例由内部工厂 object.__new__ 产出）
# ---------------------------------------------------------------------------

class SessionAuthority:
    """会话/身份/令牌的可信根。不可直构；实例由 _build_identity 产出。"""

    def __init__(self, *a, **kw):
        raise PermissionError("authority_direct_construction_forbidden")


class AuthEvent:
    """认证子系统签发的认证事件。构造受装配保护：只有 auth_backend
    （组合根内部创建/测试 fixture 注入）能产出被 validate_event 认可的事件。"""

    __slots__ = ("_event_id", "_session_id")

    def __init__(self, *a, **kw):
        raise PermissionError("auth_event_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("auth_event_immutable")


class SessionProof:
    """HMAC 会话凭证（一次性 nonce）。由 SessionAuthority.issue_proof 签发。"""

    __slots__ = ("_session_id", "_nonce", "_issued_at", "_mac")

    def __init__(self, *a, **kw):
        raise PermissionError("session_proof_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("session_proof_immutable")


class TrustedContext:
    """boss 可信上下文。不可直构、不可变；仅 mint_boss_context / confirm_mint 铸造。"""

    __slots__ = ("_caller", "_session_id", "_authority", "_api", "_token", "_minted")

    def __init__(self, *a, **kw):
        raise PermissionError("trusted_context_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("trusted_context_immutable")


class Identity:
    """身份句柄：仅一个不透明 ident_id，无 role/ctx 字段可伪造。
    信任凭据是 authority._identities 登记，不是本对象。"""

    __slots__ = ("_ident_id",)

    def __init__(self, *a, **kw):
        raise PermissionError("identity_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("identity_immutable")


class DeleteToken:
    """确认令牌标识（版本快照留在 TokenRegistry，执行时服务端取回）。"""

    __slots__ = ("_token_id", "_bound_session", "_digest")

    def __init__(self, *a, **kw):
        raise PermissionError("delete_token_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("delete_token_immutable")

    @property
    def token_id(self) -> str:
        return object.__getattribute__(self, "_token_id")

    @property
    def bound_session(self) -> str:
        return object.__getattribute__(self, "_bound_session")

    @property
    def digest(self) -> str:
        return object.__getattribute__(self, "_digest")


# ---------------------------------------------------------------------------
# IdentityAPI：装配闭包的公开面（实例由 _build_identity 产出并持有 authority）
# ---------------------------------------------------------------------------

class IdentityAPI:
    """身份服务实例。构造期固定 authority，无 setter/rebind。

    业务模块即便自构 IdentityAPI 也只得到平行对象（authority 不同一，
    登记互不可见）——无法替换应用实际使用的实例。
    """

    __slots__ = ("_authority",)

    def __init__(self, authority: SessionAuthority):
        if not isinstance(authority, SessionAuthority):
            raise PermissionError("identity_api_requires_authority_instance")
        object.__setattr__(self, "_authority", authority)

    def __setattr__(self, *a):
        raise PermissionError("identity_api_immutable")

    @property
    def owner_token(self) -> object:
        """装配归属标识（authority 实例引用）。供组合根配对校验：
        生产 writer 只接受与本 api 同源的 authorize 闭包。
        拿到引用不等于能伪造身份（登记与 secret 仍在实例内部）。"""
        return object.__getattribute__(self, "_authority")

    def make_authorize(self):
        """产出带归属标记的 authorize 闭包（组合根装配用）。

        闭包行为与 self.authorize 完全一致；附带 _pa_owner 指向本 api 的
        authority，供 CASService 构造期校验 writer/authorize 同源配对。
        伪造 lambda 无此标记 → 配生产 writer 构造即拒。
        """
        def authorize(ident):
            return self.authorize(ident)
        authorize._pa_owner = self.owner_token
        return authorize

    # -- 会话登记与凭证（职责分离，v8） ------------------------------------

    def register_session(self, auth_event) -> None:
        """登记会话：必须携带认证子系统签发且经后端校验的认证事件。"""
        a = self._authority
        if not a._auth_backend.validate_event(auth_event):
            raise PermissionError("auth_event_invalid")
        sid = auth_event._session_id
        with a._lock:
            a._sessions[sid] = {"registered_at": _now(), "revoked": False,
                                "consumed_nonces": set()}
        audit_log.log("INFO", "session_registered", actor_role="boss", target_id=sid)

    def _mac(self, sid: str, nonce: str, issued_at: float) -> bytes:
        msg = f"{sid}|{nonce}|{issued_at}".encode("utf-8")
        return hmac.new(self._authority._secret, msg, hashlib.sha256).digest()

    def issue_proof(self, session_id: str) -> SessionProof:
        """仅对已登记且未撤销未过期的会话签发凭证。"""
        a = self._authority
        with a._lock:
            rec = a._sessions.get(session_id)
            if (rec is None or rec["revoked"]
                    or _now() > rec["registered_at"] + SESSION_TTL_S):
                raise PermissionError("session_not_active")
            nonce = secrets.token_hex(16)
            issued_at = _now()
        proof = object.__new__(SessionProof)
        object.__setattr__(proof, "_session_id", session_id)
        object.__setattr__(proof, "_nonce", nonce)
        object.__setattr__(proof, "_issued_at", issued_at)
        object.__setattr__(proof, "_mac", self._mac(session_id, nonce, issued_at))
        return proof

    def consume_proof_once(self, proof) -> str | None:
        """仅铸造上下文时调用一次：HMAC + 登记 + TTL + 撤销 + nonce 未消费。
        任一不满足 → None（fail closed）；重放同一 proof → None。"""
        if not isinstance(proof, SessionProof):
            return None
        a = self._authority
        sid = proof._session_id
        expected = self._mac(sid, proof._nonce, proof._issued_at)
        if not hmac.compare_digest(expected, proof._mac):
            return None
        with a._lock:
            rec = a._sessions.get(sid)
            if (rec is None or rec["revoked"]
                    or _now() > rec["registered_at"] + SESSION_TTL_S):
                return None
            if proof._nonce in rec["consumed_nonces"]:
                return None
            rec["consumed_nonces"].add(proof._nonce)
        return sid

    def is_session_active(self, session_id: str) -> bool:
        """后续每次解析复查：只查登记/TTL/撤销，不触碰 nonce（可反复解析）。"""
        a = self._authority
        with a._lock:
            rec = a._sessions.get(session_id)
            return (rec is not None and not rec["revoked"]
                    and _now() <= rec["registered_at"] + SESSION_TTL_S)

    def revoke_session(self, session_id: str) -> None:
        """登出/超时：会话立即失效，已签发身份级联失效（authorize 复查）。"""
        a = self._authority
        with a._lock:
            rec = a._sessions.get(session_id)
            if rec is not None:
                rec["revoked"] = True
        audit_log.log("INFO", "session_revoked", actor_role="boss", target_id=session_id)

    # -- boss 上下文铸造 ----------------------------------------------------

    def mint_boss_context(self, proof) -> TrustedContext:
        """唯一铸造入口：一次性消费 proof，成功后产出不可变 ctx₀。"""
        sid = self.consume_proof_once(proof)
        if sid is None:
            raise PermissionError("session_proof_invalid")
        ctx = object.__new__(TrustedContext)
        object.__setattr__(ctx, "_caller", "boss")
        object.__setattr__(ctx, "_session_id", sid)
        object.__setattr__(ctx, "_authority", self._authority)
        object.__setattr__(ctx, "_api", self)
        object.__setattr__(ctx, "_token", None)
        object.__setattr__(ctx, "_minted", True)
        audit_log.log("INFO", "ctx_minted", actor_role="boss", target_id=sid)
        return ctx

    # -- 双通道身份解析（Agent 沿用 M2 resolve_caller 零改动） ---------------

    def resolve_identity(self, frame_caller: str, llm_args: dict,
                         trusted_ctx=None) -> Identity:
        a = self._authority
        if frame_caller == "boss":
            ctx = trusted_ctx
            if (not isinstance(ctx, TrustedContext)
                    or not ctx._minted
                    or ctx._caller != "boss"
                    or ctx._authority is not a
                    or not self.is_session_active(ctx._session_id)):
                raise PermissionError("boss_requires_trusted_context")
            role, sid, ctx_ref = "boss", ctx._session_id, ctx
            # LLM 伪造字段同样丢弃（M2 语义）
            visibility.resolve_caller("boss", llm_args)
        else:
            role = visibility.resolve_caller(frame_caller, llm_args)
            sid, ctx_ref = None, None
        ident_id = uuid.uuid4().hex
        with a._lock:
            a._identities[ident_id] = {
                "role": role, "session_id": sid, "ctx_ref": ctx_ref,
                "issued_at": _now(), "revoked": False,
            }
        ident = object.__new__(Identity)
        object.__setattr__(ident, "_ident_id", ident_id)
        return ident

    def authorize(self, ident) -> str:
        """每次写入都校验：查签发登记（未登记/撤销/过期/跨实例一律拒），
        role 取自登记而非对象字段；boss 会话 active 复查；Agent 同等校验。"""
        if not isinstance(ident, Identity):
            raise PermissionError("identity_object_required")
        a = self._authority
        with a._lock:
            rec = a._identities.get(ident._ident_id)
            if rec is None or rec["revoked"]:
                raise PermissionError("identity_not_registered_or_revoked")
            if _now() > rec["issued_at"] + IDENTITY_TTL_S:
                raise PermissionError("identity_expired")
            if rec["session_id"] is not None:
                if not self.is_session_active(rec["session_id"]):
                    raise PermissionError("session_not_active")
            return rec["role"]

    def revoke_identity(self, ident) -> None:
        if not isinstance(ident, Identity):
            raise PermissionError("identity_object_required")
        a = self._authority
        with a._lock:
            rec = a._identities.get(ident._ident_id)
            if rec is not None:
                rec["revoked"] = True

    # -- 确认事件 + 冻结快照 + 令牌传递链（v8/v9） ---------------------------

    def confirm_mint(self, table: str, ids, versions: dict, *,
                     _ctx, _confirmation_request) -> TrustedContext:
        """唯一框架级确认铸造入口（M6 UI 确认回调调用，M3 仅测试可达）。

        ① 会话复核 + authority 同一；② 可信确认事件（auth_backend 校验，
        业务模块/LLM 无法构造请求对象）；③ versions 键集 == ids 集合；
        ④ Registry 保存完整冻结快照；⑤ 构造携令牌的新不可变 ctx₁。
        """
        a = self._authority
        if (not isinstance(_ctx, TrustedContext) or not _ctx._minted
                or _ctx._authority is not a
                or not self.is_session_active(_ctx._session_id)):
            raise PermissionError("boss_requires_trusted_context")
        ids_sorted = tuple(sorted(int(i) for i in ids))
        if not ids_sorted:
            raise ValueError("empty_ids")
        if set(int(k) for k in versions) != set(ids_sorted):
            raise ValueError("versions_keyset_mismatch")
        action_spec = {"table": table, "ids": ids_sorted,
                       "versions": {str(int(k)): int(v) for k, v in versions.items()},
                       "request": _confirmation_request}
        res = a._auth_backend.confirm_action(_ctx._session_id, action_spec)
        if res is None:
            raise PermissionError("confirmation_required")
        digest = _digest_of(table, ids_sorted, versions)
        token_id = uuid.uuid4().hex
        frozen_versions = {int(k): int(v) for k, v in versions.items()}
        with a._lock:
            a._tokens[token_id] = {
                "table": table, "ids": ids_sorted, "versions": frozen_versions,
                "digest": digest, "issued_at": _now(), "state": "issued",
                "bound_session": _ctx._session_id,
            }
        tok = object.__new__(DeleteToken)
        object.__setattr__(tok, "_token_id", token_id)
        object.__setattr__(tok, "_bound_session", _ctx._session_id)
        object.__setattr__(tok, "_digest", digest)
        ctx1 = object.__new__(TrustedContext)
        object.__setattr__(ctx1, "_caller", "boss")
        object.__setattr__(ctx1, "_session_id", _ctx._session_id)
        object.__setattr__(ctx1, "_authority", a)
        object.__setattr__(ctx1, "_api", self)
        object.__setattr__(ctx1, "_token", tok)
        object.__setattr__(ctx1, "_minted", True)
        audit_log.log("INFO", "delete_token_minted", actor_role="boss",
                      target_table=table, target_id=token_id)
        return ctx1

    def claim_token(self, token_id: str, table: str, ids, *, _ctx):
        """原子占用（锁内检查+置位）。不接收 versions——成功返回 Registry
        冻结快照 (frozen_ids, frozen_versions)，执行用的唯一版本来源。"""
        a = self._authority
        ids_sorted = tuple(sorted(int(i) for i in ids))
        with a._lock:
            rec = a._tokens.get(token_id)
            if rec is None or rec["state"] != "issued":
                return None
            if rec["bound_session"] != getattr(_ctx, "_session_id", None):
                return None
            if getattr(_ctx, "_authority", None) is not a:
                return None
            ctx_tok = getattr(_ctx, "_token", None)
            if ctx_tok is None or ctx_tok.token_id != token_id:
                return None
            if _now() > rec["issued_at"] + DELETE_TOKEN_TTL_S:
                rec["state"] = "expired"
                return None
            if rec["table"] != table or ids_sorted != rec["ids"]:
                return None
            rec["state"] = "claimed"
            return (rec["ids"], dict(rec["versions"]))

    def release_token(self, token_id: str) -> None:
        """事务回滚 → claimed 回退 issued（TTL 内可重试，令牌不烧掉）。"""
        a = self._authority
        with a._lock:
            rec = a._tokens.get(token_id)
            if rec is not None and rec["state"] == "claimed":
                rec["state"] = "issued"

    def mark_consumed(self, token_id: str) -> None:
        """COMMIT 成功 → consumed，永久不可再用。"""
        a = self._authority
        with a._lock:
            rec = a._tokens.get(token_id)
            if rec is not None and rec["state"] == "claimed":
                rec["state"] = "consumed"


def _build_identity(auth_backend) -> IdentityAPI:
    """内部构造函数（组合根 app.main 与测试 fixture 调用）。

    下划线不是安全边界；安全依据是对象所有权：业务模块调用本函数只得到
    独立平行链，无法替换应用组合根持有的实例（无模块级可变服务变量）。
    """
    authority = object.__new__(SessionAuthority)
    object.__setattr__(authority, "_secret", secrets.token_bytes(32))
    object.__setattr__(authority, "_sessions", {})
    object.__setattr__(authority, "_tokens", {})
    object.__setattr__(authority, "_identities", {})
    object.__setattr__(authority, "_lock", threading.RLock())
    object.__setattr__(authority, "_auth_backend", auth_backend)
    return IdentityAPI(authority)