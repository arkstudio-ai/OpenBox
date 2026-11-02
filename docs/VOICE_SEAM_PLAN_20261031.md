# 语音前台与任务执行页之间的缝：填缝方案（2026-10-31 草案）

状态：**P0 已于 2026-10-31 上线（第 10 节）；P1/P2 与第 9 节步 1–3 在 2026-11-02 起按第 11 节顺序实施**。第 1–7 节是填缝方案，只改主会话与通话链路内部，不改通话与会话的绑定关系，不改三端语音协议的音频部分，不改任务治理的闸门语义。第 8 节是 10-31 多任务审计及其对方案的两条增补；第 9 节是填缝之后的目标形态（全局助理 + 任务间）与分步路径。

## 1. 为什么是这条缝

语音通话的结构是两个脑子：百炼实时模型做前台（听、判停、寒暄、念稿），文字版个人助理做后台（任务、工具、治理），两者靠 `assistant_ask` 一个函数调用加收件箱相连。这个结构是必然的：后台背着任务、执行会话、云桌面和分钟级的视频生成，塞不进实时语音模型。问题只在两个脑子之间的缝有多明显。

2026-10-31 17:52 的事故把这条缝完整暴露了一次（账户 `01M1WRW62H7G01ZFWWT3NQ38HX`，任务 `01M68NBFWJB3JZG46CEXRC6TPE`，执行会话 `session_7YBSQAMG3FWMNG6W564C0K9ZGP`，通话 `01M68N3TA4GC73H27G95YYJR9D`，北京时间）：

| 时间 | 发生了什么 | 电话里的前台知道吗 |
| --- | --- | --- |
| 17:52:20 | 用户在**执行页**上传商品参考图并留言 | 不知道。共享上下文只看主会话的人工输入 |
| 17:52:42 | 用户在执行页误触了运行中的停止方块，对任务会话这是 `task_cancel`，任务整体取消 | 不知道。任务控制状态变化不进通话 |
| 17:52:44 | 被打断的 image_gen 留下"结果未知"记录，任务进入 `effect_unknown`，之后所有发送都被 409 `ASSISTANT_EFFECT_UNRESOLVED` 拒绝 | 不知道。这句"先核对再继续"只存在于 409 响应体 |
| 17:53:37 | 用户在电话里说"继续推进，刚刚我把那个商品参考发给他了" | 助理读了四次执行页历史才确认图在那里 |
| 17:53:56 起 | 助理四次调 `tasks.resume`，四次撞 `ASSISTANT_EFFECT_UNRESOLVED`，一次 `tasks.followup` 撞 `ASSISTANT_TASK_NOT_RUNNING` | 助理没有任何工具能替用户核对 |
| 17:56 至 17:58 | 助理三次口头告诉用户"打开任务页面，在卡片上点'我已核对，继续'" | **执行页上没有这个卡片和按钮**。Web 的按钮在助理会话的任务卡上，App 两边都没有 |
| 17:58 | 用户去执行页把"我已经核对，现在继续"打成文字发送，收到"操作冲突，请刷新后重试" | 错误码没有文案，App 退回通用 409 句子 |

三个语音轮次从提交到播报各用了 2 到 3 分钟，每轮里助理做了 4 到 6 次工具调用，其中一半是反复读执行页历史。

缝有两个方向，各缺一半：

- **后台到前台**：执行页里发生的事（人工提交、任务状态变化、被卡住的原因和解法）不流进通话。
- **前台到后台**：用户在电话里说的"继续"，助理听懂了却办不成，因为核对动作只有一个 HTTP 接口和一个 Web 按钮，没有助理工具。

## 2. 现有通道（已核对源码）

通话链路已经有一套"后台到前台"的旁路，填缝是把缺的东西接进这套旁路，而不是另起炉灶。

| 模块 | 现在流什么 | 缺什么 |
| --- | --- | --- |
| `backend/voice/reports.py` `ReportWatcher` | 主会话里新落地的任务报告（`origin=task_result`），轮询 2 秒，前台在空闲窗口用自己的话播 | 只有"跑完"这一种事件；取消、待核对、重开都不是报告 |
| `backend/voice/shared_context.py` `SharedContextWatcher` | 主会话最近 4 条人工输入、工作区最近资产、最近 6 个对话标题与状态、最近回答过的决策，作为背景注入前台 | 人工输入只查 `AgentInboxItem.session_id == main`，执行页的提交不在内；资产只有元数据，不知道是"为哪个任务传的" |
| `backend/voice/progress.py` | 主会话 agent 循环的 `tool.running` / `message.text_delta` 变成"在翻任务列表"这类进度短语 | 只覆盖主会话自己的循环，不覆盖执行会话 |
| `backend/voice/updates.py` `UpdateHints` | 订阅收件箱、资产、消息、会话状态、提问等总线事件作为唤醒提示 | 任务控制状态变化（`assistant_tasks.desired_state/observed_state`）不上总线，`backend/assistant/control.py` 只改表不发事件 |
| `backend/voice/prompt.py` `with_sections` | 会话提示里两段动态内容："本次通话到目前为止"和"还在办：…" | 没有"当前任务焦点"这一段，开场和应答都不点名在说哪件事 |
| `backend/voice/live_context.py` + `backend/assistant/projection.py` | 反向通道：主会话的文字轮次能读到通话里最近 12 行 | 已经够用，不动 |
| `backend/voice/tools.py` `DIRECT` | 前台自己的快读工具（任务列表、日程等），一个 `DIRECT` 条目一个工具 | 没有"这个任务现在卡在哪、要做什么"的快读 |
| `backend/assistant/*` 任务工具 | `tasks.get/list/submit/followup/next_step/pause/resume/cancel/link_existing/archive/delete` | 没有 `tasks.acknowledge_effects`；核对只有 `POST /api/assistant/tasks/{id}/effects/acknowledge`（`backend/api/assistant.py:455` → `backend/assistant/control.py` `acknowledge_effects`） |

## 3. 四项改动

### 3.1 执行页的人工提交流进通话

**目标**：用户在执行页传了图、发了话，前台在下一次开口前就知道，不用助理读四遍历史。

**机制**：

- `SharedContextWatcher.poll` 里的人工输入查询，从"只查主会话"扩成"主会话 + 该用户名下所有活动任务的执行会话"。执行会话的条目带上 `task_id`、任务标题和 `entrypoint`（`session_prompt` 表示在执行页提交，`assistant_command` 表示经助理转交）。数据都在 `agent_inbox_items.origin_ref` 和 `assistant_tasks.execution_session_id` 里，不需要新表。
- 注入文本的前缀保持现在的"仅作背景，不是新的指令"措辞，追加一条规则："用户在任务页提交的内容已经在那个任务里，不要再问他要，也不要替他重发。"
- 唤醒：`AGENT_INBOX_UPDATED` 事件已经订阅，执行会话的收件箱写入也会发它（`backend/agent/inbox.py` 带 `sessionId=执行会话`），只需 `_changed` 里按 `sessionId` 过滤时把执行会话也算进去。

