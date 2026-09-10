# 自动营销定时任务模版（Autopilot）—— 现状对照、设计与分期

> 2026-09-09 规划稿。目标：客户创建一个定时任务，系统自动「收热点 → 反推提示词与文案 → 生成 → 剪辑 → 发布」。
> 本文先把五步逐一对到 openbox 现有能力上，标出缺口与硬约束，再给出设计与分期。**两处硬约束会改变方案形状，见 §1。**
> **2026-09-09 用户拍板（§6）**：发布走后台自动（前期过渡态）；模版选三档视频模型；热点宝先用浏览器采集；不限口播、靠模型复刻不追求完美；推送做移动端、迁移 bossip 的 APNs+JPush。

---

## 0. 一页结论

| 步骤 | 现有能力 | 缺口 | 判定 |
|---|---|---|---|
| 1 收热点（热点宝） | 云桌面登录态体系（A5，站点目录含 creator.douyin.com）、dev-browser 浏览器自动化 | 热点宝未入站点目录；无「热点采集」工具；反爬与频控未知 | 可做，先 spike |
| 2 反推提示词 + 文案 | `video_transcribe`（已计费）、`view_image`（单图给多模态模型）、桌面 ffmpeg 抽帧 | agent 没有把**视频**喂给模型的通道，只能抽帧逐张看；无 `video_analyze` 工具；每条热点的分析成本未测 | 可做，需新工具 |
| 3 生成 | `video_generate` + 口播技能 + 生成计费 | 无人值守时没人点费用卡 | 需**预算授权**替代确认卡 |
| 4 剪辑 | `video_compose`（IMS）+ 合成计费 | 同上；时间线要从分析结果自动生成 | 可做 |
| 5 发布 | `douyin_publish`（开放平台 H5 投稿：**出二维码，用户在抖音 App 内点发布**） | **抖音没有服务端发布接口**；「后台登录好的创作者中心自动发」= 用云桌面浏览器登录态做 UI 自动化，技术可行但违反平台规则、有封号风险 | **拍板：前期默认后台自动发布（过渡态）**，风控降级与扫码投稿包做兜底；有用户量后再切合规路径 |

**两条硬约束**：
1. **无人值守 vs 确认卡**：现在口播技能有 3–4 张必停卡（讲稿、拆段+费用、STT、合成确认），cron 临时会话里 `question` 会一直挂到任务超时（默认 1800s）。模版必须在**创建时**把这些决定做完：预算上限、模型档位、可接受的偏差、发布方式。运行期只产报告，不出卡。
2. **发布的合规与风控**：开放平台只给 H5 投稿二维码；创作者中心网页自动化违反抖音规则且账号风控高。**拍板为前期默认自动发布**，因此工程上要把风控当一等公民：发布节奏拟人、频次上限、AI 标识、任一风控信号立即停用该账号的自动发布并降级为扫码投稿包（§1.5、§4）。有一定用户量后切回合规路径。

---

## 1. 五步逐条对照

### 1.1 收热点

- **热点宝**（douhot.douyin.com）需要抖音登录。A5 的云桌面登录态体系已有 `creator.douyin.com` 站点（cookie 域 `.douyin.com`），热点宝大概率共用登录态，但**未入站点目录**（`backend/platforms/desktop/sites.py`），要加一条并验证探活。
- 抓取走 `dev-browser`（用户云桌面里的 Chrome，带登录态），取榜单：标题、链接、播放/点赞、话题、时长、封面。频控与反爬未知，spike 时用真实账号测 1 天。
- 备选数据源：巨量算数（同登录态）、第三方付费 API（蝉妈妈/新榜/飞瓜），后者稳定但要花钱且有授权条款。设计上把「热点源」做成可插拔。
- 交付物：平台工具 `hot_trends(source, category, limit)`，返回结构化条目并**只存元数据与链接**，不下载原片入库（见 §4 合规）。

### 1.2 反推提示词 + 文案

- 分析需要看画面、听声音：
  - 声音：`video_transcribe`（fun-asr，已按分钟计费）。
  - 画面：agent 现在只能通过 `view_image` 一张一张给多模态模型看，没有视频输入通道；Gemini 3.7 Flash / Luna 在菜单里带 vision。做法：桌面 ffmpeg 按场景抽 6–12 帧 + 转写文本，一次喂给模型出结构化分析。
  - 只有字幕没配音的视频：帧里的字幕由多模态模型 OCR，同一条链路。
