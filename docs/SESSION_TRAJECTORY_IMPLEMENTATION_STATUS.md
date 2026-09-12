# 会话轨迹实现与本地验收记录

日期：2026-09-11。实现位于独立 worktree `OpenBox-trajectory`，分支 `codex/session-trajectories`，基于 `521338c`。本次修改尚未提交、合并或部署。原项目的云桌面连接问题按用户要求搁置。

本文件记录实际完成范围与证据；产品要求和验收目标仍以 [完整实施计划](SESSION_TRAJECTORY_IMPLEMENTATION_PLAN.md) 和 [UI 字段规格](SESSION_TRAJECTORY_UI_SPEC.md) 为准。协议见 [协议 v1](SESSION_TRAJECTORY_PROTOCOL.md)，逐生产入口与测试索引见 [运行时覆盖清单](SESSION_TRAJECTORY_RUNTIME_COVERAGE.md)，存储、数据库并发与强杀证据见 [存储验证报告](SESSION_TRAJECTORY_STORAGE_VERIFICATION.md)，UI01–UI16 与真实浏览器证据见 [前端验证报告](SESSION_TRAJECTORY_FRONTEND_VERIFICATION.md)。

## 1. 当前交付范围

- 后端记录按会话所有者及根会话组织；主 Agent、子 Agent 和异步作业保留各自实际执行身份。旧会话发生新活动时惰性接入，旧上下文作为 baseline 保存，不回填旧事件。
- 记录与查看开关相互独立。所有用户可以被记录，只有数据库中当前有效的 `role=admin` 平台超管可以读取；会话所有者、工作区管理员和普通用户均无例外。
- 超管 API 支持跨用户列表、固定水位的记录/详情/搜索/检查点、受控内容和导出；实时订阅走专用只读 WebSocket。查看不借用目标身份，不启动 Agent 或沙箱。
- 日志、连续序号和同步投影同事务提交。已提交日志可重放，通知丢失不影响事实来源；缺失、暂停、失败和 unknown 都保留明确语义。
- 工具保留实际可观察的完整输出与传给模型的裁剪版本。文件覆盖保留版本，媒体请求中的原图/派生帧绑定源附件的当前删除权限。
- 前端由专门的前端 agent 管理 Claude CLI 实现。真实浏览器已通过各类详情、列表筛选/返回、普通用户深链拒绝、降权、媒体删除、整会话删除、固定 H 回放和只读网络验证；中英文、深浅色、390 px 窄屏、键盘页签和详情宽度调整均已检查。前端完整检查和构建已通过。

M0–M6 及 M7 的本地实现与专项验证已完成。M7 的完整验收矩阵仍保留未勾选：独立移动设备、部分真实外部服务组合、记录增加的可见输出延迟对照测量，以及下面列出的性能边界尚待发布阶段补充；M8 尚未执行。

默认两个开关均为关闭；本地验收服务单独启用，不会改变原项目运行服务或业务数据库。

## 2. 代码与证据入口

| 能力 | 实现入口 | 主要验证 |
|---|---|---|
| 身份、协议、追加日志、背压 | `backend/trajectory/context.py`、`types.py`、`recorder.py` | 存储集成、运行时单测、双进程脚本 |
| ORM、迁移、桌面兼容 | `backend/db/models/trajectory.py`、迁移 `f6a8c0e2b4d6`、`db/base.py` | 轨迹迁移、完整现有 head 迁移、readiness |
| 新旧会话、Run、Part 原子关联 | `backend/session/session.py`、`question/runtime.py` | session runtime、真实 Agent loop、旧问题恢复 |
| 模型和媒体派发 | `backend/agent/trajectory.py`、`llm.py`、媒体适配器 | 主循环、辅助调用、media dispatch、image/video 账本 |
| 工具、batch、子 Agent | `backend/agent/hooks.py`、`tool/tool.py`、`batch.py`、`task.py` | 实际工具输出裁剪、多层 task、parallel batch |
| 审批、问答、接管 | `backend/permission/permission.py`、`question/`、`tool/desktop_takeover.py` | 审批提交后丢失唤醒、旧问题回答、durable questions |
| 文件、附件与删除 | `backend/trajectory/artifacts.py`、`files.py`、`lifecycle.py`、`api/assets.py` | 实际文件工具、原图/派生媒体删除、读取竞态 |
| 后台作业与迟到回调 | `backend/trajectory/jobs.py`、`cron/`、视频/发布恢复入口 | 原上下文恢复、重复回调、删除后拒绝、真实 SIGKILL |
| 投影、固定水位和检查点 | `backend/trajectory/projector.py`、`repository.py` | Python/TypeScript 共享 fixture、历史读取竞态、规模脚本 |
| 超管读取与专用订阅 | `backend/api/admin_trajectories.py`、`admin_trajectory_ws.py` | 真实 ASGI/JWT/WebSocket、降权和 token 撤销 |
| 受控 Blob 与导出 | `backend/trajectory/payload.py`、`export.py` | 持久化 staging、归档故障、下载途中删除/撤权、恢复导出 |
| 超管列表、检查器和播放器 | `frontend-v2/src/features/admin-trajectories/`、`src/routes/admin/` | 共享投影、组件、浏览器 fixture 和原生路由浏览器验证；证据类别在前端报告分别列出 |

