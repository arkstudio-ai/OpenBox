# Agent 重构与最新 main 的整合记录

日期：2026-09-17。当前分支：`codex/integrate-agent-refactor`。

## 来源与范围

- 主线基准：`9f9452489fdca932cc9c2d4ff0eae468da738879`。完成前重新 fetch 远程 main，基准未变化。
- 合并来源：`codex/agent-refactor` 的 `7666824265264e3d97ca495daecfb3d781f87eab`。
- 采用真实 merge 保留两侧历史，在新的分支完成行为适配。

合入 Driver lease、持久化 Inbox、canonical Agent events、压缩/Fork 的稳定上下文范围、
工具并发及有序结果提交、权限收窄、子任务 descriptor/activation/outbox、图片 effect ledger、
Skill Provider 和 Platform Plugin 生命周期。

main 的 workspace、计费/订阅、每用户云桌面、问题回答后恢复、会话分页、通知、
视频、Cron 和独立 trajectory 链保持原有产品架构。未引入旧分支的共享桌面配置、
旧视频 material 接口、容器 MCP supervisor、远端签名 lease receipt 或 Cron outbox 改造。

## 关键适配

1. Driver 与问题运行时共享 run id，同时保留各自 generation 的含义；旧执行在新输入、
   stop、子任务取消及重新接管后不能继续提交当前会话输出。
2. 问题卡片的等待、回答、过期和计划切换进入 canonical history，继续使用 main 的持久化
   checkpoint；控制状态事件携带当前 Driver generation。
3. 工具请求、执行、完整输出、结果与子任务生命周期继续写入 main 的 trajectory spool。
   核心执行事件与分析轨迹保持独立。
4. 子任务和 Fork 继承 workspace/project；共享项目的创建者与会话使用者可以不同，
   项目仍必须属于同一 workspace。
5. 保留当前桌面的文件目录，通过现有 execute API 做路径规范化预检；Skill catalogue
   与 SandboxClient 使用一致的用户 scope。截图、发布和视频附件写入也带 Driver fence。
6. 现有 provider 配置默认支持继承模型和过滤工具集；推理参数、persona、输出 schema
   等额外能力仍须显式声明，避免升级后全部基础 Task 调用被拒绝。
7. Web 与移动端拒绝迟到的旧 generation 状态/输出；保留问题状态转换和历史分页。
8. main 的已存在迁移不改写；9 个 Agent 迁移顺序接到原业务库 head，独立 trajectory
   迁移链保持不变。

## main 功能兼容复核

在初次合并后追加行为核对，修正了下列兼容问题：

| main 能力 | 最终实现 |
|---|---|
| 按任意消息 Fork；复刻空会话和未完成历史 | 恢复原有能力，保留 canonical 快照校验与原子提交；Task Fork 单独要求完整回合 |
| 新消息中断上一轮；同步取得完整答案 | 默认发送仍使用 main 行为，写入受 Driver fence 保护；显式 `delivery` 才选择 Inbox 模式 |
| 普通 Bash 自动执行 | 默认允许；敏感路径检查及显式配置的 ask/deny 继续生效 |
| 使用 `/tmp`、内置 Skill、附件和其他沙箱绝对路径 | 恢复路径能力，由每用户沙箱执行访问控制；规范路径仍参与权限检查 |
| 旧工具可由 Batch 调用 | 未声明并行能力的旧工具按顺序运行；显式可并行工具保持并行，失去运行资格时取消同组调用 |
| 等待回答时停止会话 | 没有活跃 Driver 时仍取消主线问题 checkpoint |
| Cron 结果进入后续上下文 | 消息、事件和消费标记同事务提交；模型读取、Fork、重建都保留结果 |

恢复了主线原版 Fork 测试及旧工具定义的测试条件，新增
`backend/tests/unit/test_main_feature_parity.py` 验证上述行为。原有调用方无需修改发送参数。
默认发送继续保留 main 的附件传递失败后继续对话行为；显式 Inbox 模式采用严格附件传递。

静态清点确认主线 **240 个路由、879 个模型/配置/工具参数字段、39 个内置工具标识**
均存在。可运行 `python3 backend/scripts/check_main_contract.py --base origin/main` 重复检查。
该检查只覆盖名称与字段是否保留，行为兼容由回归用例验证。

诊断阶段将 main 原版测试放入隔离副本运行：3407 通过、21 失败、24 跳过；
数据库 readiness 的旧结构枚举单独由升级测试及更新后的 schema 测试验证。
21 项失败逐项核对，涉及旧内部 mock/参数签名、缓存 wire 格式、Task 扩展字段、
私有凭据脱敏，以及隔离副本缺少部署环境占位文件。最终使用与新内部接口匹配的
完整测试集验证，结果列于下表；原版测试诊断轮与最终回归结果分别记录。

## 本地验证

| 检查 | 结果 |
|---|---|
| 后端 unit（兼容修正后重跑） | 3832 通过，24 按环境条件跳过 |
| 后端 integration | 83 通过，70 按环境条件跳过 |
| Web `npm run check` | 130 个测试文件、911 项测试通过；类型/语言/ESLint 检查通过（既有 warnings） |
| Web `npm run build` | 通过 |
| Flutter `flutter test --no-pub` | 399 通过 |
| Flutter `flutter analyze --no-pub` | 无问题 |
| Flutter 800 行门禁 | 通过 |
| 变更模块 Python 语法/未定义名检查 | 通过 |
| 全库 Ruff E9/F821 | 有 1 项主线既有问题：`backend/video/productions.py:123` 引用未定义的 `RENDER_PIPELINE_REVISION`；该文件与 main 一致 |
| Alembic heads | 唯一业务库 head：`d0a2c4e6f8b1` |
| main → 新 head 的 SQLite 升级 | 通过，workspace/project 归属及原消息保留 |
| main 接口/字段/工具名称静态清点 | 240 / 879 / 39，未缺失 |

迁移验证使用 `git archive 9f94524 backend/db` 得到真实主线 ORM，在临时 SQLite
数据库创建主线结构和已有会话，标记主线 head，再运行新分支的 `alembic upgrade head`。
检查所有新增表、Inbox 的 video_resolution 列与原有数据；未使用开发/生产数据库。

新增整合用例覆盖 Driver/问题运行时身份、工作空间越权、完整模型回合、问题回答与
canonical history、兼容 provider 配置、客户端 scope、包含引号/中文的路径与符号链接
规范化、共享工作空间的 Fork/Task，以及移动端迟到事件。兼容复核另验证任意消息
Fork、默认发送中断、停止问题、Batch 调用和 Cron 结果的完整历史链路。

## 验证边界

- PostgreSQL 专用测试未运行：本机没有可用的隔离 PostgreSQL 服务。SQLite 通过不能
  替代 PostgreSQL 的锁与多进程接管验证。
- 未执行真实云桌面、付费模型/图片/视频调用或云端部署；本地回归使用隔离数据及 mock。
- Backend 检查可拦截后续请求和受保护写入，不保证撤回已被远端接受的命令。
  路径预检也不是远端文件操作的原子锁；generic effect ledger 尚未覆盖所有外部服务。
- Harness 的独立 scout CLI 在重试期间收到 HTTP 503 并超时，独立评审门禁未标记通过。
  上表是本地自动化验证结果，不代表独立评审或云上验收。

架构细节见 [Agent Kernel](AGENT_KERNEL_ARCHITECTURE.md)。历史 DeepSeek 源码比较仅作为
设计来源，不能将其中旧部署环境的验收结论套用到本分支。