- 交付物：平台工具 `video_analyze(source_url | asset_id)` → JSON：主题、目标人群、钩子句、结构（分段与秒数）、镜头/画面描述、文案全文、话题、可复用的「创作要素」。这是第 3、4 步的唯一输入，也是「不搬运只学习」的边界（§4）。
- 成本要实测：一条 30s 热点 ≈ 1 次 STT（0.05）+ 10 帧多模态调用（token 计费）。

### 1.3 生成

- `video_generate` 与口播技能现成；生成计费已按申请秒数落账（`rates.json media.video-gen`）。
- 一条 15s 720p wan3 = 9 积分，seedance 720p = 14.25。预算上限直接决定每次跑几条、用哪档。
- 关键改造：技能在 cron 模式下**不出卡**，改读模版里的预授权（§2.3）。

### 1.4 剪辑

- `video_compose` 现成（IMS，已计费）。时间线由 `video_analyze` 的结构 + 生成结果 + 实际转写自动生成：钩子标题、字幕、下三分之一、转场。
- 编译层已固化黑边/锚点规则，无人值守下可靠性够；成片实际时长在结果里，报告要写。

### 1.5 发布

- 现状：`douyin_publish(publish)` 生成 H5 投稿二维码，**用户扫码后在抖音里点发布**；标题 ≤55 字、话题 ≤10；Webhook 回写结果。这已经替客户「想好了标题、介绍、话题」，只差最后一扫。
- 「后台已登录的创作者中心自动发」：技术上 = 云桌面 Chrome 登录态 + dev-browser 驱动 creator.douyin.com 上传页。**不建议默认做**：违反抖音规则（现有技能已明写「抖音不允许应用替用户发布」）、账号风控、且客户素材是 AI 生成需按平台要求标注。
- **拍板后的形态（前期过渡态）**：默认走**桌面自动发布**——`desktop_login` 确认 `douyin_creator` 站点登录态在（站点目录已有，含探活与昵称/粉丝数读取），dev-browser 在客户自己的云桌面 Chrome 里驱动 creator.douyin.com 上传页：传成片、填标题/介绍/话题、勾 AI 生成声明、点发布，回读作品 id。实现为独立技能 `douyin-desktop-publish`，与开放平台的 `douyin_publish` 并存。
- **风控与降级（必做，不是可选）**：每账号每日发布上限与最小间隔；上传页 DOM 变化或出现验证码/风控提示 → 立即停止该账号自动发布、改产扫码投稿包并推送；登录态失效 → 推送让客户在云桌面重登。所有自动发布记录到 `publish_jobs`（A5 已有表），便于追溯。
- **切换预案**：等用户量到一定规模，把默认切回「投稿包 + 一键扫码」（`douyin_publish` 已有），自动发布退为 opt-in。模版里 `publish_mode` 字段从第一天就留出这个开关。
- 通知通道现状：openbox 后端与 App 里**没有任何推送集成**（只有 `notification` 表）。bossip 的 Flutter 客户端（`apps/codex/v2/mobile`，包名 `com.bossip.bipmobile`）已实现 **iOS APNs 直连 + Android 极光 JPush**（`SystemNotifications` 组件、Android 原生桥、服务端发送逻辑在 v1 API 的 TS 里），文档 `PUSH_NOTIFICATION_ENABLEMENT.md`。**拍板：推送做移动端，迁移这套**——App 侧组件与原生桥可搬，服务端发送要从 TS 移植到 Python 并接 cron delivery。

---

## 2. 设计

### 2.1 形态：一个技能 + 一个 cron 模版 + 两个新工具

```
cron 模版「自动营销」（用户填：类目/账号人设、每次条数、预算上限、模型档位、发布方式、频率）
   └─ 临时会话运行 skill `marketing-autopilot`
        1. hot_trends()            ← 新平台工具（云桌面登录态 + dev-browser）
        2. video_analyze()         ← 新平台工具（抽帧 + STT + 多模态 → 分析 JSON）
        3. video_generate()        ← 现有（口播技能的工艺规则复用，cron 模式不出卡）
        4. video_compose()         ← 现有
        5. douyin_publish(prepare) ← 现有，产投稿包；opt-in 自动发布另起技能
        6. 报告 + 推送             ← cron delivery 新增 push
```

