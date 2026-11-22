"""进程级 owner 会话名额（M5-a B1 生命周期）。

单独成模块的原因：`opc_workbench.app` 以 `python -m` 执行时是 `__main__`
实例，与普通 `import opc_workbench.app` 是**两个模块实例**；名额状态若放在
app 模块内，入口重执行会得到一份新状态，单例约束即失效。本模块经正常导入
缓存（sys.modules），全进程只有一份状态。

名额只由真正持有 owner 会话的 Application 在 close() 时释放；
普通 build_application() 产物不占有、也不释放名额。
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_ACTIVE = False


def claim() -> None:
    """占用名额：已有活动 owner 会话时拒绝（每进程仅一个）。"""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE:
            raise PermissionError("owner_session_already_active")
        _ACTIVE = True


def release() -> None:
    """释放名额（幂等）。只应由持有者调用。"""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = False


def is_active() -> bool:
    with _LOCK:
        return _ACTIVE
