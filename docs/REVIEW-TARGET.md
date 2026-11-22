# 审查入口（每次推送后更新，避免复审到过期快照）

**当前审查对象**

| 项 | 值 |
|---|---|
| 代码分支 | `feat/m4-knowledge` |
| 代码提交 | **`77c3a129be6ac468534f01c1e446775a1b87f05c`**（短 `77c3a12`） |
| 基线父链 | `b134ac9`（M3 基线） |
| 干净源码包 | `docs/OPC-PAv1-m4-batch1-77c3a12.zip` |
| 文档分支 | `docs/m41-checklist`（本文件所在分支） |
| 合并状态 | **未合入 master，未打 tag** |

**源码包性质**：由 `git archive --format=zip HEAD` 生成，**不含 `.git`、不含任何工作区改动**，解压即为该提交的干净快照（44 文件，含基线全部代码、全部测试、`requirements-personal-agent.txt`、`pytest.ini`、`.env.example`）。

**独立复跑**

```
pip install -r requirements-personal-agent.txt
python -m pytest tests/ -m "not live" -q
```

实现方本机（Windows / Python 3.14.4 / pytest 9.0.3）结果：**512 passed, 1 deselected**。
远程无 CI 检查记录，该数字不是 CI 或审查方独立验证结果。

**提交历史（本批）**

| 提交 | 内容 |
|---|---|
| `a6f8d5c` | 第一批初版（vault 地基 / 分块 / FakeLLM.embed），71 用例 |
| `1c12363` | 复审阻断修复：`load_doc` 目录归属组合入口、五字段类型统一校验、精确分隔符、CRLF 归一、耐久性口径 |
| `77c3a12` | 跨平台路径判定：修复 Linux 下 Windows 驱动器/UNC/反斜杠穿越/ADS 漏拒 |

> ⚠️ 若手上的 ZIP 解压后出现 `.git` 目录或检出分支为 `feat/m3-tools`，**那不是本批交付包**，请改用本文件指定的提交或源码包。

---

内容由 AI 生成