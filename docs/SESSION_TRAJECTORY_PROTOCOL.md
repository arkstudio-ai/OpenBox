# 会话轨迹协议 v1 / M0

本文件是本次实施的 Python / REST / WebSocket 协同契约，与 [实施计划](SESSION_TRAJECTORY_IMPLEMENTATION_PLAN.md)、[UI 字段规格](SESSION_TRAJECTORY_UI_SPEC.md) 配合阅读。本版以 worktree 中实际实现为准；性能与验证证据见 [存储验证报告](SESSION_TRAJECTORY_STORAGE_VERIFICATION.md)。

写入与查看开关独立：`TRAJECTORY_RECORDING_ENABLED=false`、`TRAJECTORY_ADMIN_ENABLED=false` 是兼容部署默认值；分别由 `TRAJECTORY_RECORD_USER_IDS` 与 `TRAJECTORY_ADMIN_USER_IDS`（逗号分隔，空表示全部）灰度。正式开启所有用户采集时清空记录用户白名单。`enabled(None)` 只检查总开关，指定 user_id 后才判断采集范围。超管自己的查看白名单与被查看会话的 owner 无关；普通用户始终无读取权限。读取不可初始化轨迹。

## Python 写入

模块 `trajectory` 公开：

```python
@dataclass(frozen=True)
class TraceContext:
    user_id: str
    session_id: str  # 归属会话
    source_session_id: str | None = None  # 默认为 session_id
    workspace_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    generation: int | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    step_id: str | None = None
    request_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    message_id: str | None = None
    part_id: str | None = None
    caused_by_event_id: str | None = None
    def derive(self, **changes) -> TraceContext: ...
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, value: dict) -> TraceContext: ...

current() -> TraceContext | None
bind(context) -> contextmanager  # with bind(ctx): await operation()
enabled(user_id: str | None = None) -> bool
context_for_session(db, user_id, session_id, **ids) -> TraceContext
ensure_trajectory_in_tx(db, context, baseline: dict | None = None) -> SessionTrajectory
append_events_in_tx(db, context, events: list[dict]) -> PendingRange
record(type, data, *, context=None, db=None, event_id=None, occurred_at=None, **ids)
record_stream(context, event: dict) -> Awaitable[dict]  # 返回持久化后事件
flush(context=None) -> str  # 已提交水位
mark_capture_paused_in_tx(db, user_id, session_id) -> None
delete_trajectory_in_tx(db, session_id, user_id) -> None
```

低层 `ensure/append` 显式调用不检查灰度开关，供已经确认启用的业务事务使用。无 context 时 `record` 不写入；关闭采集时不保存业务内容，但已有轨迹在下一次业务边界写入一条最小 `recording.gap`（phase=paused），不创建新轨迹。恢复后写 phase=resumed 的 gap 和新的实际 baseline，停用期间内容不补造。新的 baseline 身份保存在同一外层事务的 session.info 中，避免附件保存提前 ensure 导致恢复基线漏记。

`record` 有 db 时不提交，返回 PendingRange；无 db 时自己开短事务，返回已提交事件。低层返回值在外部事务 commit 前不是可见回执。生产者必须在业务写锁之后调用 ensure/append；多会话业务锁按 session ID 升序，最后轨迹行锁。Recorder 不获取业务行锁。PostgreSQL 使用轨迹行锁与 UPDATE RETURNING；SQLite 使用原子 UPDATE/UPSERT 的数据库写锁。不使用进程内 seq 或 MAX+1。

`context_for_session` 对 owner 和 source_session 匹配的当前上下文继承原根会话，否则构造新根。source_session 必须属于 owner，且为根会话或它的后代；workspace_id 不可伪造。需要活跃租约的事件验证 source SessionExecution 的 run_id/generation；title/suggestions 辅助请求只在 generation 未变化时允许关联刚结束的原 run。后台入队时持久化原上下文，恢复不从当前 UI 会话猜测归属。

