# DeepSeek Harness 与 OpenBox 源码级对比分析（重构基线与当前状态）

> 初始分析日期：2026-08-31
> 当前复核日期：2026-08-31
> OpenBox：`/Volumes/fanxiang/workspace2/OpenBox`
> DeepSeek Harness：`/Volumes/fanxiang/workspace2/deepseek-harness`（tag `dsh-v0.1.2-alpha.2`，commit `0a53fb55be`）
> 分析方式：以工作区实际源码为准，不以 README 宣传或设计草案代替实现。

## 0. 阅读约定与重构后的当前状态

本文最初是 OpenBox 重构前的源码基线，后续章节保留了当时发现问题的证据和演进理由。除本节及明确标注“已补齐”的段落外，文中“当前”“未实现”等措辞均应理解为**初始分析快照**，不能再直接代表重构后的工作树。当前内核事实以 [`AGENT_KERNEL_ARCHITECTURE.md`](./AGENT_KERNEL_ARCHITECTURE.md) 及对应源码/测试为准。

本轮没有把 Node/TypeScript Harness 作为外部 Runtime 嵌入 OpenBox，也没有照搬 Cordis 类型体系；采用的是更符合现有 Python/FastAPI 产品架构的方案：把 Harness 已证明有效的不变量内化为 OpenBox 的 Agent Kernel，同时保留 OpenBox 的控制面、产品协议和 WUYING 执行面。

截至当前工作树，初始报告中主要的 Harness 优势已按以下方式落地：

- **Driver 与 Inbox**：数据库时钟 lease、`run_id + generation` exact fence、单 Session 单飞、durable Prompt Inbox、steer/inject/stop/preempt、启动与周期恢复；普通 Prompt 不再用进程内状态覆盖旧 Run。
- **Canonical Event 与 Surface**：`agent_events` 是模型上下文的 append-only authority；SQL Message/Part 是可重建公共 Read Model。模型请求只从 canonical projection 构建，并以 event sequence/digest 和最终 provider-shaped prompt digest 做 dispatch checkpoint。
- **Tail repair、Compaction 与 Fork**：开放 Turn/Step/Tool 在恢复时保守闭合；压缩使用稳定 Event range、digest/CAS、replacement provenance；Fork 只复制完整 closed logical Turn，并保留 model-private replay、replacement 和 model-only exclusion authority。
- **Tool Runtime**：工具调用在统一 scheduler/hook 中完成预检、权限、超时、并发调度、顺序提交和 stale-generation fence；大目录采用 portable exposure、capability search、预算与显式 opt-in。
- **权限与 WUYING 执行面**：平台 Guard 具有不可降级语义；文件路径经过 canonical-target preflight；Action Server 对 Agent 请求执行远端 generation/lease receipt 校验。运行时固定非 root `sandbox` 用户，root 只用于 ECD 部署期的受控组件准备。
- **Plugin、Skill 与 MCP**：Platform Plugin 使用 generation、stage/activate、LKG、依赖拓扑、in-flight drain、逆序 dispose 和 watcher reconcile；Skill 使用 Provider/Scope/Rank/Revision 与有界 archive；WUYING MCP 使用 per-server owner、持久连接、有界队列、分页原子 generation、`list_changed`/轮询回退、重连预算和 desired-state fence。
- **Subagent**：Task 已有 durable descriptor/activation/outbox、spawn/fork/follow-up/interrupt/report/list、cold resume、单调 authority snapshot，以及显式 provider/model/reasoning/persona/tool/output-schema composition contract。
- **Cron 与外部副作用**：Cron 使用多副本 DB claim、集群原子配额、heartbeat、exact settlement 和 delivery outbox；图片链已接 generic effect ledger，视频/TokenSpace 等暂不能可靠自动对账的路径 fail closed 为 `manual_review`，不伪造成功。
- **产品功能与客户端**：保留 FastAPI、Project/Asset/Cron、WebSocket、Browser/Desktop/Terminal、Image/Video、frontend-v2 与 mobile；工作区路径统一展示为项目内相对路径并覆盖 Unicode。

尚未完成的最终验收边界也必须明确：

1. 当前 dev 已切换为 `ecd-4zjxaq5g45dr5qr0i`（`bossip-sh-007`，`cn-shanghai`）。2026-08-31 通过阿里云 ECD 控制面确认其为 `Running`，授权用户为 `bossip-slot8`，到期时间为 2026-10-07；prod 不是本轮验收目标。
2. dev 上运行的 Action Server 已通过同一 `.env.wuying-dev` 和阿里云命令通道核验：远端文件 SHA-256 与当前工作树完全一致，systemd 服务为 active，`/alive` 返回 `run-lease-receipt-v12`，并声明 `run_lease_receipt_v2`、`terminal_project_cwd_v1`、`mcp_supervisor_v1` 等要求能力。18001 隧道、8080 Backend `/ready` 与 frontend-v2 已连通。
3. 浏览器实机已经覆盖项目内相对路径/Unicode、Files/Review、Terminal cwd、Browser、Desktop 画面、Cron 创建/执行/删除、Skill 列表、MCP Tools/Resources/Prompts 与 Agent 动态调用，以及 Stop/Recovery 后继续对话。验收用临时 MCP 配置和脚本已删除。该结果证明当前单机 dev 用户旅程，不替代多副本故障注入或未来多租户验收。
4. 当前只跑单用户/单 WUYING 拓扑；未来“一用户一 WUYING”的路由、恶意双租户攻击验证与独立安全审计仍属于 SaaS 上线门禁。
5. Generic effect ledger 尚未覆盖所有第三方长任务；没有可恢复 provider handle 的同步 API 只能保守标记 unknown/manual-review，不能声称 exactly-once。

因此，本文当前结论不是“直接采用 Harness Runtime”，而是：**以 Harness 作为源码级正确性参考，继续维护 OpenBox 自有、可验证、与现有产品架构一致的 Agent Kernel。**

## 1. 执行摘要

以下三句话记录的是**初始分析快照**，用于解释为何启动本轮重构：

1. **作为 Agent Harness、运行时内核和长期扩展基座，DeepSeek Harness 明显更好。** 它在插件生命周期、Agent Driver、事件溯源、上下文投影、工具执行管线、事务式压缩、MCP 连接监管、Skill Provider 和可续接子 Agent 等方面形成了统一架构。
2. **作为已经覆盖多用户业务链的 Agent 产品，OpenBox 更完整。** 它已经具备 FastAPI 控制面、用户与项目、PostgreSQL、Redis、WebSocket、Cron、文件与资产、浏览器、终端、图片/视频能力、React 工作台，以及 Docker/Kubernetes/Wuying 执行面。
3. **两者当前都不应原样承担不可信多租户生产负载。** DeepSeek Harness 官方明确标注为未安全审计的开发者预览；OpenBox 虽有容器边界，但仍存在容器 root、Kubernetes 安全上下文缺失、Preview Proxy 无鉴权、同 Session 并发竞态、长会话截断等问题。

如果必须只选一个长期技术底座，应选 **DeepSeek Harness**。最合理的工程方向不是丢弃 OpenBox，而是：

```text
OpenBox 产品控制面
Auth / Project / Asset / Cron / WebSocket / React UI
                         ↓ SDK / JSON-RPC
DeepSeek Harness Agent 内核
Event Log / Agent Driver / Tool Pipeline / Plugin / Subagent
                         ↓ Executor Adapter
OpenBox Docker / Kubernetes / Wuying 执行面
```

## 2. 比较范围与重要前提

### 2.1 OpenBox 比较对象

本报告分析的是 OpenBox **当前工作树**，其中包含大量尚未提交的 Tool Exposure、Native Tool Search、Internal Part 等 WIP 修改。因此：

- 报告中的 Tool Runtime、延迟 Schema 物化、原生工具搜索等判断，比干净 HEAD 更接近当前开发方向；
- 报告不是对某个稳定 Release 的认证；
- 工作树中的设计和测试可能继续变化；
- 当前 README、旧设计文档与实际实现存在多处明显偏差，后文单独列出。

### 2.2 DeepSeek Harness 比较对象

DeepSeek Harness 是 DeepSeek 官方 MIT 开源项目，当前仓库位于 `/Volumes/fanxiang/workspace2/deepseek-harness`。官方 README 明确说明它仍处于开发者预览，未来会发生破坏兼容性的变更：[README.zh.md](/Volumes/fanxiang/workspace2/deepseek-harness/README.zh.md:11)。安全说明进一步声明它没有完成安全审计，不得视作安全或生产就绪软件：[SAFETY.zh.md](/Volumes/fanxiang/workspace2/deepseek-harness/SAFETY.zh.md:5)。

因此，本报告中的“更成熟”主要指 **内核架构、生命周期和工程验证体系更成熟**，不等于 DeepSeek Harness 已经可以无需加固地投入生产。

## 3. 总体定位差异

| 项目 | DeepSeek Harness | OpenBox |
|---|---|---|
| 核心定位 | 可组合 Agent Harness/Runtime | 多用户 Agent 产品与执行平台 |
| 主要语言 | TypeScript/Node.js | Python/FastAPI + React/TypeScript |
| 核心抽象 | Cordis Context、Service、Fiber、Event、Effect | FastAPI service、SQL ORM、全局 registry、自研 Agent loop |
| Session 真相源 | Append-only Session Event Log | 可变的 SQL Session/Message/Part 表 |
| 扩展方式 | Profile、Bundle、Cordis Plugin、事件 waterfall | 内置模块、全局工具注册、宿主 Python custom tool |
| 默认执行面 | 宿主本地 subprocess + 文件写入约束 | 每用户 Docker/K8s/Wuying sandbox |
| 产品面 | CLI、Web、Headless、SDK、ACP 等 profile | 完整 Web 工作台、用户/项目/资产/Cron/容器管理 |
| 当前状态 | 官方开发者预览 | 功能丰富、快速迭代的产品化 Beta/WIP |

## 4. DeepSeek Harness 的启动、组合与插件架构

### 4.1 “一切皆插件”不是口号，而是实际内核模型

DeepSeek Harness 的模型适配器、工具注册表、Session 日志、Agent Loop、沙箱、审批、Web Host 都是 Cordis 插件。插件向 Context 提供 Service、监听事件并登记可逆 Effect；不存在需要直接打补丁的特权核心：[architecture.zh.md](/Volumes/fanxiang/workspace2/deepseek-harness/docs/architecture.zh.md:9)。

Cordis `Context` 是带依赖解析能力的代理对象，而不是普通字典：

- Root Context 创建 Fiber、Reflect、Registry、Events、Logger；
- 子 Context 可以 extend、isolate、intercept；
- 未声明的 Service 不能随意读取；
- `provide()` 将 Service 注册为 Fiber 所有的 Effect；
- Service 来源变化会通知依赖它的 Fiber 重新计算。

源码入口：[context.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/cordis/src/context.ts:35)、[reflect.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/cordis/src/reflect.ts:127)。

