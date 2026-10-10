# 运营案例：餐饮商家「进门 → 绑店 → 人设 → 创作 → 算账 → 建议」

> 2026-09-23 规划稿。目标：一个餐饮商家从注册到收到第一份经营周报，全程由产品承接，运营只在九数云授权一步陪同。
> 本文对照 origin/main（a8283a5）现状，给出六步流程、数据模型、报告与运营中心设计、分期与验收、M0 人肉案例 SOP。
> 相关：`A5_AUTHORIZATION_CENTER.md`、`A5_DESKTOP_LOGIN_STATE.md`、`JIUSHUYUN_INGEST.md`（PR #54）、`AUTO_MARKETING_AUTOPILOT_PLAN.md`、`MESSAGE_CENTER.md`、`MOBILE_ONBOARDING_PLAN.md`、`CRON_SYSTEM_PLAN.md`。

## 0. 拍板记录（2026-09-23）

| 项 | 决定 |
|---|---|
| 首批案例行业 | **餐饮**。九数云来客数据源只覆盖到店餐饮，美团团购表也齐；非餐饮商家账目只能走浏览器路线，本期不做 |
| 人设确认 | **一张汇总卡**。系统推断的五类记忆合在一张卡里让用户改完确认，不逐条弹 |
| 报告 | **日报 + 周报都要** |
| 触达 | **回发对话 + 运营中心汇总**。报告以消息回到创建任务的对话，并在新页面「运营中心」聚合展示 |
| 九数云授权（本文假设，未拍板） | 由运营**预约陪同**完成（identify JWT 只有 5 分钟，商家必须同时在线）；来客/美团登录态绑定仍是用户自助 |

## 1. 现状对照

| 环节 | origin/main 已有 | 缺口 |
|---|---|---|
| 进门 | 移动端引导 M1/M2（首启三屏、欢迎 sheet、行业示例卡 美业/餐饮/零售、蒙层），`user_preferences.extra.onboarding` | 行业只在 UI 里切换，不落库；Web 空白页三张卡仍是「分析代码结构」（E2 未做） |
| 绑店 | 授权中心：抖音开放平台 OAuth（只拿创作者身份）；云桌面登录态站点目录 `platforms/desktop/sites.py`（`douyin_creator`、`douyin_hot`、`douyin_laike` 读 `account_name/account_id/role`、`meituan_merchant` 读 `shop_id`），L1/L2 探活、keep-alive、失效通知 | 没有「门店」实体；店名、POI、类目、地址不持久化；绑定完成不触发任何后续动作 |
| 人设 | `memory/`：14 类稳定记忆 + 4 类易变记忆；`tool/creator_context.py`（get / write / propose / search）；`agent/loop.py` 末尾注入 `<user_memory>`；PENDING 不进提示词 | 没有任何流程往里写；没有「经营定位」字段 |
| 创作建议 | 回复后 0–3 条 next-step chips（`agent/suggestions.py`，800 字 `context_summary`）；技能 `video-production` / `imagegen` / `douyin-publish` / `marketing-autopilot` | 建议只读对话摘要，不读门店与人设 |
| 账目 | 九数云 D0/D1 手工跑通（首商家泽岚鲜果，12 表 71 行，异步导出可用，每日 5 次同步） | `platforms/jiushuyun/`、落地表、`store_ledger` 工具、`/api/store-data/summary`、授权中心数据卡全部未写 |
| 运营建议 | `cron` 工具 + `scheduled-tasks` 技能（项目级，≤10 个，≥5 分钟，结果回发对话，`NO_REPLY` 静默）；消息中心 `cron_completed` 带 `session` / `cron` 深链 | 没有读账目的工具；没有经营复盘技能；没有聚合报告的页面 |

## 2. 目标流程（六步）

每步写清：用户看到什么 / 系统做什么 / 复用什么 / 新增什么。

### 2.1 进门：登记「你的店」

