# 会话轨迹存储与回放验证

日期：2026-09-11。对应 worktree：`OpenBox-trajectory`。本报告只覆盖 Recorder、数据库、投影、受控内容和超管读取 API；真实 Agent 全链路、WebSocket 和前端交互由总体验收记录另行给出。

## 已实现的边界

- 根会话归属由 frozen TraceContext 携带；子会话沿真实 parent 链验证，不能跨用户或拼接无关会话。current/bind 使用 contextvars；后台工作持久化原 context。
- 事件和业务写入可以共用外层事务。事件编号、事实日志、最新投影和摘要同事务提交；通知只在外层 commit 后发布。event_id 重试内容不同会拒绝，回滚不消耗编号。
- PostgreSQL/SQLite 使用数据库锁和原子 UPDATE 分配连续序号。两个独立进程首次接入同一根会话的专项发现并修复了双唯一约束竞争：无指定单一唯一索引的 ON CONFLICT DO NOTHING，加锁后再次检查归属；没有改动已冻结的 schema。
- 最新记录页只取摘要；固定 H 的详情、记录、统计和 Agent 树使用 H 之前的 checkpoint 与事件重放，不将最新内容提前显示。READ COMMITTED 下旧 header 与已前进缓存的竞争也有验证。
- Stream receipt 只有持久化后完成；每根 4 MiB、实例常规队列总量 64 MiB 反压，不丢 chunk。超出队列阈值的单条内容直接走持久化 staging，不进入常规队列。记录失败会拒绝 receipt；新的 run 恢复时先写明确 gap，旧队列不会永久毒死会话。
- 关闭记录不创建新轨迹。已有轨迹下一次业务活动写暂停边界，恢复时追加 gap 和新 baseline；不补造停用期间执行。普通用户不能读，超管读取在每个 HTTP 请求检查当前数据库角色，不依赖 JWT 旧角色或当前工作区。
- 大内容 bytes 在数据库事务内先持久化 staging；Blob 上传、回读验证在事务外由 worker 执行。Blob 故障时仍可读已提交 staging。显式删除覆盖历史水位，归档和导出迟到完成不能复活已删内容。
- Export 固定水位，保留 manifest、事件、统计和可用 payload 的哈希；记录缺口、未知事件和缺失内容会使 complete=false。进程关停取消导出任务，durable pending/running 可以启动恢复；显式删除状态不会被覆盖。
- 原始 JSON 参数字符串、转义/未完成片段、Bearer、签名 URL 与嵌套字段均在持久化前脱敏。凭据参数的 JSON Schema 类型/约束保留，实际默认值/示例值去除。未知事件/版本显示 unsupported，缺失用量和无开始证据的耗时保持 unknown/null。

## 自动化验证

命令在 backend 下执行，使用仓库 Python 环境：

```sh
PYTHONPATH=. python -m pytest tests/unit/test_trajectory_projection.py tests/unit/test_trajectory_migration.py tests/integration/test_trajectory_storage.py -q
```

最新 SQLite/纯投影/实际迁移验证：29 项通过。使用独立本地 PostgreSQL 数据库运行同一存储集成文件：15 项通过。PostgreSQL 测试包括实际首次并发接入，不预先创建轨迹避开竞争。数据库连接信息只在进程环境中传入，证据文件不含凭据。

后续读取竞态专项 `test_trajectory_read_races.py` 使用真实应用与 JWT：SQLite、PostgreSQL 各 11 项通过。覆盖 payload/export 的 Blob 读取途中账号降权、token 撤销、源 asset 删除、payload 删除、root 删除，以及 JSON/错误/ticket 的 no-store。服务端在返回内容前用新的数据库 session 和原请求认证重新检查状态。另有完整当前 Alembic head 链的 `test_tool_exposure_migration.py` 三项通过，保留 private state/tool identity 的 unsafe downgrade 拒绝断言；修复了既有 e1f3a5b7c9d2 类型变更的 SQLite batch 兼容。

真实前端联调发现文件记录的 ID 含 `/` 时，普通 path 参数会提前截断并返回 404。详情路由改用完整 path converter，仍将 ID 当作数据库标识而非文件路径使用；新增三项真实 ASGI 编码验证，覆盖 `/`、字面 `%2F`、Unicode、空格及 `?/#`，无需客户端二次编码。更新后的 SQLite 读取专项共 14 项通过。

