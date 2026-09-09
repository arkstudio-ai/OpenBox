# A5 二期 · 云电脑浏览器登录态纳入授权中心 —— 执行单

> 2026-09-08。自包含执行单。承接 `docs/A5_AUTHORIZATION_CENTER.md`（开放平台 OAuth 路线已上线）与 `docs/DETAILED_PLAN_M1_M2.md` §A5 v0（桌面 cookie 探活的原始设计）、`DETAILED_PLAN_M1_M2_REVIEW.md` §2.4（CDP 坑）、`docs/D1_ECD_BROWSER_DEBUG.md`（桌面浏览器诊断底座）。
> 状态：**计划，未开工**。
>
> **执行者须知**：新增 `backend/platforms/desktop/**`（站点目录 + CDP cookie 助手 + 探活）、`backend/tool/desktop_login.py`、`container/dev-browser/SKILL.md` 一段前置检查；在 `platform_accounts` 上加列不建新表；前端只在 `features/auth-center` 里加一组卡。不动 `sandbox/browser.py` 的 Chrome 启动参数（profile 目录是现成的资产，别碰）。探活永远不整页导航到抖音页面。

---

## 1. 目标

**一句话**：用户在自己的云电脑 Chrome 里登录过的站点（抖音创作者中心、抖音来客、美团开店宝、小红书创作平台……），登录态本来就一直留在那台机器的 profile 里直到过期——现在把它**登记、探活、展示、告警、可引导**：授权中心里能看到"哪些站已登录、昵称、上次检测、哪些快过期"，一键把登录页推到云电脑前台让用户扫码，模型动手前能先问一句"这个站登录着吗"，而不是像现在这样每次自己截屏去看。

**完成定义**：
1. 授权中心新增"云电脑登录态"卡组：每个站点一行，状态 `已登录 / 未登录 / 已过期 / 桌面离线 / 未知`，昵称、上次检测；按钮：去登录（把登录页推到云电脑前台并切到桌面画面）、检测、退出登录。
2. 探活分三层（§3.8）：S0 被动观察模型/用户的真实访问、S1 只读 cookie 零导航、S2 每日一次轻量 JSON；保活以自然使用 + 天级轻触为主（§3.9），预测到期并提前提醒。只对 `assigned` 桌面跑，拿 `desktop_lease` 短租，桌面忙就跳过。
3. 过期 / 退出 / 桌面被回收时写通知（`desktop_login_expired`），并在卡上标红。
4. 模型有工具 `desktop_login(action=status|open|probe, site=…)`，dev-browser 技能开头写明"要登录态的站先查 status，未登录就 open 并让用户扫，不要自己截屏猜"。
5. 单测覆盖 cookie 判定、站点目录、状态机、通知；真机验收在用户自己的桌面 `ecd-glxi1nk433hliivri` 上做。

---

## 2. 现状（main `6b5e91c`）

| 事实 | 位置 |
|---|---|
| 云电脑上**只有一个带头 Chrome**，`--user-data-dir=$HOME/.config/obx-chrome`，CDP `127.0.0.1:9333`；用户在无影画面里看到的窗口、`computer.open_browser`、dev-browser 的 Playwright 全都是它。**人扫码写进的 cookie 就是自动化读到的 cookie**——原方案最担心的前提已天然成立 | `sandbox/browser.py:36-50,495-700` |
| Chrome 每次带头启动会删 `Default/Sessions`、`Current/Last Session` 并改 `exit_type`；cookie/登录在别的文件里，会保留（**本项依赖此不变量，写进验收**） | `browser.py:556-583` |
| 无头变体用另一套 profile（`/var/lib/openbox/browser/...`，用户 `obx-browser`），登录态与人不互通；`is_headless()` 可区分 | `browser.py:736,773` |
| 后端**没有 CDP 客户端**；唯一的 CDP 往返是一段内联 Python（`websockets.sync`）经动作服务器 `client.execute` 跑在桌面上：读 `/json/version`、`/json/list`、`PUT /json/new`、`Runtime.evaluate`。**这就是 cookie 探活的模板** | `browser.py:186-232` |
| 桌面诊断底座（D1）：`GET /diag/browser` 不要租约；`obx_diag.py` 已报 profile 路径、`/json/list` 标签页 URL；`sandbox/diag.py` 双通道（隧道 / 云助手）；`desktop_events` + `events.span()`；`KINDS` 里加一个 `platform.probe` 即可 | `container/action_server.py:331`、`sandbox/obx_diag.py:216,280`、`sandbox/events.py:34,84,146` |
| 从 cron 拿任意桌面的客户端：`channel.route_for_record(row)` → `SandboxClient`；租约 `desktop_lease()` 30–600s，忙则 423 | `sandbox/channel.py:140`、`sandbox/client.py:338` |
| 桌面归 workspace（一活跃桌面一 workspace），所以**登录态是 workspace 级**，不是用户级 | `db/models/cloud_desktop.py:76-83` |
| `platform_accounts` 已预留 `auth_kind='desktop_cookie'`，有 `status/last_probe_at/last_ok_at/last_error`，`POST /{id}/probe` 与 `service.probe()` 形状正好；**缺 `desktop_id` 列**，活跃唯一索引是 `(workspace_id, platform, external_id)` | `db/models/platform_account.py:27`、`api/platform_accounts.py:150`、`platforms/service.py:361` |
| 通知表与接口已存在（`platform_auth_expired` 已在用）；内部任务原语 `register(name, interval, fn)`；授权中心页是卡片网格 | `db/models/notification.py`、`platforms/tasks.py:12`、`features/auth-center/**` |
| **没有任何"登录态 / cookie"代码**：`grep 登录态|cookie|logged_in` 在 backend/container/frontend 均为零；dev-browser 技能只说"要登录的站先问用户"，没有 per-site 检查 | `container/dev-browser/SKILL.md:36-40` |
| 打开一个 URL 到桌面前台**没有后端 API**：导航只发生在 agent 写的 Playwright 脚本里 | `tool/computer.py:452`（open_browser 无 URL） |
| `extension` 模式下 agent 用的是用户本机 Chrome，云电脑 profile 不参与——探活无意义，要明确规则 | `tool/browser_mode.py`、`session/browser_pref.py` |
| 技能 frontmatter 只解析 `allowed-tools`，`requires-platforms` 无实现 | `skill/skill.py:114-125` |
| 原方案已定的规则：探活 = cookie 存在性 + 轻量接口，**不整页导航**（抖音对自动化导航敏感，探活本身可能把登录探没）；按域清 cookie 用 `Network.deleteCookies` 逐条或 `Storage.clearDataForOrigin`，**不能用** `Network.clearBrowserCookies` | `DETAILED_PLAN_M1_M2_REVIEW.md:141-149,235` |

