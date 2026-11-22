"""统一调度入口（M5-a）。

职责边界：
- **唯一入口**：业务动作只能经 `Dispatcher.dispatch(envelope, action, params)`
  到达工具注册表；注册表本身不对外暴露，调用方无法自行传入
  `_frame_caller` / `_trusted_ctx` 绕过身份判定。
- **信封只由框架铸造**：`MessageEnvelope` 不可直构，只由本模块内部的
  `_mint_envelope`（仅供 MainChatChannel / AgentChannel 使用）产生；
  传入 dict 或其它冒充对象一律 `invalid_envelope`，字段一律不采信。
- **身份只看来源，不看正文**：`source`/`actor_role` 由入口与注册表决定；
  消息正文自称 boss、转述、伪造字段一律无效（正文甚至不进入信封）。
  转述不继承原消息权限：Agent 转述老板要求，仍按该 Agent 注册角色授权。

两种来源：
- `main_chat`：`MainChatChannel` 持有组合根铸造的一次性入口凭证，
  经 OwnerChannelBackend → register_session → issue_proof →
  mint_boss_context → resolve_identity("boss") 得到 boss 身份。
- `agent_message`：角色由 `Dispatcher.register_agent(agent_id, role)` 的
  注册信息决定，信封里的角色字段不被采信。

`parent_message_id` 只做追踪，绝不参与授权。
"""
from __future__ import annotations

import uuid

from . import audit_log, config


def _err(code: str, message: str = "") -> dict:
    return {"error": {"code": code, "message": message or code}}


