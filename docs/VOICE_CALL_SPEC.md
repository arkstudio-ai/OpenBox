# 语音通话总规格：体验、状态机、协议、文案

状态：**实施规格，待用户确认后开工**。日期：2026-10-07。上游方案：[个人助理实时语音接入方案](PERSONAL_ASSISTANT_VOICE_PLAN.md)（为什么这样做、实测依据）。本文是三端共同遵守的契约；后端、PC、移动端各自的实现细节见 [后端](VOICE_CALL_BACKEND.md)、[PC 悬浮窗](VOICE_CALL_WEB.md)、[移动端](VOICE_CALL_MOBILE.md)。

文中“现有”指已核对的源码；其余为待实现要求。所有事件名、字段名、文案 key 以本文为准，三端不得各自另起名字。

## 1. 体验目标

用户点一下就在和个人助理“打电话”：接通有声音、说话有反应、它去办事会先应答、办完主动开口、随时能打断、挂断干脆。具体要求：

1. **接通像接通**：按下后 1 秒内听到接通音并看到“已接通”，紧接着助理打招呼（“嗨，我在，你说。”）。
2. **听得见它在听**：说话时球体随音量起伏；停下 0.5 秒内它开始回应或开始去办。
3. **办事先应答**：涉及项目、任务、记忆的请求，它先说“好，我看一下”，再去问文字版助理；结果回来后它主动开口“我这边查到了……”。
4. **等得明白**：超过 20 秒还没结果，它说一句“还在办，好了我马上告诉你”；用户这期间可以继续聊别的。
5. **打断即停**：用户一开口，它立刻停（200 ms 内停止播放）。
6. **一个脑子**：电话里说的和文字里写的是同一轮对话；通话中文字界面照常出现这一轮。通话界面本身不显示对话文字——像打电话一样只听不看；要看文字去对话页。
7. **不挡事**：PC 是一个小悬浮窗，可以收成一条小药丸；手机上收起后变成顶部通话条，其它页面照常用，随时点回来。
8. **挂断干脆**：挂断立即停止收音和播放，1 秒内显示时长与费用；如果还有没办完的事，告诉用户“结果会写在对话里”。

## 2. 术语

| 词 | 含义 |
| --- | --- |
| 前台 | 百炼 `qwen3.8-omni-flash-realtime` 实时模型：听、判停、打断、寒暄、口播 |
| 后台 / 个人助理 | 现有文字版个人助理（主会话 + Inbox + 工具），`backend/assistant/*` |
| 通话 `VoiceCall` | 一次从接通到挂断的会话；一个用户同时只能有一通 |
| 语音轮次 `VoiceTurn` | 前台把用户一句话交给后台的一次 `assistant_ask` 调用，对应主会话的一条用户消息和一条回复 |
| 口播稿 | 后台这一轮的回复文本，经清洗后交给前台念 |
| 固定短语 | 服务端触发、前台模型按指定文本原样说出的短句（打招呼、还在办、结果在对话里、到时间了）：`response.create` 带 `response.instructions`，与通话同一个声音，实测 0.6 秒出声 |
| 空闲窗口 | 前台既没有在生成/播报、用户也没有在说话的时刻；晚到的结果只能在这里注入 |

## 3. 一通电话的时间线