- **用户看到**：欢迎 sheet 之后多一步「你的店」：行业（本期只开餐饮，其余显示「即将支持」）、店名、主平台（抖音来客 / 美团 / 都有）。Web 首次进入工作台弹同一表单。可跳过，跳过后运营中心顶部常驻「先登记门店」卡。
- **系统做**：`POST /api/stores` 写 `stores` 表（§3.1）。行业示例卡与空白页建议卡改读 `stores.category`。
- **复用**：移动端引导 store / welcome sheet / 行业卡组件；Web 空白页 `workspace.json` 的 `suggestions` 结构。
- **新增**：`stores` 表与迁移、`api/stores.py`、移动端「你的店」页、Web 首次表单、E2 的业务卡文案（餐饮 3 张，§2.4）。

### 2.2 绑店：登录态 + 回填门店

- **用户看到**：授权中心不变。来客 / 美团行探活成功后，运营中心的门店卡出现平台账号名与店铺 id，状态「已连接」。
- **系统做**：`probe_workspace` 里站点 L2 探活成功且 `probe_detail` 有 `account_id` / `shop_id` 时，回填 `stores.platform_bindings`（§3.1）。首次从 `pending` 变为 `bound` 时发事件 `store.bound`（§3.3），触发人设初始化任务（§4）并写消息中心通知，深链到该任务会话。
- **复用**：`platforms/desktop/service.py` 探活与 `display_paths`；`notifications/events.py::emit`。
- **新增**：回填逻辑（一个函数，挂在探活状态翻转处）；事件常量；通知模板 `store_bound`。
- **九数云数据授权（餐饮账目）**：按 `JIUSHUYUN_INGEST.md` §4 与 §3.5 的真实流程，由运营预约商家在线，运营点授权拿链接，商家 1 分钟内在自己的云桌面里登录来客确认，运营立即建连并登记（admin 接口）。授权中心两张数据卡显示「待运营开通 / 已同步 / 停更」。

### 2.3 自动完善人设

- **用户看到**：绑店后几分钟内，对话里出现一张「我对你的店的理解」汇总卡：五段可编辑文本 + 「确认」「稍后」。运营中心人设卡同步显示「待确认」。
- **系统做**：见 §4。
- **复用**：`creator_context` 工具、`memory/service.py` 的 CANDIDATE / ACTIVE 状态机、`question` 卡。
- **新增**：技能 `store-persona-init`、`creator_context` 新动作 `propose_bundle`、汇总卡样式（Web 与 App）。

### 2.4 创作建议

- **用户看到**：人设确认后，空白页三张卡按门店生成，例如餐饮：「给招牌菜『XX』做一条 30 秒到店短视频」「把这周 3 条差评写成回复并出一张致歉海报」「本周末团购活动写 3 版推广文案」。回复后的 next-step chips 也带门店语境。
- **系统做**：`GET /api/stores/{id}/starter-cards` 由后端按人设（OFFERING、SIGNATURE_CASE）+ 最近账目（热销品、差评）+ 日期（周末 / 节日）生成并缓存一天；`suggestions.py` 的提示词加入门店与人设摘要（≤200 字）。
- **复用**：`video-production`、`imagegen`、`douyin-publish` 技能；chips 机制。
- **新增**：starter-cards 接口与模板；chips 上下文扩展。

### 2.5 汇总与分析账目

- **用户看到**：运营中心数据卡：今日 / 近 7 天 / 近 30 天的核销数、GMV、应结算、实结算、退款、评价均分，分平台；对话里能直接问「昨天来客核销了多少」。
- **系统做**：实施 `JIUSHUYUN_INGEST.md` §5 全部内容，并按 §3.5 实测修正：异步导出「触发 + 轮询 + 取 URL + 下载」；每日 5 次拉取（九数云 6/11/16/20/23 点同步后 15 分钟）；CSV 中文列名映射；金额去千分位后按分入库；`connectId（系统字段）` 作商户键；停更告警改为超过 6 小时。
- **经营分析指标**（曝光 / 访问 / 转化 / 投放）继续走云桌面浏览器读来客与经营宝页面，作为 `store_ledger` 的 `metrics` 数据源，本期只在周报里按需抓一次，不做定时。
- **新增**：`platforms/jiushuyun/{client,parse,sync}.py`、`jsy_*` 落地表、`internal_tasks` 任务、`api/store_data.py`、工具 `store_ledger`、授权中心两张卡。

