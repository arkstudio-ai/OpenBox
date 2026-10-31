# Light 模式实施计划（对照 `LIGHT_MODE_PLAN.md`）

> 2026-10-31。依据：PR #105 的 `docs/LIGHT_MODE_PLAN.md`（10-27 拍板版，下称「方案」），对照 origin/main `71818450` 逐处核实落点。
> 范围与方案一致：**只做 App**，Web 不在本轮。本文把方案 §6 的四期拆成可派单的工作包，每包给落点文件、依赖和验收；方案里的产品理由不再重复。
> 对外文案一律 bossip，代码标识符不动。

## 0. 核实后对方案的修正

写方案时核实的是业务事实（费率、免费档、桌面依赖）。这次对着代码核实实现路径，有 14 处与方案的假设不同，直接影响工作包怎么切。

| # | 方案假设 | 代码事实 | 对实施的影响 |
|---|---|---|---|
| 1 | 「复用 `video_generate` 的 submit 内部函数」 | 没有可单独调用的 submit 函数，整段提交逻辑内联在 `backend/tool/video_production.py:2514` `execute_generate` 里；`video/job_recovery.py:411` 已有"手工构造 `ToolContext` 调工具"的先例 | `api/first_video.py` 直接构造 `VideoGenerateArgs(action="submit", ...)` + `ToolContext(session_id, user_id, workspace_id, project_id, message_id)` 调 `execute_generate`，放到 `asyncio.create_task` 里，不阻塞 HTTP |
| 2 | 「model = 档位 low 解析出的模型」在工具里解析 | 工具不解析档位，只认会话行上的 `video_model` / `video_resolution`（`video_production.py:1755,1774`，不一致直接抛错）。档位→模型的现成函数是 `backend/api/v1/quality.py:35` `resolve_quality(quality, config)` | 先 `create_session`，再把 `resolve_quality("low", get_config())` 的结果 UPDATE 到会话行（`api/v1/sessions.py:90-108` 的做法），然后提交 |
| 3 | 成片 FilePart 由现有链路自动落到会话 | `_attach_completed`（`video_production.py:870`）**没有 `ctx.message_id` 就什么都不做**；后台补扫 `_recover_job` 也不带 message_id，靠唤醒 agent 再调一次工具来贴 | 首条视频端点先 `create_assistant_message` 建一条占位助理消息拿 `message_id`，并把它写进 `request_data.first_video`，完成分支用它贴片（见 B4） |
| 4 | 模板 JSON 放 `video-production/references/`，「只在后端读」 | `backend/sandbox/skill_runtime.py:19-34` 把技能目录下**所有文件**同步到每台桌面 `/opt/openbox/skills/`，`wuying_bootstrap.py:310` 同理 | 模板放 `backend/light/first_video_templates.json`，与 `entries.json` 同目录，照 `billing/plans.py:64-70` 的 `lru_cache` + 严格 Pydantic 加载 |
| 5 | 首条视频状态写 `user_preferences.extra.onboarding.first_video = {...}` | 服务端 `preference_repo.py:59-60` 对 `onboarding` 整体替换；App `OnboardingController._normalize`（`mobile/lib/features/onboarding/state/onboarding_store.dart:102-106`）只保留 bool/str，对象值被丢，之后每次 `markSeen` 都 PUT 全量 onboarding | 改放 `extra.first_video`（独立顶层键；`extra` 是浅合并，安全）。App 侧新建独立 store，不塞进 OnboardingController |
| 6 | 加 ledger kind `signup_gift` 要动库 | `kind` 是 `String(24)`，无 CHECK、无 Enum（`db/models/billing.py:90`，迁移 `a8c0e2f4b6d9` 同） | **无需迁移**；要补 zh/en 标签 `admin-billing.json` `ledgerKinds`（web `frontend-v2/src/locales/*` 与 `mobile/assets/locales/*` 字节同步）；`reference_id` 非空，填 user_id |
| 7 | 「余额不足也放行一次」改闸 | 所有余额闸都是 `balance <= 0` 且仅 `BILLING_MODE=enforce` 生效（gw2 已 enforce）；工具内调 `precheck_compose`（`billing/media.py:338`）先于提交 | 不改闸。端点在提交前 `lock_balance`，余额 < 报价时按幂等键 `first_video_topup:{ws}` 一次性补差额（kind 仍 `signup_gift`），账单可见，与「上游扣了就收」一致 |
| 8 | 会话标 `source=first_video` | `sessions` 无 `source` 列；`metadata` 列契约是「调用方所有、agent 永不读」（`db/models/session.py:44`），只有 `/v1` 写 | 首条视频会话用 `kind="normal"` + 标题「我的第一条视频」，身份记在 `video_jobs.request_data.first_video` 与 `extra.first_video.session_id`。M2 的入口身份加一列 `sessions.light_entry`（见 L4） |
| 9 | 固定模板 = 每人一条自己的片 | `_find_reusable_segment`（`video_production.py:1638`）按 `prompt_hash` **跨用户复用已完成成片**，不花钱直接 OSS 拷贝；`_in_flight_duplicate`（`:2667`）对同内容在途作业返回"已在运行"而不是新建；H3 走 size 形态，seed 可能被清空（`:1809`，待确认） | 无照片 + 同模板的用户提示词完全相同 → 命中复用。两种选择见 §9 第 4 条。无论选哪种，端点都要处理「在途重复」返回值（两位销售同时演示同一模板） |
| 10 | 花字叠加「+1 张 IMS 作业」 | `compose_recovery.py:19` 只轮询和结算，**不贴片、不唤醒**；`credit_cap.IN_FLIGHT_JOB_KINDS` 不含 `compose`（在途漏算） | 做花字就要给 first_video 再串一条「compose 完成 → 贴片 → 推送」链（B6）。这是 §8 第 3 条拍板的真实代价 |
| 11 | 「新模板 `first_video_ready`」 | 没有任何 video ready/failed 模板；新 kind 要改 6 处：`notifications/templates.py`、`store.py:96` 白名单、`inbox.py:23` 分类、`inbox.py:45` 链接、`events.py:156` guard、App `inbox_navigator.dart:44` | 列为 B5 一个独立工作包 |
| 12 | `question` 消息「加一个 `light` 元信息」 | `Question.detail` 已是自由 dict（`question/question.py:69`，memory 工具在用）；但 `detail` 不在工具参数 schema（`tool/question_tool.py:34`）里，agent 填不了 | 给 `QuestionItem` 加可选 `step_id`，`ask` 时合成 `detail["light"]={entry_id, step_id, kind}`；App `QuestionItem.detail` 已解析（`interaction.dart:81`） |
| 13 | 底部 tab「专业模式保持抽屉不动」 | App 没有任何 tab 壳：go_router 全部挂根导航器，无 `ShellRoute`（`mobile/lib/app/router.dart:157`）；每个中心页自带 Scaffold + AppBar + 返回键；`WorkspaceShell` 的 PopScope 把系统返回绑成「开抽屉」（`workspace_shell.dart:195-206`）；`composer.dart` 788 行、`session_drawer.dart` 942 行（上限 800） | T2 用 `StatefulShellRoute.indexedStack` 另起一棵 light 路由树，pro 路由树不动；inbox/settings/assistant 各加 `embedded` 形态；新 UI 全部新文件 |
| 14 | 「拍一张或从相册选」 | App 无相机能力：没有 image_picker/camera 依赖，iOS 只有麦克风与相册权限，Android 无 CAMERA；相册走 `file_picker`（`pick_source.dart:30`） | A3 引入 `image_picker` + 两端权限声明，M1 就做，M2 专题页复用 |

