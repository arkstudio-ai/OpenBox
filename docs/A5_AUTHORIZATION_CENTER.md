# A5 · 授权中心（抖音开放平台 OAuth + H5 投稿）—— 调研结论与执行单

> 2026-09-07。自包含执行单。取代 `docs/DETAILED_PLAN_M1_M2.md` §A5 中"桌面 cookie 探活"的 v0 方案（见 §7 关系说明）。
> 从 `main`（≥ `ac038bb`）起分支 `codex/a5-auth-center`。
>
> **执行者须知**：新增 `backend/platforms/**`、`backend/api/platform_accounts.py`、`backend/api/webhooks_douyin.py`、`frontend-v2/src/features/auth-center/**`、`src/routes/auth-center/`；
> 只在 `Sidebar.tsx`、`Topbar.tsx`、`router.tsx`、`paths.ts`、`main.py`、`core/config.py`、`db/models/__init__.py`、locale 文件做加法。
> 不动 `backend/tool/video_production.py`、不动视频技能、不动移动端（B6 另立）。Client Secret 只进环境变量，任何情况下不入库、不入日志、不入前端。

---

## 1. 目标

**一句话**：侧栏"资源中心"下方加一个"授权中心"，用户在这里把抖音账号绑到当前 workspace（扫码授权），后台负责 token 加密保存、到期续期、失效提醒与解绑；在此基础上提供"投稿到抖音"能力（生成投稿二维码，用户抖音扫码后在抖音内确认发布，发布结果经 Webhook 回写）。

**完成定义**：
1. 授权中心页面上线：平台卡片列表（首个平台：抖音），每张卡显示状态、昵称、头像、授权范围、到期时间、上次检测；按钮：绑定 / 重新授权 / 检测 / 解绑。一个 workspace 可绑多个抖音号。
2. 绑定链路全程服务端换 token：前端只拿到跳转 URL；`code` 只经过后端；token 用 AES-GCM 加密入库；`state` 存 Redis 带 TTL 做 CSRF 与回跳定位。
3. 定时任务每日续期：access_token 到期前 3 天刷新，refresh_token 到期前 7 天且续期次数 < 5 时续期；不可续时置 `expired` 并写通知。
4. 投稿：`POST /api/platform-accounts/douyin/publish` 输入一个资源中心里的视频文件 + 标题/话题，返回投稿二维码；用户扫码后抖音内发布；Webhook `create_video` 回写 `item_id/video_id`，页面显示"已发布"。
5. `docs/API_INTERFACES.md` 补条目；`DETAILED_PLAN_M1_M2.md` 进度表 A5 改为"进行中/已验收"。

---

## 2. 抖音开放平台能力调研结论（2026-09-07，文档抓取自 developer.open-douyin.com）

### 2.1 应用现状
网站应用，已上线（正式应用），类别"视频剪辑"，已开通：抖音登录、抖音授权、投稿能力。Client Key `awo31jrmrt6j48q9`。

> ⚠ Client Secret 已在聊天里以截图形式出现过一次，建议在控制台**重置**后再配到 gw2 的 `.env`。

### 2.2 登录授权（OAuth 2.0 授权码，PC 扫码）

| 项 | 结论 |
|---|---|
| 授权页 | `GET https://open.douyin.com/platform/oauth/connect/?client_key&response_type=code&scope&redirect_uri&state[&optionalScope=x,1,y,0][&is_call_app=1]`。PC 上出二维码，用户用抖音 App 扫码确认 |
| 回调 | 302 到 `redirect_uri?code=&state=&scopes=`；`scopes` 是用户实际授予的范围 |
| redirect_uri 约束 | 必须 https；**域名 + path 与控制台"授权回调地址"逐字一致**；**不允许自带 query**（要传参放 `state`，base64url）；`#` 后不校验；每个应用最多 10 条 |
| scope 约束 | 单次申请不超过 3 个 scope；只申请业务相关的 |
| code | 10 分钟有效，一次性 |
| 换 token | `POST https://open.douyin.com/oauth/access_token/`，**form-urlencoded**，`client_key, client_secret, code, grant_type=authorization_code` → `access_token(15 天), refresh_token(30 天), open_id, scope, expires_in, refresh_expires_in` |
| 刷新 access_token | `POST /oauth/refresh_token/`（form），`client_key, grant_type=refresh_token, refresh_token` → 新 access_token 15 天，refresh_token 有效期不变；access 未过期时调用等于续期 |
| 续期 refresh_token | `POST /oauth/renew_refresh_token/`，`client_key, refresh_token` → 新 refresh_token 30 天，旧的立即失效；**最多 5 次**，之后必须重新扫码；需要应用有 `renew_refresh_token` 权限（错误码 10004 = 没权限） |
| 最长免扫码时长 | 15 + 30 + 30×5 = 195 天 |
| 失效信号 | access 过期 `10008 / 2190008`；refresh 过期 `10010`；续期次数用尽 `10020`；用户在抖音"授权管理"里可随时取消，取消后 access 立即失效；平台会定期清理不合规授权 |
| 用户信息 | `POST /oauth/userinfo/`（json，`access_token, open_id`，scope `user_info`）→ `nickname, avatar, open_id, union_id` |
| client_token | `POST /oauth/client_token/`（json，`client_key, client_secret, grant_type=client_credential`）→ 2 小时；重复获取只保留最新两个（5 分钟缓冲）；5 分钟 500 次限频；**必须全局缓存** |
| 并发陷阱 | 同一 refresh_token 并发刷新会直接失效——续期任务必须单实例（用 `internal_tasks` 的 DB 锁）并按行加锁 |

