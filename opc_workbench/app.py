"""应用组合根（M3.1 清单 v10.1/v10.2：生产服务对象的唯一组装点）。

装配顺序（main 内部，业务模块导入前完成构造）：
  auth_backend（app 内部创建，不接收外部参数）
  → _build_identity(auth_backend) → IdentityAPI
  → _open_production_writer()（取得失败 fail fast，不静默降级）
  → _build_cas_service(authorize=identity_api.make_authorize(), writer=...)
  → build_tools(cas_service=...)（业务工具只接收注入实例）
  → launch（M6 补 Gradio 接线）

安全依据 = 对象所有权：生产实例引用只存在于 main 局部/应用状态，
无模块级可变服务变量可供替换；业务模块自建整套链只得到平行对象。

M3 现实约束：无 UI。ProductionAuthBackend.authenticate 恒 None
（boss 会话无法登记 → boss 通道生产不可达，仅测试注入）；
confirm_action 恒 None（确认事件不可得 → boss ≥5 直执生产不可达）。
M6 接 Gradio 会话认证与确认弹窗后替换后端实现。
"""
from __future__ import annotations

from . import cas, db, identity


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
    """组合根产物：持有生产实例引用（对象所有权边界）。"""

    __slots__ = ("identity_api", "cas_service", "writer", "tools")

    def __init__(self, *, identity_api, cas_service, writer, tools):
        object.__setattr__(self, "identity_api", identity_api)
        object.__setattr__(self, "cas_service", cas_service)
        object.__setattr__(self, "writer", writer)
        object.__setattr__(self, "tools", tools)

    def __setattr__(self, *a):
        raise PermissionError("application_immutable")


def build_application() -> Application:
    """唯一组装点。任何一步失败原样抛错（fail fast，不静默降级）。"""
    db.init_db()                                   # 迁移先行（零播种）
    auth_backend = ProductionAuthBackend()         # app 内部创建
    identity_api = identity._build_identity(auth_backend)
    production_writer = db._open_production_writer()
    cas_service = cas._build_cas_service(
        authorize=identity_api.make_authorize(), writer=production_writer)
    from .tools import build_tools
    tools = build_tools(cas_service=cas_service, identity_api=identity_api)
    return Application(identity_api=identity_api, cas_service=cas_service,
                       writer=production_writer, tools=tools)


def main() -> Application:
    """进程入口。M3 交付组合根；M6 在此接 Gradio launch（仅 127.0.0.1）。"""
    return build_application()


if __name__ == "__main__":
    main()