### 2.2 为什么是新平台工具而不是让 agent 在沙箱里自己抓自己分析
- `hot_trends` 涉及登录态与频控，要集中控制节奏、缓存榜单（同一类目一天抓一次，全体客户共享），不能每个任务各抓一遍。
- `video_analyze` 的成本要可估可控（帧数、模型），输出要稳定成 JSON 供后续步骤消费；放工具层才能计费与限流。
- 和视频原子化的分层一致：钱、凭据、幂等、限流在工具层；工艺在技能层。

### 2.3 预算授权（替代确认卡）

模版创建时一次性确认，运行期只执行：

| 字段 | 含义 | 运行期行为 |
|---|---|---|
| `credits_cap_per_run` | 单次运行积分上限 | 每步 `estimate/validate` 累加，超过即停止并报告，不部分提交 |
| `videos_per_run` | 每次产几条 | 与预算取小 |
| `model_tier` | **三档：高 = sd2（`video-sd-720p-proⅠ` / `video-sd-1080p-pro`）、中 = `wan3.0-video`、低 = `MiniMax-H3`** | 固定，不许 agent 换；对话创建任务时由 agent 用一张卡让客户选档（三档各带每 15s 参考价：按 `rates.json` 720p 档 sd2 7.5 / wan3 9.0 / H3 768p 7.5 积分，价目是成本占位，运营定价后自动跟随） |
| `tolerances` | 时长偏差、STT 相似度阈值 | 超出则**重生一次**，再超出则弃用该条并报告 |
| `publish_mode` | `auto`（**前期默认**）/ `package`（扫码投稿包） | 见 §1.5；风控触发时运行期自动降级为 `package` 并推送 |
| `content_forms` | 允许复刻的形态：口播 / 画面+配音+字幕 / 混剪 / 展示… | 由 `video_analyze` 判定热点形态后选配方（§2.6） |
| `topics_blocklist` | 客户不碰的话题 | 热点筛选时过滤 |

技能规则：cron 上下文（`ctx.origin_session_id` 非空或会话 kind=cron）下，把每张卡的「问」改成「按模版字段决定并写进报告」。enforce 模式下运行前先做一次总预算的余额校验（`billing.media.precheck_*` 已有）。

### 2.6 内容形态：不止口播，靠模型复刻

`video_analyze` 先判定热点的形态（口播 / 画面+旁白 / 产品展示 / 剧情 / 混剪…），技能按形态选配方：
- 口播 → 现有口播工艺（人物一致性、STT 核对）。
- 画面+旁白 → 生成模型出画面，旁白用带 `generate_audio` 的模型直接念，或后期 TTS（**缺口：openbox 目前没有 TTS 工具**，bossip 有 `bossip-media-gen` 的 TTS，M1 补一个 `tts` 原子工具或先只用带音频的视频模型）。
- 展示/剧情/混剪 → 多段生成 + `video_compose` 转场与字幕。
质量口径按拍板：**复刻到「能看、像那个类型」即可，不追求完美**；预算与条数优先，STT 相似度阈值放宽到口播专用。

### 2.4 报告与推送
- 每次运行产出一条报告消息：热点来源、每条的分析摘要、生成/剪辑花费、成片卡、标题/介绍/话题、投稿二维码或自动发布结果、失败与弃用原因。
- 推送：cron delivery 实现 `push`，走移动端（APNs + JPush，迁移自 bossip）。自动发布成功 → 「今天已发 N 条，点开看作品」；降级 → 「N 条待你扫码发布」；登录态失效 → 「请重登抖音创作者中心」。
- 回流：`douyin_publish(result)` 已能拿到 item_id；后续接开放平台数据接口把播放/点赞回流，作为下一次选题权重（M2）。

### 2.5 计费
- 已有：生成、合成、转写、图片四类落账；LLM token 计费。
- 新增：`hot_trends`（按次或包含在订阅里，成本是我们的抓取资源）、`video_analyze`（帧数 × 多模态 token 已在 LLM 计费里，但要在报告里合并展示为「分析费」）。
- 每次运行的花费在报告和账单页按运行聚合展示（账单页现在按事件列，需要「按 cron 运行聚合」的视图）。

---

## 3. 分期

**M0 · 三个 spike（约 1 周，不动产品面）**
1. 热点宝：把 douhot 加进站点目录，用一台云桌面真实登录态抓一天榜单，看频控、字段、稳定性（数据源先只做这一个，后续再加）。
2. `video_analyze` 原型：桌面 ffmpeg 抽帧 + `video_transcribe` + Gemini 3.7 Flash 出 JSON（含形态判定）；拿 10 条不同形态的真实热点测输出质量与单条成本。
3. 桌面自动发布：用一个测试抖音号，dev-browser 走一遍 creator.douyin.com 上传→填写→AI 声明→发布→回读作品 id；记录 DOM 稳定性、耗时、风控表现。
   输出：三份实测记录，决定分析成本模型与自动发布的节奏参数。

