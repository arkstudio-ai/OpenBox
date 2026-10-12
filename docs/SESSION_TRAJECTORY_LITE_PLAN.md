# 轨迹监控 Lite：用户 → 对话轮 → 轮详情

> 日期：2026-10-12。状态：设计已拍板（§10），页面原型已出；**M1 后端已实现**（PR #70，分支 `feat/trajectory-lite-m1`，实现差异见 §11），**M2 前端已实现**（分支 `feat/trajectory-lite-m2`，实现记录见 §12），均未合并未发布。
>
> 现有超管轨迹页面（[UI 规格](./SESSION_TRAJECTORY_UI_SPEC.md)、[协议](./SESSION_TRAJECTORY_PROTOCOL.md)、[存储与 worker 架构](./trajectory-rearch/SPEC.md)）是为工程师排障设计的：28 种记录类型、seq 水位、回放条、九列表格。运营和客服用不起来。本文设计一套面向非技术人员的"简明模式"，作为轨迹监控的默认入口；现有页面保留为"专业模式"。
>
> 范围：**只加读接口和一套新页面，不改采集、不改事件协议、不改投影语义。** 权限、审计、只读约束与现有实现完全一致。

配套入口：超管后台 → 轨迹监控（简明模式默认）→ 用户列表 → 该用户的对话轮 → 轮详情 → 「技术详情」跳专业模式同一位置。

## 1. 目标与非目标

### 1.1 目标

1. 运营能在 30 秒内回答三个问题：**这个用户最近让 AI 干了什么、成了没有、没成是卡在哪一步。**
2. 页面上不出现 `request / step / run / context / compaction / seq` 这类词；工具名显示为中文动作。
3. 失败和等待用户的轮次一眼可见，并能按它们筛选。
4. 任何一轮都能一键跳到专业模式的原始记录，排障链路不断。

### 1.2 非目标

- 不做回放、倍速、按 seq 跳转；实时性靠刷新提示和轮询。
- 不做跨用户全文搜索（专业模式已有单会话搜索）。
- 不改聊天端、App 端，不给普通用户开放任何轨迹接口。
- 积分成本与失败告警放第二期（§8）。

### 1.3 受众假设

以**运营查客诉**为第一受众：默认排序按最近活动，异常轮次置顶筛选，列表第一屏是"谁、什么时候、要做什么、成没成"。老板看用量的需求由用户列表的 7 天统计列兼顾，不单独做报表。

## 2. 现状事实与对方案的影响

| 已核实事实 | 代码入口 | 对方案的影响 |
|---|---|---|
| 记录行 `trajectory_records` 有 `kind / status / agent_id / message_id` 列；`turn_id` 只在 `summary` JSON 里，没有列 | [store/models.py](../backend/trajectory/store/models.py) `TrajectoryRecord` | 按轮聚合必须加 `turn_id` 列，或在投影时单独维护轮汇总表（本文选后者，并顺带加列，§6.1） |
| `turn` 记录只有 `turn.started` 与 `turn.finished{status: completed}`；cron 会话不发 `turn.finished` | [question/runtime.py:349,622](../backend/question/runtime.py) | 轮的结论不能只看 turn 记录，要从 run / tool / assistant / question / permission 记录推导（§6.2） |
| `run.finished` 的 `status` 为 `waiting / failed / 其它`，现有会话运行状态即据此推导 | [repository.py `summary_rules`](../backend/trajectory/repository.py) | 轮结论复用同一口径，避免两套状态互相矛盾 |
| `tool` 记录：`title` = 工具 id，`data.requested_arguments` 为参数，`data.model_output` 为结果，`status ∈ {completed, failed, denied, cancelled, waiting}`，错误文本进 `status_reason` | [agent/hooks.py:214-262](../backend/agent/hooks.py)、[projector.py](../backend/trajectory/projector.py) | 步骤流的"做了什么 / 结果 / 为什么失败"三项都能直接取到，无需读 blob |
| `user` 记录 `preview` 是输入文本前 240 字；`assistant` 记录 `preview` 来自文本块，reasoning 单独成块 | projector `_blocks / PREVIEW_FIELDS` | 轮列表的"用户说了什么 / AI 回了什么"从 summary 直接取，列表页零 blob 读取 |
| 子 Agent 的记录带 `agent_id / parent_agent_id`，`task` 工具触发 | protocol `AgentSummary` | 简明模式默认把子 Agent 折叠成一步"分派子任务"，展开才看内部工具 |
| 会话列表已 join `trajectory_meta_users`（用户名、邮箱）与 `trajectory_session_summaries`（统计） | `repository.list_sessions` | 用户维度的聚合可以在 trace 库内完成，不碰业务库 |
| 读接口全部经 `require_trajectory_admin` + `audience` 过滤 + `audit` 落审计 | [worker/routes.py](../backend/trajectory/worker/routes.py)、[audience.py](../backend/trajectory/audience.py) | 新接口原样套用三件套 |
| 协议要求：现有 HTTP/WS 合约字节级兼容，只允许新增可选参数 | rearch SPEC §0 | records 接口新增 `turn_id` 可选参数；其余全是新路径 |
| 业务库 `usage_events` 有 `session_id / message_id / credits`，与 trace 库是两个库 | [db/models/billing.py](../backend/db/models/billing.py) | 积分不能 SQL join；二期按页内 `message_id` 批量查业务库（§8） |
| 工具注册表共 90 余个 id，含 `assistant.* / tasks.* / status.*` 等点号前缀族 | `tool/registry.py` `register_builtin_tools` | 文案字典按"精确 id → 前缀族 → 原名兜底"三层匹配（§5） |