```
用户          客户端                      后端 /ws/assistant/voice              百炼前台            个人助理
 │ 点“通话”     │                                 │                                 │                  │
 │─────────────►│ 申请麦克风；播“接通中”音          │                                 │                  │
 │              │ POST /api/auth/ticket {audience:voice}                            │                  │
 │              │──── ws 连接 ?ticket= ───────────►│ 校验票据/并发/配额                │                  │
 │              │                                 │── 连接 + session.update(tools) ─►│                  │
 │              │◄── ready ───────────────────────│◄── session.updated ─────────────│                  │
 │              │ 播“已接通”音；                    │── response.create(只说招呼语) ──►│                  │
 │ “帮我看看贪吃蛇进展”                             │                                 │                  │
 │─────────────►│── PCM16 16k 100ms/包 ──────────►│── input_audio_buffer.append ───►│                  │
 │              │◄── phase:listening ───────────────────────────────────────────────│                  │
 │              │◄── playback.clear（speech_started 时）                              │                  │
 │              │◄── phase:thinking ──────────────│◄── speech_stopped ──────────────│                  │
 │              │◄── phase:speaking + 二进制 24k 音频（“好，我看一下”）◄── response ──│                  │
 │              │◄── turn{accepted} + phase:working ◄── function_call assistant_ask │                  │
 │              │                                 │── accept_turn(entrypoint=assistant_voice) ────────►│
 │              │   文字界面出现这条用户消息          │   wait_for_inbox_terminal        │                  │
 │              │◄── phase:working(late) + “还在办”（>20 s、空闲窗口，response 级指令）  │                  │
 │              │                                 │◄── turn.finished / result_message_id ─────────────│
 │              │                                 │── function_call_output（立即）    │                  │
 │              │                                 │── response.create(逐字朗读)（空闲）►│                  │
 │              │◄── turn{delivered} + 二进制音频（“我这边查到了……”）◄──────────────│                  │
 │ 点“挂断”      │── {type:stop} ─────────────────►│── response.cancel；等最后用量 ≤2 s │                  │
 │              │◄── ended{reason:hangup, duration, cost} ；ws 关闭 1000                │                  │
```

## 4. 状态机

### 4.1 客户端通话状态（PC 与手机完全一致）

```
idle ──start()──► requesting_mic ──granted──► connecting ──ready──► connected ──stop()──► ending ──ended──► ended
                        │ denied/missing/busy                │ error/limit/close                           │
                        └──────────────► ended(reason)       └───────────► ended(reason) ◄──────────────────┘
```

`connected` 内的 `phase`（由服务端 `phase` 事件决定，客户端不自行推断）：

| phase | 含义 | 界面文案 key |
| --- | --- | --- |
| `greeting` | 刚接通，固定短语打招呼中 | `voice:state.greeting` |
| `listening` | 在听 | `voice:state.listening` |
| `thinking` | 用户说完，前台在生成 | `voice:state.thinking` |
| `speaking` | 前台在播报 | `voice:state.speaking` |
| `working` | 有语音轮次挂起（后台在办）；`late=true` 时显示“还在办…” | `voice:state.working` / `voice:state.late` |

`working` 与 `listening/speaking` 可以同时成立（用户可以边等边聊）；界面以 `phase` 为主、`working` 为副标记（小字或图标），见各端文档。

客户端本地标记：`muted`（静音：客户端改发静音帧，不通知服务端）、`speakerOn`（仅手机）、`expanded`（PC 悬浮窗/手机通话页是否展开）。

手机端多一个本地状态 `paused`（来电或系统中断：暂停采集与播放、继续发零帧保持连接；中断结束回到 `connected`，超过 60 秒则结束 `error`），见移动端文档 §4。

`ended.reason` 枚举：`hangup`（用户挂断）、`error`（服务端 `error` 后关闭）、`limit`（单次时长到）、`quota`（当日配额用完）、`concurrent`（别处已在通话）、`mic_lost`（麦克风断开）、`network`（连接断开）、`unsupported`（浏览器/设备不支持）、`mic_denied`、`mic_missing`、`mic_busy`。

### 4.2 服务端桥接状态

```
idle ─speech_started─► user_speaking ─speech_stopped─► idle
idle/user_speaking ─response.created─► responding ─response.done─► idle
```

另有两张表：`pending_calls: call_id → VoiceTurn`（前台发出但尚未回传结果的 `assistant_ask`）和 `deliveries: [VoiceTurn]`（结果已到、等待空闲窗口注入）。规则：

