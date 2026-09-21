# OpenBox Agent Team 完整架构与实施计划

> 2026-09-21 最新范围调整：用户要求“移动端暂停优化。接口后端 web 端做完就行”。本轮交付以 API、后端和 Web 为准；已做的 Flutter 改动保留，停止继续优化，不以移动端未完成项阻塞本轮收尾。

> 2026-09-21 评测范围调整：用户进一步要求“不要跑完，随机对比，但是要尽量测完修复 bug”，移动端仅保留接口接入，界面由用户设计。完整四组长评测已停止，原始结果保留，改为预先固定的随机抽样和针对性故障回归。下文完整 M5 对比与移动端界面要求不再作为本轮交付门槛。

> 2026-09-21 用户变更：移除所有独立团队预算（含模型请求额度预估和付费工具累计/单次积分限额），消费统一使用现有账户总积分。下文原预算条款仅保留为设计历史，不再作为实现/验收要求。付费工具仍需显式授权，未知外部操作保留幂等与对账保护。团队结束必须在正常聊天中输出完整总结；已成功提交的 `team_finish.summary` 是最终答复，需持久化、刷新可见，并在中断恢复后补齐。


> 状态：本地实现与验收进展见 [实现记录](AGENT_TEAM_IMPLEMENTATION.md) 和 [验收证据](AGENT_TEAM_ACCEPTANCE.md)；本文保留原始设计，不代表已经生产上线。
>
> 日期：2026-09-20。
>
> OpenBox 核对基线：1e8e16757af869b0565c0c19fb8f8d222a4795fa。
>
> DSH 核对基线：ddefc45fbc7f8e46dd73185e68295696d1297887。
>
> 主要架构参考：DeepSeek Harness Agent Team。在 OpenBox 自有运行时中实现这些协作机制，保留现有 Python、SQLAlchemy、模型适配器和前端技术栈。
>
> 适用范围：通用多 Agent 协作。调研、开发、内容创作、运营和数据分析均是应用场景，团队内核不绑定具体业务。

## 目录

