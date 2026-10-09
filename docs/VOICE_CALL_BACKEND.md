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
    voice: str = "Tina"       # 2026-10-09 对齐阿里官方 Qwen3.8-Omni-Flash-Realtime 默认音色「甜甜」
    endpoint: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"  # 正式环境换业务空间域名
    workspace_id: str = ""        # 业务空间 ID；非空时 endpoint 用 wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
    api_key: str = ""             # 空则回退 DASHSCOPE_API_KEY
    proxy: Literal["env", "none"] = "env"   # env：按 HTTPS_PROXY 等环境变量走代理，失败再直连
    vad_threshold: float = Field(default=0.5, ge=-1, le=1)
    silence_ms: int = Field(default=700, ge=200, le=6000)
    max_call_seconds: int = Field(default=1800, ge=60, le=7200)
    # （2026-10-08 起删除 daily_seconds：通话走积分，见 §18）
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
5. 积分（2026-10-08 起取代当日配额）：`billing.media.voice_credit_room(workspace_id)`，enforce 下余额 ≤ 0 → 4029，否则返回可花的积分；`max_seconds = config.max_call_seconds`。
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

（初版。现行：`assistant_ask` 立即回执、结果作为备注送达（§16），参数为能单独看懂的 `request` 并在转交前整理（§20），另有前台直接读的工具；提示词见 `voice/prompt.py::FRONT`。）

