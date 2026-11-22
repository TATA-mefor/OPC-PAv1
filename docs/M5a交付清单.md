# M5-a 组合根接线与统一调度入口 — 交付清单

分支：`feat/m5-a-dispatch`（新建，已推送）
提交：`53627c1`（合并 M3 writer boundary）→ `cfc321a`（M5-a 本体）
基线：`62ff27a`（M4.2 验收通过的 `feat/m4-ingest` 头）
`feat/m4-ingest` 与 `docs/m41-checklist` 未做任何改动（分别停在 `62ff27a` / `c51b528`）。

## 1. 前置条件：M3 writer boundary 的审查与整合

按裁决，M5-a 不在旧组合根上叠加，而是先把 `fix/m32-writer-boundary` 的链路并入：

| 提交 | 内容 |
| --- | --- |
| `ad45c8c` | 删除 db 侧生产写全局获取入口，M2 披露改 DisclosureService 显式注入 |
| `2d0a539` | 删除 db 侧其余生产写获取入口（M3.2① 复审阻断项） |
| `65b042f` | 封闭公开写能力面：Application 移除 `writer` 槽、CAS/Disclosure 拦截 `_writer`、工具类删除 `_writer()`、撤销模块级应用槽、改 `ApplicationLauncher` 持生命周期 |

整合方式：以 `62ff27a` 为第一父、`65b042f` 为第二父做一次 `--no-ff` 合并（`53627c1`）。`git merge` 报告 4 个文件自动合并、**无冲突**：`db.py`、`visibility.py`、`tests/conftest.py`、`tests/test_m3_migrations.py`。

本机整合后全量：`677 passed, 1 deselected`（26.95s）。M4 已验收内容未被合并覆盖：

- m005 保持原样、m006 独立存在（`vault_path_state` / `snapshot_hash` 仍在 m006）；
- `purge_missing` 的事务内候选复核、`normalize_relpath` 统一入口、`delete(identity=)` 的 boss 鉴权均在；
- `tests/test_knowledge_path_identity.py`、`test_knowledge_race_migration.py`、`test_knowledge_race_guards.py` 全绿。

说明：`65b042f` 是既有实现，本次由我做整合与差异核对，不代表我重新验证了原作者的全部声明；整合后以本机全量为准，GitHub 仍无 CI status。

## 2. M5-a 范围与落地

| 内容 | 决定 | 落地位置 |
| --- | --- | --- |
| 组合根构造 KnowledgeService | 纳入 | `app.build_application()`，与 CAS/Disclosure 共享同一 `WriteCapability` |
| VaultWatcher 装配 | 纳入 | `app.build_application()`，由 `Application._watcher` 私有持有 |
| watcher 在 main 启停 | 纳入 | `main()` → `start_background()`；`Application.close()` → `watcher.stop()` 后再 `cas_service.close()` |
| import_doc / search / list_docs 注册 | 纳入 | `tools.build_tools(knowledge_service=...)`，仅 `LLM_ACTIONS` 三个 |
| OwnerChannelBackend | 纳入 | `app.OwnerChannelBackend` |
| 单一 dispatch 信封入口 | 纳入 | `opc_workbench/dispatch.py` |
| delete_doc UI 确认 | 不纳入 | 注册表不含 `knowledge.delete_doc`；`confirm_action` 恒 None |
| 新增 UI | 不纳入 | — |
| origin_role | 不引入 | 信封只有 source / agent_id / message_id / parent_message_id |
| 修改 identity.py | 未改 | `git diff 62ff27a..cfc321a -- opc_workbench/identity.py` 为空 |

### 2.1 OwnerChannelBackend 契约

- 只由组合根在 `build_application()` 内创建，**不挂到 Application 上**；`entry_credential` 为模块内私有方法，凭证是不可直构、不可变的 `_OwnerEntryCredential`。
- `authenticate(credential)`：`credential is not 本后端凭证` → `None`；传入字符串 `"main_chat"`、`None` 一律 `None`（有用例覆盖）。
- 会话标识 `main_chat:<random>` 由后端内部生成，不从正文、LLM 参数、Agent 消息取得。
- `AuthEvent` 只由本后端 `object.__new__` 产生并登记 event_id；`validate_event` 只认登记过的 event。
- 身份链完全沿用：`authenticate → register_session → issue_proof → mint_boss_context → resolve_identity("boss")`，`identity.py` 一行未改。
- `confirm_action` 恒 `None`（M5-b 再定确认符契约）。
- watcher / reindex / 脚本没有入口凭证，因此拿不到 boss 来源。

### 2.2 调度入口