1. `response.function_call_arguments.done` → 创建 `VoiceTurn`，向后台 `accept_turn`，向客户端发 `turn{state:accepted}` 和 `phase:working`。
2. 后台结束 → 立刻 `conversation.item.create(function_call_output)`（任何状态下都可以），把 `VoiceTurn` 放进 `deliveries`。
3. 仅当状态为 `idle` 且 `deliveries` 非空时发一次 `response.create`，带 `response.instructions`：“逐字朗读：我这边查到了，<speech>”（实测逐字一致、0.57 秒出声）；`response.created` 到达后把队首标记为 `delivering`，`response.done` 后发 `turn{state:delivered}`。实验证明在 `responding` 时发 `response.create` 会被拒绝（`Conversation already has an active response`），重复回传同一 `call_id` 会被拒绝（`Duplicate function call output`），所以每个 `call_id` 只回传一次、`response.create` 只在空闲发。
4. `pending_calls` 中某项超过 `late_after_seconds`（默认 20）且状态 `idle` → 固定短语 `still_working`（`response.create` 带“只说这一句，不要调用任何工具：还在办，好了我马上告诉你。”；实测照说且不重复调用工具；每个轮次最多一次），向客户端发 `phase:working late=true`。
5. 超过 `turn_timeout_seconds`（默认 120）→ 回传 `{"status":"timeout","speech":"这件事还在办，办好了我在对话里告诉你。"}`，`VoiceTurn.outcome=late`；后台稍后结束时结果照常进主会话，不再注入前台。
6. 用户挂断时仍有 `pending_calls` → `ended` 事件带 `pending_turns: n`，客户端提示 `voice:ended.pendingHint`；后台继续办。
7. 固定短语与结果朗读都是前台模型的一次回复：只在 `idle` 触发；同一时刻只有一个回复；用户开口时服务端 VAD 自动取消（`interrupt_response=true`），桥接器照常发 `playback.clear`。接通后的招呼 `greeting` 在 `session.updated` 后立即触发。

## 5. 协议

### 5.1 票据与握手

- 客户端先 `POST /api/auth/ticket`，请求体 `{"audience": "voice"}`（现有路由无请求体；新增可选字段，缺省行为不变）。服务端用现有 `create_ticket(..., audience="voice")`。
- 连接 `wss://<api>/ws/assistant/voice?ticket=<票据>`。服务端用现有 `consume_ticket(ticket, audience="voice")`，再用现有 `SocketAccess.from_ticket(...).check()`；通话期间 `access.watch()` 每 5 秒复核（与 `/ws/agent` 相同）。
- 主会话必须已存在（`assistant.service.get_main_session`）；不存在则关闭 4404，客户端先调用现有 `POST /api/assistant/ensure` 再重试（网页和手机打开助理页时已经做过）。
- 握手通过后服务端先发 `ready`，客户端收到 `ready` 前不得发送音频。

### 5.2 二进制帧

| 方向 | 内容 | 约束 |
| --- | --- | --- |
| 客户端 → 服务端 | PCM16 小端、16 kHz、单声道 | 每帧 100 ms = 3,200 字节；服务端拒绝 >12,800 字节或奇数长度的帧（关闭 4400）；静音时客户端照常发零帧 |
| 服务端 → 客户端 | PCM16 小端、24 kHz、单声道 | 大小不定，按到达顺序播放；收到 `playback.clear` 后丢弃队列中未播放的音频 |

### 5.3 服务端 → 客户端 JSON 事件

所有事件形如 `{"type": "...", ...}`。未知类型客户端忽略。

