"""vault 文件层地基（M4 第一批：纯函数 + 受限文件 IO，零 DB、零网络）。

职责边界（M4.1 清单 §七 B2/B5、第一批边界）：
- 角色目录映射是 owner_role 的**唯一可信来源**，frontmatter 不得伪造归属；
- frontmatter 解析**严格 fail closed**：只认固定 5 键，拒绝重复键/未知键/
  缺键/类型错配/畸形分隔符（不引入 pyyaml，锁定依赖清单无它）；
- 路径安全：resolve 后必须仍在 vault 根内，拒绝 ../、绝对路径与符号链接逃逸；
- 原子写：同目录 tmp + os.replace，失败不留半成品；
- 快照比较是**纯函数**（diff_snapshots），不做任何文件系统 IO。

本模块**不实现** ingest / delete / search / watcher 线程，也不修改 app.py。
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from . import config, visibility

# --- 目录布局（PRD §5.8）----------------------------------------------------
# 8 员工 + personal 各有角色目录；shared 为全员可读共享区；attachments 存二进制。
ROLE_DIRS: tuple[str, ...] = (
    "customer_service", "sales", "cfo", "legal",
    "product", "research", "marketing", "hr", "personal",
)
SHARED_DIR = "shared"
SHARED_OWNER = "coordinator"          # 固定映射，文档不得自行伪造
ATTACHMENTS_DIR = "attachments"

# --- frontmatter 契约（固定 5 键，顺序即序列化顺序）-------------------------
FM_KEYS: tuple[str, ...] = ("owner_role", "visibility", "scope", "file_hash", "updated_at")

_DELIM = "---"
_KEY_RE = re.compile(r"^[a-z_]+$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class FrontmatterError(ValueError):
    """frontmatter 非法（一切非法形态统一 fail closed）。"""


def _validate_meta(meta: dict) -> None:
    missing = [k for k in FM_KEYS if k not in meta]
    if missing:
        raise FrontmatterError(f"frontmatter_missing_key:{missing[0]}")
    extra = [k for k in meta if k not in FM_KEYS]
    if extra:
        raise FrontmatterError(f"frontmatter_unknown_key:{extra[0]}")
    if meta["owner_role"] not in config.ROLES:
        raise FrontmatterError(f"frontmatter_invalid_value:owner_role")
    if meta["visibility"] not in ("public", "private"):
        raise FrontmatterError("frontmatter_invalid_value:visibility")
    if meta["scope"] not in ("global", "session"):
        raise FrontmatterError("frontmatter_invalid_value:scope")
    if not _HASH_RE.match(meta["file_hash"]):
        raise FrontmatterError("frontmatter_invalid_value:file_hash")
    try:
        stamp = datetime.fromisoformat(meta["updated_at"])
    except (TypeError, ValueError):
        raise FrontmatterError("frontmatter_invalid_value:updated_at") from None
    if stamp.tzinfo is None:                 # 时间统一 aware（CLAUDE.md 规范）
        raise FrontmatterError("frontmatter_invalid_value:updated_at")


def validate_doc_meta(meta: dict) -> None:
    """落库前的语义校验：复用 M2 治理规则，session 不可持久化。"""
    _validate_meta(meta)
    visibility.validate_persist(meta["owner_role"], meta["visibility"], meta["scope"])


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """严格解析 md 头部 YAML 子集 → (meta, body)。任何非法形态抛 FrontmatterError。"""
    if not isinstance(text, str):
        raise FrontmatterError("frontmatter_malformed")
    lines = text.split("\n")
    if not lines or lines[0].strip() != _DELIM:
        raise FrontmatterError("frontmatter_malformed")
    meta: dict[str, str] = {}
    end = None
    for i in range(1, len(lines)):
        raw = lines[i]
        if raw.strip() == _DELIM:
            end = i
            break
        if raw.strip() == "":
            raise FrontmatterError("frontmatter_malformed")
        if raw != raw.strip() or "\t" in raw:
            raise FrontmatterError("frontmatter_malformed")
        if raw.count(":") < 1:
            raise FrontmatterError("frontmatter_malformed")
        key, _, value = raw.partition(":")
        if not _KEY_RE.match(key):
            raise FrontmatterError("frontmatter_malformed")
        if not value.startswith(" ") or value.strip() == "":
            raise FrontmatterError("frontmatter_malformed")
        value = value[1:]
        if value == "" or value != value.strip():
            raise FrontmatterError("frontmatter_malformed")
        if key in ("owner_role", "visibility", "scope") and ":" in value:
            raise FrontmatterError("frontmatter_malformed")
        if key in meta:
            raise FrontmatterError(f"frontmatter_duplicate_key:{key}")
        if key not in FM_KEYS:
            raise FrontmatterError(f"frontmatter_unknown_key:{key}")
        meta[key] = value
    if end is None:
        raise FrontmatterError("frontmatter_malformed")
    _validate_meta(meta)
    body = "\n".join(lines[end + 1:])
    return meta, body


def serialize_frontmatter(meta: dict, body: str) -> str:
    """按固定键序序列化；先校验，非法不产出文本。"""
    _validate_meta(meta)
    head = "\n".join(f"{k}: {meta[k]}" for k in FM_KEYS)
    return f"{_DELIM}\n{head}\n{_DELIM}\n{body}"


def resolve_safe_path(root: Path | str, rel: str) -> Path:
    """把 vault 相对路径解析为根内绝对路径；越界一律拒绝。"""
    if not isinstance(rel, str) or rel == "":
        raise ValueError("vault_path_unsafe")
    if "\x00" in rel:
        raise ValueError("vault_path_unsafe")
    p = Path(rel)
    if p.is_absolute() or p.drive or rel.startswith(("/", "\\")):
        raise ValueError("vault_path_unsafe")
    if any(part == ".." for part in p.parts):
        raise ValueError("vault_path_unsafe")
    root_abs = Path(root).resolve()
    target = (root_abs / p).resolve()        # resolve 跟随符号链接 → 逃逸在此暴露
    if target != root_abs and not target.is_relative_to(root_abs):
        raise ValueError("vault_path_unsafe")
    return target


def owner_for_relpath(rel: str) -> str:
    """目录映射 → owner_role（唯一可信来源）。attachments 与未知目录拒绝。"""
    parts = Path(rel).parts
    if not parts:
        raise ValueError("vault_owner_undetermined")
    top = parts[0]
    if top == SHARED_DIR:
        return SHARED_OWNER
    if top in ROLE_DIRS:
        return top
    raise ValueError("vault_owner_undetermined")


def atomic_write(path: Path | str, text: str) -> None:
    """同目录 tmp + os.replace 原子落盘；失败清理 tmp，不留半成品。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def diff_snapshots(old: dict, new: dict) -> dict:
    """纯函数：比较两次文件快照 {rel: (mtime, size)}，不做任何 IO。

    返回 added / removed / changed 三个**有序**列表；mtime 或 size 任一变化
    即视为候选变更（真实是否重索引由后续 hash 复核决定，属第二批）。
    """
    old_keys, new_keys = set(old), set(new)
    changed = [k for k in old_keys & new_keys if tuple(old[k]) != tuple(new[k])]
    return {
        "added": sorted(new_keys - old_keys),
        "removed": sorted(old_keys - new_keys),
        "changed": sorted(changed),
    }