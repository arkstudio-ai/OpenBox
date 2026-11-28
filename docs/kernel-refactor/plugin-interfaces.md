# 内核插件接口草案（第 2 步，2026-11-28）

依据：`docs/KERNEL_DSH_COMPARISON_20261128.md` 9.1、第 10 节第 2 步、第 15、16 节、25.4；轨迹的约束以 `docs/trajectory-rearch/SPEC.md` 和 `docs/SESSION_TRAJECTORY_PROTOCOL.md` 为准。本轮四个接口：回合观察者、调用包装、提示词段落、工具包，外加恢复周期的注册点和计时端口。轨迹是第一个使用者，只用前两个；后两个这一轮只定义，留给记忆第一段。

## 放在哪里

| 模块 | 层 | 内容 |
|---|---|---|
| `agent/extensions.py` | 内核 | 全部 `Protocol`、空实现、注册表 `KernelExtensions`、访问器 `extensions()` / `install()` / `use()`、`ToolContext` 上插件私有状态的读写 |
| `agent/recording.py` | 内核 | 调用点用的薄门面：`record`、`bind`、`current`、`begin_model_call` 等，转给已注册的实现 |
| `agent/timing.py` | 内核 | 计时的薄门面：`phase`、`span`、`measured_run`、`create_dependency_task` 等 |
| `core/timing.py` | 底座 | 当前运行计时器的 `ContextVar`、`RunTimer` 协议、`span`/`provider_wait`/`network_wait` 和 SQL 计时监听。放底座是因为 `db/base.py` 也要读它，底座不能 import 内核 |
| `trajectory/plugin.py` | 平台服务 | `TrajectoryPlugin`：录制端口、调用包装、计时端口的实现，启动和关闭钩子，管理路由挂载 |
| `memory/plugin.py` | 记忆 | `MemoryCleanup` 和 `MemoryPlugin`，见文末“记忆第一段” |

注册表是 `main.py` 在 `create_app()` 里构造的一个对象，放在 `app.state.kernel_extensions`，lifespan 开始时 `install()`，结束时换回原来的。进程里只有一个“当前注册表”的槽位，没有模块往里面追加东西；测试和基准用 `with use(KernelExtensions()):` 换成空的或只带一个插件的。没有安装时访问器返回一个空注册表：所有端口都是空实现，内核照常工作，不多做任何事（不写控制记录，不记计时日志）。插件只在启动时注册，`install()` 之后注册表封口，不支持运行中替换（9.1 设计原则 4）。

## 接口