每个插件统一实例化为 Fiber。Fiber 状态机覆盖 `PENDING → LOADING → ACTIVE/FAILED → UNLOADING → DISPOSED`；函数、类和对象插件最终都进入相同生命周期：[registry.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/cordis/src/registry.ts:91)、[fiber.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/cordis/src/fiber.ts:139)。

`effect()` 的关键语义是：

- setup 立即执行；
- cleanup 与所属 Fiber 绑定；
- 支持同步、异步与嵌套 Effect；
- 插件卸载时按逆序释放；
- Inject 的 Provider epoch 发生变化时，依赖 Fiber 自动 unload/reload；
- 配置更新走统一的 `internal/update` waterfall。

源码证据：[fiber.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/cordis/src/fiber.ts:402)、[fiber.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/cordis/src/fiber.ts:597)。

### 4.2 Profile、Bundle、Patch 与启动顺序

运行中的 Harness 是按层叠加出来的一棵插件树：

```text
profile package.json 中列出的 bundle 顺序
  → 每个 bundle 的 cordis patch
  → profile 自己的 cordis.patch.yml
  → home 级 patch
  → CLI --patch overlay
  → telemetry/launcher overlay
  → 对空 EntryTree 一次性组合
  → Loader import 每一行插件
  → Fiber 激活与依赖审计
```

Bundle 是“分发配置层的包”，Cordis Plugin 是“被 Loader 实例化的模块”，`dsh plugin` 则是“在 Profile 目录中通过 pnpm 安装依赖并更新 manifest 的 CLI”。三者不是同一个概念。

主要证据：[profile.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/boot/app-boot/src/profile.ts:1)、[profile-boot.ts](/Volumes/fanxiang/workspace2/deepseek-harness/apps/cli/src/profile-boot.ts:105)、[app-boot/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/boot/app-boot/src/index.ts:742)、[plugin.ts](/Volumes/fanxiang/workspace2/deepseek-harness/apps/cli/src/plugin.ts:30)。

Loader Entry 更新具有事务倾向：

- disable 时 dispose；
- 只改 config 时调用 `fiber.update()`；
- module 变化时重建；
- 启动失败会恢复旧 config/plugin；
- Group 批量启动新 Entry，失败则清理新项并恢复旧项。

证据：[entry.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/loader/src/config/entry.ts:141)、[group.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/loader/src/config/group.ts:59)。

Include 支持 YAML `!!js`、按 id 的 insert/override、串行 apply、配置刷新和 `.tmp + rename` 原子写入：[include/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/include/src/index.ts:43)。

### 4.3 两种 HMR 必须区分

Harness 有两条不同的热更新链：

1. **配置/patch 热更新**：重新组合 bundle、profile、home、launcher 层，然后事务式更新 EntryTree；
2. **源码模块 HMR**：维护依赖图与 watcher，分析 accepted/declined 边，清理 ESM/CJS cache，重新 import 并替换对应 Fiber。

`live` Profile 才会监听用户 patch；`startup` Profile 启动后冻结。Watch-only fallback 只能刷新配置，不能刷新源码；源码 HMR 还依赖 Node loader internals，回滚属于 best effort。[profile-boot.ts](/Volumes/fanxiang/workspace2/deepseek-harness/apps/cli/src/profile-boot.ts:264)、[hmr/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/vendor/hmr/src/index.ts:199)。

### 4.4 Preset generation

Agent Preset 文件发生变化时，不会原地替换已经活跃的 Agent：

- 新 Agent 使用新的 standing generation；
- 老 Agent 继续使用创建时的 generation；
- 子 Agent `composeFrom(parent)` 精确继承父 Agent 的同一 generation；
- 旧 generation 当前要等整棵树 teardown 才回收，源码中仍有相关 TODO。

证据：[agent-presets/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/preset/agent-presets/src/index.ts:380)、[agent-presets/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/preset/agent-presets/src/index.ts:746)。

## 5. OpenBox 的启动与扩展架构

OpenBox 由 FastAPI lifespan 手工初始化基础设施、Agent、数据库、Sandbox reconcile、Redis Bus、Cron 和恢复任务；关闭时手工中断 Loop、停止 Cron、关闭 Bus，并保留可恢复的 Provider 资源：[main.py](/Volumes/fanxiang/workspace2/OpenBox/backend/main.py:78)。

这套方式对单体产品直观，但不是通用插件容器：

- 工具保存在进程级全局 `_tools` 字典；
- 内置工具由 `register_builtin_tools()` 手工 import；
- `.openbox/tools/*.py` 通过 `exec_module` 直接加载进 Backend 宿主进程；
- 没有 Service 注入、作用域隔离、卸载、依赖重算、失败回滚或 HMR；
- Custom Tool 属于完全信任的宿主 Python 代码。

证据：[registry.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/registry.py:13)、[registry.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/registry.py:38)、[registry.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/registry.py:89)。

因此，在“插件工程”这个维度，OpenBox 与 DeepSeek Harness 不在同一成熟度层级。

## 6. Agent Loop 与工具调度

### 6.1 DeepSeek Harness 的 Loop

默认 Agent Driver 的主路径是：

```text
kick()
  → while turn()
    → append turn/start
    → Inbox claim
    → preStep() 组装 scoped prompt/tools
    → step/start
    → buildRequest()
    → llm/stream
    → assistant events
    → tool scheduler
    → tool/call + tool/result
    → step/end
    → 若工具或 Inbox 仍欠请求则继续
    → turn/end
```

源码：[agent.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/agent-loop/src/agent.ts:219)、[agent.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/agent-loop/src/agent.ts:234)、[agent.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/agent-loop/src/agent.ts:254)。

它的关键优势：

- 一个 Agent Handle 只拥有一个 Driver reservation；
- wake latch 确保新输入不会丢失，也不会并行启动第二个 Driver；
- Inbox 区分 followup、steer、inject 等输入语义；
- `whenIdle()` 等待的是替换活动也完全结束后的真实 idle；
- pre-step、request、stream、tool pipeline 都是 scoped waterfall；
- 工具 Scheduler 只并行执行声明为 concurrency-safe 的调用，同时按模型原始顺序提交结果；
- turn/step/tool 边界都进入持久 Session Event Log。

### 6.2 OpenBox 的真实 Loop

OpenBox 当前真实调用链是：

```text
POST /prompt_async
  → 校验 Session/status
  → 必要时 preempt 旧运行
  → 持久化用户消息与附件
  → asyncio.create_task(run_loop)
  → run_loop 读取 DB history
  → filter_compacted
  → 解析 Agent/权限/工具目录/ToolRuntime
  → 组装 system prompt 与 provider history
  → 创建 assistant + step-start snapshot
  → process_step / stream_llm
  → 持久化 text/reasoning/tool part
  → 工具逐个执行
  → step-finish snapshot/diff/token
  → tool_calls 则进入下一轮
```

入口证据：[sessions.py](/Volumes/fanxiang/workspace2/OpenBox/backend/api/sessions.py:267)、[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:310)、[processor.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/processor.py:395)。

`run_loop()` 同时承担权限加载、Sandbox、模型 fallback、Compaction、工具暴露、Prompt、历史回放、图片、Todo/Reminder、Token、Retry、Snapshot/Diff、Plan、Cron 和清理等职责。产品特性丰富，但核心编排函数高度集中，修改一个能力容易影响多个状态边界。

当前模型返回的完整 tool-call batch 会先做 call-id 冲突和重复校验，这是一个优点：任何歧义在副作用前整批 fail closed。[processor.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/processor.py:709)

校验完成后，顶层工具调用通过 `for` 循环逐个执行：[processor.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/processor.py:796)。只有模型显式调用 `batch` 工具时才会并行子调用：[batch.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/batch.py:17)。而 `batch` 内部直接调用 `tool.execute`，虽然再次校验可见性、`parallel_safe` 和授权，但没有为每个嵌套工具完整经过 ToolHooks 的 running/completed/error/trace 生命周期。

### 6.3 OpenBox 的并发与中断问题

当前并发控制主要依赖数据库中的 Session status：

- `prompt_async` 创建后台任务后立即返回；
- 后台任务进入 `run_loop` 后才注册 abort 并写 `BUSY`；
- 两者之间没有 per-session mutex/single-flight；
- 同一 Session 在窗口期内可能接受两个 Prompt，形成两个交错 Loop。

证据：[sessions.py](/Volumes/fanxiang/workspace2/OpenBox/backend/api/sessions.py:267)、[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:326)。

Abort registry 是进程内字典，新 Run 会覆盖同 Session 的当前槽位：[status.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/status.py:32)。这会导致：

- 重叠 Run 时 trigger 只能命中最新槽；
- 多 Web 副本时，请求落到另一进程无法真正中断原进程中的 Loop；
- `abort_session_turn()` 只等待固定的 settle 时间，不是等待旧 Driver 真正 quiescent；
- 新 Run 可能在旧工具和旧清理尚未完全退出时开始。

OpenBox 还会先把 Session 设置成 `IDLE`，再 fire-and-forget 地扫描 pending/running ToolPart 并标记 interrupted：[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:1267)、[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:1280)。新 Prompt 可以在旧清理执行前启动，因此旧清理有误伤新 Run ToolPart 的风险。

**本维度结论：DeepSeek Harness 的 Agent Driver、Inbox、单飞语义和生命周期明显更可靠。**

## 7. Session、上下文、持久化与恢复

### 7.1 DeepSeek Harness：Append-only Event Log + Surface

Harness 将 Session Event Log 作为唯一事实来源：

- `turn/start`、`step/start`、`user/message`、`assistant/chunk`、`assistant/message`、`tool/call`、`tool/result`、`step/end`、`turn/end` 等都是追加事件；
- Event `seq` 必须连续；
- 事件必须 JSON 可序列化；
- 人类完整 Transcript 使用 append-origin 事件；
- 模型上下文使用可替换的 Surface；
- Compaction 不删除旧事件，而是追加 replacement，遮蔽模型可见区间；
- 同一套纯投影逻辑可以从任意日志前缀重建当时请求看到的消息。

架构说明：[architecture.zh.md](/Volumes/fanxiang/workspace2/deepseek-harness/docs/architecture.zh.md:49)、[surface.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/session/src/surface.ts:1)、[session/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/session/src/index.ts:567)。

这使“用户曾经看到的历史”“模型当前看到的上下文”“压缩替代关系”“审计与重放”能够同时成立，而不需要真的改写或删除旧消息。

### 7.2 DeepSeek Harness 的持久化屏障与崩溃修复

持久化插件可以 write-behind 批量写入，但 Agent Loop 会在关键外部副作用前显式 `flush`：例如下一次模型请求、工具副作用以及 turn 关闭前。这样既保留批处理效率，也提供顺序与错误观察屏障。[session-checkpoint-policy](/Volumes/fanxiang/workspace2/deepseek-harness/packages/session/session-checkpoint-policy/src/index.ts:20)

