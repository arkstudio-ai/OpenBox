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
