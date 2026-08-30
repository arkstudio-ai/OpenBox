# 直连路径清理与强化规划

> 文档状态：Execution Plan v1（2026-08-30）<br>
> 前置决定：耐久 SkillJob 运行时已于 2026-08-30 禁用（`1d453a0`），视频制作回归
> `.openbox/skills/video-production` 直连流程。本文把"禁用"推进到"移除"，并把省下的
> 力气投到工具层契约上。<br>
> 设计哲学（本轮讨论定稿）：**薄 agent + 硬工具**。状态由工具提供者管控且只有一份；
> LLM 凭 `status` 重建事实并自由决策；平台不做编排。恢复 = 用户再说一句话；
> 付费安全 = 服务端幂等键。codex（`unified_exec` yield/poll）与 Claude Code
> （后台进程 + 唤醒会话）均为此形态的实证，无一家构建耐久作业状态机。

## 0. 结论先行

耐久运行时禁用后，仓库里留着约 **1.2 万行**只为它存在的代码：运行时本体 6,179 行、
内置技能 2,010 行、API/工具 553 行、七张表与模型 319 行、专属测试 2,484+ 行（137 个
用例）、前端与移动端作业卡组件约 900 行。这些代码有测试守着、有配置门控着，但没有
任何调用者——它们现在唯一的产出是维护成本和"两套控制面"的认知负担。

本规划分两个里程碑：**M1 移除**（四个 PR，把死代码、死表、死配置清干净，历史数据的
回执渲染保留）；**M2 强化**（把移除中发现的契约缺口补在工具层：错误公开声明、
等待语义统一、status 完整性）。M2 不是可选项——移除会切断两条仍有价值的线
（材料错误指引文本、有界重试语义），必须在同一批工作里落回直连工具，否则是净退化。

## 1. 范围与不动区

**明确不碰**（防误删清单）：

| 区域 | 原因 |
|---|---|
| `frontend/`（v1 前端） | 用户指示：整体不在本次清理范围，淘汰另行处理 |
| `tool/video_workflow.py` 共享层 | 两套控制面共用的不变量层：审批哈希、花费上限、模型冻结、b-roll 豁免、lint。这是"硬工具"的本体 |
| `tool/video_production.py` 三个直连工具 | 现行唯一视频控制面 |
| `skill_install` / `user_skills` 表及 `skill/user_library.py` | 技能中心（创建/分享/安装）的表，与作业运行时同名不同族 |
| `db/models/kv_store` 相关 | `mcp/oauth.py` 与 `storage/storage.py` 在用，非运行时资产 |
| todo/中断生命周期全部代码 | 与运行时无关（`session/abort.py`、turn-view、divider 等） |
| 聊天回执**渲染器** | 历史会话里回执是已落库的消息 part，必须继续可读（见 §2.6） |
| `.openbox/skills/video-production/` | 现行技能文档（已含 b-roll 修订） |

## 2. M1 移除清单

### 2.1 后端代码（整目录/整文件删除）

| 路径 | 行数 | 说明 |
|---|---|---|
| `backend/skill_runtime/`（14 文件） | 6,179 | types / repository / worker / reconciler / manifest / service / inbox / outbox / receipt / embedded / worker_main / registry / context |
| `backend/builtin_skills/`（整目录） | 2,010 | 耐久版 video handlers 1,628 行 + demo_echo 参考技能 + 门控版 SKILL.md/skill.yaml |
| `backend/api/skill_jobs.py` | 213 | 作业 API（含无人能用的 operator-input 端点） |
| `backend/api/skill_settings.py` | 62 | 按用户启停内置技能——前端 **零调用**，纯运行时时代遗产 |
| `backend/tool/skill_job.py` | 278 | agent 侧作业工具 |
| `db/models/`：`skill_job.py` `skill_job_attempt.py` `skill_job_event.py` `skill_job_input.py` `skill_job_artifact.py` `session_inbox.py` `user_skill_setting.py` | 319 | 七表的 ORM 模型 |