冷启动恢复会检查持久日志尾部：

- 验证连续 seq；
- 识别未关闭 turn/step/tool；
- 合成 unknown/interrupted closure；
- 使恢复后的 Surface 再次满足 Loop 不变量。

证据：[repair.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/session/src/repair.ts:28)。

### 7.2.1 OpenBox 已补齐的外部副作用持久化屏障

本轮重构增加了通用 durable external-effect ledger：外部请求先在 exact Agent run fence 下
`prepare`，再以独立 DB-clock claim generation 提交 `submitting`，最后把 provider receipt 与
projection 以 exact-CAS 落库。启动和周期恢复只允许 query/reconcile，绝不重放 dispatch body；
没有可查询 handle 的不确定结果进入 `manual_review`。迟到的旧 worker 因 claim generation/token
不匹配不能提交。图像生成已端到端接入；同步图像 provider 无 handle 时仍诚实保留人工复核边界。
实现与故障注入测试见
[`effect_ledger.py`](../backend/agent/effect_ledger.py)、
[`test_effect_ledger.py`](../backend/tests/unit/test_effect_ledger.py) 和
[`MEDIA_EFFECT_SAFETY.md`](MEDIA_EFFECT_SAFETY.md)。

### 7.3 OpenBox：可变关系表

OpenBox 使用规范化 SQL 表保存 Session、Message、Part 和当前 WIP 的 Internal Part：

- 流式 Text/Reasoning 每约 0.5 秒 checkpoint；
- Assistant、pending/running ToolPart 会在副作用前落库；
- 前端可以直接查询关系数据并显示实时卡片；
- 每个短 DB Session 统一 commit/rollback。

这是很实用的产品 CRUD 模型，也是 OpenBox 刷新后能恢复文本和 Tool 卡片的基础。[db/base.py](/Volumes/fanxiang/workspace2/OpenBox/backend/db/base.py:210)

但它不是事件溯源：

- Message 和 Part 可以原地 UPDATE；
- Tool output pruning/compaction 会改变旧 Part 状态；
- regenerate 会硬删除目标以后的历史；
- DB mutation 与 Redis/WebSocket event 不是同一事务；
- 无法精确证明某次 LLM 请求使用了哪个不可变日志前缀。

证据：[session.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/session.py:499)、[session.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/session.py:539)、[session.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/session.py:678)。

### 7.4 长会话最重要的当前缺陷：只读取最早 200 条

`get_messages()` 默认：

```python
offset=0
limit=200
order_by(created_at ASC)
```

源码：[session.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/session.py:603)。

主 Loop、Compaction、Fork、Abort 等调用没有覆盖这个 limit，例如主 Loop 直接调用 `get_messages(session_id)`：[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:383)。因此超过 200 条 Message 后，查询继续返回**最老的 200 条**，最新用户输入可能完全不进入：

- Agent 上下文；
- termination scan；
- Compaction；
- Fork；
- Abort marker 判定。

这不是普通的“上下文窗口裁剪”，而是应用级分页方向错误，应视作 OpenBox 当前最高优先级的正确性问题之一。

### 7.5 OpenBox 的上下文组装

每次模型调用会从关系 Message/Part 临时投影 provider history：

- User text/file；
- Assistant text/reasoning；
- Tool call/result；
- Provider wire tool identity 与 canonical binding；
- Reminder/Todo、图片和 structured output；
- Model-specific system prompt、环境、Memory 和 instruction files。

入口：[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:1343)、[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:1522)。

这里有一个执行面错位：instruction loader 从 Backend 宿主 `Path.cwd()` 和宿主 OS 读取 AGENTS/Instruction 文件，而项目实际位于独立 Docker/Kubernetes/Wuying Sandbox 中。因此 Sandbox 项目里的指令文件通常不在 Backend 宿主路径，Agent 可能无法看到真实项目的 AGENTS.md。[instruction.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/instruction.py:95)、[instruction.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/instruction.py:145)

### 7.6 Compaction 对比

#### DeepSeek Harness

Harness 的 Compaction 以稳定 Event range 为单位：

- 只选择完整、平衡的 turn/step/tool 区域；
- 开始前固定源 range；
- 生成摘要后重新验证稳定性；
- 摘要必须比原区域更小；
- Commit 时追加带来源 provenance 的 Surface replacement；
- 原始事件继续保留，用于完整 Transcript、审计和重放。

证据：[region.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/compaction/compaction-basic/src/region.ts:92)、[region.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/compaction/compaction-basic/src/region.ts:138)、[region.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/compaction/compaction-basic/src/region.ts:340)、[region.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/compaction/compaction-basic/src/region.ts:436)。

#### OpenBox

OpenBox 的 Compaction 也并非简单截断，它有以下优点：

- 根据模型 context limit 减 reserved token 计算阈值；
- Provider overflow 或上一 Step usage 可触发自动压缩；
- 保留 recent tail；
- 历史已经大于单次模型窗口时支持分块摘要；
- 摘要成功后写 summary assistant；
- 自动模式注入 synthetic `Continue` 继续任务；
- UI 可以直接显示 CompactionPart。

证据：[agent/compaction.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/compaction.py:103)、[agent/compaction.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/compaction.py:136)、[agent/compaction.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/compaction.py:281)、[agent/compaction.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/compaction.py:426)。

但它缺少 Harness 的事务和 provenance：

- 先原地标记旧 ToolPart compacted；
- 通过特殊 Message/Part 边界重组 `[boundary, summary, preserved tail, newer]`；
- 没有 range stability transaction；
- 没有“摘要必须更短”的 Commit 条件；
- 旧工具结果可被 UPDATE，而不是保留不可变事实。

读取逻辑：[session/compaction.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/compaction.py:12)。

**结论：OpenBox 的 Compaction 产品体验和超长历史 fallback 很实用；Harness 的正确性、可审计性和事务边界明显更好。**

### 7.7 Fork、Regenerate 与 Revert

Harness Fork 只复制已经完成 turn 的稳定日志前缀，拒绝在开放 turn 中间 Fork，并保留事件语义和 provenance：[session/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/session/src/index.ts:1065)。

OpenBox Fork 是深拷贝 Message/Part：

- 受默认 200 条查询上限影响；
- 没有把新 Session `parent_id` 指向源 Session；
- 不复制完整 parent、summary、token、error、format、structured 和内部 provider transcript 语义；
- 新行可能共享相同 `created_at`，而读取只按 `created_at` 排序；
- 不创建文件系统分支；
- 同一 Project 的 Session 仍共享同一工作目录。

证据：[fork.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/fork.py:14)、[fork.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/fork.py:73)、[project/workspace.py](/Volumes/fanxiang/workspace2/OpenBox/backend/project/workspace.py:1)。

OpenBox Regenerate 是硬删除后续 Transcript，不可审计：[session.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/session.py:678)。文件 Revert 则基于 Project 级 Git tree snapshot，不是 Session branch；恢复会改写整个共享 Project 目录。`unrevert` token 只在进程内字典保存，重启即丢：[snapshot.py](/Volumes/fanxiang/workspace2/OpenBox/backend/snapshot/snapshot.py:104)、[snapshot.py](/Volumes/fanxiang/workspace2/OpenBox/backend/snapshot/snapshot.py:156)、[revert.py](/Volumes/fanxiang/workspace2/OpenBox/backend/session/revert.py:12)。

准确描述应是“对话复制 + Project 文件快照恢复”，而不是耐久的 Git 式 Session 分支。

### 7.8 OpenBox 的恢复、Retry 与 Cache 边界

OpenBox 正常结束时会扫描 pending/running ToolPart 并标记 interrupted；优雅 shutdown 会给活跃 Loop 发 abort，然后把仍处于 busy/compacting 的 Session 标成 error。[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:1280)、[main.py](/Volumes/fanxiang/workspace2/OpenBox/backend/main.py:136)

不足之处：

- 启动时没有完整 Session replay/repair；
- 进程崩溃可能留下悬空 Assistant/ToolPart；
- Agent turn 在 Web 进程内由 `asyncio.create_task` 驱动，不是 durable queue/worker；
- 多副本无法恢复对旧进程 Loop 的控制权。

当前 WIP 的 Retry 还有一个接线问题：

- Processor 只允许“收到任何 Provider Event 之前”的瞬态异常 retry；
- 这是为了避免重放已产生文本或工具卡片的 Partial Stream，设计方向正确；
- 但 Responses/LiteLLM Adapter 把异常转换成 `{type: "error"}` Event；
- Processor 在辨认 Event 类型前已经设置 `provider_event_received=True`；
- 因此实际 429/503 也可能被判定为“已有事件，不可 retry”。

证据：[processor.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/processor.py:458)、[processor.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/processor.py:985)、[llm.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/llm.py:1311)、[llm.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/llm.py:1716)。现有相关单测 mock generator 直接抛异常，没有覆盖实际 Adapter 的 error-event 语义。

Cache 也存在文档与实际路径不一致：

- `apply_caching()` 声称给 System 和尾部 Message 打缓存 marker；
- `run_loop` 实际只传 `llm_messages`，不包含独立 System list；
- OpenAI 分支因此找不到 System；
- Anthropic 分支写入 Pydantic-AI 风格 `provider_options`，当前实际却直接走 LiteLLM；
- Redis 在当前 Agent context 中不是对话缓存，主要用于 Auth、Ticket、Bus 和跨进程回答。

证据：[caching.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/caching.py:22)、[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:1028)。

## 8. Tool Runtime、权限与大目录暴露

### 8.1 DeepSeek Harness Tool Pipeline

Harness Tool Service 是 scoped registry，并通过统一执行管线处理：

```text
ToolDefinition resolve
  → tools/pre-execute waterfall
  → approval/policy decision
  → monotonic guard
  → execute/around
  → post-execute
  → result render/persist/spill
  → finalize
```

策略 Listener 可以 deny 或 ask；Guard 仍可施加最终拒绝，已拒绝的调用不能被后续 Listener 重新放宽。工具参数、审计记录、UI 和真实执行保持同一份不可变含义。[tools README](/Volumes/fanxiang/workspace2/deepseek-harness/packages/core/tools/README.md:83)

Tool Runtime 支持 scoped visible tools、schema assembly、structured output、附件、输出 spill 和 PTC/Native Code Tool 等组合。模型可见工具必须经过注册和审计，而不是任意从全局字典读取。

### 8.2 OpenBox 当前 Tool Exposure WIP 的价值

OpenBox 当前工作树新增的 Tool Exposure 子系统是它相对一般 Agent 框架的一个亮点：

- `ToolRuntime` 将 eligible catalogue、provider plan、provider tools、execution lookup、step executable ids、provider→canonical binding 冻结为一步内不可变对象；
- `CatalogEntry` 记录 source、plane、pack、schema digest、schema chars 和 same-response safety；
- `ExposurePlan` 区分 direct、deferred、discovery；
- 支持 legacy eager、shadow、portable/native、emergency eager 等 rollout；
- 使用 Schema Budget 防止上百个 MCP/Skill 工具一次性塞满 Prompt；
- 支持 OpenAI Responses Native Tool Search 和可移植 capability search；
- Provider wire name 与执行 canonical id 分离，历史回放绑定更加稳定。

