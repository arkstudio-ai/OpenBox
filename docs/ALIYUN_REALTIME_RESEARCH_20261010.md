# 阿里云实时语音模型接入与实测

核对日期：2026 年 10 月 10 日，北京时间。面向 OpenBox 的语音通话、工具执行和长期对话接入。

**两款实时对话模型的基础能力均已跑通；本次实测不足以判定 OpenBox 正式通话应优先选择哪一款。** 两款模型的工具调用、上下文增删与摘要恢复、语音输入输出、VAD、取消回复、音色选择和声音克隆均已实际调用，但尚未在完整主助理链路中对比延迟工具结果回注、真实麦克风下的误打断率和主观听感。精确控制语速、音高和音量的播报需求，可以单独使用 Qwen3 TTS Instruct。自然语言控制语速的效果有明显差异，不能把收到配置确认当作效果已经实现。

完整证据见 [实测记录](evidence/aliyun-realtime-research-20261010.json)，复跑入口为 [probe_aliyun.py](../demos/realtime-voice/probe_aliyun.py)。测试共记录 46 个场景条目（含素材生成），保留 50 份事件日志和 40 个 WAV 文件。全部输入为合成测试内容。

## 模型选择

| 模型 | 适用场景 | 主要区别 |
| --- | --- | --- |
| `qwen3.8-omni-flash-realtime` | 语音助手、带视觉信息的实时交互 | 文本、图像、音视频输入；文本与语音输出；Function Calling、远程 MCP、联网搜索；语义检测名为 `semantic_vad` |
| `qwen-audio-3.1-realtime-plus` | 专注语音的助手、客服、双工通话 | 文本和音频交互；Function Calling、联网搜索；语义检测名为 `smart_turn`，另有说话人增强配置 |
| `qwen3-tts-instruct-flash-realtime` | 已有文案的流式播报 | 输入文本、输出声音；支持数值语速、音高、音量和表达指令；对话推理和工具执行需要另行承担 |

