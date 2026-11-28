# 助理试探：三个难点和接口清单要补什么（第 2 步 2-6，2026-11-28）

在 a9da02c8 上读完了三条代码路径。可行性用一个仓库外的垫片验证过，没有合入任何代码。对照的是 9.1 的八个接口：提示词段落、上下文提供者、工具包、回合观察者、准入守卫、调用包装、事务参与者、历史视图与回合收尾。

## (i) 在调用方事务内的任务挂起检查

**路径**：`agent/driver.py:970-1072` 的 `reserve_run`。

1. 锁会话行（`with_for_update`）。
2. `require_runnable_locked(db, session, lock=True, resume_command_id=...)`（989-990）。它沿父会话链最多走 64 层，逐层锁 `AssistantTask`，还查用户、项目和待恢复命令（`assistant/scheduling.py:30-77`）。
3. 锁 `AgentDriverState`。
4. 写驱动状态，flush。
5. 助理在同一事务里认领恢复命令（`claim_resume_locked`，`assistant/control.py:544-573`）。它会改内核字段 `state.trigger_message_id`（568 行），把命令标成已应用，推进任务的 `control_revision`。

挂起时靠抛 `TaskSchedulingHeld` 回滚整个事务。

内核里同类的在事务内调用有 6 处【已核对】，每处要的结果不一样：

| 位置 | 挂起时内核做什么 |
|---|---|
| `driver.py:990`、`1122`；`session/agent_event_log.py:3096` | 抛异常，回滚 |
| `agent/inbox.py:761`、`1316` | 不抛，认领一个空批次或返回 False；之前先让助理在同一事务里清掉过期的引导（`expire_task_steers_locked`） |
| `question/continuation.py:303` | 只有 `paused`、`resuming` 两种状态把答案留着不应用，其余照常应用 |

另外 6 处自开事务（`llm.py:1727`、`tool/tool.py:145`、`inbox.py:1871`、`loop.py:1082, 1304`、`driver.py:595` 的心跳观察），挂起时置 abort。

**内核要什么**：

- 输入：已加锁的会话行、加锁与否、调用点，以及一个豁免凭证。凭证就是现在专门为助理加在 `reserve_run` 上的参数 `assistant_resume_command_id`。
- 输出：放行，或者一个带状态的"挂起"。挂起要说明是拒绝还是暂缓。
- 加锁顺序固定：内核的会话行在前，插件自己的行居中，驱动状态在后。设计文档也是这么定的，"先锁执行会话再锁任务"（9.1）。
- 插件不提交、不另开会话。改内核字段只能通过返回值，由内核去写。

**哪个接口能承接**：准入守卫加事务参与者。缺五样：

1. 准入守卫只有"拒绝"，没有"暂缓"。
2. 守卫没有在事务内被调用的形式，也没有加锁约定。
3. 守卫的调用点里没有"认领输入""应用答案""请求前检查点""心跳"。
4. 没有一个能穿过内核入口的豁免凭证。
5. 事务参与者不能通过返回值改内核字段。

**提议**：

```python
class AdmissionPoint(StrEnum):
    RUN_RESERVE = "run_reserve"; RUN_RECOVER = "run_recover"; INPUT_CLAIM = "input_claim"
    ANSWER_APPLY = "answer_apply"; CHECKPOINT = "checkpoint"
    MODEL_REQUEST = "model_request"; TOOL_BODY = "tool_body"; HEARTBEAT = "heartbeat"

@dataclass(frozen=True)
class Claim:             # 插件发、插件认；内核只负责透传
    plugin: str; kind: str; id: str

@dataclass(frozen=True)
class Hold:
    plugin: str; code: str; state: str
    disposition: Literal["refuse", "defer"]     # refuse：拒绝或跳过；defer：持久的工作留着不动
    subject_id: str | None = None; revision: int | None = None

class LockedAdmissionGuard(Protocol):
    """在调用方事务里、内核锁住 session 之后调用。只许锁插件自己的行，不许提交。"""
    name: str
    async def check_locked(self, db: AsyncSession, point: AdmissionPoint, session: Session,
                           *, lock: bool, claims: tuple[Claim, ...]) -> Hold | None: ...

class ReservationParticipant(Protocol):
    """在 reserve_run 的事务里、驱动状态 flush 之后调用，消费自己发的 Claim。"""
    async def on_reserved_locked(self, db: AsyncSession, session: Session, run: RunRef,
                                 claims: tuple[Claim, ...]) -> ReservationChanges | None: ...
    # ReservationChanges(trigger_message_id=...)：内核字段由内核写

# 内核入口：reserve_run(..., claims=()) 取代 assistant_resume_command_id=
```