## 3. 信息架构与路由

| 页面 | 路由 | 内容 |
|---|---|---|
| 用户列表（简明模式首页） | `/app/admin/trajectories` | 有记录活动的用户，按最近活动倒序 |
| 某用户的对话轮 | `/app/admin/trajectories/users/:userId` | 该用户跨会话的全部轮次，按时间倒序 |
| 轮详情 | `/app/admin/trajectories/users/:userId/turns/:sessionId/:turnId` | 一轮的步骤流 |
| 专业模式：会话列表 | `/app/admin/trajectories/sessions` | 现有 `TrajectoryListPage` 原样搬到此路径 |
| 专业模式：会话详情 | `/app/admin/trajectories/sessions/:sessionId` | **路径不变**，现有 `TrajectorySessionPage` |

- 简明/专业模式切换放在轨迹监控栏目标题右侧，选择记在 `localStorage`；直达 URL 不受切换影响。
- 轮详情里每一步的「技术详情」链接到 `/sessions/:sessionId?record=<record_id>&at=<end_seq>`，复用现有 `DetailParams` 的 `record` 与 `at`，定位到该记录并停在该轮结束水位。
- 导航 `ADMIN_SECTIONS` 不变，`activeAdminSection` 按第一段匹配，新路径自动高亮。`adminLayout` 中"详情页不显示栏目标题"的判断扩展到 `/users/:userId/turns/`。

**路由取舍**：把现有列表挪到 `/sessions` 会让旧书签失效一次。替代方案是简明模式放 `/lite`，但那样默认入口仍是专业页，违背本文目标。推荐挪动，并在旧列表路径的查询串非空时（带筛选参数的书签）自动 302 到 `/sessions` 保留参数。

## 4. 页面设计

### 4.1 用户列表

一行一个用户。列表只读聚合，不请求任何内容正文。

| 列 | 内容 | 来源 |
|---|---|---|
| 用户 | 用户名；次行邮箱 | `trajectory_meta_users` |
| 最近活动 | 相对时间（"3 分钟前"），悬停显示绝对时间 | 轮汇总 `max(started_at)` |
| 当前状态 | 进行中 / 等待用户 / 空闲 | 该用户最近一轮的结论 |
| 近 7 天轮次 | 数字 | 轮汇总 count |
| 近 7 天失败 | 数字，>0 用警示色 | 轮汇总 `status in (failed, completed_with_errors)` |
| 会话数 | 近 7 天有活动的会话数 | 轮汇总 distinct session |
| 近 7 天用量 | 输入 / 输出 token，紧凑格式 | 轮汇总 sum |
| 操作 | 「查看对话」 | |

筛选：用户名/邮箱/ID 搜索；时间窗（今天 / 7 天 / 30 天，默认 7 天，所有统计列随窗变化）；「只看有失败的」；「只看进行中或等待用户的」。排序固定为最近活动倒序。

未开启记录的用户（`TRAJECTORY_RECORD_USER_IDS` 白名单之外，或从未产生轮次）不出现在列表；搜索框精确命中用户时以一行"尚未记录"提示，避免运营以为用户不存在。

### 4.2 某用户的对话轮

按时间倒序平铺该用户所有会话的轮次。连续属于同一会话的轮次共享一个分组头（会话标题、会话内轮次数、「在专业模式打开会话」）。不要求先选会话。

| 列 | 内容 | 来源 |
|---|---|---|
| 时间 | `10-12 14:32`，当天只显示时间 | `started_at` |
| 用户说了什么 | `user_preview` 截 80 字；有附件显示图标 | 轮汇总 |
| AI 做了什么 | 工具动作标签，按次数聚合，最多 3 个 + "+N"；失败的标签带警示点 | `tool_counts` + 字典 |
| AI 回了什么 | `assistant_preview` 截 80 字 | 轮汇总 |
| 结果 | 状态标签（§6.2 的七种结论） | `status` |
| 耗时 | `1 分 12 秒` | `duration_ms` |
| 用量 | token 紧凑格式 | 轮汇总 |