项目的独立体验页使用第二款，正式语音后端使用第一款。二者的事件名称相近，音色、轮次检测和上下文接口仍需分别适配。[Omni 指南](https://help.aliyun.com/zh/model-studio/realtime)、[Qwen Audio 指南](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)、[实时语音合成指南](https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide)

正式版选型来自 [2026-10-07 接入方案](PERSONAL_ASSISTANT_VOICE_PLAN.md)第 1.3、2 节：当时验证了 Omni 的挂起工具、期间继续聊天、等空闲后续答，方案同时把 Audio 3.1 描述为“没有可靠的业务工具回路”。该方案列出的实验没有对 Audio 3.1 做同等验证；此前 [2026-10-03 参考记录](PERSONAL_ASSISTANT_VOICE_REFERENCE.md)主要根据响应时序限制排除了实时模型路线。本次 Audio 3.1 的基础工具调用、结果回填与语音续答成功，说明不能据此认定它不支持工具回路；其在 OpenBox 完整异步业务场景中的表现仍需同条件验证。正式版已经使用 Omni 是实施现状，不能单凭这一点推荐继续选用它。

Omni 3.8 官方列出 113 种语种及方言的识别、36 种语种及方言的生成。本次验证使用 WebSocket；WebRTC 的媒体走 RTP，不能照搬 WebSocket 的音频 append 流程，且 Omni WebRTC 不支持手动提交模式。[Omni 接入说明](https://help.aliyun.com/zh/model-studio/realtime)

**接入协议不是两款模型的独占差异。** 重新联网核对后，Realtime API 的型号支持表明确列出两者均支持 WebSocket、WebRTC 和 AOQ；Audio 用户指南也明确支持三者。部分模型总览仅写 WebSocket，不能据此断言 Audio 不支持另外两种协议。[协议支持矩阵](https://help.aliyun.com/zh/model-studio/realtime-api-overview)、[Audio 接入指南](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)

从已公开的接口看，Omni 的主要区别是图像/视频输入、1/2/4 声道音频和服务端原生 MCP；Audio 3.1 则明确提供目标说话人增强、环境音转写与情绪增强开关。两者都支持语义轮次检测，不能把过滤附和声归为 Audio 独有。Audio 的工具接口目前列出 Function Calling，没有列出与 Omni 相同的服务端 MCP 配置；应用仍可自行把 MCP 工具包装成函数。[Omni 模型能力](https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime)、[Audio 客户端参数](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)、[Omni 客户端参数](https://help.aliyun.com/zh/model-studio/client-events)

本轮未找到两款指定实时型号在同一条件下的直接通话评测。Audio 3.1 技术报告包含工具使用、背景人声、附和与打断评测，但其表格没有 Omni 3.8 对照组；文中的长期任务 Voice Harness 原型使用 Audio 3.0 前台，也不能当成 Audio 3.1 在 OpenBox 异步任务场景的验证。[Audio 3.1 技术报告](https://arxiv.org/html/2609.25176v2)

## 连接和会话配置

新接入按官方要求使用与 API Key 地域、业务空间对应的地址：

```text
wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model={model}
wss://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime?model={model}
Authorization: Bearer <DASHSCOPE_API_KEY>
```

此次没有发现已配置的业务空间 ID，使用项目原有的北京兼容域名 `dashscope.aliyuncs.com` 完成调用。**这个账号的成功结果不代表新账号都能依赖旧域名**；Omni 3.8 当前指南明确要求业务空间域名。[Omni 连接要求](https://help.aliyun.com/zh/model-studio/realtime)、[Audio WebSocket 接入](https://help.aliyun.com/zh/model-studio/fun-audiochat-realtime-websocket-api)

以下为此次成功使用的 Omni 兼容配置；输入为无文件头的 16 kHz、16 bit、单声道 PCM，输出按 24 kHz PCM 保存：

```json
{
  "type": "session.update",
  "session": {
    "modalities": ["text", "audio"],
    "voice": "Tina",
    "instructions": "你是中文语音助手。简短回答，工具结果到达后再报告执行结果。",
    "input_audio_format": "pcm",
    "output_audio_format": "pcm",
    "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
    "turn_detection": {
      "type": "semantic_vad",
      "threshold": 0.5,
      "silence_duration_ms": 600
    }
  }
}
```

新接口推荐将音色放在 `session.audio.output.voice`，格式放在 `audio.input.format` 和 `audio.output.format`。官方说明嵌套音色字段优先于兼容的 `session.voice`；本次脚本验证的是上述兼容字段。[Omni 客户端事件](https://help.aliyun.com/zh/model-studio/client-events)

Audio 3.1 将音色换为 `longanqian_v3.1`、检测类型换为 `smart_turn`。其转录语言可设 `input_audio_transcription.language="zh"`，合成语言可设 `output_audio.language="zh"`；后者不等于强制模型用中文写回复。这两个配置和 `enable_speech_emotion=true` 已获得确认并生成音频。[Audio 客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)

两款模型的握手默认回包此次都出现了 `Chelsie`，与各自当前音色表不同。接入时应显式配置音色，等 `session.updated` 后再送输入，并以第一段真实音频验证可用性。

## 工具调用和联网

两款模型均完成了同一个实测闭环：请求计算 17 加 25，收到 `add_numbers` 和 JSON 参数，本地函数算得 42，回填后模型正确回答。这里只执行了测试加法函数。

工具定义采用嵌套的 `function` 对象：

```json
{
  "type": "function",
  "function": {
    "name": "add_numbers",
    "description": "计算两个整数之和",
    "parameters": {
      "type": "object",
      "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
      "required": ["a", "b"]
    }
  }
}
```

在 `session.tools` 注册后，处理顺序为：

1. 累积 `response.function_call_arguments.delta`，以 `done` 事件中的完整参数为准。
2. 校验函数名和参数，执行本地工具，保存服务端给出的 `call_id`。
3. 写入 `conversation.item.create`，其中 `item.type="function_call_output"`、`call_id` 原样回填、`output` 为序列化结果字符串。
4. 等前一条 Response 结束，再发送一次 `response.create` 续答。多个工具调用需分别关联结果。

模型生成工具请求不代表业务动作已完成。结果回填、失败信息、重复执行防护和用户插话后的任务处理，应由应用管理。[Omni 工具流程](https://help.aliyun.com/zh/model-studio/omni-realtime-interaction-process)、[Audio 工具示例](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)

**Omni 远程 MCP 也已实际完成发现、调用和续答。** 测试使用 [Cloudflare 官方文档 MCP](https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/)，观察到 `mcp_list_tools.completed` 和 `response.mcp_call.completed`。MCP 由百炼服务端执行，客户端无需重复回填 `function_call_output`；父 Response 结束后发起续答即可。正式业务按工具权限设置审批；此次免审批配置只用于公开文档读取。[MCP 交互说明](https://help.aliyun.com/zh/model-studio/omni-realtime-interaction-process)

**联网搜索在两款模型均有真实调用证据**：`response.done.usage.plugins.search.count=1`，并返回 `search_info.search_results`。官方要求 `enable_search` 与 `tools` 分开使用。本次把两者同时配置也收到了 `session.updated`，但没有验证组合运行；应用仍应按官方互斥规则处理，不能靠配置回包推断兼容性。[Omni 搜索配置](https://help.aliyun.com/zh/model-studio/client-events)、[Audio 搜索配置](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)

## 上下文管理

| 操作 | Omni 3.8 实测 | Audio 3.1 实测 |
| --- | --- | --- |
| 插入文本并跨消息回忆 | 正确回忆测试口令 | 正确回忆测试口令 |
| `session.update.instructions` | 后续回答遵守新指令 | 后续回答遵守新指令 |
| 客户端指定 `item.id` | 服务端重新分配 ID | 保留指定 ID |
| `conversation.item.retrieve` | 返回 `invalid_value` | 返回指定消息 |
| `conversation.item.delete` | 返回删除确认 | 返回删除确认 |
| 删除包含事实的全部测试消息 | 回答“不知道” | 回答“不知道” |
| 新连接注入历史摘要 | 正确恢复颜色和项目代号 | 正确恢复颜色和项目代号 |

因此本地消息表必须记录**服务端确认的 ID**。只删用户原话不一定足以消除某个事实：助手回答、工具结果或摘要中也可能含有它。此次删除测试同时移除了相关用户消息和助手答案。Audio 的创建、查询、删除接口有明确文档支持；Omni 的上述增删结果以本次观测为依据。[Audio 上下文接口](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)

官方容量边界如下，属于文档限制，未做上限压力测试：

| 项目 | Omni 3.8 | Audio 3.1 |
| --- | --- | --- |
| 音频历史 | 最多 100 轮、累计 600 秒 | 默认 20 轮，可通过 `max_history_turns` 设为 1–50；累计 300 秒 |
| 视频历史 | 最多 50 轮、累计 240 秒图像帧 | 本次语音链路不涉及 |
| 最大输入 | 196608 Token | 245760 Token |
| 最大输出 | 65536 Token | 16384 Token |
| 连接时长 | 最长 120 分钟 | 本次未做长连接压力测试 |

Audio 3.1 型号页另列总上下文长度为 262144 Token。其最大输入比 Omni 3.8 Realtime 更大，音频历史却更短，因此不能笼统说某一款“上下文更大”。不要将非实时 `qwen3.8-omni-flash` 的 1M 上下文套用到实时型号。[Audio 3.1 型号页](https://help.aliyun.com/zh/model-studio/qwen-audio-3-1-realtime-plus)、[Omni 3.8 Realtime 型号页](https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime)、[型号区别](https://help.aliyun.com/zh/model-studio/omni/)

历史音频保留时长和通话持续时长是两个概念。模型可能在连接仍然存活时淘汰早期内容。[Omni 使用限制](https://help.aliyun.com/zh/model-studio/realtime)、[Audio 历史轮次](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)

对 OpenBox 的接入建议是：应用保存完整事件及必要业务状态，会话中保留近期内容；旧事实压缩为短摘要，在无活动 Response 时更新或重建会话。未完成的工具调用和输出应成对保留。此次已经证明摘要可以恢复信息，但没有证明供应商会自动生成摘要或提供跨连接长期记忆。

## 语速语调和情绪

语音人格可以通过 `instructions` 描述；精确的声音参数应区分实时对话和专用 TTS。

相同 42 字符文案，明确要求逐字朗读，并分别给正常、慢速、快速指令，得到以下音频时长。每格为一次成功样本，不是质量基准：

| 模型与音色 | 正常 | 慢速提示 | 快速提示 |
| --- | ---: | ---: | ---: |
| Omni 3.8，Tina | 6.80 秒 | 8.56 秒 | 6.88 秒 |
| Audio 3.1，longanqian_v3.1 | 7.60 秒 | 7.60 秒 | 7.60 秒 |

Omni 的慢速提示在此样本上拉长了时长，快速提示未比正常明显加速。Audio 的三次时长一致，不能据此宣称已实现语速控制。更早只传文案、依靠系统指令要求朗读时，Omni 有时直接回应文案含义；需要逐字输出的播报应优先用 TTS。

专用 `qwen3-tts-instruct-flash-realtime` 的数值控制已调用：

| 配置 | 同文案结果 |
| --- | --- |
| `speech_rate=0.7` | 13.310 秒 |
| `speech_rate=1.0` | 7.600 秒 |
| `speech_rate=1.3` | 6.366 秒 |
| `pitch_rate=0.8, volume=30` | 参数确认并返回音频；未做基频或响度的独立评价 |
| 开心语气 `instructions`，`optimize_instructions=true` | 参数确认并返回音频；未做情绪盲听评分 |

该 TTS 的语速和音高范围均为 0.5–2.0，音量范围 0–100。表达指令功能限定于 Instruct 系列，不能把这套字段直接当作所有 Realtime 对话模型的能力。Audio 另有 `enable_speech_emotion` 开关，与指定精确语速是不同功能。[TTS 客户端参数](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-client-events)、[Audio 情绪参数](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)

TTS 使用 `input_text_buffer.append → input_text_buffer.commit` 触发合成；结束时发送 `session.finish`。本次收到了音频、`response.done` 和 `session.finished`。[TTS 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-server-events)

## VAD 和打断

| 模式 | 实际使用方法 | 验证情况 |
| --- | --- | --- |
| 手动模式 | `turn_detection=null`，追加音频、commit，再 response.create | 两款均完成识别、文本回复和音频输出 |
| 声学检测 | `server_vad` | 两款均返回开始、结束、自动回复事件 |
| Omni 语义检测 | `semantic_vad` | 正常轮次和插话取消均成功 |
| Audio 语义检测 | `smart_turn` | 正常轮次和插话取消均成功 |
| 主动取消 | `response.cancel` | 两款均返回 `status=cancelled`，原因为 `client_cancelled` |

声学检测通过 `threshold` 控制灵敏度，通过 `silence_duration_ms` 控制结束等待。Audio 的这两个参数只对 `server_vad` 有效；不能给 `smart_turn` 调低静音时间就假定语义轮次变快。[Audio 模式约束](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)

测试将合成语音转为 16 kHz PCM，以每包 100 ms 的速度发送。Omni 的纯音频插话测试出现两次取消后续答，插入的一句话也被拆成两个有效轮次；它证明了打断链路有效，同时说明自然停顿仍需要场景调优。Audio 本次出现一次取消后续答。两者都没有经过真实麦克风、扬声器和回声环境测试。

客户端建议保留以下处理：

1. 收到 `speech_started` 即停止当前播音、清空已排队音频，并隔离旧 `response_id` 的晚到数据。
2. 区分“服务端生成结束”和“设备已经播放完”。已收到的音频不会因服务端取消而自动从扬声器队列消失。
3. 对 `smart_turn` 的 `speech_stopped.reason=turn_invalid` 撤销临时说话状态，不把噪声当作有效用户请求；环境转写不应直接写进业务对话。
4. 工具请求、工具结果、Response 和播放队列分别管理；用户插话时，不应盲目重发可能有副作用的工具。

`turn_invalid` 和环境转写属于文档支持，本次没有做“嗯啊”、多人噪声或误打断率测试。说话人增强使用首次配置的 `turn_detection.voiceprint_audio_urls`，需关注异步注册完成/失败事件；本次没有测其辨人效果。[Audio 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events)、[双工交互说明](https://help.aliyun.com/zh/model-studio/fun-audiochat-realtime-websocket-api)

**不要直接移植其他厂商的 `conversation.item.truncate` 行为。** 本次两款模型在收到该请求后的 8 秒观察窗口内均未返回确认或错误，不能确认截断已生效。取消生成已验证，按播放位置修改历史音频尚未验证。

## 音色选择和声音克隆

Omni 3.8 的官方默认音色为 `Tina`；此次 Tina 和 Andre 均实际出声。其他候选可从模型对应的 [Omni 音色表](https://help.aliyun.com/zh/model-studio/omni-voice-list) 试听选择，不能把旧 Omni 或 TTS 的音色名称直接混用。

Audio 3.1 的默认音色为 `longanqian_v3.1`；此次它与 `longanhuan_v3.1` 均实际出声。**音色最好在首条 session.update 就确定。** 本次 Audio 已出声后再修改 voice 返回了 `invalid_value`，与文档所述后续配置被忽略的表现略有差异；都意味着应用不能依赖会话内改音色。Omni 同样操作获得更新确认并继续生成音频。[Audio 音色规则](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)

两款模型都已完成创建克隆音色后生成音频，但使用的创建协议不同：

| 项目 | Omni | Audio Realtime |
| --- | --- | --- |
| HTTP 路径 | `/api/v1/services/audio/tts/customization` | 相同 |
| `model` | `qwen-voice-enrollment` | `voice-enrollment` |
| `input.action` | `create` | `create_voice` |
| 命名字段 | `preferred_name` | `prefix` |
| 样本字段 | `audio.data`，支持 Data URL | `url`，由服务端下载 |
| 绑定 | `target_model` 填后续使用的 Omni 模型 | 填后续使用的 Audio 模型 |
| 返回音色字段 | `output.voice` | `output.voice_id` |
| 本次额外验证 | 列表能查询到创建的音色 | 详情状态变为 `OK` 后出声 |

Omni 3.8 创建时还需 `parameters.voice_clone_mode="normal"`。官方建议样本 10–20 秒，单声道、至少 24 kHz，清晰连续人声；不要把“创建成功”当作相似度评价。[Omni 克隆 API](https://help.aliyun.com/zh/model-studio/qwen-omni-voice-cloning)

本次使用同一段 12.16 秒的合成 Tina 音频：Omni 通过 Data URL 提交；Audio 通过项目 OSS 的临时签名 URL 读取。Audio 的详情查询和 Omni 的列表查询也已调用。[Audio 克隆说明](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)、[创建与查询协议](https://help.aliyun.com/zh/model-studio/voice-clone-design-http-api)

账户中保留了两个新测试音色，便于后续体验：

```text
Omni: qwen-omni-vc-openbox_probe-voice-20261010104110101-815a
Audio: qwen-audio-3.1-realtime-plus-obprobe-4a6020cae38e4abdaa948c7c917e3f5b
```

OSS 中保留一个约 0.58 MB 的合成参考文件，路径见证据中的 `reference_object_key`。签名 URL 没有写入报告或日志。这里验证的是复刻接口和输出链路，未评价真人相似度。

## 价格和运行边界

北京区当前公开价格，单位为元/百万 Token：

| 模型 | 文本输入 | 音频输入 | 文本输出 | 音频输出 |
| --- | ---: | ---: | ---: | ---: |
| Omni 3.8 Flash Realtime | 1.5 | 6 | 4.5 | 12 |
| Audio 3.1 Realtime Plus | 5 | 40 | 40 | 150 |

Qwen3 TTS Instruct Realtime 按输入字符计费，北京区为 1 元/万字符，输出不另计费。历史上下文会重复计入后续轮次输入；音频流已经生成但未播放，不应简单视为免费。费用应以 `response.done.usage` 的分类明细核算并保留缺失用量状态，本次取消响应即存在未返回 usage 的情况。[官方价格](https://help.aliyun.com/zh/model-studio/model-pricing)

此次出现过连接握手超时、一次 Omni 模型内部错误和一次流输出中途无进展。重试后的核心功能成功不代表已完成稳定性测试。生产接入应为建连、首包、工具执行和回合完成分别设超时；重连后恢复摘要，而不是假设旧会话仍存在。

本次未验证 WebRTC/AOQ、视觉输入、多声道、长连接上限、最大上下文淘汰效果、真实设备播放延迟、回声消除、噪声下误打断率和音色情绪的听感评分。

## 复跑与试听

在仓库根目录使用现有 Python 环境，按阶段运行。请求使用 `DASHSCOPE_API_KEY`；缺省读取 `backend/.env`。下面每条都会实际请求百炼并产生相应计费。

```bash
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase basic
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase tools
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase voices
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase style
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase vad
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase extra
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase tts
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase clone --model omni
backend/.venv/bin/python -B demos/realtime-voice/probe_aliyun.py --phase clone --model audio --upload-reference
```

可用 `--case omni_interrupt` 等参数限定单个场景。默认输出目录为 `test-results/aliyun-realtime-20261010`；已有创建记录时会复用克隆音色。Audio 的首次克隆需要 `--upload-reference`，它只上传脚本生成的参考音频。音频转换依赖 ffmpeg。多个进程应使用不同 `--out`，避免覆盖汇总。

本次原始结果、脱敏事件和音频在上述目录；经过逐事件复核的结论在 [证据 JSON](evidence/aliyun-realtime-research-20261010.json)。原始组合测试中的 truncate 超时和一次带空格文本的严格匹配失败，已在证据中解释，未隐藏原始日志。

精选试听：

- [Omni Tina 正常](../test-results/aliyun-realtime-20261010/omni_voice_Tina_normal_b31ebd76-00.wav) 与 [慢速提示](../test-results/aliyun-realtime-20261010/omni_voice_Tina_slow_b31ebd76-00.wav)
- [TTS 0.7 倍语速](../test-results/aliyun-realtime-20261010/tts/audio_tts_slow-00.wav) 与 [1.3 倍语速](../test-results/aliyun-realtime-20261010/tts/audio_tts_fast-00.wav)
- [Omni 克隆音色](../test-results/aliyun-realtime-20261010/omni_voice_qwen-omni-vc-openbox_probe-voice-20261010104110101-815a_normal_38814090-00.wav)
- [Audio 克隆音色](../test-results/aliyun-realtime-20261010/audio_voice_qwen-audio-3.1-realtime-plus-obprobe-4a6020cae38e4abdaa948c7c917e3f5b_normal_7a652d6c-00.wav)
