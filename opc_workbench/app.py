"""应用组合根（M3.1 清单 v10.1/v10.2 + M5-a 调度接线）。

装配顺序（build_application 函数体内部，业务模块导入前完成构造）：
  OwnerChannelBackend（app 内部创建，唯一 boss 来源）
  → _build_identity(backend) → IdentityAPI
  → 组合根**函数体内部**自行打开生产连接并铸造 WriteCapability
    （db 侧无任何生产写获取函数；取得失败 fail fast，不静默降级）
  → _build_cas_service(authorize=identity_api.make_authorize(), writer=...)
  → visibility.DisclosureService(writer)（M2 披露写能力显式注入）
  → KnowledgeService(writer, llm, vault_root, identity_api)（M5-a）
  → build_tools(cas_service, identity_api, knowledge_service)
  → build_dispatcher(...)（唯一业务入口，注册表不对外暴露）
  → VaultWatcher(knowledge_service)（后台部件，启动点在 main）

安全边界：
- Application / CASService / DisclosureService / KnowledgeService / VaultWatcher
  / Dispatcher 都不公开原始 WriteCapability；普通属性访问取不到。
- 业务调用只能经 `Application.dispatcher.dispatch(envelope, ...)` 或两个
  Channel 句柄；原始工具注册表不对外暴露。
- boss 身份只由主聊天入口（OwnerChannelBackend → register_session →
  issue_proof → mint_boss_context）产出；来源令牌只在
  `python -m opc_workbench.app` 以模块入口执行时由启动流程铸造，交给
  **框架内部创建**的主窗口适配器（bind(submit)）——没有可获取的通道对象，
  不接受调用方传入 source="main_chat"，也不接受外部传入适配器。
- `confirm_action` 继续 fail closed（delete_doc 的 UI 确认留到 M5-b）。

生命周期：build_application() 只构造不缓存、不占有 owner 会话名额；
进程级单例与关停由 ApplicationLauncher 实例持有；owner 会话名额在
`_owner_session` 模块（全进程一份），只由真正持有名额的 Application
在 close() 中释放。
"""
from __future__ import annotations

import atexit
import os
import secrets
import sqlite3
import sys
import threading

from . import cas, config, db, identity, llm_client, visibility
from . import _owner_session
from .config import agent_roles as _agent_roles_snapshot
from .dispatch import _mint_envelope, build_dispatcher
from .knowledge_service import KnowledgeService
from .main_window import MainWindowAdapter
from .vault_watcher import VaultWatcher


# ---------------------------------------------------------------------------
# 可信启动边界（M5-a B1，用户裁决 2026-11-23）
# 唯一可信启动命令：`python -m opc_workbench.app`。
# 验证的不是"调用栈里出现 __main__"（帧可被 exec 伪造），而是进程真实的
# 主模块：sys.modules["__main__"] 必须带有 `-m` 启动生成的模块 spec
# （name == "opc_workbench.app"），且其命名空间就是**当前正在执行的这份
# 模块代码**的命名空间。python -c、普通脚本、被导入模块、exec 伪造帧
# 一律不满足；即便在真实 `-m` 进程里另行 import 本模块，那份副本的
# 命名空间也不是主模块命名空间，同样拒绝。
# ---------------------------------------------------------------------------
_APP_FILE = os.path.abspath(__file__)


def _require_entry_point() -> None:
    entry = sys.modules.get("__main__")
    spec = getattr(entry, "__spec__", None)
    if (spec is None
            or getattr(spec, "name", None) != "opc_workbench.app"
            or getattr(entry, "__dict__", None) is not globals()
            or os.path.abspath(getattr(entry, "__file__", "")) != _APP_FILE):
        raise PermissionError("owner_entry_point_only")



class _OwnerEntryCredential:
    """主对话入口凭证：不透明对象，只由 OwnerChannelBackend 持有并交给组合根。

    除组合根本身外，没有任何公开接口能取得它——因此"传一个
    source=main_chat 的字典"不可能换来 boss 身份。
    """

    __slots__ = ("_channel",)

    def __init__(self, *a, **kw):
        raise PermissionError("owner_entry_credential_direct_construction_forbidden")

    def __setattr__(self, *a):
        raise PermissionError("owner_entry_credential_immutable")


