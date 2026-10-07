# 语音通话后端实现

状态：**实施文档**。日期：2026-10-07。契约见 [总规格](VOICE_CALL_SPEC.md)，依据见 [方案](PERSONAL_ASSISTANT_VOICE_PLAN.md)。本文只讲后端怎么写：文件、配置、表、端点、桥接、提示词、测试、顺序。所有“现有”引用均已核对源码。

## 1. 目标与边界

- 新增一个 WebSocket 端点 `/ws/assistant/voice`，在服务端把客户端音频、百炼前台、现有个人助理三者接起来。
- 个人助理的代码只改两处：`inputs.accept_turn` 多一个入口标记；`projection.project_main_messages` 对语音轮次多加一个提示块。工具、记忆、任务、通知一律不动。
- 密钥只在后端；不保存音频；账本独立。

## 2. 模块与文件

| 文件 | 职责 | 大小目标 |
| --- | --- | --- |
| `backend/voice/__init__.py` | 空 | — |
| `backend/voice/config.py` | 读取 `VoiceConfig`、解析密钥与连接地址 | <120 行 |
| `backend/voice/events.py` | 服务端→客户端事件的构造函数（`ready()`, `phase()`, `turn()` …），保证字段名与总规格一致 | <120 行 |
| `backend/voice/provider.py` | 百炼 realtime 客户端：连接/重试、`session.update`、收发、把供应商事件翻译成内部 `ProviderEvent` | <300 行 |
| `backend/voice/bridge.py` | 桥接状态机：`idle/user_speaking/responding`、`pending_calls`、`deliveries`、注入规则、超时与固定短语触发 | <350 行 |
| `backend/voice/assistant_link.py` | `assistant_ask` 的后台侧：`accept_turn` → `wait_for_inbox_terminal` → 读回复 → 清洗 → 结果对象 | <200 行 |
| `backend/voice/speech_text.py` | 口播稿清洗（纯函数） | <120 行 |
| `backend/voice/prompt.py` | 前台 `instructions` 组装（含用户画像摘要、日期、最近轮次摘要）与后台语音轮次提示块文本 | <120 行 |
| `backend/voice/phrases.py` | 固定短语文本（zh/en）与 response 级指令构造：`phrase_instructions(key, lang)`、`delivery_instructions(speech, lang)`；不合成音频 | <100 行 |
| `backend/voice/meter.py` | 每轮用量核算（搬 `demos/realtime-voice/pricing.py`，价目换成 3.8 omni） | <130 行 |
| `backend/voice/calls.py` | `VoiceCall`/`VoiceTurn` 的写入、当日配额统计、并发锁 | <200 行 |
| `backend/api/voice.py` | WebSocket 端点：握手、泵任务、清理 | <250 行 |
| `backend/db/models/voice.py` | 两张表的 ORM | <80 行 |
| `backend/db/migrations/versions/<id>_voice_calls.py` | 迁移 | — |

`demos/realtime-voice/` 保留不动（独立体验页）；`meter.py` 从它复制后改价目，不要互相 import。

## 3. 配置

`backend/core/config.py` 新增（放在 `VideoTranscriptionConfig` 之后，挂到 `OpenBoxConfig.voice`）：

```python
class VoiceConfig(BaseModel):
    """Realtime voice calls with the personal assistant through a Bailian omni model."""
    enabled: bool = False
    model: str = "qwen3.8-omni-flash-realtime"
    voice: str = "Serena"     # 2026-10-07 实测 3.8 omni 可用：Serena/Tina/Maia；不可用：Cherry/Ethan/Chelsie。Serena 也是 qwen3-tts-flash 的音色
    endpoint: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"  # 正式环境换业务空间域名
    workspace_id: str = ""        # 业务空间 ID；非空时 endpoint 用 wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
    api_key: str = ""             # 空则回退 DASHSCOPE_API_KEY
    proxy: Literal["env", "none"] = "env"   # env：按 HTTPS_PROXY 等环境变量走代理，失败再直连
    vad_threshold: float = Field(default=0.5, ge=-1, le=1)
    silence_ms: int = Field(default=700, ge=200, le=6000)
    max_call_seconds: int = Field(default=1800, ge=60, le=7200)
    daily_seconds: int = Field(default=3600, ge=60)
    late_after_seconds: int = Field(default=20, ge=5)
    turn_timeout_seconds: int = Field(default=120, ge=30)
    connect_timeout_seconds: int = Field(default=5, ge=2, le=30)
    connect_attempts: int = Field(default=3, ge=1, le=5)
    debug_transcripts: bool = False   # 仅 QA：把每轮转写写进日志
```