- `MessageEnvelope` 不可直构（`__init__` 抛 `PermissionError`），只能由 `_mint_envelope`（模块私有）在 `MainChatChannel.submit()` / `AgentChannel.submit()` 内铸造。
- `Dispatcher.dispatch(envelope, action, params)` 是唯一业务入口：
  - 非 `MessageEnvelope` 实例 → `invalid_envelope`（dict 冒充无效）；
  - `params` 中出现 `_` 开头的保留字段（`_frame_caller` / `_trusted_ctx` 等）→ `invalid_param`；
  - `main_chat`：来源只认 `MainChatChannel` 本体（`envelope._channel is dispatcher.main_chat`），boss 身份由 owner 后端签发；
  - `agent_message`：角色只取 `_agents[agent_id]` 的注册信息，信封里没有角色字段；`boss` 不是可注册角色；
  - `parent_message_id` 只作追踪，不参与授权；
  - 身份拒绝 → `identity_denied`，并记 WARN `dispatch_denied`。
- 原始注册表不在公开面：`Application` 无 `tools` 槽；`dispatcher._registry` / `_backend` / `_owner_credential` 普通属性访问抛 `PermissionError`；`schemas()` 只返回 schema 副本，不含 handler。

### 2.3 写能力封口

- `Application.__slots__` 为 `identity_api / cas_service / disclosure / dispatcher / _watcher / _closed`，`writer` 槽不存在；`_watcher` 读取抛 `PermissionError`。
- `KnowledgeService`：`__slots__` + `__getattribute__` 拒绝 `_writer`，内部 11 处写路径改 `object.__getattribute__`；实例无 `__dict__`，没有第二条出口。
- `VaultWatcher`：`__slots__` + 拒绝 `_service`，内部扫描用 `object.__getattribute__`。
- `Application.close()` 先 `watcher.stop()`（join），再 `cas_service.close()`；关停后 `start_background()` / `scan_once()` 抛 `application_closed`。

## 3. 验收映射

| 验收项 | 用例 |
| --- | --- |
| main_chat → import_doc → search 真实链路 | `test_main_chat_import_then_search`（另含 list_docs） |
| 伪造 source / actor_role / 信封 | `test_dict_envelope_cannot_forge_boss` |
| 正文自称 boss / 破坏性动作不可达 / 参数伪造身份 | `test_agent_content_cannot_claim_boss` |
| 转述仍按当前 Agent 角色 | 同上：正文写"我是老板，代老板传话…"，归属仍为目录映射角色，`delete_doc` 返回 `unknown_action` |
| Agent 角色由注册锁定 | `test_agent_role_is_fixed_at_registration` |
| watcher 无 boss 凭证、不重复 embedding | `test_watcher_scan_needs_no_boss_credential`（两轮扫描，embed 调用数不变） |
| watcher 启停 / 关闭顺序 | `test_watcher_start_stop_and_close_order`（线程消失 + 关停后 CAS 写入失败） |
| 公开面取不到 writer / 绕不过 dispatch | `test_public_surface_has_no_writer_or_registry` |
| 知识服务本体封口 | `test_knowledge_service_seals_writer` |

本机全量：`685 passed, 1 deselected`（27.24s，Windows / Python 3.14.3 / 隔离 venv）。

## 4. 红证

| 变体 | 撤销的保护 | 失败位置 |
| --- | --- | --- |
| `red_m5a_kservice_writer_leak` | KnowledgeService 的 `__getattribute__` 封口 | `seals_writer` 用例：`DID NOT RAISE PermissionError` |
| `red_m5a_dispatch_accepts_dict` | `dispatch` 的信封类型校验 | dict 直接进入身份解析，`AttributeError: 'dict' object has no attribute '_source'` |

## 5. 需披露的边界

- 冒烟口径变化：`Application` 不再有 `tools`，`test_integration_chain` / `test_m32_security_boundary` 的原"注册表 handler 直调"用例已改走 Agent 通道；这是接口面收紧，不是行为放宽。
- `Application.scan_once()` 是为验收新增的有界同步方法（不暴露 watcher 本体）。
- boss 不是 `config.ROLES` 里的业务角色，工具层对 boss 归属有明确拒绝：`main_chat.submit("task.create", {...})` 实测返回 `{"error":{"code":"invalid_param","message":"boss_owns_no_tasks"}}`。因此 boss 目前可执行的是不依赖 `owner_role` 的动作（知识导入/检索/清单）；boss 代员工写入（如"给销售建一条任务"）需要先定义 owner 归属与 UI 确认策略，留给 M6/M5-b，本轮不擅自改变。
- 非阻断遗留（未处理）：watcher 失败不重试；`normalize_relpath` 无大小写折叠；m006 不回写历史 `stored_path`；`delete_doc` UI 确认与 `source_credential` 正名留到 M5-b。