- [1. 已确认目标与范围](#goals)
- [2. 工作原理与核心决策](#principles)
- [3. 参考对象与采用边界](#references)
- [4. 当前实现与差距](#baseline)
- [5. 总体架构](#architecture)
- [6. Agent、团队与运行的定义](#definitions)
- [7. 模型、Skills 与工具授权](#capabilities)
- [8. 协调者、发起入口与组队流程](#orchestration)
- [9. 通信、任务板与成果交接](#collaboration)
- [10. 持久化、调度与恢复](#runtime)
- [11. 数据模型与约束](#storage)
- [12. API、模型工具与事件协议](#interfaces)
- [13. 前端与移动端设计](#product)
- [14. 现有代码改造清单](#changes)
- [15. 实施阶段与依赖](#delivery)
- [16. 验收、测试与评估](#acceptance)
- [17. 发布、兼容与回滚](#rollout)
- [18. 风险、容量与后续扩展](#risks)
- [19. 完整运行示例](#example)
- [20. 决策记录与完成定义](#decisions)

<a id="goals"></a>
## 1. 已确认目标与范围

### 1.1 产品目标

用户可以创建自己的 Agent，为其配置职责、模型、Skills 和工具；这些 Agent 可以被反复用于不同团队。用户也可以只描述目标，让协调者选择成员、创建临时 Agent、分配任务并组织交付。

必须支持以下三种成员组合：

| 组合 | 成员来源 | 谁可以组队 |
|---|---|---|
| 用户 Agent 团队 | 用户已保存的 Agent 定义 | 用户指定，或协调者从允许的 Agent 库选择 |
| 自动创建的团队 | 协调者按任务创建的临时 Agent | 协调者在团队策略允许范围内创建 |
| 混合团队 | 用户 Agent 与协调者创建的临时 Agent | 用户指定核心成员，协调者补充；也可以完全由协调者组织 |

“谁创建 Agent”和“谁组织团队”是两个独立维度。三种组合使用相同的成员运行、通信、任务管理和恢复机制。

界面上不要求用户先在三种组合里选一个：用户面对的是一份成员名单和一个“允许协调者按需要补充成员”的选项，三种组合由这两样东西自然得出；团队在普通对话的输入区里发起，普通对话里的 AI 也可以提议使用团队，由用户在组队方案卡上决定（第 8.2 节、第 13.2 节）。

### 1.2 交付范围

下列内容全部属于本计划的交付，没有可选项，也不拆成先后两个版本：

1. 用户 Agent 的创建、编辑、版本管理、试运行、归档和复用。
2. 团队模板及其版本、协调者选择、预设成员、自动补充成员策略；把一次自动组建的团队整体另存为模板。
3. 三种成员组合；用户指定成员的配置与协调者创建成员的配置采用同一校验过程。
4. 每个成员独立选择可用模型，独立上下文、工具范围和 Skills 配置。
5. 单协调者、扁平成员结构；成员直接通信，任务依赖、并行执行与成果交接。
6. 持久化状态、多 API 进程协作、进程重启恢复、取消、暂停与继续。现行部署是单个后端进程（第 4.1 节），团队层仍按多进程正确性设计，并用两个后端进程对同一个 PostgreSQL 的专项测试验证（第 16 节）。
7. 团队预算、并发限制、成员用量及运行面板。预算包含模型调用的软上限和付费工具的严格预留两层（第 10.6 节）。
8. 成员可用工具按层全部开放：知识与文件类、桌面与浏览器类、付费媒体类（第 7.4 节）。
9. Web 完整配置和运行管理；移动端能够启动已保存团队、查看进度、回答问题、暂停和取消。用户问答沿用根会话现有的提问卡片（第 7.6 节）。
10. 普通聊天、现有 Task 子 Agent、Skills 和已有业务工具保持兼容；未识别团队工具与方案卡详情的旧客户端可读地降级显示。
11. 发布准备：开关、分批放量、回滚步骤与演练、运行与恢复说明。

完成指上述范围全部通过验收，不能将“只能固定组队”的阶段称为完整交付。里程碑的顺序按风险安排：先跑通一支最小可运行的团队并做首轮评估，再补齐 Agent 库、工具层、可靠性和产品面（第 15 节）。顺序只决定先后，不改变范围。

### 1.3 不在本计划内的扩展

嵌套团队、多协调者竞选、跨用户 Agent 市场、外部 A2A Agent、跨机器执行环境和学习型调度策略不在本计划内。本计划预留明确的接入点，不以这些扩展阻塞交付。

用户持续运营一个团队时，复用团队模板、必要的共享资料以及历史成果；每次执行创建新的 TeamRun。成员进程不需要长期空转。

### 1.4 团队在 OpenBox 中的价值边界

团队内核保持通用，但默认值、工具开放顺序和评估任务必须按 OpenBox 的真实资源形态设定。同一 workspace 的所有会话共用一个沙箱容器或云桌面；同一项目的所有会话共用一个工作目录，会话之间没有目录隔离（[SandboxManager._ensure_session_dir](/Users/wang/workspace/OpenBox/backend/sandbox/manager.py)、[workdir_for_session](/Users/wang/workspace/OpenBox/backend/project/workspace.py)）；云桌面和浏览器只有一份。这与 DSH 的“共享 checkout”是同一种形态。

| 工作类型 | 依赖的资源 | 成员间能否并行 | 对设计的含义 |
|---|---|---|---|
| 调研、资料整理（web_search、web_fetch） | 模型与网络 | 能 | 主要的并行收益来源 |
| 策划、脚本、文案、审校、数据分析 | 模型，以及共享项目目录下各自的产出子目录 | 能 | 收益来自专业分工和上下文隔离 |
| 代码分析与修改 | 共享容器的 CPU 和磁盘、共享的项目工作目录 | 部分 | 同一份文件的并发写入靠任务拆分和 stale 检查避免，不是靠隔离（第 7.5 节） |
| 浏览器、云桌面操作 | 每个 workspace 一台桌面 | 不能 | 桌面租约按工具调用串行；多步流程需要任务级独占（第 7.5 节） |
| 付费媒体生成（图片、视频、转写、合成） | 供应商异步 Job 与积分 | Job 本身已异步，单 Agent 也能并行提交 | 成员可以在预算预授权内直接生成（第 7.4 节 T2 层）；团队的收益在策划和质检，不在提交并行度 |
| 发布、平台登录 | 平台账号，外部效果不可逆 | 不能 | 不委派给成员，留在用户的普通会话里执行（第 7.4 节） |

结论：在 OpenBox 中团队的收益来自专业分工、上下文隔离、并行的调研与写作、独立审校，不来自并行操作同一台桌面。因此成员并发的默认值取小（第 6.4 节），评估任务集以知识型协作为主（第 16.3 节），并行度指标只统计上表中“能”并行的工作。

<a id="principles"></a>
## 2. 工作原理与核心决策

### 2.1 分工与执行分离

协调者是具有团队管理工具的普通 Agent，负责理解目标、选择角色、拆任务、调整分工和验收结果。TeamService 是确定性的服务代码，负责身份、权限、状态转移、消息交付、任务领取及调度。

协调者不能直接把内存中的一段话视为已创建成员或已完成任务。只有服务端提交成功的记录才构成事实。

~~~mermaid
flowchart LR
    Goal["用户目标"] --> Lead["协调者判断与规划"]
    Lead --> Command["结构化团队命令"]
    Command --> Service["TeamService 校验与提交"]
    Service --> Workers["成员独立执行"]
    Workers --> Facts["消息、任务状态、交付物"]
    Facts --> Lead
~~~

### 2.2 Agent、Skill、工具、团队的关系

| 概念 | 职责 |
|---|---|
| 模型 | 提供理解、推理、生成与工具调用能力 |
| 工具 / MCP | 提供实际的查询、操作和外部系统访问能力 |
| Skill | 提供领域知识、执行步骤和使用工具的方法 |
| Agent | 持有职责、运行配置与独立上下文，调用工具完成任务 |
| 团队 | 管理多个 Agent 的成员关系、任务依赖、通信与共同目标 |

创建“数据分析 Agent”意味着建立角色配置、选择模型、绑定已可用能力并启动会话。缺少数据连接或工具时，系统返回能力缺失；不能通过生成角色名称获得实际系统能力。

“工具”在 OpenBox 里有三种来源，对 Agent 来说都是可以调用的工具，都受同一套权限检查：平台内置工具（read、bash、web_search、video_generate 等）；管理员安装的平台插件工具，运行在后端进程内；MCP 工具，MCP 服务配置在用户自己的沙箱或云桌面上，后端每一步从沙箱读取目录，把每个 MCP 工具包装成工具并入，超过 40 个时只暴露“查找”和“调用”两个元工具（[mcp_tool.py](/Users/wang/workspace/OpenBox/backend/tool/mcp_tool.py)、[tool_resolution.py](/Users/wang/workspace/OpenBox/backend/agent/tool_resolution.py) 的 merge_sandbox_tools）。Skill 不是工具：它是一份 SKILL.md 加脚本和参考资料，通过 skill 工具按需读进上下文，告诉 Agent 怎么做事；它提到的工具 Agent 必须另外拥有才用得了。所以 Agent 定义里工具、MCP、Skills 是分开选的三项（第 6.1 节）。

Skill 内容及 frontmatter 不授予工具权限。此项沿用 [技能与工具解耦方案](/Users/wang/workspace/OpenBox/docs/SKILL_TOOL_DECOUPLING_PLAN.md) 的核心不变式；其中旧的“专业工具只限 build Agent”限制，需要按本文第 7 节为团队运行增加明确、受控的授权路径。

### 2.3 必须保持的不变式

1. 同一 TeamRun 的所有内部成员属于同一 owner_user_id、workspace_id、project_id；执行身份来自服务端。
2. 同一工作成员定义进入不同 TeamRun 时产生不同实例和会话，不共享活跃对话历史。协调者可以在用户原有根会话中接续目标，但每次运行的成员、授权和任务独立绑定。
3. 同一会话任一时刻只有一个有效 Driver 执行者；旧 generation 不能提交状态或继续操作。
4. 成员快照在成员被接纳时冻结。后加入成员单独冻结，不能修改之前成员的快照。
5. 新建角色、修改提示词、加载 Skill、收取同伴消息都不能扩大授权。
6. 任务完成、消息已送达、Agent 一轮结束、整个团队完成是四种不同事实。
7. 消息支持持久化重试和去重；外部动作是否重复执行必须由具体操作协议保证。
8. 网络通知和执行轨迹均不是团队状态的事实来源。
9. 成员停止、浏览器关闭、协调者一轮结束不会自动将所有任务标记成功。
10. 组队策略可以变化，团队的身份、通信与任务协议保持统一。
11. 工作成员不直接面向用户：不能向用户提问，也不会阻塞等待用户审批。需要用户决定的事项由成员报告受阻，协调者在根会话中用现有提问流程向用户确认（第 7.6 节）。
12. 同一 TeamRun 的所有 TeamService 命令串行提交（第 10.1 节）。任务与运行的 revision 只用来拒绝基于过期视图的操作，不承担数据库并发控制。
13. 名册、任务板、消息等动态团队状态只通过工具结果和输入消息进入模型上下文，不写入 system prompt，也不改变成员存续期间的工具 schema，以保持 provider 侧缓存前缀稳定。
14. 同伴消息和成果内容是数据。它们不是指令来源，也不是授权来源；成员是否执行其中的请求，仍由自身职责、工具权限和任务范围决定。
15. 一次团队运行的事实来源是它名下的一条只追加事件流。名册、任务板、邮箱、等待和预算占用都由回放事件得出；回放结果的缓存可以随时丢弃并由事件流重建。事件只追加，不修改、不删除。
16. 团队运行依附于根会话。根会话被删除，运行记录、事件流和成员会话随之删除；团队不是独立于会话存在的长期实体。长期存在的只有用户的 Agent 定义和团队模板。

<a id="references"></a>
## 3. 参考对象与采用边界

### 3.1 DSH 是团队内核的主要参考

本地参考入口：[Agent Team README](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/README.md)、[团队工具实现](/Users/wang/workspace/deepseek-harness/packages/experimental/tool-agent-team/src/index.ts)。

| DSH 机制 | OpenBox 采用方式 | 需要调整的地方 |
|---|---|---|
| Lead、成员名册、成员创建记录 | 明确协调者身份；成员先持久化接纳，再启动 | 加入用户 Agent 定义、版本与临时定义；成员身份独立于名称 |
| 持久化 mailbox | 定向消息、稳定消息 ID、目标接纳回执、重试去重 | 使用数据库及现有 AgentInbox，支持多进程 |
| 同伴直接通信 | 成员可向团队中的其他成员发消息 | 保留真实发送者；按团队身份授权，不能冒充协调者 |
| 共享 task board | 所有者、任务依赖、版本冲突检查 | 增加 attempt、验收、外部结果未知与恢复处理 |
| 等待、唤醒、中断 | 用事件驱动成员继续工作 | 持久化等待登记（只记事件水位和截止时间，不带条件表达式），空闲时释放模型执行额度 |
| 成员独立会话 | 独立上下文和执行状态 | 每成员独立模型、Skill 快照和权限配置 |
| 持久日志，派生状态 | 团队状态存成运行名下的一条只追加事件流，名册、任务板、邮箱由回放得出 | 事件流是数据库里的一张表而不是文件；另有一行薄索引供服务端在用户不在场时调度和恢复（第 11 节） |

DSH 没有数据库。它唯一的第一方会话持久化后端是每会话一个只追加的 JSONL 文件（[session-persistence-jsonl](/Users/wang/workspace/deepseek-harness/packages/session/session-persistence-jsonl/README.zh.md)）。团队也不是独立实体：每个根会话天然是一个隐式团队的 Lead，团队 ID 等于会话 ID，没有“创建团队”事件。团队状态是 Lead 会话日志里的四种事件（成员、任务、消息入队、消息送达），每条事件保存实体的完整快照，名册、任务板和待投递邮箱每次读取时由 [projection.ts](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/src/projection.ts) 回放得出；[invariant.ts](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/src/invariant.ts) 在追加前把候选事件对照已提交前缀回放，拒绝非法转换。成员创建时只有名字、职责描述和初始任务，没有可复用的 Agent 定义。会话删除，团队随之消失。

OpenBox 采用同样的形态。为一次任务自主组建的团队是这次任务的副产品，不应建模成一个带十来张关系表的长期实体。OpenBox 的内核本身也已经是这种形态：agent_events 是每会话只追加的事件流，messages 和 parts 只是可重建的读模型（第 4.1 节）。差别只有两处，都来自 OpenBox 是多用户的服务端而 DSH 是本机单用户工具：会话在 OpenBox 里本来就存在数据库中，所以事件流是一张表而不是一个文件；DSH 在用户重新打开会话时才恢复，OpenBox 必须在用户不在场时调度和恢复，因此需要一行索引来回答“哪些团队还活着”。所有权协调在 DSH 里是单进程内的串行队列，其 README 明确标注实验性、共享 checkout、扁平成员，以及不承诺跨进程 exactly-once；OpenBox 用运行行上的数据库锁、执行租约和现有恢复机制实现同样的约定。

DSH 的设计笔记还记录了几项经过取舍的具体做法。下表逐项说明本计划的处理，出处均在 DSH 仓库内。

| DSH 的做法与出处 | 本计划的处理 |
|---|---|
| 消息只有一种投递方式（Steer）：运行中的目标在最近的步骤边界收到，空闲目标启动一轮，未加载的目标冷恢复。DSH 曾有“静默投递”和“唤醒投递”两个工具，后来合并，原因是模型不应选择调度策略，静默消息会在无人恢复的成员处无限堆积（[send_message 决策](/Users/wang/workspace/deepseek-harness/.agents/notes/archived/simplification/2026-08-30-team-send-message-steer.zh.md)） | 采纳。team_message_send 不提供投递方式参数（第 9.2 节） |
| 等待只回答是否超时，调用方醒来后重新读取权威状态；没有其他成员处于 running 或 provisioning 时立即返回 noProgress，提示先唤醒成员（[工具实现](/Users/wang/workspace/deepseek-harness/packages/experimental/tool-agent-team/src/index.ts) 的 wait_agent） | 采纳并持久化：等待不携带任意条件表达式，没有在途工作时立即返回（第 10.3 节） |
| 成员身份写在首条 user 消息里，不写入 system prompt；system 策略和工具 schema 在成员间一致，保住缓存前缀（[Agent Teams 决策](/Users/wang/workspace/deepseek-harness/.agents/notes/implemented/feature/2026-08-05-agent-teams.zh.md) 的 Team identity 一节） | 采纳其原则：动态状态不进 system prompt、工具 schema 在成员存续期间不变（不变式 13）。OpenBox 成员各有模型和指令，前缀不跨成员共享，因此成员工具可以比协调者少 |
| 明确拒绝把任务归属或写入范围当作锁：外部写入会绕过，崩溃的负责人会永久持有，虚假的互斥保证比明确的警告更危险（同上，Alternatives considered） | 采纳。不建通用资源占用表，只在工具层能真正强制的地方做独占（第 7.5 节） |
| 被委派的成员审批策略固定为 never：需要审批的操作确定性拒绝并留审计，同时告知成员以上报限制收尾而不是重试。起因是子会话的审批提示无人可见，受阻成员与正常成员无法区分（[审批钉定决策](/Users/wang/workspace/deepseek-harness/.agents/notes/implemented/feature/2026-08-10-subagent-approval-pinned-never.zh.md)） | 采纳，且与 OpenBox 现状一致：子会话已经不能向用户提问，其通知也被抑制（第 4 节、第 7.6 节） |
| 子级模型路由必须来自用户授权的精确列表，授权在会话创建时快照；模型目录通过发现工具按需读取，不写进委派工具的 schema（[模型路由授权](/Users/wang/workspace/deepseek-harness/.agents/notes/implemented/feature/2026-08-24-user-authorized-subagent-model-routes.zh.md)） | 采纳。allowed_models 随 TeamRun 冻结，协调者通过 team_view 读取候选模型（第 7.1 节） |
| fork 成员继承 Lead 已完成的轮次，并禁止为 fork 换模型，否则继承的前缀要重新预填充（[模型选择路由](/Users/wang/workspace/deepseek-harness/.agents/notes/implemented/feature/2026-08-18-model-selected-subagent-routes.zh.md)） | 只提供 fresh 成员。每成员独立模型是已确认目标，与 fork 的缓存前提冲突 |
| 任务就绪不会启动负责人，唤醒只能靠消息；任何成员都能创建和领取无主任务 | 不采纳。OpenBox 的任务创建时即有负责人，由调度器在依赖满足时代为领取并唤醒（第 9.4 节），减少依赖模型遵守的协议步骤 |
| 只在用户明确要求时创建团队，默认工具目录和简单任务行为不变 | 采纳。TeamRun 只在用户明确同意后创建：用户自己选了团队模式，或在 AI 提议的组队方案卡上点了“用团队做”。普通聊天中的 Agent 可以提议，不能自行组队（第 8.2 节） |
| 限额：成员 16、活动任务 256、每成员待投递消息 64、单条消息 64 KB，超限返回类型化错误 | 采纳限额种类，补充每成员待投递上限和单条消息大小上限（第 6.2 节）；数值按第 6.4 节取更小的起始值 |

重点阅读源码：

- [roster.ts](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/src/roster.ts)：成员接纳与创建。
- [mailbox.ts](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/src/mailbox.ts)：持久消息及目标回执。
- [task-board.ts](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/src/task-board.ts)：任务版本及依赖。
- [journal.ts](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/src/journal.ts)：变更提交顺序。每个 Lead 的所有变更经同一条队列串行执行，读取、校验、追加在一个操作内完成；第 10.1 节的“每运行串行提交”是它在数据库上的对应物。
- [activity.ts](/Users/wang/workspace/deepseek-harness/packages/experimental/agent-team/src/activity.ts)：一次性等待者，只在所属事件落盘后唤醒。
- [表面精简决策](/Users/wang/workspace/deepseek-harness/.agents/notes/archived/simplification/2026-08-12-trim-agent-teams-read-and-lifecycle-surface.zh.md)：DSH 在首个版本之后删掉了没有读取方的快照、全局 revision、变更类型和重复字段。本计划的数据模型按同样标准裁剪（第 11 节）。

### 3.2 其他参考对象只承担明确的辅助角色

| 对象 | 已核实能力 / 定位 | 本项目参考内容 |
|---|---|---|
| [Kimi Agent Swarm](https://www.kimi.ai/blog/kimi-k2-5) | 协调者动态拆分任务并创建专业子 Agent；K2.5 技术介绍使用 PARL 训练协调者 | 自动分工、有效并行、结果汇总；不把其模型训练收益视为框架复制后的收益 |
| [Kimi Code 自定义 Agent](https://moonshotai.github.io/kimi-code/zh/customization/agents) | 自定义角色、工具范围、独立子上下文 | Agent 定义与发现机制 |
| [Kimi Code 模型池](https://moonshotai.github.io/kimi-code/zh/configuration/config-files#secondary_model) | 子 Agent 可从候选模型选择；自定义 Agent 文件与模型池是分开的配置机制 | 候选模型、默认模型和实际模型绑定 |
| [CrewAI Agent](https://docs.crewai.com/en/concepts/agents)、[CrewAI AMP](https://docs.crewai.com/enterprise/introduction) | 成员可单独配置模型；AMP 提供可视化构建、部署及监控 | Agent 库、团队模板、配置与试运行体验 |
| [Amazon Bedrock 多 Agent](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-announces-general-availability-of-multi-agent-collaboration/) | 多 Agent 协作已正式发布，主管组织专业成员 | 可复用成员与主管职责分离 |
| [Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/authoring-add-other-agents) | 支持子 Agent 与独立 Agent 组合；部分外部连接仍在预览 | 团队配置、成员启停和运行可见性 |
| [LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence) | 区分运行检查点和跨运行存储，支持恢复 | 状态保存、恢复、运行上下文与长期记忆分离 |

上述产品资料用于确认设计可行性和借鉴交互。本文没有对这些产品进行同一负载的实测，也不宣称每个产品都完整具备本项目的三种组队能力。

### 3.3 A2A 的位置

[A2A 官方说明](https://a2a-protocol.org/latest/topics/what-is-a2a/) 将其定位为跨框架、跨供应商的 Agent 通信标准，包含发现、消息与长任务交互。

本项目的选择是：内部团队直接调用 TeamService；后续外部 Agent 通过 A2A Adapter 接入同一任务与成果协议。A2A 不能替代本平台的 Agent 库、组队策略、任务所有权、预算和内部执行恢复。

本计划定义 RuntimeAdapter 接口及能力声明（第 12.3 节），只实现 OpenBox 自己的适配器；不实现远程适配器，也不向用户显示未接通的外部 Agent 入口。接口保持窄：它同时是团队内核接触 Driver、Inbox、Session 的唯一位置。

### 3.4 workspace 内的其他实现

以下三个项目在本机 workspace 中，结论均来自源码。它们不是团队内核的参考，但各自验证过或踩过本计划要面对的具体问题。

| 项目与版本 | 做法 | 对本计划的含义 |
|---|---|---|
| [Codex](/Users/wang/workspace/codex/codex-rs/core/src/tools/handlers/multi_agents_spec.rs) 7d6f808，multi_agent_v2 | 两个独立的限流器：已加载的运行时数量（空闲者按最久未用淘汰）和活跃轮次数量；默认 4 个线程且包含根，即 3 个子 Agent。V1 中“已完成的 Agent 在关闭前仍占并发名额”是它自己记录的坑 | 团队并发只统计正在执行模型轮次的成员，空闲和等待的成员不占名额；默认并发取 3（第 6.4 节） |
| 同上 | 模型在 spawn 时对照实时目录校验，错误信息里带可用模型列表；角色锁定的模型写进角色描述；角色改了模型之后重新校验推理档位 | 模型在成员接纳时校验，MODEL_NOT_ALLOWED 携带允许列表，协调者不需要单独的模型发现工具（第 7.1 节） |
| 同上 | 子级的审批策略、沙箱和权限在应用角色之后重新覆盖为父级当前值，角色只能关闭能力；重新加载时与父级当前权限取交集，漂移则拒绝 | 与第 7.3 节的交集计算一致，佐证“成员重启时重新校验并取交集” |
| 同上 | 只把子级最后一条消息放进带类型的信封交回，错误截断到约 900 token，被中断的轮次不通知父级 | 任务结果摘要和错误设长度上限（第 9.5 节） |
| 同上 | V2 子级归父级所有：拒绝用户直接输入、引导和改设置，只允许中断和回复审批 | 运行中的成员会话同样拒绝用户直接发消息，用户只和协调者对话（第 5.3 节） |
| 同上 | 邮箱在内存里，进程交接时丢弃；没有对中断轮次的自动重跑 | 反例。OpenBox 已有持久 Inbox，不应退回内存队列 |
| 同上 | 整棵 Agent 树共享一个 token 预算；提示词要求“谨慎使用 wait，不要条件反射式地反复等待”；等待最短 10 秒以防空转 | 佐证运行级预算和第 8.6 节的唤醒约束 |
| [opencode](/Users/wang/workspace/opencode/packages/opencode/src/tool/task.ts) fc80874 | Agent 用带 frontmatter 的 markdown 定义，字段为 description、mode、model、prompt、permission、steps 等；模型在首次请求时才校验，配置错误要到运行中才暴露 | 用户 Agent 的字段集合可以更小（第 6.1 节）；校验必须前置到保存和接纳时 |
| 同上 | 子会话的权限只继承父会话的 deny 规则，子 Agent 可以比父 Agent 能做更多；“始终允许”保存在实例级内存列表并按最后匹配生效 | 反例。OpenBox 的 SubagentAuthority 单调收窄更严格，保持不变 |
| 同上 | 后台任务完成时向父会话注入一条合成消息，启动或并入父会话的循环；规格写明重复唤醒要合并，并要求在开放多方投递前先设队列积压上限 | 佐证第 8.6 节的唤醒合并和每成员待投递上限 |
| [Symphony](/Users/wang/workspace/symphony/SPEC.md) 8001b52 | 单一调度权威；每个 tick 固定为先对账、再校验、取数、排序、派发；用最后事件时间判停滞；正常续跑 1 秒重试，失败按 min(10s·2^(n-1), 5min) 退避；每个 worker 有最大轮数；规格要求审批“不得让运行无限期停滞” | 调度 tick 的顺序、停滞判定和退避公式直接采用（第 10.3 节）；它的调度状态只在内存里，这一点不采用 |

### 3.5 各家怎么创建 Agent：需要的内容与 AI 参与的方式

为第 6.1 节的字段和第 6.5 节的创建流程做的对照。DSH、opencode、Codex 来自本机源码；其余来自各产品的官方文档，时间点为 2026-09-20。调研时有几家平台正处在产品更替中（例如 OpenAI 的自定义 GPT 与 Agent Builder、Bedrock Agents、扣子的低代码平台），表里按当时文档记录，只取与本计划有关的字段和流程，不依赖它们的具体时间表。

| 产品 | 定义里有什么 | AI 参与创建的方式 | 生效前人要做什么 |
|---|---|---|---|
| DSH | “Agent 预设”：一个目录，含编排文件（工具、人设前后缀、提示词段落、压缩策略）和只管显示的名称、描述。模型路由、沙箱、审批策略刻意不放进预设，属于部署方的配置。运行时模型为子 Agent 或成员只能给出名字、职责描述、初始任务，以及用户授权范围内的模型 | “创作者模式”：界面上的新建入口会开一个专门会话，由其中的 Agent 加载内置技能，复制现成预设再改写；没有一步生成的接口 | 写文件受文件审批策略约束；AI 不能启用自己写出的预设，用户必须在选择器里选中它并新开会话。浏览器端只能复制、删除、设默认，不能编辑预设内容 |
| Claude Code 子 Agent | 带 frontmatter 的 markdown，正文即系统提示词。必填只有 name 和 description；选填 tools（不填则继承全部）、disallowedTools、model（可写 inherit）、permissionMode、maxTurns、skills、memory、color 等 | 用户在对话里直接提要求，Claude 用文件写入工具把整份定义写出来，包括工具和模型；旧的交互式创建向导已移除 | 写入 .claude 目录会触发写文件的权限确认；文档要求用户审阅文件。没有单独的发布步骤，文件写入后即可用。子 Agent 的权限不超过主对话 |
| opencode | markdown 定义：description、mode、model、prompt、permission、steps、temperature、color 等 | 命令行 agent create：用户写一句描述，模型只生成三项，即标识名、何时使用（带示例对话）、系统提示词；聊天中的 Agent 也能直接写定义文件，并有内置技能指导 | 命令行里工具、模式、保存位置由人选；已有同名文件则拒绝覆盖。聊天中写项目目录默认不提示，需要重启才加载 |
| Codex | TOML 角色：description 必填，另有开发者指令、模型、推理档位、功能与技能的关闭项；角色只能收窄能力，不能替换父会话的权限 | 没有创建命令，也没有 AI 辅助；只有从 Claude Code 定义做的确定性导入 | 人手写文件；定义所在目录对 Agent 默认只读 |
| Kimi Code | markdown 定义：description 必填，另有 whenToUse、tools（不填即全部）、disallowedTools、subagents；没有 model 字段 | 文档未提供 | 文档提醒：来自不可信仓库的定义文件可以替换整个系统提示词，不写 tools 等于保留全部工具 |
| OpenAI Workspace Agents、Agents SDK | 名称、描述、指令、模型与推理档位、图标、起始提示、工具与应用、技能、文件、记忆、渠道、日程；SDK 里只有 name 必填，另有 handoff_description、output_type | 对话式构建：输入需求，先审阅草案计划，再构建、预览。AI 可以改几乎所有字段，但改动只落在草稿里 | 显式点 Create 或 Update 才生效；写操作默认逐次询问；文档写明“指令本身不授予应用访问权”；应用的授权登录必须由人完成 |
| 扣子 | 名称、功能介绍、图标、人设与回复逻辑、模型设置、插件、工作流、知识库、变量与数据库、开场白、问题建议；多 Agent 模式下每个节点另有“适用场景”，用来决定何时交给它 | “AI 创建”：输入需求生成一个智能体，进入编辑页；另有提示词自动优化 | 用户调试后点发布，填写发布说明并选择渠道 |
| Dify | 指令与模型必填；变量、知识库、工具、最大迭代次数、开场白选填。新版 Agent 另有技能包、文件、环境变量 | 提示词生成器按描述起草；新版的 Build 模式在对话中自行配置技能、文件和环境变量，改动进入构建草稿 | Apply 或 Discard 决定是否保留草稿，Publish 才上线并产生版本；文档写明已发布的 Agent 不会应最终用户要求改自己的提示词和工具 |
| CrewAI | role、goal、backstory 必填；llm、tools（默认空）、max_iter、allow_delegation（默认否）等选填。协作者按 role 寻址，文档告诫不要用含糊或重叠的角色 | Crew Studio：用文字或语音描述，AI 生成 agents、tasks 和 tools，聊天与画布共享状态 | 本地试运行后点 Publish 部署 |
| Copilot Studio、M365 Agent Builder | 名称、描述、指令（上限 8000 字符）、图标；选填模型、触发器、知识、工具、其他 Agent、建议提示。子 Agent 另有“何时使用”（默认由编排器按描述决定）、类型化的输入输出、启用开关 | “描述以创建”：AI 生成名称、描述和指令；工具、知识、触发器只作为建议，逐项由人点添加 | 测试窗格之后点发布并二次确认；每个工具可设“运行前询问用户” |
| Bedrock Agents | 名称、指令（40 到 4000 字符）、基础模型、IAM 角色；选填描述、动作组、知识库、护栏、记忆。协作者关系上另有协作者名称和协作说明，即何时使用该协作者 | 对话式构建器只能做封闭清单里的几件事：改名称和描述、归纳指令、增删动作组、关联知识库；不能设模型、角色和护栏 | Save，再 Prepare，测试后发版本和别名 |
| Google Gems、Agent Designer、ADK | 名称、指令、知识文件；企业版的主 Agent 与子 Agent 各有名称、描述、指令、模型、数据源与工具。ADK 里 description 主要供其他 Agent 路由使用，另有 input_schema、output_schema | Gems 用一两句话让 Gemini 改写指令；Agent Designer 由提示生成初版 | 预览不会自动保存，显式 Save 或 Create 才生效 |
| LangSmith Fleet | 指令、工具与连接、渠道与触发器、子 Agent、技能、记忆、模型档位、共享范围 | “Build with AI”：Agent 自己完成配置，在关键点停下等人，包括澄清问题、授权登录、审批设置 | 对话测试后 Save；每个工具有自动或询问两种审批方式；Agent 改写自己的记忆默认需要批准 |
| 文心智能体平台、腾讯元器 | 头像、名称、简介、指令或提示词、开场白与示例问题、知识库、插件、工作流、长期记忆 | 文心的创建助手由一句人设描述生成名称、简介、开场白、指令和引导示例；元器有提示词一键优化 | 右侧预览后发布；文心仅自己可见的免审核，其余可见范围要平台审核 |

出处：DSH 见 [agent-presets](/Users/wang/workspace/deepseek-harness/packages/preset/agent-presets/README.md)、[创作者模式预设](/Users/wang/workspace/deepseek-harness/packages/preset/agent-presets/presets/cordis/agent.cordis.yml)、[人设与工具过滤笔记](/Users/wang/workspace/deepseek-harness/.agents/notes/implemented/feature/2026-07-12-subagent-persona-tool-filter-and-depth.zh.md)；opencode 见 [agent.ts](/Users/wang/workspace/opencode/packages/opencode/src/cli/cmd/agent.ts)、[generate.txt](/Users/wang/workspace/opencode/packages/opencode/src/agent/generate.txt)；Codex 见 [role.rs](/Users/wang/workspace/codex/codex-rs/core/src/agent/role.rs)。其余：[Claude Code 子 Agent](https://code.claude.com/docs/en/sub-agents)、[Kimi Code](https://moonshotai.github.io/kimi-code/en/customization/agents.html)、[OpenAI Workspace Agents](https://help.openai.com/en/articles/20001143-chatgpt-workspace-agents-for-enterprise-and-business)、[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/agents/)、[扣子](https://docs.coze.cn/guides/agent_overview)、[扣子多 Agent](https://docs.coze.cn/guides/multiagent)、[Dify](https://docs.dify.ai/en/use-dify/build/agent)、[CrewAI](https://docs.crewai.com/en/concepts/agents)、[Crew Studio](https://docs-platform.crewai.com/platform/en/features/crew-studio)、[Copilot Studio 创建](https://learn.microsoft.com/en-us/microsoft-copilot-studio/authoring-first-bot)、[Copilot Studio 子 Agent](https://learn.microsoft.com/en-us/microsoft-copilot-studio/add-agent-child-agent)、[Bedrock 创建](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-create.html)、[Bedrock 协作者](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_AssociateAgentCollaborator.html)、[Gems](https://support.google.com/gemini/answer/15235603)、[ADK](https://adk.dev/agents/llm-agents/)、[LangSmith Fleet](https://docs.langchain.com/langsmith/fleet)、[文心智能体平台](https://agents.baidu.com/docs/develop/agent/zero_code_develop/)、[腾讯元器](https://yuanqi.tencent.com/guide/agent-build-first-agent)。

从对照里得到四条结论，第 6.1 节和第 6.5 节据此设计：

1. 内容的共同核心只有四样：名称或标识、指令、描述、模型。其中模型总是可解析的，可以指定、继承或取默认值。工具、知识与文件、开场白与示例、头像、记忆、步数上限都是选填。较新的产品把 Skills 作为定义里单独的一项：Claude Code 的 skills 字段、OpenAI Workspace Agents、Dify 新版 Agent、LangSmith Fleet 都是如此，与工具分开选择。
2. 描述在单 Agent 产品里是给人看的简介；到了多 Agent 产品里，它变成编排器选人的依据，并且成为必填：Claude Code 和 Kimi Code 的 description 与 whenToUse、扣子的适用场景、Copilot Studio 子 Agent 的“何时使用”、Agents SDK 的 handoff_description、ADK 的 description。Bedrock 和 Copilot Studio 还把这段话放在“关系”上而不是 Agent 本身：同一个 Agent 进了不同团队，可以有不同的使用说明。各家的共同告诫是描述要彼此区分、不重叠；微软的经验是一个编排器面对三四十个选项之后选择质量会下降。
3. AI 参与创建有四种做法。一次性起草，人改完保存：Copilot Studio、扣子、文心、Gems、Dify 的提示词生成器、opencode 的命令行。对话式构建器边聊边改草稿，显式应用或发布：OpenAI Workspace Agents、Dify 新版、M365、Bedrock、Crew Studio、LangSmith Fleet。让对话里的 Agent 直接写出定义：Claude Code、opencode、DSH 的创作者模式。完全自主、无人过目的持久 Agent：没有哪家的文档提供；完全自主的只有运行期的临时 Agent，例如 DSH 的 spawn_teammate、Claude SDK 的动态定义、OpenBox 现有 task 工具里的临时人设，以及本计划里协调者创建的临时成员。
4. 无论哪种做法，AI 写的是内容，人守的是生效和授权。AI 普遍可以写名称、描述、指令、开场白和示例；工具和知识要么只是建议、由人逐项添加，要么落在草稿里等人发布；图形界面产品从零工具起步，由人显式添加。授权登录、写操作的审批策略、发布与共享，各家都留给人。OpenAI 的说法是指令本身不授予访问权，DSH 把沙箱和审批放在预设之外，是同一个原则。

<a id="baseline"></a>
## 4. 当前实现与差距

以下事实以本文顶部提交为基线，实施前按符号复核。

| 当前代码 | 已有能力 / 限制 | 对改造的影响 |
|---|---|---|
| [AgentDef 与注册表](/Users/wang/workspace/OpenBox/backend/agent/agent.py)；[AgentOverride](/Users/wang/workspace/OpenBox/backend/core/config.py) | 内置与部署配置定义 Agent；不是按用户管理的持久 Agent 库 | 新增有所有者、版本与作用域的定义层，编译成现有运行配置 |
| [TaskArgs、execute](/Users/wang/workspace/OpenBox/backend/tool/task.py) | spawn、fork、follow_up、interrupt、report、list；可配置模型等 | 保留旧接口；团队增加异步成员接纳和事件唤醒 |
| 同文件的 _dispatch_activation | 前台等待子 Agent 运行或结果 outbox | 不能直接当作团队的非阻塞派工接口 |
| [工具调度器](/Users/wang/workspace/OpenBox/backend/agent/tool_scheduler.py) | 只有明确 parallel_safe 的工具才合并并行；Task 未作此标记 | 团队并发通过持久运行调度实现，不能只给 Task 加并行标志 |
| [SubagentComposition](/Users/wang/workspace/OpenBox/backend/agent/subagent_composition.py) | 冻结预设、模型、工具及能力；预设设置了 model 时是锁定语义，内置 explore、general 未设置 model，调用方可以指定。解析顺序为请求值、预设值、继承值、部署默认值 | 保留冻结机制；新定义分开 default_model 与 model_locked |
| 同文件的 build_subagent_composition | 明确拒绝 BUILD_ONLY_WORKFLOW_TOOLS，显式请求也不能绕过 | 团队使用独立的可委派工具政策；旧 Task 默认限制保留 |
| [SubagentAuthority](/Users/wang/workspace/OpenBox/backend/agent/subagent_authority.py) | 父子权限单调收窄；带 parent_id 的普通子会话缺 descriptor 时拒绝运行 | 团队必须有显式的运行绑定加载器，不能仅创建带 parent_id 的 Session |
| [SubagentRuntime](/Users/wang/workspace/OpenBox/backend/agent/subagent_runtime.py) | 直接父子身份检查；活跃 activation 时拒绝 follow_up | 不能把同伴通信伪装为旧 follow_up |
| [子 Agent 表](/Users/wang/workspace/OpenBox/backend/db/models/subagent.py) | descriptor、activation、outbox 已持久化；activation 关联父 Part 和父 generation | 团队自动唤醒不能伪造一个仍在执行的父工具调用 |
| [AgentInbox](/Users/wang/workspace/OpenBox/backend/agent/inbox.py) | 持久接纳、下一轮/下一步注入与唤醒；delivery 分 followup、steer、inject，其中 inject 不会唤醒空闲会话 | 新增来源列和事务 helper，作为成员输入与唤醒底座；现状与风险见第 4.1 节 |
| [Driver](/Users/wang/workspace/OpenBox/backend/agent/driver.py) | 数据库租约、generation、执行并发额度 | 所有成员复用；协调者休眠时释放执行槽 |
| [run_loop](/Users/wang/workspace/OpenBox/backend/agent/loop.py) | 已有模型、工具、问答、结果与恢复执行链 | 注入显式 RuntimeBinding，所有成员继续走同一执行循环 |
| [SkillInfo](/Users/wang/workspace/OpenBox/backend/skill/skill.py)、[Skill 工具](/Users/wang/workspace/OpenBox/backend/tool/skill_tool.py) | Skill 是指令内容，allowed_tools 不构成授予 | 继续保持，不用 Skill 文件建立权限 |
| [ExternalEffect](/Users/wang/workspace/OpenBox/backend/db/models/external_effect.py)、[effect_ledger](/Users/wang/workspace/OpenBox/backend/agent/effect_ledger.py) | 外部效果有持久记录、claim 和 outcome_unknown | 关联团队任务与 attempt，复用业务操作的恢复语义 |
| [Autopilot ledger](/Users/wang/workspace/OpenBox/backend/autopilot/ledger.py) | 现有运行账本含进程内状态 | 不能直接充当跨成员、跨进程的团队预算 |
| [现行轨迹规格](/Users/wang/workspace/OpenBox/docs/trajectory-rearch/SPEC.md) | Trace 独立存储、可丢失、不得阻塞业务；管理接口只向超管开放 | 团队业务事件独立保存，用户面板不开放超管原始轨迹 |

### 4.1 源码核对补充

下表是按同一基线逐项核对源码后补充的事实，第 5 到第 15 节的若干设计直接由它们决定。

| 当前代码 | 事实 | 对改造的影响 |
|---|---|---|
| [agent_event.py](/Users/wang/workspace/OpenBox/backend/db/models/agent_event.py)、[agent_event_log.py](/Users/wang/workspace/OpenBox/backend/session/agent_event_log.py) | 内核已经是事件溯源：agent_events 是每会话只追加的事件流，带会话内连续序号和幂等用的 event_key，历史从不 UPDATE；messages、parts 是可重建的读模型。每次模型请求前，内核对该会话的整条事件前缀做序号加摘要的比较，前缀有变化就重新回放并重建请求 | 团队存储采用同样的形态（第 11 节）。团队事件不写进根会话的 agent_events：那会让协调者的每次请求因前缀变化而重建，也会拖慢它每一步的回放。使用同结构的独立事件流 |
| [driver.py](/Users/wang/workspace/OpenBox/backend/agent/driver.py) 的 _enforce_agent_quota_locked、[config.py](/Users/wang/workspace/OpenBox/backend/core/config.py) 的 max_concurrent_agents | 并发名额按用户计，默认 5，统计该用户所有未过期的运行，子会话和被阻塞的父会话都算。名额用尽时用户自己发消息得到 429 CONCURRENT_AGENT_QUOTA_EXCEEDED；恢复路径遇到名额不足会保留记录，等下一轮扫描重试 | 一个协调者加四个并发成员会占满用户的全部名额。团队必须给用户聊天留余量，名额不足按背压处理（第 10.3 节） |
| [session_executions](/Users/wang/workspace/OpenBox/backend/db/models/question.py)、[question/runtime.py](/Users/wang/workspace/OpenBox/backend/question/runtime.py) | 除 Driver 外还有第二层 60 秒租约，其 generation 表示用户轮次，Driver 的 generation 表示一次运行预约 | 成员绑定、让出执行和恢复必须同时满足两层，本文其余位置的“generation”需区分所指 |
| [question.py](/Users/wang/workspace/OpenBox/backend/question/question.py) 的 QuestionSuspended、[processor.py](/Users/wang/workspace/OpenBox/backend/agent/processor.py)、[continuation.py](/Users/wang/workspace/OpenBox/backend/question/continuation.py) | 已有一套持久化的“工具调用挂起、释放租约、事后续跑”协议，但写死在提问类工具上，且拒绝子会话。另有先例：工具结果带 plan_ready 元数据时本轮以 stop 结束 | team_wait 不需要新发明让出机制：沿用“工具结果结束本轮”的先例，唤醒统一走 Inbox（第 10.3 节） |
| [agent_inbox.py](/Users/wang/workspace/OpenBox/backend/db/models/agent_inbox.py)、[inbox.py](/Users/wang/workspace/OpenBox/backend/agent/inbox.py) | Inbox 没有来源列，只接纳用户输入，每条都落成 role=user、synthetic=False 的消息；幂等靠 (user_id, session_id, client_id) 唯一加请求摘要。唤醒是进程内尽力而为，兜底是 15 秒一次的恢复扫描。只有请求携带 delivery 字段才进入 Inbox，Web 和移动端目前都不发送该字段，这条路径在生产上基本没有被走过 | “扩展来源类型”实际是新增列。团队把全部投递和唤醒压在一条缺少线上验证的路径上，M0 必须先让它承载真实流量（第 15.1 节） |
| [task.py](/Users/wang/workspace/OpenBox/backend/tool/task.py)、[tool_scheduler.py](/Users/wang/workspace/OpenBox/backend/agent/tool_scheduler.py) | Task 只有前台模式；等待结果时每 0.2 秒查一次数据库且没有超时；工具体统一受 600 秒上限约束，超时的子任务被持久中止 | 团队工具都不能在工具体内等待其他成员 |
| [subagent.py](/Users/wang/workspace/OpenBox/backend/db/models/subagent.py) | activation 的 parent_message_id、parent_part_id、parent_run_id、parent_generation 均为 NOT NULL，parent_part_id 唯一 | 印证第 5.3 节：成员不能复用 activation 和 outbox |
| [subagent_authority.py](/Users/wang/workspace/OpenBox/backend/agent/subagent_authority.py) 的 load_subagent_authority、[loop.py](/Users/wang/workspace/OpenBox/backend/agent/loop.py) | run_loop 只在一处加载子会话授权，得到 tool_ids、权限层、守卫层和冻结的 Agent 预设；cron 会话按 kind 豁免 | 团队绑定在这个加载点分流并返回同构结果，run_loop 的消费点不动（第 5.3 节） |
| [tool_exposure.py](/Users/wang/workspace/OpenBox/backend/agent/tool_exposure.py) 的 AGENT_RESIDENT_IDS | 每种 Agent 有常驻工具集，其余工具按意图包延迟暴露 | 团队工具必须对团队会话常驻，否则模型可能找不到 team_wait、team_finish（第 12.2 节） |
| [recovery_service.py](/Users/wang/workspace/OpenBox/backend/agent/recovery_service.py) | 已有独立于 Cron、每 15 秒一轮、分阶段且幂等的恢复服务，阶段顺序有明确约束 | 团队的补偿扫描作为其中的新阶段，不另起并行扫描器（第 10.3 节） |
| [billing/service.py](/Users/wang/workspace/OpenBox/backend/billing/service.py) | 只有事后结算，没有预留或冻结；BILLING_MODE 默认 shadow，不产生扣款；enforce 模式只预检余额是否大于零；用量记在发起调用的会话上；没有按会话或按运行的上限 | 团队预算不能假设存在预留原语：模型调用按成员会话的 usage_events 汇总做软上限，付费工具的严格预留由团队层自己实现（第 10.6 节） |
| [effect_ledger.py](/Users/wang/workspace/OpenBox/backend/agent/effect_ledger.py) | 目前只有 image_gen 接入 ExternalEffect；视频与发布工具使用各自的幂等键和 Job 表 | “复用外部效果恢复语义”只对已接入的工具成立，T2 层逐个工具确认 |
| [api/ws.py](/Users/wang/workspace/OpenBox/backend/api/ws.py)、[bus.py](/Users/wang/workspace/OpenBox/backend/bus/bus.py) | WebSocket 按 userId 路由：用户的每个连接会收到其所有会话的事件，子会话也在内；队列上限 1000，满了丢弃非关键事件；没有序号和补读，客户端靠 1 秒轮询补偿 | 多个成员同时流式输出会放大到该用户的每个客户端。成员会话的高频事件只发给订阅了该会话的连接（第 12.4 节） |
| [session.py](/Users/wang/workspace/OpenBox/backend/session/session.py)、[session_repo.py](/Users/wang/workspace/OpenBox/backend/db/repository/session_repo.py) | 会话列表只显示无 parent_id 或 kind 为 cron 的会话；每用户 200 个会话的上限只排除 cron，子会话计入 | 成员会话自动不进侧栏，但会占会话上限，需要排除并设保留期（第 11.1 节） |
| [db/base.py](/Users/wang/workspace/OpenBox/backend/db/base.py) | 配置了 jwt_secret 用 PostgreSQL 与 Alembic；否则用 SQLite，由 create_all 加手写升级桥建表，不跑 Alembic | 新表在 SQLite 上自动建立，改动既有表才需要手写桥。数据模型只改一张既有表（第 11 节） |
| 部署形态：[Dockerfile](/Users/wang/workspace/OpenBox/backend/Dockerfile)、[混合压测](/Users/wang/workspace/OpenBox/docs/trajectory-rearch/LOADTEST-2026-09-15-MIXED.md) | 单个 Uvicorn 进程，运行是进程内的 asyncio 任务；连接池 10 加溢出 20。driver、inbox、permission、session status、snapshot、sandbox manager 都有进程内注册表。容量计划的 worker 拆分尚未开始 | 团队层按多进程正确性设计并专项验证，但现行部署下所有成员都跑在同一个进程里。成员并发直接增加这个进程的内存和连接池压力，容量测量前置到 M1（第 15.2 节） |

<a id="architecture"></a>
## 5. 总体架构

### 5.1 分层架构图

~~~mermaid
flowchart TB
    subgraph Product["产品层"]
        Library["Agent 库"]
        Templates["团队配置"]
        Dashboard["团队运行面板"]
    end
    subgraph Config["定义与配置层"]
        Definitions["AgentDefinition / Version"]
        TeamDefinition["TeamDefinition / Version"]
        Compiler["配置解析、授权校验、成员快照"]
    end
    subgraph Team["团队内核：主要参考 DSH"]
        Lead["协调者 Agent"]
        Service["TeamService"]
        Roster["成员名册"]
        Board["任务板与验收"]
        Mailbox["消息与交接"]
        Scheduler["事件唤醒与恢复"]
    end
    subgraph Execution["OpenBox 执行层"]
        Binding["RuntimeBinding"]
        Inbox["现有 AgentInbox"]
        Driver["现有 Driver 与 run_loop"]
        Models["模型适配器"]
        Tools["工具、Skills、MCP、后台任务"]
    end
    subgraph Storage["持久化与可观测性"]
        Lib["Agent 库与团队模板：长期表"]
        Log["team_events：每次运行一条只追加事件流"]
        Runs["team_runs：每次运行一行索引与回放缓存"]
        Assets["文件资产与成果引用"]
        Trace["独立 Trace 管线：辅助排障"]
    end
    Library --> Definitions
    Templates --> TeamDefinition
    Definitions --> Compiler
    TeamDefinition --> Compiler
    Compiler --> Service
    Lead <--> Service
    Service --> Roster
    Service --> Board
    Service --> Mailbox
    Service --> Scheduler
    Service --> Log
    Service --> Runs
    Definitions --> Lib
    TeamDefinition --> Lib
    Mailbox --> Inbox
    Scheduler --> Inbox
    Roster --> Binding
    Binding --> Driver
    Inbox --> Driver
    Driver --> Models
    Driver --> Tools
    Driver --> Service
    Board --> Assets
    Log --> Dashboard
    Runs --> Dashboard
    Assets --> Dashboard
    Driver -.-> Trace
~~~

### 5.2 模块职责

| 模块 | 必须负责 | 不应承担 |
|---|---|---|
| AgentCatalog | 定义发现、版本、访问控制、能力摘要 | 保存成员运行中的完整对话 |
| CompositionCompiler | 解析模型、Skills、工具与授权；生成不可变快照 | 由提示词推断额外权限 |
| TeamService | 团队命令与状态机、事务、调用身份检查；每个命令是“锁运行行、对照回放状态校验、追加事件、更新缓存” | 调用一个固定模型代替所有成员；绕过事件流直接改状态 |
| TeamState（fold 与 invariant） | 纯函数：把事件流回放成名册、任务板、邮箱、等待与预算占用；追加前校验候选事件 | 读写数据库、调用模型或依赖当前时间之外的外部状态 |
| CoordinatorPolicy | 角色选择、任务拆分、必要时补充成员、验收策略 | 绕过 TeamService 改数据库状态 |
| TeamRuntimeAdapter | 从快照启动、唤醒、观察、取消成员；团队内核接触 Driver、Inbox、Session 的唯一位置 | 另写一套模型和工具循环 |
| TeamLiveness | 停滞判定、催促、隐式提交、协调者轮数与时长上限 | 让模型自己判断团队是否卡住 |
| TeamScheduler | 对账、补投、唤醒、派发、收尾；并发、名额背压、独占分组与预算门禁；慢路径挂在现有恢复服务里 | 用 LLM 猜测租约或队列状态；另起独立的扫描进程 |
| TeamProjection | 把回放状态和事件投影成用户可见的快照、任务图、消息、用量与成果；按事件种类白名单字段 | 把原始事件直接返回给客户端；暴露平台 system prompt、凭据、超管 Trace payload |

### 5.3 与原子 Agent 运行的关系

TeamMember 不直接冒充旧 SubagentDescriptor。引入通用 AgentRuntimeBinding：

- 普通会话：使用现行用户会话配置。
- 旧 Task 子会话：由 LegacySubagentBinding 加载原 descriptor 和权限快照。
- 团队协调者/成员：由 TeamMemberBinding 加载团队成员快照和 TeamRunGrant。
- 用户 Agent 的试运行会话：由 DefinitionBinding 把指定的定义版本编译为冻结预设，权限沿用普通会话。

这几种绑定最终进入同一 Driver 与 run_loop。旧 Task 的持久表和公开协议继续保留；团队执行不要求提供不存在的 parent_part_id，也不要求协调者在成员工作期间一直持有父执行租约。

绑定的落点要小。run_loop 目前只在一处调用 load_subagent_authority，得到的 SubagentAuthority 包含 tool_ids、权限层、守卫层和冻结的 Agent 预设，后续的工具过滤、权限检查和 get_agent 都消费这个结构。RuntimeBinding 在这个加载点按会话类型分流，并返回同构的结果，run_loop 内部的消费点不改：

| 会话 | 判定 | 加载来源 |
|---|---|---|
| 普通会话 | 无 parent_id，且没有以它为根的活动团队运行 | 不加载，保持现状 |
| 用户 Agent 的试运行会话 | 无 parent_id，会话的 agent 字段指向一个用户定义版本 | 把该版本编译为冻结预设并绑定到上下文；权限沿用普通会话的交互式链路（第 6.5 节） |
| 旧 Task 子会话 | 有 parent_id，kind 为 normal | 原 SubagentDescriptor，逻辑不变 |
| 团队成员 | 有 parent_id，kind 为 team_member | 该成员的接纳事件（team.member.admitted）里冻结的 authority 与 composition 快照，按成员会话 ID 索引查到；查不到或租户不符即拒绝运行 |
| 团队协调者 | 无 parent_id，存在以它为根的活动团队运行（team_runs 上的唯一索引，第 11.2 节） | 该运行协调者的接纳事件里的快照 |

kind 取新值 team_member 不需要改表。成员会话因此自动沿用现有的子会话规则：不进侧栏、不能向用户提问、不产生推送（第 7.6 节）。需要同步修改的是每用户会话上限的统计，把 team_member 与 cron 一样排除。

工作成员会话的 parent_id 指向协调者会话，仅用于层级与产品归属。运行授权必须来自经过验证的 TeamMemberBinding。绑定冲突、缺失或串租户时拒绝执行，不能退回普通 build Agent。

根会话在活动 TeamRun 期间绑定协调者快照。运行终态后释放该活动绑定，用户根会话可继续普通聊天或启动新 TeamRun；历史 TeamMember 记录保留。成员会话在运行期间拒绝用户经普通 Session 接口直接发消息或改模型，用户只和协调者对话；已经结束的工作成员会话只读，继续使用其成果时启动新成员，不通过普通 Session 接口绕过团队恢复协议。

协调者复用根会话有一个成本：根会话的既有历史会成为协调者每次被唤醒时的上下文。因此从团队模板点“运行”时总是新建根会话；从已有聊天发起也允许，组队方案卡上提示历史长度并给出“在新对话中运行”的链接。新建根会话换掉的只是对话历史，不是文件：工作目录属于项目而不属于会话，新根会话和全部成员会话都在所选项目的目录里运行，该项目下以前的会话留下的文件团队都能直接读写。团队沿用现有的工作目录机制，不为团队或成员另建目录体系；需要团队接着某个项目的工作做，就在那个项目下发起。协调者绑定会替换 system prompt 和工具集，缓存前缀在绑定和释放时各重建一次。运行结束后历史里会留下团队工具的调用记录，而普通模式下这些工具不再声明；各 provider 适配器对“历史中存在未声明工具的调用”的处理需在 M1 验证。

<a id="definitions"></a>
## 6. Agent、团队与运行的定义

### 6.1 AgentDefinition

AgentDefinition 是可复用的角色配置，存放在长期表 agent_definitions 与 agent_definition_versions 中，归属本人及当前 workspace，可在该 workspace 的多个项目、团队中使用，不属于任何项目或会话。定义不携带账号密钥或其他成员的会话。

定义放在数据库里，不像 opencode、Codex 那样做成项目目录里的文件。定义含有工具白名单和模型绑定，属于授权配置；项目目录是所有 Agent 都能写的地方，放在那里等于让 Agent 能改写自己的权限。沙箱里的 Skill 目前也按不可信数据处理，理由相同。

下列字段全部交付。最后一列说明用户创建时是否必须填写，不填的字段使用默认值，它们同样是定义和快照的一部分。

| 字段 | 含义 | 填写 |
|---|---|---|
| id、owner_user_id、workspace_id | 稳定身份与访问边界 | 系统生成 |
| name、description、when_to_use | 展示与协调者选择依据；名称不是权限凭据。when_to_use 对应各家多 Agent 产品里编排器选人用的那段描述（第 3.5 节），要求写清触发条件，彼此区分、不重叠 | 必填 |
| display、example_tasks | 图标与颜色；最多 5 条示例任务，既是试运行的起始输入，也帮助协调者理解何时使用 | 选填 |
| source | builtin、user、legacy；运行内生成的定义通过成员快照记录为 coordinator | 系统生成 |
| current_version_id、status | 版本入口；draft、active、archived | 系统生成 |
| instruction | 职责说明 | 必填 |
| input_schema、output_schema | 输入输出契约；output_schema 接到现有的 structured output 能力 | 选填 |
| default_model、model_locked、allowed_models | 模型默认值与允许范围 | 选填，默认继承团队或部署默认值 |
| reasoning、generation_options | 模型推理档位和生成参数 | 选填 |
| tool_allowlist | 可调用的平台工具和平台插件工具，按工具 ID 列出；表单里用预设选择（第 6.5 节） | 选填，默认是只读调研预设 |
| mcp_refs | 可调用的 MCP 工具，按“服务名加可选的工具名模式”引用，不按单个工具 ID：MCP 目录来自用户的沙箱，是动态的，服务可以增删，工具名会变 | 选填，默认为空 |
| skill_mode、skill_refs | 这个 Agent 掌握的 Skills。skill_mode 为 selected 时只能读取 skill_refs 里按名称与来源列出的 Skill，为 all_accessible 时可以检索用户能访问的全部 Skill。Skill 是知识，不是工具，不授予工具 | 创建时必经的一步，可以为空；默认 selected |
| resource_refs | 可访问的资料：FileAsset 或项目目录内的路径 | 选填，默认为空 |
| execution_policy | 步数（对应现有 AgentDef.max_steps）、运行时限、工具类别等约束 | 选填，有默认值 |

表单默认只展示必填项，其余放在高级设置里。用户填得越少，配置出错的面越小。必填的四项与市面产品的共同核心一致（第 3.5 节）：名称、描述、指令，加上多 Agent 场景下必需的“何时使用”；模型总是可解析的，所以不要求填写。

定义里不放三样东西。记忆：OpenBox 的长期记忆是用户与 workspace 级的，成员通过 creator_context 的只读动作读取，不按 Agent 各存一份。开场白：成员不直接面向用户，只有试运行用得到，由示例任务代替。授权与凭据：沙箱、审批策略、账号授权和付费上限属于运行时的授权（第 7.3 节），DSH 同样把它们放在预设之外。

定义编译的产物是现有的 FrozenAgentPreset。get_agent 已经优先读取上下文里冻结的预设，用户定义由此进入执行，不写入全局注册表。

定义编辑产生新版本。存量成员继续使用旧版本快照。归档阻止新建实例，已接纳运行按原快照继续；工具或账号授权被撤销时立即按实际权限收窄。

自定义提示词作为角色指令使用，不得覆盖平台身份、团队协议和工具执行检查。成员自动发现只返回可访问定义的摘要，选中后再读取完整配置。

部署配置里通过 AgentOverride 定义的 Agent 以 legacy 来源只读出现在目录里，用户另存后进入自己的 Agent 库；配置里 model 的锁定语义保持不变。

### 6.2 TeamDefinition

TeamDefinition 保存可重复使用的团队模板：

- 团队名称、用途、目标输入格式。
- 协调者 Agent 定义及模型配置；也可使用平台默认协调者。
- preset_members：Agent 定义引用、团队别名、职责补充、模型覆盖、追加的 Skills、启用开关。职责补充可以改写该成员在这支团队里的“何时使用”：Bedrock 的协作说明和 Copilot Studio 的关联 Agent 描述都放在团队与成员的关系上，同一个 Agent 进了不同团队可以有不同的用法说明。启用开关用于临时停用某个预设成员而不删除它。
- member_selection：explicit_only 或 coordinator_select。
- member_creation：disabled 或 run_scoped。
- allowed_agent_ids / 可搜索目录范围、allowed_models、delegable_tools、allowed_skills。allowed_skills 限定协调者为临时成员选择 Skill 的范围，默认是用户能访问的全部 Skill。
- max_members、max_concurrent_members、max_tasks、max_messages、预算和最长运行时间。
- max_pending_messages_per_member、max_message_bytes、max_coordinator_turns：每成员待投递消息上限、单条消息大小上限、协调者被唤醒的总轮数上限。超限返回类型化错误，不复用 ID，也不静默丢弃。
- result_schema、默认验收方式、可访问共享资料。

member_selection 与 member_creation 在后端保持正交，界面上合并成一个“允许协调者按需要补充成员”的勾选项：不勾选对应 explicit_only 加 disabled，勾选对应 coordinator_select 加 run_scoped；是否允许临时创建（只从 Agent 库里选人，还是也可以创建临时角色）放在模板的“范围与上限”里单独调整。preset_members 为空时必须勾选。“固定、混合、自动”只是这两样东西的三种取值，不是三套后端类型，也不作为名词出现在界面上（第 8.2 节）。

模板的策略可以被单次运行临时改写：用户在输入区的队伍选择器里改“允许补充成员”、用 @ 点名额外的 Agent，这些选择作为 team_request 随消息保存，只影响这一次运行，最终生效的值冻结在该运行的 policy_snapshot 里。

团队模板与 Agent 定义一样版本化：编辑产生新版本，存放在 team_definitions 与 team_definition_versions 中，用户可以对比和回退。每次 TeamRun 仍在 policy_snapshot 中冻结完整策略和成员配置，模板编辑只影响之后的运行，运行的可复现性不依赖模板历史。

### 6.3 TeamRun 与 TeamMember

TeamRun 绑定一次用户目标、一个根会话和一个项目，是用户在项目下新建的一个团队模式的会话。用户根会话可以顺序承载多次 TeamRun，但同时最多一个活动团队；同一个项目同时也最多一个活动团队，因为同项目的所有会话共用一个工作目录（第 1.4 节），两支团队并行写同一个目录会互相破坏。所有工作成员始终为本次运行创建独立会话。

存储上，一次 TeamRun 只有两样东西（第 11 节）：team_runs 里的一行，和 team_events 里属于它的一条事件流。成员、任务、消息都不是表，而是事件流回放出来的状态。运行依附于根会话：根会话删除，运行、事件流和成员会话一起删除。

成员的持久身份就是它的会话 ID，与 DSH 相同；alias 是给模型和界面看的不可变标签，运行内唯一，退役或失败后也不复用。协调者也是一个成员，role 为 coordinator，它的成员 ID 是根会话 ID。

每个成员由两类事件描述：

- 接纳事件（team.member.admitted），只写一次、内容不可变：成员 ID、role、alias、source、created_by；definition_id 与 version_id，临时成员这两项为空但内联定义快照必须完整；composition 快照、authority 快照、模型绑定、Skill 清单。
- 状态事件（team.member），每次变化写一条完整快照：membership_state、失败原因、当前 attempt、已看到的事件水位 last_seen_seq、等待登记 wait_after_seq 与 wait_deadline（第 10.3 节）。

协调者为任务临时创建的 Agent 因此只存在于这次运行的接纳事件里，不进入 Agent 库；用户可以把单个临时成员或整支团队另存（第 8.4 节）。

membership_state 使用 provisioning、active、retired、failed。运行中、空闲、等待、停止是执行状态，从 Driver、Inbox 和成员的等待登记派生，不混入成员是否属于团队的状态。

同一个定义可在同一团队启动多个实例，例如两个分析员分工处理不同资料。每个实例有各自的会话和别名。

成员默认保留独立上下文。显式交接才传递摘要或成果；不复制其他成员的原始 provider 历史、内部推理或凭据。

### 6.4 配置示例

下面是混合团队的目标配置示意。模型别名、Skill ID、工具 ID 均须在当前部署与用户目录真实存在，示例字符串不代表已经注册。

~~~json
{
  "schema_version": 1,
  "name": "通用调研与分析团队",
  "coordinator": {
    "agent_ref": "builtin:team-coordinator",
    "model": "model-reasoning"
  },
  "preset_members": [
    {
      "alias": "researcher",
      "agent_ref": "agent-user-research",
      "version_policy": "latest_at_run_start",
      "model_override": "model-balanced"
    },
    {
      "alias": "analyst",
      "agent_ref": "agent-user-analysis",
      "version_policy": "latest_at_run_start"
    }
  ],
  "policy": {
    "member_selection": "coordinator_select",
    "member_creation": "run_scoped",
    "allowed_models": ["model-reasoning", "model-balanced", "model-fast"],
    "delegable_tools": ["web_search", "web_fetch", "read"],
    "max_members": 8,
    "max_concurrent_members": 3,
    "max_tasks": 100,
    "max_messages": 300,
    "max_pending_messages_per_member": 32,
    "max_message_bytes": 32768,
    "max_coordinator_turns": 60,
    "budget_credits": "50.00",
    "max_wall_time_seconds": 7200
  }
}
~~~

示例容量是建议起始配置，需压测后确定部署默认值。max_members 包含协调者；max_concurrent_members 统计正在执行模型轮次的成员，包含正在推理的协调者，空闲和等待中的成员不计入。

并发默认取 3 有两个依据。第一，Driver 名额按用户计且默认 5（第 4.1 节），团队最多使用 max_concurrent_agents 减去保留余量（部署配置 team_reserved_agent_slots，默认 2）的名额，保证用户在团队运行期间还能正常聊天、定时任务还能启动；模板里配置得更大时按这个上界截断并在配置页提示。第二，Codex 的同类默认值是 3 个子 Agent（第 3.4 节）。部署另设全局上限 team_max_running_members，限制整个后端进程内同时执行的团队成员数，初始值由 M1 的容量测量确定。

### 6.5 创建 Agent：入口、AI 起草与生效

市面做法的对照见第 3.5 节。结论是 AI 负责写内容，人负责让它生效，授权不随定义发生。OpenBox 按这个原则提供三个入口，保存的都是第 6.1 节的同一种结构化定义。

| 入口 | 谁来写 | 生效前的人工步骤 |
|---|---|---|
| Agent 库页面（Web） | 用户填表；可由一句话描述起草，或复制已有定义 | 用户点保存，再点启用 |
| 普通聊天里让 AI 创建 | 聊天中的 Agent 调用 agent_manage 工具写出整份定义 | 用户在确认卡片上点一次；低风险的定义可以按用户偏好免确认 |
| 团队运行中协调者临时创建 | 协调者 | 不需要逐个确认，受团队策略和启动时的授权约束；只存在于那次运行里，要长期使用需另存（第 8.4 节） |

#### Agent 库页面

移动端可以使用已保存的 Agent 和团队模板，不提供表单编辑。起点有三个：空白表单；一句话描述，由 POST /api/agent-definitions/draft 调用一次模型，按结构化输出生成草稿填进表单，草稿不落库，起草时服务端先用现有的 Skill 检索找出与描述相关的 Skill 交给模型，让草稿里带上推荐的 Skills 和与之配套的工具预设；复制内置、legacy 或自己已有的定义。

主表单有六项。前四项必填：名称、职责描述、何时使用、角色指令。何时使用是写给协调者看的，协调者自动选人依据的就是它和职责描述，所以要求写清触发条件，并尽量附一两个示例任务。后两项决定这个 Agent 会什么、能做什么，放在主表单里而不是高级设置里，可以为空，但创建时一定会经过这一步：

- Skills：这个 Agent 掌握哪些做事的方法。候选清单与用户在技能中心看到的一致，也就是 skill 工具向该用户展示的同一份并集：平台内置的、从技能商店安装的、用户自己创建的、沙箱里已安装的。支持搜索，每项显示 Skill 的简介。有两种方式：只用所选（默认），成员只能看到并读取这里选中的 Skill；全部可用，成员像普通聊天里的 Agent 一样可以检索用户能访问的任何 Skill，适合通才型角色。对 OpenBox 的用户来说，一个 Agent 会不会做口播视频、会不会写某类文案，主要取决于它选了哪些 Skill。
- 工具：不让用户面对一长串工具名，提供几个预设，对应第 7.4 节的分层：只读调研（默认）、读写文件与命令、桌面与浏览器、可生成媒体。选了预设之后可以逐个增减。不可委派的工具不出现在列表里。

Skill 与工具分开选，但表单把两者联动起来，因为 Skill 只是方法，它提到的工具 Agent 必须另外拥有才用得了：选中一个 Skill 后，表单读取它仅作说明用的 allowed_tools 字段，列出“这个 Skill 通常要用到的工具”，缺少的可以一键加入，属于桌面类或付费类的带标记；其中不可委派的工具（例如发布）单独提示“这一步成员做不了，需要用户在普通会话里完成”。这个字段只用来提示和建议，不会因为选了 Skill 就自动加上工具，“Skill 不授予工具”的原则不变。

其余字段在高级设置里，都有默认值：

- 模型：默认不指定，运行时跟随团队模板或部署默认值；可以指定默认模型、锁定，或给出允许的模型范围。
- MCP：列出用户沙箱里当前配置的 MCP 服务，按服务勾选，可以再限定到其中某些工具；默认不选。保存时某个服务当前不在线只给提示，不拦截。
- 图标与颜色、示例任务、推理档位、输入输出结构、步数与时限：默认沿用部署值或留空。

#### 聊天里让 AI 创建

用户在普通会话里说“帮我建一个专门做小红书选题的 Agent”，聊天中的 Agent 就可以把它建出来。OpenBox 已有同类先例：聊天中的 Agent 现在就能用 skill_manage 创建用户的 Skill、用 cron 工具创建定时任务，用 creator_context 提议一条长期记忆并弹出确认卡片。agent_manage 沿用这些做法。

工具只提供给根会话里的 build Agent。它加入 BUILD_ONLY_WORKFLOW_TOOLS，Task 子会话、团队成员和定时任务的临时会话都不能调用。动作有四个：

| 动作 | 作用 |
|---|---|
| list、get | 读取用户已有 Agent 的摘要或详情。创建前先看一遍，避免重名和职责重叠，也用于向用户推荐现成的 Agent |
| create | 提交一份完整定义：名称、职责描述、何时使用、角色指令、示例任务、图标，以及 Skills、工具预设或工具清单、MCP 服务、输出结构。聊天中的 Agent 本来就有 skill_search，创建前应先检索用户有哪些相关 Skill 再决定选哪些；一次调用最多提议 4 个，对应“帮我建一组 Agent”的场景 |
| update | 对已有定义提交修改，产生一个草稿版本 |

AI 可以写的是内容。模型字段默认留空，只有用户在对话里明确点名某个模型时才填，多数产品同样不让 AI 替用户选模型，这也避免 AI 顺手选了最贵的那个。归档和删除不提供给 AI，由用户在 Agent 库里操作，skill_manage 也只有创建和导出。

服务端收到 create 后按下面的顺序处理：

1. 用本节后文“校验、状态与版本”所述的同一个编译器校验。不通过就把原因返回给模型，模型可以改正后重试，这时不打扰用户。
2. 通过后把定义保存为 draft，记录来源：created_via 为 chat，以及会话、消息和工具调用的 ID。draft 不能被模板引用，也不会出现在协调者的目录里，所以这一步没有任何效果外溢。
3. 立即弹出确认卡片，机制与记忆提议相同：写入一条提问检查点，本轮挂起并释放执行名额，用户回答后续跑。卡片内容由服务端根据校验后的数据生成，不采用模型的自由文本：名称、职责描述、何时使用、Skills（逐个列出名称与简介）、工具清单（桌面类和付费类带醒目标记）、模型、可展开的完整指令；所选 Skill 需要而定义里缺少的工具在卡片上提示。选项是“启用”“先存为草稿”“不要”，另有一个到 Agent 库表单的链接，供用户改完再启用。
4. 用户选启用，定义转为 active 并发布第 1 个版本；选存为草稿，保持 draft；选不要，定义归档；直接关掉卡片，定义保持 draft，工具结果告诉模型不要在这次对话里重复提议。update 走同样的卡片，卡片上显示改了什么，启用即发布新版本。
5. 卡片的文字部分是普通的提问文本，移动端和尚未实现专用渲染的客户端按普通提问卡片显示，同样可以回答。移动端因此能通过聊天创建 Agent，虽然它没有表单编辑页。

免确认是一个用户偏好，默认关闭。打开后，工具范围不超过 T0 层的定义由 AI 直接创建并启用，聊天里显示一条带“撤销”的通知；包含桌面类、付费类工具或任何 MCP 服务的定义无论偏好如何都要走确认卡片。第 3.5 节里没有哪家产品提供完全无人过目的持久 Agent 创建，OpenBox 自己的 Skill 和定时任务创建则是不确认的，这个偏好取两者之间：低风险的放开，涉及费用和外部操作的仍然要人看一眼。

此前担心的是提权：聊天中的 Agent 会读到网页和文件里的不可信内容，如果它能直接造出带一堆工具的 Agent，是否多了一条攻击路径。这个担心被下面三点限制住，所以提供这个入口：

- 定义不是授权。定义只能从用户本来就能委派的工具里选一个子集；成员实际能用什么，取决于用户启动团队时在确认页上批准的授权（第 7.3 节、第 7.6 节），付费工具还要单独的预授权和上限（第 7.4 节）。一份来路不明的定义在用户亲手把它放进团队并批准授权之前，什么都做不了。
- 用户没要求却弹出来的创建卡片，本身就是异常信号，用户点“不要”即可；来源记录让事后追查到是哪次对话、哪条消息触发的。
- 每个用户的定义总数、每次对话的提议次数有上限，被拒绝过的名称在同一对话里不再提议。

#### 校验、状态与版本

三个入口保存时用的是成员接纳时的同一个编译器（CompositionCompiler），所以在这里通过的配置，到了团队里不会因为同样的原因被拒绝：

- 模型在部署允许的范围内；锁定时必须给出默认模型；允许范围是部署范围的子集。
- 每个工具都是可委派的；不可委派或前提未满足的工具当场报出原因（TOOL_NOT_TEAM_READY 及其说明）。
- 每个 Skill 用户都能访问；所选 Skill 提到而定义里没有的工具作为提示返回，不算校验失败。
- 输出结构是合法的 JSON Schema；名称在该用户当前 workspace 的未归档定义里不重复；文本字段不超过长度上限：名称 40 字符，职责描述 500，何时使用 1000，角色指令 8192（与现有 task 工具的 persona 上限一致），示例任务最多 5 条。
- 编译同时产出能力摘要（实际模型范围、有效工具、Skills），保存在版本里，供列表展示和协调者检索使用。

创建 Agent 不会带来新的能力。缺少数据连接或工具时，保存页或工具结果直接提示能力缺失。角色指令只作为平台成员提示词里的一个角色段落出现，不能覆盖平台身份、团队协议和工具执行检查。

定义的状态有 draft、active、archived。版本另有草稿与已发布之分：编辑 active 的定义时，改动先进入一个草稿版本，可以单独试运行，点发布后才成为当前版本；正在运行的成员始终使用各自接纳时的快照。各家的图形界面产品都是草稿与发布分开、线上版本在发布之前不变，这里采用同样的做法。归档后不能再建新实例，已有运行不受影响。

#### 试运行

试运行是用指定的定义版本开一个单 Agent 的普通会话，用户直接和它对话来验证效果，对应各家产品的预览或测试窗格。会话的 agent 字段指向这个定义版本；run_loop 启动时由授权加载点把该版本编译成冻结预设并绑定到上下文，get_agent 本来就优先读取上下文里的冻结预设，所以不需要把用户定义写进全局注册表。试运行会话没有 parent_id，是交互式的：提问、审批、通知都按普通会话处理，用量正常计费。试运行页同时显示编译结果，即实际生效的模型、工具和 Skills，让用户看到的和团队里成员拿到的是同一份东西。示例任务在这里作为一键填入的起始输入。

<a id="capabilities"></a>
## 7. 模型、Skills 与工具授权

### 7.1 模型解析

先验证用户锁定和允许集合，再按以下顺序解析：

1. 本次成员创建请求的显式覆盖。
2. 团队模板对该成员的覆盖。
3. Agent 定义默认模型。
4. workspace / 部署默认模型。

model_locked=true 时，只接受锁定值；冲突返回 MODEL_LOCKED。未锁定也只能选择 Agent、团队和部署三者允许集合内的模型。

允许集合在创建 TeamRun 时冻结进 policy_snapshot，之后部署新增模型不会扩大正在运行的团队的选择面。候选模型不写进工具 schema 和 system prompt：协调者通过 team_view 读取，MODEL_LOCKED、MODEL_NOT_ALLOWED 的错误体也携带当前允许列表，模型可以据此直接改正。校验发生在成员接纳时，不留到成员的第一次模型请求。

协调者的模型不自动覆盖其他成员。模型信息至少绑定 provider_id、model_id、route/config revision、reasoning、capability_snapshot 和配置摘要；凭据通过已有安全引用实时解析，不将密钥写入快照。

成员接纳前验证工具调用、结构化输出、图片等本次任务所需能力。工具用于生成图片/视频的服务模型，与 Agent 的对话模型分别配置，不能混用字段。

默认不静默替换已启动成员的模型。模型下线或授权撤销时成员进入受阻状态；选择新模型会创建替代成员和新的 task attempt，并保留原配置及结果。

### 7.2 Skills 绑定

- 每个成员的 Skills 在创建它时选定（第 6.5 节）：用户 Agent 来自定义里的 skill_mode 与 skill_refs，团队模板可以为某个成员追加，协调者创建临时成员时在团队策略的 allowed_skills 范围内为它挑选。
- 保存选中的 Skill 来源、版本/内容摘要以及授权读取范围，记录在成员接纳事件的 Skill 清单里。
- 运行时通过成员专属 Skill 目录过滤器和已有 Skill 工具渐进读取：selected 方式下 skill 与 skill_search 只看得到清单里的 Skill；all_accessible 方式下范围是用户能访问的全部 Skill。
- 清单里的 Skill 在成员接纳时生成不可变内容引用（all_accessible 方式下检索到的其他 Skill 在首次读取时生成）：SKILL.md 的正文按内容摘要存入现有的 blob 存储，清单里记录摘要。成员此后读到的始终是这份内容，Skill 在运行中被编辑不影响已接纳的成员，新运行才采用新内容。
- 附属资源（脚本、参考资料）来自多个 provider，其中一部分存放在沙箱或云桌面上，后端无法在接纳时一次性取全。它们在成员第一次通过 Skill 工具读取时按摘要保存快照，之后读取同一资源都返回这份快照。某个资源在首次读取前已被删除或无法读取时明确报错，不悄悄换用其他版本。
- 快照层放在 Skill 工具的读取路径上，因为成员读取 Skill 内容都经过这个工具；成员用 bash 直接执行云桌面上的脚本不经过快照层，这一点在成员的提示协议和 Skill 清单里注明。
- Skill 的工具名称只是使用说明，实际工具集合始终由能力编译和权限检查决定。

### 7.3 团队授权与成员工具可见性

需要区分协调者当前可见的工具和用户允许团队委派的能力。

例如协调者只需团队工具、只读检查工具和 question（第 8.1 节），专业成员可以持有用户已授权的数据查询工具。协调者不必在自身模型上下文里加载全部专业工具 schema。

新增 TeamRunGrant，由用户启动团队的认证请求及已有授权规则在服务端生成。它包含两部分：可委派给成员的工具集合，以及用户在组队方案卡上预先批准的权限模式，后者用于成员的非交互权限解析（第 7.6 节）。权限计算为：

~~~text
成员可用工具 =
部署允许的团队工具
∩ 用户当前可用工具
∩ TeamRunGrant 可委派工具
∩ Agent 定义工具范围
∩ 本次成员请求范围

操作是否可执行 =
上述工具存在
且现有 permission/guard/account/resource 检查全部通过
且当前 Driver generation 有效
且预算与资源占用条件满足
~~~

上式里的“工具”包含平台工具、平台插件工具和 MCP 工具三种；MCP 工具在每一层都按服务名与工具名模式参与交集。Skills 不在这条式子里：Skill 是否可读由用户对该 Skill 的访问权决定，读到的内容不改变工具集合。

TeamRunGrant 不是模型可编辑字段。协调者创建成员只能请求授权子集；成员重启时重新校验当前有效权限，并与原始快照取交集。用户后续明确扩权时生成新授权版本、新成员或新运行，不在存量快照上悄悄扩大权限。

### 7.4 现有专业工具委派限制的改造

保留旧 Task 的 BUILD_ONLY_WORKFLOW_TOOLS 拒绝规则。该集合（[agent.py](/Users/wang/workspace/OpenBox/backend/agent/agent.py)）当前包含 image_gen、video_generate、video_transcribe、video_compose、video_analyze、hot_trends、creator_context、skill_manage、douyin_publish、desktop_publish、autopilot_run、desktop_login，代码注释把它称为“子 Agent 不接触媒体工具”的结构性保证。也就是说，被挡在子会话之外的正是付费生成、发布和登录这些 OpenBox 的核心业务工具，团队要用它们必须逐级补齐前提。

为团队能力编译增加服务端 ToolDelegationPolicy：

- team_allowed：是否允许在团队成员运行。
- required_guards：现有权限、付费、账号、资源校验要求。
- exclusive_group：空表示可并行；desktop 表示占用 workspace 唯一的云桌面和浏览器（第 7.5 节）。
- effect_adapter：产生外部效果时使用的现有恢复适配器。

工具分三层开放，三层都属于交付范围。分层只表示各层的前提不同、因而落在不同的里程碑；某一层的前提尚未实现时，该层工具对成员返回 TOOL_NOT_TEAM_READY。不能删除整个旧黑名单后把所有工具默认放开。

| 层级 | 工具 | 开放前提 | 里程碑 |
|---|---|---|---|
| T0 知识与文件类 | read、glob、grep、view_image、web_search、web_fetch、todo_write、todo_read、skill、skill_search、creator_context 的只读动作，以及 write、edit、multiedit、apply_patch、bash。能力面与现有 general 子 Agent 相当，工作目录是共享的项目目录 | 成员绑定与非交互权限解析（第 7.6 节）；共享目录的写入约定（第 7.5 节） | M1 |
| T1 桌面与浏览器类 | browser_mode、computer 等桌面与浏览器工具；hot_trends，它读的是每日共享缓存，但缓存未命中时会在云桌面浏览器里采集 | exclusive_group 调度（第 7.5 节） | M3 |
| T2 付费媒体类 | image_gen、video_generate、video_transcribe、video_compose、video_analyze | 启动团队时的付费预授权、严格预留（第 10.6 节）、外部效果或 Job 与 attempt 的关联 | M3 |
| MCP 工具 | 用户沙箱或云桌面上的 MCP 服务提供的工具与资源 | 定义或成员配置里按服务名显式引用；组队方案卡逐个列出这些 MCP 服务并由用户批准；成员侧按模式而不是按固定工具 ID 做限制（见下） | M3 |
| 平台插件工具 | 管理员安装的进程内插件提供的工具 | 管理员在插件清单里为每个工具标明可委派及其 exclusive_group；未标明的一律不可委派 | M3 |
| 不委派 | douyin_publish、desktop_publish、desktop_login、desktop_takeover、skill_manage、agent_manage、autopilot_run、cron、task、plan_enter、question、creator_context 的 propose_memory 动作，以及全部团队管理工具 | 见下 | 不适用 |

付费工具在普通会话里依靠用户逐次确认费用。团队成员是非交互的，无人逐次点确认，[Autopilot 计划](/Users/wang/workspace/OpenBox/docs/AUTO_MARKETING_AUTOPILOT_PLAN.md) 对同一问题的结论是必须在创建时完成预算授权。团队沿用这个结论：组队方案卡的“可以做”一行列出本次运行允许成员使用的付费工具及其单次上限和总上限，取值来自团队模板的“范围与上限”；自动组队默认不含付费工具，用户可以在卡片的回答框里要求放开，提议方据此重新出卡。用户点“开始”即完成预授权，这些进入 TeamRunGrant。成员调用付费工具时先在运行行锁内做严格预留，超出预授权的调用得到 PERMISSION_REQUIRES_USER，走第 7.6 节的上报路径由用户追加授权。没有预授权任何付费工具的团队，成员看不到 T2 层工具。

MCP 工具需要单独说明，因为它与内置工具有三点不同。第一，目录是动态的：现有子会话的工具边界是接纳时冻结的一组工具 ID，MCP 工具不能这样冻结，成员快照里保存的是服务名和工具名模式，每一步从沙箱读到当前目录后按模式取交集；服务暂时不可用时成员少几个工具继续运行并留一条团队事件，这与现有“沙箱没有 MCP 是常态，不因此让运行失败”的处理一致。第二，MCP 工具超过 40 个时模型看到的是“查找”和“调用”两个元工具，调用元工具可以调到任何 MCP 工具，所以模式限制必须在元工具的执行路径里再检查一次，不能只过滤工具列表。第三，MCP 工具是第三方代码，副作用和费用平台无从得知：它们不进任何工具预设，必须由用户按服务名显式加入；含 MCP 服务的定义由 AI 在聊天里创建时一律走确认卡片，不适用免确认偏好（第 6.5 节）。协调者不持有 MCP 工具。

“不委派”是逐个审核后的结论，不是未完成的工作：发布和登录的外部效果不可逆且涉及平台账号风控，必须有用户在场；desktop_takeover、question 和 creator_context 的 propose_memory 的含义就是等待用户；skill_manage、agent_manage、autopilot_run、cron 会改变用户的长期配置，其中 agent_manage 还要向用户弹确认卡片；task 和团队管理工具会造成嵌套。团队产出的成片和发布方案由用户在普通会话里确认发布，现有的发布确认流程不变。

协调者通过团队专用工具组织成员，它自己的工具集见第 8.1 节。

### 7.5 共享资源与外部操作

不同上下文不等于不同文件系统或浏览器。先列出现有事实，再给规则。

| 现有事实 | 位置 |
|---|---|
| 同一项目的会话共用一个工作目录；同一 workspace 共用一个容器或云桌面 | [manager.py](/Users/wang/workspace/OpenBox/backend/sandbox/manager.py)、[workspace.py](/Users/wang/workspace/OpenBox/backend/project/workspace.py) |
| write、edit 在写入前比对上次读取时记录的 mtime，文件被别人改过就拒绝；bash、格式化工具和生成器不经过这项检查 | [filetime.py](/Users/wang/workspace/OpenBox/backend/tool/filetime.py) |
| 逐步快照和 revert 作用于整个项目目录，不区分会话；同一项目的快照命令在进程内串行 | [snapshot.py](/Users/wang/workspace/OpenBox/backend/snapshot/snapshot.py) |
| 云桌面操作由 Action Server 上的桌面租约按单次工具调用串行，跨会话、跨后端进程生效；默认等待 90 秒、租期 180 秒 | [SandboxClient.desktop_lease](/Users/wang/workspace/OpenBox/backend/sandbox/client.py) |
| 每个沙箱请求都会校验当前 Driver 租约，旧代次的请求被拒绝 | 同文件的 _authorize_request |

规则：

1. 只读查询及独立模型推理可并行。
2. 成员把产出写到约定的子目录 .openbox/team/{run_id}/{alias}/。成员在同一文件系统上，同伴交接直接传路径，不为交接复制文件。成员提交任务时，交付物登记为 FileAsset 并记录内容摘要，用于冻结版本和用户侧的预览下载。
3. 任务可以声明预期写入范围。范围只是提示：创建或派发任务时如果与运行中的任务重叠，向协调者返回警告，不阻止执行。需要先后顺序的写入用任务依赖表达。写入冲突的最后一道检查是现有的 mtime 拒绝，成员被拒绝后重新读取再修改。
4. 桌面与浏览器类工具的 exclusive_group 为 desktop。调度器保证同一 workspace 内所有团队运行中，同时最多一个该分组的 attempt 处于 running。这是任务级的独占，防止两个成员的多步浏览器流程交错；物理互斥仍由桌面租约负责。同一 workspace 的普通会话和定时任务仍可能在两次调用之间用到桌面，这是现有行为，团队不加重也不解决它。成员等待桌面租约超时后收到 RESOURCE_BUSY，应报告受阻，不循环重试。
5. 活动 TeamRun 期间，该项目内所有会话的 revert、unrevert 返回 TEAM_ACTIVE，包括同项目下与团队无关的普通会话，因为 revert 会重置整个项目目录，抹掉成员的产出。成员会话默认不做逐步快照：它们没有 revert 入口，逐步快照还会让所有成员的每一步排队等同一个项目的 git 存储。TeamRun 在启动和终态各记一次快照，团队卡片展示这两次之间的差异。M1 需要实测三个成员并发时的快照等待来确认这个默认值。
6. 产生外部效果的工具（T2 层）把 ExternalEffect 或各自的 Job 记录关联到 task attempt；旧远程进程仍可能运行时任务保持受阻，核对停止或外部状态后再转交。

范围声明本身不能证明沙箱隔离。未被拦截的外部写入无法获得强一致保证。不建通用的资源占用表：账号、文档、路径这类资源在工具层没有强制点，登记一张占用表只会给出虚假的互斥保证（第 3.1 节 DSH 的同一结论）。

### 7.6 成员的非交互运行

现有代码已经规定子会话不面向用户：

- 带 parent_id 的非 cron 会话调用提问工具会被拒绝，错误信息要求把澄清交回父 Agent（[question.py](/Users/wang/workspace/OpenBox/backend/question/question.py) 的 ask）。
- 带 parent_id 的会话不产生任务完成、等待输入、等待审批的站内通知和推送（[notifications/events.py](/Users/wang/workspace/OpenBox/backend/notifications/events.py)）。
- 权限审批在进程内阻塞等待，待审批请求保存在进程内字典里，可选经 Redis 转发，键的有效期 300 秒（[permission.py](/Users/wang/workspace/OpenBox/backend/permission/permission.py) 的 ask）。成员会话一旦进入这条路径，不会有站内通知和推送；Web 端按 session_id 归档待审批项，用户只有恰好打开那个成员会话才看得到。其间成员占着执行名额空等，从外面看不出它与正常工作的成员有何区别。DSH 遇到过同样的问题并选择了钉定拒绝（第 3.1 节）。

成员会话的 parent_id 指向协调者会话，前两条自动生效，不需要新代码。据此确定：

1. 成员不获得 question 工具。需要澄清时给协调者发 question 类消息；协调者能答则答，不能答则在根会话用现有提问卡片问用户。根会话没有 parent_id，提问、通知和移动端展示都是现成的。
2. 成员的权限解析增加非交互模式：规则结果为 allow 或 deny 时行为不变；结果为 ask 时先查 TeamRunGrant 中用户在启动团队时预先批准的工具与模式，命中则放行并留审计，未命中则确定性拒绝并返回 PERMISSION_REQUIRES_USER，不进入阻塞等待。
3. 成员收到 PERMISSION_REQUIRES_USER 后把任务置为 blocked 并说明需要的授权。协调者向用户确认；用户同意则生成新的授权版本，对后续调用生效，不回溯改写已冻结的快照。
4. 成员的提示协议写明：权限范围在启动时固定，被拒绝的操作不要重试，应上报限制。
5. 启动团队的确认界面列出将预先批准的工具和模式（例如项目目录内的写入、bash 的常用只读命令），以及允许成员使用的付费工具和各自的金额上限（第 7.4 节），让用户一次看清团队的权限面。

用户消息中的已有授权继续有效。同伴消息不能替用户作出授权。

<a id="orchestration"></a>
## 8. 协调者、发起入口与组队流程

### 8.1 协调者工具与职责

协调者使用目录搜索、成员创建、消息、任务管理和团队结束工具。默认流程：

1. 明确目标、输入与交付标准。
2. 检查预设成员与可访问 Agent 库。
3. 为角色缺口选择已有 Agent；允许时创建临时定义。临时定义同样包含四样：角色指令、Skills、工具、模型，其中 Skills 由协调者用 skill_search 在允许范围内挑选，工具按所选 Skill 的需要和任务性质在可委派范围内选取。
4. 生成任务及依赖图，为每个任务指定负责人、是否为交付物以及验收方式。
5. 调用 team_wait 结束本轮。依赖已满足的任务由调度器派发并唤醒负责人，协调者不需要逐个启动（第 9.4 节）。
6. 被唤醒后阅读变化摘要，处理成员的求助、受阻和失败；调整未开始的任务。
7. 验收交付物任务，必要时发起返工、重试或重新打开。
8. 确认所有交付物任务完成，再提交团队完成命令。

创建临时 Agent 的行为限于配置组合。生成新 Skill、安装插件、接入新账号或编写新工具是独立操作，遵守已有产品授权流程。

协调者的工具集固定为三部分：团队工具（第 12.2 节）、只读检查工具（read、glob、grep、view_image、web_fetch、skill_search），以及 question。skill_search 只返回 Skill 的名称和简介，范围限于团队策略的 allowed_skills，协调者用它为临时成员挑选 Skill；协调者自己不加载 Skill 正文。它没有 write、edit、bash 和任何业务工具。只读工具让它能查看用户给的资料和成员的产出；question 让它成为团队唯一面向用户的出口（第 7.6 节）；不给执行类工具是为了防止它自己把活干了而不委派，这既会撑大协调者的上下文，也会让它在长工具调用期间无法响应成员。

### 8.2 发起入口与组队确认

三种成员组合（第 1.1 节）不是用户要先选的“模式”。用户面对的只有两样东西：一份成员名单，和一个“允许协调者按需要补充成员”的开关。名单来自团队模板、来自用户在输入框里用 @ 点名的 Agent，也可以是空的；开关关着就只用名单里的人，开着协调者就可以从用户的 Agent 库里再选人或临时创建角色；名单为空且开关打开就是全自动。三种组合由这两样东西自然得出，界面上不出现“固定、混合、自动”这组名词。开关对应 member_selection 与 member_creation 两个策略字段（第 6.2 节），后端仍然保持两个维度正交。

团队是普通会话的一种模式，不是另一个产品。发起入口有三个，走到同一张组队方案卡：

| 入口 | 用户动作 | 到达方案卡之前发生的事 |
|---|---|---|
| 输入区的团队模式 | 在模式选择器里选“团队”，旁边出现“用哪支队伍”的选择器：自动组队，或某个团队模板，外加“允许协调者补充成员”的开关（默认取模板的设置，本次运行可以临时改）。在 @ 菜单里点名 Agent，被点名的一定在队里；@ 一个团队模板等于在选择器里选中它 | 消息以 agent=team 发出，团队选项作为该用户消息上的一个 team_request part 保存，不改 sessions 表。协调者读取目标和 team_request，检索 Agent 库，调用 team_propose |
| 团队模板上的“运行” | 在“团队模板”页签点运行，选项目（与定时任务“对话创建”的弹窗相同），进入一个新会话，团队模式和这个模板已经选好，光标停在输入框里 | 之后与第一个入口完全相同 |
| 普通对话里由 AI 提议 | 用户什么都不用选。build Agent 判断任务由几块互不依赖的工作组成、值得分工时，调用 team_propose | build Agent 用 agent_manage 的 list 查看用户已有的 Agent，给出阵容、理由和预计的时间与费用。它不能自己切换模式，做法与 plan_enter 相同 |

在非团队模式下 @ 一个 Agent，输入区会切到团队模式并把它列为点名成员，用户可以手动切回。用户 Agent 只以团队成员的身份运行，一个成员的团队是合法的；不为“直接和某个自定义 Agent 对话”另开一条运行路径，编辑页里的试运行除外（第 6.5 节）。

~~~mermaid
flowchart TD
    E1["输入区选团队模式并发送"] --> Coord["协调者分析目标，检索 Agent 库"]
    E2["模板页点运行，选项目"] --> E1
    E3["普通对话：build Agent 认为值得分工"] --> Propose
    Coord --> Propose["team_propose：阵容、各自职责、上限、预计"]
    Propose --> Check{"服务端校验：与成员接纳同一套编译"}
    Check -->|"不通过"| Fix["错误返回给模型，不打扰用户"]
    Fix --> Propose
    Check -->|"通过"| Card["组队方案卡：提问卡片加 team_lineup 详情"]
    Card -->|"开始"| Create["同一事务：创建 TeamRun、冻结策略与授权、接纳成员、会话切到 team"]
    Card -->|"在回答框里写调整意见"| Coord2["意见作为工具结果返回，提议方修改后重新提议"]
    Coord2 --> Propose
    Card -->|"不用团队，你自己做 / 跳过"| Solo["会话回到 build，由它独自完成；本次对话不再提议"]
    Create --> Tasks["协调者创建任务与依赖"]
    Tasks --> Execute["统一调度、消息和成果协议"]
    Execute --> Review["协调者验收"]
    Review -->|"需要返工"| Tasks
    Review -->|"全部满足"| Done["团队完成，会话模式回到 build"]
~~~

组队方案卡是现有提问卡片的一种详情内容（第 13.2 节），不是新的弹窗或新的卡片体系。team_propose 的实现与 plan_enter、creator_context 的 propose_memory 同构：

- 参数：目标摘要；提议理由与预计的时间、费用倍数（AI 提议时必填）；成员列表，每项是 Agent 定义引用或内联临时定义，加上别名和这次任务里的职责；上限（预算、时长、并发）取模板或平台默认，提议方只能调低。
- 校验：与 team_member_start 走同一套编译（模型、Skills、工具、授权范围，第 7 节），并检查 team_request 的约束——被点名的 Agent 必须在阵容里；开关关闭时阵容必须是模板名单的子集；名单为空时开关必须打开。校验不通过时错误返回给模型，附可选的修正方向，不向用户出卡。
- 出卡：question.ask，detail.kind 为 team_lineup，continuation 的 kind 为 team_lineup，并携带提议内容的摘要值。卡片选项为“开始”和“不用团队，你自己做”，保留提问卡片自带的自由回答框：用户写“去掉合规审校，预算改成 30”，这句话作为答案回到提议方，它修改后再次调用 team_propose，出一张新卡。
- 生效：用户选“开始”时，continuation 在一个事务里创建 TeamRun（team.run.created、policy_snapshot、TeamRunGrant）、写入各成员的接纳事件、把会话的 agent 设为 team 并写一条 synthetic 用户消息（与 plan_enter 切到 plan 的写法相同）。应用前核对摘要值，提议在用户回答前被改过则要求重新确认。成员会话的创建仍按第 10.2 节异步完成。
- 拒绝：选“不用团队”或跳过时不创建任何团队数据；会话在 team 模式下则切回 build 并写一条 synthetic 消息说明由它独自完成。工具结果要求模型在这次对话里不再提议，做法与 propose_memory 被忽略后的处理相同。

team_propose 的可用范围按 plan_enter 的现有做法控制：默认权限规则为 deny（[loop.py](/Users/wang/workspace/OpenBox/backend/agent/loop.py) 的默认规则表），只有内置的 build 和 team 两个 Agent 显式 allow；协调者在已有活动运行时只能以 amend 方式调用。Task 子会话、团队成员都带 parent_id，提问本身就会被拒绝（第 7.6 节），定时任务的临时会话不提供这个工具。build Agent 的工具说明写明何时该提议、何时不该提议：只有任务能拆成至少两块互不依赖、各自有明确产出的工作时才提议；单步任务、用户明确要求立刻动手、或者瓶颈是同一台云桌面（第 1.4 节）时不提议。是否值得多花费用由用户在卡片上决定。

运行开始之后阵容仍然可以变：

- 开关打开时，协调者用 team_member_start 直接补充成员，不再逐个询问用户——用户在方案卡上已经同意了这条规则。每次补充在根会话里落一条分隔线（“协调者新增了临时成员‘数据核查员’”），右侧阵容图上同时多一个点。
- 开关关闭时，team_member_start 对名单之外的成员返回类型化错误。协调者确实需要加人时再次调用 team_propose，mode 为 amend，用户在同一种卡片上确认，授权随之生成新版本（第 7.3 节）。

### 8.3 防止无效协作

- 每次新增成员必须说明其职责、待处理任务和所需能力；没有任务用途的成员不自动启动模型。
- 能独立执行的任务才并行，依赖关系由任务图表达。
- 默认优先复用本次运行中空闲且能力匹配的成员。
- 自动重规划受最大任务数、成员数、消息数、时间和预算约束。
- 同一问题反复互发消息达到策略上限后交回协调者，避免无效对话持续消耗模型。

停滞判定由服务端按确定性规则完成，不交给模型判断。规则只看持久状态，初始阈值如下，随评估调整：

| 情形 | 判定 | 处理 |
|---|---|---|
| 无人能推进 | 没有成员在执行或接纳中，没有未投递的输入，没有关联的后台 Job，没有待用户回答的问题，但仍有未终态的必需任务 | 立即唤醒协调者一次并说明原因；协调者下一轮结束后仍是这种状态，则暂停运行并通知用户 |
| 长时间无进展 | 10 分钟内没有任务状态变化，也没有新成果，同时有成员在执行 | 唤醒协调者并附停滞成员的清单 |
| 两个成员来回消息 | 同一对成员围绕同一任务连续互发 6 条消息而任务状态没有变化 | 后续消息照常投递，同时向协调者发一条注意事件 |
| 下游被卡住 | 上游任务进入 failed、canceled 或 outcome_unknown，使下游永远无法就绪 | 立即唤醒协调者并附受影响的任务清单，由它重试、替换或取消 |
| 协调者轮数用尽 | 达到 max_coordinator_turns | 暂停运行并通知用户，可追加轮数或结束 |
| 超过最长运行时间或预算 | 达到 max_wall_time_seconds 或预算上限 | 暂停并通知用户，不直接判失败，已完成的工作保留 |

“最后一次进展”的时间取自团队事件，与 Symphony 用最后事件时间判停滞的做法一致（第 3.4 节）。

### 8.4 用户介入与保存

用户介入都发生在团队对话里：向协调者补充要求就是在输入框里直接发消息，指定成员用 @ 点名，调整阵容在组队方案卡的回答框里说；临时角色的配置在右侧团队页签的成员详情里查看，暂停与取消在团队进度卡和团队页签上。用户输入属于用户来源，同伴输入属于团队来源，二者在模型历史和执行权限上保持区别。

协调者为任务自主组建的团队只存在于这次运行里（第 6.3 节）。要复用它，有两个另存动作：

- 保存单个临时 Agent 到 Agent 库：只复制角色定义与用户选择的配置，不复制团队会话、任务记录、运行授权或其他成员数据。保存操作生成新的用户 Agent 版本和来源关联。
- 把整支团队另存为模板：临时成员逐个存入 Agent 库（用户可以取消勾选其中某些），来自 Agent 库的成员直接引用原定义，再生成一条团队模板，预设成员指向这些定义，策略取自本次运行的 policy_snapshot。用户下次可以直接运行这个模板，得到一支固定团队。

删除遵循“运行依附于根会话”：团队处于活动状态时，根会话和所在项目都不能删除，需要先取消运行，现有的项目删除本来就会检查活动会话。运行结束后删除根会话，运行记录、事件流和成员会话一并删除。Agent 库和团队模板在 workspace 层，不受会话和项目删除的影响，另存出去的定义也不会因为来源运行被删除而失效。

根会话聊天框上的“停止”在活动 TeamRun 期间等同于暂停团队：中止协调者当前轮次，停止新的派发，运行中的成员在安全边界停下。用户按停止时预期的是“都停下来”，而暂停可以继续、不丢工作；取消是团队进度卡和团队页签上单独的显式操作。只中止协调者而让成员继续运行的状态不对用户暴露。

### 8.5 模型协议的容错

持久化和授权可以由服务端保证，模型是否按协议调用工具不能。实际运行中最常见的偏差是成员做完事直接用一段话收尾而不提交、协调者布置完任务直接结束本轮而不等待、同一个调用被模型重复下发。协议按“模型少做一步也不会卡死”来设计：

| 偏差 | 服务端行为 |
|---|---|
| 协调者本轮结束时没有调用 team_wait 或 team_finish | 视为隐式等待：以它最后看到的事件序号登记等待，后续按第 8.6 节唤醒。team_wait 只是带超时提示的显式写法 |
| 成员本轮正常结束时名下有 running 的 attempt，既没有提交也没有 team_wait | 先催一次：投递一条服务端消息，要求提交结果、报告受阻或说明在等什么。再次无动作地结束，则把它最后一条回复作为结果自动提交，标记 implicit，进入验收。隐式提交的任务不走自动验收，一律交协调者验收。被暂停或中断的轮次不触发；因模型错误或步数上限结束的轮次按 attempt 失败处理，由协调者决定重试 |
| 成员在 team_wait 中，但没有任何在途工作能产生它等待的变化 | 不进入等待，立即返回 no_progress 和当前状况，让模型换一种做法（DSH 的做法，第 3.1 节） |
| team_member_start 重复下发 | alias 在运行内唯一：同别名且配置摘要相同则返回已有成员，配置不同则返回 TEAM_MEMBER_ALIAS_TAKEN |
| 基于过期视图的任务修改 | 返回 STALE_REVISION，错误体带该任务的最新内容，模型无需再查一次 |
| 成员被拒绝后反复重试同一个越权操作 | 沿用现有 doom loop 检测；提示协议要求上报而不是重试（第 7.6 节） |
| 成员向不存在或已退役的成员发消息 | 返回 TEAM_MEMBER_NOT_FOUND 并附当前名册 |

幂等键解决的是传输层重试：同一次工具调用被恢复流程重放时，事件键取自该工具调用已持久化的 part_id，与 QuestionCheckpoint 以 part_id 唯一的现有做法一致；事件流上“运行内事件键唯一”的约束保证重放不会追加第二份事件，命令结果从已有事件还原（第 10.1 节）。模型重新生成的调用是一次新的工具调用、新的 part_id，幂等键对它无效，靠上表的别名唯一和任务板可见性兜底。

### 8.6 唤醒的经济性

DSH 的等待在进程内完成，醒来只是同一轮里的下一步。OpenBox 的成员等待时结束本轮并释放执行名额，每次唤醒都是一次带完整上下文的模型调用，协调者尤其如此。唤醒规则因此要克制：

1. 只有值得唤醒的事件才会启动空闲成员的新一轮。对协调者：任务进入 review、failed、blocked、outcome_unknown，成员接纳失败，发给它的非 progress 消息，第 8.3 节的停滞判定，用户输入。对工作成员：派给它的任务，发给它的非 progress 消息，返工和控制指令。progress 类消息只落库并在面板显示，不唤醒任何人；成员下次醒来时通过 team_view 看到。
2. 合并唤醒：同一成员同时最多有一条未领取的唤醒输入。第一条值得唤醒的事件出现后等待 3 秒的合并窗口再生成输入，窗口内的其他事件并入同一条摘要。
3. 唤醒输入是一段紧凑摘要，列出自上次以来的变化（哪些任务到了什么状态、谁发来了什么），不是事件原文的堆叠。成员醒来后按需调用 team_view 读取权威状态，与 DSH“等待只报告是否有变化，调用方重新读取”的约定一致。
4. 验收默认不逐个唤醒协调者：中间任务通过契约校验即自动通过，只有标记为交付物的任务由协调者验收（第 9.4 节）。
5. 协调者的总轮数受 max_coordinator_turns 约束；每轮的输入摘要和 team_view 输出都有长度上限。
6. 团队的成本观测至少按“协调者开销、成员有效工作、催促与重试”三类拆分，评估据此判断协调开销是否合理（第 16.3 节）。

来自同伴和服务端的输入在成员历史中以带信封的消息呈现，例如“[团队消息 tm_012，来自 researcher]”，并在消息元数据里记录来源和发送者。它们在界面上不显示为用户本人发言，也不进入用户的输入历史、标题生成和推送。Codex 把同伴邮件记为 assistant 角色的信封，目的同样是不让它带上用户权威（第 3.4 节）；OpenBox 的 Inbox 目前只会落成普通用户消息（第 4.1 节），这是 M0 需要补的差异。

<a id="collaboration"></a>
## 9. 通信、任务板与成果交接

### 9.1 消息信封

~~~json
{
  "schema_version": 1,
  "message_id": "tm_001",
  "team_run_id": "tr_001",
  "from_member_id": "member_research",
  "to_member_id": "member_analysis",
  "kind": "question",
  "task_id": "task_analysis",
  "task_attempt_id": "attempt_01",
  "reply_to_message_id": null,
  "causation_id": "command_001",
  "body": "资料已整理，请确认还缺少哪些字段。",
  "artifact_refs": ["artifact_sources_v1"]
}
~~~

from_member_id、team_run_id 和执行身份由服务端 ToolContext 解析。公开 API 不能通过这些字段冒充成员；API 用户命令使用独立的 actor_type=user。

kind 包括 message、question、answer、handoff、progress、result、control。control 由授权的协调者或服务端生成，不能仅根据消息正文执行控制操作。

首版单条消息只指定一个接收者。广播由协调者显式指定目标集合，服务端展开为独立交付记录，各自去重和回执。所有成员共享可访问任务摘要；完整角色提示词、私人知识和凭据不随名册公开。

### 9.2 投递时序

~~~mermaid
sequenceDiagram
    participant A as 发送成员
    participant S as TeamService
    participant DB as 业务数据库
    participant I as AgentInbox
    participant B as 接收成员
    A->>S: team_message_send
    S->>S: 校验身份、成员关系、配额
    S->>DB: 提交消息与团队事件
    S-->>A: 返回消息 ID 和 queued
    S->>I: 使用稳定来源 ID 接纳输入
    I->>DB: 保存 Inbox 接纳记录
    S->>DB: 记录 delivered 回执
    I->>B: 空闲时唤醒或在下一安全边界注入
    B->>S: 回复或更新任务
~~~

消息状态使用 queued、delivered、recorded、canceled、failed。delivered 只表示目标 Inbox 已持久接纳，不表示模型已阅读、任务已处理或已回复；recorded 只用于不投递的 progress 消息。消费进度通过关联的 Inbox 和执行记录观察。

queued 消息由调度的补投步骤持续重试。目标已接纳但回执未写入时，通过确定性的 client_id 去重并补回执。跨进程重试可能发生，效果不依靠消息正文中的“请勿重复”保证。

运行中的成员在下一安全步骤边界接收消息，不并发修改正在发送给 provider 的请求。空闲成员冷唤醒；暂停团队只接纳消息而不发起模型运行。

发送方不能选择投递方式。对应到现有 Inbox：目标正在运行时用 steer 在下一步边界注入，目标空闲时用 followup 启动新一轮。inject 虽然不会唤醒空闲会话，但会让消息在目标的 Inbox 里堆积，并在它下次醒来时一次性灌进上下文，团队不使用。唯一的例外由 kind 决定而不是由调用方决定：progress 类消息不进入任何成员的 Inbox，提交后状态直接为 recorded，只出现在面板和 team_view 中（第 8.6 节）。Inbox 的去重直接复用现有的 client_id 唯一约束，团队输入的 client_id 取确定值，例如 team:msg:{message_id}，不需要新增唯一索引。

### 9.3 任务契约

Task 必需字段：title、description、input_refs、expected_output、acceptance_criteria、owner_member_id、dependencies、priority、state、revision。另有三个带默认值的字段：deliverable 标记该任务的结果是否属于最终交付，它决定默认验收方式，也决定 team_finish 的检查范围；write_scopes 是提示性的预期写入范围（第 7.5 节）；exclusive_group 默认取自负责成员的工具集，成员持有桌面类工具时为 desktop。

持久化的状态有八个。DSH 的任务板只有 pending、in_progress、completed、deleted 四个状态，“就绪”是视图上算出来的；OpenBox 需要验收、受阻和外部结果未知，但同样不应把可推导的量存成状态。

| 状态 | 含义 |
|---|---|
| pending | 尚未开始，或返工、重试、重新打开后等待再次派发 |
| running | 当前 attempt 已派发给负责成员 |
| review | 成员提交了成果，等待验收 |
| succeeded | 满足验收条件 |
| blocked | 需要协调者或用户解决权限、资源、信息不足等问题 |
| failed | 本次任务已确认失败，可由协调者显式重试 |
| outcome_unknown | 可能已产生外部效果但无法确认 |
| canceled | 已取消，且与仍在运行的外部效果的处理状态已明确记录 |

两个展示状态由查询推导，不落库：ready 指 pending 且全部依赖为 succeeded；waiting 指 running 且负责成员登记了等待，等待原因取自成员的等待记录。这样依赖完成时不需要在同一事务里去改写所有下游任务的状态，也不会出现“任务说在等待、成员其实在运行”的两处不一致。

~~~mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: 调度器派发
    running --> review: 提交成果
    review --> succeeded: 验收通过
    review --> pending: 返工
    running --> blocked: 成员报告受阻
    blocked --> pending: 条件修复
    running --> failed: 确认失败
    failed --> pending: 协调者重试
    running --> outcome_unknown: 外部结果不明
    outcome_unknown --> review: 核对成功
    outcome_unknown --> failed: 核对失败
    succeeded --> pending: 协调者重新打开
    pending --> canceled: 取消
    blocked --> canceled: 取消
    review --> canceled: 取消
    failed --> canceled: 取消
    running --> canceled: 中断并核对后取消
    succeeded --> [*]
    canceled --> [*]
~~~

每次回到 pending 再派发都建立新的 attempt。任何活动状态的取消通过统一 cancel 命令处理，不能直接 PATCH 为 canceled 绕过外部效果核对。failed 只有在 TeamRun 进入终态后才是最终结果。重新打开一个已通过的任务不会自动停止已经开始的下游任务，命令结果会列出受影响的下游任务，由协调者决定是否返工。

### 9.4 所有权、版本与验收

- 每个任务只有一个负责成员，创建时由协调者指定；协助工作通过消息或独立子任务表达。未开始的任务可以改派。
- 派发由调度器完成，成员不需要领取。任务为 pending、依赖全部 succeeded、负责成员处于 active 且名下没有 running 的 attempt、团队并发和独占分组允许时，调度器在一个事务内追加 attempt 事件和任务转为 running 的事件，并向成员的 Inbox 接纳一条任务输入（client_id 为 team:task:{attempt_id}），内容包含任务说明、验收标准和输入引用。成员的 ToolContext 由服务端绑定到这个 attempt。DSH 由模型自己 claim，是因为它的任务创建时没有负责人；OpenBox 的任务总有负责人，让服务端代为领取可以去掉“忘记领取、领错任务、领取时 revision 过期”这一类协议错误。
- 同一成员名下有多个可派发任务时，按 priority、再按创建顺序依次派发。
- 成员更新自己的 attempt（进度、提交、受阻、失败）不需要携带 expected_revision：服务端已经从 ToolContext 知道是哪个 attempt，并在运行行锁内对照该 attempt 的当前状态和当前 Driver generation 校验。要求模型在每次更新时带对版本号，只会制造无意义的失败。协调者的修改（编辑、改派、验收、返工、重试、重新打开）会与成员的提交竞争，必须携带 expected_revision；服务端拒绝过期更新并在错误体中返回最新内容。
- 返工或重新分配创建新的 attempt；原 attempt 的结果和错误保留。
- 已确认 failed 的任务可在 TeamRun 尚未终态时由协调者显式 retry，检查原外部效果已核清后转回 pending；下一次派发建立新 attempt。outcome_unknown 不能直接 retry。
- 任务依赖必须无环。只依赖 succeeded 任务；失败、取消、结果未知不会自动放行下游，但会立即唤醒协调者处理（第 8.3 节），不让下游无限期挂起。
- 每个成员同时负责一个正在执行的 task attempt；可在处理该任务时接收同伴信息。
- 提交是一次原子操作：team_task_update 的 submit 动作同时带上结果摘要和产出文件，服务端在同一事务里登记成果并把任务置为 review，不存在“成果已发布但任务未提交”的中间状态。
- 验收方式由 acceptance_mode 决定。auto：提交通过契约校验（摘要非空且不超长、声明的文件存在、有 output_schema 时结构合法）即转为 succeeded，不唤醒协调者。coordinator：进入 review 并唤醒协调者。deliverable 任务默认 coordinator，其余默认 auto，协调者创建任务时可以改。下游成员发现上游结果有问题时给协调者发消息，由协调者重新打开上游任务。成员不能将自行宣称成功等同于共同目标完成：team_finish 要求所有 deliverable 任务为 succeeded，或已带原因取消。
- 闲置、进程退出或租约过期不自动释放业务所有权；恢复器先核对原 attempt 与外部操作，再重试或重新分配。

### 9.5 成果与上下文

Artifact 保存稳定 ID、版本、FileAsset/内容引用、类型、摘要、来源成员、任务和 attempt，以及访问范围。大量文本、二进制与媒体走资产存储，消息只传摘要和引用。

每个成员获得目标相关摘要、当前任务、必要前置成果和定向消息。共享上下文更新使用带版本的摘要/成果，避免多个成员覆盖一个无版本共享字符串。

Task attempt 的交付物版本冻结；来源文件后来变化不能悄悄改变已验收结果。访问成果前重新检查所属用户、workspace 和当前访问权。

成员之间在同一文件系统上，运行中的交接传项目目录内的路径即可（第 7.5 节）；提交时才把交付物登记为 FileAsset。进入其他成员或协调者上下文的永远是摘要加引用：结果摘要上限 4000 字符，错误信息上限 1000 字符，超出部分截断并注明，完整内容留在成员会话和文件里。Codex 只交回子级的最后一条消息并截断错误（第 3.4 节），目的相同：协调者的上下文是整个团队里最贵的。

<a id="runtime"></a>
## 10. 持久化、调度与恢复

### 10.1 事实来源与事务

一次团队运行的事实来源是 team_events 里属于它的那条只追加事件流（不变式 15）。名册、任务板、邮箱、等待登记和预算占用都是回放事件得到的派生状态。team_runs 行上的 state_cache 保存最近一次回放结果，只是为了不必每个命令都从头回放；它与事件流在同一事务里更新，任何时候都可以丢弃并由事件流重建。

每条事件保存所涉实体变化后的完整快照，不保存增量。回放因此只是“每个实体取最后一条快照”，不需要按顺序解释每一步的含义，损坏或缺失时也容易独立检查。DSH 作了同样的选择，代价是日志更长；团队规模有上限（任务、成员、消息都有配额），这个代价可以接受。体积大且不变的内容只写一次：成员的授权与编排快照在接纳事件里，消息正文在入队事件里，缓存里只保留它们的 ID 和状态。

每个 TeamService 命令在同一业务数据库事务中：

1. 锁定该运行的 team_runs 行。PostgreSQL 用 SELECT … FOR UPDATE，SQLite 用现有的 BEGIN IMMEDIATE 写事务。同一运行的所有命令由此串行。
2. 检查 actor 和事件键。成员命令检查当前 Driver generation；用户控制检查用户身份和目标运行版本；恢复命令检查服务端 claim，不伪造一个活跃协调者身份。事件键已存在时说明这是一次重放：请求摘要相同就从已有事件还原并返回原结果，不同则返回 IDEMPOTENCY_CONFLICT。
3. 取得当前状态：缓存的 cache_seq 等于 last_seq 时直接用缓存，否则从事件流回放。
4. 生成候选事件，交给 invariant 对照当前状态校验：状态机转移、expected_revision、依赖无环、别名唯一、一个成员一个运行中的 attempt、各类配额。校验不通过即拒绝，不写任何东西。
5. 追加事件，序号取 last_seq 之后的连续值；把事件应用到状态上，写回 state_cache、cache_seq、last_seq，以及 team_runs 行上供索引用的 state。
6. 提交后发送唤醒/前端通知。

每运行串行是 DSH journal 的对应物：DSH 把一个 Lead 的全部变更排进同一条队列，读取、校验、追加在队列内一次完成，所以名册、任务图和邮箱的校验都不需要考虑并发交错。OpenBox 用运行行上的锁得到同样的性质，多个 API 进程之间同样成立；运行内序号唯一的约束是第二道防线。代价是同一团队的命令不能并行提交，而一个团队只有几个成员、每个命令是几毫秒的短事务，这个代价可以忽略；不同团队之间互不影响。事务内不调用模型、网络工具或资产远程读取，这一点使锁的持有时间有上界。revision 只保留一个用途：拒绝模型或界面基于过期视图发起的修改（不变式 12）。

fold 和 invariant 是纯函数，不读写数据库，可以用性质测试覆盖：任意合法事件序列回放的结果等于逐条应用的结果；缓存与重新回放的结果相同；非法候选事件一律被拒绝。它们对应 DSH 的 projection.ts、invariant.ts 和 task-graph.ts，逻辑可以对照移植。

运行恢复读取事件流回放出的状态，以及 Inbox、Driver、ExternalEffect 的现有记录，不依赖可丢失的 Trace。team_events 只记录任务、成员、消息等离散业务变更，不记录逐 token 流。它与独立的 trajectory 数据管线是不同用途，不能重新把完整 Trace 写回业务数据库。

团队事件不写进根会话的 agent_events。内核在每次模型请求前对该会话的整条事件前缀做序号加摘要的比较（第 4.1 节），成员的每一次任务更新如果都落在协调者会话的事件流上，协调者的请求会反复因前缀变化而重建，它每一步的回放也会被成百上千条与模型上下文无关的事件拖慢。team_events 采用与 agent_events 相同的结构和约束，但是独立的一条流。

### 10.2 异步成员接纳

member_start 返回成员 ID（即成员会话 ID）、alias 和 provisioning/active 状态，不等待 LLM 完成。

第一阶段事务追加接纳事件（冻结的快照在其中）和 provisioning 状态事件，创建 kind 为 team_member 的会话并写入首条 Inbox 输入；随后由 RuntimeAdapter 补齐必要环境并调度。任何环境初始化失败均可根据已追加的成员事件恢复或标记失败。调用重试命中同一个事件键，返回同一个成员。

首条输入承载成员的身份和协作约定：你是哪个团队里的哪个成员、职责是什么、如何提交和上报受阻、权限范围已固定。有首个任务时，任务说明紧随其后。身份不写进 system prompt（不变式 13）：DSH 把身份放在首条 user 消息里，它随普通历史经历冷恢复和压缩，不需要每一步重新插入。压缩成员会话时，摘要提示要求保留这段身份与约定。

原子创建 helper 与现有会话/Inbox 模块共享事务。不能出现成员已对外宣告成功而对应会话无法追踪的状态。DSH 的对应做法是先落一条 provisioning 记录，再启动子会话，初始输入被目标持久接纳后才转为 active；恢复时凭“子会话存在、血缘匹配、初始输入已记录”三个条件判定成功，否则记为 failed，并且失败的成员永久占用名字和名额，使接纳失败保持可见。OpenBox 沿用同样的判定和同样的不复用规则。

### 10.3 调度与等待

调度分快慢两条路径，做的是同一件事：

- 快路径：TeamService 命令提交后在进程内触发一次该运行的调度，延迟在毫秒级。
- 慢路径：在现有的 [agent_recovery_service](/Users/wang/workspace/OpenBox/backend/agent/recovery_service.py) 里增加团队阶段，每 15 秒一轮，负责进程重启和快路径丢失后的补偿。不另起一个并行的扫描器。该服务的阶段有顺序约束（先处理中断请求，再收敛子会话 outbox，再修复过期会话，再恢复 Inbox，最后核对外部效果），团队阶段排在最后，此时成员会话的执行状态、待处理输入和外部效果都已收敛。

一次调度对一个运行按固定顺序执行，顺序取自 Symphony 的 tick（第 3.4 节），先对账再派发可以避免基于过期状态派工：

1. 对账：核对 running 的 attempt 对应的成员会话是否仍有有效执行或待处理输入，处理过期等待和第 8.3 节的停滞判定。
2. 补投：重试 queued 消息，为已接纳但缺回执的消息补回执。
3. 唤醒：按第 8.6 节为有新事件的等待成员生成唤醒输入。
4. 派发：按第 9.4 节派发可执行的任务。
5. 收尾：推进 pausing、canceling、completing 状态。

并发与名额：

- 成员的模型执行名额统一由现有 Driver 配额控制，团队另有自己的并发上限，取值规则见第 6.4 节。
- Driver 名额不足是背压，不是失败。派发或唤醒遇到 DriverQuotaExceededError 时保留输入，任务保持 pending，面板显示“等待执行名额”，下一次调度重试；同一运行连续因名额不足而空转时，重试间隔按 min(10 秒 × 2^(n-1), 5 分钟) 退避。现有恢复路径对名额不足也是保留记录等下一轮，语义一致。
- 成员或协调者等待时结束本轮，Driver 租约和 session_executions 租约都正常释放，不占名额；业务 task 所有权继续保留。容量计划里记录的“父会话占着名额等子会话”的死锁在团队里不会出现。
- 多个 API 进程同时调度同一个运行时，运行行上的锁、运行内事件序号唯一和 Inbox 的 client_id 唯一三者保证不重复派工。慢路径在每个进程的恢复服务里都会运行，互相之间不需要选主。暂停、取消和中断通过 Driver 记录上的 abort_requested_at 传到持有该成员执行权的那个进程，不依赖进程内信号。这条路径用两个后端进程对同一个 PostgreSQL 的专项测试验证（第 16.2 节）。

等待协议不新建让出机制。现有代码里已有“工具结果结束本轮”的先例：工具结果带 plan_ready 元数据时，processor 以 stop 结束本轮。team_wait 沿用这一形式，唤醒统一走 Inbox：

1. team_wait 在运行行锁内读取成员的 last_seen_seq，即最近一次通过 team_view、唤醒摘要或等待结果交付给该成员的事件水位。已有该成员未见过的值得唤醒的事件时，直接返回变化摘要，不结束本轮；登记等待和检查事件在同一事务里，不存在事件在登记前到达而漏掉的窗口。
2. 没有任何在途工作能产生变化时，返回 no_progress，不结束本轮（第 8.5 节）。
3. 其余情况登记等待：追加一条成员状态事件，其中带 wait_after_seq 和 wait_deadline，返回带 turn_yield 元数据的结果，本轮结束，会话回到 idle。等待只是成员状态里的两个字段，一个成员同时最多一个等待。
4. 之后该成员的唤醒输入由调度器生成（第 8.6 节）。超时也是一种唤醒，输入里注明是超时。
5. 本轮以其他方式结束时按隐式等待处理（第 8.5 节），走同一条路径。
6. 用户输入优先处理。待处理结果不会在协调者正在运行时启动另一个协调者实例：运行中的协调者通过下一步边界的注入收到新事件。

等待的超时范围取 10 秒到 1 小时，默认值由角色决定；下限防止模型把等待当轮询用，DSH 与 Codex 的取值相同。提问类工具的挂起续跑协议（QuestionSuspended）不复用到 team_wait：它把结果写回被挂起的工具调用，而会话接纳新消息时现有逻辑会把挂起中的检查点作废（question/runtime.py 的 invalidate_locked）。成员在等待期间必须能通过 Inbox 收消息，两者叠在同一个会话上，每来一条同伴消息就会作废一次等待。协调者向用户提问仍走这套协议，不受影响。

### 10.4 TeamRun 状态

~~~mermaid
stateDiagram-v2
    [*] --> provisioning
    provisioning --> running: 协调者与配置就绪
    provisioning --> failed: 初始化失败
    running --> waiting: 暂无可执行工作
    waiting --> running: 事件或用户输入
    running --> pausing: 用户暂停
    waiting --> pausing: 用户暂停
    pausing --> paused: 成员到达安全边界
    paused --> running: 用户继续
    running --> canceling: 用户取消
    waiting --> canceling: 用户取消
    paused --> canceling: 用户取消
    canceling --> canceled: 停止与核对完成
    running --> completing: 全部必需交付验收
    completing --> completed: 执行与费用收尾完成
    running --> failed: 无法完成并已收尾
    completed --> [*]
    canceled --> [*]
    failed --> [*]
~~~

暂停停止新调度，在安全边界终止当前模型执行并保留输入。已提交外部 Job 可继续回传，结果进入持久记录，暂停期间不自动调用模型。

暂停不只来自用户。预算或时长用尽、协调者轮数用尽、协调者连续三轮因模型或 provider 错误失败、停滞判定二次触发，都使运行进入 paused 并记录 pause_reason，同时通过根会话的现有通知提醒用户。这些情形下已完成的工作有价值，用户补充预算、换模型或给出指示后可以继续，所以不判为 failed。failed 只用于两种情况：初始化阶段无法建立协调者，或协调者显式以失败结束（team_finish 带 failed 状态和原因）。

取消先关闭新工作接纳，取消 Inbox 中未执行的控制/任务输入并请求停止活动成员；外部 Job 按能力取消或继续核对。仍无法确定结果时保留 canceling 及明确 reason，不能伪装为已撤销。

终态 TeamRun 不直接重新打开。用户要求继续新工作时创建关联的新 TeamRun，显式选取历史摘要与成果。paused/waiting 属于同一运行的可恢复状态。

team_finish 提交经过验证的最终摘要和成果引用，先进入 completing，关闭新成员、任务与普通协作消息接纳。协调者通过明确的终结/yield 协议结束执行；Scheduler 确认没有活动成员执行、未核对外部操作、未结算请求或必需的待处理输入后写入 completed。未处理的一般进度通知按已记录的终结规则关闭，不再唤醒模型。最终答复从已保存的交付摘要生成用户视图，不依靠收尾时额外调用模型。完成中崩溃也能继续收尾。

暂停、取消和完成之间使用运行 revision 竞争，先提交的状态转换决定后续允许操作。completing 阶段取消不能撤销已发生的外部效果；请求只影响尚可停止的收尾操作并如实展示最终状态。

### 10.5 崩溃恢复矩阵

| 故障位置 | 恢复依据 | 处理 |
|---|---|---|
| 命令已提交，响应丢失 | 事件键 + 请求摘要 | 从已有事件还原并返回相同结果，不重复创建成员或任务 |
| provisioning 后进程退出 | member、session、初始输入关联 | 补齐启动；已有会话时复用，不新建第二份 |
| queued 消息未投递 | 回放得到的“已入队减已送达”集合 | 按入队顺序重试投递 |
| Inbox 已接纳，delivered 回执未写 | client_id 唯一记录 | 补回执，不重复接纳 |
| 输入已领取，Driver 退出 | Inbox claim、Driver fence、执行记录 | 交由现有恢复协议核对，不直接把输入重放为全新操作 |
| 成员完成但协调者未收到 | 任务状态 + 持久结果通知 | 补投递结果通知并唤醒 |
| 任务执行者失联 | task attempt + Driver + 外部效果 | 隔离旧代次，核对后重试或转 blocked/outcome_unknown |
| 外部请求已发出但回执丢失 | ExternalEffect、provider handle | 使用适配器核对；没有证据时保持未知 |
| 协调者正在等待时退出 | 成员状态事件里的等待登记、事件水位、Inbox | 恢复服务的团队阶段继续匹配事件与唤醒 |
| 派发事务已提交，进程内唤醒丢失 | attempt 为 running、Inbox 有未领取的任务输入 | 现有 Inbox 恢复阶段在 15 秒内重新唤醒，不重复建立 attempt |
| 派发或唤醒时 Driver 名额不足 | 保留的 Inbox 输入、pending 的任务 | 按背压处理并退避重试（第 10.3 节），不判失败 |
| 成员一轮结束但没有提交 | running 的 attempt、空闲的成员会话、无待处理输入 | 先催一次，再次无动作则隐式提交（第 8.5 节） |
| 付费工具预留后执行未开始 | 预留事件与稳定调用身份 | 确认请求未发出后追加释放事件；未知请求先核对 |
| state_cache 缺失、损坏或落后于事件流 | cache_seq 与 last_seq 不一致，或回放校验失败 | 丢弃缓存，从事件流完整回放后重写 |
| WebSocket 断线 | 快照水位与事件流序号 | 客户端按水位补读；水位无效时重新取快照 |

### 10.6 预算与计费

所有成员和协调者共用 TeamRun 预算。复用 [计费服务](/Users/wang/workspace/OpenBox/backend/billing/service.py) 的实际扣费事实，不能再建立一份独立扣款系统。现有计费只有事后结算，没有预留或冻结原语，默认模式还是不扣款的 shadow（第 4.1 节），所以团队层自己实现预算门禁，分两层，严格程度与单次调用的金额相称。两层都属于交付范围。

| 层 | 适用 | 做法 | 超支上界 |
|---|---|---|---|
| 软上限 | 模型 token 调用 | 每个成员每一步开始前，汇总该运行所有成员会话已结算的 usage_events，加上正在执行的成员数乘以单步费用估计；超过预算则不再发起新的模型调用，运行转入 paused | 并发成员数乘以单步最大费用。token 调用单价低，这个上界是可接受的 |
| 严格预留 | 事先可知价格的付费工具，即 T2 层（第 7.4 节） | 调用前在运行行锁内追加预留事件，以稳定调用身份为事件键；校验“已预留加已结算不超过预算，且不超过该工具的预授权上限”；完成后追加结算事件并释放差额；结果未知的请求核对后再结算 | 无，前提是适配器能给出价格上界 |

- 软上限不在每次模型调用前增加写事务。usage_events 已经按会话记账，团队只需要按成员会话集合汇总。
- 严格预留不需要单独的表：预留、结算、释放都是事件，未结清的预留总额是回放状态的一部分，原子性由运行行锁保证，多个成员同时预留时依次通过校验。
- 团队预算包含协调者、成员、重试与工具费用；成员费用聚合不重复叠加父会话显示值。
- 额度不足阻止新付费请求，保留未完成工作，给用户显示补充预算或结束的入口。
- 使用既有计费精度（Numeric(28, 12) 与 Decimal）和价格版本，不使用浮点金额；事件载荷里的金额用十进制字符串。
- 只有能给出调用费用上界的适配器才能提供严格执行上限；其他适配器需限制调用规模或明确采用估算预算，不能承诺第三方所有计费绝不超出估计。
- 未知外部请求不立即释放预留；核对后再结算。
- 部署处于 shadow 计费模式时，预算按同一套用量数字执行，只是不产生扣款；面板显示的仍是真实用量。

<a id="storage"></a>
## 11. 数据模型与约束

以下为目标表与字段契约。命名可以按仓库 ORM 约定微调，但职责、唯一性和恢复关联不能省略。

数据分成两类，存法不同。

长期数据是用户自己创建、跨运行复用的东西，用普通的关系表：

| 表 | 关键字段 / 职责 |
|---|---|
| agent_definitions | id、owner_user_id、workspace_id、name、source、status、current_version_id |
| agent_definition_versions | definition_id、version、spec_json、content_digest、created_by、created_at |
| team_definitions | 模板身份、所有者、status、current_version_id |
| team_definition_versions | definition_id、version、成员与策略配置、content_digest、created_by、created_at |

运行数据是一次团队运行产生的全部状态，包括协调者为任务自主组建的成员。它依附于根会话，只有两张表：

| 表 | 关键字段 / 职责 |
|---|---|
| team_runs | 每次运行一行。id、root_session_id（外键，随会话级联删除）、owner_user_id、workspace_id、project_id、template_id 与 template_version_id、title、goal、summary（成员数与其中临时成员数、任务完成数与总数、成果数、是否需要用户处理、最近一次汇总的用量及三类占比）、policy_snapshot、grant_snapshot、state、pause_reason、revision、session_active、project_active、last_seq、state_cache、cache_seq、起止快照、final_summary 与成果引用、时间戳 |
| team_events | 每次运行一条只追加事件流。id、team_run_id（外键，随运行级联删除）、sequence、event_key、request_digest、kind、actor_type、actor_member_id、entity_type、entity_id、payload、created_at。结构与约束照搬 agent_events |

team_runs 这一行承担三件事：它是同一运行所有命令串行化时被锁的那一行；它是恢复服务找“哪些团队还活着”的索引；它保存回放缓存。事实在 team_events 里，这一行丢了缓存也能重建。

成员、任务、attempt、消息、成果、等待、预算预留都不是表，而是事件：

| 事件种类 | 载荷 |
|---|---|
| team.run | 运行状态的完整快照：state、pause_reason、revision |
| team.member.admitted | 成员接纳，只写一次：成员 ID（即成员会话 ID）、role、alias、source、created_by、定义与版本引用或内联定义快照、composition 快照、authority 快照、模型绑定、Skill 清单 |
| team.member | 成员可变状态的完整快照：membership_state、失败原因、当前 attempt、last_seen_seq、wait_after_seq、wait_deadline、是否已催促 |
| team.task | 任务的完整快照：标题、说明、输入引用、预期输出、验收标准、负责人、依赖、priority、state、revision、deliverable、acceptance_mode、write_scopes、exclusive_group、current_attempt、blocked_reason |
| team.attempt | attempt 的完整快照：任务、序号、成员、state、Driver run 与 generation、结果摘要、成果引用、implicit、错误、起止时间 |
| team.message.queued | 消息全文：消息 ID、发送者、接收者、kind、任务与 attempt、回复关系、正文、附带路径与成果引用 |
| team.message.delivered / recorded / canceled / failed | 消息 ID、目标、对应的 Inbox 条目 |
| team.artifact | 成果的完整快照：成果 ID、版本、任务与 attempt、成员、FileAsset 引用、内容摘要、摘要文字、媒体类型 |
| team.budget.reserved / settled / released | 调用身份、工具、金额、计费引用 |
| team.notice | 停滞判定、催促、Skill 摘要变化等需要留痕或提示用户的事项 |

回放状态的缓存里不放消息正文和成员的大块快照，只放它们的 ID、状态和索引；需要正文时按实体索引从事件流读取。缓存中唯一不来自事件的字段是成员的 last_seen_seq 在两次事件之间的推进（team_view 会推进它但不写事件）；缓存重建后它回退到最近一条成员状态事件里的值，后果最多是多唤醒一次，方向是安全的。

### 11.1 复用表与关联

- Session 仍保存真实对话，不另建团队对话存储。成员会话的 kind 为 team_member，parent_id 指向根会话，不需要改表；成员会话 ID 就是成员 ID。每用户会话上限的统计把 team_member 与 cron 一样排除。成员会话不设单独的保留期，它们跟随根会话：根会话在，运行面板和成员记录就在；根会话删除，它们一并删除。
- AgentInboxItem 新增一个可空列 source_type，空值表示用户输入，团队取值为 team_task、team_message、team_control。这是对既有表的唯一改动，需要一条 Alembic 迁移和一个 SQLite 升级桥。来源标识不另设列和唯一索引：沿用 client_id，取 team: 前缀的确定值，由现有的 (user_id, session_id, client_id) 唯一约束去重。
- 团队输入落成消息时写为 synthetic 消息，与定时任务注入提示词的现有做法一致。现有代码已为 synthetic 消息保留了平台专用的 client_message_id 前缀（sjr:、tabort:），把 team: 加入保留前缀后，HTTP 用户无法伪造团队来源。发送成员、team_run_id 和 message_id 记录在消息元数据里，供界面区分展示。
- 统一 Inbox 承担执行接纳，团队不再维护另一套独立模型输入队列。
- Task attempt 通过事件里的显式引用绑定后台 Job 和 ExternalEffect；外部效果的归属 session/run 保持真实执行者身份。
- FileAsset/现有资产服务负责内容存取；成果事件负责业务成果关系与版本。Skill 的内容快照存入现有 blob 存储，按内容摘要寻址（第 7.2 节）。

### 11.2 强约束

数据库约束只有少数几条，其余业务不变式由 invariant 在追加事件前校验，两者都在运行行锁内生效。

数据库约束：

1. 每个根会话最多一个非终态 TeamRun：team_runs 上 (root_session_id, session_active) 唯一，运行非终态时 session_active 为 1，进入终态时置为 NULL。PostgreSQL 和 SQLite 的唯一索引都把 NULL 视为互不相等，所以历史运行不冲突，活动运行只能有一个，不需要先查询后插入，也不需要额外的占用表。这个索引同时是第 5.3 节判定“根会话是否绑定协调者”的查询入口。
2. 每个项目最多一个非终态 TeamRun：(project_id, project_active) 唯一，做法相同。
3. team_events 的 (team_run_id, sequence) 唯一，序号从 1 开始连续，在提交事务内分配，不用墙钟时间排序；回放遇到序号缺口即失败，不猜测。
4. team_events 的 (team_run_id, event_key) 唯一；同键不同请求摘要返回冲突。
5. 消息目标接纳按现有的 user_id + session_id + client_id 唯一，团队输入的 client_id 由消息、attempt 或唤醒序号确定性生成。
6. 定义版本不可覆盖；事件只追加，不 UPDATE、不 DELETE，只随运行整体删除。
7. 成员会话的 (entity_id, kind) 索引支持按成员会话 ID 直接找到它的接纳事件。

由 invariant 校验的不变式：

1. 每个运行有且仅有一个 coordinator；成员 alias 在该运行内唯一，retired 和 failed 的名称不复用以避免误路由；成员数不超过 max_members，失败的成员也计数。
2. 同一工作会话不能同时被旧 SubagentDescriptor 和团队成员身份绑定。
3. 同一个任务只有一个活动 attempt；attempt 序号单调递增；一个成员同时只有一个 running 的 attempt。
4. 任务依赖的两端必须在同一运行，禁止自环和环路。
5. 任务和运行的状态转移符合第 9.3 节和第 10.4 节的状态图；携带 expected_revision 的修改与当前 revision 一致。
6. 每成员待投递消息数、单条消息大小、任务数、消息总数、协调者轮数不超过策略上限。
7. 未结清的预留加已结算金额不超过预算和各工具的预授权上限。

外键之外还需检查 owner_user_id/workspace_id/project_id 一致；仅靠传入 run_id 查询不足以授权。

### 11.3 PostgreSQL 与 SQLite

服务部署使用 PostgreSQL 的事务、条件更新和行锁；桌面/开发使用 SQLite 的写事务与条件更新实现同样的业务不变式。SQLite 不依赖 SELECT FOR UPDATE 的行锁语义。两者都是真实的部署目标：配置了 jwt_secret 时用 PostgreSQL 与 Alembic，否则用 SQLite 且不运行 Alembic，单元测试跑在内存 SQLite 上。

所有命令在运行级短事务中序列化（第 10.1 节）：PostgreSQL 锁 team_runs 行，SQLite 用 BEGIN IMMEDIATE，现有的 internal_parts 已是这种写法。事务内不调用模型、网络工具或资产远程读取。

六张新表在 PostgreSQL 上走 Alembic，在 SQLite 上由 create_all 自动建立；只有 agent_inbox 的新列需要手写 SQLite 升级桥。业务字段以后的增减是事件载荷的变化，不需要迁移，载荷带 schema_version，回放按版本解码。

采用数据库持久队列与短事务；无需先引入 Kafka、独立分布式消息系统或新的任务框架。

<a id="interfaces"></a>
## 12. API、模型工具与事件协议

### 12.1 用户 API

| 方法与路径 | 用途 |
|---|---|
| GET /api/agent-definitions | 当前用户可访问的定义目录；搜索和分页 |
| POST /api/agent-definitions/draft | 由一句话描述生成定义草稿，不落库（第 6.5 节） |
| POST /api/agent-definitions | 创建用户定义，状态为 draft，返回编译得到的能力摘要和校验问题 |
| POST /api/agent-definitions/{id}/duplicate | 以已有定义为起点复制，来源可以是内置、legacy 或自己的定义 |
| POST /api/agent-definitions/{id}/activate | 启用，之后可被团队模板引用并出现在协调者的目录检索里；聊天创建的确认卡片选“启用”时走的是同一个操作 |
| GET /api/agent-definitions/{id} | 定义与版本信息 |
| PUT /api/agent-definitions/{id}/draft-version | 保存对已有定义的修改为草稿版本，可单独试运行 |
| POST /api/agent-definitions/{id}/versions | 把草稿版本发布为当前版本 |
| POST /api/agent-definitions/{id}/archive | 归档，阻止新实例 |
| POST /api/agent-definitions/{id}/test-runs | 用指定版本开一个单 Agent 的试运行会话，返回会话 ID 和实际生效的模型、工具、Skills |
| GET/POST /api/team-definitions | 列出或创建模板 |
| GET /api/team-definitions/{id} | 模板与版本信息 |
| POST /api/team-definitions/{id}/versions | 保存模板新版本 |
| POST /api/team-definitions/{id}/archive | 归档模板 |
| POST /api/team-runs/preview | 校验一份团队配置并返回解析结果：成员与模型、将预先批准的工具与模式、能力缺失、预计占用的并发名额。模板编辑页用它显示静态配置错误；team_propose 在服务端走的是同一段编译 |
| 现有的发消息接口（prompt_async） | 增加可选的 team_request：template_id 或 auto、allow_supplement、requested_agent_ids。agent 为 team 时保存为该用户消息上的一个 part，不改 sessions 表（第 8.2 节） |
| 现有的回答提问接口 | 团队没有单独的“启动”接口。TeamRun 只由组队方案卡的回答创建：用户选“开始”，team_lineup 续跑在一个事务里创建运行、冻结策略与授权、接纳成员（第 8.2 节）。模板页的“运行”、运行记录的“再运行一次”、移动端启动已保存团队，都是“新开一个带 team_request 的对话”，走同一条路径 |
| GET /api/team-runs | 运行记录列表：按状态、项目、模板筛选，游标分页；只读 team_runs 行上的摘要，不回放事件流 |
| GET /api/team-runs/{id} | 回放状态的用户可见投影及其事件水位，含连线图用的 links 摘要（第 12.4 节） |
| GET /api/team-runs/{id}/events?after_seq=... | 增量补读：事件流经 TeamProjection 投影后的用户可见事件 |
| GET /api/team-runs/{id}/members、/tasks、/messages、/artifacts | 分页读取详情；messages 支持按单个成员（member）或一对成员（between）筛选，供连线图点线后使用 |
| POST /api/team-runs/{id}/messages | 用户向协调者补充要求，保留用户来源 |
| POST /api/team-runs/{id}/pause、/resume、/cancel | 用户控制 |
| POST /api/team-runs/{id}/members/{member_id}/save-definition | 将单个临时成员的配置保存到用户 Agent 库 |
| POST /api/team-runs/{id}/save-as-template | 把整支团队另存为模板：临时成员存入 Agent 库，生成引用它们的团队模板（第 8.4 节） |
| POST /api/team-runs/{id}/grant | 用户追加授权或预算，生成新的授权版本 |

所有变更请求要求幂等键，它成为事件键的来源；有版本竞争的请求携带 expected_revision。HTTP 接纳返回 202 或已完成命令结果，不等待整队工作完成。

默认仅运行所有者可启动、控制及读取团队内容；平台管理员排障走已有管理员权限和审计。工作区共享的读取/控制不能因为知道 ID 自动获得，后续共享能力需要显式 ACL。

### 12.2 模型可用工具

| 工具 | 协调者 | 普通成员 |
|---|---|---|
| team_propose | 提出阵容、各自职责与上限，服务端校验后向用户出组队方案卡；用户同意后创建运行并接纳成员（第 8.2 节）。运行中以 amend 方式申请调整阵容 | 不提供 |
| agent_catalog_search / agent_catalog_get | 可使用 | 不提供 |
| team_member_start | 选择定义，或给出内联定义（角色指令、Skills、工具、MCP、模型），接纳并返回成员 ID | 不提供 |
| team_view | 查看团队摘要、任务图、候选模型、预算与名额 | 查看名册、可见任务与自身状态 |
| team_message_send | 定向发送；可显式展开广播；可附项目目录内的文件路径 | 定向发送；可附文件路径 |
| team_task_create | 创建任务、依赖、负责人与验收方式 | 不提供；需要新任务时给协调者发消息 |
| team_task_update | 编辑、改派、验收、返工、重试、重新打开、取消，携带 expected_revision | 仅对自己当前的 attempt：progress、submit（带结果摘要和产出文件）、blocked、failed，不携带 revision |
| team_wait | 登记等待并结束本轮 | 登记等待并结束本轮 |
| team_member_interrupt | 中断当前执行或按流程退役 | 不提供 |
| team_finish | 提交共同目标完成与交付引用 | 不提供 |

成员没有领取任务和单独发布成果的工具：派发由调度器完成（第 9.4 节），成果随 submit 原子登记，运行中的交接通过消息附路径完成（第 9.5 节）。协调者有 11 个团队工具，成员有 4 个；作为对照，DSH 的完整工具集是 9 个。成员工具越少，每次请求的 schema 成本和协议出错面越小。

工具只是 TeamService 的入口，不建立独立业务逻辑。成员接受同伴请求仍需处于其职责、工具权限与任务范围内。

普通聊天里的 build Agent 有两个与团队有关的工具。一个是 team_propose：它判断任务值得分工时用来向用户提议，用户在方案卡上同意后会话切到 team、由协调者接手；它自己不能创建运行，也没有其余团队工具（第 8.2 节）。另一个是与团队运行无关的 agent_manage，只给根会话里的 build Agent，用于在对话中创建和修改用户的 Agent 定义（第 6.5 节）。它的动作是 list、get、create、update；create 和 update 先落草稿，再经确认卡片由用户决定是否启用。它不属于团队工具，协调者和成员都没有。

三条与执行层的衔接要求：

- 团队工具对团队会话常驻。现有的工具暴露规划按 Agent 定义常驻集，其余工具延迟暴露；协调者和成员的常驻集必须包含各自的全部团队工具，不能让模型靠检索去找 team_wait。
- 工具 schema 在成员存续期间固定，名册、任务、候选模型等可变内容只出现在工具结果里（不变式 13）。工具结果用紧凑 JSON，不带缩进；DSH 为每个团队工具声明完整的结果 schema 并统一紧凑渲染，理由是这些结果会出现在每一次名册和任务读取里。
- team_view 的输出分段并设上限：默认只返回与调用者相关的任务和最近的变化，完整任务图按需分页读取。

### 12.3 RuntimeAdapter 契约

~~~text
capabilities() -> model_selection, steer, resume, cancel, artifact_types
prepare_member(snapshot, identity) -> durable_member_binding
wake_member(member_id, inbox_source) -> accepted_receipt
observe_member(member_id) -> execution_snapshot
interrupt_member(member_id, expected_generation) -> control_receipt
reconcile_member(member_id) -> recovery_result
~~~

本计划定义这个接口并实现 OpenBoxRuntimeAdapter 一个适配器。团队内核的其他模块不直接引用 Driver、Inbox 和 Session 的内部函数，全部经过它，所以它同时是团队层与执行层之间唯一的接触面。capabilities 由适配器如实声明，TeamService 在接纳成员和下发中断前检查，不假设所有适配器行为相同。

后续 A2A Adapter 映射远程任务与成果，不假设远程 Agent 支持本地模型覆盖、原始上下文读取或完全相同的中断语义（第 3.3 节）。

### 12.4 团队事件

面向客户端的团队事件不是另一套记录，而是 team_events 事件流经 TeamProjection 投影后的结果：按事件种类白名单字段，去掉授权快照、平台提示词等不该出现在客户端的内容，并把内部种类映射成下面的用户可见类型。seq 就是事件流的序号。

~~~json
{
  "schema_version": 1,
  "event_id": "te_001",
  "team_run_id": "tr_001",
  "seq": "42",
  "type": "team.task.review_requested",
  "actor": {
    "type": "member",
    "member_id": "member_analysis"
  },
  "entity": {
    "type": "task",
    "id": "task_analysis",
    "revision": 5
  },
  "payload": {
    "attempt_id": "attempt_01",
    "artifact_ids": ["artifact_report_v1"]
  }
}
~~~

用户可见类型包括 run.state_changed（暂停时带 pause_reason）、member.accepted/active/retired/failed/nudged、task.created/assigned/dispatched/review_requested/succeeded/blocked/failed/reopened/canceled、message.queued/delivered/recorded、artifact.published、budget.updated、skill.digest_changed、attention.required。任务的 ready 与 waiting 是推导状态，不产生事件。

阵容连线图（第 13.3 节）不需要新的事件种类。GET /api/team-runs/{id} 的状态投影里带一份 links 摘要，由 TeamProjection 从回放状态统计得出，不落库：

~~~json
{
  "links": [
    { "kind": "assign", "a": "ses_root", "b": "ses_writer", "count": 5, "last_seq": "88" },
    { "kind": "message", "a": "ses_writer", "b": "ses_reviewer", "count": 3, "last_seq": "91" }
  ]
}
~~~

assign 表示协调者给该成员派过任务，count 为任务数；message 表示两个成员互相发过消息，a 与 b 按成员 ID 排序后不分方向，count 为条数。客户端收到 task.assigned、message.queued 这类事件后在本地递增，水位不连续时重新读取投影。消息子页按成员对筛选走 GET /api/team-runs/{id}/messages 的 between 参数。

WebSocket 只通知“有新事件”或传送可去重事件；客户端按 seq 补读。首次快照包含同一事务水位，补读接续该水位。事件流在运行存续期间不裁剪，所以补读不会遇到过期的水位；水位超出当前序号或运行已删除时返回明确错误，客户端重取快照。

现有 WebSocket 层没有序号和补读，事件按 userId 发给该用户的每一个连接，队列满了就丢非关键事件，客户端靠 1 秒轮询兜底（第 4.1 节）。团队面板不能照搬这种做法，后端曾因 1 秒全量轮询被打满并 OOM。三条要求：

- 团队事件低频，走上面的序号加补读机制。没有 WebSocket 通知时面板以不低于 5 秒的间隔做增量补读，禁止对快照接口做秒级轮询；快照大小有上限，任务和消息分页。
- 成员会话的高频事件（流式片段、工具调用进度）默认不发给用户的所有连接，只发给显式订阅了该成员会话的连接，即用户打开该成员的只读会话的时候。否则三个成员同时输出会把流量放大到用户的每个客户端，移动端也在内。
- 面板上成员的“当前状态”来自团队事件和回放状态里的成员字段，不为每个成员会话各起一个状态轮询。

### 12.5 错误契约

至少固定：AGENT_NOT_ACCESSIBLE、MODEL_LOCKED、MODEL_NOT_ALLOWED、CAPABILITY_UNSUPPORTED、TOOL_NOT_TEAM_READY、TEAM_MEMBER_LIMIT、TEAM_BUDGET_EXCEEDED、TEAM_PAUSED、TEAM_CLOSED、STALE_REVISION、DEPENDENCY_CYCLE、MEMBER_BUSY、RESOURCE_BUSY、AUTHORITY_REVOKED、IDEMPOTENCY_CONFLICT、OUTCOME_UNKNOWN。

另有：PERMISSION_REQUIRES_USER、TEAM_ALREADY_ACTIVE、TEAM_ACTIVE（活动运行期间的 revert）、TEAM_MEMBER_ALIAS_TAKEN、TEAM_MEMBER_NOT_FOUND、TEAM_MAILBOX_FULL、TEAM_MESSAGE_TOO_LARGE。no_progress 是 team_wait 的正常结果，不是错误。

错误包含可解释的原因与当前状态，并尽量带上模型改正所需的数据：MODEL_NOT_ALLOWED 带允许列表，STALE_REVISION 带任务的最新内容，TEAM_MEMBER_NOT_FOUND 带当前名册。模型可以据此调整任务或报告受阻，不能把校验失败当作已完成操作。

<a id="product"></a>
## 13. 前端与移动端设计

### 13.1 设计原则与界面清单

界面稿是本仓库里的一个静态页面 [docs/mockups/agent-team-ui.html](mockups/agent-team-ui.html)：用浏览器直接打开即可，无需构建，右上角可切换浅色与深色。它共 12 屏，每屏下方注明对应的现有组件和取舍依据，编号与下表一致；页面里的颜色、字号、圆角、阴影都取自 tokens.css，文案取自现有的 i18n 资源，浅色与深色都已核对。四条原则决定了所有界面的取舍：

1. 团队是普通会话的一种模式，不是另一个产品。团队对话就是 [ChatRoute](/Users/wang/workspace/OpenBox/frontend-v2/src/routes/workspace/ChatRoute.tsx)：同一个 ChatFlow、同一个 Composer、同一套卡片。团队相关的能力只能以“往现有组件里加一项”的方式出现——模式选择器多一个选项、@ 菜单多两个分组、提问卡片多一种详情、右侧工作台多一个页签——不为团队做第二套聊天界面、第二套卡片或特例样式。
2. 先说话，后配置。用户不需要先建 Agent、先建模板才能用团队：选“团队”模式说出目标即可，好用的阵容在完成后一键留下来。模板和 Agent 库是为了复用，不是前置条件。
3. 用户面对的概念尽量少：Agent（一个角色）、团队（一份名单加一个“允许补充”的开关）、一次运行（一个对话）。“固定、混合、自动”、策略字段、generation 等内核术语不进入界面。
4. 每个界面对照一个现有页面或组件实现（第 13.7 节），市面习惯只在现有页面没有先例时作为依据（第 3.5 节）。

| 编号 | 界面 | 位置 |
|---|---|---|
| A1 | 输入区：模式选择器里的“团队”、队伍选择器、@ 菜单里的“我的 Agent”与“团队模板” | 聊天页 |
| A2 | 组队方案卡：三个入口共用 | 聊天页，对话流末尾 |
| A3 | 普通对话里 AI 提议用团队 | 聊天页 |
| A4 | 运行中：工具行、团队进度卡、成员变动分隔线；右侧工作台“团队”页签与阵容连线图 | 聊天页与右侧工作台 |
| A5 | 成员会话的只读视图；按连线筛选的消息子页 | 聊天页与右侧工作台 |
| A6 | 完成之后：成果、另存为团队模板 | 聊天页 |
| B1 | 我的 Agent | Agent 团队页 |
| B2 | 新建与编辑 Agent：左栏配置，右栏试一试 | 独立页面 |
| B3 | 对话创建 Agent 的确认卡 | 聊天页 |
| B4、B5 | 团队模板列表、编辑页、点“运行”后的选项目弹窗 | Agent 团队页 |
| B6 | 运行记录 | Agent 团队页 |

### 13.2 在聊天里使用团队

**输入区（A1）。** 三处改动，都是现有组件里多一项：

- [ModePicker](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/composer/ModePicker.tsx) 的选项来自服务端的 Agent 列表，内置 Agent 增加 team 之后这里自动多出“团队”，前端只补 mode.team 与 mode.teamDesc 两条文案。
- 选中“团队”后，模式选择器右边出现队伍选择器（TeamPicker），与 VideoModelPicker 挨着聊天模型的排法相同。菜单内容：自动组队；我的团队模板列表，每项下面一行成员名；“允许协调者补充成员”，一个带勾的菜单项，默认取所选模板的设置，只对本次运行生效、不改模板，选“自动组队”时恒为勾选；“管理团队模板…”。没有模板时菜单里只有“自动组队”和管理入口。
- [MentionMenu](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/composer/MentionMenu.tsx) 在资源、文件、技能、命令之外增加“我的 Agent”和“团队模板”两个分组。点名的 Agent 一定在队里；@ 一个模板等于在队伍选择器里选中它；非团队模式下 @ 了 Agent，输入区切到团队模式（第 8.2 节）。

这些选择随消息一起发出（agent=team 加 team_request），不产生任何页面跳转。运行期间输入框照常可用，发出的内容就是给协调者的补充要求；“停止”键在活动运行期间等同于暂停团队（第 8.4 节）。运行结束后模式回到“执行”，这个对话可以继续当普通对话用。

**组队方案卡（A2、A3）。** 方案卡就是 [QuestionDock](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/QuestionDock.tsx)，只新增一个详情组件 TeamLineupDetail，与 VideoApprovalDetail、DesktopTakeoverDetail 并列，按 detail.kind 为 team_lineup 渲染，其他情况返回空。卡片外框、页码、选项药丸、自由回答框、“确认 / 跳过”、草稿保存、多端同步、过期与被替代状态全部沿用，输入区上方的“Agent 已暂停，等待你的回答”提示行也沿用。

详情内容分两段。上段是阵容：协调者一行，每个成员一行——头像、名称、来源标记（我的 Agent、你点名的、临时创建）、这次任务里的职责、模型与技能数，临时成员有“查看配置”。下段是四到五行键值：预计（只在 AI 提议时出现：时间、相对单人完成的费用倍数）、在哪工作（项目目录）、上限（积分、时长、同时工作的成员数，以及对用户同时聊天的影响）、可以做、不会做。从已有长对话发起时，“在哪工作”一行下面提示历史长度并给出“在新对话中运行”的链接（第 5.3 节）。

选项是“开始”与“不用团队，你自己做”；AI 提议时文案为“用团队做”与“不用，你继续做”。调整阵容不需要学新的操作：在卡片自带的回答框里写一句话，提议方修改后出一张新卡；详情内容的最后一行用一句提示把这件事说明白（“想换人、改预算或放开付费工具，在下面的回答框里直接说”），卡片上不另做调整用的表单控件。回答后的记录沿用 [QuestionAnswered](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/tool/QuestionAnswered.tsx)，和桌面接管一样补一行摘要（几名成员、上限多少）。

**运行中的对话（A4）。** 左边仍是和协调者的普通对话，新增的只有三样，全部复用现有渲染：

- 协调者调用团队工具显示为普通的工具行（[ToolRows](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/ToolRows.tsx)）：加入成员、创建任务、等待成员、验收、结束团队。在 tool-map 里为团队工具登记图标、名称和目标文案即可。
- 团队进度卡复用 [TodoCard](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/TodoCard.tsx) 的外框、标题行、StatusMark、进行中任务下方的进度条和折叠区。在它的基础上每行右侧多一个负责人与状态（待验收、等上游几项、待分派），标题行右侧多成员头像与积分，折叠区里显示该成员当前动作的一行（SubagentLine 的写法），底部多“在右侧查看详情”“暂停”“取消”。任务由协调者维护，所以不显示待办卡片的增删按钮。一个对话同一时间只有一张进度卡，随事件更新，不重复追加。
- 成员变动是一条分隔线（StepDivider 的样式）：协调者新增了临时成员、某成员已退役、团队已暂停及原因。

完成之后（A6）：进度卡收成一行已完成状态，带“另存为团队模板”“保存某个临时成员到我的 Agent”和“费用明细”；协调者的总结是普通回复；交付文件用现有的 ResultArtifacts。

**成员的执行过程（A5）。** 不新做“成员执行视图”。成员会话本来就是会话，用 ChatRoute 的只读状态打开：Topbar 带返回，标题是成员名、副标题是所属团队对话；对话流顶部一条分隔线写明来自协调者的任务；输入区换成现有的一行只读提示，文案为“这是团队成员的会话，只读。要补充要求，请回到团队对话告诉协调者。”只读判断在现有的 isReadOnlySession 上增加会话 kind 为 team_member 的情形。

### 13.3 右侧工作台的“团队”页签与阵容连线图

右侧工作台（[PanelTabBar](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/components/PanelTabBar.tsx)、[panel store](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/stores/panel.ts)）的 TabKind 增加 team，与文件、终端、浏览器、桌面并列，在 TAB_GLYPH 与新建页签菜单里各登记一项。会话存在团队运行时，这个页签自动出现；点进度卡上的“在右侧查看详情”、从运行记录点进来，都停在这个页签。

页签内用筛选药丸切四个子页，右上角是运行状态与已运行时间：

| 子页 | 内容 |
|---|---|
| 阵容（默认） | 连线图，加选中成员的详情卡 |
| 任务 | 全部任务，样式同进度卡；展开一项看依赖、验收方式、各次尝试与等待原因 |
| 消息 | 成员之间、成员与协调者之间的消息，按时间排列；可按某个成员或某一对成员筛选 |
| 成果 | 已登记的成果，预览与下载；运行起止之间的文件差异；费用明细（协调、成员、重试三类） |

~~~mermaid
flowchart LR
    Card["对话里的团队进度卡"] -->|"在右侧查看详情"| Tab["右侧工作台：团队页签"]
    Record["运行记录里点开一行"] --> Tab
    Tab --> Lineup["阵容：连线图与成员详情卡"]
    Tab --> Tasks["任务"]
    Tab --> Messages["消息"]
    Tab --> Outputs["成果与费用"]
    Lineup -->|"点成员"| Detail["详情卡：技能、当前任务、最近消息"]
    Lineup -->|"点连线"| Messages
    Detail -->|"点当前任务"| Tasks
    Detail -->|"查看执行过程"| Member["成员会话，只读"]
    Detail -->|"看它的消息"| Messages
~~~

**连线图的画法。** 图只回答两个问题：队里有谁、谁和谁在往来。它不是流程画布，不能拖拽、不能缩放、不能编辑。

- 布局固定：协调者在上方居中，成员在下方排成一行，超过 5 名时排成两行。成员数上限是 8（含协调者），不需要自动布局算法，也不引入图形库；用一个 SVG 画线，节点是绝对定位的普通按钮。
- 节点：头像、名称、一行状态（当前任务名、已完成几项、等上游几项、临时 · 刚加入），头像右下角的 6px 状态点沿用全局含义：执行中 accent、完成 sage、异常 danger、空闲 n400。
- 连线两种：实线表示协调者给它派过任务；虚线表示两个成员互相发过消息，线上的数字是条数。线的有无和数字来自团队状态投影里的 links 摘要（第 12.4 节），由任务的负责人和消息的收发双方统计得出，不另存数据。
- 协调者中途加人，图上多一个点，对话里同时多一条分隔线；成员退役后节点变灰并保留，历史往来仍然可查。

**交互只有三种**，都是“点了就看到相关内容”，没有隐藏手势：

1. 点成员：节点出现选中环，与它相连的线变为强调色，图下方出现它的详情卡——名称与来源、模型与工具范围、技能、当前任务、最近一条消息，以及“查看执行过程”“看它的消息”两个按钮。点协调者显示的是团队总览：目标、预算与用量、已运行时间、暂停与取消。
2. 点连线或线上的数字：切到“消息”子页，并只看这两个成员之间的消息，筛选条件以一个可关闭的药丸显示在列表上方。
3. 点详情卡里的当前任务：切到“任务”子页并展开这一项。

节点和线上的数字都是可聚焦的按钮，键盘可达；图有等价的文字版本：同一份数据在窄屏和移动端直接显示为成员列表加同一张详情卡，不画图。面向普通用户的产品多数用“每个 Agent 一行”的列表展示进度，连线图主要出现在开发者工具里（第 3.5 节）；这里保留图是因为它能一眼看出谁和谁在往来，但不看图也能完成全部操作。

面板展示“等待用户、等待成员、等待后台任务、等待执行名额、预算不足、结果未知”等真实原因；暂停和取消尚未到达安全边界时显示处理中状态。运行处于活动状态时，根会话和所在项目的删除入口置灰并说明需要先取消运行。

### 13.4 Agent 团队页：我的 Agent、团队模板

左栏在技能中心与定时任务之间增加“Agent 团队”入口，页面内三个页签：我的 Agent、团队模板、运行记录（第 13.8 节）。版式与技能中心相同。

**我的 Agent（B1）。** 列表行显示图标、名称、状态标记（已启用、草稿、AI 创建、含付费工具）、适用场景一行、模型、技能数与工具范围；行内操作为试一试、编辑、更多（复制、版本记录、归档）。右上角“对话创建”和“新建”，后者的菜单含手动创建、一句话起草、复制已有（第 6.5 节）。市面产品的 Agent 库多用卡片网格，这里不跟：同级的技能中心、定时任务都是列表行，同一个产品里并存两种列表样式更别扭。

**新建与编辑 Agent（B2）。** 独立页面，左栏配置，右栏试一试。

- 左栏字段顺序和叫法跟着用户已经熟悉的来：名称、图标、简介；适用场景（写给协调者看，它靠这段话决定什么时候把任务交给这个 Agent）；人设与指令；技能（只用所选 / 全部可用，缺少所需工具时提示并可一键加入）；工具（四个预设）；高级设置里是模型、MCP 服务、示例任务、输出结构、步数与时限。“适用场景”和“人设与指令”旁边各有一个“AI 优化”，逐字段的一键优化是中文产品的通行做法。
- 右栏不是另做的小聊天框，而是把聊天页原样嵌进来。实现上把 ChatRoute 的主体抽成一个 ChatSurface 组件（ChatFlow、等待提示行、错误提示、Composer），聊天页和这里共用；试运行会话只有一个可用 Agent，模式选择器按现有逻辑自动不显示（ModePicker 在选项少于两个时不渲染）。右栏顶部一行显示“当前草稿 · 几个技能 · 几个工具”和“重新开始”，改了配置点它即按新草稿重开会话。
- 草稿自动保存，Topbar 右侧显示“草稿已自动保存 · 线上仍是 v1”和主按钮“发布并启用”。左配置右调试、发布在右上角、草稿与发布分开，是查到的所有图形界面产品的共同排法（第 3.5 节）。窄屏时右栏收成一个“试一试”按钮，点开为全屏对话。

**对话创建 Agent 的确认卡（B3）。** 与组队方案卡是同一种做法：QuestionDock 加一个 AgentProposalDetail（detail.kind 为 agent_proposal），内容为图标、名称、简介、适用场景、技能、工具范围（桌面类和付费类有醒目标记）、人设与指令的展开链接、“去表单里修改”。选项为启用、先存为草稿、不要；想改哪里在回答框里写一句话，AI 改完再出一张卡。由聊天创建的定义在列表里带“AI 创建”标记和来源会话的链接。设置里有“低风险的 Agent 免确认”偏好，默认关闭（第 6.5 节）。

**团队模板（B4、B5）。** 团队模板就是一支存下来的队伍，对应第 6.2 节的 TeamDefinition。三样东西的关系是：Agent 是一个角色，团队模板是一支队伍的编制，团队运行是这支队伍接的一次活。模板本身不运行、没有状态。

- 列表行：成员头像叠放、名称、用途、成员名单、预算上限、运行过几次；允许补充的模板带“协调者可补充成员”标记；操作为运行、编辑、更多（复制、版本记录、归档）。
- 编辑页三张卡：基本信息（名称、用途）；成员——协调者一行（平台默认，可换模型），每个成员一行（头像、名称、在这支队伍里的职责补充与追加的技能、模型覆盖、更多菜单；停用、移除、改别名在更多菜单里，停用的成员行带“已停用”标记），“添加成员”从我的 Agent 里选，最后一行是“允许协调者按需要补充成员”勾选项（圆形勾选，与待办卡片和 Skill 选择器同款），说明文字写明“不勾选时只用上面的名单；名单为空时由协调者全权决定需要谁；每次运行还可以在聊天框里临时更改”；范围与上限——三个折叠行：预算与最长运行时间，成员能用的模型、技能与工具（含是否允许付费工具，以及补充成员时是否允许临时创建），验收方式与最终交付格式。名单为空且未勾选时不能保存。
- 编队用成员名单加每人一句“什么时候用它”，不用画布：面向业务用户的产品（Copilot Studio、元器、扣子 3.0、Bedrock）都是这种排法，画布只出现在低代码和开发者工具里。
- 配置页直接显示能力缺失、模型冲突和工具不可委派原因（数据来自 POST /api/team-runs/preview），不让用户执行到中途才发现明显的静态配置错误。预算、并发、最长运行时间提供合理默认值。
- 点“运行”：与定时任务的“对话创建”相同的弹窗（[ChatCreateDialog](/Users/wang/workspace/OpenBox/frontend-v2/src/features/cron/components/ChatCreateDialog.tsx)），选项目后新开一个对话，团队模式和这个模板已经选好，光标停在输入框里；之后就是第 13.2 节的流程。
- 模板有三个来源：在这里新建；把一次运行整体另存（第 8.4 节），协调者自动组出来的好阵容就是这样留下来的；复制已有模板。

### 13.5 与 Trace 的边界

用户团队面板读取团队业务 API。不得复用或放开超管 /api/admin/trajectories 接口。TeamEvent 可以异步关联 TraceContext 便于管理员排障，但 Trace 关闭或丢失时，组队、状态和恢复必须正常工作。

### 13.6 客户端覆盖

Web frontend-v2 完成配置与运行管理。移动端支持在输入区选团队模式与已保存的团队模板发起、查看进度与成果、回答问题、暂停与取消；Agent 与模板的编辑通过 Web 完成，界面应明确入口。组队方案卡和回答问题都不需要移动端新做界面：它们是根会话里的提问卡片，走的是移动端已经支持的提问卡片和推送（第 7.6 节），移动端未实现专用详情之前，阵容以文字列在问题正文里。移动端的运行视图原定为成员列表加详情、不画连线图；2026-09-21 用户要求与 Web 一致，移动端现已实现同一张连线图，列表版本移除（实现记录有当日条目）。不提供成员会话的逐步查看，成员的工具细节在手机上不便查看，与移动端不做会话追踪页面的既有取舍一致。

团队在根会话里没有专用的消息类型：协调者的团队工具调用是普通的工具 part，团队进度卡由客户端根据团队状态渲染，锚定在当前轮里第一个团队任务工具调用的位置（与待办卡片锚定在 todo 工具调用处相同）。未识别团队工具的旧客户端把它们显示为普通工具行，协调者的进度说明与最终答复是普通文本，可读性不受影响。成员会话不进入普通会话列表（现有的列表过滤已保证），其用量正常计费，不能被当作 cron 免计费会话。

### 13.7 视觉与组件：与现有页面保持一致

新页面不引入新的视觉语言，全部使用 frontend-v2 现有的设计令牌、共享组件和页面骨架。界面稿 [docs/mockups/agent-team-ui.html](mockups/agent-team-ui.html) 里的颜色、字号、圆角、阴影逐项取自 [tokens.css](/Users/wang/workspace/OpenBox/frontend-v2/src/styles/tokens.css)，卡片文案（确认、跳过、或者自己写一个答案、Agent 已暂停，等待你的回答）取自现有的 i18n 资源，浅色与深色都已核对。

[工程规范](/Users/wang/workspace/OpenBox/frontend-v2/docs/ENGINEERING_SPEC.md) 第 9 节的硬约束同样适用：颜色、间距、圆角、字号、阴影全部来自 token，className 里不写颜色任意值；不写 isDark 分支，深色靠 token 反色；方向相关样式用逻辑属性（ms、me、ps、pe、start、end）；尺寸用 rem 系，随字号档位缩放；新增视觉在默认主题的浅色与深色下验收；文案全部进 i18n 资源，命名空间按 feature 划分（agents、teams），聊天里新增的文案进现有的 chat 命名空间。

代码整洁的判断标准只有一条：团队功能在聊天页里不允许出现“if 团队则换一套组件”的分支。允许的改动形式只有四种——现有组件的数据里多一项（模式、@ 分组、页签种类、工具登记）、现有卡片多一个按 kind 渲染的详情组件、现有卡片外框里多一列或多一行、把现有页面主体抽成可复用组件。下表逐项对应：

| 新界面 | 对照的现有实现 | 改动形式 |
|---|---|---|
| 输入区的“团队”模式 | [ModePicker](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/composer/ModePicker.tsx) | 不改组件：选项来自服务端的 Agent 列表，只补两条文案 |
| 队伍选择器 TeamPicker | VideoModelPicker 与 ModelPicker 的并排方式；[Menu](/Users/wang/workspace/OpenBox/frontend-v2/src/shared/ui/Menu.tsx) 与 MenuItem | 新增一个选择器，结构照抄 ModePicker：药丸按钮加向上弹出的 Menu，菜单项为名称加一行 text-xs 说明；“允许协调者补充成员”是同一种带勾的 MenuItem。代码库里没有开关（switch）控件，不为团队新增 |
| @ 菜单里的“我的 Agent”“团队模板” | [MentionMenu](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/composer/MentionMenu.tsx) 现有的资源、文件、技能、命令分组 | 多两个分组，行样式不变 |
| 组队方案卡、对话创建 Agent 的确认卡 | [QuestionDock](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/QuestionDock.tsx) 及其详情组件 [VideoApprovalDetail](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/VideoApprovalDetail.tsx)、DesktopTakeoverDetail | 各新增一个详情组件（TeamLineupDetail、AgentProposalDetail），内容块为 bg-n100、rounded-lg、p-3；卡片外框（max-w-190、rounded-xl、border-hair、bg-card、p-4、shadow-sm）、选项药丸（选中为 border-accent、bg-a100、text-a800）、回答框、按钮全部不动 |
| 回答后的记录 | [QuestionAnswered](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/tool/QuestionAnswered.tsx) | 与桌面接管相同：多一行摘要 |
| 团队工具的工具行 | [ToolRows](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/ToolRows.tsx) 与 tool-map | 登记图标、名称、目标文案；24px 图标块、80px 名称列、等宽目标、右侧用时 |
| 团队进度卡 | [TodoCard](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/TodoCard.tsx)：max-w-165、rounded-xl、p-4，StatusMark，进行中任务下方 4px 高的 accent 进度条，折叠区 | 行右侧多负责人与状态一列，标题行多头像与积分，底部多一行操作；折叠区里的成员当前动作沿用 [SubagentLine](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/SubagentLine.tsx) 的写法 |
| 成员变动、暂停原因 | [StepDivider](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/StepDivider.tsx)、InterruptionDivider | 新增文案 |
| 交付文件 | [ResultArtifacts](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/ResultArtifacts.tsx) | 不改 |
| 成员会话 | ChatRoute 的只读状态（ComposerAccess）、[Topbar](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workspace/components/Topbar.tsx) 的返回 | isReadOnlySession 增加 team_member 的情形，只读提示增加一条文案 |
| 右侧“团队”页签 | [PanelTabBar](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/components/PanelTabBar.tsx)、[panel store](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/stores/panel.ts) 的 TabKind、[glyphs](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/utils/glyphs.ts) | TabKind 多一个取值；52px 页签条、32px 药丸页签；子页用筛选药丸切换 |
| 阵容连线图 | 没有现成组件；节点头像、6px 状态点、Badge、详情卡都用现有样式 | 唯一新增的视觉元素：一个 SVG 画 1.5px 的 n400 线（虚线为 4 4），选中相关的线与数字用 accent；节点选中环为 2px ink；不引入图形库 |
| 左栏入口“Agent 团队” | [Sidebar](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workspace/components/Sidebar.tsx) 里的 NavRow，放在技能中心与定时任务之间 | 40px 高的圆角行、图标列、选中态 bg-n200 |
| 页面骨架 | [SkillsRoute](/Users/wang/workspace/OpenBox/frontend-v2/src/routes/skills/SkillsRoute.tsx)、CronRoute | 单列居中，最大宽度 820px；Topbar 显示标题与副标题 |
| 我的 Agent、团队模板、运行记录三个列表（第 13.4 节、第 13.8 节） | 技能中心的 [SkillCenterToolbar](/Users/wang/workspace/OpenBox/frontend-v2/src/features/skills-center/components/SkillCenterToolbar.tsx) 与 [EntryRow](/Users/wang/workspace/OpenBox/frontend-v2/src/features/skills-center/components/EntryRow.tsx) | 黑色药丸页签；右侧“对话创建”（黑色药丸）加“新建”（发丝线药丸）；圆角搜索框加筛选；列表行为 rounded-xl、bg-hairsoft 半透明底、左侧方形图标、名称加 Badge、单行描述、右侧图标按钮 |
| 状态与标记 | EntryRow 的 Badge、定时任务卡片的 StateDot | 已启用用 ok 色，草稿用 muted，AI 创建、含付费工具、交付物用 warn，错误用 danger；成员执行状态用 6px 圆点：执行中 accent、完成 sage、异常 danger、空闲 n400 |
| 新建入口、模板的“运行” | [CronPage](/Users/wang/workspace/OpenBox/frontend-v2/src/features/cron/components/CronPage.tsx) 的“新建”菜单与 [ChatCreateDialog](/Users/wang/workspace/OpenBox/frontend-v2/src/features/cron/components/ChatCreateDialog.tsx) | “新建”下拉菜单含手动创建、一句话起草、复制已有；“对话创建”和模板的“运行”与定时任务的做法相同：选项目，新开一个会话 |
| 新建与编辑 Agent | 左栏字段样式取自 [CronJobForm](/Users/wang/workspace/OpenBox/frontend-v2/src/features/cron/components/CronJobForm.tsx)，分组卡片取自定时任务卡片，高级设置的行取自设置页的 SettingsRow；右栏是从 ChatRoute 抽出的 ChatSurface | 白底发丝线卡片（rounded-lg、border-hair、bg-card）；标签 text-xs text-n600；输入框 min-h-9、rounded-lg、聚焦时边框变 ink；主操作“发布并启用”为 Topbar 右侧的黑色药丸 |
| Skill 选择器 | 技能中心的搜索框与列表行、待办卡片的圆形勾选 | 已选项为药丸形 chip；候选行左侧 18px 圆形勾选，选中为 s600 底白勾；“只用所选 / 全部可用”用筛选药丸；缺少工具的提示用 n100 底的提示条，动作为小号黑色药丸 |
| 工具预设 | 设置页的选项卡片 | 四个等宽卡片，选中态为 ink 边框加 n100 底 |
| 另存为模板、删除确认 | [Dialog](/Users/wang/workspace/OpenBox/frontend-v2/src/shared/ui/Dialog.tsx) 及其 DialogTitle、DialogBody、DialogActions | rounded-2xl、shadow-pop、text-2xl 标题；右下角为文字“取消”加黑色药丸主按钮；危险操作的主按钮为 bg-danger |
| 加载、空状态、错误 | 定时任务页 | Spinner 加 text-sm text-n600；空状态为一张白底卡片，标题加一句说明；错误为 text-danger |

图标只用 lucide-react。Agent 的头像用 emoji 或 DisplayIcon，与技能中心的 EntryIcon 一致，不引入插画。动效只用现有的 fade-up 与 pulse-dot，并遵守 prefers-reduced-motion。

移动端沿用各自现有的组件：方案卡与确认卡在未实现专用详情时按普通提问卡片显示（问题、选项、回答框都在，阵容以文字列在问题正文里），进度卡用聊天里已有的卡片样式，运行视图用现有的列表与状态点，不画连线图。

### 13.8 运行记录

“运行记录”页签跨项目列出用户在当前 workspace 里的每一次团队运行。它解决三件事：找到需要自己处理的运行，回看做过什么、花了多少，把用得好的运行变成可复用的东西。团队运行本身也是项目下的会话，会出现在左栏的项目树里；项目树按项目找，这个页签按状态和成本找。

每行显示：

| 内容 | 来源 |
|---|---|
| 目标，取用户发起时的第一句话或根会话标题 | team_runs 的 title |
| 状态：运行中、已暂停及原因（预算用尽、时长用尽、协调者出错、停滞、用户手动）、等你回答、已完成、已取消、失败 | team_runs 的 state 与 pause_reason；“等你回答”来自根会话是否有待回答的问题 |
| 最新进展一行，例如“口播脚本写手正在改写第 4 条，合规审校已通过前 3 条”；暂停时写还差什么、怎么继续 | 摘要字段，取协调者最近一次面向用户的进度说明的首句，没有时由任务状态拼出 |
| 所在项目；来自哪个模板，或“自动组队” | team_runs 的 project_id、template_id 与策略快照 |
| 成员头像 | 摘要字段 |
| 已用积分与上限。只显示积分，不显示 token；协调、成员、重试三类各占多少放在“费用明细”里，不放在列表上 | 按成员会话汇总的 usage_events，结果缓存在摘要里 |
| 开始时间、已运行或总用时 | 时间戳 |

排序与分组：需要用户处理的在最前，其次是进行中，最后是已结束，组内按开始时间倒序。筛选为全部、进行中、需要处理、已结束，另可按项目和模板筛选。

每行的主操作随状态变化：预算用尽是“追加预算”，等你回答是“去回答”，运行中是“暂停”，已完成是“再运行一次”（在同一个项目下新开一个对话，团队模式、同一个模板和原来的目标已经填好，发送后走组队方案卡），没有模板的已结束运行是“另存为模板”。更多菜单里有继续、取消、另存为团队模板、查看费用明细、删除。

点开一行不进入新页面，而是打开那次运行的根会话，右侧工作台停在“团队”页签（第 13.3 节）：阵容、任务、消息、成果，成果子页里有运行起止之间的文件差异和费用明细。这与市面上任务列表的习惯一致：标题加最新进展加时间的列表，点开就是那次对话。成员的逐步执行过程从阵容里点成员进入它的只读会话查看，运行记录不平铺成员会话。

运行记录不是独立保存的历史。运行依附于根会话（不变式 16），删除一条运行记录就是删除它的根会话，确认框写明会一并删除成员会话和事件流；已另存出去的 Agent 和模板不受影响。运行处于活动状态时不能删除。

列表只读 team_runs 行，不回放事件流：成员数、任务数、成果数、是否需要处理这些摘要字段在追加事件的同一事务里更新，用量在每次软预算检查和进入终态时刷新。

<a id="changes"></a>
## 14. 现有代码改造清单

### 14.1 新增模块

以下目录是计划新增，尚未存在；文件名表示建议的职责划分。

~~~text
/Users/wang/workspace/OpenBox/
  backend/
    agent_catalog/
      schemas.py          定义与版本契约
      repository.py       用户目录和版本存取
      compiler.py         模型、Skill、能力快照编译；保存、试运行、成员接纳共用
      drafting.py         由一句话描述生成定义草稿
    agent_team/
      schemas.py          团队命令、事件种类与载荷、回放状态
      events.py           事件流的追加、读取、事件键与序号分配
      state.py            fold：事件流回放为名册、任务板、邮箱、等待、预算占用
      invariant.py        追加前对照当前状态校验候选事件
      service.py          统一命令入口：锁运行行、校验、追加、更新缓存
      roster.py           成员接纳、退役
      task_board.py       任务、依赖、attempt 与验收
      mailbox.py          发送、去重、回执
      scheduler.py        对账、补投、唤醒、派发、收尾；并发与独占分组
      liveness.py         停滞判定、催促与隐式提交
      recovery.py         恢复服务中的团队阶段
      authority.py        TeamRunGrant、成员授权与非交互权限解析
      artifacts.py        成果关系与版本
      budget.py           用量汇总与软上限；付费工具的预留、结算与释放
      templates.py        临时成员另存、整支团队另存为模板
      lineup.py           team_propose 的校验与出卡、team_request 约束检查、team_lineup 续跑（创建运行、接纳成员、切换会话 Agent）
      projection.py       事件与回放状态到用户可见视图的投影
      adapters/base.py    RuntimeAdapter 接口与能力声明
      adapters/openbox.py OpenBox 执行适配器：与 Driver、Inbox、Session 的唯一接触面
    agent/runtime_binding.py 统一运行配置与授权加载
    skill/snapshot.py        成员 Skill 内容与资源的按摘要快照
    db/models/agent_definition.py   agent_definitions、agent_definition_versions、team_definitions、team_definition_versions
    db/models/agent_team.py         team_runs、team_events
    api/agent_definitions.py
    api/agent_teams.py
    tool/agent_team.py              团队工具，含 team_propose
    tool/agent_manage.py            普通聊天里创建与修改 Agent 定义的工具
  frontend-v2/src/
    features/agents/                我的 Agent 列表、编辑页、Skill 选择器、AgentProposalDetail
    features/teams/
      components/TeamPicker.tsx         输入区的队伍选择器
      components/TeamLineupDetail.tsx   组队方案卡的详情内容
      components/TeamProgressCard.tsx   在待办卡片外框上扩展的团队进度卡
      components/TeamPanel.tsx          右侧工作台的团队页签：阵容、任务、消息、成果
      components/LineupGraph.tsx        阵容连线图：一个 SVG 加按钮节点，不引入图形库
      components/MemberDetailCard.tsx   选中成员的详情卡，窄屏与移动端的列表视图共用
      components/TemplateList.tsx、TemplateEditor.tsx、RunList.tsx
    features/chat/components/ChatSurface.tsx   从 ChatRoute 抽出的聊天主体，聊天页与试运行右栏共用
    routes/agents/
    routes/teams/
  mobile/lib/features/teams/
~~~

### 14.2 既有代码改造表

| 文件 / 模块 | 具体改造 | 兼容要求 |
|---|---|---|
| [agent.py](/Users/wang/workspace/OpenBox/backend/agent/agent.py) | 定义编译成 AgentDef；协调者增加管理工具，普通成员使用独立团队提示协议 | 不修改全局注册表来冒充不同用户的 Agent |
| [core/config.py](/Users/wang/workspace/OpenBox/backend/core/config.py) | 团队开关、部署上限（team_reserved_agent_slots、team_max_running_members）、允许模型/工具。部署配置里的 AgentOverride 以 legacy 来源只读映射进目录（第 6.1 节） | 不把部署级配置当作用户可写数据库 |
| [subagent_composition.py](/Users/wang/workspace/OpenBox/backend/agent/subagent_composition.py) | 提取可复用校验；团队编译支持 default/locked 与版本快照 | 旧 Task 的模型锁定、权限、专业工具限制不自动放宽 |
| [subagent_authority.py](/Users/wang/workspace/OpenBox/backend/agent/subagent_authority.py) | load_subagent_authority 成为按会话类型分流的加载点，团队成员和协调者返回同构的 SubagentAuthority（第 5.3 节）；legacy loader 保留原逻辑 | 未绑定的 parented Session 继续 fail closed |
| [loop.py](/Users/wang/workspace/OpenBox/backend/agent/loop.py)、[processor.py](/Users/wang/workspace/OpenBox/backend/agent/processor.py) | 加载点改为调用分流后的 loader；ToolContext 绑定真实成员与 attempt；processor 识别 turn_yield 元数据并结束本轮，与 plan_ready 同一处；团队会话本轮结束时回调 liveness（隐式等待、催促） | 所有普通聊天、旧 Task 使用原行为；消费授权结构的代码不改 |
| [mcp_tool.py](/Users/wang/workspace/OpenBox/backend/tool/mcp_tool.py)、subagent_authority 的 restrict_tools | 团队成员的 MCP 边界按服务名与工具名模式匹配，而不是按接纳时冻结的工具 ID；mcp_call_tool 与 MCP 资源工具在执行路径里再检查一次模式 | 普通会话和旧 Task 子会话的 MCP 行为不变 |
| [platform_plugins.py](/Users/wang/workspace/OpenBox/backend/tool/platform_plugins.py) | 插件清单为每个工具增加可委派标记与 exclusive_group，未标明即不可委派 | 清单缺少该字段的现有插件照常加载，只是不进入团队 |
| [tool_exposure.py](/Users/wang/workspace/OpenBox/backend/agent/tool_exposure.py) | 为协调者和成员定义常驻工具集，包含各自的全部团队工具 | 现有 Agent 的常驻集不变 |
| [permission.py](/Users/wang/workspace/OpenBox/backend/permission/permission.py) | 增加非交互解析：ask 先查 TeamRunGrant 预批准，未命中则确定性拒绝并留审计，不进入阻塞等待（第 7.6 节） | 只对团队成员会话生效；普通会话和旧 Task 的审批行为不变 |
| [question.py](/Users/wang/workspace/OpenBox/backend/question/question.py)、[notifications/events.py](/Users/wang/workspace/OpenBox/backend/notifications/events.py) | 不改。成员会话依赖它们现有的 parent_id 判断来保持非交互和不推送 | 这两处判断成为团队的依赖，今后修改需同时考虑成员会话 |
| [question.py](/Users/wang/workspace/OpenBox/backend/question/question.py)、[continuation.py](/Users/wang/workspace/OpenBox/backend/question/continuation.py)、[processor.py](/Users/wang/workspace/OpenBox/backend/agent/processor.py) | 增加两种续跑类型：聊天创建 Agent 的 agent_proposal，组队方案卡的 team_lineup。提问类工具目前写死在三处：QUESTION_TOOL_CONTINUATIONS、ask 里的类型白名单、continuation 的 _apply，三处都要加，processor 里识别提问类工具的集合也要加入 team_propose 与 agent_manage。agent_proposal 应用时锁定草稿定义并核对它仍与卡片内容一致，再执行启用、保留或归档；team_lineup 应用时核对提议摘要，同意则调用 TeamService 创建运行并按 plan_enter 的写法把会话 Agent 切到 team，拒绝则切回 build | 现有的 question、plan_enter、memory_proposal 三种类型不变 |
| [agent.py](/Users/wang/workspace/OpenBox/backend/agent/agent.py) | build Agent 的工具清单加入 agent_manage，并把它加入 BUILD_ONLY_WORKFLOW_TOOLS | 配置定义的 Agent 和子会话不继承它 |
| [agent.py](/Users/wang/workspace/OpenBox/backend/agent/agent.py)、[loop.py](/Users/wang/workspace/OpenBox/backend/agent/loop.py) 的默认权限规则、Agent 列表接口 | 新增内置的 team Agent（协调者），mode 为 primary，因此自动出现在输入区的模式选择器里；team_propose 的默认权限为 deny，build 与 team 显式 allow，与 plan_enter 的做法相同；团队开关关闭时 team 不出现在 Agent 列表里，build 也拿不到 team_propose | plan 与用户自定义 Agent 不获得 team_propose；旧客户端不认识 team 时按名称原样显示 |
| 发消息接口（prompt_async）与 [session.py](/Users/wang/workspace/OpenBox/backend/session/session.py) 的 create_user_message | 接受可选的 team_request，校验模板与 Agent 的归属后保存为用户消息上的一个 part | 不带 team_request 的请求行为不变；不改 sessions 表 |
| [recovery_service.py](/Users/wang/workspace/OpenBox/backend/agent/recovery_service.py) | 在现有阶段之后增加团队阶段，结果计入 AgentRecoveryResult | 现有阶段的顺序和语义不变；团队开关关闭时该阶段为空操作 |
| [session_repo.py](/Users/wang/workspace/OpenBox/backend/db/repository/session_repo.py) | 会话数量统计排除 kind 为 team_member 的会话 | cron 的排除规则不变 |
| [session/session.py](/Users/wang/workspace/OpenBox/backend/session/session.py) | team: 加入 synthetic 消息专用的 client_message_id 保留前缀 | 现有保留前缀不变 |
| [snapshot.py](/Users/wang/workspace/OpenBox/backend/snapshot/snapshot.py)、session/revert | 活动 TeamRun 期间拒绝该项目内所有会话的 revert；成员会话跳过逐步快照；TeamRun 起止各记一次（第 7.5 节） | 普通会话的快照与 revert 不变 |
| [tool_resolution.py](/Users/wang/workspace/OpenBox/backend/agent/tool_resolution.py) | 合并注册工具后施加成员工具/Skill 目录限制，保留现有 permission guards | Skill 内容不能扩大工具暴露或授权 |
| [skill_tool.py](/Users/wang/workspace/OpenBox/backend/tool/skill_tool.py)、[skill.py](/Users/wang/workspace/OpenBox/backend/skill/skill.py) | 成员会话的 Skill 读取经过按摘要的快照层：SKILL.md 在接纳时冻结，附属资源在首次读取时冻结（第 7.2 节） | 普通会话读取 Skill 的行为不变；allowed_tools 仍不授予工具 |
| [task.py](/Users/wang/workspace/OpenBox/backend/tool/task.py) | 可抽出通用会话执行 helper | 旧动作、返回值、前台语义和 descriptor/outbox 恢复契约保持 |
| [subagent_runtime.py](/Users/wang/workspace/OpenBox/backend/agent/subagent_runtime.py) | 公共执行 helper 与团队适配器共享，原父子控制独立 | 同伴通信不走伪装父身份的 follow_up |
| [inbox.py](/Users/wang/workspace/OpenBox/backend/agent/inbox.py)、[Inbox ORM](/Users/wang/workspace/OpenBox/backend/db/models/agent_inbox.py) | 新增 source_type 列和一个供 TeamService 在自己事务内调用的接纳 helper；团队来源落成 synthetic 消息并写来源元数据；去重沿用 client_id | HTTP 用户不能伪造来源或绕过成员快照覆盖模型；source_type 为空的现有行为不变 |
| [driver.py](/Users/wang/workspace/OpenBox/backend/agent/driver.py) | 不改配额逻辑。团队并发在调度器一侧控制，名额不足时调度器按背压处理（第 10.3 节） | Team 不另开不计入额度的模型执行通道 |
| [ToolContext](/Users/wang/workspace/OpenBox/backend/tool/tool.py) | 增加私有 team_run/member/task/attempt 绑定 | 从服务端绑定读取，不接受工具参数覆盖 |
| [effect_ledger.py](/Users/wang/workspace/OpenBox/backend/agent/effect_ledger.py)、video 的 Job 表与 job_recovery | T2 层：外部效果或 Job 记录与团队 attempt 互相引用，沿用各自的 dispatch/reconcile；目前只有 image_gen 接入了 ExternalEffect，视频工具沿用自己的幂等键与 Job 恢复 | outcome_unknown 不自动重复操作 |
| [billing/service.py](/Users/wang/workspace/OpenBox/backend/billing/service.py) | 提供按会话集合汇总已结算用量的查询；付费工具结算时回填团队预留事件所需的计费引用 | 一次实际计费只记一次；结算路径不改 |
| [Session API](/Users/wang/workspace/OpenBox/backend/api/sessions.py) | 团队会话查询、归属与更新限制；成员会话拒绝用户直接发消息、改模型和 revert；根会话的“停止”在活动运行期间转为暂停团队；终态运行后恢复普通会话行为 | 不能通过普通 Session PATCH 修改活跃成员的冻结模型 |
| [project/workspace.py](/Users/wang/workspace/OpenBox/backend/project/workspace.py)、session 删除 | 活动 TeamRun 期间拒绝删除根会话和所在项目；运行结束后删除根会话时一并软删成员会话，硬删除时运行与事件流随外键级联删除 | 现有的“项目内有活动会话则拒绝删除”规则不变 |
| [db/base.py](/Users/wang/workspace/OpenBox/backend/db/base.py)、[ORM 注册](/Users/wang/workspace/OpenBox/backend/db/models/__init__.py) | 注册六张新表、迁移、readiness；双数据库支持；agent_inbox 新列的 SQLite 升级桥 | 旧数据无需批量转换成团队 |
| [main.py](/Users/wang/workspace/OpenBox/backend/main.py) 的路由注册、[ws.py](/Users/wang/workspace/OpenBox/backend/api/ws.py) | 注册团队 API 与用户作用域事件；成员会话的高频事件改为按订阅下发（第 12.4 节）。api/__init__.py 是空文件，路由不在那里注册 | 订阅检查运行所有权；断线可补读；非团队会话的事件路由不变 |
| [前端 router](/Users/wang/workspace/OpenBox/frontend-v2/src/app/router/router.tsx)、[paths](/Users/wang/workspace/OpenBox/frontend-v2/src/shared/router/paths.ts) | Agent/团队入口、运行详情、聊天卡片。app/router/paths.ts 只是转出口，路径常量在 shared/router/paths.ts | 现有路由和旧会话渲染保持可用 |
| [前端 WebSocket 层](/Users/wang/workspace/OpenBox/frontend-v2/src/shared/ws/client.ts)、[events.ts](/Users/wang/workspace/OpenBox/frontend-v2/src/shared/ws/events.ts) | 新增团队事件映射及按水位刷新；成员会话订阅。shared/events/bus.ts 是只有一个事件的界面内总线，不是服务端事件通道 | 不依赖内存事件完整送达 |
| [Composer](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/Composer.tsx)、[MentionMenu](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/composer/MentionMenu.tsx) 与 useMentionMenu、发消息的 hook | 模式为 team 时在模式选择器右侧渲染 TeamPicker；@ 菜单增加“我的 Agent”“团队模板”两个分组；发送时带上 team_request；非团队模式下选中 Agent 时把模式切到 team。ModePicker 本身不改 | 团队开关关闭或 Agent 列表里没有 team 时，这些入口都不出现 |
| [QuestionDock](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/QuestionDock.tsx)、[QuestionAnswered](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/tool/QuestionAnswered.tsx) | 在 VideoApprovalDetail、DesktopTakeoverDetail 旁边挂载 TeamLineupDetail 与 AgentProposalDetail；回答记录增加一行摘要 | 卡片外框、选项、回答框、按钮、草稿与同步逻辑不改；detail.kind 不匹配时两个组件返回空 |
| [TodoCard](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/TodoCard.tsx)、tool-map、[StepDivider](/Users/wang/workspace/OpenBox/frontend-v2/src/features/chat/components/StepDivider.tsx) | 把待办卡片的外框、标题行、StatusMark、进度条、折叠区导出为可复用的部件，TeamProgressCard 在其上增加负责人列与底部操作；团队工具在 tool-map 登记；成员变动分隔线增加文案 | 待办卡片自身的渲染和行为不变 |
| [ChatRoute](/Users/wang/workspace/OpenBox/frontend-v2/src/routes/workspace/ChatRoute.tsx) | 主体抽成 ChatSurface，聊天页与 Agent 编辑页的试运行右栏共用；isReadOnlySession 增加 team_member 的情形并补一条只读文案 | 聊天页的行为与样式不变 |
| [panel store](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/stores/panel.ts)、[PanelTabBar](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/components/PanelTabBar.tsx)、[MenuTab](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/components/MenuTab.tsx)、[glyphs](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workbench/utils/glyphs.ts) | TabKind 增加 team；页签菜单与图标各登记一项；URL 的 panel 参数支持 team，供运行记录和进度卡跳转 | 其他页签不变；会话没有团队运行时菜单里不出现这一项 |
| [Sidebar](/Users/wang/workspace/OpenBox/frontend-v2/src/features/workspace/components/Sidebar.tsx) | 在技能中心与定时任务之间增加“Agent 团队”入口 | 团队开关关闭时不显示 |

### 14.3 容量计划协同

[聊天容量与 Trace 资源隔离计划](/Users/wang/workspace/OpenBox/docs/trajectory-rearch/CHAT_CAPACITY_PLAN.md) 中的独立聊天 worker/持久 Job 能力仍需按实际实施状态判断。

本计划不假设该容量改造已完成：该计划的 P0 到 P3 目前都是待实现，主干上没有聊天 worker 或持久 Job 的代码。团队调度的快路径在 API 进程内触发，慢路径是现有恢复服务里的一个阶段（第 10.3 节），成员运行由现有 Driver 在同一进程内执行；以后迁移到独立 worker 时只替换派工运输层，持久成员、Inbox、状态与幂等身份保持不变。

团队会直接加重容量计划要解决的问题。现有压测的结论是单实例在高并发下先耗尽的是业务数据库连接池（默认 10 加溢出 20），生产环境出现过后端进程被 OOM 终止。每个并发成员都是同一进程里的一个完整 run_loop，占用内存、连接和事件循环时间。因此：

- 进程级上限 team_max_running_members 是硬约束，在 M1 根据实测的单成员内存与连接占用设定，达到上限时新的派发按背压排队。
- TeamService 的事务不跨模型调用和网络调用，调度一轮的查询数与活动运行数成正比而不是与任务数成正比。
- 调度读的是 team_runs 行上的回放缓存，一个活动运行一次主键读取，不需要逐表扫描任务和消息。
- 容量测量显示单进程承载不了预期的并发团队数时，放量受 team_max_running_members 限制，扩大范围依赖容量计划的 worker 拆分；团队层已经按多进程正确性设计，届时不需要改动，也不在团队内部另造一套执行进程。

<a id="delivery"></a>
## 15. 实施阶段与依赖

所有复选框表示未来实施工作，文档完成不等于已完成这些工作。阶段按退出条件推进，不给未经拆解和实测的工期承诺。

六个里程碑全部是必须完成的，没有可选项。它们按风险排序，不按层排序：整个计划里最不确定的两件事是协调者模型能否稳定地拆任务和推进，以及团队在 OpenBox 的真实负载上比单 Agent 好在哪里。持久化和恢复是工作量，不是不确定性。所以先用最少的内核跑通一支真实的团队并立刻评估，用评估结果调默认值和提示协议，再补齐其余部分。排序只决定先后，不改变范围。

| 里程碑 | 交付 | 退出条件 |
|---|---|---|
| M0 底座 | 授权加载点分流、Inbox 来源列与团队接纳 helper、synthetic 落库、turn_yield、非交互权限解析、六张新表、事件流与 fold/invariant 核心、RuntimeAdapter 接口、开关；Inbox 路径的验证 | 普通聊天和旧 Task 回归通过；新入口默认关闭；fold 与 invariant 的性质测试通过；Inbox 路径有真实流量或专项测试的证据 |
| M1 最小可运行团队 | 输入区的团队模式（内置 team Agent）、team_propose 与组队方案卡、由方案卡回答创建运行；成员用内联预设：协调者、成员、消息、任务板与调度器派发、team_wait、team_finish、停滞判定、T0 工具、软预算、暂停与取消；对话里的工具行、团队进度卡与成员变动分隔线；容量测量；首轮评估 | 第 19 节示例用真实模型端到端跑通（三个成员都用内联预设，不含创建临时成员和另存两步）；运行中重启后端能继续推进；得到单成员的内存与连接占用并据此设定进程级上限；首轮评估完成，默认值和提示协议据此调整 |
| M2 Agent 库与三种组队 | AgentDefinition 与版本、legacy 来源、试运行、TeamDefinition 与版本、目录选择、临时成员、名单加“允许补充”的策略、队伍选择器与 @ 点名、普通对话里由 AI 提议、运行中调整阵容、保存临时 Agent、整支团队另存为模板、Agent 团队页 | A01 到 A05、A18 的定义部分、A33、A37、A38、A40、A42 到 A45 通过；三种成员组合走同一套协议测试 |
| M3 工具层与可靠执行 | T1 桌面工具与 exclusive_group 调度；T2 付费工具、付费预授权、严格预留、外部效果关联；Skill 内容冻结；恢复矩阵全部条目；授权撤销；删除语义；多 API 进程专项验证；按恢复矩阵的故障注入 | A09、A12、A14、A15、A18、A19、A22、A23、A34、A36、A39 全部通过；两个后端进程对同一个 PostgreSQL 的专项测试通过；故障注入逐点通过 |
| M4 产品闭环 | 右侧工作台的团队页签与阵容连线图、成员会话只读视图与按订阅下发、试运行右栏复用聊天主体、用量与费用明细、运行记录、移动端运行能力、旧客户端降级、管理员排障关联 | 用户可创建、启动、观察、介入、交付、保存临时 Agent 和另存团队模板；A21、A32、A41、A46、A47 通过 |
| M5 评估与发布 | 完整评估（单 Agent、固定、混合、自动四组对比）、多用户并发下的容量测量、分批放量、回滚演练、运行与恢复说明 | 第 16.1 节全部验收通过，发布准备完成，之后开启正式入口 |

关键路径：M0 → M1 → M2 → M3 → M4 → M5。M2 和 M4 的页面可以在对应契约稳定后与内核并行开发。M1 使用内联成员配置，它与临时成员的内联定义快照是同一种格式，M2 接入 Agent 库时内核不需要返工。可靠性不是后补的“优化”：M1 就要求重启后能继续，M3 把恢复矩阵做全并用故障注入验证。

### 15.1 M0 底座

- [ ] load_subagent_authority 按会话类型分流；kind 为 team_member 的成员会话；以 team_runs 的活动运行索引判定协调者。现有两种运行行为不变。
- [ ] agent_inbox 新增 source_type（Alembic 迁移与 SQLite 升级桥）；供 TeamService 在自身事务内调用的接纳 helper；团队输入落成 synthetic 消息并写来源元数据；team: 保留前缀。
- [ ] processor 识别 turn_yield 元数据并结束本轮。
- [ ] 成员会话的非交互权限解析。
- [ ] 六张新表、readiness、入口开关、部署上限配置。
- [ ] 事件流的追加与读取（序号、事件键、请求摘要）；fold 与 invariant 及其性质测试；state_cache 的写入与重建。
- [ ] RuntimeAdapter 接口与能力声明，OpenBoxRuntimeAdapter 的骨架。
- [ ] 会话数量统计排除 team_member；成员会话拒绝用户直接发消息。
- [ ] 验证 Inbox 路径。团队的全部投递和唤醒都压在这条路径上，而它目前没有线上流量（第 4.1 节）。两种方式任选：让 Web 端“运行中追加消息”在内部账号上改走 delivery 字段并观察一段时间；或补齐覆盖接纳、步骤边界注入、空闲唤醒、15 秒兜底恢复的数据库集成测试。

### 15.2 M1 最小可运行团队

- [ ] 发起路径：内置 team Agent 出现在模式选择器里；team_propose 的校验与出卡；team_lineup 续跑在一个事务里创建运行、冻结策略与授权、接纳成员并切换会话 Agent；拒绝时切回 build。这一阶段方案卡的阵容只有内联预设成员，TeamLineupDetail 先做出阵容与上限两段。
- [ ] TeamService 命令事务：锁运行行、校验、追加事件、更新缓存；事件键幂等。
- [ ] 成员异步接纳与会话创建；成员身份和首个任务写在首条输入里。
- [ ] 定向消息、回执、去重，steer 与 followup 两种接纳。
- [ ] 任务依赖、调度器派发、attempt、submit 原子登记成果、auto 与 coordinator 两种验收。
- [ ] 调度五步与恢复服务里的团队阶段；Driver 名额背压。
- [ ] team_wait、隐式等待、no_progress、唤醒合并与摘要。
- [ ] 停滞判定、催促与隐式提交。
- [ ] 协调者与成员的提示协议；常驻工具集。
- [ ] 软预算、暂停、继续、取消；根会话“停止”转为暂停；活动运行期间拒绝项目内 revert；运行起止快照；每项目一个活动运行。
- [ ] 对话里的呈现：团队工具的工具行登记、在待办卡片外框上扩展的团队进度卡（含暂停与取消）、成员变动与暂停原因的分隔线；团队事件按序号补读。右侧工作台的团队页签在这一阶段只有任务与消息两个只读子页。
- [ ] 容量测量：1、2、3 个并发成员下后端进程的内存增量、数据库连接占用、项目快照锁等待，据此设定 team_max_running_members。
- [ ] 验证各 provider 适配器对“历史中存在未声明工具的调用”的处理（第 5.3 节）。
- [ ] 首轮评估（第 16.3 节）：单 Agent 对固定团队。结论用于调整并发默认值、验收默认方式、合并窗口、停滞阈值和提示协议，写入第 20.1 节。

### 15.3 M2 Agent 库与三种组队

- [ ] AgentDefinition 与版本的存储、访问控制、搜索、启用、归档、复制；全部字段（第 6.1 节）。
- [ ] 创建流程（第 6.5 节）：保存时的编译校验与能力摘要、工具预设、一句话描述起草、草稿版本与发布、试运行会话及其运行绑定。
- [ ] 聊天里让 AI 创建：agent_manage 工具、agent_proposal 续跑类型与确认卡片（Web 专用渲染，移动端按普通提问卡片显示）、来源记录、免确认偏好及其 T0 限制、数量与频次上限。
- [ ] 部署配置的 AgentOverride 以 legacy 来源只读映射进目录，保留 model 锁定语义。
- [ ] 定义编译为 FrozenAgentPreset；模型锁定、覆盖与能力校验。
- [ ] TeamDefinition 与版本、名单加“允许补充”的策略映射（member_selection 与 member_creation）、单次运行的 team_request 改写、配置预览接口。
- [ ] 协调者目录工具、临时角色校验、成员复用、用户定义优先选择；协调者的 skill_search 与 allowed_skills 范围，临时成员的 Skills 选择。
- [ ] Skills 选择：主表单里的 Skill 选择器（与技能中心同源、可搜索）、selected 与 all_accessible 两种方式、Skill 所需工具的提示与一键加入、模板为成员追加 Skills、成员侧的 Skill 目录过滤。
- [ ] 共同目标终态检查；保存单个临时 Agent；整支团队另存为模板。
- [ ] Agent 团队页：我的 Agent、新建与编辑 Agent（左栏表单、Topbar 上的发布并启用、草稿自动保存、逐字段 AI 优化）、团队模板列表与编辑页（名单加“允许补充”勾选项、范围与上限）、模板的“运行”弹窗、版本对比、配置错误提示。
- [ ] 输入区：队伍选择器 TeamPicker、@ 菜单的“我的 Agent”“团队模板”分组、team_request 随消息保存与校验、非团队模式下 @ Agent 自动切到团队模式。
- [ ] 普通对话里由 AI 提议：build Agent 的 team_propose 权限与工具说明、提议被拒后本次对话不再提议、同意后会话切到 team 并由协调者接手。
- [ ] 运行中调整阵容：允许补充时的分隔线提示；不允许时 team_member_start 的类型化错误与 team_propose 的 amend 方式、授权新版本。
- [ ] TeamLineupDetail 与 AgentProposalDetail 两个详情组件、QuestionAnswered 的摘要行；移动端按普通提问卡片显示。

### 15.4 M3 工具层与可靠执行

- [ ] T1：桌面与浏览器工具的委派政策，exclusive_group 调度，桌面租约超时的受阻上报。
- [ ] T2：付费工具的委派政策；组队方案卡上的付费预授权进入 TeamRunGrant；预留、结算、释放事件；超出预授权的上报路径；ExternalEffect 或 Job 记录与 attempt 互相引用；结果未知的核对。
- [ ] MCP 工具与平台插件工具的委派：定义里的 mcp_refs、成员快照里的模式、逐步按模式取交集、元工具执行路径上的二次检查、组队方案卡列出 MCP 服务、插件清单的可委派标记。
- [ ] Skill 内容冻结：接纳时冻结 SKILL.md，首次读取时冻结附属资源，缺失时明确报错。
- [ ] 恢复矩阵全部条目、任务重新分配与旧代次阻断、缓存重建。
- [ ] 授权撤销后对运行中成员的即时收窄；用户追加授权生成新的授权版本。
- [ ] 删除语义：活动运行期间拒绝删除根会话与项目；结束后随根会话删除。
- [ ] 多 API 进程：两个后端进程对同一个 PostgreSQL，验证并发派发、并发命令、跨进程暂停与中断、两个恢复服务同时运行。
- [ ] 故障注入：按第 10.5 节逐点在提交边界终止进程，重启后核对事实与外部调用次数。
- [ ] SQLite 专项：BEGIN IMMEDIATE 下的命令串行、唯一索引的 NULL 语义、agent_inbox 升级桥。

### 15.5 M4 产品闭环

- [ ] 团队模板页签：列表、新建与编辑页、版本记录、复制与归档、运行入口。
- [ ] 运行记录页签：列表接口与摘要字段、分组与筛选、随状态变化的主操作、再运行一次、删除确认。
- [ ] 右侧工作台的团队页签（第 13.3 节）：TabKind 增加 team、panel 参数跳转；阵容子页的连线图（固定布局、links 摘要、三种点击交互、键盘可达、窄屏退化为成员列表）与成员详情卡；点协调者显示团队总览；任务子页含依赖与各次尝试；消息子页含按成员和成员对筛选；成果子页含预览与下载、文件差异、按三类拆分的费用明细；注意事项入口、追加授权与预算。
- [ ] 视觉一致性（第 13.7 节）：逐个界面对照现有实现复用组件与 token，默认主题浅色与深色下验收，通过 ESLint 的颜色任意值拦截与 i18n 检查。
- [ ] 成员会话的只读视图：复用 ChatRoute 的只读状态，Topbar 返回团队对话；WebSocket 按订阅下发成员会话事件。
- [ ] 把 ChatRoute 主体抽成 ChatSurface，Agent 编辑页的试运行右栏改为直接复用它。
- [ ] 移动端：输入区选团队模式与已保存的团队模板发起、组队方案卡按提问卡片回答、进度与成果、成员列表视图、暂停与取消。
- [ ] 旧客户端降级：团队工具按普通工具行显示，组队方案卡按普通提问卡片显示，协调者的进度说明与最终答复为普通文本。
- [ ] 管理员排障：TeamEvent 与 TraceContext 的异步关联，走已有管理员权限与审计。

### 15.6 M5 评估与发布

- [ ] 完整评估：单 Agent、固定团队、混合团队、自动团队四组对比，统计成功率、费用、时延、协议偏差与人工介入。
- [ ] 容量测量：多个用户同时运行团队、团队运行期间普通聊天的首字延迟、消息密集交接、长外部 Job、模型限流下的背压、SQLite 单机、PostgreSQL 多进程。
- [ ] 分批放量、回滚步骤的演练、运维与恢复说明（第 17 节）。
- [ ] 根据实测确定部署容量与默认限制。

<a id="acceptance"></a>
## 16. 验收、测试与评估

### 16.1 必须通过的验收场景

全部场景都必须通过。最后一列标明场景最早在哪个里程碑可验收。

| 编号 | 场景 | 判定条件 | 里程碑 |
|---|---|---|---|
| A01 | 用户创建三个不同职责 Agent 组队 | 定义可保存复用；实例独立；指定模型生效 | M2 |
| A02 | 协调者完全自动组队 | 成员来源为 coordinator；配置可查看；完成后可保存定义 | M2 |
| A03 | 用户成员与临时成员混合 | 共用消息、任务、模型解析和成果协议 | M2 |
| A04 | 锁定模型与非法覆盖 | 接纳前明确拒绝，错误带允许列表，不能静默改用其他模型 | M2 |
| A05 | 同一定义进入两个团队 | 独立 session、任务与授权，历史不串入 | M2 |
| A06 | 忙碌成员收到同伴消息 | 下一安全边界处理，不产生第二个并发 Driver | M1 |
| A07 | 空闲成员冷唤醒 | 不依赖协调者仍有活动父工具；正确加载冻结配置 | M1 |
| A08 | 并行任务与依赖 | 无依赖任务实际重叠执行；下游只在上游 succeeded 后派发；上游失败时协调者被唤醒 | M1 |
| A09 | 两个后端进程同时派发同一任务 | 仅一个有效 attempt，Inbox 只有一条任务输入；另一个进程收到冲突或跳过 | M1 单进程并发；M3 两进程 |
| A10 | 命令响应丢失后重试 | 返回相同成员、任务或消息 ID，事件流里没有第二份事件 | M1 |
| A11 | 投递中崩溃 | Inbox 接纳去重；能补齐回执 | M1 |
| A12 | 执行中崩溃与外部结果未知 | 旧 generation 无法写入；未知效果核对前不重复操作 | M3 |
| A13 | 协调者等待时重启 | 等待登记仍在；结果事件可唤醒，无永久漏事件 | M1 |
| A14 | 暂停、继续、取消 | 无新越界执行；外部操作未确认停止时如实显示；跨进程时同样生效 | M1；跨进程 M3 |
| A15 | 团队预算 | 软上限：达到预算后不再发起新的模型调用，超支不超过并发数乘以单步上界。严格预留：多个成员同时预留按配置上界原子准入，不重复记账 | 软上限 M1；严格预留 M3 |
| A16 | Skill 请求未授权工具 | 工具集合不扩大；文件内容不建立权限 | M1 |
| A17 | 其他用户访问 / 伪造发送者 | API、WS、成果、消息与控制全部拒绝；HTTP 请求不能使用 team: 前缀的 client_id | M1 |
| A18 | 定义或 Skill 在运行中编辑 | 存量成员读到的定义快照和 Skill 内容不变；新运行采用新版本 | 定义 M2；Skill M3 |
| A19 | 账号授权撤销 | 现有运行及时受限，不借旧快照继续调用 | M3 |
| A20 | Trace 关闭或丢失 | 团队业务和恢复正常，用户面板不依赖 Trace | M1 |
| A21 | WebSocket 断线 | 按水位补读或重新取快照，状态无重复倒退；面板没有秒级全量轮询 | M1；完整面板 M4 |
| A22 | 成员一轮结束但后台 Job 未完成 | 任务和团队不被错误标记成功 | M3 |
| A23 | 文件与桌面操作冲突 | 桌面类任务按 exclusive_group 串行；共享目录内的并发编辑由 mtime 检查拒绝后一次写入并要求重读 | 文件 M1；桌面 M3 |
| A24 | 普通聊天及旧 Task | 原模型、权限、返回结构与恢复行为通过回归 | M0 起每个里程碑 |
| A25 | 团队运行期间用户正常聊天 | 团队占用的名额不超过上界，用户发消息不因名额得到 429；名额不足时任务排队并显示原因，名额释放后自动继续 | M1 |
| A26 | 成员遇到需要用户审批的操作 | 不进入阻塞等待；得到 PERMISSION_REQUIRES_USER；任务转为 blocked；协调者在根会话向用户提问；用户同意后后续调用放行 | M1 |
| A27 | 成员做完事直接结束而不提交 | 被催促一次；仍不提交则以最后回复隐式提交并标记 implicit，交协调者验收；团队不因此停滞 | M1 |
| A28 | 没有在途工作时调用 team_wait | 立即返回 no_progress，本轮不结束 | M1 |
| A29 | 多个成员几乎同时完成 | 协调者只被唤醒一次，输入是合并后的摘要；progress 消息不唤醒任何人 | M1 |
| A30 | 活动运行期间对同项目内任一会话 revert，包括与团队无关的普通会话 | 返回 TEAM_ACTIVE，项目目录不被重置 | M1 |
| A31 | 用户在根会话按“停止” | 团队进入 paused，成员在安全边界停下；继续后工作不丢 | M1 |
| A32 | 三个成员同时流式输出 | 未订阅成员会话的连接收不到其高频事件；移动端只收到团队事件 | M4 |
| A33 | 把自动组建的团队另存为模板 | 临时成员进入 Agent 库，模板引用它们；用模板再次运行得到配置相同的固定团队；来源运行被删除后模板和定义仍可用 | M2 |
| A34 | 删除与依附关系 | 活动运行期间删除根会话或项目被拒绝；结束后删除根会话，运行、事件流和成员会话一并删除，Agent 库与模板不受影响；同一项目或同一根会话启动第二个活动团队被拒绝 | M1 约束；M3 删除 |
| A35 | 回放与缓存 | 丢弃 state_cache 后从事件流回放得到完全相同的状态；缓存落后于事件流时自动重建；事件序号出现缺口时回放失败而不是猜测 | M0 |
| A36 | 付费工具预授权 | 未勾选的付费工具对成员不可见；超出单次或总上限的调用得到 PERMISSION_REQUIRES_USER；用户追加授权后放行，授权版本递增 | M3 |
| A37 | 用户在 Agent 库创建 Agent | 一句话起草的结果不经用户保存不落库；创建过程包含 Skills 的选择，候选清单与技能中心一致；选中 Skill 后提示其所需而尚未加入的工具，可一键加入，不会自动加入；保存时不允许的模型、不可委派的工具、不可访问的 Skill 被拒绝并说明原因；draft 定义不出现在协调者目录里，也不能被模板引用；试运行实际生效的模型与工具和该定义进入团队时一致 | M2 |
| A38 | 聊天里让 AI 创建 Agent | 校验不通过时错误返回给模型而不打扰用户；AI 先检索用户已有的 Skill 再提议，提议里包含所选 Skills；通过后定义为 draft 并弹出确认卡片，卡片内容来自服务端校验后的数据，逐个列出 Skills 和工具；选启用后才能被模板引用；关掉卡片则保持 draft 且同一对话不再重复提议；含桌面类或付费类工具的定义即使开了免确认也要走卡片；子会话、团队成员和定时任务会话调用该工具被拒绝；移动端能在普通提问卡片上完成确认 | M2 |
| A39 | 工具、MCP 与 Skill 的边界 | 成员只能看到并调用其定义里列出、且用户在组队方案卡上批准的 MCP 服务的工具；MCP 工具超过 40 个走元工具时，调用范围外的工具被拒绝；MCP 服务中途不可用时成员少几个工具继续运行并留下团队事件；给成员绑定 Skill 不会让它多出任何工具；未标明可委派的平台插件工具对成员不可见 | M3 |
| A40 | 协调者为临时成员选择 Skill | 协调者只能检索到团队策略允许范围内的 Skill；临时成员的接纳事件里记录所选 Skills 及其内容摘要；selected 方式下成员检索和读取不到清单之外的 Skill，all_accessible 方式下可以；组队方案卡显示每个成员的技能数，临时成员可展开查看所选 Skills | M2 |
| A41 | 运行记录 | 列出用户在当前 workspace 的全部团队运行，包含没有模板、在聊天里直接发起的；需要处理的排在最前，原因与实际一致；用量与账单页一致；点开进入根会话并停在团队页签；“再运行一次”新开一个对话并带上同一模板和目标，仍然经过组队方案卡；删除记录即删除根会话，活动中的运行不能删除；列表请求不回放事件流 | M4 |
| A42 | 输入区发起团队 | 模式选“团队”后出现队伍选择器；选模板、改“允许补充成员”、@ 点名 Agent 后发送，team_request 随消息保存；方案卡的阵容包含全部被点名的 Agent；不允许补充时阵容是模板名单的子集；校验不通过的提议不会出卡；非团队模式下 @ Agent 会切到团队模式；不带 team_request 的普通消息行为不变 | M1、M2 |
| A43 | 组队方案卡 | 三个入口出的是同一种提问卡片，外框、选项、回答框、确认与跳过、草稿与多端同步均为提问卡片原有行为；选“开始”后运行、授权与成员接纳在同一事务里出现，会话模式变为团队；选“不用团队”或跳过后没有任何团队数据，会话回到执行模式并由 build 继续完成；在回答框里写调整意见会得到一张新卡；提议在回答前被改过时要求重新确认 | M1、M2 |
| A44 | 普通对话里 AI 提议 | build Agent 只能提议，不能创建运行，也拿不到其余团队工具；同意后协调者接手并能看到此前的对话；拒绝后同一对话里不再提议；团队开关关闭、plan 模式、Task 子会话、定时任务的临时会话里都没有 team_propose | M2 |
| A45 | 运行中调整阵容 | 允许补充时，协调者新增成员不再询问用户，对话里出现分隔线、阵容图多一个节点；不允许补充时，名单外的 team_member_start 返回类型化错误，协调者以 amend 方式出卡，用户同意后授权生成新版本、拒绝则阵容不变 | M2 |
| A46 | 团队页签与连线图 | 会话有团队运行时右侧出现团队页签，运行记录和进度卡的跳转都停在这里；图上的节点与成员一一对应，实线与派发一致，虚线上的数字与消息条数一致；点成员出现详情卡，点连线进入按这两人筛选的消息，点当前任务进入任务子页并展开；节点与线上的数字可用键盘聚焦和触发；窄屏显示为成员列表且功能等价；浅色与深色下都符合 token | M4 |
| A47 | 复用而非特例 | 聊天页代码里没有按“是否团队”切换组件的分支：团队进度卡使用待办卡片导出的部件，方案卡与 Agent 确认卡只是提问卡片的详情组件，成员会话用聊天页的只读状态打开，试运行右栏与聊天页是同一个 ChatSurface；待办卡片、提问卡片、聊天页自身的测试全部照常通过 | M4 |

### 16.2 测试组织

- 纯逻辑单元测试：定义解析、模型选择、权限交集与非交互解析、DAG、任务与运行状态机、停滞判定、唤醒合并、错误码。fold 与 invariant 另加性质测试：任意合法事件序列的回放结果等于逐条应用的结果，缓存等于重新回放，非法候选事件一律被拒绝。这一层数量最多，跑在内存 SQLite 上。
- 数据库集成，用真实 PostgreSQL：运行行锁下的命令串行、事件键幂等、序号唯一、两个活动运行索引的冲突、Inbox 的 client_id 去重、跨租户查询；涉及并发必须用真实数据库验证。SQLite 上重复其中与方言有关的部分：BEGIN IMMEDIATE 串行、唯一索引的 NULL 语义、升级桥。
- 多进程：两个后端进程对同一个 PostgreSQL，覆盖并发派发、并发命令、跨进程暂停与中断、两个恢复服务同时运行。
- FakeProvider/FakeTool 场景：用脚本化的模型回复驱动完整协议，覆盖可控暂停点、消息交错、依赖派发、费用预留、重试、验收返工，以及第 8.5 节的每一种模型偏差。
- 故障注入：按第 10.5 节逐点在提交边界终止进程；重新启动后核对事件流、回放状态与外部调用次数。M1 先覆盖三个点（成员已接纳未启动、消息已入队未投递、协调者等待中），M3 覆盖矩阵全部条目。
- 前端与移动端：三种组队、运行控制、断线补读、模型锁定、成果显示、另存为模板、旧客户端兼容。
- 真实模型评估：验证协调者确实会调用团队工具并按结果推进，并产出第 16.3 节的数据。

测试应验证业务不变式和失败边界，不仅复写实现分支。涉及外部发布等操作的自动化验收使用受控测试适配器；已有真实业务验收遵守项目授权流程。

建议新增测试模块覆盖 agent_catalog、team_state、team_invariant、team_authority、team_mailbox、team_task_board、team_runtime、team_budget、team_api，并运行现有 subagent、inbox、driver、effect ledger 相关回归。

### 16.3 模型协作效果评估

评估做两轮。首轮在 M1 之后立即进行，对比单 Agent 与固定团队，目的是尽早暴露协调质量和提示协议的问题，并据此调整默认值；完整评估在 M5 进行，对比单 Agent、固定团队、混合团队和自动团队四组。评估的结论用来调参数、改提示协议、决定自动启用策略，不用来删减交付范围。

任务集至少三类通用任务：多来源调研、数据分析与报告、代码分析/修改；另加 OpenBox 自己的典型任务：为一个账号做选题调研、脚本和分镜方案并交叉审校，以及在预授权内生成素材（完整评估时）。每类 5 到 10 个任务即可，要的是方向性结论，不是统计显著性。内容生产可以作为用例，但不成为唯一验收标准。

记录：

- 任务成功率、成果准确性、遗漏和返工。
- 总耗时、首个有效成果耗时、关键依赖链耗时。
- 实际模型与工具费用，按协调者开销、成员有效工作、催促与重试三类拆分。
- 无意义消息、重复工作、空闲等待与人工介入次数。
- 协议偏差的发生率：隐式提交、催促、no_progress、STALE_REVISION、名额背压各出现多少次。它们衡量提示协议和工具设计的质量，比成功率更早暴露问题。

评估前先写定参照线，避免看到结果后再找理由：团队在至少两类任务上的成果质量不低于单 Agent，且耗时更短或质量明显更高；团队费用不超过单 Agent 的 3 倍；协调者开销不超过团队总费用的三分之一。未达到参照线的项目进入调整清单（提示协议、验收默认方式、合并窗口、并发默认值、工具结果的形态），调整后复测。参照线可以在评估前修改，不能在评估后修改。

先验证在需要分工的任务上带来收益，再调整自动启用策略。Agent 数量及消息数量不是成功指标。Kimi 的 PARL 研究成果不作为 OpenBox 的性能承诺。

<a id="rollout"></a>
## 17. 发布、兼容与回滚

### 17.1 迁移策略

数据层的改动全部是增量：六张新表，加上 agent_inbox 的一个可空列。不回填、不批量把历史普通会话改造成团队。部署配置中的 Agent 以 legacy 来源只读展示，用户主动另存后进入新定义库。

先发布能识别团队绑定和新来源、但关闭团队接纳的兼容版本；再分批启用团队入口。不识别团队的旧进程不能领取或运行团队成员会话：成员会话带 parent_id 而没有 SubagentDescriptor，旧版本的 load_subagent_authority 会直接拒绝运行它，现有的 fail closed 规则在这里起保护作用。

已有配置 model 的锁定语义保持；新增 UI 才明确区分默认值和锁定。旧权限 snapshot 按旧版本读取，不能自动推断新授权。事件载荷带 schema_version，回放按版本解码，载荷结构以后的变化不需要数据迁移。

### 17.2 开关

分开以下功能开关：

- team_admission_enabled：允许创建新运行。
- team_generated_members_enabled：允许临时成员。
- team_tools_enabled / 逐工具委派名单：控制 T1、T2 各层工具的开放。
- team_ui_enabled：控制页面入口。

已有团队运行的恢复不能仅由 admission 开关决定。关闭新接纳后仍保留消息核对、外部效果核对、取消及必要的收尾能力。

### 17.3 分批放量

内部受控账号和 FakeProvider → 少量真实模型与 T0 工具 → 混合与自动组队 → T1 桌面工具 → 已完成预授权与幂等适配的 T2 付费工具 → 移动端覆盖 → 扩大用户范围。

每一步检查错误率、重复操作、等待泄漏、预算结算、用户任务成功率，以及团队运行期间普通聊天的首字延迟和 429 次数。阈值由放量前基线确定，不能用未经测试的固定吞吐量承诺替代。

### 17.4 回滚

1. 关闭新 TeamRun 接纳。
2. 保留支持团队状态的兼容后端，暂停或按用户意图收尾活跃运行。
3. 完成或隔离外部结果未知与未结清的预算预留。
4. 确认没有处于 running 的成员 attempt 后，才回退到不支持团队的执行版本；否则它们会停在 running，等新版本恢复后由停滞判定处理。
5. 新表保留，默认不做破坏性 downgrade；只读历史能力可由兼容版本提供。

不能回滚到不识别团队绑定的旧二进制后，让它把带 parent_id 的成员当普通会话运行；如第 17.1 节所述，现有规则会拒绝运行这类会话。根会话在旧版本下退回普通聊天，权限不超过用户本来的 build Agent，只是失去协调能力。

回滚步骤在 M5 按上面的顺序完整演练一次，演练记录作为发布准备的一部分。

<a id="risks"></a>
## 18. 风险、容量与后续扩展

| 风险 | 设计应对 |
|---|---|
| 团队在 OpenBox 的负载上不比单 Agent 好 | 第 1.4 节的价值边界；M1 之后立即做首轮评估，未达参照线的项目进入调整清单并复测（第 16.3 节）；自动启用策略以评估结果为准 |
| 协调者拆任务效果不稳定 | 明确职责与交付契约；固定团队可用；协调者没有执行类工具，不会自己包办；真实任务评估后优化自动策略 |
| 模型不按协议调用工具 | 调度器代为领取、隐式等待、催促与隐式提交、no_progress、错误体自带改正所需的数据（第 8.5 节）；评估记录各类偏差的发生率 |
| 协调者的上下文和唤醒次数推高费用 | 值得唤醒的事件分类、合并窗口、摘要输入、中间任务自动验收、轮数上限、默认新建根会话（第 8.6 节、第 5.3 节） |
| 团队占满用户的并发名额，用户自己无法聊天 | 名额按用户计且默认 5；团队使用上界留出余量，名额不足按背压处理（第 6.4 节、第 10.3 节） |
| 单进程后端的内存与连接池被并发成员压垮 | 进程级上限 team_max_running_members；M1 实测后设值；面板不做秒级轮询；成员高频事件按订阅下发（第 14.3 节、第 12.4 节） |
| Inbox 路径缺少线上验证 | M0 先让它承载真实流量或补齐集成测试（第 15.1 节） |
| 成员被审批或提问卡住而无人知晓 | 成员非交互：未预先批准的操作确定性拒绝并上报，协调者是唯一面向用户的出口（第 7.6 节） |
| 共享项目目录和桌面上的并发冲突 | 约定产出子目录、提示性写入范围、mtime 拒绝；桌面类任务按 exclusive_group 串行；活动运行期间禁止 revert（第 7.5 节） |
| 进程级全局状态在并发运行增多时互相干扰 | 现有代码里有多处进程内注册表，例如一次运行结束时调用的 clear_all_claims 会清掉进程内所有运行的指令文件占用记录。M1 的三成员并发测试用来暴露这类问题，发现后在原模块修复 |
| 事件唤醒形成循环 | 消息、任务、时间和轮数上限；确定性的停滞判定（第 8.3 节） |
| 模型或 Skill 变化导致恢复漂移 | 成员快照与能力核对、替代成员显式关联；Skill 只记录摘要并在变化时留事件（第 7.2 节） |
| 权限从普通子 Agent 迁移时被放大 | 新 TeamRunGrant 路径；旧限制保留；工具分层开放；来源与配置双重检查 |
| 同伴消息或抓取的网页内容诱导成员越权 | 同伴内容是数据不是授权（不变式 14）；团队输入不以用户身份落库；权限检查与消息内容无关 |
| API 进程退出丢失后台工作 | 数据库接纳、现有恢复服务里的团队阶段、有效租约与幂等恢复 |
| 外部操作已发生但无法确认 | T2 层逐个工具接入 ExternalEffect 或各自的 Job 幂等机制，预留在核对前不释放，禁止无证据重复提交；发布与登录不委派给成员 |
| 回放缓存与事件流不一致 | 缓存与事件在同一事务里写；cache_seq 与 last_seq 不等即重建；fold 的性质测试保证缓存等于重新回放（A35） |
| 长时间运行使事件流变长、回放变慢 | 任务、成员、消息均有配额，事件总量有上界；正常路径读缓存不回放；完整回放只发生在缓存重建时 |
| 团队事件拖累根会话的内核事件流 | 使用独立的 team_events，不写入 agent_events（第 10.1 节） |
| Trace 与业务状态耦合 | 独立团队业务事件，Trace 保持 fail-open |

容量测量分两次。M1 先回答一个问题：现行单进程部署能同时承载多少个运行中的团队成员，覆盖 1 到 3 个并发成员的内存与连接占用、团队运行期间普通聊天的首字延迟、项目快照锁等待。M5 覆盖多个用户同时运行团队、消息密集交接、长外部 Job、模型限流下的背压、SQLite 单机和 PostgreSQL 多进程。实际并发受已有账户模型额度、工具限额、数据库及沙箱容量共同约束。

后续 A2A Adapter 必须声明能否选择远程模型、取消、恢复和读取成果。外部成员保存独立认证引用及远程任务 ID，不把对方 Agent Card 中的自述能力当作已授权的本地工具。

嵌套团队可在现有 TeamRun 之上增加父任务关联与预算分配；本计划禁止成员隐式创建无限子团队，先把单协调者的生命周期与故障恢复做好。

<a id="example"></a>
## 19. 完整运行示例

目标：“比较三个方案，整理证据，输出对比报告。”

1. 用户在项目的对话里把模式切到“团队”，队伍选“自动组队”，在输入框里 @ 了自己保存的调研 Agent 和分析 Agent（两者分别配置了模型 A、B），写下目标并发送。协调者检索 Agent 库后调用 team_propose；对话里出现组队方案卡，列出成员及各自职责、模型、预算、将预先批准的工具，以及团队最多占用 3 个并发名额。用户点“开始”，运行在同一个事务里创建。
2. 在所选项目下新建一个根会话并创建 TeamRun：写入 team_runs 的一行和事件流的头几条事件，冻结模板版本、用户授权、预设成员版本、允许的模型集合及模型配置；该项目此时没有其他活动团队，创建通过。协调者绑定到这个根会话。
3. 协调者发现需要独立核查，创建临时核查 Agent，选择允许模型池中的模型 C。它先请求了不在允许集合里的模型，得到 MODEL_NOT_ALLOWED 和允许列表后改正。
4. 协调者创建 T1、T2、T3 三项资料收集任务（负责人为调研成员，自动验收）、T4 分析报告和 T5 独立核查（标记为交付物，由协调者验收），T4 依赖 T1 到 T3，T5 依赖 T4。随后调用 team_wait，本轮结束，协调者不再占用执行名额。
5. 调度器把 T1 派发给调研成员：建立 attempt，向其 Inbox 接纳任务输入并唤醒。同一成员同时只执行一个任务，T2、T3 在它提交后依次派发。
6. 调研成员把资料写到约定的产出目录，提交 T1。契约校验通过，任务直接成为 succeeded，协调者不被唤醒。
7. T1 到 T3 全部通过后 T4 自动派发给分析成员。分析成员发现缺一个字段，给调研成员发 question 消息并调用 team_wait；调研成员被冷唤醒、补充资料并回复；分析成员被唤醒后继续。
8. 分析成员提交报告草稿，T4 进入 review。协调者被唤醒，收到的是一段摘要：T1 到 T3 已通过、T4 待验收及其成果引用。它验收通过，T5 被派发给核查成员。
9. 核查成员发现两处数据与来源不符，提交核查结论。协调者据此重新打开 T4 并写明返工要求；T4 建立新的 attempt，T5 随后重新执行。
10. 期间核查成员想执行一条未预先批准的 bash 命令，得到 PERMISSION_REQUIRES_USER，把任务置为 blocked；协调者在根会话里向用户提问，用户同意后任务回到 pending 并重新派发。
11. 返工完成、核查通过，协调者验收 T4、T5 并调用 team_finish。TeamService 检查所有交付物任务为 succeeded、没有待核对效果或阻塞项，提交 completed，并给用户展示成果、运行起止的文件差异和按三类拆分的费用。
12. 用户在运行总览上选择“另存为团队模板”：临时的核查 Agent 存入 Agent 库，新模板引用调研、分析、核查三个定义并带上本次的策略。下次直接运行这个模板，得到一支固定团队。这次运行本身仍然只是根会话名下的一条事件流，根会话删除时随之删除，模板和三个定义不受影响。

如果步骤 6 后进程重启，成员、任务和资料关系由事件流回放恢复；恢复服务的团队阶段在 15 秒内补投未回执的消息、重新唤醒有未领取输入的成员；完成的资料不重复查询。如果某个成员做完事没有提交就结束了本轮，它会被催促一次，之后以最后回复隐式提交。如果用户在团队运行期间继续在别的会话聊天，团队保留的名额余量保证这些请求不会因并发名额被拒绝。

<a id="decisions"></a>
## 20. 决策记录与完成定义

### 20.1 已作出的架构决策

| 决策 | 结论 | 主要依据 |
|---|---|---|
| 团队内核主要参考谁 | DSH Agent Team 的名册、mailbox、task board、协调机制和存储形态 | DSH 源码与设计笔记（第 3.1 节） |
| 是否嵌入 DSH 运行时 | 使用 OpenBox 自有执行底座实现参考机制 | 保留现有 Python、SQLAlchemy、模型适配器和前端技术栈 |
| 如何兼顾三种团队 | 统一定义和运行协议，成员来源与组队策略分别配置 | 三种组合只是配置组合的 UI 预设 |
| 是否支持不同模型 | 支持；每成员解析、校验、冻结独立模型配置 | 已确认目标 |
| Skills 是否继续使用 | 继续，作为成员知识和操作方法；不授予工具权限；内容在接纳时冻结 | 技能与工具解耦方案 |
| 协调者能否创建 Agent | 团队策略允许时，创建运行内角色配置与实例 | 已确认目标 |
| 用户创建的 Agent 存在哪里 | 长期表 agent_definitions 及其版本表，归属用户与 workspace，跨项目复用；团队模板同理 | 它们是用户的长期资产；定义含授权配置，不能放在 Agent 可写的项目目录里 |
| 用户如何创建 Agent | 三个入口：Agent 库表单（可由一句话起草或复制）、普通聊天里由 AI 创建、团队运行中协调者临时创建后另存。保存与成员接纳用同一个编译器校验；AI 写内容，生效要经用户确认，低风险定义可按偏好免确认 | 第 3.5 节的市面对照：各家都是 AI 写内容、人守生效与授权，没有哪家提供完全无人过目的持久 Agent 创建；OpenBox 已有 skill_manage、cron、记忆提议三个先例；定义不是授权，授权发生在启动团队时 |
| 自主组建的团队存在哪里 | 只存在于那次运行里：team_runs 的一行加 team_events 的一条只追加事件流，依附于根会话，随根会话删除 | DSH 把团队存成 Lead 会话日志里的事件并回放；OpenBox 内核的 agent_events 也是同一形态；为一次任务组建的团队是任务的副产品 |
| 事实来源是什么 | 事件流。名册、任务板、邮箱、等待、预算占用由回放得出；缓存可丢弃重建 | 同上；事件保存完整快照，回放简单、可独立检查 |
| 团队事件是否写进根会话的 agent_events | 不写，使用同结构的独立事件流 | 内核在每次模型请求前校验整条事件前缀，混写会让协调者的请求反复重建 |
| 自主组建的团队如何复用 | 保存单个临时 Agent，或把整支团队另存为模板 | 运行记录不是长期实体，复用必须显式另存 |
| 一个项目能否同时跑多个团队 | 不能，每个项目和每个根会话同时最多一个活动团队 | 同项目的所有会话共用一个工作目录 |
| 是否支持同伴直接通信 | 支持，通过服务端验证的定向持久消息；只有一种投递方式 | DSH 的 Steer 决策 |
| 是否直接放宽旧 Task 权限 | 保留旧行为，团队使用独立的显式授权和运行绑定 | BUILD_ONLY_WORKFLOW_TOOLS 的现有保证 |
| 成员能否直接面向用户 | 不能。成员非交互运行，协调者在根会话里是唯一出口 | 现有代码已禁止子会话提问并抑制其通知；权限审批是进程内阻塞等待；DSH 的审批钉定决策 |
| 并发控制的形式 | 每运行串行提交命令；revision 只防过期视图 | DSH journal 的串行事务；团队规模小、命令是短事务 |
| 任务由谁启动 | 调度器在依赖满足时代为领取并唤醒负责人，成员没有 claim 工具 | 任务创建时即有负责人；减少依赖模型遵守的协议步骤 |
| 任务状态的粒度 | 八个持久状态；ready 与 waiting 为推导值 | DSH 把就绪放在视图里；避免两处状态不一致 |
| 等待如何实现 | 工具结果结束本轮，唤醒统一走 Inbox；不复用提问的挂起续跑协议 | plan_ready 的先例；新消息会作废挂起中的检查点 |
| 中间任务是否逐个由协调者验收 | 默认自动验收，只有交付物任务由协调者验收 | 协调者每次被唤醒都是一次完整上下文的模型调用 |
| 共享资源如何互斥 | 不建通用占用表；桌面类任务由调度规则串行，物理互斥靠现有桌面租约；文件冲突靠任务拆分和 mtime 拒绝 | 同一项目共用工作目录、同一 workspace 共用桌面；DSH 拒绝把归属当锁的理由 |
| 创建 Agent 时 Skill 怎么选 | Skill 选择是创建流程里必经的一步，放在主表单，与工具分开选但联动提示；一句话起草和聊天创建时由 AI 先检索再推荐；协调者为临时成员挑选时限于团队策略允许的范围；成员默认只能读取所选的 Skill，也可设为全部可用 | 对 OpenBox 的用户，一个 Agent 会做什么主要取决于它掌握的 Skill；较新的同类产品也把 Skills 作为定义里单独的一项 |
| 工具、MCP、Skill 的关系 | 工具分平台内置、平台插件、MCP 三种来源，都是可调用的工具，走同一套权限与授权交集；Skill 是知识，单独选择，不授予工具。定义里三项分开：tool_allowlist、mcp_refs、skill_refs | 现有代码已明确 Skill 字段不影响运行时工具集；MCP 目录来自用户沙箱且是动态的，必须按服务名与模式引用 |
| 成员可用哪些工具 | T0 知识与文件类、T1 桌面与浏览器类、T2 付费媒体类全部交付；发布、登录和需要用户在场的工具不委派 | 逐个审核的结论；无人值守的付费调用靠启动时的预授权 |
| 预算的严格程度 | token 调用用软上限，事先可知价格的付费工具做严格预留，两层都交付 | 现有计费只有事后结算且默认 shadow；严格程度与单次金额相称 |
| 运行形态 | 按多 API 进程正确性设计并专项验证；现行部署是单进程，进程级并发上限由实测设定 | 容量计划的 worker 拆分尚未开始 |
| 是否需要 A2A | 定义 RuntimeAdapter 接口与能力声明，只实现 OpenBox 适配器；外部接入属于第 1.3 节的扩展 | A2A 不能替代平台自己的 Agent 库、任务所有权、预算和恢复 |
| 团队拓扑 | 单协调者、扁平成员，可动态追加和退役 | DSH 的扁平名册 |
| 自动组队是否依赖训练模型 | 使用现有模型和结构化工具，通过评估迭代策略 | Kimi 的 PARL 收益不能靠复制框架获得 |
| 团队如何发起 | 团队是普通会话的一种模式。三个入口走到同一张组队方案卡：输入区选团队模式、团队模板上点运行、普通对话里由 AI 提议；运行只在用户于方案卡上同意后创建，AI 只能提议不能自行组队；没有单独的启动接口 | plan_enter 的先例（提议、确认卡、切换会话 Agent）；DSH 的显式委派策略；多 Agent 增加延迟和费用，是否值得由用户决定 |
| 用户怎么选“组队方式” | 不让用户在固定、混合、自动三个名词里选。只有一份成员名单和一个“允许协调者按需要补充成员”的勾选项，模板里设默认值，每次运行可在输入区临时改；@ 点名的 Agent 一定在队里 | 用户反馈：三选一把用法限死，且无法从聊天框引用；后端的 member_selection 与 member_creation 仍然正交 |
| 启动确认用弹窗还是卡片 | 用对话里的提问卡片加一种详情内容，不用弹窗，也不做新的卡片体系；调整阵容在卡片自带的回答框里用一句话说 | 现有的 QuestionDock 已有详情组件的先例（视频确认、桌面接管）；市面上启动长任务普遍是对话内的方案卡 |
| 团队对话的界面 | 在现有聊天页上增加，不做第二套：工具行、待办卡片外框、分隔线、只读会话、右侧工作台页签全部复用；试运行右栏与聊天页共用同一个聊天主体 | 用户要求测试窗口和团队对话与普通聊天同一套组件、不走特例；第 13.7 节的四种允许的改动形式 |
| 是否提供阵容连线图 | 提供，放在右侧工作台团队页签的默认子页；固定布局、只画人和往来、三种点击交互，窄屏与移动端退化为成员列表 | 用户要求能看到当前团队的连线并可点击；面向普通用户的产品多数只用列表，所以图保持最小，不看图也能完成全部操作 |
| 布尔选项用什么控件 | 菜单里用带勾的菜单项，表单里用圆形勾选；不新增开关控件 | 代码库里没有 switch 组件，现有页面用选项卡片、勾选和筛选药丸表达同类选择 |
| 实施顺序 | 先最小可运行团队并做首轮评估，再 Agent 库、工具层与可靠执行、产品闭环、评估与发布；全部必须完成 | 最大的不确定性是协调质量和团队收益，不是持久化 |

### 20.2 完成定义

当 M0 到 M5 的退出条件、第 16.1 节的全部验收以及发布准备全部满足时，本计划才算完成。计划内没有可选项，也不分先后两个版本；第 1.3 节列出的扩展不在本计划内。最终交付应包括代码、数据库迁移、API/工具契约、用户界面、运行与恢复说明、测试证据和评估结果。

实施过程中如需变更已确认语义，应更新本文件的对应章节和决策记录，使后续开发者能从文档直接理解最终设计。业务示例可以增加，不能将通用内核重新绑定到视频或某一平台。
