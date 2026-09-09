# A5 授权中心 · 验证清单（给 Codex）

> 2026-09-08。测试环境，不要求覆盖边界；每条按"怎么做 → 应该看到什么"走一遍，结果直接记在本文末尾的表里。
> 背景与实现细节见 `docs/A5_AUTHORIZATION_CENTER.md`（开放平台 OAuth + 投稿）与 `docs/A5_DESKTOP_LOGIN_STATE.md`（云电脑登录态）。

> **2026-09-09 更新**：gw2 已由 andrew 切到 `20260909-ask-2183504`（backend+frontend），落地页已合入 main，0.1 不用再做，§1/§2 前端项可以直接测。
>
> **2026-09-08 20:30 更新**：Codex 报的两处阻塞已修（详见 `A5_DESKTOP_LOGIN_STATE.md` §7.6）：① `/api/platforms` 默认只回 OAuth，旧前端不再崩，gw2 backend 已切 `20260908-a5fix-7c891ee`；② 桌面 dev-browser 技能改走运行时正规下发，`--check` ready 且 SKILL.md 含登录态前置段。1.1 与 3.1 可以重跑；0.1 的候选前端镜像改为 `openbox-frontend-v2:20260908-a5fix-7c891ee`（已在 gw2 机器上）。

## 0. 环境事实

| 项 | 值 |
|---|---|
| 测试站 | `https://ai.bossipai.com.cn`（阿里云 gw2） |
| 登录账号 | `bbdwxh_admin`（该工作空间 owner；密码找用户） |
| 工作空间 id | `01M1VTTN99V33P1ASKFT5TSM2P` |
| 该工作空间的云电脑 | `ecd-glxi1nk433hliivri`（隧道 ssh / up；已登录：抖音创作者中心、抖音来客、美团经营宝；小红书未登录） |
| gw2 当前镜像 | backend + frontend `20260909-ask-2183504`（`main@2183504`，andrew 09-09 10:34 切换；同时含落地页、`/api/platforms` 兼容修复与"云电脑登录态"卡，线上 `AuthCenterRoute-BKEUV8Cu.js` 已核实带 `kinds=oauth,desktop`） |
| 远程执行 | `aliyun ecs RunCommand --RegionId cn-shanghai --InstanceId.1 i-uf66pcsepxpc23v5qsts --Type RunShellScript --ContentEncoding Base64 --CommandContent <base64脚本>`，结果 `aliyun ecs DescribeInvocationResults --RegionId cn-shanghai --InvokeId <t-…>`（Output 是 base64） |
| gw2 上的库 | `docker exec openbox-postgres-1 psql -U openbox -d openbox -Atc "<sql>"` |
| 抖音开放平台控制台 | 授权回调 `https://ai.bossipai.com.cn/api/platform-accounts/douyin/callback`、Webhook `https://ai.bossipai.com.cn/api/webhooks/douyin`（用户已配） |

### 0.1 ~~先把 gw2 前端切到含卡片的版本~~（2026-09-09 已无需：gw2 前端已是 `20260909-ask-2183504`，含卡片与落地页；以下留档）

镜像 `openbox-frontend-v2:20260908-a5fix-7c891ee` 已在 gw2 机器上。gw2 的 `/opt/openbox/docker-compose.override.yml` 把 frontend 钉在队友的 `landing-8b80e28`（该提交不在 main）。**切换会把落地页改动换掉，做之前跟 andrew 说一声**；要恢复就把 override 里的行改回去再 `docker compose up -d --no-deps frontend`。

```bash
cd /opt/openbox
cp docker-compose.override.yml docker-compose.override.yml.bak-$(date +%Y%m%d%H%M%S)
sed -i 's#image: openbox-frontend-v2:.*#image: openbox-frontend-v2:20260908-a5fix-7c891ee#' docker-compose.override.yml
docker compose up -d --no-deps frontend
docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep frontend
```

---

## 1. 授权中心 · 抖音开放平台（OAuth + 投稿）