**M1 · 可用的自动营销（约 4 周）**
- `hot_trends` / `video_analyze` 平台工具（含计费与限流）；`marketing-autopilot` 技能（cron 模式无卡，预算授权，形态配方）；`douyin-desktop-publish` 技能（含风控降级）；cron 模版 UI 与对话创建（三档模型选择）；移动端推送（迁移 bossip 的 APNs+JPush，服务端发送移植到 Python，cron delivery `push`）；报告消息。
- 验收：一个真实客户账号连续 5 天每天自动产出并发布 2 条，预算不超、零人工介入；风控降级路径至少演练一次。

**M2 · 增强**
- 播放数据回流影响选题；TTS 原子工具；多平台（小红书站点目录已有 creator.xiaohongshu.com）；账单页按运行聚合；更多热点源；到一定用户量后把默认发布切回扫码合规路径。

---

## 4. 合规与风险（要在产品文案和模版确认里写清）

1. **不搬运**：热点视频只做分析，不下载入库、不复用原片画面与配音、文案不逐字复用；生成的是原创口播/画面。`video_analyze` 的输出是「创作要素」而非原片素材，这条边界写进工具和技能。
2. **AI 生成标识**：抖音要求 AI 生成内容标注；投稿包的标题/介绍里带标识或走平台的声明选项。
3. **广告法**：营销文案中的绝对化用语、功效承诺要有过滤（技能里加一条合规自审，口播技能已有雏形）。
4. **热点宝抓取**：用客户自己的登录态在客户自己的云桌面里抓，频率保守；被风控只影响该账号的采集，不影响生成与发布。
5. **自动发布**：前期默认开启，是**明知的过渡态**——用客户自己的登录态在客户自己的云桌面里发，节奏拟人、每日上限、AI 声明必勾；任何风控信号立即停用该账号的自动发布并降级为投稿包；模版确认里明示账号风险。切回合规路径的开关从第一天就有。
6. **预算**：无人值守花钱必须有硬上限；enforce 上线前 shadow 模式下先跑出真实单次成本分布。

---

## 5. 已拍板（2026-09-09）

| 问题 | 结论 |
|---|---|
| 发布方式 | **后台自动发布**，前期推广过渡态；有一定用户量后切合规扫码路径 |
| 模型档位 | 模版创建时客户选三档：**高 sd2 / 中 wan3 / 低 H3**；对话创建任务时由对话选择 |
| 热点源 | 先用**浏览器采集热点宝**快速验证，其他源慢慢加 |
| 内容形态 | **不止口播**，热点形态多样，靠模型能力复刻，不追求完美质量 |
| 推送 | **移动端**；迁移 bossip 已有的 APNs + 极光 JPush |

## 6. 仍开放的细节
1. 单次运行预算的默认值与上限（建议默认 2 条 × 中档 ≈ 20 积分/次，模版可改）。
2. 自动发布的节奏参数（每日条数、最小间隔、发布时段）——M0 spike 3 之后定。
3. openbox App 的包名与极光应用是否沿用 `com.bossip.bipmobile / BossIP-bip`，还是新建；决定推送迁移的工作量。
4. 非口播形态的配音：等 TTS 工具，还是先只用带音频的视频模型。

---

## 7. 实施计划（任务级）

> 估时按一人全职工作日（d）。依赖用 → 标注。每条任务都有可验证的完成定义，没有「基本可用」。
> 工作流可并行：A（采集）、B（分析）、C（自动发布）三条 spike 同时开；D（技能+模版）依赖 A/B/C 的结论；E（推送）独立，只被 F（联调）依赖。

### A · 热点采集（hot_trends）

