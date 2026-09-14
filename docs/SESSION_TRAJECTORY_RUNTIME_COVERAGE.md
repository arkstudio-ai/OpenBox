# 会话轨迹运行链覆盖与证据

日期：2026-09-11。代码位于独立 worktree `OpenBox-trajectory`，尚未提交、合并或部署。本文件只说明已经接入的生产代码和实际测试证据，不将计划项或外部替身结果写成线上观测。

范围是输入、Agent、模型、工具和后台执行链。产品字段见 [UI 规格](SESSION_TRAJECTORY_UI_SPEC.md)，协议见 [协议 v1](SESSION_TRAJECTORY_PROTOCOL.md)，权限、存储、多进程与前端总体验收见 [实现状态](SESSION_TRAJECTORY_IMPLEMENTATION_STATUS.md) 和 [存储验证](SESSION_TRAJECTORY_STORAGE_VERIFICATION.md)。

## 1. 观测层级与证据口径

| 名称 | 实现中的含义 | 不包含的内容 |
|---|---|---|
| `capture_level=provider_wire` | 本应用直接构造并提交的最终 HTTP 业务 body，以及实际读取的 JSON 响应或 SSE 事件；白名单和受控媒体引用作用于录制副本 | HTTP 传输字节、签名头、重定向过程、provider 内部重试和服务端内部执行 |
| `capture_level=adapter_input` | 参数组装完成后传给 LiteLLM、OpenAI 或 Tea SDK 的公开输入，以及 SDK 实际返回的公开对象/chunk | SDK 继续变换后的 HTTP body、SDK 内部网络尝试和未返回的私有状态 |
| 业务事实事件 | `input.*`、`run.*`、`tool.*`、`job.*` 等在实际业务入口显式记录 | 不额外赋予模型 `capture_level`，不把业务终态反推为未经观察的模型网络调用 |

所有模型 Request 均标明 `sdk_internal_attempts=not_observed`。除下述显式脱敏控制事件外，一条 `request.delta` 对应本层实际观察到的 chunk 或完整非流式响应；分块内容包括可见文本、provider 返回的 reasoning、工具参数片段及原始公开响应。仅在最终响应才出现的完整块使用 `mode=replace`，不拆成虚构的 token 流。没有返回的内部推理不生成记录。

流式文本在进入 Recorder 队列前，按 Request / Block 分别维护凭据脱敏状态。实际参数或正文片段在 raw 中可以表现为明确的安全 Block 引用，避免 raw 形成未脱敏副本；非流式业务响应仍保留公开字段。EOF 释放无敏感的暂存前缀时，单独使用 `redaction_control=finalize`、`source=recorder_redaction`，附最后实际 chunk 数，不把它计为额外 provider chunk。工具累计输出及完整结果使用每次 Call 独立的相同脱敏机制，执行器返回值不被修改。

下文区分三类测试证据：

- **应用链**：真实 ASGI 输入路由、Session/Message/Part 事务、运行租约、Agent loop、Recorder 和 SQLite；模型 SDK、沙箱及配置发现使用确定性替身。
- **适配器链**：真实参数构造和适配器，使用 `httpx.MockTransport`、模拟 SSE 或 SDK client 替身；是否启用真实轨迹数据库逐项注明。
- **局部契约**：调用 helper、工具执行器或模拟 worker；不能替代完整应用入口、真实进程重启或付费云服务。

## 2. 身份、输入与持久化边界

代码入口：[context.py](../backend/trajectory/context.py)、[session.py](../backend/session/session.py)、[question/runtime.py](../backend/question/runtime.py)、[loop.py](../backend/agent/loop.py)、[trajectory.py](../backend/agent/trajectory.py)。

| 层级 | 身份来源及已实现行为 | 对应证据 |
|---|---|---|
| 用户、根会话 | `user_id` 是执行对象的实际所有者；`session_id` 是轨迹根，`source_session_id` 是实际执行会话。冻结的 `TraceContext` 经 `derive` 派生，不能原地改变父上下文 | E01、E03、E10、E11 |
| 用户输入 / Turn | `create_user_message` 在输入事务内保存 baseline、`input.accepted`、兼容 Message/Part 和 `SessionExecution.trace_context`；普通新输入使用消息 ID 作为 Turn ID，委派/压缩等 synthetic 输入继承所属上下文 | E01、E05、E06、E10 |
| Run / Generation | `question.runtime.start_run` 根据持久租约生成实际 Run ID 和 `run_generation`，`get_run_trace` 交给 loop；`run.started/finished` 由 runtime 记录。主根 Run 完成时收束对应 Turn；cron 有自身 Turn 收束入口 | E01、E03、E07、E11；问答补充见 E14 |
| Step / Request | loop 的每个推理步骤有独立 Step ID；每次实际适配器派发生成独立 Request ID，记录 attempt、previous/parent request。工具内请求清空自身 `call_id`，以 `parent_call_id` 保留触发工具 | E01、E03、E08、E16、E17、E18 |
| Tool / 子 Agent | 每次工具调用独立 Call ID；batch 和 task 派生新上下文。task 保留父 Agent、父 Call、根 Turn，子会话有独立 Run/Generation | E03、E09、E10 |
| 延迟回调 | 问答、cron 和 VideoJob 读取提交时持久保存的上下文，不从当前最新消息推断旧执行身份；媒体还持久保存可观察 dispatch 的 Request ID 列表 | E11、E12、E13、E14、E16、E18 |

