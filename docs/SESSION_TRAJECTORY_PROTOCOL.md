# 会话轨迹协议 v2

本文件概述轨迹改造后业务 backend（生产者）、spool、轨迹 worker 与超管接口之间的约定，取代 v1 的业务事务内记录器（已下线）。
字段级细节以 [trajectory-rearch/SPEC.md](trajectory-rearch/SPEC.md) §3–§8 为准，SPEC 顶部的 “Scope changes after v1” 优先于其正文；
UI 字段见 [UI 字段规格](SESSION_TRAJECTORY_UI_SPEC.md)，部署见 [deploy/gw2/RUNBOOK.md](../deploy/gw2/RUNBOOK.md)。
旧录制（v1 数据）不保留，也没有转换工具。

## 1. 开关

| 变量 | 含义 |
|---|---|
| `TRAJECTORY_RECORDING_ENABLED`、`TRAJECTORY_RECORD_USER_IDS` | 录制总开关与被录制用户白名单（逗号分隔，空表示全部用户）。 |
| `TRAJECTORY_ADMIN_ENABLED`、`TRAJECTORY_ADMIN_USER_IDS` | 超管查看开关与查看者白名单；普通用户始终无读取权限，关闭查看时管理接口返回 404。 |
| `TRAJECTORY_WORKER_MODE` | `external`：独立的 `trajectory-worker` 进程（生产）；`embedded`：worker 服务作为 backend 进程内的 asyncio 任务运行，管理路由挂在 backend 上（桌面、开发）；`off`：整条链路关闭，不启动 emitter、元数据同步和 worker。默认：设置了 `JWT_SECRET` 时为 `external`，否则 `embedded`。 |

四个录制与查看开关由 backend 与 worker 共用，必须一致。`TRAJECTORY_SINK` 已删除，backend 只写 spool。
录制关闭时不写业务内容，但元数据、删除和暂停/恢复等控制记录照常写。

## 2. 生产者约定

- `emit`、`emit_after_commit`、`emit_control`、`emit_stream` 及兼容入口 `record`、`record_stream`、`flush` 永不抛异常，
  不阻塞也不改变业务行为；业务请求与运行路径上没有轨迹 SQL、blob I/O 或 OSS 读取。
- 与业务写入绑定的事实在外层业务事务提交后才入队：`emit_after_commit`（以及带 `db` 的 `record`、`emit_control`）
  挂在 SQLAlchemy `after_commit` 上，回滚即丢弃，嵌套事务的提交不触发。
- 入队只做大小检查并追加到有界字节队列（`TRAJECTORY_EMIT_QUEUE_BYTES`，默认 64 MiB；单事件上限
  `TRAJECTORY_EMIT_MAX_EVENT_BYTES`）。写线程分配 `n`、追加 JSONL，按大小或时间轮转文件。
- 丢弃不重试也不反压：`queue_overflow`、`spool_full`、`serialization_failed`、`invalid_event`、`event_too_large`、
  `writer_error`、`budget` 按根会话汇总成 `gap` 控制记录（最多每 100 ms 一条），worker 把它转成 `recording.gap` 事件。
- 预算文件 `control/budgets.json` 由 worker 写、生产者读：`degraded` 丢弃中间增量块并截断工具输出，`blocked` 只放行生命周期事件。
- 暂停与恢复不读轨迹表，标记保存在业务列 `SessionExecution.trace_context`：根会话首次录制写 `baseline.captured`
  （`evt_baseline_{root}_0`）；录制关闭后的活动写 `recording.state` paused；重新开启时 `recording_epoch` 加 1，
  写 `recording.state` resumed 与新的 `baseline.captured`（`evt_baseline_{root}_{epoch}`）。
- 请求白名单与流式脱敏留在生产者，通用 `sanitize` 在 worker 执行。spool 可能含未脱敏内容，只能挂载给 backend 与 worker。

## 3. Spool 格式 v2

目录 `TRAJECTORY_SPOOL_DIR`（目录 0700、文件 0600）：

```
producers/<producer_id>/   producer.json、00000000000000000001.jsonl（已关闭）、00000000000000000002.jsonl.part（写入中）
blobs/<sha256>             移出事件行的值，所有生产者共用
control/                   budgets.json（worker 写）、worker.json（worker 心跳）
quarantine/                无法解析的文件及其 .reason
```

每行一个紧凑 JSON 对象，以 `\n` 结尾：

```json
{"v":1,"k":"event","n":42,"t":"2026-09-14T08:00:00.123Z","event":{...}}
{"v":2,"k":"event","n":43,"t":"2026-09-14T08:00:00.125Z","event":{..."data":{"input":{"system":{"$blob":"<sha256>"}}}}}
{"v":1,"k":"control","n":44,"t":"2026-09-14T08:00:00.130Z","control":{"type":"gap", ...}}
```

