---
name: marketing-autopilot
description: Run or set up the 自动营销 pipeline — pick today's hot videos, break them down, recreate them with the chosen model tier, compose, publish to 抖音, and report — under a one-time budget authorisation (template). Use when a cron prompt carries a 「模版参数（预算授权）」 block, or when a person asks for 自动营销/自动跟热点/定时自动出视频发抖音, wants to create or edit such a schedule, or wants one run done now.
allowed-tools:
  - autopilot_run
  - hot_trends
  - video_analyze
  - video_generate
  - video_transcribe
  - video_compose
  - desktop_publish
  - douyin_publish
  - desktop_login
  - creator_context
  - cron
  - question
  - share_file
  - bash
---

# 自动营销（marketing-autopilot）

一条流水线：热点 → 拆解 → 按配方复刻 → 合成 → 发布 → 报告。钱和容差由 `autopilot_run` 管，你不要自己算：每一步付费前先 `reserve`，被拒就停。配方在 `references/recipes.md`，口播工艺沿用 video-production 技能的 prompt 规范（`/opt/openbox/skills/video-production/references/prompt-recipes.md`）。

## 两种模式

- **定时运行**：提示词里有「模版参数（预算授权）」块 → 无人值守。**不出任何 `question` 卡**，所有决定按模版字段做并写进报告。
- **对话**：没有模版块 → 要么帮用户**创建模版**（下节），要么**现在跑一次**：跑一次时仍要在三个点出卡：选题（热点列表）、成片+发布文案、合成/发布确认；预算改为当次口头确认的上限。

## 创建模版（对话）

1. 问清三件事（一次问完，不要挤牙膏）：账号做什么/卖什么（`account_profile`）、想跟哪些类目（`categories`，用 `hot_trends(action="sources")` 里的垂类名）、每次几条、多久跑一次。
2. `autopilot_run(action="tiers")` 拿三档与参考价，用 `question` 出**选档卡**：三档各一行「档位 · 模型 · 每 15 秒约 N 积分 · 适合什么」，选项就是三档；另一问：发布方式 `自动发布（默认，用云电脑的创作者中心）` / `扫码投稿包`。
3. 预算：按 `videos_per_run × 单条估价（≈ 该档 15 秒参考价 + 0.2）× 1.3` 建议一个 `credits_cap_per_run`，让用户确认或改。
4. 组装模版 JSON → `autopilot_run(action="validate_template", template=…)`；无效就按提示改。字段：`account_profile, categories, hot_source("auto"), credits_cap_per_run, videos_per_run, model_tier, tolerances(默认), publish_mode, visibility("public"), content_forms(默认全部), topics_blocklist`。
5. `desktop_publish(action="precheck")`：自动发布模式下若创作者中心未绑定，提醒用户在授权中心绑定（`desktop_login(action="open", site="douyin_creator")`）；热点宝未绑定会退到公开热榜，也说一句。
6. `cron(action="add", name=…, schedule=…, task="按 marketing-autopilot 技能运行一次", template=<validated JSON>)`。告诉用户：每次运行结束会收到一条报告；预算超了会停；风控会自动降级为扫码。

## 运行一次（定时或对话）

### 0. 开局
`autopilot_run(action="start", template=…)`（定时：模版块原样；对话：当次确认的字段）。记下 `model / resolution / videos_planned / tolerances`。**模型和分辨率由模版固定，不许换。** `creator_context(action="get_user_context")` 读人设补充。

### 1. 选题
`hot_trends(action="list", source=<模版 hot_source>, category=<categories 之一>, limit=20)`（多类目就多调几次，缓存命中不花钱；真采集前先 `reserve kind=hot_trends credits=0.2`）。对每个候选 `autopilot_run(action="record", kind="candidate", data={id,title,topics,form?})`：`blocked_topic` 非 none 或 `form_allowed=false` 的跳过。取前 `videos_planned × 2` 个候选备用。对话模式：把候选列成表出**选题卡**。

### 2. 拆解
对候选逐个：先 `reserve kind=analysis credits=0.15`，再 `video_analyze(source=<media_url 或 resolve 出的直链>)`（热点宝条目 `has_media_url=true` 直接用；公开榜先 `hot_trends(action="resolve")`）。`form` 不在模版 `content_forms` 或为「其他」→ 记录并换下一条。够 `videos_planned` 条合格拆解即停。

### 3. 按配方规划与生成
查 `references/recipes.md` 取该 `form` 的段数、时长、声音与字幕方案。写每段 prompt（口播按 video-production 的骨架；其余形态按配方）。对每段：`video_generate(action="estimate", model=<模版模型>, resolution=<模版分辨率>, ratio="9:16", duration=<秒>, …)` → `autopilot_run(action="reserve", kind="generation", credits=<estimated_credits>, note="video1 shot2")` → 允许才 `submit`；同一条视频的各段同一响应并发提交，然后一起 `wait`。

### 4. 质检
每段拿到成片后：口播段 `video_transcribe` 取转写和时长，其余形态只量时长。`autopilot_run(action="judge_shot", shot="v1s2", planned_sec=…, actual_sec=…, similarity=…, spoken=<是否口播>)`：
- `accept` → 进合成；
- `regenerate` → 只重生这一段（同 prompt 先试一次，再改 prompt），先 `reserve`；
- `drop` → 这条视频弃用，报告写原因，继续下一条。

### 5. 合成
`video_compose(action="schema")` 只在第一次；按配方组时间线（口播用 `spoken_preset`，字幕用实际转写），`validate`（免费）→ `reserve kind=compose credits=<报价>` → `submit` → `wait`。成片 `share_file` 登记拿 `asset_id`。

### 6. 文案与发布
标题 ≤ 30 字、简介 1–3 句、话题 3–5 个（来自热点 `topics` 与人设），`declaration="ai"`，`hot_word` 填热点词，`visibility` 按模版。
- `publish_mode=auto` → `desktop_publish(action="precheck")`，`can_auto_publish=true` 就 `publish`；`degrade=true` / `login_expired` / 预算或时段受限 → 这条改 `douyin_publish(action="publish", …)` 出投稿码，并在报告写明原因。
- `publish_mode=package` → 直接 `douyin_publish`。
对话模式在这一步出**成片+文案确认卡**后再发。

### 7. 报告（每次运行必发，哪怕一条都没成）
`autopilot_run(action="report", items=[…])`，每条一个对象：`{source, hot_title, form, analysis_summary, shots, final_asset_id, title, intro, topics, credits, publish:{mode,status,item_id|reason,visibility}, dropped_reason}`。把返回的 markdown 作为最终回复原样发出（定时任务会注入到用户的会话）。报告里的花费来自账单，不要自己改数字。

## 硬规则
1. 任何付费步骤前必须 `reserve`；`allowed=false` 后**不再提交任何付费任务**，已成片的照常发布，报告写「预算到顶」。
2. 一段最多重生 `max_regenerations` 次，由 `judge_shot` 计数；不要自己再试。
3. 不换模型、不换分辨率、不拆更多条数、不建议开多账号。
4. 定时模式下没有人回答问题：遇到需要人的事（登录失效、余额不足）就降级或跳过，写进报告，`NO_REPLY` 只在**没有任何**可报告内容时使用。
5. 对用户不暴露 job_id / asset_id / run_id；报告面向客户。
