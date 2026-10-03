# 个人主助理语音接入参考（不在当前范围内）

状态：**参考记录，不是计划**。日期：2026-10-03。当前的 [个人主助理设计](PERSONAL_ASSISTANT_DESIGN.md) 只做文字交互，语音列为非目标。本文保存修订时对公开语音协议的核查结论，以及将来若要接语音时的链路依据；没有增量、里程碑或放行条件，其中提到的表、端点、组件都未列入任何计划。本次没有运行模型、服务或设备。

## 1. 当前 Demo 实际做了什么

`demos/realtime-voice` 当前是浏览器与阿里实时语音 WebSocket 之间的桥接。`server.py` 第 24 行固定 `MODEL = "qwen-audio-3.1-realtime-plus"`，第 26 行直连 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime`，接收音频后直接转发 provider 输出音频；识别文本用于字幕；第 232 到 242 行的 `session.update` 使用 `smart_turn`，未声明业务 tools。浏览器发送的 stop 是结束通话。`app.js` 在 speech started 时清本地播放，现有每轮 usage/计价和新通话清零逻辑应保留。

所以当前 Demo 没有“强主助理工具回路”，也没有主任务结果可靠回注。不能把已有流式音频代理描述成任何委派协议的实现。本节据此和公开协议文档作出定案；没有运行验证的事项仍标注为待实测。

## 2. 若将来接语音：级联管线的依据

若将来接语音，主助理的语音链路采用级联管线：**流式 ASR 产生 final 转录 → 与键盘完全相同的主助理文字链路 → 主助理持久保存的 `speech_text` 分段 → TTS 按段合成 → 设备播放器**。实时语音模型（qwen-audio realtime 系列）继续留在 `demos/realtime-voice` 作为独立通话模式，不进入主助理链路，不与 ASR 双发。“实时模型口语化”不作为可选项。

这样定的依据：

1. **阿里协议对回注有硬限制。** 官方客户端事件文档说明：响应生成中不允许重复触发 `response.create`；`smart_turn` 模式下，从 `input_audio_buffer.speech_started` 到 `response.done` 之间禁止触发。服务端事件文档说明 function call 部分不会送入 TTS 播报。后台任务结果到达时经常正处于这些窗口，“把结果作为 `function_call_output` 回注并让模型播报“没有可靠时机。
2. **协议没有带外响应。** OpenAI Realtime 有 `response.conversation = "none"` 加 `metadata` 的 out-of-band 响应，可以在不改会话历史的情况下并行生成；阿里文档未列出同类能力。设计不能建立在对方未承诺的功能上。
3. **公开 Chat-Supervisor 示例证明等待空档和逐字问题在实时模型路线下同样存在。** openai-realtime-agents 的 README 自述：实时小模型负责寒暄与收集信息，文本主管模型负责工具调用和复杂回答，从“稍等我查一下”说完到真正答复开始约有 2 秒空档；README 没有逐字复述的强制机制。既然复杂回答必然要等强模型，把播报文本的控制权留在服务器才能让权限、金额、任务状态与可审计文本一致。
4. **管线模式把打断控制留在自己手里。** LiveKit Agents 文档说明：STT-LLM-TTS 管线下 `min_duration`、`min_words`、假打断恢复等参数全部可控；使用实时模型时打断处理转到供应商服务端，SDK 甚至拒绝关闭打断。主助理需要“用户附和不打断播报、真正插话立刻停”的精细控制，这只有管线模式能做到。

管线各段的具体选型写入配置，首版默认如下，切换不改业务代码：

| 段 | 默认 | 备选 | 选型依据与边界 |
| --- | --- | --- | --- |
| ASR 常听流 | `fun-asr-realtime` | `paraformer-realtime-v2` | WebSocket 长流，输出中间结果与最终句；`max_sentence_silence` 决定句末；支持热词；按音频秒计费。只有最终句形成 `input_turn_id`，中间结果只更新字幕 |
| ASR 按轮会话 | `qwen3-asr-flash-realtime` | 无 | 与 Demo 同一套 Realtime 事件协议，`server_vad` 可配 `threshold` 与 `silence_duration_ms`（官方对话场景建议 400ms 起）；固定 16k PCM；官方建议单会话累计音频不超过 60 秒，因此只能在每个用户轮次开新会话，不能当常听流 |
| TTS | `qwen3-tts-flash-realtime` 的 `commit` 模式 | `cosyvoice-v3-flash` | 每个已持久化的 `speech_text` 段执行 `input_text_buffer.append` 加 `input_text_buffer.commit`，收到 `response.audio.delta/done`，音频段与文本段一一对应。`server_commit` 模式由服务端自行断句，适合导航类低延迟场景，不用于权威内容。CosyVoice 用于需要音色克隆或指令控制时 |
| 固定短语 | 预合成缓存 | 无 | “收到”“正在确认”“有一个问题需要你回答”等模板一次合成、按文本 hash 缓存，首包零延迟；同样带 `playback_id`，可被用户插话取消 |

“严格”指使用已持久化的文本作为唯一内容源，并核对发送给 TTS 的文本及 `speech_revision`；不宣称任何合成器永远发音正确。审批选项、金额、目标、结果状态等必须有同版本可视文本。主模型先形成可独立成立的小段 `speech_text`，持久提交后才交给 TTS；可按段流式合成，不能在主答复尚可能撤回时播出“已完成”。

延迟预算分三段分别记录：ASR 句末判定到 final、主助理首段 `speech_text` 持久化、TTS 首包。只记录实测分布与设备型号，不在接入前给出数字承诺。

## 3. 轮次、打断与假打断

播放器状态机只有四个动作：暂停、确认打断并丢弃、假打断恢复、正常播完。规则来自 LiveKit 和 Pipecat 两套公开实现的共同做法，参数全部放服务器配置：

| 事件 | 处理 | 来源 |
| --- | --- | --- |
| VAD 检测到用户开始说话 | 立即暂停本设备播放，不丢弃缓冲；记录暂停位置 | Pipecat `VADUserTurnStartStrategy` 是最快的开始判定 |
| 机器人正在播报时的短促声音 | 需满足最少词数或转录非空才确认为打断；“嗯”“对”等附和不算 | Pipecat `MinWordsUserTurnStartStrategy`：机器人未说话时 1 个词即触发，说话时需要更多；LiveKit `mode="adaptive"` 过滤附和 |
| 确认打断 | 丢弃缓冲、递增 playback epoch、丢弃迟到的旧 epoch 音频；向 TTS 发取消 | 对应 OpenAI Realtime 的 `response.cancel` 加 `conversation.item.truncate` 语义 |
| 假打断 | VAD 触发但在超时内无有效转录，从暂停位置恢复播放 | LiveKit `false_interruption_timeout` 与 `resume_false_interruption` |
| 用户结束说话 | ASR 的静音阈值判定句末，final 才提交为用户轮次 | `silence_duration_ms` 或 `max_sentence_silence`；Pipecat `SpeechTimeoutUserTurnStopStrategy` 默认 0.6 秒 |

停止播放不会自动 abort 主助理或任何执行 Session。用户明确说“停止这个任务”才进入主设计第 5 节的任务控制流程。语音模型不存在于主助理链路，所以“弱语音模型抢答”不再需要防御：播放器只能播放已持久化的 `speech_text` 段和固定短语，没有第二个内容来源。

## 4. 播报队列、改口与断线

播报队列保存准确的消息 ID、文本 revision、关联 task/result、优先级和播放回执。最新要求到来后，旧答复可标记 superseded；尚未播放的旧计划被移除/更新，已播放部分无法撤回，要用简短更正说明。播报前再查 task revision，旧结果仍有历史价值时明确说“上一轮的结果”，不能当作当前最新进展。

多个设备同时在线时，默认最后明确启用语音的设备是播放 owner；其他设备更新文字和任务卡，不同时外放。设备切换增加 epoch，旧设备不能继续确认新播放。用户可手动指定设备；选设备不等于转移任务所有权。

断线后客户端按主会话事件游标补读，并读取当前 SQL 快照、未读答复和待答事项；不重发已接收的任务。如果 final 转录提交的应答丢了，先用同一 idempotency key 查询/重试，不能生成新轮次 ID。新通话有新的 voice session，但仍关联原主会话和未完成任务。

默认不长期保存原始音频。确需语音排错或回听时单独定义用户设置、保留期和删除链；文本转录按主会话隐私和来源版本管理。费用分三本账：ASR 按音频秒、TTS 按字符、主模型与执行任务按各自 token 与现有计费；Demo 当前的 usage 显示与新通话清零只属于 Demo 的独立通话模式，不影响主助理账本。

## 5. 设备能力核对清单

Web 桌面优先完成主会话、卡片和严格播报；Flutter 接相同 API/事件，再按 iOS/Android 的实际音频会话、后台权限与通知机制验证。每个平台都需验证前台、锁屏、后台、来电中断、耳机切换和网络切换。浏览器或系统不允许后台麦克风时，明确展示状态并依靠通知及重连补读，不能静默假装仍在聆听。这些是开发者在接入设备时的手工核对清单，不是独立的验收轮次。

## 6. 接入时才需要的数据、端点与界面（仅记录，不是计划）

| 表/记录 | 最少字段 | 约束与解释 |
| --- | --- | --- |
| `VoiceTurn` | voice_session_id, input_turn_id, asr_session_ref, final_revision, assistant_inbox_id, digest | 一个可提交 final 只接收一次；更正是显式后续命令，不偷偷改已执行输入 |
| `SpeechPlayback` | playback_id, device_id, epoch, assistant_message_id, speech_revision, state, last_segment | 不承担业务完成/用户批准语义；不要求保存音频二进制 |

端点：认证音频连接、轮次映射、播放确认；不另开业务命令体系。

界面：
- 麦克风按钮放在 `components/composer/ComposerActions.tsx`，与附件、资源按钮同一行同一样式；按下进入常听，再按停止。
- ASR 中间结果直接写进 `InputGroup` 的输入框作为实时字幕；final 到达时等价于用户按下发送，走同一 `onSubmit`。
- 播报状态用 `Topbar` 的 `statusSlot`，与 `CronStatusPill` 相同的位置和 `StatusPill` 样式：显示“播报中/已暂停/待播 n 条”与当前播放设备，点击停止本设备播报。
- 固定短语与分段 TTS 的音频只在客户端播放器里，不产生新的消息组件；`speech_text` 对应的文字就是 `AssistantTurn` 里已有的那条答复。

```
┌ Topbar ───────────────────────────── [▶ 播报中 · 本机] ┐
│ …                                                      │
│ [附件][@][🎤 常听中][模式：个人助理][模型 ▾] 口播先别讲架构… │
└────────────────────────────────────────────────────────┘
```

单测：final 才建轮次；VAD 暂停、确认后丢弃、假打断恢复；中间结果不产生 Inbox 项；epoch 递增后旧音频包被丢弃；超时内无转录则从暂停位置恢复。

## 7. 公开参考的适用范围

**阿里官方协议与模型文档**（在线文档，无仓库 commit SHA；具体模型支持矩阵和时序约束需接入时实测确认）：

| 文档 | 本文采用的事实 |
| --- | --- |
| [Qwen-Audio Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events) | 响应生成中不允许重复触发 `response.create`；`smart_turn` 下从 `speech_started` 到 `response.done` 之间禁止触发；适用 `qwen-audio-3.1-realtime-plus`、`3.0-realtime-plus`、`3.0-realtime-flash` |
| [Qwen-Audio Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events) | function call 通过 `response.output_item.added` 返回，且 function call 部分不送入 TTS 播报；`smart_turn` 独有 ambient 转录与 `turn_invalid` |
| [实时语音识别用户指南](https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide) | `fun-asr-realtime`、`paraformer-realtime-v2`、`qwen3-asr-flash-realtime` 等均为 WebSocket，输出中间与最终结果；`max_sentence_silence`/静音阈值决定句末；热词与情感识别为可选能力 |
| [qwen3-asr-flash-realtime 接入说明](https://help.aliyun.com/zh/model-studio/qwen-real-time-speech-recognition) | `server_vad` 的 `threshold` 与 `silence_duration_ms`（对话建议 400ms）或 `turn_detection: null` 手动 commit；固定 16k PCM；单会话累计音频建议不超过 60 秒 |
| [fun-asr-realtime 模型页](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/fun-asr-realtime) | 按音频秒计费，1200 RPM；支持大规模热词与语气词过滤；最新快照 2026-02-28 |
| [实时语音合成用户指南](https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide) | `qwen3-tts-flash-realtime`、`cosyvoice-v3-flash`、`cosyvoice-v3.5-flash` 等支持流式文本输入与流式音频输出；首包延迟含 WebSocket 建连 |
| [qwen-tts 实时合成交互流程](https://help.aliyun.com/zh/model-studio/interactive-process-of-qwen-tts-realtime-synthesis) | `server_commit` 由服务端判断合成起点，`commit` 模式必须显式 `input_text_buffer.commit` 才合成；事件为 `response.audio.delta/done`、`session.finish/finished` |

**OpenAI 与开源管线框架**（仅作架构背景，不移植代码，不等同阿里协议）：

| 文档 | 本文采用的事实 |
| --- | --- |
| [OpenAI Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations) | function calling 为 `function_call_output` 加 `response.create`；out-of-band 响应通过 `conversation: "none"` 加 `metadata` 并行生成而不改历史；打断用 `response.cancel`、`conversation.item.truncate`、`output_audio_buffer.clear` |
| [openai-realtime-agents](https://github.com/openai/openai-realtime-agents) | Chat-Supervisor：实时模型负责寒暄与收集信息，文本主管模型负责工具与复杂回答；README 自述转交空档约 2 秒，未提供逐字复述的强制机制 |
| [LiveKit Agents：Turns 与打断](https://docs.livekit.io/agents/build/turns/) | 管线模式可配 `min_duration`、`min_words`、`mode="adaptive"`、`false_interruption_timeout`、`resume_false_interruption`；实时模型下打断转到供应商服务端且不可关闭 |
| [Pipecat：User Turn Strategies](https://docs.pipecat.ai/server/utilities/turn-management/user-turn-strategies) | 开始判定有 VAD、转录、最少词数、唤醒词等策略，最少词数策略在机器人未说话时 1 词即触发；结束判定默认静音 0.6 秒；`enable_interruptions` 控制是否打断机器人 |

本文未将任一文档中存在某个事件解释为当前 Demo 已具备完整异步回注，也不以未固定版本的示例作为 OpenBox 接口证据。

## 8. 基线源码证据

| 现有入口与行号 | 可确认的事实 |
| --- | --- |
| [Demo relay：90、112、153、165、232](https://github.com/arkstudio-ai/OpenBox/blob/125ebc67c3ceb782c49117886e3fb9864201ac53/demos/realtime-voice/server.py#L90)；[播放器：48、134](https://github.com/arkstudio-ai/OpenBox/blob/125ebc67c3ceb782c49117886e3fb9864201ac53/demos/realtime-voice/app.js#L48) | 当前转录供字幕、直接音频转发、smart_turn 与本地打断；尚无业务 tools/可靠结果回注 |
| [Demo 模型与上游：24、26](https://github.com/arkstudio-ai/OpenBox/blob/125ebc67c3ceb782c49117886e3fb9864201ac53/demos/realtime-voice/server.py#L24) | 固定 qwen-audio-3.1-realtime-plus 直连 dashscope realtime；主助理语音链路不复用此连接 |
