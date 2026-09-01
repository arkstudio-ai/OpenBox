# OpenBox Agent Kernel Architecture

> 本文描述当前分支已经落地的实现事实，不是路线图。若本文与源码冲突，以源码和数据库迁移为准。
> 对照基线来自 [`DeepSeek-Harness-vs-OpenBox-source-analysis.md`](./DeepSeek-Harness-vs-OpenBox-source-analysis.md)；该报告记录的是重构前审计，本文记录重构后的内核边界。

## 1. 系统边界

OpenBox 保留 Python/FastAPI 产品控制面，把 WUYING 定义为隔离执行面。Agent 的模型选择、上下文投影、权限、工具排序、Run ownership、用户/项目所有权都在 Backend 决定；WUYING Action Server 只接受已鉴权、带作用域和可选 fencing header 的执行请求。

```text
Web / Mobile
    │ REST + SSE/WebSocket
    ▼
FastAPI control plane
    ├── PostgreSQL: Session, Message/Part, AgentDriverState, Task handoff, Cron, Surface event
    ├── Redis: multi-worker event delivery（启用 JWT 的 SaaS 模式）
    ├── Agent driver + loop + model/context policy
    └── ordered tool scheduler
                  │ SandboxClient / X-OpenBox-* headers
                  ▼
WUYING execution plane
    ├── root-owned Action Server（控制接口与凭据）
    ├── persistent run fence / tenant catalogue state
    └── non-root `sandbox` runner（shell、stdio MCP、Skill install 等）
                  │
                  ▼
tenant/project namespaced workspace
```

主要入口：

- Agent 生命周期：[`backend/agent/driver.py`](../backend/agent/driver.py)、[`backend/agent/loop.py`](../backend/agent/loop.py)、[`backend/agent/recovery.py`](../backend/agent/recovery.py)、[`backend/agent/recovery_service.py`](../backend/agent/recovery_service.py)
- Task 交付：[`backend/agent/subagent_runtime.py`](../backend/agent/subagent_runtime.py)、[`backend/db/models/subagent.py`](../backend/db/models/subagent.py)；旧 one-shot 兼容路径仍在 [`backend/agent/task_handoff.py`](../backend/agent/task_handoff.py)
- Prompt 接受：[`backend/api/sessions.py`](../backend/api/sessions.py)
- 工具执行：[`backend/agent/processor.py`](../backend/agent/processor.py)、[`backend/agent/tool_scheduler.py`](../backend/agent/tool_scheduler.py)
- 执行面传输：[`backend/sandbox/client.py`](../backend/sandbox/client.py)、[`container/action_server.py`](../container/action_server.py)
- WUYING Provider：[`backend/sandbox/wuying.py`](../backend/sandbox/wuying.py)

## 2. 从 Harness 采用的不变量

以下“不变量”是本次重构从 Harness 对照中抽出的正确性约束。状态中的“部分”表示 OpenBox 只覆盖了明确子集，不能推断为完整 Harness 等价实现。

| Harness 对照不变量 | 当前状态 | OpenBox 实现事实 |
|---|---|---|
| 一个 Session 同时只有一个 Agent Driver | 已实现 | `agent_driver_states` 是跨 Worker 权威；`sessions.status` 只是 UI read model。|
| 接受工作必须先持久化，再以 durable ownership 异步 wake | 已实现 | 普通 Prompt 先写 `agent_inbox_items.accepted`；空闲时才 reserve，并在同一事务完成 claim、Message/FilePart、事件和 trigger bind，提交后 wake。显式 Regenerate/Command 仍直接 reserve/bind；Task 使用 descriptor + activation 的独立协议。|
| Phase 决定恢复是否允许重放 | 已实现 | 只重放 `reserved`；`running`/`finalizing` 一律做保守 tail repair。|
| 旧 Worker 不能在 takeover 后继续提交副作用 | 已实现到请求边界 | Backend generation fence 加 WUYING persisted run epoch；已发出的长进程不能被 epoch 自动撤销。|
| 工具可以并行执行，但可观察提交顺序必须等于模型顺序 | 已实现 | 只有显式 `parallel_safe is True` 的 body 并发；prepare/finalize/Part/SSE 按 slot 顺序提交。|
| Session 真相应 append-only，Surface 是可替换投影 | 已实现到 Agent transcript 边界 | `agent_events` 是普通 Agent 模型上下文、工具身份和 provider replay 的 serving truth；SQL Message/Part/InternalPart 是兼容 UI/API read model。Compaction/Fork 使用稳定 Event range、digest、replacement/lineage。Session 元数据、Todo 与文件系统不在此 event-sourcing 边界内。|
| 后台恢复必须修复开放的 Turn/Step/Tool tail | 已实现到 Message/Part 层 | 启动立即执行并周期扫描；pending tool 标记 `tool_not_started`；running tool 标记 `tool_outcome_unknown`；开放 step/message 被闭合为 aborted。|
| Skill/MCP/Plugin 必须有 Scope 与 Lifecycle | 已实现（边界不同） | Skill/MCP 按 user scope 隔离并支持 catalogue generation；可信宿主插件具有依赖拓扑、generation、异步 Effect/Dispose、原子激活、LKG 回滚、在途调用排空和低频 digest 热重载。它不是第三方代码沙箱，也不提供 Cordis Service 注入。|
| Subagent 应有 durable descriptor、inbox/outbox、fork 与 cold resume | 已实现到 Task 激活层 | one-shot/continuable 共用 descriptor + activation inbox + bounded outbox，支持 fresh `spawn`、closed-prefix `fork`、follow-up、interrupt、report/list、expired-claim takeover 和 cold resume；仍有意不自动续跑不确定边界后的 parent Loop。|

这些约束的共同原则是：内存中的 `asyncio.Task`、Event 或缓存可以加速，但不能作为多 Worker、重启和 takeover 的权威。

## 3. Agent Driver

### 3.1 权威行与 fencing identity

每个 Session 最多有一行 `AgentDriverState`。表结构位于 [`backend/db/models/agent_driver.py`](../backend/db/models/agent_driver.py)，迁移位于 [`backend/db/migrations/versions/d8e0f2a4b6c8_agent_driver_lease.py`](../backend/db/migrations/versions/d8e0f2a4b6c8_agent_driver_lease.py)。

一个活动 generation 的完整身份是：

```text
(session_id, user_id, run_id, generation, owner_id)
```

- `generation` 单调递增，是 takeover 的 fencing epoch。
- `run_id` 标识一次用户可见 Run，也会进入 ToolContext、Surface replacement 和 WUYING header。
- `owner_id` 标识具体 Backend 进程实例。
- `lease_expires_at` 决定 owner 是否仍存活；当前 TTL 为 60 秒，monitor 每 10 秒续租，并每 0.5 秒轮询 durable abort。
- `trigger_message_id` 把 generation 与被接受的 User Message 绑定。
- `abort_requested_at` 是跨 Worker stop 信号。

`reserve_run()` 在同一事务内锁定 Session 和 Driver 行、拒绝未过期 owner、递增 generation，并把 `sessions.status` 投影为 `busy`。进程内 `_activities` 和 `asyncio.Event` 只负责低延迟通知；数据库行才是裁决者。见 [`backend/agent/driver.py`](../backend/agent/driver.py)。

### 3.2 Phase 状态机

```text
idle ──reserve_run──▶ reserved ──run_loop──▶ running
  ▲                                           │
  └──────────── exact fenced release ◀── finalizing
```

| Phase | 已越过的边界 | 允许冷恢复重放 | 处理方式 |
|---|---|---:|---|
| `idle` | 无活动 owner | 不适用 | 可接受新 generation。|
| `reserved` | Prompt 已绑定，但 `run_loop` 尚未进入 provider/tool 执行 | 是 | 校验 trigger 与是否已有回答后，以新 generation 重启。|
| `running` | 可能已发 provider 请求或 Tool body | 否 | 只修复 transcript，绝不自动重放。|
| `finalizing` | 主执行结束，仍在 run-owned cleanup | 否 | 视为中断并修复；旧 owner 不得写回 idle/error。|