`save_part`、`update_part_data`、消息更新和记录辅助方法将兼容投影与事实追加放入同一事务；plan Part 变更另记 `plan.changed`。请求开始的短事务包含 `ensure_trajectory`、请求媒体引用规范化、`request.prepared` 和 `request.started`，提交成功后才派发模型。输入附件、基线和工具资产先在事务外准备字节，再在写事务内校验所有权并保留版本。

活跃请求、Step 和 Tool 使用源会话的运行租约做代际校验。已完成 Run 的 title/suggestions 只在 generation 未变化时允许继续记录；陈旧 generation 的兼容 Part 不能落库。终态及明确的延迟回调可以沿原身份记录事实，不将已结束 Run 复活。

## 3. 模型出口逐入口覆盖

### 3.1 主聊天与原生工具发现

| 实际入口 | 采集层级和输入 | 终态、错误与重试 | 证据及限制 |
|---|---|---|---|
| [llm.py](../backend/agent/llm.py) `_stream_litellm_direct` → `litellm.acompletion` | `adapter_input`，在最终 `call_kwargs` 完成后记录 system/messages、工具 schema、tool choice、采样/输出/推理参数等白名单 | 每个实际 SDK chunk 在交给消费者前等待 durable receipt；记录 observed usage、finish、取消和失败。loop 发起的新尝试有新 Request ID | E01 应用链含两个主请求；E03 多层 task；E08 局部流式/背压/取消契约 |
| [llm.py](../backend/agent/llm.py) `_stream_responses_api` 的直接 HTTP 分支 | `provider_wire`，记录实际 `input`、`tools`、推理和输出参数；逐条读取 SSE JSON，在业务归一化前捕获 | 非 200 记录失败；流异常/协议错误记录已观察的部分输出和失败；最终完整块以 replace 记录 | E32 使用真实输入、租约、`trace_ctx`、Recorder 与 SQLite，核对实际 body、两个 SSE chunk 和最终投影；HTTP 是替身。E15 补充协议错误契约 |
| 同一 Responses 分支的 native tool search | `provider_wire`，含最终 `tool_search`、`defer_loading` 与实际 provider schema；公开 search/reveal/call 按实际事件处理 | 校验 reveal 顺序、调用关联、schema 不变性以及匹配的 `response.completed`；不接受尚未审核的工具执行 | E32 `[native]` 验证持久请求及公开发现/调用事件；E15 验证 wire、事件顺序与关闭条件；并非真实 provider 服务端发现测试 |
| Native → portable fallback | 原 native 与 portable 各为独立 Request；`request.route_changed` 记录路线变化。portable 请求记录其实际重新组装后的 input/tools | 仅首个 SSE 之前明确 unsupported 可以降级一次；出现 SSE 后不重放。`TrajectoryError` 不作为模型不支持来触发 fallback | E32 `[fallback]` 证明两个实际派发与两个独立 prepared/started/finished，前次失败、后次完成、route/previous Request 关联；E15 验证不重放，E08 验证开始录制失败不派发 |

输入白名单排除 API key、Authorization、cookie、token、SDK 私有状态和 provider replay 等；未知字段只列名称到 `omitted_fields`，不复制值。`extra_body` 也采用白名单。普通图片编码过程记录 SHA→原资产的显式映射，录制副本中的 base64/派生媒体交由受控 payload 处理，实际发给 SDK 的 body 不被替换。

Schema 单独识别：名为 `password`、`api_key` 的属性仍保留类型、required、长度等输入约束，其实际 default/examples 值脱敏。`request.prepared` 与该调用的 `tool.requested` 引用同一份最终 provider schema；不能因属性名称敏感而删掉定义、留下失效的 required。E35 验证实际参数仍交给执行器，录制副本保留约束且没有示例、默认值、实参或 SDK 私有数据。

### 3.2 辅助模型与工具内模型

流式辅助调用统一经过 `stream_llm`：最终走 LiteLLM 时为 `adapter_input`，最终走直接 Responses 时为 `provider_wire`。下表的非流式调用单独注明。