---

## 3. 方案

### 3.1 站点目录（`backend/platforms/desktop/sites.py`）

每个站点一条静态记录，**值要在真机上侦察后填**（§5 P0）：

```python
DesktopSite(
  key="douyin_creator", display="抖音创作者中心", group="douyin",
  login_url="https://creator.douyin.com/", home_url="https://creator.douyin.com/creator-micro/home",
  cookie_domains=[".douyin.com"],
  session_cookies=["sessionid", "sid_tt", "uid_tt", "passport_auth_status"],   # 侦察确认（§3.10）
  light_probe=LightProbe(url="https://creator.douyin.com/aweme/v1/creator/user_message/unread_count/", ok_when="status_code==0", expired_when="status_code==8"),
  profile_probe=LightProbe(url="https://creator.douyin.com/aweme/v1/creator/user/info/", nickname_path="douyin_user_verify_info.nick_name", uid_path="douyin_user_verify_info.douyin_unique_id"),
  logout=LogoutRule(domains=[".douyin.com"]),
  sensitive=True,            # 抖音系：L2 每天最多一次，失败不重试
)
```
首批：`douyin_creator`、`douyin_laike`（life.douyin.com：S2 `/life/gate/v1/user/login_info/`，昵称 `data.name`，店名 `/life/gate/v1/account/detail` → `account_name`）、`meituan_merchant`（美团经营宝 e.dianping.com：S2 `/merchant/portal/common/cityshop`，未登录 `error.code==10008`；用户口中的"开店宝"实为此站）、`xiaohongshu_creator`（creator.xiaohongshu.com，待侦察）。后续加 `weixin_channels`（视频号助手）、`kuaishou_creator`。
目录通过 `GET /api/platforms` 一并返回（`kind: "desktop"`），前端按 `group` 归卡。

### 3.2 数据（`platform_accounts` 加列，不建新表）

```
ALTER platform_accounts ADD desktop_id String(96) NULL       -- 登录态所在桌面
ALTER platform_accounts ADD probe_detail JSON NULL           -- 最近一次探活：cookies 命中/过期时间/L2 结果
status 取值扩为 bound | expired | revoked | unknown | desktop_offline
新增活跃唯一索引 uq_platform_accounts_desktop (workspace_id, desktop_id, platform) WHERE auth_kind='desktop_cookie' AND deleted_at IS NULL
```
- `auth_kind='desktop_cookie'` 的行：`external_id` 初始 = `"desk:" + desktop_id`，L2 取到昵称/uid 后再回填；token 两列恒空。
- 桌面被回收 / 重建（`cloud_desktops.desktop_id` 变化）：该 workspace 所有 desktop_cookie 行置 `unknown` 并通知"云电脑已更换，需重新登录"。
- 同一 workspace 多成员共用一台桌面：行归 workspace，卡上显示"该云电脑上的登录"，不写成某个人的。

### 3.3 CDP cookie 助手（`backend/platforms/desktop/cdp.py`）

