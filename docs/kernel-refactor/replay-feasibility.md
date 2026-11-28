# 回放测试可行性（第 0 步 0-8，2026-11-28）

依据：代码和文档，在 a9da02c8 上读的；外加一个仓库外的原型。没有碰生产环境，没有读任何真实轨迹。对应第 10 节第 0 步"回放测试"提的三个问题。

## 结论

**这一轮做回放，但素材换成内部录制的脚本化会话，不用线上真实会话。** 线上轨迹有三处不能直接拿来用：

1. 录下它的代码版本说不清。生产是补丁镜像，底版不在 git（第 10 节第 0 步），事件里也没有提交号（spool 的 `producer.json` 只记 hostname、pid、boot_id，SPEC §3.1）。拿它当主干"行为没变"的标准答案，对比的对象就不对。
2. 回放要的环境有一半不在记录里：`openbox.json`（模型列表、工具暴露方式、按模型选的提示词），记忆和助理资料，Skill 目录（经沙箱 HTTP 读，不录），系统提示词里的日期。
3. 内容不脱敏（见 c），进仓库就成了删不掉的副本。

原型证明，用同一套录制格式驱动真实的 `run_loop` 是做得到的（见最后一节）。所以做法是：在基线提交上跑脚本化会话，录下来，存成用例；之后每一步改动都重放这些用例，逐字节比对发给模型的请求。定向测试和 SQL 门槛（0-3）照样保留。回放补的是它们看不到的一块：请求体和落库结果有没有变。

## (a) 轨迹里有没有逐字的模型请求和响应

**有，够驱动循环。**【已核对】

- 请求：`request.prepared.data.input` 是发出去的完整请求体，只去掉 `TRANSPORT_FIELDS` 里的传输设置和凭据，去掉的名字写在 `omitted_fields`（`agent/trajectory.py:30-35, 102-108`）。litellm 通道录的是 `acompletion` 的参数（`capture_level="adapter_input"`，`agent/llm.py:2092`）；Responses 通道录的是线上的 payload（`provider_wire`，`llm.py:1323`）。
- 响应：每个 chunk 一条 `request.delta`，`raw` 原样保存（SDK 对象存 `model_dump`），旁边是适配器整理过的 `blocks`（`trajectory.py:571-591`，SPEC §5.5）。`request.finished` 记状态、`finish_reason` 和用量；出错时只有异常类型和消息（`trajectory.py:657-674`）。
- 大段内容会换成引用：spool 里是 `$blob`（SPEC §3.5），轨迹库里是 `$ref`（§8.4），读的时候都能展开。管理端导出的 ZIP 里有 `events.jsonl`，也有 payload 字节，包括资产字节（`trajectory/export.py:1-14`），可以离线用。

**用不了的情况**：

- 有 `recording.gap`。
- 预算降级（`degraded` 丢掉中间的 delta）或阻断（`blocked` 连 `request.prepared` 都丢），见 SPEC §5.7。
- 请求里带了图片：自有图片只录成资产引用（`trajectory.py:111-129`，SPEC §8.4 第 4 步），回放要另外拿到字节。
- 失败的重试只留下错误消息，不能原样再现 HTTP 层。

## (b) 工具和沙箱结果怎么回放，循环要提供什么

**把录下的结果喂回去，不要只回放不调工具的回合。** 真实回合大多带工具，只回放纯文本覆盖不到 `process_step` 的主要分支。

`tool.finished` 带着喂回去要的三样：`model_output`（就是 `ToolResult.output`）、`title`、`metadata`（经 `public_value`，去掉下划线开头的键和 `PRIVATE_FIELDS`，`agent/hooks.py:255-261`）。内核读的 metadata 键（`error`、`blocked`、`waiting_input` 等）都不在被去掉的范围里【已核对】。

匹配规则：按调用顺序，校验工具名和 `arguments_raw`，对不上就让用例失败。对不上说明模型收到的输入已经变了，这本身就是要抓的行为变化。工具执行不需要沙箱；工具解析和提示词要读沙箱，回放和录制要用同一个确定的假沙箱。

需要四个注入点，都不能是 `agent.loop` 或 `agent.processor` 上的属性。现有的 `loop_harness`（`tests/unit/test_run_fencing_api.py:49-77`）替换了 `_build_system_prompt`、`resolve_step_tools`、`get_agent`，代码一搬就要跟着改。