模型记录区分 `provider_wire` 与 `adapter_input`：直接 HTTP 派发可以捕获最终业务 body；LiteLLM/OpenAI/IMS SDK 前的输入不冒称 SDK 的网络字节。每次实际可观察派发有独立 Request ID，SDK 内部尝试显示 `not_observed`。没有返回的模型内部推理不生成内容。

HTTP 作业提交的成功表示请求已接受，视频生成完成仍以作业终态为准。轮询是作业进度，不计作新模型请求；统计引用既有 UsageEvent，不新增计费。

跨片段脱敏使用独立词法状态，覆盖分片的参数名/值、Bearer、短 token 前缀、签名 URL 及 Bash 超出聊天预览预算后的输出。raw 中的流式正文可以引用对应安全 blocks，非流式业务响应保留；EOF 缓冲收尾显式标识，不增加实际模型 chunk 数。修复同时保留 Schema 中敏感参数的定义/约束，只脱敏实际值和默认示例。

## 3. 验收矩阵的实际证据

下表区分已经执行的服务端及浏览器验证与剩余检查。后端测试文件均在 `backend/tests`；浏览器使用独立测试数据库与真实应用路由。确定性外部替身不等同于真实付费 provider、远端云沙箱或多节点生产部署。

| 计划项 | 已执行证据 | 范围与剩余检查 |
|---|---|---|
| AC01/02/35 | `integration/test_trajectory_storage.py` 的跨用户列表、分页及全部读取路径；`test_trajectory_boundaries.py` 的专用 WS；真实浏览器筛选用户 B、进入详情、返回保留查询 | 普通用户真实登录并打开本人轨迹深链被拒绝，没有发出轨迹管理 API 请求；大列表分页及切换竞态见前端专项 |
| AC03/04/05 | `test_trajectory_agent_loop.py`、`unit/test_trajectory_session_runtime.py`、未记录会话读取断言 | 使用真实输入事务、Run、主循环和日志；只读不会创建轨迹 |
| AC06/12/13 | `test_trajectory_boundaries.py` 的旧问题接入、审批提交后丢失 Redis 唤醒；现有 durable questions 回归 | 不引入新的用户答题超时策略；已有实际取消/终态被记录 |
| AC07/08 | `unit/test_trajectory_runtime.py` 的逐派发身份、chunk、取消；现有 llm/provider/native 回归 | SDK 内部重试不观测；媒体显式重试也检查新请求 ID |
| AC09/10/11 | parallel batch 单测、`test_multilevel_task_uses_one_root_and_independent_child_runs` | 真实 root→middle→leaf、三 Run、父工具关联和回传；耗时按实际区间 |
| AC14/16 | session generation 过期写入拒绝、cron 原目标注入、后台迟到/重复回调 | 原终态不被晚到结果复活；删除后的源对象不重建 |
| AC15 | `test_trajectory_process_crash.py` 与 SQLite/PG crash JSON | 工具副作用及 seq15 已提交后真实 SIGKILL，新进程自然租约到期恢复为 unknown，无自动重执行 |
| AC17/18/19 | 存储 READ COMMITTED 竞态、共享投影 fixture、客户端同步测试；独立进程提交新事件但不通知前端服务，真实浏览器轮询补齐 | H17 时选中记录、数量和输出保持不变，回到实时约 1.29 s 收敛；这是丢通知恢复时间，不是正常推送延迟 |
| AC20/25/26 | 固定 H 的 records/detail/search、历史 checkpoint 测试；浏览器过滤/搜索/虚拟化；真实 100,009→75,200→17 回放 | 延迟历史响应期间没有未来摘要、记录或工作区；落到 H75,200 时 7,529 行，H17 时 10 行；实时 head 与 playhead 分离，选择和输出保持稳定 |
| AC21 | 真实 compaction/prune 链、`test_trajectory_tool_output.py` | Bash 超 100k 后的每个 chunk、MCP/WebFetch 内部裁剪前结果均可回放；模型视图独立 |
| AC22 | `test_trajectory_auxiliary.py` 的 fork、snapshot restore/undo | fork 是独立根与历史基线；revert 保存实际快照引用，不声称完整桌面录像 |
| AC23/24 | 实际文件工具、`test_trajectory_media_deletion.py`、`test_trajectory_read_races.py`；真实浏览器媒体与整会话删除 | H35 不请求未来媒体，H37 预览/下载成功；删除后旧按钮重新 GET 得到 410，图片/按钮移除，3 个 object URL 全释放；整会话删除后工作区、缓存和订阅清除 |
| AC27/28/31 | `unit/test_trajectory_projection.py`、共享 fixture、前端 seq/projector 测试 | 完整/分批/checkpoint replay、unsupported、超安全整数 seq |
| AC29/30 | 存储故障/背压/幂等测试、SQLite/PG 多进程并发和迁移 | 已提交 receipt 才交付；记录失败不触发未记录的新派发 |
| AC32 | 真实后端路由进入同一录制链 | Web/API 同一服务端入口覆盖；未运行独立移动客户端设备验收 |
| AC33/37 | 真实只读 WS、普通 WS 不分发 trajectory.*；真实浏览器 22 组功能场景的网络断言 | 未连接 `/ws/agent`，模型/工具/交互执行请求为 0，账单/余额初始化请求为 0；写请求仅登录态刷新和超管只读 WS ticket |
| AC34 | 实际 UsageMeter + 主调用/建议、image/video 既有账本专项 | 引用原账单；未调用付费 provider，回放不结算 |
| AC36 | 专用 WS 每秒及发送前角色/token 检查；payload/export Blob 读取途中撤权；真实浏览器打开后在数据库降权 | 约 1.24 s 拒绝访问，缓存正文 2→0、同步实例 1→0、查看目标清空；角色恢复仅用于清理测试环境。迟到响应隔离另有客户端专项 |