照 `browser.py:186-232` 的内联 Python 模式再写一段脚本（base64 → `python3` on desktop），一次往返完成：
1. `/json/version`、`/json/list`；找一个已有标签页（优先该站点域名的，其次 `about:blank`），没有就 `PUT /json/new?about:blank` 建一个用完关掉。
2. `Network.enable` → `Network.getCookies {urls: [home_url]}` → 按 `session_cookies` 判：全部存在且 `expires` > now+1h → `cookie_ok`；缺或过期 → `cookie_missing`。
3. 仅当 `cookie_ok` 且需要 L2（每站每天一次；`sensitive` 站失败不重试）：`Target.createTarget {url: light_probe.url, background: true}` → 等 `Page.loadEventFired`（≤5s）→ `Runtime.evaluate document.body.innerText` → `Target.closeTarget`；解析 JSON 取昵称与状态码。**永远不导航到站点首页或创作页。**
4. 输出 JSON：`{site, cookie_ok, cookies:[{name, expires}], probed, nickname, uid, http_ok, error}`。
另两个动作复用同一脚本：`open`（`PUT /json/new?<login_url>` + `/json/activate/{id}`，把窗口提到前台）、`logout`（对 `logout.domains` 的每个 cookie `Network.deleteCookies {name, domain}`）。
所有动作在 `desktop_lease` 里跑（`open`/`logout` 必须；`probe` 拿不到租约就返回 `busy`，任务下一轮再来）。`extension` 模式或 `is_headless()` 为真：直接返回 `not_applicable`。

### 3.4 服务与接口（`backend/platforms/desktop/service.py`、`api/platform_accounts.py` 加路由）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/platforms` | 现有；desktop 站点带 `kind:"desktop"`、`group`、`display`、`loginUrl` |
| GET | `/api/platform-accounts` | 现有；desktop_cookie 行多返回 `desktopId`、`desktopOnline`、`probeDetail`（脱敏：只给 cookie 名和到期时间，不给值） |
| POST | `/api/platform-accounts/desktop/{site}/open` | 成员；`ensure_browser` → `open` 登录页到前台 → upsert 行为 `unknown`（无行时）→ 返回 `{accountId, desktopId}`；前端随即切到云桌面画面并每 5s 轮询 probe，3 分钟停 |
| POST | `/api/platform-accounts/{id}/probe` | 现有路由，按 `auth_kind` 分流到桌面探活；结果写 `status/last_probe_at/last_ok_at/probe_detail/nickname` |
| POST | `/api/platform-accounts/{id}/logout` | owner/admin；按域删 cookie → `revoked`；审计 |
| DELETE | `/api/platform-accounts/{id}` | 现有；desktop 行只删登记不动 cookie |

状态机：`unknown --probe ok--> bound`；`bound --cookie 缺/过期 或 L2 401--> expired`（写通知）；`bound/expired --logout--> revoked`；桌面隧道不通 → `desktop_offline`（不改 last_ok_at，不通知，连续 3 天再通知）。

### 3.5 定时探活（`platforms/desktop/tasks.py`）

`internal_tasks.register("desktop_login_probe", 6*3600, run)`：只取 `cloud_desktops.pool_state='assigned'` 且 `tunnel_state` 正常的桌面，每台一次租约、逐站 L1；L2 按站点每日一次且只在 06:00–08:00 那一轮跑（人最少在用的时段，且失败不重试）。每台桌面记一条 `desktop_events kind=platform.probe`（`events.span`），不存截图、不存 cookie 值。

### 3.6 前端（`features/auth-center`）

- 卡组标题"云电脑登录态"，副标题显示桌面 id 与在线状态；`extension` 模式或没有桌面时显示"当前用本机浏览器 / 未开通云电脑，无法监控"。
- 每站一行：图标、名称、状态徽标、昵称、上次检测、cookie 最早到期时间；按钮"去登录 / 检测 / 退出登录"。"去登录"点击后调 `open`，然后把工作台切到云桌面视图（复用现有桌面画面入口），提示"在云电脑里扫码登录，登录后这里会自动变绿"，轮询期间显示倒计时。
- 通知红点复用 `GET /api/notifications`。
- Fleet 诊断抽屉加一个"登录态"分节（只读），运营排障用。

### 3.7 模型侧

- 工具 `desktop_login`（`backend/tool/desktop_login.py`，build agent 独占，身份只来自 ToolContext）：`status`（本 workspace 全部站点或指定站点的状态，附"要不要先让用户登录"的判断）、`open`（把登录页推到前台并告诉用户去扫）、`probe`（用户说扫完了之后确认）。返回结构化 `DESKTOP_LOGIN_REQUIRED {site}`。
- `container/dev-browser/SKILL.md` 开头加一段：需要登录态的站点先 `desktop_login(status)`；未登录 → `open` → 等用户 → `probe`；**不要自己导航到登录页截屏猜**，不要代填验证码。`douyin-publish` 技能不受影响（走开放平台）。
- 硬阻断（`requires-platforms` frontmatter → 浏览器类工具前置检查）放到 P3，需要改 `skill/skill.py` 与 `ToolContext`，本单只做软引导。

### 3.8 探活怎么做：三层信号，越被动越优先

登录态失效有两种：**cookie 本身到期**（能从 `expires` 看出来）和**服务端把会话作废**（cookie 还在、请求却回登录页，从本地看不出来）。抖音系大多是后者，所以只看 cookie 不够，但主动请求又有风险。分三层，能用被动信号就不发主动请求：