不开 `enable_search`（与 tools 互斥）。音色从配置读（默认 `Tina`，见[阿里官方音色列表](https://help.aliyun.com/zh/model-studio/omni-voice-list)）；不支持的音色要到第一次生成才报错（`Voice 'X' is not supported`），所以接通后的招呼语就是音色校验：报这个错时记错误日志并按 `provider_error` 结束。

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


## 18. 接通即开口、回铃音、积分计费（2026-10-08）

| 项 | 实现 |
| --- | --- |
| 先生成招呼语再接听 | `api/voice.py::_call`：provider/客户端 pump、权限与失败监视先启动，`bridge.start()` 请求招呼语；等 `bridge.greeted`（招呼语完成、被拒或失败，最多 `GREETING_WAIT_SECONDS = 8`）后才发 `ready` 并启动 `bridge_to_client`，攒在 outbox 里的招呼语事件和音频随即一次发出。通话时长从 `ready` 起算；接听前挂断时长为 0。 |
| 招呼语不被打断 | `Bridge.answered()` 记接听时刻；`feed_audio` 在招呼语生成中或其音频播完前（`greeting_bytes / 48000 + 0.3 s`）把上行换成静音：外放回声、抢话不会触发 VAD 打断。被拒或无音频时不拦。招呼语提示改为一句、三十字以内。 |
| 回铃音 | 客户端从 ws 打开到 `ready` 循环 450 Hz 1 s 响 / 4 s 停，`ready` 即停，不再播“已接通”提示音（网页 `audio/tones.ts::startRingback`，手机 `PcmPlayer.startRinging` + `assets/sounds/call_ringback.wav`）。 |
| 积分代替时长配额 | 删除 `VoiceConfig.daily_seconds` 与 `calls.remaining_seconds_today`。价格挪到 `billing/rates.json` 的 `media.voice-realtime`（`voice.meter.call_prices` 读取，通话里显示的费用与结算一致）。拨号前 `voice_credit_room`（enforce 下余额 ≤ 0 → 4029）；通话中 `timers` 每秒比较 `meter` 累计与可花积分，到额 `begin_limit("credits")`，说 `credits_exhausted` 后 `CallEnded("quota")`。`_end` 在 `finish_call` 后 `settle_voice_call`：每通一条 `usage_events`（`voice:{call_id}` 幂等，kind `voice_call`，标题“语音通话”，tokens 含四类用量与时长），enforce 记账扣积分。 |
| 记录不丢 | 结果播报后立刻挂断会取消 provider pump；`turns.py` 用 `asyncio.shield` 保证轮次的送达记录写完。 |

测试：`tests/unit/test_voice_bridge.py`（招呼语前置与静音保护的时间）、`tests/integration/test_voice_ws.py`（接听时招呼语已生成、回声不打断、积分不足 4029、通话中积分用完说再见并记账）、`tests/unit/test_billing_media.py`（按模态定价、每通一条、enforce 扣账、shadow 不封顶）。实测：拨号到接听约 1.8–2.0 s（期间回铃），接听后 7–63 ms 招呼语开始；一通 149 s 的测速通话记为 0.0778 积分（shadow）。

## 19. 任务汇报进通话、多条结果合并说、口语化（2026-10-08）

| 项 | 实现 |
| --- | --- |
| 任务汇报主动播报 | 交给任务的事，语音轮次在助理“交出去”时就结束了；任务自己的结果之后以主会话的汇报轮次（`origin=task_result`，assistant/results.py）回来，以前只写进对话。`voice/reports.py::ReportWatcher` 在接听前记下已结算的汇报，通话中每 2 s（`timers`）查主会话最近 30 条 `task_result` 收件项，新结算且成功的取助理汇报原文与任务标题，交给 `Bridge.report()`：作为备注“个人助理主动汇报，任务「X」有新结果：…”排进播报队列，空闲时主动说（`phrases.together_instructions`：先一句自然过渡“对了……”，再一两句结果与要用户做的事）。不记 VoiceTurn。 |
| 多条结果合并说 | `turns._deliver` 把同时排队的普通结果（不含卡片、失败）最多 3 条合成一条编号备注、一次回复（`joined_note` + `together_instructions`：一件一件说，“另外”“还有”衔接，先说要用户处理的）；没听到则整组重排，听到后逐条记账。`Bridge._next_delivery` 先说用户自己问的，再捎带主动汇报。 |
| 口语化 | 会话参数 `smooth_output: true`（官方 Qwen-Omni-Realtime 口语化开关，3.8 实测接受）。前台提示词改为“跟了老板很久的助理打电话”的口吻：口语短句、自然口头语、不用书面腔/客服腔和“结论是”“第一”这类汇报格式、长名字说顺口、用户不耐烦先接一句。转述指令改为“别念备注，用自己的话说”（实测旧指令“先说结论”会被念成“结论：……”）；书名号名字只在需要用户选选项时才要求原文（“确认”二字不再触发）。进度句改为“像请对方稍等”的一句口语，状态类工具各有自己的说法（不再是“正在查账户状态”）。 |
| 查结果别凭印象 | 提示词要求用户问任务结果时先 `tasks_overview`；其 `latest` 由一句放宽为前两句（≤160 字）。 |

测试：`tests/unit/test_voice_reports.py`、`test_voice_turns.py`（主动汇报带过渡、两条合并一次说、没听到整组重排）、`test_voice_phrases.py`、`tests/integration/test_voice_ws.py`（通话中真实任务结果的汇报轮次结算后被主动说出）。

## 20. 记忆什么时候查、谁来处理、交给助理前先整理（2026-10-08）

三条通用规则，替代“前台模型自己想起来就查、拿不准就把原话交给助理”。依据：OpenAI 实时语音的 Chat-Supervisor 模式（实时模型只管对话和白名单内的事，其余交给读得到整段对话的文字模型）、Gemini Live 的异步工具（结果在空档说，`WHEN_IDLE`）、LiveKit 的“用户说完先检索再回答”；百炼 Omni-Realtime 只有 VAD 自动回复和手动模式两种，没有“先别回复”的开关，所以判断只能和回复并行，回复完再核对。

**记忆检索的时机（`voice/recall.py`）**

| 时机 | 做法 |
| --- | --- |
| 接通时 | 会话提示词带上“你记得的关于用户的事”：就是文字助理每轮都带的核心记忆（`memory.orchestrator._stable_background`，个人 + 自己的全部项目，按重要度，≤1200 字），不再只有资料/偏好两类、300 字。实测“云杉项目负责人是小李”“松鼠青柠”都在其中，以前前台拿不到。称呼只用明确写着希望被怎么叫的。 |
| 每句话转写一到 | 并行做一次和文字助理同一套的召回（`retrieval.search_memory`：个人和项目的记忆、资料、知识页，重排过滤，`include_all_projects=True`，约 0.7–1 s），不靠前台自己想起来去查。 |
| 前台回答完 | 没调工具的回答，用 JEV 核对召回结果（`router.complement`：add / covered / irrelevant，≥0.7 才补）：答错（说成“李总”）、说没查到、只说“我去查一下”、或和记忆冲突（想吃海鲜大餐 vs 海鲜过敏），就以备注“记忆里查到……”让前台补一句（“哦对了，我翻到了”）。用户已经接着说别的，这条就作废。 |
| 前台主动查 | `memory_search` 改用同一套召回（以前是只查个人范围、不重排的关键词检索，项目记忆查不到）；知识页的 `[source:…]` 引用去掉。 |

**谁来处理（`voice/router.py`）**

前台（实时模型）照常先答：聊天、澄清、它记得的事、快速读（任务、记忆、定时任务、项目、积分、卡片）；要办的事（建、改、删、发、安排、提醒、调查）和要翻对话、看文件、上网、分析的交给助理。每句转写同时给 JEV 判去向（chat / read / assistant / unclear，p50 约 0.3 s，实测 23 句判对 20 句），回答完核对：

- 判为 assistant：回答里说了要去办却没调工具（原有规则）→ 转交；没说要办，再问 JEV 这句回答是不是把事落下了（`followthrough`：undone / handled / no_request，13 句全判对）→ undone 才转交，并让前台补一句“交给助理了”（前台若说过“已经建好了”顺口更正，`CLAIMS_DONE`）。反问、说明办不了、只是聊天都不会被转交（实测“今晚想吃海鲜大餐”被判 assistant，但核对为已处理，不再误转交）。
- 判为 read / chat：走上面的记忆核对。
- 只在 `memory.route_jev` 覆盖的用户上开（和文字助理的记忆路由同一灰度）且有 JEV key，`voice.router` 可关；没有判断或超时（3 s）就按原规则。

**交给助理前先整理（`voice/handover.py`）**

- `assistant_ask` 的参数改为 `request`：给没听到电话的助理看的一句完整的话（补上“它/那个/查一下”指的对象、要求和限制，不加用户没说的）。
- 转交前用百炼 `qwen-flash`（`voice.handover_model`，语音同一把 key，关思考，约 0.5 s；经网关的 qwen3.8-flash 实测 2–17 s）读整段通话、核心记忆和前台的转述，三选一：`brief` 用户口吻的完整指令（同音错字按通话和记忆纠正，如“云山”→“云杉”）；`answer` 只是问一件事且快速读（召回 + 任务列表）足以回答时直接答，不开助理轮次（JEV 有把握是要办的事 ≥0.8 时连资料都不读）；`ask` 看不出要办什么时先问用户。超时（4 s）或失败就用前台的 `request`。
- 文字助理那一轮除了整理后的指令，还拿到用户原话（语音识别）和通话最后 6 句（`origin_ref.voice_context` → `assistant/projection.py` 的 voice-turn 块；说明“与原话有实质出入以原话为准，不授予额外权限”）。来源引用超长时只发指令本身。

**实测（QA，2026-10-08，`.local-dev/voice-qa/voice_routing_e2e.py`）**：“云杉/青麦项目负责人”当场答对；“星灯计划演示在哪”经检索答对；前台把云杉负责人说成“李总”时 1 s 内补正“是小李，不是李总”；“想吃海鲜大餐”提醒过敏且不转交；“你让助理查青麦发布说明用什么语言”整理为“帮我查一下青麦项目的发布说明使用什么语言”，10 s 出结果（以前 15–40 s）；“让助理查它为什么卡住了”整理后带上任务名和已知卡点。

测试：`tests/unit/test_voice_routing.py`（转交、核对、整理三条路径）、`test_voice_router.py`（JEV 请求与校验、灰度、失败即无判断、核心记忆）、`test_voice_tools.py`（同一套召回、跨项目、无权限）、`test_voice_assistant_link.py`、`test_assistant_projection_voice.py`（通话上下文进助理轮次）。

## 21. 不说谎、不空许诺、用户的答复一定送到（2026-10-08 下午）

QA 实测（用户自己打的两通电话）：要建“每五分钟回复 hello”的定时任务，助理问“放测试项目可以吧？”，用户说“可以放到测试项目里吧”，前台回“行，那这就在测试项目里给你建好”却没交给助理；用户问“建好了吗”“你确定吗”，前台两次说已经建好；通话摘要也照抄，下一通电话开场还说“测试项目里的定时任务已经建好了”。用户逼着“那你帮我建啊”后，助理真去建了，`schedules.create` 6 次都被参数校验挡住（模型把 `schedule` 对象写成了 JSON 字符串），最后回“你先别急，我这就重试，好了第一时间告诉你”就结束了这一轮，什么也没再做；“那你试啊”又被半句话规则挡回。同一通电话里还有两处：“你让他改一改”被整理成用户没说过的改法（白榆改英文、青麦改中文）并真的改了；用户接着说的“改一下白榆项目使用中文、负责人是小李”打断了前台的回复，被打断的回复不做检查，这件事没交出去。

| 问题 | 改法 |
| --- | --- |
| 参数写成 JSON 字符串 | 根因在我们发给模型的工具说明：`schedule` 是三选一的 `oneOf`，没写 `type`。助理其实在循环里判断对了（每次推理都说“要传对象，不是字符串”），但 qwen3.8-flash 遇到没类型的字段只会填文本：原样重放 6/6 是文本，补上 `type: object` 6/6 是对象（gemini 两种都对，所以文字对话里建定时任务一直正常）。`agent/llm.py::_simplify_schema` 现在给分支类型一致的联合补上类型（88 个工具里只有定时任务的创建、修改两处），可空分支里的嵌套字段也一并简化；修好后重放 8/8 是对象。`tool/argument_repair.py` 留作兜底：校验失败且该处要对象/数组、传来的是能解析成对象/数组的字符串时，解析后再校验一次。 |
| 助理空许诺 | `assistant/follow_through.py`：个人助理一轮结束时的回复若承诺之后再做（“我这就重试”“好了告诉你”“你先别急”），而本轮没有启动会自己回报的工作（成功的 `tasks.submit/followup/next_step/resume`、`schedules.create/update/run`），就再给一步：紧跟这条回复加一条提醒“现在就做，做不到就如实说缺什么，不要许诺之后”，每轮最多一次、不超请求预算（`agent/loop.py`）。语音读到的是这一步之后的最终回复。系统提示加一条：回复即结束本轮，失败要当场改参数重试。 |
| 助理问了、用户答了 | 前台转述的结果里有问句时记下（`turns._asked`），用户接下来两句话里的回答，只要前台没调工具、没反问、不是闲聊，就自动交回给助理（前台说过“这就去办”就不再重复说，否则补一句“交给助理了”，说过“已经建好”顺口更正）。 |
| 刚办完又说“算了” | 刚说完一个结果（`turns._result_told`）后的两句话，即使被判成闲聊，也让 JEV 结合刚说的结果判断是不是落下了要办的事（`router.followthrough` 带 `just_told_result`：结果说已经建好、用户说“算了/不要了/停掉”就是要撤销）；是就交给助理。实测“算了，先不建了”在“这回建好了”之后判为 undone 0.91，“好的谢谢”判为 no_request。门槛降到 0.3：21 条实测里真正落下的请求都选 undone（最低 0.34），其余从没选过。 |
| 前台说“好了/建好了” | 回复里声称办好了、或用户问的是进展（JEV 判为 read）时，核对的证据除了召回的记忆，再加本通电话的事实（还在办的请求、助理最近两次回复）和当前的任务列表、定时任务列表（`Judge.state`）；不符或漏了就让前台马上更正（“等下，我刚才说错了”）。前台的直读工具每次调用都记一行日志（工具名、状态、耗时），能查证它到底查没查。 |
| 说要办却没调工具 | 承诺识别补上“这就在…给你建好”“我这就用…再试”“换个写法再试”等说法（排除“给你建议”）；被用户打断但已经播出的回复同样检查。 |
| 短句被挡回 | 有整理器时只有纯语气词（“嗯”“那个”）才回 `need_more`，“那你试啊”这类靠上下文的短句交给整理器，由它写成完整指令或决定先问。 |
| 整理时编内容 | 整理器换成 `qwen3.8-flash`（百炼直连，p50 1.3 s、最大 1.6 s；六类典型请求全对），提示词要求缺了关键内容（改成什么、删哪个、发给谁）一定先问，绝不替用户想改法。qwen-flash 实测会编出改法，已弃用。 |
| 摘要照抄谎话 | 摘要提示词：办没办成以后台备注为准，前台自己说的“建好了/办好了/再试一次”不算。用这通电话的记录重新生成，摘要变为“前台声称已建好，但未提供后台确认证据，此项暂无确切完成记录”。 |
| 前台说“我有点无语” | 情绪词禁用列表加上“无语、郁闷”。 |

测试：`tests/unit/test_assistant_follow_through.py`（真实 loop：许诺后再给一步且提醒紧跟其后、只给一次、已启动后台工作不提醒、接近请求上限不提醒；参数修复用当时失败的原参数）、`test_voice_routing.py`（用户答复交回、闲聊不交、反问后的下一句、“建好了”按任务和定时任务列表更正、空许诺的重试、被打断的许诺、短句交给整理器、承诺识别、刚建好又说“算了”交给助理、道谢不交）。

实测（QA，13:44，不建不改）：开场“那个每五分钟回复hello的任务已经建好了”（这时确实已建好）；问“建好了吗”答“建好了，就在测试项目里……要我把它删掉吗？”；“帮我设一个提醒”反问“提醒什么、什么时候”；“算了，先不用了”什么也不做。

## 22. 进展问题交给助理、语音轮次跟随助理的模型（2026-10-08 下午）

QA 实测（用户 14:07–14:25 的三通电话）：问“它进行到哪一步了”，前台查了 `tasks_overview`，答“还在进行中，具体做到哪一步系统没细说，我这就让助理去查”——这句许诺出现在读完工具后的回复里，以前只检查模型自发的回复，所以没交出去；之后“你看做的怎么样啊”“你再看看怎么样了”前台都用“还在进行中”搪塞，直到用户说“让个人助理帮忙查一下”才交给助理，30 秒后助理读了任务对话给出真实进度。另外“你确定办完了吗”时前台只查了任务列表，没看定时任务，答“确认过了，办完了”；一次“Unknown function call id”错误被算成播报被拒，已经说出的结果被记成失败，又多说了一句“办好了，结果我写在对话里了”。语音转交的轮次全部跑在 `qwen3.8-flash`/low（QA 启动参数 `VOICE_TURN_MODEL` 强制），而用户给个人助理选的是 `gemini-3.8-flash-high`。

| 问题 | 改法 |
| --- | --- |
| 进展问题前台自己答 | 分流标准改为：前台只看得到列表（任务名、是否在跑/做完、定时任务、项目、积分）和记得的事实，看不到里面；“做到哪一步、现在在干什么、为什么、结果的具体内容”判给助理（15 条典型句子全对，进展问题 0.52–0.99）。判给助理的事前台没转交、自己答了（哪怕只是“还在进行中”或“这个我办不了”），只要分流有把握（≥0.5）且不是闲聊（followthrough 判 no_request ≥0.6）、前台没反问，就交给助理并说一句“我让助理去看了”。前台提示词同步：问进展细节直接交助理，不要拿状态搪塞。 |
| 读完工具后的许诺 | 回答工具结果的回复（followup）同样检查许诺、同样对应到用户那句话。 |
| 先纠错后许诺 | 回复里说了“我这就去看看”，先交出去，不再用旧事实纠正一句了事。 |
| 定时任务查错地方 | `tasks_overview` 同时列出定时任务（名称、是否启用、下次运行），并注明“做到哪一步、具体内容、原因要交给个人助理”。 |
| 整理器用状态凑答案 | 问进展细节而资料里只有状态时，整理器一律写成转交指令。 |
| 错误归属 | “Unknown function call id”等针对工具回执的错误归为 item 类，不再当作播报被拒。 |
| 语音轮次的模型 | 不再强制 `VOICE_TURN_MODEL`：`voice.turn_model` 为空时（默认）语音转交跟随个人助理当前选的模型，用户在个人助理里切换模型，电话里交办的事也跟着换。QA 重启参数去掉了 `VOICE_TURN_MODEL`/`VOICE_TURN_VARIANT`。 |

测试：`test_voice_routing.py`（判给助理的事前台自答也会转交、反问/闲聊/没把握时留在前台、读完工具后的许诺、先兑现许诺再纠错、工具回执错误不算播报被拒）、`test_voice_tools.py`（任务概览带定时任务）。

## 23. 前台提示词按 OpenAI 实时语音的写法重排（2026-10-08 下午）

参考：OpenAI Codex 开源的语音层提示词 `codex-rs/prompts/templates/realtime/backend_prompt.md`（前台 + 后台 agent，与我们的结构一致：默认交给后台、说话不等于办事、完成只认后台回执）、OpenAI Cookbook《Realtime Prompting Guide》（分节：角色与目标、性格与语气、背景、工具、规则、对话流程；过渡语、没听清、换着说）、GPT-Live 文档（等结果时不猜结果，确认后才说完成，背景标注新鲜度）。只取与我们已知问题对得上的做法，原有被测试锁住的规则全部保留。

| 做法 | 落到哪里 |
| --- | --- |
| 分节 | `FRONT` 改成「角色与目标 / 性格与语气 / 你看得到什么、做得了什么 / 诚实（最重要）/ 工具 / 对话流程」，接通时的事实放在「背景」节；约 2500 字，内容不增加，原来散落的规则归位。 |
| 说话不等于办事（Codex） | 工具节与 `assistant_ask` 的说明都写明：说了“我去办”“我去问问”“我再试一次”，就必须现在调用 `assistant_ask`，嘴上答应不算。 |
| 诚实优先 | 「诚实」一节与其他要求冲突时以它为准；过渡语不能暗示结果（不说“马上就好”“应该没问题”）。 |
| 过渡语 | 调用工具前一句很短的话，说在做什么、不说理由，换着说；直接回答、回答卡片、没听清时不说。 |
| 没听清 | 请用户再说一遍，不猜，也不调用工具。 |
| 直接办 | 用户说清楚了就办，不复述、不预告计划。 |
| 背景新鲜度 | 接通时读到的事实前加一句“下面是接通时读到的，可能已经过时；用户问现在怎么样，先查或交给助理”（没有事实时不加）。 |
| 工具说明写“用在 / 不要用在” | `assistant_ask` 的 description 按用在（要办的事、进展细节、翻对话/看文件/上网、查不到的、回答助理的问题）和不要用在（寒暄、澄清、确认卡片）重写。 |
| 后台回复分清三种状态 | 语音轮次块（`VOICE_TURN_BLOCK`）要求助理说清：哪些办成了、哪些只是交出去还在跑、哪些没办成，不把交出去或做了一半的说成办完，结果不明就直说。 |
| 投递说明变短 | `_DELIVERY` 等转述指令压到两三句：开口说结果、不念备注、数字状态与备注一致、不调用工具。 |

实测（三通脚本电话，只问只读问题）发现并修了两处：

- 新加的“拿不准就交给助理”让“星灯计划的演示安排在哪里”从 1–3 秒的记忆查询变成 15 秒的转交：改为“人和项目的事实先用 `memory_search` 查；查了还答不了，或者拿不准是不是要办事，交给助理”。复测 0.8 秒答出，补充检查判 covered。
- 助理刚汇报完华为视频的进展，用户紧接着又问“做得怎么样了”：前台用刚收到的结果答了（followthrough 判 handled 0.94），却仍按分流（assistant 0.51）再转交一次，同一结果说了两遍。现在只有在“刚汇报过结果”且判 handled ≥0.7（`router.RETOLD_CONFIDENCE`）时留在前台；没有刚汇报的结果时 handled 仍不可信（只说状态也会判 handled 0.31–0.58），照旧转交。复测不再重复转交。

其余场景复测正常：核心记忆直接答；“口播视频现在什么情况”“具体做了哪些内容”交给助理，答出成片时长、五个镜头和未发布；“有哪些定时任务”前台查定时任务列表；“建好了吗”答“建好了，但没启用”。

测试：`test_voice_phrases.py`（新规则、背景新鲜度只在有事实时出现）、`test_voice_routing.py`（刚汇报过的结果再问不重复转交，没有刚汇报的结果照旧转交）。

## 24. 派出去的活不当结果说、说过头当场更正（2026-10-08 傍晚）

用户两次在电话里让助理“联网查一下”（OpenAI 今年的技术发展、Codex 的 Dot 语音助手）。助理那一轮都只是建了一个任务（`tasks.submit` → 命令 `task_create`），回复“已经帮你安排去联网查询了，目前正在后台检索整理中，查好之后第一时间告诉你”；前台却说“查好了，OpenAI 今年主要推了 GPT‑5、Sora 视频生成和 Operator 智能体”“查到了，Codex 那个语音助手叫 Dot，主打实时对话和屏幕操作”，内容是自己编的，真正的结果 40–60 秒后才由任务汇报送到。

根因不在措辞：语音这边把助理每一轮的结束都当成“结果到了”（备注写“个人助理回来了”，转述指令写“个人助理的结果到了……开口就说事情怎么样了”），而这一轮只是把活派了出去。修法按结构判断，不加提示词规则：

| 层 | 做法 |
| --- | --- |
| 结果是什么，看数据不看措辞 | 助理那一轮结算时，`assistant_link.running_tasks` 读这一轮（`AgentInboxItem.run_id`）发出的命令：`task_create`/`task_input`/`task_resume`/`task_finish_continuation` 指向、且仍在 `queued`/`running`/`resuming` 的任务。有就是“已交出去”：备注写明“个人助理把这件事交给了任务「…」，还没做完，做完会汇报结果”，用原有的“没查到东西”那套转述说明（`notice_instructions`，本来就用于超时和失败），不与别的结果合并成一句。 |
| 任务汇报接回原问题 | 记下任务 → 用户那句话；任务汇报在通话中到达时，备注写“关于用户说的“……”：交给任务「…」做的有结果了：……”，按普通结果转述（这时“结果到了”是真的），不再当作“用户没问的主动汇报”。`ReportWatcher` 的汇报带上 `task_id`。 |
| 每次转述都对照备注核对 | 前台每说完一段转述（包括抢先看到备注的那句回复），用 JEV 判断说的话是否都有备注支撑（`router.grounded`：backed / unbacked）；unbacked ≥0.8 时，立刻用备注里的原话补一句更正（走原有的“哦对了／我刚才说错了”记录补充通道）。反问和更正本身不再检查；用户已经接着说别的，就不插这句。 |

`grounded` 的阈值用今天 35 通 QA 电话里的 64 段转述实测：两段编出来的结果判 unbacked 0.99、1.00；其余 62 段照实转述的只有一段判 unbacked，0.62（反问时自己加了“火锅还是烧烤”两个选项），所以取 0.8。判定约 0.3 秒，在转述说完之后做，不影响开口速度。

实测（修后一通脚本电话）：“帮我联网查一下 OpenAI 最近发布了什么新模型”→ 备注写明交给了任务「查询OpenAI最新发布的新模型」还没做完 → 前台“已经安排去查了，结果一出来我马上告诉你”（grounded backed 0.99）→ 88 秒后任务汇报到达，按这句问话的答案转述“查完了，OpenAI 最近主要发了 GPT‑5 家族、深度推理的 o 系列……”（backed 1.00）。

测试：`test_voice_assistant_link.py`（这一轮交出去且仍在进行的任务：其他回合、已完成、暂停类命令、别的用户都不算；PostgreSQL 也跑过）、`test_voice_turns.py`（交出去的结果按“已交出去”说、单独说，任务汇报作为原问题的答案）、`test_voice_routing.py`（说过头的转述按备注更正；低于阈值、判 backed、没有判定都不动；更正不再被检查；用户已接话就不插）、`test_voice_phrases.py`、`test_voice_reports.py`。

## 25. 按用户的设置说话、电话里说的话进记忆（2026-10-08 晚）

个性化缺口分析（文字和电话两端都只“记事实”、不“记相处方式”）里电话这一侧的六条，一起落地。账号偏好统一由 `assistant/profile.py` 管（名字、称呼、语气、长短、表情、补充说明、通话习惯），文字助理和电话前台读同一份。

| 问题 | 做法 |
| --- | --- |
| 前台把记忆里的沟通偏好当资料，不照做 | 提示词新增「# 这位用户」一节（`prompt.user_lines`）：名字、称呼、用户自己写的补充说明、学到的说话偏好（`assistant/style.py` 的相处方式卡：`personal.style.*` 记忆 + 近 30 天反复出现的点踩原因）。`FRONT` 写明“这一节是用户自己定的说话方式，照做，和上面冲突时以这里为准”；其余记忆仍按资料处理。 |
| 称呼来源不稳（核心记忆里“演示草稿称呼用户为松果同学”） | 称呼来自设置的 `address`，没设就不加称呼；名字和称呼不再存成记忆（抽取丢弃，改名时把旧的命名记忆停用，不留墓碑）。 |
| 电话里抽出的记忆引用的是前台的转述 | 交给助理的那一轮，消息正文改为用户原话（`voice_context.request` 带前台整理后的请求，`VOICE_CONTEXT_BLOCK` 让助理按整理后的请求办）；抽取读到的、记忆引用的都是用户自己说的话。 |
| 只在闲聊里说的长期信息进不了记忆 | 判为闲聊/前台自答的一句（≥6 个字），再用 JEV 问一次 `lasting`：长期事实、长期喜好、以后怎么说话。`lasting` ≥0.7 时安静地交给助理一轮“记住”（lane `remember`，正文是用户原话，`phrases.remember_request`），当下不插话，结果照常汇报。 |
| 核心记忆进电话只有 1200 字，被重复项和一次性安排挤掉 | `CORE_CHARS` 1200→2000；相处方式不再占核心记忆名额（单独进「这位用户」）；同一事实的副本在保存后合并（`memory/curator.py`）；一次性安排带 `valid_until`，过期后不再出现。 |
| 回答长短、开场提旧事、主动汇报都不能调 | 设置 → 语音通话 → 通话习惯：`call_recap`（开场提上次聊的事，`greeting_instructions(recap=False)` 只打招呼）、`call_reports`（关掉后只汇报这通电话里交代的事，其余结果留在对话里）、`call_detail`（详细时 `VOICE_TURN_BLOCK` 的长短要求换成“可以说三四句”，随每次请求带上）。 |

通话中改设置：`api/voice.py` 订阅 `assistant.profile.updated`，重新算「这位用户」一节，`Bridge.rebase` 立刻 `session.update` 新的指令，`tell_reports`、`detail` 同步；正在挂断的通话不再改。近两天刚结束的安排（有 `valid_until` 的计划）每通电话最多提一次（`followups.mark_offered`）。

`lasting` 的阈值用 71 句标注过的话校准（`.local-dev/voice-qa/lasting/`：43 句来自 10-07、10-08 的真实通话，28 句补写的长期信息和容易误判的一次性说法）：没有一句一次性的话被判成 lasting；长期信息的置信度 0.60–1.00（“我女儿今年上小学三年级”0.74、“说话慢一点，我年纪大了听不清”0.60）。0.8 会漏 3 句，0.7 漏 2 句（另一句是“给你改名叫Mary”，作为请求本来就交给助理处理），取 0.7。

测试：`test_voice_personal.py`（通话中改设置立刻重建指令、关掉开场回顾、关掉主动汇报但本通电话的请求照常回答、详细回答随请求带上、闲聊里的长期信息安静交给助理、低于阈值或太短的不交）、`test_assistant_profile.py`（前台「这位用户」一节、每个计划只提一次）、`test_voice_assistant_link.py`（正文是用户原话、整理后的请求放在上下文）、`tests/integration/test_voice_ws.py`。

## 26. 第一通电话问一次称呼，设置优先于学到的（2026-10-08 夜）

个人助理的“初次见面”（DEVLOG 同日一节）在电话这一侧的部分。

- **什么时候问**：由 `FrontFacts.ask_address` 决定，取值来自 `profile.call_should_ask_address(decided, intro)`。以下都成立时为真：
  - 称呼还没有决定；
  - 见面里没答也没跳过；
  - 没说过“以后再说”；
  - 之前的电话也没问过（`intro.call_asked`）。
- **只问一次**：接通时 `user_facts(offer=True)` 算一次。算出要问，就立刻 `mark_call_asked`，以后的电话不再问，这通问没问成都一样。
- **怎么问**：「# 这位用户」一节里，称呼为空且要问时加一条：“打完招呼、用户的事先办着，找个自然的时候问一次怎么称呼（只问这一次，用户不想说就算了，不追问）。用户说了就这样叫，同时用 assistant_ask 把用户的原话交给助理记到设置里。”助理调用 `assistant.preferences` 设称呼，记为 via chat。
- **通话中设置变了**（`profile_changed`）：
  - 重算时传 `asking=facts.ask_address`，称呼一旦决定，这条就去掉；
  - 这通已经提过的近况跟进（followups）保留，不会再提一次。
- **行业**：带上用户的行业，“用户做的生意：美业；举例和建议默认往这上面靠。”
- **设置优先**：学到的说话偏好和用户自己的设置冲突时，以设置为准。提示词改为：“照做，和上面的通用规则冲突时以这里为准；和用户自己的设置冲突时以设置为准。”对长短、语气、表情、电话详略的反馈，在个人助理对话里说的直接改设置（抽取 `source-only-v8` 的 `setting`），不再作为学到的偏好进这一节。

测试：
- `test_assistant_intro.py`：第一通电话问一次，之后不再问；`call_should_ask_address` 各种情况；
- `test_assistant_profile.py`：设置优先的措辞、行业一行。
