# Agent Team acceptance evidence

2026-09-21 本地验收记录。此表记录证据覆盖，不将协议测试等同于真实任务收益，也不将“自动化通过”表述为全部浏览器流程已执行。本地双用户容量和完整旧版回滚演练已有证据；大规模生产负载未评估。

最新范围：用户已要求暂停移动端优化，本轮以 API、后端和 Web 为交付范围。移动端已有改动保留，后续 native 验收暂停；此前“移动端”浏览器证据主要指窄屏 Web，原生实测另记在实现记录，二者不混同。

范围更新（2026-09-21 晚）：用户解除移动端暂停，要求按 Web 实现与第 13 节把原生界面做完并在 iOS 模拟器上实测。原生团队界面已重做并通过 449 项移动端测试；模拟器上完成了一次真实运行的完整链路（输入区 → 组队方案卡 → 进度卡 → 完成与最终答复）以及运行详情、成员只读会话和工作面板入口。未在模拟器上实测的部分：运行中的暂停/继续/取消、含真实文件成果的“成果”页，以及设备上的深色与英文界面（这三项以渲染图和自动化测试为证据）。详见 [实现记录](AGENT_TEAM_IMPLEMENTATION.md)。

用户随后明确停止完整四组长评测，改为随机对比和修复 bug；移动端仅需接口接入，界面由用户设计。v4 的 11 条已结束记录和 1 条用户中止记录保留。新的 [固定随机样本](evaluations/agent-team-random-20260921/PROTOCOL.md) 不代表完整 M5 性能排名；原长评测不再阻塞本轮交付。

随机对比现已结束：[6 次结果](evaluations/agent-team-random-20260921/RESULTS.md) 为 5 次完成、1 次超时暂停。最后一条固定团队保留两项已验收任务，第三项修订被原定 600 秒上限中断。单 Agent 内容题有一处价格类型差异，严格评分保留 4/5；没有将未完成记录计为成功。自动追加成员的模型误拒绝已修复并经真实 Qwen 自动组队复验；忙碌成员收到消息后误退回排队的状态问题已修复，48 项相关回归与 PostgreSQL 双进程检查通过。移动接口专项 3 项通过，后续界面由用户设计。

最终全量检查：后端 **4258 项通过、24 项跳过**，前端 **951 项通过**，Web 生产构建通过。上一轮的两项失败及处理过程保留在 [实现记录](AGENT_TEAM_IMPLEMENTATION.md)，最终全量复测通过后未再修改业务源码。


最新用户变更（2026-09-21）：独立团队预算相关旧要求已取消，改为共享账户积分验收。新增覆盖：跨团队共享余额、单次精确扣费、余额不足暂停、不同用户不受影响、旧配置读取，以及最终总结正文、历史错误标记兼容和总结提交后崩溃恢复。v3 预算评测未执行，已标记被本次需求替代。


本次浏览器复验：`team_01M312V92VPNHYHQW96F25DJND`，Gemini 与 Qwen 两项均验收，最终正文显示 17+29=46，刷新仍保留，数据库仅一份 final 正文。后端 4250、前端 951 项通过，PG 双进程检查通过。详细证据见 [本次回执](evaluations/agent-team-account-summary-20260921.json)。

自动化模块均位于 `backend/tests/unit/test_team_*.py`；子进程验证脚本位于 `backend/scripts/`。最新计数和运行 ID 见 [实现记录](AGENT_TEAM_IMPLEMENTATION.md)，恢复步骤见 [运维说明](AGENT_TEAM_OPERATIONS.md)。