流式 receipt 在事件持久化提交后才完成，`flush(ctx)` 等待结构事件之前已排队的 chunk；无 ctx 且无当前绑定时 flush 全部本实例队列。队列受每根会话及实例总字节上限约束，达到上限反压生产者。单个超大 chunk 通过流屏障后直接写入持久化 payload staging。写入失败以 `RecordingError` 或 `TrajectoryError` 向上传播，所有未提交 receipt 明确失败。失败队列终止后允许新 run 恢复，并在下一次成功提交前写入 phase=recovered 的缺口，保留旧 run、已提交水位及未提交结果 not_recorded。不能把记录失败当成继续执行的理由。

事件输入是 `{type, data, event_id?, occurred_at?, version?, ...关联ID覆盖}`。一次逻辑事实写入前生成 event_id，提交结果未知时复用；相同 ID 内容不同报错。`occurred_at` 为 UTC aware datetime 或 ISO 字符串。新轨迹第一条是 trajectory.started；baseline 由生产者提供实际业务快照。所有关联会话归属检查，删除会话不能再写。后台入队元数据必须保存 `ctx.to_dict()`，恢复用 `TraceContext.from_dict()`。

## 事件与投影

REST 信封字段：`event_id, trajectory_id, user_id, session_id, source_session_id, seq, type, version, occurred_at, recorded_at, data` 和非空 context IDs。`seq`、所有水位及 start/end/applied 序号都是十进制字符串，前端用 BigInt 或长度/字典序比较。协议版本与 projector_version 均为 1。

事件类型来自主计划 5.2；每个类型注册关联ID需求及增量语义。实际模型 chunk 的主格式为 `request.delta.data={chunk_index,mode:"delta"|"replace",blocks:[{block_id,type,delta,...}],raw?,elapsed_ms?,purpose?}`，一个实际 chunk 可同时含 reasoning、text 和 tool arguments，保留它的共同 chunk_index。兼容单块 `block_id,block_type,chunk_index,delta`。`tool.output` 包含 `mode=delta|replace,output,chunk_index`；`request.usage` 使用 `mode=replace|delta`，实际 token 位于 `usage`。结束事件 `status` 统一为 `completed,failed,cancelled,denied,timed_out,unknown,waiting`，reason/error/duration_ms 在 data 中，缺失不能用 0 替代。开始/增量的内部状态可为 pending/running/streaming/accepted 等。

大 JSON 的原始事件仍通过受控 payload 引用读取，列表不加载长内容。`data` 可为 `{"$payload": {"payload_id":"…", "sha256":"…", "size_bytes":123, "media_type":"application/json", "availability":"available"}}`；事件接口需要展开时用 `include_data=true`（默认 true），返回的展开值经过当下删除状态检查。payload 端点按 `through_seq` 检查首次可见位置，不提供公开签名 URL。凭据及 provider 私有字段在哈希、日志、摘要和 payload 写入前删除/脱敏，包含完整、嵌套和未完成 arguments_raw 字符串；JSON Schema 中凭据参数的类型与约束仍可查看，实际默认值/示例值脱敏。

流式正文与参数使用每 Request/每 block 独立的词法脱敏状态，工具输出使用每 Call 独立状态。尚不能判定的短前缀暂缓发布；敏感值不能先提交再从最终文本移除。`blocks[].redaction` 标记处理版本、是否脱敏及是否暂存前缀。对应 raw 正文可用 `{"$stream_blocks":["block_id"],"availability":"sanitized_reference"}` 引用同事件的安全块，`raw_content_mode=sanitized_stream_references_and_complete_fields` 说明表示方式；完整非流式业务响应和其他公开字段仍保留结构脱敏后的值，不能被空引用替代。未映射的真实 raw delta 使用独立路径状态并标明 `sanitized_inline_delta`。

EOF 若有已确认安全的待发尾片，追加一条 `request.delta`，显式带 `source=recorder_redaction`、`redaction_control=finalize`、`observed_chunk_count`。这是录制器缓冲收尾，不能算成模型额外 chunk 或新 Request；`request.finished.chunk_count` 仍是实际观察到的 provider chunk 数。其尾片参与相同有序投影，所以从头、分批和 checkpoint 回放一致。Schema 在进入 Recorder 之前的公开字段过滤同样识别 schema 子树，不能删除名为 password/token 的参数定义后只留下 required 名称。

