# 个人助理实时语音接入方案

状态：**方案，待用户确认后分期实施**。日期：2026-10-07（Asia/Shanghai）。分支 `codex/long-term-memory-plan`。

本轮做了三件事：联网调研 OpenAI Dot / Codex 语音、豆包（火山）端到端语音、百炼 Qwen-Omni 实时 API 的公开资料；用 `backend/.env` 里现有的 DASHSCOPE key 对百炼实时模型做了三次真实协议实验（第 2 节，脚本在本会话临时目录，不入库）；没有修改平台代码，没有启动 OpenBox 服务。本文替代 [语音接入参考](PERSONAL_ASSISTANT_VOICE_REFERENCE.md)（2026-10-03）第 2 节"级联管线"的选型结论；该文第 3、4、5 节关于打断、播报队列、设备核对的要求仍然有效，并入本文第 6 节。

## 0. 一页结论

**推荐：语音前台 + 个人助理后台。** 百炼 `qwen3.8-omni-flash-realtime` 当"前台"：听、判停、打断、寒暄、口播；现有个人助理原封不动当"后台"：工具、记忆、任务。前台只有一个工具 `assistant_ask(text)`，把用户原话交给后台（走现在键盘输入完全相同的 `inputs.accept_turn`），结果回来后前台口播；结果晚到就在下一个空闲窗口注入。这与 OpenAI "轻量实时语音模型 + 重推理模型异步"、豆包对话式 AI 的"混合编排"是同一种结构。

不选另外两条路的原因：

- **级联（ASR → 助理 → TTS）**：个人助理一轮 2–5 秒起步，带工具几十秒，每句话都要等它，没法像打电话一样聊；而且判停、打断、假打断、附和过滤都要自己做（参考文档第 3 节那套状态机），实时模型自带这些。
- **纯实时模型**：没有工具、记忆、任务，实验里还会编造（第 2.3 节），只能当前台。

**今天实测确认的事实**（第 2 节）：现有 key 能连 `qwen3.8-omni-flash-realtime` 并注册工具；用户说完 0.2 秒模型就开口“好嘞，我这就去帮你看看”，并在同一次回复里发出 `assistant_ask`；后台结果延迟 6–12 秒回传后 0.3 秒开始口播，按提示词先说“我这边查到了”再逐字念结果；结果未回来时用户可以继续聊，结果在模型空闲时注入成功、在它说话时注入会报错（代理必须等空闲）。

**费用量级**：按实测用量和北京区价目，纯通话约 0.5 元/小时（上下文变长后更高；不含个人助理自身的模型费用）。

**分期**：P1 后端 + Web（本机/QA 可用）；P2 手机端；P3 打磨（逐字口播、后台结果主动播报、费用账本、通话记录回看）。

## 1. 三家的做法

### 1.1 OpenAI：Dot 与 Codex 语音