### 2.3 投稿（网站应用 = "H5 发布"，不是服务端上传接口）

**关键结论**：当前"移动/网站应用"的 OpenAPI 列表里**没有**服务端上传/创建视频接口（旧的 `video.create` 一类已不对网站应用开放）。网站应用的投稿走 **H5 发布 Schema → 二维码 → 用户抖音扫码 → 抖音 App 拉取视频 → 用户在抖音内点发布**。也就是说：

- **每次投稿都需要用户本人扫一次码、点一次发布**，无法由 agent 全自动发出。这是产品层面必须接受的边界。
- 投稿链路本身**不用**用户 access_token，用的是应用级 `client_token` + `ticket` + 签名。用户绑定（§2.2）的作用是：知道绑的是谁、显示昵称头像、把 Webhook 回来的 `from_user_id`（open_id）对上账号、以及后续查数据类能力。

流程与接口：

| 步骤 | 接口 / 规则 |
|---|---|
| 0. 能力前提 | 控制台"能力管理 > 内容能力 > 投稿能力"下要有：**发布内容至抖音：H5 场景（`h5.share`）**、**获取 openTicket（`open.get.ticket`）**；抖音 ≥30.5.0 还要 **`aweme.share`**（投稿）/ `aweme.forward`（转发到日常）。**需要用户在控制台核对这三项是否都已开通**（截图里只看得到"投稿"大项） |
| 1. client_token | 见 §2.2，全局缓存 |
| 2. ticket | `GET https://open.douyin.com/open/getticket/`，header `access-token: <client_token>`，scope `open.get.ticket` → `ticket`，约 7200 秒，全局缓存 |
| 3. share_id（可选但建议） | `POST https://open.douyin.com/share-id/?need_callback=true`，header `access-token: <client_token>` → `share_id`，**1 小时有效**；放进 schema 的 `state`，Webhook 回调靠它对账 |
| 4. 签名 | 服务端算：`nonce_str, ticket, timestamp(秒级字符串)` 按 key 字典序拼 `k=v&k=v`，MD5 → `signature`。**必须在服务端算** |
| 5. Schema | `snssdk1128://openplatform/share?share_type=h5&client_key=&nonce_str=&timestamp=&signature=&state=<share_id>&video_path=<https URL>&title=&hashtag_list=<JSON 数组字符串>&share_to_publish=1&private_status=0&download_type=1`，参数全部 urlencode |
| 6. 拉起 | PC：把 schema 生成二维码给用户扫；移动端 H5：直接跳 schema |
| 7. 结果 | Webhook 事件 `create_video`：`{event, from_user_id(open_id), client_key, content:{share_id, item_id, video_id, has_default_hashtag}}` |

`video_path` 约束：**公网可访问的 https 直链**，单视频 ≤128 MB，mp4/mov，响应 `content-type` 必须是 `video/mp4 | video/quicktime | video/3gpp`；iOS 不支持路径含中文，`+` 号可能出问题 → 用 ASCII object key 的 OSS 签名 URL（有效期 ≥ 二维码有效期）。`share_to_publish=1` 直达发布页（仅单视频）；不传则先进裁剪/编辑页。`title`、`hashtag_list`、`private_status`、`download_type`、`short_title` 均支持（抖音 ≥30.0.0）。

### 2.4 Webhook

- 控制台"设置 > 开发配置 > Webhooks"填 URL，保存时平台 POST `{"event":"verify_webhook","client_key":"","content":{"challenge":N}}`，我方必须以 text 形式在 body 返回 `{"challenge": N}`。
- 之后要在控制台勾选订阅事件（`create_video`）。
- 签名：header `X-Douyin-Signature = sha1(client_secret + 原始请求体)`，逐字节比对；`Msg-Id` 去重；5 秒超时，重试 3 次；长时间不响应会被取消订阅。

### 2.5 对产品的三条硬结论

1. 投稿不可全自动，一定有"用户扫码 + 抖音内确认"这一步；agent 的角色是把片子、标题、话题准备好并出二维码。
2. 授权免扫码最长 195 天，之后必须重新绑定；页面要有明确的"需要重新授权"状态与入口。
3. 视频必须以公网 https 直链交给抖音——这是**平台侧生成的短时 OSS 签名 URL**，由后端投稿接口签发，不是技能层的"素材外传"，与 C5 硬规则不冲突，但要在技能里写明"投稿只能走 `platform_publish` 工具"。

---

## 3. 现状（main `ac038bb`）