另外两条顺手发现、不改方案但要记着：免费档每周 10 积分是北京时间周一懒发且**累积不清零**，赠送 4 积分会叠在上面；`model_tiers.video[].label` 是服务端下发的，App 直接显示，所以「省钱 / 标准 / 高清」只改 gw2 `openbox.json` 即可，但 Web 专业模式也会跟着改名（§9 第 5 条）。

## 1. 分期总览

| 期 | 内容 | 粗估 | 开始条件 | 并行关系 |
|---|---|---|---|---|
| M0 量数 | 20 条 15s H3 耗时 + 12 条模板定稿 | 2 天 | 立即 | — |
| M1 首条视频 | 后端 B1–B7 ∥ App A1–A7 | 2 周 | M0 的 p50（等待页估算用）；§8 第 1/2/3/9 条拍板 | 后端与 App 并行，第 2 周联调 |
| M2-0 原型 | 已完成（10-27 三轮画布）；只剩「省心托管默认值」 | — | — | — |
| M2 App Light | 后端 L1–L8 ∥ App T1–T8 | 3 周 | App T2 tab 壳不依赖后端，可与 M1 第 2 周重叠开工 | 后端 L1/L2/L5 第 2 周可起 |
| M2.5 省心托管 | 结果卡开关 + 三问卡 + 托管列表 | 1 周 | §8 第 11 条默认值拍板 | 可并入 M2 末或单独一周 |
| M3 你拍我剪 | F1–F9 | 3–4 周 | M2 的专题页组件与 `light/start`；§8 第 6/7/10 条拍板 | F1–F3 的 spike 可在 M2 期间穿插半天 |
| M4 衔接 | `stores` 合并后首页改读门店 | 随 PR #60 | PR #60 合并 | — |