| 入口 / `purpose` | 已捕获输入与身份 | 终态及故障行为 | 证据及未观察项 |
|---|---|---|---|
| [loop.py](../backend/agent/loop.py) `_ensure_title` → `_generate_title_with_llm` / `title` | 最终标题 prompt、模型及 temperature，经 `metered_completion`，`adapter_input`；使用该会话及调用时绑定的执行上下文 | 完整响应、usage、既有账单、失败/取消；普通模型失败仍可使用截断标题，`TrajectoryError` 透传 | E33 经实际标题入口保存生成标题；主 Run 已结束后仍归原会话/Turn/Run/Generation，SDK 一次、原账单及关联结算各一次。SDK 为替身 |
| [suggestions.py](../backend/agent/suggestions.py) / `suggestions` | 实际建议上下文、结构化输出工具和 `tool_choice=required`；保持已完成主 Run 的原身份 | 建议 Part 的 pending/最终状态与事务记录；生成超时或格式错误清理 pending。结构化响应仅作为建议答案，不实际派发工具 | E01 应用链真实触发一次建议请求；E07 验证 Run 结束后仍可写同代际请求 |
| [compaction.py](../backend/agent/compaction.py) `_chunked_summarize` / `compaction_chunk` | 每块最终摘要输入独立 Request；属于实际压缩上下文 | 各块请求都有已观察响应/终态；普通块摘要失败有既有处理，录制故障透传 | E04 真实应用链触发两个块摘要 |
| `process_compaction` / `compaction` | 最终汇总 prompt、块摘要、实际所保留上下文；summary Message/Part 关联对应 Request | 成功才应用 summary boundary；失败为 `applied=false`，取消有 compaction 终态；`context.replaced` 随实际边界更新提交 | E04 真实摘要、prune、上下文替换及续聊；**未做完整 ASGI 压缩失败/取消专项** |
| [cron/executor.py](../backend/cron/executor.py) `_generate_summary` / `cron_summary` | 目标会话实际历史的最终摘要消息；按 CronRun 保存的目标根上下文调用 `stream_llm` | 普通摘要失败沿用既有处理，`TrajectoryError` 不被吞后继续外部执行 | E11 验证上游上下文，但替换了摘要 helper；**没有真实 cron 摘要 provider 采集整链测试** |
| [bash.py](../backend/tool/bash.py) 空闲判断 / `bash_judge` | 实际判断 prompt 经 `metered_completion`，`adapter_input`；继承父 Tool Call，独立模型 Request | 完整判断响应/usage；录制异常直接透传，不误当普通判断失败而 kill 或重复执行 Bash | E19 对判断/输出两处注入录制故障；没有单独的成功判断 + 真实账本轨迹专项 |
| [video_analyze.py](../backend/tool/video_analyze.py) `_complete` / `video_analyze` | 最终 vision/文本消息、模型参数，`adapter_input`；复制媒体上下文，帧和音轨关联原视频 | 完整 SDK 响应、usage、finish/失败/取消；`capture_billing` 引用原 UsageMeter，不追加第二次收费 | E20 实际 `_complete` + SQLite 账本，SDK 替身；ASR 故障不得继续 vision 派发的代码已接入 |
| [image_gen.py](../backend/tool/image_gen.py) `_call_provider` / `image_generation` | OpenAI SDK generate/edit 前的最终 prompt、n、size、quality、格式、背景及图片/mask 版本清单，`adapter_input` | 响应记录公开描述/摘要；真正输出图片经 `artifact.recorded` 关联同 Request；引用既有图片账单，异常/取消有终态 | E21 真实 image edit 业务入口 + SDK 替身 + 轨迹/资产/账本 DB；此用例没有调用 generate 分支 |

### 3.3 四路视频、两路 ASR 和 IMS

共同入口为 [trajectory.py](../backend/agent/trajectory.py) 的 `service_scope`、`capture_service_dispatch`、`capture_http_response` 与 `observe_service_response`。工具执行时使用该 Call 的上下文；后台恢复使用 VideoJob 保存的提交上下文。每个实际 submit 先把 Request ID 追加到该 Job 的持久列表，再进行外部派发。

录制请求位于 `request.prepared.data.input.input`：包含 `operation`、`business_body`、`media_inputs`、`job_id` 和遗漏字段名称。这里双层 `input` 分别是 Request 快照及该快照中的业务输入字段。实际返回体保存在 `request.delta.data.raw.response.output`，HTTP 状态在 `response.status`。这些路径是当前代码字段，不另造平行 schema。