| type | 字段 | 说明 |
| --- | --- | --- |
| `ready` | `call_id`, `model`, `input_sample_rate` (16000), `output_sample_rate` (24000), `max_seconds`, `price_date` | 握手完成；`max_seconds` 已取单次上限与当日剩余配额的较小值 |
| `phase` | `value` ∈ greeting/listening/thinking/speaking/working, `working` (bool), `late` (bool) | 界面状态唯一来源 |
| `playback.clear` | — | 用户开口或回复被取消：客户端立即清空播放队列并停止当前播放 |
| `phrase` | `key` ∈ greeting/still_working/result_in_text/limit_reached | 一个固定短语回复开始（音频走普通二进制帧）；客户端只用于计时与调试，不显示文字 |
| `turn` | `turn_id`, `state` ∈ accepted/working/late/delivered/timeout/failed, `inbox_id`, `message_id` (可空) | 一个语音轮次的进度；客户端据 `message_id` 让文字界面滚到该消息 |
| `cost` | 同现有 demo `CallMeter.snapshot()`：`total_yuan`, `confirmed_yuan`, `provisional_yuan`, `costs_yuan{input_text,input_audio,output_text,output_audio}`, `tokens{...}`, `settled_rounds`, `unreported_rounds`, `pending`, `final`, `price_date` | 每轮 `response.done` 后与挂断后各发一次 |
| `heartbeat` | `elapsed_seconds` | 每 10 秒；客户端 30 秒没有任何帧视为断线 |
| `limit` | `reason` ∈ max_duration/daily_quota, `elapsed_seconds` | 到时：服务端先播固定短语 `limit_reached`，再发 `ended` |
| `error` | `code`, `message` | `message` 已是可直接显示的用户语言文案；随后关闭连接 |
| `ended` | `reason` (同 4.1 枚举), `duration_seconds`, `pending_turns`, `cost` | 通话结束的最终事件；之后服务端关闭 1000 |

服务端不下发实时转写：通话界面不显示对话文字，转写只在服务端用于 `VoiceTurn.transcript` 和调试日志（默认关）。

### 5.4 客户端 → 服务端 JSON 事件

| type | 字段 | 说明 |
| --- | --- | --- |
| `stop` | — | 挂断：服务端 `response.cancel`、等待最后用量最多 2 秒、发 `ended`、关闭 |
| `ping` | — | 可选；服务端回 `heartbeat` |

静音、扬声器切换、展开收起都是客户端本地行为，不上报。

### 5.5 关闭码与错误码

| WebSocket 关闭码 | 含义 | 客户端 `ended.reason` |
| --- | --- | --- |
| 1000 | 正常结束（`ended` 已发） | 按 `ended.reason` |
| 4001 | 票据无效/过期/受众不符 | `error`（文案 `voice:errors.connectFailed`） |
| 4003 | 无工作区权限 | `error` |
| 4009 | 该用户已有一通电话 | `concurrent` |
| 4029 | 当日配额已用完 | `quota` |
| 4404 | 主会话不存在 | `error`（客户端先 ensure 再重试一次） |
| 4400 | 帧格式错误 | `error` |
| 4503 | 语音未配置/未开通 | `error`（文案 `voice:errors.disabled`） |
| 1011 | 服务端异常（含百炼连接失败） | `error`（文案 `voice:errors.connectFailed`） |

`error.code` 枚举：`provider_unavailable`、`provider_error`、`assistant_unavailable`、`bad_frame`、`internal`。`message` 永不包含供应商原始错误、密钥、ID。

### 5.6 心跳与超时

- 服务端每 10 秒发 `heartbeat`；客户端 30 秒无帧 → 本地结束（`network`），P2 起改为尝试恢复。
- 服务端 30 秒收不到客户端音频帧 → 关闭 1011（客户端卡死或网络断）。
- 连接百炼：`open_timeout` 10 秒，失败重试 2 次（共 ≤30 秒）；期间客户端处于 `connecting`，超过 25 秒客户端自行结束（`error`）。
- 通话中百炼连接断开：P1 直接 `error` 结束；P2 服务端重连一次并用 `instructions` 带上本通电话已完成轮次的摘要续上。

## 6. 口播内容来源