## 4. 运行的检查

使用原项目已安装的 Python 环境运行 worktree 代码，没有更改原项目源码。最终后端检查命令：

```sh
cd /Users/wang/workspace/OpenBox-trajectory/backend
/Users/wang/workspace/OpenBox/backend/.venv/bin/python -m pytest tests/unit tests/integration/test_trajectory_*.py -q --tb=short
```

最终汇总：**2289 passed，1 failed，51 warnings，72.83 s**。唯一失败是下面已在主分支复现的云桌面旧用例；没有将它排除出总回归。日志为本机 `/tmp/openbox-trajectory-final-backend-check.log`。已单独完成：

- 媒体/主循环/工具相关回归 179 passed；最后媒体账单关联修正后专项再次 10 passed。
- 实际 Bash、MCP、WebFetch 长输出专项连同现有 MCP 安全测试 44 passed。
- 普通/大 JSON 请求媒体删除及辅助生产入口 10 passed。
- SQLite 存储/迁移/投影 29 passed，PG 存储 15 passed；读取竞态各数据库原 11 passed，修复含 `/` 的详情 ID 后 SQLite 14 passed。
- SQLite/PG 双进程顺序验证和真实 SIGKILL 均通过，原始证据在 `docs/trajectory-verification/`。
- 新增真实 Responses/native/fallback 与标题原账本专项 3 passed；跨 chunk/Schema/原始响应保真连同运行链回归 63 passed；词法边界最终单测 41 passed，连同真实数据库脱敏专项 54 passed；已纳入最终总回归。

主分支已存在一项无关失败：`tests/unit/test_internal_tunnel_keys.py::test_desktop_preflight_has_stable_503_code` 导入不存在的 `_desktop_route_preflight`。已在未改动的原项目单独复现，日志 `/tmp/openbox-trajectory-baseline-failure.log`。本次不因轨迹功能修改已搁置的云桌面逻辑，也不删除该断言掩盖失败。

冻结后的前端 `npm run check` 已通过：**105 个测试文件、736 项测试通过**，国际化与 TypeScript 检查通过，ESLint 为 **0 errors / 35 warnings**。其中新增的 3 条警告来自两个懒加载路由和浏览器 fixture 的 Fast Refresh 提示，其余为已有警告。`npm run build` 已通过；185 个改动路径的 Prettier 检查通过。最终浏览器样本与 UI01–UI16 的证据类别见 [前端验证报告](SESSION_TRAJECTORY_FRONTEND_VERIFICATION.md)。