最终跨 chunk 脱敏验证：[`test_trajectory_stream_redaction.py` 词法单测](../backend/tests/unit/test_trajectory_stream_redaction.py) 41 项，加上[同名真实数据库专项](../backend/tests/integration/test_trajectory_stream_redaction.py) 13 项，共 54 项通过（3.75 s）。覆盖所有拆分位置、短 sk/JWT 前缀、Bearer/Authorization、签名 URL、转义/带标点的敏感 JSON 键、嵌套值、100k 累计输出切换增量、并发 Request/Call/Block 隔离、完整非流式响应与工具 Schema 保真。数据库专项逐项检查原始事件、raw/blocks、内联与外置 payload、各历史水位、投影/search_text 和 ZIP，避免只检查最终替换视图。另确认私密 raw 字段不会建立可在 EOF 吐出尾片的内容流。实现为 [`stream_redaction.py`](../backend/trajectory/stream_redaction.py)，在持久化前完成；未知 provider 嵌套 delta 也使用独立路径状态。最终执行命令为 `PYTHONPATH=. python -m pytest tests/unit/test_trajectory_stream_redaction.py tests/integration/test_trajectory_stream_redaction.py -q`，文件随后冻结。

存储集成覆盖：事务回滚与通知、相同 ID 重试冲突、并发首次接入、固定水位/检查点、源会话归属、角色降权与所有读取路径、删除后的历史/导出、Blob 归档故障、流屏障、READ COMMITTED 竞争、暂停恢复新基线、失败队列恢复、checkpoint 页复用、跨根会话总字节反压、非法游标、导出取消恢复与删除竞争。共享 fixture 覆盖 request blocks[]、不同 chunk 类型、Unicode 长预览、工具未开始就失败、系统变更、子 Agent、问答和迟到结果。

真实进程专项：`python -m trajectory.benchmark_concurrency --output ...` 使用 multiprocessing spawn，两进程通过 barrier 同时开始，每个写 25 个事实并重试其中一条。SQLite 通过；PostgreSQL 连续 5 轮通过。每轮都有不同 PID，最终均为 51 个唯一事件（含 trajectory.started）、连续 seq 1–51、committed_seq=51、next_seq=52。它与同进程 asyncio 并发测试是不同的证据。

## AC15 真实进程强杀与恢复

`python -m trajectory.benchmark_process_crash --output ...` 在独立数据库中运行真实 `create_user_message → start_run → RequestCapture → ToolHooks → define_tool`。工具先向本地 fixture 文件追加一条副作用凭据并 fsync，再调用实际 `ToolContext.update_output`，由 hooks 提交 `tool.output`。父进程读到已提交水位后，仅对自己创建并核对 PID 的执行子进程发送 SIGKILL；没有执行异常处理或伪造 `tool.finished`。

恢复使用另一个全新 spawn 进程和数据库连接。fixture 将租约配置为 2 s，等待真实时钟到期后调用实际 `recover_expired_runs`，没有直接改写数据库租约。随后重复恢复一次，确认没有重复工具调用；只有显式创建新用户输入才调用新的 `start_run` 并正常执行同一工具。

| 检查 | SQLite | PostgreSQL |
|---|---:|---:|
| 被强杀执行进程 PID / 恢复进程 PID | 81210 / 81212 | 81716 / 81719 |
| 被杀前已提交事件 / 水位 | 15 / 15 | 15 / 15 |
| lease expiry 恢复后水位 | 16 | 16 |
| 原工具、请求、运行终态 | unknown | unknown |
| 自动重执行副作用次数 | 0 | 0 |
| 新输入完成后水位 / generation | 34 / 2 | 34 / 2 |
| 新工具和运行终态 | completed | completed |

两次执行均验证：被杀进程退出码 -9；恢复进程 PID 不同；原始 1–15 事件的完整 canonical SHA256 不变；固定水位和恢复后的投影都保留工具输出；全部 seq 连续；恢复不写假成功事件。副作用文件最终恰有两条，分别来自强杀前运行与用户明确的新输入。旧工具和运行在新运行完成后仍为 unknown。

这里的生命周期、工具验证与 hooks、Recorder、数据库 commit、租约过期恢复和水位重放均为真实实现。模型请求使用确定性的 `RequestCapture` 适配器输入，没有调用模型 SDK；外部工具副作用使用本地 fsync 文件替代。此证据覆盖进程突然退出，不覆盖机器断电、数据库故障切换或云沙箱。普通测试套件通过 `tests/integration/test_trajectory_process_crash.py` 自动运行独立 SQLite 版本，PostgreSQL 使用同一脚本和明确指定的专用本地可弃库。

## 10 万事件规模实测

运行环境：macOS 26.6.2、ARM64、Python 3.12.14；PostgreSQL 为本机 Docker，SQLite 为本机独立临时文件。各次使用专门的可丢弃测试数据库，不触及项目业务库，不调用外部模型、工具或云端资源。P95 使用 nearest-rank `ceil(0.95 × n)`；原始读样本保存在 JSON。

每个数据库的主轨迹为 5,000 次请求、100,001 个事件、10,001 个投影记录；每个请求 20 个事件，17 个约 95 字符 delta，初始输入约 840 字符；每事务 200 个事件。含 4 个 agent_id，规模测试本身是一名 writer，不冒充多进程子 Agent 的执行压测。测试显式每 10,000 事件做 checkpoint；生产 worker 的候选阈值是 1,000 事件，每 5 s 检查。

