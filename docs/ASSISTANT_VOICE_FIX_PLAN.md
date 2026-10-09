# 个人助理与语音通话修复计划（2026-10-07）

状态：**修复计划，可直接按本文开工**。分支 `codex/long-term-memory-plan`。前置文档：[语音方案](PERSONAL_ASSISTANT_VOICE_PLAN.md)（§10 为第二轮诊断）、[总规格](VOICE_CALL_SPEC.md)、[后端](VOICE_CALL_BACKEND.md)。本文把所有待修问题收成一份：每一项写清现状（代码位置）、根因、改法（文件、函数、参数、提示词、数据）、测试和验收，并按依赖排好顺序。文中“现有”均已核对源码；引用的行号以当前提交为准。

## 0. 问题清单

| # | 问题 | 现状证据 | 根因位置 |
| --- | --- | --- | --- |
| P1 | 开场固定 | 5 通电话 5 次“嗨，我在，你说。” | `backend/voice/phrases.py` 固定文本 + `phrase_instructions` 要求“只说这一句” |
| P2 | 结果照搬读、念两遍、念旧结果 | 23 句以“我这边查到了”开头；逐字念文字回复（中位 82 字、最长 271 字）；同一结果两次；用户提新要求时复读旧结果 | `phrases.delivery_instructions` 逐字朗读；`voice/bridge.py` 结果一到就 `function_call_output`，VAD 回复把它念了，随后我们安排的朗读再念一遍 |
| P3 | 前台没有上下文维护 | 接通后再无新信息进入；逐字稿留在上下文；520 秒通话累计输入 7.3 万 token | `voice/prompt.py` 只在接通时取画像与最近三句；无刷新、裁剪、轮换、通话摘要 |
| P4 | 简单查询也要等助理 13–22 秒 | 前台只有 `assistant_ask` | `voice/provider.py` `TOOLS` 只有一个工具 |
| P5 | 多件事不并行、串行等待 | 一句两件事只发一次调用且丢第二件；两句话串行各 15–20 秒 | 前台不能可靠拆分；`assistant/queue.py` 一次只执行一轮；主会话用 `gemini-3.8-flash-high` |
| P6 | 助理不能创建项目 | 回答“新建项目需要你在页面上手动点击新建” | 没有 `projects.create` 工具；`assistant/runtime.py` 提示词没写 |
| P7 | 不能代答/自动选择卡片 | `requests.answer` 返回 `ASSISTANT_ANSWER_HUMAN_ONLY: It asks the user to choose files`；`requests.reply` 返回 `ASSISTANT_REPLY_SOURCE_REQUIRED`；语音里“需要你在界面的卡片上亲手点一下” | `assistant/request_answers.py::answerable` 把 `allow_attachments`、`detail.kind` 一律判为只能用户答；`assistant/request_reply.py::human_evidence` 要求 `entrypoint == "assistant_turn"`（语音轮次是 `assistant_voice`）；语音端没有“展示卡片”和“口头作答”的动作 |
| P8 | 不能删除项目/会话/任务 | 提示词：“You cannot delete conversations; the user does that in the interface”；`assistant/session_tools.py` 注释“Deleting a conversation is never a tool” | 没有 `projects.delete`、`sessions.delete`、`tasks.delete` 工具 |
| P9 | 高危操作缺统一的二次确认，语音端没有口头确认 | 现有确认卡只有两处：工作区可见会话的发送确认（`assistant/confirmations.py`）、敏感记忆（`memory_tools.remember`） | 没有统一的高危操作表和确认协议；语音端无法读卡、答卡 |

另两处 bug：没查就说“查到了”（前台编造）；半句话（“新建一个。”）就交给助理。

## 1. 统一的二次确认协议（P9，先做，后面都依赖它）

### 1.1 高危操作表

| 操作 | 风险 | 确认方式 |
| --- | --- | --- |
| 删除项目、删除会话、删除任务（停止并不再跟进） | 高 | 必须确认卡；文字端点卡，语音端口头确认 |
| 向工作区可见会话发送文字/附件（现有） | 高 | 现有确认卡（`require_shared_send_confirmation`） |
| 代答含金额、付款、发布、授权、删除字样的问题；代答文件选择 | 高 | 确认卡（扩展 `request_answers` 的判定） |
| 敏感记忆（现有） | 中 | 现有卡 |
| 取消运行中的任务 | 中 | 确认卡（用户明确说“取消/停掉 X”时免卡：原话即授权，记录 `source_message_ids`） |
| 创建项目、派任务、改名、归档、记普通偏好、代答普通问题 | 低 | 不要卡；一句话汇报 |
| 权限批准、计划评审、云电脑接管 | 不可代办 | 永远用户亲自（现状保留） |

### 1.2 机制：复用主会话确认卡

现有 `assistant/confirmations.py::require_card(ctx, digest, prompt, header, description, confirm, target)`：在主会话建一张 `QuestionCheckpoint`（`continuation.kind="question"`，子键 `assistant_send`），工具调用挂起（`QuestionSuspended`）；用户答“确认”后再次以相同参数调用即通过一次。改造：