| # | 任务 | 依赖 | 估时 | 完成定义 |
|---|---|---|---|---|
| A1 ✅ | 热点宝入站点目录：`platforms/desktop/sites.py` 加 `douyin_hot`。spike 证明它**不复用** `.douyin.com` 登录态，是独立 OAuth 会话（cookie 域 `.douhot.douyin.com`，~60 天）；探活 `GET /douhot/v1/user/user_info` `code==0`，昵称/uid/粉丝数字段现成 | — | 1d | 2026-09-09 落地；授权中心与 `desktop_login(status)` 列出 `抖音热点宝`，用户在云桌面扫一次码 |
| A2 ✅ | 采集脚本 spike：dev-browser 在云桌面 Chrome 里取榜单（类目、时间窗），抽字段：标题、链接、作者、播放/点赞/评论、话题、时长、封面 | A1 | 3d | 2026-09-10 落地于 M0 记录「A3 采集接口」：热点宝 `video_billboard/challenge_billboard/hot_search` 均为 POST JSON，签名只在 URL 上，页面内复用已签名 URL 换 body 即可查任意时间窗/榜单/垂类；垂类过滤形如 `tags:[{value:一级,children:[{value:二级}…]}]`；视频条目自带 `item_url` 直链。**24h 连续频控观测未做**（缓存把频次压到每键每天 1 次，先上线观察） |
| A3 ✅ | 平台工具 `hot_trends`（`tool/hot_trends.py`，服务 `trends/service.py`）：actions `list / sources / resolve`；`source=auto` 已绑定热点宝走热点宝否则走公开热榜；按 (source, board, window, category, 上海日期) 缓存于 `hot_trend_snapshots`，**全体客户共享**，cache 命中不计费、真采集按 `rates.json media.hot-trends` 落账 `kind=hot_trends`；`min_interval_seconds` 内不重采、每源每日 `max_fetches_per_day` 上限、失败快照可见且同样限流；只存元数据+链接，直链镜像到 `hot_media_links`（TTL 6h）；`resolve` 把公开榜视频页解析成 douyinvod 直链给 `video_analyze` | A2 | 3d | 2026-09-10 落地，12 项单测（同键两工作空间只驱动一次桌面、失败可见不重试、日配额、直链缓存）；真机四条采集脚本（美食视频榜/话题榜/公开热榜/直链解析）通过 |
| A4 ✅ | 热点源抽象：`trends/sources.py` 的 `HotSource(plan, parse, boards, windows, requires_site)` + `SOURCES` 注册表，`douhot` 与 `douyin_public` 两个实现；工具与服务不按源名分支 | A3 | 1d | 2026-09-10 随 A3 落地；新源 = 一个 `HotSource` 实例（页面 URL、等待的资源、页面内 JS、归一化函数） |

### B · 热点分析（video_analyze）

| # | 任务 | 依赖 | 估时 | 完成定义 |
|---|---|---|---|---|
| B1 | 原型：桌面 ffmpeg 场景抽帧（6–12 帧）+ `video_transcribe` + Gemini 3.7 Flash（菜单内、带 vision）一次调用出 JSON：形态判定、主题、人群、钩子、结构（分段秒数）、镜头描述、文案全文、话题、创作要素 | — | 3d | 10 条不同形态热点的输出经人工评分 ≥ 7/10；记单条成本（STT + 多模态 token） |
| B2 | 字幕型（无配音）热点：帧 OCR 走同一多模态调用，验证准确率 | B1 | 1d | 5 条字幕型样本文案还原 ≥ 90% |
| B3 ✅ | 平台工具 `video_analyze(source)`（`tool/video_analyze.py`）：source 为 owned asset_id / 工作区路径 / 直链媒体 URL；沙箱 ffmpeg 抽 N 帧+音轨 → OSS `analysis/<user>/<job>/` 中转 → fun-asr 转写 → 视觉模型出 JSON（`video/analysis.py` schema 校验）；**帧数 < min_frames 直接失败不退化**；结果按 (source, 帧数, 转写, 模型) 缓存于 `video_jobs kind=analyze`；转写按分钟落账、视觉调用按 token 计量（kind `video_analyze`），输出 `credits=` | B1,B2 | 4d | 2026-09-09 落地，7 项单测；跨客户共享缓存与网页 URL 解析留给 A3（hot_trends 负责把页面解析成直链） |
| B4 ✅ | 形态 → 配方映射表：`backend/.openbox/skills/marketing-autopilot/references/recipes.md`，七种 `form` 各给段数×时长、生成方式、声音/字幕、合成转场、质检口径（口播 STT ≥ tolerances.stt_similarity，其余只查时长与成功数），加选题过滤/预算/重生/发布/报告通用规则；字段与 `video_analyze` 输出、`autopilot/tiers.py` 三档对齐 | B1 | 1d | 2026-09-10 落地；配方随 D2 技能实跑再修 |

### C · 桌面自动发布（douyin-desktop-publish）