```python
class Plugin(Protocol):
    name: str
    def register(self, registry: KernelExtensions) -> None: ...      # 只在这里调用 set_*/add_*
    # 可选：def mount(self, app) / async def startup(self, app) / async def shutdown(self, app)
    #       async def start_background(self, app) / def stop_background(self, app) -> Mapping[str, Awaitable]（记忆第一段加的）

class TurnObserver(Protocol):          # 回合观察者，第一档：提交之后、进程内、最多一次
    async def record(self, type: str, data: dict, *, context=None, db=None,
                     event_id: str | None = None, occurred_at=None) -> None: ...
    def record_stream(self, context, event: dict) -> None: ...
    def emit(self, type: str, data: dict, *, context=None, event_id=None) -> None: ...
    def emit_control(self, control: dict, *, db=None) -> None: ...

class RecordingIdentity(Protocol):     # 同一个观察者的身份部分（轨迹的 TraceContext，对内核不透明）
    def enabled(self, user_id: str) -> bool: ...
    def current(self): ...                       # 任务局部的身份
    def bind(self, context) -> ContextManager: ...
    def new_identity(self, **fields): ...; def parse_identity(self, saved): ...
    def saved_identity(self, saved) -> dict: ...; def markers(self, saved) -> dict: ...
    async def session_identity(self, db, user_id, session_id, **ids): ...
    async def activity_identity(self, db, user_id, session_id, *, saved=None, **ids): ...
    async def mark_recording_in_tx(self, db, execution, *, user_id, session_id, context) -> None: ...
    async def paused_in_tx(self, db, context) -> bool: ...
    async def baseline_candidate_in_tx(self, db, context, session_row) -> ...: ...
    async def capture_asset_ids_in_tx(self, db, context, asset_ids, *, role) -> dict: ...
    def context_of(self, ctx): ...; def attach(self, ctx, context) -> None: ...
    async def context_for_tool(self, ctx): ...
    def public_value(self, value): ...; def tool_schema(self, tool_info, name) -> dict: ...
    def requested_tool_schema(self, ctx, name, tool_info=None) -> tuple[dict, str]: ...
    def timestamp(self, value) -> str | None: ...

class Recorder(TurnObserver, RecordingIdentity, Protocol): ...   # 注册表里的 recorder 槽位

class CallWrapper(Protocol):           # 调用包装：模型调用和工具体
    async def begin_model_call(self, ctx, *, purpose, model_id, payload, capture_level) -> ModelCall: ...
    def chunk_blocks(self, kind: str) -> Callable[[Any], list[dict]]: ...   # "litellm" | "responses"
    async def capture_billing(self, context, meter, usage, credits, purpose: str) -> None: ...
    def bind_model_billing(self, ctx, meter) -> None: ...; def active_request(self, ctx): ...
    def note_model_attempt(self, ctx, attempt: int) -> None: ...
    def media_capture(self, ctx, enabled: bool) -> tuple[dict | None, dict | None]: ...
    def note_tool_request(self, ctx, tool_info, provider_call_id, arguments_raw) -> None: ...
    def pending_tool_request(self, ctx) -> tuple: ...
    def begin_tool_body(self, ctx, context, *, tool, owner, stopped=None) -> ToolBody: ...
    def tool_body(self, ctx, context, *, tool, owner) -> ToolBody: ...      # define_tool：复用或新开
    def running_tool_body(self, ctx) -> ToolBody | None: ...
    def retain_full_output(self, ctx, output: str | None) -> None: ...
    def retained_output(self, ctx, fallback: str) -> str: ...

class ModelCall(Protocol):             # 一次模型调用的观察者
    context: Any; finished: bool
    def stream_chunks(self, stream, block_builder) -> AsyncIterator: ...   # 原样转发，不等录制
    async def chunk(self, raw, *, blocks=None) -> None: ...
    async def capture_usage(self, usage: dict) -> None: ...
    async def finish(self, status: str, *, reason=None, error=None) -> None: ...
    async def route_changed(self, reason: str) -> None: ...

class ToolBody(Protocol):              # 一次工具调用的输出观察者
    closed: bool; finished: bool
    async def update(self, output: str) -> None: ...
    async def finish(self, output, **fields) -> None: ...
    async def close(self) -> None: ...

class TimingPort(Protocol):            # 计时：为一次运行和一个预取任务各开一个 RunTimer
    def start_run(self) -> RunTimer: ...
    def finish_run(self, timer, *, session_id, run_id, ended) -> None: ...
    def start_dependency(self, parent: RunTimer | None) -> RunTimer: ...

class RecoveryHook(Protocol):          # 恢复周期的注册点
    name: str
    async def recover(self) -> int: ...

class PromptSection(Protocol):         # 提示词段落（记忆第一段起有使用者）
    name: str; order: int
    async def render(self, request: PromptRequest) -> str | None: ...

class ToolPack(Protocol):              # 工具包（记忆第一段起有使用者）
    name: str; capability: str | None
    def tools(self) -> Mapping[str, ToolInfo]: ...
    def admit(self, tools: dict[str, ToolInfo], *, user_id: str | None) -> dict[str, ToolInfo]: ...
    async def before_call(self, tool_id: str, args: dict, ctx) -> ToolResult | None: ...
```

`PromptRequest`（`agent/extensions.py`）是一次组装的只读描述：agent 名、模型、用户、项目、工作区、会话类型，外加这一轮还由循环决定的 `memory_isolated`、`include_user_memory`。`PROMPT_ORDER_PROJECT_BRIEF = 500` 是内核项目简介在段落顺序里的位置。内核侧的调用入口是 `render_prompt_sections`、`pack_tools`、`admit_pack_tools`、`check_before_call`。

`ToolContext` 上不再有轨迹字段。插件的每次调用状态（身份、输出流、完整输出、请求的工具 schema 等）放在一个通用的私有槽位 `_plugin_state` 里，按插件名分开，内核不读它。写入一律换掉整个映射而不是原地修改，所以 `copy.copy(ctx)` 得到的并行副本、批量子调用和服务调度上下文的语义和原来各自一个属性时完全一样。内核自己的两件事不再借用轨迹字段：工具体开始的时间 `_body_started`（处理器判断“已开始执行”、工具元数据里的 `duration` 都靠它），和已经通告过请求的工具 part `_requested_part_id`（去重 `tool.requested`，助理绑定权限请求也读它）。