**不做**：不把执行页提交当成语音轮次，不让前台替用户向任务转发。

### 3.2 任务控制状态变化作为事件流进通话

**目标**：任务被取消、暂停、卡在待核对、重新跑起来，电话里当场听到一句话，并且这句话带着下一步。

**机制**：

- `backend/assistant/control.py` 里每次改 `desired_state/observed_state` 的提交点（`accept_control_command`、`reopen_canceled_for_input_locked`、`acknowledge_effects`、`_resume_plan_locked` 失败分支），在事务提交后发一条总线事件 `assistant.task.control`，载荷：`userId、workspaceId、taskId、mainSessionId、desiredState、observedState、controlRevision、reason`（`reason` 取自命令 `source_ref`：`user_stop` / `card` / `voice` / `effect_unknown` 等）。
- `UpdateHints` 订阅这个事件，置 `reports` 标志。
- `ReportWatcher` 旁边加一个 `ControlWatcher`：读 `assistant_tasks` 自通话开始以来 `control_revision` 变大的行，去重后生成"状态备注"交给前台，在空闲窗口播。备注模板（口播稿，不含内部标识、字段名、接口名）：
  - 取消：`「{任务标题}」刚刚被停下了。{如果是用户在任务页点的：是你在任务页按了停止。}要继续的话跟我说一声。`
  - 待核对：`「{任务标题}」停下时有一步的结果没来得及确认。你确认过没问题的话，跟我说"核对过了，继续"，我就能让它接着做。`
  - 重开 / 恢复：`「{任务标题}」接着做了。`
  - 暂停：`「{任务标题}」先停在这儿了，随时可以让它继续。`
- 事件和报告一样走"空闲窗口注入"，用户正在说话或前台正在播报时不插话；同一任务 10 秒内多次变化只播最后一次。

**不做**：不在通话里播每一步工具进度；执行会话的 `tool.running` 不接进 `progress.py`。

### 3.3 给助理一个核对工具，让电话里的"继续"真的能解开任务

**目标**：用户在电话里说"核对过了，继续"，助理一次调用完成核对并恢复，不再把用户支到一个不存在的按钮前。

**机制**：

- 新增助理工具 `tasks.acknowledge_effects(task_id, expected_revision)`，直接复用 `backend/assistant/control.py` `acknowledge_effects`（幂等键由工具层按命令生成），与 `tasks.resume` 同在 `backend/tool/assistant_tools.py` 注册（`_tool("tasks.…", ControlArgs, …)`），同一回执形状。`backend/assistant/reporting.py`、`follow_through.py` 的后台工具集合同步加入。
- 工具的授权边界写进工具描述和 `backend/assistant/runtime.py` 里的助理提示：**只有用户本人明确说过"核对过了 / 没问题 / 继续"才能调用**，助理自己的判断、任务结果、电话里的助理发言都不算。这与 `projection.py` 里"语音上下文不是同意"的既有规则一致。
- `tasks.resume` 和 `tasks.followup` 返回 `ASSISTANT_EFFECT_UNRESOLVED` 时，错误文案里的指引从"在任务卡上点按钮（POST …）"改成两句：给模型的一句"如果用户已经明确确认，调用 tasks.acknowledge_effects 再 resume"，给人的一句"这一步要你确认过结果没问题"。
- 前台加一个 `DIRECT` 快读 `task_blocker(task_id)`：返回任务当前 `desired/observed` 状态和一句人话"卡在哪、要谁做什么"，让前台在用户问"怎么还没动"时一秒内答出来，不必走一次 15 到 25 秒的助理轮次。

**不做**：不让前台实时模型直接调核对；核对必须经助理轮次，留下 `AssistantCommand` 记录。

### 3.4 当前任务焦点进开场与应答

**目标**：用户任何时候都能确认电话里的它在说哪件事。

**机制**：

- `prompt.py` `with_sections` 加第三段 `focus`："当前在跟进：「{任务标题}」，{状态一句话}"。来源：本通电话里最近一次经 `assistant_ask` 交办或被 3.2 事件提到的任务；没有就留空。
- 开场（`phrases.py` `greeting_instructions`）：有焦点时在招呼语后追一句"上次说到「{任务标题}」，{状态}"。重新接通时这句就是连续感的来源。
- 应答规则：涉及任务的回复第一句点名任务标题；用户同时有多个活动任务时，前台先确认"你说的是「A」还是「B」"再转交。

## 4. 必须一起修的三处执行页问题

这三处不是语音改动，但 3.2 和 3.3 把用户往执行页引的概率更高，不补会再出一次今天的事故。

1. **错误文案**：核实后更正：`ASSISTANT_EFFECT_UNRESOLVED`、`ASSISTANT_TASK_NOT_RUNNING`、`ASSISTANT_TASK_HELD`、`ASSISTANT_STILL_STOPPING` 的文案 10-22 已随 `84a76b69` 进入 main 与线上 Web，10-31 用户 iPad 上看到通用"操作冲突"是因为 App 安装包早于该版本（文案随 App 二进制打包）。本次只补 `ASSISTANT_DUPLICATE_TASK`，并把 `ASSISTANT_EFFECT_UNRESOLVED` 的文案改成指向本页按钮；App 需随下次发版带上。
2. **App 缺核对入口**：`mobile/lib/features/chat/widgets/assistant_task_card.dart` 现在只在 `desired=paused` 时给"继续"按钮，取消加待核对的任务什么按钮都没有；执行页发送撞 `ASSISTANT_EFFECT_UNRESOLVED` 时也没有任何引导。对齐 Web：任务卡在 `observed=effect_unknown` 时给"我已核对，继续"，执行页发送失败时给同一个入口。Web 执行页的发送路径 `frontend-v2/src/features/chat/hooks/useSendChat.ts` 也没有处理这个码，一并补。
3. **任务会话里的停止按钮**：`mobile/lib/features/chat/widgets/composer/composer.dart` 的发送/停止变形按钮在任务会话里代价是取消整个任务并可能进入待核对。在 `assistantManaged` 会话里：停止态用明确的"停止任务"样式而非与发送同形的方块，点击先弹一次确认，文案说明"会停下整个任务"。Web `frontend-v2/src/features/chat/components/Composer.tsx` 的 `SendButton` 同样处理。

## 5. 顺序与验收

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P0 | 第 4 节三处 + 3.3 的助理工具和错误文案改写 | 回放本次事故：执行页误触停止后，用户在电话里说"核对过了继续"，一个语音轮次内任务恢复运行；App 执行页发送失败显示可操作文案和按钮 |
| P1 | 3.1 执行页提交进共享上下文 + 3.2 控制事件进通话 | 执行页上传图后 10 秒内，电话里问"收到图了吗"前台直接答"收到了，在「X」任务里"，不经助理轮次；执行页点停止后 15 秒内电话里听到状态备注 |
| P2 | 3.4 焦点段 + `task_blocker` 快读 | 重新接通时开场带上次任务；"怎么还没动"一秒内有答 |