1. 把子键从写死的 `KIND = "assistant_send"` 改为参数 `kind`（默认仍为 `assistant_send`，新增 `assistant_confirm`），`require_card(..., kind="assistant_confirm", action="project_delete", target={...})`。`continuation[kind] = {action, digest, confirm, target, consumed, impact}`。
2. 卡片文案统一格式：第一行“要做什么”，第二行“影响”，选项 `确认`/`取消`。例：“删除项目「贪吃蛇」。影响：它的 3 个会话会一起删除，目录移到回收站，可以找回。” 文字端和语音端都用这段文案。
3. 过期：卡片 10 分钟未答 → `expires_at`，过期后工具再调用会重新出卡（`ask(..., expires_at=now+10min)`）。
4. 一张卡只能消费一次（现有 `consumed` 逻辑保留）。

### 1.3 语音口头确认

前台新增两个工具（`backend/voice/provider.py::TOOLS`，服务端在 `voice/bridge.py` 执行，不经助理模型）：

```json
{"name": "cards_pending", "description": "列出此刻等用户决定的卡片：个人助理要做的高危操作的确认、项目会话里的问题。返回要念给用户听的内容。", "parameters": {"type": "object", "properties": {}}}
{"name": "cards_answer", "description": "用户听完卡片内容并明确表态后，替用户作答。choice 必须是卡片给出的选项原文。", "parameters": {"type": "object", "properties": {"card_id": {"type": "string"}, "choice": {"type": "string"}}, "required": ["card_id", "choice"]}}
```

服务端实现 `backend/voice/cards.py`：

- `pending(user_id, workspace_id, main_id)`：两类来源合并返回——(a) 主会话上 `QuestionCheckpoint.status == "pending"` 的卡（`continuation.kind == "question"` 且有 `assistant_send`/`assistant_confirm`/`memory_proposal`）；(b) `assistant.request_answers.list_waiting()` 的任务问题（带 `assistant_may_answer`）。每张返回 `{card_id, kind, speech, options, high_risk}`，`speech` 是口语化文案（标题 + 影响 + 选项，去 markdown）。
- `read(card_id)`：对任务问题/权限调用现有 `request_display.review()` 得到 `display_token` 并立即 `displayed()`，把展示事件 payload 加 `channel: "voice", call_id`——这是“完整展示”的证据，和界面展示等价。主会话卡不需要展示事件（它本来就只属于主会话）。
- `answer(card_id, choice, call_id, transcript)`：主会话卡 → `question.question.reply(request_id, [[choice]], user_id, source_ref={"kind": "voice", "call_id": ..., "display_id": ...})`；任务问题 → 同一函数（UI 走的就是它）；权限请求 → 不允许（返回 `user_only`，前台说“这个要你在屏幕上点”）。校验：`choice` 必须与选项标签完全一致（前台不许改写）；高危卡要求 5 分钟内 `read` 过；一张卡只答一次。
- 作答后主会话里被挂起的工具调用自动继续（现有 `QuestionSuspended` 机制），前台 2 秒内收到助理的下一步进展（§5 的进度注入）。

前台提示词（§5.4）加规则：念卡片必须说清“要做什么、影响是什么”，然后问“确认吗”；只有用户明确同意（“确认/删吧/可以/就这样”）才调用 `cards_answer`；含糊、反问、沉默都不算；用户拒绝时选“取消”。

### 1.4 文字端

主会话卡片在 web/mobile 由现有 `AssistantRequests`/`QuestionDock` 渲染，无需改界面；只需确保卡片 `prompt` 已含影响说明。

### 1.5 测试

`tests/unit/test_assistant_confirmations.py`：`kind` 参数、过期重出卡、消费一次；`tests/unit/test_voice_cards.py`：pending 合并两类来源、高危卡未 read 不能 answer、choice 不匹配拒绝、权限请求拒绝、作答后挂起的工具再调用通过；`tests/integration/test_voice_ws.py` 增加“前台调用 cards_pending → cards_answer”的脚本用例。

## 2. P6：创建项目 `projects.create`

1. **服务**：新文件 `backend/assistant/project_tools.py`：
   ```python
   async def create_project(*, user_id, workspace_id, main_id, name, description=None, source=None) -> dict
   ```
   校验 `main` 权限（`_authority`），`_tool_source_locked(db, main, source, "project_create")` 记录来源；调用现有 `project.workspace.create_project(user_id, workspace_id, name, description=description)`；`ProjectError` → `AssistantError(400, "ASSISTANT_PROJECT_INVALID", str(exc))`；同名已存在 → 返回现有项目并标 `state: "existing"`（不报错，助理直接用）；成功后 best-effort 建目录（同 `api/projects.py` 的做法，拿 `sandbox_manager.get_client_any`）。返回 `{"project_id", "name", "state": "created"|"existing", "link": "/app?project=<id>"}`。
2. **工具注册**（`backend/tool/assistant_tools.py`）：`class ProjectCreateArgs(Arguments): name(1..128), description(≤500, 可空), source_message_ids(1..20)`；在 `_tool.execute` 加分支 `elif operation == "projects.create"`；在 TOOLS 列表加 `_tool("projects.create", ProjectCreateArgs, "Create a project the user asked for, named as they said, then hand work to it with tasks.submit. Existing name: returns that project. Not for guessing: only on explicit request or when the user names a project that does not exist.")`；`assistant/reporting.py::ASSISTANT_TOOLS` 加 `"projects.create"`。
3. **提示词**（`assistant/runtime.py` “Handing work to a project”段）加：“When the user names a project that does not exist, create it with projects.create and continue in the same turn; say so in one line. Never ask them to click 新建 in the interface.”
4. **测试**：`tests/unit/test_assistant_project_tools.py`：创建、重名返回现有、默认项目不重复、来源记录；提示词断言；语音回放用例“新建一个项目叫手机视频宣传，然后做一个 iPhone 18 口播视频” → 一轮内 `projects.create` + `tasks.submit`。