`run_loop()` 每步开始前调用 `lease.assert_current()`；进入 cleanup 前切换为 `finalizing`，在同一 ownership window 内完成 pending ToolPart、Todo 和 prune 清理，再由 `lease.release(session_status="idle")` 在同一事务内清除精确 Driver identity 并写入 Session `idle`，确认 matched 后才发布状态事件。异常或取消则原子写入 Session `error` 并把同一 identity 立即变为过期 recovery marker；提交失败时保持 marker，不允许先写终态再遗留 live `finalizing` lease。旧 generation 的 `release()`、phase update 或 cleanup 因 tuple 不匹配而不能清除新 generation；lease 一旦过期，旧 owner 的 `release()` 也不能清空该行，因为原 `run_id/generation/phase/trigger` 会保留为 durable recovery marker。恢复接管必须对这四项做 exact-CAS 并再次确认 lease 仍过期，晚到的 snapshot 只能 stale-skip，不能覆盖用户刚创建的新 generation。见 [`backend/agent/driver.py`](../backend/agent/driver.py) 和 [`backend/agent/loop.py`](../backend/agent/loop.py)。

### 3.3 Inbox accept → reserve → claim/bind → wake

主 Prompt 的 durable acceptance protocol 是：

1. 校验 user/session、bounded prompt（1–65536 字符）、最多 32 个归属当前 user 的 ready attachment，并以 `(user_id, session_id, client_id)` + request digest 幂等写入 `accepted`。
2. `delivery=followup` 映射到 `next-turn`；`steer`/`inject` 映射到 `next-step`。`followup` 与 `steer` 会尝试 wake；`inject` 只排队，不主动启动空闲 Session。
3. 若 Driver 正忙，输入保持 FIFO `accepted`，不会 abort 或抢占当前 generation。若空闲，dispatcher 先 `reserve_run()`。
4. 在一个 exact run-fenced 事务内把 boundary 输入改为 `claimed`，物化 User Message/TextPart/FilePart，写 `inbox.claimed` AgentEvent，并把最后一个 Message 绑定为 Driver trigger。同一 generation 的首批与后续 next-step Message 共用一个 logical `turn_id`；只追加一个 `turn.started`，最终 Assistant 以同一 identity 追加 `turn.finished`。
5. 事务提交后才以 strict 模式逐 Inbox item 投送附件并 wake `run_loop`；必须按 exact asset ID 全部落盘。每次失败都在该 item 写入 bounded `delivery_attempts` 与不含 secret 的 `delivery_last_error`：临时传输错误把同一 `reserved` marker 立即过期留给 recovery，第三个 durable pass 仍失败或资产已删除/不可用时，在 exact run fence 内写 terminal Assistant error、把该 item `settled(delivery_error)` 并释放 Driver，后续扫描不再选择它，且全程不启动模型。坏 item 与同批有效 item 隔离，有效 FIFO 输入仍可继续。Python Task 不是 durable acceptance 的依据。
6. Loop 在每次读取上下文前 claim `next-step`；一个逻辑 Turn 只 claim 一条 `next-turn`。stop 时若仍有 `next-step` 则继续下一步；释放当前 generation 后 dispatcher/recovery 再续跑下一条 followup。
7. 当前 generation 完成时把它消费的条目写为 `settled` 并绑定 exact result Message；用户 stop 先取消尚未 claim 的 `accepted` 条目，再读取 durable Driver 并对当时的 exact run/generation 做 CAS abort。同步 API 等待自己的 item 进入 `settled/canceled`，而不是等待“Session 最新结果”；空闲 `inject` 不 wake，因此同步入口直接返回 `202` receipt。

表结构位于 [`backend/db/models/agent_inbox.py`](../backend/db/models/agent_inbox.py)，基础迁移为 [`backend/db/migrations/versions/b5e8f1a4c7d0_agent_inbox_driver_wake.py`](../backend/db/migrations/versions/b5e8f1a4c7d0_agent_inbox_driver_wake.py)，bounded attachment recovery 迁移为 [`backend/db/migrations/versions/d0a2c4e6f8b1_agent_inbox_delivery_attempts.py`](../backend/db/migrations/versions/d0a2c4e6f8b1_agent_inbox_delivery_attempts.py)，协议实现位于 [`backend/agent/inbox.py`](../backend/agent/inbox.py)。普通同步/异步 Prompt 使用 Inbox；Regenerate、Plan accept/reject、Summarize、Command 保留显式直接 ownership 语义。Regenerate 不创建重复 User Message，而是把保留下来的最后一个 User Message 绑定为 trigger。

`accepted`、`claimed`、`canceled`、`settled` 都追加同名 `AgentEvent`（Surface projector 对这些 kernel lifecycle event 为 no-op）。附件身份快照在 accept 时校验，并在 claim 事务重新校验；Message、全部 Parts、claim 和 Driver bind 要么一起提交，要么一起回滚。

`max_concurrent_agents` 在 `reserve_run()` 和 provider-capable `reserve_recovered_run()` 的数据库事务内执行，而不是依赖 API 预检。PostgreSQL 统一取得 global→user advisory transaction lock，再按 Session→Driver 顺序判定 DB-clock live lease；SQLite 使用 loop-local serializer 与 `BEGIN IMMEDIATE`。Heartbeat 和所有会延长 deadline 的 phase transition 走同一锁协议，避免“旧 expiry 已过、未提交续租随后复活”造成瞬时超额。额度已满时 exact recovery 不改写旧 run/generation/phase/trigger marker，由后续 periodic pass 在槽位释放后重试；同一轮内多个 expired marker 最多只启动可用槽位数。

### 3.4 Abort 与 preemption

`request_abort()` 同时设置当前进程的 Event，并对活动、未过期 Driver 写 `abort_requested_at`。Monitor 在别的 Worker 中也能观察它。`wait_for_idle()` 会持续跟踪 replacement generation，不会因为旧 generation 刚释放就错误返回。见 [`backend/agent/driver.py`](../backend/agent/driver.py) 和 [`backend/session/abort.py`](../backend/session/abort.py)。

中止不会宣称未知副作用已经回滚。中断 marker 明确提醒后续 Agent：已提交的 provider task 或半执行工具需要先查询外部状态，不能盲目重试。

## 4. Cold recovery

WUYING/provider reconciliation 和 Redis event bus 初始化完成后，Backend 先执行一次有序恢复，然后由独立于 Cron 的 Agent recovery service 每 15 秒周期扫描。每轮既观察过期 Driver，也无条件扫描 main Inbox 的 `accepted` 与过期 `claimed`；因此 accept→reserve、claim→wake、terminal→settle 任一窗口崩溃都可在没有过期 Driver snapshot 的轮次收敛。滚动启动不会清除健康 peer 的 lease。生命周期入口在 [`backend/main.py`](../backend/main.py)，有序单轮与 start/stop loop 位于 [`backend/agent/recovery_service.py`](../backend/agent/recovery_service.py)。