筛选：结果（多选）；时间范围；会话（下拉，该用户近 30 天会话）；关键字（对 `user_preview / assistant_preview` 做 ilike，服务端执行）。默认不筛。顶部常驻"只看异常"快捷开关 = 结果 ∈ {失败, 完成但有失败步骤, 等待用户, 结果未知}。

实时性：复用现有列表页的 probe 模式，每 30 秒探测首行，变化时顶部出现"有新活动，点击刷新"，不自动重排。

### 4.3 轮详情：步骤流

头部：用户名 → 会话标题 → 时间；一句结论（§5.3 模板）；耗时、用量；「技术详情」（跳专业模式该轮结束位置）。

正文是按 `start_seq` 排序的步骤卡片，一步一卡：

| 步骤类型 | 来源记录 | 卡片内容 | 默认 |
|---|---|---|---|
| 用户输入 | `user` | 全文；图片/文件缩略图走现有 payload 接口 | 展开 |
| AI 思考 | `assistant` 的 reasoning 块 | 折叠为一行"AI 思考了 12 秒"，点开看全文 | 折叠 |
| 工具动作 | `tool`（主 Agent） | 中文动作名 + 参数摘要 + 结果摘要 + 状态 + 耗时；失败时红框显示 `status_reason` 原文 | 展开 |
| 向用户提问 | `question` | 问题全文、用户回答、等待时长；未回答显示"等待用户回答中" | 展开 |
| 等待确认 | `permission` | 请求做什么、用户同意/拒绝/超时、等待时长 | 展开 |
| 分派子任务 | `agent` + 其 `tool` | 一张卡"分派子任务：<名称>"，内含该子 Agent 的工具动作列表 | 折叠 |
| 后台任务 | `job` | 名称、进度、结果（例如视频生成） | 展开 |
| 产物 | `artifact` | 文件名、类型、下载（payload 接口） | 展开 |
| AI 回复 | `assistant` 文本块 | Markdown 渲染全文 | 展开 |
| 记录缺口 | `gap` | 黄色提示条"这期间有一段过程没有记录" | 常显 |
| 重试 | `retry` | 合并进相邻请求：角标"重试了 2 次" | 常显 |

默认完全隐藏：`request / step / run / context / compaction / tool_catalog / baseline / system / settings / skill / plan / todo / history / late_result / interrupt / resume`。其中 `interrupt / resume` 的信息折进轮结论（"已取消"）。

低价值工具合并：连续的文件处理类（`read / write / edit / glob / grep / bash / apply_patch / multiedit`）、记忆类（`memory.* / memory_*`）、助手内部状态类（`status.* / history.read / current_task_state`）在步骤流里合并为一张卡"整理文件 ×6"、"查阅记忆 ×2"，展开可见每一次。失败的不合并，单独成卡。

每张卡右上角「原始记录」链接到专业模式并选中该记录。

详情页的数据来源：轮汇总 + 该轮的 records 摘要列表（新增 `turn_id` 筛选）。用户输入全文、AI 回复全文、工具结果全文在卡片展开时才按记录调现有 `records/{record_id}` 详情接口，列表级不读 blob。

## 5. 文案字典

### 5.1 工具动作名

字典由后端配置下发（§10 第 4 项），改文案只需改配置并重启 worker。匹配顺序：精确 id → 点号前缀族 → `mcp_*` → 原名兜底。

