# 个人助手剩余性能问题与查询放大分析

更新：2026-10-06（Asia/Shanghai）。本轮实际沙箱测试范围仅为已配置的无影云。

## 当前状态

**31 个已列功能验收场景均有通过记录；另有 1 项性能交付收尾未完成。** 下文列的是这项性能工作的子项，不改变 31 项功能验收的分母。当前不能宣称全部工作完成或性能验收通过。

已经提交的最近修复：

- `5922c370`：同一校验周期内，历史消息批次变化时保留交集中的已验证内容；新消息仍等待校验。撤权、失败、取消、重连及真实历史变化仍使旧证明失效。77 项相关前端测试、类型检查和定向 lint 通过。
- `65c73fc5`：复用三类参数化 SQL 语句的表达式结构，减少构造和生成结构缓存键的 CPU 工作。每次仍执行原查询，**实际 SELECT 数没有下降**。隔离 PostgreSQL 102 项通过；SQLite 是同组中的 77 项通过、25 项 PostgreSQL 专用跳过，不能相加成独立覆盖数。
- `97bb2975`：保存上述修复的浏览器、数据库诊断及无影完整流程证据。

## 当前测量结果

| 测量对象 | 最近结果 | 口径 |
| --- | ---: | --- |
| 53 条聊天历史记录的来源校验 | 中位 1.969 秒 | 浏览器空闲窗口内 9 个完整请求；不是冷启动或首屏时间 |
| 助手未读快照 | 中位 0.194 秒 | 同窗口 9 个完整请求 |
| 原 W2 任务的一次真实文件读取，到主助手最终汇报结算 | 593.912 秒，约 9 分 54 秒 | gen7，正常后端，无诊断观察器；一次读取、汇报 attempt 1、无重试 |
| 上述流程的请求前准备 | 合计 379.039 秒 | 主输入、执行、汇报三个 run 的互不重叠准备区间 |
| 上述流程的模型调用日志区间 | 合计 61.220 秒 | 7 组本地 start/end 日志的唯一时间窗配对；不是首 token、纯推理或供应商请求链路追踪 |
| 一次执行可运行性校验 | 4,495 次 SELECT | 单独的 gen6 `require_runnable_locked(..., lock=False)` 只读诊断，详见下节 |

此前完整流程为 647.768 秒；历史、代次、代码和模型输入不同，每个条件只有一个样本，不能据此归因或宣称稳定加速。历史分页的 177 个 DOM 样本中，既有 gen6 汇报标识持续存在，新记录的 pending 最终归零；这只直接观察了一个既有标识，全部 53 条的保留行为由组件测试覆盖。

## 为什么仍有 4,495 次查询

**这个数量明显过高，反映了来源校验实现的查询放大。权限正确性有必要保留，但不能把当前往返次数都解释为必要成本。**

### 计数范围

这是对原 W2 gen6 数据进行的**一次后端可运行性校验**，不是一次浏览器加载、一次文件读取，也不是整个 9 分 54 秒流程的查询总数。诊断调用了真实校验代码，但没有加行锁、调用模型或访问远端沙箱；外层为 READ COMMITTED，内部读取保留各自的只读 REPEATABLE READ 快照。

前后两版实际查询均为 4,495。带相同诊断插桩的单次墙钟为 5.666 → 4.246 秒，进程 CPU 为 3.043 → 2.244 秒。`65c73fc5` 优化的是 SQL 表达式构造，不是查询数量；这个单对诊断也不能代表模型或完整任务延迟。

以下使用最内层函数的 **direct SELECT** 归属，互斥相加恰好为 4,495。不能把递归函数的 inclusive 数相加，否则会重复统计。

| 查询用途 | 次数 | 主要函数 |
| --- | ---: | --- |
| 当前成员、私有主会话权限 | 661 | `assistant.commands._authority` |
| Task、执行 Session、Project 范围 | 469 | `assistant.commands.read_task_scopes` |
| 执行来源链和 Result 边界 | 588 | `_lineage` 294 + `validate_execution_message` 294 |
| 记忆/知识来源正文、撤销、版本与访问范围 | 1,330 | `_source_body_is_available` 336、`resolve_access_scope` 301、`current_revision` 266、`_source_is_available` 189、`read_sources` 126、`target_is_deleted` 112 |
| 任务快照及计划来源 | 487 | `validate_task_snapshots` 262 + `validate_task_schedule_locked` 225 |
| 原始证据和业务来源 | 603 | `_source_original` 248、`business_context._sources` 144、`_message_evidence` 72、`validate_source_ref` 70、`validate_task_command_sources` 69 |
| 其他校验读取 | 357 | continuation、project、knowledge/memory 读取及少量调度/控制读取 |
| **总计** | **4,495** | |

### 放大路径