| 路线 / 实际函数 | `capture_level` | 最终参数捕获位置与内容 | 终态与测试 |
|---|---|---|---|
| Ark 原生 / [video_production.py](../backend/tool/video_production.py) `_provider_submit` | `provider_wire` | `POST /api/v3/contents/generations/tasks` 前的实际 content、model、resolution、ratio、duration、generate_audio、watermark、seed 等白名单 | 提交响应成功为 `completed/accepted`；E16 `[ark]` |
| BossIP relay / `_provider_submit` | `provider_wire` | `_bossip_video_payload` 变换后的 `POST /v1/videos` body，含实际 prompt/metadata/content 等路线字段 | 不把变换前 Ark 输入冒充最终 relay body；E16 `[bossip]` |
| SD2 / [video_providers.py](../backend/tool/video_providers.py) `build_payload` → `submit` | `provider_wire` | 实际 `/v1/videos` body，含模型映射、提示词增强、时长/尺寸及参考素材参数 | 同一 submit 只有一个 Request；E16 `[sd2]` |
| Task gateway / `build_payload` → `submit` | `provider_wire` | 实际 `/v1/video/generations` body；prompt 与最终 metadata，按所选模型完成参数规范化 | 同一 submit 只有一个 Request；E16 `[task]` |
| DashScope ASR / `_dashscope_transcribe` | `provider_wire` | 初始 `POST /api/v1/services/audio/asr/transcription` 的 model、input.file_urls、parameters.channel_id/language_hints | 初次 POST 为 `accepted`；状态轮询和结果 JSON 下载为 `job.progress`；E17 `[dashscope]` |
| OpenAI URL ASR / `_provider_transcribe` | `provider_wire` | `POST /v1/audio/transcriptions` 的 model、audio_url、response_format | 同步结果为 `completed/stop`；保留实际响应再做正文校验；E17 `[openai_url]` |
| IMS / [video_compose.py](../backend/tool/video_compose.py) → [ims_client.py](../backend/video/ims_client.py) `_rpc` | `adapter_input` | `OpenApiRequest.query` 已组装完成后捕获 SubmitMediaProducingJob：Timeline、OutputMediaTarget、OutputMediaConfig、ClientToken、Source、UserData | 提交为 `accepted`；GetMediaProducingJob 为原 Job 的进度；E18 使用真实 query 构造和 Tea client 替身 |

上述非流式 POST 的 Request 耗时是派发及响应区间，**不是视频生成耗时**。异步生成完成、失败或取消由现有 Job 状态机记录 `job.finished`；轮询、结果下载与远端 cancel 是 Job 观察，不增模型生成次数，不新建计费。媒体 Request 不继承父聊天的 `billing_usage_event_id`。

临时签名 URL 在录制副本中替换成 `trajectory-media:<payload_id>` 并附版本清单。实际 provider body 仍使用原 URL。源资产读取在写事务外准备；派生视频帧/音轨绑定原视频资产，删除原视频后旧请求中的派生媒体也不可读取。E16/E18 比较实际提交体与录制副本，E22 验证派生媒体删除链。

所有 submit 的异常/取消均结束本次 Request；JSON 解析失败先保留实际非 JSON 响应。E23 证明录制失败时没有 HTTP submit，E24 证明显式重试产生两个 Request ID，并分别保留失败和 accepted 响应。此处没有观测 provider 内部重试。当前轮询只在成功读取并解析返回值后记进度，**并未为每一次失败 GET 建立完整 HTTP 尝试日志**。

## 4. 工具、委派、历史变化与恢复