纯投影 `reduce(state,event)` 保持稳定 record_id：request:`request:{request_id}`；assistant:`assistant:{request_id}`（无 request 时 message ID）；tool:`tool:{call_id}`；user:`user:{message_id或event_id}`；run/turn/step/agent/job/question/permission 各按对应稳定 ID。一次更新修改同一对象，重试新 request_id 不覆盖旧请求。

记录统一字段：`record_id,kind,title,preview,result_preview,status,status_reason,turn_id,run_id,step_id,request_id,agent_id,parent_agent_id,call_id,parent_call_id,source_session_id,message_id,part_id,start_seq,end_seq,as_of_seq,started_at,finished_at,duration_ms,timing_source,data,blocks,usage`。缺失关联字段可为 null。`data` 保留该对象在指定水位前所有结构事件已捕获的键；增量在 blocks/output 归并；详情另返回 `events` 原事件列表。`kind` 为 user/assistant/request/tool/system/context/agent/permission/question/interrupt/resume/retry/compaction/job/artifact/plan/todo/skill/tool_catalog/takeover/settings/history/baseline/gap/late_result/turn/run/step。工具 data 中区分 requested_arguments、effective_arguments、arguments_raw、schema、output、model_output；请求 data 中 input/options/capture_level/purpose/provider/model；其余字段遵循 UI_SPEC 4 节。未捕获字段保持不存在，可用性由 UI 显示 not_recorded，不能补造。

`blocks` 的投影结构为 `[{block_id,type,text,chunk_index?,...捕获的块元数据}]`。只有实际 tool.started 才能产生工具执行开始时间；参数校验失败、拒绝或没有开始证据时 duration_ms 保持 null，不把审批等待当执行耗时。run.interrupted 将仍未完成的 request/tool/assistant/step 关闭为 unknown，迟到结果使用单独的 late_result，不覆盖原终态。空 assistant Message 不制造第二条 AI 输出，后续 Part 按 message_id 关联已有请求输出。

System 行由 `request.prepared` 中实际 system/instructions/messages/input 和 tools 派生：首次或实际变化时生成 `system:{request_id}`，data 中为 `system,tools,before:{system,tools}|null,source_request_id,capture_level`。这是一条可追溯到真实请求的展示记录，不冒充新的模型调用。

Checkpoint state 的完整传输形状固定为：

```json
{"projector_version":1,"through_seq":"0","records":{},"unsupported_events":[],"coverage_start":null}
```

records 为 record_id 到上述完整 Record 的映射。服务端内部按 100 条记录切分不可变页，重复利用未变化页；REST 返回时展开并校验完整 state digest，客户端按版本、序号和 shape 检查，不能另猜浮点 JSON 的哈希规则。`header.agents` 为 `[{agent_id,parent_agent_id,source_session_id,name,status,record_id}]`。共享 fixture 同时包含 expected_state、expected_statistics、expected_agents 及历史位置 state，覆盖 Unicode 长文本、真实 blocks[]、系统变化和无 tool.started 失败案例。

## REST

所有路由在 `/api/admin/trajectories`，依赖当前 DB 账号有效且 role=admin（不信任 JWT role）。关闭读取返回 404；无认证 401；普通/降权用户 403。不受 X-Workspace-Id 限制。403/401 前端立即清理数据与订阅。JSON 错误使用 FastAPI detail，损坏/不支持返回明确 code。

所有管理 JSON、校验/权限错误和专用 ticket 响应均使用 `Cache-Control: no-store`。payload/export 下载在 Blob I/O 后、返回内容前以新数据库 session 重新检查 root、payload、源 asset、export 状态，并重新解析原请求认证以验证 token 过期/撤销与当前 DB 超管角色；读取途中删除或降权不会继续返回已取到的字节。