## 3. P7：代答与自动选择

### 3.1 放宽 `requests.answer`（`backend/assistant/request_answers.py`）

- `answerable()` 改为返回 `(reason, high_risk)`：
  - `continuation.kind != "question"`、工具不在 `ANSWERABLE_TOOLS`、权限/计划/接管类 → 仍 `human_only`。
  - `allow_attachments` 的问题：**不再一律拒绝**。若答案选的是不需要附件的选项（如“不需要，直接生成”）→ 可代答，`high_risk=True`；若需要附件 → 助理先 `assets.list` 找到文件，`requests.answer` 新增参数 `attachments: list[list[str]] | None`（现有 `questions.validate_answers`/`resolve_locked` 已支持附件），仍 `high_risk=True`。
  - `detail.kind` 存在（要求用户亲自操作的）→ 仍 `human_only`。
  - 文本/选项含“付款、支付、金额、发布、授权、删除、清空”等关键词或任务属于发布类 → `high_risk=True`。
- `answer_question()`：`high_risk` 或工作区可见 → `require_card(kind="assistant_confirm", action="request_answer", ...)`，文案“以你的名义回答「<会话>」里的问题：<问题> → <答案>。影响：<任务将继续执行/将发布…>”；否则直接代答（现状）。
- `RequestAnswerArgs` 加 `source_message_ids`（用户要求代答的原话），写进 `assistant_answer` 的 `answered_by`/command `source_ref`。
- 提示词（`runtime.py` “Questions, approvals and status”段）改写：“You may answer questions the user asked you to settle, including file-choice questions when their words settle the choice; the tool shows a confirmation card for high-risk ones (money, publishing, deletion, file choices) — in a call the front desk reads it to the user. Approvals, plan reviews and desktop takeovers stay with the user.”

### 3.2 `requests.reply` 支持语音轮次（`backend/assistant/request_reply.py`）

- `human_evidence()`：`entrypoint in {"assistant_turn", "assistant_voice"}`。
- `display_evidence()`：展示事件 payload 允许 `channel == "voice"`（§1.3 的 `cards.read` 写入），其余校验不变（≤5 分钟、唯一展示、摘要一致）。
- 这样“前台念卡 → 用户口头回答 → 该句话作为语音轮次进入主会话（自动携带 `request_context`）→ 助理 `requests.reply`”整条链路可用。但首选 §1.3 的 `cards_answer`（更快、不经助理模型）；`requests.reply` 作为文字端和复杂答案的路径。

### 3.3 “帮我自动选择”

用户说“你帮我选不需要，直接生成视频” → 前台把原话交给助理 → 助理 `requests.list` 找到问题 → `requests.answer(request_id, answers=[["不需要"]], source_message_ids=[那句话])` → 高危 → 卡 → 前台 `cards_pending` 读到卡 → 复述并问确认 → 用户“确认” → `cards_answer` → 挂起的 `requests.answer` 继续 → 代答完成 → 任务继续 → 前台说“已经替你选了不需要，视频在做了”。

### 3.4 测试

`tests/unit/test_assistant_request_answers.py`：文件选择题无附件选项可代答且出卡；需附件时带 `attachments`；`detail.kind` 仍拒绝；高危关键词出卡；`tests/unit/test_assistant_request_reply.py`：`assistant_voice` 轮次 + voice 展示事件通过，缺展示拒绝。

## 4. P8：删除项目、会话、任务

### 4.1 工具与服务（新文件 `backend/assistant/delete_tools.py`）

| 工具 | 参数 | 执行 | 影响说明（卡片文案） |
| --- | --- | --- | --- |
| `projects.delete` | `project_id`, `source_message_ids` | 先 `require_card(action="project_delete")`；通过后 `project.workspace.delete_project(project_id, user_id, workspace_id, sandbox=client)`；默认项目或有运行中会话 → `AssistantError(409, "ASSISTANT_PROJECT_BUSY", "<n> 个会话还在运行，先停止它们")`，助理据此建议先 `tasks.cancel` | “删除项目「X」。影响：其中 N 个会话一起删除，目录移到回收站可找回。” |
| `sessions.delete` | `session_id`, `source_message_ids` | 卡；`session.session.delete_session(session_id, user_id, workspace_id)`；若该会话是某个关注任务的执行会话 → 同时 `archive_task`（现有）；主会话不可删（现有 ValueError → 409） | “删除会话「X」（项目「Y」，M 条消息）。影响：会话不再显示，相关定时任务停用，正在等的问题作废。” |
| `tasks.delete` | `task_id`, `expected_revision`, `source_message_ids` | 卡；运行中先 `accept_control_command(action="cancel")`，再 `archive_task`；会话保留 | “停止并不再跟进任务「X」。影响：正在做的会停下，已完成的改动保留，会话仍在项目里。” |

共同点：`ToolSource` + `tool_command_key` 幂等；`AssistantCommand.action` 为 `project_delete`/`session_delete`/`task_delete`，`source_ref` 记用户原话；只操作本人拥有、本工作区的对象（`owned_session`、`get_project` 现有校验）。

### 4.2 注册与提示词