关键路径：M0 → B2/B4（首条视频提交与完成链）→ A3 六屏联调 → gw2 后端发版 + App 测试包。按两人（后端一人、App 一人，技能与模板由后端兼）排，M1 第 3 周结束可出第一个可演示包；M2 在第 6 周末；M3 在第 10 周末。

## 2. M0 量数（2 天，后端）

| 任务 | 做法 | 产出 |
|---|---|---|
| H3 15s 耗时 | 在 gw2 后端容器里按 `job_recovery.py:411` 的方式构造 `ToolContext` 调 `execute_generate`，跑 20 条 15s 768p 9:16（10 条带 `input_assets=[{asset_id, role:"reference_image"}]`），从 `video_jobs.started_at/completed_at` 算 p50/p90；成本约 20 × 1.35 元 | p50/p90；验收线 p90 ≤ 180s，超线则等待页默认走「做好通知你」 |
| 参考图出片率 | 招牌菜 10 张、门头 10 张，统计审核拒绝与画面跑偏比例 | 决定 A3 第 4 屏是否默认展开拍照 |
| 模板定稿 | 行业 4 × 模板 3 = 12 条提示词，各跑 3 次（约 36 × 1.35 元），淘汰出片率低的写法；**顺手确认 H3 是否接受 `seed`**（`video_production.py:1809`） | `backend/light/first_video_templates.json`，每条带 `verified_at`、`shots`、`negative`、`with_image`/`without_image` 两版 |

## 3. M1 首条视频（2 周）

### 3.1 后端工作包