| 内容 | 来源 | 谁念 |
| --- | --- | --- |
| 寒暄、澄清、应答（“好，我看一下”） | 前台模型自己 | 前台 |
| 后台回复 | 主会话这一轮的回复文本 → 清洗（去链接留标签、去列表/标题/代码标记、去 ID 样式串、超过 300 字截断到句末并加“详细的我写在对话里了”） → `function_call_output.speech` | 前台逐字朗读：`response.create` 的 `response.instructions` 要求“逐字朗读：我这边查到了，<speech>”（实测逐字一致） |
| 打招呼、还在办、结果在对话里、到时间了 | 固定短语文本（第 7 节表格） | 前台按 response 级指令原样说，同一个声音 |

前台提示词、后台语音轮次附加提示见 [后端文档 §9](VOICE_CALL_BACKEND.md)。

## 7. 文案（命名空间 `voice`）

网页 `frontend-v2/src/locales/{zh-CN,en-US}/voice.json` 与手机 `mobile/assets/locales/{zh-CN,en-US}/voice.json` **字节一致**（现有门禁 `mobile/scripts/check_locales.sh`），并在 `mobile/lib/shared/i18n/i18n.dart` 的 `_namespaces` 登记 `'voice'`。

zh-CN：

```json
{
  "button": { "start": "通话", "startLabel": "和个人助理通话", "inCall": "通话中" },
  "title": "个人助理",
  "state": {
    "requestingMic": "请允许使用麦克风",
    "connecting": "正在接通…",
    "greeting": "已接通",
    "listening": "我在听",
    "thinking": "想一下…",
    "speaking": "在说话",
    "working": "在办，稍等…",
    "late": "还在办…",
    "ending": "正在挂断…",
    "reconnecting": "正在重新连接…",
    "paused": "通话已暂停"
  },
  "controls": {
    "mute": "静音", "unmute": "取消静音", "speaker": "扬声器", "earpiece": "听筒",
    "hangUp": "挂断", "cancel": "取消", "collapse": "收起", "expand": "展开", "cost": "本次费用", "move": "拖动"
  },
  "banner": { "inCall": "通话中", "tapToReturn": "点按返回通话" },
  "hint": { "start": "直接说话就好，随时可以打断。" },
  "ended": {
    "title": "通话已结束",
    "hangup": "通话已结束",
    "error": "通话中断了",
    "limit": "这通电话到时间了，我们文字里继续。",
    "quota": "今天的通话时长用完了，明天再打吧。",
    "concurrent": "你在另一台设备上正在通话。",
    "micLost": "麦克风断开了，通话结束。",
    "network": "网络断开，通话结束。",
    "pendingHint": "还有 {{count}} 件事在办，结果会写在对话里。",
    "duration": "时长 {{duration}}",
    "cost": "费用约 ¥{{yuan}}"
  },
  "errors": {
    "unsupported": "当前浏览器不支持语音通话，请用最新版 Chrome 或 Edge。",
    "micDenied": "麦克风权限未开启，请在设置里允许后重试。",
    "micMissing": "没有找到麦克风。",
    "micBusy": "麦克风被其他应用占用。",
    "connectFailed": "没能接通，请稍后再试。",
    "disabled": "语音通话还没开通。",
    "backlog": "网络不稳定，通话已断开。",
    "assistantUnavailable": "先打开个人助理，再打电话。"
  },
  "actions": { "redial": "重新拨打", "close": "关闭", "viewInChat": "在对话里查看", "retry": "重试" },
  "cost": { "pending": "本轮金额待确认", "partial": "部分用量未返回，金额可能偏低", "settled": "已核算 {{count}} 轮",
    "items": { "inputText": "文字输入", "inputAudio": "语音输入", "outputText": "文字输出", "outputAudio": "语音输出" } },
  "duration": { "remaining": "剩余 {{minutes}} 分钟" },
  "permission": {
    "title": "需要使用麦克风",
    "body": "和个人助理通话需要麦克风。只在通话时收音，不保存录音。",
    "allow": "允许", "later": "以后再说", "openSettings": "去设置"
  },
  "interruption": { "phoneCall": "有来电，通话已暂停", "resumed": "通话已恢复", "background": "回到应用继续通话" }
}
```