环境变量覆盖在现有 `_apply_env_overrides` 里加一组 `VOICE_*`（`VOICE_ENABLED`、`VOICE_MODEL`、`VOICE_VOICE`、`VOICE_WORKSPACE_ID`、`VOICE_API_KEY`、`VOICE_MAX_CALL_SECONDS`、`VOICE_DAILY_SECONDS`），写法与 `MEMORY_*` 相同。`api_key` 为空时 `voice/config.py` 回退读取 `DASHSCOPE_API_KEY`（与 `memory/providers/common.py` 的回退顺序一致）。`enabled=false` 时端点直接关闭 4503，客户端隐藏入口（通过现有 `GET /api/agent/config` 增加字段 `voice_enabled`）。

## 4. 数据表与迁移

```python
class VoiceCall(Base):
    __tablename__ = "voice_calls"
    id: str (64, pk)                     # generate_id()
    user_id: FK users.id
    workspace_id: FK workspaces.id
    main_session_id: FK sessions.id
    client: String(16)                   # web / ios / android
    model: String(64); voice: String(32)
    status: String(16)                   # active / ended / failed / limit
    end_reason: String(24) | None        # 与客户端 ended.reason 同枚举
    started_at, ended_at (tz-aware)      # 迁移里用 DateTime(timezone=True)，见 pbb1c2d3e4f5 的教训
    duration_seconds: Integer default 0
    turns: Integer default 0             # assistant_ask 次数
    usage: JSONType                      # {"input_text":..,"input_audio":..,"output_text":..,"output_audio":..}
    estimated_yuan: String(16)           # Decimal 字符串，与 meter 一致
    unreported_rounds: Integer default 0
    price_date: String(10)
    __table_args__ = (Index("ix_voice_calls_owner", "user_id", "workspace_id", "started_at"),
                      Index("ix_voice_calls_active", "user_id", "status"))

class VoiceTurn(Base):
    __tablename__ = "voice_turns"
    id: str (64, pk)
    call_id: FK voice_calls.id
    user_id: FK users.id
    provider_call_id: String(64)         # 百炼 call_id，唯一（call_id 内）
    inbox_id: String(64) | None          # 主会话 Inbox 项
    message_id: String(64) | None        # 用户消息
    result_message_id: String(64) | None
    transcript: Text                     # 用户原话（已入主会话，这里仅用于通话记录回看）
    requested_at, settled_at, delivered_at (tz-aware, 可空)
    outcome: String(16)                  # pending / delivered / late / timeout / failed / cancelled
    __table_args__ = (UniqueConstraint("call_id", "provider_call_id", name="uq_voice_turn_call"),
                      Index("ix_voice_turns_call", "call_id", "requested_at"))
```

迁移：`down_revision = "pbb1c2d3e4f5"`（当前 head，`alembic heads` 已核对）；SQLite 与 PostgreSQL 都要能跑（单测用 SQLite，QA/线上用 PG）。按 [分支合并记录](PERSONAL_ASSISTANT_DESIGN_V2.md) 的做法在 main 状态库副本上演练一次 upgrade/downgrade。

## 5. 票据改动

`backend/auth/routes.py` 的 `POST /ticket`：

```python
class TicketBody(BaseModel):
    audience: Literal["voice"] | None = None

@router.post("/ticket")
async def get_ticket(body: TicketBody | None = None, current_user=..., _workspace=...):
    ticket = await create_ticket(..., audience=body.audience if body else None)
```

现有 `create_ticket` 已有 `audience` 参数、`consume_ticket(ticket, audience=...)` 已校验受众：无受众票据不能连语音端点，语音票据不能连 `/ws/agent`。现有 `test_auth_api.py` 补两条断言。

## 6. WebSocket 端点 `backend/api/voice.py`

握手顺序（每一步失败的关闭码见总规格 5.5）：

1. `consume_ticket(ticket, audience="voice")` → 4001；`SocketAccess.from_ticket(...).check()` → 4003。单用户模式（`is_auth_enabled()` 为假）沿用 `/ws/agent` 的处理。
2. `config.voice.enabled` 且密钥非空 → 否则 4503。
3. `assistant.service.get_main_session(user_id, workspace_id)` → 空则 4404。
4. 并发锁：`cache.incr(f"voice:lock:{user_id}", ttl=60) == 1` 才继续，否则 4009；通话中每 20 秒 `cache.set(key, "1", ttl=60)` 续期；结束时 `delete`。
5. 配额：`calls.remaining_seconds_today(user_id)` ≤ 0 → 4029；`max_seconds = min(config.max_call_seconds, remaining)`。
6. 建 `VoiceCall(status=active)`；连接百炼（`provider.connect()`，含重试）；失败 → `error{provider_unavailable}` + 1011，`VoiceCall.status=failed`。
7. `websocket.accept()` 在第 1 步之后立即做（否则无法发自定义关闭码的原因），但 `ready` 只在第 6 步成功后发。
8. 发 `ready`；启动固定短语 `greeting`。