| 包 | 内容 | 落点 | 验收 |
|---|---|---|---|
| B1 模板加载 | `backend/light/__init__.py`、`templates.py`：`lru_cache` 读 `first_video_templates.json`，严格 Pydantic（`extra="forbid"`），`LIGHT_TEMPLATES_FILE` 环境变量可覆盖 | 照 `billing/plans.py:64-70`；镜像里有文件靠 `Dockerfile:20` `COPY backend/ .` | 单测：缺字段启动报错；按 (industry, template, has_image) 取到提示词 |
| B2 提交端点 | `backend/api/first_video.py`，`APIRouter(prefix="/api/first-video", dependencies=[Depends(get_workspace)])`，在 `main.py:514-519` 附近注册。`POST /`：body `{industry, shop_name, headline, template, asset_id?}`（`extra="forbid"`）。流程：① 按 `(user_id, "segment", "first_video:{ws}")` 查 `video_jobs`，已有则直接返回状态；② 回写 `assistant_profile.business` 与 `extra.first_video={status:"submitted", template, shop_name, headline}`；③ `create_session(kind="normal", title="我的第一条视频", user_id, workspace_id, project_id=默认项目)`，UPDATE `video_model/video_resolution` = `resolve_quality("low", get_config())`；④ 写一条用户消息（表单摘要）+ `create_assistant_message` 占位 → `message_id`；⑤ `lock_balance` → 余额 < `quote_generation` 时 `post_ledger(kind="signup_gift", key="first_video_topup:{ws}", reference_id=user_id)` 补差额（一次）；⑥ `asyncio.create_task(execute_generate(VideoGenerateArgs(action="submit", prompt, ratio="9:16", duration=15, input_assets, idempotency_key="first_video:{ws}", allow_duplicate=True), ToolContext(..., message_id)))`，提交前把 `{message_id, template, shop_name, headline}` 塞进 `request_data.first_video`；⑦ 返回 `{session_id, eta_seconds}`（eta 来自 M0 p50，放模板文件顶层）。`GET /`：按幂等键查作业，返回 `{status, session_id, asset_id, error, eta_seconds}` | 模板 `api/inbox.py`（`get_current_user` + `get_workspace`）；工具调用方式照 `job_recovery.py:411`；档位解析 `api/v1/quality.py:35`；会话列写法 `api/v1/sessions.py:90-108`；占位消息 `session/session.py:1444` | 同一工作空间重复 POST 返回同一作业；余额 0 的免费档账号能提交且账单多一行 `signup_gift`；无 `asset_id` 走纯文字模板 |
| B3 注册赠送 | `db/repository/user_repo.py:11` `create` 成功后（两处调用：`auth/routes.py:158` 密码注册、`:361` Logto 首登）发 `signup_gift` 4 积分，幂等键 `signup_gift:{ws}`；金额放 `billing/plans.json` free 档新字段 `signup_gift_credits`；标签 zh「新人首条视频」/ en 加到 web 与 mobile 的 `admin-billing.json` `ledgerKinds` | 幂等写法照 `billing/subscriptions.py:51-63`（lock → select by key → `post_ledger` → flush） | 新注册账户 `GET /api/billing/ledger` 有一行 kind=signup_gift；重复登录不重复发 |
| B4 完成分支 | `video/job_recovery.py`：`_recover_job` 与 `resume_unattached_completions` 在 `job.request_data.get("first_video")` 时走新分支：用保存的 `message_id` 构造 ctx 调 `_attach_completed` → `save_part(TextPart("这是你的第一条视频。想改风格、换文案，直接告诉我。"))` + 3 条 next-step 芯片 → 关闭占位消息 → `extra.first_video.status="ready"` → `emit(kind="first_video_ready", link=session)`；**不调 `resume_conversation`**（不花 LLM 回合）。失败：若带参考图且错误是审核拒绝 → 去掉图按 `first_video:{ws}:retry` 重提一次；再失败 → `status="failed"` + `first_video_failed` | `_attach_completed` `video_production.py:870`；固定助理消息写法照 `cron/injector.py:276` `_commit_injection`；sweep 每 60s 一次（`cron/timer.py:152`），首条视频可把 `_recover_job` 的 120s 门槛对 first_video 作业降到 30s | 后台完成的作业 60s 内贴回会话、推送到达、会话里没有 agent 回合 |
| B5 通知 | 新 kind `first_video_ready` / `first_video_failed`：`notifications/templates.py` TEMPLATES、`store.py:96-98` 白名单、`inbox.py:23` `CATEGORY_FOR_KIND`、`inbox.py:45` `link_for`（session 链接）、`events.py:156` `guard_valid`（guard=None 直接过） | 端到端范例：`question/runtime.py:641` → `events.py:38` `task_finished` | 推送点开落到会话；消息中心有记录 |
| B6 花字叠加（§8 第 3 条拍板后才做） | B4 分支里 generate 完成后再 `video_compose` submit（timeline：1 个 shot + `TextClip` 店名/slogan，字体 `AlibabaPuHuiTi`），把 compose 作业也标 `first_video`；`compose_recovery.py:19` 加同样的完成分支（贴片 + 推送）；顺手把 `credit_cap.IN_FLIGHT_JOB_KINDS` 加 `"compose"` | `video/timeline.py:73` TextClip、`ims_compiler.py:145`；`compose_recovery.py` 目前只结算不贴片 | 成片右下角店名正确；账单多一行约 0.045 积分 |
| B7 测试 | `backend/tests/unit/test_first_video.py`：幂等、余额补差只一次、无图路径、完成分支不唤醒 agent、重复在途返回同作业；`test_onboarding_preferences.py` 风格加 `extra.first_video` 浅合并用例 | 跑法见 `MODEL_TIERS` 记忆：`uv run --with aiosqlite --with pytest --with pytest-asyncio pytest` | 新用例全绿；origin/main 既有 7 条失败不新增 |

### 3.2 App 工作包

| 包 | 内容 | 落点 | 验收 |
|---|---|---|---|
| A1 状态与接口 | 新 feature `mobile/lib/features/first_video/`：`state/first_video_store.dart`（读写 `extra.first_video`，本地 SharedPreferences 缓存 + `ensureLoaded`，照 `OnboardingController` 但**独立**）、`api/first_video_api.dart`（POST/GET `/api/first-video`） | `onboarding_store.dart:63` 为模板；偏好接口 `settings_api.dart:15,22` | 单测：服务端为真源、冷启动能取 |
| A2 开屏与分流 | `intro_banner_page.dart` 三屏压一屏：定位句 + 示例成片自动播（`video_player`，素材随包）+「免费做一条你的店的视频」；CTA → 登录/注册；`auth_flow.dart:79` `_completeAuth` 后判定：`first_video` 未完成 → `/app/first-video`，否则 `postAuthDestination` | `auth_redirect.dart:24-31`、`paths.dart:38` | 新账号登录后直接进六屏；老账号不受影响 |
| A3 六屏 | 路由 `/app/first-video/{shop,template,photo,making,result}` 挂根导航器（不进 `WorkspaceShell`）。你的店：行业/店名/主打，PUT `/api/assistant/profile` business（`assistant_profile.dart:229`）；选模板：三卡 + 5s 示例片（随包）；拍一张：**引入 `image_picker`**，iOS `NSCameraUsageDescription`、Android `CAMERA`，上传走 `resources_api.dart:54` 三步（POST `/api/assets` → PUT OSS → `/complete`），可跳过；制作中：轮询 GET（3s）+ eta 进度 + 三张能力卡轮播，可退到后台；成片：复用 `attachment_gallery.dart:513` `_VideoBox` 播放与 `asset_download.dart:35` 保存；「发到抖音」免费档 → `/app/billing` 并带一句「发布需要云电脑」；「换个风格」→ `/app/s/{session_id}`；「去首页」→ `/app`（M2 后改 `/home`） | 文案 `assets/locales-mobile/*/first_video.json` + `i18n.dart:20` `_mobileNamespaces` | 不输入任何自由文本能走到成片；每一步可跳过 |
| A4 推送落地 | `inbox_navigator.dart:44` 加 `first_video_ready/failed` → 成片页；App 在前台时 `notification_host.dart:161` 同样路由 | — | 退到后台后点推送直达成片 |
| A5 设置项 | 「重新查看新手引导」（`account_section.dart:39-44`）只重放介绍，不触发 first_video；`reset()` 不清 `extra.first_video` | — | — |
| A6 文件尺寸与 lint | 新页面全部新文件；`scripts/check_file_size.sh` 800 行、`check_locales.sh` 字节同步不破 | — | CI 门禁绿 |
| A7 测试 | `test/features/first_video/`：六屏跳转、跳过路径、轮询到 ready 切页；假 Dio 照 `onboarding_widgets_test.dart:19,76-90` | — | — |

