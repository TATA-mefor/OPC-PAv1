"""工具注册表（M3.1 清单 §二：action 名 → 函数映射 + pydantic 参数模型汇总）。

M3 先以纯函数 + pydantic 参数校验交付，schema 字典预留 M5 Function Calling
消费；治理函数（request/resolve/consume_bulk_delete 等）不进注册表
（减少暴露面；身份约束来自 resolve_identity 本身，不依赖注册表）。

身份字段（_frame_caller/_llm_args/_trusted_ctx）不在任何 FC schema 内，
由框架注入。
"""
from __future__ import annotations


def build_tools(*, cas_service, identity_api) -> dict:
    """组合根装配入口：业务工具只接收注入实例，不拿模块/绑定函数。

    注册表仅含 LLM 可触达 action；治理函数（request/resolve/consume_
    bulk_delete 等）不注册（减少暴露面；身份约束来自 resolve_identity）。
    """
    from .task import TaskTool
    registry: dict = {}
    if cas_service is not None and identity_api is not None:
        task_tool = TaskTool(cas_service=cas_service, identity_api=identity_api)
        for action, schema in task_tool.fc_schemas().items():
            registry[f"task.{action}"] = {
                "handler": lambda _a=action, **kw: task_tool.dispatch(_a, **kw),
                "schema": schema,
            }
    if cas_service is not None and identity_api is not None:
        from .schedule import ScheduleTool
        schedule_tool = ScheduleTool(cas_service=cas_service, identity_api=identity_api)
        for action, schema in schedule_tool.fc_schemas().items():
            registry[f"schedule.{action}"] = {
                "handler": lambda _a=action, **kw: schedule_tool.dispatch(_a, **kw),
                "schema": schema,
            }
    if cas_service is not None and identity_api is not None:
        from ..llm_client import get_llm
        from .report import ReportTool
        report_tool = ReportTool(cas_service=cas_service, identity_api=identity_api,
                                 llm=get_llm())
        for action, schema in report_tool.fc_schemas().items():
            registry[f"report.{action}"] = {
                "handler": lambda _a=action, **kw: report_tool.dispatch(_a, **kw),
                "schema": schema,
            }
    return registry