| 层 | 信号 | 代价 | 什么时候用 |
|---|---|---|---|
| **S0 被动观察**（免费、零风险，最重要） | 模型或用户真的在用这个站时，dev-browser 的 Playwright 客户端已经看到了每个响应：遇到 302 到 `passport/login`、`/login` 页、401/403、页面里出现"登录/扫码"的登录组件 → 在桌面写一行 `/tmp/obx-browser-ops.jsonl`（D1 预留的 L3 页面级日志，现在没人写），`obx_diag` 已在 tail 它 → 后端立刻把该站置 `expired`。同理，一次正常的已登录访问 → 记 `last_activity_at`，等于免费探活 | 0 | 一直开着 |
| **S1 cookie 快照**（本地、零导航） | `Network.getCookies` 读该站 `session_cookies`：缺 / `expires` 已过 → `expired`；都在 → `cookie_ok`。同时记下最早到期时间与 cookie 集合哈希（会话轮换了也知道） | 一次 CDP 往返，桌面上无任何可见动作 | 每 6 小时 |
| **S2 轻量认证请求** | 用站点自己的前端在登录后就会调的 JSON 接口（侦察时从 Network 面板抄，不用猜）。优先在**已存在的该站标签页**里 `Runtime.evaluate(fetch(url,{credentials:'include'}))`（同源、零导航）；没有该站标签页才 `Target.createTarget(url, background:true)` 打 JSON 地址，5 秒内关掉。回 200 且有用户字段 → `bound` + 昵称；回登录跳转/401 → `expired` | 一次真实请求 | 每天一次，且只在 S1 为 `cookie_ok`、S0 近 24h 没有真实访问时才做（有真实访问就不必再问）；敏感站失败不重试 |

判定优先级：S0 > S2 > S1。S1 说 ok 而 S2 说过期，以 S2 为准；S0 一小时内刚成功访问过，S1/S2 都跳过。

时机：每台桌面加随机抖动（±30 分钟），不在整点；S2 只在桌面空闲（X 空闲 > 10 分钟，`xprintidle`）时做，既不打扰人，也避免和用户操作并发。

### 3.9 保活怎么做：先弄清每个站的过期机制，再选手段

"一直有登录态直到过期"里的"过期"由站点决定，我们能做的是**延后它、预测它、失效时最快让人重扫**。手段按风险排序，默认只开前两条：

| 手段 | 做法 | 风险 | 默认 |
|---|---|---|---|
| **K0 自然保活** | 用户/模型正常使用就是最好的保活，S0 记 `last_activity_at` | 无 | 开 |
| **K1 定时轻触** | 站点会话通常是"多少天不活动即失效"。对每站维护 `inactivity_ttl`（侦察阶段先给保守估值，之后用探活数据校准：记录每次 expired 与上一次活动的间隔），在 `last_activity_at + ttl − 2 天` 这个点做一次 S2 同款请求（同源 fetch，带随机时间），把服务端的活跃时间往后推。**cookie 自身到期（`expires`）无法靠请求延长**，那种只能提前提醒 | 低：请求就是站点前端自己发的那种；频率是"几天一次"不是"每小时" | 开 |
| **K2 常驻后台标签页** | 在 Chrome 里给该站留一个固定标签页（如创作者中心首页），让站点自己的脚本续期 token。我们控制 `browser.py`，可在 `ensure_browser` 后自动补开；标签页放在独立窗口并最小化 | 中：用户看得见、可能顺手关掉；Chrome 重启清会话文件后要重建；后台标签页会被 Chrome 节流，续期是否真的发生要验证 | 关，仅当 K1 对某站证实无效时对该站单独开 |
| **K3 导出/注入 cookie** | 把 cookie 备份，过期后注入 | 不能延长服务端会话，安全与合规都过不去 | **禁止** |

**预测与提醒**：每站算 `predicted_expiry = min(最早 session cookie 的 expires, last_activity_at + inactivity_ttl)`，卡上显示"预计 N 天后需重新登录"；进入最后 3 天发一次通知，失效当天再发一次并把卡标红；"去登录"一键重扫是兜底，永远比任何保活都可靠。

**侦察阶段要拿到的每站事实**（P0，需要用户在自己桌面登录后配合）：
1. 登录后 `getCookies` 全量：哪些是会话 cookie、各自 `expires`、`httpOnly/sameSite`。
2. 登录后页面自己发的 XHR 里挑一个只读、返回用户信息的 JSON 接口作为 S2/K1 目标，记下 URL 与响应字段（用 CDP `Network.requestWillBeSent` 抓 30 秒即可）。
3. 退出登录后再抓一次，确认 S2 接口在未登录时的表现（302 / 401 / 空 JSON），作为 `expired` 判据。
4. `inactivity_ttl` 初值：抖音创作者中心 / 来客暂按 30 天，美团开店宝按 7 天，小红书创作平台按 14 天——**都是假设**，靠上线后的探活数据校准，卡上标"预计"。

### 3.10 侦察结果（2026-09-08，用户桌面 `ecd-glxi1nk433hliivri`，三站已登录；小红书未登录待补）

方法：云助手在桌面上跑内联 Python，浏览器级 `Storage.getCookies`（只记名字/域/到期/标记，不取值）+ 各标签页 `performance` 资源记录 + 被动监听 45 秒 + 在各自标签页里同源 `fetch` 候选接口，分别带 cookie 与不带 cookie（`credentials: omit`）各调一次。**全程零导航、零刷新。**