class OwnerChannelBackend:
    """v1 单机单用户工作台的主对话入口认证后端。

    不校验自然人：boss 来源 = 组合根持有的入口凭证。会话标识由本对象内部
    生成，不从用户正文、LLM 参数或 Agent 消息取得。`validate_event` 只认
    本对象签发并登记过的 AuthEvent。
    """

    __slots__ = ("_lock", "_credential", "_session_id", "_events")

    def __init__(self):
        object.__setattr__(self, "_lock", threading.Lock())
        cred = object.__new__(_OwnerEntryCredential)
        object.__setattr__(cred, "_channel", "main_chat")
        object.__setattr__(self, "_credential", cred)
        object.__setattr__(self, "_session_id",
                           "main_chat:" + secrets.token_hex(8))
        object.__setattr__(self, "_events", {})

    def __setattr__(self, *a):
        raise PermissionError("owner_backend_immutable")

    def _entry_credential(self):
        """组合根专用：返回入口凭证（不暴露为 Application 属性）。"""
        return object.__getattribute__(self, "_credential")

    def authenticate(self, credential):
        """只有持有入口凭证的调用方才能拿到 AuthEvent；其余一律 None。"""
        if credential is not object.__getattribute__(self, "_credential"):
            return None
        ev = object.__new__(identity.AuthEvent)
        event_id = secrets.token_hex(16)
        object.__setattr__(ev, "_event_id", event_id)
        object.__setattr__(ev, "_session_id",
                           object.__getattribute__(self, "_session_id"))
        with object.__getattribute__(self, "_lock"):
            object.__getattribute__(self, "_events")[event_id] = \
                object.__getattribute__(self, "_session_id")
        return ev

    def validate_event(self, ev) -> bool:
        if not isinstance(ev, identity.AuthEvent):
            return False
        event_id = object.__getattribute__(ev, "_event_id")
        with object.__getattribute__(self, "_lock"):
            return object.__getattribute__(self, "_events").get(event_id) == \
                object.__getattribute__(ev, "_session_id")

    def confirm_action(self, session_id: str, action_spec: dict):
        """删除确认留到 M5-b：本批没有确认通道，继续 fail closed。"""
        return None


class ProductionAuthBackend:
    """遗留占位（M3 无 UI 语义）：authenticate / confirm_action 恒 None。

    生产装配已改用 OwnerChannelBackend；保留此类仅为历史测试与文档对照，
    应用组合根不持有它。
    """

    def authenticate(self, session_id: str):
        return None

    def validate_event(self, ev) -> bool:
        return False

    def confirm_action(self, session_id: str, action_spec: dict):
        return None


class Application:
    """组合根产物：只持有**有界服务**，不持有也不返回原始 WriteCapability。

    公开面只有：identity_api / cas_service / disclosure / dispatcher，
    以及 start_background() / close()。原始注册表、KnowledgeService、
    VaultWatcher、writer 都不在公开面。
    """

    __slots__ = ("identity_api", "cas_service", "disclosure", "dispatcher",
                 "_watcher", "_closed", "_owns_owner_session")

    def __init__(self, *, identity_api, cas_service, disclosure, dispatcher,
                 watcher, owns_owner_session=False):
        object.__setattr__(self, "identity_api", identity_api)
        object.__setattr__(self, "cas_service", cas_service)
        object.__setattr__(self, "disclosure", disclosure)
        object.__setattr__(self, "dispatcher", dispatcher)
        object.__setattr__(self, "_watcher", watcher)
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_owns_owner_session", bool(owns_owner_session))

    def __setattr__(self, *a):
        raise PermissionError("application_immutable")

    def __getattribute__(self, name):
        if name == "_watcher":
            raise PermissionError("application_watcher_not_exposed")
        return object.__getattribute__(self, name)

    # -- 生命周期 -----------------------------------------------------------

    def start_background(self) -> None:
        """启动后台 vault 扫描（幂等）。只在入口启动流程调用。"""
        if object.__getattribute__(self, "_closed"):
            raise PermissionError("application_closed")
        object.__getattribute__(self, "_watcher").start()

    def scan_once(self) -> dict:
        """同步扫一轮（组合根暴露的有界操作，不暴露 watcher 本体）。"""
        if object.__getattribute__(self, "_closed"):
            raise PermissionError("application_closed")
        return object.__getattribute__(self, "_watcher").scan_once()

    def close(self) -> None:
        """有界关停（幂等）：先停 watcher（join），再关内部写连接。

        owner 会话名额只由**实际持有名额**的应用释放（build_application()
        产物不占有名额，关闭不影响活动 owner 会话）；释放放在 finally，
        watcher/写连接关闭异常也不会把名额遗留为活动状态。
        """
        if object.__getattribute__(self, "_closed"):
            return
        object.__setattr__(self, "_closed", True)
        try:
            try:
                object.__getattribute__(self, "_watcher").stop()
            finally:
                self.cas_service.close()
        finally:
            if object.__getattribute__(self, "_owns_owner_session"):
                object.__setattr__(self, "_owns_owner_session", False)
                _owner_session.release()