### 3.3 M1 验收与发布

- 验收按方案 §6：新注册用户不看说明、不打字，3 分钟内拿到带店名的 15 秒竖屏视频；免费档余额 0 也能做一次；「发到抖音」在免费档跳开通页。
- 发布顺序：gw2 后端（**先全树比对**，线上镜像有 19 个文件不在 git）→ 验证 `openbox.json` 的 `model_tiers.video` 含 `low` = MiniMax-H3（gw2 已有）→ App 双端测试包（iOS TestFlight、Android 正式签名本机）。无数据库迁移。AWS 不发。
- 提交一律走临时 worktree（主工作树被 Codex 共用）。

## 4. M2 App Light（3 周）

### 4.1 后端工作包

| 包 | 内容 | 落点 |
|---|---|---|
| L1 入口配置 | `backend/light/entries.json` + `entries.py`：版本号、每入口 `{id, title, subtitle, sample_media[], steps[{id,label}], opening_card{text, chips[]}, price_hint, default_tier, requires:{plan?, desktop?, binding?}, skill}`；第一版 7 个入口：`video_shop`/`video_dish`/`video_visit`（做视频三子类）、`menu`、`poster`、`douyin_publish`、`review`（成绩复盘）、`shopkeeper`（掌柜）、`footage_edit`（灰卡） | 同 B1 的加载模式 |
| L2 两个接口 | `backend/api/light.py`：`GET /api/light/entries` 把 `requires` 算成 `availability: ok/needs_plan/needs_desktop/needs_binding`（套餐看订阅服务；桌面看 `desktop_publish` 返回 `SANDBOX_SUBSCRIPTION_REQUIRED` 的同一判定；登录态看授权中心平台状态）；`POST /api/light/start {entry_id, project_id?, context?}`：`create_session` → `resolve_quality(default_tier)` 写会话 → 写 `sessions.light_entry` → 开场卡作为第一条助理消息直接落库（复用 B2 的"直接落助理消息"函数）→ 返回 `session_id`；之后 App 走现有 `prompt_async` | `api/inbox.py` 模板；开场卡落库复用 `cron/injector.py:276` 写法 |
| L3 `ui_mode` 偏好 | `PUT /api/auth/me/preferences {extra:{ui_mode:"light"|"pro"}}`，schema 不用改；注册两处（`auth/routes.py:158,361`）新用户 upsert `extra.ui_mode="light"`；老用户为空视为 `pro` | `preference_repo.py:48` 浅合并 |
| L4 会话入口身份 | 迁移：`sessions` 加 `light_entry String(32) NULL`（head `ph73f4a5b6c7` 之后，命名 `pi84…`，模板 `pg40c1d2e3f4_session_credit_cap.py`）；`Session` 内部模型映射该列 | 不用 `metadata` 列（契约是调用方所有） |
| L5 系统提示词 | `agent/loop.py:1785-1808` 的每回合提示里加：`ui_mode=light` 时一句「用户在 Light 模式：每步只问一件事，能点选不让打字，价格按整条算」；有 `light_entry` 时加入口名与 `steps[]`，要求每张 `question` 卡带 `step_id` | `profile._extra`（`assistant/profile.py:169`）读偏好 |
| L6 `question` 卡元信息 | `tool/question_tool.py:34` `QuestionItem` 加可选 `step_id`；`question/question.py:247` `ask` 时若会话有 `light_entry` 则写 `detail["light"]={entry_id, step_id, kind}`；`question.asked` 事件自带 detail | App 侧 `QuestionItem.detail` 已解析 |
| L7 技能 | `video-production/SKILL.md` 加「Light 单卡」段（条件：系统提示词含 `ui_mode=light`，三卡合一，`credit_cap` 仍兜底），记入 `C5_VIDEO_SKILL_ALIGNMENT.md` 例外表；新技能 `backend/.openbox/skills/menu-maker/SKILL.md`（视觉识别旧菜单 → 菜品表 → `image_gen`），新目录即被 `skill/skill.py:56` 发现；做海报复用 `shop-copy` + `image_gen`；`agent/tool_exposure.py:60-72` 意图包按入口放行工具 | `video-production` 的硬编码路由在 `assistant/commands.py:365,494`、`placement.py:150`、`assistant_tools.py:624`，新技能不需要进这些 |
| L8 「最近做的」聚合 | `GET /api/light/recent`：按会话分组返回最近 N 个会话的产出（FilePart：图、视频、发布状态），供首页；避免 App 拉全部历史再筛 | `db/models/part.py:32` `data` JSON；`FileRelation.kind` 区分 |
| L9 配置 | gw2 `openbox.json` `model_tiers.video[].label` → 省钱 / 标准 / 高清（Web 同步改名，见 §9 第 5 条）；`entries.json`、`first_video_templates.json` 随镜像 | 改 `openbox.json` 要重启 backend，以 `session_executions` 租约为闸 |