- **Dot**（DevDay，2026-09-29）：always-on 的个人 agent，跑在自己的云电脑和浏览器里，连接 4,000 多个应用；用户从 ChatGPT、Slack、Teams 联系它，媒体报道还包括电话（短信后续）；没在对话时做只读的"主动调研"，需要决定时来问用户；记忆跨渠道。它暂时不能主动打电话或发短信。[官方 DevDay 页](https://learn.chatgpt.com/docs/whats-new/devday-2026)、[DevX](https://www.devx.com/artificial-intelligence-ai/openai-dots-pricing-access-explained/)、[报道](https://pasqualepillitteri.it/en/news/19302/openai-dots-personal-ai-agent-devday-2026)。
- **ChatGPT Voice（GPT-Live，桌面 2026-07-23）**：全双工实时语音模型，一边听一边说，用语音指挥 Work / Codex 里的多个 agent。第三方整理说它的结构是"轻量低延迟语音模型 + 重推理模型异步"。[TechCrunch](https://techcrunch.com/2026/07/24/openais-new-voice-mode-makes-it-to-the-chatgpt-desktop-app/)、[Codex 语音编排指南（第三方）](https://codex.danielvaughan.com/2026/07/25/voice-first-agent-orchestration-guide-codex-cli-gpt-live-presence-realtime-v3/)。
- **Codex CLI**：`/voice` 开实时会话（Realtime v3，WebRTC）。两种模式：conversational（实时会话自带 Codex 工具，会话用最近线程上下文初始化）和 transcription（语音转文字当普通用户消息进 agent loop，延迟和费用更低）；Goal 模式在预算阈值时用语音回问；AGENTS.md 里写"语音会话约定"（破坏性操作先口头确认、每个改动一句话、跑测试报通过数、长话短说）。[Codex 实时会话（第三方）](https://codex.danielvaughan.com/2026/03/31/codex-cli-realtime-sessions-voice-transcription/)、[Codex changelog](https://releasebot.io/updates/openai/codex)。
- 对我们的启发：语音层薄，重活在后台 agent；语音既是指令入口也是汇报出口；给前台写一份"语音约定"。

### 1.2 豆包（火山引擎）

- **消费端**：端到端 Speech2Speech 模型（Seed-RealtimeVoice），2026-04 升级全双工（Seeduplex），裸模型约 700 ms；工程上靠火山 RTC（WTN 传输网络）、客户端帧级 VAD、随时打断。[技术解析](https://developer.volcengine.com/articles/7400774724738875428)、[全双工升级报道](https://news.qq.com/rain/a/20260410A07DVH00)、[RTE 社区](https://www.cnblogs.com/rtedev/p/18682102)。
- **开放 API（端到端实时语音大模型）**：WebSocket 二进制协议 `wss://openspeech.bytedance.com/api/v3/realtime/dialogue`，O2.0 / SC2.0 版本。它**没有** function calling；留给业务的口子是：`ChatTTSText`（ASREnded 之后由客户端指定要播的文本，模型只负责合成——官方的"逐字口播"）、`ChatRAGText`（外部 RAG 文本交给模型总结口语化，≤4K 字）、`ChatTextQuery`（文本提问）、上下文增删改查、`ConversationTruncate`（按已播进度对齐上下文，只让模型看到已经播出的内容）。[API 接入文档](https://docs.volcengine.com/docs/6561/1594356)。
- **RTC 对话式 AI 的"混合编排"**：每句语音同时送端到端模型和方舟大模型（带 Tools）；方舟模型决定调工具就采用它的输出，否则采用端到端模型的输出；防抢话靠 `end_smooth_window_ms` 调到 800–1200 ms。[接入端到端实时语音大模型](https://docs.volcengine.com/docs/6348/1902994)。
- 对我们的启发：可靠内容由"外部大脑"产出，端到端模型只负责听和说；`ChatTTSText` 就是我们 P3 要做的逐字口播。

### 1.3 百炼 Qwen-Omni Realtime（现用供应商）

- `qwen3.8-omni-flash-realtime`（2026-09-21 上线）：function calling、远程 MCP、`semantic_vad`、WebSocket / WebRTC / AOQ 三种接入；北京区价格 语音输入 6、语音输出 12、文本输入 1.5、文本输出 4.5 元/百万 token；单会话最长 120 分钟、输入上限 196,608 token、音频历史 100 轮/600 秒。[模型页](https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime)、[实时指南](https://help.aliyun.com/zh/model-studio/realtime)、[上线说明](https://www.nexbrain.xyz/news/a/qwen3-8-omni-flash-realtime-bailian.html)。
- 协议要点（对本方案关键的）：工具在 `session.update.tools` 注册；模型以 `response.function_call_arguments.done` 给出 `call_id`/`arguments`，function call 不产生语音；结果用 `conversation.item.create(type=function_call_output)` 回传，再 `response.create` 续答；`conversation.item.create` **只接受** `function_call_output` 和 `mcp_approval_response`，不能塞文本消息；联网搜索与 tools 互斥；WebRTC 的 SDP POST 要带 API Key，所以浏览器不能直连；AOQ 只有 Android / iOS / HarmonyOS 原生 SDK。[客户端事件](https://help.aliyun.com/zh/model-studio/client-events)、[服务端事件](https://help.aliyun.com/zh/model-studio/server-events)、[AOQ 实践](https://help.aliyun.com/zh/model-studio/best-practice-aoq-omni-realtime)。
- 与 10-03 参考文档的差别：那时的依据是 `qwen-audio-3.1-realtime-plus`（smart_turn、没有可靠的业务工具回路），结论是实时模型不进主链路。现在 3.8 omni 有工具回路和 semantic_vad，而且"挂起的 function_call_output 可以等空闲再注入"今天实测成立，所以首选变了。

## 2. 今日实测（2026-10-07）

环境：本机，`backend/.venv` 的 websockets 15.0.1，`backend/.env` 的 `DASHSCOPE_API_KEY`，直连（`proxy=None`，与 demo 一致；QA 有 HTTPS_PROXY）。语音用 macOS `say -v Tingting` 合成 16 kHz PCM16 流式发送，每包 100 ms。只打印事件、文本和用量，不打印 key。

| # | 实验 | 结果 |
| --- | --- | --- |
| 1 | 连接探测：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=…`，`session.update` 带 1 个 tool + `semantic_vad` | `qwen3.8-omni-flash-realtime`：`session.updated` 回显 tools=1，`turn_detection` 为 semantic_vad、`create_response=true`、`interrupt_response=true`。`qwen3-omni-flash-realtime`：连上但 tools 回显 0（不支持工具）。`qwen3.5-omni-flash-realtime`：握手超时，未再试。文档要求 3.8 用业务空间域名 `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime`，今天旧域名也可用；正式环境按文档配置并填 WorkspaceId。 |
| 2 | 工具回路：3.7 秒语音"帮我看一下贪吃蛇项目现在进展到哪了"；收到 function call 后等 6 秒模拟后台，再回传 `{"speech": …}` + `response.create` | 语音结束后 0.2 秒 `speech_stopped`、0.4 秒转写完成、0.5 秒发出 `assistant_ask`（参数就是原话）。回传后 0.35 秒 `response.created`，1.5 秒后说完 8.6 秒音频。口播："贪吃蛇的收尾自检昨晚已经做完，一切正常，这次没有改动文件。配色方案还在等你选一个。"——原文是"「贪吃蛇」的收尾自检昨晚做完了，一切正常，这次没有改动文件；「配色」还在等你选一个方案。"，近似复述，非逐字。用量：输入文本 1,159 token（提示词 + 工具定义，每轮重算）、音频 35 token/3.7 秒；输出音频 108 token/8.6 秒。 |
| 3 | 挂起期间继续对话：A 触发工具后不回结果；2 秒后说 B"你觉得今天天气适合跑步吗"；B 播报中抢先注入；B 结束后再注入 | 模型在工具挂起时正常回答了 B，但**编造了天气**（提示词必须禁止它回答需要事实的问题）。B 播报中：`function_call_output` 被接受，`response.create` 报 `Conversation already has an active response`。B 结束后再发：重复的 `function_call_output` 报 `Duplicate function call output`，但 `response.create` 成功，模型口播了 A 的结果。结论：结果项可以随时创建，`response.create` 必须等模型空闲；代理要维护"响应中 / 用户说话中 / 空闲"状态机。 |

| 4 | 先应答再查：提示词要求“先用一句话告诉用户你去查，再在同一次回复里调用 assistant_ask；结果回来先说‘我这边查到了’”；语音“你好，帮我看一下贪吃蛇项目进行得怎么样了”，后台模拟 12 秒 | 语音结束 0.2 秒后同一个 response 里先出语音项“好嘞，我这就去帮你看看贪吃蛇项目的进展。”（3.8 秒音频），紧接着出 function call，`response.done` 一起结束。12 秒后回传，0.3 秒开始口播：“我这边查到了，「贪吃蛇」的收尾自检昨晚做完了，一切正常，这次没有改动文件；「配色」还在等你选一个方案。”——这次是逐字。用量：输入文本 1,195 / 1,304 token，输出音频 48 + 125 token。 |

其它：音色 `Cherry` 对该模型不可用（生成时报错），默认 `Tina` 可用，音色清单见[音色列表](https://help.aliyun.com/zh/model-studio/omni-voice-list)；直连握手偶发超时（三次连接里一次），代理要带重连。

费用按实验 2 的用量估算：每轮约 1.2k 文本输入（0.0018 元）+ 35 音频输入（0.0002 元）+ 108 音频输出（0.0013 元）≈ 0.0035 元；按每分钟两轮、一小时 120 轮约 0.4 元，上下文累积后翻一倍以内，所以"约 0.5 元/小时"是量级，接入后用 `response.done.usage` 核算。

## 3. 架构

```
浏览器 / 手机 ──PCM16 16k，100 ms/包──► OpenBox 后端 /ws/assistant/voice（语音代理）──► 百炼 Omni Realtime
            ◄──PCM16 24k + 状态──────┘            │ assistant_ask(text)
                                                    ▼
                                      个人助理（现有）：inputs.accept_turn → Inbox → 主会话一轮
                                                    │ turn.finished / result_message_id
                                                    ▼
                                        口播稿 → function_call_output →（空闲时）response.create → 口播
```

1. **一个大脑。** 事实、工具、记忆、任务全在个人助理。前台只有 `assistant_ask`，不给它 memory / tasks / 联网（联网与 tools 本来互斥），避免两个大脑说法不一致。前台提示词（第 4.4 节）规定：凡涉及用户的项目、任务、进展、记忆、日程、文件、费用的请求必须交给后台；寒暄和澄清自己答；不知道的事不猜；口播只能用 `speech` 里的事实。
2. **每次 `assistant_ask` 就是一次普通主会话轮次。** 走 `inputs.accept_turn(origin=human, entrypoint="assistant_voice", client_id=通话轮次 ID)`，所以文字界面照常出现这条用户消息和助理回复，记忆提取、关注列表、通知全部不变。前台自己答的寒暄不进主会话，也不在通话界面显示（通话界面不显示对话文字；要看文字去对话页）。
3. **等结果。** 代理用现有 `agent.inbox.wait_for_inbox_terminal(inbox_id, user_id, timeout)` 等这一轮结束，读 `result_message_id` 的文本；可订阅 bus 的 `message.text_delta`（sessionId=主会话）给客户端一个"助理在写"的提示。P1 不做流式口播。
4. **语音轮次的回复就是口播稿。** `assistant_voice` 来源的轮次在系统提示里加一段："这条来自语音通话：两三句话说完，不用链接、列表、标题和任何标识符，像在电话里说。"文字界面显示的也是这一条，单一来源。代理再做一层保险：去掉 markdown 链接（保留标签）、列表符号、代码标记。
5. **结果晚到。** 代理状态机：`idle / user_speaking / responding / tool_pending(call_id, inbox_id)`。结果一到立刻 `conversation.item.create`，`response.create` 只在 `idle` 且非 `user_speaking` 时发（实验 3）。“好的，你稍等一下，我去看看”这句应答由模型自己在发出工具调用的同一次回复里说（实验 4），不需要代理插手。工具挂起超过 20 秒且模型空闲时，代理直接给客户端下发一段预合成的固定短语（"还在办，好了我告诉你"，不经过模型；同一个 call 只能回一次结果，所以不能用中间结果），并带 `playback_id` 可被插话打断。通话结束时还没回来的结果照常写进主会话，用户在文字界面能看到，通知走现有通道。
6. **打断。** 沿用 demo：收到 `speech_started` 立刻清空客户端播放队列并隔离旧回复音频（按 `response_id`）；服务端 `interrupt_response=true` 会自动取消生成。附和过滤交给 `semantic_vad`（`threshold` 0.5、`silence_duration_ms` 600–800，接入后按实际调）。
7. **前台的"认识你"。** 每次通话新建 provider session，`instructions` 里放：当前日期时间、用户画像摘要（现有 `memory_context` 的 profile 部分，不放具体记忆）、最近 3–5 轮主会话的一句话摘要（便于"刚才那个"的指代）。不塞完整历史——`conversation.item.create` 也塞不进去。
8. **密钥与认证。** DASHSCOPE key 只在后端；客户端先 `POST /api/auth/ticket`（`audience="voice"`，`create_ticket` 已有该参数）再连 `wss://…/ws/assistant/voice?ticket=`，与 `/ws/agent` 同一套票据机制。
9. **限额。** 同一用户同时只允许一个通话；单次通话上限 30 分钟（provider 上限 120）；每用户每日通话分钟数配额；超限挂断并提示。通话期间文字界面照常可用（Inbox 本来就串行）。
10. **传输路线。** P1/P2 都走后端 WebSocket 代理（demo 验证过的链路，Web 和手机共用同一套服务端逻辑，工具编排全在服务端）。WebRTC 要么把 API Key 交给浏览器（不行），要么由后端代理 SDP 再让客户端自己编排工具（逻辑分叉到两个客户端，不取）；AOQ 只有原生 SDK，手机端若弱网表现差再评估。

## 4. 后端（拟新增）

### 4.1 模块

| 文件 | 职责 | 来源 |
| --- | --- | --- |
| `backend/voice/provider.py` | 百炼 realtime 客户端：连接（带重连和超时）、`session.update`、事件解析成内部事件、`response.create`/`cancel`、`function_call_output` | 从 `demos/realtime-voice/server.py` 的 relay 抽出，去掉本机限制 |
| `backend/voice/bridge.py` | 状态机；`assistant_ask` → `inputs.accept_turn` → `wait_for_inbox_terminal` → 读回复 → 口播稿 → 回传；超时与失败的话术；通话结束清理 | 新 |
| `backend/voice/speech_text.py` | 去 markdown（链接留标签、去列表/标题/代码标记、去 ID 样式串），长度上限 | 新，纯函数 |
| `backend/voice/prompt.py` | 前台提示词与语音轮次附加提示 | 新 |
| `backend/voice/meter.py` | 每轮 `response.done.usage` 核算 | 搬 `demos/realtime-voice/pricing.py`，价格改为 3.8 omni |
| `backend/api/voice.py` | `/ws/assistant/voice` 端点、票据 audience、并发与配额、消息格式校验 | 参考 `api/ws.py` 握手 |
| `core/config.py` `VoiceConfig` | `enabled`、`model`、`voice`、`workspace_id`、`api_key`（默认复用 DASHSCOPE）、`endpoint`、`max_call_minutes`、`daily_minutes`、`vad_threshold`、`silence_ms` | 参照 `VideoTranscriptionConfig` |

### 4.2 数据

| 表 | 字段 | 说明 |
| --- | --- | --- |
| `VoiceCall` | id, user_id, workspace_id, main_session_id, model, voice, client, started_at, ended_at, status(active/ended/failed/limit), turns, usage(四类 token), estimated_yuan, price_date | 一次通话一行；费用账本独立于主助理 |
| `VoiceTurn` | id, call_id, provider_call_id, inbox_id, message_id, transcript, requested_at, delivered_at, outcome(delivered/late/failed/cancelled) | 一次 `assistant_ask` 一行；`inbox_id` 对应主会话那条输入 |

不保存音频，也不单独存转写：用户原话就是主会话里的用户消息，助理回复就是那条消息；通话界面不显示实时字幕。

### 4.3 WebSocket 消息

客户端 → 服务端：二进制 PCM16 16 kHz 单声道 ≤ 100 ms/包（沿用 demo 的校验：≤ 12,800 字节、偶数长度）；JSON `{"type":"stop"}`、`{"type":"mute","on":true|false}`。

服务端 → 客户端：二进制 PCM16 24 kHz；JSON `ready{model,output_sample_rate,call_id}`、`speech.started`、`speech.stopped{invalid}`、`response.started`、`response.done{cancelled}`、`transcript.user{text,final}`、`transcript.assistant{text,final}`、`assistant.turn.accepted{inbox_id,message_id}`、`assistant.working`、`assistant.late`、`phrase{playback_id}`（固定短语）、`cost{…}`、`limit{reason}`、`error{message}`、`ended`。

### 4.4 前台提示词（草案）

```
你是 OpenBox 个人助理的语音前台，正在和用户打电话。你自己没有工具、记忆和任务信息。
凡是涉及用户的项目、任务、进展、记忆、日程、文件、费用、云电脑、发布的请求，调用 assistant_ask，
先用一句话告诉用户你去查（"好的，你稍等一下，我去看看，查到了告诉你"），再在同一次回复里调用，
把用户的原话原样交过去，不要改写、不要先猜答案。两步都要做。
结果回来后，先说"我这边查到了"，然后只说 speech 里有的事实，不增删，不解释内部过程。
结果还没回来时用户又问起，就说还在办。
寒暄、重复、澄清可以自己回答；天气、新闻、价格这类需要事实的问题你不知道，就直说不知道。
用用户的语言，一到两句话，像人说话，不念链接、ID、编号。
用户说"停""别做了"只是要你停下口播；要停止任务要明确说出来，由个人助理处理。
```

语音轮次附加给个人助理的提示：

```
这条消息来自语音通话。请用两三句话口语回复，不用链接、列表、标题、表情和任何标识符；
需要用户在界面上操作的，说"我把链接发到对话里了"，链接会出现在文字界面。
```

### 4.5 测试

- 状态机单测：空闲注入、说话中不注入、响应中不注入、工具超时短语、通话结束时挂起结果照常入库。
- 口播稿清洗单测：链接留标签、列表拍平、ID 串去除、长度上限。
- 端点：票据 audience 不符拒绝；同用户第二个通话拒绝；配额耗尽挂断；错误音频包断开；`stop` 后等待最后一次用量。
- 桥接：用假 provider（回放事件脚本）+ 真 Inbox（PG 一次性库）验证 `accept_turn` 的 `origin_ref.entrypoint="assistant_voice"`、回复出现在主会话、`VoiceTurn.outcome`。
- 真连一次（手动、带 key）：第 2 节脚本改成 pytest 的可选标记，默认跳过。

## 5. 客户端

### 5.1 Web（frontend-v2，P1）

- 入口：助理页 `Topbar` 的 actions 加"通话"按钮；通话面板是一个覆盖层：状态球、计时、静音、挂断、本次费用（不显示对话文字），样式按现有 tokens。采集/播放直接搬 demo 的 `capture-worklet.js` 和 `app.js` 的播放队列逻辑，改成 hook（`useVoiceCall`）。
- 通话期间主会话列表照常实时刷新：`assistant.turn.accepted` 到了就滚到那条消息。
- 不改 composer；参考文档里"麦克风转文字进输入框"的听写功能是另一件事，不在本方案里。

### 5.2 手机（Flutter，P2）

- `pubspec.yaml` 现在没有音频库。候选：`record`（PCM16 16 kHz 流式采集）+ PCM 播放库（`flutter_pcm_sound` 一类）或 `flutter_webrtc`，接入时定；iOS 加 `NSMicrophoneUsageDescription`，Android 加 `RECORD_AUDIO`。
- 入口：助理页 AppBar 的通话按钮，通话页全屏。
- 设备核对清单沿用参考文档第 5 节：前台、锁屏、后台、来电中断、耳机切换、网络切换；系统不允许后台麦克风时明确显示，不假装在听。

## 6. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 前台编造（实验 3 编了天气） | 提示词禁止答事实类问题；不给联网；上线前打开 `debug_transcripts` 抽查转写日志；文字界面始终是权威 |
| 复述不保证逐字（实验 2 近似、实验 4 逐字），金额/选项/批准可能走样 | P1 接受近似并要求"只用 speech 里的事实"；P3 对含金额/选项/审批的回复改走 `qwen3-tts-flash-realtime` 合成助理原文直接下发（相当于豆包的 ChatTTSText），并给前台一个"系统已播报"的回传——需实测模型是否配合 |
| 后台慢（工具链几十秒） | 20 秒固定短语；通话结束后结果照常入主会话并通知 |
| 后台失败/超时 | `function_call_output` 返回失败说明，前台如实说"没办成，文字界面里有原因" |
| 同一结果被注入两次 | 以 `call_id` 去重（provider 也会报 Duplicate） |
| 直连偶发握手超时 | 连接重试 3 次、`open_timeout` 30 秒；通话中断线给客户端 `error` 并允许一键重拨 |
| 费用失控 | 单次与每日分钟配额；`cost` 实时下发；账本独立 |
| 隐私 | 不存音频；交给助理的轮次即主会话消息，沿用主会话的隐私与删除规则 |
| 域名/地域 | 正式环境按文档用业务空间域名并填 `workspace_id`；新加坡区按 key 另配 |

## 7. 分期与验收

**P1 后端 + Web（QA 可用）**：第 4 节全部、第 5.1 节；验收：在 QA 账号里打一通电话——问"我有什么待办"，前台调后台、口播结果且文字界面出现同一轮；问"今天天气"前台说不知道；后台 30 秒不回时听到固定短语、结果稍后口播；对方说话时打断立即停；挂断后费用与 `VoiceCall` 一致；第 4.5 节测试全绿。

**P2 手机端**：第 5.2 节；验收：iPhone 真机/模拟器完成 P1 同样的通话，加锁屏与来电中断两项。

**P3 打磨**：逐字口播（TTS 直发）、后台任务结果主动播报（通话中有新结果时在空闲窗口播）、通话记录回看、费用进计费中心。

工作量按代码范围：P1 约等于一次中等功能（新模块 6 个文件 + 一个 Web 面板 + 迁移 2 张表），P2 约等于 P1 的一半加真机验证，P3 视 TTS 实测结果。

## 8. 实施文档（2026-10-07）

按本文方案拆成四份可直接开工的文档，事件名、字段名、文案 key 以总规格为准：

| 文档 | 内容 |
| --- | --- |
| [语音通话总规格](VOICE_CALL_SPEC.md) | 体验目标、通话时间线、客户端与服务端状态机、WebSocket 协议（票据、二进制帧、全部事件与字段、关闭码）、`voice` 命名空间全部文案（zh-CN/en-US）、固定短语、提示音、限额与隐私、跨端验收 14 条 |
| [后端实现](VOICE_CALL_BACKEND.md) | 模块与文件、`VoiceConfig`、`voice_calls`/`voice_turns` 表与迁移、票据受众、`/ws/assistant/voice` 握手与泵任务、百炼适配器与事件翻译、桥接状态机规则、`assistant_ask` 在个人助理侧的两处改动、口播稿清洗、前台提示词、固定短语、计量、测试清单、任务顺序 |
| [PC 悬浮窗](VOICE_CALL_WEB.md) | 顶栏入口、悬浮窗两种形态与各状态画面、交互细节、与聊天列表联动、音频实现（采集 worklet、播放队列、提示音）、文件与 store、测试清单、任务顺序 |
| [移动端](VOICE_CALL_MOBILE.md) | 全屏通话页 + 顶部通话条（内容下移、不遮挡）、画面规格、权限/后台/来电中断/耳机/网络的系统行为、依赖与平台声明、Riverpod 结构、测试清单、任务顺序 |

## 9. 资料

OpenAI：[DevDay 2026 官方页](https://learn.chatgpt.com/docs/whats-new/devday-2026) · [DevX：Dots 价格与访问](https://www.devx.com/artificial-intelligence-ai/openai-dots-pricing-access-explained/) · [Dots 报道](https://pasqualepillitteri.it/en/news/19302/openai-dots-personal-ai-agent-devday-2026) · [DevDay 现场故障报道](https://officechai.com/ai/openai-faces-technical-glitches-during-dev-day-2026-with-dot-not-responding-voice-mode-not-working/) · [TechCrunch：ChatGPT Voice 桌面版](https://techcrunch.com/2026/07/24/openais-new-voice-mode-makes-it-to-the-chatgpt-desktop-app/) · [Codex 语音编排指南（第三方）](https://codex.danielvaughan.com/2026/07/25/voice-first-agent-orchestration-guide-codex-cli-gpt-live-presence-realtime-v3/) · [Codex 实时会话（第三方）](https://codex.danielvaughan.com/2026/03/31/codex-cli-realtime-sessions-voice-transcription/) · [Codex changelog](https://releasebot.io/updates/openai/codex)

豆包 / 火山：[端到端实时语音大模型 API](https://docs.volcengine.com/docs/6561/1594356) · [RTC 接入端到端模型（混合编排）](https://docs.volcengine.com/docs/6348/1902994) · [技术解析](https://developer.volcengine.com/articles/7400774724738875428) · [全双工升级报道](https://news.qq.com/rain/a/20260410A07DVH00) · [RTE 社区](https://www.cnblogs.com/rtedev/p/18682102)

百炼：[Qwen-Omni 实时指南](https://help.aliyun.com/zh/model-studio/realtime) · [qwen3.8-omni-flash-realtime 模型页](https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime) · [客户端事件](https://help.aliyun.com/zh/model-studio/client-events) · [服务端事件](https://help.aliyun.com/zh/model-studio/server-events) · [Function Calling 上线说明](https://www.aliyun.com/product/news/28874) · [AOQ 实践](https://help.aliyun.com/zh/model-studio/best-practice-aoq-omni-realtime) · [3.8 上线报道](https://www.nexbrain.xyz/news/a/qwen3-8-omni-flash-realtime-bailian.html) · [音色列表](https://help.aliyun.com/zh/model-studio/omni-voice-list)
