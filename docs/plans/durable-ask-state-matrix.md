# Ask 状态与异常回归验收

日期：2026-09-09。本地测试；未登录或操作其他用户的线上会话，未部署线上。

## 本轮修复

1. **Web / Flutter 旧快照覆盖新 ask**：HTTP pending 请求开始后，WebSocket
   到达的新问题可能被旧 HTTP 响应删除。现在使用请求序号、账号 epoch 和事件
   revision 合并；较旧请求不能覆盖较新结果，已关闭问题不能被延迟事件复活。
2. **等待误报执行结束**：`waiting_input` 和回答后 `queued` 不再显示
   “本轮未生成最终答复 / 执行已经结束”。正常失败且没有最终答复仍保留提示。
   该修复同步覆盖 Web 和 Flutter。
3. **手机网页操作被裁剪**：小屏侧栏改为默认关闭的抽屉，独立于桌面侧栏偏好；
   支持遮罩、键盘关闭和焦点约束。聊天取消固定最小宽度，输入工具栏换行，
   长模型名称截断，发送/停止按钮在 320px、390px 和桌面宽度内可见可点。
4. **回归测试就绪条件**：全量 Web 运行中 `DesktopTab` 用例曾出现一次偶发失败，
   独立及全量复跑通过。该用例原来只等 SDK 创建，现在继续等待真正显示已连接
   控件再做交互断言；不把 SDK 方法已调用等同于 React 已完成界面提交。

数据库 / Agent 持久化业务逻辑沿用启动前的提交 `4aa21eb`；本轮新增后端故障
回归，没有为通过测试修改视频模型配置或生产数据。

## 证据等级

- **真实链路**：本地账号密码、真实浏览器、真实模型、Docker PostgreSQL/Redis，
  用户指定的无影开发桌面配置。真实后端等待时重启在上一轮完成，本轮再次验证刷新。
- **集成 / 注入**：真正的 Alembic 表和事务，在 SQLite / PostgreSQL 中运行；
  外部模型、消息通知或故障时机由测试控制。PostgreSQL 用独立 schema，禁用
  进程内串行锁，确认并发行为由数据库锁保护。不是直接关闭正在使用的数据库。
- **客户端回归**：Web / Flutter 单测及真实 Chromium 渲染组件、拦截 HTTP。
  拦截请求的浏览器用例不是完整后端端到端测试。

## 业务状态矩阵

| 输入 / 原状态 | 预期结果与不变量 | 验证 |
| --- | --- | --- |
| Agent 生成 ask | checkpoint 为 `pending`，会话 `waiting_input` | 真实 + 集成 |
| 等待无回答 | 无活动 `run_id`、无 lease、无自动续跑意图；等待不占运行配额 | 真实 SQL + 集成 |
| 没有 TTL，长时间不答 | 不自动确认、不自动过期、不启动 run | 模拟时间前进 30 天 |
| 1–4 个问题、选项 / 自填 / 多选 | 一个 ask 一次提交完整答案；多选去重去空白；5,000 字上限 | 真实三题 + 边界集成 |
| 多个独立 ask 同轮出现 | 各自保存答案，全部解决后才恢复 Agent | 集成 |
| `pending` 回答 | `answered` 持久化，与恢复意图同事务提交 | 真实 + 集成 |
| 答案提交时原 run 尚未完全退出 | 不同时启动第二个 run | 集成 |
| 回答后等待配额 | `queued`，答案保留；取得配额后 `busy`，完成后 `idle` | 真实续跑 + 配额注入 |
| 相同答案重发 / 12 路并发重复回答 | 同代幂等成功，只产生一次 resolution，不重复应用 continuation | 真实重发 + PostgreSQL 并发 |
| 不同答案 / 回答与跳过竞争 | 已接受决议不可覆盖，冲突返回 409 | 真实 + PostgreSQL 并发 |
| 用户发送新内容，AI 直接输出文本 | 旧 ask `superseded`，旧表单移除、历史只读 | 真实 + 集成 |
| 用户发送新内容，AI 再生成 ask | 旧 ask `superseded`，只有新 ask 可回答 | 真实 + 集成 |
| 新消息事务失败 | 不替代旧 ask，不丢草稿 | 集成 + 客户端回归 |
| 新消息与旧答案竞争 | generation 隔离，不能恢复旧任务 | PostgreSQL 并发 |
| 用户跳过 | `rejected`，不是批准；恢复时只应用拒绝语义 | 真实 + generic/plan/memory 集成 |
| 用户取消等待 | `cancelled`，会话 `idle`，不启动 Agent；旧答案 410 | 真实 + 集成 |
| 有明确过期时间并到期 | `expired`，不批准，不能答题或修改草稿 | 集成 |
| 旧代 / 已取消 / 已替代 / 已过期答案 | 410；当前代已解决但答案不同为 409；不重复执行 | 真实 + 集成 |
| 删除会话 | 取消 pending 和恢复意图，不复活任务 | 集成 |
| plan / memory 审批 | 只应用已保存决议一次；记忆提案改变必须重新审批 | 集成，未做真实付费业务操作 |

