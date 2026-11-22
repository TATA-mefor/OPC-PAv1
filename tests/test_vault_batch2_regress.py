"""第一批 vault 接口的第二批回归补强：日志可追溯 + 目录映射平台一致。

这两条是第二批依赖审计实测发现的缺陷，属地基修正，不改已批准的分块口径。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from opc_workbench import config, vault
from opc_workbench.knowledge_service import body_hash


def _doc(owner_role="cfo", visibility="public"):
    meta = {"owner_role": owner_role, "visibility": visibility, "scope": "global",
            "file_hash": body_hash("正文"),
            "updated_at": datetime.now(timezone.utc).isoformat()}
    return vault.serialize_frontmatter(meta, "正文")


def test_owner_mismatch_log_carries_path_and_both_roles(tmp_path):
    """审计字段必须落在白名单内，否则出处被静默丢弃、无法追溯。"""
    vault.load_doc(tmp_path, "cfo/a.md", _doc(owner_role="sales"))
    log = config.LOG_DIR / "audit.log"
    assert log.exists()
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x]
    rec = [r for r in lines if r["action"] == "frontmatter_owner_mismatch"][-1]
    assert rec["source"] == "cfo/a.md"          # 出处
    assert rec["actor_role"] == "cfo"           # 目录归属（可信来源）
    assert "sales" in rec["result_code"]        # frontmatter 谎报值


@pytest.mark.parametrize("rel,expected", [
    ("cfo/a.md", "cfo"),
    ("cfo\\a.md", "cfo"),          # 反斜杠在两个平台都必须映射为 cfo
    ("shared\\s.md", "coordinator"),
    ("personal/p.md", "personal"),
])
def test_owner_mapping_is_platform_consistent(rel, expected):
    assert vault.owner_for_relpath(rel) == expected


@pytest.mark.parametrize("rel", ["attachments\\x.md", "unknown\\y.md", "\\cfo\\a.md"])
def test_owner_mapping_still_rejects_non_owner_dirs(rel):
    with pytest.raises(ValueError, match="vault_owner_undetermined"):
        vault.owner_for_relpath(rel)