每阶段都要有：单元测试（`backend/tests` 语音与 assistant 目录现有模式）、语音 WebSocket 集成测试回放、`mobile/scripts/check_locales.sh` 通过、真机一通电话的人工验收记录写进本文。

## 6. 风险与边界

- **口播越权**：3.2 的备注是前台在空闲窗口说的"背景"，和报告一样不构成用户同意、不触发执行。规则沿用 `reports.py` 的注入措辞。
- **核对工具被滥用**：3.3 的调用前提写死为"用户本人明确确认"，并在 `follow_through.py` 的漏转交检查里把"用户说了核对过了但助理没调"和"助理调了但用户没说过"都列为需要纠正的情况。
- **事件风暴**：控制事件按任务去重并限频；`ControlWatcher` 与 `ReportWatcher` 共用轮询节奏，不新增心跳。
- **多任务歧义**：3.4 的二选一确认是硬规则，前台不得猜。
- **线上代码与 main 不一致**：线上后端是叠补丁镜像，19 个文件不在 git（见 `docs/DEPLOY.md` 2026-10-31 记录）。语音模块与 assistant 模块发布前必须像 10-21 语音漏转交那次一样，以线上镜像为底只替换本次改动的文件，并逐文件比对哈希。
- **不解决的事**：语音轮次 2 到 3 分钟的延迟、前台重连后的听觉上下文丢失、执行页直接语音、输入框麦克风键。这些属于另一条线（见本文第 1 节的结构判断），不在本方案内。

## 7. 涉及文件

后端：`backend/voice/shared_context.py`、`reports.py`（或新文件 `control_notes.py`）、`updates.py`、`prompt.py`、`phrases.py`、`tools.py`、`backend/assistant/control.py`、`commands.py`、`reporting.py`、`follow_through.py`、`runtime.py`（助理提示）、`backend/tool/assistant_tools.py`（工具注册）。

Web：`frontend-v2/src/locales/*/errors.json`、`features/chat/components/AssistantTaskCard.tsx`、`AssistantEffectAcknowledge.tsx`、执行页发送与 `SendButton`。

App：`mobile/assets/locales`（与 Web 逐字节镜像）、`mobile/lib/features/chat/widgets/assistant_task_card.dart`、`chat_session_controller.dart`、`widgets/composer/composer.dart`、`state/session_stop.dart`。

## 8. 多任务串线审计（2026-10-31，gw2 最近 14 天）

样本：6 个有任务的主会话共 40 个任务；10 个主会话 88 通电话；30 次 `tasks.followup`、4 次 `tasks.resume`、95 次 `tasks.submit`；主会话用户消息 338 条。

**没有查到"追加到错的任务上"。** 30 次 followup 的目标任务与其引用的用户原话逐条核对，没有一例指向明显不相干的任务。只有两处异常：10-20 14:36 一次 followup 引用的原话是"打开抖音继续提交"，写进任务的却是"每种海鲜单独砂锅重制"（即 10-21 漏转交保护修的那起）；10-28 水果店任务把用户说的店名"小鸭"写成了"小丫"。用户纠正语句（"不是那个 / 我说的是 / 另一个"等）338 条里只有 1 条。

**查到的是任务分裂。** 40 个任务里 12 个（30%）是同一主会话里同标题任务的重复提交，形成 6 条链：荷叶奶蛙 4 个、艾薇兰 4 个（31 分钟内）、酒吧视频 3 个、雨后公园 3 个、武侠 2 个、砂锅海鲜 2 个。每个重复任务创建前 20 分钟内，主会话都出现过继续失败的工具错误：`ASSISTANT_EFFECT_UNRESOLVED`、`ASSISTANT_TASK_NOT_RUNNING`、`ASSISTANT_EXECUTION_UNAVAILABLE`、`ASSISTANT_VIDEO_TIER_CHANGED`、`superseded`、`ASSISTANT_HISTORY_CURSOR`，或前一个任务已处于 `error`。也就是说：原任务续不上时，助理（或被用户催着）就开一个新任务，尽管工具错误文案明写"不要提交替代任务"。

分裂的后果就是用户感觉到的"串线"：同一件事有三四个任务页、三四份报告、三四张卡；最活跃的主会话 16 个任务里 14 个 `desired_state` 仍是 running，从未收口。14 天内有 8 对任务结果在 5 分钟内先后到达同一主会话，其中 5 对发生在通话中，3 对是同一条链里的重复任务，电话里会把"同一件事"报两遍。

**对本方案的影响。**

- 第 3.4 节的焦点规则按原样做即可，不必升级成"必须带任务标识转交"，证据不支持前台选错任务是主要问题。
- 新增一条 P0：**续不上时不许开新任务**。服务端在 `tasks.submit` 时检查同主会话是否有标题高度相似且未归档的任务，有则 409 `ASSISTANT_DUPLICATE_TASK` 并回传原任务号，除非用户原话明确要求"另开一个"（命令带 `replace_of`）。配合把错误文案里的"不要提交替代任务"从劝告改成闸门。
- 新增一条 P1：**链式任务收口**。被同标题新任务接替的旧任务自动标记 superseded 并归档；`error` 状态超过 24 小时且无后续输入的任务提醒用户关闭。报告口播按链合并，只报最新一个。

## 9. 目标形态：全局助理 + 任务间（填缝之后的下一阶段）

状态：方向已在 2026-10-31 讨论中认可，未拍板排期。本节是目标形态和分步路径，不是本次填缝的工作量。

### 9.1 形态

系统已经是两层：个人助理主会话是全局层，每个任务的执行会话是任务层，执行页里打字就是给该任务追加输入。缺的是三样：用户看不出自己此刻在哪一层；语音永远只接全局层；全局层在"续不上"时另开一个任务而不是把人带回原来那个。第 8 节审计里 30% 的重复任务和 10-31 事故都出在这三样上。

目标形态把两层做成显式的"接待处 + 任务间"：

- **一件事一个房间。** 重做、改字幕、换模型、发布，全部是同一执行会话里的追加输入。房间不归档就不开第二间（第 8 节的 `ASSISTANT_DUPLICATE_TASK` 闸门是它的服务端保证）。
- **全局助理只做接待。** 听用户要什么，判断是新事还是旧事；新事开间，旧事领进去。它只收每间的结果摘要（现有任务报告机制），不保存房间内细节。
- **进门是显式状态。** 文字端：执行页头部常驻"你在：「{任务标题}」· 回到总助理"。语音端：前台把当前这通电话切到该房间，之后用户的话直接作为该任务的输入，该任务的进展、卡片、结果直接播给用户，不再经过全局助理转述。
- **出门也是显式的。** 说"回去"/点"回到总助理"/挂断，回到全局层。另一间有事时，由第 3.2 节的控制事件通道插播一句"「X」那间出结果了，要过去吗"，不自动切换。

