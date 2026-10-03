# 长期记忆实现静态审查与待修复问题

> 日期：2026-10-03（Asia/Shanghai）。分支：`codex/long-term-memory-plan`。
>
> **阶段认定：按用户当前决定，暂记阶段一完成。这不代表整体无风险，也不代表本文问题已修复或已通过本轮运行验证。**
>
> 原始审查版本记录 17 项 **OPEN（未修复）** 问题：5 项 P1、12 项 P2，其中 LTM-016/017 是范围较小的界面缓存/当前性问题。该版本的证据来自静态源码。
>
> **2026-10-03 再次复核：第一轮曾记录“17 项均已修复”，但在 `d9abb57b` 仍发现 LTM-001/005/007/012/015 的遗漏路径，以及暂停设置、上传孤立文件两项问题。本轮在此基线上补修，实际改动、运行验证和限制以第 9 节为准；各项早先的“修复与验证”及第 7、8 节保留为历史记录，不能代替本轮证据。**
>
> 原始审查提交 `125ebc67` 只新增本文档。本轮用户已授权修复残留问题、补充测试、提交并推送；范围不含生产部署、现有业务库迁移、付费模型调用或无关工作区改动。

## 1. 审查基线与证据边界

完整审查基线：[`0a6b8441ae624d3961b48f650e51e6d34e733a38`](https://github.com/arkstudio-ai/OpenBox/tree/0a6b8441ae624d3961b48f650e51e6d34e733a38)。文中所有源码行号链接均固定到该 SHA，不随后续修复漂移。

审查的是计划提交 `a0ea9ef1040f0c6a2fa6c6aedba74cf3a67a0af9` 之后的四个实现提交及其必要调用链：

| 完整提交 SHA | 提交主题 |
|---|---|
| `5a4889d841086091d617f4e5cd779fb917b75477` | feat(memory): implement long-term memory with scoped recall and Jev routing |
| `42a8c201f3cde9560827c8abe0e44d5928ba2123` | fix: restore sandbox searches and filter irrelevant memory recall |
| `384dda197eef637deeac816b8883d883c747c113` | feat(memory): automate knowledge curation and document ingestion |
| `0a6b8441ae624d3961b48f650e51e6d34e733a38` | feat(memory): one 2C knowledge page and a hardened memory loop |

设计与实现说明：[长期记忆计划](LONG_TERM_MEMORY_PLAN.md)、[长期记忆实现记录](LONG_TERM_MEMORY_IMPLEMENTATION.md)、[个人自动知识库](CONSUMER_KNOWLEDGE_IMPLEMENTATION.md)、[Wiki 平台实现](WIKI_PLATFORM_IMPLEMENTATION.md)、[Wiki 阅读实现](WIKI_READER_IMPLEMENTATION.md)。计划中的历史暂停说明和实现记录中的历史测试结果，不替代当前源码，也不自动扩大本轮授权。

审查范围覆盖权限与来源、显式写入/更正/遗忘、回合完成与自动抽取、Task 委派、主模型/压缩投影、SQL–Qdrant 索引与恢复、Wiki 编纂/增量/文档、Jev 路由及真实调试/知识页调用链，并阅读相关既有测试判断覆盖。类型化 Profile、多阶段工作流和各导出编码器未逐函数展开；不能把本文视为全仓每一行均已审完。

用户已亲测且反馈效果不错，该反馈与边界缺陷可以同时成立。本文没有重复运行测试、浏览器或服务，没有连接业务数据库、Qdrant 或 Blob，没有调用付费模型；既有文档里的测试数量与浏览器结果不是本次审查的实测结果。所有“验收条件”仅描述后续修复后的预期行为，本轮均未执行，也不要求建设固定质量测试集。

已检查工作树及相关 `.agents/skills` 位置，未找到适用审查技能；读取了适用的 [backend/AGENTS.md](../backend/AGENTS.md) 和仓库验证说明。审查期间并行任务明确请求 `gpt-6-astra / max`，可见接口未提供可独立核实的运行时模型标识。

## 2. 总体判断与问题索引

主链路已经具备个人助理的使用基础：持久 job/outbox、SQL 修订与来源、混合检索、模型发送前复核、独立 Wiki 核心、调试及独立重跑均有实际接点。尚未闭合的是证据可信度、派生输出的权限、遗忘/撤权传播以及并发与故障恢复。阶段一暂记完成后，可以继续个人受控使用；在扩大到多人工作区或依赖严格遗忘承诺之前，应优先处理 P1。

P1 表示影响证据可信度、个人数据隔离或撤销后继续使用的重要缺陷；P2 表示条件触发的正确性、恢复、清理或界面当前性问题。等级不表示本轮测得的出现频率。

| ID | 等级 | 问题 | 状态 |
|---|---|---|---|
| LTM-001 | P1 | PERSONAL 记忆正文进入同工作区成员可读取的会话历史 | 已修复；自动化测试与浏览器验证通过 |
| LTM-002 | P1 | 父助手生成的子任务提示被当作用户原话进入自动记忆抽取 | 已修复；自动化测试通过 |
| LTM-003 | P1 | creator_context 兼容读取绕过更正、遗忘与 compaction 再鉴权 | 已修复；自动化测试通过 |
| LTM-004 | P1 | 失败或中断的调试记录可绕过遗忘后的正文保护 | 已修复；自动化测试通过 |
| LTM-005 | P1 | 后台多次模型调用之间缺少新的来源与权限检查 | 已修复；自动化测试通过 |
| LTM-006 | P2 | 重生成较后回答会使保留前缀的有效记忆失效 | 已修复；自动化测试通过 |
| LTM-007 | P2 | 来源 hash 墓碑越过选定来源与项目范围 | 已修复；自动化测试通过（SQLite 与 PostgreSQL） |
| LTM-008 | P2 | 来源集合去重丢掉同一消息里的不同事实 | 已修复；自动化测试通过 |
| LTM-009 | P2 | 旧索引 worker 可删除已完成的新版本向量 | 已修复；自动化测试通过 |
| LTM-010 | P2 | DEAD UPSERT 让已完成的遗忘清理永久显示 pending | 已修复；自动化测试通过 |
| LTM-011 | P2 | 删除编辑过的文档遗漏旧修订来源正文 | 已修复；自动化测试通过 |
| LTM-012 | P2 | 原文件删除失败缺少持久恢复任务 | 已修复；自动化测试通过 |
| LTM-013 | P2 | 旧删除请求可能误删刚重新上传的原文件 | 已修复；自动化测试通过 |
| LTM-014 | P2 | 一次 Wiki 合并契约错误会停止相同输入的自动整理 | 已修复；自动化测试通过 |
| LTM-015 | P2 | 仅禁止记忆读取时，明确的任务状态请求也被跳过 | 已修复；自动化测试通过 |
| LTM-016 | P2 | 调试详情刷新授权失败后仍渲染旧缓存正文 | 已修复；自动化测试通过 |
| LTM-017 | P2 | 记忆详情用清理状态替代当前正文和来源校验 | 已修复；自动化测试通过（SQLite 与 PostgreSQL） |

## 3. P1：优先修复

### LTM-001 · P1 · PERSONAL 记忆正文进入同工作区成员可读取的会话历史

**状态：已修复；自动化测试与浏览器验证通过。** 原审查结论如下。

**静态证据与调用链：** [backend/tool/memory_tools.py:144](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/tool/memory_tools.py#L144) 和 [backend/tool/memory_tools.py:195](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/tool/memory_tools.py#L195) 返回个人记忆/来源正文；[backend/agent/processor.py:1300](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/agent/processor.py#L1300) 将工具输出原样持久化。[backend/api/sessions.py:605](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/api/sessions.py#L605) 的 /message 及同文件 619 行的 /history 只检查工作区，再以 session.user_id 读取所有者历史。[backend/db/models/part.py:19](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/db/models/part.py#L19) 仅移除工具回放身份字段，没有按读取者过滤记忆正文。工作区邀请成员与共享会话列表是已有可达入口，并非假设不存在的共享功能。

**可触发条件：** A、B 是同一工作区的有效成员。A 保存 PERSONAL 记忆，并在自己的会话调用 memory_search 或 memory_read_sources；B 通过工作区会话列表取得该 Session ID，再读取 /message 或 /history。B 无需获得个人记忆 API 的所有者权限。

**影响：** 个人记忆的派生副本被工作区共享历史扩大受众。源记忆 API 的 actor 过滤正确，也不能弥补这条旁路。单成员工作区不满足该触发条件；不是任意外部用户可匿名读取。

**建议：** 公开 ToolPart 仅保存安全引用，或在历史/SSE 等公共投影处按实际访问者重新鉴权正文；同时明确主模型输出所在会话的受众，不能把会话共享默认视为个人记忆共享许可。

**后续可验证验收条件：** 同工作区 B 读取 A 的会话历史和相关公共事件时，既拿不到 A 的 PERSONAL 工具正文，也不能借来源 ID 回读；A 仍可在授权路径读取。保留普通共享会话行为，任何显式共享必须有独立权限依据。

**证据边界：** 共享历史机制原已存在；本项确认的是新记忆工具把 PERSONAL 正文送入该共享持久面。当前结论来自静态调用链，未执行跨账号 HTTP 请求。

**修复与验证（2026-10-03）：** 记忆读取工具（memory_search、memory_read_sources、current_task_state，以及 creator_context 的 get_user_context / search_memories / list_active_memories）的输出，在写入 Part、AgentEvent 和 SSE 时一律替换为只含引用的标记（`backend/session/agent_event_log.py` 的 `strip_memory_text`，经 `sanitize_public_part_data` 覆盖保存、事件与推送）。读取 /message、/history 时对本规则之前已保存的旧记录同样替换（`backend/session/session.py` 的 `_assemble`）。主助理只在同一回合内经 `backend/memory/tool_projection.py` 按当前权限重读正文，之后回合与压缩只保留引用。聊天里的工具卡片改为说明"记忆内容只在当时提供给助手，不保存在聊天记录里"（`frontend-v2/src/features/chat/components/tool/ToolOutput.tsx`）。分类定义集中在无依赖的 `backend/memory/transient_tools.py`，保存与投影共用。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_tool_projection.py::test_chat_history_and_live_events_keep_references_never_memory_text`（memory_search / memory_read_sources / creator_context 三种参数）：Part 行、AgentEvent、与 /message 同源的 `get_messages`、SSE 发布内容均不含记忆正文且保留引用；同一回合的模型投影仍含正文。`::test_history_saved_before_the_rule_is_read_without_memory_text` 覆盖旧数据。前端 `ToolOutput.test.tsx` 两例。浏览器（本地 QA 账号，openbox_memory_dev）：新对话中让助手用记忆搜索查询跑步计划，助手依据当回合重读的正文正确作答；该会话 /history 返回的 memory_search 工具输出只有 `stored_without_text` 标记与记忆引用，工具卡片显示上述说明。
- 残余限制：助手回复本身如果复述了记忆，仍是聊天内容的一部分，随会话可见；memory_search 的查询词是助手自己写的，保留可见；"忘记这条记忆"确认卡片显示该条摘要。共享工作区已加 `shared_chat` 提示（见第 8 节），它是给助手的指令，不是硬性保证。未用两个真实账号执行跨账号 HTTP 请求，结论来自同一读取路径的自动化测试。

### LTM-002 · P1 · 父助手生成的子任务提示被当作用户原话进入自动记忆抽取

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/tool/task.py:196](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/tool/task.py#L196) 将模型生成的 args.prompt 交给子任务；[backend/agent/subagent_runtime.py:417](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/agent/subagent_runtime.py#L417) 以用户消息、synthetic=False 写入，follow-up 在 746 行同样处理。[backend/agent/loop.py:2514](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/agent/loop.py#L2514) 与 [backend/agent/inbox.py:1021](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/agent/inbox.py#L1021) 在子任务成功后也记录抽取完成点。[backend/memory/jobs.py:181](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/jobs.py#L181) 仅按 role、synthetic 等标记筛选，198 行将来源标为 user_statement；[backend/memory/grounding.py:74](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/grounding.py#L74) 的独立核验只收到正文。

**可触发条件：** 自动抽取及自动知识策略已启用。父助手的 task prompt 含自己的推断、转述或此前召回的事实；子任务成功完成。即使没有真实用户 Inbox 项，child trigger 和终止消息仍可形成抽取边界。

**影响：** 确定的缺陷是作者与证据来源归属丢失。模型生成的背景具备成为直接用户证据、继而 SYSTEM_VERIFIED 记忆或更正依据的路径，可能形成记忆自我强化。某段文本最终是否准入仍取决于抽取和核验模型，不能把可能性写成每次必然发生。

**建议：** 保存原始作者、Task activation 类型及来源链；排除模型委派消息作为直接 user_statement，必要时追溯原始用户消息。子代理读取授权记忆的能力可以保留。

**后续可验证验收条件：** 父助手自行写入任务提示的虚构偏好不能形成用户原话来源或自动准入记忆；真正用户陈述仍可经明确来源链被提取。spawn、follow-up、fork 和恢复路径使用相同规则。

**证据边界：** 未调用模型验证某个 prompt 的准入概率；缺陷依据是已可达的来源标记和冻结路径。

**修复与验证（2026-10-03）：** 子任务（`Session.parent_id` 非空）不再记录自动抽取完成点（`backend/memory/jobs.py` 的 `record_completion_locked`），已排队的任务在冻结/提交时以 `delegated_session` 取消（`_validate_sources_locked`）。真正的用户陈述仍由父会话自己的完成点抽取。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_branch_removal.py::test_a_delegated_task_prompt_is_never_taken_as_the_persons_words`。
- 残余限制：子代理读取授权记忆的能力保留；没有追溯"父助手转述了哪句用户原话"的来源链，子会话里的内容整体不进入自动记忆。

### LTM-003 · P1 · creator_context 兼容读取绕过更正、遗忘与 compaction 再鉴权

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/tool_projection.py:20](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/tool_projection.py#L20) 的临时工具名单仅包含 memory_search、memory_read_sources、current_task_state。仍可调用的 creator_context 在 [backend/tool/creator_context.py:235](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/tool/creator_context.py#L235) 返回正文，search_memories/list_active_memories 在 263–287 行同样没有 transient_memory_refs。主模型使用 [backend/agent/loop.py:1573](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/agent/loop.py#L1573) 的投影，压缩使用 [backend/agent/compaction.py:55](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/agent/compaction.py#L55)，两者均漏掉该兼容入口。

**可触发条件：** Session B 通过 creator_context 读取来自 Session A 的记忆；随后在 A 的来源或记忆上执行更正、遗忘或撤销。继续 B 的后续回合，或让 B 进入压缩。

**影响：** 历史工具正文继续成为模型输入，也可能进入长期压缩摘要。此项是同用户旧内容继续复用，与 LTM-001 的跨用户共享历史不同；并不承诺能够撤回用户已经看到的回答。

**建议：** 全部 creator_context 记忆结果采用版本化引用和统一投影；旧的无版本结果默认要求重新读取。检查兼容写入/确认返回是否也携带记忆正文，避免另留入口。

**后续可验证验收条件：** 更正、遗忘或撤销后，旧 creator_context 结果不会原样进入新的模型输入或压缩正文；仍有效的来源可在当前权限下重新读取。原始聊天保留语义与临时召回复用语义分开。

**证据边界：** 未运行真实压缩或模型请求；依据是工具仍可发现调用且投影选择逻辑遗漏。

**修复与验证（2026-10-03）：** creator_context 的三种读取改为携带版本化引用（`transient_memory_refs`，operation 为 `creator_context`），由统一投影处理：同一回合按当前权限重新执行同一读取（`backend/memory/context.py` 的 `legacy_read`，不重复计命中），之后回合与压缩只保留仍有效的引用；此前保存、没有引用的旧结果一律按"需重新读取"处理，不再原样回放。保存与提议类动作不受影响；retrieval_v2 关闭的账号仍可正常读取。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_tool_projection.py::test_creator_context_reads_are_fresh_in_turn_and_citations_later`（三种读取；更正/遗忘后当前回合与后续回合都不再出现旧正文）、`::test_creator_context_reads_work_without_retrieval_v2`、`::test_creator_context_saves_are_not_memory_reads`，以及原有压缩回归。
- 残余限制：write_memory / propose_memory 的返回内容来自本会话刚给出的摘要，未改。

### LTM-004 · P1 · 失败或中断的调试记录可绕过遗忘后的正文保护

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/orchestrator.py:119](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/orchestrator.py#L119) 先单独保存含 candidates 正文的步骤，146–151 行仅在成功尾部写入来源引用；159–161 行失败收尾不传引用。[backend/memory/observability.py:94](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/observability.py#L94) 将 source_refs 设为空数组，而 [backend/memory/observability.py:216](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/observability.py#L216) 对空引用执行 all(...) 得到真，227 行据此返回旧步骤正文。

**可触发条件：** debug_view 已启用。检索步骤保存后，任务状态或稳定背景读取等后续步骤失败；也可在正文与依赖两个事务之间退出进程。随后遗忘被召回的记忆或撤销其来源，但调试 run 所在会话和原问题仍有效，再查看该 run。

**影响：** 失败/未完成 run 的旧候选正文仍可读，来源复核实际上没有覆盖到这些正文。正常成功 run 的依赖校验不能覆盖此窗口。

**建议：** 正文步骤与其依赖引用原子保存；失败收尾保留已登记依赖；未完成依赖登记时默认隐藏正文。run 状态、步骤状态和正文可见资格应明确区分。

**后续可验证验收条件：** 分别在检索后异常和步骤保存后进程退出的边界构造未完成 run；遗忘或撤销来源后，详情不返回旧候选正文。有效的纯元数据步骤仍可排查，不能靠 all([]) 给正文授权。

**证据边界：** 并发/退出时序未执行复现；已静态核实独立事务与空依赖放行条件。

**修复与验证（2026-10-03）：** 调试步骤与其来源引用原子合并保存（`backend/memory/observability.py` 的 `add_debug_step(..., source_refs=...)` 与 `_merge_refs`），失败收尾合并而不覆盖已登记引用；读取时只有已完成（SUCCEEDED/DEGRADED）或已登记引用的 run 才可能显示正文，空引用不再经 `all([])` 放行。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_debug_api.py::test_a_failed_or_unfinished_run_never_shows_bodies_it_cannot_recheck`（失败与未完成、有无引用的组合；遗忘后不再返回旧候选正文）、`::test_an_interrupted_run_keeps_its_step_sources_for_rechecking`，原有 `test_changed_sources_hide_old_snapshot_and_block_replay` 继续通过。
- 残余限制：进程在两个事务之间退出的时序由"未完成且无引用即隐藏"覆盖，未做真实进程终止实验。

### LTM-005 · P1 · 后台多次模型调用之间缺少新的来源与权限检查

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** Wiki 在 [backend/memory/wiki/automatic.py:53](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/wiki/automatic.py#L53) 等待编纂后，于 55–66 行继续核验冻结来源。维护重编的 [backend/memory/wiki/maintenance.py:122](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/wiki/maintenance.py#L122) 只检查策略、开关和额度，不检查当前来源/ACL；[backend/memory/wiki/provider.py:71](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/wiki/provider.py#L71) 会实际发送正文。普通抽取同样在 [backend/memory/extraction.py:327](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/extraction.py#L327) 冻结输入、等待抽取后，于 343–350 行继续核验/协调，直到 352 行才提交复核；[backend/memory/grounding.py:49](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/grounding.py#L49) 的核验输入仍是旧正文。

**可触发条件：** 自动抽取，或带 maintenance_id 的 Wiki 自动维护重编已开始；在首个模型请求等待期间，用户遗忘来源、删除来源会话或失去工作区访问权。首个请求返回后，流水线进入第二次模型核验。

**影响：** 最终提交/CAS 可以阻止失效内容发布，但阻止不了撤权后新发起的正文外发。Wiki 组织任务的 reserve_call 存在 inventory 重验，本项不把所有组织调用一概视为同样缺失。

**建议：** 每次新的 provider 请求前统一检查当前租约、scope/ACL、来源和相关记忆版本；校验应覆盖核验及协调步骤，不能依附于可选预算钩子。撤销时取消尚未发送的后续工作。

**后续可验证验收条件：** 用可暂停的 provider 替身停在首个响应边界，撤销来源或权限后恢复；第二次请求计数保持为零，任务明确终止且不发布。权限未变化的正常链路仍完成。已经发出的首个请求与尚未发出的新请求须区分。

**证据边界：** 未发外部请求或重现撤权竞争；结论针对可成立的异步控制流，不声称能撤回已发送数据。

**修复与验证（2026-10-03）：** 抽取在每次后续模型调用前（核验、协调规划、修订核验）重新检查租约、开关、暂停、来源、ACL 与相关记忆版本（`backend/memory/jobs.py` 的 `recheck_extraction_input`，与提交共用 `_check_frozen_locked`；`prepare_reconciliation(before_call=...)`）。Wiki 编纂用 `CheckedModel` 在每次模型调用前重读任务的来源、权限与目标（`backend/memory/wiki/worker.py`），`compile_automatic(recheck=...)` 在核验调用前同样复查，不再依赖可选预算钩子。撤销后任务取消（来源/暂停/权限）或重试（记忆版本变化，重试时重新冻结）。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_provider_rechecks.py`：抽取期间暂停会话→核验调用次数为 0 且任务 CANCELLED；核验期间遗忘被比较的记忆→协调调用为 0；规划期间暂停→修订核验不发出；权限未变的链路仍完成全部调用；Wiki 编纂期间遗忘→核验调用为 0、任务 CANCELLED、无候选。
- 残余限制：已经发出的请求无法撤回；检查与发送之间仍有很短的窗口。Wiki 组织的概念抽取沿用既有 `reserve_call` 的 inventory 复查。

## 4. P2：正确性、清理与恢复

### LTM-006 · P2 · 重生成较后回答会使保留前缀的有效记忆失效

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/service.py:294](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/service.py#L294) 要求 source.branch_id 等于整个 Session 最新 surface.messages_removed 事件；214–225 行批量路径相同。正常重生成经 [backend/session/session.py:1879](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/session/session.py#L1879) 只删除目标及之后的消息，并在 1909 行追加删除事件，保留更早用户事实。

**可触发条件：** 同一会话早期回合已经抽取有效记忆，之后生成另一个回答；只重新生成较后的回答，或删除较后的失败回合。早期消息和 Part 仍保留。

**影响：** 全会话 branch ID 改变，使早期来源一并失败，影响管理列表、稳定背景、检索和依赖 Wiki。现有恢复扫描补缺失任务，并不恢复已成功旧来源的读取资格。

**建议：** 按精确 message/part 是否仍位于当前 canonical 有效内容及其修订/hash 判断；保留前缀不应因后缀重试整体失效。

**后续可验证验收条件：** 重生成第二个回合后，第一个未受影响回合形成的记忆仍可读、可检索、可支撑 Wiki；确实删除的后缀证据必须失效。覆盖批量和单条来源校验。

**证据边界：** 现有分支检查覆盖被移除的来源；本项针对仍保留的前缀，未运行重生成。

**修复与验证（2026-10-03）：** 来源可用性改为判断这条来源消息本身是否已被移除（`backend/memory/service.py` 的 `SourceFacts.removed` / `_source_is_available`），不再要求整个会话的分支 ID 不变；批量与单条校验一致，抽取任务也不再比较分支。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_branch_removal.py::test_regenerating_a_later_reply_keeps_what_an_earlier_turn_taught`、`::test_removing_the_statement_itself_still_withdraws_its_memory`、`::test_pending_extraction_survives_a_later_regenerate`。
- 残余限制：依赖 surface.messages_removed 事件记录的消息 ID；更早没有该事件的历史会话按原样处理。

### LTM-007 · P2 · 来源 hash 墓碑越过选定来源与项目范围

**状态：已修复；自动化测试通过（SQLite 与 PostgreSQL）。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/service.py:196](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/service.py#L196) 及 278–281 行按 user/workspace/source_hash 拒绝来源，忽略项目和来源身份；[backend/memory/service.py:879](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/service.py#L879) 的遗忘接口却只接受明确 source_ids，889–906 行仅按这些 ID 计算影响并记录带 project_id 的墓碑。

**可触发条件：** 同用户在项目 A、B 分别手动保存相同正文的独立记忆，例如“项目预算为 1000 元”。仅选择 A 的来源副本执行遗忘，再读取 B 的记忆或尝试创建同文的独立记录。

**影响：** B 的不同来源也被判不可用，却不在操作返回的受影响记忆清单中。新同文来源也可能被拒绝。入队/候选抑制另有按项目规则，读写语义不一致。

**建议：** 以稳定来源身份与版本限定删除；hash 防重复导入需绑定原始来源及项目，不能将独立同文材料全部当成同一目标。

**后续可验证验收条件：** 删除 A 的指定来源后，B 的独立同文记忆继续有效且不被加入清理；真正同一被遗忘来源的重放仍受抑制。返回影响清单与实际停止使用范围一致。

**证据边界：** 这是过度抑制和范围误伤，不是跨用户数据读取；未创建或删除实际记录。

**修复与验证（2026-10-03）：** 来源 hash 墓碑只在同一项目、且证据时间不晚于遗忘时间时生效（`backend/memory/service.py` 的 `tombstoned_hashes` 与 `_copy_of_cleared`）；精确来源 ID 的墓碑仍绝对生效。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_source_clearing.py::test_clearing_one_project_source_spares_the_same_words_elsewhere`（含 PostgreSQL 变体）。
- 第一轮曾保留“同一项目内、遗忘之前的相同原话一并抑制”的规则。第 9 节已将来源遗忘收敛到精确来源 ID 及其依赖，旧的同文扩展规则不再适用；事实遗忘与拒绝候选的规则分别保留。

### LTM-008 · P2 · 来源集合去重丢掉同一消息里的不同事实

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/retrieval.py:324](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/retrieval.py#L324) 直接丢弃来源集合已被覆盖的候选。[backend/memory/jobs.py:699](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/jobs.py#L699) 的不同 proposal 可重用同一冻结来源，[backend/memory/service.py:420](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/service.py#L420) 的来源身份不含事实键或引用片段，[backend/memory/grounding.py:91](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/grounding.py#L91) 准入仍链接这些来源。

**可触发条件：** 一条用户消息包含预算和日期等两条独立事实，抽成 A、B，均只由来源 S 支持；查询同时需要两条，A、B 均通过相关性筛选，A 排在前面。

**影响：** A 入选后 B 和完整原文 S 都可能被判重复而丢弃，返回集合缺信息。它们并非相同正文；主模型可能再查来源，但不能用补查可能性替代召回完整性。

**建议：** 按事实身份、相同正文或明确的内容覆盖去重；共享证据只意味着不能重复计票，不代表事实可互相替代。

**后续可验证验收条件：** 同源的预算与日期可一起返回；真正重复的摘要/正文仍被去重。组合问题保留所需细节，单一原文引用不会无故抑制所有其他独立事实。

**证据边界：** 未运行检索；现有同源事实样例不能作为本组合召回已通过的证据。

**修复与验证（2026-10-03）：** 检索去重只对非记忆文档按"证据已覆盖"跳过；不同记忆即便共享同一来源也各自保留（`backend/memory/retrieval.py`）。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_branch_removal.py::test_two_facts_from_one_message_are_recalled_together`，原有 `test_cross_channel_tie_is_reranked_before_evidence_deduplication` 继续通过。
- 残余限制：原文来源片段与其支持的记忆同时命中时，原文仍可能因证据重复被省略，记忆本身保留。

### LTM-009 · P2 · 旧索引 worker 可删除已完成的新版本向量

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/outbox.py:203](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/outbox.py#L203) 写后复核，212 行调用清理；[backend/memory/index/qdrant.py:100](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/index/qdrant.py#L100) 在异步 collection GET 后，以 revision != keep_revision 删除所有其他版本。[backend/memory/outbox.py:235](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/outbox.py#L235) 的 SQL 确认只在 desired_revision 相等时更新状态，[backend/memory/retrieval.py:120](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/retrieval.py#L120) 的 lag 只检查 desired_revision > indexed_revision。

**可触发条件：** W1 写 r1 并通过 SQL/租约复核，在清理的 collection GET 等待；此时用户更正到 r2，W2 完成 r2 写入、清理和 SQL 确认；W1 恢复，以 revision != r1 删除 r2。两个 worker 处理不同 outbox 事件，W1 的租约仍可有效。

**影响：** r2 向量缺失，但 SQL 仍显示 INDEXED/2，lag 可为零。SQL/BM25 与最终授权仍有效，不是旧事实被放行。后台实际 point 对账可补建，但需等至少 300 秒间隔及每批范围/对象轮转扫到该对象。

**建议：** 清理只删除严格早于本次 revision 的版本，或明确已知旧 point IDs，不能使用“不等于我”过滤；修复排队状态也应反映在 lag/健康信息中。

**后续可验证验收条件：** 按上述交错安排两个 worker，新版本向量始终存在且 SQL/point manifest 一致；旧版本仍可清理。不能只覆盖版本变化发生在写后复核之前的时序。

**证据边界：** 并发时序尚未运行；已核实异步窗口、删除过滤器及后续对账恢复边界。

**修复与验证（2026-10-03）：** 索引清理只删除严格早于本次 revision 的版本（Qdrant 过滤条件 `revision < keep_revision`，`backend/memory/index/qdrant.py`），不再用"不等于我"。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_retrieval_runtime.py::test_index_cleanup_only_removes_versions_older_than_its_own`。
- 补充验证（第二轮）：在本地真实 Qdrant（v1.19，独立测试集合）按交错顺序执行写入与清理：旧 worker 的延迟清理保留了新版本，清理只删除严格更旧的版本。
- 残余限制：按交错顺序执行，未做真正的并发调度。

### LTM-010 · P2 · DEAD UPSERT 让已完成的遗忘清理永久显示 pending

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/outbox.py:67](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/outbox.py#L67) 把耗尽尝试的过期任务设为 DEAD；243–249 行 DELETE 结算墓碑不把 DEAD 算作未完成任务。[backend/memory/reconcile.py:90](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/reconcile.py#L90) 的目标发现遗漏已成功墓碑对应的 DEAD UPSERT，而 [backend/memory/service.py:1008](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/service.py#L1008) 的清理状态始终把 DEAD 计为债务。

**可触发条件：** r1 UPSERT 在最后一次尝试中崩溃，用户遗忘到 r2；旧租约到期变 DEAD，随后 r2 及相关来源 DELETE 全部成功。合法 max_attempts=1 即可在首次崩溃形成该状态。

**影响：** 实际向量已空、索引为 DELETED、墓碑为 SUCCEEDED，但用户仍看到 stopped_cleanup_pending；现有候选扫描不再选中目标，不能自动收敛。此条件不要求存在晚到的网络写入。

**建议：** 把旧 UPSERT 的 DEAD 债务纳入清理扫描；确认失效、在途宽限和所有 generation 后转为 CANCELLED。状态查询、候选发现与结算共享同一完成判据。

**后续可验证验收条件：** 上述崩溃后，清理能在核实所有 generation 与在途写入结束后最终显示 cleaned；不能仅修改展示文案掩盖未结债务。

**证据边界：** 未执行崩溃或 Qdrant 检查；现有 DEAD DELETE/RUNNING UPSERT 场景不等于覆盖此组合。

**修复与验证（2026-10-03）：** 对账扫描新增发现"已被更晚且成功的 DELETE/REVOKE 取代的 DEAD UPSERT"（`backend/memory/reconcile.py`），在既有的租约宽限、全部 generation 删除与核验后改为 CANCELLED；DELETE 结算墓碑时也把 DEAD 计为未完成（`backend/memory/outbox.py`），与清理状态、候选发现使用同一完成判据。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_retrieval_runtime.py::test_an_old_write_that_died_before_the_forget_finished_does_not_leave_cleanup_pending`：旧 UPSERT 先变 DEAD、随后 DELETE 全部成功，对账后旧任务 CANCELLED、向量为空、状态显示 cleaned。已确认该用例在修复前的代码上失败。
- 残余限制：仍依赖后台对账周期收敛，期间状态如实显示 pending。

### LTM-011 · P2 · 删除编辑过的文档遗漏旧修订来源正文

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/documents/service.py:214](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/documents/service.py#L214) 编辑创建新修订并撤销旧索引；[backend/memory/documents/worker.py:128](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/documents/worker.py#L128) 按文档修订生成 source ID，164 行以新集合替换 document.source_ids。[backend/memory/documents/service.py:149](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/documents/service.py#L149) 删除仅遍历当前集合清空正文，再删除所有文档修订和文档行。

**可触发条件：** 上传 v1，编辑并完成 v2 发布，然后删除文件。

**影响：** v1 的 MemorySource.body、文件名等 metadata 不会被这次删除流程清除，来源行仍可为 ACTIVE。当前读取因文档行不存在而拒绝它们；这是清除不完整，不应表述为当前 API 可越权返回。

**建议：** 按稳定 document_id 收集全部修订的片段，或维护完整版本来源清单；删除范围不能只取当前投影。

**后续可验证验收条件：** 对多次编辑后的文件执行删除，所有修订的来源正文及需清除 metadata 均按删除语义清除，相关索引也结算；其他文档不受影响。

**证据边界：** 未检查或清理真实存储；既有未编辑文档删除样例不能证明历史修订已清除。

**修复与验证（2026-10-03）：** 删除文件时按稳定的文档 ID 收集全部修订产生的片段（`source_kind = document_chunk` 且 `source_metadata.document_id` 相同，另含当前清单），逐一清空正文与 metadata、撤回索引并使依赖页面失效（`backend/memory/documents/service.py` 的 `_all_revision_sources`）。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_documents.py::test_deleting_an_edited_file_clears_the_text_of_every_revision`：上传、编辑并发布 v2 后删除，v1 与 v2 的全部片段都被清除，其他文件不受影响。
- 残余限制：—

### LTM-012 · P2 · 原文件删除失败缺少持久恢复任务

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/documents/service.py:167](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/documents/service.py#L167) 先删除 SQL 文档及修订记录，169–178 行才删 Blob；异常仅记录错误类型并返回 original_cleanup=pending。现有 memory reconcile 处理索引，不保存或恢复此 Blob key 的删除任务。[frontend-v2/src/features/memory/knowledge/FileList.tsx:89](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/frontend-v2/src/features/memory/knowledge/FileList.tsx#L89) 的删除流程没有向用户跟踪该 pending 状态。

**可触发条件：** SQL 删除已提交，Blob 删除超时/报错，或进程在 SQL 提交与 Blob 删除之间退出；随后按原 document ID 重试。

**影响：** 原文件仍可能存在，原记录却已不可查，重试只得到缺失结果。没有保存精确 storage_key 的持久任务，后台无法自动恢复，界面可能已经提示删除成功。

**建议：** 在同一 SQL 事务写入带明确对象身份的清理任务/墓碑，成功后结算；保留可读取的清理进度，失败及重启后继续处理。

**后续可验证验收条件：** 在两个边界分别模拟错误与进程退出，重启后能按已冻结目标完成删除；界面区分逻辑停止使用与原件物理清理，失败期间不宣称全部清除。

**证据边界：** 未触发任何 Blob 操作；这里只记录现有代码缺少持久恢复所有者。

**修复与验证（2026-10-03）：** 新增 `memory_document_cleanups` 表（迁移 `ma0c1d2e3f4a5`）：删除事务内写入带精确 storage key 的清理记录，提交后立即尝试删除；失败或进程中断时由文档 worker 按退避（上限 1 小时）持续重试。删除后用 `exists()` 确认对象确实不在，弥补部分存储客户端吞掉删除错误的问题。删除接口如实返回 `original_cleanup: pending`，文件列表返回 `cleanup_pending`；界面提示"原文件还在从存储中移除"，不再宣称全部清除。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_documents.py::test_a_failed_original_removal_is_retried_until_the_file_is_gone`（存储报错、存储静默失败两种）、`::test_a_removal_interrupted_after_the_delete_commits_resumes`；迁移往返 `tests/unit/test_wiki_platform_migrations.py`、单 head 检查；前端 `KnowledgeHome.test.tsx` 两例。
- 残余限制：持续重试不设上限，暂无告警；没有 `exists()` 的存储实现只能依赖删除调用自身报错。

### LTM-013 · P2 · 旧删除请求可能误删刚重新上传的原文件

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/documents/service.py:43](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/documents/service.py#L43) 使用 knowledge/{domain}/{digest}/original{suffix} 作为可复用 Blob key；[backend/memory/documents/service.py:168](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/documents/service.py#L168) 提交删除旧记录后，才异步删除该 key。重新上传可创建新 document ID，但复用同一对象路径。

**可触发条件：** 客户端 A 删除旧文件并提交 SQL，但 Blob 删除仍等待；客户端 B 在同一范围上传同内容、同扩展名文件并创建新记录；A 的旧请求随后恢复删除。

**影响：** 旧任务删除了新上传的原件，导致新文档后台读取失败，或正文可读但原件无法下载。完全串行的删除后重传不会暴露该问题。

**建议：** 对象 key 包含上传实例/不可变 generation，或使用存储对象版本条件删除；旧任务只能清理获准旧实例。仅增加重试队列无法解决身份复用问题。

**后续可验证验收条件：** 按上述交错完成旧删除和新上传，新文档原件始终可下载、可解析；旧实例被清理。重复请求与恢复重试也不能删除替代对象。

**证据边界：** 未执行并发上传或删除；依据是复用键和 SQL/Blob 操作顺序。

**修复与验证（2026-10-03）：** 每次上传使用独立对象路径 `knowledge/{domain}/{digest}/{upload_id}/original{suffix}`，旧删除只会清理自己那份；清理前若仍有在用文档引用同一 key（旧版共享路径）则跳过；并发重复上传落败的一方，其未被引用的副本也走同一清理流程。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_documents.py::test_a_late_removal_never_deletes_the_same_file_uploaded_again`：旧删除暂时失败、随后重新上传同一文件，补删只移除旧对象，新文件原件完好。
- 残余限制：此前已上传的文件保留旧路径。

### LTM-014 · P2 · 一次 Wiki 合并契约错误会停止相同输入的自动整理

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/wiki/consolidation.py:38](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/wiki/consolidation.py#L38) 对额外字段、重复成员等抛 WikiStateError，188 行在模型返回后调用该校验。[backend/memory/wiki/organization_worker.py:338](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/wiki/organization_worker.py#L338) 将其按 terminal=True 处理，任务变 CANCELLED。[backend/memory/wiki/maintenance.py:194](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/wiki/maintenance.py#L194) 对相同 input_hash 只恢复 FAILED，且已保存 last_input_hash。

**可触发条件：** 默认自动整理进入合并阶段，模型返回可解析 JSON，但带多余 reason 字段、重复成员或不合法 ID；资料此后没有变化。

**影响：** 页面调度前任务被取消；后续扫描、冷却和每日预算重置都不会恢复相同资料的整理，普通消费界面又没有内部恢复入口。

**建议：** 把模型输出契约错误归入有界重试/冷却类别，与权限或来源变化等真正终止错误分开；耗尽尝试后接入现有 FAILED 恢复策略。

**后续可验证验收条件：** 首次契约错误、随后返回合法结果时，任务能在预算内自动恢复并完成；永久错误受重试上限约束；真正撤权或来源变化仍终止旧任务。

**证据边界：** 未运行模型或 worker；可触发结构来自校验器，结论不依赖模型一定生成某类错误。

**修复与验证（2026-10-03）：** Wiki 合并的模型输出契约错误改为 `ConsolidationOutputError`（属于可重试的 `OrganizationError`），在尝试次数内重试，耗尽后进入既有 FAILED 冷却恢复；权限或来源变化等仍为终止错误（`backend/memory/wiki/consolidation.py`）。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_wiki_consolidation.py::test_a_malformed_grouping_is_retried_rather_than_cancelling_the_run`（首次多出字段、随后合法，任务最终 COMPLETED 并完成合并），原校验用例改为断言新错误类型。
- 残余限制：—

### LTM-015 · P2 · 仅禁止记忆读取时，明确的任务状态请求也被跳过

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [backend/memory/routing.py:13](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/routing.py#L13) 把“不要查历史/记忆”与“仅使用当前输入”合为一条规则；31–35 行立即返回，memory/task 都保持 skip。[backend/memory/orchestrator.py:126](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/orchestrator.py#L126) 因而跳过权威任务读取。

**可触发条件：** 输入“不要查历史记忆，请看当前任务状态”。该文本同时明确禁止记忆、要求读取任务状态。

**影响：** 前置路由违背两个独立需求，未执行明确要求的业务状态读取。主助理仍可能通过工具补查，所以本项不声称最终回答必然错误。

**建议：** 分别处理全局输入限制、记忆禁止和任务读取，独立锁定每个明确请求；禁止一条数据路径不能自动禁止另一条。

**后续可验证验收条件：** 示例输入不读取记忆，但执行当前授权任务状态查询；“只根据本轮消息回答”等真正全局限制仍按其语义处理。规则不通过新模型判断增加额外延迟。

**证据边界：** 未调用 Jev 或业务状态接口；这是规则分支的确定性静态结果。

**修复与验证（2026-10-03）：** 路由按分句判断：只有"只根据本轮内容"这类全局限制同时跳过两条路径；"不要查记忆"只禁止记忆，同句中明确要求的任务状态照常读取（`backend/memory/routing.py` 的 `_explicit_needs`）。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_retrieval_runtime.py::test_routing_independent_rules_bounded_redaction_and_fallback`（"不要查历史记忆，请看当前任务状态"只读任务状态；两者都禁止时都跳过；全局限制不变）。
- 残余限制：规则为确定性匹配，不增加模型调用。

## 5. P2：范围较小的界面问题

### LTM-016 · P2 · 调试详情刷新授权失败后仍渲染旧缓存正文

**状态：已修复；自动化测试通过。** 原审查结论如下。

**静态证据与调用链：** [frontend-v2/src/features/memory-debug/DebugRunDetail.tsx:31](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/frontend-v2/src/features/memory-debug/DebugRunDetail.tsx#L31) 取得 query.data，37–42 行显示错误后仍继续渲染 run 和 steps。[frontend-v2/src/features/memory-debug/api.ts:116](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/frontend-v2/src/features/memory-debug/api.ts#L116) 使用查询缓存，没有针对权限错误清除该 run 的正文。

**可触发条件：** 同一用户先查看 run，随后删除关联 Session、移除所属项目或失去访问资格；刷新详情得到 403/404，组件仍持有此前成功的 query.data。

**影响：** 页面在当前授权失败时继续显示已读旧正文。成功响应 body_available=false 时已有隐藏逻辑，因此主要问题是权限错误路径；不等于服务器向一个新用户返回这些数据。

**建议：** 需要重新授权的期间或权限错误后隐藏缓存正文，403/404 清除对应 run 缓存，重新打开时取得当前资格后再显示；安全元数据和错误提示可以保留。

**后续可验证验收条件：** 先缓存正文，再让详情请求返回 403/404，旧输入/步骤正文不继续展示；恢复权限并取得新的成功响应后可显示。不能只增加错误提示而保留正文。

**证据边界：** 此项风险低于后端跨用户泄露，限定为同用户已读取的页面状态；未运行浏览器。

**修复与验证（2026-10-03）：** 详情请求出错时不再渲染缓存的输入与步骤（`DebugRunDetail.tsx`），run 详情查询不缓存（`staleTime`/`gcTime` 为 0），401/403/404 不重试。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`frontend-v2/src/features/memory-debug/MemoryDebugPage.test.tsx` 的 "stops showing a run's details once a fresh read is refused"：先成功缓存正文，再返回 403，旧正文不再显示。
- 残余限制：—

### LTM-017 · P2 · 记忆详情用清理状态替代当前正文和来源校验

**状态：已修复；自动化测试通过（SQLite 与 PostgreSQL）。** 原审查结论如下。

**静态证据与调用链：** [frontend-v2/src/features/memory/knowledge/MemorySheet.tsx:53](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/frontend-v2/src/features/memory/knowledge/MemorySheet.tsx#L53) 请求 cleanup，58 行据 active 放行旧列表传入的 summary，84 行渲染该旧正文。[backend/memory/service.py:1015](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/service.py#L1015) 的 active 只表示没有相应墓碑，不证明来源/修订仍当前。[frontend-v2/src/features/memory/api.ts:38](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/frontend-v2/src/features/memory/api.ts#L38) 还继承 [frontend-v2/src/app/providers/AppProviders.tsx:11](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/frontend-v2/src/app/providers/AppProviders.tsx#L11) 的 15 秒缓存策略。

**可触发条件：** 先缓存列表/打开详情，再从另一会话或窗口删除原始来源、修改来源或更正记忆；旧详情对象仍在，cleanup 可继续为 active。短时间重开还可能直接使用缓存。

**影响：** 来源/历史接口可能已隐藏失效内容，但详情仍显示旧 summary。它是当前性和撤源后的界面问题，不应被扩大为新用户的数据泄露。

**建议：** 详情接口返回当前授权正文、来源资格与修订，组件渲染该响应；cleanup 只用于清理进度，不能为缓存正文授权。来源失效或修订变化应使详情快照失效。

**后续可验证验收条件：** 分别更正记忆、删除来源及快速重开详情，页面显示当前合法版本或明确不可用，不能以 cleanup=active 继续显示旧列表正文；无需等待缓存自然过期。

**证据边界：** 未运行界面；静态依据为正文使用旧 props，以及 cleanup 的实际判定条件。

**修复与验证（2026-10-03）：** 新增 `GET /api/memories/{id}` 返回当前授权正文与 `body_available`，来源不可用时不返回摘要（`backend/memory/service.py` 的 `get_memory`）；详情页渲染该接口的当前结果而非列表缓存，查询不缓存，修订变化时回写列表；"忘记"按钮在清理状态刷新完成前不可用（`MemorySheet.tsx`）。

- 修复提交：与本节同一提交（分支 `codex/long-term-memory-plan`，提交信息以 `fix(memory): close the long-term memory review findings` 开头）。
- 验证：`tests/unit/test_memory_source_clearing.py::test_the_detail_read_shows_current_text_only_while_its_sources_allow`（含 PostgreSQL 变体）与 `KnowledgeHome.test.tsx` 中详情相关用例。
- 残余限制：—

## 6. 已接通的能力与额外完成度缺口

| 能力 | 静态结论及适用范围 |
|---|---|
| SQL 权威与普通记忆 API | actor/workspace/project、来源、版本、确认与墓碑检查已落地；本轮没有确认通过普通记忆 API 参数直接读取其他用户记忆的路径。LTM-001 发生在正文被复制进共享历史之后 |
| 自动抽取与纠正 | 真实完成挂点、冻结边界、独立核验、持久任务及统一写入存在；不能据此忽略 LTM-002 的作者归属或 LTM-006 的来源误失效 |
| Qdrant 与混合检索 | 独立向量适配、不可变 revision point ID、中文关键词、dense、RRF、相关性 rerank 和 SQL 复核已接入；存在 LTM-008/009/010 的信息损失及恢复问题 |
| Wiki 与文档 | 纯编纂核心和宿主权限/发布分离，依赖修订、发布 CAS、增量维护、文档阅读/索引均有实现；外发、自动失败恢复和清理尚需闭合 |
| Jev | 两问题请求、模型/概率/confidence 合同检查、明确规则和主助理补查可达；没有发现该路由替换主助理模型 |
| 调试与重跑 | 页面连接真实后端记录，重跑有独立开关、预览、费用确认、一次性 claim 与当前来源检查；LTM-004/016/017 是失败和缓存路径的遗漏 |

另有一个未单独计入缺陷数量的计划缺口：[backend/memory/routing.py:22](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/routing.py#L22) 接受 recent_context，但 [backend/memory/orchestrator.py:106](https://github.com/arkstudio-ai/OpenBox/blob/0a6b8441ae624d3961b48f650e51e6d34e733a38/backend/memory/orchestrator.py#L106) 的真实调用没有传入，当前前置路由只看到本轮问句及最小元数据。带指代的问题可能更多依赖稳定背景和主助理补查；不能因此断言必然答错，也不能把所有跨会话成功都归功于 Jev。后续可用一次明确含近期指代的已授权调用记录，核对送入路由的实际输入是否符合计划。

**2026-10-03 更新：** 已接通。主循环把本轮之前最近两条可见消息（各取末尾至多 400 字，合成消息不计）作为 recent_context 传给路由，路由内照常脱敏（`backend/agent/loop.py` 的 `_recent_exchange`，`run_memory_context(recent_context=...)`）。验证：`tests/unit/test_memory_router_context.py` 与 `tests/unit/test_memory_retrieval_runtime.py::test_the_router_receives_the_exchange_before_this_turn`。

本文没有给出总体准确率、延迟或完成百分比，也没有把“代码存在”当作生产验收。生产部署状态、真实模型质量、真实数据库/对象存储并发和故障恢复仍未由本次审查验证。

## 7. 后续处理与关闭标准

建议先处理 LTM-001–005 的权限、证据和撤销边界，再处理来源误失效/误去重、索引竞态与删除恢复，随后收敛路由及界面状态。此顺序是工程建议，不是本轮实施授权；本文提交不会修改问题状态。

每个问题关闭时，应保留问题 ID，记录修复 commit、所执行验收的环境/步骤/结果、覆盖范围及残余限制；源码修改完成但未验证时标为“待验证”，不能直接写“已修复并验收”。复现用虚构数据、替身或获授权的隔离环境，不需要新增固定业务质量数据集。

**本轮整体验证（2026-10-03）：** 后端全量单元测试（SQLite + 隔离 PostgreSQL 验证库 `openbox_memory_verify_20261002`）：4464 通过、30 跳过、15 失败、1 错误；这 16 项全部在修改前 HEAD 的基线失败清单内（trajectory 生命周期、若干迁移测试、llm schema、skill reload、platform plugin、memory_api 等），本轮没有新增失败，另有 8 项基线失败转为通过。全量运行之后又改动的两处（旧读取引用复查上限、文档 worker 清理隔离）已复跑对应测试文件，全部通过。前端 `npm run check`（i18n、SEO、lint、tsc、vitest）通过：150 个测试文件、1046 个用例全部通过，lint 0 错误。本地环境已应用迁移 `ma0c1d2e3f4a5`；浏览器中确认文件列表返回 `cleanup_pending`、记忆详情来自实时接口、LTM-001 如上。未在真实 Qdrant、真实对象存储或两个真实账号之间做并发与故障实验。

阶段一完成的记录与问题清单并存：阶段进度不豁免已知风险，问题文档也不否认用户已经观察到的正常路径效果。后续范围、部署和推送按新的明确指令处理。

## 8. 第二轮补充修复（2026-10-03）

复查时又发现两个记忆问题（都来自本轮早先的改动），并处理了上次列为待定的事项。修复提交：与本节同一提交（提交信息以 `fix(memory): keep pause and decline promises, faster recall, green suite` 开头）。

| 问题 | 修复 | 验证 |
|---|---|---|
| "不记忆"可被绕过：暂停期间的回合没有抽取记录，后台补漏扫描会补建任务；若扫描前恢复了记忆，暂停期间说的话会被记住 | 暂停时把该回合记为"不可抽取"（已取消的任务，`memory_paused`），补漏扫描不再补建；扫描也跳过子任务会话（`backend/memory/jobs.py`） | `tests/unit/test_memory_controls.py::test_resuming_never_saves_what_was_said_while_paused`（对话暂停、关闭自动记忆两种；修复前两者都失败） |
| "不用记"之后再说一遍会被自动保存：为"忘记后重说可再记"加的时间规则把拒绝也放开了 | 被拒绝的提议（记忆为 REJECTED）始终抑制；忘记仍按时间判断（`backend/memory/service.py` 的 `is_candidate_suppressed`） | `tests/unit/test_memory_forget_restate.py::test_a_declined_proposal_is_not_saved_when_said_again_later`；原先因 SQLite 一秒时间精度偶发失败的 `test_worker_schema_validation_retry_and_suppression` 现在稳定通过 |
| 回复起步慢：每轮先路由（约 1.0 秒）再检索（约 1.1 秒） | 路由规则无法排除记忆时，检索与路由同时开始，路由说不需要就丢弃结果；问候、致谢类短消息跳过路由（"好的""可以"这类确认仍走路由，因为它们常常是在批准一项可能用到记忆的任务），核心记忆照常附带（`backend/memory/orchestrator.py`、`backend/memory/routing.py`） | `tests/unit/test_memory_retrieval_runtime.py::test_retrieval_runs_alongside_routing_and_is_used_only_when_routing_asks`（两者各 0.3 秒时总耗时低于 0.55 秒；路由说不需要时检索被取消）、`tests/unit/test_memory_router_context.py::test_small_talk_skips_routing_and_routing_rules_decide_what_can_start_early`。本地实测（重启后 5 轮对话）：致谢消息 72 毫秒（此前约 1.3 秒）；由路由模型判断的两轮，路由 569 / 2125 毫秒、检索 942 / 1097 毫秒，总耗时 1157 / 2364 毫秒，接近两者中较慢的一个，而不是相加；路由判断不需要时，提前开始的检索被丢弃（总耗时 846 毫秒）。 |
| 共享工作区：成员能在侧栏打开彼此的对话，助手回复若复述个人记忆会被看到 | 工作区有其他活跃成员时，本轮记忆上下文带 `shared_chat`，记忆使用指引要求助手应用记忆但不主动说出个人细节（健康、家庭、关系、钱、行踪） | `tests/unit/test_memory_retrieval_runtime.py::test_shared_workspaces_mark_the_memory_context` |
| 记忆列表、导出接口直接调用时拿到 `Query` 对象作默认值，跨用户 404 测试一直失败 | 改用 `Annotated` 查询参数（`backend/api/memories.py`） | `tests/unit/test_memory_api.py` 全部通过 |
| creator_context 工具描述超出常驻工具 schema 预算，且丢了 "confirmation card" 安全措辞 | 精简描述、补回措辞（`backend/tool/creator_context.py`） | `tests/unit/test_llm_schema.py` 通过 |

此前一直失败的 12 个测试也已处理（迁移测试是被本分支的记忆迁移带坏的，其余与记忆功能无关）：工具暴露迁移测试的夹具补上它所在版本已存在的 `user_memories`（本分支的记忆迁移会修改这张表），子任务迁移测试固定升级到自身版本；Agent 恢复服务在启动时新建停止事件，平台插件 watcher 不再等待已关闭事件循环里的任务，模块级服务可在新事件循环里重新启动（修复了 lifespan 测试及其连带的插件测试；lifespan 测试结束时关闭共享的内存测试库，避免后续测试拿到随事件循环失效的连接）；技能热加载测试解析临时目录，避免 macOS `/var` 符号链接导致路径不一致。

**整体验证：** 后端全量单元测试（SQLite + 隔离 PostgreSQL 验证库 `openbox_memory_verify_20261002`）：4491 通过、30 跳过、0 失败，此前一直失败的 16 项全部转为通过。前端 `npm run check` 通过：150 个测试文件、1046 个用例，lint 0 错误，i18n 一致。LTM-009 在本地真实 Qdrant 上复核通过；记忆召回延迟在浏览器中实测（见上表）。

**残余限制：** `shared_chat` 是给助手的指令，不是硬性保证；若要在共享对话里完全不用个人记忆，或把对话改为仅本人可见，属于产品决定。路由说不需要时被丢弃的检索，其调用费用不计入调试记录的用量。未标记为成功、经恢复的回合仍由补漏扫描补建，此时按扫描当时的暂停状态处理。本地开发库中 10 月 1 日测试误跑留下的数据（约 99 个属于测试用户的卡住任务）未清理，删除需单独授权；真实 Qdrant 验证留下一个空的测试集合 `openbox_verify_ltm009_2ea8996c`。


## 9. 残留路径补修与重新验证（2026-10-03）

本轮基线为远程 `d9abb57b5bb17cedb0bddac2c352a8efb1ea1251`，已先 fetch 确认，再在独立修复 checkout 工作。范围是五项未完全闭环的问题及两项补充问题，用户已授权代码修改、测试、提交和推送。修复与本节同一提交，提交信息为 `fix(memory): close privacy and recovery gaps`。第一轮的直接调用测试通过，不能证明批量包装、确认卡、来源失效或异常恢复路径也受保护。

### 9.1 实际修复与针对性回归

| 项目 | 原先遗漏的路径与本轮改动 | 本轮回归证据 |
|---|---|---|
| LTM-001 / P1：共享历史中的记忆正文 | `batch` 拒绝执行记忆/任务读取工具，要求直接调用以保留临时引用和权限复查。历史批量结果通过同一分类器过滤，不能进入后续回合或 compaction。遗忘卡的摘要仅在本人 question API 中提供；共享 Part/AgentEvent、等待和完成后的元数据只含通用确认文本，旧卡读取也过滤。 | `test_memory_tool_projection.py` 中批量工具执行前拒绝、旧批量历史/回放/压缩过滤、旧卡元数据过滤；`test_durable_questions.py` 中两个合成工作区成员通过实际 ASGI HTTP 读取 `/message`、`/history`、`/question/{id}`，覆盖忘记、保留、关闭三种结果。所有者仍可读详细卡，另一成员为 404。 |
| LTM-005 / P1：记忆版本未变时来源已经失效 | 每次后续外部模型调用前，在新 SQL 事务中重新检查冻结记忆的有效状态、确认状态、时间有效性和来源可用性，不能仅凭 `id/revision` 未变化继续发送旧摘要。失效进入已有重试流程，重新读取证据。 | `test_memory_provider_rechecks.py::test_deleted_old_source_chat_without_memory_revision_change_stops_next_call`：在抽取、证据验证、合并规划三个阶段分别删除独立旧 Session 的来源或让记忆过期，共六个场景；明确断言旧记忆 revision 未改变，以及下一阶段没有被调用。 |
| LTM-007 / P2：来源遗忘范围与重述不一致 | 将稳定来源 ID 的生成集中到 `source_snapshot_id`，存储、候选准入和 job 回放共用。来源遗忘只清除所选来源及其依赖，不再用正文 hash 扩展到独立的相同文字。新来源遗忘使用 SOURCE 范围；旧 FACT 标记可由不可变修订的 `source_forgotten` 原因识别，不要求迁移历史数据。事实遗忘仍按证据时间抑制旧回填，遗忘后的新陈述可再记；REJECTED 候选继续保持拒绝。 | `test_memory_source_clearing.py` 增加同项目、相同旧正文的独立来源不受影响，并覆盖旧格式标记；`test_memory_forget_restate.py` 同时覆盖 memory/sources 两种模式、逐字原话与改写重述、旧回合重试和拒绝候选。 |
| LTM-012 / P2：Azure 故障被误当不存在 | Azure 的 `delete`、`exists` 只对明确的 `ResourceNotFoundError` 返回不存在；认证、网络和服务错误向上传播，持久清理记录继续等待重试。 | `test_azure_blob_deletion.py` 覆盖认证失败、网络失败和明确不存在；`test_memory_documents.py::test_azure_faults_keep_original_cleanup_pending` 使用真实适配器代码及假的 SDK 传输，覆盖删除失败、存在性探测失败和恢复后的清理。 |
| LTM-015 / P2：禁止记忆仍注入背景 | 路由保存独立的 `memory_forbidden` 标记，背景注入也遵守它。正常的“无需检索”仍可携带核心背景；明确禁止记忆时不携带背景，但允许明确请求的权威任务状态。 | `test_memory_retrieval_runtime.py::test_explicit_no_memory_reaches_final_context_but_keeps_task_reads` 检查最终渲染上下文，覆盖中文、英文、任务请求和正常 skip 的对照场景。 |
| 补充 P2：暂停设置并发覆盖、501 条静默淘汰 | 偏好仓库和记忆设置共用事务内锁定读取：先原子确保偏好行存在，再锁行并合并指定字段。SQLite 在首次读取前取得写锁，PostgreSQL 使用唯一键与行锁。保留所有暂停 Session ID，不再截取最后 500 条。 | `test_memory_controls.py` 覆盖首次建行/已有行时自动保存开关、8 个 Session 暂停与外观设置并发写入，以及第 501 个暂停不解除第 1 个暂停。 |
| 补充 P2：上传成功但 SQL 未提交的孤立文件 | 在上传 IO 前，将唯一对象 key 的意图写入现有 `memory_document_cleanups`；文档与 ADOPTED 状态原子提交。未提交、失去权限、崩溃、竞争失败或超时的上传保留可恢复清理记录；被清理流程接管后不能再成为活动文档。 | `test_memory_documents.py` 覆盖存储写入后进程退出、SQL 事务回滚、重试上传的保护、活动租约保护、租约过期后迟到返回、并发重复上传、上传期间撤销成员权限，以及首次清理之后才到达的对象写入。 |

上述测试使用合成数据和模型/索引/存储替身；没有为复现而向真实模型发送个人信息。HTTP 测试运行真实路由和 SQL 权限路径，但认证依赖使用合成身份，不代表已操作两套真实登录账号。

### 9.2 上传清理的持久状态与回退边界

复用已有表，不新增前向数据库迁移：

- `UPLOADING`：上传前已经记录确切 key，默认租约 900 秒；清理器不能删除未过期上传。
- `ADOPTED`：与活动文档在同一事务提交，孤立文件扫描不处理它。
- `ABANDONED`：租约到期或重复上传竞争失败，不能再被活动文档接纳；清理确切 key，默认每 3600 秒复核一次，以处理进程退出后才完成的远端写入。确认当前不存在后仍保留该标记，因此历史失败上传会产生周期性存储检查成本。
- `PENDING → SUCCEEDED`：用户删除已登记文件的原有流程；删除或存在性验证失败仍保留 PENDING。

唯一 key 保持不复用，清理前继续检查活动文档引用。清理只针对持久记录中的对象，不扫描或删除无关存储路径。旧版本已经产生但从未登记 key 的孤立文件无法由本轮代码自动找回，需另行盘点，未执行历史存储清理。

`ma0c1d2e3f4a5` 的回退保护补充检查 UPLOADING/ABANDONED，防止降级丢掉这些记录。若回退应用版本，须保留能够处理上述状态的清理器或等价恢复机制；旧版仅处理 PENDING 的 worker 不足以完成本轮上传恢复。测试只在隔离数据库/临时 schema 中建表和验证迁移约束，未迁移现有业务库。

### 9.3 实际运行结果

- 独立 Python 环境：在修复 checkout 用 `uv sync --frozen --extra test` 按现有锁文件安装；未修改依赖清单或共享 Python 环境。
- 首轮针对性 SQLite 检查：176 passed、42 skipped。随后上传清理与迁移约束检查：27 passed、1 skipped。这些是过程记录，不与以下数量相加。
- 最终启用独立 PostgreSQL 测试库 `openbox_memory_followup_20261003_9c159e1e` 的回归套件：187 passed、0 failed，覆盖暂停控制、发送前复查、重述与来源遗忘、文件清理、检索上下文、pipeline、Azure 适配器及测试 schema 的迁移约束；套件中也有 SQLite 对照参数。遗忘卡 HTTP 权限回归使用另一个独立库 `openbox_questions_followup_9c159e1e`：6 passed、36 deselected。
- 全量 SQLite 后端单元检查（预先排除下列两项已证实的基线失败）：4460 passed、104 skipped、2 deselected、1 failed。唯一新增失败是 `batch` 工具描述连同 schema 为 958 字符，超过已有 900 字符预算；已精简使用说明，未放宽预算。修正后复跑批量工具、工具描述预算、LLM schema、工具载荷及记忆历史投影六个相关测试文件：55 passed、0 failed。最后调整的文档上传/来源重试测试另行复跑：52 passed、2 skipped。没有再次执行全量检查，也没有把先前全量运行写为全绿。
- 两项基线失败已在未修改的 `d9abb57b` checkout、同一离线守卫下逐项复现：`test_trajectory_producers_delegation.py::test_a_subagent_spawn_records_its_lifecycle_under_the_parent_trajectory` 仅替换 OpenAI provider，但本机默认模型配置选择了另一 provider，得到 `provider_missing`；`test_wuying_provisioning.py::test_channel_failure_keeps_existing_billable_desktop_for_recovery` 未替换云桌面查询，外部请求被守卫阻断，状态断言失败。未为通过测试修改无关代码、开放外网或访问真实云桌面。
- 测试修正：来源清除后的 pipeline 重试改为重放确切旧来源；相同原话的新回合由重述测试单独覆盖。复用 PostgreSQL 验证库时，假的对象存储通过测试夹具排除此前用例的清理记录，避免把不属于该存储替身的对象判为不存在；生产清理范围没有因此收窄。
- `scripts/check_main_contract.py --base d9abb57b`：262 个路由、1001 个模型字段、43 个内置工具，缺失项为空。
- 网络边界：扩大检查时使用本地 pytest 守卫，阻断外部网络地址；允许本机隔离 PostgreSQL 和测试用本机连接。未调用付费模型、真实 Qdrant 或真实对象存储。
- 本轮未修改前端代码，也未重跑前端测试；共享会话权限验证覆盖后端真实 HTTP 路由与序列化。运行环境未提供可独立核实的模型标识，不能将指定的模型名称当成运行时核验结果。

### 9.4 保留的产品与验证限制

本轮关闭的是表中列出的具体路径。共享会话中助手主动写入回复的个人信息仍随聊天受众可见；`shared_chat` 提示不构成硬性隐私保证。是否禁止共享会话使用个人记忆、是否改变会话默认可见性，需要独立产品决定。

来源遗忘不删除原聊天；来源 ID 范围与事实遗忘范围现在明确分开。对自然语言“不要用记忆”的识别仍使用确定性规则；测试证明匹配后的禁止标志约束本轮记忆注入，不承诺识别所有自然语言变体。Azure 的故障路径通过 SDK 替身验证，未对云端执行故障实验；本轮没有重测模型质量、真实召回延迟或承诺固定性能收益。