证据：[tool_runtime.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/tool_runtime.py:25)、[tool_exposure.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/tool_exposure.py:89)、[tool_exposure.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/tool_exposure.py:117)。

这套设计尤其适合 OpenBox 的“大工具目录 + 多 MCP + 用户 Skill + 视频/浏览器工具”产品场景。当前不足是它仍直接嵌在大型 `run_loop` 中，而且属于未提交 WIP，生命周期和正确性尚未达到 Harness 的通用 Tool Service 水平。

### 8.3 OpenBox Tool Hooks 与 Permission

OpenBox 每个直接工具调用都会经过 ToolHooks：

- 参数/上下文准备；
- doom-loop 检查；
- 权限判断；
- running/completed/error 事件；
- trace；
- 异常转 ToolResult。

定义层通过 Pydantic Schema 验证参数并统一截断输出：[hooks.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/hooks.py:40)、[tool.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/tool.py:111)。

权限默认值需要特别注意。当前 `_get_permission_rules()` 明确采用“Docker Sandbox 是保护边界”的思路：

- `* / *` 默认 allow；
- doom loop ask；
- question/plan 默认 deny，由 Agent 覆盖；
- `.env` 仅对 `read` 工具 ask。

证据：[loop.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/loop.py:2354)。这意味着 `bash cat .env` 不会命中 `read *.env`，而是走默认 Bash allow。

规则采用 last-match-wins：[permission.py](/Volumes/fanxiang/workspace2/OpenBox/backend/permission/permission.py:127)。`ask()` 将用户历史“always allow”规则追加到 config+agent 规则之后，因此用户持久化 allow 有覆盖 Agent deny 的潜在风险：[permission.py](/Volumes/fanxiang/workspace2/OpenBox/backend/permission/permission.py:186)、[hooks.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/hooks.py:192)。Plan Agent 只明确 deny edit-family 工具，仍保留 Bash，所谓只读很大程度依赖提示词：[agent.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/agent.py:192)。

**权限结论：OpenBox 的交互式授权和跨 Worker Redis 回答很产品化，但默认规则与 deny 单调性需要重新设计；Harness 的中央管线和最终 Guard 更可靠。**

## 9. Agent 执行环境与安全边界

### 9.1 DeepSeek Harness 默认执行环境

Base Bundle 默认组合：

```text
subprocess-local
  + sandbox-local
  + sandbox-policy(workspace-write)
  + bash-sandbox / pwsh-sandbox
  + approval(ask)
```

配置证据：[base cordis.patch.yml](/Volumes/fanxiang/workspace2/deepseek-harness/packages/bundle/base/cordis.patch.yml:205)。

Harness 对 Sandbox 的定义非常准确：它是“进程的文件副作用策略”，不是通用容器安全边界：

- `read-only` 主要限制写入；
- `workspace-write` 允许 Workspace 和 Backend 定义的临时目录；
- 宿主进程本来可读的路径通常仍然可读；
- Network 和 Process visibility 明确不在该词汇表内；
- `danger-full-access` 直接绕过 confinement；
- `danger-full-access` 同时把 approval 设置为 never。

源码声明：[sandbox/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/sandbox/sandbox/src/index.ts:23)。安全说明也明确要求不要将其作为不可信工作负载的唯一安全控制，建议在一次性 VM、容器或专用环境中运行：[SAFETY.zh.md](/Volumes/fanxiang/workspace2/deepseek-harness/SAFETY.zh.md:11)。

Approval 并不是“每条命令都弹窗”：普通命令在 standing policy 内直接执行；只有 pre-execute 返回 ask 或 Bash/FS 显式请求一次性 escalation 时才进入审批。PTY、后台 Job 和 Process Tree cleanup 有统一的 owner/lifecycle，但仍共享宿主内核。

因此，DeepSeek Harness 的默认 Sandbox 更适合可信开发者本机或外层已经有 VM/容器的环境，不适合作为 SaaS 租户之间的唯一边界。

### 9.2 OpenBox 的实际隔离粒度

OpenBox 文档声称“每 Session 一个容器”，但实际代码是：

- `_map_key()` 只返回 `user_id`；
- 同一用户所有 Session 共用一个 Sandbox；
- 同一用户的 client、`/workspace` 和 `/data` 也共用；
- Session 的实际工作目录是 Project 目录；
- 同一 Project 的多个 Session 共享同一工作树。

证据：[manager.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/manager.py:13)、[manager.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/manager.py:35)、[manager.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/manager.py:281)、[docker.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/docker.py:262)。

准确边界是：**用户之间有容器/PVC 边界，同一用户的 Project/Session 之间只有目录约定，没有安全边界。**

即便如此，相比 Harness 默认宿主本地 subprocess，OpenBox 的容器化仍然提供更强的潜在宿主隔离和租户 blast-radius 控制。

### 9.3 Docker Sandbox 当前硬化问题

Sandbox 镜像虽然创建了 `sandbox` 用户，但：

- Dockerfile 没有 `USER sandbox`；
- CMD 直接以默认 root 启动 Python action server；
- `/execute` 与 `/execute_stream` 的 shell 子进程继承 root；
- 文件 API 也以 root 直接访问路径；
- 只有交互式 PTY 显式 `setuid(1000)`，但该用户又拥有免密 sudo；
- Docker 创建参数没有 capability drop、`no-new-privileges`、read-only rootfs 或网络限制。

证据：[container/Dockerfile](/Volumes/fanxiang/workspace2/OpenBox/container/Dockerfile:16)、[container/Dockerfile](/Volumes/fanxiang/workspace2/OpenBox/container/Dockerfile:51)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:525)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:754)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:1057)、[docker.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/docker.py:270)。

Action server API Key 是合理的控制面→执行面鉴权，但鉴权后：

- command 可指定任意 cwd；
- read/write/upload/download 没有限制在当前 Project；
- 命令黑名单只能减少误操作，不能抵御 Python、间接 shell 等绕过；
- root Agent 可以访问同用户容器中的全部 `/workspace`、`/data` 和系统目录。

### 9.4 Kubernetes Sandbox 当前硬化问题

动态创建的 `V1PodSpec` 有 PVC、资源 requests/limits、readiness/liveness probe 和可配置 ServiceAccount，这是正面基础：[kubernetes.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/kubernetes.py:151)、[kubernetes.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/kubernetes.py:194)。Owner metadata 校验和确定性资源冲突处理也比较完整。

但真实 Python Manifest 没有：

- Pod/Container `securityContext`；
- `runAsNonRoot` / `runAsUser`；
- `allowPrivilegeEscalation: false`；
- capability drop；
- seccomp；
- read-only rootfs；
- `automountServiceAccountToken: false`；
- NetworkPolicy 或 Sandbox egress 策略。

证据：[kubernetes.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/kubernetes.py:164)。仓库中的 `sandbox-pod-template.yaml` 虽写过 runAs 1000，但动态创建代码并不读取该模板：[sandbox-pod-template.yaml](/Volumes/fanxiang/workspace2/OpenBox/k8s/sandbox-pod-template.yaml:16)。

### 9.5 Control Plane 与 Preview 风险

本地 Compose 中 Backend 挂载 Docker socket，Backend 进程一旦失陷，通常等价于宿主高权限：[docker-compose.yml](/Volumes/fanxiang/workspace2/OpenBox/docker-compose.yml:8)。这不是 Sandbox 容器直接拿到 Docker socket，但要求对 Backend 自身和所有宿主 Custom Tool 采用高信任模型。

Preview 路径存在更直接的问题：

- 已经实现了短期 Preview Token 生成；
- 真正 Preview Proxy 却明确不鉴权、不验证 Token；
- `provider.get_container(container_id)` 未传 `user_id`；
- Provider 在没有 user_id 时跳过 owner 校验；
- Proxy 再使用 Container 的 Action Server API Key 转发请求。

证据：[containers.py](/Volumes/fanxiang/workspace2/OpenBox/backend/api/containers.py:165)、[containers.py](/Volumes/fanxiang/workspace2/OpenBox/backend/api/containers.py:183)、[provider.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/provider.py:130)。

`ContainerInfo` 数据模型还直接包含 `api_key` 字段，API 返回模型需要逐个确认是否有脱敏：[container.py](/Volumes/fanxiang/workspace2/OpenBox/backend/models/container.py:19)。Refresh Cookie 当前 `secure=False`，公网 HTTPS 部署前也必须修复：[auth/routes.py](/Volumes/fanxiang/workspace2/OpenBox/backend/auth/routes.py:120)。

**执行环境结论：OpenBox 的“每用户容器”是更适合多用户产品的方向，但当前实现不能称作已经完成强隔离；Harness 的默认本地 Sandbox 边界更窄，却对自己的限制描述得更准确。**

## 10. Skill 系统

### 10.1 DeepSeek Harness Skill

Harness Skill 不是代码插件，而是“分层发现、摘要发布、按需注入的可信本地 Markdown 指令”。调用链是：

```text
skill-filesystem provider 扫描 roots
  → SkillRegistry 按 global/scope layer 合并
  → 每个 pre-step 只发布 Skill 摘要目录
  → 模型调用 skill(name) 后加载正文
  → 或用户以 /name 显式注入 user-invocable Skill
```

Registry 特征：

- global + scope chain；
- scope 从远到近，最近 layer 直接 shadow 远端；
- Rank 只在同一个 layer 内比较；
- Provider 有 async list/get/invalidate；
- Runtime Skill first-wins；
- Cache key 包含 cwd、scope chain 和 revision；
- Provider 错误隔离，不完整结果不缓存；
- invalidate 增 revision、清 cache 并发 `skills/change`；
- Skill 内容被明确视为 trusted local instructions。

证据：[skill/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/skill/src/index.ts:248)、[skill/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/skill/src/index.ts:328)、[skill/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/skill/src/index.ts:521)。

Filesystem Provider 的层次包括 Project `.dsh`、Project `.agents`、Custom、User `.dsh`、User `.agents` 和 Bundled。只识别 Root 下 `*.md` 或一层目录中的 `SKILL.md`，Frontmatter 必须包含 name、description；非 Bundled 内容通过挂载的 `ctx.fs` 读取。Watcher 和 Cache 都有明确上限。[skill-filesystem/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/skill-filesystem/src/index.ts:36)、[skill-filesystem/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/skill-filesystem/src/index.ts:658)、[skill-filesystem/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/skill-filesystem/src/index.ts:793)。

模型入口只先看摘要，正文只有精确调用后才进入上下文，避免所有 Skill 全文占满 Prompt：[tool-skill/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/tool-skill/src/index.ts:71)、[tool-skill/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/skill/tool-skill/src/index.ts:206)。