### 9.2 它解决什么、不解决什么

解决：任务分裂（没有第二间可开）、焦点歧义（在哪间就是哪间）、转述延迟（任务层对话不绕全局助理的 2–3 分钟轮次）、"全局助理把人支到不存在的按钮前"（人就在房间里，按钮在手边）。

不解决：全局层的历史窗口（40 条 / 7.2 万字符）仍是固定窗口；跨任务问题（"上次砂锅视频用的什么档"）要靠任务事实表与记忆回答，不能靠历史；语音前台重连后的听觉上下文丢失仍在。

### 9.3 代价（只在用户显式进门后发生）

1. **节奏。** 房间里的脑子是 build 智能体，一轮是分钟级。语音在房间里要有"还在做第二镜，好了叫你"的节奏，不能沿用全局层的秒级应答；这要求把执行会话的工具进度接进前台（`backend/voice/progress.py` 今天只覆盖主会话循环）。
2. **治理口令。** 房间里"停"到底是停播报、停这一轮还是取消任务，必须二选一确认；取消后的待核对流程要有语音版本（第 3.3 节工具是前提）。
3. **全局层失明。** 只看摘要是好处也是代价，跨任务回答全部依赖 `tasks.get`/结果表/记忆。
4. **双房间并行。** 电话一次只能在一间；另一间的事件只能插播，不能抢占。
5. **三端协议。** 语音进门需要前台"房间绑定"事件与 UI 状态，PC 悬浮窗、Web、手机一起改。

### 9.4 分步路径

| 步 | 内容 | 依赖 | 验收 |
| --- | --- | --- | --- |
| 0 | 本方案 P0：核对工具、分裂闸门、错误文案、App 核对入口、停止确认 | 无 | 第 5 节 P0 验收 |
| 1 | 文字端任务间：执行页"你在哪间"头部与"回到总助理"；全局助理识别到旧事时的标准动作改为"带你进去"（回执里给执行页深链，不转述、不新建）；任务列表按链折叠，只显最新一间 | 步 0 | 同标题重复任务 14 天内为 0；用户从主会话进入既有任务的路径只需一次点击 |
| 2 | 执行会话进度与卡片语音化：`progress.py` 覆盖执行会话；确认卡、提问卡、权限卡在房间语境下的口播与应答 | 步 1、第 3.2 节 | 一通电话内听到某任务的"在做第几镜"与卡片提问，并能口头回答 |
| 3 | 语音进门：前台房间绑定、电话内切换、出门回全局层、跨房间插播 | 步 2 | 用户说"进艾薇兰那间"后，说的话直接成为该任务输入，结果直接播报；说"回去"后回到全局层 |

步 1 不动语音协议，步 2 不动绑定关系，只有步 3 改三端协议。每一步都可独立上线、独立回退。

### 9.5 与前文的关系

第 3 节四项填缝和第 8 节两条闸门是任务间模式的地基，不是被它替代：没有核对工具房间续不上，没有分裂闸门全局助理照样开第二间，没有控制事件通道跨房间插播无从谈起。任务间模式只是把这些能力收进一个用户看得见的形态里。


## 10. P0 实施记录（2026-10-31，分支 `feat/voice-seam-p0`）

- 后端：新增助理工具 `tasks.acknowledge_effects`（`backend/tool/assistant_tools.py`，复用 `assistant/control.py` `acknowledge_effects`，带 `source` 绑定原始人类消息并记入命令 `source_ref`）；`effect_unresolved_message` 改为同时面向模型与客户端的指引，不再把人支到任务卡；`assistant/commands.py` 新增 `similar_task_title` 与 `ASSISTANT_DUPLICATE_TASK` 闸门（仅对助理工具发起的新建生效，`tasks.submit` 新增 `new_despite_similar`）；`assistant/runtime.py` 助理提示补两条规则。测试：`tests/unit/test_task_title_similarity.py`、`test_assistant_commands.py` 新增重复任务用例；相关套件 111 项通过，`test_delegated_real_loop_runs_with_the_shared_sandbox` 在未改动的检出上同样失败，与本次无关。
- Web：执行页新增 `ExecutionEffectNotice`（任务 `effect_unknown` 或本页发送被 `ASSISTANT_EFFECT_UNRESOLVED` 拒绝时，在输入框上方给"我已核对，继续"）；任务卡的核对按钮对已取消任务也显示；任务会话的停止改为红色方块 + 内联二次确认（`useStopWithConfirm`、`StopTaskNotice`）。vitest 新增 4 项、调整 1 项；`AssistantTurn.timing` 与 `AssistantWelcome` 两个文件在 main 上本就失败（缺 QueryClientProvider），与本次无关。
- App：镜像 locales；`AssistantApi.acknowledgeEffects`、`AssistantController.acknowledgeEffects`、任务卡"我已核对，继续"菜单项；执行页 `ExecutionEffectNotice` 与 `StopTaskNotice`；任务会话停止键红色 + 确认；发送被拒时置 `executionEffectAckRequestedProvider`。`flutter analyze` 改动文件无问题；新增 widget 测试 3 项通过。`check_locales.sh` 的 knowledge/settings/voice 漂移为 main 既有问题。
- App 通话页卡片栏（同日追加，用户提出"通话里要能看到并切换卡片"）：`AssistantRequests` 新增 `deck` 布局，一页一张，头部显示"第 k / N 张"与上一张/下一张；`VoiceCallPage` 新增 `cards` 插槽，由 `app/router.dart` 注入（features 之间不互相 import），通话中球体下方最多占屏高 42%，无待处理时不占位；卡片打开对话前先收起通话页。复用助理页同一套读取（问题、权限、其他对话的等待），所以语音念到的卡和看到的卡是同一份。widget 测试 1 项。这是第 9 节第 2 步的可视部分先行落地；执行会话卡片的语音化仍在第 2 步。
- 未做：3.1 执行页提交进通话、3.2 控制事件进通话、3.4 焦点段、第 8 节链式收口（P1/P2）、第 9 节第 2 步的语音化部分。

## 11. 界面层辨识度：对话卡与任务间头部（2026-11-02 增补，并定全部剩余阶段的实施顺序）

状态：用户 2026-11-02 提出两条需求并要求"把方案实施完"。本节把两条需求写成设计，并给出 P1、P2、第 9 节步 1–3 的实施顺序。

### 11.1 两条需求

