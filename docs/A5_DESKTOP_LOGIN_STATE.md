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
2. 探活不整页导航：L1 只读 cookie（`Network.getCookies`），L2 才在后台标签页打一个轻量 JSON 接口取昵称；每天一次，只对 `assigned` 桌面上有登记的站点跑，拿 `desktop_lease` 短租，桌面忙就跳过。
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
  cookie_domains=[".douyin.com", "creator.douyin.com"],
  session_cookies=["sessionid", "sessionid_ss", "sid_tt"],   # 侦察后定
  light_probe=LightProbe(url="https://creator.douyin.com/web/api/media/user/info/", nickname_path="user.nickname", ok_when="status_code==0"),  # 侦察后定
  logout=LogoutRule(domains=[".douyin.com"]),
  sensitive=True,            # 抖音系：L2 每天最多一次，失败不重试
)
```
首批：`douyin_creator`、`douyin_laike`（life.douyin.com）、`meituan_shopkeeper`（开店宝 e.meituan.com）、`xiaohongshu_creator`（creator.xiaohongshu.com）。后续加 `weixin_channels`（视频号助手）、`kuaishou_creator`。
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

### 3.8 边界与风险

| 风险 | 对策 |
|---|---|
| 抖音对自动化敏感，探活把登录探没 | L1 零导航；L2 只打 JSON 接口、后台标签、每日一次、敏感站失败不重试；侦察阶段先在用户桌面验证 L2 接口是否引起下线 |
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

## 6. 开工前需用户确认
1. 首批站点是不是这四个：抖音创作者中心、抖音来客、美团开店宝、小红书创作平台；要不要加视频号助手 / 快手。
2. 侦察需要你在 `ecd-glxi1nk433hliivri` 上把这几个站登录一遍（我不碰验证码），登录后告诉我，我只读 cookie 名和到期时间。
3. 探活频率默认每 6 小时 L1、每天一次 L2，可接受吗。
4. "退出登录"限 owner/admin，成员只能看和检测——同现有授权中心规则。