**垫片验证**：`/private/tmp/claude-501/-Volumes-fanxiang-workspace2-OpenBox/65ad3bdc-74f5-4847-8ff1-8d8816e34147/scratchpad/probe/admission_shim.py`（临时目录，可能被清掉，没有进仓库）把 6 处在事务内的调用全部改走 `check_locked` 加内核侧的映射。在 13 个相关测试文件上（`test_agent_driver`、`test_agent_inbox`、`test_durable_questions`、`test_assistant_control`、`test_assistant_plans` 等），通过数与基线完全相同：241 个通过，2 个要 PostgreSQL 而跳过。五个调用点都被走到了。签名够用。

垫片验证不了加锁顺序：锁竞争的测试只在 PostgreSQL 上跑，本地 SQLite 上它们被跳过（基线里 39 个跳过全是这类）。

## (ii) 一条异常分流路径：`AssistantBudgetExceeded`

**路径**：

- **开始**：会话是助理类型时，主循环开一个回合预算，放进 contextvar（`loop.py:1411-1436`，`1025`/`3645` 设置和复原）。
- **准入**：每次请求模型前（`loop.py:2664-2666`）和每个工具体前（`hooks.py:496-497`），预算在自己的事务里带运行栅栏写一条 `assistant.budget.*` 事件，用幂等键计数（`assistant/budget.py:106-127`）。工具体外面还套了截止时间（`budget.py:174-191`）。
- **超限**：同时置上 lease 的 abort（`budget.py:51-56`）。

这一个异常类型在内核里有 7 处，处理各不相同：

| 位置 | 做什么 |
|---|---|
| 步骤重试（`loop.py:433`） | 这一步以 ERROR 结束 |
| 工具准备和执行（`processor.py:1372, 1393`，`held_outcome` 在 1200-1207） | 工具 part 标错，写失败码和"未开始/结果未知"，再置 abort |
| 两处通用捕获（`hooks.py:529`、`tool/batch.py:143`） | 必须原样抛出，不能变成工具报错文字 |
| 轨迹（`hooks.py:225-240`） | 记为 cancelled，`not_started` |
| 压缩（`compaction.py:326-333`） | 转成 `CompactionInterrupted` |
| 回合末尾（`loop.py:3254-3266`） | 插件先限时 3 秒读一次回执，改写错误文字（`budget.py:84-104`），内核把它写成消息的错误，结束原因记为 `error` |

语义上这 7 处都是第 10 节四种结局里的一种：**整轮终止并结算**。另外三种：

- **中止**：`TaskSchedulingHeld`，`processor.py:1588`、`loop.py:429`。
- **关闭这一步并报错**：同一个 `AssistantError`，在准入或候选检查时出现（`loop.py:2820-2843`）。
- **作为工具结果还给模型**：还是 `AssistantError`，在 `authorize_assistant_tool` 时出现（`hooks.py:728-735`）。

所以结局不由异常类型决定，要由抛出的一方在抛出时说明。

**内核要什么**：

- 输入：运行标识和栅栏、回合 id、每次准入的幂等身份（`{消息 id}:{第几次}`、工具 part id）。
- 输出：放行，或者一个带结局的拒绝；结算时可以改写最终的错误。
- 事务：准入在插件自己的事务里写，不在调用方事务里。
- 顺序：开始在认领输入之后、第一次请求之前；请求准入在每次真正发出之前，包括重试；工具准入在权限之后、工具体之前；结算在消息的结束状态提交之前。
- 每轮的插件状态由内核保管，不靠 contextvar。