### 2.2 接线手术点（逐处断开，不是整文件删）

1. **`main.py`**：`ensure_job_engine`（:89）、embedded worker 启停（:126-133, :168）、
   `InboxDispatcher` 启停（:138-146）、两个 router 挂载（:283-286）。
2. **`session/session.py:277`** 会话删除时的 continuation 作业取消块——连同上游收集
   `continuation_jobs` 的查询一起移除。
3. **`api/ws.py:_has_active_skill_jobs`** 浏览器断连后沙箱清理的 fail-closed 门。
   直连路径下"工作是否在进行"的事实源是**会话 busy 状态**（回合在服务端跑），
   替换判定并在 §4.3 浏览器实测断连场景，不许直接裸删。
4. **`tool/registry.py`**：删除 `durable_video_authoritative` 分支——三个直连工具无条件
   注册；同时删除 `skill_job_tool` 的注册与 import。
5. **`skill/skill.py`**：删除 `gated_builtins` / `enabled_config_flag` 机制与 builtin 扫描
   （builtin 目录即将不存在；project/global 两级保留）。
6. **`core/config.py`** 配置簇整段删除：`skill_jobs_enabled`、`skill_worker_mode` /
   `queues` / `concurrency` / `lease_seconds` / `per_user_concurrency` /
   `invocation_timeout`、`skill_jobs_video_write`、`skill_job_chat_receipt`，
   及对应 env 映射（`SKILL_JOBS_ENABLED` 等）。
7. **`tests/conftest.py`** 的旗标钉住 fixture **必须与 #6 同一 PR 撤除**——
   `monkeypatch.setattr` 打在已删除的属性上会直接 `AttributeError`。
8. 本地 `openbox.json`（gitignored）与 `openbox.jsonc.example` 的相关键清除。

### 2.3 数据库

新增一个 alembic 迁移（单 head，注意此前多 head 教训），`upgrade()` 按依赖序 drop：
`session_inbox` → `skill_job_artifacts` → `skill_job_inputs` → `skill_job_events` →
`skill_job_attempts` → `skill_jobs` → `user_skill_settings`；`downgrade()` 原样重建
（从 `a2c4e6f8b0d1` 复制建表代码，保证结构可回滚——数据不可回滚，见风险表）。

破坏性评估：这些表只在本机 dev 库有数据（全部是测试作业）；其余环境
`skill_jobs_video_write` 从未开启，表恒为空。单用户 SQLite 模式建表走 ORM
`create_all`，删除模型即自然不再建表；存量 SQLite 文件里的孤表无害，不做清理。

### 2.4 测试（删 137 例，移植其中约 20 例的不变量）

**整文件删除**：`test_skill_job_outbox / receipt / repository / tool / transitions /
worker`、`test_skill_manifest`、`test_job_continuation`、`test_job_failure_report`、
`test_video_skill_handlers`（合计 137 个用例，约 2,900 行）。

**先移植后删**（这是 M1 里最关键的一步，丢了就是净退化）：

| 源 | 移植去向 | 不变量 |
|---|---|---|
| `test_job_failure_report` §"what a handler is allowed to publish"（4 例） | 新 `test_video_error_text.py`，对准直连工具的 `_public_error` | 供应商响应体永不外泄；**声明过 public 的指引文本必须外显**（"请配置 material_base_url"这类）；retryable 声明可读 |
| `test_video_skill_handlers::test_a_node_that_never_comes_back_stops_dialling_and_parks` 的精神 | 直连工具已有 `wait_seconds` 有界语义，在 `test_video_production.py` 补"wait 超时返回而非死等"断言（若已有则标注即可） | 一切等待有界 |
| `test_video_model_snapshot.py` | **保留原文件**，仅剥离对 `skill_runtime` 的 import（其测的是共享层 `resolve_segment_model`） | 选择器/冻结语义 |

### 2.5 错误公开契约的迁移（M1 内完成，不留窗口期）

