# Agent 行为评测集（第 0 步 0-7）

规格见 `docs/KERNEL_DSH_COMPARISON_20261128.md` 第 25.5 节，动机见第 18、21 节（"先有评测，再改提示词"）。

- 脚本：`backend/tests/manual/agent_behavior_eval.py`
- 题目：`backend/tests/manual/agent_behavior_tasks.py`
- 离线测试：`backend/tests/unit/test_agent_behavior_eval.py`

## 它做什么

每个题目单独跑：新建一个临时 SQLite 库和一个临时目录，造好合成数据，然后走真实的 Agent 主循环（`agent.loop.run_loop`：系统提示词、工具解析和暴露、processor、持久化提问卡、真实工具），最后由题目自带的判定函数检查结果。不读任何现有用户的数据。

每次运行记录：是否完成、总步数、每步工具调用数、提问次数（`question`）、`capability_search` 次数、同一个 Skill 的重复加载次数、格式错误的工具调用数、输出 token、总 token、耗时。另记：调用了还没露出的真实工具的次数（`unmaterialized_tool_calls`，算可见性成本，不算格式错误）、子代理的请求数、第一步调用了哪些工具、实际用到的模型、这次运行里的提问卡和它们的标题（`cards`，工具自己弹的卡只在这里看得到）。

一步 = 被评测对话里的一条 assistant 消息，同一请求的重试只算一次。格式错误 = 参数不是一个 JSON 对象，或者工具名根本不存在，或者参数被工具的 schema 拒绝。

单次运行的硬上限：30 步，20 万 token（子代理的 token 也算）。到了就停，记 `limit_hit`，这次运行算没完成。

## 和生产环境不同的地方

- **沙箱**：`LocalSandbox` 代替 Action Server。沙箱里的 `/workspace` 是这次运行的临时目录；`bash` 在宿主机上执行，路径里的 `/workspace` 被改写，环境变量只留系统 `PATH`，`HOME`、`TMPDIR` 都在临时目录里，模型拿不到 `.env` 里的密钥。macOS 上用 `sandbox-exec` 禁止写临时目录以外的地方；读不受限，其他平台没有任何隔离。模型写的命令以执行者的身份运行，所以请在开发机上跑。
- 关掉了：会话标题生成、回复建议（都是旁路调用）、记忆、轨迹记录、变更快照。计费按影子模式计量（`BILLING_MODE=shadow`），合成的工作区没有积分，不能让它拒绝调用。
- 不唤醒收件箱：个人助理派出去的任务会被记录，但不会执行（和 `assistant_placement_eval.py` 一样）。所以派出去的对话不会停下来等用户；`tasks.submit` 停在 `waiting_input`，只能是这次调用自己弹了卡（安排位置卡或任务 AI 卡）。
- `--model` 指定的模型同时成为默认模型和模型目录（`models`）里的一项。生产上默认模型总在目录里；不在的话，个人助理没法按"用户默认的任务 AI"派单，会弹一张「任务 AI 模型」卡。加这一条之前，用 `--model openai/qwen3.8-flash`（不在目录里）跑的几轮里 `assistant_new_conversation` 全是 0/3，原因就在这里，不在模型：模型的调用是对的，服务端停在了这张卡上。
- 没有沙箱里安装的 Skill 和 MCP 工具；内置 Skill 读宿主机上的 `backend/.openbox/skills`。
- `--tuning` 之外的 `agent_tuning` 开关一律关；`--exposure` 决定 `tool_exposure.mode`。提交 a9da02c8 时这些开关还没有代码读取，打开它们暂时不改变行为，只会记进结果文件。

## 题目（24 个）