**哪个接口能承接**：准入守卫管准入，调用包装管截止时间，历史视图与回合收尾管结算时改写。缺四样：

1. 拒绝的结果只有一种。
2. 没有每轮的插件状态。
3. 回合收尾不能改写最终错误。
4. 通用捕获处要靠导入助理的异常类型才知道该抛出。

**提议**：

```python
class Outcome(StrEnum):
    ABORT = "abort"; FAIL_STEP = "fail_step"; TOOL_RESULT = "tool_result"; END_TURN = "end_turn"

@dataclass(frozen=True)
class Refusal:
    plugin: str; code: str; message: str; outcome: Outcome
    execution: Literal["not_started", "unknown"] | None = None

class KernelRefusal(Exception):        # 内核定义；插件抛它，不再抛自己的类型
    refusal: Refusal

class RunGuard(Protocol):
    name: str
    async def start(self, run: RunRef) -> object | None: ...                       # 返回的状态由内核按轮保存
    async def before_request(self, run: RunRef, state, attempt_id: str) -> Refusal | None: ...
    async def before_tool(self, run: RunRef, state, part_id: str) -> Refusal | None: ...
    async def settle(self, run: RunRef, state, ending: TurnEnding) -> TurnEnding: ...  # 可改写 error/finish
```

"每个调用点怎样落实每种结局"由内核自己维护成一张表，并配一组测试，一行对应一个调用点、一种结局。通用捕获处只看 `outcome`：ABORT 和 END_TURN 原样抛出，TOOL_RESULT 变成工具结果。

## (iii) 一处提问的截获：回复和应用

**路径**：

- **提问**：在内核事务里插入提问行之后，助理绑定当前运行，算一个版本摘要写进 `row.continuation["assistant_request"]`；没有当前运行就以 409 拒绝（`question/question.py:315-316`，`assistant/requests.py:52-69`）。内核后来改截止时间时，得替助理重算这个摘要（`question.py:382-391`）。
- **读**：列表、详情、存草稿都要问助理"可不可见"，看不见就跳过（`question.py:354, 518, 552-561`）。
- **回复**：内核先问助理"这条是不是你的"（`question.py:437-440`）。是的话，整个回复由助理接管：在自己的事务里按"操作者、主会话、执行会话、任务"的顺序加锁，校验带版本的回复身份，回调内核的 `resolve_locked`，记下自己的命令，返回回执（`requests.py:187-257`）。内核之后在自己的事务里再确认一次归属没变（`question.py:443-444`）。
- **应用**：在内核事务里（`continuation.py:296-388`）：
  - 先做挂起检查（就是第 (i) 节的暂缓）。
  - 再逐行让助理复核决定（`apply_guard`）。复核失败，内核把提问行作废、会话置错，助理把命令标成失败、追加自己的事件，都在同一事务里。
  - 复核通过才应用。应用结果的 metadata 里带着助理的回复引用；应用后助理把命令标成已应用、追加事件。
- **续跑处理器**：`_apply` 按续跑类型分派。`plan_exit`、模型选择、项目放置、确认卡归助理，`memory_proposal`、`memory_forget` 归记忆（`continuation.py:68-145`）。
- **审批**：结构相同。`register` 做持久化，`_wait_linked` 换掉等待方式（轮询持久存储，不等进程内的回调），`maybe_reply` 截获回复（`permission/permission.py:401-420, 523, 607-609`）。

**内核要什么**：

- 提问时（事务内）插件写自己命名空间下的数据，可以拒绝；内核改行上字段时要通知插件刷新。
- 读时（无锁）判断可见性。
- 回复时在内核开事务之前判断归属，归插件的交给插件，插件回调内核的加锁操作。
- 应用时（事务内）先复核再应用，应用后让插件结算。
- 审批还要能换掉等待方式。

**哪个接口能承接**：只有事务参与者沾得上边。缺四样：