| 每轮观察到的状态 | 校验 | 恢复动作 | 是否重放模型/工具 |
|---|---|---|---:|
| 未过期 active lease | 无 | 保持不动，由原 owner 继续 | 否 |
| `reserved` + 有效 User trigger + 尚无 child assistant reply | trigger 属于同一 user/session；旧 marker 的 run/generation/phase/trigger exact-CAS 且仍过期；硬并发额度仍有槽位；收集其 FilePart asset IDs | 原子申请新 generation 并继承 trigger，逐 item 投送已落库附件，wake `run_loop`；额度满则保持 exact marker 到下一轮 | 是（仅成功取得槽位且附件严格落盘后） |
| `reserved` + trigger 已有 assistant reply | parent_id 指向该 trigger | 新 generation 立即 release 为 idle | 否 |
| `reserved` 但无 trigger、trigger 丢失或不属于 owner | fail closed | 标记 error，进入 tail repair | 否 |
| `running` | 不推断 provider/tool 是否完成 | tail repair | 否 |
| `finalizing` | 不推断 cleanup 完成度 | tail repair | 否 |
| Main Inbox `accepted` | user/session ownership；仅 `followup`/`steer` 可主动 wake | 尝试 reserve；同事务 claim/materialize/bind，冲突则保留 FIFO | 是（仅成功取得新 `reserved` generation 后） |
| Main Inbox 过期 `claimed` | exact run/generation 与 Driver phase | 活动 Driver 保持不动；过期 Driver takeover 后 rebind；Driver 已 idle 时用短 maintenance generation 结算 terminal claim | 由 Driver phase 决定 |
| Task descriptor `accepted`，child 尚未 bind | 原 parent generation 已不再 live，且 parent message 仍是 tail | reserve child 后再检查一次 parent/tail fence，绑定并 wake | 是 |
| Task child 过期于 `running`/`finalizing` | descriptor 的 child run/generation 精确匹配 | 写入 `task_child_outcome_unknown` 的 bounded outbox | 否 |
| Task child Driver 已 `idle` | Child Session 为 `idle`，且有精确 `parent_id=trigger` + `finish=stop` 的 Assistant reply | 成功结果进入 outbox；否则写入 `task_child_no_terminal_result` | 否 |
| Task outbox `completed` | parent 无 live generation，原 Message 仍是 tail，Part/tenant/fence 精确匹配 | maintenance generation 幂等写回 Task Part，并闭合开放 tail | 否 |
| Task outbox `completed`，parent transcript 已前进 | 原 Message 不再是 tail | 保留 outbox 与新 transcript，完全 no-op | 否 |

Tail repair 的工具矩阵：

| ToolPart 原状态 | 恢复码 | 含义 |
|---|---|---|
| `pending` | `tool_not_started` | 调度器尚未确认 body 启动，可以确认未执行。|
| `running` | `tool_outcome_unknown` | body 可能产生外部副作用；禁止自动重试。|

Repair 还会补齐未配对的 `step-finish`、把受影响 Assistant Message 终止为 `aborted`，并在 transcript 提交及末次 fence 通过后，由 exact lease release 同事务将 Session 投影为 `error`。Repair 本身通过 expired-marker exact-CAS 取得新的 `finalizing` maintenance generation，避免和刚到达的新 Prompt 交错；若进程在 claim 后再次崩溃、终态提交失败，或 repair 被异常/超时取消，当前 identity 会保持或立即变为过期 marker，供下一轮重试。即使本轮没有 expired Driver，service 仍扫描已完成 Task outbox 和 descriptor→reserve 的 unbound child。实现与测试分别位于 [`backend/agent/recovery.py`](../backend/agent/recovery.py)、[`backend/agent/recovery_service.py`](../backend/agent/recovery_service.py)、[`backend/tests/unit/test_agent_recovery.py`](../backend/tests/unit/test_agent_recovery.py)、[`backend/tests/unit/test_agent_recovery_service.py`](../backend/tests/unit/test_agent_recovery_service.py)。

### Durable external-effect ledger

跨 provider/OSS 的不可逆调用使用独立账本
[`backend/agent/effect_ledger.py`](../backend/agent/effect_ledger.py)。稳定
`effect_id` 不包含 Agent generation；同一逻辑调用若 request hash 改变会 fail closed。
首次 `prepare` 和 dispatch 前最后一道门都校验 exact live
`(tenant, session, run_id, generation)`，而 effect worker 使用另一组单调
`claim_generation + token + owner + DB-clock lease`。因此 takeover 后旧 worker 即使迟到收到
provider response，也不能覆盖新 claim。

状态机为 `prepared → submitting → accepted → succeeded/failed`，不确定边界进入
`outcome_unknown`，无法查询 provider receipt 时进入 `manual_review`。`submitting` 必须先于
网络调用提交；启动与周期恢复只调用 adapter 的 query/reconcile，不会再次执行 dispatch body。
receipt、最终 projection 与可选 domain projector 在同一事务中提交；projector 失败则整体回滚。
尝试次数、扫描批次与 evidence payload 都有界，evidence 只保留 field-aware 清洗后的公开安全数据。
迁移为 `c6f9a1d3e5b7`；数据库 readiness 明确要求主表与 append-only evidence 表。

当前 `image_gen` 是完整适配：确定性的 FileAsset/OSS 投影可对账成功；同步图像 API 没有可查询
handle，响应字节丢失时只能人工复核，绝不重新付费提交。TokenSpace/STT/video/OSS 仍保留各自
已经实现的 receipt、CAS、heartbeat 和 fail-closed 逻辑；它们的剩余 adapter 边界见
[`docs/MEDIA_EFFECT_SAFETY.md`](MEDIA_EFFECT_SAFETY.md)，不伪称全 provider exact-once。

## 5. Session Surface 与 append-only provenance

### 5.1 当前事实源边界

普通 Agent 的每次模型上下文只从连续的 `agent_events` prefix 投影；序列缺口、缺少 model seed 或损坏的 replacement 会 fail closed，不会退回 SQL transcript。旧 Session 首次加载时在 owner lock 事务中确定性写入公开 seed 与 private model seed。公共分页与 reconnect 仍从 `messages`/`parts` read model 读取，见 [`backend/session/session.py`](../backend/session/session.py)。

当前并不是完整 event-sourced Session：Session 配置与状态、Todo store、文件系统以及 Tool reveal catalogue 仍有各自事实源。当前有两层 append-only 历史：

- `agent_events`：普通 Agent transcript 的 canonical truth；同事务记录 Message/Part、Turn/Step/Tool 生命周期、Surface remove/replacement、API-hidden provider replay 与 model request checkpoint，可重建公开 SQL Surface 和模型 replay Surface。
- `session_surface_events`：在两种破坏性投影变化前保存被隐藏 branch 的完整恢复快照：

- `regenerate`：隐藏目标 Assistant Message 及其后续 branch，然后以新的 `(run_id, generation)` 生成 replacement。
- `dismiss`：隐藏一个失败 turn，不创建 replacement generation。

### 5.2 Append before delete

`delete_messages_from()` 和 `delete_failed_turn()` 必须先锁定 owner Session，在同一数据库事务内调用 `append_surface_change_locked()`，随后才删除 live public/private rows。事件包含：

- per-session 单调 `sequence`；
- `kind`、`anchor_message_id`；
- 完整 `hidden_message_ids`；
- 可恢复的完整公开 Message/Part snapshot；
- Regenerate 的 `replacement_run_id` 与 `replacement_generation`。

Snapshot 只使用 `public_part_data()`，不会把 private provider binding/内部凭据复制进审计面。任一 hidden message 或 part 越界、缺失时整个事务 fail closed；事件 append 或 delete 任一失败都会共同回滚。

实现：[`backend/session/surface_log.py`](../backend/session/surface_log.py)、[`backend/db/models/session_surface_event.py`](../backend/db/models/session_surface_event.py)、迁移 [`backend/db/migrations/versions/fa2c4e6d8b0a_session_surface_events.py`](../backend/db/migrations/versions/fa2c4e6d8b0a_session_surface_events.py)。

限制：Compaction 仍由 [`backend/agent/compaction.py`](../backend/agent/compaction.py) 与 [`backend/session/compaction.py`](../backend/session/compaction.py) 维护兼容 Message/Part Surface，但其替换范围与 provenance 已由稳定 Agent Event range 约束；`session_surface_events` 仍不能单独重建整个 Session。

### 5.3 Canonical Agent Event log 与双 Surface projector