| 事实 | 位置 |
|---|---|
| 主前端是 `frontend-v2`（React 19 / Router 8 / TanStack Query 5 / Zustand / Tailwind 4 / i18next，zh-CN 与 en-US 必须对齐 `npm run check:i18n`） | `README.md:9`、`Makefile:4`、`frontend-v2/docs/ENGINEERING_SPEC.md` |
| 侧栏是三个平级入口：资源中心 → 技能中心 → 定时任务，硬编码按钮，没有子菜单概念；"资源中心下边"= 在它后面插一个平级入口 | `features/workspace/components/Sidebar.tsx:128-160` |
| 路由常量集中在 `paths` / `routePatterns`；路由表在 `app/router/router.tsx:57-63`；Topbar 按 pathname 决定标题 | `shared/router/paths.ts`、`features/workspace/components/Topbar.tsx:29-49` |
| 可直接照抄的页面模板：技能中心（`cd3e5c8`）——route 壳 + feature 目录（`api/keys.ts`、`api/*.ts`、`components/`、`types/`、`index.ts`）+ 侧栏 + locale | `src/routes/skills/SkillsRoute.tsx`、`src/features/skills-center/**` |
| features 之间禁止互相 import（eslint boundaries）；每个 feature 只暴露一个 `index.ts` | `frontend-v2/eslint.config.js:60-95` |
| 后端 FastAPI + SQLAlchemy 2 async + Alembic + Redis；每个域一个 `api/<x>.py` 的 `router`，在 `main.py:238-301` 注册 | `backend/main.py` |
| 当前用户 / workspace：`Depends(get_workspace)` 从 `X-Workspace-Id` 取并校验成员，把 `workspace_id`、`workspace_role` 挂到 user；cron/agent 侧没有 request，要从 session 行取 workspace_id | `backend/auth/workspace.py:23-64` |
| 唯一可用的秘密加密 helper：AES-GCM，`WUYING_CHANNEL_KEY` 主密钥，`v1:` 前缀 | `backend/sandbox/channel.py:35-70` |
| 已有一个通用 OAuth 客户端（MCP 用），但 pending state 是进程内 dict，多副本不安全——**不照抄** | `backend/mcp/oauth_provider.py`、`api/metadata.py:919-1010` |
| MCP 的 OAuth token 明文存 kv_store——**不沿用** | `backend/mcp/oauth.py:42-53` |
| 定时任务原语：`internal_tasks.register(name, interval_sec, fn)`，DB 抢锁单实例 | `backend/cron/internal_tasks.py:30` |
| 审计：`audit.record(user_id, workspace_id, action, resource_type, resource_id, details, request)` | `backend/auth/routes.py:258-267` |
| 没有通知模型（A5 v2 已指出） | `docs/DETAILED_PLAN_M1_M2.md` §A5 |
| 资源中心文件模型 `file_assets`（workspace_id + user_id，OSS 对象） | `backend/db/models/file_asset.py` |
| 视频技能明写"投稿另有技能"，C5 对照表把投稿挂在"A5 之后" | `skills/video-production/SKILL.md:147`、`C5_VIDEO_SKILL_ALIGNMENT.md:95` |
| 仓库里没有任何抖音开放平台代码，绿地 | grep 结果 |

---

## 4. 方案

### 4.1 前端：授权中心页

**改哪里**
- `shared/router/paths.ts`：`authCenter: "/app/auth-center"`、`routePatterns.authCenter: "auth-center"`。
- `app/router/router.tsx`：lazy 引入 `routes/auth-center/AuthCenterRoute.tsx`，挂在 `paths.app` 子路由。
- `features/workspace/components/Sidebar.tsx`：资源中心按钮**之后**插一个按钮，图标 `KeyRound`（lucide），文案 `t("authCenter")`。
- `features/workspace/components/Topbar.tsx`：`isAuthCenter` → 标题 `authCenter` / 副标题 `authCenterHint`（"管理各平台的登录授权"）。
- 新 feature `features/auth-center/`：
  - `api/keys.ts`（`platformAccountKeys.list(workspaceId)`）、`api/platform-accounts.ts`（`usePlatforms`、`usePlatformAccounts`、`useStartAuthorize`、`useProbeAccount`、`useUnbindAccount`、`usePublish`、`usePublishJob`）
  - `components/AuthCenter.tsx`（按平台分组的卡片网格）、`PlatformCard.tsx`、`AccountRow.tsx`、`PublishDialog.tsx`（选文件 + 标题 + 话题 → 二维码 + 轮询状态）
  - `types/index.ts`、`index.ts`
- locale：`locales/{zh-CN,en-US}/auth-center.json` + `workspace.json` 加 `authCenter`、`authCenterHint`。

**交互**
- 点"绑定抖音"→ `POST /api/platform-accounts/douyin/authorize` → 拿 `authorize_url` → `window.location.assign`（整页跳转，不用弹窗；抖音授权页会 302 回后端回调，后端再 302 回 `/app/auth-center?bound=<id>` 或 `?error=<code>`）。页面读 query 后 toast 并清掉 query。
- 状态徽标：`bound`（绿）/ `expiring`（≤7 天，黄）/ `expired`（红，按钮变"重新授权"）/ `revoked`（灰）。
- "检测"= `POST .../{id}/probe`，后端调 `userinfo` 验 token 并刷新昵称头像。
- 投稿对话框：从资源中心选一个视频（复用 `file_assets` 列表接口，只过滤 mp4/mov ≤128 MB）→ 标题、话题 → 出二维码（后端返回 PNG base64）+ 倒计时（share_id 1 小时）→ 每 5 秒轮询 `GET /api/publish-jobs/{id}` 直到 `published`/`expired`。