## 故障与客户端矩阵

| 故障 / 交错 | 验收结果 | 验证 |
| --- | --- | --- |
| 刷新 / 客户端重连 | 从数据库恢复同一个 ask、选项和自填草稿 | 真实 + Chromium + Flutter |
| 等待时重启后端 | 无需旧协程，重启后回答仍恢复一次 | 真实重启 + 集成 |
| 提交事务 commit 前异常 | 整体回滚，pending / 答案 / 恢复意图无半提交，可重试 | 集成注入 |
| commit 后响应丢失 | 第一次客户端见 500，但答案已保存；重试成功，仅续跑一次 | HTTP 中间件注入 + 集成 |
| WS 通知丢失 | DB 可重新列出问题；已提交恢复意图不依赖消息通知 | 集成注入 |
| 临时数据库 / worker 异常 | 保留恢复意图、退避重试，恢复后应用一次 | 集成注入，不是容器停机演练 |
| continuation 所依赖的工具 part 丢失 | 保存用户答案；回滚审批副作用；进入 error，不高频死循环重试 | 集成注入 |
| worker 租约失效 / 心跳失败 | 旧执行不能继续获得执行权限；触发中止 | 集成注入 |
| 两个 worker 竞争同一恢复任务 | 只 claim 一次；未开始的过期 claim 可重新投递 | PostgreSQL 并发 |
| 执行已取得进展后进程中断 | 不盲目重放历史工具或付费操作；保留中断状态 | 集成注入 |
| 空答案 / 题数不符 / 类型错误 / 超长输入 | 422，原 pending、答案和草稿不变 | 真实三类 + 12 类参数化集成 |
| 未登录、坏 / 过期 / refresh / 已撤销 token | 所有 5 个问答读写入口返回 401，无状态修改 | 真实未登录 + JWT 集成 |
| 其他账号 / 不存在的 ID | 404，既不能读内容也不能修改 | 集成 |
| 草稿 revision 冲突 | 保留本地编辑、显示失败、显式重试使用新 revision | Web + Flutter 回归 |
| 非法草稿 / 旧 revision | 422 或 409，不覆盖已保存草稿 | 集成 |
| HTTP 500 / 409 / 422 / 浏览器断网 | 仍能编辑和重试；选项、自填不丢失 | Chromium 拦截 + 客户端回归 |
| 提交中重复点击 / 重建组件 | 所有选项、自填和跳过禁用，不重复提交 | Chromium + Flutter |
| HTTP 410 | 移除失效卡片，不显示可无限重试的假入口 | Chromium + Flutter |
| 新 WS ask 先于旧 HTTP 响应到达 | 新 ask 保留且可回答 | Web / Flutter 单测 + Chromium |
| 两个读取结果乱序 | 旧快照不能覆盖新快照 | Web / Flutter 单测 |
| 已关闭问题的延迟 asked / list 事件 | 不复活旧卡片 | Web / Flutter 单测 |
| 切换账号时前一账号读取返回 | epoch 隔离，旧结果与旧草稿不带入新账号 | Web / Flutter 单测 |
| waiting / queued 无最终文本 | 不误报执行结束；真正缺失最终文本仍提示 | Web / Flutter 单测 + 真实页面 |
| 320px / 390px / 1280px | 三题答齐校验、发送和停止可用；页面无水平溢出 | Chromium；320 / 390 另有真实页面实测 |

