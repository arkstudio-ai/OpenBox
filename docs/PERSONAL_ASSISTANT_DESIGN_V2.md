# OpenBox 个人助理 V2 设计：主 agent + 子会话

状态：**已确认，按本文分期实施**。日期：2026-10-06（Asia/Shanghai）。分支 `codex/long-term-memory-plan`，起点提交 `f001c3a4`。

本文替代 [V1 设计](PERSONAL_ASSISTANT_DESIGN.md) 中与“每次读取都重验整张来源图”有关的要求，以及第 7.4 节的整链记忆隔离。V1 里与本文不冲突的部分（固定私人主会话、任务卡、命令幂等、结果投递 outbox、暂停/取消、资源接管租约、待答事项）继续有效。性能测量历史见 [性能分析](PERSONAL_ASSISTANT_PERFORMANCE.md)。

## 0. 一页结论

**V1 的问题不是 SQL 写得不好，而是需求本身要求“每次读取、每次模型调用都把整张递归来源图重验一遍”。**

- 一次挂起检查、一次模型派发前检查点、一次历史页刷新，都要沿着 任务 → 命令 → 提交 → 结果 → 来源消息 → 片段 → 更早的回答 … 一路走到底，约 1,100–1,200 条 SQL。
- 已验证闭包缓存把固定数据上的单次检查降到 5–25 条，但真实浏览器流程反而更慢（同一流程 636 秒，对照 283 秒）：每次写入都会让一批闭包失效，后台重新捕获又和前台抢资源。
- 页面每次刷新 `/history` 都重验一遍，约占一个流程全部 SQL 的 90%。

**V2 改需求，不再改缓存：**

1. 个人助理就是一个普通会话：历史分页存储、压缩、按页读取，不重验。
2. 其它会话都是它的子 agent：只把**最终摘要**交回给它；需要细节时它自己按需去读。
3. 它每轮只读两样东西：用户画像（常驻提示词）和关注列表（一张小表，一次查询）。
4. 撤回不追溯：删除记忆、撤销授权、删除会话只影响以后的读取，不改写已经说过的话。
5. 只管一层：子会话自己再派的子会话由它自己负责，交回的只有摘要。
6. 个人助理拥有自己的长期记忆（只从用户原话里学），并负责维护每个项目的项目档案。

目标：每轮主会话上下文构建、每次历史翻页、每次任务状态检查都是**个位数 SQL**，与历史长度和任务数量无关。

## 1. 已确认的决定

2026-10-06 用户回复“按你推荐的来”，以下 6 条全部采用推荐方案：

| # | 决定 | 内容 |
| --- | --- | --- |
| D1 | 撤回不追溯 | 删除记忆、撤销授权、删除或修改来源，只影响**以后**的读取、注入和工具调用；已经写进主会话的回答、摘要不改写，也不在每次读取时重验。与 ChatGPT“删除记忆不改写过去聊天”的做法一致。 |
| D2 | 只管一层 | 个人助理只跟踪它直接派出或关注的会话。这些会话内部再用 Task 工具派出的子会话由它们自己管理（沿用现有深度上限 3），最终只以摘要交回。个人助理永远不递归遍历子会话树。 |
| D3 | 关注范围 | 默认只关注个人助理自己创建的会话和用户明确指定的会话；完成、提问、等待审批都会通知个人助理。其它会话只进入每日简报。 |
| D4 | 共享会话先确认 | 往工作区可见的会话发消息之前，必须由用户在确认卡上点确认；个人记忆和主会话里的私人内容不得自动进入共享会话。 |
| D5 | 记忆写入 | 普通偏好自动记住并在回复里显示“已记住 · 撤销”；健康、财务、人际关系、证件等敏感内容进入待确认，由用户确认后才生效。 |
| D6 | 代答边界 | 普通会话里的提问可以由个人助理代答并标注“由个人助理代答”；权限审批（不可逆、对外、花钱、删除）永远由用户本人点。 |

## 2. 业界做法参考

| 产品 / 项目 | 做法 | V2 借鉴 |
| --- | --- | --- |
| OpenAI Dots | 常驻个人 agent，有自己的云电脑、记忆和审批；后端未开源 | 只借鉴体验：一个固定入口、长期记忆、需要时请求批准 |
| OpenAI Codex subagents | 主 agent 派出子 agent 并行处理，子 agent 只返回结果 | 子会话只交回摘要 |
| OpenClaw | `sessions_spawn` 派出隔离的子会话，完成后 announce 回请求方；主会话就是普通会话；禁止子 agent 再派子 agent；`sessions_list` / `sessions_history` 按需读取；`MEMORY.md` 常驻记忆 | 关注 + 完成通知、按需读历史、只管一层、用户画像常驻 |
| Hermes Agent | `delegate_task` 委派；SQLite FTS 记忆检索 | 委派即会话；记忆按需检索 |
| OpenAI Agents SDK | manager 模式：子 agent 作为工具，只有最终输出回到主 agent | 摘要交回，不回传中间轨迹 |
| LangGraph supervisor | `output_mode="last_message"`：只把子 agent 最后一条消息交回 | 摘要取子会话最终回复 |
| ChatGPT 记忆 | 删除记忆不改写过去的聊天；记忆可查看、可删除 | D1 撤回不追溯；记忆管理页可撤销 |

共同点：**没有一家在每次读取时重验整条来源链。** 正确性靠三件事保证：读取时检查当前权限、写入时检查受众、子 agent 的输出当作数据而不是指令。

## 3. 目标架构