### 2.6 运营建议：日报 + 周报

- **用户看到**：每天 9:00 一条日报、每周一 9:00 一条周报，回到创建任务的对话；运营中心「报告」区按日期列出，点开看全文；差评待回、登录态失效等作为待办出现在运营中心顶部。
- **系统做**：见 §5。
- **复用**：`cron` 工具与执行器、`scheduled-tasks` 技能的创建话术、消息中心 `cron_completed` 深链。
- **新增**：技能 `store-ops-review`、工具 `store_report`、`store_reports` 表、cron 模板「经营日报 / 经营周报」（人设确认后自动创建，用户可在定时任务页停用）。

## 3. 数据模型

### 3.1 `stores`（新表，workspace 级）

| 列 | 说明 |
|---|---|
| `id` | `sto_` ULID |
| `workspace_id` | 边界，索引 |
| `name` | 店名，用户填或探活回填 |
| `category` | `food` / `beauty` / `retail` / `other`，本期只开 `food` |
| `address`, `city` | 九数云 `门店管理-查询门店信息` 回填，可空 |
| `platform_bindings` | JSON：`{"douyin_laike": {"account_id", "account_name", "poi_id", "status", "bound_at"}, "meituan_merchant": {"shop_id", "status", "bound_at"}}` |
| `data_sources` | JSON：`{"jsy_lk": {"connection_id", "status", "last_sync_at"}, "jsy_mt": {...}}`，由 `jsy_connection` 同步写入 |
| `persona_status` | `none` / `proposed` / `active`，由人设任务与确认动作维护 |
| `created_at`, `updated_at` | |

一个 workspace 本期只允许一家店（`UNIQUE(workspace_id)`），多店留到后续，列结构不用改。

### 3.2 `store_reports`（新表）

| 列 | 说明 |
|---|---|
| `id` | `rpt_` ULID |
| `workspace_id`, `store_id` | |
| `kind` | `daily` / `weekly` |
| `period_start`, `period_end` | 报告覆盖区间（东八区自然日 / 自然周） |
| `session_id`, `cron_job_id`, `run_id` | 来源，深链用 |
| `summary` | JSON，固定结构（§5.2），运营中心渲染卡片 |
| `markdown` | 全文 |
| `created_at` | |

`UNIQUE(store_id, kind, period_start)`：同一天重跑覆盖，不重复。

### 3.3 事件与通知

| 事件 | 触发点 | 消费者 |
|---|---|---|
| `store.bound` | `stores.platform_bindings` 任一平台首次 `bound` | 人设初始化任务；通知 `store_bound`（link `session`） |
| `store.persona_active` | 汇总卡确认 | 自动创建两个 cron 模板；刷新 starter cards；通知 `persona_ready` |
| `store.ledger_connected` | `jsy_connection` 首次同步成功 | 通知 `ledger_ready`；日报从次日起有数据段 |
| `store.report_ready` | `store_report(save)` | 通知 `report_ready`（link `session`）；运营中心刷新 |

事件走现有 `bus`，通知走 `notifications/events.py::emit`，`source_key` 用 `{event}:{store_id}:{period}` 去重。

### 3.4 旅程状态（派生，不存）

`GET /api/ops-center/overview` 按数据现算：`store_registered` → `store_bound` → `persona_active` → `ledger_connected` → `first_report`。空白页与运营中心顶部待办按当前阶段给下一步，不新增状态表。

## 4. 人设初始化任务

### 4.1 触发与运行方式