| 类别 | id | 用户说的话（摘要） | 判定 |
|---|---|---|---|
| 文件 | `files_sales_summary` | 按门店汇总十一月销售 CSV，存成 `门店汇总.csv` | 表头、三家店的销售额和单数、按销售额降序 |
| 文件 | `files_price_list_edit` | 价目表里牛肉面改 22 元、删掉卤蛋，别的不动 | 两处改动正确，其余行逐字不变 |
| 文件 | `files_meeting_todos` | 三次周会记录里的待办整理成清单 | 6 条待办，每条同一行有负责人、事项、日期 |
| 文件 | `files_members_export` | 积分高于 1000 的会员导出 CSV，手机号打码 | 行集合完全一致，手机号中间四位为 `****` |
| 文件 | `files_rename_photos` | 按说明把 `IMG_*.jpg` 改成菜名 | 四个文件改名且内容对应，旧名不留，说明文件还在 |
| 编程 | `code_fix_pricing` | 会员价项目测试不过，修好，别改测试 | 跑项目自带的 unittest；测试文件逐字不变 |
| 编程 | `code_todo_done_command` | 给 todo 命令行加 `done` 子命令 | 同上 |
| 编程 | `code_parse_orders` | 实现 `parse_order_line` | 同上 |
| 编程 | `code_fix_booking_slots` | 预约时段跨午夜排不出来，修好 | 同上 |
| 资料查询 | `research_ecommerce_law` | 电子商务法施行日期、首页公示营业执照的规定 | 人判，标准写在题目里 |
| 资料查询 | `research_python_313` | Python 3.13 发布日期和三个新特性 | 人判 |
| 资料查询 | `research_hangzhou_min_wage` | 杭州市区现行月最低工资和执行日期 | 人判（先找到现行文件再判） |
| 资料查询 | `research_douyin_food_qualification` | 抖音电商卖预包装食品要哪些资质 | 人判（打开给出的链接核对） |
| 定时 | `cron_daily_bad_reviews` | 每天早上 9 点提醒看差评 | 库里有启用的定时任务，按上海时间推算的触发时刻全是每天 09:00，任务内容提到差评 |
| 定时 | `cron_weekly_sales_report` | 每周一 10 点出销售周报，数据在 `销售/每日流水.csv`（题目造好两周三家店的流水） | 只在周一 10:00 触发；任务内容提到周报或销售，并写明数据文件 `每日流水`（写在任务内容里，或写在任务内容点名的项目文件里，比如模型写好、让定时任务去跑的脚本） |
| 定时 | `cron_workday_supplier_order` | 工作日下午三点提醒给供应商下单 | 只在周一到周五 15:00 触发 |
| 商家创作 | `merchant_menu_review` | 专业版做菜单，给了四道菜和价格 | 停在待答的确认卡上；卡上每道菜和价格在同一行；没有调用付费生成 |
| 商家创作 | `merchant_menu_light` | Light「做菜单」入口，三道菜 | 同上 |
| 商家创作 | `merchant_video_light` | Light「做一条视频」入口，没有照片 | 确认卡上有「15 秒」、店名、菜名；没有调用 `video_generate` |
| 商家创作 | `merchant_poster_groupbuy` | 专业版做团购海报，团购价 68 元 | 停在生成前最后一张卡（标题卡）上，卡里有 68；没有调用 `image_gen` |
| 助理派单 | `assistant_followup_budget` | 「把杭州那家店开业的预算也补上」 | 沿用 `assistant_placement_eval.py`：`tasks.followup` 被接受且指向原任务，项目、会话、任务数不变 |
| 助理派单 | `assistant_new_conversation` | 「在 North Star 项目里另开一个对话」 | 沿用：`tasks.submit` 被接受且落在 North Star，项目数不变、会话数加一；另查新会话确实在 North Star 里。停在卡片上（`waiting_input`）不算完成 |
| 点名 Skill | `skill_mention_poster` | `@skill:poster-maker 帮我做一张开业海报…` | 第一步调用了 `skill`，名字是 `poster-maker` |
| 点名 Skill | `skill_mention_scheduled_tasks` | `@skill:scheduled-tasks 每天晚上 10 点提醒…` | 第一步调用了 `skill`，名字是 `scheduled-tasks` |

每个题目都要给出完成它所需的信息。`cron_weekly_sales_report` 原来只说"把上周的销售情况整理成周报"，工作区是空的，模型去问数据从哪来是合理的，判定却记成没完成；现在题目造好流水文件并在话里点明，定时任务也必须带上这个文件（直接写在任务内容里，或者在它要运行的脚本里），否则到点运行的新对话同样找不到数据。

商家创作的判定从不回答卡片，所以不会确认、不产生生成费用。被服务端闸门挡回的付费调用（例如 Light 页还没出样例卡时的 `image_gen`）不算失败，但会写进判定说明。

**商家创作能在本地跑到确认卡。** 用 `--fake-model` 试过：内置 Skill 从宿主机加载，`portable` 模式下 `example_pick` 先经 `capability_search` 露出，样例库是空的所以不出样例卡，随后 `question` 卡挂起，运行以 `waiting_input` 结束。四个商家题目都不需要跳过。

**没收的（覆盖上的缺口）**：浏览器操作、视频制作的后半段（逐镜生成和合成，`video-production` 的脚本在云桌面上）、发布到抖音、你拍我剪（素材分析本身就要对真实视频付费）。这些都要云桌面，第一版不收。