| # | 任务 | 依赖 | 估时 | 完成定义 |
|---|---|---|---|---|
| C1 ✅ | spike：测试抖音号，dev-browser 走 creator.douyin.com 上传→标题/介绍/话题→AI 生成声明→发布→回读作品 id；记 DOM 稳定性、耗时、失败形态 | — | 3d | 2026-09-10 完成：表单全部字段可稳定定位（见 M0 记录「C2 表单事实」）；1.7 MB 测试片表单 2 秒就绪、发布点击到内容管理 1.2 秒；未遇验证码。**连续 10 次成功率未测**（先以每账号每日 3 条的节奏上线观察） |
| C2 ✅ | 平台工具 `desktop_publish`（`tool/desktop_publish.py`，服务 `publish/desktop_service.py`，脚本 `publish/desktop_script.py`）+ 技能 `douyin-desktop-publish`：`precheck / publish / status / enable_auto`；成片由 OSS 复制到云电脑 `/workspace/uploads/`，Playwright 脚本在带登录态的桌面 Chrome 里填表并点发布；每次尝试一条 `publish_jobs(platform=douyin_creator)`，dry_run 记 `draft` 不计入预算；`details` 存可见范围/声明/上传耗时/截图路径 | C1 | 4d | 2026-09-10 落地，9 项单测；真机：dry_run 一次（暂存离开留草稿）+ 真实发布两次（仅自己可见，均成功，第二次回读到作品 id `7683584584923106586`，点发布到落地 1.0–1.2 秒）。作品 id 由内容管理页自身的 `work_list` 接口在页面内回放取得；`关联热点` 挂载仍未成功（best effort，返回 `hot_word_attached=false`） |
| C3 ✅ | `publish/desktop_policy.py`：每账号每日上限 / 最小间隔 / 发布时段（`DesktopPublishConfig`，模版只能收紧）；脚本任一阶段命中 `risk_patterns`（验证码、滑动验证、操作频繁、账号异常…）或登录跳转 → 该账号 `auto_publish_disabled_at` 熔断、写 `notifications(desktop_publish_degraded)`、工具返回 `degrade=true` 让技能改走 `douyin_publish`；登录失效写 `desktop_login_expired`；只有人能 `enable_auto` | C2 | 3d | 2026-09-10 落地；`simulate_risk=true` 注入假验证码遮罩演练降级（单测覆盖，真机演练见 M0 记录）。推送仍是站内通知，E 完成后接移动端 |
| C4 ✅ | `desktop_publish.default_mode = auto \| package`（`openbox.json`）是唯一全局默认；工具/模版参数 `mode` 可覆盖；账号熔断优先于两者 | C2 | 0.5d | 2026-09-10 落地，单测覆盖三层优先级 |

### D · 技能与 cron 模版（marketing-autopilot）

| # | 任务 | 依赖 | 估时 | 完成定义 |
|---|---|---|---|---|
| D1 ✅ | `autopilot/template.py` `AutopilotTemplate`（extra=forbid）：`account_profile / categories / hot_source / credits_cap_per_run / videos_per_run / model_tier / tolerances{duration_deviation_sec, stt_similarity, max_regenerations} / publish_mode / visibility / content_forms / topics_blocklist`；`cron_jobs.template` JSON 列（迁移 `d2f4a6c8e0b2`）；`CronJobCreate/Update.template` 经 `validate_template` 严格校验（含「预算至少够一条该档视频」）；执行器把模版以「模版参数（预算授权）」块注入 cron 提示词；`cron` 工具可透传 `template` | — | 2d | 2026-09-10 落地，7 项单测；前端/App 表单是 D5 |
| D2 ✅ | 技能 `marketing-autopilot`（`backend/.openbox/skills/marketing-autopilot/SKILL.md`）+ 工具 `autopilot_run`（`tool/autopilot_run.py`、`autopilot/ledger.py`）：cron 判定靠提示词里的「模版参数（预算授权）」块；钱与容差不靠 prose——`start` 由模版定模型/分辨率/可负担条数，**每个付费步骤前 `reserve`，超上限拒绝并终止后续付费**，`judge_shot` 按 tolerances 判 accept/regenerate/drop 并计数，`record kind=candidate` 做黑名单与形态过滤，`report` 从本会话 `usage_events` 汇总花费 | A3,B3,B4,C2,D1 | 5d | 2026-09-10 落地，6 项单测覆盖「预算超限停止」「STT 超阈值重生一次后弃用」「黑名单/形态过滤」「报告与账单一致」。**真实端到端一次运行（F）未做** |
| D3 ✅ | `autopilot/tiers.py` 三档映射与 `autopilot_run(action="tiers")` 每 15 s 参考价（读 `rates.json`）；技能「创建模版」流程用 `question` 出选档卡（三档 + 发布方式），预算建议按 `videos_per_run × (参考价 + 0.2) × 1.3`，`validate_template` 后 `cron(add, template=…)`；模版存 tier 不存模型 id | D1 | 1d | 2026-09-10 落地；真机对话创建一次待 F |
| D4 ✅ | `autopilot_run(action="report", items=[…])` 生成 markdown 报告：档位/预算/实际落账（读本会话 `usage_events` charged+shadow）、成片/已发布/待扫码/弃用计数、每条的热点来源、形态、拆解摘要、分段、文案、花费、发布结果或降级/弃用原因、花费按 kind 明细；技能把它作为最终回复，cron 注入到用户会话 | D2 | 2d | 2026-09-10 落地；单测断言总额 = 该会话账单行之和（unpriced 不计） |
| D5 | 模版 UI（web + App）：创建/编辑表单含 §2.3 字段；运行历史与报告入口 | D1 | 4d | 前端 `npm run check` 与 App `flutter test` 通过；真机创建一条模版 |
| D6 | 账单页「按运行聚合」视图（一次 cron 运行的 LLM+媒体花费合并） | D4 | 2d | 与 D4 报告数字一致 |

