# 阿里云 Trace 异常排查记录（2026-09-19）

> 此文保留 09:46–09:54 的初始只读排查现场。10:34 已部署修复并完成服务端完整回放和 19 条隔离事件恢复，见 [修复与验收记录](trace-memory-recovery-20260919.md)。

排查目标：`https://ai.bossipai.com.cn/app/admin/trajectories/sessions/session_7YBXB6W60GNJSP818KH7FM5MHN`。
时间：北京时间 09:46–09:54。环境：上海 gw2，ECS `i-uf66pcsepxpc23v5qsts`。

页面报错的直接原因已经确认：`trajectory-worker` 超过容器 1 GiB 内存限制，被内核 OOM killer 杀死并自动重启，Nginx 对轨迹请求返回 502，部分请求触发 10 秒超时返回 503。长会话的全量快照处理存在明确的内存放大路径，是最强的代码层嫌疑点；本次没有抓到 OOM 瞬间的 Python 分配栈，不能断言具体是哪一行分配触顶。

本次只读取运行状态、日志、数据库元信息、隔离文件的事件标识与类型及源文件哈希。没有部署、重启、修改生产配置、更新数据库或重放数据。下列证据来自阿里云云助手的只读检查；数据库检查强制 `default_transaction_read_only=on` 并设置查询超时。内置浏览器停在登录页，页面失败链路以生产 Nginx/worker 已有请求日志核对，未宣称完成登录后的 UI 验收。

**已确认的运行事实**

| 项目 | 现场结果 |
|---|---|
| worker 镜像 | `openbox-backend:20260918-main-0c19704` |
| frontend 镜像 | `openbox-frontend-v2:20260918-main-0c19704` |
| backend 镜像 | `openbox-backend:20260918-prices-787122f` |
| worker 内存限额 | 1,073,741,824 字节，即 1 GiB |
| OOM / 自动重启 | 近 24 小时内核记录 81 次 cgroup OOM，worker `RestartCount=81` |
| 首次 OOM | 2026-09-19 06:04:32 |
| 最近一次启动 | 09:47:46；09:54:28 复核仍为第 81 次重启 |
| OOM 时内存 | 多条内核日志的 Python `anon-rss` 约 1,040,000–1,045,000 KiB |
| 主机资源 | 总内存约 7.1 GiB，采样时可用约 5.0 GiB；磁盘使用 36% |
| 其他服务 | backend、frontend、postgres 的 `RestartCount=0`，检查时 healthy |
| 暂时恢复状态 | 09:54 worker 约 323.3 MiB，CPU 5.12%；健康接口可返回成功 |

OOM 的 cgroup 对应 `openbox-trajectory-worker-1`。因此本次是 worker 的容器限额被击穿，并非整台 ECS 没有可用内存。自动重启后的 `OOMKilled=false` 或瞬时 `healthy` 不推翻内核日志及累计重启次数。

生产请求日志显示：09:44:48 上游连接被重置；09:44:52–09:44:53 会话头部接口连接被拒绝；09:47:47–09:47:50 checkpoint 返回 502；09:48:06、09:48:17、09:48:28 events 请求约 10 秒后返回 503。09:48:43 起，已有浏览器的头部轮询又能返回 200，但这不能证明完整回放恢复。

截图中的错误卡由 `frontend-v2/src/features/admin-trajectories/components/TrajectorySessionPage.tsx:65` 的 header 错误分支产生，通用错误文案在 `frontend-v2/src/locales/zh-CN/errors.json:2`。它没有把上游 OOM 或 HTTP 错误原因直接展示出来。

**长会话与快照的证据**

| 项目 | 数值 |
|---|---:|
| 目标 trajectory | `trj_309f314843c4485a5f9026052a3e1c32` |
| committed_seq / projected_seq | 16,236 / 16,236 |
| 记录数 / 模型请求数 | 3,257 / 608 |
| checkpoint_seq | 11,155，至 09:54 仍未推进 |
| 最近成功快照时间 | 2026-09-19 06:04:48 |
| 最近成功快照页数 | 23 |
| 快照页解压字节数合计 | 140,617,865，约 134.1 MiB |
| 同一快照页压缩字节数合计 | 3,332,297，约 3.18 MiB |
| 最大单页解压字节数 | 11,778,770，约 11.23 MiB |
| 会话全部 JSON payload 原始字节数合计 | 246,038,541；含不同历史版本，不能当成当前驻留内存 |
| 会话 stored_bytes / budget_level | 26,673,219 / normal |

最新快照本身已经远超 HTTP 的 8 MiB 解码/响应预算，生产日志明确记录 checkpoint 413，以及部分 records 请求 413。9 月 17 日上线的前端逻辑会在 checkpoint 413 后从事件 0 分页回放（`api/sync.ts:265`）；本次日志也看到了 events 0、500、1000 的成功分页，但 worker 的重启与超时会打断该路径。因此不能仅把此前的 413 降级修复视为这次故障的完整解决方案。

代码中可确认的内存放大链路：