| 注入点 | 现在用什么 | 以后 |
|---|---|---|
| 模型 | `litellm.acompletion`，外部库，重构碰不到。默认对话模型走这条（`_needs_responses_api` 只认 GPT-5，`llm.py:940-951`） | Responses 通道在 `llm.py:1329` 当场建 `httpx.AsyncClient`，第一版不覆盖；等"模型适配器接口"再补 |
| 工具执行 | 包住 `tool.registry` 里已注册内置工具的 `execute`；所有工具体都经过 `assistant.budget.run_tool_body`（`hooks.py:513-514`） | 第 2 步的"调用包装"建好后，回放执行器改成一个调用包装，这个接口就有了第二个使用者 |
| 沙箱 | `sandbox.sandbox_manager.get_client` 返回 None，或一个固定的假沙箱 | 不变 |
| 时钟 | 系统提示词里有日期；知道设备时区时还有到分钟的时间（`agent/loop.py:3746-3759`）。固定 `DeviceLocale` | 第 1 步"接收时间移出系统提示词"之后只剩日期 |

另外，标题、建议、压缩、记忆抽取也会请求模型。假模型按 `data.purpose` 加顺序作答。

## (c) 用户数据

规范明确**不做内容脱敏**：spool、轨迹库、blob 存的都是原始内容，只靠管理员白名单限制访问（SPEC §5.5）。一个真实会话里有：

- 用户的原话、上传的文件和工具输出；
- 发给模型的记忆召回正文和助理资料段落。记忆规则 6 只让记忆正文不进历史，请求体里照样有；
- 资产引用，回放要的话还得拿资产字节。

**脱敏做不成。** 自由文本里有没有个人信息，没法机械判断。替换又必须在库的初始数据、历史和期望请求体三处一致，base64 内容还替换不了。

**进仓库还违反删除语义。** 删会话会清掉轨迹内容，删资产会让引用作废（SPEC §8.11），内容 180 天后过期。git 里的副本这三件事一件都做不到。

所以真实会话一律不进仓库。用例只用：

1. 合成的脚本化会话（第一版全部用这种）；
2. 以后要更真实的长会话，由团队成员在本地后端加本地沙箱上，用真实模型、在同意的前提下录制，转成同样的格式。

## 这一轮的设计

- **用例**：`backend/tests/replay/cases/<名字>.json`。每个用例写明：初始数据、逐轮的用户输入、每个模型请求的回复（chunk 列表）、每次工具调用的结果，以及期望值。期望值有三样：每个请求的请求体（去掉传输字段，由测试自己算，不调 `agent.trajectory`，因为轨迹试点要搬它）；按会话归一化后的 `agent_events`（去掉 id 和时间）；SQL 条数。
- **生成**：`record_case.py` 在基线提交上跑脚本，录下 spool，转成用例文件。只在基线上跑，或经评审有意改行为的提交上跑。
- **执行**：`test_replay_cases.py` 按用例参数化，并入 `make check-kernel`。
- **第一批 10 个**：纯文本；一次工具；两个并行工具；工具报错；未知工具名和参数修复；提问、回答、续跑；权限询问后放行一次；长历史触发压缩；429 重试后成功；流中途中止。之后补一个按生产配置开记忆召回的场景。
- **工作量**：约 3 个工作日，放进第 0 步。完成标准：10 个用例在 a9da02c8 上通过，两次运行逐字节一致；第 2 步的架子（不注册插件）上也通过。
- **退路**：如果某类不确定性压不住（比如并行工具的落库顺序），那个用例只比请求体，不比事件；仍然不稳就去掉，回到定向测试加 SQL 门槛。

## 原型（仓库外，没有合入）

`/private/tmp/claude-501/-Volumes-fanxiang-workspace2-OpenBox/65ad3bdc-74f5-4847-8ff1-8d8816e34147/scratchpad/replay_proto/test_replay_proto.py`（临时目录，可能被清掉）分两遍跑：

- **录制**：用 `recording_spool` 夹具（`tests/unit/trajectory_producer_support.py:43-53`）录两轮会话，每轮一次工具调用加一段文字，共 4 个对话请求。
- **回放**：假的 `litellm.acompletion` 只拿录下的 `request.delta.raw` 重建 `ModelResponseStream`；工具只返回录下的 `tool.finished`。

结果：

- 4 个请求体与录制时完全相同。比对的是 blob 的 sha256；系统提示词是真实的 `_build_system_prompt`，32,688 个字符。
- 录制和回放分在两个进程、隔了一分钟，结果也一样。
- 还发现一点：恢复真实的 `get_agent` 之后，新加的工具不在 agent 的工具白名单里，模型调用它就成了"未知工具"。所以用例要用真实的内置工具、只替换执行，不能另造工具。