- `GET /sessions?user_id=&user_query=&q=&workspace_id=&status=&recording_status=&activity_from=&activity_to=&include_unrecorded=false&cursor=&limit=50&sort=last_activity_desc` -> `{items,next_cursor,has_more}`。sort 可为 last_activity_desc/last_activity_asc，cursor 绑定全部筛选与排序。最多 200 行；默认只列已有轨迹。
- `GET /sessions/{sid}?through_seq=H` -> 会话 header；未记录会话仍 200，trajectory_id=null，seq="0"。
- `GET /sessions/{sid}/records?through_seq=H&before=&limit=100&kind=&status=&agent_id=` -> `{items,next_cursor,has_more,through_seq,projector_version,unsupported_events}`，默认从尾部读取，items 按 start_seq 正序。每页最多 500，摘要不含 data/blocks；before 绑定水位，切换筛选要清空 before。
- `GET /sessions/{sid}/records/{record_id}?through_seq=H` -> `{record,through_seq,projector_version}`。record_id 是不透明字符串，可含 `/`、`%` 和 Unicode；客户端用 encodeURIComponent 编码一次，服务端使用 path converter 接收完整 ID，不二次解码。水位前尚不存在为404。
- `GET /sessions/{sid}/events?after_seq=0&until_seq=H&limit=500&include_data=true` -> `{events,from_seq,through_seq,until_seq,has_more,committed_seq}`。through_seq 是本页最后实际序号，空页等于 after_seq；不支持 kind/agent 筛选。
- `GET /sessions/{sid}/checkpoint?at_seq=H` -> `{checkpoint: null|{through_seq,projector_version,state,digest},through_seq}`；没有 checkpoint 返回 null，客户端从 0 重放。
- `GET /sessions/{sid}/search?q=&through_seq=H&cursor=&limit=50` -> `{items:[{record_id,seq,kind,preview}],next_cursor,has_more,through_seq}`，完整范围搜索固定 H。
- `GET /sessions/{sid}/payloads/{payload_id}?through_seq=H` -> 受控内容 Response（JSON/text/二进制）；尚未产生404，显式删除410，损坏409。
- `POST /sessions/{sid}/export` JSON `{through_seq?:string}` -> 202 `{export_id,status,through_seq,error,download_url}`；`GET /sessions/{sid}/exports/{export_id}` -> 同上，download_url 只在 completed 时为本 API 相对路径；`GET /sessions/{sid}/exports/{export_id}/download` -> zip，每次检查当前超管权限及归属。
- `POST /ticket` -> `{ticket}`，为下面专用只读 WebSocket 签发一次性票据，仍需当前超管认证。

Header 和列表 row 均包含：`session_id,user_id,trajectory_id,title,owner:{user_id,username,email},workspace:{id,name},workspace_id,running_status,recording_status,coverage_start,last_activity_at,model,agent,committed_seq,projected_through_seq,through_seq,statistics`。user_id/owner.user_id 均是被观察会话所有者，不是查看者。header 额外 `agents,capabilities:{recording,admin_read,export},projector_version,unsupported_events`；无轨迹 export=false。statistics 是 `{through_seq,coverage_start,request_count,tool_count,error_count,unknown_count,input_tokens,output_tokens,usage_complete,duration_ms}`；token/耗时未记录为 null，局部 usage 标明 usage_complete=false。

固定 H 的记录/详情/统计/Agent 树均以不晚于 H 的 checkpoint 加事件重建；最新投影不能直接伪装历史版本。已删除内容当前权限始终优先于历史水位。

## 只读通知

`/ws/admin/trajectories?ticket=...` 必须使用专用 `POST /api/admin/trajectories/ticket` 的一次性票据，audience=admin_trajectories，与普通聊天票据隔离，并保存原 access token 的 jti/expiry 以支持撤销和过期。仅接收 `{type:"subscribe",session_id,after_seq?:string}`、`{type:"unsubscribe",session_id}`、`{type:"ping"}`；每连接最多 16 个订阅。响应 `trajectory.available`/`subscribed` 的 data 为 `{user_id,owner_user_id,session_id,trajectory_id,committed_seq}`，user_id 与 owner_user_id 是同一个会话所有者。ping 返回 pong；取消返回 unsubscribed。订阅失败返回 `{type:"error",data:{code,message?,session_id?}}`，失去认证/权限关闭为 4401/4403。禁止任何审批/问答/执行消息。