| 站 | 会话 cookie（判 S1） | 到期 | S2 接口（同源 GET） | 已登录 | 未登录 | 昵称 |
|---|---|---|---|---|---|---|
| 抖音创作者中心 `creator.douyin.com` | `.douyin.com`: `sessionid` `sessionid_ss` `sid_tt` `uid_tt` `x_tt_token`（httpOnly）；`passport_auth_status`（30 天）；`sid_guard` 360 天 | 会话 60 天（2026-11-07） | `/aweme/v1/creator/user_message/unread_count/`（页面自己每几十秒轮询它） | `status_code: 0` | `status_code: 8, "用户未登录"` | ✅ `GET /aweme/v1/creator/user/info/` → `douyin_user_verify_info.nick_name` / `douyin_unique_id` / `avatar_url` / `follower_count`（备选 `/web/api/media/user/info/` → `user.nickname`）。探活用 `unread_count`，昵称每日一次随 S2 取 |
| 抖音来客 `life.douyin.com` | `.life.douyin.com`: `sessionid_ls` `sid_tt_ls` `uid_tt_ls` `passport_auth_status_ls`（30 天）+ 同上 `.douyin.com` 那套 | 会话 60 天 | `/life/gate/v1/user/login_info/` | `status_code: 0`，`data.login_status: 1`，`data.name`、`role_name`、`user_id` | `status_code: 4000100, "用户鉴权失败"` | ✅ `data.name`；店名/账户用 `/life/gate/v1/account/detail` 的 `account_name` / `account_id` |
| 美团经营宝 `e.dianping.com`（点评商户平台，用户口中的"开店宝"） | `.dianping.com edper`（httpOnly，400 天）、`.meituan.com com.sankuai.meishi.fe.kdb-bsid`（400 天）、`epassport.meituan.com eplt/eprt`（66 天） | cookie 长期，服务端会话 TTL 未知 | `/merchant/portal/common/cityshop` | `data.currentShopIdStr` | `error.code: 10008, "账号未登陆"` | 接口无昵称；`queryAccountInfo` 需参数，`epassport getBizAccount` 跨域不可用——先只显示"已登录 + 当前门店 ID" |

其他观察：
- 创作者中心标签页开着时页面自己轮询 `user_message/notice`、`msg/top`、`live_status`（每几十秒一次），**只要标签页不关，天然就是 K2 保活**；来客与经营宝 45 秒内无轮询。
- 抖音 `passport_auth_status` 只有 30 天，比 `sessionid` 的 60 天短——它可能就是"多久没活动要重新验证"的信号，S1 把它一并纳入判定，预测到期取两者较早者。
- 美团 cookie 到期都很远，S1 对它几乎无效，只能靠 S2 与 S0。
- 抖音会话 cookie 全部 httpOnly，页面 JS 读不到，必须走 CDP `Storage.getCookies`。
- 美团 `queryAccountInfo` 带 cookie 时报 `10004 业务参数为空`，不带时 `606 未登录`——也能当判据，但 `cityshop` 更简单。

`inactivity_ttl` 初值：抖音创作者中心 / 来客按 30 天（对齐 `passport_auth_status`），经营宝按 7 天；上线后用探活数据校准。

侦察脚本与原始输出（脱敏后）留在本次会话的 scratchpad，未入库；正式实现时 `platforms/desktop/cdp.py` 按 §3.3 重写，侦察脚本只作参考：浏览器级 `Storage.getCookies` 一次拿全部站点的 cookie，比逐页 `Network.getCookies` 省一次连接。

### 3.11 边界与风险

| 风险 | 对策 |
|---|---|
| 抖音对自动化敏感，探活把登录探没 | 被动信号优先（S0）；S1 零导航；S2 只打站点前端自己也会调的 JSON 接口、优先同源 fetch、每日一次、敏感站失败不重试；侦察阶段先在用户桌面验证 S2 接口不会引起下线 |
| 保活手段本身被识别为自动化 | K1 频率是天级且带随机；K2 默认关；绝不做 K3 |
| Chrome 重启清会话文件 | 依赖"cookie 文件保留"不变量，P0 验收项：重启 Chrome 后 L1 仍 `cookie_ok` |
| 桌面回收/重建 profile 丢失 | `desktop_id` 变化 → 全部置 `unknown` + 通知 |
| 探活与用户操作抢桌面 | 租约短（≤60s）、拿不到就跳过；`open` 只在用户点击时发生 |
| `extension` 模式 | 卡上明示不可监控；工具返回 `not_applicable` |
| 多成员共用桌面 | 登录态按 workspace 展示与授权（与桌面归属一致），退出登录需 owner/admin |
| cookie 值属于敏感数据 | 永不落库、永不进日志、不进 `probe_detail`（只存名字与到期） |

---

## 4. 分期