## 调用时机、失败策略、顺序

| 接口 | 何时调用 | 失败策略 | 顺序 |
|---|---|---|---|
| 回合观察者 | 原来调用 `trajectory.record` 的位置不变。带 `db` 的事实由观察者挂在该会话最外层提交之后，回滚即丢，嵌套提交不触发；不带 `db` 的立即入队 | 实现不得抛出；`emit*` 永不抛出（SPEC §5.3）。身份和录制标记方法在调用方事务里读写业务行（§5.6 的 `SessionExecution.trace_context`），不碰轨迹表 | 同一进程内按调用顺序；事件 id、时间戳在调用点生成，序号仍只由 worker 分配。这一轮只允许一个 recorder，原因是内核把它的身份穿过调用点传递 |
| 调用包装 | 模型调用：发请求前 `begin_model_call`，之后流、用量、结束各一次。工具：hooks 分派时 `begin_tool_body`，`define_tool` 校验通过后取 `tool_body`，体结束后 `finish`/`close` | 记录失败只记一次日志（`_not_recorded`），从不让请求或令牌等录制；流式块不等入队 | 和被包住的调用同序；一个调用一个观察者。这一轮只允许一个，计费计量以后作为第二个时再定扇出顺序 |
| 计时端口 | `run_loop` 进入时开一个计时器，退出时写四行日志；预取任务开子计时器，被等待时转发外部等待 | 没有端口时计时器为空，全部操作是空操作 | 只有一个 |
| 恢复周期 | `recover_agent_work_once` 跑完内核自己的固定顺序（过期 Driver、中断和子任务 outbox、历史修复、预约任务、Inbox、external effects，`docs/AGENT_KERNEL_ARCHITECTURE.md:97-98`）之后，按注册顺序逐个调用 | 每个钩子单独 `try`，出错记日志，不影响内核的恢复和后面的钩子 | 内核在前，插件在后，按注册顺序。记忆的补排（`memory.extraction_jobs`）原来排在 effects 之前，记忆第一段起是钩子，排在 effects 之后 |
| 提示词段落 | `_build_system_prompt` 组装非助理 agent 的提示词时渲染两次：`order < 500` 的在内核静态段落和账号身份之后、项目简介之前；其余在项目简介之后，放在最后。助理 agent 的提示词由助理自己组装，不经过插件段落 | 出错跳过该段并记日志（`Prompt section skipped`） | `order` 升序，同序按注册顺序 |
| 工具包 | `tools()`：`register_builtin_tools()` 注册完内置工具后注册进同一个注册表，按 id 查工具的内核代码（批量、嵌套调用、压缩）照常找得到。`admit()`：`resolve_step_tools` 取出 agent 的工具之后收窄这一步的工具集。`before_call()`：`ToolHooks.authorize_tool` 里、权限策略之前 | `tools()` 出错，这个包的工具不注册；`admit()` 出错，这一步去掉这个包自己的工具（失败即关闭）；`before_call()` 的异常照原样抛给调用方，从不当作放行 | 注册顺序；重名报错；第一个拒绝结果生效 |
| 后台服务 | `start_background(app)`：内核自己的后台服务（恢复、提问 worker、平台插件监视）启动之后。`stop_background(app)`：返回按名字的停止操作，入口把它们和自己的放进同一次并行停止 | 和入口自己的服务一样：每个停止操作单独记日志，出错不影响别的 | 启动按注册顺序；停止一起发出；名字重复报错。`shutdown(app)` 在 agent 循环排空、worker 停止之后 |

## 附：生命周期事件改由 `agent_events` 派生的可行性（只读核对）

这一轮没有派生，轨迹事件仍在原调用点经端口发出。下面回答“哪些类别改由回合观察者从已提交的 `agent_events` 派生后，事件 id、时间戳和顺序还能保持”。

**做法上的三个硬约束**（读代码得出）：

- **id**：没有显式 `event_id` 的事实，id 是发射时现取的 `evt_<uuid4>`（`trajectory/types.py:167` 的 `prepare_fast`），`occurred_at` 缺省也是那一刻的时间；`agent_events.id` 是另一次 `ascending("aevt")`。派生出来的只能是新 id。显式 id 的那一类（`run_start:{run}`、`turn_finish:{turn}`、`fork:{session}:source`、`question_*:{row}`、`agent_result:{activation}`、`request:{id}:…`）用的是事件日志里大多没有的业务 id。
- **时间戳**：`occurred_at` 是调用点的时间；`agent_events.created_at` 是事务里追加那一刻的时间，两者不是同一个值。各种 `duration_ms` 用的是调用点前后的单调时钟，事件日志里没有。
- **顺序**：worker 按到达分配 seq。不带 `db` 的事实（`tool.requested` 在解析出调用时、`request.delta` 在流里、`tool.output` 在工具体里）比对应的业务提交早；按游标在提交之后派生，就会排到它们后面。