### 4.2 后端

**改哪里**
- `backend/core/crypto.py`（新）：把 `sandbox/channel.py` 的 AES-GCM 三个函数抽成通用 `encrypt_secret / decrypt_secret / secret_hash`（AAD 参数化），`channel.py` 改为调用它，行为不变；主密钥沿用 `WUYING_CHANNEL_KEY`（新增 `SECRETS_MASTER_KEY` 作为可选覆盖）。
- `backend/platforms/`（新）：
  - `base.py`：`PlatformProvider` 协议：`key`、`display`、`capabilities`（`login`、`publish`）、`build_authorize_url(state)`、`exchange(code)`、`refresh_access(row)`、`renew_refresh(row)`、`fetch_profile(row)`、`revoke(row)`（抖音无远端撤销接口，只本地删）。
  - `registry.py`：`get_provider(key)`。
  - `douyin/client.py`：httpx 封装上表全部接口，错误码映射为 `PlatformAuthError(code)`；`client_token` 与 `ticket` 走 Redis 缓存（key 带 client_key，TTL 取 `expires_in - 300`）。
  - `douyin/provider.py`：实现协议；`douyin/publish.py`：share_id、签名、schema、二维码（`qrcode` 库，PNG）。
  - `service.py`：`start_authorize(user, ws, platform)`（写 Redis `oauth:state:<state>` = `{user_id, workspace_id, platform, nonce}` TTL 600s）、`complete_callback(code, state, scopes)`、`probe(id)`、`unbind(id)`、`refresh_due()`。
  - `tasks.py`：`internal_tasks.register("platform_token_refresh", 6*3600, refresh_due)`；每次扫 `status in (bound, expiring)` 且 `access_expires_at < now+3d` 或 `refresh_expires_at < now+7d` 的行，`SELECT ... FOR UPDATE SKIP LOCKED`，逐行调 provider；失败按错误码置 `expired` 并写通知。
- `backend/api/platform_accounts.py`（新，`prefix="/api/platform-accounts"`，router 级 `Depends(get_workspace)`；回调路由例外，单独无鉴权）。
- `backend/api/webhooks_douyin.py`（新，`POST /api/webhooks/douyin`，无鉴权，验签）。
- `backend/api/notifications.py` + `db/models/notification.py`（A5 v2 已定义，照抄：`notifications(id, workspace_id, user_id nullable, kind, title, body, read_at, created_at)`，`GET /api/notifications`、`POST .../{id}/read`）。
- `backend/tool/platform_publish.py`（**P2**）：agent 工具 `platform_publish(platform, file, title, hashtags)` → 返回二维码给用户（走现有卡片机制），不轮询。
- `core/config.py`：`douyin_client_key / douyin_client_secret / douyin_redirect_uri / public_base_url`，env 名 `DOUYIN_CLIENT_KEY / DOUYIN_CLIENT_SECRET / DOUYIN_REDIRECT_URI / PUBLIC_BASE_URL`。
- `main.py`：注册三个 router + task。

**数据**

```
platform_accounts(
  id String(64) PK,
  workspace_id FK, bound_by_user_id FK,
  platform String(32)            -- 'douyin'
  auth_kind String(16)           -- 'oauth'（预留 'desktop_cookie' 给创作者中心路线）
  external_id String(128)        -- open_id
  union_id String(128) NULL,
  nickname, avatar_url,
  scopes String(256)             -- 实际授予的 scopes
  status String(16)              -- bound | expiring | expired | revoked
  access_token_ciphertext Text, refresh_token_ciphertext Text,
  access_expires_at, refresh_expires_at, renew_count Int default 0,
  last_refresh_at, last_probe_at, last_ok_at, last_error Text NULL,
  bound_at, created_at, updated_at, deleted_at NULL
)
UNIQUE (workspace_id, platform, external_id) WHERE deleted_at IS NULL   -- postgresql_where + sqlite_where 都要写
INDEX (status, access_expires_at), INDEX (status, refresh_expires_at)

publish_jobs(
  id PK, workspace_id, user_id, platform, platform_account_id NULL,
  file_asset_id, title, hashtags JSON, share_id, schema_hash,
  status String(16)              -- pending | published | failed | expired
  item_id NULL, video_id NULL, from_open_id NULL,
  error Text NULL, expires_at, published_at NULL, created_at, updated_at
)
INDEX (share_id), INDEX (workspace_id, created_at)
```

Redis：`oauth:state:<state>`（600s）、`douyin:client_token:<client_key>`、`douyin:ticket:<client_key>`、`douyin:webhook:msg:<Msg-Id>`（去重，24h）。