### F 首轮真机验收（2026-09-10 15:07–15:26，gw2 bbdwxh_admin）与修复

首轮跑通了「建模版 → 立即运行 → 热点宝选题 → 拆解 → 两段 Wan 生成 → 合成 → 创作者中心私密发布（作品 7683804546912636169）」，落账 15.15 积分（shadow）。
完整记录在 `work/f-acceptance-20260910/RESULTS.md`。暴露的问题与处置：

| 问题 | 处置（PR #20） |
|---|---|
| 中档固定 Wan 720p，但 gw2 的 `wan3.0-video` 只声明 1080p，`estimate` 报错后模型自行改 1080p 提交 | `autopilot/tiers.resolve_resolution` 按本部署模型声明把档位换算成可用分辨率（720p 不可用 → 就近取 1080p，`start` 输出 `resolution_note`）；**`video_generate` 提交时强制校验运行锁定的模型/分辨率**，不一致直接拒绝 |
| 付费步骤的 reserve 靠模型自觉，冷缓存热点采集没预留 | 改为**工具自动预留**：`video_generate` 提交、`video_compose` 提交、`video_analyze`、`hot_trends` 真采集都在花钱前把估价记到本会话运行的预算上，超限即拒（`BudgetStop`）；技能只对工具不覆盖的花费用 `reserve` |
| 人工停止运行被记为 ok，工具片段被当结果注入 | 执行器检查临时会话里 `finish=aborted`，视为运行失败并写明「被人工停止」，不再注入片段 |
| 建任务时区：用户说 9 点，存成 UTC 09:00 | `cron` 工具默认时区改 Asia/Shanghai；技能要求显式传 `timezone` |
| 未主动问人设/类目；形态「画面+旁白」不在模版却被改编成口播 | 技能：第一条回复必须问齐四件事；`form_allowed=false` 或「其他」必须跳过并在报告写明 |
| 简介话题重复、`declaration` 显示 None | `desktop_publish` 去掉简介里与 `topics` 重复的 `#词`；返回值带声明字段 |
| 报告费用与最终账单差一笔回复费 | 报告写明「统计到报告生成一刻」，技能要求报告是最后一条回复 |
| 建完任务未说明会收到报告 | 技能：最后一条回复必须包含下次运行时间、报告、预算到顶、风控降级、实际模型分辨率 |

仍未覆盖（下轮 F 补测）：对话「现在跑一次」三卡路径；黑名单实际命中；预算改 10 的首段拒绝；`simulate_risk` 降级与恢复；90 分钟间隔与每日 3 条拦截；`关联热点` 挂载仍失败；
Luna 过载属网关问题。**运营待办**：确认 gw2 把 `wan3.0-video` 收紧为仅 1080p 的依据（9 月 6 日前的备份即如此），否则中档实际按 1080p 计价（每 15 秒 18 积分而非 9）。

### E · App 极光推送集成（迁移 bossip）

