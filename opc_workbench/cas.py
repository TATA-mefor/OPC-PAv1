"""CAS 乐观锁写服务（M3.1 清单 v4/v9/v10.1/v10.2 定稿实现）。

核心纪律：
- 读可见 ≠ 写授权：read_where ≡ M2 visible_filter；write_where ≡ PRD §3.2
  写矩阵（boss 全读写 / coordinator 全只读 / 员工仅自有）。二者是纯 SQL
  片段函数，**不对外作为授权入口**——授权判定唯一发生在注入的 authorize。
- CASService 实例化：构造期同时固定 authorize 与 writer（WriteCapability），
  无 setter/rebind、无 bind()、无模块级绑定槽、无模块级可变服务变量。
  写 SQL 只经注入 writer，绝不 import db 取共享连接（v10.2）。
- 配对校验：生产 writer 与 authorize 必须同属一次组合根装配
  （writer._bind_authorize 一次性绑定；伪造 authorize 配生产 writer 构造即拒）。
- 治理字段（id/version/owner_role/scope/created_at）禁改；version 仅原语递增；
  授权条件纳入最终 UPDATE/DELETE 的 WHERE（封堵"查询时 public、并发改
  private"窗口）；BEGIN IMMEDIATE 单事务；rowcount=0 事务内诊断分流
  not_found 同构 / version_conflict。

安全依据 = 对象所有权与依赖注入（非命名/导入顺序/静态检查）。
诚实边界：进程内 Python 无硬安全边界；本模块封死正常应用架构中的
伪造/挪用/抢占路径，不抵御本机恶意代码（威胁模型外）。
"""
from __future__ import annotations

from . import audit_log, db

# ---------------------------------------------------------------------------
# 表/列双白名单（治理字段不在任何白名单）
# ---------------------------------------------------------------------------

_UPDATABLE: dict[str, frozenset[str]] = {
    "tasks": frozenset({"title", "detail", "status", "priority", "due_at", "visibility"}),
    "schedules": frozenset({"title", "remind_at", "repeat_rule", "status", "visibility"}),
    "artifacts": frozenset({"title", "content_md", "refs_json"}),
}

# cas_delete 允许表（删除是写操作，一律 write_where）
_DELETABLE = frozenset({"tasks", "schedules", "artifacts"})


class CASNotFound(Exception):
    """不存在 / 不可见 / 不可写 → 同构错误（不区分原因，防信息泄露）。"""

    def __init__(self):
        super().__init__("not_found")


class CASVersionConflict(Exception):
    """目标存在且可写，但 expected_version 不符。"""

    def __init__(self):
        super().__init__("version_conflict")


# ---------------------------------------------------------------------------
# 纯 SQL 片段（入参是已校验的 role 字符串；授权判定不在此）
# ---------------------------------------------------------------------------

def read_where(caller: str) -> tuple[str, list]:
    """读可见 ≡ M2 visible_filter：boss 全可见；coordinator 仅 public
    （含自有）；其他 Agent 自有 public+private、他角色仅 public。"""
    return ("(?='boss' OR (?<>'coordinator' AND owner_role=?) OR visibility='public')",
            [caller, caller, caller])


def write_where(caller: str) -> tuple[str, list]:
    """写授权 ≡ PRD §3.2：boss 全部；coordinator 一律拒绝；其他仅自有。"""
    return ("(?='boss' OR (?<>'coordinator' AND owner_role=?))",
            [caller, caller, caller])


# ---------------------------------------------------------------------------
# CASService：写操作只能经实例执行
# ---------------------------------------------------------------------------

