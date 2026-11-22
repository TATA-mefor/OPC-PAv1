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

import ntpath
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import audit_log, config, visibility

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
    if not isinstance(meta, dict):
        raise FrontmatterError("frontmatter_invalid_type:meta")
    missing = [k for k in FM_KEYS if k not in meta]
    if missing:
        raise FrontmatterError(f"frontmatter_missing_key:{missing[0]}")
    extra = [k for k in meta if k not in FM_KEYS]
    if extra:
        raise FrontmatterError(f"frontmatter_unknown_key:{extra[0]}")
    # 公开入口（serialize / validate_doc_meta）不依赖调用者传入正确类型：
    # 五个字段一律先做类型校验，非字符串统一 FrontmatterError，绝不漏原生 TypeError
    for k in FM_KEYS:
        if not isinstance(meta[k], str):
            raise FrontmatterError(f"frontmatter_invalid_type:{k}")
    if meta["owner_role"] not in config.ROLES:
        raise FrontmatterError("frontmatter_invalid_value:owner_role")
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
    """严格解析 md 头部 YAML 子集 → (meta, body)。任何非法形态抛 FrontmatterError。

    换行口径：先把 CRLF / CR 归一为 LF 再解析（兼容 Windows 编辑器与
    Obsidian 保存的 md），返回的 body 一律使用 LF。
    分隔符口径：起止行必须**精确等于** '---'，带空白的 ' --- ' 一律拒绝。
    """
    if not isinstance(text, str):
        raise FrontmatterError("frontmatter_malformed")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0] != _DELIM:
        raise FrontmatterError("frontmatter_malformed")
    meta: dict[str, str] = {}
    end = None
    for i in range(1, len(lines)):
        raw = lines[i]
        if raw == _DELIM:
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
    if not isinstance(body, str):
        raise FrontmatterError("frontmatter_invalid_type:body")
    head = "\n".join(f"{k}: {meta[k]}" for k in FM_KEYS)
    return f"{_DELIM}\n{head}\n{_DELIM}\n{body}"


def resolve_safe_path(root: Path | str, rel: str) -> Path:
    """把 vault 相对路径解析为根内绝对路径；越界一律拒绝。

    **平台无关**：不依赖本机 Path  flavour。Windows 驱动器/UNC 前缀用
    ntpath.splitdrive 识别（在 Linux 上 PurePosixPath 不会识别 C:\\），
    反斜杠统一按分隔符处理，避免同一路径在两个平台判定不一致。
    """
    if not isinstance(rel, str) or rel == "":
        raise ValueError("vault_path_unsafe")
    if "\x00" in rel:
        raise ValueError("vault_path_unsafe")
    # 驱动器号 / UNC / NTFS 备用数据流（"a.md:stream"）一律拒绝
    if ntpath.splitdrive(rel)[0]:
        raise ValueError("vault_path_unsafe")
    normalized = rel.replace("\\", "/")
    # 冒号一律拒绝：驱动器前缀与 NTFS 备用数据流（ntpath 只认单字母驱动器）
    if ":" in normalized:
        raise ValueError("vault_path_unsafe")
    if normalized.startswith("/"):
        raise ValueError("vault_path_unsafe")
    parts = PurePosixPath(normalized).parts
    if any(part == ".." for part in parts):
        raise ValueError("vault_path_unsafe")
    root_abs = Path(root).resolve()
    target = (root_abs / PurePosixPath(normalized)).resolve()   # 跟随符号链接 → 逃逸在此暴露
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


def load_doc(root: Path | str, rel: str, text: str) -> dict:
    """**唯一组合入口**：路径校验 → 解析 → 目录映射定归属 → 落库前语义校验。

    B2 红线：owner_role 的唯一可信来源是目录映射。frontmatter 声明与目录
    不符时**以目录为准**，记 WARN 并回显，绝不让文件内容决定归属。
    调用方（第二批 ingest / watcher）必须经本入口，不得单独用 parse_frontmatter
    的结果直接落库。

    返回 {"meta", "body", "owner_corrected", "declared_owner"}。
    """
    resolve_safe_path(root, rel)                 # 非法路径先拒（含 .. 与逃逸）
    meta, body = parse_frontmatter(text)
    dir_owner = owner_for_relpath(rel)           # attachments/未知目录在此拒绝
    declared = meta["owner_role"]
    corrected = declared != dir_owner
    if corrected:
        audit_log.log("WARN", "frontmatter_owner_mismatch",
                      target_path=rel, declared_owner=declared, directory_owner=dir_owner)
        meta["owner_role"] = dir_owner
    validate_doc_meta(meta)                      # 用归一化后的归属做持久化校验
    return {"meta": meta, "body": body,
            "owner_corrected": corrected, "declared_owner": declared}


def atomic_write(path: Path | str, text: str) -> None:
    """同目录 tmp + os.replace 原子落盘；失败清理 tmp，不留半成品。

    耐久性口径：对文件内容 fsync 后原子替换，保证**读者不会看到半截文件**；
    **未对父目录 fsync**，因此不宣称具备完整断电持久性保证。
    """
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