- 触发：`store.bound`。同一店 24 小时内只跑一次；用户主动在运营中心点「重新生成人设」可再跑。
- 形态：复用 cron 执行器的「临时会话」能力，起一个一次性任务（`schedule=once`，`cron_tool` 需加 `once` 语义或由后端直接 `executor.run_once`），会话 kind 记为 `system`，回发到用户最近一个对话；没有对话就新建一个「你的店」对话。
- 约束：运行期不出任何 `question` 卡；所有结论写成 CANDIDATE 记忆；只在结束时出一张汇总卡。

### 4.2 输入

| 来源 | 内容 | 方式 |
|---|---|---|
| `stores` | 店名、行业、平台账号 | 直接读 |
| 来客后台（云桌面浏览器） | 店铺简介、门店地址、团购商品列表（名称 / 价格 / 销量）、近 30 天评价前 50 条 | `desktop_login` 确认登录态 → dev-browser 读页面。只读，不点任何提交按钮 |
| 美团经营宝（如已绑） | 团购项目、评分、近 30 天点评 | 同上 |
| 九数云落地表（如已接） | `jsy_lk_shop`、热销 SKU、评价 | `store_ledger` |
| 用户已有记忆 | 已 ACTIVE 的记忆不覆盖，只补空类型 | `creator_context.get_user_context` |

### 4.3 输出：五类记忆，一张汇总卡

| 类型 | 内容 | 例 |
|---|---|---|
| `IDENTITY` | 店是谁：店名、品类、城市商圈、人均 | 「南宁·泽岚鲜果，社区鲜果切与果汁，人均 25」 |
| `OFFERING` | 卖什么、主推什么 | 「主推 19.9 双人果切与 9.9 鲜榨券，团购占比高」 |
| `AUDIENCE_PROFILE` | 谁在买、什么时段 | 「附近上班族与宝妈，午后与晚间为主」 |
| `SIGNATURE_CASE` | 评价里被反复夸的点 | 「果切新鲜、分量足、老板娘热情」 |
| `VOICE` | 建议的表达风格 | 「亲切、直接、带本地口语，不用网络梗」 |

写入：`creator_context.propose_bundle(items=[{type, summary, evidence}])`（新动作）。它把五条各写一行 CANDIDATE（`owner=SYSTEM_INFERRED`，`confidence` 由证据条数给），并发一张 `question` 汇总卡：五段可编辑文本，按钮「确认」「稍后」。确认 → 五条转 ACTIVE、`stores.persona_status=active`、发 `store.persona_active`；稍后 → 保持 CANDIDATE，运营中心人设卡显示「待确认」并可再次打开。

不变量：PENDING_NOTE 不进提示词（`memory/context.py` 现有）；本任务不写 `USER_NOTE`；已 ACTIVE 的同类型记忆不被系统推断覆盖。

## 5. 报告

### 5.1 节奏与数据边界

| 报告 | 时间 | 覆盖 | 数据前提 |
|---|---|---|---|
| 日报 | 每天 09:00（用户时区） | 前一自然日 | 九数云 23:00 同步 + openbox 23:15 拉取已完成，前一日数据完整 |
| 周报 | 每周一 09:00 | 上周一至周日 | 同上；另在运行时用浏览器读一次来客「经营分析」近 7 天曝光 / 访问 / 转化 |

账目未接通时，日报只报「登录态 / 发布 / 评价」三段并静默（`NO_REPLY`），不输出空数字；周报照常出，标注「交易数据待开通」。

### 5.2 结构（`store_reports.summary`）

```json
{
  "period": {"start": "2026-09-22", "end": "2026-09-22"},
  "ledger": {"verified": 38, "gmv_cents": 152000, "settle_cents": 139800, "refund_cents": 3800,
             "refund_count": 2, "review_avg": 4.7, "new_reviews": 6, "by_platform": {"douyin_laike": {...}, "meituan": {...}}},
  "delta": {"verified_pct": 0.12, "gmv_pct": 0.08, "vs": "prev_period"},
  "top_skus": [{"name": "19.9 双人果切", "count": 21}],
  "reviews": {"negative": [{"score": 2, "text": "…", "reply_suggestion": "…"}]},
  "publish": {"posted": 1, "pending_scan": 0, "login_expired": false},
  "actions": [{"kind": "reply_review", "title": "回复 2 条差评", "prompt": "…"},
              {"kind": "content", "title": "本周末做一条果切拼盘短视频", "prompt": "…"}],
  "topics": ["…", "…", "…"]
}
```