en-US：

```json
{
  "button": { "start": "Call", "startLabel": "Call the personal assistant", "inCall": "In call" },
  "title": "Personal assistant",
  "state": {
    "requestingMic": "Allow the microphone",
    "connecting": "Connecting…",
    "greeting": "Connected",
    "listening": "Listening",
    "thinking": "Thinking…",
    "speaking": "Speaking",
    "working": "On it, one moment…",
    "late": "Still on it…",
    "ending": "Hanging up…",
    "reconnecting": "Reconnecting…",
    "paused": "Call paused"
  },
  "controls": {
    "mute": "Mute", "unmute": "Unmute", "speaker": "Speaker", "earpiece": "Earpiece",
    "hangUp": "Hang up", "cancel": "Cancel", "collapse": "Collapse", "expand": "Expand", "cost": "This call", "move": "Move"
  },
  "banner": { "inCall": "In call", "tapToReturn": "Tap to return to the call" },
  "hint": { "start": "Just talk. Interrupt any time." },
  "ended": {
    "title": "Call ended",
    "hangup": "Call ended",
    "error": "The call dropped",
    "limit": "This call has reached its time limit. Let's continue in text.",
    "quota": "Today's call time is used up. Try again tomorrow.",
    "concurrent": "You are already on a call on another device.",
    "micLost": "The microphone disconnected, so the call ended.",
    "network": "The network dropped, so the call ended.",
    "pendingHint": "{{count}} thing(s) still in progress; the results will appear in the conversation.",
    "duration": "Duration {{duration}}",
    "cost": "About ¥{{yuan}}"
  },
  "errors": {
    "unsupported": "This browser cannot make voice calls. Use the latest Chrome or Edge.",
    "micDenied": "Microphone access is off. Allow it in settings and try again.",
    "micMissing": "No microphone found.",
    "micBusy": "The microphone is in use by another app.",
    "connectFailed": "Could not connect. Try again later.",
    "disabled": "Voice calls are not enabled yet.",
    "backlog": "The network is unstable; the call ended.",
    "assistantUnavailable": "Open the personal assistant first, then call."
  },
  "actions": { "redial": "Call again", "close": "Close", "viewInChat": "View in conversation", "retry": "Retry" },
  "cost": { "pending": "This turn is not settled yet", "partial": "Some usage was not reported; the amount may be low", "settled": "{{count}} turn(s) settled",
    "items": { "inputText": "Text in", "inputAudio": "Audio in", "outputText": "Text out", "outputAudio": "Audio out" } },
  "duration": { "remaining": "{{minutes}} min left" },
  "permission": {
    "title": "Microphone needed",
    "body": "Calling the personal assistant needs the microphone. It listens only during the call and nothing is recorded.",
    "allow": "Allow", "later": "Not now", "openSettings": "Open settings"
  },
  "interruption": { "phoneCall": "Incoming phone call; the call is paused", "resumed": "Call resumed", "background": "Return to the app to continue the call" }
}
```

固定短语文本（服务端常量，不走 i18n，按用户语言选择；由前台模型按 response 级指令原样说出）：

| key | zh-CN | en-US |
| --- | --- | --- |
| `greeting` | 嗨，我在，你说。 | Hi, I'm here. Go ahead. |
| `still_working` | 还在办，好了我马上告诉你。 | Still on it. I'll tell you as soon as it's done. |
| `result_in_text` | 办好了，结果我写在对话里了。 | Done. I've put the result in the conversation. |
| `limit_reached` | 这通电话到时间了，我们文字里继续。 | This call has reached its time limit. Let's continue in text. |