1. 没有接管回复的挂点；这是反方向的，插件反过来调内核。
2. 没有读的可见性过滤。
3. 插件在内核行上存数据，没有约定。
4. 没有续跑类型的注册表。

**提议**（问题和审批共用，记忆的两种续跑是第二个使用者）：

```python
class InteractionPlugin(Protocol):
    name: str
    async def on_asked_locked(self, db, request: InteractionRow, run: RunRef) -> Mapping[str, Any] | None: ...
    async def on_changed_locked(self, db, request: InteractionRow, change: RowChange) -> Mapping[str, Any] | None: ...
    async def visible(self, db, request: InteractionRow) -> bool: ...
    async def owns_reply(self, db, request: InteractionRow) -> bool: ...
    async def reply(self, request: InteractionRow, reply: Reply, binding: Mapping[str, Any],
                    ops: LockedInteractionOps) -> Receipt: ...        # 插件自己开事务，用 ops 回调内核
    async def verify_locked(self, db, request: InteractionRow, execution) -> object | None: ...  # 抛 InteractionGone 即作废
    async def settle_locked(self, db, request: InteractionRow, decision, result: Literal["applied", "failed"]) -> None: ...
    async def wait(self, request: InteractionRow) -> Reply | None: ...   # 可选：换掉进程内的等待

class ContinuationHandler(Protocol):          # 按 continuation["kind"] 注册
    kind: str
    async def apply_locked(self, db, session, row, decision) -> tuple[dict, list[dict]]: ...

class LockedInteractionOps(Protocol):         # 内核公开给插件的加锁操作，取代 37 处私有名 import
    async def execution_locked(self, db, session_id: str, user_id: str): ...
    async def resolve_locked(self, db, session, execution, row, *, answers, attachments=None, source_kind="user") -> bool: ...
    def check_pending(self, row, execution) -> None: ...
```

插件数据存进内核行上的一个插件命名空间字段，比如 `plugin_data[name]`，不再混进 `continuation`。

## 对接口清单的建议

1. **准入守卫拆成两种形态。**
   - 自开事务的 `check`，用在请求、工具和心跳。
   - 在调用方事务内的 `check_locked`，用在预留、恢复、认领输入、应用答案和检查点。
   - 结果都是带类型的 `Hold`/`Refusal`，不是布尔值。
   - 内核入口加一个透传的 `claims`，替掉 `assistant_resume_command_id` 这种专门为助理加的参数。
2. **新增"结局"类型。** 四种结局加内核基类 `KernelRefusal`。这一步只改内核，可以先做，能去掉内核对 32 个助理异常类型里的大部分 import。
3. **事务参与者加约束。**
   - 只在列出的调用点被调。
   - 拿到的是内核已经锁好的行。
   - 加锁顺序：内核会话行在前，插件自己的行居中，驱动状态在后。
   - 不提交，不另开会话。
   - 改内核字段只能通过返回值。
   - 拒绝时抛 `KernelRefusal`。
   - 两个使用者：助理（恢复认领、引导过期、提问绑定）和记忆（结算时的暂停标记、提议确认）。
4. **新增"交互插件"和"续跑处理器"两个接口。** 盖住提问和审批这 35 个名字。两个接口都有两个使用者：助理和记忆。
5. **历史视图与回合收尾补两件事。** 一是每轮的插件状态，二是结算时能改写最终错误和结束原因。
6. **内核要公开一小组加锁操作给插件用。** 助理现在 import 内核的 37 个私有名，大半是这类。这一组要写进接口清单，否则搬完助理反向依赖还在。
7. **验收要有 PostgreSQL。** 在事务内调用的守卫和参与者，加锁顺序只能在 PostgreSQL 上验证。2-2 的"对照测试基线没有新增失败"要加上在 PostgreSQL 上跑这批锁竞争测试。
8. **顺序。** 先做第 2 条（纯内核）。再做第 1 条：12 个调用点，垫片已经证明签名够用。最后做第 4 条。"助理整体搬迁"留到这三件做完再评估。