| 工具 id | 显示 | 参数摘要字段 | 结果摘要规则 |
|---|---|---|---|
| `desktop_publish` | 发布到抖音（云电脑） | `title` / 平台 | 成功：帖子链接或"已发布"；失败：原因 |
| `douyin_publish` | 发布到抖音（开放平台） | `title` | 同上 |
| `video_generate` | 生成视频 | 档位、时长、提示词前 40 字 | 成功："已生成 N 条"；失败：原因 |
| `video_compose` | 合成视频 | 片段数 | 成品文件名 |
| `video_analyze` | 分析视频 | 文件名 | 结论前 80 字 |
| `video_transcribe` | 视频转文字 | 文件名 | 字数 |
| `image_gen` | 生成图片 | 提示词前 40 字 | "已生成 N 张" |
| `share_file` | 把文件发给用户 | 文件名 | 已发送 / 失败原因 |
| `assets.attach` / `assets.list` | 整理附件 | 文件名 | |
| `computer` | 操作云电脑 | 动作类型（点击/输入/截图） | 截图缩略图或文本 |
| `browser_mode` | 切换云电脑浏览器模式 | 模式 | |
| `desktop_login` | 云电脑登录 | 平台 | 成功/失败 |
| `desktop_takeover` | 请用户接管云电脑 | 原因 | 用户是否接管 |
| `hot_trends` | 查询热点 | 平台、关键词 | 条数 |
| `web_search` | 搜索网页 | 关键词 | 条数 |
| `web_fetch` | 读取网页 | 域名 | 字数 |
| `question` | 向用户提问 | 问题 | 用户回答 |
| `task` | 分派子任务 | 子任务名 | 子任务结论 |
| `skill` / `skill_search` / `capability_search` | 使用技能 | 技能名 | |
| `skill_manage` | 管理技能 | 操作 | |
| `creator_context` | 读取创作者资料 | | |
| `autopilot_run` | 自动运营 | 任务名 | |
| `cron` / `schedules.*` | 设置定时任务 | 周期、名称 | |
| `plan_enter` / `plan_exit` / `todo_read` / `todo_write` | 规划步骤 | | |
| `memory.*` / `memory_*` | 查阅记忆 / 更新记忆（按读写分） | | |
| `knowledge.*` | 查阅知识库 | 目录/文件 | |
| `read / write / edit / glob / grep / bash / apply_patch / multiedit / view_image` | 处理文件 | 文件名 | |
| `tasks.*` / `requests.*` / `results.read` / `decisions.propose` | 处理任务请求（助手内部） | 任务名 | |
| `projects.*` / `sessions.*` / `briefing.configure` / `assistant.preferences` | 管理项目与设置 | | |
| `status.*` / `history.read` / `current_task_state` | 查看状态 | | |
| `mcp_find_tool` / `mcp_call_tool` / `mcp_read_resource` | 调用外部工具 | 外部工具名 | |
| `batch` | （透明，展开为内部各步） | | |
| 其它 | 原名 | | |

维护保障：后端单元测试遍历 `register_builtin_tools()` 的 id，断言每个 id 都能被配置精确命中或前缀命中。新增工具没写文案时 CI 变红，而不是线上显示英文 id。

### 5.2 状态文案

记录级状态复用现有 `admin-trajectories.json` 的 `status.*`。轮结论七种，新增键 `lite.turnStatus.*`：

| 结论 | 显示 | 色调 |
|---|---|---|
| `completed` | 已完成 | 中性 |
| `completed_with_errors` | 完成，有失败步骤 | 警示 |
| `failed` | 失败 | 危险 |
| `waiting` | 等待用户 | 强调 |
| `running` | 进行中 | 强调 |
| `cancelled` | 已取消 | 中性 |
| `unknown` | 结果未知 | 警示 |

### 5.3 一句结论模板

不调用模型，纯规则拼接，优先级从上到下：

1. `failed`：「<首个失败步骤的动作名>失败：<status_reason 前 60 字>」；没有失败步骤时「AI 回复失败：<原因>」。
2. `waiting`：「等待用户<回答问题 / 确认操作 / 接管云电脑>（已等 <时长>）」。
3. `completed_with_errors`：「已完成，但<动作名>失败了 <N> 次」。
4. `completed` 且有关键动作（发布 / 生成 / 发送文件）：「已<动作名>」+ 数量。
5. `completed` 其它：取 `assistant_preview` 前 60 字。
6. `running`：「AI 正在<当前进行中步骤的动作名>」。
7. `cancelled` / `unknown`：固定文案。

## 6. 后端设计

### 6.1 存储：轮汇总表与 `turn_id` 列

trace 库新增迁移 `t0006_turn_summaries`：

1. `trajectory_records` 加列 `turn_id VARCHAR(128) NULL`，索引 `(trajectory_id, turn_id, start_seq)`。worker 投影写行时从 record 的 `turn_id` 填入；历史行由一次性回填脚本从 `summary` JSON 补齐（`backend/trajectory/ops/` 下，按 trajectory 分批，可重跑）。
2. 新表 `trajectory_turn_summaries`：

| 列 | 说明 |
|---|---|
| `trajectory_id`, `turn_id` | 主键 |
| `session_id`, `user_id`, `workspace_id` | 冗余自 `session_trajectories`，用于用户维度查询 |
| `started_at`, `finished_at`, `duration_ms` | 起止；`finished_at` 为 null 表示未结束 |
| `status`, `status_reason` | §6.2 推导结论与首个失败原因 |
| `user_preview`, `assistant_preview` | 240 字以内，来自记录 summary |
| `user_message_id`, `assistant_message_id` | 二期联积分用 |
| `tool_counts` JSON | `{tool_id: {total, failed}}` |
| `waiting_kind` | `question / permission / takeover / null` |
| `agent_count`, `job_count`, `artifact_count`, `retry_count` | 计数 |
| `request_count`, `input_tokens`, `output_tokens`, `usage_complete` | 与 `TrajectoryStatistics` 同口径 |
| `first_seq`, `last_seq`, `applied_seq` | 水位 |
| `has_attachments` | 用户输入含 `$media / $payload` |

