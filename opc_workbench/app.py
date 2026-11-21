"""应用组合根（M3.1 清单 v10.1/v10.2：生产服务对象的唯一组装点）。

装配顺序（build_application 函数体内部，业务模块导入前完成构造）：
  auth_backend（app 内部创建，不接收外部参数）
  → _build_identity(auth_backend) → IdentityAPI
  → 组合根**函数体内部**自行打开生产连接并铸造 WriteCapability
    （db 侧无任何生产写获取函数；取得失败 fail fast，不静默降级）
  → _build_cas_service(authorize=identity_api.make_authorize(), writer=...)
  → visibility.DisclosureService(writer)（M2 披露写能力显式注入）
  → build_tools(cas_service=...)（业务工具只接收注入实例）
  → launch（M6 补 Gradio 接线）

安全依据 = 对象所有权：生产写能力引用只存在于装配函数体局部与其注入的
有界服务内部；Application / CASService / DisclosureService / 工具实例
都不公开原始 WriteCapability，普通属性访问取不到。

生命周期（M3.2① 第三轮复审）：**不设模块级可变应用槽**。
  build_application() 只负责构造，每次返回一个独立的有界应用；
  进程级单例与关停由 ApplicationLauncher **实例**持有，启动器由明确的
  启动入口（main）创建并持有，不放进模块全局变量。
  构造中途失败 → 关闭已打开的连接后原样抛错，不留悬挂连接。

M3 现实约束：无 UI。ProductionAuthBackend.authenticate 恒 None
（boss 会话无法登记 → boss 通道生产不可达，仅测试注入）；
confirm_action 恒 None（确认事件不可得 → boss ≥5 直执生产不可达）。
M6 接 Gradio 会话认证与确认弹窗后替换后端实现。
"""
from __future__ import annotations

import sqlite3
import threading

from . import cas, config, db, identity, visibility


class ProductionAuthBackend:
    """M3 生产认证后端：无 UI，认证与确认恒不可得（fail closed）。

    M6 实现：authenticate 校验 Gradio 会话登录态并签发 AuthEvent；
    confirm_action 校验真实 UI 确认回调请求对象。
    """

    def authenticate(self, session_id: str):
        return None

    def validate_event(self, ev) -> bool:
        return False

    def confirm_action(self, session_id: str, action_spec: dict):
        return None


class Application:
    """组合根产物：只持有**有界服务**，不持有也不返回原始 WriteCapability。

    M3.2① 复审：原 __slots__ 含 "writer"，普通模块只需
    app.build_application().writer 即可取得生产写能力并用 transaction()
    绕过 CAS——与修复前 db._open_production_writer() 效果相同，只是入口
    从 db 移到 app。现移除该槽；关停经有界的 close() 完成。
    """

    __slots__ = ("identity_api", "cas_service", "tools", "disclosure", "_closed")

    def __init__(self, *, identity_api, cas_service, tools, disclosure):
        object.__setattr__(self, "identity_api", identity_api)
        object.__setattr__(self, "cas_service", cas_service)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "disclosure", disclosure)
        object.__setattr__(self, "_closed", False)

    def __setattr__(self, *a):
        raise PermissionError("application_immutable")

    def close(self) -> None:
        """有界关停（幂等）：委托 CAS 关闭内部写连接，不暴露写能力句柄。"""
        if object.__getattribute__(self, "_closed"):
            return
        object.__setattr__(self, "_closed", True)
        self.cas_service.close()


def build_application() -> Application:
    """唯一组装点：**只构造，不缓存**。任何一步失败原样抛错（fail fast）。

    每次调用装配一个独立的有界应用，调用方持有其生命周期并负责 close()。
    普通模块即便调用本工厂，也只拿到有界服务集合，取不到原始 writer；
    进程级"只装配一次"的语义由 ApplicationLauncher 实例承担。
    """
    db.init_db()                                   # 迁移先行（零播种）
    auth_backend = ProductionAuthBackend()         # app 内部创建
    identity_api = identity._build_identity(auth_backend)
    # 生产写能力铸造：**只存在于本函数体内部**，不暴露为任何模块级可调用
    # 对象，也不经 Application / CASService / 工具实例向外泄露。
    _conn = sqlite3.connect(str(config.DB_PATH),
                            timeout=config.DB_BUSY_TIMEOUT_MS / 1000,
                            check_same_thread=False)
    try:
        _conn.row_factory = sqlite3.Row
        db.apply_write_pragmas(_conn)
        production_writer = object.__new__(db.WriteCapability)
        object.__setattr__(production_writer, "_conn", _conn)
        object.__setattr__(production_writer, "_db_path", str(config.DB_PATH))
        object.__setattr__(production_writer, "_cap_id", "production")
        object.__setattr__(production_writer, "_lock", threading.Lock())
        object.__setattr__(production_writer, "_bound_authorize", None)
        authorize = identity_api.make_authorize()
        cas_service = cas._build_cas_service(authorize=authorize, writer=production_writer)
        # M2 披露写能力显式注入（db 侧已无普通模块可调用的获取入口）
        disclosure = visibility.DisclosureService(production_writer)
        from .tools import build_tools
        tools = build_tools(cas_service=cas_service, identity_api=identity_api)
        return Application(identity_api=identity_api, cas_service=cas_service,
                           tools=tools, disclosure=disclosure)
    except BaseException:
        # 装配中途失败：关闭已打开的生产连接后再抛，绝不遗留悬挂连接
        _conn.close()
        raise


class ApplicationLauncher:
    """进程级生命周期持有者：单例装配 + 关停 + 关停后重启。

    状态只存在于**实例**上（_application / _lock），模块级没有任何可变
    应用槽；启动器由明确的启动入口创建并持有。build() 幂等只防重复装配，
    **不是访问控制**——安全依据仍是 Application 不暴露原始写能力。
    close() 后状态清空，允许重新 build() 得到全新可用应用。
    """

    __slots__ = ("_application", "_lock")

    def __init__(self):
        object.__setattr__(self, "_application", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def build(self) -> Application:
        with self._lock:
            current = self._application
            if current is not None:
                return current
            application = build_application()   # 失败时内部已清理连接
            object.__setattr__(self, "_application", application)
            return application

    def close(self) -> None:
        """幂等关停：先清状态再关连接，避免关 stopped 实例被再次取用。"""
        with self._lock:
            current = self._application
            if current is None:
                return
            object.__setattr__(self, "_application", None)
            current.close()


def main() -> ApplicationLauncher:
    """进程入口：创建并持有启动器，完成一次装配。

    M3 交付组合根；M6 在此接 Gradio launch（仅 127.0.0.1），
    并在退出时调用 launcher.close()。
    """
    launcher = ApplicationLauncher()
    launcher.build()
    return launcher


if __name__ == "__main__":
    main()