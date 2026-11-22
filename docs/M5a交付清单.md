# M5-a 组合根接线与统一调度入口 — 交付清单

分支：`feat/m5-a-dispatch`（新建，已推送）
提交：`53627c1`（合并 M3 writer boundary）→ `cfc321a`（M5-a 本体）→ `8e85617`（B1/B2 修复）→ `5594a8e`（注释同步）
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
| 组合根构造 KnowledgeService | 纳入 | `app._compose()`，与 CAS/Disclosure 共享同一 `WriteCapability` |
| VaultWatcher 装配 | 纳入 | `app._compose()`，由 `Application._watcher` 私有持有 |
| watcher 在 main 启停 | 纳入 | `main()` → `start_background()`；`Application.close()` → `watcher.stop()` 后再 `cas_service.close()` |
| import_doc / search / list_docs 注册 | 纳入 | `tools.build_tools(knowledge_service=...)`，仅 `LLM_ACTIONS` 三个 |
| OwnerChannelBackend | 纳入 | `app.OwnerChannelBackend` |
| 单一 dispatch 信封入口 | 纳入 | `opc_workbench/dispatch.py` |
| delete_doc UI 确认 | 不纳入 | 注册表不含 `knowledge.delete_doc`；`confirm_action` 恒 None |
| 新增 UI | 不纳入 | — |
| origin_role | 不引入 | 信封只有 source / agent_id / message_id / parent_message_id |
| 修改 identity.py | 未改 | `git diff 62ff27a..cfc321a -- opc_workbench/identity.py` 为空 |

### 2.1 OwnerChannelBackend 契约

- 只由组合根在 `_compose()` 内创建，**不挂到 Application 上**；`_entry_credential()` 为模块内私有方法，凭证是不可直构、不可变的 `_OwnerEntryCredential`。
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
  - `main_chat`：信封 `_channel` 必须与组合根内部铸造的通道同一（否则 `main_chat_envelope_required`），boss 身份由 owner 后端签发；该通道不在 Dispatcher 公开面（B1 修复后）；
  - `agent_message`：角色只取 `build_dispatcher` 时的 `config.AGENT_ROLES` 快照，信封里没有角色字段；`boss` 不是可注册角色，调用方也没有注册入口（B2 修复后）；
  - `parent_message_id` 只作追踪，不参与授权；
  - 身份拒绝 → `identity_denied`，并记 WARN `dispatch_denied`。
- 原始注册表不在公开面：`Application` 无 `tools` 槽；`dispatcher._registry` / `_backend` / `_owner_credential` / `_main_chat` 普通属性访问抛 `PermissionError`；`schemas()` 只返回 schema 副本，不含 handler。

### 2.3 写能力封口

- `Application.__slots__` 为 `identity_api / cas_service / disclosure / dispatcher / _watcher / _closed`，`writer` 槽不存在；`_watcher` 读取抛 `PermissionError`。
- `KnowledgeService`：`__slots__` + `__getattribute__` 拒绝 `_writer`，内部 11 处写路径改 `object.__getattribute__`；实例无 `__dict__`，没有第二条出口。
- `VaultWatcher`：`__slots__` + 拒绝 `_service`，内部扫描用 `object.__getattribute__`。
- `Application.close()` 先 `watcher.stop()`（join），再 `cas_service.close()`；关停后 `start_background()` / `scan_once()` 抛 `application_closed`。

## 3. 验收映射