```
个人助理主会话（kind=assistant，私有，每个用户每个工作区一个）
 ├─ 每轮上下文：系统提示 + 用户画像（记忆核心块）+ 关注列表（一次查询）+ 最近若干轮 + 压缩摘要
 ├─ 工具（按需、每次只做当前权限检查）
 │   ├─ 会话：sessions.list / sessions.read / tasks.submit（新建会话）/ tasks.followup（继续会话）
 │   │        tasks.link_existing（关注已有会话）/ tasks.pause|resume|cancel / tasks.archive（取消关注）
 │   ├─ 记忆：memory.search / memory.read / memory.remember / memory.update / memory.forget
 │   ├─ 项目：projects.list / projects.brief.read / projects.brief.update
 │   ├─ 待办：requests.list / requests.get / requests.reply
 │   └─ 其它：knowledge.* / assets.* / schedules.* / status.*（额度、资源、技能、发布，只读）
 ├─ 收件箱：用户输入（human）+ 被关注会话的结果（task_result，report_only 轮）
 └─ 关注列表 = assistant_tasks：每行一个被关注的会话（助理创建的，或用户指定的）

被关注的会话（普通会话，出现在项目侧栏）
 ├─ 自己的 agent、上下文、项目文件、项目记忆和项目档案
 ├─ 内部可以再用 Task 工具派子会话（深度 ≤3，助理不跟踪，D2）
 └─ 一次运行结束 → TaskResult（最终回复摘要）→ 助理收件箱 → report_only 轮汇报
```

### 3.1 现有能力可以直接复用的部分

梳理代码后，V1 已经搭好的任务层基本就是 V2 需要的“子 agent 管理”，只是被两个约束锁死：

| 已有能力 | 位置 | V2 处理 |
| --- | --- | --- |
| 新建执行会话 + 首条输入（命令幂等） | `assistant/commands.py` `create_task_locked` | 保留；新会话仍为 private + `assistant_isolated`（私有运行环境、私有浏览器都依赖这个标记，委派提示也不会进入记忆抽取）；项目档案照常注入 |
| 继续 / 插话 / 暂停 / 继续 / 取消 | `assistant/commands.py`、`control.py`、`steering.py` | 保留 |
| 关注已有会话 | `assistant/linking.py` | 放开：任何本人拥有的普通会话都能关注；工作区可见会话发消息前要确认（D4） |
| 运行结束 → TaskResult → 助理收件箱 → report_only 汇报 | `assistant/results.py`、`delivery.py`、`queue.py` | 保留；汇报内容就是最终回复，不再递归核验来源 |
| 待答事项 | `assistant/requests.py`、`request_reply.py` | 保留并扩展到所有本人会话（P4） |
| 主会话一次处理一个输入、报告与普通输入交替 | `assistant/queue.py` | 保留 |

两个约束：

1. **只认私有且记忆隔离的会话。** `linking.py:92-95` 拒绝普通会话（`ASSISTANT_LINK_SHARED`、`ASSISTANT_LINK_MEMORY_POLICY`），`task_locked`（`commands.py:166-171`）、`list_tasks`、`read_history` 同样过滤。
2. **每次读取都递归重验来源。** 见第 4 节。

现有 Task 工具（`tool/task.py`、`agent/subagent_runtime.py`）不适合给主助理直接用：它会阻塞父会话这一轮直到子会话结束、子会话固定在父会话的项目里、工具只能是父会话的子集、不显示在侧栏、不能向用户提问。所以 V2 的“子 agent”就是 `assistant_tasks` 关联的普通会话，Task 工具留给普通会话内部使用（D2）。

### 3.2 每轮读什么（SQL 预算）

| 读取 | 语句数 | 说明 |
| --- | ---: | --- |
| 主会话最近若干轮（分页） | 2–3 | 与普通会话相同的 `get_message_window` |
| 用户画像 + 相关记忆 | 2–4 | `run_memory_context`，全项目个人范围 |
| 关注列表 | 1 | `assistant_tasks` 未归档行 + 最近结果，一条连接查询 |
| 当前权限 | 1–2 | 成员资格、主会话所有者 |

合计个位数，且与历史长度、任务数量无关。工具调用各自按需查询，每次只检查当前权限。

## 4. 去掉什么、保留什么

### 4.1 现在每次运行在哪里重验

主会话一次模型调用前后会经过下面这些校验点（代码核对结果，摘要）：

| 位置 | 频率 | 校验内容 |
| --- | --- | --- |
| `projection.project_main_messages`（`agent/loop.py:1838`） | 每次构建请求、每次压缩 | 最近 40 条消息里每条回答的完整来源图、每个片段、每个决定、每个任务快照；同一轮里之前所有请求的上下文再校验一遍（轮内平方级） |
| `checkpoint_model_request` → `checked_context_locked(fresh=True)`（`session/agent_event_log.py:2719`） | 每次发给模型前 | 上下文里全部引用；业务读取重跑并比对；任务快照重读并比对；读入本会话全部事件 |
| `runtime_view`（`loop.py:1377`、`hooks.py:724`、`assistant_tools.py:229`） | 每步、每次工具调用 2 次 | 汇报 / 协调轮：结果来源图、授权派生 |
| 挂起检查（`scheduling._held_lineage`） | 每次运行、每步、每次工具、每次派发、监视器每 5 秒 | 执行会话：完整任务图（计划绑定、每条派生命令的派生证明、继续执行授权） |
| `_fresh_read`、`business_context.refresh` | 每步 | 本轮每个读取工具的结果重新执行一遍再比对 |
| 命令工具 `_tool_source_locked` → `capture_command_derivation` | 每次提交、追加 | 本轮已消费的所有上下文重新校验，加上原话哈希 |
| 汇报结算 `finalize_report_locked` | 每次汇报 | 结果来源图 + 要求读完每个来源片段的全部范围 |
| 历史接口 `public_messages`、`/api/assistant/messages`、快照 `_answer_digest`、`/unread`、`/read-cursor`、通知 `valid_target` | 每次页面刷新、每次通知打开 | 每条回答的完整来源图 |
| `session/public_events.py:19-43` | 每个实时事件 | 把助理会话和执行会话的事件折叠成“请重新拉取”，导致页面反复走上面的接口 |