### 4.2 App 工作包

| 包 | 内容 | 落点 |
|---|---|---|
| T1 `UiMode` | `features/light/state/ui_mode_store.dart`：`enum UiMode {light, pro}`（与 `ColorMode.light` 区分），读 `extra.ui_mode`，本地缓存，**冷启动自取**（App 今天只在登录后取偏好，`auth_flow.dart:109-115`）；注册完成默认 light；设置页「界面模式」开关（`settings_screen.dart:30-38` 加入 appearance 或 account 段） | — |
| T2 tab 壳 | `app/router.dart` 加 `StatefulShellRoute.indexedStack` 四分支：`/home`、`/assistant`、`/inbox`、`/me`；`ui_mode=light` 时 redirect 把 `/app` 导到 `/home`，pro 路由树一字不动；`NavigationBar` 样式照 `admin_console.dart:70-93`；`theme_builder.dart` 加 `navigationBarTheme`；light 下 `WorkspaceShell` 的 PopScope「返回=开抽屉」停用 | `router.dart:62-242`、`paths.dart` |
| T3 内嵌版页面 | `InboxScreen`（`inbox_screen.dart:123`）、`SettingsScreen`、`AssistantScreen`（`assistant_screen.dart:39`）加 `embedded: true`：去掉自己的 AppBar 返回键，body 复用 | — |
| T4 首页 | `features/light/widgets/home_screen.dart` + `entry_card.dart` + `recent_groups.dart`：顶部店名（`assistant_profile.business`，M4 后读 `stores`）与积分（`/api/billing` 余额，点进 `/app/billing`）；入口卡渲染 `GET /api/light/entries`，`availability≠ok` 画灰卡并跳订购/云电脑；黑色横幅「省心托管」→ 托管列表（M2.5 前先指向 `/app/cron`）；「最近做的」用 L8；底部固定输入条 → `/assistant` | 新文件，避开 800 行 |
| T5 对话专题页 | `features/light/topic_chat_screen.dart`：样片画廊页（`entry.sample_media` 子类切换 → 「就做这种」→ `POST /api/light/start`）；对话页复用 `ChatScreen` 内核（`chat_screen.dart:116` rows、`:230` ChatFlow、`QuestionDock`），加顶部步骤条（`entry.steps` + `QuestionItem.detail.light.step_id` 点亮）；Light 卡皮肤：`question_dock.dart:391` 按 `detail.light` 分支走大芯片卡；工具轨迹折叠一行（`tool_chain_trace.dart:38` summary + 简明模式字典）；上下文输入条 `light_input_bar.dart`（当前步骤芯片 + 文本 + 相机/相册），`composer.dart` 788 行不再加代码 | — |
| T6 「对话」tab | 内嵌 `AssistantScreen`；列表合并：个人助理会话（`/api/assistant/sessions`）与专题页创建的 normal 会话（`workspace_api.dart:74`）同列，按时间排 | §9 第 6 条 |
| T7 「我的」tab | 订购、授权中心、云电脑、资源、设置入口 + `UserRow`；复用现有页面（push 到根导航器） | `user_row.dart:16` |
| T8 测试与门禁 | tab 壳路由测试、首页灰卡渲染、专题页步骤条点亮、`ui_mode` 切换后 pro 界面与今天一致（`workspace_shell_test.dart` 不改）；`check_file_size.sh`、`check_locales.sh` | — |