| 验收项 | 用例 |
| --- | --- |
| main_chat → import_doc → search 真实链路 | `test_main_chat_import_then_search`（另含 list_docs；通道来自 `app.main()`） |
| 普通 Application 无 boss 通道 | `test_build_application_has_no_boss_channel`（另：`launcher.main_chat` 未启动时拒绝） |
| 伪造 source / actor_role / 信封 | `test_dict_envelope_cannot_forge_boss` |
| 正文自称 boss / 破坏性动作不可达 / 参数伪造身份 | `test_agent_content_cannot_claim_boss` |
| 转述仍按当前 Agent 角色 | 同上：正文写"我是老板，代老板传话…"，归属仍为目录映射角色，`delete_doc` 返回 `unknown_action` |
| Agent 角色由可信注册表锁定 | `test_agent_roles_come_from_framework_registry`（无 `register_agent`、未登记 id 拒绝、句柄同一） |
| 生产默认无员工 Agent | `test_production_registry_is_empty_by_default` |
| watcher 无 boss 凭证、不重复 embedding | `test_watcher_scan_needs_no_boss_credential`（两轮扫描，embed 调用数不变） |
| watcher 启停 / 关闭顺序 | `test_watcher_start_stop_and_close_order`（线程消失 + 关停后 CAS 写入失败） |
| 公开面取不到 writer / 绕不过 dispatch | `test_public_surface_has_no_writer_or_registry` |
| 知识服务本体封口 | `test_knowledge_service_seals_writer` |

本机全量：`687 passed, 1 deselected`（26.29s，Windows / Python 3.14.3 / 隔离 venv；B1/B2 修复后）。

## 4. 红证

| 变体 | 撤销的保护 | 失败位置 |
| --- | --- | --- |
| `red_m5a_kservice_writer_leak` | KnowledgeService 的 `__getattribute__` 封口 | `seals_writer` 用例：`DID NOT RAISE PermissionError` |
| `red_m5a_dispatch_accepts_dict` | `dispatch` 的信封类型校验 | dict 直接进入身份解析，`AttributeError: 'dict' object has no attribute '_source'` |
| `red_b1_public_main_chat` | 恢复 `Dispatcher.main_chat` + 公开工厂铸造通道 | `test_build_application_has_no_boss_channel` 于 `assert not hasattr(...)` 变红；探针复现普通调用方 `main_chat.submit("knowledge.import_doc")` 成功落库 |
| `red_b2_public_register_agent` | 恢复公开 `register_agent(agent_id, role)` | `test_agent_roles_come_from_framework_registry` 于 `assert not hasattr(...register_agent)` 变红；探针复现自选 `cfo` 建任务成功（`version=1, owner_role=cfo`） |

## 5. 需披露的边界

- 冒烟口径变化：`Application` 不再有 `tools`，`test_integration_chain` / `test_m32_security_boundary` 的原"注册表 handler 直调"用例已改走 Agent 通道；这是接口面收紧，不是行为放宽。
- `Application.scan_once()` 是为验收新增的有界同步方法（不暴露 watcher 本体）。
- boss 不是 `config.ROLES` 里的业务角色，工具层对 boss 归属有明确拒绝：`main_chat.submit("task.create", {...})` 实测返回 `{"error":{"code":"invalid_param","message":"boss_owns_no_tasks"}}`。因此 boss 目前可执行的是不依赖 `owner_role` 的动作（知识导入/检索/清单）；boss 代员工写入（如"给销售建一条任务"）需要先定义 owner 归属与 UI 确认策略，留给 M6/M5-b，本轮不擅自改变。
- 非阻断遗留（未处理）：watcher 失败不重试；`normalize_relpath` 无大小写折叠；m006 不回写历史 `stored_path`；`delete_doc` UI 确认与 `source_credential` 正名留到 M5-b。

## 6. 初审阻断 B1/B2 的修复（`8e85617`，注释同步 `5594a8e`）

初审结论：M3 整合与 writer 注入方向通过，但发现两处"可信对象被公开获取"，不属于信封字段伪造。

### 6.1 B1：主对话通道不再从普通 Application API 取得

- 新增模块级 `_compose(*, with_owner_channel)`，把原 `build_application()` 的装配逻辑收为单点。
- 公开 `build_application()` 固定 `with_owner_channel=False`：不铸造 `MainChatChannel`，`Dispatcher._main_chat` 为 `None`。
- `Dispatcher` 删除公开 `main_chat` property；`_main_chat` 纳入 `__getattribute__` 拒绝名单（`dispatcher_internal_not_exposed`）。
- 通道只能由 `ApplicationLauncher._build_owner()` 获得，而该方法仅由 `main()` 调用；`launcher.main_chat` 在未启动时抛 `owner_channel_not_launched`。
- `_resolve_main_chat` 以内部 `_main_chat` 做同一性校验：`None` 或信封通道不匹配 → `main_chat_envelope_required`，即使有人构造出通道对象也无法走 boss 分支。