Recorder 提交后发布内部 Bus `trajectory.available`，data 使用上述 owner/session/trajectory/committed_seq；普通聊天全 Bus listener 排除 trajectory.*。通知只有水位，无 prompt；丢失通知用打开详情的水位轮询补读。客户端始终从自己的连续 last_applied_seq 通过 HTTP 补齐，不能将通知水位直接当已应用。订阅前、每次发送和 1 s 权限检查都重新检查当前 DB 角色、账号状态、票据原 token 的过期/撤销身份，撤权停止推送。

## 持久化归档、删除与部署

事件、投影和大内容引用在同一业务事务提交。大内容的不可变 bytes 先随事务写入 TrajectoryPayload.content（storage_status=pending），所以 Blob 暂时不可用时已经提交的事实仍可读取。写事务不等待外部 Blob 上传/下载。后台 `trajectory.payload.start_archive_worker()` 每 5 s 执行归档、checkpoint 和删除 GC：在事务外上传并回读验证 SHA-256 后，短事务条件更新为 stored 并释放 staging bytes。失败保留数据库内容重试；当前删除状态优先，不能被迟到归档覆盖。

默认参数：TRAJECTORY_INLINE_BYTES=65536、TRAJECTORY_BATCH_MS=50、TRAJECTORY_PENDING_BYTES=4194304、TRAJECTORY_TOTAL_PENDING_BYTES=67108864、TRAJECTORY_CHECKPOINT_INTERVAL=1000。批次等待窗口后可收齐当前有界队列；64 KiB 是事件外置阈值，不是严格的每批事务大小。checkpoint 在后台建立，避免执行线程每个阈值重拷贝整段历史；查询读路径从不触发初始化或执行。

显式文件删除立即将关联 payload 置 deleted 并清除 staging bytes；会话删除将它自己的根轨迹保留为 tombstone，删除事实/投影/checkpoint、撤销 payload 和 export，随后重试物理 Blob GC。删除子会话不抹掉已属于父根轨迹的历史。旧导出在内容删除后失效，不能通过导出绕开当前删除状态。旧水位仍以当下删除授权为准。

导出只读取固定 through_seq 的事件、统计及引用内容，ZIP 内含 manifest.json、events.jsonl、statistics.json 与 payloads。manifest 包含文件哈希、coverage_start、recording_status、gaps、missing_payloads、unsupported_events；有采集暂停/失败缺口时 complete=false，不能把恢复后的轨迹伪装为全程完整。显式删除与导出完成竞争时删除胜出。`trajectory.export.resume_exports()` 启动恢复 durable pending/running 工作；`stop_exports()` 停止进程内任务，保留可恢复状态。应用停机应先停止 Agent，再 flush，最后停止 export/archive worker。

上线必须先执行 f6a8c0e2b4d6 迁移：七个轨迹表、SessionExecution.trace_context 与 CronRun.trace_context。桌面旧 SQLite 的字段升级和数据库 readiness 检查均须匹配；默认关闭开关不能替代迁移。应持续观察 pending staging 行数/总字节/最早 created_at、归档失败次数、checkpoint 水位落后、删除 GC 积压、recording_failed 和 gap 数；当前提供归档失败日志及可查询持久状态，监控平台告警接入需部署侧配置。

## 验证与发布边界

共享 fixture 位于 `backend/trajectory/fixtures/`，前端验证相同事件和投影输出。开启两个独立开关不代表全部生产路径已验收；coverage 与 M0–M8 完成证据由实施清单维护。未知版本必须显示 unsupported，不静默丢事件。导出和回放无模型/工具调用，只读取数据库和受控不可变内容。
