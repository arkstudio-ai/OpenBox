# A5 授权中心 · 验证清单（给 Codex）

> 2026-09-08。测试环境，不要求覆盖边界；每条按"怎么做 → 应该看到什么"走一遍，结果直接记在本文末尾的表里。
> 背景与实现细节见 `docs/A5_AUTHORIZATION_CENTER.md`（开放平台 OAuth + 投稿）与 `docs/A5_DESKTOP_LOGIN_STATE.md`（云电脑登录态）。

## 0. 环境事实

| 项 | 值 |
|---|---|
| 测试站 | `https://ai.bossipai.com.cn`（阿里云 gw2） |
| 登录账号 | `bbdwxh_admin`（该工作空间 owner；密码找用户） |
| 工作空间 id | `01M1VTTN99V33P1ASKFT5TSM2P` |
| 该工作空间的云电脑 | `ecd-glxi1nk433hliivri`（隧道 ssh / up；已登录：抖音创作者中心、抖音来客、美团经营宝；小红书未登录） |
| gw2 当前镜像 | backend `20260908-a5p2-499e8a9`；**frontend 仍是队友钉的 `20260908-landing-8b80e28`，没有"云电脑登录态"卡** |
| 远程执行 | `aliyun ecs RunCommand --RegionId cn-shanghai --InstanceId.1 i-uf66pcsepxpc23v5qsts --Type RunShellScript --ContentEncoding Base64 --CommandContent <base64脚本>`，结果 `aliyun ecs DescribeInvocationResults --RegionId cn-shanghai --InvokeId <t-…>`（Output 是 base64） |
| gw2 上的库 | `docker exec openbox-postgres-1 psql -U openbox -d openbox -Atc "<sql>"` |
| 抖音开放平台控制台 | 授权回调 `https://ai.bossipai.com.cn/api/platform-accounts/douyin/callback`、Webhook `https://ai.bossipai.com.cn/api/webhooks/douyin`（用户已配） |

### 0.1 先把 gw2 前端切到含卡片的版本（否则 §2 前端项没法测）

镜像 `openbox-frontend-v2:20260908-a5p2-499e8a9` 已在 gw2 机器上。gw2 的 `/opt/openbox/docker-compose.override.yml` 把 frontend 钉在队友的 `landing-8b80e28`（该提交不在 main）。**切换会把落地页改动换掉，做之前跟 andrew 说一声**；要恢复就把 override 里的行改回去再 `docker compose up -d --no-deps frontend`。

```bash
cd /opt/openbox
cp docker-compose.override.yml docker-compose.override.yml.bak-$(date +%Y%m%d%H%M%S)
sed -i 's#image: openbox-frontend-v2:.*#image: openbox-frontend-v2:20260908-a5p2-499e8a9#' docker-compose.override.yml
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
| 0.1 | | |
| 1.1–1.8 | | |
| 2.1–2.10 | | |
| 3.1–3.5 | | |
| 4 | | |
