# 长期记忆实现与本地验证

更新日期：2026-10-02。分支：`codex/long-term-memory-plan`。

本次按用户恢复实施的指令实现 P0–P5，并完成 P6 的有界历史 dry-run 与运维入口。功能在本地隔离测试环境启用；默认配置仍关闭自动能力。本次提交包含实现、测试与验证文档，未执行全部历史回填、生产发布或 push。

## 已实现的行为

| 能力 | 当前行为 |
|---|---|
| 记忆权威库 | 继续使用业务 SQL。记忆修订、来源及 hash、确认状态、墓碑、持久任务、索引状态和 outbox 均持久化；Qdrant 与 Wiki 是派生数据。 |
| 权限与范围 | 每次读取重新校验 actor、workspace、当前项目归属、PERSONAL visibility、确认状态、TTL 和来源。未指定项目的检索仅包含个人记忆；指定项目时包含该项目与个人记忆。管理列表可以显式查看自己的全部项目。 |
| 显式操作 | API、`creator_context` 与持久确认卡使用同一写入服务。确认、更正、拒绝、遗忘使用预期修订与幂等命令；并发冲突返回明确错误。更正立即抑制旧值。 |
| 自动抽取 | 成功逻辑回合的持久完成记录冻结原始用户消息边界、分支及来源修订。后台租约 worker 校验后生成待确认候选；用户确认后才可用于上下文和检索。主回复不等待抽取。失败有重试、死信和恢复检查。 |
| 索引 | SQL outbox 驱动 Qdrant，固定 generation、配置指纹和不可变 revision point ID。payload 只保存身份及权限元数据。乱序或过期 worker 不能替代当前 SQL 修订；后台对账和删除验证可修复派生状态。 |
| 混合检索 | 中文与英文 BM25、dense、RRF 合并及相关性 rerank；选择后再次读取 SQL 当前内容。预算分别限制稳定背景、详细召回、候选、原文块及模型输入。 |
| Jev 路由 | 显式请求先走规则；其他请求最多一次 Jev 调用独立判断记忆与实时任务需求。概率和 confidence 分别检查；超时、无效响应或低置信度使用保守路径。主助理模型不因路由而更换。 |
| 时间与实时状态 | 相对日期使用用户时区和原始用户消息的真实时间。新抽取将时间纳入冻结 hash；旧来源在权限、原文及修订一致时只读恢复 canonical 时间，不改旧记录。未知手工来源仍为空。`current_task_state` 读取当前授权业务状态；`memory_search`、`memory_read_sources` 可供主助理补查。 |
| 上下文安全 | 召回内容在每次模型序列化前重新校验。跨回合及 compaction 保留引用，避免缓存的旧原文在更正、撤权或遗忘后再次进入模型。密钥及敏感字段在 provider 输入和调试快照中脱敏。 |
| 管理与调试页面 | `/app/memory` 提供候选、确认、更正、来源、历史、遗忘及清理进度。`/app/memory-debug` 显示阶段、路由概率与置信度、授权候选、最终引用、Wiki、实际 usage 和健康状态。普通打开与刷新不调用模型。 |
| 独立调试重跑 | 先生成绑定当前来源修订的预览，明确确认费用后一次性提交；产生独立 run/attempt，不写入新记忆或发布 Wiki。来源变更、过期或被遗忘后旧输入不可重跑。 |
| Wiki | Python 标准库编纂核心位于 `backend/wiki_compiler/`，不依赖宿主 ORM。宿主负责授权快照、模型调用、持久任务、不可变候选、CAS 审批与发布。逐段引用绑定真实来源修订，来源更正或遗忘后立即失效。 |
| 有界历史 dry-run | 只读取限定 actor/workspace/project、带时区日期和数量的既有成功完成记录。最多 31 天、500 条；不扫描全部聊天、不调用模型、不入队。当前没有历史执行回填入口。 |

候选和旧数据采用保守准入：旧 `CANDIDATE` 不因本次迁移自动升级为已确认知识。默认只支持个人可见记忆，未启用 workspace 共享。