**接口**（全部挂 `X-Workspace-Id`；角色：成员可看可投稿，绑定/解绑需 `owner|admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/platforms` | 平台目录：`key, display, capabilities, configured`（未配 client_key 时 `configured=false`，前端灰掉） |
| GET | `/api/platform-accounts` | 当前 workspace 的账号列表（不含 token） |
| POST | `/api/platform-accounts/{platform}/authorize` | → `{authorize_url, state}`；抖音 scope 固定 `user_info`（登录只要这一个；投稿不需要用户 scope） |
| GET | `/api/platform-accounts/{platform}/callback` | **无鉴权**；校验 `state`→换 token→`userinfo`→upsert 行→审计→302 到 `${PUBLIC_BASE_URL}/app/auth-center?bound=<id>`；任何失败 302 到 `?error=<code>`，不把 code/token 暴露到前端 |
| POST | `/api/platform-accounts/{id}/probe` | 调 `userinfo` 校验 + 刷新资料；token 失效则尝试刷新，再失败置 `expired` |
| POST | `/api/platform-accounts/{id}/refresh` | 手动触发刷新/续期（管理员） |
| DELETE | `/api/platform-accounts/{id}` | 软删 + 清 token 密文 → `revoked`；提示用户可在抖音"授权管理"里彻底取消 |
| POST | `/api/platform-accounts/douyin/publish` | `{file_asset_id, title, hashtags[], private_status?, download_type?}` → 校验文件（mp4/mov、≤128 MB、object key 纯 ASCII）→ 签 OSS URL（有效 2h）→ share_id → schema → `{job_id, qr_png_base64, schema, expires_at}` |
| GET | `/api/publish-jobs/{id}` | 状态轮询 |
| POST | `/api/webhooks/douyin` | 验签（sha1(client_secret+body)，`hmac.compare_digest`）；`verify_webhook` 回 challenge；`create_video` 按 `share_id` 更新 job，`from_user_id` 对到账号；`Msg-Id` 去重；**先 200 再处理**（5 秒超时） |
| GET / POST | `/api/notifications`、`/api/notifications/{id}/read` | A5 v2 原样 |

**回调地址与环境**
- 生产（gw2）：`https://ai.bossipai.com.cn/api/platform-accounts/douyin/callback` —— 要**逐字**填进控制台"授权回调地址"。
- Webhook：`https://ai.bossipai.com.cn/api/webhooks/douyin`。
- 本地开发：抖音要求 https 且域名+path 白名单，本地收不到回调。**不做**"gw2 回调再按 `state` 转发回 localhost"这类方案（等于开放重定向）。本地只跑单测与 mock provider；真实链路在 gw2 验。

### 4.3 安全与合规
- `client_secret` 只在 `.env`；`/api/platforms` 不返回它；日志里对 `code/access_token/refresh_token/client_secret` 打码（httpx event hook）。
- `state` 32 字节随机，Redis 一次性消费；回调校验 `state` 对应的 `workspace_id` 与登录态无关（用户可能在回调时已换 tab），所以回跳后前端要用 `bound=<id>` 重新拉列表而不是信任本地状态。
- token 密文用 `core/crypto.py`，AAD `openbox:platform:douyin:v1`；主密钥轮换沿用 `v1:` 前缀策略。
- 审计：`platform_account.bind / unbind / refresh_failed / publish.create / publish.published`。
- 遵守抖音规范：只申请 `user_info`；投稿按钮文案用"发布到抖音"；由用户自己扫码发布，agent 不代操作。

### 4.4 与 agent / 技能的接口（P2）
- 工具 `platform_publish`：输入 `platform="douyin"`、`file`（资源中心路径或 file_asset_id）、`title`、`hashtags`；输出二维码卡片给用户 + `job_id`；不轮询，用户扫码后由 Webhook 更新，用户问时 agent 查 `publish_jobs`。
- 技能 `douyin-publish`（新，接 C5 结尾"投稿另有技能"）：frontmatter `requires-platforms: [douyin]`；工具执行前查 `platform_accounts` 有 `bound` 行，否则返回结构化错误 `PLATFORM_AUTH_REQUIRED {platform: douyin}`，agent 引导去授权中心，不自行重试。
- 移动端适配（B6）实现与验收见 §9.5；同机操作优先，扫码为备选，真实客户端链路仍需真机验收。

---

## 5. 分期与估时

| 期 | 内容 | 估时 |
|---|---|---|
| P0 | `core/crypto.py` 抽取；`platforms/` 抖音 provider（authorize/exchange/refresh/renew/userinfo）；`platform_accounts` 表 + 迁移；authorize/callback/list/probe/unbind 五个接口；续期任务；通知表与两个接口；前端授权中心页（无投稿）；locale；`API_INTERFACES.md` | 4 天 |
| P1 | client_token/ticket 缓存；share_id + 签名 + schema + 二维码；`publish_jobs`；publish/publish-jobs 接口；Webhook（验签、challenge、去重）；前端投稿对话框；gw2 配回调与 Webhook 并真机走通 | 3 天 |
| P2 | `platform_publish` 工具 + `douyin-publish` 技能 + `requires-platforms` 阻断 | 2 天 |