### 4.3 M2 验收与发布

- 验收按方案 §6：从首页点「做一条视频」到看到确认卡只需点选；销售现场能指着按钮讲三大特色；老用户切回专业模式后界面与今天一致。
- 发布：**带迁移**（L4）→ gw2 后端 → `openbox.json` label 改名 → App 发版。老用户 `ui_mode` 为空 = pro，不被动切换。

### 4.4 M2.5 省心托管（1 周，§8 第 11 条拍板后）

结果卡右下角开关 → 三问卡（什么时候 / 只出片还是直接发 / 每次上限）→ 建 cron + 技能模板（视频走 autopilot 预算模板，上下架、回评价、成绩复盘各一个小模板）；首页横幅与「我的 → 托管中的事」列表（暂停 / 取消）。这同时解决「autopilot 预算模板只能对话创建」的旧问题。免费档只放行「只出片」。

## 5. M3 你拍我剪（3–4 周）

前提：§8 第 6（定价）、7（BGM）、10（是否对免费档开放）条拍板。F1–F3 是免费档开放的前提，也是最大的一块。

| 包 | 内容 | 落点 |
|---|---|---|
| F1 媒体探测 | 迁移：`file_assets` 加 `meta JSON NULL`；`api/assets.py:289` `/complete` 对 `video/*` 调 IMS `RegisterMediaInfo` + `GetMediaInfo` 写 `{duration_sec, width, height, rotation, codec, has_audio, media_id}`；同时补上 `video_compose` 报价缺 `duration_sec` 的缺口 | `video/ims_client.py` 加两个调用；`db/models/file_asset.py:17` 今天**没有** `meta` 列 |
| F2 采帧搬离桌面 | `tool/video_analyze.py` 加 `source=ims` 分支：`SubmitSnapshotJob` 按秒抽帧到 OSS → 现有视觉提示词；`sandbox_required` 改为按分支判定（`:673`） | 今天 ffprobe/ffmpeg 全在 `ctx.sandbox.execute`（`:160-193`） |
| F3 转写 | spike 半天：DashScope fun-asr 能否直接吃视频 URL；不行则 IMS 转码出音频再喂。`execute_transcribe`（`video_production.py:3298`）在 `source=ims` 下放开 `video/*` | `:3323` 今天拒绝非 `audio/*` |
| F4 音轨 | `video/timeline.py:123` `Timeline` 加 `audio: [{asset|library_id, from_sec, to_sec, volume, fade}]`（`extra="forbid"` 要显式加字段）；`ims_compiler.py:69` 输出 `AudioTracks`，原声 + BGM 两轨，口播段压低 BGM；横屏素材 `fit=cover` 居中裁；HEVC/mov 作为 MediaURL 输入在 IMS 侧实测 | 今天没有任何 `AudioTracks` |
| F5 compose 完成链 | `compose_recovery.py` 加贴片 + 唤醒（或 footage_edit 专用推送）；`credit_cap.IN_FLIGHT_JOB_KINDS` 加 `compose`（B6 若已做则跳过） | `billing/credit_cap.py:38` |
| F6 费率与结算 | `billing/rates.json` `media` 加 `footage-analyze`（1 积分/分钟、不足 1 分钟按 1）与 `footage-compose` 阶梯（≤60s 5、≤180s 9、每 +60s +2）；`billing/media.py` 新 `quote_footage_*` / `settle_footage_*`；`settle_compose` 在 `kind=footage_edit` 时按阶梯；方案卡出卡前先结分析费并在卡上写明 | `media.py:121,356,437` |
| F7 技能 `footage-edit` | SKILL.md + `references/structure-templates.md`（探店 / 新品 / 活动 / 日常四种结构）、片段挑选规则、字幕规范、方案卡格式；`tool_exposure.py` 意图包只放 `video_analyze / video_transcribe / video_compose / question / share_file`；`video_compose.py:153` 与 `references/compose-timeline.md` 的 0.03/0.06 成本价改售价口径 | — |
| F8 入口与 App | `entries.json` `footage_edit` 从灰卡转可用；App 多段上传（`pick_source.dart` 放开 `allowMultiple`，逐条进度），专题页复用 T5 | — |
| F9 通知 | `footage_edit_ready/failed`，改法同 B5 | — |