| 期 | 内容 | 估时 |
|---|---|---|
| P0 | **真机侦察**（用户在 `ecd-glxi1nk433hliivri` 上登录四个站，我用 `/diag/browser` 模式跑一次 `getCookies` 导出 cookie 名/域/到期，找每站的轻量 JSON 接口并验证不触发下线）→ 站点目录 → `cdp.py` 三动作 → 迁移加列 → `probe` 分流 + 单测 | 2 天（侦察 0.5 天，需用户配合登录） |
| P1 | 授权中心"云电脑登录态"卡组 + open/logout 接口 + 切桌面画面与轮询 + 通知 + 定时探活 + `desktop_events` 埋点 + Fleet 抽屉分节 | 2 天 |
| P2 | `desktop_login` 工具 + dev-browser 技能前置段 + 单测 + 桌面技能同步 | 1 天 |
| P3（可选） | `requires-platforms` frontmatter → 浏览器类工具硬阻断 | 1 天 |

---

## 5. 验收

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 用户桌面上四个站各登录一次后，授权中心四行都 `已登录` 且有昵称；`probe_detail` 只有 cookie 名与到期时间 | 截图 + 行 |
| AC-2 | 探活期间用户桌面上**没有新开的可见页面**（只允许后台标签且 5 秒内关闭）；探活前后抖音创作者中心仍处于登录状态 | 云桌面录屏 + 复探 |
| AC-3 | 重启 Chrome（`ensure_browser` 走一遍恢复守卫）后 L1 仍 `cookie_ok` | 复探 |
| AC-4 | 在抖音 App 里退出网页登录 / 手动清 cookie 后下一轮探活变 `已过期`，通知出现、红点亮 | 通知列表 |
| AC-5 | 点"去登录"：登录页出现在云电脑前台、工作台自动切到桌面画面、扫码后 15 秒内行变绿 | 录屏 |
| AC-6 | 点"退出登录"：只清该站域的 cookie，其他站仍 `已登录` | 复探 |
| AC-7 | 桌面隧道断开时状态 `桌面离线`，`last_ok_at` 不变，不发通知 | 断隧道验证 |
| AC-8 | 模型：对话里说"帮我在创作者中心看数据"，agent 先调 `desktop_login(status)`，未登录时贴出引导而不是截屏 | 会话记录 |
| AC-9 | 单测：cookie 判定（缺/过期/正常）、站点目录完整性、状态机、桌面更换置 unknown、通知去重 | pytest 绿 |

---

## 6. 开工前确认（2026-09-08 用户已拍板）
1. ✅ 首批就这四个站：抖音创作者中心、抖音来客、美团开店宝、小红书创作平台。
2. ✅ 用户已在 `ecd-glxi1nk433hliivri` 上登录抖音创作者中心、抖音来客、美团经营宝三站，侦察结果见 §3.10；小红书创作平台未登录，待补侦察。
3. ✅ 频率：S1 每 6 小时、S2 每天一次、K1 天级。
4. ✅ 退出登录限 owner/admin。
5. 用户要求：探活方式与保活手段要想清楚——见 §3.8 / §3.9；K2 常驻标签页默认关，等 K1 数据说话。

---

## 7. 执行记录

### 7.1 2026-09-08 P0 落地（在用户桌面 `ecd-glxi1nk433hliivri` 上验证）

- **代码**：`backend/platforms/desktop/{sites,cdp,service}.py`；`platform_accounts` 加 `desktop_id`、`probe_detail`，迁移 `b8e3f5a7c9d1`（接在 D1 的 `b6d1e2f3a4b5` 后）；`desktop_events` 新增 `platform.probe`；API 加 `POST /api/platform-accounts/desktop/{site}/open`、`POST /desktop/probe`、`POST /{id}/logout`，`/{id}/probe` 按 `auth_kind` 分流，`GET /api/platforms` 带 `kind:"desktop"` 的站点；OAuth 续期任务改为只取 `auth_kind='oauth'` 行。
- **脚本形态**：一段内联 Python（`cdp.SCRIPT`，约 10 KB）base64 进一条 shell 命令，经动作服务器 `client.execute` 在桌面上跑；浏览器级 `Storage.getCookies` 一次拿全站 cookie（只保留名/域/到期）；S2 优先在已开着的该站标签页里同源 `fetch`，没有标签页才 `Target.createTarget(background)` 打 JSON 地址并在 6 秒内关掉；`open` 用 `/json/new` + `/json/activate`；`logout` 逐条 `Network.deleteCookies`。
- **服务语义**：`probe_workspace(level=1|2)`——6 小时一级、每日一次二级（每站 `last_level2_at` 间隔 ≥20h，`recon_pending` 站永不二级）；桌面上已登录但没人登记的站会**自动登记**；桌面 id 变化 → 全部置 `unknown` + 通知 `desktop_login_reset`；失效 → `desktop_login_expired`，同一站 23h 内不重复；桌面忙（租约 423）→ `DESKTOP_BUSY` 跳过；隧道不通 → 行置 `desktop_offline`。`predicted_expiry = min(最早会话 cookie 到期, last_ok_at + inactivity_ttl)`。
- **真机**：脚本以 level 2 + profile 在用户桌面上跑一次（云助手通道）：创作者中心 `bound`（昵称、抖音号、粉丝数到手）、来客 `bound`（昵称、角色"商家子账号"、店名"芊屿芊浔美甲美睫(汉街北门店)"、account_id）、经营宝 `bound`（`cityshop` 有返回，`shop_id` 为 -1 说明该账号当前未选门店）、小红书 0 cookie → `unknown`。三站 S2 都走了"已开标签页同源 fetch"（`via: tab`），零新标签零导航。抖音会话 cookie 到期 2026-11-07，`passport_auth_status` 到期 2026-10-08 → 预计到期取 10-08。
- **单测**：`tests/unit/test_desktop_login.py` 10 条（目录与侦察值一致、脚本不带 cookie 值、cookie/接口判定、二级探活节奏、自动登记、失效通知去重、桌面更换重置、打开登录页与退出、离线、到期预测）。
- **上线与端到端（18:20）**：backend `20260908-a5p0-f160cf7` 发到 gw2 与 AWS（两边 backend 现在都由 `docker-compose.override.yml` 钉镜像，发版要改 override 的 `image:` 行；gw2 前端被钉在队友的 `landing-8b80e28`，本期未动），迁移 `b6d1e2f3a4b5 → b8e3f5a7c9d1` 两边跑完，gw2 发布前有库备份 `backups/pre-a5p0-20260908181953.sql.gz`。
  在 gw2 容器里对 `bbdwxh_admin` 的工作空间（桌面 `ecd-glxi1nk433hliivri`，通道 ssh/up）跑 `probe_workspace(level=2, force_level2=True, lease=True)`：**1.4 秒**返回，三站自动登记为 `bound`（创作者中心昵称与抖音号、来客昵称/角色/店名、经营宝当前门店 -1），`predicted_expiry` 抖音两站 = 2026-10-08（`passport_auth_status` 到期），经营宝 = 2026-09-15（7 天不活动假设）；`desktop_events` 记到一条 `platform.probe ok 1438ms`。S2 全部经"已开标签页同源 fetch"，桌面上无任何可见动作。