验收落点：`test_build_application_has_no_boss_channel`；`test_main_chat_import_then_search` 证明确实由 `main()` 持有的通道仍能 import/search/list_docs。

### 6.2 B2：Agent 角色注册移入可信注册边界

- 新增 `config.AGENT_ROLES: dict[str, str]`（agent_id → role），默认**空表**：v1 没有真实员工 Agent 进程，生产不预置任何角色通道。
- 组合根在 `build_dispatcher` 时读取快照并校验角色合法（非法抛 `unknown_role`），此后调度器只读该快照。
- `Dispatcher` 删除公开 `register_agent`；改为 `agent_channel(agent_id)`：只查快照，未登记 → `agent_not_provisioned`；不接受 role 参数；句柄按 agent_id 缓存，同一 Agent 无法重注册或改角色。
- 测试夹具通过 `isolated_env` 注入 `test-agent-<role>` 预置表，生产路径保持空表（`test_production_registry_is_empty_by_default` 单独验证）。

### 6.3 本轮红证与复跑

| 项 | 结果 |
| --- | --- |
| `red_b1_public_main_chat` | 目标用例 1 failed（`assert not hasattr(... main_chat)`）；探针 `EXPLOIT_OK status=ready`（普通调用方经公开通道完成导入） |
| `red_b2_public_register_agent` | 目标用例 1 failed（`assert not hasattr(... register_agent)`）；探针 `EXPLOIT_OK version=1 owner_role=cfo`（自选角色注册后建任务） |
| 全量 | `687 passed, 1 deselected`（26.29s） |

原始输出：`docs\M5a-B1B2红证原始输出.txt`、`docs\M5a-B1B2全量测试输出.txt`；增量 diff：`docs\M5a-B1B2修复diff.patch`（`cfc321a..5594a8e`，7 文件 +224/−105）。

### 6.4 本轮未做的事（按裁决保持范围）

- 未改 M4（`feat/m4-ingest@62ff27a`）、未改 `identity.py` 签发链；
- 未实现 `delete_doc` 确认、未加 UI、未引入 `origin_role`；
- 未重做知识服务与 watcher、未重设 writer boundary；
- 分支仍在 `feat/m5-a-dispatch`，未合并 `master`、未打 tag、未删分支。

### 6.5 残留披露

- `_compose(with_owner_channel=True)` 仍是模块内函数，同进程代码理论上可直接 `import` 调用；当前防护是"私有命名 + 不写入任何公开 API"，主入口只经 `main()`。若要更强，可加"每进程仅允许 main 调用一次"的运行期守卫，本轮未擅自增加。
- `ApplicationLauncher._build_owner()` 为私有方法；测试统一走 `app.main()`。若先 `build()` 再 `main()`，启动器复用已有应用且无通道，`launcher.main_chat` 抛 `owner_channel_not_launched`（该用法的语义尚未定义）。
- `AGENT_ROLES` 默认空表意味着 v1 生产没有员工 Agent 通道；真实 Agent 进程的注册来源（配置文件/部署注入）留到 M6 定义。

## 7. 设计裁决（冻结）：老板来源是消息属性，不是通道对象

冻结日期 2026-11-23，适用于 M5-a 及其后里程碑；改动此裁决需重新裁决，不得在实现层自行放宽。

### 7.1 老板来源原本承担的职责

只区分指令来源，不做自然人识别、不做登录：

| 来源 | 系统识别角色 | 权限含义 |
| --- | --- | --- |
| 主聊天窗口 | `boss` | 老板直接下达的指令 |
| 员工 Agent | 对应员工角色 | 按员工权限执行 |
| watcher | `system` | 后台维护，不具备老板权限 |
| 普通脚本 | 无可信身份 | 不得获得老板权限 |

因此：主聊天窗口的「删除这份知识文档」可进入老板授权路径；销售 Agent 转述「老板让我删」仍是销售 Agent 的消息，不继承老板权限；watcher 只能做维护操作。

### 7.2 裁决结论