索引：`(user_id, started_at DESC, turn_id)`、`(trajectory_id, started_at)`、`(status, started_at)`、`(session_id, started_at)`。

维护点：worker `projection.py` 每批 upsert 记录行之后，收集本批触及的 `(trajectory_id, turn_id)`，对每个轮重新从该轮的记录行（走新索引）计算一遍汇总并 upsert。重算是幂等的，crash 后重跑无副作用，与现有"单写者、按批提交"模型一致。`turn_id` 为 null 的记录（系统状态、基线、设置等）不参与。

不选"读时聚合"的原因：用户列表和跨会话轮列表要扫一个用户几十个会话的全部记录，用户一多就是全表扫描；轮汇总把成本摊到写入侧，读侧只走索引。

### 6.2 轮结论推导规则

对一轮内的记录按下列顺序判定，第一条命中即为结论：

1. 存在 `question / permission / takeover` 记录状态为 `waiting` 且无后续 resolved → `waiting`，`waiting_kind` 取该记录类型。
2. 该轮 `run` 记录状态 `failed`，或最后一条 `assistant` 记录状态 `failed` → `failed`。
3. `run` 记录状态 `cancelled / interrupted`，或存在 `interrupt` 记录且无 `resume` → `cancelled`。
4. `turn` 记录已 finished，或 `run` 记录已 finished（cron 会话不发 `turn.finished`，以 `run` 为准）：若有任一 `tool / job` 记录状态 ∈ {failed, denied, timed_out} → `completed_with_errors`，否则 `completed`。
5. 以上皆否且该轮最后一条记录 `as_of_seq` 对应时间距今 < 30 分钟 → `running`。
6. 否则 → `unknown`（不猜成功，与现有"结果未知"原则一致）。

`status_reason` 取第一条失败记录的 `status_reason`；`failed` 时优先取 `run / assistant` 的。阈值 30 分钟做成配置 `TRAJECTORY_LITE_RUNNING_GRACE_MINUTES`。

### 6.3 接口

全部挂在 `/api/admin/trajectories` 下，与现有接口同一个 router，同样经 `require_trajectory_admin`、`audience` 过滤、`audit` 落审计（新增 action：`lite_users / lite_turns / lite_turn`）。

**`GET /users`**

参数：`q`（用户名/邮箱/ID 模糊）、`window=today|7d|30d`、`has_failures=bool`、`active_only=bool`、`cursor`、`limit≤200`。
返回：`items[{user:{user_id, username, email}, last_activity_at, current_status, turns, failed_turns, waiting_turns, sessions, input_tokens, output_tokens, usage_complete}]`、`next_cursor`、`has_more`。
实现：对 `trajectory_turn_summaries` 按 `user_id` group by，窗内过滤，join `trajectory_meta_users`；按 `max(started_at)` 倒序，游标 `(last_activity_at, user_id)`。audience：按 `visible_sessions` 过滤出该页用户参与的会话集合，统计只计可见会话；一页用户 ≤200，会话集合有界。

**`GET /users/{user_id}/turns`**

参数：`status[]`、`from`、`to`、`session_id`、`q`、`cursor`、`limit≤100`。
返回：`items[TurnSummary]`（即汇总表一行 + `session_title`）、`next_cursor`、`has_more`。
实现：走 `(user_id, started_at DESC)` 索引；`q` 对两个 preview 列 ilike；游标 `(started_at, turn_id)`。audience：取页内 distinct `session_id` 调 `visible_sessions`，不可见的轮整行剔除后再补页，复用 `list_sessions` 的"先过滤再发游标"做法。

**`GET /sessions/{session_id}/turns/{turn_id}`**

返回：`{summary: TurnSummary, steps: [StepSummary], through_seq}`。
`StepSummary` = 该轮记录的 `RecordSummary`（现有形状，不带 `data / blocks`）+ 服务端附加的 `step_type`（§4.3 的类型）与 `group_key`（低价值合并组）。前端据此渲染卡片，展开时再调现有 `records/{record_id}` 取全文。`through_seq` 固定为 `summary.last_seq`，与现有详情"同一水位"原则一致。