- `assistant_tools.py`：`ProjectDeleteArgs`、`SessionDeleteArgs`、`TaskDeleteArgs(TaskArgs + expected_revision + source_message_ids)`；三条 `_tool(...)` 描述都写明“confirmation card first”；`ASSISTANT_TOOLS` 加入。
- `session_tools.py` 模块注释“Deleting a conversation is never a tool”改掉。
- `runtime.py`：删除“You cannot delete conversations; the user does that in the interface”，改为“Delete a project, conversation or task only on the user's explicit request naming it (projects.delete, sessions.delete, tasks.delete). Each shows a confirmation card with the impact; in a call the front desk reads it and the user confirms by voice. Never delete on inference, from a summary, or because something looks unused; if the target is ambiguous, ask which one.”

### 4.3 测试

`tests/unit/test_assistant_delete_tools.py`：每个工具第一次调用出卡（`QuestionSuspended`）、答“取消”不执行、答“确认”后同参数执行一次、第二次不重复；项目有运行会话时 409；默认项目拒绝；主会话拒绝；删除会话时关注任务自动归档；PG 一次性库跑一遍。

## 5. 语音端改造（P1–P5）

客户端不改；全部在 `backend/voice/`。

### 5.1 工具调用改为“立即回执 + 备注交付”

- `bridge._on_tool_call`：`assistant_ask` 到达后立即 `send_tool_output(call_id, {"status": "accepted", "note": "结果稍后以备注送到"})`，不再挂起（删除 `pending_calls` 对 `response.create` 的约束；保留 `refs`）。
- 结果回来（`_settle`）：`provider.create_item(role="user", text="（后台备注，不是用户说的话）个人助理回来了：<清洗后的回复>")`（新方法：`conversation.item.create` `type: message / role: user / input_text`，实测可用），记下 `item_id`。
- 交付规则（`fill_idle`）：注入后 1.5 秒内若出现 `response.created`（用户正好在说话，模型顺带说了）→ 视为已交付；否则 `create_response(delivery_instructions(...))`。`delivery_instructions` 改为：“个人助理的结果到了。用你自己的话、结合刚才聊的内容告诉用户，三句以内；名字、数字、状态、选项必须和备注一致，不要加没有的事。”金额、审批选项：追加“这些要原文说：…”。删除 `FOUND`（“我这边查到了”）。
- 交付完成后 N（=3）个回复之后 `conversation.item.delete(item_id)` 清掉备注（§5.3）。
- 失败/超时话术保留，但也走备注 + 转述。

### 5.2 真实进度代替“还在办”

- `bridge` 订阅主会话的 `TOOL_RUNNING`/`message.text_delta` 事件（`bus.subscribe`），把当前工具名映射成人话：`tasks.list/get`→“在翻你的任务列表”、`history.read/results.read`→“在看那个对话的记录”、`tasks.submit/followup`→“在把活交给项目”、`memory.search`→“在翻记忆”、`projects.create`→“在建项目”、`requests.*`→“在看那张卡片”、文本生成中→“在整理回复”。
- 进度写进 `instructions` 的“当前后台进度”段（`provider.update_instructions()`：`session.update` 仅改 `instructions`，实测可用，只在 `idle` 时发）。
- 空闲（无回复、用户没说话）超过 `late_after`（改 12 秒）且有事在办 → `create_response("用一句自然的话说说现在在干什么，不要重复之前说过的话，不要调用工具")`，每件事最多 3 次，间隔 ≥12 秒。删除 `still_working` 固定短语。

### 5.3 上下文维护

- `voice/transcript.py`：服务端保存本通电话的句子（用户/助手/备注，带时间；不发给客户端）。
- 每 10 轮或 `response.done.usage.input_tokens > 60000`：用 `qwen3.8-flash`（现有 LLM 客户端）把早期转写压成 ≤300 字摘要，写进 `instructions` 的“本通电话到目前为止”段；`conversation.item.delete` 已交付的备注项和摘要覆盖范围内的早期轮次（需要记录 `conversation.item.created` 的 `item_id`，按顺序删）。
- 超过 25 分钟或 `input_tokens > 120000`：静默换会话——新开 provider 会话，`instructions` 带摘要与当前进度，客户端无感（`ready` 不重发；桥接器替换 provider 引用）。
- 挂断：生成通话摘要（说了什么、办了什么、还在办什么、用户表达的偏好），写入主会话一条系统备注消息（`origin="system_recovery"`、`synthetic=True`，界面显示为“📞 通话摘要”），记忆提取从中学习（如“叫我 Mary”）；开场时读最近一次摘要。

### 5.4 前台提示词（替换 `voice/prompt.py::FRONT`）

```
你是 OpenBox 个人助理的语音前台，正在和用户打电话。像一个熟悉用户的助理那样自然地说话：
- 每次用不同的说法，同一通电话里不说重复的话；不用“我这边查到了”“还在办”这类套话。
- 你能自己查的先自己查（tasks_overview、memory_search、schedules_list、projects_list、credits），再决定要不要交给助理。
- 要办事（建项目、派任务、改东西、记偏好、删除）就调用 assistant_ask，把用户原话原样交过去，先用一句话告诉用户你去安排。
- 只有工具结果和“后台备注”里有的事实才能说；没有就说没查到。不要编造进度。
- 后台备注到了，用自己的话结合刚才聊的内容转述，事实不变；备注说要用户确认的，先念清楚要做什么和影响，再问“确认吗”，用户明确同意才调用 cards_answer。
- 听起来没说完的话先等一等或追问一句，不要半句就交办。
- 用户说“停”“别说了”只是让你停下。
- 你是 AI 助理：寒暄简短友好，不说自己累了饿了。用用户的语言，一到两句话，不念链接、ID、编号。
```
开场：`create_response("电话刚接通。结合现在的时间、用户的称呼、上次通话聊的事和这段时间新办完的事，自然地打个招呼，一两句话。")`。