验收按方案 §6：免费档账号传 5 段手机实拍（含 1 段横屏、1 段 HEVC）→ 一张方案卡 → 60 秒成片，总耗时 ≤ 5 分钟；账单两行与方案卡一致；BGM 不压过人声。

## 6. M4 衔接

PR #60（`stores` 表、绑店回填、人设初始化）合并后：首页顶部店名与「做一条视频」默认主题改读 `stores`；`starter-cards` 接口喂首页「今天可以做」；「掌柜」的数据区接 `store_ledger`（OPS_CASE M2）。不单独排期，随 PR #60 的节奏。

## 7. 第一周动作清单

1. 拍板 §8 第 1、2、3、9 条与下面 §9 第 4、5 条（都在 M1 路径上）。
2. 后端：M0 脚本 + 跑数（第 1–2 天）；并行起 B1、B2 骨架。
3. App：A3 的 `image_picker` 与权限先合（最小 PR，M1/M2 共用）；A1 store 与 A2 分流。
4. 产品：12 条模板文案与 7 个入口的 `entries.json` 初稿、三张能力卡文案、灰卡文案。
5. 第 2 周：B4 完成链 ↔ A3 制作中/成片页联调；T2 tab 壳开工。

## 8. 风险（实施层，补方案 §7）

| 风险 | 应对 |
|---|---|
| `execute_generate` 内部逻辑（在途重复、复用、预检、cap）为 agent 场景设计，HTTP 直调的边界情况多 | B7 单测覆盖四条返回路径；端点对所有非"新建作业"的 `ToolResult.metadata` 统一映射成 GET 可读状态 |
| 完成分支依赖 60s 一次的 sweep，等待页会多等 0–60s | first_video 作业把 `_recover_job` 的"未动 120s"门槛降到 30s，或端点内 `create_task` 自己轮询到完成再走分支 |
| 两个新 JSON（模板、入口）随镜像走，改文案要发后端 | 方案已接受；用 `LIGHT_*_FILE` 环境变量指向挂载路径，gw2 可不重建镜像只重启 |
| M2 的 tab 壳是 App 侧最大的结构改动，`router.dart` 的"恢复上次会话"redirect（`:78-105`）与 light 分流容易打架 | light 下只恢复到 `/home`，不恢复会话；写路由测试 |
| 两位销售同时演示同一模板（无照片）→ 第二个命中在途重复 | 端点传 `allow_duplicate=True` 或等待并复用同一作业；§9 第 4 条一并定 |
| `BILLING_MODE` 在新环境默认 shadow，余额闸不生效，首条视频"余额不足补差"在 shadow 下会白送 | 补差逻辑只在 enforce 下执行；shadow 下记日志 |

## 9. 仍需拍板

| # | 事项 | 建议 | 阻塞 |
|---|---|---|---|
| 1 | 方案 §8 第 1 条：15 秒 | 15 秒，M0 跑完若 p90 > 180s 再议 10 秒 | M0 |
| 2 | 方案 §8 第 2 条：赠送口径 | 注册送 4 积分（B3）+ 首条视频余额不足补差一次（B2 ⑤） | B2/B3 |
| 3 | 方案 §8 第 3 条：花字 | 叠；但代价是 B6 多一条 compose 完成链（约 2 天）。若 M1 时间紧，先不叠，M2 补 | B6 |
| 4 | **新**：无照片同模板跨用户复用成片 | 两选一：(a) 允许复用——无照片用户秒出片、成本 0，花字叠店名后仍"像自己的"；(b) 禁止——给 `VideoGenerateArgs` 加 `allow_reuse=False`（或模板加 3 个变体轮换），每人独一条，成本 1.35 元。建议 (a)，销售现场秒出片更有冲击力 | B2 |
| 5 | **新**：档位 label 改「省钱 / 标准 / 高清」会连带 Web 专业模式改名 | 接受改名（Web 当前为质量 / 标准 / 灵活）；或 `entries.json` 单独下发 Light 用的档位名、App 只在 light 下用 | L9 |
| 6 | **新**：「对话」tab 的会话列表是否把个人助理会话与专题页会话合并 | 合并，按时间排，专题页会话带入口小标 | T6 |
| 7 | 方案 §8 第 9 条：首条视频成片页放「发到抖音」 | 放，免费档跳开通页 | A3 |
| 8 | 方案 §8 第 11 条：省心托管默认值 | 原型上定；默认建议「每天 10:00 / 只出片 / 每次 ≤ 10 积分」 | M2.5 |
| 9 | 方案 §8 第 6、7、10 条：你拍我剪定价、BGM、免费档开放 | 按方案建议 | M3 |