- **未做（P1/P2）**：定时任务注册、前端卡组、`desktop_login` 工具与 dev-browser 技能前置段、Fleet 抽屉分节。

### 7.2 2026-09-08 P1 落地

- **定时任务** `desktop_login_probe`（`platforms/desktop/tasks.py`）：每 6 小时一次；上海时间 06:00–09:00 的那一轮跑二级，其余一级；只取 `pool_state=assigned` 且 `tunnel_state=up` 的桌面；5 小时内探过的工作空间跳过；每台随机 0–20 秒抖动；桌面忙（租约 423）计 `busy` 跳过，隧道不通计 `unreachable`。
- **授权中心"云电脑登录态"卡组**（`DesktopLoginCard.tsx` + `useDesktopLogin` 钩子）：站点目录每站一行，状态徽标（已登录 / 已失效 / 待确认 / 云电脑离线 / 已退出 / 未登录 / 等待扫码）、昵称、店名与角色、"预计 X 需重新登录（约 N 天）"、上次检测、失败原因；按钮：去登录 / 重新登录（推登录页到云电脑 + 打开桌面面板 + 每 5 秒轮询 3 分钟）、检测、退出登录（确认框，owner/admin）、全部检测、查看云电脑。小红书标"待侦察"并禁用去登录。
- **通知条** `NotificationStrip`：拉 `GET /api/notifications?unread=true`，逐条"已读"。
- **本地验证**（SQLite + Redis + JWT 模式，塞入三行桌面登录态与一条通知，`cloud_desktops` 隧道置 down）：深色与浅色两种主题截图正常；"重新登录"在桌面离线时提示"云电脑不在线或还没开通"；已读后通知条消失。**发现并修复**：`/api/platform-accounts/desktop/probe` 被先声明的 `/{account_id}/probe` 抢先匹配（"账号不存在"），已把桌面路由移到 id 路由之前并加了一条路由顺序单测。
- **检查**：后端 `test_desktop_login*` 15 条 + 前端 `npm run check` 全绿（223 条含 `DesktopLoginCard.test.tsx` 2 条）。
- **上线**：AWS 与 gw2 见 §7.3。

### 7.3 2026-09-08 19:10 P1 上线

- AWS：backend + frontend `20260908-a5p1-9848834`（含路由顺序修复）。
- gw2：**只切了 backend** 到 `20260908-a5p1-9848834`；frontend 仍是队友用 override 钉的 `20260908-landing-8b80e28`，因为 `8b80e28` 不在 main 上，换成 main 构建会把他们的落地页改动冲掉。**gw2 上要看到"云电脑登录态"卡，得等落地页合进 main 再统一发前端**（或用户拍板先换）。
- gw2 容器内确认：`desktop_login_probe` 已注册（与 `platform_token_keepalive` 并列），桌面路由排在 `/{account_id}/probe` 之前，库 `b8e3f5a7c9d1`。定时任务下一轮起会按 6 小时探活；06:00–09:00 那轮带二级。

### 7.4 2026-09-08 P2 落地（模型侧）