1. **任务卡旁边加一张"对话卡"。** 通话页"只听不看字"，用户回看不了刚才说了什么，也确认不了助理此刻在说什么。要一张纯文字的卡：上面是已经说过的历史轮次（用户说的、助理答的），底部是正在进行的这一轮实时刷出来。用户确认两种内容都要：**通话本身的转写**，和**当前任务那间执行会话里的来往**（任务助理在做什么、输出了什么、问了什么）。
2. **主助理和子任务助理的切换不明显。** 从任务卡点进执行会话，页面和主助理会话长得一样（同样的气泡、同样的输入框，只换了标题），用户说话时不清楚是在对全局助理说还是对某个任务的助理说；切换那一刻也没有任何动静。要"我现在在和谁说话"一眼可见，"切过去了 / 切回来了"有感知。

这两条都是第 9 节"全局助理 + 任务间"形态的界面层：第 1 条让任务间里发生的事看得见，第 2 条让用户知道自己站在哪一层。第 2 条就是第 9.4 节步 1 的"执行页头部与回到总助理"；第 1 条是方案此前没写的新增项。

### 11.2 对话卡

**位置。** App 通话页卡片栏（第 10 节的 `AssistantRequests(deck: true)`）的第一页，常驻：没有待处理请求时卡片栏只剩这一页，不再整体消失。Web 通话窗展开态加同一内容的字幕区。两端读同一份数据。

**两个通道，一个分段控件切换。**

| 通道 | 显示什么 | 数据来源 | 底部"正在进行"行 |
| --- | --- | --- | --- |
| 通话 | 本通电话里用户说的和前台答的，按时间顺序 | 新增服务端→客户端事件 `transcript`：`{role: user\|assistant, text, item_id, final, at}`，由 `backend/voice/bridge.py` `_said` 在每条最终转写落地时发出；`_on_assistant_transcribing` 的增量以 `final=false` 发出，客户端按 `item_id` 覆盖 | 前台正在说的话（partial），或"正在听…"（phase=listening 且 hearing） |
| 任务间：「{标题}」 | 当前焦点任务执行会话里最近的来往：用户在执行页发的、任务助理最终答复的正文、任务助理提的问题 | 客户端现有执行会话读取（App `chatSessionProvider(execution_session_id)` / Web `useSessionQuery` + 消息查询），只取文本正文，不取工具轨迹 | 该会话 `status` 忙碌时显示最后一轮的流式正文；有待回答卡片时显示"等你回答" |

通道默认跟随层级：没有焦点任务时只有"通话"；有焦点任务时默认停在"通话"，分段控件出现第二项，用户手动切；第 9.4 节步 3 语音进门落地后，进门时自动切到任务通道。

**焦点任务的来源。** 新增服务端→客户端事件 `focus`：`{task_id, title, session_id}` 或 `null`。服务端在三处更新焦点并发事件：某次 `assistant_ask` 的结果带 `running`（`TurnsMixin._settle` 写 `_passed_on`）、`ReportWatcher` 播了某任务的报告、客户端 `view` 消息指向某任务的执行会话（现有 `scope.viewed_session_id`）。这同一个焦点就是第 3.4 节"当前任务焦点"段的来源，P2 直接复用，不另存一份。

**规则。** 对话卡只显示，不是指令入口：卡上不放输入框，想对任务说话就点卡进那间（已有 `beforeOpen` 收起通话页的路径）。后台备注（`note` 角色的注入）不显示。通话通道只保留本通电话，最多 200 行，重连后从 `live_context` 的最近 12 行补；任务通道最多显示最近 8 轮。

**协议边界。** `transcript` 与 `focus` 都是加法事件，旧客户端按"未知类型忽略"处理（App `VoiceUnknownEvent`、Web 的 `default` 分支），不改音频、不改握手；写进 `docs/VOICE_CALL_SPEC.md` §5.3。服务端原来"转写绝不发给客户端"的注释随之改为"只发给本通电话的客户端"。

### 11.3 任务间头部与"带你进去"（第 9.4 节步 1）

**执行页头部（Web + App）。** 执行会话（`assistant_managed && kind != assistant && task_control != null`）的对话区顶部常驻一条横幅：左侧房间图标 + "你在任务间：「{任务标题}」"，中间任务状态短语（运行中 / 已暂停 / 待核对 / 已停止 / 已完成），右侧按钮"回到总助理"（Web `navigate(paths.assistant)`，App `context.go(Paths.assistant)`）。横幅用与主助理会话不同的底色，让两类页面一眼可分。主助理会话不显示它。

**服务端补字段。** `GET /api/agent/session/{id}` 的 `task_control` 加 `task_title` 与 `archived`（`backend/assistant/session_control.py` `target_for_session`），客户端不再依赖会话标题猜任务标题。

**进出有感知。** 从主助理进入执行页（任务卡、助理回复里的链接、通话页卡片）都走现有 `/app/s/{id}` 路径；进入后横幅在，退出靠横幅按钮。App 在进入执行页的瞬间弹一次轻提示"已进入「{标题}」"，Web 不弹（横幅本身可见）。

**全局助理识别到旧事时"带你进去"。** 现在的受理回执没有链接，助理回复只能转述。改三处：`tasks.submit` / `tasks.followup` 回执加 `url: /app/s/{execution_session_id}`；`ASSISTANT_DUPLICATE_TASK` 的 `details` 加 `url`，文案改成"用链接把用户带回那间，或 tasks.followup 追加"；`backend/assistant/runtime.py` 助理提示加规则：用户提到一件已有的事而不是下达新指令时，一句话说明现状并给进入那间的链接，不转述历史、不新建任务。

### 11.4 任务列表按链折叠与链式收口（第 9.4 节步 1 的第三项 + 第 8 节 P1）

- **折叠。** `GET /api/assistant/watch`（`backend/assistant/task_context.py`）按 `similar_task_title` 把同一主会话的同链任务折成一项，保留 `updated_at` 最新的，附 `chain: {count, task_ids}`。Web `AssistantTasks` 与 App `AssistantTasksSheet` 在卡片上显示"共 {count} 轮，显示最新"。`tasks.list` 不折叠，助理仍能看到全部。
- **归档。** 新建任务时（`accept_task_command` 创建分支，含人工路径），同一主会话里标题相似、且**不在运行**（`desired_state=canceled`，或 `observed_state` 属于 completed / error / idle）的旧任务自动归档并写 `superseded_by_task_id`（新增列，迁移 `ph95b6c7d8e9_assistant_task_superseded.py`，桌面 SQLite 列补丁同步）。运行中或等待输入的旧任务不动，只在 `chain` 里计数。
- **提醒关闭。** `watch` 项加 `stale: true`：`observed_state=error` 超过 24 小时且其间执行会话没有新的人工输入。卡片显示"出错超过一天，建议关闭"，操作仍是现有"不再跟进"。
- **口播按链合并。** 归档的任务其结果投递本来就被拦（`results.py` `task_archived`），`ReportWatcher` 不需要改。