泵任务（`asyncio.wait(FIRST_COMPLETED)`，与现有 `agent_websocket` 一样统一取消和 `gather`）：

- `client_to_bridge`：收二进制帧 → 校验长度 → `bridge.feed_audio(bytes)`；收 `stop` → `bridge.stop()`；收 `ping` → `heartbeat`。30 秒无帧 → 1011。
- `provider_to_bridge`：`provider.events()` 异步迭代 → `bridge.on_provider_event(ev)`。
- `bridge_to_client`：`bridge.outbox` 队列 → `websocket.send_json/send_bytes`。队列上限 1000，满则关闭 1011（客户端卡住）。
- `timers`：每 10 秒 `heartbeat`、每 20 秒续锁、每秒检查时长上限与 `late_after`。
- `access.watch()`：现有 5 秒复核。

结束（任一泵退出或异常）：`bridge.close(reason)` → `provider.close()`（先 `response.cancel`，等最后 `response.done` ≤2 秒，取用量）→ `calls.finish(call, reason, meter.snapshot())` → 发 `ended` → 释放锁 → 关闭 1000。`finally` 里用 `anyio.CancelScope(shield=True)` 兜底，参考现有 `agent_websocket`。

## 7. 供应商适配器 `backend/voice/provider.py`

连接：`websockets.connect(url, additional_headers={"Authorization": f"Bearer {key}"}, open_timeout=connect_timeout, proxy=<见下>, max_size=8 MiB, ping_interval=20)`；URL 为 `endpoint?model=<model>`，`workspace_id` 非空时 endpoint 改为业务空间域名。2026-10-07 实测（各 5 次）：走环境代理（websockets 15 的 `proxy=True`）5/5 成功、0.14–0.24 秒；直连 4/5 成功、0.13–0.52 秒，1 次 12 秒握手超时。所以 `proxy="env"` 时第 1、3 次尝试走环境代理、第 2 次直连；`proxy="none"` 只直连；每次 `open_timeout=5` 秒，共 `connect_attempts` 次，间隔 0.5 秒，日志记每次尝试耗时与方式。

`session.update`（握手后立即发，等 `session.updated`）：

```json
{"type":"session.update","session":{
  "modalities":["text","audio"], "voice":"<voice>",
  "instructions":"<prompt.front_instructions(...)>",
  "input_audio_format":"pcm", "output_audio_format":"pcm",
  "turn_detection":{"type":"semantic_vad","threshold":<vad_threshold>,"silence_duration_ms":<silence_ms>},
  "tools":[{"type":"function","function":{"name":"assistant_ask",
    "description":"把用户的请求原话交给个人助理处理，返回它的回答；speech 字段是要读给用户听的话。",
    "parameters":{"type":"object","properties":{"text":{"type":"string","description":"用户原话"}},"required":["text"]}}}]
}}
```

不开 `enable_search`（与 tools 互斥）。音色从配置读（默认 `Serena`）；不支持的音色要到第一次生成才报错（`Voice 'X' is not supported`），所以接通后的招呼语就是音色校验：报这个错时记错误日志并按 `provider_error` 结束。

事件翻译（供应商 → 内部 `ProviderEvent(kind, **fields)`）：

| 供应商事件 | 内部事件 |
| --- | --- |
| `input_audio_buffer.speech_started` | `user_started` |
| `input_audio_buffer.speech_stopped` | `user_stopped(invalid=reason=="turn_invalid")` |
| `conversation.item.input_audio_transcription.completed` | `user_transcript(text)`（只写 `VoiceTurn.transcript` 与调试日志，不转发客户端；delta 忽略） |
| `response.created` | `response_started(response_id)` |
| `response.audio.delta` | `audio(response_id, bytes, event_id)` |
| `response.audio_transcript.done` | `assistant_transcript(response_id, text)`（仅调试日志，不转发客户端；delta 忽略） |
| `response.function_call_arguments.done` | `tool_call(call_id, name, arguments)` |
| `response.done` | `response_done(response_id, status, usage)` |
| `error` | `provider_error(code)`（原文只进日志，不出事件） |

发送：`send_audio(bytes)`（base64 → `input_audio_buffer.append`）、`send_tool_output(call_id, payload: dict)`、`create_response(instructions: str | None = None)`（有指令时发 `{"type":"response.create","response":{"instructions":...}}`；不带输入、历史时必须带指令，否则供应商报 `Cannot create response without input, history, or instructions`）、`cancel_response()`。