| 路径 | 已实现的事实入口 | 实际证据及边界 |
|---|---|---|
| 常规 Tool | [hooks.py](../backend/agent/hooks.py) 在权限前记录 `tool.requested`；[tool.py](../backend/tool/tool.py) 参数校验后记录真实 `tool.started`；完整 executor 输出在裁剪前记录，`tool.finished.model_output` 保持模型实际看到的版本 | E01、E08。拒绝/非法参数没有伪造 started 或执行耗时；开始与等待时长分开 |
| 工具内部提前裁剪 | Bash、MCP、WebFetch 使用 `_trajectory_full_tool_output` 保留内部裁剪前可观察内容，hooks 每次调用清空，避免继承上一工具内容；累计输出和最终模型输出的录制副本使用每 Call 独立脱敏状态 | E08 的 slot 清理专项；E25 的真实工具输出专项由父任务执行；E34 验证跨片凭据和 Call/Request 状态隔离 |
| batch | [batch.py](../backend/tool/batch.py) 子工具经过各自 hooks、校验和权限；每个 child Call 关联 batch 父 Call，上下文和 capability 集合分别复制 | E09 实际 batch/hook/executor 配 Recorder spy；**尚无通过 ASGI 发起 batch 的 SQLite 轨迹整链专项** |
| 多层 task | [task.py](../backend/tool/task.py) 创建子会话，记录 spawned/message/finished；子 Agent 实际运行 `run_loop`，根据实际返回/错误/取消收束 | E03：root→middle→leaf，1 根/1 Turn/3 Run/6 Request/12 chunk/2 个子 Agent 完成，核对 owner/source/parent/turn/run/request/call/generation。取消或失败子链没有同深度应用链专项 |
| 旧会话续聊 | 首次新输入保存真实历史与设置 baseline，只追加此后输入及执行事实，不回填旧 `input.accepted` | E05、E06；旧事件不存在时不能回放上线前执行过程 |
| 暂停/恢复记录 | 已有轨迹遇关闭开关只写不带输入正文的 paused gap；重开取得当前历史与新 baseline，coverage 保留缺口 | E05 验证两个 baseline、关闭期间无正文事件及事务外附件读取；从未记录的新会话在关闭时不新建轨迹 |
| prune / compaction / context replacement | 实际 prune 更新有效 Part；原始 Tool 输出和旧事件保留。摘要 Message 的边界提交同步产生 `context.replaced`，包含新 summary/boundary 引用 | E04 在三个历史回合后真实压缩，两个旧 Part 被 prune，两个块摘要和一个总摘要；旧 H 状态、长输出内容保持一致，续聊实际模型输入使用新摘要 |
| fork / revert | fork 新根仅把复制历史作为 baseline；revert/snapshot restore 与 undo 记录实际引用和终态 | E26、E27 由父任务执行；不声称可以复原未捕获的文件版本或整段桌面录像 |
| 审批 / 等待用户 | [permission.py](../backend/permission/permission.py)、[question](../backend/question/question.py)、[continuation.py](../backend/question/continuation.py) 在请求/回答事务保存原上下文；回答回填完成结果，不再次执行已完成工具 | E13 验证丢失唤醒后按持久决定恢复原 Call；E14 验证旧 pending 问题接入 baseline 后回答，不伪造旧 asked/started；E28 的真实 loop/新 worker 测试替换了 `stream_llm` |
| cron 临时子会话 | [executor.py](../backend/cron/executor.py) 从任务持久目标建立上下文；有目标归目标根，无目标以新执行会话为根；摘要、临时子会话和 job/agent/turn 终态显式关联 | E11 证明目标根和源会话切换，foreign ambient context 不串入；其中 summary/子 loop 是替身，无目标分支无专门轨迹整链专项 |
| cron 回传及恢复 | [injector.py](../backend/cron/injector.py) 用 CronRun 原上下文，输入注入与消费标记同事务、稳定幂等 ID；[recovery.py](../backend/cron/recovery.py) 对已开始但中断的执行记 unknown/gap | E12 验证新 Turn 活跃时旧结果仍归原 Turn、重复注入只一次；E29 为恢复函数测试，**不是 cron 进程真实强杀验证** |
| 视频后台恢复/迟到结果 | [job_recovery.py](../backend/video/job_recovery.py)、[compose_recovery.py](../backend/video/compose_recovery.py) 和 [jobs.py](../backend/trajectory/jobs.py) 还原提交身份及已保存 Request ID；typed recording failure 不被扫表消费者吞后继续派发 | E18 真实恢复上下文的 IMS poll；E30 验证迟到/重复回调与删除 tombstone；E31 验证既有恢复路由及状态行为，但替换了 provider |

## 5. 精确测试索引

“父任务”表示该测试由总集成任务提供并汇总。第 6 节列出的命令实际包含哪些文件，就计入哪些结果；集合间有重叠，不重复累加。其余父任务专项的最终结果以 [实现状态](SESSION_TRAJECTORY_IMPLEMENTATION_STATUS.md) 为准。