### 4.2 V2 的规则

**写入时检查一次，读取时只查当前权限。** 当前权限指：工作区成员资格、对象属于当前用户、对象未删除、任务未暂停/取消。每项一条语句，可合并。

| 操作 | V2 只做 |
| --- | --- |
| 主会话任何读写 | `_authority`：成员资格 + 本人的私有主会话（1 条） |
| 构建上下文 | 普通会话的历史投影 + 压缩；附加关注列表块（1 条）、有效决定块（1 条，不重验）；继续做凭据脱敏（无 SQL） |
| 发给模型前 | 不再做助理专属检查点 |
| 挂起检查 | 任务 `desired_state`、是否归档、会话/项目是否删除（1–2 条） |
| 命令（提交、追加、控制） | 幂等 + 当前权限；`source_ref` 只记录触发它的那条用户消息 id，不做派生证明 |
| 汇报 | TaskResult 记录时把最终回复截成摘要存下；汇报轮直接把摘要放进输入，回答非空即结算 |
| 历史、快照、未读、通知 | 普通分页读取 + 当前权限，不重验 |
| 实时事件 | 与普通会话一样推送增量 |
| 读取工具结果 | 本轮内照常使用；下一轮起记忆/知识类正文替换为“需要时重新查询”（第 8.4 节） |
| 决定记录 | 记录时核对引用的是用户本人原话（一次）；之后注入不重验 |

### 4.3 模块处理清单

| 处理 | 模块 |
| --- | --- |
| **删除** | `assistant/evidence_cache.py`、`verified_units.py`、`evidence.py`（来源图部分）、`command_sources.py`、`execution_sources.py`、`source_scope.py`、`context_sources.py`、`projection.py`（主会话专用投影）、`business_context.py`（快照重跑）、`public_history.py`、`assistant/compaction.py`（改用普通压缩）、`db/evidence_schema.py`、`db/models/evidence.py`；前端 `assistant-transcript.ts`、`history-source-proof.ts`、`source-projection.ts`、`useVerifiedAssistantCopy.ts` |
| **精简** | `results.py`（去掉 `validate_result_source`，记录摘要）、`reporting.py`（去掉覆盖率要求与来源读取记录）、`runtime.py`（保留模式与工具范围）、`commands.py`（去掉派生证明）、`scheduling.py`（只保留状态挂起）、`continuation.py`（授权只看到期与任务状态）、`schedule_runs.py` / `schedule_commands.py`（去掉配置来源校验）、`linking.py`（去掉隔离出生事件链）、`decisions.py`（注入不重验）、`snapshot.py`、`history.py`、`notifications.py`、`transactions.py`（只保留 `begin_snapshot`、`read_session`）、`memory_provenance.py` / `knowledge_provenance.py`（只保留参数模型）、`session/public_events.py`、`session/agent_event_log.py`（检查点）、`session/session.py`（结算钩子） |
| **保留** | `service.py`、`policy.py`、`identities.py`、`inputs.py`、`queue.py`、`budget.py`、`delivery.py`、`retry.py`、`report_stop.py`、`reads.py`、`events.py`、`control.py`、`suspended_control.py`、`session_control.py`、`steering.py`、`assets.py`、`schedules.py`、`requests.py` 系列、`permission_requests.py`、`plans.py`、`knowledge.py`、`memory.py`、`memory_documents.py`、`resource_*`、`browser_*` |

## 5. 数据结构

迁移 `pb7…`（P1）与 `pb8…`（P3）完成下列改动，均可降级。

| 表 / 列 | 处理 | 说明 |
| --- | --- | --- |
| `assistant_evidence_epochs`、序列 `assistant_evidence_version_seq`、全部 `assistant_evidence_*` 函数与触发器、8 张热表上的 `evidence_version` 列 | **删除**（P1） | 只服务于闭包缓存的派生计数，没有用户内容；降级会按 `pb6` 结构重建（计数从头开始） |
| `assistant_tasks` | 保留，含义扩展为**关注列表** | 每行一个被关注的会话（助理新建，或用户指定的已有会话）。`archived_at` 表示取消关注：不再记录结果、不再汇报 |
| `assistant_task_results` | 保留，新增 `summary TEXT NULL`（P1） | 记录结果时从最终回复截取（≤4,000 字符，脱敏）；汇报轮、关注列表、`sessions.list` 都读它 |
| `assistant_commands` | 保留 | 幂等账本；`source_ref` 只存触发它的用户消息 id |
| `assistant_task_submissions`、`assistant_read_cursors`、`assistant_event_projections`、`resource_control_leases` | 保留 | 不变 |
| `project_briefs`（新，P3） | 新增 | `id, user_id, workspace_id, project_id, content(≤6,000), revision, updated_by(user/assistant), created_at, updated_at`；唯一 `(user_id, project_id)` |
| `sessions` | 不改结构 | 助理新建的会话保持 `visibility="private"`、`memory_policy="assistant_isolated"`；用户指定关注的已有会话保持原来的可见性和记忆策略（P2 放开关联限制） |
| 旧事件 `assistant.message.committed`、`assistant.business.read`、`model.requested.payload.assistant_context` | 保留不读 | 历史数据，不迁移、不删除 |