## 8. 桥接器 `backend/voice/bridge.py`

```python
class Bridge:
    state: Literal["idle", "user_speaking", "responding"]
    active_response: str | None
    interrupted: bool            # 用户开口后，旧回复的剩余音频一律丢弃（demo 逻辑）
    pending_calls: dict[str, VoiceTurnRef]
    deliveries: deque[VoiceTurnRef]
    response_kind: str | None    # 当前回复的来源："model"（VAD 触发）/ "phrase:<key>" / "delivery:<turn_id>"
    requested: str | None        # 已发出 response.create、尚未收到 response.created 的种类
    outbox: asyncio.Queue        # 发给客户端的 JSON/bytes
```

事件处理（只写关键规则，其余按总规格 4.2）：

- `user_started`：`state=user_speaking`，`interrupted=True`，发 `playback.clear`、`phase(listening)`；若有固定短语在播，标记结束。
- `user_stopped`：`state=idle`；`invalid` 时发 `phase(listening)`，否则 `phase(thinking)`。
- `response_started`：`state=responding`，`interrupted=False`，`active_response=id`；`response_kind = requested or "model"`，`requested=None`；`phrase:*` 时发 `phrase{key}`，`delivery:*` 时队首标记 `delivering`。有音频到达时发 `phase(speaking)`。
- `audio`：`meter.audio(...)`；`interrupted` 或 `response_id != active_response` → 丢弃；否则二进制下发。
- `tool_call(name=="assistant_ask")`：`turn = assistant_link.start(text)`（见 §9），`pending_calls[call_id]=turn`，发 `turn(accepted)`、`phase(..., working=True)`；`VoiceCall.turns += 1`。未知工具名 → 回传 `{"status":"unknown_tool"}`。
- `assistant_link` 完成（通过 `asyncio.create_task` 回调）：`provider.send_tool_output(call_id, {"status": "ok", "speech": ...})` 立即发；`deliveries.append(turn)`；`try_deliver()`。
- `response_done`：`meter.settle(...)`，发 `cost`；`state=idle`，若 delivering 的 turn 对应此响应 → `turn(delivered)`；然后 `try_deliver()`、`maybe_still_working()`、`maybe_phase_listening()`。
- `try_deliver()`：`state=="idle" and requested is None and deliveries` → `provider.create_response(phrases.delivery_instructions(speech, lang))`，`requested=f"delivery:{turn_id}"`，队首状态 `creating`。若供应商回 `error` 且文案含 `active response` → `requested=None`、回到 `queued`，等下一个 `response_done` 再试；其它错误 → 固定短语 `result_in_text`，`turn(outcome=failed)`。
- `maybe_still_working()`：某 pending turn 已超过 `late_after` 且未提示过、`state=="idle"` 且 `requested is None` → `say_phrase("still_working")`，发 `phase(working, late=True)`。
- `say_phrase(key)`：`provider.create_response(phrases.phrase_instructions(key, lang))`，`requested=f"phrase:{key}"`。接通后立即 `say_phrase("greeting")`；到时 `say_phrase("limit_reached")`，其 `response_done`（或 5 秒兜底）后结束通话。
- 超时：`assistant_link` 自带 `turn_timeout`；超时时回传 `{"status":"timeout","speech":...}`，`turn(timeout)`，照常走 deliveries（让前台把这句话说出来）。
- `stop()`：若 `state=="responding"` → `cancel_response()`；等待 `response_done` ≤2 秒；`pending_turns=len(pending_calls)`；交给端点发 `ended`。

固定短语与结果朗读都是普通的前台回复：音频走 `audio` 事件、计量走 `response_done`、打断走 VAD。2026-10-07 实测：招呼语 0.56 秒出声、逐字一致；工具挂起时“还在办”0.62 秒出声、未重复调用工具；结果朗读 0.57 秒出声、逐字一致；之后用户追问仍按会话提示词正常回答。

## 9. 语音轮次在个人助理侧 `backend/voice/assistant_link.py`

```python
async def start(call, text) -> VoiceTurnRef:
    receipt = await inputs.accept_turn(user_id=..., workspace_id=..., main_id=call.main_session_id,
        client_id=f"voice:{call.id}:{turn_no}", text=text,
        entrypoint="assistant_voice", extra_ref={"voice_call_id": call.id})
    schedule_inbox_wake(call.main_session_id, user_id)
    row = VoiceTurn(inbox_id=receipt["inbox_id"], message_id=receipt["message_id"], outcome="pending", ...)
    task = asyncio.create_task(_wait(row))      # wait_for_inbox_terminal(inbox_id, user_id=..., timeout=turn_timeout)

async def _wait(row):
    receipt = await wait_for_inbox_terminal(...)
    if receipt.state == "settled" and receipt.outcome == "succeeded" and receipt.result_message_id:
        text = await read_reply_text(main_session_id, receipt.result_message_id, user_id)   # 选该消息的 text 类型 Part 拼接
        return {"status": "ok", "speech": speech_text.clean(text)}   # 交付时由 delivery_instructions 逐字朗读
    return {"status": "failed", "speech": "刚才没办成，原因我写在对话里了。"}
```