`public_error_text` / `HandlerError` 现居 `skill_runtime/types.py`，而
`video/materials.py` 的 `MaterialProviderError` 按此契约携带 `public_message` /
`retryable` 标记。运行时删除后，直连路径的 `_public_error` 只会输出
`"MaterialProviderError: operation failed"`——用户将**再也看不到**"请配置
material_base_url"这句修复指引。

处置：把契约下沉为工具层资产——`_public_error(exc)` 开头加一条：凡异常携带
`public_message=True`（逐实例声明、默认保密的语义原样保留），返回其消息文本
（截断 500 字符）。`HandlerError` 类若无其他使用者则不迁移，`MaterialProviderError`
自带标记已够。§2.4 的移植测试即验收此项。

### 2.6 frontend-v2（v1 前端不在范围）

**删除**：`src/features/jobs/` 下的 `SkillJobsDock.tsx`、`SkillJobCard.tsx`（及其
test）、`hooks/useSkillJobLiveEvents.ts`、`api/jobs.ts`、`api/keys.ts`，
`ChatRoute.tsx` 中的 dock 接线与"后台任务"区块。

**保留**：`features/chat/components/SkillJobReceipts.tsx`——它自注释写明"仅凭
part 数据渲染，不依赖 jobs API"（§4.1 合规），是历史会话回执的唯一读取器；
`shared/types/api` 的 `SkillJobPart` 类型随之保留。若 `features/jobs/types` 中有
被回执引用的类型，随保留件迁往 `shared/types`（遵守 §4.1 禁止跨 feature import）。

**locale**：`jobs.json`（web 与 mobile 各一份）裁剪到只剩回执 chip 所需键；
两侧**逐字节一致**是验收门。

### 2.7 mobile（Flutter，对照 web）

删除 `features/jobs/api/jobs_api.dart`、`widgets/skill_job_card.dart`、
`widgets/skill_jobs_dock.dart` 及 `router.dart` / `chat_screen.dart` 中的接线；
保留 `features/chat/widgets/cards/skill_job_receipt.dart` 与
`shared/models/skill_job.dart`（回执 part 模型）。800 行门禁照常。

### 2.8 文档与记忆

- `docs/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md` 移入 `docs/archive/`，头部墓碑注明
  移除 commit 与本文件路径（它保留的价值是：若干年后重做后台任务时的完整反面教材
  与设计底稿）。
- DEVLOG 记一条移除事件。
- `openbox.jsonc.example` 清除运行时配置示例。

## 3. M2 强化（把力气花在契约上）

### 3.1 等待语义统一

现状不对称：`video_transcribe` / `video_render` 有 `wait_iteration` + `after_version`，
`video_generate` 只有裸 `wait`。统一为 codex `unified_exec` 同构的三段式：

1. `submit` 幂等提交，返回句柄；
2. `wait` **单次有界**（服务端上限对齐现有 `wait_seconds` 边界），超时返回
   `still_running` + 当前状态快照，绝不在一次调用里等到天荒地老；
3. 重复 `wait` 用 `wait_iteration` 递增自证不是复读，`after_version` 防错拿旧态。

`SKILL.md` 步骤 6/7/10 的等待表述随之核对（现文已基本符合，逐句过一遍）。

### 3.2 status 完整性审计——"仅凭 status 恢复"测试

哲学落地的硬指标：**新回合、零记忆，只调 `video_project(action="status")`，
必须能重建继续推进所需的全部事实**。新增一个端到端测试：走到"分段已批、第 1 段
已生成、第 2 段提交中"的状态，然后模拟全新回合，仅凭 status 返回值断言能拿到——
当前阶段、每段状态与 `generation_job_id`、全部审批及其哈希匹配性、三类幂等键、
冻结的模型、花费余量。缺哪个字段补哪个字段。这个测试今后是 status 字段的回归锚。

### 3.3 断连场景验收

替换 `_has_active_skill_jobs` 后，浏览器实测：发起一段真实生成（可用最小时长），
生成中关闭浏览器标签 → 服务端回合继续 → 重开页面 → 会话恢复显示进行中/完成结果；
沙箱不被断连清理误杀。