## 怎么跑

都在 `backend` 目录下，用平常的本地模型配置（脚本会像 `assistant_placement_eval.py` 一样加载 `backend/.env`）。真实运行会调用模型、产生费用，由有密钥的人执行。资料查询题需要本地配置了联网搜索。

先看一眼题目：

```bash
.venv/bin/python -m tests.manual.agent_behavior_eval --list
```

不花钱的自检（假模型按题目脚本走，走的是同一条主循环）：

```bash
.venv/bin/python -m tests.manual.agent_behavior_eval --fake-model --repeats 1
```

**基线**：改任何东西之前，主模型跑两轮，每题 3 遍，一轮 72 次：

```bash
.venv/bin/python -m tests.manual.agent_behavior_eval --model openai/MiniMax-M3.1-Flash --repeats 3 \
    --label baseline-1 --output ../docs/kernel-refactor/eval-results/baseline-1.json
.venv/bin/python -m tests.manual.agent_behavior_eval --model openai/MiniMax-M3.1-Flash --repeats 3 \
    --label baseline-2 --output ../docs/kernel-refactor/eval-results/baseline-2.json
```

`portable` 对照 `legacy_eager`（不改代码）：

```bash
.venv/bin/python -m tests.manual.agent_behavior_eval --model openai/MiniMax-M3.1-Flash --repeats 3 \
    --exposure legacy_eager --label legacy-eager --output ../docs/kernel-refactor/eval-results/legacy-eager.json
```

某项优化（以 A1 为例，`--tuning` 可以写多次，名字见 `core/config.py` 的 `AgentTuningConfig`）：

```bash
.venv/bin/python -m tests.manual.agent_behavior_eval --model openai/MiniMax-M3.1-Flash --repeats 3 \
    --tuning relaxed_prompt --label a1 --output ../docs/kernel-refactor/eval-results/a1.json
```

QWEN 和 GEMINI 两个家族各取示例配置里的一个模型，每题 1 遍，只看格式错误和明显的退步：

```bash
.venv/bin/python -m tests.manual.agent_behavior_eval --model openai/qwen3.8-max --repeats 1 --tuning relaxed_prompt \
    --label a1-qwen --output ../docs/kernel-refactor/eval-results/a1-qwen.json
.venv/bin/python -m tests.manual.agent_behavior_eval --model openai/gemini-3.8-flash --repeats 1 --tuning relaxed_prompt \
    --label a1-gemini --output ../docs/kernel-refactor/eval-results/a1-gemini.json
```

其他参数：`--tasks` 只跑某些题目或类别（如 `--tasks merchant code_fix_pricing`），`--keep-dirs` 保留每次运行的临时目录，`--verbose` 显示平台的警告日志。每跑完一次就写一次结果文件，中途停掉也不丢已跑完的部分。

## 人判和对比

资料查询的每次运行在结果文件里是 `"judge": "human"`、`"completed": null`，带着 `rubric` 和 `final_text`。执行的人按 rubric 判，把 `completed` 改成 `true` 或 `false`。没判的运行在对比时不计入完成率，会单独提示。

对比用 25.5 的判定规则（初值），参数是一到两个基线文件，最后一个是候选：

```bash
.venv/bin/python -m tests.manual.agent_behavior_eval --compare \
    ../docs/kernel-refactor/eval-results/baseline-1.json ../docs/kernel-refactor/eval-results/baseline-2.json \
    ../docs/kernel-refactor/eval-results/a1.json --a1
```

| 规则 | 通过条件 |
|---|---|
| completion | 完成率不低于两轮基线里较低的那一轮 |
| steps_plus_questions | 每次运行的步数加提问次数，不高于较高的那一轮 |
| a1_tool_calls_per_step（仅 `--a1`） | 每步工具调用数的平均值高于较高的那一轮 |
| malformed_tool_calls | 格式错误的工具调用为 0 |
| total_tokens | 每次运行的总 token 不超过较高那一轮的 115% |

只比较所有文件都测过的题目。每轮结果按"每次运行"平均，两轮次数不同也能比。候选一轮的总 token 超过某一轮基线的 1.5 倍时，会额外提示停下来查原因（25.5 的预算）。有任何一条 FAIL，命令以退出码 1 结束。

## 这套评测回答不了的

真实用户的任务分布、长会话、云桌面上的行为。假模型跑通只说明脚本、主循环和判定接得上，不说明任何模型的表现。