class MessageEnvelope:
    """框架铸造的消息信封（不可直构、不可变）。

    只承载来源与追踪信息；**不承载正文**，因此不存在"正文自称 boss"的入口。
    """

    __slots__ = ("_source", "_agent_id", "_message_id", "_parent_message_id", "_channel")

    def __init__(self, *a, **kw):
        raise PermissionError("message_envelope_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("message_envelope_immutable")

    @property
    def source(self) -> str:
        return object.__getattribute__(self, "_source")

    @property
    def agent_id(self) -> str | None:
        return object.__getattribute__(self, "_agent_id")

    @property
    def message_id(self) -> str:
        return object.__getattribute__(self, "_message_id")

    @property
    def parent_message_id(self) -> str | None:
        return object.__getattribute__(self, "_parent_message_id")


def _mint_envelope(*, source: str, agent_id: str | None, parent_message_id: str | None,
                   channel: object) -> MessageEnvelope:
    """内部铸造入口（模块私有）：只有两个 Channel 句柄会调用。"""
    if source not in ("main_chat", "agent_message"):
        raise ValueError(f"invalid_source:{source!r}")
    env = object.__new__(MessageEnvelope)
    object.__setattr__(env, "_source", source)
    object.__setattr__(env, "_agent_id", agent_id)
    object.__setattr__(env, "_message_id", uuid.uuid4().hex)
    object.__setattr__(env, "_parent_message_id", parent_message_id)
    object.__setattr__(env, "_channel", channel)
    return env


class MainChatChannel:
    """主对话入口句柄：组合根创建并交给主窗口适配层。

    只暴露 `submit()`；入口凭证与身份链细节不外露。**不接受外部传入的
    source/role**——来源硬编码为 main_chat，身份由 OwnerChannelBackend 签发。
    """

    __slots__ = ("_dispatcher", "_credential")

    def __init__(self, *a, **kw):
        raise PermissionError("main_chat_channel_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("main_chat_channel_immutable")

    def __getattribute__(self, name):
        if name == "_credential":
            raise PermissionError("main_chat_credential_not_exposed")
        return object.__getattribute__(self, name)

    def submit(self, action: str, params: dict | None = None) -> dict:
        """提交一次老板动作。params 原样进工具校验，绝不影响来源与角色。"""
        dispatcher = object.__getattribute__(self, "_dispatcher")
        env = _mint_envelope(source="main_chat", agent_id=None,
                             parent_message_id=None, channel=self)
        return dispatcher.dispatch(env, action, params)


class AgentChannel:
    """员工 Agent 句柄：由 `Dispatcher.register_agent` 按注册信息发放。

    角色固定在注册时确定；本句柄无法写入或覆盖角色。
    """

    __slots__ = ("_dispatcher", "_agent_id")

    def __init__(self, *a, **kw):
        raise PermissionError("agent_channel_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("agent_channel_immutable")

    @property
    def agent_id(self) -> str:
        return object.__getattribute__(self, "_agent_id")

    def submit(self, action: str, params: dict | None = None,
               *, parent_message_id: str | None = None) -> dict:
        """提交一次 Agent 动作。parent_message_id 仅供追踪，不参与授权。"""
        dispatcher = object.__getattribute__(self, "_dispatcher")
        env = _mint_envelope(source="agent_message",
                             agent_id=object.__getattribute__(self, "_agent_id"),
                             parent_message_id=parent_message_id, channel=self)
        return dispatcher.dispatch(env, action, params)


class Dispatcher:
    """调度器：唯一业务入口 + 身份解析 + 注册表隔离。

    - `_registry` / `_backend` / `_owner_credential` 经普通属性访问一律拒绝，
      防止拿到调度器后绕过 dispatch 直接取工具闭包或入口凭证；
    - `schemas()` 只返回 schema 副本，供 Function Calling 使用，不含 handler。
    """

    __slots__ = ("_identity_api", "_registry", "_backend", "_owner_credential",
                 "_agent_roles", "_channels", "_agent_lock", "_owner_session_id",
                 "_main_chat", "_closed")

    def __init__(self, *a, **kw):
        raise PermissionError("dispatcher_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("dispatcher_immutable")

    def __getattribute__(self, name):
        if name in ("_registry", "_backend", "_owner_credential", "_main_chat"):
            raise PermissionError("dispatcher_internal_not_exposed")
        return object.__getattribute__(self, name)

    def agent_channel(self, agent_id: str) -> AgentChannel:
        """取得**框架已注册** Agent 的句柄。

        角色只取可信注册表（`config.AGENT_ROLES` 在组合根装配时的快照），
        调用方无法指定角色、无法新增条目、无法改写既有角色。
        """
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("invalid_agent_id")
        roles = object.__getattribute__(self, "_agent_roles")
        role = roles.get(agent_id)
        if role is None:
            raise PermissionError("agent_not_provisioned")
        channels = object.__getattribute__(self, "_channels")
        with object.__getattribute__(self, "_agent_lock"):
            channel = channels.get(agent_id)
            if channel is None:
                channel = object.__new__(AgentChannel)
                object.__setattr__(channel, "_dispatcher", self)
                object.__setattr__(channel, "_agent_id", agent_id)
                channels[agent_id] = channel
        return channel

    def schemas(self) -> dict:
        """FC schema 副本（不含 handler）。"""
        registry = object.__getattribute__(self, "_registry")
        return {name: dict(entry["schema"]) for name, entry in registry.items()}

    def dispatch(self, envelope, action: str, params: dict | None = None) -> dict:
        """唯一业务入口。信封必须是框架铸造的 MessageEnvelope 实例。"""
        if not isinstance(envelope, MessageEnvelope):
            return _err("invalid_envelope")
        registry = object.__getattribute__(self, "_registry")
        entry = registry.get(action)
        if entry is None:
            return _err("unknown_action")
        if params is not None and not isinstance(params, dict):
            return _err("invalid_param", "params")
        if params and any(str(k).startswith("_") for k in params):
            # 身份字段只能由框架注入；参数面出现 _frame_caller/_trusted_ctx 一律拒绝
            return _err("invalid_param", "reserved_param")
        try:
            frame_caller, trusted_ctx = self._resolve(envelope)
        except PermissionError as exc:
            audit_log.log("WARN", "dispatch_denied", result_code=str(exc)[:80],
                          target_id=object.__getattribute__(envelope, "_message_id"))
            return _err("identity_denied", str(exc))
        except ValueError as exc:
            return _err("identity_denied", str(exc))
        handler = entry["handler"]
        try:
            return handler(_frame_caller=frame_caller, _llm_args={},
                           _trusted_ctx=trusted_ctx, **(params or {}))
        except PermissionError as exc:
            return _err("identity_denied", str(exc))

    # -- 身份解析（只看来源） -------------------------------------------------

    def _resolve(self, envelope):
        source = object.__getattribute__(envelope, "_source")
        if source == "main_chat":
            return self._resolve_main_chat(envelope)
        if source == "agent_message":
            return self._resolve_agent(envelope)
        raise PermissionError(f"unknown_source:{source!r}")

    def _resolve_main_chat(self, envelope):
        main_chat = object.__getattribute__(self, "_main_chat")
        channel = object.__getattribute__(envelope, "_channel")
        if main_chat is None or channel is not main_chat:
            raise PermissionError("main_chat_envelope_required")
        identity_api = object.__getattribute__(self, "_identity_api")
        backend = object.__getattribute__(self, "_backend")
        credential = object.__getattribute__(self, "_owner_credential")
        sid = object.__getattribute__(self, "_owner_session_id")
        if sid is None or not identity_api.is_session_active(sid):
            ev = backend.authenticate(credential)
            if ev is None:
                raise PermissionError("owner_channel_credential_invalid")
            identity_api.register_session(ev)
            sid = object.__getattribute__(ev, "_session_id")
            object.__setattr__(self, "_owner_session_id", sid)
        proof = identity_api.issue_proof(sid)
        ctx = identity_api.mint_boss_context(proof)
        identity_api.resolve_identity("boss", {}, ctx)   # 校验 ctx 已铸造且会话有效
        return "boss", ctx

    def _resolve_agent(self, envelope):
        if object.__getattribute__(envelope, "_channel").agent_id != \
                object.__getattribute__(envelope, "_agent_id"):
            raise PermissionError("agent_envelope_mismatch")
        agent_id = object.__getattribute__(envelope, "_agent_id")
        roles = object.__getattribute__(self, "_agent_roles")
        role = roles.get(agent_id)
        if role is None:
            raise PermissionError("agent_not_provisioned")
        identity_api = object.__getattribute__(self, "_identity_api")
        identity_api.resolve_identity(role, {})
        return role, None


def build_dispatcher(*, identity_api, registry: dict, backend, owner_credential,
                     agent_roles: dict | None = None,
                     main_chat: bool = False) -> Dispatcher:
    """组合根内部构造入口：装配身份链、入口凭证与注册表。

    `agent_roles` 是可信注册表的**快照**（组合根读取 `config.AGENT_ROLES`），
    之后调用方无法通过调度器改写。
    `main_chat=True` 时才铸造主对话句柄，由可信启动入口持有。
    """
    import threading
    d = object.__new__(Dispatcher)
    object.__setattr__(d, "_identity_api", identity_api)
    object.__setattr__(d, "_registry", registry)
    object.__setattr__(d, "_backend", backend)
    object.__setattr__(d, "_owner_credential", owner_credential)
    roles_snapshot = dict(agent_roles or {})
    for agent_id, role in roles_snapshot.items():
        if role not in config.ROLES:
            raise ValueError(f"unknown_role:{role!r}")
    object.__setattr__(d, "_agent_roles", roles_snapshot)
    object.__setattr__(d, "_channels", {})
    object.__setattr__(d, "_agent_lock", threading.Lock())
    object.__setattr__(d, "_owner_session_id", None)
    object.__setattr__(d, "_closed", False)
    main = None
    if main_chat:
        main = object.__new__(MainChatChannel)
        object.__setattr__(main, "_dispatcher", d)
        object.__setattr__(main, "_credential", owner_credential)
    object.__setattr__(d, "_main_chat", main)
    return d