- `v` 只接受 1 和 2，其他版本整个文件隔离；`k` 为 `event` 或 `control`；`n` 是生产者内从 1 连续的计数（事件与控制记录共用），
  缺号即数据丢失；`t` 为入队时间；未知顶层键忽略。
- `event` 同 `trajectory.types.prepare()` 的输出：`type, version, event_id, occurred_at`、身份字段（空值省略）与 `data`。
  生产者辅助键 `media_sources`、`asset_ref`、`source_root_session_id` 由 worker 使用后删除。
- `$blob`（v2）：写线程把 `request.prepared` 的 `system`、`instructions`、`tools` 以及 `messages`（或列表型 `input`）每一项中
  紧凑 JSON 超过 `TRAJECTORY_SPOOL_BLOB_MIN_BYTES`（1024）的值，和 `data` 里其他超过 16 KiB 的值写入 `blobs/<sha256>`，
  行内换成 `{"$blob":"<sha256>"}`。blob 先于引用它的行落盘（临时文件、fsync、rename），已存在则刷新 mtime，写不了就内联为 v1；
  spool 预算包含 `blobs/`。
- worker 解码前把引用替换回原值，脱敏、事件哈希与内容寻址和内联行完全一致；blob 缺失或摘要不符时该行变成
  `spool_blob_missing` / `spool_blob_corrupt` 的 `gap`，文件其余部分照常摄取。worker 清扫 mtime 早于最老数据文件 600 s 以上的 blob。

## 4. 控制记录

| type | 主要字段 | worker 动作 |
|---|---|---|
| `gap` | `reason`、`dropped_events`、`dropped_bytes`、首末丢弃时间、`sessions`（≤ 200，含 run/request id） | 追加 `recording.gap` |
| `producer.goodbye` | `last_n` | 标记生产者正常关闭 |
| `session.meta`、`user.meta`、`workspace.meta`、`asset.meta` | 业务行快照 | 按 `updated_at` 后写者胜，upsert 元数据副本 |
| `session.deleted`、`asset.deleted` | id、`user_id`、`deleted_at` | 墓碑化与清理、撤销引用（§8） |
| `recording.state` | `user_id`、根 `session_id`、`state`（`paused`/`resumed`）、`reason`、`at`、`epoch` | 暂停/恢复记账并写 gap |

- 控制记录不含业务内容，录制关闭时也写。删除类控制在业务删除提交后立即写；元数据由 backend 的同步任务在启动时全量发送，
  之后每 `TRAJECTORY_META_SYNC_SECONDS`（30）增量发送，每个周期最多 4 个分页查询。
- `recording.state` 必带 `epoch`（根会话录制周期的编号）：worker 只应用 epoch 比该轨迹上次已应用值更新的记录，
  同一次切换的重复上报与乱序到达都不会改变结果。

## 5. Worker 保证

- **单写者**：每个轨迹库只有一个摄取、投影、归档写者（PostgreSQL advisory lock，SQLite 文件锁）。拿不到锁的进程只提供读接口，
  `/health` 的 `writer` 为 false。
- **投递**：生产者内 FIFO；跨生产者按已关闭文件的先后近似到达顺序。生产者到 spool 最多一次（丢失以 gap 表示），
  spool 到轨迹库等效恰好一次：文件偏移与事件在同一事务提交，再加 `event_id` 去重。
- **seq**：只由 worker 在轨迹行锁（`SELECT … FOR UPDATE`）下分配，每条轨迹从 1 连续递增，seq 1 是 `trajectory.started`；生产者不产生 seq。
- **去重（keep-first）**：按 `event_id` 查幂等表。同轨迹同内容哈希算重复并跳过；哈希或轨迹不同算冲突（计数，每个 id 只记一次日志）并跳过。
  永远保留先到的事件，所以终态事实的重复发送无害。
- **缺口**：`gap` 控制按 run 拆成 `recording.gap`（`phase:"dropped"`）；生产者缺号、残行、隔离文件或崩溃写 `phase:"lost"`
  （`producer_lines_lost` / `producer_crashed`）；暂停、恢复写 `phase:"paused"` / `"resumed"`。gap 事件 id 确定，重放幂等。
- **失败隔离**：blob 上传失败以 1 s → 60 s 退避重试，10 次后内容换成 `{"availability":"not_recorded","reason":"blob_store_unavailable"}`
  并记 gap；同一批连续失败 `TRAJECTORY_INGEST_MAX_BATCH_FAILURES`（10）次即隔离该文件并记 gap，同一生产者的后续文件继续摄取。
