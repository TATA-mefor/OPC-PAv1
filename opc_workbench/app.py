"""应用组合根（M3.1 清单 v10.1/v10.2：生产服务对象的唯一组装点）。

装配顺序（main 内部，业务模块导入前完成构造）：
  auth_backend（app 内部创建，不接收外部参数）
  → _build_identity(auth_backend) → IdentityAPI
  → 组合根**函数体内部**自行打开生产连接并铸造 WriteCapability
    （db 侧无任何生产写获取函数；取得失败 fail fast，不静默降级）
  → _build_cas_service(authorize=identity_api.make_authorize(), writer=...)
  → visibility.DisclosureService(writer)（M2 披露写能力显式注入）
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

import sqlite3
import threading

from . import cas, config, db, identity, visibility

# 生产 writer 一次性名额：仅防重复装配（fail fast），**不是访问控制**。
# 真正的边界是"铸造代码只存在于 build_application 函数体内部"，
# 项目 API 中没有任何返回生产写能力的模块级可调用对象。
_production_writer_taken = False
_production_writer_lock = threading.Lock()


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

    __slots__ = ("identity_api", "cas_service", "writer", "tools", "disclosure")

    def __init__(self, *, identity_api, cas_service, writer, tools, disclosure):
        object.__setattr__(self, "identity_api", identity_api)
        object.__setattr__(self, "cas_service", cas_service)
        object.__setattr__(self, "writer", writer)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "disclosure", disclosure)

    def __setattr__(self, *a):
        raise PermissionError("application_immutable")


def build_application() -> Application:
    """唯一组装点。任何一步失败原样抛错（fail fast，不静默降级）。"""
    db.init_db()                                   # 迁移先行（零播种）
    auth_backend = ProductionAuthBackend()         # app 内部创建
    identity_api = identity._build_identity(auth_backend)
    # 生产写能力铸造：**只存在于本函数体内部**，不暴露为任何模块级可调用对象。
    # 普通业务模块无法经项目 API 抢先取得生产 writer（一次性锁只防重复装配，
    # 不承担访问控制）。失败原样抛错 → 启动失败，不静默降级。
    global _production_writer_taken
    with _production_writer_lock:
        if _production_writer_taken:
            raise RuntimeError("production_writer_already_taken")
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
        _production_writer_taken = True
    authorize = identity_api.make_authorize()
    cas_service = cas._build_cas_service(authorize=authorize, writer=production_writer)
    # M2 披露写能力显式注入（db 侧已无普通模块可调用的获取入口）
    disclosure = visibility.DisclosureService(production_writer)
    from .tools import build_tools
    tools = build_tools(cas_service=cas_service, identity_api=identity_api)
    return Application(identity_api=identity_api, cas_service=cas_service,
                       writer=production_writer, tools=tools, disclosure=disclosure)


def main() -> Application:
    """进程入口。M3 交付组合根；M6 在此接 Gradio launch（仅 127.0.0.1）。"""
    return build_application()


if __name__ == "__main__":
    main()