## 4. 已定决策（默认值，不再开口子）

| 决策 | 定论 | 依据 |
|---|---|---|
| `session_inbox` 表 | 随七表一起删 | 未来"完成唤醒"薄层按 Claude Code 形态重新设计（provider 看护 → 注入会话消息），不复用九态机的附属表 |
| demo_echo | 删 | 纯运行时参考件 |
| `api/skill_settings.py` | 删 | 前端零调用；技能启停将来若需要，挂在技能中心而非运行时 manifest |
| 回执渲染器 | 保留（只读历史件） | 老会话数据完整性 |
| operator-input 类端点 | 不迁移 | 直连路径失败当场可见，无停靠概念 |
| 运行时代码"归档到分支" | 不做 | git 历史即归档（`1d453a0` 之前俱在），另立分支只会腐烂 |

## 5. PR 分解与验收门

| PR | 内容 | 验收门 |
|---|---|---|
| **PR#1** 后端移除 | §2.1–2.5 全部（含迁移、conftest、测试删除与移植） | 后端套件全绿（预期 1016 − 137 + 移植新增，以实测数为准）；`alembic upgrade head` + `downgrade -1` 在 dev PG 实跑通过；uvicorn 冷启动日志无 runtime 残留；`grep -r skill_runtime` 零命中 |
| **PR#2** 前端与移动端 | §2.6–2.7 | `tsc` 清洁；lint 不高于既有基线（content-view.ts 两处旧账）；vitest 全绿；`dart analyze` 清洁；web/mobile locale 逐字节 diff 为空；浏览器验收 A/B（见下） |
| **PR#3** 契约强化 | §3.1–3.2 | 新增测试全绿且做变异检查（破坏 `_public_error` 的 public 分支、破坏 status 字段各须有用例变红）；浏览器验收 C |
| **PR#4** 文档归档 | §2.8 | 死链检查（`grep -r REBUILD_PLAN docs/ backend/ --include='*.md'`） |

**浏览器验收场景**（延续本仓库"不信代码测试"的规矩）：

- **A 历史回执**：打开含耐久时代回执/作业卡的老会话（如"魔仙堡"）：回执 chip 正常、
  无"后台任务"区块、控制台零报错。
- **B 直连全流程**：新会话走技能到剧本审批卡（不产生花费），确认工具直连、无作业卡。
- **C 恢复力**：进行中的等待被打断（重启后端）后，新消息一句"继续"，agent 仅凭
  status 重建并接续；失败注入（临时改坏 material_base_url）时用户能看到指引文本
  而非类名。

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| drop 表不可逆（数据层面） | 仅本机 dev 数据；迁移 `downgrade` 保证**结构**可回滚；执行前 `pg_dump -t 'skill_job*' -t session_inbox -t user_skill_settings` 留一份快照在 scratchpad 外的本地目录 |
| 老会话渲染回归 | 回执渲染器保留 + 验收场景 A；作业卡区块消失属预期行为（卡片数据源 API 已删），不算回归 |
| 断连清理门替换引入沙箱误杀 | §3.3 实测；PR#1 内先保守（宁可不清理）再在 PR#3 收紧 |
| 材料错误指引文本丢失 | §2.5 在 PR#1 内同批完成，不留窗口期 |
| `test_video_model_snapshot` 误删 | §1 不动区 + §2.4 明确"保留仅剥 import" |
| alembic 再度多 head | 迁移前 `alembic heads` 确认单 head；这是上次合并踩过的坑 |

## 7. Backlog（本规划明确不做）

1. **完成唤醒薄层**：provider 任务看护 + 完成时向会话注入一条续跑消息
   （Claude Code 形态）。等产品真的需要"关掉页面也继续"再立项，届时从零设计，
   预算按"一根线"而非"一个运行时"评估。
2. v1 前端（`frontend/`）淘汰——用户已明示另行处理。
3. `qa_jobs` 测试账号清理（密码曾出现在对话记录，环境对外前须删号或改密）。