| 证据 | 测试文件与具体函数 | 证据形态 |
|---|---|---|
| E01 | [test_trajectory_agent_loop.py](../backend/tests/integration/test_trajectory_agent_loop.py) `test_user_input_through_application_agent_loop_and_auxiliary_request` | 应用链；两个主请求 + 一个 suggestions、五类工具事实；实际 UsageMeter shadow 账本三条并关联原结算 |
| E03 | 同文件 `test_multilevel_task_uses_one_root_and_independent_child_runs` | 多层 task 应用链 |
| E04 | 同文件 `test_real_compaction_prunes_effective_context_but_preserves_replay` | 压缩、prune、历史 H 和续聊应用链 |
| E05 | 同文件 `test_pause_resume_captures_new_baseline_and_reads_attachments_before_write_lock` | 实际 Session/轨迹 DB；模拟读资产时另一连接可写，证明未持写锁 |
| E06 | [test_trajectory_session_runtime.py](../backend/tests/unit/test_trajectory_session_runtime.py) `test_existing_session_records_only_new_input_and_captures_actual_baseline`；`test_input_and_compatibility_projection_rollback_with_journal` | 实际事务与失败回滚 |
| E07 | 同文件 `test_superseded_generation_cannot_commit_a_late_chat_part`；`test_auxiliary_request_chunks_can_follow_finished_run_with_original_identity` | 实际租约、DB；辅助请求直接调用 RequestCapture |
| E08 | [test_trajectory_runtime.py](../backend/tests/unit/test_trajectory_runtime.py) `test_request_snapshot_uses_allowlist_and_omits_private_provider_state`；`test_each_adapter_dispatch_has_identity_and_persists_chunks_before_delivery`；`test_failed_recording_never_dispatches_provider`；`test_tool_denial_is_recorded_without_dispatch_or_execution_duration`；`test_full_tool_result_is_retained_before_model_truncation`；`test_invalid_tool_arguments_never_have_execution_start`；`test_custom_executor_full_output_slot_is_cleared_between_calls`；`test_responses_final_only_output_is_a_replace_checkpoint`；`test_stream_reader_can_fill_one_batch_before_first_receipt`；`test_closing_consumer_closes_provider_reader_and_records_cancel` | 公开字段、真实 executor、SDK/Recorder spy、receipt barrier 局部契约 |
| E09 | 同文件 `test_parallel_batch_keeps_parent_and_sibling_contexts_separate` | 实际 batch 与 hooks，Recorder spy |
| E10 | [test_trajectory_session_runtime.py](../backend/tests/unit/test_trajectory_session_runtime.py) `test_child_prompt_and_messages_join_parent_trajectory_before_child_runs` | 子 Run 启动前的真实消息事务 |
| E11 | 同文件 `test_cron_pipeline_uses_saved_target_for_summary_child_run_and_completion` | cron 管线与 DB，摘要/子 loop/外部 delivery 替身 |
| E12 | 同文件 `test_delayed_cron_injection_restores_original_turn_and_is_idempotent` | 实际注入事务与幂等 |
| E13 | [test_trajectory_boundaries.py](../backend/tests/integration/test_trajectory_boundaries.py) `test_permission_commit_survives_lost_wakeup_and_keeps_original_call` | 父任务；真实权限事实 DB，模拟通知丢失 |
| E14 | 同文件 `test_old_pending_question_adopts_baseline_and_resumes_without_tool_execution` | 父任务；真实 ask/reply/apply_answers/租约，尚未启动真实 provider 续聊 |
| E15 | [test_llm_tool_search_responses.py](../backend/tests/unit/test_llm_tool_search_responses.py) `test_real_responses_adapter_wire_stream_and_usage_contract`；`test_real_native_stream_waits_for_reveal_before_public_tool_call`；`test_real_responses_adapter_pre_stream_fallback_is_one_portable_request`；`test_real_responses_adapter_never_replays_after_first_sse_event`；`test_real_native_stream_fails_closed_without_one_matching_completed`；`test_native_fallback_happens_once_only_before_first_event` | 实际 Responses/native 适配器，模拟 HTTP/SSE，未传 trace_ctx |
| E16 | [test_trajectory_media_dispatch.py](../backend/tests/integration/test_trajectory_media_dispatch.py) `test_video_dispatch_captures_final_wire_and_poll_does_not_create_request`，参数 `ark/bossip/sd2/task` | 四路实际适配器 + MockTransport + SQLite；每路一次 submit、两次 poll，保持一个 Request |
| E17 | 同文件 `test_asr_dispatch_and_observed_results_keep_one_request`，参数 `dashscope/openai_url` | 实际 ASR HTTP 链 + MockTransport + SQLite |
| E18 | 同文件 `test_ims_captures_final_sdk_query_and_polls_saved_job_context` | 实际 OpenApiRequest、SDK client 替身、SQLite 和 Job 恢复上下文 |
| E19 | [test_trajectory_auxiliary.py](../backend/tests/integration/test_trajectory_auxiliary.py) `test_bash_recording_failure_does_not_kill_or_reexecute`，参数 `idle_judge/output` | 父任务；实际 Bash 业务分支，模拟沙箱流和录制失败 |
| E20 | [test_trajectory_video_billing.py](../backend/tests/integration/test_trajectory_video_billing.py) `test_video_analysis_settlement_uses_existing_bill_once` | 实际 `_complete`、UsageMeter、SQLite；一个 Request/响应/原账单 |
| E21 | [test_trajectory_auxiliary.py](../backend/tests/integration/test_trajectory_auxiliary.py) `test_image_edit_captures_actual_parameters_owned_versions_and_existing_bill` | 父任务；实际 image edit 输入/输出资产及账本，OpenAI SDK/OSS 替身 |
| E22 | [test_trajectory_media_dispatch.py](../backend/tests/integration/test_trajectory_media_dispatch.py) `test_derived_media_remains_revocable_with_original_video` | 实际源删除与受控 payload 拒绝读取 |
| E23 | 同文件 `test_media_recording_failure_prevents_actual_submit` | Recorder 失败注入；HTTP 派发数为零 |
| E24 | 同文件 `test_explicit_media_retry_has_a_new_dispatch_id` | 实际适配器先 400 后 202，两次派发、独立 Request 和终态 |
| E25 | [test_trajectory_tool_output.py](../backend/tests/integration/test_trajectory_tool_output.py) `test_bash_stream_over_preview_limit_is_replayable_before_and_after_finish`；`test_mcp_internal_truncation_preserves_full_observed_public_body`；`test_web_fetch_retains_processed_body_before_tool_truncation` | 父任务；真实工具处理与 SQLite，外部工具/HTTP 结果替身 |
| E26 | [test_trajectory_auxiliary.py](../backend/tests/integration/test_trajectory_auxiliary.py) `test_fork_retains_baseline_only_and_new_root_records_future_input` | 父任务；实际 fork、baseline 与新输入 |
| E27 | 同文件 `test_snapshot_restore_and_undo_have_observed_terminal_events` | 父任务；实际 restore/undo 业务入口，快照执行替身 |
| E28 | [test_durable_questions.py](../backend/tests/unit/test_durable_questions.py) `test_complete_loop_releases_wait_and_new_worker_continues_with_answer`；`test_answer_is_durable_idempotent_and_completed_tool_not_replayed`；`test_new_worker_recovers_committed_answer`；`test_replacement_and_answer_race_cannot_revive_old_generation` | 父任务；真实 loop/问答/新 worker 对象，`stream_llm` 替身；不等同 OS 进程强杀 |
| E29 | [test_cron_recovery.py](../backend/tests/unit/test_cron_recovery.py) `test_stuck_markers_cleared_and_running_runs_marked`；`test_fresh_missed_run_stays_due_for_replay`；`test_stale_missed_run_reschedules_instead_of_replaying` | 恢复函数与数据库状态；已中断执行与尚未开始的错过调度分别处理 |
| E30 | [test_trajectory_boundaries.py](../backend/tests/integration/test_trajectory_boundaries.py) `test_late_job_callbacks_use_submission_identity_and_cannot_revive_deleted_trace` | 父任务；真实 Job 事实、幂等回调及根删除 |
| E31 | [test_video_job_recovery.py](../backend/tests/unit/test_video_job_recovery.py) `test_stale_in_progress_finalizes_when_provider_done`；`test_matching_route_fingerprint_still_recovers`；`test_route_mismatch_does_not_reclaim_stale_finalizing`；`test_provider_failed_settles_job` | 既有实际恢复业务逻辑配 provider 替身；不代表真实远端生成 |
| E32 | [test_trajectory_responses_title.py](../backend/tests/integration/test_trajectory_responses_title.py) `test_responses_native_and_fallback_persist_actual_dispatches`，参数 `native/fallback` | 实际 Session 输入、运行租约、Responses 适配器、SQLite 轨迹及投影；复用既有模拟 HTTP/SSE，显式传 trace_ctx |
| E33 | 同文件 `test_title_entry_after_run_records_original_identity_and_existing_bill_once` | 实际 `_ensure_title`、标题保存、UsageMeter、SQLite；主 Run 结束后保留原身份，SDK 替身 |
| E34 | [test_trajectory_stream_redaction.py](../backend/tests/integration/test_trajectory_stream_redaction.py) `test_fragmented_request_credentials_never_enter_retained_views`；`test_tool_cumulative_output_redaction_resets_for_each_call`；`test_redaction_state_is_not_shared_between_requests` | 前者为 LiteLLM arguments/text/reasoning、Responses arguments/text × inline/外置 payload 共十组；真实 Recorder、每个历史 H、物化投影/搜索内容、payload 字节和 ZIP 均不含分片凭据，原 SDK/工具输出不改变 |
| E35 | 同文件 `test_sensitive_schema_properties_remain_the_same_request_and_tool_contract` | 实际 RequestCapture → ToolHooks/define_tool；公开密码属性定义及约束一致，示例/默认值/输入值脱敏，SDK 私有状态不重新开放 |