### 11.5 实施顺序与验收

| 轮 | 内容 | 验收 |
| --- | --- | --- |
| A（本轮，PR 一个） | 11.2 对话卡（含 `transcript`/`focus` 事件）+ 11.3 头部与带你进去 + 11.4 折叠与归档 | App 通话里能读到自己和前台刚说的话，助理正在说的字实时出现；点进任务能看到那间最近来往；执行页顶部始终能看到在哪间并一键回去；助理对"那个艾薇兰的事怎么样了"回一句现状加链接而不是新建；任务列表里艾薇兰 4 个任务只显示一项并标"共 4 轮" |
| B | 第 5 节 P1：3.1 执行页提交进共享上下文 + 3.2 控制事件进通话（`ControlWatcher`） | 第 5 节 P1 验收 |
| C | 第 5 节 P2：3.4 焦点段（复用 11.2 的焦点）+ `task_blocker` 快读 | 第 5 节 P2 验收 |
| D | 第 9.4 节步 2：`progress.py` 覆盖执行会话；房间语境下卡片口播与应答 | 第 9.4 节步 2 验收 |
| E | 第 9.4 节步 3：语音进门、电话内切换、出门、跨房间插播；对话卡随进门自动切通道 | 第 9.4 节步 3 验收 |

每轮独立 PR、独立上线、独立回退。gw2 后端继续叠补丁发布（第 6 节最后一条）；App 文案随 Web 逐字节镜像，随下次发版带上。

### 11.6 实施记录

**轮 A（2026-11-02，分支 `feat/task-rooms-step1`）**

- 后端语音：`voice/events.py` 新增 `transcript`（role/text/item_id/final/at）与 `focus`（task 或 null）两个加法事件；`Bridge._said` 对用户与前台的最终转写发 `final=true`，`_on_assistant_transcribing` 按 item 累积后发 `final=false`，保活与未验证回复不发，后台备注不发；`Bridge.set_focus_task` 只在变化时发事件，三处调用：`TurnsMixin._settle`（交办带 running 的第一个任务）、`TurnsMixin.report`（播报的任务）、`api/voice.py` 的 `view` 消息（`task_of_session` 查执行会话对应任务）。`docs/VOICE_CALL_SPEC.md` §5.3 补两行并改掉"不下发转写"的说明。测试：`test_voice_bridge.py` 新增 3 项；`test_a_dropped_session…` 的"outbox 为空"断言改为排除 transcript。`test_voice_turns.py::test_assistant_ask_is_acknowledged_at_once…` 在 main 上本就因 `phase.hearing` 字段失败，与本次无关。
- 后端任务：`assistant_tasks` 新增列 `superseded_by_task_id`（迁移 `ph95b6c7d8e9`，桌面 SQLite 列补丁同步）；`commands.supersede_chain_locked` 在创建任务时把同主会话、标题相似、且已停止或已结束（`REPLACEABLE_OBSERVED`）的旧任务归档并记下接替者，运行中或等待输入的不动；受理回执加 `url` 与 `superseded_task_ids`；`ASSISTANT_DUPLICATE_TASK` 的 `details` 加 `url`，文案加"用户只是想看或聊那件事时给链接带回去"；`task_context.fold_chains` 只用于 `GET /api/assistant/watch`（客户端列表），模型读的环境列表不折叠；`watch` 项加 `stale`（error 超过 24 小时未动）；`task_control` 加 `task_title`、`archived`；助理提示加"提到旧事不下新指令就带进去"规则。测试：`test_assistant_commands.py`、`test_assistant_task_context.py`、`test_assistant_session_control.py` 各加一项。
- Web：`TaskRoomBanner`（执行页顶部，`lib/room-state.ts` 判状态词，`回到总助理` 链到 `/app/assistant`）；`AssistantTasks` 卡下显示"共 N 轮，显示最新"与"出错超过一天"提示；通话窗展开态新增 `VoiceTranscript`（通话行、进行中斜体、焦点任务可点进执行页）；reducer 新增 `transcript`/`focus` 处理（最多 200 行，最终行覆盖 partial，迟到 partial 不回退）。vitest 新增 6 项。
- App：`VoiceTranscriptEvent`/`VoiceFocusEvent` 与 `VoiceLine`/`VoiceFocusTask` 状态；`TaskRoomBanner`（执行页顶部，进入时弹一次"已进入「…」"）；`AssistantRequests` 新增 `leading` 页，有它时卡片栏不再因无待办而消失；`app/voice_conversation_card.dart` 在应用层组合语音状态与聊天存储：两个通道"通话 / 任务间：「…」"，任务通道读执行会话最近 8 轮正文、忙碌与等待提示、"进这间说"按钮；`router.dart` 注入为卡片栏首页；任务列表卡下显示链数与过期提示。widget/单元测试新增 7 项。
- 文案：`chat.json` 新增 `room.*`、`assistant.taskList.chain/stale`；`voice.json` 新增 `transcript.*`；App 逐字节镜像。`check_locales.sh` 仍报 knowledge/settings 漂移，为 main 既有问题。
- 未在本轮：通话里"进门"后对话卡自动切通道（轮 E）；任务通道的"正在进行"只到会话忙碌态，工具级进度在轮 D。

**轮 B（2026-11-02，分支 `feat/task-rooms-p1`，第 5 节 P1）**

- 3.2 控制事件：总线新增 `assistant.task.control`（`bus/events.py`），`assistant/control.py` `publish_task_control` 在 `accept_control_command`（reason `cancel:user` / `pause:assistant` 等）、`acknowledge_effects`（`acknowledged`）、`block_resume`（`blocked:<code>`）提交后发出，`commands.accept_task_command` 在取消后被任务页输入重开时发 `reopened:user`。`voice/updates.py` 新增 `controls` 事件并订阅它；`voice/control_notes.py` `ControlWatcher` 记录通话开始时每个任务的控制修订，之后修订增大的任务按 `kind_of` 判定取消 / 待核对 / 暂停 / 恢复 / 受阻，用 `note_text` 生成口播稿（无字段名），取消时查最近一条 `task_cancel` 命令无工具来源即说"是你在任务页按了停止"。`TurnsMixin.control_note` 把它当未被要求的报告排队（走 `_next_delivery` 的"等用户说完、随答案带出"规则），同一任务未播出的多次变化只留最后一次，尊重 `tell_reports`，并设焦点。
- 3.1 执行页提交：`SharedContextWatcher._read` 的人工输入从只查主会话扩成主会话 + 该用户未归档任务的执行会话，任务页条目带 `task_id`、`task_title`、`where=task_page`，前缀加"内容已在那个任务里，不要再要、不要重发"；`CallUpdates` 对任务房间的 `agent.inbox.updated` 置 `context`（房间集合来自 watcher 每次读取）。
- 测试：`test_voice_control_notes.py`（判定、口播稿、真实库里任务页停止→一条备注→不重复→通话中新建任务不播）、`test_voice_updates.py`、`test_voice_bridge.py`（合并与 tell_reports）、`test_assistant_assets.py`（任务页输入进共享上下文）各加一项。
- 未在本轮：3.2 的"同一任务 10 秒内多次变化只播最后一次"以"未播出前合并"实现，已播出后的再次变化会再播一次。