限制：Markdown-only、目录较浅、Watcher 有界；Skill 是可信指令注入，不是代码执行沙箱。

### 10.2 OpenBox Skill

OpenBox Skill 有更强的产品功能：

- 扫描多种兼容目录；
- 宿主 Project/User Skill 与 Sandbox 安装 Skill；
- 按需加载正文；
- 限制目录摘要体积；
- 超出目录预算时启用 `skill_search`；
- 用户 Skill library、发布、安装与管理 UI；
- 个人 Skill 创建会校验路径遍历、Secret、文件数和大小；
- Sandbox Skill 声明的 `allowed-tools` 不会自动给模型扩权。

证据：[skill.py](/Volumes/fanxiang/workspace2/OpenBox/backend/skill/skill.py:44)、[skill.py](/Volumes/fanxiang/workspace2/OpenBox/backend/skill/skill.py:151)、[skill_tool.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/skill_tool.py:88)、[skill_tool.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/skill_tool.py:289)。

问题主要在一致性和供应链：

- 宿主与 Sandbox 存在两套 Catalogue/优先级；
- Skill 指令究竟从宿主还是执行面读取，需要更明确的唯一真相源；
- URL 安装会 clone 第三方仓库并执行 `install.sh`；
- 当前 Docker/K8s 路径下这等价于容器 root 代码执行；
- 安装脚本可以读取同一用户的全部 Project、MCP 配置和 `/data` 持久目录。

证据：[skill_manage.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/skill_manage.py:53)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:1387)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:2148)。

**Skill 结论：OpenBox 的 Skill 产品体验和用户库更强；Harness 的 Runtime Provider、Scope、Rank、Cache 和 Invocation Policy 更系统。**

## 11. MCP

### 11.1 DeepSeek Harness MCP

Harness 采用“每个 Server 一个 Cordis Plugin”的模型：

```text
mcp-client plugin apply
  → 预留 server namespace
  → ConnectionSupervisor 建连
  → listTools 全量分页
  → 两阶段 syncTools
  → 原子替换本地 Tool generation
  → ToolDefinition 调 callTool
  → canonical MCP content 投影为文本/可选图片附件
```

支持 stdio 和 Streamable HTTP。Stdio 使用 scrubbed parent environment + 显式 env：[transport.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/mcp/mcp-client/src/transport.ts:15)。

ConnectionSupervisor：

- 每一代创建新的 Client/Transport；
- 支持 `tools/list_changed`；
- 默认指数退避 500ms→30s；
- 有重连次数预算；
- 稳定运行会重置预算；
- 耗尽后注销工具；
- dispose 停止重连、关闭 Client、等待 attempt/sync 并注销工具。

证据：[connection.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/mcp/mcp-client/src/connection.ts:172)、[connection.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/mcp/mcp-client/src/connection.ts:227)、[connection.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/mcp/mcp-client/src/connection.ts:307)。

工具同步先抓完所有分页，确认新 Generation 完整后再整体替换旧 Generation；注册冲突会撤销新注册，避免部分新、部分旧的混合状态：[tools.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/mcp/mcp-client/src/tools.ts:120)。公开名称有长度/字符约束和稳定 Hash，MCP `isError` 转异常；Rich Image 只有在格式、附件存储和当前模型 image input 能力全部满足时才持久化，否则降级为诊断文本。

限制：

- 默认不启用任何 MCP Server；
- 当前只桥接 Tools，不支持 Resources/Prompts；
- `task-required` 工具拒绝执行；
- 未知 output schema 不一定能强制校验；
- Rich Result 主要保留受验证图片。

证据：[mcp-client README](/Volumes/fanxiang/workspace2/deepseek-harness/packages/mcp/mcp-client/README.md:10)、[mcp-client README](/Volumes/fanxiang/workspace2/deepseek-harness/packages/mcp/mcp-client/README.md:184)。

### 11.2 OpenBox MCP

OpenBox 当前 MCP 实际运行在 Sandbox `action_server` 内，而不是 README 所写的 Backend 宿主进程：

- 支持 stdio 和远程 HTTP；
- stdio 子进程使用敏感环境变量 denylist；
- 支持 Tools、Resources 和 Prompts；
- Agent 侧用 Server/Tool Hash 形成 canonical identity；
- 权限检查针对 underlying canonical tool；
- 大 Catalogue 超过阈值时切换 Meta Tool；
- 发现证据绑定 user/project/session/run/agent/sandbox/schema；
- 超大结果只把完整内容保存在 Sandbox，不落 Backend 宿主。

证据：[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:2463)、[mcp_tool.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/mcp_tool.py:176)、[mcp_tool.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/mcp_tool.py:421)、[mcp_tool.py](/Volumes/fanxiang/workspace2/OpenBox/backend/tool/mcp_tool.py:736)。

这使 OpenBox 在“协议宽度、用户隔离和大目录 UX”上优于当前 Harness。

连接生命周期则明显较弱：

- `connect` 主要用于探测并缓存，然后关闭；
- 每次操作重新建 Session；
- stdio Server 每次调用重新启动进程；
- 无法很好支持有内存状态的 MCP Server；
- 不能持续消费 `tools/list_changed`；
- 配置中的 headers/env 明文保存在 `/data/mcp/config.json`，默认允许的 Bash 可以读取。

证据：[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:2711)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:2747)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:2837)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:3009)。

**MCP 结论：OpenBox 胜在 Tools/Resources/Prompts、Sandbox 隔离和大目录体验；Harness 胜在长连接 Supervisor、list_changed、重连、失败预算、原子 Generation 和完整 disposal。**

## 12. 动态 Cordis、自修改与插件安全

DeepSeek Harness 除了持久安装的 Profile Plugin，还提供模型驱动的 Dynamic Cordis：

```text
tool-cordis define
  → 保存不可变 package version，仅做语法检查
  → run/update
  → resolve exact package
  → Host-only 可直接激活
  → 含 Client half 时创建 approval
  → guarded Host Fiber + Client settlement
  → 成功后 commit current pointer
```

每个版本不可变，Stable Plugin 保留 package map、approvals、current/next/latest。一般 inspect 不返回源码，只有 exact package inspect 才能读取对应 source。[registry.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/extensions/cordis-host-runner/src/registry.ts:16)

激活过程有 single-flight 和精确 run/package 校验：

- define 只追加版本，不执行；
- run/update 先 retract 旧 Run，再启动目标；
- 只有 Host/Client 全部成功才 commit current；
- 技术失败只回收本次 owned run；
- Runtime failure 会把纠正信息 steer 回拥有该插件的 Agent；
- 所有 Dynamic Cordis 状态仅在进程内，Harness 重启后丢失。

证据：[cordis-host-runner/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/extensions/cordis-host-runner/src/index.ts:146)、[cordis-host-runner/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/extensions/cordis-host-runner/src/index.ts:768)、[cordis-host-runner/index.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/extensions/cordis-host-runner/src/index.ts:1247)。

安全边界必须正确理解：

- `node:vm` 只是新 Realm 和受控 Helper，不是 containment；
- Host helper closure 是潜在逃逸路径；
- `require`、timer、fetch 被映射到 Cordis Service；
- VM timeout 只约束同步部分，异步代码可以越过 timeout；
- Client half 有显式 Approval；
- Host-only Plugin 没有同一套 UI Approval 门；
- 官方 self-mod preset 直接把它描述为高信任、接近 shell access。

证据：[sandbox.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/extensions/cordis-host-runner/src/sandbox.ts:1)、[sandbox.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/extensions/cordis-host-runner/src/sandbox.ts:243)、[guard.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/extensions/cordis-host-runner/src/guard.ts:490)、[agent.cordis.yml](/Volumes/fanxiang/workspace2/deepseek-harness/packages/preset/agent-presets/presets/cordis/agent.cordis.yml:1)。

结论：Dynamic Cordis 适合受信开发者与协作式 Agent 扩展，不是恶意代码沙箱；它也不同于 `dsh plugin` 的持久 pnpm 安装。

OpenBox 当前不存在对应的通用动态插件体系。宿主 `.openbox/tools/*.py` 比 Dynamic Cordis 更简单，也更直接：import 时就是 Backend 宿主完全权限，且没有版本、审批、Guard、回滚和卸载语义。

## 13. 子 Agent

### 13.1 DeepSeek Harness Subagent Runtime

Harness 子 Agent 是 Provider Registry + Child Agent composition + 两类生命周期：

- `spawn`：空上下文的新 Child；
- `fork`：复制父 Agent 最后一个完整 `turn/end` 之前的稳定前缀，不复制尚未结束的当前 Turn；
- one-shot：精确一个 Turn，结束后返回 Result；
- continuable：持久 Child Session，可 follow-up、interrupt、report、cold resume。

Provider 必须显式声明支持的 capability；请求了不支持的 model/reasoning/persona/tool filter/output schema 等能力会 fail loud。[subagent types](/Volumes/fanxiang/workspace2/deepseek-harness/packages/subagent/subagent/src/types.ts:75)、[subagent runtime](/Volumes/fanxiang/workspace2/deepseek-harness/packages/subagent/subagent/src/index.ts:535)

Child 继承：

- `depth = parent + 1`，默认工具层最大深度 3；
- 父请求的 provider/model/reasoning/maxTokens；
- 父 Agent 精确的 Preset generation；
- 父 Sandbox override；
- Approval Policy 强制为 `never`，不能借 Child 扩大授权；
- 可叠加 delegation prompt、persona 和 tool restriction。

证据：[child-agent.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/subagent/subagent/src/child-agent.ts:39)、[child-agent.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/subagent/subagent/src/child-agent.ts:166)。

Continuable Child：

- 创建 durable descriptor；
- Provider seed 只运行一次；
- Follow-up 对 Child 加锁，Resident 时 enqueue/wake，非 Resident 时 cold resume；
- Cold resume 从 durable descriptor 恢复，不重跑 Provider seed；
- 父 Agent 持有 Child，因此 Child 未 settle 时父不能先 settle；
- Cancel top-down，释放 child-first；
- Settlement 总会通知 exact live parent；
- `report` 只接受直接父子 lineage；
- 依赖 SessionPersistence + SessionQuery。

证据：[continuation.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/subagent/subagent/src/continuation.ts:395)、[continuation.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/subagent/subagent/src/continuation.ts:942)、[continuation.ts](/Volumes/fanxiang/workspace2/deepseek-harness/packages/subagent/subagent/src/continuation.ts:1167)。

### 13.2 OpenBox Task 子 Agent

OpenBox `task` 工具：