## 6. 本运行链任务已执行的命令

工作目录均为 `/Users/wang/workspace/OpenBox-trajectory/backend`，使用原项目已安装的 Python 环境。未访问付费模型或真实云桌面，未修改原服务配置。

### 流式脱敏与 Schema 修正后的关联回归

```sh
/Users/wang/workspace/OpenBox/backend/.venv/bin/python -m pytest -q \
  tests/integration/test_trajectory_stream_redaction.py \
  tests/integration/test_trajectory_responses_title.py \
  tests/integration/test_trajectory_media_dispatch.py \
  tests/integration/test_trajectory_auxiliary.py \
  tests/integration/test_trajectory_video_billing.py \
  tests/integration/test_trajectory_agent_loop.py \
  tests/unit/test_trajectory_runtime.py tests/unit/test_trajectory_projection.py --tb=short
```

结果：**63 passed，2 条现有 Pydantic 警告，11.51 秒**。新文件的 13 项检查包括跨片 JSON api_key、Authorization/Bearer、短 `sk-` 前缀，覆盖 raw/blocks、外置内容、每个 H、搜索投影与实际 ZIP。原主运行链、native/fallback、标题、四路视频/两路 ASR/IMS、image edit、既有账本和投影 fixture 同时通过；完整非流式业务响应及 native schema 没有变为空内容引用。