---

## 6. 验收

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 授权中心入口在侧栏"资源中心"正下方，标题/副标题正确，zh/en 对齐 | 截图；`npm run check` 通过 |
| AC-2 | 用真实抖音号在 gw2 完成一次绑定：跳抖音扫码页 → 扫码 → 回到授权中心，卡片显示昵称头像、状态 `bound`、到期时间 | 截图 + `platform_accounts` 行（token 列为 `v1:` 密文） |
| AC-3 | 后端日志与前端网络面板里没有 `code`、`access_token`、`refresh_token`、`client_secret` 明文 | grep 日志；浏览器 Network 检查 |
| AC-4 | 把某行 `access_expires_at` 改成明天，跑一次 `platform_token_refresh`，行被刷新、`last_refresh_at` 更新；把 `renew_count` 改 5 且 `refresh_expires_at` 改成 3 天后，任务置 `expired` 并出通知 | SQL + 通知列表 |
| AC-5 | 用户在抖音 App"授权管理"取消授权后点"检测"，状态变 `expired`，按钮变"重新授权"，重新授权后回到 `bound`（同 open_id 复用同一行） | 截图 |
| AC-6 | 选一个资源中心里的 mp4 投稿，出二维码，抖音扫码后直达发布页（`share_to_publish=1`），标题与话题已带上；在抖音点发布后 1 分钟内 `publish_jobs` 变 `published`、有 `item_id` | 截图 + 行 |
| AC-7 | Webhook：控制台保存 URL 时验证通过；伪造签名的 POST 返回 401；同一 `Msg-Id` 重放不重复处理 | curl 记录 |
| AC-8 | 非 owner/admin 成员看不到绑定/解绑按钮，直接调接口得 403；投稿可用 | 双账号验证 |
| AC-9 | 单测：签名算法（用文档示例 `nonce_str=Wm3WZYTPz0wzccnW…` → `3f7b739a91a52cb7d85c4f89c5f611fe`）、schema 拼接、state 校验、错误码映射、续期状态机、Webhook 验签 | pytest 绿 |

---

## 7. 与原 A5 方案的关系

原 A5（`DETAILED_PLAN_M1_M2.md` §A5）走的是"桌面 Chrome 里人扫码登录创作者中心/来客 → CDP 探活 cookie"，页面放设置页。本单改为**开放平台 OAuth 路线 + 侧栏独立入口**，理由：

1. 开放平台已审核通过且有投稿能力，token 有官方续期与失效信号，不依赖桌面在线、不碰抖音对自动化敏感的页面。
2. 投稿链路（H5 发布）完全不需要桌面，纯服务端 + 用户扫码。
3. 用户明确要"资源中心下边加授权中心"，作为跨平台授权的统一入口，比设置页 tab 更符合后续加平台（视频号、小红书、B 站）的形态。

保留：`platform_accounts` 表名与 `auth_kind` 字段预留 `desktop_cookie`，创作者中心数据（开放平台不给的）以后仍可按原方案接到同一张表、同一个页面。原方案里的通知模型与 `requires-platforms` 阻断机制原样采用。

---

## 8. 开工前确认（2026-09-07 用户已拍板）

| # | 项 | 结论 |
|---|---|---|
| 1 | 投稿能力 scope | 控制台截图确认 `aweme.share`、`h5.share`、`open.get.ticket` 三项均"已通过"，免费额度各 100000 次/日；"抖音授权登录"与"抖音授权"均"已开通"。`renew_refresh_token` 未见，按无处理（免扫码期 45 天），后续可申请 |
| 2 | 回调与 Webhook 地址 | 抖音控制台保存回调/Webhook 时会**先校验地址可达且返回正确**（Webhook 要回 challenge）。所以顺序必须是：**先把 P0/P1 的两个路由部署到 gw2，再去控制台填地址**。执行单里把"控制台配置"放在 gw2 部署之后 |
| 3 | Client Secret | 暂不重置。只进 gw2 `.env` |
| 4 | 投稿视频直链 | 接受 OSS 签名 URL。桶用 **bossip 上海桶**（另一会话正在迁移桶，后续 openbox 全部用 bossip 上海桶）；本单代码只通过现有 OSS 客户端签 URL，不写死桶名 |
| 5 | 多账号 | 接受：一个 workspace 可绑多个抖音号，按 open_id 去重 |

---

## 9. 执行记录

### 9.1 2026-09-07 P0 + P1 代码落地（分支 `a5-auth-center`，从 main `ac038bb` 起）