### 5.5 前台直接查询工具（新文件 `backend/voice/tools.py`）

| 工具 | 服务端实现 | 返回 |
| --- | --- | --- |
| `tasks_overview` | `assistant.reads.watch_list(user_id, workspace_id)` | 任务名、状态、最新结果一句话，≤12 条 |
| `memory_search(query)` | `assistant.memory.search(user_id, workspace_id, main_id, query, limit=5)` | 命中文本 + 时间 |
| `schedules_list` | `assistant.schedules.list_schedules(...)` | 名称、下次时间、启用状态 |
| `projects_list` | `assistant.reads.list_projects(...)` | 名称 |
| `credits` | `assistant.status_tools` 的 `status.credits` 读法 | 余额一句话 |
| `cards_pending` / `cards_answer` | §1.3 | — |

全部只读（除 `cards_answer`），服务端执行 ≤1 秒，结果以 `function_call_output` 回传，模型自己组织语言。写操作仍走 `assistant_ask`。

### 5.6 速度与并行（P5）

- 语音来源的轮次用快速档：`AssistantLink.start` 调 `accept_turn(..., variant="low")` 或 `model="openai/qwen3.8-flash"`（`accept_turn` 已有参数），先用回放用例比较两者的正确率，取合格且最快的；配置项 `VoiceConfig.turn_model`/`turn_variant`。
- 多条请求排队：`AssistantLink` 记录未完成数，`instructions` 进度段写“排队 2 件”，前台据此说明顺序。
- 一句话多件事：不拆分，原话整句交助理（助理一轮可派多任务，任务并行）。

### 5.7 回放测试

`backend/tests/manual/voice_replay.py`：把当晚 34 句用户原话（`.local-dev/assistant-backend.log` 里的转写）合成语音，按原顺序和间隔打一通电话，输出四个指标：重复句比例、编造次数（说了工具/备注里没有的事实）、同一结果复读次数、半句交办次数，以及每句的应答延迟。目标：重复句 <5%，后三项为 0。

## 6. 验收用例

| # | 场景 | 通过标准 |
| --- | --- | --- |
| 1 | 连打三通电话 | 三次开场各不相同，且提到时间或上次的事 |
| 2 | “我有哪些任务在进行” | ≤2 秒开口，内容与关注列表一致，不经助理 |
| 3 | “帮我看看贪吃蛇做得怎么样了” | 先一句应答；等待中说的是真实步骤；结果用转述且事实一致；只说一遍 |
| 4 | 结果到时用户正在说话 | 不插话；之后不重复念 |
| 5 | “新建一个项目叫 X，然后做 Y” | 项目建好 + 任务派出，一轮内完成，电话里一句话汇报 |
| 6 | 任务问“人物画面怎么准备”，用户说“选不需要直接生成” | 前台复述卡片内容并问确认 → 确认 → 任务继续；文字界面显示“由个人助理代答” |
| 7 | “把贪吃蛇项目删掉” | 前台念出影响并问确认；说“确认”才删；说“算了”不删；文字界面出现同一张卡 |
| 8 | “停掉五子棋的任务” | 运行中的任务停止并不再跟进，会话保留 |
| 9 | 通话 30 分钟 | 无感换会话，回答仍连贯；挂断后主会话出现通话摘要 |
| 10 | “以后叫我 Mary” | 下次开场用这个称呼 |
| 11 | 问天气 | 说查不到，不编 |
| 12 | 半句停顿 | 不交办，等或追问 |

## 7. 分期与任务清单

**阶段 A（后端语音，1 个代理）**：§5.1、5.2、5.4、5.5（不含 cards）、5.6、5.7。
**阶段 B（助理工具，1 个代理，可与 A 并行）**：§1.2、§2、§3.1–3.2、§4。
**阶段 C（打通，A、B 之后）**：§1.3 `cards_*`、§3.3 联调、§5.3 上下文维护、§6 全部验收。

每阶段结束：相关单元测试 + 全量 `tests/unit` 分片 + Web/移动端不变（只跑既有测试）；QA 重启后用回放脚本和真人各打一通。

## 8. 涉及文件

新增：`backend/voice/cards.py`、`backend/voice/tools.py`、`backend/voice/transcript.py`、`backend/assistant/project_tools.py`、`backend/assistant/delete_tools.py`、`backend/tests/manual/voice_replay.py`、对应单测。
修改：`backend/voice/{bridge,provider,prompt,phrases,assistant_link,config}.py`、`backend/assistant/{confirmations,request_answers,request_reply,runtime,reporting,session_tools}.py`、`backend/tool/assistant_tools.py`、`backend/core/config.py`（`VoiceConfig.turn_model/turn_variant`、`late_after` 默认 12）。
不改：前端、移动端、数据库表（确认卡复用 `QuestionCheckpoint`，通话摘要是普通消息）。

## 9. 实现记录（2026-10-07）

按 §7 分三段完成：阶段 A（语音前台 §5）、阶段 B（助理工具 §1–4）两个代理并行，阶段 C（`cards_*` 打通、QA 联调、浏览器与语音端到端）在其后。另按用户要求加了“语音助手声音选择”（§9.4）。

### 9.1 计划条目 → 代码