CLI 原始执行记录位于本地 `frontend-v2/test-results/trajectory-claude/`，属于本机验收产物，不包含在源码交付中。用于复核的脱敏汇总及关键截图保存在 `docs/trajectory-verification/`。

## 5. 性能数据的口径

两种数据库都运行了 100,001 事件、10,001 记录的同规模脚本；详细硬件、输入大小、writer 数量、checkpoint 间隔及完整 JSON 在存储报告中。

| 指标 | SQLite P95 | 本机 PostgreSQL P95 |
|---|---:|---:|
| 最新 100 条记录摘要 | 18.54 ms | 8.54 ms |
| 不同历史位置 seek | 712.77 ms | 841.31 ms |
| 4 根会话/2 用户流式 durable receipt | 60.30 ms | 72.48 ms |

这组 receipt 计时包含生产者等候数据库提交，不包含浏览器 paint。100k 事件的单次 Node 投影耗时同样不能替代真实页面首屏或 seek。

冻结前端后，另以真实应用 API、SQLite、Vite 和 Chromium 151 测量 100,009 个持久事件、10,009 条记录、20 个检查点的会话。浏览器为 1600×1000，JS 与连接已预热；每次从列表进入前清理轨迹查询缓存，10 个历史跳转依次移到更早检查点，实际读取 checkpoint 和到 H 为止的尾页。测量从真实点击/提交开始，到对应 DOM 可见并经过两帧结束：

| 浏览器指标 | 中位数 | P95 |
|---|---:|---:|
| 首批记录摘要出现 | 116.9 ms | 175.6 ms |
| 完整工作区就绪 | 1,249.1 ms | 1,352.7 ms |
| 历史跳转内容就绪 | 502.0 ms | 642.5 ms |

每项 n=10，按最近秩计算，P95 在这批样本中等于最大值；虚拟列表只渲染 38 行。摘要首屏与普通 seek 达到本机目标，不能将预热 SPA 结果解释为冷启动或生产网络保证。完整样本见前端报告及其 JSON。

另有模拟 REST/WS 的浏览器规模 fixture：100,000 个事件、10,004 条记录，最多渲染 53 行；30 次混合缓存 seek 的 P95 为 215.1 ms，30 次滚动的 P95 为 33.9 ms。20 次整页 reload 的首行 P95 为 **735.9 ms**，未达该 fixture 的初始 500 ms 目标，计时包含整页导航与开发模块加载。20 次实时水位绘制 P95 为 **114.6 ms**，未达其初始 100 ms 目标；它只衡量水位标记绘制，不能代表正文 delta 或后端采集额外开销。两种未达初始目标的测量保留在报告中，没有用原生预热 SPA 的数据替代。

历史加载还使用真实 checkpoint 响应延迟验证：加载期间不显示最新摘要、记录或工作区；释放后只恢复指定 H 的内容。早期跳转错误地补读到 live checkpoint，已经改为只补到目标 H；跳到 seq17 的单次复核从 3.77 s 降至 44.14 ms，此单次值不当作 P95。

计划中“记录增加的可见输出延迟 p95 ≤100 ms”的整链指标仍未完成对照测量。fixture 的 live 水位绘制、数据库 receipt、跨进程丢通知后的轮询恢复是不同指标，不能据此写成已全面达标。跨越 main checkpoint 基点时，若缓存中没有该原始事件，播放器有一次零等待推进；目标 H 的内容仍正确，此计时边界已在前端报告注明。

当前导出使用内存 ZIP；多 GB 产物导出的峰值内存、远端 Blob 容量/故障和长期多节点负载不在本机验证范围内。checkpoint 仍需还原目标状态，已验证规模为 10,001 条记录。已提供归档失败日志和可查询状态，生产监控告警需接入部署方监控系统。

## 6. 本地查看

独立验收后端入口为 `backend/scripts/trajectory_dev_server.py`。它使用真实应用路由、JWT、数据库与 Recorder，载入确定性会话 fixture；不调用外部模型、不创建付费沙箱、不运行 cron。重启相同 data-dir 保留数据，创建不同 data-dir 得到全新隔离库。

```sh
cd /Users/wang/workspace/OpenBox-trajectory/backend
/Users/wang/workspace/OpenBox/backend/.venv/bin/python scripts/trajectory_dev_server.py --data-dir /tmp/openbox-trajectory-acceptance-v2 --port 8091
```