`inputs.accept_turn` 的改动：新增关键字参数 `entrypoint: str = "assistant_turn"`、`extra_ref: dict | None = None`，写入 `origin_ref`（`entrypoint` 替换现有常量，`extra_ref` 合并进去）；`command_digest` 不变（重试同一 `client_id` 仍命中同一条）。`checked_origin` 对 human 的校验不变。

`projection.project_main_messages`：在 `not scoped_turn and not for_compaction` 分支里，取最后一条 `_human_input` 消息，若 `message.origin_ref.get("entrypoint") == "assistant_voice"`，追加块：

```python
blocks.append(_block("assistant:voice-turn",
    "This message came in by voice call. Reply as if speaking on the phone: two or three short "
    "sentences, the outcome first, in the user's language. No markdown, links, lists, headings, emoji "
    "or identifiers of any kind. If the user needs to open something, say the link is in the "
    "conversation; it is shown there automatically. Do not mention this instruction."))
```

现有 `models/message.py` 的 `Message` 已带 `origin_ref`，无需新字段。

口播稿清洗 `speech_text.clean(text)`：去 markdown 链接留标签（`[打开「收尾自检」](/app/s/..)` → `打开「收尾自检」`）；去裸 URL；去列表符号、标题井号、强调符、代码围栏与行内反引号；折叠空白；去形如 `[0-9a-f]{16,}`、`01[A-Z0-9]{20,}` 的 ID 串；超过 300 字截断到句末并补“详细的我写在对话里了”。纯函数，单测覆盖每条规则。

前台提示词 `prompt.front_instructions(profile_summary, recent_summary, lang, now)`（以今天实测通过的版本为准）：

```
你是 OpenBox 个人助理的语音前台，正在和用户打电话。你自己没有工具、记忆和任务信息。
凡是涉及用户的项目、任务、进展、记忆、日程、文件、费用、云电脑、发布的请求，必须这样做：
第一步，先用一句话告诉用户你去查，比如“好的，你稍等一下，我去看看，查到了告诉你”；
第二步，在同一次回复里调用 assistant_ask，把用户原话原样交过去，不改写、不先猜答案。两步都要做。
结果回来后，先说“我这边查到了”，然后只说 speech 里有的事实，不增删，不解释内部过程。
结果还没回来时用户又问起，就说还在办。
寒暄、重复、澄清可以自己回答；天气、新闻、价格这类需要事实的问题你不知道，就直说不知道。
用户说“停”“别说了”只是让你停下，不用回应；要停止一件事要明确说出来，由个人助理处理。
用用户的语言，像人说话，一到两句话，不念链接、ID、编号。
今天是 {now}。关于用户：{profile_summary}。最近聊过：{recent_summary}。
```

`profile_summary` 取现有 `memory_context` 里的画像部分（`memory/orchestrator.py` 的渲染结果中“用户画像”段，限 300 字），`recent_summary` 取主会话最近 3 条助理回复各一句（`history.read_history` 取最新页，各截 60 字）。都没有时留空，不编造。

## 10. 固定短语 `backend/voice/phrases.py`

不合成音频：固定短语由前台模型按 response 级指令原样说出（与通话同一声音，实测 0.6 秒出声）。

```python
PHRASES = {
    "greeting":       {"zh": "嗨，我在，你说。", "en": "Hi, I'm here. Go ahead."},
    "still_working":  {"zh": "还在办，好了我马上告诉你。", "en": "Still on it. I'll tell you as soon as it's done."},
    "result_in_text": {"zh": "办好了，结果我写在对话里了。", "en": "Done. I've put the result in the conversation."},
    "limit_reached":  {"zh": "这通电话到时间了，我们文字里继续。", "en": "This call has reached its time limit. Let's continue in text."},
}

def phrase_instructions(key, lang) -> str:
    # zh: "只说这一句，不要调用任何工具，不要加别的话：{text}"
def delivery_instructions(speech, lang) -> str:
    # zh: "个人助理的结果回来了。请逐字朗读下面这段话，一个字都不要增减或改写，不要调用工具：我这边查到了，{speech}"
```