- 保留 legacy `spawn` 的 fresh one-shot 默认行为，可选 continuable lifecycle；
- 增加 `fork`，稳定 range 截止最后 closed logical Turn 的末个 Event，digest/CAS 只覆盖该 prefix 与 closed Surface；开放 Turn append 不造成误 drift，开放内容也不进入 Child；
- 支持针对同一 Child Session transcript 的 `follow_up`、`interrupt`、`report` 和 `list`；
- 原子创建 Child Session、exact User trigger/event seed、durable descriptor、activation inbox、bounded outbox 和 parent ToolPart pointer，中途回滚不留 orphan child；
- Foreground 和 periodic recovery 通过 DB exact-CAS owner claim 争抢每个 activation，输家只等待同一 outbox；claim 过期可 takeover，accepted activation 可 cold resume；
- Child 继承父 `project_id`/workdir 和有效 model，不再修改 Sandbox Manager 私有映射；
- Provider/ModelConfig 与 Agent preset 显式声明 composition capability；provider slot 按 `ModelConfig.provider` 绑定并纳入 endpoint/readiness digest，不再按模型名猜测。model、reasoning、persona、tool allowlist、output schema 会在接受前逐项校验并 fail loud；
- descriptor private authority v2 持久化 exact model/provider binding、冻结 preset prompt、persona、tool intersection 与 schema，cold resume 精确恢复并把 prompt/persona 实际注入 child system prompt，follow-up 只能收窄；旧 v1 descriptor fail-safe 兼容；
- structured terminal result 在出站前由 Draft 2020-12 JSON Schema 本地验证；合法 payload 优先写入 result/outbox，无 TextPart 仍可成功，类型或 additionalProperties 错误会明确失败；
- tenant/project/direct-parent lineage、descriptor/activation generation、exact trigger/run 和 interrupt generation 在 reserve/wake 前共同 fencing；
- 只有 exact trigger 的 terminal `finish=stop` 才投影为 `succeeded`；其他终态区分 `interrupted`、`outcome_unknown` 和 `error`，partial text 不伪装成功；
- 结果通过 durable outbox 幂等投影到每轮独立的 exact parent ToolPart，旧 Part 不被后续 follow-up 覆盖；
- 内置 Subagent 默认不能再次调用 task，避免无限递归。

证据：[task.py](../backend/tool/task.py)、[subagent_runtime.py](../backend/agent/subagent_runtime.py)、[subagent_composition.py](../backend/agent/subagent_composition.py)、[subagent_authority.py](../backend/agent/subagent_authority.py)、[fork.py](../backend/session/fork.py)、[subagent.py](../backend/db/models/subagent.py)、[recovery_service.py](../backend/agent/recovery_service.py)。

限制：

- 不自动重启 parent Loop。这是有意的保守边界：父进程崩溃后无法证明是否已跨过 provider/兄弟 Tool 边界，因此只投影精确 outbox 或等待新 activation；
- 已发送的外部 provider/tool 副作用不可撤回，`running`/`finalizing` 崩溃只能报 `outcome_unknown`而不重放。

证据：[subagent_runtime.py](../backend/agent/subagent_runtime.py)、[recovery.py](../backend/agent/recovery.py)。

**子 Agent 结论：OpenBox 已补齐 spawn/fork、fail-loud Provider/Agent composition、单调 scoped authority、durable continuation 与 cold resume；DeepSeek Harness 仍在 parent-held child settlement 与崩溃后自动续跑组合上领先。**

## 14. OpenBox 产品控制面与业务能力

### 14.1 API 与生命周期

OpenBox 已经覆盖典型 Agent SaaS 的大部分产品面：

- Auth、JWT、Refresh、Logto OIDC/PKCE；
- User、Project、Session、Permission、Question；
- Container、Terminal、Files、Desktop、Browser；
- Asset、Memory、Prompt History；
- Cron、Video Production、Video Material；
- WebSocket 实时消息；
- PostgreSQL/SQLite、Redis Bus；
- Docker/Kubernetes/Wuying Provider。

FastAPI 路由装配：[main.py](/Volumes/fanxiang/workspace2/OpenBox/backend/main.py:185)。Lifespan 会初始化 DB、Redis、Sandbox reconcile、Cron 和恢复，并在关闭时中断活跃 Loop、关闭 Bus、保留可恢复的 Provider 资源：[main.py](/Volumes/fanxiang/workspace2/OpenBox/backend/main.py:78)。

这是 OpenBox 相对 Harness 最明显的优势：它不是只有 Agent 内核，而是已经有完整业务控制面。

### 14.2 WebSocket 与 Redis Bus

主 WebSocket 使用一次性 Ticket，按 user 路由实时事件；前端有单飞连接、代际防旧 Socket 覆盖和指数退避。[ws.py](/Volumes/fanxiang/workspace2/OpenBox/backend/api/ws.py:331)、[frontend ws client](/Volumes/fanxiang/workspace2/OpenBox/frontend-v2/src/shared/ws/client.ts:24)

Redis Bus 支持跨 Web Worker Pub/Sub、断线重连和关键路径 `publish_confirmed()`。但普通 Session/UI Event 的常规路径是：

```text
本进程 dispatch
  → fire-and-forget Redis publish
  → 不持久、不重放
```

证据：[bus.py](/Volumes/fanxiang/workspace2/OpenBox/backend/bus/bus.py:65)、[bus.py](/Volumes/fanxiang/workspace2/OpenBox/backend/bus/bus.py:119)。WebSocket 队列有上限，拥塞时会丢非关键事件；重连依靠 DB Snapshot 和前端重新拉取收敛，而不是 Event Replay。[ws.py](/Volumes/fanxiang/workspace2/OpenBox/backend/api/ws.py:183)

### 14.3 数据库与多租户

OpenBox 有较完整的 ORM 和 Alembic Migration，生产使用 PostgreSQL，桌面单用户模式可使用 SQLite。应用层多数查询显式带 `user_id`，Project、Session、Asset、Container 都有 Owner 概念。

但它不是企业组织级多租户：

- 没有 Organization、Workspace Membership、Team、Invitation；
- Project 是单一 user owner；
- RBAC 主要是 admin/user；
- 数据库没有 PostgreSQL RLS；
- Permission 虽有 Project 字段，但运行时主要按 User 加载；
- 隔离依赖每个应用查询都正确带 owner 条件。

因此更准确的定位是“用户级资源隔离”，而不是企业级组织/Workspace 租户模型。

### 14.4 Cron

OpenBox Cron 是真实产品能力，不是 Demo：

- Job/Run 有持久状态、统计、索引和错误次数；
- 条件 UPDATE 原子 claim；
- 并发池、用户并发限制、Timeout、指数退避、自动禁用；
- 每次任务创建临时 Agent Session，保存 Transcript、Token 和结果；
- 可把结果注入原 Session；
- 有 missed-run 恢复和 Transcript retention。

证据：[cron model](/Volumes/fanxiang/workspace2/OpenBox/backend/db/models/cron.py:10)、[timer.py](/Volumes/fanxiang/workspace2/OpenBox/backend/cron/timer.py:304)、[timer.py](/Volumes/fanxiang/workspace2/OpenBox/backend/cron/timer.py:336)、[executor.py](/Volumes/fanxiang/workspace2/OpenBox/backend/cron/executor.py:42)、[recovery.py](/Volumes/fanxiang/workspace2/OpenBox/backend/cron/recovery.py:77)。

多副本边界：Scheduler 嵌在每个 FastAPI 进程；启动恢复会清理 DB 中的 running 状态，滚动升级或多个 Scheduler 可能影响仍在另一个副本运行的任务。当前 K8s 固定单副本与这一实现是一致的。要水平扩展，需要独立 Scheduler、Lease 或 fencing token。

### 14.5 Frontend v2

Frontend v2 已具备相当完整的产品体验：

- React 19、TanStack Query、Zustand、React Router、i18n、xterm；
- 登录、工作区、Chat、设置、Cron、资源中心、Skill 中心；
- Message/Part/Tool/Status/Permission/Question 的统一实时 Store；
- Terminal、Browser、文件、资源、视频等 UI；
- Vitest 和 Playwright 测试结构。

证据：[package.json](/Volumes/fanxiang/workspace2/OpenBox/frontend-v2/package.json:18)、[router.tsx](/Volumes/fanxiang/workspace2/OpenBox/frontend-v2/src/app/router/router.tsx:20)、[useChatEvents.ts](/Volumes/fanxiang/workspace2/OpenBox/frontend-v2/src/features/chat/hooks/useChatEvents.ts:33)。

交付链尚未完全收口：

- Makefile 本地入口已切到 v2；
- `docker-compose.yml` 仍构建旧 `frontend/`；
- GKE 文档仍按旧 Frontend 描述；
- `frontend-v2` 自己有生产 Dockerfile，但没有完整接入 Compose/GKE；
- 工程规范声称 CI 强制校验，仓库却没有对应根 CI Workflow。

证据：[Makefile](/Volumes/fanxiang/workspace2/OpenBox/Makefile:3)、[docker-compose.yml](/Volumes/fanxiang/workspace2/OpenBox/docker-compose.yml:24)、[frontend-v2 Dockerfile](/Volumes/fanxiang/workspace2/OpenBox/frontend-v2/Dockerfile:1)、[gke.md](/Volumes/fanxiang/workspace2/OpenBox/docs/gke.md:50)。

## 15. 工程成熟度、测试与文档一致性

### 15.1 OpenBox 当前实测和仓库状态

审查当前工作树时得到的测试结果：

- Backend：`1161 passed`；
- Frontend Vitest：22 files / 174 tests passed；
- TypeScript：通过；
- `npm run check`：2 个 complexity error、20 个 lint warning；
- Playwright E2E 未执行，因为配置需要正在运行的 Backend 和共享 Dev Account。

说明 OpenBox 的单元测试基础并不差，但测试金字塔偏单元：Integration 测试较少，缺乏覆盖 Gate、仓库 CI、多 Worker Agent Loop、真实 Provider Retry、真实长会话、真实 Sandbox/K8s/E2E 等系统边界验证。现有单测通过并没有覆盖本报告发现的 200 Message 截断、同 Session 竞态、Adapter error-event Retry、旧 Cleanup 误伤新 Run 等问题。

当前仓库开发速度很高、提交集中在较短时间且当前工作树有大量修改/未跟踪文件。这代表快速迭代能力，也意味着高 Churn、关键人集中和较短的生产验证时间。

### 15.2 DeepSeek Harness 工程验证

当前 Clone 的工程面包含大量 Workspace Package、测试文件和 GitHub Workflow，Root scripts 覆盖：

- Static/Type/Constraint；
- Coverage；
- Snapshot/Artifact；
- Node compatibility；
- Windows；
- Real API E2E；
- Stress；
- Release；
- 文档一致性；
- Package invariant。

入口：[ci.yml](/Volumes/fanxiang/workspace2/deepseek-harness/.github/workflows/ci.yml:21)、[e2e.yml](/Volumes/fanxiang/workspace2/deepseek-harness/.github/workflows/e2e.yml:54)、[package.json](/Volumes/fanxiang/workspace2/deepseek-harness/package.json:35)。