| # | 怎么做 | 应该看到 |
|---|---|---|
| 1.1 | 登录后侧栏"资源中心"正下方点"授权中心" | 页面标题"授权中心"，副标题"管理各平台的登录授权"；抖音卡片带"登录授权""投稿"两个标签 |
| 1.2 | 抖音卡片点"绑定账号"，用抖音 App 扫码确认 | 回到授权中心，toast"账号已绑定"，出现账号行：昵称、头像、"已授权"、"预计到期 X（约 N 天后需重新扫码）"、"当前授权有效至"、"剩余自动续期 5 次"。地址栏不残留 `?bound=` |
| 1.3 | 账号行点"检测" | toast"授权正常"，上次检测时间刷新 |
| 1.4 | 点"发布到抖音"，选一个资源中心里的 mp4（≤128 MB），填标题和话题，点"生成二维码" | 出二维码 + 倒计时；抖音 App 扫码后直达发布页，**标题和话题已带上**（这是上次反馈过的问题，重点看）；在抖音点发布后 1 分钟内对话框变"已在抖音发布"并显示作品 id |
| 1.5 | 库里看投稿记录：`select id,status,share_id,item_id,error from publish_jobs order by created_at desc limit 3;` | 最新一条 `published`，有 `item_id`；`error` 为 `schema_source=local` 说明走的是本地拼 schema（`jump.basic` 未开通），为空说明走了抖音短链 |
| 1.6 | 在抖音 App"设置 › 账号与安全 › 授权管理"里取消对应用的授权，回网页点"检测" | 状态变"已失效"，按钮变"重新授权"；重新扫码后回到"已授权"，且还是同一行（同 open_id 复用） |
| 1.7 | 点账号行的垃圾桶 | 确认框 → 行消失 |
| 1.8 | 用一个普通成员账号登录同一工作空间看授权中心 | 看不到"绑定账号""解绑"按钮，能看到"检测"和"发布到抖音" |

## 2. 授权中心 · 云电脑登录态（需先做 §0.1）

| # | 怎么做 | 应该看到 |
|---|---|---|
| 2.1 | 打开授权中心 | 抖音卡下面有"云电脑登录态"卡，副标题带"云电脑 …hliivri"；四行：抖音创作者中心、抖音来客、美团经营宝（点评商户平台）、小红书创作平台 |
| 2.2 | 看三个已登录的行 | "已登录"徽标；创作者中心和来客有昵称，来客还有"店铺：芊屿芊浔美甲美睫(汉街北门店)"和"商家子账号"；"预计 2026/10/8 需重新登录（约 N 天）"；有"检测"按钮和退出图标。小红书是"未登录 + 待侦察"，"去登录"灰掉 |
| 2.3 | 点"全部检测" | 转圈后上次检测时间刷新，状态仍"已登录"；**云电脑画面上不应出现任何新窗口或页面跳动** |
| 2.4 | 库里看事件：`select ts,status,duration_ms,summary from desktop_events where kind='platform.probe' order by ts desc limit 3;` | 有一条 `ok`，1–3 秒，summary 是 `probe douyin_creator,douyin_laike,...` |
| 2.5 | 点某一行的"检测"（例如抖音来客） | toast"授权正常"；`probe_detail` 里 `via` 应为 `tab`（在已开着的标签页里同源请求） |
| 2.6 | 在云电脑上手动退出美团经营宝（或在授权中心点它的退出图标 → 确认） | 该行变"已退出"/"已失效"；`select kind,title from notifications where workspace_id='01M1VTTN99V33P1ASKFT5TSM2P' order by created_at desc limit 3;` 里出现 `desktop_login_expired`（退出登录不发通知，失效才发） |
| 2.7 | 对已退出的站点点"重新登录" | toast"登录页已推到云电脑，请扫码"；工作台右侧的云电脑面板自动打开，云电脑前台是该站登录页；行变"等待扫码…"并出现"查看云电脑"按钮 |
| 2.8 | 在云电脑上完成扫码登录 | 15 秒内该行自动变"已登录"，toast"云电脑登录成功"（超过 3 分钟没登会提示超时，之后点"检测"也能补） |
| 2.9 | 页面顶部通知条 | 有未读时显示"N 条未读通知"，点 × 变已读并消失 |
| 2.10 | 定时任务：`select name,last_run_at,last_status from internal_task_state where name='desktop_login_probe';` | 有记录；上线后每 6 小时一次（上海时间 06–09 点那轮带二级检查） |