| 计划 | 实现 |
| --- | --- |
| §1.2 统一确认 | `assistant/confirmations.py`：`require_card(kind="assistant_confirm", action, impact, high_risk)`；卡片第一行“要做什么”，第二行“影响：…”；未答 10 分钟过期；答过的确认 30 分钟内有效、只用一次；`pending_cards()` 给语音读卡。`question/continuation.py` 认 `assistant_confirm`。 |
| §1.3 语音确认 | `voice/cards.py` + `voice/tools.py` 注册 `cards_pending` / `cards_answer`（见 §9.2）。 |
| §2 建项目 | `assistant/project_tools.py`：`projects.create`，同名返回已有项目（`state: existing`），不启动云电脑；**项目名必须出现在引用的用户原话里**（`ASSISTANT_PROJECT_NAME_UNSAID`，实测快模型会按旧话题起名“语音播报”再补建正确的一个）。 |
| §3.1 代答 | `assistant/request_answers.py`：文件选择题可代答（无文件选项或用户自己的文件），金额/支付/发布/授权/删除等关键词、文件选择、工作区可见会话 → “确认代答”卡；`detail.kind`、计划审阅、接管仍只能用户答。 |
| §3.2 语音轮次回复 | `request_reply.human_evidence` 接受 `assistant_voice`；`request_display.displayed(channel="voice")`。 |
| §4 删除 | `assistant/delete_tools.py`：`projects.delete` / `sessions.delete` / `tasks.delete`，卡片影响从数据库读；默认项目、自己的主会话 409；有运行中会话的项目 409 `ASSISTANT_PROJECT_BUSY`；命令先记后做，重复调用不会删两次。 |
| §5.1–5.7 前台 | `voice/turns.py`（立即回执、备注交付、半句拦截）、`progress.py`、`transcript.py`、`upkeep.py`、`summary.py`、`tools.py`（直接查询）、`prompt.py`（新前台提示词、自由开场）、迁移 `pbd3e4f5a6b7`（`voice_calls.summary`）、`tests/manual/voice_replay.py`。详见 VOICE_CALL_BACKEND.md §16–17。 |
| §5.6 快速档 | QA 设 `VOICE_TURN_MODEL=openai/qwen3.8-flash`、`VOICE_TURN_VARIANT=low`（三题实测 8–12 s，默认 gemini-3.8-flash-high 12–58 s）。 |

### 9.2 阶段 C：语音读卡与确认（`voice/cards.py`）

- 助理在卡片处停下时，这一轮的 Inbox 照常结算（run 以 `waiting_input` 结束），原来语音端只会说“办好了，结果在对话里”。现在 `AssistantLink.wait` 结算后读 `pending_cards()`，结果带上卡片；备注写成“个人助理要用户先确认，再动手。卡片1「确认删除」：删除项目「X」。影响：…选项：「确认」、「取消」”，交付指令只让前台念清楚并问“确认吗”，这一句不调用工具。
- 卡片用本通电话里的短编号（"1"、"2"），只有内容交给过模型的卡片才有编号。`cards_answer(card, choice)` 服务端校验：编号有效；卡片仍待答；`choice` 是选项原文；**卡片念出之后用户开过口**（等转写最多 2.5 s），且除“取消/不用记”外必须是明确同意（确认、可以、删吧…，含拒绝、犹豫、反问的不算）；同意必须在卡片（重新）念出后的两句话以内，否则要求重念（`read_again`）。
- 作答走和卡片按钮相同的 `question.question.reply`；主会话经 Inbox 续跑（`entrypoint: question_answer`），`AssistantLink.follow` 找到这条续跑并像普通轮次一样等结果、转述一次。确认后续跑**沿用语音轮次的快速模型**（`continuation._voice_turn`），文字轮次不变。
- 卡片刚念完用户就明确拒绝（“算了，先不删了”）时服务端直接选“取消”（`cards.decline`）：实测前台只会口头说“好，不删了”而不调用工具，卡片会一直挂在屏幕上。
- 卡片待答时用户说“确认/好的/算了”这类短答而前台却调用 `assistant_ask`：拦下并告诉前台用 `cards_answer`（新的主会话轮次会让卡片作废）。
- **说了不做的兜底**：前台回复里承诺去办（“这就去删”“我去安排”）却没调用任何工具、而用户的话是要办事时，服务端把用户原话按 `assistant_ask` 交给助理（实测拒绝一次后再要求删除，前台连说两次“这就去删”都没交办）。

### 9.3 联调中发现并修复的问题

| 现象（QA 实测） | 修复 |
| --- | --- |
| 语音/测试轮次的模型“粘”成主会话默认，之后打字也用 qwen-flash-low，出现“本轮未生成最终答复” | `agent/inbox.py`：`assistant_voice` 轮次及其续跑的模型只对本轮有效；QA 主会话模型恢复为 `openai/gemini-3.8-flash-high` |
| 快模型把项目建成“语音播报”再补建正确名字 | 项目名必须出自用户原话 |
| 进度句编情绪（“心里有点打鼓”），同一步骤 40 s 内说三次 | 进度指令“平实、不加情绪”；同一步骤 30 s 内不重复（新步骤仍 12 s） |
| 开场念“现在是 2026 年 10 月 7 日星期三 22 点 07 分” | 开场只按时间段问好，不报日期时间 |
| 摘要把“答应去删但没交办”写成“正在执行”，下一通开场说成“已经处理完” | 摘要：没有结果的写“还没有结果”；开场只有“上次通话后办完的事”里写了的才算办完 |
| 卡片被拒后助理说“语音和打字都没法替这一步” | 续跑提示改为“用户在卡片上（屏幕或通话里）拒绝了；再要求时重新出卡，可在屏幕或语音确认” |
| 助理建/删项目或会话后网页侧边栏不刷新 | `tool.completed` 带 `tool`，网页在 `projects.create/delete`、`sessions.delete/rename`、`tasks.submit` 后刷新项目与会话列表 |
| 通话摘要只给下一次开场 | 文字助理每轮上下文加“最近一次通话摘要”（24 小时内，不授予任何操作权限） |