[`backend/session/agent_event_log.py`](../backend/session/agent_event_log.py) 把以下稳定事件追加到 [`agent_events`](../backend/db/models/agent_event.py)：

- `surface.seed`、`surface.model_seed`、`surface.model_import`、`surface.messages_removed`；
- `turn.started`、`turn.finished`；
- `message.created`、`message.updated`；
- `part.created`、`part.updated`；
- `step.started`、`step.finished`；
- `tool.called`、`tool.updated`、`tool.result`；
- `provider.transcript`（private model replay sidecar）；
- `model.requested`（发送 provider 前的不可变 prefix checkpoint）；
- `surface.replacement`、`session.forked`。Replacement 对 model projector authoritative；public projector 与 lineage 均不暴露 private sidecar。

每个 Session 先锁 owner Session row；desktop SQLite 在第一次读取前取得 `BEGIN IMMEDIATE`。Agent-owned mutation（包括 Todo/Plan/Task 与媒体工具追加的公开 Part）还在同一事务中验证精确 `(run_id, generation)` Driver fence。相同事务同时提交 read model 与事件，任一失败共同回滚。`(session_id, sequence)` 连续单调，`(session_id, event_key)` 唯一。普通状态变化使用 occurrence identity，因此 `A → B → A` 会保留三个事件；只有持有稳定 operation id 的调用才使用显式 idempotency key，重复 Part create 也在 read-model precondition 层收敛，恢复不会覆盖旧事件。

写入边界是 field-aware 的：User TextPart、Tool input/output 与普通结构内容原样保留，包括中文、emoji、`api_key` 字段名和 `sk-...` 示例；只有 Message provider error、Tool provider-owned metadata/error 与 InternalPart provider transcript 子树会递归移除 credential/auth 字段及秘密值。安全后的 SQL read model 状态被精确复制到事件。ToolPart 的 canonical ID、provider wire name、binding digest、dialect、stream sequence，以及 provider transcript 的无秘密 replay 字段，仅进入 private model sidecar；不记录 API key 或 raw auth header。

`project_agent_events()` 是无数据库副作用的公开 projector，只产生 REST/UI 可见 Message/Part，忽略全部 private replay。`project_model_agent_events()` 是同样纯且确定性的模型 projector，在公开状态上恢复隐藏 Tool identity 与 provider transcript，并应用最新有效 `surface.replacement`。`rebuild_sql_read_model_from_events()` 可删除并重建 Message、Part 与 provider transcript read model；`verify_agent_event_parity(...)` 只比较公开 projector，确保 private sidecar 永不进入 API parity Surface。

每次 provider attempt 前，Loop 先从 canonical Event prefix 构造候选请求，并在 checkpoint 前冻结 Todo notice/pacing、解析后的图片、system/messages、model/variant/tool choice，以及实际 provider wrapper（含 `_noop` 或 native→portable fallback）的有序 tool definitions。随后在 exact `(run_id, generation)` fence 事务中用候选的 event sequence+digest 做 CAS；只有 prefix 未漂移才追加 `model.requested`。Event 只记录 replacement generation、model、binding/tool-schema digest 与最终 `prompt_shape_digest`，不记录 raw prompt、图片数据、tool 描述、credential 或 auth header。CAS 漂移会重新投影并重建，provider 只收到刚刚通过 CAS 的同一冻结对象，checkpoint 后不再读取可变 prompt state。

Todo notice 是 presentation/planning 提示而非 canonical transcript：attempt 只读取非破坏性快照，pre-stream error、CAS drift 和 retry 都复用该快照；收到完整 provider response 后才精确确认该前缀，并保留飞行期间新追加的 notice。若进程在 provider response 后、确认前崩溃，语义是安全的 at-least-once 重复提示，而不是静默丢失。

每次模型 load 共用同一 tail-repair primitive：当前准确活跃 generation 永不被修复；idle/无 live driver 或旧 generation 的 pending Tool 变为 `tool_not_started`，running Tool 变为保守 `tool_outcome_unknown`，并补齐 Step/Assistant/Turn 终态。旧 `turn.finished` 不能覆盖同一 logical turn 中后来追加的 User；若最新 User 尚无精确 `parent_id` 回复，repair 会为它建立独立的 aborted Assistant，recovery 也拒绝用旧回复或 `result=None` 静默结算 claimed Inbox。Repair 沿用原 logical run identity，避免 maintenance writer generation 破坏 strict balance。

事实源边界：`agent_events` 是普通 Agent transcript/model request context 的 serving truth，但不单独恢复 Session 元数据、Todo、Tool reveal catalogue 或文件系统。流式 delta 仍只在持久 Message/Part checkpoint 时进入 canonical history。

Event 表迁移仍复用既有 [`a8c1e4f7b9d2_agent_event_shadow_log.py`](../backend/db/migrations/versions/a8c1e4f7b9d2_agent_event_shadow_log.py)；canonical serving 本身不新增 migration。验证：[`test_agent_event_log.py`](../backend/tests/unit/test_agent_event_log.py)、[`test_canonical_model_surface.py`](../backend/tests/unit/test_canonical_model_surface.py)、[`test_agent_event_migration.py`](../backend/tests/unit/test_agent_event_migration.py)、[`test_event_range_compaction_fork.py`](../backend/tests/unit/test_event_range_compaction_fork.py)。

### 5.4 Stable Event range Compaction 与 Fork

[`backend/session/event_range.py`](../backend/session/event_range.py) 从 Event projector 选择只包含完整已闭合 Turn/Step/Tool 的公开 Message 子集，并记录 `start_sequence`、`end_sequence`、canonical SHA-256 digest 与有序 `covered_message_ids`。Turn 按 event 中的 logical `(run_id, generation, turn_id)` 聚合，因此 `User → Assistant(tool_calls) → next-step User → Assistant(stop)` 是一个可闭合边界，而不会被 SQL 连续 role 分组错误截断。闭合 group 必须以 terminal Assistant 结束，且该 Assistant 的 `parent_id` 必须精确指向该 logical turn 的最新 User；`User → Assistant(stop) → late User` 仍是 dangling turn。旧会话必须先写一次 `surface.seed`；开放 Assistant、pending/running Tool、不配对 Step 或用户/中间 Assistant cutoff 都不能成为 Fork 边界。

Compaction 先冻结原始 Event range，再只在 detached provider view 上标记 aggressive tool-output prune；provider 前不会永久改写源 Part。释放 Session 锁调用 provider 后，返回时在同一 owner/run fence 下重新锁 Session，并以当前 Event 投影重算被覆盖 Message 状态。只有 CAS digest 仍相同且 summary token 估值严格小于实际 provider source payload 时，才在**同一事务**写入 summary TextPart、完成 Assistant、补全 CompactionPart range descriptor，并追加 log-only `surface.replacement`。漂移、空输出、provider 错误或不收缩的 summary 只留下无 `finish` 的诊断尝试，原始 ToolPart 保持不变，也不会被 `filter_compacted()` 识别为 boundary。新 filter 使用完整 replacement descriptor；字段部分写入或损坏时 fail closed，旧 descriptor 继续走 legacy 兼容路径。

普通 Session Fork 的 `None` cutoff 表示“最后一个完整闭合 Turn”，显式 cutoff 必须是完整 Turn 的终态 Assistant。复制前先冻结 Event-projected prefix；随后源 Session lock/CAS、源 Project live-owner 校验、目标 Session 建立、remapped Message/Part seed、完整包含在 fork range 内的 remapped `surface.replacement` authority 与 `session.forked` lineage 全部在一个事务中完成，提交后才发布 child。导入 replacement 保留父 Session 的原始 range/digest provenance，并以独立 child-id projection 字段应用相同 compaction，因此 fork 后的 model Surface 不会重新看到已被替换的旧上下文；public Surface 仍保留兼容历史。CAS、Part drift 或 lineage append 失败均不留下可见空 child。lineage event 包含 source Session/range/digest 与 ID mapping，且是 public projector no-op，因此目标 SQL/Event Surface parity 保持成立。普通 Session Fork 与 Task `fork` 共用 [`clone_stable_event_prefix_locked`](../backend/session/fork.py)：Task 额外把 child Session、delegation trigger、descriptor/activation/outbox 和 exact parent ToolPart pointer 放进同一个事务，开放的当前 Turn 不会进入 seed。