语言：用户语言取主会话所属用户的界面语言偏好（现有 `/api/auth/me/preferences` 的存储；没有则 zh）。

## 11. 计量 `backend/voice/meter.py`

价目（北京，2026-10-07 核对，元/百万 token）：`input_text 1.5`、`input_audio 6`、`output_text 4.5`、`output_audio 12`；`PRICE_DATE="2026-10-07"`。其余逻辑与 demo `CallMeter` 相同：按 `response.done.usage` 的四类 token 核算，被打断未返回用量的回复按已生成音频时长估算（12.5 token/秒，实测 108 token / 8.6 秒 ≈ 12.6）。结束时写 `VoiceCall.usage/estimated_yuan/unreported_rounds`。

## 12. 日志与可观测

- 每通电话一行结构化日志：call_id、user_id、client、duration、turns、tokens、yuan、end_reason、provider 连接尝试次数与耗时。
- 每个语音轮次一行：turn_id、inbox_id、accept→settle 秒数、settle→delivered 秒数、outcome。
- 不记录音频、转写全文、密钥、供应商原始错误正文（只记 code）。`debug_transcripts=true`（默认关，仅 QA）时记录每轮转写文本，用于抽查前台有没有编造。

## 13. 测试

| 文件 | 用例 |
| --- | --- |
| `tests/unit/test_voice_speech_text.py` | 链接留标签、裸 URL、列表/标题/代码、ID 串、截断、空输入 |
| `tests/unit/test_voice_bridge.py` | 假 provider（事件脚本）：工具调用→accepted；结果到达且 idle→create_response 一次；responding 时不发；`active response` 错误→重排队；重复 call_id 不再回传；20 秒→still_working 一次；超时→timeout 回传；用户开口→playback.clear 且丢弃旧音频；stop→cancel 并等待用量 |
| `tests/unit/test_voice_meter.py` | 从 demo 的 pricing 单测迁移，改价目 |
| `tests/unit/test_voice_phrases.py` | 四个短语 zh/en 指令文本；结果朗读指令包含完整 speech 且以“我这边查到了”开头；未知 key 报错 |
| `tests/unit/test_voice_assistant_link.py` | `accept_turn` 的 `origin_ref.entrypoint=="assistant_voice"`；回复文本读取；失败/超时映射 |
| `tests/unit/test_assistant_projection_voice.py` | 语音轮次追加 `assistant:voice-turn` 块；键盘轮次不追加；汇报轮不追加 |
| `tests/unit/test_voice_calls.py` | 并发锁、当日配额、`finish` 写入、SQLite 可跑 |
| `tests/integration/test_voice_ws.py` | 票据受众不符 4001；未开通 4503；主会话不存在 4404；第二连接 4009；坏帧 4400；完整握手收到 `ready`、`phrase(greeting)`；`stop` 后收到 `ended`（provider 用假实现） |
| `tests/unit/test_auth_ticket_audience.py` | 带受众票据不能连 `/ws/agent`，无受众票据不能连语音 |
| PG 一次性库 | 迁移 upgrade/downgrade；`VoiceCall` 写入与查询 |
| 手动（带 key，默认跳过） | `tests/manual/voice_live_check.py`：今天的三个实验脚本整理版 |

## 14. 任务顺序

1. 配置与 `voice_enabled` 暴露；票据受众。
2. 表与迁移；`calls.py`。
3. `speech_text.py` + 单测。
4. `provider.py`（先用录好的事件脚本做假实现）。
5. `assistant_link.py` + `accept_turn` 改动 + `projection` 块 + 单测。
6. `bridge.py` + 单测（这是核心，先把 13 条用例写出来再实现）。
7. `phrases.py`（指令构造）、`meter.py`。
8. `api/voice.py` + 集成测试。
9. 真连 QA：用今天的实验脚本改成客户端，跑总规格 §10 的 1–6、9、11、12、14。
10. 文档回填：`docs/MOBILE_WEB_PARITY.md` 加“语音通话”行、方案文档 §7 验收记录。

## 15. 实现与本文的差异（2026-10-07 实施记录）

代码以实现为准，差异如下（均已有测试）：