1. [`scheduling.py`](../backend/assistant/scheduling.py) 的 `_held_task` 会校验计划来源、任务命令来源及继续执行权限。它不是只查一条 Task 状态。
2. [`command_sources.py`](../backend/assistant/command_sources.py) 的 `validate_command_derivation` 展开命令保存的原始消息、业务读取、决策和任务快照；每个命令建立自己的消息/引用验证集合。
3. [`evidence.py`](../backend/assistant/evidence.py) 在获取原始行后仍继续验证其来源依赖，结果和历史消息又可能引回任务及命令。这次记录有 4,985 次 `validate_source_ref` 调用、327 次消息证据请求、482 次 Result 请求；函数调用数不等于 SQL 次数，但显示了遍历规模。
4. 记忆和知识来源的验证进入各自的新只读快照，再检查权限、当前版本、删除/遗忘状态和正文。该调用内新增了 **98 个**快照事务：知识验证 56 次（含 21 次正文读取验证）、记忆验证 42 次。原始指标的 `read_only_transactions=100` 还包含诊断基线 1 个事务及预先建立的外层 1 个事务，不能写成“函数新建 100 个事务”。这些观察的唯一身份数量未在此诊断中记录，不能把 98 次都称为完全重复。
5. [`transactions.py`](../backend/assistant/transactions.py) 中的 `SnapshotChecks` 只在明确的同一只读快照内复用事实。执行准入、工具调用及模型派发的当前权限检查不能直接复用历史页面的旧验证结果；外层 READ COMMITTED 的相同参数查询也可能读到中途撤权。

原始行缓存已经工作，并非简单的“没开缓存”：命令原始行 777 次请求只有 69 次实际读取，来源原始行 1,300 次请求只有 248 次实际读取，Result 482 次请求只有 34 次实际读取。该次 CommandWalk 仅有 14 份证明、443 份原始读取记录，未达到 512 的容量上限，也没有失效。因此继续扩大容量不能解释或解决这次查询放大；命中原始行后仍进行的来源遍历、当前事实查询和独立快照读取需要结构性改进。

## 剩余工作

| 优先级 | 未完成项 | 下一步实现方向 | 完成时必须提供的证据 |
| --- | --- | --- | --- |
| P0 | 降低执行校验的 SQL 放大 | 按顶层校验阶段记录依赖身份及分支；设计有界的依赖读取计划，在允许的同一快照/范围中批量获取事实，复用原始结构与哈希计算，保留逐路径的循环、深度及预算判断。先评审事务边界，再改代码 | 固定相同证据数据的前后 direct SQL 总数和分组；查询数实际下降，输出、拒绝原因和原始证据一致；独立事务撤权/改来源回归通过 |
| P0 | 缩短模型调用前准备及整体响应 | 区分准入、目录准备、工具边界、模型派发和汇报阶段；确认各阶段的重复工作，优先解决上面的 SQL 扇出，不以微基准替代整体优化 | 同一类原任务只读输入的完整无影浏览器测量；分别记录输入接收、首个模型检查点、可见响应、执行结束和汇报结算 |
| P1 | 缩短历史重新校验的等待 | 在不恢复失效正文的前提下降低批量来源检查耗时；验证分页、刷新、历史事件后的重新显示及前后台切换 | 相同消息 ID 样本、完整未截断网络窗口、可见内容/等待状态记录；首次加载和稳定窗口分开报告 |
| P1 | 优化后的最终回归与独立复核 | 每次冻结候选源码后再测，测速期间不并行跑测试/诊断；继续使用原无影任务和现有环境 | 前端及 PostgreSQL 相关回归；原任务/Result/副作用/运行身份保留；正常运行的耗时及独立复算 |

上述方向仍待实现和验证，不能直接把独立快照合成一个、缓存所有权限判断、提高图预算，或省略撤权/遗忘检查来减少数字。候选小优化“在既有只读快照内合并两个正文否决查询”最多涉及 168 条（3.74%）；它不是解决分钟级等待的完整方案。

本轮工作先完成问题记录与提交。性能项保持 `in_progress`；功能已通过的 31 项记录保留。

## 证据索引

完整进度和 SHA-256 引用见 [`PERSONAL_ASSISTANT_IMPLEMENTATION.json`](PERSONAL_ASSISTANT_IMPLEMENTATION.json) 的 `performance_statement_carry_followup` 与 `performance_query_amplification`。

以下原始资料保存在本地 `.local-dev/assistant-web-20261003/`，不包含在 Git 提交中：

- `wuying-w2-gen6-rc-profile-20261006.jsonl`、`wuying-w2-gen6-rc-statements-after-20261006.jsonl`：一次可运行性校验的前后计数与调用分布。
- `source-statement-gen6-comparison-20261006.json`：相同 Task、Driver、Session、事件水位、调用次数和 SQL 数的比较。
- `statement-carry-browser-analysis-20261006.json`、`statement-carry-browser-pagination-20261006.json`：历史校验及分页观测。
- `wuying-w2-statement-carry-followup-evidence-raw.json`、`wuying-w2-statement-carry-followup-timing.json`、`wuying-w2-statement-carry-followup-verification-20261006.json`：gen7 单次读取和汇报、分段时间及保留性验证。已独立复算时间窗、7 组模型日志、一次读取和原始记录保留证据。