### 5.5 上下文与工具 Schema 预算

顶层 `build` Agent 默认使用 provider-independent `portable` 工具暴露，而不是把完整
Skill/MCP/媒体工具目录塞入每次模型请求。每一步先冻结 eligible catalogue、执行映射与
provider wire name；模型只看到 resident core、当前意图 pack、已验证 reveal 和
`capability_search`。搜索得到的 Schema 从下一 step 才可执行，执行前仍重新经过权限、审批、
计费和 sandbox 边界。

- portable model-visible hard cap 为 32,000 serialized chars；目录本身仍受 128,000 chars
  provider wire ceiling 约束。
- `legacy_eager` 只保留为显式回滚模式，不再是默认值。
- `native_auto` 只有 endpoint、model 与 binding allowlist 同时命中后才尝试原生 Tool Search；
  没有可验证能力或明确不支持时 sticky fallback 到 portable。
- config-defined Agent 只有显式把 discovery slot 加入白名单才进入 portable；否则继续 shadow，
  防止工具资格在迁移时被静默扩大。

实现位于 [`backend/agent/tool_runtime.py`](../backend/agent/tool_runtime.py)、
[`backend/agent/tool_exposure.py`](../backend/agent/tool_exposure.py) 和
[`backend/agent/native_tool_search.py`](../backend/agent/native_tool_search.py)。

## 6. Ordered Tool Scheduler

Provider stream 完成后，Processor 先验证整批 Tool Call ID：同一 provider call ID 对应不同 payload 会在任何 executor 进入前整批 fail closed；字节相同的重复 event 只执行一次。见 [`backend/agent/processor.py`](../backend/agent/processor.py)。

每个调用被拆成四段：

1. `prepare`：创建/更新 RUNNING card，做 executable frontier、validation、doom-loop、permission 和 hook prepare；严格按模型顺序。
2. `body`：只有这一段允许并发。
3. `finalize/commit`：整理 Hook result、Agent switch、ToolPart 和 SSE；严格按模型顺序。
4. `aborted_before_dispatch`：为未启动调用生成明确的 synthetic error result。

[`backend/agent/tool_scheduler.py`](../backend/agent/tool_scheduler.py) 用 `slots[]` 保存 body outcome，以 `started` 和 `committed` 作为启动/提交 cursor。后一个 slot 即使先完成，也必须等待前面的连续 slot settle 后才能 commit。

### 6.1 并发与 exclusive barrier

- `ToolInfo.parallel_safe` 和 `define_tool(..., parallel_safe=...)` 默认都是 `False`，见 [`backend/tool/tool.py`](../backend/tool/tool.py)。
- 只有值精确为布尔 `True` 才能进入并发组；未知、缺失、truthy object 或 classifier 异常全部按 exclusive 处理。
- 当前显式并发的主要是已审查的 read/search 类工具，如 Glob、Grep、Web Fetch/Search 和 Todo Read。
- Exclusive 调用是 singleton barrier：必须等待前面的并发组全部 commit，结束后后续组才可启动。
- [`backend/tool/batch.py`](../backend/tool/batch.py) 同样拒绝任何未显式标记 safe 的嵌套工具，不能绕过主调度器的 fail-closed 默认。

### 6.2 Abort 与异常

Abort 停止补充新 body，但不会遗弃已经启动的 body：调度器先 drain 已启动任务，再按 slot 顺序提交结果，并为所有未启动 suffix 提交 `aborted_before_dispatch`。发生异常时也先 quiesce in-flight body，再抛出首个异常。

已经启动且成功完成的 body 可以在 abort 后以真实成功结果提交；系统不会谎称它没有执行。反过来，如果 body 不合作且永久阻塞，通用调度器不会强制杀死它，整个 group 仍可能等待；外层 Tool 自己必须实现 timeout/abort。测试位于 [`backend/tests/unit/test_ordered_tool_scheduler.py`](../backend/tests/unit/test_ordered_tool_scheduler.py) 和 [`backend/tests/unit/test_processor_tool_concurrency.py`](../backend/tests/unit/test_processor_tool_concurrency.py)。

## 7. 每步模型选择

模型不是只在 Session 创建时选择一次。每个 Agent step 按以下优先级重新解析：

```text
AgentDef.model
    > 当前最后一个 User Message.model
    > Session 持久 base model
    > deployment config default
```

实现位于 [`backend/agent/model_resolve.py`](../backend/agent/model_resolve.py)，调用点在 [`backend/agent/loop.py`](../backend/agent/loop.py)。

- `AgentDef.model` 是临时 override，不会写回 Session base。
- User Message 上的 model 固定一次 turn 的用户选择。
- 每个候选都经过当前 deployment model catalogue 校验；不可用时回退到 config default。
- 旧 Session 的 base model 已不可用时，Loop 会把 fallback 持久化回 Session，避免每步重复失败。
- Assistant Message 可以记录实际执行模型，但这不改变上述下一步优先级。

Cron 创建临时 Session 前还有一层产品选择：`CronJob.model > notify Session.model > config default`；进入 Agent Loop 后仍遵守相同的 per-step 解析。见 [`backend/cron/executor.py`](../backend/cron/executor.py)。

## 8. Task 子代理

`task` 保留旧 `spawn` 参数的 one-shot 默认行为，并增加 closed-prefix `fork`、continuable `spawn`、`follow_up`、`interrupt`、`report` 和 `list`。执行入口位于 [`backend/tool/task.py`](../backend/tool/task.py)，持久协议位于 [`backend/agent/subagent_runtime.py`](../backend/agent/subagent_runtime.py)，Provider/Agent 组合协议位于 [`backend/agent/subagent_composition.py`](../backend/agent/subagent_composition.py)；旧 [`backend/agent/task_handoff.py`](../backend/agent/task_handoff.py) 仍由 recovery 兼容。

已实现事实：