- **握手**：先 `accept` 再校验票据，浏览器才能拿到 4001/4003 等关闭码（否则只有 1006），与 `/ws/admin/trajectories` 一致。
- **通话中出错**：先发 `error`，再发最终 `cost` 与 `ended{reason}`，然后以 4400 或 1011 关闭，客户端仍能拿到时长、费用和未完成轮次；握手阶段的拒绝只发关闭码。`assistant_unavailable` 不发送，主会话缺失只用 4404 表示，客户端据此 ensure 后重试。
- **`turn` 事件**：`accepted` 时消息还未生成，`message_id` 为空；助理认领这条输入后发 `turn{state:"working", message_id}`，客户端据此滚动到该消息。
- **连接**：环境代理与直连两条路线竞速（代理先、直连 0.4 秒后跟上，先建立会话者胜，其余关闭），单次超时 5 秒；见 `voice/provider.py` 与总规格 §5.6。
- **桥接器补充规则**：有效说完后 3 秒内不注入结果（给供应商 VAD 自动回复让路）；用户说话时创建的回复会被取消、未被听到的交付最多重试 3 次；我们的 `response.create` 与 VAD 回复同时发生时，该回复归为模型自答、我们的请求重新排队；`phase.working` 保持到结果读完；到时后麦克风改为静音帧、取消 VAD 回复、在下一个空闲窗口说 `limit_reached`。
- **提示词**：语音轮次块的措辞为 “The user's latest message came in by voice call…”；前台提示词增加“你是 AI 助理：寒暄时简短友好，不说自己累了、饿了、困了这类身体感受”；画像或最近轮次为空时整句省略；英文界面多一句“先用英文交谈”；最近轮次直接查询最后 3 条已完成回复；上下文读取在连接供应商的同时进行，上限 1 秒。
- **超时与失败**：读出时不加“我这边查到了”；交给助理失败有单独的话术“我这边没能交给个人助理……”。
- **其它**：`VoiceCall.client` 为 web/mobile（票据里只有这两种）；并发锁续期写整数 1（RedisCache 会 JSON 编码，字符串会让竞争方的 INCR 失败）；通话时长从 `ready` 计到挂断请求，不含等待最终用量的 2 秒。
- **QA**：开启方式为 QA 后端环境变量 `VOICE_ENABLED=true`（可加 `VOICE_DEBUG_TRANSCRIPTS=true` 记录转写），通过 `.local-dev/assistant-web-20261003/private_browser_qa_runtime.py restart <证据名>.json` 重启；手动真连检查 `backend/tests/manual/voice_live_check.py`。

## 16. 第二轮：前台自然说话（2026-10-07，修复计划 §5）

客户端协议不变（`phrase.key` 以 `progress` 取代 `still_working`，客户端只记录）。代码：

| 计划 | 实现 |
| --- | --- |
| §5.1 立即回执 + 备注交付 | `voice/turns.py`：`assistant_ask` 立即回传 `{"status":"accepted"}`；结果在空闲窗口作为备注注入（`provider.create_note`，user 角色 `input_text`，“（后台备注，不是用户说的话）关于用户说的“…”：…”），同一步紧接着请求转述（`phrases.delivery_instructions`：自己的话、三句以内、名字数字状态选项不变、金额与选项原文、不用“我这边查到了”）。供应商不接受客户端指定的 item id，按文本匹配 `conversation.item.created`。抢在我们之前、在备注确认之后 1.5 秒内开始的 VAD 回复算作已交付（按事件顺序判断）；没人听到的交付删掉备注、下次空闲重新注入，最多 3 次。超时与失败同样是备注 + 通知。 |
| §5.2 真实进度 | `voice/progress.py` 订阅主会话的 `tool.running`/`message.text_delta`，工具 id 译成短语；`instructions` 的“当前后台进度”段（含“排队 N 件”）在空闲时用只含 `instructions` 的 `session.update` 更新（≤每 2 秒一次）；安静 ≥ `late_after_seconds` 且有事在办时请前台说一句（每件事最多 3 次、间隔 ≥12 秒），发 `phrase{progress}` 与 `turn{late}`。 |
| §5.3 上下文维护 | `voice/transcript.py` 服务端转写与供应商 item 顺序；`voice/upkeep.py` 每 10 轮或单次输入 >6 万 token 用 `voice.summary_model`（默认 `openai/qwen3.8-flash`，走记忆抽取同样的提供方配置）合成 ≤300 字备忘写进“本通电话到目前为止”，按创建顺序删除早期 item（保留最近 8 个和未交付的备注），已交付备注 3 个回复后删除；25 分钟或 12 万 token 时在安静时刻静默换新会话（备忘 + 最后几句 + 进度），只有打开新会话的瞬间暂停我们的回复。挂断后摘要写入 `voice_calls.summary`（迁移 `pbd3e4f5a6b7`），`calls.latest_call_summary()` 供下次开场与文字助理读取；不往主会话写消息。 |
| §5.4 提示词与开场 | `voice/prompt.py::FRONT`；开场事实：时间、画像（称呼）、24 小时内上次通话摘要、上次通话后完成的关注任务；`phrases.greeting_instructions` 只给目标。 |
| §5.5 直接查询 | `voice/tools.py` 注册表 `DIRECT`（`tasks_overview`、`memory_search`、`schedules_list`、`projects_list`、`credits`），每次 ≤2 秒，超时或出错回 `{"status":"unavailable"}`，输出后在空闲窗口发不带指令的 `response.create`。新增工具只需一条 `DirectTool`。 |
| §5.6 快速档 | `VoiceConfig.turn_model/turn_variant`（`VOICE_TURN_MODEL`/`VOICE_TURN_VARIANT`，默认空 = 主会话的）。 |
| 半句 | 提示词 + `tools.is_fragment`：触发这句的原话去掉标点和语气词后不足 4 字、不含动作也不是回答（“好的/确认/可以/嗯”）时回 `{"status":"need_more"}`，不交办。 |
| §5.7 回放 | `backend/tests/manual/voice_replay.py`（默认跳过会真正办事的句子，`--with-actions` 才播）。 |