**现有 `GET /sessions/{session_id}/records`** 新增可选参数 `turn_id`。缺省行为不变，满足 SPEC 字节兼容。

### 6.4 读预算与性能

- 三个新接口均不读 blob、不读 events。
- 用户列表一次查询加一次 audience 调用；轮列表同上；轮详情为一次汇总行读 + 一次记录摘要读（按 `turn_id` 索引，一轮通常 < 200 行）。
- 现有 `read_budget` 不受影响；轮详情展开全文时走原有记录详情接口，沿用原预算。

## 7. 前端设计

- 目录：`frontend-v2/src/features/admin-trajectories/lite/`，与专业模式同 feature，直接复用 `api/endpoints.ts`（新增 `users / userTurns / turn` 三个方法）、`stores/access`（权限撤销监听）、`AccessNotice`、`StatusPills`、`utils/time`、`MediaRefView`、`shared/ui/DataTable`。
- 公开导出新增 `TrajectoryUsersPage / TrajectoryUserTurnsPage / TrajectoryTurnPage`；`routes/admin/` 下对应三个 Route 文件，现有两个 Route 保留。
- 查询：TanStack Query，key 带全部筛选参数；轮列表 probe 复用 `useSessionListProbe` 的模式写 `useUserTurnsProbe`。
- 文案：页面文案在 `admin-trajectories.json` 新增 `lite.*`；工具动作名来自后端 `lite/labels` 接口，前端用 TanStack Query 缓存一天，失败时显示原名。
- 模式切换：栏目标题右侧 segmented control「简明 / 专业」，写 `localStorage` 键 `admin-trajectories.mode`；路由本身决定当前模式，开关只负责跳转。
- 窄屏：用户列表保留用户、最近活动、结果、失败数四列；轮列表保留时间、用户说了什么、结果；步骤卡片天然单列。
- 测试：三页的渲染与筛选测试；字典覆盖率测试（§5.1）；结论模板的纯函数测试；`activeAdminSection / adminLayout` 对新路径的测试。

## 8. 分期与工作量

| 期 | 内容 | 预估 |
|---|---|---|
| M1 后端 | 迁移 t0006（`turn_id` 列 + 汇总表）、投影维护、回填脚本、三个接口、`turn_id` 筛选、单元测试 | 2–3 天 |
| M2 前端 | 三页、字典、结论模板、模式切换、路由搬迁、i18n、测试 | 3–4 天 |
| M3 积分与告警 | 轮详情和轮列表显示积分：接口按页内 `assistant_message_id` 批量查业务库 `usage_events`（≤100 个 id，不跨库 join、不复制数据）；失败轮次推送到消息中心给运营 | 1–2 天 |

M1、M2 合在一个 PR 发 gw2；发布时先跑迁移，再跑回填脚本（后台、按 trajectory 分批），回填期间旧轮在简明模式显示"统计中"，专业模式不受影响。

## 9. 验收

**运营用例**：搜用户 → 看到近 7 天 5 轮、1 轮失败 → 点开该用户 → 失败轮置顶，一句结论"发布到抖音（云电脑）失败：登录态已过期" → 点开轮详情，步骤流看到用户输入、生成视频成功、发布失败的红框原因 → 点「原始记录」落到专业模式该 tool 记录。全程不出现英文标识符和 seq。

**性能**：轮汇总表 10 万行规模下，用户列表与轮列表 P95 < 500 ms；轮详情 < 300 ms。投影每批额外重算的轮数 ≤ 本批触及轮数，单轮重算 < 20 ms。

**安全**：非超管 403；被 audience 拒绝的会话不出现在用户统计和轮列表；三个新接口均只读，审计表有对应记录；撤销 admin 角色后页面立即进入现有 `AccessNotice`。

**兼容**：现有专业模式全部测试通过；`/sessions/:sessionId` 路径与 `DetailParams` 不变；`records` 接口不传 `turn_id` 时响应逐字节不变；WS 合约不动。

**数据正确**：对 20 条人工标注的轮（含 cron 会话、子 Agent、等待确认、取消、崩溃未完成）核对结论与一句话模板；`unknown` 不得出现在任何已 finished 的轮上。

## 10. 已拍板（2026-10-12）

1. **路由搬迁**：现有会话列表挪到 `/sessions`，简明模式成为 `/app/admin/trajectories` 默认入口；旧列表路径带查询串时 302 保留参数。
2. **子 Agent**：默认折叠为一张"分派子任务"卡。
3. **"结果未知"阈值**：30 分钟，配置项 `TRAJECTORY_LITE_RUNNING_GRACE_MINUTES`。
4. **字典归属**：后端配置下发。工具动作名、参数摘要字段、结果摘要规则放在后端配置文件 `backend/trajectory/lite_labels.json`（结构见下），由新增接口 `GET /api/admin/trajectories/lite/labels` 下发，前端启动时拉取并缓存；缺失项前端显示原名。§5.1 的覆盖率测试改为后端测试：注册表每个工具 id 必须被配置精确命中或前缀命中。
5. **M3 积分口径**：显示用户实际被扣的 `credits`（用户充值消耗的那一种）；`cost_credits` 不在简明模式展示。