- `accept_spawn` 在一个事务内校验 tenant/project/parent exact ToolPart 与 `(run_id, generation)` fence，并创建具有与公开 Session API 相同不变量的 Child Session、exact User trigger/event seed、descriptor、activation、waiting outbox 和 parent pointer。提交失败不留 child/message orphan。
- `fork` 先冻结 parent 最后一个完整闭合 logical Turn 的 canonical Event prefix；range end 截止该 closed Turn 的末个 Event（含属于该 Turn 的 replacement，但早于下一 `turn.started`），digest 只覆盖稳定 prefix 与所选 closed Surface。随后在 acceptance 事务内做 source lock + digest CAS；开放 Turn 的后续 append 不会误触 drift，covered closed state 的变化仍会拒绝。remapped public Surface、private Tool identity/provider replay、`surface.replacement` 与安全 lineage 一并导入。
- Child 继承父 Session 的严格 `project_id`/workdir；模型优先使用 child Agent definition override，否则继承父 Session model。协议不再修改 `SandboxManager` 私有映射。
- 新 descriptor 使用向后兼容的 private `authority_snapshot` v2，原子保存 SHA-256 保护的 exact composition：配置内 model、由 `ModelConfig.provider` 解析的 credential slot、显式 provider/model capability 与 reasoning 声明、无密钥 endpoint/readiness binding digest、冻结 Agent preset/prompt/permissions、persona overlay、父 authority 与 child preset 的 tool allowlist 交集、bounded output schema 和 fresh/fork seed mode。模型不存在、binding 未就绪、provider/agent 未声明 capability、tool 越过父边界或 child preset、build-only 媒体工具、schema 超预算都会在 acceptance 前 fail loud；不再从 provider/model 名字猜 capability，v1 descriptor 仍按旧语义恢复但绝不新增 composition capability。
- Cold resume 从 descriptor Context-local 恢复冻结 preset，实际 system prompt 优先使用该 preset prompt + persona，不会被 `build`/`plan` 名称路由绕过；provider/model catalogue、endpoint、readiness 或 capability binding 漂移均 fail closed。Follow-up 的 model/reasoning/persona/schema 不可改变，tools 只能与当前 delegator authority 再求交集，因而不能扩大首次接受的权限。
- 每次 spawn/follow-up 是独立 activation，有唯一 `(descriptor_id, descriptor_generation)`、exact child trigger 和当前 parent ToolPart；旧 follow-up Part 不会被后续轮次覆盖。Descriptor 行的 generation/active pointer 在 PostgreSQL 和 SQLite 上共用同一锁与唯一性规则。
- Foreground 与 periodic recovery 都必须对 activation 做 DB exact-CAS claim，claim 用 DB clock 的 owner lease 并辅以本地 monotonic deadline。输家等待同一 outbox；claim 过期后可 takeover，且 bind 前会再验证 claim、Driver、trigger 和 interrupt generation fence。
- Continuable descriptor 在前一 activation 收敛后复用同一 Child Session transcript；Resident child 可 wake，非 Resident child 可按 exact accepted trigger cold resume。运行中或 finalizing 边界的崩溃只生成 `outcome_unknown`，不自动重放不确定 provider/tool 边界。
- Durable interrupt 先提交 descriptor generation fence，scanner 在任何 reserve/wake 之前消费该 fence；已绑定的 child 收到 durable abort。关机先收敛 recovery-owned child，再有界等待/取消。
- Outbox 区分 `succeeded`、`interrupted`、`outcome_unknown` 和 `error`。只有 child `idle` 且 exact trigger 的 Assistant reply 为 `finish=stop` 才可能成功；请求 `output_schema` 时 terminal `Message.structured` 还必须通过本地 Draft 2020-12 JSON Schema 校验，错误类型、额外字段或缺失结构都会 fail closed。通过校验的 bounded structured payload 优先进入 result/outbox，即使没有 TextPart 也不报空。Prompt 限 65,536 字符，title 限 255，result/metadata 只保存 bounded allowlisted projection。
- `report`/`list` 仅允许直接 parent lineage 且同 tenant/project，结果仅来自终态 outbox。Parent 交付使用 exact Part 幂等投影，并保留旧 `task_handoff_id + task_outbox_completed` marker 的 Processor 兼容。

表结构与迁移：[`backend/db/models/subagent.py`](../backend/db/models/subagent.py)、[`backend/db/migrations/versions/fe6a8c0e2b4d_subagent_continuations.py`](../backend/db/migrations/versions/fe6a8c0e2b4d_subagent_continuations.py)。三表分别是 descriptor、activation inbox 和 result outbox。

已知限制：

1. **不自动续跑 parent Loop。** Recovery 可精确投递 child 结果并闭合原 tail，但不能证明 parent 在崩溃前是否跨过兄弟 Tool/模型边界，因此不会重启父模型或自动继续后续 Tool。用户或新 Prompt 需在可见的 aborted/error tail 上继续。
2. **不保证子代理没有外部副作用。** Generation fence 防止旧 Worker 继续提交 Backend 副作用，但已发送到 provider/远程工具的请求不可撤回，因此不确定边界只报 `outcome_unknown`。

定向测试：[`backend/tests/unit/test_subagent_runtime.py`](../backend/tests/unit/test_subagent_runtime.py)、[`backend/tests/unit/test_subagent_composition.py`](../backend/tests/unit/test_subagent_composition.py)、[`backend/tests/unit/test_subagent_migration.py`](../backend/tests/unit/test_subagent_migration.py)、[`backend/tests/unit/test_task_handoff.py`](../backend/tests/unit/test_task_handoff.py)、[`backend/tests/unit/test_task_durable.py`](../backend/tests/unit/test_task_durable.py)、[`backend/tests/unit/test_subagent_project.py`](../backend/tests/unit/test_subagent_project.py)、[`backend/tests/unit/test_event_range_compaction_fork.py`](../backend/tests/unit/test_event_range_compaction_fork.py)。

## 9. WUYING control plane / execution plane

### 9.1 Provider ownership

[`backend/sandbox/wuying.py`](../backend/sandbox/wuying.py) 把 WUYING 当作预配置、长期存在的外部桌面：Backend 不创建、停止或删除它；`reconcile()` 只检查 Action Server 可达性。当前 Provider 把所有 user 路由到同一物理 desktop，这只是开发/验收拓扑。

Backend `SandboxManager` 仍按 user 管理逻辑 client，并为每个 client 派生 pseudonymous `u-<hash>` scope。Session 在同一 project 中共享工作树；不同 project 使用不同目录。见 [`backend/sandbox/manager.py`](../backend/sandbox/manager.py)。

### 9.2 跨执行面的 Run fencing

`run_loop` 把当前 `RunLease` 绑定到 contextvar。`SandboxClient` 为该上下文中的请求添加：

```text
X-OpenBox-Session
X-OpenBox-Run
X-OpenBox-Run-Epoch: <generation>
X-OpenBox-Run-Lease-Expires: <database lease expiry in epoch ms>
X-OpenBox-Run-Lease-Signature: HMAC-SHA256(...)
```

Backend 对每次请求都从当前绑定的 `RunLease` 读取最新数据库 expiry，并用仅控制面与
Action Server 持有的 API key 签名；结算一开始就撤销本地 transport receipt。Agent 请求前
还要求 `/alive` 声明 `run_lease_receipt_v2`，旧 Action Server 会 fail closed，不能静默退回
仅 high-water 的协议。Action Server 先验证签名、严格到期时间和最大 65 秒 lifetime，再按
Session 保存最高 `(epoch, run_id)` 到 `/data/openbox_run_fences.json`：

- 更低 epoch 返回 HTTP 409；
- 同 epoch 但不同 run_id 返回 HTTP 409；
- 更高 epoch 原子持久化后成为新 fence；
- fence store 无法落盘时返回 503，而不是无 fence 执行。

Backend header 生成在 [`backend/sandbox/client.py`](../backend/sandbox/client.py)，执行面校验在 [`container/action_server.py`](../container/action_server.py)。

边界：receipt 依赖 Backend DB 与 Action Server 的系统时钟保持同步；部署/ready gate 必须持续
监测时钟漂移。run fence 阻止 stale Worker 发起**下一次**执行面请求，不会自动杀死已经被旧
epoch 成功启动的 shell、浏览器动作或外部 provider task。其结果仍需依赖 Tool timeout、
process kill、idempotency key 或 `tool_outcome_unknown` 恢复语义。

### 9.3 Runner UID 与文件边界

Action Server systemd service 保持 root 控制身份，以便保护 API key/MCP config、切换 UID 和维护文件所有权；不可信工作不会继承该身份：

- shell/stream/PTY、Skill install script、Git/解包、stdio MCP 等通过 `setpriv` 降为非 root `sandbox` 账户；UID 由桌面系统分配，不依赖硬编码数字；
- runner environment 是显式 allowlist，不传递 Action Server、provider 或 MCP 控制面 secrets；
- 任意 file API 路径必须落在 `/workspace`，只读 Skill resource 另允许当前 user scope 的 Skill root；`..` 和 symlink escape 会被拒绝；
- systemd 配置启用 `NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome=read-only` 和收窄的 capability bounding set。

实现与部署入口：[`container/action_server.py`](../container/action_server.py)、[`backend/scripts/wuying_bootstrap.py`](../backend/scripts/wuying_bootstrap.py)、[`backend/scripts/wuying_deploy_action_server.py`](../backend/scripts/wuying_deploy_action_server.py)。