**后端**：`core/crypto.py`（通用 AES-GCM）、`db/models/{platform_account,publish_job,notification}.py` + 迁移 `a5c0d1e2f3a4`、`platforms/{base,errors,registry,service,tasks}.py`、`platforms/douyin/{client,provider,publish}.py`、`api/{platform_accounts,webhooks_douyin,notifications}.py`、`main.py` 注册、`internal_tasks` 注册 `platform_token_keepalive`（6 小时）、`core/config.py` 新增 `PUBLIC_BASE_URL / SECRETS_MASTER_KEY / DOUYIN_CLIENT_KEY / DOUYIN_CLIENT_SECRET / DOUYIN_REDIRECT_URI`。
**前端**：侧栏"资源中心"下新增"授权中心"（`/app/auth-center`），`features/auth-center/**`（平台卡、账号行、投稿对话框、二维码用 `qrcode` 包在浏览器端渲染）、`auth-center.json` 双语。
**单测**：`backend/tests/unit/test_platform_accounts.py` 15 条（签名与文档示例一致、schema、错误码映射、加密绑定 AAD、绑定/重绑/续期/失效通知/无 renew 权限/探活/解绑/投稿 staging/Webhook 验签与去重/过期）全绿；前端 `npm run check` 全绿（i18n 对齐、lint、tsc、209 条既有测试）。

**本地浏览器验证**（SQLite + 临时 Redis + JWT 鉴权模式，真实 client_key + 占位 secret）：
- 侧栏入口在"资源中心"正下方，Topbar 标题/副标题正确；平台卡显示能力标签与 195 天提示；空态/已授权/已失效三种账号行渲染正确。
- 回跳 `?platform=douyin&bound=<id>` → toast「账号已绑定」并清掉 query。
- 「检测」对一条塞入的假 token 账号：后端先调 `/oauth/userinfo/` 被拒 → 自动 `/oauth/refresh_token/` → **抖音真实返回 10010** → 行置 `expired`、按钮变「重新授权」、「发布到抖音」入口随之消失（没有 bound 账号）。
- 解绑：确认框文案正确 → `DELETE` 200 → 行消失。
- 投稿对话框：本地无 OSS，`/api/assets` 503 → 对话框显示「视频列表加载失败」（为此补了 `videosError` 状态）。二维码与 Webhook 回写只在单测里覆盖，真机链路留待 gw2。
- 未做：深色模式截图（浏览器面板隐藏时无法截图）；无 renew 权限的真实返回。

**顺带发现（未改）**：`backend/.openbox/skill_jobs.db` 被提交进了 git，且 schema 陈旧（users 无 `default_workspace_id`），单用户模式在干净 checkout 上起不来；建议单独提交把它从仓库移除并加 .gitignore。

### 9.2 待办（部署与真机）
1. ✅ 2026-09-07 15:42 已通过云助手把 `PUBLIC_BASE_URL / DOUYIN_CLIENT_KEY / DOUYIN_CLIENT_SECRET` 追加到 gw2 `/opt/openbox/config/backend.env`（备份 `backend.env.bak-20260907154232`；`WUYING_CHANNEL_KEY` 已有，不需要 `SECRETS_MASTER_KEY`；`DOUYIN_REDIRECT_URI` 留空走默认路径）。容器未重启，当时 gw2 镜像为 `20260907-subscription-129e758`，键在下次 `docker compose up -d` 时生效。
2. ✅ 2026-09-07 16:00 已合 main（`a043dea`，迁移改接 `a4b6c8d0e2f5` 之后）并以 `20260907-a5-a043dea` 先后部署 AWS 与 gw2；gw2 库升到 `a5c0d1e2f3a4`，备份 `backups/pre-a5-20260907155958.sql.gz`。公网已验：Webhook challenge、错签名 401、回调 302、前端含 auth-center 路由。详见 `docs/DEPLOY.md` 2026-09-07 条目。
   验证账号：桌面 `ecd-glxi1nk433hliivri` 挂在 gw2 用户 `bbdwxh_admin` 的工作空间（owner）。
3. ⏳ 控制台填授权回调地址 `https://ai.bossipai.com.cn/api/platform-accounts/douyin/callback` 与 Webhook `https://ai.bossipai.com.cn/api/webhooks/douyin`（`verify_webhook` 已能回 challenge），勾选 `create_video` 事件——用户操作。
4. 按 §6 AC-2 ～ AC-8 真机验收，截图进 `docs/evidence/`。
5. P2：`platform_publish` 工具 + `douyin-publish` 技能 + `requires-platforms` 阻断。

### 9.3 2026-09-07 真机反馈修正（第二版）

用户在 gw2 用真实账号跑通绑定与投稿后反馈两点：

1. **扫码后标题/话题没带过去。** 对照抖音官方 `dy_open_util.serialize`（`dy_open_util_v0.0.6.umd.js`）：它按键排序、键值都用 `encodeURIComponent`（空格 `%20`），而我们原来用 Python `urlencode`（空格 `+`），且多传了 `share_to_type/private_status/download_type` 三个旧版本不认识的键。修正：`publish.py` 改为与官方一致的序列化；话题除 `hashtag_list`（发布页话题 chips）外再以 `title_hashtag_list` 写进标题末尾；`private_status/download_type` 只在非默认值时发送。
   另外发现文档里有服务端接口 **`POST /api/douyin/v1/schema/get_share/`**（scope `jump.basic`，控制台"能力管理 › 内容管理 › 获取跳转到抖音链接"）：参数以 JSON 结构化传给抖音，由抖音生成**短链** schema，二维码密度大幅降低。现在投稿优先走它，应用没有该权限（28001018/2190004）时回退到本地签名 schema；接口响应多返回 `schemaSource: "get_share" | "local"`，`publish_jobs.error` 为 `schema_source=local` 时表示回退。**需要用户到控制台申请 `jump.basic`。**