| 指标 | SQLite | PostgreSQL |
|---|---:|---:|
| 200 事件事务提交 P50 | 32.97 ms | 38.89 ms |
| 200 事件事务提交 P95 | 44.15 ms | 81.51 ms |
| 最新会话 header P95（10 次） | 25.34 ms | 15.08 ms |
| 最新 100 记录摘要页 P95（10 次） | 18.54 ms | 8.54 ms |
| 不同历史位置 seek P95（10 次） | 712.77 ms | 841.31 ms |
| 写入含 10 次 checkpoint 总时间 | 21.50 s | 28.21 s |

每次 seek 都断言精确目标水位，并检查所有记录的 applied/as_of 序号不晚于目标。主轨迹统计为 5,000 次请求、1,200,000 input tokens、600,000 output tokens，与明确的人工输入一致；没有假造 run 执行时间。

每次运行的 checkpoint staging 内容约 31.8 MB、110 个不可变 payload 页。这不是数据库总容量估计；事件、记录 JSON、索引、WAL 和真实二进制文件还会增加占用。未变化的 checkpoint 页重复使用，避免每次永久复制全部旧输出。

同环境更早一轮 PostgreSQL 的 200 事件批提交 P95 曾达到 184.20 ms，说明本机缓存、I/O 和并行工作负载会影响尾延迟。最终表格是完整保留读样本的最近一次运行，不能将它作为所有部署负载的上限。

## 实际流式 receipt 延迟

`python -m trajectory.benchmark_stream --output ...` 调用实际 record_stream，4 个并发根会话、2 个用户、每根 40 个 256 字符 chunk，总计 160 个观测；使用默认 50 ms 合批窗口。计时从调用到 durable receipt 完成，包含排队、反压、序列分配、投影和 commit。

| 指标 | SQLite | PostgreSQL |
|---|---:|---:|
| receipt P95 | 60.30 ms | 72.48 ms |
| receipt 最大值 | 113.27 ms | 86.66 ms |

这是执行生产者等待存储引入的延迟代理指标。它不包含真实模型、远端数据库/Blob 网络、WebSocket 传输或浏览器 paint；因此不能单独据此宣称整条产品链路“可见输出延迟 P95 ≤100 ms”已经验收。当前本机摘要读取与普通 seek 满足计划给出的服务端目标；多用户长期负载、分布式 worker 与浏览器首屏需要独立验收。

## 发布与运行注意事项

先执行 `f6a8c0e2b4d6` 迁移并通过 readiness 检查，再灰度开启记录/查看。迁移包含七个轨迹表、SessionExecution.trace_context 与 CronRun.trace_context；桌面旧 SQLite 的自动列升级必须一致。两项开关默认关闭，记录白名单清空后才覆盖所有用户，查看权限始终仅超管。

后台每 5 s 尝试 Blob 归档、checkpoint 和删除 GC；Blob 失败保留 DB staging 内容重试。部署方需为 pending staging 行数/字节/最旧年龄、归档失败日志、checkpoint 落后水位、删除积压与 recording_failed/gap 设置监控。当前未提供与现有监控平台整合的 dashboard/告警规则，也没有远端 Blob 故障容量压测。记录正常长期保留，显式删除才释放对应内容；回滚关闭开关不删除历史。

导出目前使用内存 ZIP；多 GB 产物导出的峰值内存和远端传输未压测。常规队列上限不包括 SDK 已经交付的单个超大 chunk 的原始内存及正在直接 staging 的内容；生产者必须等待 receipt，不能无限创建未等待的任务。回放大历史仍需要加载目标 checkpoint 状态，当前实测范围为 10,001 个记录。

## 原始证据

- [SQLite 规模数据](trajectory-verification/sqlite-scale.json)、[PostgreSQL 规模数据](trajectory-verification/postgresql-scale.json)
- [SQLite 流式 receipt](trajectory-verification/sqlite-stream.json)、[PostgreSQL 流式 receipt](trajectory-verification/postgresql-stream.json)
- [SQLite 双进程](trajectory-verification/sqlite-processes.json)、[PostgreSQL 双进程五轮](trajectory-verification/postgresql-processes.json)
- [SQLite 进程强杀](trajectory-verification/sqlite-crash.json)、[PostgreSQL 进程强杀](trajectory-verification/postgresql-crash.json)
- 可复现脚本：`backend/trajectory/benchmark.py`、`benchmark_stream.py`、`benchmark_concurrency.py`、`benchmark_process_crash.py`；PG 环境变量只接受本地地址和 `openbox_trajectory_storage_` 前缀的可丢弃数据库。

这些证据不替代总体验收矩阵中的真实生产入口、超管 WebSocket、前端权限清理、虚拟化和回放操作验证。