字典配置结构：

```json
{
  "version": 1,
  "tools": {
    "desktop_publish": {"label": "发布到抖音（云电脑）", "args": ["title", "platform"], "result": "publish"},
    "video_generate": {"label": "生成视频", "args": ["tier", "duration", "prompt:40"], "result": "count:已生成 {n} 条"}
  },
  "prefixes": {
    "memory.": {"label": "查阅记忆", "group": "memory"},
    "status.": {"label": "查看状态", "group": "internal"}
  },
  "groups": {
    "files": {"label": "处理文件", "collapse": true},
    "memory": {"label": "查阅记忆", "collapse": true},
    "internal": {"label": "查看状态", "collapse": true}
  }
}
```

## 11. M1 实现记录（2026-10-12）

与 §6 的差异和落地细节，以代码为准：

- **文件**：`backend/trajectory/turns.py`（规则与汇总写入）、`backend/trajectory/lite.py`（三个读接口与字典）、`backend/trajectory/lite_labels.json`（字典配置）、`backend/trajectory/ops/turns_backfill.py`（回填）、迁移 `t0006_turn_summaries`。投影在 `worker/projection.py` 写完记录行后调用 `turns.refresh`。
- **时间窗参数**：接口用 `activity_from / activity_to`（ISO 时间），不用 `window=7d`；"今天 / 7 天 / 30 天" 由前端按本地时区换算后传入，统计列随窗变化。
- **`turn_id` 筛选只走新列**：records 接口和轮详情不再回退读 `summary` JSON（JSONType 不支持跨方言取键）。因此**迁移后必须跑一次回填**，否则旧轮在简明模式不可见、`turn_id` 筛选为空。回填命令见 [deploy/gw2/RUNBOOK.md](../deploy/gw2/RUNBOOK.md) §5。
- **汇总行的 `status` 是结构状态**：`running` 只表示还没有终态事实；读接口按 `TRAJECTORY_LITE_RUNNING_GRACE_MINUTES`（默认 30）把过期的 `running` 显示为 `unknown`，筛选 `running / unknown` 也按同一阈值拆分。
- **等待判定**：question / permission 记录状态 `waiting`，takeover 记录 `pending / running / waiting`，或最新一次 run 以 `waiting` 结束。
- **多列新增**：`last_activity_at`（宽限期判断）、`main_agent_id`（前端折叠子 Agent 用）。
- **用户列表的审计口径**：用户列表、轮列表、轮详情分别记 `admin.trajectory.lite_users / lite_turns / lite_turn`，与 list / view 一样按分钟去重；业务侧 `api/internal.py` 的允许列表已加这三项，否则审计会被 422 拒收进死信。
- **字典接口**：`GET /api/admin/trajectories/lite/labels` 原样下发配置文件；文件改动后无需重启（按 mtime 重新读取）。可用 `TRAJECTORY_LITE_LABELS_PATH` 指到容器外的配置。
- **轮详情返回**：`summary` + `steps`（记录摘要加 `step_type / label / group / collapsible / transparent`）+ `through_seq`（= projected_seq）；步骤全文由前端按需调现有记录详情接口。
- **删除与过期**：tombstone 时显式删轮汇总；content expire 保留轮汇总（与会话汇总一致）。
- **测试**：`tests/unit/test_trajectory_lite.py`（规则、投影、回填、读接口、字典覆盖率）、`tests/unit/test_worker_routes_lite.py`（鉴权、审计、形状）；现有路由契约与迁移 schema 测试已随之更新。

## 12. M2 前端实现记录（2026-10-12）

