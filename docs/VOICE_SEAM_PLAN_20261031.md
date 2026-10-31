# 语音前台与任务执行页之间的缝：填缝方案（2026-10-31 草案）

状态：**方案，未拍板，未开工**。第 1–7 节是填缝方案，只改主会话与通话链路内部，不改通话与会话的绑定关系，不改三端语音协议的音频部分，不改任务治理的闸门语义。第 8 节是 10-31 多任务审计及其对方案的两条增补；第 9 节是填缝之后的目标形态（全局助理 + 任务间）与分步路径。

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
- 未做：3.1 执行页提交进通话、3.2 控制事件进通话、3.4 焦点段、第 8 节链式收口（P1/P2）。
