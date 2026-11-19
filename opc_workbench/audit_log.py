"""最小审计日志器（M2.1 v3 §四.6）。

定性：**内部运维日志**——落本地 logs/，可读性由 OS 文件权限决定；
.gitignore 只防误提交，不构成角色访问控制。M2 不提供应用内日志查询接口；
M6 若提供日志查看 UI，该接口必须校验 actor=boss（预留声明，本轮不实现）。

红线：
- 字段白名单：白名单外字段一律丢弃（内容原文/Key/对话永不落盘）。
- 注入清洗：去控制字符/换行（防伪造日志行）、长度截断。
- 轮转 10MB×5。完整 trace_id 链路留 M5/M6。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
from datetime import datetime, timezone

from . import config

_ALLOWED_FIELDS = frozenset({
    "timestamp", "level", "actor_role", "action", "target_table",
    "target_id", "approval_id", "result_code", "source",
})
_LEVELS = frozenset({"INFO", "WARN", "ERROR"})
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_LEN = 200

_lock = threading.Lock()
_logger: logging.Logger | None = None
_logger_dir = None


def _clean(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return _CTRL.sub(" ", str(value))[:_MAX_LEN]


def _get_logger() -> logging.Logger:
    global _logger, _logger_dir
    with _lock:
        if _logger is None or _logger_dir != config.LOG_DIR:
            if _logger is not None:
                for h in list(_logger.handlers):
                    h.close()
                    _logger.removeHandler(h)
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            lg = logging.getLogger("pa.audit")
            lg.setLevel(logging.INFO)
            lg.propagate = False
            handler = logging.handlers.RotatingFileHandler(
                config.LOG_DIR / "audit.log",
                maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(handler)
            _logger, _logger_dir = lg, config.LOG_DIR
        return _logger


def log(level: str, action: str, **fields) -> None:
    """记一条审计元数据。level ∈ INFO/WARN/ERROR；白名单外字段丢弃。"""
    level = level.upper()
    if level not in _LEVELS:
        level = "INFO"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "level": level,
        "action": _clean(action),
    }
    for key, value in fields.items():
        # timestamp/level 为内部保留字段：调用参数不得覆盖（防元数据伪造）
        if key in _ALLOWED_FIELDS and key not in ("timestamp", "level"):
            record[key] = _clean(value)
    _get_logger().log(
        getattr(logging, "WARNING" if level == "WARN" else level),
        json.dumps(record, ensure_ascii=False),
    )