事实：openbox 的 Flutter 应用就是 bossip 的同一个应用（`name: bossip_mobile`，Android/iOS 均为 `com.bossip.bipmobile`），所以**极光应用 `BossIP-bip`（AppKey 见 bossip `PUSH_NOTIFICATION_ENABLEMENT.md`）与 APNs Key 直接沿用，不用新建**；openbox 侧目前**零推送代码**（后端只有 `notification` 表，App 无原生桥）。bossip 已实现的可迁移物：Dart `SystemNotifications`（MethodChannel `bossip/system_notifications`）、`codex_notification_coordinator`、Android 原生（`BossIpPushBridge.kt` / `BossIpJPushReceiver.kt` / `BossIpJCommonService.kt`、Manifest、图标）、iOS `AppDelegate.swift` APNs 接入、服务端设备端点表与发送（v1 API TS：迁移 `0009–0013_*push*`）。

| # | 任务 | 依赖 | 估时 | 完成定义 |
|---|---|---|---|---|
| E1 | App 侧迁移：把 `SystemNotifications`、协调器、Android 原生桥与 Manifest/图标、iOS AppDelegate APNs 接入搬进 openbox `mobile/`；权限申请与设置页开关 | — | 3d | Android 真机拿到 JPush Registration ID；iOS 真机拿到 APNs token；`flutter test` + `dart analyze` 通过 |
| E2 | 后端设备端点：表 `push_endpoints`（user、platform、token/registration_id、environment、绑定的 mobile session、created/last_seen），登录时注册、退出时解绑；每设备单端点（对齐 bossip 0011 的教训） | — | 2d | 迁移 + API + 单测；重复注册不产生多端点 |
| E3 | 后端发送：`notifications/push.py`——iOS 用 APNs HTTP/2（token-based，Key 沿用），Android 用极光 REST；统一 payload（title/body/deeplink/kind）；失败重试与失效端点清理；密钥走 `secrets/`，不进镜像 | E2 | 3d | 对真机各推一条；无效 token 自动清理 |
| E4 | 接入 cron delivery：`delivery_mode=push`，模版默认开；三类文案（已发 N 条 / N 条待扫码 / 请重登） | E3,D4 | 1d | 一次真实运行结束后手机收到推送，点开进报告 |
| E5 | 复用到既有场景：视频生成完成、`polling_paused` 恢复、桌面登录态失效提醒（A5 已有通知条）也走推送 | E3 | 1d | 三个场景各真机一次 |
| E6 | 厂商离线通道（华为/小米/OPPO/vivo）开通与配置 | E1 | 2d + 审核等待 | 至少两个厂商通道在锁屏离线状态收到 |

### F · 联调、灰度、上线

| # | 任务 | 依赖 | 估时 | 完成定义 |
|---|---|---|---|---|
| F1 | shadow 模式下真实客户账号连续 5 天运行，采集单次成本分布、发布成功率、降级次数 | D2–D5,C3,E4 | 5d（观察） | 报告与数据齐全；预算无超限 |
| F2 | 运营定价：按 F1 成本分布给 `rates.json` media 段定售价与三档默认 | F1 | 0.5d | 价目提交并发布 |
| F3 | 内测放量：模版对内测用户开放；风控告警面板（哪些账号被降级） | F1 | 2d | 后台能看到每账号的自动发布状态 |
| F4 | 合规文案：模版确认页的风险说明、AI 生成声明、广告法用语过滤进技能 | D5 | 1d | 文案评审通过 |

### 顺序与并行

```
周 1      A1→A2 ‖ B1→B2 ‖ C1 ‖ E1‖E2          （三个 spike + 推送两端同时开）
周 2      A3 ‖ B3,B4 ‖ C2 ‖ D1,D3 ‖ E3
周 3      A4 ‖ C3,C4 ‖ D2 ‖ E4,E5 ‖ D5(起)
周 4      D2(收尾),D4 ‖ D5 ‖ D6 ‖ E6(提交厂商)
周 5–6    F1 观察 ‖ F3,F4 ‖ F2
```

人力：两人并行约 6 周；一人串行约 10 周。E（推送）单独一人可在 2 周内做完 E1–E5。

### 决策检查点
- 周 1 末：三份 spike 记录 → 定热点宝采集频率、分析成本模型、自动发布节奏参数（§6 第 1、2 项）。
- 周 3 末：干跑通过 → 决定 F1 用哪个客户账号、预算默认值。
- F1 结束：决定是否放量，以及切回扫码路径的触发条件（用户量或风控次数）。

### 已排除或推后
- 第三方热点 API、多平台（小红书）、播放数据回流、TTS 原子工具 → M2。
- 账单按运行聚合（D6）若挤不进 M1，可先在报告里给总额。