### 9.4 Workspace namespace

规范路径由 [`backend/project/workspace.py`](../backend/project/workspace.py) 生成；raw user/project/asset id 从不直接拼接路径：

```text
/workspace/openbox/users/u-<hash>/projects/p-<hash>-<slug>/
/workspace/openbox/users/u-<hash>/.openbox/uploads/...
/workspace/openbox/users/u-<hash>/.openbox/snapshots/...
/workspace/openbox/users/u-<hash>/.openbox/exports/
/data/skills/u-<hash>/
/data/mcp/u-<hash>/config.json
```

完整约定见 [`docs/WORKSPACE_NAMESPACING.md`](./WORKSPACE_NAMESPACING.md)。Hash namespace 防止误碰撞和同 slug 覆盖，但不是恶意租户的安全边界：共享桌面上的任意 shell 都以同一个 `sandbox` OS 用户执行，获得任意命令能力的用户理论上仍可遍历其他 user 目录。

Desktop GUI 另有 Action Server 级 lease，串行化 input → capture → upload；它解决多个 Run 操作同一物理桌面的互相踩踏，不替代 Agent generation fence。实现也在 [`backend/sandbox/client.py`](../backend/sandbox/client.py) 与 [`container/action_server.py`](../container/action_server.py)。

## 10. Cron lease 与 Agent 的关系

Cron 使用独立于 Agent Driver 的 durable ownership，因为一个 Cron claim 覆盖“排队、创建临时 Session、Agent 执行、结果/投递结算”的完整业务运行。

`CronJob` 保存 `run_generation`、`run_token`、`run_owner`、`lease_expires_at`、`heartbeat_at`；`CronRun` 复制 claim identity 作为审计 fence。模型位于 [`backend/db/models/cron.py`](../backend/db/models/cron.py)，迁移位于 [`backend/db/migrations/versions/e9f1a3c5d7b9_cron_run_leases.py`](../backend/db/migrations/versions/e9f1a3c5d7b9_cron_run_leases.py)。

当前协议：

- timer 和 manual trigger 都通过 [`backend/cron/lease.py`](../backend/cron/lease.py) 的同一个 conditional `claim_job()`；
- token 随机，generation 单调递增，owner 标识进程；TTL 90 秒、heartbeat 20 秒；
- lease 更新和最终 result apply 使用数据库 statement-time clock，并匹配完整 token/generation/owner；
- worker 只有即将执行时才 claim，避免队列等待期间 lease 无 heartbeat 过期；
- claim 后重新读取 Job 的 project/prompt/model/schedule，避免使用收集阶段的陈旧 payload；
- takeover 会把更老 generation 的仍-running `CronRun` 标为 error；失去 lease 的 Worker 丢弃结果；
- startup recovery 只清理真正过期的 claim，并只结束属于该 claim 的 run；missed schedule 每个 Job 最多补一次且受 staleness window 限制。

Timer/result fence 位于 [`backend/cron/timer.py`](../backend/cron/timer.py)，startup repair 位于 [`backend/cron/recovery.py`](../backend/cron/recovery.py)。实际执行为 Job 创建继承 `user_id/project_id` 的临时 Cron Session，再进入 Agent Loop，见 [`backend/cron/executor.py`](../backend/cron/executor.py)。因此外层 Cron lease 防止业务运行重复结算，内层 Agent generation 防止同一临时 Session 中两个 Loop 交错；两者不是同一把锁。

## 11. Skill / MCP scoped lifecycle

### 11.1 User scope transport

`SandboxManager` 从 user id 派生 `u-<20 hex>`，`SandboxClient` 通过 `X-OpenBox-User-Scope` 发送。WUYING Action Server 以 `OPENBOX_REQUIRE_USER_SCOPE=1` 运行，`/catalog`、`/skills*`、`/mcp*` 缺少 scope 时返回 428。

Scoped client 还会先检查 `/alive` 是否声明 `tenant_catalogue_scopes_v1`；旧 Action Server 不支持时 fail closed，不回退到 legacy global `/data/skills` 或 `/data/mcp/config.json`。见 [`backend/sandbox/client.py`](../backend/sandbox/client.py)、[`container/action_server.py`](../container/action_server.py)。

### 11.2 Skill lifecycle

- Backend host 仍扫描内置/项目兼容目录，并只在模型调用 `skill(name)` 时加载正文；目录摘要有预算，超限使用 `skill_search`。见 [`backend/skill/skill.py`](../backend/skill/skill.py)、[`backend/tool/skill_tool.py`](../backend/tool/skill_tool.py)。
- WUYING user-installed package 位于 `/data/skills/u-<hash>/`；创建/上传采用 staging、路径/symlink/secret/体积校验后发布。Skill 文件中的 `allowed-tools` 只是文档字段，不能扩大 Agent tool frontier。
- `skill_manage` 创建/导出 personal package；Backend `UserSkill`/`SkillInstall` 保存 owner、archive hash、published immutable snapshot 和 store-install provenance。见 [`backend/tool/skill_manage.py`](../backend/tool/skill_manage.py)、[`backend/skill/user_library.py`](../backend/skill/user_library.py)。
- Personal snapshot 自动恢复只调用 `create_only=true` 且携带 lifecycle generation 的 archive upload；Action Server 必须声明 `skill_archive_create_only_v1` 与 `skill_restore_fence_v1`，并在跨进程同名发布锁内同时检查持久化 delete fence 和目标存在性。Personal uninstall 先推进同一代际 fence、删除执行面 package，再提交 `UserSkill` tombstone；因此晚到的旧 snapshot restore 会收到结构化 409，既不能覆盖并发创建，也不能在卸载后复活 orphan package。所有同名 create/install/upload/uninstall 共用执行面锁，显式用户更新仍保持 update 语义。
- install/uninstall/create/upload 会失效 Backend catalogue cache；Action Server 的 `/catalog` 给出 `skills_generation` 与整体 generation。
- Tenant mode 删除 legacy `/workspace/skills` 全局 symlink；模型读取 user Skill bundle 时使用精确 scoped base directory。

### 11.3 MCP lifecycle

- 每个 user scope 有独立 `ContainerMcpManager` 和 `/data/mcp/u-<hash>/config.json`；配置原子写入、mode 0600、拒绝 symlink。
- stdio MCP 以 `sandbox` runner 启动，使用 secret-free base env 与显式配置 env；受保护控制面变量不能由 package 覆盖；HOME/cache 指向该 user 的 `.openbox/mcp-home`。
- 每个 enabled server 由唯一 owner task 持有持久 transport/session；connect、tool call、resource read、prompt get、refresh 与 stop 都进入该 owner 的有界队列，因此 transport 的 enter/request/exit 始终发生在同一 asyncio task，stdio 不再每次调用重启进程。
- discovery 会完整分页读取 Tools/Resources/Prompts 到临时 generation，全部成功后才一次性替换公开目录；刷新失败保留 last-known-good，不会暴露半新半旧目录。声明 `listChanged` 的 server 使用通知触发合并刷新；raw HTTP 无可靠通知时使用认证的持久 receiver 或低频轮询降级。
- desired `enabled` 状态先原子落盘；显式 disconnect/remove、慢刷新竞态与 shutdown 都等待 owner 收敛并 dispose，旧 probe 不能在禁用后复活。连续连接失败使用有界指数退避与稳定窗口重置；预算耗尽的 owner 不再作为可调用目录广告，后续 reconnect 成功才重新发布。
- Action Server 启动只恢复 enabled server，慢/坏 server 不阻塞 readiness。HTTP initialize、initialized notification 和持久 receiver 使用同一认证/session header builder。
- `/catalog` 以完整 metadata snapshot 输出 `mcp_generation`；Backend 使用 ETag/last-known-good snapshot，并把 normalization cache 绑定 client identity、boot/generation 和 MCP generation。
- Agent MCP canonical ID、permission subject 和 discovery evidence 绑定 user/project/session/run/agent/sandbox/generation/schema，不能把一次发现证明重放到另一 scope。见 [`backend/tool/mcp_tool.py`](../backend/tool/mcp_tool.py)。