**轮 C（2026-11-02，分支 `feat/task-rooms-p2`，第 5 节 P2）**

- 3.4 焦点段：新模块 `voice/focus.py`。`state_phrase` 把 desired/observed（含等待的卡片问题）说成一句人话，三处共用；`recent_focus` 取主会话 24 小时内最近动过的未归档任务，作为 `FrontFacts.focus`（`prompt.front_context` 并行读取，失败留空）；`phrases.greeting_instructions(focus=)` 在开场招呼后接一句"上次在跟进的事：「X」，状态"（关闭回顾、首次使用、unified 不加）；`prompt.with_sections(focus=)` 加"当前在跟进：「X」，状态。涉及任务的回复第一句先点名；所指不明先问清是哪一件再转交，不猜"。`Bridge(focus_task=)` 以它初始化焦点并在开场前发 `focus` 事件；`set_focus_task` 接受 `state`，状态变化只更新指令不重发事件；交办（"刚交办过去，正在做"）、报告（"刚出了结果"）、控制备注（备注原文）都带状态。
- `task_blocker` 快读：`DIRECT` 新增，按 `task_id` 或口头标题（包含或相似）找任务，不给则取最近在跟进的；返回 `say`（可直接口播）与 `waiting_on_card`，注明恢复/核对必须经 `assistant_ask`。注册在 `DIRECT` 末尾，不改既有工具顺序。
- 测试：`test_voice_focus.py` 三项（状态句无字段名、开场与指令点名、真实库里 task_blocker 从排队到停止、陌生人 not_found、下一通电话的 recent_focus）；`test_voice_bridge.py` 一项（接通即点名、发 focus、状态变化不重发）。

**轮 D（2026-11-02，分支 `feat/task-rooms-step2`，第 9.4 节步 2）**

- 执行会话进度进通话：`voice/progress.py` 新增 `RoomProgress`，订阅 `tool.running` / `message.text_delta` / `session.status`，只收该用户任务房间（集合来自 `SharedContextWatcher.rooms`）的事件，把 build 智能体的工具换成口播短语（`ROOM_STEPS`：在生成视频、在发布到抖音、在等你接管云桌面……；未知工具只说"在处理"，不念工具名；会话回到 idle/waiting_input/error 即清掉；超过 180 秒的旧步骤不算"现在"）。`Bridge.refresh_room_progress` 每秒由 `timers()` 调一次：焦点任务房间的步骤变了，就更新指令并发一次带 `progress` 的 `focus` 事件，同一步骤不重复。
- 房间语境口播：`prompt.focus_section` 在有进度时写成"「X」，正在生成视频"，并加节奏规则"它一轮是分钟级：用户问进度就按这一步答一句，说好了会叫他，不追问也不替他催"；待答卡片归属以卡片自己标的任务名为准（`question_instructions` 原有规则不动）。
- 客户端：`focus.task.progress` 字段（空串表示空闲）；App 对话卡任务通道底部显示"任务正{step}，好了会叫你"，Web 通话窗同句；文案 `voice.transcript.taskDoing`，App 逐字节镜像。
- 测试：`test_voice_bridge.py` 一项（房间事件→指令与 focus 事件各一次、非房间与他人事件忽略、回到 idle 清空、未知工具"在处理"、过期不算）。
- 未在本轮：确认卡/权限卡在房间语境下的专门口播（现有 `card_instructions`/`question_instructions` 已点名任务，未再加）。

**轮 E（2026-11-02，分支 `feat/task-rooms-step3`，第 9.4 节步 3）**

- 进门与出门：`voice/room.py`。前台快读 `room_enter(task_id|title)`（用户说"进艾薇兰那间""到那个任务里说"时）把这通电话绑到该任务的房间：`Bridge.enter_room` 记下 `room`，设焦点，发 `focus(bound=true)`，会话指令加 `room_section`（"你现在在「X」这间：用户说的话都是给这个任务的……说'回去'就 room_leave……别的任务出结果只提一句"）。`room_leave` 解绑并发 `focus(bound=false)`。挂断即解绑，不持久化。`CallScope` 新增 `call` 字段供这两个工具回到桥接对象；只是切换，不改任务本身。
- 房间内的交办：`AssistantLink.start(ref, room=)` 在绑定时改走 `inputs.accept_session_input(执行会话, text, client_id=voice-room:…)`，即任务页打字同一条路（取消的任务也会被它重开），不经全局助理；`wait()` 读执行会话的答复，不读主会话的卡片/回执/决定；超时话术改为"交给这间了，做好了我叫你"（`room_pending`）。三处 `link.start` 调用都带 `room=self.room`。
- 房间的回复直接播：`RoomWatcher` 轮询绑定房间执行会话的收件箱（`updates.rooms` 由房间的 `agent.inbox.updated`/`session.status` 唤醒），进门后新结算且成功的运行其答复成为 `RoomReply`，`TurnsMixin.room_reply` 不看 `tell_reports` 直接排队播；带 `result_id`（`TaskResult.result_message_id` 反查），全局助理随后对同一结果的报告被 `_claim_delivery` 去重。
- 跨房间插播：`report()` 在绑定且报告的是别的任务时只说"「X」那间出结果了，要过去看看吗？"（`phrases.room_interjection`），不展开，不自动切换。
- 客户端：`focus.bound`；App 对话卡监听它自动切"任务间"通道并显示"你在这间说话：……说'回去'回到总助理"，Web 通话窗同句；`docs/VOICE_CALL_SPEC.md` 的 `focus` 行补 `progress`/`bound`。
- 测试：`test_voice_room.py`（口头标题进出、无 call 不可用、房间回复进门后才播且只播一次）；`test_voice_bridge.py` 一项（进门后交办带 room、别的任务只插播、房间回复不看 tell_reports、出门解绑）；`FakeLink.start` 接受 `room`。
- 未在本轮：房间内"停"的三义确认（停播报/停这一轮/取消任务）仍走现有停止确认与核对流程；PC 悬浮窗无独立进门入口（Web 通话窗只显示状态）。

**上线（2026-11-02）**：五轮合并为 `main@a9278fd2`（PR #113/#115/#117），gw2 `20261102-rooms-a9278fd2` 17:37 切换，带迁移 `ph95b6c7d8e9`；App 1.0.44 (55) 双端同日出包，iOS 17:40 上传 TestFlight。发布细节与验收要点见 docs/DEPLOY.md 与 docs/MOBILE_RELEASE_1_0_44_20261102.md。真实通话验收未做。

