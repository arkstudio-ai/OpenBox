# 测试基线：a9da02c8（第 0 步 0-5，2026-11-28）

提交 `a9da02c8`（`kernel-refactor/round1`）。这批测试的范围比根目录 `AGENTS.md` 的"不跑全量"大，这次运行已经得到同意。逐个文件的结果在 `test-baseline-a9da02c8.json`。选文件、运行和汇总的三个脚本放在 `baseline-tools/`，评估点照原样再跑一遍。

## 怎么跑的

**选哪些文件**：`backend/tests/` 下的 `test_*.py`，不含 `manual/`，共 712 个。凡是 import 了下面这些模块的都选上，用 `ast` 静态扫描，函数里的 import 和 `importlib.import_module` 字符串也算：

- `agent`、`session`、`question`、`permission`、`command`、`snapshot`；
- `tool/` 下的 11 个框架模块：`tool`、`registry`、`truncation`、`argument_repair`、`invalid`、`batch`、`capability_search`、`platform_plugins`、`plugin_lifecycle`、`sensitive_paths`、`filetime`。

结果是 **359 个文件**，unit 354 个，integration 5 个。

和第 10 节"至少 259 个"的关系：只数模块顶部 import 那 6 个包的，正好 259 个；加上函数里的 import 是 331 个；再加上 `tool/` 框架模块是 359 个。

**怎么跑**：

- 每个文件单独起一个 pytest 进程，套 coreutils 的 `timeout -k 15 600`，结果写成 JUnit XML。pytest-timeout 和 xdist 都没装。
- 分三批，三批并行，批内一个接一个跑。
- 失败的文件全部单独、按顺序再跑一次。
- 命令：`cd backend && timeout -k 15 600 python -m pytest -c pyproject.toml --rootdir . <file> -q -p no:cacheprovider -rfE --junitxml=<xml>`，解释器用主检出的 `.venv`，import 确认解析到 worktree。

**环境**：

| 项 | 值 |
|---|---|
| 软件 | Python 3.12.9，pytest 9.0.2，pytest-asyncio 1.3.0，litellm 1.81.11，SQLAlchemy 2.0.47 |
| 机器 | macOS 26.5.2 arm64，10 核 |
| `TRAJECTORY_RECORDING_ENABLED` | `false` |
| `LITELLM_MODE` | `PRODUCTION`。litellm 默认的 DEV 模式会用 `find_dotenv` 从共享的 venv 往上找，加载主检出的 `backend/.env` |
| `LITELLM_LOCAL_MODEL_COST_MAP` | `True`，和 `main.py` 一样 |
| 私有配置 | 没有 `.env`、`backend/openbox.json`，也没有 `~/.config/openbox/openbox.json` |
| 外部服务 | 没有 Docker、PostgreSQL、Redis，库是测试自带的 SQLite |

所以这是**干净检出**的基线。在主检出里跑测试会读到私有的 `backend/openbox.json` 和 `.env`（测试的当前目录是 `backend/`，配置会从当前目录找），结果可能不同。评估点要用同样的设置。

## 总数

| 项 | 数 |
|---|---|
| 文件 | 359 个：316 个全过，43 个有失败，没有超时 |
| 用例 | 5,038 个：通过 4,909，失败 86，出错 4（setup 阶段），跳过 39 |
| 耗时 | 实际 13 分 26 秒（19:23:55 到 19:37:21，三批并行）。各文件进程耗时加起来 2,285 秒，相当于全部顺序跑一遍；其中 pytest 自己计的时间是 1,948 秒。单个文件中位数 3.1 秒，最慢的是 `test_durable_questions.py`，39 秒 |
| 跳过的 39 个 | 全部要 PostgreSQL，包括助理那批锁顺序和 MVCC 的竞争测试 |
| 重跑 | 43 个失败文件各单独重跑一次，失败的用例完全相同。**没有不稳定的** |

## 失败分类

**真失败 38 个文件、65 个用例。** 也就是这个提交上本来就是坏的，多数是测试跟不上代码。按原因分：

1. **12 个文件、25 个用例，一个共同原因。** 测试里用 `SimpleNamespace` 替身冒充 `ResolvedStepTools`，替身缺 `deferred`。主循环从 ebe2278c（10 月 26 日）起读这个字段，替身一读就报错，之后的断言全部落空。这 12 个是：
   - `test_assistant_` 开头的 continuation_roundtrip、cross_day_acceptance、decision_roundtrip、memory_chain_acceptance、mixed_queue_acceptance、recovery_scanner_acceptance、rolling_history、roundtrip；
   - report_binding、report_injection、report_recovery、report_stop 四个 `_acceptance`。

   它们是助理的端到端验收，现在等于没有在测。