- **归属**：已墓碑化或元数据已删除的会话丢弃事件；元数据显示归属不符时丢弃；元数据未知时接受。
- **投影**：worker 按批更新记录、摘要与 checkpoint（事件数与 `TRAJECTORY_PROJECTION_BATCH_BYTES` 双重上限）；读取语义同 v1。

## 6. 引用信封

- `$payload`：`{"$payload":{"payload_id","sha256","size_bytes","media_type","availability"[,"reason"]}}`，外置的整段事件 `data` 与 checkpoint 页。
- `$media`：`{"$media":<payload 引用或可用性标记>,"source_kind","original_encoding","declared_media_type"[,"source_asset_id"]}`，
  解码出的媒体；来源是业务资产时只存引用（`source_kind:"asset"`），不复制字节。
- `$ref`：`{"$ref":{"sha256","size_bytes","media_type":"application/json","kind":"system"|"tools"|"message"|"value","payload_id"}}`，
  轨迹内按内容寻址的 JSON 值：`system`/`instructions` 与 `tools` ≥ 1024 字节、每条 message ≥ 512 字节、其他值超过 `TRAJECTORY_INLINE_BYTES`（65536）。

`availability` 为 `available`、`deleted` 或 `expired`；已删除的内容不会因为新引用而复活。接口默认把 `$ref` 展开回原 JSON。

## 7. 超管接口

路径、参数、响应形状、错误映射与 `Cache-Control: no-store` 保持 v1 契约，改由 worker 提供
（nginx 把 `/api/admin/trajectories/*` 与 `/ws/admin/trajectories` 转给 worker）。只增加可选能力：

- `GET /sessions/{sid}/records/{record_id}?expand=full|refs`：默认 `full` 与 v1 相同；`refs` 时 `record.data` 与 `record.events[*].data`
  中只有 `$ref` 保持不展开，`$payload`、`$media` 不变。
- `GET /sessions/{sid}/blobs/{sha256}?through_seq=H`：响应体就是该 `$ref` 的 JSON 值（`application/json`、`no-store`、
  `X-Content-Type-Options: nosniff`）；未知或在 H 时不可见 404，已删除或过期 410，损坏 409。
- `GET /sessions/{sid}/payloads/{payload_id}?meta=1[&through_seq=H]`：返回 `{payload_id,availability,media_type,size_bytes,sha256}`，
  不含字节，可见性与状态规则同下载。
- 会话 header 的 `capabilities.refs = true`。`/events`、`/checkpoint` 仍返回完全展开的数据。

鉴权：worker 本地校验 JWT（含 Redis 吊销名单），再经 backend 内部接口 `/api/internal/trajectory/viewer` 确认账号状态与超管角色
（缓存 5 s；读取字节后的复核不走缓存）。审计记录进 outbox 批量送到 `/api/internal/trajectory/audit`，
超过 `TRAJECTORY_AUDIT_MAX_ATTEMPTS`（30）次或 `TRAJECTORY_AUDIT_MAX_AGE_SECONDS`（3 天）转为死信。

WebSocket 协议不变。提示 `trajectory.available` 的 data 为 `{user_id,owner_user_id,session_id,trajectory_id,committed_seq}`，
轨迹被墓碑化时另带 `deleted: true`。提示只有水位，客户端仍从自己连续的 `last_applied_seq` 经 HTTP 补读。

## 8. 保留与删除

- **会话删除**：`session.deleted` 使根会话的轨迹墓碑化：删除热事件、记录、checkpoint、摘要与段行，payload 置 `deleted`，导出失效，
  轨迹前缀下的 OSS 对象进入 GC 队列（失败以 30 s → 6 h 退避重试），并发布带 `deleted` 的提示。轨迹行与 payload 行保留，用来拒绝迟到事件与内容复活。
- **资产删除**：`asset.deleted` 把引用该资产的 payload 置 `deleted`，不再被可用行引用的 blob 排队删除，受影响的轨迹追加 `artifact.removed`。
- **内容过期**：最后活动早于 `TRAJECTORY_CONTENT_RETENTION_DAYS`（180 天）的轨迹删除内容，保留摘要行与统计（`recording_status=expired`），读取返回 410。
- **归档**：事件投影后写成 OSS 段（zstd，读回校验 sha256），PostgreSQL 日分区在 `TRAJECTORY_HOT_DAYS`（7 天）后删除；
  幂等键保留 `TRAJECTORY_DEDUPE_DAYS`（3 天），导出保留 `TRAJECTORY_EXPORT_RETENTION_DAYS`（30 天）。
- **spool**：文件在摄取事务提交后删除；总预算 `TRAJECTORY_SPOOL_MAX_BYTES`（2 GiB），超出后新行丢弃并记 gap。