### 11.4 Trusted platform plugins

宿主 Python 扩展是管理员部署面，不是用户项目扩展。推荐目录为
`.openbox/plugins/<name>/plugin.json`，清单固定 `schema_version`、`name`、`version`、
`enabled`、Python `entrypoints` 与有界 `dependencies`。所有启用项先做确定性依赖拓扑排序；
缺失/禁用依赖或依赖环在 import 前拒绝。加载会限制清单/入口数量、拒绝目录逃逸和 symlink、
先在不可见的 `PluginGeneration` 中 import、验证该插件的全部 `ToolInfo` 并完成可选异步
`openbox_setup`/`setup` 或 `openbox_activate`/`activate`，全部成功后才以一次 CAS 指针切换提交。
任一入口、setup 或 tool contract 失败时，新 generation 按逆序 dispose 并清理其 `sys.modules`；
仍完整且无冲突的旧 generation 作为 last-known-good 保留。注册通道会重写 source/plane/canonical/provider
等信任元数据，插件声明不能取得 same-response authority 或并发安全特权。
注册成功也不等于自动暴露：平台插件工具仍须出现在 Agent 定义的显式 `tools` 白名单中，
再经过与内置工具相同的 exposure 规划和 permission 检查，模型才可能调用。

工具执行器持有 generation pin。生产 lifespan 启动独立 `PlatformPluginWatcher`，按
`PLATFORM_PLUGIN_WATCH_INTERVAL_SECONDS`（默认 5 秒）低频 fingerprint；digest 未变化时复用
standing generation、不会重复 import，version、entrypoint、dependencies 或源码变化才执行
`reconcile_platform_plugins()`。声明的 dependency generation 变化会重建 dependent；显式调用
同一 reconcile API 也可立即触发，无需重启。切换点先停止旧 generation 的新调用，再发布新映射，已经 pin
的调用完成后才按依赖与 effect 的逆序执行可选 `openbox_dispose`/`dispose`、setup 返回 disposer
和 `ctx.add_disposer()` 注册项。disable、manifest remove 与应用 shutdown 走同一排空路径；一个
dispose 失败只记录诊断，不阻止其余资源和插件回收。并发 reconcile 由运行时串行，并用 registry
pointer CAS 防止覆盖同时发生的可信内置注册。shutdown 首先停止 watcher，随后才停止 Agent 并
dispose generation；排空超过 30 秒会持续记录 plugin/generation/in-flight 诊断，但不会在执行中
强拆模块。吞掉取消的可信插件因此可能拖延 reload/shutdown，这是可用性故障而不是绕过 pin 的理由。

旧 `.openbox/tools/*.py` 仍作为一个原子 legacy generation 兼容加载，但新部署应使用版本清单。
实现与回归见 [`backend/tool/platform_plugins.py`](../backend/tool/platform_plugins.py)、
[`backend/tool/plugin_lifecycle.py`](../backend/tool/plugin_lifecycle.py)、
[`backend/tool/registry.py`](../backend/tool/registry.py)、
[`backend/tests/unit/test_platform_plugins.py`](../backend/tests/unit/test_platform_plugins.py) 和
[`backend/tests/unit/test_platform_plugin_lifecycle.py`](../backend/tests/unit/test_platform_plugin_lifecycle.py)。

该机制提供可信代码的运行时资源生命周期，不改变 Python 的安全模型：import 本身拥有 Backend
完整权限，contract 只能回收插件主动登记的资源，不能回滚任意 import 外部副作用。当前没有租户
install API、Client approval、Cordis Context/Service 注入或 Service provider epoch 自动重算；
polling watcher 只观察管理员控制的宿主目录，不把写权限开放给 Agent/租户。
租户安装的第三方能力必须继续使用 WUYING scope 中的 Skill/MCP 路径，不能写入宿主插件目录。

管理 API 位于 [`backend/api/metadata.py`](../backend/api/metadata.py)，执行面 Manager 位于 [`container/action_server.py`](../container/action_server.py)。

### 11.5 与 Harness lifecycle 的差距

Skill/MCP 与可信宿主 Plugin 仍不是同一个统一 Service 容器。宿主 Plugin 已补齐 generation、Effect、依赖图/依赖 generation 重建、可逆 unload、失败回滚与自动源码热替换，但没有 Cordis 的 Context 隔离、Service provide/inject 或 Service provider epoch 驱动的通用依赖重算。

因此“scoped lifecycle”在本文中只表示 owner-scoped install/config/connect/refresh/disconnect/uninstall 和 catalogue generation，不表示已实现 Harness Cordis Plugin runtime。任意第三方 Skill `install.sh` 或 MCP package 仍是执行面内的高信任代码，只是已降 UID、限环境和限目录，并不等于供应链审计或网络 egress containment。

## 12. Single-user、共享验收桌面与 SaaS

### 12.1 Backend SaaS 能力

启用 JWT 时，Session、Project、Asset、Cron、Skill Library 等 API/数据库操作都按 `user_id` 校验；Redis 负责跨 Worker event delivery。Agent Driver 和 Cron lease 使用数据库 fencing，能容忍多个 Backend replica，不依赖单进程 status。

Workspace、attachment、snapshot、Skill/MCP catalogue 已分别按 user/project scope 命名，防止两个合法租户因相同 slug/name 误覆盖。默认 `user_id="default"` 的兼容路径仍服务 desktop/single-user 模式，不能被当作 SaaS owner wildcard。

### 12.2 当前 WUYING 边界

当前 `WuyingProvider` 只有一个预配置 desktop，所有 user 都路由到它。目录 scope、API ownership 和 catalogue scope 可以防止正常 API 路径串租户，但共享 `sandbox` Unix UID 无法约束已经获得任意 shell 的恶意用户；Action Server 源码也会在 JWT + shared WUYING 配置下记录高优先级错误。

因此当前支持的部署结论是：

- **单用户/可信验收：** 一台 WUYING 可完成端到端验收；namespace 仍应启用，以验证未来布局并防误碰撞。
- **公开 SaaS：** 安全边界要求一用户一 WUYING desktop（或等价的独立 VM/container/OS identity + egress policy）。
- **尚未实现：** 动态按 user 分配/回收 WUYING desktop 的 provider control plane；当前代码不能把“路径已分 namespace”解释成“一用户一桌面已经完成”。

## 13. 明确未实现的能力

为了避免把局部重构误读为完整 Harness 移植，当前仍未实现：

1. Agent transcript 之外的完整 Session event sourcing：当前 Session 元数据、Todo、Tool reveal catalogue 与文件系统仍不能只靠 `agent_events` 恢复；provider 的 raw transport request/response 也不会因 replay 与安全原因被完整记录。
2. Cordis 式 Plugin Context/Service provide/inject 和 Service provider epoch 通用依赖重算；可信宿主插件的 generation/Effect、manifest 依赖 epoch、版本 activation、polling 热替换与事务 unload/rollback 已实现。
3. Task 子代理在 parent 进程崩溃后的自动 Loop 续跑。Fork seed、Provider/Agent capability composition、durable activation inbox/outbox、continuable follow-up/interrupt/report/list 和 cold resume 已实现；自动续跑仍因无法证明 parent 是否跨过 provider/兄弟 Tool 边界而保持关闭。
4. WUYING 一用户一桌面的自动 provisioning/lifecycle。
5. 共享桌面上的强恶意租户隔离和完整 Network/Egress Policy。

这些限制是部署与故障处理边界，不是隐含的下一阶段承诺。