2. **需要预计到期时间。** 后端 `to_public` 新增 `estimatedExpiresAt` = `refresh_expires_at + 30 天 × 剩余续期次数`（应用被告知无 `renew_refresh_token` 权限时就等于 `refresh_expires_at`）；账号行第一行显示"预计到期 X（约 N 天后需重新扫码）"，第二行保留"当前授权有效至"与剩余续期次数。

未能在本地复现第 1 条（没有真机）；如换用官方序列化后仍丢标题，下一步是让用户提供手机系统与抖音版本，并对比 `get_share` 短链的表现。

### 9.4 2026-09-07 P2：模型接入（`douyin_publish` 工具 + `douyin-publish` 技能）

- **工具** `backend/tool/douyin_publish.py`，四个动作：`status`（列出工作空间绑定的抖音号、预计到期、是否可投稿）、`authorize`（生成带 `is_call_app=1` 的授权链接，服务端用 segno 出二维码 PNG 钉在回复里）、`publish`（复用 `service.create_publish_job`，把投稿二维码钉在回复里，返回 `job_id`）、`result`（查 `publish_jobs`）。身份只来自 `ToolContext`（`workspace_id` 取自会话行）；没有 bound 账号时返回结构化错误 `PLATFORM_AUTH_REQUIRED`，非 owner/admin 调 `authorize` 返回 `PLATFORM_ROLE_REQUIRED`。二维码作为 `transient` 的 `FileAsset` + `FilePart(relation.kind="qr_code")` 挂在回复上，不进资源中心。
- **注册**：`tool/registry.py`、`AGENTS["build"].tools`、`BUILD_ONLY_WORKFLOW_TOOLS`（子代理不继承）。权限走默认 allow。
- **技能** `backend/.openbox/skills/douyin-publish/SKILL.md`：status → 失效则 authorize 并等用户扫完再 status → 拿 asset_id（`share_file` 返回）→ 拟标题（≤55 字）与 3–5 个话题 → `question` 确认卡 → publish → 用户扫码发布 → result。`video-production/SKILL.md` 末尾改为指向 `douyin-publish`。
- **依赖**：`segno>=1.6.6`（纯 Python 出 PNG，不需要 Pillow）。
- **单测** `tests/unit/test_douyin_publish_tool.py` 6 条：注册与 build-only、技能 frontmatter、无绑定→authorize→publish 拒绝、member 不能 authorize、绑定后 publish/result/跨工作空间隔离、PNG 头。
- 与原 A5 的 `requires-platforms` frontmatter 设计的差异：仓库已把技能字段与运行时解耦（2026-08-30），所以阻断放在工具内部（查 `platform_accounts`），frontmatter 只做文档。

### 9.5 2026-09-07 移动端对齐（代码完成，未发布）

基于 main `205226d` 对齐 A5 P0/P1、预计到期/短链修正及 P2 聊天产物。完整清单与证据见 [`MOBILE_WEB_PARITY.md` §9](MOBILE_WEB_PARITY.md#9-a5-授权中心抖音投稿与聊天二维码2026-09-07)。

- Flutter 侧新增授权中心、工作空间多账号操作与权限、视频投稿表单、原生保存二维码、最近投稿及指定 job 详情；使用现有 REST API，无新增 OAuth 回调地址。授权带 `is_call_app=1`，原 `state`/redirect 不变，用户完成后手动回到 App，页面回前台拉服务端结果。
- 授权链接 10 分钟有效，投稿采用服务端 `expiresAt`；仅对有效的白名单 URL 提供显式跳转。后台取消轮询，回前台重查；published/failed/expired 不再展示可操作的旧投稿二维码。冷启动恢复服务端任务而非签名 schema，不自动生成重复投稿。
- 账号/工作空间绑定请求与缓存，发出请求和接收结果均校验；退出/换空间/角色变化销毁旧 UI。解绑需确认；网络错误不自动重试创建投稿。
- 聊天二维码不再被 16:9 缩略图裁切，授权/结果按钮放在产物下方。工具 metadata 兼容增加：`authorize.expiresAt`、`publish.launchUrl`；后者是原二维码中的短期签名链接，非 OAuth token。只有部署此增量后，新聊天消息才有同机快捷按钮；旧消息继续用 QR/结果入口。
- 验证：Flutter analyze、65 项 Flutter 测试、23 项相关后端测试、Web check（217 项测试）、locale/文件大小门禁通过，Android debug 与 iOS arm64 simulator 构建通过。iOS 26.5 模拟器接生产，验证空态、授权 QR 生成和原生保存/取消清理；没有真实授权/投稿。本轮未提交推送或部署。
- 真机发布门禁：Android/iOS 抖音客户端唤起（含未安装）、用户拒绝/接受授权、发布页标题/话题、取消/成功/延迟 Webhook、返回 App 与杀进程后查结果。不得以“成功打开链接”代替这些验收。