日报 `actions` ≤2 条，周报 ≤5 条并附 `topics` 3 条内容选题与 `metrics`（周报独有）。每个 action 带可直接发起对话的 `prompt`，运营中心一键执行。

### 5.3 技能 `store-ops-review`

输入：`store_ledger(summary, from, to, group_by=day|sku|platform)`、`store_ledger(reviews, from, to)`、`creator_context.get_user_context`、`douyin_publish(list)`、周报另加浏览器读经营分析。
规则：数字全部来自工具返回，不许估算；无数据段写「暂无」；差评回复建议遵循 `VOICE` 与 `BOUNDARY`；结尾调用 `store_report(save, kind, period, summary, markdown)`；日报若「无核销、无退款、无新评价、无登录态变化」则只保存报告并回复 `NO_REPLY`。

### 5.4 cron 模板

`store.persona_active` 时由后端直接创建两个 cron job（不经对话）：名称「经营日报」「经营周报」，`task` 为固定提示词（含 store_id 与时区），`delivery` 回发到「你的店」对话。用户可在定时任务页停用；运营中心报告区有「重新生成」按钮触发一次即时运行。

### 5.5 回发与推送

- 回发对话：现有 cron 注入机制，报告消息带 `store_report` 卡片（渲染 §5.2 前三段 + actions 按钮）。
- 消息中心：`report_ready` 通知，`link.kind=session`；App 推送沿用 `cron_completed` 模板。
- 运营中心：读 `store_reports`，与对话里的内容同源。

## 6. 运营中心

### 6.1 页面

Web 路由 `/app/ops`，侧栏「运营中心」放在「资源」之后；App `Paths.opsCenter`，侧栏同位。一屏四区：

| 区 | 内容 | 数据 |
|---|---|---|
| 顶部待办 | 先登记门店 / 去绑定来客 / 确认人设 / 数据待运营开通 / 登录态失效 / 2 条差评待回 | overview 派生 |
| 门店与连接 | 店名、行业、来客 / 美团账号名与状态、九数云两条连接状态与上次同步 | `stores` + `platform_accounts` + `jsy_connection` |
| 经营数据 | 今日 / 7 天 / 30 天 六个指标，分平台切换，7 天核销与 GMV 折线 | `/api/store-data/summary` |
| 报告 | 最近日报（摘要 + actions 按钮）、最近周报、历史列表 | `store_reports` |

人设卡放在门店区右侧：状态、五类各一行、「编辑」跳记忆页、「重新生成」。

### 6.2 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/PATCH | `/api/stores` | 当前 workspace 的店（本期一家） |
| GET | `/api/stores/{id}/starter-cards` | 三张建议卡，缓存一天 |
| GET | `/api/ops-center/overview` | 待办 + 门店与连接 + 人设状态 + 指标三窗口 + 最近两份报告 |
| GET | `/api/store-reports?kind=&cursor=` | 报告列表 |
| GET | `/api/store-reports/{id}` | 全文 |
| POST | `/api/store-reports/run?kind=` | 立即生成一次（调用 cron run-once） |
| POST | `/api/stores/{id}/persona/regenerate` | 重跑人设任务 |
| GET/POST/… | `/api/store-data/*` | 按 `JIUSHUYUN_INGEST.md` §5.7 |

工具：`store_ledger`（只读，§2.5）、`store_report`（`save` / `latest`）、`creator_context.propose_bundle`。

