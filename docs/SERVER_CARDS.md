# 服务端弹窗卡片管线（2026-11-12）

后端（帮手的工具、个人助理、语音通话）需要用户做一个结构化选择时，用同一条管线：服务端出卡 → App 弹出底部面板（同时留在对话里）→ 用户点选、或在通话里口头说 → 服务端在恢复帮手的同一个事务里执行选择的效果。

第一张卡是**换视频模型**卡；以后语音助理里的其他交互（确认、二选一、档位选择等）都可以登记成新的卡种，不需要再改事件、接口和 App 管道。

## 为什么做

11-12 查 gw2 近 14 天数据：没选定视频模型的对话里，每个镜头用哪个模型由 AI 自己填，服务端不拦。48 个这样的对话里有 12 个在同一条视频里混用了 2–4 个模型。选定了模型的对话没有这个问题（102 个镜头全部一致）。

## 规则：一个对话一个视频模型

- 用户在输入框选了档位：以它为准（原有规则）。
- 没选：第一次出片时，服务端把默认档（`video_generation.model` 所在的档位，否则第一档）**锁定到这个对话**（写 `sessions.video_model` / `video_resolution`，发 `session.updated`，输入框胶囊随之显示）。
- AI 传了别的模型或分辨率：**不出片，弹「换视频模型」卡**（换成 X / 保持 Y，带每秒积分），用户选完写回对话，帮手不带 `model` 再提交一次。
- 同一对话里已有一张换模型卡在等，并发的其他镜头直接报错让帮手等，不会弹多张。
- 无人值守的运行（`kind=cron` 会话、自动营销 autopilot）保持旧规则：会话选了就用会话的，没选用 AI 传的；没人能答卡，autopilot 还有自己的模板锁。
- `action=validate` 不弹卡，只报不一致。子代理不能出卡，同样只报错。

代码：`backend/tool/video_model_card.py`，接入点 `tool/video_production.py` `_resolve_open_submission`。

## 管线

一张卡就是一条普通的 durable question（`question_checkpoints`），多带两样东西，所以 question 已有的持久化、`question.*` 事件、过期、新消息作废、恢复挂起的工具调用全部照用：

| 位置 | 内容 | 给谁看 |
|---|---|---|
| `Question.detail` | `kind`、`present`（`sheet` 弹窗 / `inline` 只在对话里）、`options[]`（与 `Question.options` 同序：`id`、`role`=`choose`/`cancel`、`recommended`、展示用附加字段） | App |
| `continuation.card` | `kind`、私有 `payload`、选项 `id`/`label`/`role` | 服务端 |

答案仍然是选项文字（`[[label]]`），所以不认识这个 kind 的老版本 App 会退回普通选项卡照样能答。

### 后端：登记一个卡种

```python
from question.cards import CardKind, CardOption, ask_card, register

async def _apply(db, session, row, payload, option):   # option=None 表示关掉或选了 cancel
    ...  # 与恢复帮手同一事务；可返回 "events" 在提交后发布
    return {"title": "...", "output": "给模型看的结果", "metadata": {...}}

register(CardKind(kind="xxx", voice_label="口头称呼", apply=_apply, voice_answerable=True, present="sheet"))

# 在工具里：
await ask_card(ctx, kind="xxx", header="...", question="...", options=[CardOption(...), ...], payload={...})
```

- 出卡的工具要在 `question/question.py` `QUESTION_TOOL_CONTINUATIONS` 里登记为 `"question"`（不能在 batch 里调用）。
- 新模块要在 `question/cards.py` `_load()` 里 import 一次（登记靠 import 副作用）。
- `ask_card` 抛 `QuestionSuspended`；工具里如果有 `except Exception` 要先放行它。

### 语音

- 主会话的卡：`assistant/confirmations.pending_cards` 自动列出已登记、`voice_answerable` 的卡，带 `option_roles`；`voice/cards.answer` 按 role 判断是不是"取消"，不再假定最后一项是取消。
- 帮手会话里的卡：`assistant/request_answers.answerable` 对已登记卡按 `voice_answerable` 放行，语音模型用 `task_question_answer` 代答（显示"由个人助理代答"，需要用户原话）。
- 通话中卡会弹在通话面板之上（见下），用户可以直接点，也可以说。

### App

- `mobile/lib/features/chat/cards/`：`ServerCard`（解析 `detail`）、`CardPopupHost`（挂在 `MaterialApp.builder` 里、`VoiceCallHost` 外层，所有页面和通话面板之上）、`answerServerCard`（与对话卡同一套提交：助理绑定的走 `AssistantReplyService`，否则 `POST /api/agent/question/{id}`）、`cardRenderers`（按 kind 的附加展示，没登记的 kind 只显示文字和选项）。
- 弹窗出现条件：待答问题里只有一题、带 `detail.kind`+`present=sheet`、2 个以上选项；按创建时间先来先弹。
- 「稍后再说」/点遮罩只收起弹窗，不作答；卡仍留在对话里。在别处答掉（对话里、其他设备、语音）后弹窗自动消失。
- App 启动时主动拉一次待答列表，后台帮手出的卡不用先打开那个对话也能弹。

## 没做 / 已知

- 卡片文字目前是中文（与现有任务准备卡一致），没走 Light 三语。
- Web 前端没做弹窗，Web 上仍是对话里的普通选项卡（能答）。
- 真实通话里口头答卡、gw2 真实帮手出卡的端到端没验，只有单测和 App 组件测试。