## 6. 项目会话管理

### 6.1 场景：在贪吃蛇项目里接着改界面

用户对助理说：“把贪吃蛇的界面改成暗色，就在我上周那个会话里接着改。”

1. `sessions.list(project="贪吃蛇", query="界面")` → 候选会话，每个带状态、更新时间、是否已关注、最近一次结果摘要。
2. `history.read(session_id, limit=20)` → 按需分页读最近内容，只做当前权限检查。
3. `tasks.link_existing(session_id)` → 关注这个会话（私有会话直接关注；工作区可见会话也可以关注，关注本身只读）。
4. `tasks.followup(task_id, "把主题改成暗色……")` → 作为一条输入进入该会话队列，片段 `origin="assistant_delegation"`，界面显示“由个人助理发送”。工作区可见会话先弹确认卡（D4），用户点确认后由服务器执行发送。
5. 会话自己的 agent 用它自己的上下文、项目文件、项目档案干活；跑完 → TaskResult（摘要）→ 助理收件箱 → 汇报轮告诉用户结果，必要时附截图或预览链接。

### 6.2 工具契约

| 工具 | 参数 | 行为 | 当前权限检查 |
| --- | --- | --- | --- |
| `sessions.list` | `project_id?`、`query?`（标题包含，按字面匹配）、`status?`、`watched?`、`limit ≤ 50` | 返回本人在当前工作区的顶层普通会话，最新的在前：`id, title, project_id, project_name, visibility, status, updated_at, watched, task_id, latest_summary` | 成员资格；`user_id` 为本人；排除助理会话、定时任务会话、子会话、已删除 |
| `history.read` | `session_id`、`before?`（消息 id）、`limit ≤ 20`、`max_chars ≤ 16,000` | 范围从“主会话 + 已关联任务会话”扩大到本人任意顶层普通会话；按普通聊天的分页语义返回文字、工具名与结果摘要 | 同上；不再逐条重验来源 |
| `tasks.submit` | `project_id`、`title`、`instructions`、`attachment_ids?`、`client_key` | 在项目里新建会话（私有、标准记忆策略、显式标题），自动关注并投递首条输入 | 项目属于本人且未删除；配额 |
| `tasks.link_existing` | `session_id` | 关注本人已有的顶层普通会话（不限私有/隔离） | 本人、未删除、不是助理/定时/子会话、未被关注 |
| `tasks.followup` | `task_id`、`text`、`attachment_ids?`、`expected_revision`、`client_key` | 往被关注的会话发输入。工作区可见会话：返回“等待确认”并在主会话弹确认卡，确认后由服务器投递 | 当前权限 + 任务未取消；会话正在等待提问回答时拒绝并提示改用 `requests.reply` |
| `tasks.pause` / `resume` / `cancel` | 不变 | 不变 | 不变 |
| `tasks.archive` | `task_id`、`expected_revision` | 取消关注：设置 `archived_at`，之后的运行不再记录结果，归档前已产生但未汇报的结果也不再汇报（`task_archived`）；会话本身不动，再次 followup 或 link 即重新关注 | 本人 |
| `sessions.rename` | `session_id`、`title ≤ 128` | 改标题并推送 `SESSION_TITLE` | 本人 |

不提供删除会话的工具：删除由用户在界面上操作（删除边界）。

### 6.3 规则

- **权限：** 助理永远等于用户本人：写操作只限本人拥有的会话（与 `_require_session_owned` 一致），每次调用都核对成员资格。
- **共享会话（D4）：** 会话 `visibility == "workspace"` 时，`tasks.followup` / `assets.attach` 不直接投递：工具在主会话里发一张问题卡（`assistant/confirmations.py`），卡上是目标会话、要发送的**完整原文**和附带的文件名；用户点“确认发送”后，模型用同样的内容再调用一次才会投递，且一张卡只放行一次（发送失败会退回，可再用）；点“取消”则不发送。卡片记录的是任务 + 原文 + 附件的摘要，换了内容就要重新确认。
- **共享会话不开自动继续：** 自动继续的每一步都无法逐条确认，所以给工作区可见的会话开启“继续执行授权”会被拒绝（`ASSISTANT_CONTINUATION_SHARED`），会话之后才变成共享的，协调轮也不能再自动追加。
- **私人内容不外流：** 系统提示要求发往共享会话的文字只包含完成任务所需的项目信息；确认卡展示原文，用户可以取消。
- **并发：** 沿用现有收件箱语义：会话正在运行时 followup 排队；用户在界面直接发的消息仍可打断。
- **防循环：** 同一个任务在没有新的用户输入时，助理最多自动追加 3 次；超过后工具返回错误，要求先询问用户。
- **标题：** 助理发的输入是合成消息，不触发自动标题，所以 `tasks.submit` 必须给标题。

## 7. 会话结束通知个人助理

### 7.1 现状

- 每个普通会话的运行结束都会走 `question/runtime.py:finish_run` → `notifications/events.py:38 task_finished`，给**用户**发消息中心通知和推送（`task:{run_id}:terminal` 去重）。
- 被关注（关联 `AssistantTask`）的会话：运行由收件箱启动，结算时在同一事务里写 `TaskResult`（`assistant/results.py:52`），提交后投递到助理收件箱（`on_execution_result_committed` → `deliver_task_result` → `accept_report_locked`），助理用一个 `report_only` 轮汇报；`recover_assistant_results` 兜底重投。
- 没被关注的会话不通知助理（符合 D3）。