## 3. 模型侧（在对话里）

| # | 怎么做 | 应该看到 |
|---|---|---|
| 3.1 | 新对话说"帮我看看抖音创作者中心的数据" | 模型先调 `desktop_login(action=status, site=douyin_creator)`，返回"已登录"后直接用云电脑浏览器干活，**不自己截屏猜、不打开登录页** |
| 3.2 | 先在授权中心退出美团经营宝，再说"去美团经营宝看看今天的订单" | 模型调 status 得到 `DESKTOP_LOGIN_REQUIRED` → 调 `open` → 对话里让你去云电脑扫码并停下来等；你说"扫完了" → 模型调 `probe` → 确认已登录后继续 |
| 3.3 | 说"把刚才那条视频发到抖音"（或先让它做一条口播视频再发） | 模型加载 `douyin-publish` 技能：先报账号状态 → 拟标题（≤55 字）和 3–5 个话题出确认卡 → 你点"可以" → 回复里贴出投稿二维码图片 → 扫码在抖音发布 → 你说发了 → 模型查结果报"已发布" |
| 3.4 | 在授权中心解绑抖音账号后再说"发抖音" | 模型贴出授权二维码/链接并停下来等你扫，不会自己绕 |
| 3.5 | 用普通成员账号让模型"发抖音" | 模型说只有工作空间管理员能绑定 |

## 4. 顺手看一眼（不算失败项）

- gw2 后端日志里不该出现 `access_token`、`refresh_token`、`client_secret`、cookie 值：`docker logs --since 1h openbox-backend-1 2>&1 | grep -ciE 'act\\.|rft\\.|sessionid='` 应为 0。
- `platform_accounts` 的 desktop 行 `probe_detail` 里只有 cookie 名和到期时间戳，没有值。
- 桌面上 `/opt/openbox/skills/dev-browser/SKILL.md` 开头有"Login state on the cloud desktop"一节（`sha256` 前 12 位 `e38f88623df6`）。

---

## 结果记录

| 项 | 结果（✅/❌/跳过） | 备注 |
|---|---|---|
| 0.1 | 跳过 | 已核实仍为旧 frontend + 新 backend；切换会替换 andrew 落地页，尚未协调/切换。候选镜像已在机器上。 |
| 1.1–1.8 | ❌ / 部分通过 | 1.1 页面崩溃；1.3 后端真实检测通过；扫码、投稿、解绑、普通成员 UI 尚未验收。详见逐项记录。 |
| 2.1–2.10 | 部分通过 | 全站 S1、来客 S2、事件落库、任务注册通过；页面与扫码闭环被前端阻塞。 |
| 3.1–3.5 | ❌ / 跳过 | 3.1 真实对话只调用 skill，未调用 desktop_login，报告浏览器启动失败；其余依赖扫码/成员账号。 |
| 4 | ✅ | 近 1 小时日志指定敏感模式命中 0；cookie 详情无值；技能 SHA 与前置段核实一致。 |

### 2026-09-08 验证记录（19:47–19:56，Asia/Shanghai）

**结论：尚未通过整体验收。** 本次实际访问 gw2 网页，读取线上容器与数据库，在现有工作空间执行 OAuth 检测、云电脑全站 cookie 检测、来客单站认证请求，并发送一条真实模型验证消息。未切换镜像，未解绑/退出任何现有账号，未实际发布视频。检测会正常更新检测时间与事件记录。

验证代码：工作树 `openbox-a5-docs`，分支 `a5-auth-center`，提交 `a3b340e`。线上 backend `20260908-a5p2-499e8a9`，frontend `20260908-landing-8b80e28`，两者 healthy；数据库迁移为 `b8e3f5a7c9d1`。浏览器复用了 `bbdwxh_admin` 的现有登录态，无需密码。

#### 实际失败与阻塞