class CASService:
    """构造期同时固定 authorize 与 writer；无 setter/rebind。

    authorize：组合根注入的身份校验闭包（IdentityAPI.authorize），
      每次写入查签发登记 → 取登记中的真实 role。
    writer：WriteCapability 实例；生产 writer 构造期一次性配对绑定，
      伪造 authorize / 跨组合根 authorize 配生产 writer → 构造拒绝。
    """

    __slots__ = ("_authorize", "_writer")

    def __init__(self, *, authorize, writer: db.WriteCapability):
        if not callable(authorize):
            raise TypeError("cas_authorize_required")
        if not isinstance(writer, db.WriteCapability):
            raise TypeError("cas_writer_required")
        # 配对校验（v10.2 ⑥）：生产 writer 只接受同一组合根装配的 authorize。
        # 依据 = 闭包归属标记（IdentityAPI.make_authorize 产出，_pa_owner 指向
        # 签发 authority）+ writer 一次性绑定；伪造 lambda 无标记 → 构造即拒。
        # 测试 writer（cap_id 以 test: 开头）供测试组合根自由装配。
        if writer.cap_id == "production":
            owner = getattr(authorize, "_pa_owner", None)
            if owner is None:
                raise PermissionError("writer_requires_composition_root_authorize")
            bound = object.__getattribute__(writer, "_bound_authorize")
            if bound is not None and bound is not owner:
                raise PermissionError("writer_authorize_pairing_mismatch")
            writer._bind_authorize(owner)
        object.__setattr__(self, "_authorize", authorize)
        object.__setattr__(self, "_writer", writer)

    def __setattr__(self, *a):
        raise PermissionError("cas_service_immutable")

    # -- 写入原语 -----------------------------------------------------------

    def cas_update(self, table: str, target_id: int, expected_version: int,
                   *, ident, **fields) -> dict:
        if table not in _UPDATABLE:
            raise ValueError(f"forbidden_table:{table}")
        if not fields:
            raise ValueError("empty_update")
        allowed = _UPDATABLE[table]
        for key in fields:
            if key not in allowed:
                raise ValueError(f"forbidden_column:{table}.{key}")
        role = self._authorize(ident)          # 每次写入校验身份登记
        frag, params = write_where(role)
        sets = ",".join(f"{k}=?" for k in fields)   # 列名全部来自白名单常量
        values = list(fields.values())
        now = db.now_iso()
        writer = object.__getattribute__(self, "_writer")
        with writer.transaction() as cur:
            cur.execute(
                f"UPDATE {table} SET {sets}, version=version+1, updated_at=? "
                f"WHERE id=? AND version=? AND {frag}",
                values + [now, int(target_id), int(expected_version)] + params)
            if cur.rowcount == 0:
                self._diagnose(cur, table, target_id, expected_version, frag, params)
            row = cur.execute(
                f"SELECT * FROM {table} WHERE id=?", (int(target_id),)).fetchone()
        audit_log.log("INFO", "cas_update", actor_role=role,
                      target_table=table, target_id=target_id, result_code="ok")
        return dict(row)

    def cas_delete(self, table: str, target_id: int, expected_version: int,
                   *, ident) -> dict:
        if table not in _DELETABLE:
            raise ValueError(f"forbidden_table:{table}")
        role = self._authorize(ident)
        frag, params = write_where(role)
        writer = object.__getattribute__(self, "_writer")
        with writer.transaction() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE id=? AND version=? AND {frag}",
                [int(target_id), int(expected_version)] + params)
            if cur.rowcount == 0:
                self._diagnose(cur, table, target_id, expected_version, frag, params)
        audit_log.log("INFO", "cas_delete", actor_role=role,
                      target_table=table, target_id=target_id, result_code="ok")
        return {"deleted": True, "id": int(target_id)}

    @staticmethod
    def _diagnose(cur, table, target_id, expected_version, frag, params) -> None:
        """rowcount=0 事务内诊断：存在且可写 → 版本冲突；否则 not_found 同构。

        诊断 SELECT 与最终写 SQL 同一事务（BEGIN IMMEDIATE 写锁内），
        授权条件重新求值——诊断本身不泄露不可见行的存在性。
        """
        row = cur.execute(
            f"SELECT version FROM {table} WHERE id=? AND {frag}",
            [int(target_id)] + params).fetchone()
        if row is None:
            raise CASNotFound()
        if row["version"] != int(expected_version):
            raise CASVersionConflict()
        raise CASNotFound()   # 理论不可达（同事务内条件一致）；fail closed


def _build_cas_service(*, authorize, writer: db.WriteCapability) -> CASService:
    """组合根内部构造入口（app.main 与测试 fixture 调用）。"""
    return CASService(authorize=authorize, writer=writer)