- 取消「老板通道对象」：删除 `MainChatChannel`，不暴露 `main_chat` 属性，不在业务代码间传递老板句柄。
- 全工作台只有一个业务入口：`Dispatcher.dispatch()`。
- 老板来源改为**消息携带的可信来源属性**：主聊天入口在框架边界内注入 `source=main_chat`，并由此得到 `actor_role=boss`。
- Agent 消息按直接发起者角色授权；watcher 走 `system`；普通脚本无可信身份。
- 不增加登录、密码或老板身份认证（v1 口径不变：主对话窗口即 boss）。
- 前提：普通代码不能自己构造 `source="main_chat"` 就拿到老板权限。

### 7.3 由此得到的两条实现约束

1. 来源令牌只能在可信启动边界铸造，且只交给主窗口适配器一次，不经普通 `Application`/`ApplicationLauncher` API 暴露。
2. Agent 角色注册表必须是可信配置加载后生成的不可变快照，运行期不可写。

## 8. 第二轮修复（`0a7b717`，基于 `5594a8e`）

### 8.1 B1：老板来源回归消息属性

- `dispatch.py`：删 `MainChatChannel`；`MessageEnvelope.__slots__` 为 `_source/_agent_id/_message_id/_parent_message_id/_origin`，不提供 `origin` property；`_mint_envelope(*, source, agent_id, parent_message_id, origin)` 为模块私有，`AgentChannel` 铸造时把自身写入 `origin`。`Dispatcher` 以 `_owner_provenance` 取代原通道字段，`__getattribute__` 拒绝 `_registry/_backend/_owner_credential/_owner_provenance`（`dispatcher_internal_not_exposed`）；`_resolve_main_chat` 要求 `envelope._origin is provenance`，否则 `PermissionError("main_chat_provenance_required")`；`_resolve_agent` 要求 `channels.get(agent_id) is envelope._origin`，否则 `agent_envelope_mismatch`，角色取自快照，未登记 → `agent_not_provisioned`。角色解析链不变：`backend.authenticate` → `register_session` → `issue_proof` → `mint_boss_context`。
- `app.py`：新增入口守卫 `_require_entry_point()`，按 `f_code.co_filename == _APP_FILE` 识别本模块帧，扫描中遇 `__name__ == "__main__"` 放行，遇非本文件帧抛 `PermissionError("owner_entry_point_only")`；`_claim_owner_session()/_release_owner_session()` 保证每进程只铸一次（重复 → `owner_session_already_active`）。`_compose(*, with_owner_channel)` 在 `True` 时先过守卫与占位，装配异常时先关连接再释放占位，并返回 `(application, owner_submit)`；`owner_submit` 是闭包，内部用 `_mint_envelope(source="main_chat", agent_id=None, parent_message_id=None, origin=provenance)` 后 `dispatch`。`ApplicationLauncher` 收敛为 `__slots__=("_application","_lock")` 加 `build()/close()`，`__getattribute__` 拒绝 `_application`（`launcher_application_not_exposed`）。`main(adapter=None)` 过守卫后 `_compose(True)`、`start_background()`、`adapter.bind(owner_submit)`、注册 `atexit`，返回的是有界 `Application`，**不返回提交端口**。
- 删除上一轮的 `ApplicationLauncher._build_owner()` 与 `main_chat` property；`build_application()` 仍为 `_compose(False)[0]`，无老板来源。

### 8.2 B2：角色注册表改为导入期不可变快照

- `config.py`：删可变 `AGENT_ROLES` 与 `_reload_agent_roles`；新增 `AGENT_ROLES_ENV="OPC_AGENT_ROLES"`、`_parse_agent_roles(raw)`（空串 → `{}`，JSON 必须是 object，agent_id 为非空 str，role 必须在 `ROLES` 内，否则 `ValueError`）；`_AGENT_ROLES_SNAPSHOT = MappingProxyType(_parse_agent_roles(os.getenv(AGENT_ROLES_ENV, "")))` 在导入期固化；`agent_roles()` 只返回该快照，快照本身无 `update`/`__setitem__`。
- `build_dispatcher(..., agent_roles=None, ...)` 再校验角色合法性与 `ROLES` 一致，并再包一层 `MappingProxyType(dict(...))` 存入调度器，配置快照与调度器快照互不影响。
- 测试夹具在导入 `opc_workbench` 之前用 `os.environ.setdefault("OPC_AGENT_ROLES", ...)` 注入 `test-agent-<role>` 十角色；生产路径默认空表。