1. **授权中心整页崩溃（1.1，阻塞大部分页面验收）。** 新开 Chrome 标签访问 `/app/auth-center` 即显示“出错了”。控制台定位到 `AuthCenterRoute-CmSYvWHC.js:1:6023`：`TypeError: Cannot read properties of undefined (reading 'includes')`。在线脚本对应语句是 `e.capabilities.includes('publish')`。运行中后端的 `list_platforms()` 返回 1 个 OAuth 平台和 4 个 `kind=desktop` 站点，desktop 对象没有 `capabilities`；旧前端未按 kind 分组，传入 OAuth 卡片后崩溃。新工作树 `AuthCenter.tsx` 已按 kind 过滤。这次不是静态资源缺失：出错 JS 可下载且包含上述执行语句。工作台侧栏中的入口顺序本身正确。
2. **模型流程未通过（3.1）。** 新对话“抖音创作者中心首页数据验证”，输入“帮我看看抖音创作者中心的数据。此次只做验证，查看首页概要即可，不修改数据。”，使用界面既有 GPT-5.6 Luna。只发生 1 次 `skill(dev-browser)` 调用，未调用 `desktop_login(status)`；最终报告浏览器运行环境无法启动，需要从完整代码仓库修复，未访问首页。直接调用部署代码中的 `desktop_login(status, douyin_creator)` 能返回 bound，不能用该工具层成功替代模型流程通过。验证会话：`session_7YBXZJ8D2J7VXC2WRZ5VHBS435`；界面显示消耗 `0.001222` 积分。
   后端保存的工具输出进一步确认：`<browser_mode>preference=local; the browser could not be started: browser runtime sources are not available at /container/dev-browser; run the repair from a full checkout (bootstrap or scripts), not from the backend image [diag:dev_01M20DRFHSG889CY8K4AGP0QHA]`。这证实技能加载阶段的运行时修复失败；尚未进一步诊断最初为何进入修复分支。见 `model-evidence.log`。
3. **真实发布结果缺少证据（1.4/1.5）。** 本次查询仅有一条旧任务 `pub_01M1XFBFKV2C4RW8N27RG6EF2W`，2026-09-07 创建，数据库仍 pending，share_id/item_id/error 为空；不能证明二维码、标题话题或 Webhook 发布回写成功。该行已过 expires_at；代码在读取单个任务时惰性更新 expired，因此旧数据库状态本身不单独判定为新缺陷。

#### 逐项结果