## 本轮真实页面补测记录

测试会话：`session_7YBXY5WN8GK9M5GCSCFBFFPYWR`，仅属于本地 `ask_local_test`。

- `01M21WW1A88A01855GD54FZ3YS`：三题在 320px 完成选项 + 自填输入，刷新后
  草稿 revision 1 完整恢复。非法提交三类均 422，草稿不变。点击“跳过全部”后
  `rejected / applied=true`，Agent 只回复“已跳过本次确认”。同代重复跳过 200，
  再补答案 409，未增加续跑。
- `01M21X5E09G5D2XR2MR02X9TEV`：等待中点击“取消等待”后
  `cancelled / applied=false`，旧答案 410，会话 `idle`。
- 取消后最终 SQL：`run_id=NULL`、`lease_until=NULL`、`resume_pending=false`，
  pending 列表为空。此前两个 answered、两个 superseded 历史状态未改变。
- 之前的真实后端重启、文本替代 / 新 ask 替代、无影 `hostname` 执行和轻量延迟
  样本仍保留于本地 ignored `test-results/ask-local/report.md`。本轮生成的
  `followup-*.json` 是脱敏状态证据，不含账号密码或密钥。

## 自动化结果与复跑

| 检查 | 结果 |
| --- | --- |
| ask / abort / plan / processor 相关后端 gate | 124 passed |
| durable question 两个文件，SQLite | 78 项，包含在上述 124 项内 |
| 相同 78 项，Docker PostgreSQL 16 独立 schema | 78 passed，35.85s |
| Web `npm run check` | 384 tests passed，i18n / lint / TypeScript 通过（有既有 lint warnings） |
| Web `npm run build` | 通过 |
| 隔离 Chromium `playwright.questions.config.ts` | 11 passed |
| Flutter `flutter analyze --no-pub` | No issues found |
| Flutter `flutter test --no-pub` | 104 passed |
| 全量后端 `tests/unit` | 1,793 passed / 7 个原有失败，37.95s |

这些数量有包含关系，不能相加当作独立覆盖用例总数；没有声称行覆盖率 100%。

```sh
# backend 目录：正常 + 故障 + 相关中止流程
uv run pytest tests/unit/test_durable_questions.py tests/unit/test_durable_question_failures.py tests/unit/test_abort_race.py tests/unit/test_turn_abort.py tests/unit/test_plan_enter_question.py tests/unit/test_processor_outcomes.py -q

# backend 目录：配置 localhost 独立 openbox_questions 数据库，禁止指向用户库
OBX_QUESTION_TEST_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5432/openbox_questions uv run pytest tests/unit/test_durable_questions.py tests/unit/test_durable_question_failures.py -q

# frontend-v2 目录：浏览器 fixture 使用独立 4318 端口，不登录真实账户
npm run check
npm run build
npx playwright test --config playwright.questions.config.ts

# mobile 目录
flutter analyze --no-pub
flutter test --no-pub
```

## 明确边界与已知非 ask 失败

本次覆盖上述有限状态、交错和故障点，**不是“所有可能异常均不可能发生”的保证**。
尚未进行生产灰度、多节点真实断电 / 网络分区、真实 PostgreSQL / Redis 容器停机、
数万等待会话容量压测、真实连续 30 天 soak、实体 iOS / Android 设备或应用商店构建
验证，也未对真实付费视频业务做故障重放。模拟 30 天只验证状态与到期逻辑，
之前 12 次串行测速不能代表并发性能。

全量后端七个原有失败（与本轮开始前一致）：

- `test_computer_batch.py`：测试假上下文没有 `sandbox_error`。
- `test_internal_tunnel_keys.py`：引用已不存在的 `_desktop_route_preflight`。
- `test_video_open_generation.py` 三项：测试要求的模型时长范围 / MiniMax 配置
  与本机环境配置不一致。
- `test_video_production.py` 两项：声明模型 ID 与当前配置下的 fallback 映射不一致。

未通过改动视频模型业务规则或忽略这些测试来制造全绿。部署前仍需按
`durable-ask.md` 中的顺序停止旧 worker、迁移数据库并同步客户端协议；本任务未发布。