## 7. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| **M0 人肉案例（1 周）** | 不写代码。运营陪 1 家餐饮商家走完六步：登记、绑来客 / 美团、九数云授权、用对话手动生成人设并 `propose_memory` 确认、用现有技能出 1 条视频 + 1 组差评回复、用 `scheduled-tasks` 建一个「每天 9 点总结昨天评价与发布」任务 | 产出一篇可对外讲的案例文案；每步耗时与卡点记入本文 §9 |
| **M1 门店与人设（2 周）** | §2.1、§2.2、§4、§2.4 | 新用户登记餐饮店 → 绑来客 → 10 分钟内收到汇总卡 → 确认后空白页三张卡是这家店的；`<user_memory>` 里出现五类记忆 |
| **M2 账目（2 周，与 M1 并行）** | §2.5 全部 | 首商家授权后 2 小时内库里有当天来客订单 / 账单 / 评价 / 退款与美团结算 / 验券 / 点评；对话里问「昨天核销多少」答案与来客后台一致；授权中心两张卡状态正确 |
| **M3 报告与运营中心（1.5 周）** | §5、§6 | 人设确认次日 9:00 收到日报，下周一收到周报；运营中心四区数据与对话同源；差评待回能一键发起对话 |

关键路径 M2 → M3。M1 与 M2 并行，两人各一条线约 4 周到 M3。

## 8. 风险与约束

| 风险 | 处理 |
|---|---|
| 九数云授权 5 分钟 JWT，商家必须同时在线 | 正式流程即「运营预约陪同」；授权中心数据卡文案写明「由运营协助开通」 |
| 美团团购授权独占，会踢掉门店已绑收银 / SaaS | 接入前逐店确认；冲突店只接来客 |
| 来客后台读页面（人设输入、周报经营分析）有频控与改版风险 | 只读、每店每天最多 2 次；DOM 变化时该段写「暂无」不失败整个任务 |
| 系统推断人设可能错 | 全部 CANDIDATE + 一张汇总卡；未确认不进提示词；`BOUNDARY` 永远只由用户写 |
| 日报刷屏 | 无变化 `NO_REPLY`；运营中心仍保存报告 |
| 数据新鲜度上限约 4 小时（九数云每日 5 次） | 报告只报「前一自然日 / 上周」，不报「今天到现在」；运营中心今日数据标注更新时间 |
| 一 workspace 多店 | 本期 `UNIQUE(workspace_id)`，表结构已按多店设计 |

## 9. M0 人肉案例 SOP（餐饮）

| 步 | 动作 | 负责 | 预计 |
|---|---|---|---|
| 1 | 商家注册、App 走完引导、在对话里说店名与主平台；运营手动记录到案例表 | 商家 + 运营 | 10 分钟 |
| 2 | 授权中心：商家在云桌面登录来客与美团经营宝，探活变绿 | 商家 | 10 分钟 |
| 3 | 九数云授权（按 `JIUSHUYUN_INGEST.md` §3.5）：运营点授权拿链接 → 商家 1 分钟内在云桌面登录确认 → 运营立即选表建连并同步 | 运营 + 商家同时在线 | 15 分钟 |
| 4 | 对话里让 agent 读来客店铺页与评价，按 §4.3 五类给出人设，用 `propose_memory` 逐条确认（M0 无汇总卡） | 商家 | 20 分钟 |
| 5 | 用建议卡出 1 条 30 秒招牌菜视频与 3 条差评回复；扫码发布 | 商家 | 30 分钟 |
| 6 | 运营从九数云导出前一日 CSV，贴给 agent 出一份日报样稿；用 `scheduled-tasks` 建「每天 9 点总结昨天评价与发布」 | 运营 | 20 分钟 |
| 7 | 一周后回收：报告是否被看、建议是否执行、数字是否与后台一致；写入案例文案 | 运营 | — |

记录字段：每步实际耗时、卡点、商家原话。这些直接决定 M1–M3 的文案与默认值。

## 10. 不动的东西

- `platforms/douyin/**` OAuth 链路、视频技能、发布路线偏好不改。
- 记忆模块的状态机与「PENDING 不进提示词」不变，只加一个 `propose_bundle` 动作。
- cron 执行器不改语义，只加 run-once 入口。
- 消息中心 `link` 白名单不扩：报告一律深链到会话。