| 编号 | 场景 | 当前结果 | 可复核证据与限制 |
| --- | --- | --- | --- |
| A01 | 用户创建三个不同职责 Agent 组队 | 通过 | Qwen 运行 team_01M30XQRP0TVTT70274R6AB6V0：资料、计算、写作三个独立成员，3/3 验收；对象结果和两行文件内容正确。旧结束快照失败另记；新运行 team_01M30YFK4KE7KWBTTT53QW4S78 也完成 3/3，并在浏览器打开正确两行文件差异 |
| A02 | 协调者完全自动组队 | 通过 | proposals / agent_selection / templates / member_admission；修复后真实 Qwen 运行 team_01M3188RZY4ATB0EMBPMH6FTBP 只确认一次，成功追加成员、3 项验收并持久化完整总结。更早运行 team_01M304VKG6NTR8TS2GS0VDKK6D 覆盖另存定义与模板 |
| A03 | 用户成员与临时成员混合 | 通过 | 混合运行 team_01M30AR6W86BQT3RHRMK5QHTY4，2/2 验收、Skill、成员退出与刷新 |
| A04 | 锁定模型与非法覆盖 | 自动化通过 | compiler / agent_selection / catalog：锁定、不可用模型、推理档位、工具能力；编辑器默认模型目录浏览器复验 |
| A05 | 同一定义进入两个团队 | 自动化通过 | templates / trials / runtime_binding：冻结版本、会话隔离和项目授权 |
| A06 | 忙碌成员收到同伴消息 | 自动化通过 | mailbox_state / commands / runtime / Inbox：真实固定团队暴露状态回退；修复前 2 个忙碌场景失败，修复后成员与协调者均保持 running，等待者进入 queued，消息幂等且 Driver 代次不变；相关 48 项通过 |
| A07 | 空闲成员冷唤醒 | 通过 | runtime；恢复脚本 dispatch_before_wake；真实成员冷唤醒与权限恢复运行 |
| A08 | 并行任务与依赖 | 通过 | commands / state / liveness：依赖、DAG、上游失败；真实依赖复核通过。Qwen 容量运行三个工作成员同时活动约 565.037 秒（0.5 秒采样估算），计入协调者后峰值仍为 3 |
| A09 | 两个后端进程同时派发同一任务 | 通过 | verify_team_postgres.py：两个独立子进程同时派发，一次 attempt 和 Inbox |
| A10 | 命令响应丢失后重试 | 通过 | journal / commands；恢复脚本 command_response_lost，提交后 os._exit(73) |
| A11 | 投递中崩溃 | 通过 | Inbox 唯一键、共享事务；恢复脚本 queued_message / inbox_before_receipt |
| A12 | 执行中崩溃与外部结果未知 | 通过 | runtime / paid_tools / external_effect；claimed_before_driver / external_receipt_lost：旧代次拒绝、未知不重发 |
| A13 | 协调者等待时重启 | 通过 | runtime；恢复脚本 coordinator_wait，结果持久投递、重启唤醒 |
| A14 | 暂停、继续、取消 | 通过 | runtime / completion；浏览器暂停继续取消；team_01M30YNBC8NY4HCQAMT0M3DCR3 在问题待答时取消，等待卡消失且普通聊天恢复；恢复脚本 cross_process_pause：另一进程观察实际 abort 后释放 |
| A15 | 共享账户积分（替代原团队预算） | 自动化通过 | budget / billing / execution / paid_tools：两支团队共享账户余额，单次精确结算，耗尽后拒绝新请求，其他用户独立；旧团队预算不再生效，未知外部操作仍需对账。历史低预算观察保留但不再是当前要求。 |
| A16 | Skill 请求未授权工具 | 通过 | skills / skill_storage / compiler / permission_grants：Skill 内容不授予工具 |
| A17 | 其他用户访问 / 伪造发送者 | 通过 | catalog / ws / artifacts / commands / journal / owner_messages；PG 租户隔离与 Inbox 来源拒绝；用户补充接口只向根发送真实用户输入，响应丢失重试不重复 |
| A18 | 定义或 Skill 在运行中编辑 | 通过 | skills / skill_storage / templates：冻结主文件与附属资源、版本引用、内容摘要 |
| A19 | 账号授权撤销 | 通过 | revocation / mcp_http / permission_grants：每次访问重新检查，实时收窄 |
| A20 | Trace 关闭或丢失 | 通过 | Skill 存储独立于 Trace；恢复脚本不使用 Trace；本地 OSS/文件存储验收 |
| A21 | WebSocket 断线 | 通过 | team_01M30QAQ7AHZS2VSSTVTAJD4ER：独立本地故障代理实际关闭浏览器 WebSocket 并阻断 30 秒；CDP 记录 closed=1，代理重连计数 2→3，断线期间 43 次请求被拒；无需刷新，面板从 seq 59 收敛到 seq 75，正确显示取消、1/2 与未知费用 1。三成员订阅隔离仍归 A32；此前仅 HTTP offline 的尝试不计通过 |
| A22 | 成员一轮结束但后台 Job 未完成 | 通过 | paid_tools / completion：尚在途或结果未知的外部工作阻止结束；仅模型价格缺失会保留未知费用记录，不再阻止已验收文本成果完成 |
| A23 | 文件与桌面操作冲突 | 自动化通过 | resources、现有 mtime 回归；PG 两项目跨进程争用 workspace 桌面；真实桌面图形工具未运行 |
| A24 | 普通聊天及旧 Task | 通过 | 全量 backend tests/unit 与 frontend npm run check，包含旧 Task、聊天、Inbox、权限、恢复 |
| A25 | 团队运行期间用户正常聊天 | 通过（本地单用户） | 三成员运行 team_01M30WHH0CMGG6BM8PS9A0HRYN；0/1/2/3 成员时普通聊天各一例 HTTP 200，首字 6.759/7.664/4.319/3.708 秒；不据此声称多用户吞吐量。见容量记录 |
| A26 | 成员遇到需要用户审批的操作 | 通过 | 权限运行 team_01M30H6C3NR5CPGZK2H0MQKJBF：实际拒绝、根提问、准确路径追加、原任务重试与独立审校 |
| A27 | 成员做完事直接结束而不提交 | 通过 | runtime / lifecycle；恢复脚本 natural_answer：一次催促、隐式提交进入 review |
| A28 | 没有在途工作时调用 team_wait | 通过 | commands / runtime：无在途工作返回 no_progress，不结束本轮 |
| A29 | 多个成员几乎同时完成 | 通过 | runtime / commands：完成摘要合并、progress 只记录不唤醒 |
| A30 | 活动运行期间对同项目内任一会话 revert，包括与团队无关的普通会话 | 自动化通过 | retention：同项目任一会话 revert/unrevert 拒绝 TEAM_ACTIVE |
| A31 | 用户在根会话按“停止” | 自动化通过 | runtime / Session abort：根停止进入团队暂停，保留任务与未知结果 |
| A32 | 三个成员同时流式输出 | 通过（本地） | 三个实际 WebSocket 客户端：全订阅分别收到 5465/7878/5866 个成员增量；聚合视图 0 个成员增量，单成员视图 0 个其他成员增量。浏览器未截断片段确认仅选中成员 193 个增量，较后截断片段排除 |
| A33 | 把自动组建的团队另存为模板 | 通过 | templates / retention；浏览器另存固定模板并再次运行；来源删除后的可用性仅在隔离数据库测试 |
| A34 | 删除与依附关系 | 自动化通过 | retention / journal / migration；PG 活动 NULL 唯一约束；未删除现有浏览器或 ECD 数据 |
| A35 | 回放与缓存 | 通过 | state / journal 性质与序号检查；恢复脚本 corrupt_cache，回放相同且写入 cache v2 |
| A36 | 付费工具预授权 | 自动化通过 | paid_tools / paid_adapters / grants / amendments：授权前不发送、版本与已有操作幂等记录保留；消费走账户积分，不再设置团队单次或累计限额。真实素材评估仍缺已核实服务价格与操作范围 |
| A37 | 用户在 Agent 库创建 Agent | 通过 | catalog / trials / compiler / resources；浏览器起草、编辑、Skill、试运行、发布；临时成员完整表单、模板 v1→v2 发布、刷新保留和版本差异也已复验 |
| A38 | 聊天里让 AI 创建 Agent | 通过 | catalog / agent_manage / plugins；浏览器四项批量确认、移动端选项、T0 免确认与单项撤销 |
| A39 | 工具、MCP 与 Skill 的边界 | 通过 | mcp / mcp_http / plugins：真实本地 HTTP/SSE 45 工具、元工具范围、资源、Bearer 通知及撤销 |
| A40 | 协调者为临时成员选择 Skill | 通过 | skills / proposals / projection：允许范围、内容冻结与清单；浏览器混合团队 Skill 实际读取 |
| A41 | 运行记录 | 通过 | history / retention：SQL 分页、不回放、未知费用优先；浏览器记录跳转、再运行预填与账本一致 |
| A42 | 输入区发起团队 | 通过 | chat_creation / proposals；输入区模式、模板、点名与 team_request；普通聊天全量回归 |
| A43 | 组队方案卡 | 通过 | proposals / amendments / canonical_question_resume；实际开始、拒绝、调整与移动端共享提问组件 |
| A44 | 普通对话里 AI 提议 | 通过 | tool exposure / proposals：build 仅提议，plan/Task/cron/开关关闭不暴露；根绑定恢复 |
| A45 | 运行中调整阵容 | 通过 | amendments；真实权限运行加入审校员、固定名单确认、成员分隔线；混合运行成员退役 |
| A46 | 团队页签与连线图 | 通过 | 浏览器成员图、键盘连线与过滤、任务展开、390×844 列表、浅色深色、成果预览与用量 |
| A47 | 复用而非特例 | 通过 | 共用 TaskCardFrame、QuestionDock 详情、ChatSurface；跨 feature 集成在 app 层；951 项前端测试和生产构建通过 |

A01/A08/A25/A32 的本地真实模型证据已补齐，容量边界详见 [采样记录](evaluations/agent-team-capacity-20260921.json) 和 [双用户记录](evaluations/agent-team-multiuser-20260921.json)。多进程正确性已验证，大规模生产负载和分批放量阈值未验证；完整 M5 四组评估已按用户要求停止。完整旧版本本地回滚演练于 09:13 通过：隔离未知效果后，实际启动未修改的旧版服务和恢复循环，确认旧版执行器拒绝成员、关闭后的根会话完成普通聊天，团队表与事件保留。该结果是本地脚本化模型的发布演练，不是生产发布。真实素材长评估也不属于本次缩减后的随机文本回归。