它还保留真实故障 Postmortem，包括“178 个单测和 100% 行覆盖仍未发现真实编辑器加载路径崩溃”的案例。这种记录说明其团队意识到覆盖率不能替代真实装配路径验证：[postmortem 0001](/Volumes/fanxiang/workspace2/deepseek-harness/docs/postmortem/0001-acp-default-export-drops-inject.zh.md:13)。

不过，工程 Gate 更完整不改变其官方“开发者预览、破坏兼容、未安全审计”的状态。

### 15.3 OpenBox 文档与源码的关键不一致

1. README 称“Pydantic AI 单轮工具循环”，实际运行时没有创建或执行 `pydantic_ai.Agent`，而是自研 Processor + LiteLLM/Responses。`_to_pydantic_messages()` 只是未接入的兼容 Helper：[README.md](/Volumes/fanxiang/workspace2/OpenBox/README.md:7)、[llm.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/llm.py:1330)、[llm.py](/Volumes/fanxiang/workspace2/OpenBox/backend/agent/llm.py:1376)。
2. README 称“每 Session 独占 Sandbox”，实际是每 User 一个容器、每 Project 一个共享目录：[README.md](/Volumes/fanxiang/workspace2/OpenBox/README.md:20)、[manager.py](/Volumes/fanxiang/workspace2/OpenBox/backend/sandbox/manager.py:35)。
3. README/设计文档称 MCP 运行在宿主，实际 MCP 在 Sandbox action server 内：[README.md](/Volumes/fanxiang/workspace2/OpenBox/README.md:56)、[action_server.py](/Volumes/fanxiang/workspace2/OpenBox/container/action_server.py:2463)。
4. README 称已部署 GKE，GKE 文档末尾仍把真实集群联调列为待执行事项：[README.md](/Volumes/fanxiang/workspace2/OpenBox/README.md:7)、[gke.md](/Volumes/fanxiang/workspace2/OpenBox/docs/gke.md:346)。
5. 前端本地入口、Compose、GKE 文档分别指向 v2 和旧 Frontend，交付事实不唯一。

这些偏差会降低架构审计、故障排查和新成员理解的可信度，应与代码问题同级修复。

## 16. 完整维度对比矩阵

| 维度 | DeepSeek Harness | OpenBox | 结论 |
|---|---|---|---|
| 启动组合 | Profile + Bundle + Layered Patch + Loader audit | FastAPI lifespan 手工装配 | Harness 胜 |
| 插件生命周期 | Cordis Fiber、Effect、Inject/Provide、可逆卸载 | 全局 Registry、手工 import | Harness 明显胜 |
| 配置更新 | EntryTree 事务式更新、配置 HMR | 配置与模块各自手工读取 | Harness 胜 |
| 源码 HMR | 依赖图、Cache 替换、Fiber 重建、best-effort rollback | 无通用源码 HMR | Harness 胜 |
| Agent Driver | 单 Driver、Inbox、Phase、wake latch | 后台 asyncio Task + DB status | Harness 明显胜 |
| 同 Session 串行 | Handle/Driver 保证 | 无 mutex，存在启动窗口竞态 | Harness 明显胜 |
| Turn/Step 语义 | 明确且持久化 | 由 Message/Part 和 Loop 控制流隐式表达 | Harness 胜 |
| Session 真相源 | Append-only Event Log | Mutable SQL row | Harness 胜 |
| 审计/重放 | 可从日志前缀精确投影 | 原地更新和硬删除削弱审计 | Harness 明显胜 |
| 崩溃修复 | Tail repair + synthetic closure | 正常结束清理，启动无完整 repair | Harness 胜 |
| Compaction | Stable range、事务、provenance、必须变小 | Tail 保留、chunk fallback、原地标记 | 正确性 Harness；产品 fallback OpenBox |
| Fork | 稳定完整 Turn 前缀、保留语义 | 复制最多 200 条 SQL Message/Part | Harness 胜 |
| System Prompt | Scoped plugin-owned sections | 产品化 Prompt + Memory + Instruction | Harness 组合性胜；OpenBox 产品能力强 |
| Provider 广度 | 官方 Adapter seam，范围相对收敛 | LiteLLM 100+ + 自写 Responses | OpenBox 广度胜 |
| Provider 一致性 | 统一 LLM vocabulary/stream seam | 两套 Adapter，边界已有偏差 | Harness 胜 |
| 工具 Pipeline | 统一 pre/guard/execute/post/finalize | ToolHooks + define_tool，Batch 有旁路 | Harness 胜 |
| 自动工具并行 | concurrency-safe 自动并行、顺序提交 | 默认串行，显式 batch 并行 | Harness 胜 |
| 大工具目录 | Scoped visible Tool + PTC | 延迟 Schema、Native Search、Meta Tool、Budget | 当前 OpenBox WIP 略胜 |
| 权限单调性 | 最终 Guard 不可被后续放宽 | last-match + 用户 always 可能覆盖 deny | Harness 胜 |
| 默认执行边界 | 宿主 local file-effect sandbox | 每用户容器/远程桌面 | OpenBox 方向胜 |
| 当前隔离硬化 | 明确承认不是 containment | root、无 securityContext/NetworkPolicy | 两者均需外层加固 |
| Skill Runtime | Provider/Scope/Rank/Cache/Invocation Policy | 双 Catalogue + 产品管理 | Runtime Harness；产品 OpenBox |
| Skill 产品 | 文件发现与命令调用 | Library、安装、发布、Skill Center | OpenBox 胜 |
| MCP 协议宽度 | 仅 Tools | Tools/Resources/Prompts | OpenBox 胜 |
| MCP 生命周期 | Supervisor、重连、list_changed、原子代际 | 每操作新 Session，stdio 每次拉起 | Harness 胜 |
| 子 Agent | spawn/fork、one-shot/continuable、cold resume | spawn/fork/follow-up/interrupt/report/list、fail-loud capability composition、durable activation/outbox、cold resume | OpenBox 已对齐 fork/composition；Harness 的 parent-held settlement 仍领先 |
| 动态自修改 | 版本化 Dynamic Cordis + Guard + approval | 无通用体系 | Harness 胜，但属于高信任能力 |
| Web 产品 | 有 Web Profile，但不是完整 SaaS 业务面 | 完整 React 工作台 | OpenBox 明显胜 |
| 用户/项目/资产 | 非主要定位 | 已完整实现 | OpenBox 明显胜 |
| Cron/业务自动化 | Goal/Job 等通用能力 | 持久 Cron Job/Run、UI、恢复 | OpenBox 胜 |
| 多租户产品 | 不是默认目标 | 用户级资源隔离已具雏形 | OpenBox 胜，但非企业组织级 |
| CI/工程 Gate | 大量 Workflow 和 invariant | 单测基础好，但无完整仓库 CI | Harness 胜 |
| 文档一致性 | 体系完整，仍有 Preview 变更 | 多处设计/README 落后真实代码 | Harness 胜 |
| 当前生产就绪 | 官方明确否 | 接近产品，但安全/并发/运维未硬化 | 两者都不能原样上线 |

## 17. 风险清单与修复优先级

以下 P0/P1 是本次源码审查建议的工程优先级，不是项目官方评级。

### 17.1 OpenBox P0：上线或继续扩展前必须修

1. **修复 Message 查询方向和 200 条上限。** Agent 内核必须读取完整逻辑历史或明确的最新稳定窗口，不能默默返回最老 200 条。
2. **为每个 Session 建立持久 single-flight ownership。** API 接受、Loop 启动、Abort、Preempt、Cleanup 都必须围绕同一个可跨 Worker 识别的 Run/Lease；禁止两个 Loop 交错。
3. **把清理并入 Run settle。** 不得先标 `IDLE` 再异步扫描整个 Session；应在旧 Run 完成自有资源释放后原子发布 idle。
4. **Sandbox 非 root 化。** Dockerfile 声明非 root USER；删除免密 sudo；K8s 设置 runAsNonRoot、capability drop、seccomp、no privilege escalation、只读根文件系统和最小可写挂载。
5. **修复 Preview Proxy 鉴权和 Owner 校验。** 强制短期 Token、绑定 user/container/port，并避免把 Action Server API Key 暴露给普通资源模型。
6. **建立 NetworkPolicy/Egress Policy。** 每用户容器不是网络隔离；MCP/Skill/Browser 的外网权限必须显式建模。
7. **修复 Permission 单调性。** Agent deny/平台 Guard 不得被用户历史 always-allow 覆盖；Bash 对敏感文件访问不能绕过 read 规则。
8. **修复 Adapter Retry 语义。** 异常在进入 Provider Event 计数前分类，测试覆盖真实 Responses/LiteLLM Adapter 的 pre-stream 429/503 和 partial-stream failure。
9. **维持 Subagent Project/lineage fence 回归门。** Child 已继承父 Project/workdir；后续改动必须持续防止 default Project 回退或跨 tenant/direct-parent 控制。
10. **明确第三方代码信任边界。** Custom Tool、Skill `install.sh`、MCP env/header 和宿主 Plugin 必须有签名/来源/审批/最小权限策略。

### 17.2 OpenBox P1：内核演进期完成

1. 引入 Append-only Agent Event Log，将 SQL Message/Part 变成 Read Model，而不是唯一真相源。
2. 增加启动时 Tail Repair，修复开放 Turn、Step、Tool 和悬空流式 Part。
3. 将 Compaction 改成 Event range replacement，保存 provenance，并增加 summary-shrinks 条件。
4. 将 Fork 改为完整 Turn 的稳定前缀，保存 lineage；文件分支语义与对话分支语义分开命名。
5. 让 Instruction 从真实 Sandbox/Workspace 读取，而不是 Backend cwd。
6. 修复 Prompt Cache marker 与实际 LiteLLM/Responses payload 的接线。
7. 将 ToolHooks、Batch、MCP、Native Search 统一进一个不可旁路的 Tool Runtime Pipeline。
8. 把 MCP 改为持久 Supervisor，支持 list_changed、重连、失败预算与原子 Catalogue generation。
9. 为 Plugin/Tool/Skill 建立 Scope、Lifecycle、Unload 和失败回滚抽象。
10. 将 Agent Turn 从 Web 进程 Task 迁出，至少使用 durable Worker/Queue 或带 fencing 的 Agent Runtime Service。
11. 将 Cron Scheduler 与 Web API 解耦，增加 Lease/Fencing，支持滚动升级和多副本。
12. 补齐 CI、Coverage Gate、真实 Provider E2E、Multi-worker、Sandbox/K8s、长会话和恢复测试。
13. 统一 Frontend v2 的 Makefile、Compose、GKE、Docker Image 和文档入口。
14. 全面同步 README/架构文档，删除不再成立的 Pydantic AI、每 Session 容器、宿主 MCP 等描述。

### 17.3 DeepSeek Harness 的关键风险