### 补充 Native / fallback 与标题入口证据

```sh
/Users/wang/workspace/OpenBox/backend/.venv/bin/python -m pytest -q \
  tests/integration/test_trajectory_responses_title.py --tb=short
```

当次结果：**3 passed，2 条现有 Pydantic 警告，2.91 秒**。此轮仅增加入口证据；后续流式脱敏修正后，三个用例也纳入上面的 63 项。Native 测试解析明确的安全 Block 引用后比较实际响应字段；完整 JSON 工具参数按解析值比较，允许脱敏过程重新序列化。

### 运行链与媒体汇总回归

```sh
/Users/wang/workspace/OpenBox/backend/.venv/bin/python -m pytest -q \
  tests/integration/test_trajectory_media_dispatch.py \
  tests/integration/test_trajectory_agent_loop.py \
  tests/integration/test_trajectory_video_billing.py \
  tests/unit/test_video_production.py tests/unit/test_video_providers.py \
  tests/unit/test_video_compose.py tests/unit/test_video_analyze.py \
  tests/unit/test_video_job_recovery.py tests/unit/test_model_capabilities.py \
  tests/unit/test_trajectory_runtime.py tests/unit/test_tool_runtime_loop.py --tb=short
```

结果：**179 passed，2 条现有 Pydantic 警告，7.18 秒**。该次之后修正了媒体子请求误继承父聊天账单 ID 的关联问题，并重跑受影响文件：

```sh
/Users/wang/workspace/OpenBox/backend/.venv/bin/python -m pytest -q \
  tests/integration/test_trajectory_media_dispatch.py --tb=short
```

结果：**10 passed，1.75 秒**。四路视频用例包含不继承父聊天账单 ID 的断言。

### 此前完成的运行/会话/cron/native 回归

```sh
/Users/wang/workspace/OpenBox/backend/.venv/bin/python -m pytest -q \
  tests/unit/test_trajectory_runtime.py tests/unit/test_trajectory_session_runtime.py \
  tests/unit/test_processor_outcomes.py tests/unit/test_prune_tail.py \
  tests/unit/test_compaction_select.py tests/unit/test_cron_executor.py \
  tests/unit/test_cron_injector.py tests/unit/test_cron_recovery.py \
  tests/unit/test_batch_parallel_safety.py tests/unit/test_llm_tool_search_responses.py \
  tests/unit/test_tool_runtime_loop.py tests/unit/test_tool_part_runtime_binding.py \
  tests/unit/test_suggestions.py tests/unit/test_capability_search.py \
  tests/unit/test_billing.py tests/unit/test_provider_params.py \
  tests/unit/test_llm_schema.py tests/unit/test_video_analyze.py --tb=short
```

当次结果：**258 passed，2 条现有 Pydantic 警告，4.86 秒**。这是后续媒体扩展前的阶段结果，不是最终所有文件的重新验证。以上测试集合有重叠，不能相加为独立用例数量。最终全后端回归由父任务另行汇总。

## 7. 明确未观察或未独立验收的边界

1. 没有真实付费 provider、远端云沙箱或云桌面调用证据。HTTP/SDK 替身验证的是本应用可控边界；不推断 SDK/provider 内部重试、内部推理、媒体生成中间帧或完整桌面录像。
2. Native Responses/fallback 现有实际适配器 + 输入/租约/轨迹 DB 证据，标题现有实际 `_ensure_title` + 原账本证据；这些新增专项没有从 ASGI 自动选择路由/后台调度标题。image generate 没有对应 edit 专项同等深度的入口证据。
3. batch 使用真实执行器配 Recorder spy；task 深度应用测试覆盖成功返回。多层 task 失败/取消和完整压缩失败/取消没有同等深度专项。
4. cron 轨迹测试替换摘要与子 loop，未验证实际 cron 摘要请求和子 Agent 请求的组合链；无目标 cron 的新根分支没有专门轨迹应用测试。恢复函数测试不等同 cron 进程强杀。
5. 问答恢复有真实 loop + 新 worker 对象测试和旧问题轨迹事务测试，但前者替换整个 `stream_llm`。进程强杀和租约 unknown 的独立后端证据请查看存储验证报告，不能把它替代所有业务恢复入口。
6. 媒体成功 POST 仅表示派发已接受。provider 内部异步工作、失败 GET 的完整网络尝试序列和未返回的中间产物没有全部捕获；最终业务结论由现有 Job 状态机提供。
7. 本文件不验收前端回放操作、超管浏览器隔离、长期多节点负载、真实网络延迟和 GB 级导出内存。SQLite 应用链结果不能自动当作相同入口的 PostgreSQL 结果。
8. 上线前或关闭记录期间没有发生过采集的执行不能重建；baseline 只保留继续执行所需的实际可见历史、配置与可用附件版本，不补造事件。