### 8.3 验收：五条红证逐条对账

| 裁决要求 | 落点 | 当前结果 |
| --- | --- | --- |
| ① 普通模块调 `app.main()` 不得老板通道 | `test_ordinary_module_cannot_enter_owner_boundary`（断言 `app.main()` 与 `_compose(True)` 均抛 `owner_entry_point_only`）；`test_owner_session_is_entry_scoped` | 通过 |
| ② 普通模块调 `launcher._build_owner()` 不得老板通道 | `test_build_application_has_no_boss_source`（断言启动器既无 `main_chat` 也无 `_build_owner`） | 通过（方法已删除） |
| ③ 改角色注册表不能自封 CFO | `test_ordinary_module_cannot_register_roles`、`test_agent_roles_come_from_configured_snapshot`（快照无写入口、赋值抛 `TypeError`）、`test_production_registry_is_empty_by_default`（子进程默认空表） | 通过 |
| ④ 正常主窗口 `main_chat → knowledge.import_doc → knowledge.search` | `test_main_chat_import_then_search`（经 `main(adapter)` 的 `bind` 端口提交） | 通过 |
| ⑤ 已配置 Agent 角色固定不可改写 | `test_agent_roles_come_from_configured_snapshot`、`test_agent_cannot_impersonate_another_agent`、`test_agent_content_cannot_claim_boss` | 通过 |

变体红证（把当轮保护改回上一轮形态后复跑目标用例 + 利用探针）：

| 变体 | 撤掉的保护 | 目标用例 | 探针 |
| --- | --- | --- | --- |
| `red_b1_ordinary_main` | `_require_entry_point` 函数体改为 `return` | `DID NOT RAISE PermissionError` | 普通测试模块直接 `app.main(adapter)` 后导入 → `EXPLOIT_OK status=ready` |
| `red_b1_launcher_owner` | 同上，且启动器重新长出 `_build_owner()`/`main_chat` | `assert not hasattr(launcher, "_build_owner")` 失败 | `launcher._build_owner()` 后 `launcher.main_chat("knowledge.import_doc", ...)` → `EXPLOIT_OK status=ready` |
| `red_b2_mutable_roles` | `agent_roles()` 改回返回公开可变 `AGENT_ROLES` 字典 | `DID NOT RAISE ... agent_not_provisioned` | 写 `config.AGENT_ROLES["attacker"]="cfo"` 后建任务 → `version=1 owner_role=cfo` |

三条变体都变红且探针可复现利用，说明当轮保护是该路径上的唯一拦截点；也说明 ①-③ 不是靠命名约定而是靠可验证的拒绝路径成立。

### 8.4 边界与残留披露

- 已明确边界：Python 同进程内无法防御反射、内存改写、直接改运行时对象；本次只封项目提供的普通调用 API，不承诺 OS 级隔离。
- 未定口径（需裁决）：`python -c` 或任意以 `__main__` 执行的脚本调用 `app.main(adapter)` 当前会被视作入口并取得提交端口。若要求严格「只有 `python -m opc_workbench.app` 自身算入口」，需把守卫从「`__main__` 帧」收窄到「入口模块路径 + `-m` 自运行」，本轮未擅自收窄。
- `main()` 无 adapter 时（`__main__` 块即 `main()`）令牌铸造后无处交付、端口丢弃；M6 Gradio 适配器的正式契约（`bind(submit)` 还是适配器对象）未定。
- `Application.close()` 无条件调用 `_release_owner_session()`（含 `build_application()` 产出的无 owner 应用），当前幂等无害，语义是否接受未确认。
- `AGENT_ROLES` 真实来源定为环境变量 `OPC_AGENT_ROLES`（JSON），v1 默认空表；真实 Agent 进程注册来源仍按原裁决留到 M6。
- 未做项：未合 `master`、未打 tag、未动 M4（`feat/m4-ingest@62ff27a`）、未重做 `KnowledgeService`/watcher/CAS/Identity。