## 遗忘语义

“仅这条记忆及其派生数据”立即停止该记忆及相关旧事实的复用，隐藏不可用的来源正文、历史正文和调试输入，使依赖 Wiki 失效，并异步删除所有已知 generation 的向量。它保留原始聊天以及 SQL 中按该模式保留的来源记录。

“同时遗忘所选来源副本”必须选择明确的 source ID；它还清除这些记忆来源副本及相关记忆修订中的正文，级联停止依赖它们的记忆。原始聊天、项目和 Session 不在此操作范围内。

清理状态在 Qdrant 确认删除、核对全部已知 generation 且无活跃旧写入租约后才完成。旧 worker 崩溃时等待租约及网络宽限期；关闭 rollout 或移出白名单不阻止已授权的 DELETE/REVOKE 清理。不能把“已停止使用”当作物理清理完成。

调试正文默认保留 14 天。后台维护到期后清除 run/step 的内容快照及 replay grant，保留 usage、hash 和请求身份等诊断元数据。

## Provider 与运行配置

本次真实调用使用以下固定配置：

| 用途 | 配置 |
|---|---|
| 向量 | 阿里云百炼 `qwen3.7-text-embedding`，1024 维、Cosine |
| 精排 | 阿里云百炼 `qwen3.7-text-rerank` |
| 路由 | `jev-1.13.0`，3 秒截止时间 |
| 主助理及本次抽取/Wiki | 现有 `openai/gemini-3.8-flash-high`；未改变主助理配置 |
| 向量库 | 本地 Docker `qdrant/qdrant:v1.19.0`，仅监听 `127.0.0.1:6333`，独立持久 volume |
| SQL | 本地 PostgreSQL 16；实现同时覆盖 SQLite |
| Sandbox | 现有 `wuyingyundev` 配置，经本地 tunnel 访问；真实安全命令成功返回 |