**逐类结论**

| 类别 | 事件日志里有没有对应 | 能否保持 id / 时间戳 / 顺序 |
|---|---|---|
| `message.committed`、`part.committed`、`plan.changed` | 有：`message.*`、`part.created/updated`，同一事务追加 | 顺序和投递档（提交之后）能保持；id 和时间戳不能（见上）。若接受“id 改为由 `agent_events.id` 推导、时间戳取 `created_at`”，这是最先能搬的一组，顺带去掉为补角色读 `messages.role` 的查询 |
| `turn.started`、`turn.finished` | 有 | `turn.finished` 的 id 可以重算，时间戳不能；`turn.started` 两者都不能；它和 `input.accepted` 的相对顺序要靠同一事务里的追加顺序，`input.accepted` 本身没有对应 |
| `step.started/finished`、`tool.requested/started/finished` | 部分有（`step.*`、`tool.called/result`），但载荷缺 schema、`arguments_raw`、`provider_call_id`、单调时钟耗时、状态映射 | 不能：id 是现取的；`tool.requested` 在提交前发出，派生后顺序会变；载荷要给事件补字段才够 |
| `request.*`、`tool.output`、`skill.loaded`、`compaction.*` | `model.requested` 只是请求凭证，没有逐字请求、流和用量 | 不能，留在调用包装（本轮已经在） |
| `run.*`、`permission.*`、`question.*`、`input.injected`（提问）、`agent.message/finished`、`history.reverted/forked`、`job.progress`、`artifact.recorded`、`session.settings_changed` | 没有：它们在 `session_executions`、提问检查点、权限、子任务、作业等各自的表里 | 不能；要派生就得新增事件行，违反“业务路径不增加写入” |
| `baseline.captured`、`recording.state`、`session.deleted`、`gap` 等控制记录 | 没有 | 不能，也不该：它们是录制标记本身，录制关闭时也要写（SPEC §3.4、§5.6） |

**结论**：按“id、时间戳、顺序都不变”的标准，没有一类能改由事件日志派生；能保持顺序和投递档的只有会话投影那一组（消息、part、计划、回合起止）。所以这一轮全部保留在原调用点、经端口发出，这本身保证了 id、时间戳和顺序不变。下一步如果要搬会话投影那一组，需要轨迹的维护者先同意 id 和时间戳的来源改变（worker 按 `event_id` 去重、先到者保留，新 id 不会和旧 id 冲突；管理端只回放事件，不依赖 id 的格式，这一点要再核对前端的 TypeScript 投影器）。按游标“至少一次”的那一档和游标表这一轮不做。

## 现状（2026-11-28，分支 `worktree-agent-ab0bb46cfe359e338`）

- 内核对 `trajectory` 的 import：62 条（15 个文件）→ 0。`db/base.py` 不再 import `agent.latency`（它对 `agent.schema` 的 import 是原有的，不在本轮范围）。
- 轨迹的录制、调用包装、计时三个端口都由 `TrajectoryPlugin` 注册；`main.py` 只构造注册表，启动、关闭和管理路由挂载由插件的钩子在原位置执行。
- 离线基准（真实 `run_loop` 和 `llm.stream_llm`，litellm 在 `acompletion` 处替身，第 2–4 轮每轮 SQL 条数）：纯文本轮，录制关 250 → 250，录制开 255 → 251，不注册插件 250；带一次工具调用的轮，录制关 414 → 414，录制开 424 → 415，不注册插件 414。开着录制时剩下多出的 1 条是 §5.6 录制标记读 `session_executions`。两种场景录到的事件在规整 id、时间戳、耗时之后逐字节相同（含键顺序）。

## 记忆第一段（2026-11-28，分支 `worktree-agent-a81d32922e5a02b25`）

依据 `docs/KERNEL_DSH_COMPARISON_20261128.md` 9.1“迁移步骤”第一段。记忆包有两个注册单元，都在 `memory/plugin.py`；`main.py` 按顺序注册轨迹、`MemoryCleanup`、`MemoryPlugin`：