### 7.2 V2 改动

1. **结果带摘要。** `record_execution_result_locked` 记录结果时取最终回复文字，脱敏后截成 ≤4,000 字符存进 `TaskResult.summary`；同时记下改动文件数（`Session.files_changed` 等）。
2. **汇报轮不再强制读取。** 汇报输入直接包含摘要与元数据（任务标题、项目、成功/失败、改动文件数），按非人类输入格式包裹（“Platform-delivered context. This is not a new human message or approval.”）。助理需要细节时可以调 `results.read` / `history.read`，但不再要求覆盖全部来源才能结算；回答非空即结算为 `processed`。
3. **提问与审批。** 被关注会话里的提问、权限审批会出现在助理页的待答事项里，关注列表块也会显示“等待回答 N 条”；P4 起提问会作为一条通知进入助理收件箱，助理可以按 D6 代答或转告用户。
4. **锁顺序。** 不在执行会话的结算事务里直接写主会话收件箱（现有代码一律先锁主会话再锁执行会话），继续沿用“同事务记录 TaskResult、提交后投递、恢复任务兜底”的 outbox 模式。
5. **合并唤醒。** 主会话一次只处理一个输入，汇报与普通输入交替（`queue.py`）；短时间内多个结果会排队逐个汇报，不会并发打断用户。
6. **每日简报（P5）：** 每天第一次打开助理页（或每天 9 点，有活动的用户）生成一条简报：等待用户处理的事项、关注会话的状态变化、失败的任务；未关注会话只在简报里出现。

## 8. 个人助理自己的长期记忆

### 8.1 现状（代码核对）

- 记忆抽取在每轮结束、收件箱结算的同一事务里登记（`agent/loop.py:2971` → `agent/inbox.py:1096` → `memory/jobs.py:305 record_completion_locked`），但 `parent_id` 非空或 `memory_isolated(session)` 时直接跳过（`jobs.py:316`）。助理主会话 `kind=="assistant"` 属于隔离，**它的对话从不进入长期记忆**。
- 抽取只读用户原话：`_user_sources`（`jobs.py:174-208`）只取 `role=="user"`、片段 `origin=="human"`、非合成、非忽略的文本；`_validate_sources_locked`（`jobs.py:592`）再核对 `origin_ref.actor_user_id`。任务结果、委派提示、系统恢复、助理回复、工具输出天然被排除——**“只从你的原话学”在抽取管线里已经成立**。
- 自动抽取的记忆一律带项目：`Session.project_id` 不能为空，抽取时把它写到记忆上（`jobs.py:258,288`、`service.py:631`）；召回条件是“无项目 或 当前项目”（`memory/policy.py:41-47`）。所以在项目 A 说的“我喜欢简洁回复”到了项目 B 就用不上。
- 助理的存储项目是工作区默认项目，查询时没有按所有者过滤（`assistant/service.py:43-55`）；共享工作区里它可能属于别的成员，`resolve_access_scope` 会拒绝。
- 普通会话的记忆注入：`run_memory_context`（`memory/orchestrator.py:70`）产出常驻核心块（最多 12 条、3,000 字符）和相关条目（最多 10,000 字符），拼在最新一条用户消息前（`loop.py:1879`）。助理的 `_build_system_prompt` 只返回 `ASSISTANT_PROMPT`（`loop.py:3189`），**什么都不注入**。
- 普通会话的 `creator_context.propose_memory` → 提问卡 → `question/continuation.py:125-154` 记住/不用记；人工接口 `POST /memories`、`PATCH`、`/forget` 都有服务函数（`memory/service.py` 的 `create_note`、`edit_note_in_session`、`forget_memory`）。
- 没有定期合并、去重、衰减记忆的任务；`hit_count`、`last_hit_at` 写了但没人读。

### 8.2 V2 的三层记忆

| 层 | 内容 | 存储 | 何时进入上下文 |
| --- | --- | --- | --- |
| 用户画像 | 身份、偏好、沟通风格、常用项目 | 现有记忆表里 `project_id IS NULL` 的个人类条目（`USER_PROFILE`、`PREFERENCE` 等） | 每轮常驻：复用 `run_memory_context` 的核心块（≤12 条、≤3,000 字符），全项目范围 |
| 事实与偏好 | 具体事实、约定、纠正 | 现有 `user_memories`（个人或项目） | 每轮按最新用户消息检索相关条目（≤10,000 字符）；也可用 `memory.search` 主动查 |
| 工作日志 | 每天做了什么、用户是否满意 | 每日简报写入主会话，压缩后沉淀为摘要 | 按需（`history.read` / `memory.search`） |

### 8.3 写入

1. **主会话的用户原话进入抽取。** 新增抽取资格判断，与 `memory_isolated` 分开：`kind=="assistant"` 且无父会话的主会话可以抽取；旧的 `assistant_isolated` 执行会话仍然排除。主会话抽出的记忆一律写成**个人范围**（`project_id=None`），不挂到默认存储项目上。改动点：`jobs.py:316/401/429/515-521/595-599`、`_create_completion_locked`、`enqueue_completion_locked`、`_acl_hash`、`service.py:236-239/317/321-325`、`assistant/memory.py:83-86`。
2. **个人类事实跨项目。** 在任何会话里抽出的 `personal.*` 键（用户画像、偏好类）写成个人范围；`project.*` 键仍写当前项目。这样在项目 A 说的偏好，项目 B 也能用。
3. **助理显式记忆工具**（ChatGPT `bio` 工具的做法）：
   - `memory.remember(summary, quote, scope=personal|project:<id>)`：引用的原话必须是本主会话里用户本人发的消息（复用 `decisions._human_sources` 的核对方式）。普通内容直接生效，回复下方显示“已记住 · 撤销”；命中敏感内容（`MemorySensitiveContent`）转为待确认，显示确认卡。
   - `memory.update(memory_id, summary, quote)`：同样要求用户原话；走 `edit_note_in_session`，带版本号。
   - `memory.forget(memory_id)`：用户在本轮明确要求忘记时直接执行（`forget_memory` 软删除）；助理自己判断要删时只能给出确认卡，由用户点。
   - 尊重“暂停记忆”设置（`saving_paused_locked`）。