def _compose(*, with_owner_channel: bool):
    """内部装配：返回 (Application, owner_submit | None)。

    带 boss 来源的装配是**模块入口专属**：进程必须以
    `python -m opc_workbench.app` 启动且当前执行的就是入口模块本体，
    且同一进程只允许一个活动 owner 会话。普通模块直接 import 调用、
    exec 伪造 `__main__` 帧、`python -c` 一律被拒。
    """
    if with_owner_channel:
        _require_entry_point()
        _owner_session.claim()
    _conn = None
    try:
        db.init_db()                                   # 迁移先行（零播种）
        backend = OwnerChannelBackend()                # app 内部创建
        identity_api = identity._build_identity(backend)
        # 生产写能力铸造：**只存在于本函数体内部**，不暴露为任何模块级可调用
        # 对象，也不经 Application / CASService / 工具实例向外泄露。
        _conn = sqlite3.connect(str(config.DB_PATH),
                                timeout=config.DB_BUSY_TIMEOUT_MS / 1000,
                                check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        db.apply_write_pragmas(_conn)
        production_writer = object.__new__(db.WriteCapability)
        object.__setattr__(production_writer, "_conn", _conn)
        object.__setattr__(production_writer, "_db_path", str(config.DB_PATH))
        object.__setattr__(production_writer, "_cap_id", "production")
        object.__setattr__(production_writer, "_lock", threading.Lock())
        object.__setattr__(production_writer, "_bound_authorize", None)
        authorize = identity_api.make_authorize()
        cas_service = cas._build_cas_service(authorize=authorize,
                                             writer=production_writer)
        # M2 披露写能力显式注入（db 侧已无普通模块可调用的获取入口）
        disclosure = visibility.DisclosureService(production_writer)
        knowledge_service = KnowledgeService(
            production_writer, llm=llm_client.get_llm(),
            vault_root=config.VAULT_PATH, identity_api=identity_api)
        from .tools import build_tools
        registry = build_tools(cas_service=cas_service,
                               identity_api=identity_api,
                               knowledge_service=knowledge_service)
        dispatcher = build_dispatcher(
            identity_api=identity_api, registry=registry, backend=backend,
            owner_credential=backend._entry_credential(),
            agent_roles=_agent_roles_snapshot(),
            main_chat=with_owner_channel)
        watcher = VaultWatcher(knowledge_service)
        application = Application(
            identity_api=identity_api, cas_service=cas_service,
            disclosure=disclosure, dispatcher=dispatcher, watcher=watcher,
            owns_owner_session=with_owner_channel)
        owner_submit = None
        if with_owner_channel:
            provenance = object.__getattribute__(dispatcher, "_owner_provenance")

            def owner_submit(action: str, params: dict | None = None) -> dict:
                """主聊天提交端口：来源令牌由框架在此注入，调用方无从指定。"""
                env = _mint_envelope(source="main_chat", agent_id=None,
                                     parent_message_id=None, origin=provenance)
                return dispatcher.dispatch(env, action, params)

        return application, owner_submit
    except BaseException:
        # 装配中途失败：关闭已打开的生产连接后再抛，绝不遗留悬挂连接
        if _conn is not None:
            _conn.close()
        if with_owner_channel:
            _owner_session.release()
        raise


def build_application() -> Application:
    """公开组装工厂：**只构造，不缓存，也不产出 boss 通道**。

    普通业务模块即便反复调用，也只得到有界服务集合；主对话通道只能由
    `__main__` 进程入口铸造，并直接交给主窗口适配器。
    """
    return _compose(with_owner_channel=False)[0]


class ApplicationLauncher:
    """进程级生命周期持有者：单例装配 + 关停。

    `build()` 只装配有界应用（**不含**主对话来源，也不占有 owner 会话
    名额）；boss 来源只由模块入口启动流程铸造并交给内部创建的适配器，
    不挂在启动器上。
    """

    __slots__ = ("_application", "_lock")

    def __init__(self):
        object.__setattr__(self, "_application", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def __getattribute__(self, name):
        if name == "_application":
            raise PermissionError("launcher_application_not_exposed")
        return object.__getattribute__(self, name)

    def build(self) -> Application:
        with object.__getattribute__(self, "_lock"):
            current = object.__getattribute__(self, "_application")
            if current is not None:
                return current
            application = build_application()   # 失败时内部已清理连接
            object.__setattr__(self, "_application", application)
            return application

    def close(self) -> None:
        """幂等关停：先清状态再关连接，避免关 stopped 实例被再次取用。"""
        with object.__getattribute__(self, "_lock"):
            current = object.__getattribute__(self, "_application")
            if current is None:
                return
            object.__setattr__(self, "_application", None)
            current.close()


def _run_entry():
    """模块入口启动流程（私有，无参数）：只在本文件 `__main__` 块调用。

    适配器由本流程**内部创建**（M6 换 Gradio 实现，契约不变 bind(submit)），
    不接受外部传入；绑定或后台启动失败时关闭应用（释放 owner 名额、
    停 watcher、关写连接）后再抛，绝不遗留活动会话或悬挂资源。
    """
    application, owner_submit = _compose(with_owner_channel=True)
    try:
        adapter = MainWindowAdapter()
        adapter.bind(owner_submit)
        application.start_background()
    except BaseException:
        application.close()
        raise
    atexit.register(application.close)
    return application, adapter


if __name__ == "__main__":
    # 唯一可信启动：python -m opc_workbench.app
    # M6 在此接入主窗口事件循环；当前批次装配 + 绑定后即收尾（atexit 关停）。
    _entry_application, _entry_adapter = _run_entry()