## 8. 提示音与触感

| 时机 | 声音 | 手机触感 |
| --- | --- | --- |
| 开始接通 | 两声短“嘟”（440 Hz 80 ms ×2，间隔 120 ms，音量 0.2） | 无 |
| 接通成功 | 上行双音（660→880 Hz，各 90 ms） | `lightImpact` |
| 挂断/结束 | 下行双音（660→440 Hz，各 90 ms） | `lightImpact` |
| 错误结束 | 单音 330 Hz 200 ms | `mediumImpact` |

网页用 WebAudio 振荡器生成（无资源文件）；手机用 `assets/sounds/` 下三个 wav（由 ffmpeg 生成，命令见移动端文档）。提示音不经过通话播放队列，不被 `playback.clear` 影响。

## 9. 限额、费用、隐私

- 单次通话上限 `max_call_seconds`（默认 1800）；当日配额 `daily_seconds`（默认 3600，按 UTC 日计算，服务端在 `ready` 里给出本次可用秒数）。到点先播 `limit_reached` 再结束。
- 同一用户同时一通；第二处连接收到 4009。
- 费用按 `response.done.usage` 核算（价目见后端文档），存 `VoiceCall`；客户端只展示，不参与计算。固定短语与结果朗读都是前台模型的回复，已包含在用量里。
- 不保存音频；用户的话以主会话用户消息保存（仅 `assistant_ask` 轮次），前台自己答的寒暄不入库也不展示；通话界面没有任何文字记录。
- 日志不记录音频、转写全文、密钥；只记事件名、时长、token 数、错误码（QA 可打开 `debug_transcripts` 临时记录转写）。

## 10. 跨端验收清单（P1 网页、P2 手机各过一遍）

| # | 场景 | 通过标准 |
| --- | --- | --- |
| 1 | 首次点“通话” | 出现麦克风权限请求；允许后 ≤3 秒听到接通音 + 招呼语，状态“已接通”→“我在听” |
| 2 | 说“我有什么待办” | ≤1 秒内球体停止跟随、状态“想一下…”；先听到应答句；文字界面出现这句话；≤30 秒内听到“我这边查到了……”，内容与文字界面回复一致 |
| 3 | 后台超过 20 秒 | 听到“还在办，好了我马上告诉你”；期间问“今天几号”能正常对话 |
| 4 | 结果回来时正在说话 | 不插话；当前句说完后再播结果 |
| 5 | 它说话时插话 | ≤200 ms 停止播放，状态回到“我在听” |
| 6 | 问“今天天气怎么样” | 回答不知道/没法查，不编造 |
| 7 | 静音 | 球体不再跟随，它不再响应；取消后恢复 |
| 8 | 收起 | PC 变药丸/手机变顶部通话条；切换页面通话继续；点回来展开 |
| 9 | 挂断 | ≤1 秒显示时长与费用；若有未完成轮次显示“还有 n 件事在办” |
| 10 | 刷新/杀进程 | 通话结束；后台未完成轮次的结果仍出现在文字对话 |
| 11 | 另一设备再拨 | 提示“你在另一台设备上正在通话” |
| 12 | 到时 | 听到“这通电话到时间了”，通话结束，文案 `ended.limit` |
| 13 | 断网 | 30 秒内提示“网络断开，通话结束”，可重拨 |
| 14 | 费用 | `VoiceCall` 行的金额与界面一致；账本独立于主助理 |

## 11. 分期

- **P1**：后端 + 网页悬浮窗，QA 环境可打电话；验收 1–14（网页）。
- **P2**：手机端（iOS 优先，Android 跟进）；验收 1–14（手机）+ 来电中断、锁屏、耳机切换、断线恢复。
- **P3**：后台任务结果主动播报（通话中有新任务结果时在空闲窗口播）、拖动悬浮窗位置记忆、通话记录回看、费用进计费中心。逐字口播已由 response 级指令在 P1 实现。