4. **撤销。** “撤销”按钮调用现有 `POST /memories/{id}/forget`，由用户本人点击。

### 8.4 读取与撤回不追溯（D1）

- 每轮注入的画像和相关条目都从 SQL 重新查询，只经过便宜的当前检查：范围（`resolve_access_scope` + `predicates`）、`active_memory_predicates()`（已忘记、已拒绝、过期、墓碑都会排除）。**删除的记忆下一轮就不会再注入。**
- `memory.search` / `memory.read` / `knowledge.*` 的工具输出只在本轮交给模型；后续轮次替换为“需要时重新查询”的占位，`history.read` 也只返回占位，不把记忆正文重放给模型。工具结果本身留在只有本人能看的主会话记录里（D1：已有聊天记录不改写）。这样“忘记后不再使用”的承诺不依赖每步重验。
- 去掉：`memory_provenance.capture/validate` 与 `knowledge_provenance` 的每步重验、`assistant.business.read` 事件里保存的整份投影、元数据/依赖哈希、`source_set_hash`、`_human_identity` 的片段版本核对、加密游标。
- 助理已经写进回答里的内容不改写（D1）。忘记卡的文案改为：“以后不会再使用这条记忆；已有聊天记录里出现过的内容不会被改写。”

### 8.5 纠错闭环与定期整理

- 用户纠正助理（“不对，我要表格”）时，助理用 `memory.remember` 记成一条反馈类记忆，附原因；抽取管线里的自动纠正（`apply_reconciliation`）继续生效。
- 新增每晚整理任务（复用 `MemoryIndexWorker` 的周期）：合并同一 `fact_key` 的重复条目、冲突时新条目替代旧条目并保留修订、长期未命中的低重要性条目降权（读取 `hit_count`、`last_hit_at`）。整理只改写记忆表，不删除来源。

## 9. 项目长期记忆与项目档案

### 9.1 现状

- 记忆、来源、知识页、记忆文档都带 `project_id`；普通会话抽取时按会话所在项目入库；召回是“个人 + 当前项目”。
- 没有项目档案：`Project` 只有名称和描述；知识页不能由 agent 自己编写（必须引用已确认的记忆来源，且拒绝 `assistant_inference` 来源）。

### 9.2 项目档案

- 新表 `project_briefs`（第 5 节），每个用户每个项目一份，≤6,000 字符，建议结构：目标、技术栈、约定、当前进度、关键决定、重要会话。
- **注入：** 本人在该项目里的每个会话（包括助理新建的会话）在系统提示里附带 `<project_brief>`（≤6,000 字符）。助理派活时只需写本次要做什么，不必塞背景。
- **维护：** 助理用 `projects.brief.update(project_id, content, expected_revision)` 改写；收到该项目的结果汇报后，按系统提示更新“当前进度”。用户可以在界面查看和编辑（`GET/PUT /api/projects/{id}/brief`）。
- **只写项目信息：** 档案会进入该项目所有会话（可能是工作区可见会话），系统提示禁止写入个人信息；写入走与记忆相同的敏感内容检查，命中则拒绝。
- **工具：** `projects.brief.read(project_id)`、`projects.brief.update(...)`。

### 9.3 作用域

- 项目 A 的会话只加载：用户画像（个人范围）+ A 的记忆 + A 的档案；绝不加载 B。
- 助理主会话检索时用全项目范围（`include_all_projects=True`），但写回答或派活时只引用与目标项目相关的内容。
- 跨项目提炼：多个项目都出现的同类偏好（如前端都用 Tailwind），助理用 `memory.remember(scope=personal)` 提升为个人记忆。
- 每个工作区一个助理（`uq_sessions_active_assistant`）；跨工作区默认不共享记忆。

## 10. 统一待办与审批（P4）

- `requests.list` 从“只看关注会话”扩大到本人所有会话里待回答的提问和待审批的权限请求，返回会话、项目、问题摘要。
- `requests.reply` 可以回答本人任意会话里的**提问**，回答记录 `answered_by="assistant"`，界面显示“由个人助理代答”（D6）。
- 以下永远只能由用户本人操作，助理只能转告并给出链接：权限审批（不可逆、对外、花钱、删除）、记忆提议确认（`memory_proposal`）、遗忘确认（`memory_forget`）。
- 被关注会话出现新提问时，进入助理收件箱，助理在下一轮决定代答还是转告。

## 11. 前端

### 11.1 现在为什么慢（实测与代码）

助理页面复用普通聊天的 `ChatSessionView`，但外面多包了一层“来源核验”，并且后端把实时事件全部改写成了“请重新拉取”：