1. `backend/trajectory/worker/projection.py:556` 的 `build_checkpoint()` 调用 `expanded_state()`，先构建全量展开状态，再生成每 100 条记录一页的 blob。分页写出发生在全量展开之后。
2. `backend/trajectory/repository.py:303` 的 `_head_state()` 取出全部记录并展开 `$ref`。`payload.py:557` 的 Resolver 在本次读取内保留解析值与展开值；原来去重的模型上下文在序列化快照时会再次写入各条记录。
3. `backend/trajectory/repository.py:424` 的 `store_checkpoint()` 调用 `digest(state)`。`backend/trajectory/types.py:60` 通过完整 `json.dumps(...).encode()` 生成字节后再哈希，会同时产生大字符串/字节缓冲。
4. HTTP 读取预算在 `backend/trajectory/worker/read_limits.py:152` 建立；后台快照没有该请求上下文的预算。`read_budget.py` 也明确注明后台录制没有读取预算。
5. 预算降级主要看事件数与压缩后的 `stored_bytes`，无法衡量这种解压、引用展开和完整 JSON 序列化造成的峰值。

上述三个核心文件在运行中的 worker 内与本地源码 SHA-256 完全相同：`projection.py` 为 `44961b28…`，`repository.py` 为 `5a23132c…`，`payload.py` 为 `543f6059…`。代码定位不是仅依据过时部署文档。

“全量快照/序列化造成 OOM”是结合体积、快照停滞和源码得出的强推断；当前没有分配栈证据来排除 HTTP 回放、录制批次与后台任务叠加后的峰值。09:53 还观察到一个 trace 连接停在 payload SELECT 之后、处于 `idle in transaction` 约 307 秒，这只说明事务内仍在等待客户端处理，不能单凭该状态认定数据库锁或 OSS 故障。

**已确认的数据影响**

全库 43 条轨迹在采样时投影积压为 0，目标会话 16,236 条已提交事件也全部投影。但这些计数不包含被隔离、未入库的事件，不能据此宣称录制没有缺口。

`/var/lib/docker/volumes/openbox_trajectory-spool/_data/quarantine/` 中有 11 个 JSONL 文件及 11 个 reason 文件，总计约 2.9 MB；全部原因为 `batch_crashed`。11 个批次共 5,523 行，其中 5,500 条 `asset.meta`、4 条 `session.meta` 和 19 条事件。19 条事件属于另一个会话 `session_7YBXECM49Z1ZSWZFFC9TQDPC51`，发生于 09:27:36.950–09:27:37.778；其 event_id 在 `trajectory_event_keys` 中匹配数为 0，确认尚未入库。事件类型包括请求增量、用量与结束、工具与权限状态、消息提交等。原始 JSONL 仍在，存在进行去重校验后补录的材料；本次没有实施补录。

`backend/trajectory/worker/ingest.py:1266` 通过进程遗留的 in-flight 标记给录制批次累计崩溃次数，三次后由 `ingest.py:553` 隔离整个批次。该标记证明“进程死亡时这个批次正在处理”，并不能证明该批次是 OOM 来源。在 HTTP、快照与 ingest 共用进程时，这个机制可能把其他任务导致的进程死亡归到正常录制批次，扩大影响。现有隔离文件大多只有约 260 KB 的元数据，也不支持直接认定它们本身就是异常大输入。

以上是 Trace 录制缺口，未发现业务聊天消息丢失的证据。本次未完整核对业务消息表，不能把两类数据混为一谈。

**建议处理顺序（尚未执行）**

1. 先保全这 11 个隔离批次及 reason 文件，避免后续隔离区的保留期限/容量清理妨碍补录。现有默认策略为 7 天、256 MiB。
2. 短期仅针对 worker 止血：根据主机余量临时增加内存限额，并限制大快照/历史回放的并发或生成开销，持续验收重启数、峰值内存与快照进度。增内存不是长会话增长的根治方案；不建议放大 HTTP 8 MiB 保护来掩盖问题。
3. 根治快照内存：保留内容引用、按页读取和生成、流式哈希；为后台解码和展开建立独立字节预算，避免一次构造全量 JSON。隔离重型快照任务的内存故障域，并检查超时取消能否释放资源。变更需覆盖已有快照兼容、历史回放和摘要校验。
4. 修正崩溃归因/隔离策略，避免其他任务 OOM 导致 ingest 正常批次被丢弃。修复后对 19 条事件和元数据批次做幂等重放与序列一致性验证；不能直接移动文件后无校验重试。
5. 验收目标：目标会话完整回放成功、checkpoint 可推进或可靠使用有界回放、worker 无新增 OOM、隔离数据处理有明确结果。监控同时看 OOM/重启次数、快照年龄及录制缺口，不能只看健康接口和投影积压。前端应展示可区分的 502/503/413 状态与恢复入口。

主要云助手取证调用：`t-sh06xhrgdmi8ao0`（运行时与内核日志）、`t-sh06xhrlip4eeps`（会话与快照体积）、`t-sh06xhrq9cqnta8`（范围与源码哈希）、`t-sh06xhrsxcwohds`（隔离与指标）、`t-sh06xhrvcedstmo`（隔离批次结构）、`t-sh06xhrz2qq7lds`（未入库事件及事务状态）、`t-sh06xhs3t8v730g`（09:54 最终快照）。报告没有保存认证令牌、用户消息正文或素材内容。