实测（2026-10-07，`tests/manual/voice_live_check.py`）：开场“晚上好，Andrew。上次聊到贪吃蛇项目的 UI 优化……”；交办后立即回执；进度句说出真实步骤（“正在查看……相关对话记录”）；结果转述一次、事实与选项不变；“我有哪些任务在进行”直接查询 1.7 秒出答案。


## 17. 读卡确认、说了不做的兜底、音色选择（2026-10-07，修复计划 §1.3、§9）

| 项 | 实现 |
| --- | --- |
| 卡片随结果回来 | `AssistantLink.wait` 结算成功后读 `assistant.confirmations.pending_cards()`；有卡片时备注用 `phrases.card_note`（编号、要做什么、影响、选项原文），交付用 `phrases.card_instructions`（念清楚、问确认、这一句不调用工具）。 |
| `cards_pending` / `cards_answer` | `voice/cards.py`，注册在 `tools.DIRECT`。`CardDesk` 给卡片发本通电话内的短编号；`answer` 校验编号、仍待答、选项原文、卡片念出后用户开过口（等转写 ≤2.5 s）、非拒绝选项须明确同意且在（重新）念出后两句话以内；作答调 `question.question.reply`。 |
| 续跑 | 作答前记下主会话最新 Inbox id，`AssistantLink.follow` 找 `origin=system_recovery`、`entrypoint=question_answer` 的新条目，按普通轮次等结果并转述；拒绝不跟。续跑沿用语音轮次模型（`question/continuation.py::_voice_turn`，Inbox 条目带 `voice: true`）。 |
| 服务端拒绝 | 卡片念出后用户第一句是明确拒绝（`cards.refuses`）→ `cards.decline` 选“取消/不用记”。 |
| 短答拦截 | 有新念的卡片时 `assistant_ask` 收到“确认/好的/算了”这类短答 → 回 `{"status":"answer_card","card":…}`，不进主会话。 |
| 说了不做 | VAD 回复说了承诺（`turns.PROMISE`：这就去、我去办、帮你删…）却没有任何工具调用，且用户原话是办事（`tools.asks_for_work`）→ 以 `provider_call_id="promise:<response_id>"` 开一个普通轮次。 |
| 模型不粘 | `agent/inbox.py`：`assistant_voice` 轮次及带 `voice` 的续跑，认领时不改写会话的 `model/variant`。 |
| 进度 | 同一步骤 30 s 内不重复（`SAME_STEP_GAP_SECONDS`），新步骤 12 s；指令要求平实、不加情绪。 |
| 音色 | `voice/voices.py`（15 个，均在 qwen3.8-omni-flash-realtime 实测可用）；`GET /api/assistant/voice/voices`、`PUT /api/assistant/voice/voice`（存 `preferences.extra.assistant_voice`，只收列表内 id，否则 422 `VOICE_UNKNOWN`）、`GET /api/assistant/voice/samples/{id}`（公开，约 4 s AAC）。`_call` 开始时 `voices.resolve(选择, config.voice)`，写入 `VoiceCall.voice`。 |
| 文字助理上下文 | `assistant/projection.py::_recent_call`：24 小时内最近一次通话摘要作为背景块（不授予操作权限，压缩时不带）。 |

测试：`tests/unit/test_voice_cards.py`、`test_voice_voices.py`，`tests/integration/test_voice_ws.py`（选择音色后下一通电话用它、试听只给列表内的声音），`test_assistant_sessions_v2.py`（续跑模型），`test_assistant_projection_voice.py`（摘要块、模型不粘），`test_assistant_project_tools.py`（项目名须出自原话）。端到端脚本 `.local-dev/voice-qa/voice_confirm_e2e.py`（建临时项目 → 语音删除，先拒绝一次再确认；只在卡片名字与临时项目完全一致时才说确认）。