| 现在 | 位置 | 后果 |
| --- | --- | --- |
| 后端把助理会话和助理执行会话的所有实时事件（消息、片段、工具、状态、提问、权限）改写成不带内容的 `assistant.history.changed`，并丢掉增量 | `backend/session/public_events.py:9-43` | 页面收不到任何内容推送，每一步运行都变成“重新拉取 + 重新核验” |
| 历史接口 `GET /api/agent/session/{id}/history?turns=8` 对助理会话做完整来源投影，对执行会话做执行投影 | `backend/api/sessions.py:712-745` → `assistant/public_history.py:124-135` | 一次刷新约 1,190 条 SQL |
| 每次刷新再调一次 `GET /api/assistant/messages` 做来源核验；核验结果回来之前回答显示“正在准备可核验的汇报…” | `frontend-v2/src/features/chat/api/assistant-transcript.ts`、`lib/source-projection.ts`、`AssistantTurn.tsx:104-117` | 一次刷新两次重验；页面隐藏时永远显示待核验 |
| 历史屏障（history barrier）：任何提示、重连、事件页都会抬高屏障，已显示的行立即变回待核验 | `frontend-v2/src/features/chat/api/history-source-proof.ts` | 屏障抬一次就整页重验一次 |
| 轮询：事件每 5 秒；忙时每 1 秒拉增量和会话；待答事项每 5 秒×2 种；每张任务卡每 5 秒；通知目标每 5 秒 | `assistant.ts`、`ChatRoute.tsx:117-122`、`AssistantRequests.tsx`、`AssistantTaskCard.tsx` | 两个页面 700 秒内 508 次历史刷新、153 次核验 |

### 11.2 V2 的做法

1. **助理页 = 普通聊天页。** 后端不再改写助理会话的实时事件，消息、片段、工具增量照常推送；历史接口对助理会话直接返回普通分页结果（与普通会话同一条路径）。
2. **去掉来源核验层。** 删除 `AssistantReadBoundary` / `ExecutionReadBoundary` 的核验门控、`sourceProjection` 的待核验替换、`useVerifiedAssistantCopy` 的点击时重验，以及 stream store 中“已核验消息不接受推送更新”的分支。回答收到即显示。
3. **历史屏障。** 屏障本身只服务于来源核验；核验层去掉后，屏障没有读者，P1 直接删除 `history-source-proof.ts` 及其调用点（前后端同版本发布，不需要兼容期）。本文即该改动的设计评审；代码改动另做一次独立评审。
4. **轮询收敛到事件驱动。** 任务卡、待答事项、通知目标改为随 socket 事件失效，兜底轮询 30 秒；忙时增量沿用普通会话的 1 秒补拉（只拉 `after=` 增量，后端不再重验）。
5. **“由个人助理发送”标记。** 消息带 `origin`（后端从片段数据 `data.origin` 投影出来），`UserMeta` 下显示“由个人助理发送”，复用 `MetaBadges` 的 `BADGE` 样式。
6. **关注列表。** 侧栏“个人助理”下方显示被关注会话（状态点 + 标题 + 需要处理的红点），复用 `CronSidebarJobs` 的插槽和 `CronStatusPill` 样式；点击进入原会话。
7. **确认卡。** 需要用户确认的动作（往共享会话发消息、敏感记忆、删除等）在助理页显示确认卡：动作说明、目标、将发送的原文，按钮“确认 / 取消”。
8. **记忆提示。** 助理自动记住的内容在回复下方显示“已记住：… · 撤销”；待确认的显示“确认记住 / 不要记”。

## 12. 其它 OpenBox 能力：按风险分级开放（P4）

| 能力 | 现在 | V2 |
| --- | --- | --- |
| 知识库 | `knowledge.directory` / `read` 只读 | 不变（写入仍走人工审核流程） |
| 文件资产 | `assets.list` / `attach` | 不变 |
| 定时任务 | `schedules.list/create/update/run` | 不变 |
| 额度 | 无 | `status.credits`：当前余额、本月用量（只读） |
| 云电脑 / 浏览器资源 | 只有界面上的控制接口 | `status.resources`：资源列表与状态（只读）；启动、关闭仍由用户在界面操作 |
| 技能 | 无 | `status.skills`：已安装技能列表（只读） |
| 平台发布 | 无 | `status.publishing`：最近发布任务与状态（只读）；发布动作由执行会话按现有审批完成 |
| 成员 / 权限 | 无 | 不开放 |

分级规则：只读直接做；可撤销的写操作直接做并留审计；对外、不可逆、花钱、删除一律由用户本人确认。

## 13. 安全、隐私与成本

- **身份：** 服务器从认证上下文绑定用户与工作区，模型给出的这些字段无效；每次工具调用核对成员资格与对象所有权。
- **外部内容是数据：** 汇报摘要、读取的历史、网页内容一律按非人类输入包裹，不提升为指令；只有用户本人消息能作为记忆和决定的来源。
- **私人内容不外流（D4）：** 发往工作区可见会话前确认；项目档案只写项目信息。
- **记忆：** 只从用户原话学；记忆读取结果不长期留在历史里重放；敏感内容待确认（D5）。
- **防循环与预算：** 每任务无新用户输入时最多自动追加 3 次；主会话预算沿用 `budget.py`（普通轮 180 秒 / 12 次请求 / 24 次工具，汇报轮 120 秒 / 8 / 16）；并发运行数沿用 `max_concurrent_agents`。
- **审计：** 助理的每个动作都有命令记录（`assistant_commands`）和事件；关注列表与任务卡可追溯。

## 14. 迁移与回滚