2. **脚本化的模型流没有结束标记，现在被当成传输中断**：`test_trajectory_pipeline_e2e`、`test_trajectory_business_isolation`。`test_processor_outcomes`：空流现在返回 RETRY，测试期待 CONTINUE。
3. **提示词或目录的文字改了，测试没跟上**：
   - `test_e_feedback_20261113`、`test_light_mode`：提示词文字；
   - `test_light_example_pick`：场景分类里还有 story；
   - `test_llm_schema`：多了 `prepare_template`；
   - `test_trajectory_lite`：6 个新工具没有标签。
4. **返回值或参数的形状变了**：
   - `test_assistant_foundation`、`test_workspace_access`：`'Session' object is not subscriptable`；
   - `test_assistant_resource_observations`：解包个数不对；
   - `test_memory_consumer_quality`：条目多了字段；
   - `test_skill_search`：`EmptyParams` 没有 `model_config`；
   - `test_video_resume_and_card_deadline`：替身缺 `run_id`；
   - `test_autopilot_enforcement`：时区默认值改成了 None；
   - `test_voice_consistency_sql`：结果里没有 `suspended`。
5. **行为和断言不一致，根因没查**：
   - `test_assistant_history_authority`：夹具建任务被判重复；
   - `test_assistant_memory_documents`：多了一次嵌入调用；
   - `test_assistant_private_runtime`：没有向桩沙箱发 `/execute`；
   - `test_automatic_compaction`：中止压缩的用例超时；
   - `test_automatic_knowledge`；
   - `test_context_budget`：7 个用例，剪枝和摘要的顺序不对；
   - `test_event_fold`：增量读多了一条 SQL；
   - `test_memory_personalization`：停在 RETRY；
   - `test_run_fencing_jobs`：没有抛 `RunRevoked`；
   - `test_video_completion_delivery`。

**环境原因 5 个文件、25 个用例：**

| 文件 | 缺什么 |
|---|---|
| `test_assistant_schedule_commands`、`test_cron_executor` | 本机 Docker 守护进程。后者的桩没盖住新加的沙箱那一步 |
| `test_deletion_boundary` | 私有 `backend/openbox.json` 里的 `instructions`。默认是空列表，所以 `AGENTS.md` 进不了提示词 |
| `test_trajectory_producers_delegation` | 子代理要 anthropic provider 的绑定 |
| `test_assistant_memory_v2` | 13 个用例。`remember` 的取证模型要 provider 的密钥；测试没有替换这个模型，所以在配了密钥的检出里，它会**真的请求模型服务** |

**不稳定的**：0 个。

每个失败文件的第一个失败用例和一句话原因，见 JSON 里的 `first_failure`、`classification`、`reason`。

## 评估点怎么对照

- 用 `baseline-tools/` 的三个脚本，同样的选择规则、环境变量和超时，在评估点的提交上再跑一遍：

  ```sh
  PY=/Volumes/fanxiang/workspace2/OpenBox/backend/.venv/bin/python   # run_batch.py 里也写死了这个路径
  OUT=<临时目录>
  cd backend
  $PY -I ../docs/kernel-refactor/baseline-tools/select_kernel_tests.py . $OUT/selected.json
  # 把 selected.json 里的文件分成几份 batchN.txt，每份一行一个路径，再分别在后台跑：
  $PY -I ../docs/kernel-refactor/baseline-tools/run_batch.py . $OUT/batch0.txt $OUT/out0 600
  $PY -I ../docs/kernel-refactor/baseline-tools/aggregate.py $OUT $OUT/selected.json $OUT/result.json
  ```
- 按文件比对失败用例的集合：
  - 新出现的失败用例算改坏了；
  - 原来失败、后来通过的，要说明是修好了还是测试被改了。
- 新增的测试文件单独列出，不计入对照。

## 建议（不在这次的范围里，没有动手）

- 先修第 1 类的共同原因：替身补上 `deferred`，或者改用真的 `ResolvedStepTools`。这是一处小改动，能让助理的 12 个验收测试在重构期间重新起作用；不修的话，助理试探和以后的搬迁都没有这层保护。
- 环境类的 5 个文件，要么让测试自己提供桩和配置，要么标成需要外部服务。`test_assistant_memory_v2` 会真的请求模型服务，应该先处理。