Provider 接口依据：[百炼 embedding](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)、[百炼 rerank](https://help.aliyun.com/zh/model-studio/text-rerank-api)、[Jev models](https://docs.typesafe.ai/models)。价格和实际账单按 provider 记录核对；embedding/rerank 单价未配置时显示未知，不能按零计费。Jev 估算仅使用输入 token 与已配置单价，输出 token 单独记录。

所有配置字段映射为 `MEMORY_` 加大写字段名。下面是受控启用示例；key 继续从已有忽略文件或进程环境读取，不写进代码和文档。

```dotenv
MEMORY_ALLOWED_USER_IDS=<explicit-test-user-id>
MEMORY_V2_WRITE=true
MEMORY_AUTO_EXTRACT=true
MEMORY_INDEX_SYNC=true
MEMORY_RETRIEVAL_V2=true
MEMORY_ROUTE_JEV=true
MEMORY_RERANK=true
MEMORY_DEBUG_VIEW=true
MEMORY_DEBUG_REPLAY=true
MEMORY_WIKI=true
MEMORY_BACKFILL=true
MEMORY_QDRANT_URL=http://127.0.0.1:6333
MEMORY_INDEX_GENERATION=memory-v1
MEMORY_EMBEDDING_MODEL=qwen3.7-text-embedding
MEMORY_EMBEDDING_DIMENSIONS=1024
MEMORY_RERANK_MODEL=qwen3.7-text-rerank
MEMORY_RERANK_MIN_SCORE=0.5
MEMORY_JEV_MODEL=jev-1.13.0
```

`MemoryConfig` 中这十个功能开关默认均为 `false`。启用开关但使用空白名单代表该开关面向所有 actor，因此灰度环境应配置明确的 user ID。SQL 权限、候选确认和墓碑检查始终执行。

启用 rerank 且 `rerank_min_score > 0` 时，有候选的详细检索都会检查相关性，包括只有一条候选的情况。精排成功后，低于阈值或超出精排预算而未评分的候选不会进入最终检索结果；调试记录保留经过权限复核的候选、分数及过滤数量。默认阈值为 0.5，可按实际模型校准；分数不代表准确率。设为 0 可恢复原有的按需精排且不做分数过滤。精排不可用时继续返回混合检索降级结果，并明确标记降级和未应用过滤。

本次本地环境：

- 前端：`http://127.0.0.1:3001`；代理至后端 `http://127.0.0.1:8081`。
- 记忆页：`http://127.0.0.1:3001/app/memory`；调试页：`http://127.0.0.1:3001/app/memory-debug`。
- 隔离 QA 数据库：`openbox_memory_dev`；回归测试库：`openbox_memory_verify_20261002`；QA Redis 使用 DB 8。
- 注册的测试用户名：`memoryqa_20261001_793038`。随机密码只保存在本机忽略文件 `/Volumes/fanxiang/workspace2/OpenBox/.local-dev/memory-qa-account.json`，没有放入报告或日志。
- 本地启动配置封装在忽略目录 `.local-dev/`，从已有 `.env`、`.env.wuying-dev`、`.env.local` 加载凭据，并仅为该测试 actor 启用功能。现有 8080/3000 服务继续运行。

`wuyingyundev` 当前使用预配置的共享开发桌面；本次验证不证明云桌面的多租户隔离，未创建、重建或删除云桌面。

## 启动、迁移与恢复

在仓库根目录启动独立 Qdrant：

```sh
docker compose -f docker-compose.memory.yml up -d qdrant-memory
```

部署进程加载正常配置后，按原有 backend entrypoint 执行 Alembic 并启动服务；记忆抽取、索引与 Wiki worker 随后端启动和停止。新增迁移为 `m1a2b3c4d5e6` 至 `m5d6e7f8a9b0`，Alembic 保持单 head。本次在新建 PostgreSQL 库执行空库 upgrade → 五个新迁移 downgrade → upgrade，全程成功。已产生 v2 数据后的破坏性降级会被迁移保护拒绝。

暂时关闭自动提取、路由、检索或 Wiki 可使用独立开关；显式 SQL 更正和遗忘仍可执行。Qdrant 故障时检索返回当前授权的 BM25 结果及降级原因，主助理可以继续运行；outbox 重试并在恢复后收敛。清理债务不因功能关闭而丢弃。

索引运维脚本只处理指定用户和空间；修复可能重新 embedding，因此要求 `--confirm-cost`。示例中的 ID 必须替换为操作范围：

```sh
cd backend
.venv/bin/python scripts/memory_index_admin.py health \
  --user-id <user-id> --workspace-id <workspace-id>
.venv/bin/python scripts/memory_index_admin.py reconcile \
  --user-id <user-id> --workspace-id <workspace-id> \
  --project-id <project-id> --limit 100 --confirm-cost
.venv/bin/python scripts/memory_index_admin.py rebuild \
  --user-id <user-id> --workspace-id <workspace-id> \
  --project-id <project-id> --generation <new-generation> \
  --limit 100 --confirm-cost
```

返回 `next_cursor` 时需继续处理后续批次。重建不自动切换读取 generation；核对全部目标 scope、缺失项和墓碑清理后再修改 `MEMORY_INDEX_GENERATION`。旧 generation 保留以便切回，SQL 当前授权检查仍是最终门槛。

历史 dry-run 通过已认证 API `POST /api/memory-backfill/dry-run`，或以下只读客户端；token 由环境提供，不在命令中展开：

```sh
OPENBOX_MEMORY_TOKEN=<token-provided-securely> .venv/bin/python -m memory.backfill_cli \
  --base-url http://127.0.0.1:8081 --workspace-id <workspace-id> \
  --project-id <project-id> --start 2026-10-01T00:00:00+08:00 \
  --end 2026-10-02T00:00:00+08:00 --limit 50
```

个人范围使用 `--personal` 替换 `--project-id`。该命令不启动历史抽取；实际历史范围和费用仍需单独决定。

## 真实浏览器验证记录

测试通过隔离 Chrome 中的 `jev-browser-use` 执行，并在真实前后端、SQL、Qdrant 与 provider 上核对结果。

| 场景 | 已观察到的结果 |
|---|---|
| 注册和手动记忆 | 创建测试账号；中文偏好与预算记忆入库、BM25/dense 命中，来源和修订历史可见。 |
| 更正 | 预算从 1200 改为 900 元，修订从 1 变为 2；即时检索与新 Session 回答使用 900，并给出来源 ID。 |
| 确认卡 | 真正的模型工具提出“松果同学”称呼，浏览器确认后转为已确认记忆；重启续接和 CAS 使用统一服务。 |
| 回合后抽取 | 用户只要求回复“收到”，主助理没有调用记忆写入工具；持久 worker 抽取“16:9 横屏、暖橙色”成为候选。确认前不命中，浏览器确认后检索命中。实际抽取 680 input / 167 output token，约 6.7 秒。 |
| 跨 Session | 第二个独立 Session 自动使用已确认称呼和排版偏好。真实 Jev 返回 memory skip 概率 0.75、confidence 0.63，因 confidence 低于 0.65 使用保守路径，稳定背景仍可使用；主助理配置保持原值。 |
| Wiki | 实际模型编纂得到不可变候选，逐段引用通过校验；浏览器批准后发布且参与混合检索。单次编纂 273 input / 155 output token，约 7.1 秒。 |
| 调试与独立重跑 | 逐阶段页面显示真实请求、模型、耗时、概率、confidence 和 token。预览后明确选择费用确认，生成独立完成记录，未新增记忆。 |
| 遗忘 | 预算记忆退出有效列表，派生 Wiki 立即失效；管理详情、来源、历史和旧调试记录不再显示正文，旧 run 重跑被禁用。记忆、两个来源索引和 Wiki 全为 DELETED，清理状态为 SUCCEEDED、pending_components 为 0；27 个已知 generation 均无残留向量。 |
| Qdrant 故障与恢复 | 停止本次新建的容器后，同一中文查询仍返回 BM25 授权结果，页面显示 `provider_error` 降级且向量分数未知。恢复容器后，再查得到 dense 0.828、rerank 0.987，降级消失。 |
| 相对日期 | “今天松鼠青柠的排版偏好是什么？”按 Asia/Shanghai 的 2026-10-02 至 2026-10-03 半开区间命中 16:9 与暖橙色。旧自动来源从原始消息恢复 `2026-10-01T16:37:33.024549+00:00`；未使用 worker 入库或确认时间。 |

上述延迟与 usage 是单次真实样本，不构成 p95/p99 或固定准确率结论。最早一次 QA 自动抽取因 JSON 解析失败留有 DEAD 记录；解析器现支持完整 JSON markdown fence，后续实际新任务成功。没有为了清空历史错误而批量重跑或重复收费。

最后一轮相关后端回归为 **325 passed**，约 90 秒，无 skip；包含 SQLite 与真实 PostgreSQL 的权限/CAS、抽取租约与恢复、删除乱序与 generation 验证、混合检索、时间、只读调试/重跑、Wiki、工具投影和上下文/compaction 接点。剩余两条 warning 来自既有 Message Pydantic class config 的弃用提示。测试在独立 `openbox_memory_verify_20261002` 库运行，provider/index 使用测试替身，未混入后台 QA worker。

前端最后一轮相关回归为 **53 passed / 7 files**，涵盖管理页面、Wiki、调试与实际 HTTP 路由契约、standalone layout 和路由；TypeScript、生产 build、ESLint、i18n 检查通过。Python 编译及 `git diff --check` 通过。

真实调用与浏览器操作使用单独的 `openbox_memory_dev` 库。可核对的请求包括：

- 断线检索：`memoryrequest_01M3W7SQZX5ZC6MWK4EKFAGE6V`；恢复检索：`memoryrequest_01M3W7XBS77VC8NQCZ0TMQYN2X`。
- 相对日期：`memoryrequest_01M3W9H18V3G5F8VEET9GQ6EH0`；独立重跑：`memoryrun_01M3W6MCPP9NGAFMJB6HBR05ZV`。
- 已遗忘预算对象：`mem_39b2e0651e5ae834762f7b7bfb78e55b3166f4712cbd2abd`。

截图已做视觉核对，保存在本机项目的忽略目录：`openbox-memory-cross-session-20261002.png`、`openbox-memory-debug-unavailable-20261002.png`、`openbox-memory-time-retrieval-20261002.png`（目录 `/Volumes/fanxiang/workspace2/OpenBox/.local-dev/memory-qa-evidence/`）。本地 `.local-dev/` 中保留测试日志和有界 QA 元数据，均被 Git 忽略，没有加入提交内容。

## 普通用户自然对话追加验证

2026-10-02 使用内置浏览器，在同一测试账号与项目中新建两个独立 Session。每个 Session 的第一条消息都是普通任务描述，没有提供称呼、比例或颜色，也没有要求读取记忆或引用来源；未复制旧聊天历史，未点击预填建议。

| 自然提问 | 页面显示总用时 | 实际回答 |
|---|---|---|
| 帮我想想松鼠青柠演示怎么开场，顺便给点排版建议。 | 9.7 秒 | 自动采用 16:9 与暖橙色，但开场白把用户称呼误用于观众，写成“各位松果同学”。 |
| 我下周要讲松鼠青柠，封面怎么做比较合适？ | 8.2 秒 | 自动提到此前确定的 16:9 与暖橙色；再次把用户称呼用于受众，建议“面向松果同学”。 |

只读调试页确认两次 `model_context.stable_background` 都包含原会话已确认的排版偏好，以及正文“松鼠青柠的演示草稿中称呼用户为「松果同学」”。来源均指向旧 Session `session_7YBW3W5ZMWBCTR648647TVNBDX`，而非当前对话新输入。

第一轮路由耗时 1899 毫秒、记忆选择 skip、confidence 0.66；第二轮路由耗时 1091 毫秒、记忆选择 skip、confidence 0.59，触发保守路径。两轮详细检索均跳过，已确认稳定背景仍进入主模型。因此本轮验证了自然任务下的跨 Session 偏好获取，没有验证 dense/rerank 的详细召回；页面总用时也不能当作纯记忆检索延迟。

**初测发现的问题：主模型两次都误用了称呼的对象。** SQL 记忆与模型输入明确指向用户，回答却将它用作观众称呼。初测保留了原始结果，未执行任何候选确认；随后按用户要求实施修复并反复验证，见下节。

记录分别为 `memoryrun_01M3WNHVH24452GBQ4NMRPVNM6`（Session `session_7YBW3AEFAQXKC4F3FWTHNTV2PN`）和 `memoryrun_01M3WNMKE7V3AN3C9NVFDREKR1`（Session `session_7YBW3ABHK91JAF6TAFX84C821Z`）。问题原文、页面截图与脱敏调试内容保存在本机忽略目录 `.local-dev/memory-qa-evidence/`；汇总为 `natural-cross-session-20261002.json`。浏览器已回到第二个自然对话，保留原始回答供核对。

### 称呼对象修复与反复验证（历史版本）

修复在系统层补充记忆的主语、关系和适用范围规则，并在实际记忆注入处明确当前委托人、代写内容作者/讲者、听众/收件人的对应关系。用户称呼用于直接回复用户或作者署名，不能变成受众称呼；当前任务的临时要求可以覆盖默认偏好。实现位于 `backend/memory/context.py`、`backend/memory/orchestrator.py` 与 `backend/agent/loop.py`，没有编码测试项目名或测试称呼，也未手动改写既有已确认记忆。

第一版仅补充全局使用规则，原题复测仍然出现误用，已保留失败记录。上一版在实际记忆内容旁明确角色关系后，使用内置浏览器连续新建六个独立 Session，以自然用户消息完成以下验证：

| 自然提问 | 页面总用时 | 观察结果 |
|---|---|---|
| 帮我想想松鼠青柠演示怎么开场，顺便给点排版建议。 | 14 秒 | 直接称呼用户为松果同学；讲稿以大家好/各位好开场，昵称仅用于讲者自我介绍；16:9、暖橙色保留。 |
| 我下周要讲松鼠青柠，封面怎么做比较合适？ | 10 秒 | 对用户的称呼正确，封面写“主讲人：松果同学”；比例与主色正确。 |
| 帮我写一段松鼠青柠演示的开场白，我可以直接照着讲的那种。 | 6.6 秒 | 助手对用户称松果同学，可照读的讲稿对听众说大家好，没有移用昵称。 |
| 这次松鼠青柠是给客户讲的，帮我写个开头。 | 15 秒 | 用户称呼与客户受众分开，三个开场方案均未把用户昵称当作客户称呼。 |
| 这次松鼠青柠的封面我想换成竖屏、冷色调，你觉得怎么排版？ | 9.3 秒 | 当次回答采用9:16竖屏、冷色调，没有强套既有横屏暖橙默认。 |
| 松鼠青柠的演示封面帮我定一版吧，署名也安排一下。 | 8.0 秒 | 随后的独立新对话恢复16:9横屏、暖橙色；作者署名正确，没有继承上一段的临时风格。 |

上一版六轮均符合上述观察目标。这些时间是页面显示的回答总用时，不是纯检索耗时，也不构成总体准确率或性能分位数。所有聊天输入均未要求读取记忆、引用来源或遵守测试断言；未确认测试中新出现的候选。

最终相关后端回归 **87 passed**，覆盖系统提示组装、记忆上下文、检索运行时、补充工具与记忆工具投影；两条 warning 仍为既有 Pydantic 弃用提示。`git diff --check` 通过。本次没有修改前端代码。

浏览器原始记录保存在 `.local-dev/memory-qa-evidence/role-fix-browser-trials-20261002.json` 和 `role-fix-trial-2.txt` 至 `role-fix-trial-7.txt`。其中 JSON 的 id 1 保留第一版失败结果，id 2–7 是上一版连续六轮成功记录；每条均含实际 Session URL。回归日志为 `.local-dev/memory-role-fix-final-pytest.log`。浏览器保留可直接核对的短讲稿会话 `session_7YBW38YZV7RB0DHYZGADRY1Z97`。

以下通用化修改取代了这一版按称呼、讲稿、受众分别编写的规则。上述六轮只验证当时设定的窄范围目标；“称呼可以用于署名”也不能从称呼偏好一概推出。

### 通用化：保留事实关系与来源边界

2026-10-02 按用户要求重构。移除了按演示、讲稿、邮件等题型编写的 `memory_role_bindings`，也移除了默认用户就是作者/讲者、固定使用某种开场招呼的规则。

- `memory/presentation.py` 集中维护共享的记忆使用约束与记录投影。事实的主体、关系、对象、条件与时间必须一起匹配；记忆所有者不自动等于事实主体，任务请求方不自动等于生成内容中的发送者、接收者或被描述对象。称呼、语言、语气等交互偏好保留其作用对象，不升级成身份或署名依据。
- 旧上下文不再把同类型事实拼接为“创作者人设”，而是逐条保留记录 ID、版本、类别、存储范围、有效期与确认状态。预算按序列化后的记录计算。
- 预取、检索和同回合工具读取使用同一个 `document_item` 投影，保留来源与范围。模型发送前不仅检查内容与版本，还从当前授权 SQL 重建这些元数据；源文工具投影保留来源类型、会话和消息身份。没有用猜测生成或迁移语义主体字段。
- 抽取提示升级为 `source-only-v2`，要求保留实体关系、条件、否定与转述归属，不将临时任务覆盖当作长期偏好。候选仍须走既有确认流程，原有已确认记忆未被人工重写。

自然对话测试使用原测试账号，通过聊天说出两个新项目的分工与语言约定，再在正常记忆确认卡中确认。后续问题在独立新会话发送，没有要求模型“读取记忆”、提示正确答案或附加断言。

通用规则的前三个迭代仍在原开场问题中出现称呼错位，已分别保留为 `generalization-trial-6-failed.txt`、`generalization-trial-7-failed.txt`、`generalization-trial-8-failed.txt`。这说明抽象原则和代码回归通过并不足以证明模型语义行为正确；最终验证以浏览器实际回答为准。

最终版本（记录标签 `generic_v4`）在 8 个有明确观察目标的独立新会话中通过，原始失败问题分别在首尾复测；所有观察均针对记忆归属、作用范围和缺失证据处理，不是对回复里所有写作内容的事实核验：

| ID | 普通用户提问 | 页面总用时 | 观察结果 |
|---|---|---|---|
| 9 | 帮我想想松鼠青柠演示怎么开场，顺便给点排版建议。 | 13 秒 | 三个开场方案均未将用户称呼移给接收者；保留16:9暖橙默认 |
| 10 | 松鼠青柠的演示封面帮我定一版吧，署名也安排一下。 | 28 秒 | 补读记忆后仍不把称呼当姓名，署名留占位；16:9暖橙默认正确 |
| 11 | 白榆的版本说明开头帮我写两句，再列一下前后端找谁对接。 | 8 秒 | 中文说明；前端林悦，后端用户；没有把昵称推断成后端负责人的姓名 |
| 12 | Can you write Lin Yue's self-introduction for the 白榆 demo? Just two sentences in English. | 5.8 秒 | 英文使用 Lin Yue 和白榆前端职责，不移用用户的后端角色或昵称 |
| 13 | 这次青麦的说明改成中文，我发到客户群，简单两句就好，主要是修好了登录卡顿。 | 4.9 秒 | 按当前要求生成中文客户通知，不套用英文默认或其他对象称呼 |
| 14 | 青麦下个版本会支持导出表格，给负责人拟一条简短的更新预告，末尾带上他的署名。 | 5.8 秒 | 新会话主稿恢复英文，附中文参考；署名周宁，未把临时中文要求当作长期默认 |
| 15 | 云杉项目的前端应该找谁？我想约个联调。 | 44 秒 | 检索后明确云杉没有记录，没有把林悦或周宁指派给云杉；缺失查询明显较慢 |
| 17 | 帮我想想松鼠青柠演示怎么开场，顺便给点排版建议。 | 8.8 秒 | 独立新会话复测，三个开场均未移用用户称呼；16:9暖橙默认正确 |

另有 id 16 的普通鼓励对话耗时 8.1 秒，未实际使用昵称，保留原始记录但不计作称呼召回成功。上述样本不能推导总体准确率；生成行为仍依赖模型理解关系约束，并非代码对输出语义的硬性保证。

SQL 诊断与真实浏览器操作相互核对：id 11、15 实际运行了 `hybrid_retrieval`；id 10 补调一次 `memory_search`（991 毫秒），id 15 补调两次（1900、1845 毫秒）。其余观察主要使用已确认稳定背景。页面工具次数还包含工具发现过程，不能全部当成记忆检索次数。页面 4.9–44 秒是完整回答耗时，不是纯检索延迟；未命中记录的多轮查证仍较慢，本次没有声称完成检索性能优化。

复查后只有三条已确认记忆：原演示的版式、称呼，以及通过普通确认卡新增的两个项目分工/语言约定。临时中文覆盖未改写这些默认。模型发送前的诊断记录中，记忆的类别、存储范围、来源、版本和有效期字段均保留。

最终相关后端测试 **130 passed**（53.54 秒），覆盖系统提示组装、旧上下文、通用记录投影、检索、读取工具、工具投影、抽取管线和 creator-context 工具。新增回归验证不同主体的同类事实不被拼接、不同语言和转述事实保持原文、来源副本不反向修改快照、预取/检索投影一致、发送前元数据由 SQL 重新构建、工具投影保留来源身份；既有授权、版本与压缩隔离用例继续通过。两条 warning 是既有 Pydantic 弃用提示，`git diff --check` 通过。本轮未改动前端或数据库表结构。

复核材料：

- `.local-dev/memory-qa-evidence/generalization-trials-20261002.json`：17 次独立会话的版本、提问、URL、结果和耗时，包含失败与未计分样本。
- `.local-dev/memory-qa-evidence/generalization-setup.txt`、`generalization-trial-*.txt`：浏览器原始可访问性记录。
- `.local-dev/memory-qa-evidence/generalization-sql-evidence.json`：限定合成测试账号的只读 SQL 证据。
- `.local-dev/memory-generalization-final-pytest.log`：最终 130 项回归日志。
- `.local-dev/memory-qa-evidence/generalization-final-20261002.png`：英文请求中正确使用林悦前端职责的页面截图。

## 向量检索与重排追加检查（2026-10-02）

Qdrant 的 `/readyz` 返回 200，`openbox_memory_v1` 为 green、optimizer 为 ok，向量配置为 1024 维 Cosine。检查开始时 QA 账号的三条有效记忆和六条来源均已索引，SQL 版本、配置指纹与九个实际向量一致，无缺失、旧版本残留或同步积压。用户随后新增负责人记忆；09:57 再次只读核对时为四条记忆、八条来源、十二个向量，版本仍完全一致、积压为零。

只读实测发现：精排确实执行并改变顺序，但原实现仍把低相关候选全部返回；无关的编程问题会得到五条演示或分工材料。新增通用的 `rerank_min_score` 过滤，不按项目名、语言或提问文本设特例。候选超过精排预算时，未评分的尾部也不会补入最终结果；关闭过滤或 provider 故障时保留已有降级路径并记录真实状态。

修复后的五组实际调用没有降级：版式问题只返回 16:9/暖橙色，称呼问题返回对应称呼来源与记忆，前端负责人问题返回项目分工；编程和做菜两个无关问题均返回零条。正例首位精排分数分别约 0.989、0.989、1.000；总检索耗时 0.77–0.99 秒，其中 embedding 0.27–0.43 秒、精排 0.23–0.32 秒。分数及五组样本均不能换算为整体准确率。

相关回归共 **162 passed**（检索 68 项、配置/上下文/工具/调试/时间等 94 项），覆盖小候选集无关拒绝、阈值边界、预算尾部、故障降级及 SQLite/PostgreSQL 权限与版本检查。证据保存在本机忽略目录 `.local-dev/memory-qa-evidence/vector-retrieval-audit-20261002-before-filter.json` 和 `vector-retrieval-audit-20261002.json`；实测 SQL 事务强制只读。

重启本地 QA 后端后，内置浏览器的检索页面再次验证：无关问题零命中；版式问题只显示对应记忆，向量分数 0.641、精排分数 0.989。两次请求均实际召回并精排十二条候选，分别过滤十二条和九条，耗时 1133/969 毫秒，无降级。页面截图为 `vector-search-unrelated-20261002.png` 与 `vector-search-ranked-20261002.png`，最终同步核对为 `vector-index-latest-20261002.json`，均在上述忽略目录内。

## 参考源码与许可

按用户允许的范围，只读参考源码已下载至 `workspace2`：

- `/Volumes/fanxiang/workspace2/openbox-memory-ref-wiki-20261001`：`atomicstrata/llm-wiki-compiler`，commit `eb18e769db8096c80b81665fb7195e3b29f4cb6b`，MIT。
- `/Volumes/fanxiang/workspace2/openbox-memory-ref-lemon-20261001`：`lemon-casino/LemonCode`，commit `c8f7400eac541f4c934b325d58e99d5ee31d7920`，Apache-2.0。

借鉴 Wiki 的 source hash、依赖闭包、引用检查和不可变候选，以及 LemonCode 的成功回合抽取边界。OpenBox 使用 Python 重写与 SQL 宿主集成，没有安装或运行上游整包。`backend/wiki_compiler/UPSTREAM_LICENSE` 保存上游许可和出处；组件 README 记录接口及宿主边界。