- **数据：** 不回填、不删除历史数据。旧任务继续可用；旧的隔离执行会话保持隔离；主会话历史按普通分页读取。
- **结构：** `pb7`（P1）删除闭包缓存结构、新增 `assistant_task_results.summary`；`pb8`（P3）新增 `project_briefs`。降级可回到 `pb6`。
- **代码回滚：** 按提交回退 + `alembic downgrade`。不加双路径开关，避免两套语义并存。
- **兼容：** 前后端同时发布；`/api/assistant/messages` 删除前端调用后一并删除。

## 15. 分期与验收

每期结束：后端相关测试通过（SQLite 全量助理测试 + PostgreSQL 关键用例），前端单测通过，浏览器端到端验收通过，提交。浏览器验收使用本地 QA 账号（`127.0.0.1:3001` + `8081`）。

| 期 | 内容 | 浏览器验收 | 性能目标 |
| --- | --- | --- | --- |
| P1 地基 | 第 4 节全部删除与精简；`pb7`；实时事件正常推送；前端去掉核验层与屏障读取、轮询收敛；汇报带摘要 | 助理回答流式出现、不再出现“正在准备可核验的汇报”；派一个小任务，结果自动汇报；刷新页面历史完整 | 主会话一轮上下文构建 ≤10 条 SQL；历史一页 ≤6 条；挂起检查 ≤3 条；委派到汇报端到端明显快于 283 秒基线 |
| P2 会话管理 | `sessions.list` 扩展、`history.read` 扩大范围、`tasks.link_existing` 放开、`tasks.archive`、`sessions.rename`、D4 确认卡、防循环、“由个人助理发送”标记、侧栏关注列表 | 新建“贪吃蛇”项目：助理新建会话写一个小页面 → 侧栏出现、带标记 → 结果汇报；再让助理在该旧会话里追加修改 → 汇报；共享会话出现确认卡 | 同上 |
| P3 记忆 | 主会话原话进入抽取（个人范围）、个人类事实跨项目、`memory.remember/update/forget`、画像常驻、记忆读取临时化、项目档案（表、注入、工具、接口） | 说“以后回复都用表格” → 显示已记住 → 新一轮按表格回答；撤销后不再注入；敏感内容出现确认卡；任务汇报后项目档案更新，新会话能看到档案 | 画像注入 ≤4 条 SQL |
| P4 待办与能力 | 全局待答事项、代答标记、提问进入助理收件箱、`status.*` 只读工具 | 普通会话里提问 → 助理代答并标注；权限请求只给链接；问“额度还剩多少”得到正确数字 | — |
| P5 收尾 | 每日简报、每晚记忆整理（同键合并、冲突替代、低命中降权）、清理残留代码、全量回归 | 手动触发简报；整理任务在测试数据上合并重复记忆 | — |

## 16. 与 V1 的关系

| V1 章节 | V2 |
| --- | --- |
| 0.3 增量 1–3 的“完整隔离”、7.4 整链隔离 | 被第 8 节替代：主会话可抽取（个人范围）；委派会话仍隔离（委派提示不是用户原话），用户自己会话里的原话照常学习；只从用户原话学 |
| 6.1 “每次读取、提交、模型调用前重新验证必要范围”、7.2/7.3 “撤权后旧目录、摘要、缓存与未来模型调用都必须失效/重查” | 被第 4.2 节与 D1 替代：读取只查当前权限，撤回不追溯；注入的记忆每轮从 SQL 重新查询 |
| 6.1 `history.read` 只读主会话及关联任务 | 扩大到本人任意顶层普通会话 |
| 4.2 链接旧 Session 只限私有隔离 | 放开到本人任意会话，共享会话发消息需确认 |
| 8.2–8.5 Task / Command / Submission / Result / ReadCursor 契约 | 保留，Result 增加摘要，汇报结算去掉覆盖率要求 |
| 11 界面 | 第 11 节：去掉核验层，加关注列表、确认卡、来源标记、记忆提示 |
| [性能分析](PERSONAL_ASSISTANT_PERFORMANCE.md) 的闭包缓存 | 删除（第 4.3、5 节） |

## 17. 实施记录

每期完成后在此更新：提交、测试结果、浏览器验收证据与实测数据。

**P1 实测 SQL（真实 QA 数据，只读，`.local-dev/assistant-web-20261003/v2-p1-sql-profile.json`）：** 历史页 2 条（V1 约 1,187）；主会话上下文构建 9 条（V1 约 1,130）；关注列表 4 条；`history.read` 一页 7 条；W2 挂起检查 7 条（V1 1,124）；首页快照 19 条（批量读取前 178 条）。全部与历史长度、任务数量无关。

| 期 | 状态 | 提交 | 测试 | 浏览器验收 |
| --- | --- | --- | --- | --- |
| P1 地基 | **完成**（2026-10-06） | `926e6950`、`085059ad`、`6468d287`（快照批量读取） | SQLite 助理全量：931 通过、14 跳过、6 失败（均为改动前已有：2 个附件恢复、4 个调度测试的 litellm 全量顺序问题）；助理以外全量无新增失败；前端 vitest 1277 通过（1 个原有超时）；PostgreSQL 迁移 pb6→pb7→pb6→pb7 往返通过 | 无影 W2 只读续接：界面发送→汇报完成 **83.8 秒**（主轮 30.1 秒、执行 43.5 秒、汇报 19.3 秒；V1 边界复用 283 秒、闭包缓存 636 秒）；回答流式显示，无“可核验”占位；执行会话显示“由个人助理发送” |
| P2 会话管理 | 未开始 | — | — | — |
| P3 记忆 | 未开始 | — | — | — |
| P4 待办与能力 | 未开始 | — | — | — |
| P5 收尾 | 未开始 | — | — | — |