## 12. 收口：助理、帮手、任务单（2026-11-02 拍板，叫法方案 A）

状态：方向与叫法已拍板，本节是设计与实施顺序；代码未动。系统提示词的精简与流程说明在另一个会话里做（见 12.6）。

### 12.1 心智模型与对外叫法

用户的理解：**我把活交给助理；助理派人去干；干活的人用我们的 AI 工作台；干完向助理回报。** 三层各只回答一个问题，用户自始至终只和一个对象说话。

| 内部 | 对外叫法 | 用户看到的 | 用户在这里做什么 |
| --- | --- | --- | --- |
| 个人助理主会话 + 语音前台 | **助理**（用户起的名字） | 助理页、电话 | 说话：交代、问进度、改要求、停 |
| 执行会话（build 智能体） | **帮手** | 只在助理的转述里出现（"帮手那边做好了"），不自我介绍 | 不直接对话 |
| assistant_tasks 一行 | **任务单** | 一张卡，到处长一样 | 看状态、点主按钮 |
| 执行页 | **现场** | 「X」现场 · 帮手在做 | 看过程、加要求、回助理 |
| 工具（云电脑、生图、生视频、发布） | 不单独出现 | 现场的工作记录里一句话（"用云电脑打开了抖音"） | 无 |
| 任务报告 | **回报** | 助理转述的一两句 + 任务单 | 看 |

禁用词（对用户）：工头、工人、任务间、房间、子助理、执行会话、主会话、卡片栏。第 9 节"全局助理 + 任务间"的机制不变，措辞全部按本表替换。

### 12.2 任务单：一张卡，到处一样

- 一个任务只有一个卡组件，助理聊天里的受理卡、"我的任务"列表、通话页卡片栏共用（Web `AssistantTaskCard`、App `assistant_task_card.dart` 各留一个实现，三处只是容器不同）。
- 卡上固定四行：活叫什么；谁在做、到哪一步（`focus.progress` 或状态短语）；最新进展或结果一句话；**一个主按钮**。主按钮按状态只出一个：`去现场`（运行中/排队）、`他在问你`（等待输入，直接在卡上答）、`核对后继续`（待核对）、`再来一轮`（已完成，追加要求）、`重试`（出错）。暂停、取消、不再跟进收进菜单。
- 帮手的提问不再单独出一张卡：显示在它那张任务单里（"帮手问：用哪张图？"），在卡上回答或口头告诉助理。权限卡（云桌面接管等）同样挂在任务单上。
- 同链多轮已折叠（第 11.4 节），折叠行文案改为"第 N 轮"。

### 12.3 现场：看过程，不是换个人聊

- 从任务单"去现场"进入。顶栏"「{任务标题}」现场 · 帮手在做"（替换第 11.3 节的"你在任务间"），右上"回助理"，输入框占位"给这件活加要求…"。
- 页面里不出现"助理"字样；帮手不寒暄、不自我介绍（执行会话 system prompt 补一句）。工作记录按工具步骤折叠成一句话，产物（图、视频、文件）直接展示。
- 现场里说的话助理知道（第 3.1 节已做）。

### 12.4 交代一件活：不再先过两张卡

- 去掉建任务前的"视频档次"与"安排这件事"两张卡：助理按用户默认档位（无则系统默认）与最近项目（无则新建同名项目）直接开单；任务单上显示"快速档 · 视频创作"并可改。只有用户明确问到或改动时才多一轮。
- 服务端：`tasks.submit` 的 placement/setup 卡改为可跳过（默认 `auto_place=true`，带 `project_id` 或 `video_tier` 时不出卡）；两张卡的代码保留给用户显式要求"先问我"的场景。
- 助理回执改为两句话 + 任务单："好，派帮手去做 10 秒风景视频，快速档，大概几分钟。"不转述分镜、不贴长文；结果回报先一句结论，再最多三条要点，长内容放在任务单的结果里。

### 12.5 电话：只和助理说

- 通话页卡片栏只放两页：对话卡（第 11.2 节）与当前这件活的任务单（PR #120 已加）。待答问题随任务单显示，不再是独立页。
- "进门/出门"不再作为口令主路径：任务单上一个开关"**盯现场**"，打开 = `room_enter`，任务完成、用户切话题或关掉开关 = `room_leave`；口令仍可用但提示词里不再主动提。前台提示词与 `room_section` 的措辞改为"盯现场"。
- 助理的插播统一口吻："「X」那边帮手做好了，要看吗。"

### 12.6 助理懂这套流程 + 首次引导

- **系统提示词**（另一个会话）：精简啰嗦规则、加回复长度上限；加一段"你怎么运作"的说明，用 12.1 的叫法，能回答"我的任务去哪了 / 谁在做 / 怎么看进度 / 怎么改要求 / 怎么停"。语音前台提示词同步叫法。
- **首次引导**：第一次打开助理页（Web/App）弹一个四步引导：① 你只跟我说 → ② 我开任务单派帮手 → ③ 点任务单去现场看过程、加要求 → ④ 做好了我回报。每步一张图 + 一句话，可跳过。复用 App 既有引导框架（`features/onboarding`，蒙层引导）与 Web `AssistantIntro`。
- **随时呼出**：助理页右上角"?"（Web 顶栏 `AssistantPageActions`、App `_AssistantRoute` actions）重放引导；对助理说"你是怎么工作的"也能得到同一套说明（提示词保证）。
- 用户资料里记一次 `intro_seen`（Web localStorage + 服务端 profile 字段），跨端只弹一次。

### 12.7 实施顺序

| 轮 | 内容 | 端 | 说明 |
| --- | --- | --- | --- |
| F | 叫法替换（11.3 头部、12.3 现场文案、通话页卡片栏标题、`room_section`/插播口吻）+ 任务单合一（主按钮、帮手提问并入卡、折叠文案） | Web + App + 后端文案 | 纯界面与文案，不改数据 |
| G | 去掉建任务前两张卡（`tasks.submit` 默认自动安排）+ 助理回执/回报的长度（配合 12.6 提示词 PR） | 后端 + 提示词 | 要和提示词会话的 PR 一起上 |
| H | 首次引导四步 + 随时呼出 + `intro_seen` | Web + App + 后端 profile | 引导图用现有插画风格 |
| I | 电话："盯现场"开关替代口令主路径；待答问题并入任务单页 | App + 后端前台提示词 | 第 11.5 节口令保留 |

每轮独立 PR；gw2 后端继续叠补丁。真机与真实通话验收这次必须做一轮（11-02 的两次发布都没做）。

**F 轮已上线（2026-11-02 19:25，gw2 `20261102-rooms2-9a0b00de`，App 1.0.45 (56)，PR #124 与 #120）。** 系统提示词 PR #123 待随 G 轮合并。真机与真实通话验收仍未做。