| 单元 | 内容 | 为什么分开 |
|---|---|---|
| `MemoryCleanup`（`memory.cleanup`） | 索引 worker（outbox 的 DELETE/REVOKE 投递不看开关、待删除核对、到期、调试快照清除）；文档 worker（已删除文件原件的清理）；关闭时关掉共享的 provider 连接池 | 这些是清理义务（9.1 规则 7，`docs/LONG_TERM_MEMORY_PLAN.md:575`）。入口始终注册它，不注册 `MemoryPlugin` 时它照样跑 |
| `MemoryPlugin`（`memory`） | 三个提示词段落；工具包（6 个记忆工具、每步准入、调用前检查）；抽取任务补排的恢复钩子；抽取、Wiki、Wiki 组织三个 worker；读取通道预热；约 10 个记忆路由 | 记忆功能本身。不注册时没有记忆段落、记忆工具和记忆路由，抽取和 Wiki 不跑，对话照常 |

接口为此做的增补（都已写进上面的接口和时机表）：

- **提示词段落**：`render(request: PromptRequest)`，按 `PROMPT_ORDER_PROJECT_BRIEF` 分两处渲染。记忆的三个段落：使用规则 400、自动保存说明 410、旧路径的 `<user_memory>` 900，位置和原来一样。
- **工具包**：`available(ctx)` 换成 `tools()`、`admit()`、`before_call()` 三个方法。记忆的 `admit()` 就是原来 `resolve_step_tools` 里按开关去掉记忆工具的那段（含助理的 `memory.read_more`、`memory.prepare`），`before_call()` 就是原来 `ToolHooks.authorize_tool` 里按会话隔离策略拒绝的那段。
- **后台服务**：`Plugin` 多了 `start_background` 和 `stop_background` 两个可选钩子，让记忆 worker 在原来的位置启动（Redis 总线和首次恢复之后），关闭时和别的消费者一起收到停止请求再各自排空。
- **恢复周期**：接口没变。`AgentRecoveryResult.memory_jobs_recovered` 去掉了，这个数计入 `plugin_recovered`。

路由挂载的位置从助理路由之后移到了插件挂载点（管理路由之后）。核对过全部 484 个路由：位置互换的路由对之间，没有路径模式和方法都重叠的。

这一段没有动、留在原处的：本轮读取范围和按 `read_policy` 的历史截断、召回作为临时输入注入（`_prepend_to_last_user`）、“读取不可用必须说明”、结算时在同一事务里登记（`agent/inbox.py`）、记忆提议在调用方事务里确认（`question/continuation.py`）、`session/agent_event_log.py` 的 `strip_memory_text`、批量和嵌套调用里对记忆读取的拒绝（`tool/batch.py`、`agent/nested_tool_runtime.py`）、`memory_policy`/`memory_isolated` 的判断（含循环里按隔离去掉记忆能力的两处）。后面三项留在内核，也保证了不注册插件时剥离和拒绝照样生效。`agent/agent.py` 里 build agent 的工具名单仍列着 6 个记忆工具的 id，不注册插件时它们解析不到任何工具。

现状：

- 内核对 `memory` 的 import：36 条（11 个文件）→ 30 条（9 个文件）。去掉的是 `agent/loop.py` 3 条（提示词段落）、`agent/hooks.py` 2 条（调用前检查）、`agent/recovery_service.py` 1 条（恢复）。另外 `agent/tool_resolution.py`、`tool/registry.py` 对 `tool.memory_tools` 的 2 条（归在 products 层）也去掉了。`tool/memory_tools.py` 搬成 `memory/tools.py`，没有留旧路径；`scripts/check_main_contract.py` 跟着改名比对。
- `main.py` 只在 `_build_kernel_extensions` 里注册记忆的两个单元；那里的 `cache.memory_cache` 是进程内缓存，和长期记忆无关。
- 104 种开关组合下系统提示词和每步工具集逐字相同；离线基准四个场景的 SQL 条数和提交数不变，语句的多重集合逐条相同（生产场景里召回是并发的，语句先后在同一份代码的两次运行之间也会变）。
- 不注册 `MemoryPlugin` 的测试在 `tests/unit/test_memory_plugin.py`：应用能启动和关闭、索引和文档 worker 照常启动、连接池照常关闭、一轮带工具调用的对话能跑完且 SQL 不多、提示词和工具集里没有记忆、记忆正文照样从历史里剥掉。