- **工具** `backend/tool/desktop_login.py`（build agent 独占）：`status` 只读授权中心记录不碰桌面（含预计到期、店名），指定站点未登录时返回结构化 `DESKTOP_LOGIN_REQUIRED`，extension 模式返回"不适用"；`open` 推登录页到云电脑并告诉模型让用户扫码；`probe` 强制二级探活确认。站点参数接受 key 或中文名。
- **技能** `container/dev-browser/SKILL.md`：两浏览器表里 `local` 一行改为"只有用户在此登录过的站点，先问 desktop_login"；新增"Login state on the cloud desktop"一节：status → open → probe，禁止自己开登录页、填验证码、靠截图判断登录。已同步到全部云桌面（§7.5）。
- **单测** `tests/unit/test_desktop_login_tool.py` 5 条：注册与技能前置段、全站状态与缺站标记、extension 不适用、open→probe 往返、结构化错误。

### 7.5 2026-09-08 19:30 P2 上线

- AWS：backend + frontend `20260908-a5p2-499e8a9`；gw2：backend `20260908-a5p2-499e8a9`（frontend 仍是队友钉的 landing，同 §7.3）。gw2 容器内确认 `desktop_login` 已注册且在 build agent 工具表中（33 个内置工具）。
- `container/dev-browser/SKILL.md` 新版已同步到全部 15 台云桌面（OSS 临时对象 + 云助手逐台校验 sha256，旧版备份在各桌面 `/opt/openbox/backups/`），`e38f8862…`。
- **P0–P2 至此全部上线。剩余可选项**：P3 `requires-platforms` 硬阻断；Fleet 抽屉"登录态"分节；小红书侦察；`inactivity_ttl` 用真实数据校准；K2 常驻标签页只在 K1 证实无效时开。

### 7.6 2026-09-08 20:30 Codex 验收回归修复

Codex 按 `docs/A5_VERIFY_CHECKLIST.md` 验收，抓到两处由我引入的回归，当晚修掉：

1. **授权中心整页崩溃**（旧前端 + 新后端）。`GET /api/platforms` 混入了 `kind=desktop` 的四个站点，旧前端把每一项当 OAuth 卡片渲染，`capabilities.includes` 报错。修复 `7c891ee`：目录接口加 `kinds` 参数，**默认只返回 OAuth 平台**，云电脑站点要 `kinds=oauth,desktop` 显式请求；desktop 项补齐 `capabilities: []`、`configured`、`maxGrantDays` 键，形状与 OAuth 项一致。新前端请求 `kinds=oauth,desktop`。加了向后兼容测试。教训：**同一个后端要同时伺候不同版本的前端（gw2 前端被队友钉住），列表接口只能加参数不能改默认形状。**
2. **模型加载 dev-browser 技能失败**。§7.5 把新版 `SKILL.md` 直接拷到桌面的 `/opt/openbox/skills/dev-browser/`，但桌面的 `repair_browser_runtime.py --check` 会把该目录与 `/opt/openbox/tools/dev-browser-sources.json` 逐文件比对，`SKILL.md` 不一致 → 后端在技能加载时走 runtime 修复分支 → 镜像里没有 `/container/dev-browser` → 抛 `browser runtime sources are not available`，模型看到"浏览器无法启动"，根本没走到 `desktop_login`。**这是不许直接拷贝 dev-browser 源文件到桌面的原因**。处理：先把 15 台桌面的 `SKILL.md` 恢复原版（`e0bd3abf…`，`--check` 回到 ready），再用**正规路径**下发：在完整 checkout 里 `sandbox.browser_runtime.runtime_cloud_commands()` 生成分片安装脚本（10 段，每段 <16 KiB），云助手逐台顺序执行——它同时更新 `dev-browser-sources.json` 与技能目录，然后跑 `--install-deps --register-service` 校验，只停 relay（Chrome 与用户登录态不动）。用户桌面 `ecd-glxi1nk433hliivri` 结果 `dev_browser_sources_updated=True`、`{"version":"20260907.4","ready":true}`，`SKILL.md` = `e38f8862…`（含登录态前置段）。其余 14 台同法下发。以后改 `container/dev-browser/**`（含 SKILL.md）一律走这条路，或 `scripts/wuying_bootstrap.py` 的 dev-browser 步骤。

上线：AWS backend + frontend `20260908-a5fix-7c891ee`；gw2 backend `20260908-a5fix-7c891ee`，frontend 仍是队友钉的 landing（旧前端现在能正常打开授权中心）。Codex 的验收记录与证据在 `docs/A5_VERIFY_CHECKLIST.md` 末尾和 `docs/evidence/a5-verify-20260908/`；其余未验项（扫码发布、退出重登、普通成员、模型 3.1–3.5）待用户配合后重跑。
- **2026-09-09**：andrew 把落地页分支合入 main（`2183504`）并把 gw2 backend+frontend 切到 `20260909-ask-2183504`，其中已含 `7c891ee` 的兼容修复与云电脑登录态卡片；gw2 前端切换这件事就此完成，`20260908-a5fix-7c891ee` 的前端镜像不含落地页，不要再切回去。AWS 仍是 `20260908-a5fix-7c891ee`（无落地页 SEO 变更之外的差异，待 andrew 的 ask 发布一并推 AWS）。