### 9.4 语音助手声音选择

- 官方音色表（https://help.aliyun.com/zh/model-studio/omni-voice-list ，“Qwen3.8-Omni-Flash-Realtime”）列出 56 个，逐个实测全部可用；只列给旧模型的 25 个（Cherry、Ethan、Chelsie、Vivian…）在第一次回复时报 `Voice '<X>' is not supported`。官方默认是 Tina，当时配置默认 Serena；2026-10-09 按用户要求将默认值同步改为官方的 Tina。
- 设置里提供 15 个（`voice/voices.py`）：中文女声 甜甜 Tina、苏瑶 Serena、四月 Maia、清欢 Liora Mira、舒然 Mia、卡捷琳娜 Katerina、绵绵 Cici；中文男声 安德雷 Andre、林川野 Raymond、予安 Theo Calm、江晨 Evan、泽恩 Zane；英文 詹妮弗 Jennifer、敏儿 Mione、艾登 Aiden。方言、港台腔、角色音和外国人设不放进来。
- 接口：`GET /api/assistant/voice/voices`（列表、默认、当前）、`PUT /api/assistant/voice/voice`（只收列表内 id，存 `preferences.extra.assistant_voice`）、`GET /api/assistant/voice/samples/{id}`（约 4 秒试听，模型本身录的，AAC 32 kbps，`backend/voice/samples/`）。通话开始时按用户选择配置会话，`voice_calls.voice` 记录实际用的声音；通话中途不换。
- 网页：设置 → 语音通话（只在开通语音的部署显示），分组卡片、试听、点选即存。手机：设置页同名标签，试听用 `video_player`。实测选“清欢”后下一通电话 `voice_calls.voice = Liora Mira`。

### 9.5 验收结果（QA，2026-10-07）

| # | 结果 |
| --- | --- |
| 1 | ✅ 连续多通开场各不相同，提到时间段和上次的事（如“晚上好。上次聊到‘语音演练七号’已经建好了，不过那个误建的‘语音播报’还没定要不要删”） |
| 2 | ✅ “我有哪些任务在进行”前台直接查，说完话 1.9 s 开口，不经助理 |
| 3 | ✅ 先一句应答（1.8 s），等待中说真实步骤（“正在查看贪吃蛇对话的最近一次修改内容”），结果用自己的话转述一次（约 20–25 s） |
| 4 | 单元测试覆盖（备注在空闲时注入；抢先的 VAD 回复看到备注即算交付） |
| 5 | ✅ 语音建项目（只测了建项目；派任务部分由单元测试覆盖，避免在 QA 账号上跑付费任务） |
| 6 | ✅ 文字端：“帮我选先不发布” → “确认代答”卡（写明回答与影响）→ 确认 → 任务对话显示“由个人助理代答”并继续 |
| 7 | ✅ 语音：念出影响并问“你确认要删吗？”；“算了，先不删了” → 卡片取消、项目保留；再次要求 → 新卡 → “好的，我确认，删掉吧” → 0.4 s 内作答 → 18 s 后播报已删除；文字界面同一张卡。文字端也测了取消一次、确认一次 |
| 8 | ✅ 文字端 `tasks.delete`：确认后不再跟进，会话保留（测的是空闲任务） |
| 9 | 强制换会话实测 6.3 s 换好、回答连贯；摘要写入 `voice_calls.summary` 并进入文字助理上下文，**不在主会话插消息**（插消息会开启新一轮并作废待答卡片） |
| 10 | 未实测（依赖记忆写入；开场读画像记忆的称呼） |
| 11 | ✅ 问天气说查不到（文本探针） |
| 12 | ✅ 半句不交办（单元测试 + 文本探针） |

测速（修复后，`voice_speed_test.py`）：接通 0.36 s；开场出声 0.69 s；寒暄 1.43 s；直接查询 1.93 s；交办应答 1.83 s、结果 24.6 s（进度句在 15.4 s）；插话打断 0.76 s。

回放（`voice_replay.py`，当晚 34 句里不会真正办事的 25 句，同一 QA 账号）：

| 指标 | 修复前 | 修复后 | 目标 |
| --- | --- | --- | --- |
| 重复句 | 17/66（25.8%） | 5/49（10.2%） | <5% |
| “我这边查到了”开头 | 13/37 | 0 | 0 |
| 同一结果说两遍（脚本按相似度判断） | 3 | 2（用户连续两次问几乎同一个问题，两次回答相近） | 0 |
| 半句交办 | 0 | 0 | 0 |
| 首句开口中位数 | 1.39 s | 1.35 s | — |

重复句仍高于目标：多是用户换个说法问同一件事时，前台按同一份查询结果给出相近的句子；“Yeah, Mary.” 这类英文短句开口仍要 8 s（两次一样，疑为 VAD 判断），留待后续。