- **目录**：`frontend-v2/src/features/admin-trajectories/lite/`：`types.ts`（接口形状）、`api.ts`（`liteApi` + TanStack 钩子，key 与现有轨迹 key 同根，权限撤销时一起清空）、`params.ts`（URL 状态：筛选、游标、trail）、`labels.ts`（字典解析、状态色调、一句结论模板）、`steps.ts`（步骤流折叠：隐藏类型、低价值工具成组、子 Agent 收纳、重试计数）、`mode.ts`（简明/专业记忆）、`components/`（`UsersPage`、`UserTurnsPage`、`TurnPage`、`StepCards`、`ModeToggle`、`bits`）。
- **路由**：`/app/admin/trajectories` → 用户列表（简明模式默认）；`/app/admin/trajectories/sessions` → 原专业会话列表；`/sessions/:sessionId` 不变；`/users/:userId` → 某用户的对话轮；`/users/:userId/turns/:sessionId/:turnId` → 轮详情。旧列表书签带 `owner / user / workspace / status / recording / unrecorded / sort` 任一键时 302 到 `/sessions` 并保留查询串；只带 `q / from / to / cursor` 的旧书签无法区分，落到简明模式。
- **时间窗**：用户列表 `window=today|7d|30d|all`（默认 7d），轮列表 `window=7d|30d|90d|all`（默认 30d）；前端按本地零点换算成 `activity_from` 传给后端，统计列一天内稳定。
- **轮列表筛选**：「只看异常」= 状态集合 {failed, completed_with_errors, waiting, unknown}，与状态多选共用 `status=` 参数；会话筛选由分组头的「只看这个会话」写入 `session=`；关键字走后端 `q`。每 30 秒探测首行，变化时显示「有新活动，点击刷新」。
- **轮详情**：步骤流由 `steps.ts` 在前端构建；用户输入、AI 回复和失败步骤默认展开，其余折叠；展开时按记录调现有 `records/{record_id}`（expand=full）取全文，AI 回复按 Markdown 渲染、思考折叠；工具参数/结果复用专业模式的 `JsonTree / TextBlock`。「技术详情」跳 `/sessions/:id?at=<last_seq>`，每张卡「原始记录」加 `record=`。积分位留白标注第二期。
- **字典**：`GET /lite/labels` 缓存一天，未返回时显示工具原名；结论模板里的"关键动作"列表（发布、生成、发送文件等）在 `labels.ts`，与后端配置无关。
- **测试**：`lite/params.test.ts`、`labels.test.ts`、`steps.test.ts`、`pages.test.tsx`（三页渲染、URL 筛选、分页、权限拒绝、步骤流折叠与原始记录链接）；更新 `router.test.ts`、`AdminNav.test.tsx`、`WorkspaceLayout.isolation.test.tsx`（新增简明模式首页只读隔离用例）与 e2e 的列表路径常量。未在浏览器里对接真实后端验收，待 M1 发到 gw2 后联调。

## 13. 线上联调记录（2026-10-12）

M1/M2 发到 gw2 后用真实记录（177 条轨迹、1,249 轮、23 个用户）走查三页，发现并修正（PR #74/#75/#76）：

- **工具名**：投影给工具记录的 `title` 是结果标题（bash 的 "exit code: 0"、glob 的 "Found 27 files"、share_file 的文件名），导致工具芯片、步骤标题、结论句全是结果文案。改为读记录 `data.tool`；汇总表 `tool_counts` 按工具 id 计数，轮详情步骤带 `tool_id`，前端一律按 id 查字典、以结果标题作一行摘要。回填脚本可重跑，重算 1,249 轮 17 秒。
- **权限噪音**：每次工具调用都留下策略自动放行的 `permission` 记录（`source_kind=policy`，0 ms），一轮里 74 条。规则：`completed` 且耗时 < 1 s 的权限记录隐藏；等待过、拒绝、超时的保留。
- **主 Agent 自身记录**：定时任务轮有一条 `agent` 记录就是主 Agent（`agent_id == main_agent_id`），不是子任务；汇总的 `agent_count` 和步骤流都排除它。
- **纯工具调用的回复**：一轮 26 条 `assistant` 记录里多数只有工具调用块没有正文，成卡只会显示"没有正文"；前端不成卡，只计有正文的回复。
- **读限流**：默认展开全部回复卡会同时读几十条记录详情，worker 返回 429。改为默认只展开用户输入、最后一条回复和失败步骤；步骤详情遇 429 按 0.8/1.6/3.2/6.4 s 退避重试 4 次。
- **用户列表排序**：私有会话（`visibility != workspace` 或 `kind == assistant`）对非本工作区成员的超管不可见，被 audience 过滤后用户的统计与"最近活动"只算可见会话，而 SQL 排序键算了全部，出现 "前天" 排在 "3 小时前" 之后。改为页内按可见活动重排，游标仍用 SQL 键。
- **文案细节**：步骤时间原来是截取格式化字符串的第 11–19 位，中文 locale 下成了 "GMT+8 09"；改用 Intl 时钟格式。列表预览去掉 Markdown 的 `**`/`###`/`>` 等符号。

未改的已知项：同一轮的"AI 做了什么"芯片按失败数再按次数排序，长尾折成 "+N"；结论句按工具失败字面陈述（例如"处理文件失败了 3 次"），不推断业务原因。
