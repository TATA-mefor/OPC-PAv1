"""主窗口适配器（M5-a 契约，M6 接 Gradio）。

启动契约（用户裁决 2026-11-23）：
- 适配器由工作台启动流程**内部创建**；外部脚本不能把适配器塞给启动函数。
- 框架只经 `bind(submit)` 交付受限提交函数；不交付 Dispatcher、Identity、
  来源令牌或工具注册表。
- `send(action, params)` 是主聊天窗口的提交面；未绑定时不可用，绑定后
  不可换绑（防止运行期替换提交端口）。

本类自身不铸造任何身份：直接构造得到的是未绑定空壳，`send` 拒绝；
`bind` 需要框架铸造的提交端口，普通代码无从取得。
"""
from __future__ import annotations


class MainWindowAdapter:
    """主聊天窗口适配器：只持有框架注入的提交端口。"""

    __slots__ = ("_submit",)

    def __init__(self):
        object.__setattr__(self, "_submit", None)

    def __setattr__(self, *a):
        raise PermissionError("main_window_adapter_immutable")

    def bind(self, submit) -> None:
        """框架专用：注入提交端口（只允许一次）。"""
        if not callable(submit):
            raise TypeError("submit_port_must_be_callable")
        if object.__getattribute__(self, "_submit") is not None:
            raise PermissionError("main_window_already_bound")
        object.__setattr__(self, "_submit", submit)

    def send(self, action: str, params: dict | None = None) -> dict:
        submit = object.__getattribute__(self, "_submit")
        if submit is None:
            raise PermissionError("main_window_not_bound")
        return submit(action, params)