1. **官方开发者预览。** API/Profile/Plugin 契约仍会破坏兼容，不能把当前内部接口视作稳定长期 ABI。
2. **尚未安全审计。** 官方明确禁止将其视为安全或生产就绪。
3. **默认 Sandbox 不是容器。** 它只管理文件副作用，Network、Process visibility 和宿主可读路径不在边界内。
4. **Dynamic Cordis `node:vm` 不是 containment。** 同步 timeout 也不能约束异步代码；Self-mod Agent 等价高信任能力。
5. **MCP 仅桥接 Tools。** 需要 Resources/Prompts 的产品必须自己扩展或保留 OpenBox Bridge。
6. **Preset 旧 Generation 暂不及时回收。** 长生命周期 Host 需要关注内存和资源滞留。
7. **HMR 能力分层。** Startup Profile 不 watch；watch-only 不刷新源码；源码 HMR 依赖 Loader internals，回滚 best effort。
8. **Dynamic Cordis 只在进程内。** 重启丢失，不等于持久安装插件。
9. **第三方 Plugin/Skill 仍是高信任输入。** Cordis 生命周期解决资源管理，不自动解决恶意插件安全。

## 18. 按使用场景选型

| 场景 | 建议 | 原因 |
|---|---|---|
| 构建长期 Agent Runtime | DeepSeek Harness | Session、Driver、Tool、Plugin、Subagent 内核更统一 |
| 本地 CLI/SDK/ACP Agent | DeepSeek Harness | Profile 与 SDK 组合完整 |
| 研究插件化 Agent 架构 | DeepSeek Harness | Cordis 是核心设计，不是附加层 |
| 快速交付现成多用户 Web 工作台 | OpenBox | 产品面和 UI 已存在 |
| 用户项目、文件、资产、Cron、视频工作流 | OpenBox | 已有业务模型和交互 |
| 上百个 Skill/MCP Tool 的目录控制 | OpenBox WIP 值得保留 | 延迟暴露、Budget、Native Search、Meta Tool |
| MCP Resources/Prompts | OpenBox | Harness 当前只桥接 Tools |
| 不可信多租户代码执行 | 两者都不能原样使用 | OpenBox 需容器硬化；Harness 需外层容器/VM/Remote Executor |
| 立即公开生产 SaaS | 暂不建议 | OpenBox 先修 P0；Harness 官方仍是 Preview |
| OpenBox 下一代统一架构 | OpenBox Python Agent Kernel（内化 Harness 不变量）+ WUYING 执行面 | 复用 Harness 已验证思想，同时维持单一事实源与现有产品协议 |

## 19. 已采用的融合架构

### 19.1 保留 OpenBox 的部分

- FastAPI API Gateway；
- User/Auth/Project/Asset 数据模型；
- Redis/WebSocket 产品事件；
- Cron 业务模型与 UI；
- React Frontend v2；
- 文件、Terminal、Browser、Desktop、Image/Video 产品能力；
- WUYING Action Server 执行面；Docker Compose 只保留本地 PostgreSQL、Redis、Azurite 等基础服务；
- 大工具目录的 Schema Budget、Native Search、Meta Tool 经验；
- MCP Resources/Prompts 与用户配置管理。

### 19.2 内化的 DeepSeek Harness 不变量

- generation 化的 Plugin stage/activate/drain/dispose 生命周期；
- Agent Handle、Driver、Inbox 和 Run ownership；
- Append-only Session Event Log；
- Surface projection 与 Compaction replacement；
- 持久化 checkpoint、flush 和 tail repair；
- Scoped SystemPrompt 和 Tool Registry；
- Tool pre/guard/execute/post/finalize Pipeline；
- concurrency-safe Tool scheduler；
- MCP Supervisor 与 atomic tool generation；
- Skill Provider/Scope/Rank/Invocation Policy；
- Preset generation；
- Subagent Provider、spawn/fork、continuable、cold resume；
- 显式、可冻结、可审计的 Agent/Subagent composition contract。

### 19.3 当前接口边界

最终没有引入第二套 Node/TypeScript Runtime。Harness 在本项目中是源码级正确性参考，生产边界保持为 OpenBox Python Agent Kernel 与 WUYING Action Server：

```text
OpenBox Control Plane
  ├─ 创建/查询产品 Session
  ├─ 鉴权、额度、项目和资产
  ├─ 接受 durable Inbox item
  ├─ 投递/订阅 canonical Agent Event
  └─ 将 Event 投影成 UI Read Model

OpenBox Python Agent Kernel
  ├─ 独占 Agent Run 和 Inbox
  ├─ 保存 Canonical Agent Event Log
  ├─ 组装 Prompt/Tool/Skill/Preset
  ├─ 调用 LLM
  ├─ 通过 fenced SandboxClient 调 WUYING
  └─ 输出可重放事件

WUYING Action Server
  ├─ 当前可信单用户桌面；未来按用户路由独立桌面
  ├─ Bash/FS/Browser/MCP/Skill 执行
  ├─ 非 root + 最小权限
  ├─ generation/lease receipt 与幂等操作
  └─ Network/Egress/credential policy
```

## 20. 实施状态与后续阶段

### 阶段 0：正确性止血（已完成）

- 修复 200 Message 截断；
- 增加 Session mutex/Run id；
- 修复 idle-before-cleanup；
- 修复 Retry Event 接线；
- 修复 Child Project；
- 关闭 Preview 无鉴权；
- Sandbox 非 root、Preview 独立 origin、WUYING 组件受控部署；
- 更新 README，避免继续基于错误假设开发。

### 阶段 1：定义 Python Agent Kernel 边界（已完成）

- 把现有 `run_loop` 外围产品逻辑与内核逻辑分离；
- 固定 LLM Adapter、Session Store、Tool Runtime、Executor、Event Sink 接口；
- 所有 Side Effect 必须携带 run/session ownership；
- Harness 只作为对照实现，不引入第二事实源或跨语言 Runtime。

### 阶段 2：Canonical Event 与 Surface（已完成主链）

- Agent Event 成为模型上下文 authority；
- Message/Part 保留为可重建公共 Read Model；
- provider request 使用稳定 prefix、digest 与 dispatch checkpoint；
- model-only replacement/exclusion、Fork lineage 与严格 parity 已接入。

### 阶段 3：Driver、Inbox 与恢复（已完成主链）

- 数据库 Driver 独占 Session Run，所有权为 `run_id + generation`；
- Prompt API accept-first 写 durable Inbox，支持 next-turn/next-step/steer/inject；
- Stop/Preempt/Recovery 使用 exact CAS，不再依赖进程内信号作为权威；
- UI 状态携带 generation，拒绝旧代终态覆盖新代 busy。

### 阶段 4：Tool、Plugin、Skill、MCP（已完成主链）

- 保留 OpenBox Canonical ID、Budget 和 Native Search；
- 使用 OpenBox scoped ToolDefinition、portable exposure 与 capability search；
- Sandbox 调用统一携带 Driver/transport fence；
- MCP Resources/Prompts 使用 per-server Supervisor 与原子 catalogue generation；
- Skill 产品库使用 Provider/Scope/Rank/Revision，Plugin 使用 generation lifecycle。

### 阶段 5：Compaction、Fork、Subagent（已完成主链）

- Compaction 使用 Event range replacement；
- Fork 只允许完整 Turn prefix；
- 文件 Snapshot 与 Transcript lineage 分离；
- Task 子 Agent 已采用 Provider/Agent capability contract 与 exact durable composition snapshot；
- 长期任务已支持 continuable/cold resume，后续仅在能证明 parent provider/兄弟 Tool 边界时再考虑自动续跑。

### 阶段 6：产品 Read Model 与副作用收口（大部分完成）

- OpenBox SQL Message/Part 变成可重建 Read Model；
- Redis/WebSocket 只负责低延迟 Delivery，不再承担事实存储；
- 重连从 Event checkpoint 恢复；
- Cron、图片使用持久 claim/outbox/effect ledger；缺少 provider receipt 的媒体路径 fail closed；
- 仍需把 generic effect ledger 扩展到所有可对账第三方长任务。

### 阶段 7：部署与真实验收（进行中）

- 已只使用新 dev `ecd-4zjxaq5g45dr5qr0i` / `.env.wuying-dev`；阿里云控制面确认实例 Running，prod 未被触碰；
- 已部署源码 v12 Action Server，并核对远端 SHA、systemd active、版本与 capability；
- 已打通 18001 隧道、8080 dev Backend 和 frontend-v2；
- 已实机覆盖项目内相对路径/Unicode、Terminal、Files/Review、Browser、Desktop、Stop/Recovery、Cron、Skill 与 MCP Tools/Resources/Prompts/Agent 动态调用；
- Action Server 的 Python 3.10 MCP timeout 兼容、无登录 profile 的 headless Bash、SSE 取消杀进程组和 Bash Stop 信号均已在新 dev 实机验证；
- 当前稳定树门禁：Backend `2036 passed, 1 skipped`，frontend-v2 `217 passed` 且 production build 通过，mobile `33 passed` 且 analyze 通过，Alembic `heads/current/check` 一致；
- 仍需按具体发布范围决定是否执行会产生第三方费用的真实图片/视频供应商 canary；
- 未来多用户上线前完成一用户一 WUYING 路由、双租户攻击测试和独立安全审计。

## 21. 最终判断

如果问题是“哪个 **Harness 工程** 更好”，答案没有悬念：**DeepSeek Harness 更好，而且差距主要来自内核模型，而不是代码量。**

DeepSeek Harness 的优势集中在长期最难补的部分：

- 生命周期；
- 作用域；
- 事件真相源；
- Driver 串行化；
- 可重放上下文；
- 工具策略管线；
- 插件组合；
- 子 Agent 持久协议；
- 失败回滚和恢复。

OpenBox 的优势则集中在非常值得保留的产品资产：

- 多用户控制面；
- 项目和工作区；
- 容器/远程执行面；
- WebSocket 与完整 UI；
- Cron、资产、图片/视频；
- Skill/MCP 产品管理；
- 大工具目录和 Provider 广度。

所以，最佳决策不是“用 Harness 删除 OpenBox”，也不是继续把所有内核功能无边界地堆进一个大 Loop，而是：

> **将 DeepSeek Harness 的 Driver、Event/Surface、Pipeline、生命周期与恢复不变量内化为 OpenBox 自有的 Python Agent Kernel；OpenBox 继续作为产品控制面，并通过 WUYING Action Server 提供隔离执行面。**

在安全结论上必须保持克制：

- DeepSeek Harness 官方明确不是生产安全软件；
- OpenBox 当前单用户 WUYING 路径已完成主要授权、并发与执行面加固，但未来恶意多租户拓扑仍需“一用户一 WUYING”、独立安全审计和真实故障注入；
- 任何公开多租户上线都应先完成本报告 P0，并进行独立安全审计、威胁建模和真实集群验证。

---

本文保留初始只读分析作为重构基线；第 0 节和明确“已补齐”的段落由后续实现证据更新。本文档未执行 Git 提交。