另一个终端运行 v2：

```sh
cd /Users/wang/workspace/OpenBox-trajectory/frontend-v2
VITE_API_URL=http://127.0.0.1:8091 npm run dev -- --host 127.0.0.1 --port 3101
```

页面 `/app/admin/trajectories`；详情 `/app/admin/trajectories/sessions/session_user_a`。本地测试账号密码由脚本生成到 data-dir 的 `credentials.json`，权限 0600，不能提交或复制到文档。示例数据是测试记录，不冒充用户真实执行。前端依赖当前使用原项目的 node_modules 链接；忽略规则同时覆盖目录和链接，依赖不属于交付源码。

## 7. 部署与回滚操作

M8 涉及实际环境发布，本轮尚未执行。上线时按以下顺序操作：

1. 准备兼容代码/镜像，保持 `TRAJECTORY_RECORDING_ENABLED=false`、`TRAJECTORY_ADMIN_ENABLED=false`；规划在途 Run 排空或交接。
2. 使用同版本 `backend/scripts/backend_entrypoint.py --migrate-only`（项目 `make migrate` 的既有入口）执行 Alembic 到 `f6a8c0e2b4d6`。新增七张轨迹表，并给 SessionExecution/CronRun 添加 nullable trace_context。再发布全部执行与回调节点，数据库 readiness 必须通过；开关关闭不代替 schema 迁移。
3. 配置 Blob 后端，验证 staging 可以提交、归档、回读哈希及删除。首次灰度选择专用测试用户 ID 和平台超管 ID；两类白名单分别配置。
4. 开启记录后，从新会话输入、旧会话续聊、旧问题回答、子 Agent、异步回调各执行一个已授权样本。核对 coverage_start、原会话 owner、输入/输出/终态、现有账单，确认旧内容只作为 baseline。
5. 开启超管查看，验证跨工作区列表、实时更新、H 中途回放、导出、普通用户拒绝，以及降权/删除后立即停止读取。
6. 观察稳定后清空 `TRAJECTORY_RECORD_USER_IDS`，使所有用户后续活动接入；保留平台超管校验。按容量和延迟扩大流量，记录环境、版本、样本水位及观察结果。

| 环境变量 | 默认 | 用途 |
|---|---:|---|
| `TRAJECTORY_RECORDING_ENABLED` | false | 记录开关 |
| `TRAJECTORY_RECORD_USER_IDS` | 空 | 目标用户灰度；空表示全部 |
| `TRAJECTORY_ADMIN_ENABLED` | false | 超管读取/订阅开关 |
| `TRAJECTORY_ADMIN_USER_IDS` | 空 | 超管灰度；仍必须 DB role=admin |
| `TRAJECTORY_INLINE_BYTES` | 65536 | 大 JSON 外置到受控 payload 的阈值 |
| `TRAJECTORY_BATCH_MS` | 50 | 增量合批窗口 |
| `TRAJECTORY_PENDING_BYTES` | 4194304 | 单根会话常规待提交队列字节上限 |
| `TRAJECTORY_TOTAL_PENDING_BYTES` | 67108864 | 实例常规待提交队列字节上限 |
| `TRAJECTORY_CHECKPOINT_INTERVAL` | 1000 | 后台建立检查点的候选事件间隔 |

配置从进程环境读取，多节点需要一致配置并按既有发布流程滚动重启。单个超大 chunk 会等待直接 staging，不计入常规内存队列；调用方必须等待 receipt，不能无限启动不等待的写入任务。

回滚优先关闭超管读取并结束相关连接，保持日志继续写入。若写入链故障，先停止接纳新的受监控执行并排空/交接现有 Run，再关闭记录；不能让记录失败触发未记录的新工具或模型派发。保留表、tombstone、序号和既有数据，不执行破坏性 downgrade。退回旧镜像前确认其启动迁移流程识别新 revision，不能通过强制 stamp 掩盖版本不兼容。恢复开启后追加 gap 与实际新 baseline，沿用原序号，不补造停用期间的事件。

故障排查至少覆盖：`recording_failed/gap`、事件 commit 耗时、pending staging 数量/字节/年龄、归档失败、checkpoint 落后、删除 GC 积压和存储增长。Blob 失败优先修复归档通道，数据库 staging 仍是已提交事实；租约过期 unknown 需检查实际外部结果，不自动重做工具。日志不打印完整请求、凭据或签名 URL。
