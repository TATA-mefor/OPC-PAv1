"""M4 第一批：vault 文件层地基（frontmatter 严格解析 / 路径安全 / 原子写 / 快照纯函数）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from opc_workbench import vault


def _meta(**over):
    base = {
        "owner_role": "cfo",
        "visibility": "private",
        "scope": "global",
        "file_hash": "sha256:" + "a" * 64,
        "updated_at": "2026-11-22T10:00:00+08:00",
    }
    base.update(over)
    return base


def _doc(**over):
    return vault.serialize_frontmatter(_meta(**over), "正文内容\n")


# --- frontmatter 往返与严格拒绝 -------------------------------------------

def test_frontmatter_roundtrip():
    meta, body = vault.parse_frontmatter(_doc())
    assert meta == _meta()
    assert body == "正文内容\n"


def test_frontmatter_body_kept_verbatim():
    text = vault.serialize_frontmatter(_meta(), "# 标题\n\n段落一\n段落二\n")
    _m, body = vault.parse_frontmatter(text)
    assert body == "# 标题\n\n段落一\n段落二\n"


@pytest.mark.parametrize("bad", [
    "",                                   # 空
    "no frontmatter here\n",              # 无起始分隔符
    "---\nowner_role: cfo\n",             # 无结束分隔符
    "---\n\nowner_role: cfo\n---\n",      # 块内空行
    "---\nowner_role:cfo\n---\n",         # 冒号后无空格
    "---\nowner_role: \n---\n",           # 空值
    "---\n owner_role: cfo\n---\n",       # 缩进
    "---\nowner_role:\tcfo\n---\n",       # 制表符
])
def test_frontmatter_malformed_rejected(bad):
    with pytest.raises(vault.FrontmatterError):
        vault.parse_frontmatter(bad)


def test_frontmatter_duplicate_key_rejected():
    text = _doc().replace("visibility: private", "visibility: private\nvisibility: public")
    with pytest.raises(vault.FrontmatterError, match="duplicate_key"):
        vault.parse_frontmatter(text)


def test_frontmatter_unknown_key_rejected():
    text = _doc().replace("scope: global", "scope: global\nis_admin: true")
    with pytest.raises(vault.FrontmatterError, match="unknown_key"):
        vault.parse_frontmatter(text)


def test_frontmatter_missing_key_rejected():
    text = "\n".join(l for l in _doc().split("\n") if not l.startswith("scope:"))
    with pytest.raises(vault.FrontmatterError, match="missing_key"):
        vault.parse_frontmatter(text)


@pytest.mark.parametrize("over", [
    {"owner_role": "boss"},                       # 非 config.ROLES 角色
    {"owner_role": "hacker"},
    {"visibility": "internal"},
    {"visibility": "PUBLIC"},
    {"scope": "team"},
    {"file_hash": "md5:abc"},
    {"file_hash": "sha256:" + "z" * 64},
    {"updated_at": "2026-11-22T10:00:00"},        # naive，无时区
    {"updated_at": "昨天"},
])
def test_frontmatter_invalid_value_rejected(over):
    with pytest.raises(vault.FrontmatterError, match="invalid_value"):
        vault.parse_frontmatter(_doc(**over))


def test_serialize_rejects_invalid_meta():
    with pytest.raises(vault.FrontmatterError):
        vault.serialize_frontmatter(_meta(visibility="internal"), "x")


def test_doc_meta_rejects_session_scope():
    """落库语义校验复用 M2 规则：session 不可持久化。"""
    vault.validate_doc_meta(_meta())              # global 合法
    with pytest.raises(ValueError, match="session_not_persistable"):
        vault.validate_doc_meta(_meta(scope="session"))


# --- 目录映射（owner_role 唯一可信来源）-----------------------------------

@pytest.mark.parametrize("rel,expected", [
    ("cfo/report.md", "cfo"),
    ("sales/a/b.md", "sales"),
    ("personal/note.md", "personal"),
    ("shared/policy.md", "coordinator"),
])
def test_owner_from_directory(rel, expected):
    assert vault.owner_for_relpath(rel) == expected


@pytest.mark.parametrize("rel", ["attachments/x.pdf", "unknown/x.md", "", "x.md"])
def test_owner_undetermined_rejected(rel):
    with pytest.raises(ValueError, match="vault_owner_undetermined"):
        vault.owner_for_relpath(rel)


# --- 路径安全 ---------------------------------------------------------------

def test_safe_path_normal(tmp_path):
    got = vault.resolve_safe_path(tmp_path, "cfo/a.md")
    assert got == (tmp_path / "cfo" / "a.md").resolve()
    assert got.is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("rel", [
    "../escape.md", "cfo/../../escape.md", "..", "cfo/..",
    "/etc/passwd", "\\windows\\system32", "C:\\evil.txt", "", "cfo/\x00.md",
])
def test_unsafe_path_rejected(tmp_path, rel):
    with pytest.raises(ValueError, match="vault_path_unsafe"):
        vault.resolve_safe_path(tmp_path, rel)


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "vault"          # isolated_env 已建
    root.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.md").write_text("机密", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows 无符号链接权限时用目录联接（junction）兜底，无需管理员
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                           capture_output=True)
        if r.returncode != 0 or not link.exists():
            pytest.skip("本机既无符号链接权限也无法创建目录联接")
    with pytest.raises(ValueError, match="vault_path_unsafe"):
        vault.resolve_safe_path(root, "link/secret.md")


# --- 原子写 -----------------------------------------------------------------

def test_atomic_write_creates_and_overwrites(tmp_path):
    target = tmp_path / "cfo" / "a.md"
    vault.atomic_write(target, "第一版")
    assert target.read_text(encoding="utf-8") == "第一版"
    vault.atomic_write(target, "第二版")
    assert target.read_text(encoding="utf-8") == "第二版"
    assert list(target.parent.glob(".tmp-*")) == []      # 无残留 tmp


def test_atomic_write_failure_leaves_no_partial(tmp_path, monkeypatch):
    target = tmp_path / "a.md"
    vault.atomic_write(target, "原始")

    def boom(*a, **kw):
        raise OSError("replace_failed")

    monkeypatch.setattr(vault.os, "replace", boom)
    with pytest.raises(OSError, match="replace_failed"):
        vault.atomic_write(target, "新内容")
    assert target.read_text(encoding="utf-8") == "原始"   # 原文件未被破坏
    assert list(tmp_path.glob(".tmp-*")) == []            # tmp 已清理


# --- 快照比较（纯函数，无 IO）-----------------------------------------------

def test_diff_snapshots_categorizes():
    old = {"a.md": (1.0, 10), "b.md": (2.0, 20), "c.md": (3.0, 30)}
    new = {"b.md": (2.0, 20), "c.md": (9.0, 30), "d.md": (4.0, 40)}
    assert vault.diff_snapshots(old, new) == {
        "added": ["d.md"], "removed": ["a.md"], "changed": ["c.md"],
    }


def test_diff_snapshots_size_only_change_detected():
    assert vault.diff_snapshots({"a": (1.0, 10)}, {"a": (1.0, 11)})["changed"] == ["a"]


def test_diff_snapshots_identical_is_empty():
    snap = {"a": (1.0, 10), "b": (2.0, 20)}
    assert vault.diff_snapshots(snap, dict(snap)) == {"added": [], "removed": [], "changed": []}


def test_diff_snapshots_is_pure_and_sorted():
    old, new = {"z": (1, 1)}, {"m": (1, 1), "a": (1, 1)}
    before_old, before_new = dict(old), dict(new)
    out = vault.diff_snapshots(old, new)
    assert out["added"] == ["a", "m"]                    # 有序，便于确定性断言
    assert old == before_old and new == before_new       # 不改入参

# --- 阻断修复 1：owner_role 必须由目录映射约束（组合入口 load_doc）-----------

def test_load_doc_directory_overrides_frontmatter_owner(tmp_path):
    """cfo/a.md 里写 owner_role: sales → 最终归属必须是 cfo。"""
    out = vault.load_doc(tmp_path, "cfo/a.md", _doc(owner_role="sales"))
    assert out["meta"]["owner_role"] == "cfo"
    assert out["owner_corrected"] is True
    assert out["declared_owner"] == "sales"


def test_load_doc_shared_maps_to_coordinator(tmp_path):
    out = vault.load_doc(tmp_path, "shared/p.md", _doc(owner_role="sales", visibility="public"))
    assert out["meta"]["owner_role"] == "coordinator"
    assert out["owner_corrected"] is True


def test_load_doc_matching_owner_not_corrected(tmp_path):
    out = vault.load_doc(tmp_path, "cfo/a.md", _doc())
    assert out["meta"]["owner_role"] == "cfo"
    assert out["owner_corrected"] is False


def test_load_doc_result_passes_persist_validation(tmp_path):
    """后续持久化校验使用的是目录归一化后的归属。"""
    out = vault.load_doc(tmp_path, "shared/p.md", _doc(owner_role="cfo", visibility="public"))
    vault.validate_doc_meta(out["meta"])          # 不抛
    assert out["meta"]["owner_role"] == "coordinator"


def test_load_doc_rejects_traversal_before_mapping(tmp_path):
    """shared/../cfo/a.md 不得被映射成 coordinator，必须整条拒绝。"""
    with pytest.raises(ValueError, match="vault_path_unsafe"):
        vault.load_doc(tmp_path, "shared/../cfo/a.md", _doc())


@pytest.mark.parametrize("rel", ["../x.md", "/abs/x.md", "attachments/x.pdf", "unknown/x.md"])
def test_load_doc_rejects_illegal_paths(tmp_path, rel):
    with pytest.raises(ValueError):
        vault.load_doc(tmp_path, rel, _doc())


def test_load_doc_rejects_session_scope_after_normalization(tmp_path):
    with pytest.raises(ValueError, match="session_not_persistable"):
        vault.load_doc(tmp_path, "cfo/a.md", _doc(scope="session"))


def test_load_doc_logs_warn_on_mismatch(tmp_path):
    from opc_workbench import config
    vault.load_doc(tmp_path, "cfo/a.md", _doc(owner_role="sales"))
    log = config.LOG_DIR / "audit.log"
    assert log.exists()
    assert "frontmatter_owner_mismatch" in log.read_text(encoding="utf-8")


# --- 阻断修复 2：非字符串字段统一 FrontmatterError，不漏原生 TypeError -------

@pytest.mark.parametrize("key,bad", [
    ("file_hash", None), ("file_hash", 123), ("file_hash", ["a"]), ("file_hash", b"x"),
    ("owner_role", None), ("owner_role", 7),
    ("visibility", None), ("visibility", ["private"]),
    ("scope", 1), ("updated_at", None), ("updated_at", 20261122),
])
def test_non_string_field_rejected_in_validate(key, bad):
    with pytest.raises(vault.FrontmatterError, match="invalid_type"):
        vault.validate_doc_meta(_meta(**{key: bad}))


@pytest.mark.parametrize("key,bad", [
    ("file_hash", None), ("file_hash", 123), ("owner_role", None), ("updated_at", 3.14),
])
def test_non_string_field_rejected_in_serialize(key, bad):
    with pytest.raises(vault.FrontmatterError, match="invalid_type"):
        vault.serialize_frontmatter(_meta(**{key: bad}), "正文")


def test_non_dict_meta_rejected():
    with pytest.raises(vault.FrontmatterError, match="invalid_type"):
        vault.validate_doc_meta(["not", "a", "dict"])


def test_non_string_body_rejected():
    with pytest.raises(vault.FrontmatterError, match="invalid_type"):
        vault.serialize_frontmatter(_meta(), None)


# --- 非阻断项修正：分隔符精确匹配 / CRLF 兼容 --------------------------------

@pytest.mark.parametrize("bad", [" --- \n", "--- \n", " ---\n"])
def test_delimiter_must_match_exactly(bad):
    text = bad + "\n".join(f"{k}: {v}" for k, v in _meta().items()) + "\n---\n正文"
    with pytest.raises(vault.FrontmatterError, match="malformed"):
        vault.parse_frontmatter(text)


def test_closing_delimiter_must_match_exactly():
    text = _doc().replace("\n---\n正文内容", "\n --- \n正文内容")
    with pytest.raises(vault.FrontmatterError, match="malformed"):
        vault.parse_frontmatter(text)


def test_crlf_input_accepted_and_body_normalized():
    text = _doc().replace("\n", "\r\n")
    meta, body = vault.parse_frontmatter(text)
    assert meta == _meta()
    assert body == "正文内容\n"          # 归一为 LF
    assert "\r" not in body


def test_lone_cr_input_accepted():
    text = _doc().replace("\n", "\r")
    meta, body = vault.parse_frontmatter(text)
    assert meta == _meta()
    assert "\r" not in body
