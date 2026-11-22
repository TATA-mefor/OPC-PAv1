"""全局配置：角色单一来源、路径、硬上限。密钥只从 .env 读。

角色集（PRD §1.3，用户两次纠正后定案）：
8 类公司数字员工（标识沿用 saaopc-demo）+ personal 个人助理 + coordinator 总管家。
ROLES 是角色枚举的唯一来源，db.py 的 DDL CHECK 由此生成，禁止两处手写漂移。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # OPC-PAv1/
load_dotenv(PROJECT_ROOT / ".env", override=False)

# ---- 密钥与模型（变量名沿用旧项目，不得改） ----
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "").strip()
DASHSCOPE_BASE_URL = os.getenv(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
).strip()
DASHSCOPE_MODEL = os.getenv("DASHSCOPE_MODEL", "qwen-plus").strip()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v4").strip()
# M4 第二批 C5（用户裁决 2026-11-22）：维度固定 1024；模型/维度变化由老板
# 显式触发重建，不自动全量调用 API。批上限 10 条/请求为 text-embedding-v4 规格。
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
EMBED_BATCH_SIZE = 10
KNOWLEDGE_TOP_K = 5           # 检索默认返回块数
KNOWLEDGE_MIN_SCORE = 0.35    # 余弦阈值，低于视为未命中（PRD §5.3）
MISSING_RETENTION_DAYS = 7    # watcher 判定消失后索引保留天数（PRD §5.3）

# ---- 路径（测试经 fixture 重定向，业务代码一律引用本模块常量） ----
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.getenv("PA_DB_PATH", str(DATA_DIR / "personal_agent.db")))
VAULT_PATH = Path(os.getenv("VAULT_PATH", str(PROJECT_ROOT / "vault")))
LOG_DIR = PROJECT_ROOT / "logs"
BACKUP_DIR = PROJECT_ROOT / "backups"

# ---- 硬上限（沿用旧演示防失控参数，PRD §2.3/§15.4） ----
MAX_HANDOFFS = 5          # 单轮最多 5 次角色接力
MAX_TOOL_STEPS = 8        # 单角色工具循环上限（技术文档 §3）
LLM_TIMEOUT_S = 30.0      # 单次 LLM 调用 30s
ROUND_TIMEOUT_S = 90.0    # 整轮 90s 封顶
LLM_ATTEMPTS = 2          # 1 次重试，仅对可重试错误
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
DB_BUSY_TIMEOUT_MS = 5000
REMINDER_SCAN_INTERVAL_S = int(os.getenv("REMINDER_SCAN_INTERVAL_S", "30"))
VAULT_SCAN_INTERVAL_S = int(os.getenv("VAULT_SCAN_INTERVAL_S", "30"))

# ---- 角色单一来源（PRD §1.3 / 技术文档 §2） ----
# default_visibility 为可调假设：cfo/legal/hr/personal 默认私密，其余公开。
ROLES: dict[str, dict] = {
    "customer_service": {
        "name": "智能客服", "avatar": "🤖", "group": "employee",
        "duty": "产品咨询 / 工单 / FAQ",
        "persona": "耐心的产品专家",
        "default_visibility": "public", "color": "#2b6bab",
        "keywords": ["怎么用", "怎么使用", "功能", "是什么", "介绍", "教程", "FAQ", "客服", "工单", "支持"],
    },
    "sales": {
        "name": "销售顾问", "avatar": "💼", "group": "employee",
        "duty": "需求分析 / 报价 / 客户线索",
        "persona": "敏锐的成交顾问",
        "default_visibility": "public", "color": "#e8833a",
        "keywords": ["价格", "报价", "多少钱", "套餐", "购买", "预算", "订阅", "客户", "成交", "毛利"],
    },
    "cfo": {
        "name": "财务 CFO", "avatar": "💰", "group": "employee",
        "duty": "现金流 / 经营报表 / 预算",
        "persona": "严谨的账房先生",
        "default_visibility": "private", "color": "#3f8f6b",
        "keywords": ["收入", "营收", "成本", "利润", "现金流", "财务", "核算", "盈亏", "预算"],
    },
    "legal": {
        "name": "法务顾问", "avatar": "⚖️", "group": "employee",
        "duty": "合同审查 / 合规 / 风险",
        "persona": "审慎的合规守门人",
        "default_visibility": "private", "color": "#7b61a8",
        "keywords": ["合同", "审查", "法务", "合规", "条款", "风险", "知识产权", "违约"],
    },
    "product": {
        "name": "产品经理", "avatar": "📊", "group": "employee",
        "duty": "指标 / PRD / VOC / 路线图",
        "persona": "数据驱动的产品经理",
        "default_visibility": "public", "color": "#c2557a",
        "keywords": ["指标", "Dashboard", "PRD", "需求", "规划", "反馈", "NPS", "路线图"],
    },
    "research": {
        "name": "研发 Lead", "avatar": "💻", "group": "employee",
        "duty": "Sprint / 代码质量 / 发布",
        "persona": "务实的研发负责人",
        "default_visibility": "public", "color": "#4a7fb5",
        "keywords": ["Sprint", "研发", "部署", "代码", "测试", "质量", "发布", "技术债"],
    },
    "marketing": {
        "name": "市场运营", "avatar": "✍️", "group": "employee",
        "duty": "内容 / MQL / SEO / 推广",
        "persona": "活跃的增长操盘手",
        "default_visibility": "public", "color": "#d08a2c",
        "keywords": ["MQL", "线索", "SEO", "市场", "内容", "推广", "曝光", "转化"],
    },
    "hr": {
        "name": "人力效能", "avatar": "👥", "group": "employee",
        "duty": "排期 / 招聘 / 培训 / 外包",
        "persona": "周到的组织管家",
        "default_visibility": "private", "color": "#61876b",
        "keywords": ["排期", "人事", "外包", "招聘", "培训", "归档"],
    },
    "personal": {
        "name": "个人助理", "avatar": "🏠", "group": "boss_direct",
        "duty": "老板私人事务：个人日程 / 生活任务 / 私密备忘 / 健康家庭",
        "persona": "贴心守密的私人管家，老板私事绝不外传",
        "default_visibility": "private", "color": "#8a6d4f",
        "keywords": ["个人", "家里", "体检", "生日", "纪念日", "私事", "提醒我", "生活"],
    },
    "coordinator": {
        "name": "Coordinator 总管家", "avatar": "🎩", "group": "system",
        "duty": "意图识别 / 路由 / 跨角色接力编排 / 兜底直答",
        "persona": "只做路由与兜底的总管家",
        "default_visibility": "public", "color": "#334155",
        "keywords": [],  # 兜底角色，不参与关键词竞争
    },
}

ROLE_ORDER = list(ROLES)
ROLE_NAMES = {k: v["name"] for k, v in ROLES.items()}
DEFAULT_PRIVATE_ROLES = frozenset(
    k for k, v in ROLES.items() if v["default_visibility"] == "private"
)

# 可信 Agent 注册表（M5-a B2）：agent_id → 角色。
# 只由框架/部署方在启动前写入（组合根读取快照）；业务调用方不能通过任何
# 接口追加条目或指定角色。v1 还没有真实员工 Agent 进程，故默认空表：
# 生产里不预置任何角色通道，调用方也就拿不到 cfo/sales 的身份句柄。
AGENT_ROLES: dict[str, str] = {}


# 敏感词升级表（PRD §4.2 规则2，M2.1 v3 §四.7）：命中即 private，宁可错杀，
# 老板可显式改回 public。启发式词表，可调；不追求完备，角色默认兜底。
SENSITIVE_HINTS = (
    "病", "诊断", "药", "体检", "手术", "心理",
    "工资", "存款", "账号", "密码", "银行卡", "身份证",
    "分手", "离婚", "情感", "私事",
)


def role_label(role: str) -> str:
    info = ROLES.get(role)
    return f"{info['avatar']} {info['name']}" if info else role


def deterministic_route(text: str) -> str:
    """无 LLM 时的关键词路由降级链（沿用旧机制，coordinator 兜底）。"""
    best, score = "coordinator", 0
    for key, info in ROLES.items():
        if key == "coordinator":
            continue
        hit = sum(1 for kw in info["keywords"] if kw in text)
        if hit > score:
            best, score = key, hit
    return best