| 项 | 结果 | 当前证据与未覆盖部分 |
|---|---|---|
| 0.1 | 跳过 | 候选前端镜像已存在，ID `sha256:b81312e96904480c08673d1c4d4d7d8f979cbb39177980ff0385322523b2f440`；override 第 12 行仍固定 landing。依清单要求先协调 andrew，未切换。 |
| 1.1 | ❌ | 侧栏位于资源中心下方，但点击后整页崩溃，详情见上。 |
| 1.2 | 跳过 | 页面阻塞且需用户扫码。现有 OAuth 账号 bound、昵称及到期字段存在，不能代替本次绑定/回跳验收。 |
| 1.3 | ✅ 后端 / 跳过 UI | 调用运行中部署代码的 `service.probe`，真实抖音检测成功，仍 bound，last_probe_at 更新至 19:50:04；按钮、toast 未验。 |
| 1.4 | 跳过 | 页面阻塞；未生成新投稿二维码，也未执行手机扫码发布。标题/话题问题仍待真机确认。 |
| 1.5 | 跳过 | 已查询旧投稿任务，未找到 published/item_id；没有本轮新投稿，不能给本项通过。 |
| 1.6 | 跳过 | 需用户在手机撤销授权并再次扫码；本次未撤销现有授权。 |
| 1.7 | 跳过 | 页面阻塞，未解绑现有账号。 |
| 1.8 | 跳过 | 目标工作空间只有 1 个 owner，没有普通成员；未创建成员或改变权限。 |
| 2.1 | 跳过 | 旧前端不含新卡且页面崩溃；后端目录包含四个预期站点。 |
| 2.2 | ✅ 数据 / 跳过 UI | 三个已登录站点均 bound；创作者与来客昵称、来客店名及“商家子账号”存在。小红书 reconPending=true，无登录行。日期预期见下方修正。 |
| 2.3 | ✅ 后端 / 跳过 UI | 全站 S1 实际执行成功，三个站点仍 bound，检测时间刷新；本次无云桌面录屏，不声称已证明画面全程无跳动。 |
| 2.4 | ✅ | 19:50:04 的本轮 `platform.probe` 事件为 ok，耗时 356ms，summary 为四站点 probe，Chrome targets=3。比清单 1–3 秒更快。 |
| 2.5 | ✅ 后端 / 跳过 UI | 19:51:07 调用来客单站检测成功，probe_detail.via=tab、HTTP 200、code=0；昵称/店铺仍正常。 |
| 2.6 | 跳过 | 未退出美团或人为制造失效；无失效通知样本。 |
| 2.7 | 跳过 | 未退出站点，页面亦阻塞；推登录页和自动打开面板未验。 |
| 2.8 | 跳过 | 需要用户扫码配合，未验 15 秒自动识别与 toast。 |
| 2.9 | 跳过 | 工作空间通知表为空，页面阻塞；未注入通知或标记已读。 |
| 2.10 | ✅ 注册/已有运行记录 | `desktop_login_probe` 已有 last_run_at=19:09:48、last_status=ok；部署代码间隔 21600 秒、上海 06≤hour<09 选择 S2。本次没有跨 6 小时观察调度。历史明细存在 Connection refused 失败，任务总状态 ok 不能代表每台桌面探活成功。 |
| 3.1 | ❌ | 真实模型对话未按预期调用 desktop_login，未读取首页；见失败详情。 |
| 3.2 | 跳过 | 未退出美团或让用户重新扫码。工具层未登录分支已用小红书 status 验到 DESKTOP_LOGIN_REQUIRED，但不等于本项闭环通过。 |
| 3.3 | 跳过 | 未启动付费视频生成，未提交新发布任务；确认卡、二维码与发布回查需联调。 |
| 3.4 | 跳过 | 未解绑已有抖音号，也未执行新授权扫码。 |
| 3.5 | 跳过 | 缺普通成员。且该预期应限定为“无有效绑定时需要绑定”：普通成员对已有绑定账号本应可投稿，不应一律被拒。 |
| 4 | ✅ 本次范围 | 后端近 1 小时 352 行日志，对 act\\./rft\\./sessionid= 匹配为 0；desktop probe_detail 无 cookie 值或 token，OAuth 两个 token 列为 v1 密文；桌面技能 SHA 前 12 位 e38f88623df6，40–54 行含登录态检查流程。非全日志泄露审计。 |

#### 清单预期需调整

- 2.2：抖音创作者与来客预计重登日期为 2026-10-08；美团依据 `inactivityTtlDays=7`，本轮探活前为 2026-09-15，不能把三个站一律断言为 10 月 8 日。预计时间不等于平台保证的实际失效时间。
- 2.6：产品主动“退出登录”应验证 revoked，且不发失效通知；外部失效再检测才验证 expired 与 desktop_login_expired。原项表述混合两条路径。
- 3.5：只有未绑定且需要发起授权时，普通成员才应收到管理员绑定提示；已有有效授权的普通成员应能投稿。

#### 自动检查与证据

- 后端 5 个专项文件共 **42 passed**：platform_accounts、desktop_login、desktop_login_tool、desktop_login_task、douyin_publish_tool。工作树自己的 venv 缺 pytest，改用主 checkout 现有 venv 的解释器、在 A5 工作树 backend 目录运行；未改依赖。
- `frontend-v2/npm run check` **退出码 0**：i18n、eslint、TypeScript、35 个测试文件 / **223 tests passed**。Vite 有既有配置兼容提示，未阻止通过。该检查针对新工作树，不能证明线上旧前端可用。
- 公网 Webhook：`verify_webhook` challenge 20260908 → HTTP 200、`{"challenge":20260908}`；错误 `X-Douyin-Signature` 的 create_video → HTTP 401、`bad signature`。未伪造成功发布事件。
- 原始脱敏结果见 [evidence/a5-verify-20260908](evidence/a5-verify-20260908/) 的 preflight/runtime/detail/release-preflight 与测试日志；网页崩溃及模型响应见本轮浏览器工具记录。运行时检查通过容器内已部署代码连接真实 DB、抖音和桌面，**未覆盖浏览器 HTTP 鉴权、按钮事件和 toast**。

下一步先处理前后端版本兼容与模型加载浏览器失败，再约用户完成扫码、发布与退出重登，补普通成员账号，重跑尚未通过项。