### 8.5 本轮材料

- 增量 diff：`docs\M5a-B1B2第二轮修复diff.patch`（`5594a8e..0a7b717`，6 文件 +366/−184）。
- 红证原始输出：`docs\M5a-B1B2第二轮红证原始输出.txt`（三变体的目标用例与探针输出）。
- 全量测试：`docs\M5a-B1B2第二轮全量测试输出.txt`（`691 passed, 1 deselected`，34.73s）。


## 9. 第三轮修复（`238fb4f`，基于 `0a7b717`）：唯一模块入口 + owner 会话生命周期

第三轮复审裁决：B2 通过；B1 阻断——守卫把"调用栈出现 `__main__`"当可信证明，
`python -c` 与 exec 伪造帧均可进入；另判两处生命周期阻断：普通应用 close 误释放
owner 名额、`main(adapter=None)` 丢端口且 `bind` 异常不清理。裁决同时冻结启动契约：
可信启动命令唯一为 `python -m opc_workbench.app`；适配器由启动流程内部创建，
接口 `bind(submit)`，框架只交付受限提交函数。

### 9.1 B1：守卫改验"进程真实主模块"，删除 `main(adapter)`

- `_require_entry_point()` 不再走调用帧，改为四重校验 `sys.modules["__main__"]`：
  ① 带有 `-m` 启动生成的模块 spec；② `spec.name == "opc_workbench.app"`；
  ③ 主模块命名空间 **就是当前正在执行的这份模块代码的命名空间**（`is globals()`）；
  ④ `__file__` 与本文件一致。任一不满足 → `owner_entry_point_only`。
  - `python -c` / 普通脚本：主模块无 spec → 拒绝；
  - exec 伪造 `__main__` 帧：不改 `sys.modules` → 拒绝；
  - 普通导入模块调用：主模块是 pytest/脚本 → 拒绝；
  - 真实 `-m` 进程里另行 `import opc_workbench.app` 的副本：命名空间不同 → 拒绝。
- 删除公开 `main(adapter=None)`；新增私有 `_run_entry()`（无参数），只在本文件
  `if __name__ == "__main__":` 块调用。适配器 `MainWindowAdapter`（新模块
  `opc_workbench/main_window.py`）由 `_run_entry` **内部创建**，`bind(submit)`
  一次绑定、不可换绑、未绑定不可用；外部传入适配器在签名上即不可能（TypeError）。
- 测试入口不再用 exec 伪造：`conftest._run_module_entry()` 用标准库
  `runpy.run_module("opc_workbench.app", run_name="__main__", alter_sys=True)`
  ——这是 `-m` 的实现本体，以真实 spec + 临时主模块命名空间**重新执行入口源码**，
  不能注入适配器、不能跳过入口代码；另有子进程用例直接跑真实命令
  `python -m opc_workbench.app`（exit 0）与 `python -c` / 脚本（exit≠0，
  stderr 含 `owner_entry_point_only`）。

### 9.2 生命周期：名额归持有者，清理走 finally

- 名额状态移入新模块 `opc_workbench/_owner_session.py`（claim/release/is_active）。
  原因：`-m` 执行时 app 是 `__main__` 实例，与普通 import 是两个模块实例；
  状态放 app 内会随入口重执行而重置，单例约束失效。该模块经 sys.modules 缓存，
  全进程一份。
- `Application` 新增 `_owns_owner_session` 槽：只有 `_compose(with_owner_channel=True)`
  产物为 True；`build_application()` 产物为 False，close 不触碰名额。
- `close()`：watcher.stop → cas.close → 释放名额，层层 finally；watcher/写连接
  关闭异常也不会把名额遗留为活动状态；释放前清持有标记，幂等。
- `_run_entry()`：装配成功后 `bind` / `start_background` 任一异常 →
  `application.close()`（停 watcher、关写连接、释放名额）后再抛；不存在
  无适配器却成功启动的路径。

### 9.3 验收对账（三个阻断点）