测试中建的“语音演练七号”“语音播报”“侧栏刷新测试”三个项目及其中的对话、任务，都在测试中按确认卡删除（“侧栏刷新测试”用来验证侧边栏：建好后约 10 s、确认删除后约 12 s 自动更新，无需刷新页面）；QA 账号其余项目未动，通话音色测试后改回默认。

### 9.6 已知问题

- 百炼会话 300 s 无任何回复会被服务端关闭（`response_idle_timeout`），通话以错误结束；用户长时间不说话时应改为礼貌挂断或保活。
- 手机端助理页仍不显示主会话的确认卡（确认发送、确认代答、记忆确认此前就不显示），需要加 `QuestionDock`；语音里可以确认。
- 卡片拒绝后若用户紧接着又要求同一件事，前台有时先反问“改主意了吗？”，再交办。


## 10. 追加修复（2026-10-08）

- **接通时“说一半、断掉、再说一遍”**：开场语音还在播时，外放回声或用户的声音被服务端当成插话，开场被掐断后模型又说了一遍。现在服务端先把开场完整生成好（最多 8 s）再接听，客户端这段时间响回铃音“嘟——”（1 s 响、4 s 停）；接听后开场一次播完，播放期间上行声音不送模型。实测拨号到接听约 2 s，接听后 7–63 ms 开场出声。
- **通话配额改为积分**：取消每天 1 小时的通话时长配额，通话和其他用量一样按积分结算（1 积分 = 1 元，按百炼返回的文字/语音用量和 `billing/rates.json` 里的单价）；余额不足时拒接（4029，提示“积分不足，充值后再打吧”并给“去充值”），通话中花到余额会先说一句再挂断；每通电话在用量里记一条“语音通话”。通话中费用显示改为“本次消耗 … 积分”。单通 30 分钟上限保留。详见 VOICE_CALL_BACKEND.md §18。

## 11. 追加修复（2026-10-08 上午）

- **任务结果没在通话里汇报**：QA 实测“让它在云电脑浏览器打开抖音创作者中心”，助理把事交给任务后这一轮就结束了；任务随后返回“打开受阻，云桌面自动化未就绪”，只写进了文字对话，通话里没人说，用户追问时前台还编了“它只说页面打开了”。现在通话期间任务汇报一结算就作为备注排进播报，空档时主动说（“对了，刚才那个口播视频有结果了……”）；同时到的几条合成一次说，用“另外”串起来，先说用户自己问的。用户问任务结果时前台先查 `tasks_overview`（最新结果给到前两句）。
- **语气怪**：按阿里云官方说明开启 `smooth_output`（口语化风格），重写前台口吻（口语短句、不用书面腔和“结论：”格式、长名字说顺口），转述改为“别念备注，用自己的话说”，进度句改成请对方稍等的口吻。实测：“我这边挺顺的。你最近忙啥呢？”“贪吃蛇那次只改了界面，没动游戏逻辑……”“那个口播视频发布的事，现在卡在云桌面没开和抖音没登录上……”。详见 VOICE_CALL_BACKEND.md §19。

## 12. 记忆检索时机、分工边界、转交整理（2026-10-08）

用户反馈：问“云杉项目负责人是谁”拿不到记忆；交给文字助理的是原话（“你使用工具查一下呀”），助理没有通话上下文；前台什么时候自己处理、什么时候交给助理的界限又死又模糊。按三条通用规则改：

1. **记忆检索时机**：接通就带上文字助理每轮都带的核心记忆；每句话转写一到就并行做同一套召回；前台答完由决策模型 JEV 核对，漏了、答错了、和记忆冲突就马上补一句；`memory_search` 改用同一套召回（跨项目、知识页、重排）。
2. **分工边界**：前台照常先答；JEV 同时判断这句话是闲聊、查询、要办的事还是没说清，答完再核对“要办的事有没有落下”，落下的自动转交并告诉用户；查询类请求即使交了出去，快速读足以回答时直接答，不开 15–40 秒的助理轮次。
3. **转交前整理**：前台给出能单独看懂的 `request`；转交前用百炼 qwen-flash（约 0.5 s）结合整段通话写成用户口吻的完整指令、纠正同音错字，或直接回答、或先问用户；助理那一轮同时拿到用户原话和通话最后几句。

实测与细节见 VOICE_CALL_BACKEND.md §20。

## 13. 说谎与空许诺（2026-10-08 下午）

用户实测：确认“放到测试项目里”后前台没交给助理还谎称建好了；助理建定时任务失败后说“你先别急，我这就重试，好了告诉你”，然后什么也没做。原因与修复：

1. **用户的答复没送到助理**：助理问了问题，前台没把答复交回去。现在助理的问句之后，用户的答复由后台自动交回（前台调了工具或反问时除外）。
2. **前台凭空说“建好了”**：回复声称办好了或用户问进展时，后台用本通电话的事实、任务列表和定时任务列表核对，不符马上更正；摘要不再采信前台自己的说法。
3. **助理空许诺**：一轮回复若许诺之后再做而本轮没有启动会回报的工作，就多给一步，要求当场做或如实说明；系统提示同步写明。
4. **建不成的直接原因**：模型把对象参数写成 JSON 字符串，现在所有工具统一自动解析。
5. 顺带修了：整理指令时编出用户没说的改法（换更稳的模型，缺内容就先问）、被打断的回复不检查、短句被挡回。

详见 VOICE_CALL_BACKEND.md §21。