| 裁决要求 | 落点 | 结果 |
| --- | --- | --- |
| `python -c` 不能取得老板来源 | `test_python_c_and_script_cannot_enter`（子进程，exit≠0 + 错误码）；`test_ordinary_module_cannot_enter_owner_boundary`（含 `main` 已删除、`_run_entry` 无参） | 通过 |
| exec 伪造 `__main__` 帧不能进入 | `test_forged_main_frame_cannot_enter`（`_run_entry` 与 `_compose(True)` 两路） | 通过 |
| 真实入口可用 | `test_module_entry_command_starts`（子进程 `python -m opc_workbench.app` exit 0）；`test_main_chat_import_then_search`（runpy 真实执行入口 → import → search → list_docs） | 通过 |
| 普通应用关闭不得释放名额 | `test_ordinary_close_does_not_release_owner_session`（老板会话活动中关普通应用 → 第二会话仍 `owner_session_already_active`；持有者关闭后才放行） | 通过 |
| 绑定/启动失败必须清理 | `test_entry_bind_failure_cleans_up`、`test_entry_start_failure_cleans_up`（名额归还、无残留 watcher、可重新启动）；`test_close_releases_session_even_if_stop_fails`（stop 异常仍释放） | 通过 |
| 适配器契约 bind(submit) | `test_main_window_adapter_contract`（未绑定拒绝、一次绑定、非可调用拒绝）；框架不交付 Dispatcher/Identity/令牌/注册表 | 通过 |
| 既有回归 | 主聊天/Agent/watcher/m32 攻击面全部保留并通过 | 通过 |

变体红证（撤掉当轮保护后目标用例变红 + 探针复现利用）：

| 变体 | 撤掉的保护 | 目标用例 | 探针 |
| --- | --- | --- | --- |
| `red_b1_frame_guard` | 守卫退回调用栈 `__main__` 检查 | 伪造帧用例 `DID NOT RAISE`；`python -c` 用例 `assert 0 != 0`（进程成功进入） | exec 伪造帧进入入口后 `adapter.send(import_doc)` → `EXPLOIT_OK status=ready` |
| `red_b2_unconditional_release` | close 退回无条件释放名额 | 顺序用例 `DID NOT RAISE ... owner_session_already_active` | 老板会话活动中关普通应用 → 第二个老板会话 `EXPLOIT_OK second_owner_session=started` |
| `red_b3_no_entry_cleanup` | `_run_entry` 去掉失败清理 | `assert not is_active()` 失败 | bind 抛异常后 `EXPLOIT_OK session_leaked=True` |

### 9.4 边界与残留披露

- 同进程边界不变：不防反射/内存改写/直接替换 `sys.modules["__main__"]` 等运行时
  对象篡改；只封项目提供的普通调用 API。
- `runpy.run_module(..., run_name="__main__", alter_sys=True)` 能进入入口——它就是
  `python -m` 的标准库实现，语义上等价于执行可信启动命令本身（无法注入适配器、
  无法跳过入口代码），不视为绕过；如裁决认为进程内重执行入口也应收紧，需另行拍板。
- 测试卫生：`isolated_env` teardown 兜底 `release()`，防用例中途失败泄漏名额跨用例；
  生产路径无此兜底，名额只由持有者 close 释放。
- `__main__` 块当前装配 + 绑定后即返回（进程退出，atexit 关停）；M6 在此接
  Gradio 事件循环，适配器契约 `bind(submit)` 已冻结。
- 未动项：M4、CAS、Identity、KnowledgeService、watcher 业务逻辑零改动
  （watcher 仅被测试 monkeypatch 模拟故障）；未合 master、未打 tag。
- CI 披露：仓库无 GitHub Actions workflow，全部测试结果为本机 venv 实测输出
  （原始文件归档），非 CI 状态。

### 9.5 本轮材料

- 增量 diff：`docs\M5a-B1B2第三轮修复diff.patch`（`0a7b717..238fb4f`，6 文件 +313/−105，新增 `main_window.py`、`_owner_session.py`）。
- 红证原始输出：`docs\M5a-B1B2第三轮红证原始输出.txt`。
- 全量测试：`docs\M5a-B1B2第三轮全量测试输出.txt`（`699 passed, 1 deselected`，36.58s）。

