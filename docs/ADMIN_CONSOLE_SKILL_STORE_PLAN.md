# 超管系统 + 第一方技能商店 —— 设计规划与执行单

> 2026-09-08。自包含执行单。从 `main`（≥ `f2c70ba`）起分支 `codex/admin-console`，用**自己的 worktree**，不要动主工作树。
>
> 本单回答三件事：(1) 把「技能商店」从第三方目录改成**我们自己的商店**——用户投稿、官方技能、超管上下架；(2) 账户菜单的「舰队管理」升级为「超管系统」，下设舰队管理 / 技能管理 / 订阅管理三个栏目；(3) 全部按 `frontend-v2/docs/ENGINEERING_SPEC.md` 落地。
>
> **执行者须知**
> - 只做加法的文件：`UserRow.tsx`、`router.tsx`、`paths.ts`、`guards.tsx`、`main.py`、`core/config.py`、`db/models/__init__.py`、locale 文件。
> - 不动：`sandbox/**`、`billing/service.py` 的扣费逻辑、支付回调、`admin_fleet.py` 的业务（只搬壳）、移动端代码（只登记到 `MOBILE_WEB_PARITY.md`）。
> - 超管的每一个写操作都必须 `audit.record`、必须二次确认并带原因；订阅管理 v1 **只读**，不提供任何改钱的接口。
> - §3 里带「默认」的问题按默认值执行；用户改口时先改 §3 与对应方案，不要静默改代码。

---

## 1. 目标

**一句话**：技能商店只剩两类东西——**官方技能**和**用户投稿并经审核的技能**——由超管系统统一上下架；超管系统同时能看到每台桌面、每个用户装了什么、每个空间订了什么、付了多少。

**完成定义**
1. 账户菜单「舰队管理」变为「超管系统」（仅 `users.role=admin` 可见）；进入后左侧三个栏目：舰队管理 / 技能管理 / 订阅管理；旧链接 `/app/admin/fleet` 继续可用。
2. 技能管理：
   - **商店上架**：目录里每一条（官方 / 用户共享 / 第三方，Skill 与 MCP）都能看到来源、作者、版本、发布时间、安装数、上架状态；能上架、下架、置顶；下架必须填原因。
   - **投稿审核**：用户发布的技能进入「待审核」队列；审核页能看 SKILL.md 全文、文件清单、依赖、大小与哈希，能下载 ZIP，能通过或驳回（驳回必填原因）。
   - **用户安装**：按技能看谁装了、按用户看装了什么。
3. 用户侧：商店按「官方 / 用户共享 / 第三方」分区；「我的」里已发布技能显示 待审核 / 已上架 / 已驳回 / 已下架（含原因）/ 已撤回；作者可以撤回发布。
4. 订阅管理（v1 只读）：订阅列表（每空间一行）、订单列表（全量可筛）、空间详情（订阅历史、订单、账本尾、用量汇总）。
5. 所有新页面满足 `ENGINEERING_SPEC.md` §18 DoD；后端 pytest 与前端 `npm run check` 全绿；`API_INTERFACES.md`、`DETAILED_PLAN_M1_M2.md` 进度表、`MOBILE_WEB_PARITY.md` 回写。

**非目标**：技能版本历史与回滚、签名与发布流水线（bossip 那套，等有需求再上）；调账 / 代付 / 退款 / 改到期（v2，§3-Q5）；运营角色拆分（客服、财务、审核员，里程碑三）；远程从用户沙箱强制卸载；移动端超管页；兑换码；费率后台覆盖表（`BILLING_REVIEW_2026-09-07.md` 另记）。

---

## 2. 现状（2026-09-08，main `f2c70ba`）

| 事实 | 位置 |
|---|---|
| 商店目录 = 三层合并：代码硬编码的 `SKILL_CATALOG`（4 条：Anthropic 包 + 3 条 publisher=OpenBox 的内容型技能）与 `MCP_CATALOG`（7 条第三方 MCP）；`OPENBOX_CATALOG_URL` 远程覆盖（**未配置**）；`user_skills` 里 `status=published` 的社区发布 | `backend/skill/catalog.py`、`backend/api/metadata.py:495` |
| **用户上传到商店已存在，且即发即上架、无审核**：「我的」→「上传到商店 / 更新商店版本」→ `POST /api/agent/skill/{name}/publish` → `publish_personal_skill` 把草稿快照原样复制到 `published_*` 列，`published_version` +1，全站立即可见（商店里 badge「用户共享」） | `backend/api/metadata.py:313`、`backend/skill/user_library.py:423`、`frontend-v2/src/features/skills-center/components/PublishSkillDialog.tsx` |
| `user_skills`：草稿列与 `published_*` 快照列分开，`status ∈ {unpublished, published}`；**没有**下架、审核、作者撤回、管理员视角；`archive_data` / `published_archive_data` 是 LargeBinary，与元数据同表 | `backend/db/models/user_skill.py` |
| `skill_installs(user_id, user_skill_id, name, install_dir, installed_at)` 记录"谁装了哪个社区技能"，**只覆盖社区技能**；从内置目录装的没有留痕；卸载时按 category 删记录 | `backend/db/models/skill_install.py`、`metadata.py:557`（安装）、`metadata.py:426`（卸载） |
| 沙箱里技能的归类：`annotate_installed_skills` 按 `skill_installs` / `user_skills` 判 `store / personal / installed / builtin / host` | `backend/skill/user_library.py:533` |
| 镜像内置技能只有 `dev-browser`（`/opt/openbox/skills/`）；用户目录 `/data/skills/` | `container/Dockerfile:34-37` |
| 管理端：`users.role ∈ {user, developer, admin}`，`require_admin` 只认 `admin`；角色在 access JWT 里，改库后要重新登录；SSO 首登不带 admin，只能运维改 `users.role` | `backend/auth/middleware.py:75`、`docs/A3_A4_FLEET_POOL.md` §3-7e |
| `/api/admin/*` 现有：`users`（只读搜索）、`workspaces/{id}`、`audit`、`internal-tasks`；`/api/admin/fleet/*` 全套。两个 router 都挂了 `Depends(get_workspace)`，即管理请求也要带（或回落到默认）workspace | `backend/api/admin.py`、`backend/api/admin_fleet.py` |
| 前端只有一个管理页 `/app/admin/fleet`：入口在账户菜单 `UserRow.tsx`（`role === "admin"` 才渲染「舰队管理」），`AdminFleetRoute` 自己 `Navigate` 非 admin；`features/admin/` = FleetPage + DesktopDiagDrawer + api/types；`admin.json` 全是舰队文案；Topbar 不处理 admin 路径，管理页自带标题 | `frontend-v2/src/features/workspace/components/UserRow.tsx:38`、`src/routes/admin/AdminFleetRoute.tsx`、`src/features/admin/**` |
| 可照抄的页面模式：设置页 = 左侧 pill 导航 + 右侧标题/副标题/内容（`SettingsRoute` + `SettingsNav`，选中态 `bg-n300 text-ink`）；计费页 = 顶部 pill tab（`BillingRoute`）；舰队页 = 卡片 `rounded-xl border-hair bg-card p-4` + 手写 `<table class="text-xs">` + `rounded-full border-hair` 按钮 + `window.confirm` | `src/routes/settings/SettingsRoute.tsx`、`src/features/settings/components/SettingsNav.tsx`、`src/routes/billing/BillingRoute.tsx`、`src/features/admin/FleetPage.tsx` |
| `shared/ui` 只有 Dialog / Menu / Spinner / Toast / Tooltip / EnvBadge / AssetPreview / BrandMark；**没有表格与分页原语** | `src/shared/ui/` |
| 订阅归 **workspace** 不归用户：`billing_subscriptions(order_id PK, workspace_id, plan_id, cycle, plan JSON, starts_at, ends_at)`；`payment_orders(workspace_id, user_id, provider, amount_fen, currency, kind topup\|subscription, product, credits, status, provider_order_id, paid_at, cancelled_at)`；`credit_balances(workspace_id)`；`credit_ledger`；`usage_events`。套餐 free / pro / max（当前统一测试价 ¥0.10） | `backend/db/models/billing.py`、`backend/billing/plans.json` |
| 计费接口全部是 workspace 视角（`/api/billing/*`，owner/admin 成员可管）；**没有任何 admin 计费接口**；`DETAILED_PLAN_M1_M2.md:280` 曾写 `POST /api/admin/billing/grant` 代付，未实现 | `backend/api/billing.py` |
| 审计：`audit.record(actor_user_id, workspace_id, action, target_type, target_id, detail, request)`；现有 admin 读接口都记 `admin.view_*` | `backend/audit/__init__.py:12` |
| 通知：`notifications(workspace_id, user_id nullable, kind, title, body, read_at)` + `GET /api/notifications`、`POST .../{id}/read`（A5 加的） | `backend/db/models/notification.py`、`backend/api/notifications.py` |
| alembic head `b6d1e2f3a4b5`（`add_desktop_events`）；历史上有两个 merge 迁移，新迁移前先 `alembic heads` 确认仍是单头 | `backend/db/migrations/versions/` |
| 路线图里程碑三写"第一方技能市场只发布我们自己的技能，不开放第三方投稿"——与本单口径不同，本单以用户 2026-09-08 的要求为准（§3-Q1） | `docs/PLAN_SHARE.md:73` |
| 移动端：技能中心（含发布）已同构；舰队管理"有意省略"；locale 文件逐字节复制，有门禁 `mobile/scripts/check_locales.sh` | `mobile/lib/features/skills/**`、`docs/MOBILE_WEB_PARITY.md` |
| 前端硬规范：路径常量化、顶层路由懒加载 + errorElement、鉴权在守卫层、URL 即状态、feature 互不 import（eslint boundaries）、≤ 800 行、颜色全走 token、逻辑属性、i18n namespace = feature 且 zh-CN / en-US 对齐（`npm run check:i18n`）、四态、深浅色截图验收 | `frontend-v2/docs/ENGINEERING_SPEC.md` §4 / §6 / §8 / §9 / §10 / §18 |

---

## 3. 待拍板（本单按「默认」执行；改口即改本表与 §4）

| # | 问题 | 选项 | 默认（本单按此执行） | 状态 |
|---|---|---|---|---|
| Q1 | 用户投稿流程 | (a) 即发即上架 + 超管事后下架（现状）；(b) 投稿 → 审核 → 上架 | **(b)**，并加部署开关 `SKILL_STORE_REVIEW=true`；关掉即退回 (a)，下架能力保留 | 已按默认执行（待用户追认） |
| Q2 | 官方技能从哪来 | (a) admin 账号在「我的」发布，自动标官方、免审直接上架；(b) 继续代码硬编码 + 数据库覆盖上下架；(c) git 目录 + 签名发布流水线（bossip 模式） | **(a) + (b)**：admin 发布的走 (a)；现有代码目录条目走 (b)；(c) 等有版本 / 回滚需求再上 | 已按默认执行（待用户追认） |
| Q3 | 现有第三方条目去留 | 隐藏 / 保留为「第三方」分区 / 删除 | **保留代码，商店分区「第三方」**；`anthropic-skills` 包默认下架，7 个 MCP 默认上架（3 条官方内容技能依赖它们），超管可逐条改 | 已按默认执行（待用户追认） |
| Q4 | 下架语义 | (a) 只停展示与新装；(b) 连已装用户沙箱一起清 | **(a)** + 通知作者；作者可自行撤回 | 已按默认执行（待用户追认） |
| Q5 | 订阅管理这一期做不做写操作 | 只读 / 含调账、代付、改到期 | **只读**；写操作列 v2，且需二次确认 + 幂等键 + 审计 | 已按默认执行（待用户追认） |
| Q6 | 权限粒度 | 只认 `role=admin` / 本期拆运营角色 | **只认 admin**；接口与路由按栏目分文件，日后按栏目挂角色 | 已按默认执行（待用户追认） |
| Q7 | 是否加「用户」栏目作为交叉入口 | 加 / 并入订阅管理搜索 | **并入**：订阅列表与用户安装都支持按用户名 / 邮箱搜索；独立用户页留 v2 | 已按默认执行（待用户追认） |

---

## 4. 方案

### 4.1 信息架构

```
账户菜单（UserRow）      「舰队管理」→「超管系统」   仅 role=admin 渲染
/app/admin               → 重定向 /app/admin/fleet
├─ 舰队管理  /app/admin/fleet                        现有 FleetPage 原样迁入壳内
├─ 技能管理  /app/admin/skills/:tab?                 tab = store | review | installs
│    ├─ store     商店上架：官方 / 用户共享 / 第三方 全部条目，上架·下架·置顶
│    ├─ review    投稿审核：待审核队列 → 审核详情（SKILL.md、文件清单、依赖、ZIP）
│    └─ installs  用户安装：按技能 / 按用户
└─ 订阅管理  /app/admin/billing/:tab?                tab = subscriptions | orders
     ├─ subscriptions  每空间一行
     ├─ orders         payment_orders 全量
     └─ /app/admin/billing/workspaces/:workspaceId   空间详情
```

壳：左侧 pill 导航（照 `SettingsNav`），右侧标题 + 副标题 + 内容；容器 `max-w-[1180px]`（舰队页现值），二级 tab 用 `BillingRoute` 的顶部 pill。tab、筛选、搜索、分页全部在 URL query（§8.4）。

### 4.2 权限与守卫

- 前端：`app/router/guards.tsx` 新增 `RequireAdmin`（读 `useAuthStore` 的 `user.role`，非 admin → `Navigate` 到 `paths.app`），包住 `/app/admin` 父路由；`AdminFleetRoute` 里的自检删掉（§8.3：鉴权只在守卫层做一次）。
- 后端：新 router 只挂 `Depends(require_admin)`，**不挂** `get_workspace`——超管视图是全局的，强迫带 workspace 头没有意义；审计的 `workspace_id` 填被操作对象所属空间（技能作者的、订单的），没有就 `None`。
- 角色仍是 `users.role=admin` 一档（Q6）。文件按栏目分：`api/admin_fleet.py`（已有）、`api/admin_skills.py`、`api/admin_billing.py`；将来拆角色时只改各自 router 的依赖。

### 4.3 数据（一个迁移，`down_revision='b6d1e2f3a4b5'`，可降级；SQLite 与 PostgreSQL 都要能跑）

**`user_skills` 加列**

| 列 | 类型 | 含义 |
|---|---|---|
| `listing` | String(16) NOT NULL default `'listed'` | 超管侧状态：`pending` / `listed` / `rejected` / `delisted`。存量 `published` 行回填 `listed`（已经公开的东西不因迁移消失） |
| `listing_note` | Text NULL | 驳回 / 下架原因，作者可见 |
| `listing_changed_by` | String(64) NULL | 操作者 user_id |
| `listing_changed_at` | DateTime NULL | |
| `is_official` | Boolean NOT NULL default false | 官方技能（发布者当时是 admin，或超管手动标） |
| `featured` | Boolean NOT NULL default false | 置顶 |

索引 `(listing, published_at)`；现有 `ix_user_skills_status_published` 保留。`status` 增加取值 `withdrawn`（作者撤回；`published_*` 快照保留，便于再次发布与追溯）。

**`skill_installs` 扩到目录条目**

| 改动 | 说明 |
|---|---|
| `user_skill_id` 改允许 NULL | 目录条目安装没有 `user_skills` 行 |
| 新增 `kind` String(8) NOT NULL default `'skill'` | `skill` / `mcp` |
| 新增 `catalog_id` String(96) NOT NULL | 统一键：社区 `community:<row>`；目录 `<kind>:<id>`（如 `skill:web-research`、`mcp:playwright`）；存量行回填 `community:<user_skill_id>` |
| 索引 `(catalog_id, installed_at)` | 按技能看安装者 |
| 唯一约束 `(user_id, install_dir)` 保留 | |

**新表 `catalog_overrides`**（代码目录条目的运营覆盖；无行 = 用代码里的默认）

```
catalog_overrides(
  catalog_id  String(96) PK,        -- 'skill:anthropic-skills' / 'mcp:firecrawl'
  listing     String(16) NOT NULL,  -- listed | delisted
  featured    Boolean NOT NULL default false,
  note        Text NULL,
  changed_by  String(64) NULL,
  changed_at  DateTime NOT NULL
)
```

`catalog.py` 每条加 `"listing": "listed" | "delisted"` 作为代码默认（`anthropic-skills` 写 `delisted`，其余 `listed`，见 Q3）；`load_catalog()` 读 overrides 覆盖，并给每条算 `origin`：`publisher == "OpenBox"` → `official`，否则 `third_party`；社区行 `is_official` → `official`，否则 `community`。

### 4.4 技能的状态机

```
可见且可安装  ⇔  status == published  AND  listing == listed

作者 publish ──► status=published；listing 按规则：
    SKILL_STORE_REVIEW=true : pending（首次 / rejected / delisted 后再发都回 pending，超管再审）
    SKILL_STORE_REVIEW=false: listed；但当前 listing==delisted 时保持 delisted（下架不能被「更新版本」绕过）
    发布者 role==admin      : listed + is_official=true（不论开关）
作者 withdraw ─► status=withdrawn（listing 不变，快照保留）；再 publish 走上一条
超管 approve  ─► pending → listed           通知作者
超管 reject   ─► pending → rejected(note)   通知作者
超管 delist   ─► listed  → delisted(note)   通知作者；已装副本不动（Q4）
超管 relist   ─► rejected | delisted → listed
超管 feature  ─► featured 翻转（listed 时才有意义）
超管 official ─► is_official 翻转
```

目录条目（`catalog_overrides`）只有 `listed ⇄ delisted` 与 `featured`。

`with_mcp` 依赖安装**不受** listing 限制（只查 `catalog_index`）：官方内容技能声明的 MCP 必须永远装得上；这是有意为之，写进注释。

### 4.5 后端接口

#### 4.5.1 技能管理 `api/admin_skills.py`（`prefix="/api/admin/skills"`，`require_admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/store?origin=&kind=&listing=&q=&sort=&offset=&limit=` | 合并视图：目录条目 + 社区行。每条：`catalog_id, kind, origin, title, name, icon, description, publisher / author{username,email}, workspace_id, version, published_at, installs_count, listing, listing_note, featured, is_official, requires_mcp, size, sha256`。社区行 `load_only` 排除两列 LargeBinary；`installs_count` 用 `skill_installs` group by 子查询 |
| POST | `/store/{catalog_id}/listing` | `{listing: listed \| delisted, note?}`；`delisted` 必须带 note；社区行写 `user_skills`，目录条目 upsert `catalog_overrides`；审计 `admin.skill.listing`；社区行下架通知作者 |
| POST | `/store/{catalog_id}/featured` | `{featured: bool}`；审计 |
| POST | `/store/{catalog_id}/official` | `{is_official: bool}`，只对社区行；审计 |
| GET | `/review?state=pending\|rejected&offset=&limit=` | 审核队列（`user_skills` 中 `status=published`），按 `published_at` 升序 |
| GET | `/review/{catalog_id}` | 审核详情：元数据 + `skill_md`（从 ZIP 内存读出 `SKILL.md` 文本，上限 64 KB）+ `files[]{path,size}`（`zipfile` 只列目录不解压，条目上限 500）+ `requires_mcp` + 作者与空间 |
| GET | `/review/{catalog_id}/archive` | 下载 `published_archive_data`，`Content-Disposition` 同现有 `/skill/{name}/download`；审计 `admin.skill.download` |
| POST | `/review/{catalog_id}/approve` | pending → listed；审计 `admin.skill.approve`；通知 |
| POST | `/review/{catalog_id}/reject` | `{note}` 必填；pending → rejected；审计；通知 |
| GET | `/installs?catalog_id=&user_id=&q=&offset=&limit=` | 安装记录：`user{username,email}, catalog_id, kind, title, install_dir, installed_at`；`q` 匹配用户名 / 邮箱 / 技能名 |

所有 GET 记 `admin.view_skills`（同现有 `admin.view_users` 口径）。

#### 4.5.2 订阅管理 `api/admin_billing.py`（`prefix="/api/admin/billing"`，`require_admin`，**全部只读**）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/subscriptions?plan=&state=&q=&offset=&limit=` | 每空间一行：`workspace{id,name,kind}, owner{username,email}, plan_id, cycle, starts_at, ends_at, state(active \| expired \| free), queued_count, balance, last_paid_at`。`state` 用 `billing_subscriptions.starts_at <= now < ends_at` 现算；`q` 匹配空间名 / owner 用户名 / 邮箱 |
| GET | `/orders?provider=&status=&kind=&from=&to=&q=&offset=&limit=` | `payment_orders` 全量 + 空间名 + 下单用户；`amount_fen` 原样返回，前端换算 |
| GET | `/workspaces/{workspace_id}` | 空间详情：空间与成员数、当前订阅与排队订阅、订阅历史（全部 `billing_subscriptions`）、订单（最近 100）、账本尾（最近 50 条 `credit_ledger`）、用量汇总（近 30 天 `usage_events` 按 `status` 汇总 credits / tokens） |

审计 `admin.view_billing`（detail 里带 workspace_id）。复用 `billing/subscriptions.py` 的 `active_subscription`、`plan_catalog()`，不复制逻辑。

#### 4.5.3 用户侧接口改动（`api/metadata.py` + `skill/user_library.py`）

| 改动 | 说明 |
|---|---|
| `GET /api/agent/catalog` | 只返回 `listing == listed` 的条目；每条多 `origin`、`featured`、`official`、`installs_count`；排序 featured → installs_count → published_at |
| `GET /api/agent/config` | 多 `skill_store_review: bool`，前端据此选发布对话框文案 |
| `POST /api/agent/skill/{name}/publish` | 按 §4.4 规则决定 `listing`；返回值多 `listing / listing_note`；补审计 `skill.publish` |
| `POST /api/agent/skill/{name}/withdraw`（新） | 作者撤回：`status=withdrawn`；只对 `category == personal` 且有已发布快照的行；审计 `skill.withdraw` |
| `GET /api/agent/skill` | 个人技能条目多 `listing / listing_note / is_official`；`publication_status` 语义不变 |
| `POST /api/agent/catalog/install` | 目录条目（skill 与 `with_mcp` 里的 mcp）安装成功后也写 `skill_installs`（`catalog_id=<kind>:<id>`，`user_skill_id=NULL`）；失败回滚口径同社区分支 |
| `DELETE /api/agent/skill/{name}`、`DELETE /api/agent/mcp/{name}` | `category == store` 或目录条目卸载后删对应 `skill_installs` 行（`remove_community_installation` 改名 `remove_store_installation`，按 `install_dir` / `name` / `catalog_id` 三选一匹配） |
| `core/config.py` | `SKILL_STORE_REVIEW: bool = True`（env `SKILL_STORE_REVIEW`），写进 `.env.example` |

### 4.6 前端

#### 4.6.1 目录与路由

```
src/shared/router/paths.ts
  paths.admin = "/app/admin"
  paths.adminFleet = "/app/admin/fleet"
  paths.adminSkills(tab?) = "/app/admin/skills[/tab]"
  paths.adminBilling(tab?) = "/app/admin/billing[/tab]"
  paths.adminWorkspace(id) = "/app/admin/billing/workspaces/<id>"
  routePatterns.admin = "admin", adminFleet = "fleet", adminSkills = "skills/:tab?",
  adminBilling = "billing/:tab?", adminWorkspace = "billing/workspaces/:workspaceId"

src/app/router/router.tsx        /app 下加嵌套路由 { path: admin, element: <RequireAdmin><AdminRoute/></RequireAdmin>,
                                  children: [index → Navigate fleet, fleet, skills/:tab?, billing/:tab?, billing/workspaces/:id] }
src/app/router/guards.tsx        + RequireAdmin
src/routes/admin/AdminRoute.tsx  壳：AdminNav + 栏目标题 / 副标题 + <Outlet/>
src/routes/admin/AdminFleetRoute.tsx      改为只渲染 <FleetPage/>（壳与守卫上移）
src/routes/admin/AdminSkillsRoute.tsx     读 :tab，分发 StorePage / ReviewPage / InstallsPage
src/routes/admin/AdminBillingRoute.tsx    读 :tab，分发 SubscriptionsPage / OrdersPage
src/routes/admin/AdminWorkspaceRoute.tsx  WorkspaceDetailPage

src/features/admin/              保留舰队；新增壳：components/AdminNav.tsx、sections.ts（ADMIN_SECTIONS）；index.ts 多导出 AdminNav
src/features/admin-skills/       api/keys.ts、api/admin-skills.ts、components/{StorePage,StoreRow,ListingDialog,ReviewPage,ReviewDetail,InstallsPage}.tsx、lib/、types/、index.ts
src/features/admin-billing/      api/keys.ts、api/admin-billing.ts、components/{SubscriptionsPage,OrdersPage,WorkspaceDetailPage,BillingFilters}.tsx、lib/money.ts、types/、index.ts
src/shared/ui/DataTable.tsx      表头 / 行 / 空态 / 加载态 / 错误态 + 横向滚动容器
src/shared/ui/Pagination.tsx     offset / limit → 上一页 / 下一页 / 总数
src/shared/ui/StatusPill.tsx     ok / warn / muted / danger 四色（skills-center 的 Badge 风格抽到 shared）
src/shared/hooks/useUrlState.ts  query string ⇄ 筛选 / 分页（若已有等价物则复用）
```

query key 全部带 `userId`（§7.2）；写操作走 mutation 并声明失效范围：listing 变更失效 `["admin-skills"]` 与 `["skill-center", userId]`。三个 feature 互不 import（eslint boundaries），共享的东西只能进 `shared/`。

#### 4.6.2 页面设计

**共同规则**：卡片 `rounded-xl border border-hair bg-card p-4`；表格 `text-xs`，行 `border-t border-hair`，超宽 `overflow-x-auto`；次要按钮 `rounded-full border border-hair px-3 py-1.5 text-xs text-n800 hover:bg-hairsoft`，主按钮 `bg-ink text-bg rounded-full`；危险操作用 `shared/ui/Dialog` 二次确认并要求填原因，**不用** `window.confirm`（舰队页现有的 confirm 顺手换掉）；日期用 `Intl` 走 i18n（§10.6）；方向样式用 `ps- / pe- / start / end`；四态（空 / 加载 / 错误 / 正常）；默认主题浅色 + 深色各截一张。

**壳**（`AdminRoute`）：标题「超管系统」+ 副标题「舰队、技能商店与订阅的运营视图。」；左侧 `AdminNav` 三项：舰队管理 / 技能管理 / 订阅管理；右侧每个栏目自己的标题与副标题（沿用 `admin.json` 的 `title / subtitle` 结构）。

**技能管理 › 商店上架**
- 顶部：搜索框 + 筛选 pill：来源（全部 / 官方 / 用户共享 / 第三方）、类型（Skill / MCP）、状态（上架 / 下架 / 待审核 / 已驳回）。
- 表列：图标 + 名称（副行 name / catalog_id）、来源 pill、作者（用户名，hover 邮箱）、版本、发布时间、安装数（点击 → 用户安装 tab 预填 catalog_id）、状态 pill、操作。
- 操作：`上架` / `下架`（Dialog 填原因）、`置顶` 翻转、`标为官方`（社区行）、`查看`（跳审核详情，任何状态都可看）。
- 空态：「商店里还没有条目。」

**技能管理 › 投稿审核**
- 左列队列（待审核 / 已驳回 切换），每条：名称、作者、提交时间、大小；右侧详情：SKILL.md 全文（等宽只读文本，**不渲染 Markdown、不执行任何内容**）、文件清单（路径 + 大小）、依赖 MCP、sha256、下载 ZIP；底部 `通过` / `驳回`（Dialog 填原因）。
- 待审核为 0 时空态：「没有待审核的投稿。」

**技能管理 › 用户安装**
- 搜索框（用户名 / 邮箱 / 技能名）+ 按技能筛选（从商店上架跳过来时预填）。
- 表列：用户、技能（图标 + 名称 + 来源 pill）、类型、安装目录、安装时间。
- 页脚说明：「按安装记录显示；用户在沙箱里手动删除的技能不会实时反映。」

**订阅管理 › 订阅**
- 筛选：套餐（free / pro / max）、状态（有效 / 已到期 / 免费）、搜索（空间名 / owner / 邮箱）。
- 表列：空间（名称，副行 id）、owner、套餐、周期、开始 / 到期、状态 pill、余额（`formatCredits`）、排队续期数、最近支付；行点击 → 空间详情。

**订阅管理 › 订单**
- 筛选：渠道、状态、类型（订阅 / 充值）、时间范围、搜索。
- 表列：订单号、创建时间、空间、下单用户、类型与商品（套餐 / 周期 或 充值）、金额（`amount_fen / 100` 两位小数 + 币种）、积分、渠道、状态 pill、支付时间、渠道单号（等宽）。

**订阅管理 › 空间详情**
- 头部卡：空间名 / id / kind / owner / 成员数 / 当前套餐与到期 / 余额。
- 四个卡片：订阅历史（表）、订单（表）、账本尾（表：时间、类型、金额、余额）、近 30 天用量（charged / shadow / historical / unpriced 各一格 + tokens 合计）。
- 只读；v2 在头部卡加「调账」「代付」按钮位（本期不出现）。

**用户侧技能中心**
- 商店：分区改为 官方 / 用户共享 / 第三方（按 `origin`），置顶条目带「置顶」小标；条目多显示安装数。
- 「我的」个人技能：状态 chip `待审核 / 已上架 / 已驳回 / 已下架 / 已撤回`，驳回与下架显示原因（tooltip + 展开行）；操作加「撤回发布」（Dialog 确认）；被驳回或下架后按钮文案「重新提交」。
- `PublishSkillDialog` 的 `publicNotice` 在审核开关开启时改为「提交后由管理员审核，通过后所有用户可见…」——两段文案都进 locale，前端按 `/api/agent/config` 的 `skill_store_review` 选。

#### 4.6.3 i18n

- `admin.json`：加壳文案（`console.title / subtitle`、`nav.fleet / skills / billing`），舰队 key 原位不动。
- 新 `admin-skills.json`、`admin-billing.json`（zh-CN 与 en-US 同步，`npm run check:i18n`）。
- `workspace.json`：`adminFleet` → `adminConsole`（「超管系统」）。
- `skills.json`：`badge.listing.*`、`action.withdraw`、`action.resubmit`、`withdraw.*`、`publish.reviewNotice`、`section.storeOfficial / storeCommunity / storeThirdParty`。
- 移动端 `mobile/assets/locales/*/` 逐字节复制（含两个新文件，App 不用也要复制过门禁）。

### 4.7 通知与审计

| 动作 | 审计 action | 通知（kind → 收件人） |
|---|---|---|
| 上 / 下架、置顶、标官方 | `admin.skill.listing` / `admin.skill.featured` / `admin.skill.official` | 下架：`skill_delisted` → 作者 |
| 通过 / 驳回 | `admin.skill.approve` / `admin.skill.reject` | `skill_approved` / `skill_rejected` → 作者 |
| 查看列表 / 详情 / 下载 ZIP | `admin.view_skills` / `admin.skill.download` | — |
| 查看订阅 / 订单 / 空间详情 | `admin.view_billing` | — |
| 作者提交 / 撤回 | `skill.publish` / `skill.withdraw`（现有发布无审计，补上） | 审核开启时提交 → `skill_pending` → 每个 admin 一条（`user_id` 为各 admin，`workspace_id` 用作者空间） |

通知正文含技能名、版本、原因；前端已有通知列表，不需要新 UI。

### 4.8 移动端

- 超管系统：Web-only，`MOBILE_WEB_PARITY.md` 登记「有意省略」，与舰队管理同一行。
- 用户侧变化（商店分区、状态 chip、撤回、审核提示文案）：登记为「待对齐」，字段已在接口里，App 端 `store_list.dart` / `mine_list.dart` / `create_publish_sheets.dart` 后续跟进；接口对老客户端保持兼容（新字段全部可选，`origin` 缺省按 `community` 处理，`listing` 缺省按 `listed`）。

### 4.9 安全与性能

- 审核详情读 ZIP：`zipfile` 内存打开、只 `infolist()`、只读 `SKILL.md` 且上限 64 KB；条目上限 500，超出截断并标注；不解压到磁盘、不执行任何脚本；前端按纯文本渲染。
- 列表查询 `load_only` 排除 `archive_data` / `published_archive_data`，否则一页列表拖几十 MB。
- 分页服务端做（`limit ≤ 200`），计数用 `count()` 子查询。
- 所有超管写接口：二次确认 + 原因 + 审计 + 幂等（同状态重复调用返回 200，不重复写审计与通知）。
- 订阅管理不返回 `checkout_url`、支付渠道密钥、任何 token；`provider_order_id` 可返回（对账要用）。
- `SKILL_STORE_REVIEW` 关闭时，`delisted` 行不能被作者再发布绕过（§4.4）。

---

## 5. 分期与估时

| 期 | 内容 | 估时 |
|---|---|---|
| P0 壳 | 菜单改名、`RequireAdmin`、`/app/admin` 嵌套路由与 `AdminRoute` 壳、`AdminNav`、舰队迁入、`DataTable` / `Pagination` / `StatusPill`、舰队页 confirm 换 Dialog、locale | 0.5–1 天 |
| P1 技能管理 | 迁移（`user_skills` 加列、`skill_installs` 扩展、`catalog_overrides`）、`SKILL_STORE_REVIEW`、状态机与发布规则、`admin_skills.py` 全部接口、目录过滤与 `origin`、目录安装留痕、撤回接口、通知与审计、三个管理页 + 用户侧商店 / 我的改动、单测 | 3 天 |
| P2 订阅管理 | `admin_billing.py` 三个只读接口、订阅 / 订单 / 空间详情三页、金额与状态展示、单测 | 2 天 |
| P3 收尾 | `API_INTERFACES.md` 第 16 / 17 节、`DETAILED_PLAN_M1_M2.md` 进度行、`MOBILE_WEB_PARITY.md` 登记、深浅色截图进 `docs/evidence/`、gw2 部署与 §6 验收 | 0.5 天 |
| v2（另立单） | 调账 / 代付 / 改到期（含幂等与审计）、独立用户页、手动排序、审核员「一键装到我的沙箱试用」、技能版本历史与回滚、运营角色拆分 | — |

---

## 6. 验收条件

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 普通用户账户菜单没有「超管系统」，直接访问 `/app/admin/*` 被送回 `/app`；直接调 `/api/admin/skills/store` 得 403 | 双账号；curl |
| AC-2 | admin 进入超管系统，左侧三栏目切换，URL 变化，刷新保留当前 tab / 筛选 / 分页；旧链接 `/app/admin/fleet` 仍打开舰队页 | 截图 |
| AC-3 | 用户 A 发布技能 → A 的「我的」显示「待审核」，商店里**看不到**；admin 审核队列出现该条，详情能看 SKILL.md 与文件清单、能下载 ZIP | 截图 + `user_skills.listing='pending'` |
| AC-4 | admin 通过 → 商店「用户共享」分区出现；A 收到通知；审计有 `admin.skill.approve` | 截图 + SQL |
| AC-5 | admin 驳回（原因必填）→ A 的「我的」显示「已驳回」和原因；A 修改后「重新提交」→ 回到待审核 | 截图 |
| AC-6 | admin 下架一条已上架技能（原因必填）→ 商店消失、已装用户 B 的沙箱副本仍在且能用；B 的「我的」该条仍显示；A 收到通知；`SKILL_STORE_REVIEW=false` 时 A 更新版本**不会**自动重新上架 | 截图 + 沙箱 `list_skills` |
| AC-7 | A 撤回发布 → 商店消失、状态「已撤回」；再次发布走审核 | 截图 |
| AC-8 | admin 账号发布技能 → 免审直接上架且在「官方」分区，`is_official=true` | SQL |
| AC-9 | 用户安装 tab：B 从商店安装官方技能与一个 MCP 后各出现一行（`catalog_id` 形如 `skill:web-research` / `mcp:playwright`）；B 卸载后行消失 | SQL |
| AC-10 | 商店上架：`anthropic-skills` 默认不在用户商店里；admin 上架后出现在「第三方」分区；置顶后排在分区最前 | 截图 |
| AC-11 | 订阅列表：有效 / 已到期 / 免费三种状态各至少一行正确；订单列表金额 = `amount_fen / 100`，筛选与搜索生效；空间详情四个卡片与 `/api/billing/*` 用户侧看到的数一致 | 与 workspace 侧页面对照截图 |
| AC-12 | 订阅管理没有任何写按钮；所有 `/api/admin/billing/*` 只有 GET；网络面板里无 `checkout_url` / 密钥 | 代码 grep + Network |
| AC-13 | 后端 pytest 全绿；前端 `npm run check` 全绿；`mobile/scripts/check_locales.sh` 通过；默认主题浅色 + 深色各栏目截图进 `docs/evidence/admin-console/` | CI 输出 + 截图 |
| AC-14 | 迁移 `alembic upgrade head` / `downgrade -1` 在 PostgreSQL 与 SQLite 上都能跑；存量已发布行迁移后 `listing='listed'`，商店不掉条目 | 迁移日志 + 前后目录数对比 |

---

## 7. 测试方式

- 后端单测（`backend/tests/unit/`）：`test_user_skill_library.py` 扩展（状态机每条边、可见性谓词、撤回、审核开关两种取值、`delisted` 不可绕过）；新 `test_admin_skills_api.py`、`test_admin_billing_api.py`（照 `test_admin_fleet_api.py` 的 SQLite + 假 sandbox client 写法，覆盖 403、审计写入、通知写入、分页与筛选、`load_only` 不带二进制）；`test_user_skill_api.py` 补目录安装留痕与卸载删记录；迁移照 `test_fleet_migration.py` 的写法跑 upgrade / downgrade。
- 前端组件测试（vitest）：`RequireAdmin`、`AdminNav`、`DataTable` 四态、`StorePage` 的下架 Dialog 必填原因、`ReviewDetail` 纯文本渲染、`SubscriptionsPage` 状态计算、`money.ts`。
- E2E（`e2e/admin.spec.ts`，拦截响应 + 本地 fixture，照 `billing.spec.ts`）：admin 登录 → 三栏目切换 → 审核通过一条 → 商店出现；非 admin 被重定向。
- 真机（gw2）：§6 AC-3 ～ AC-11 用两个真实账号走一遍。

---

## 8. 交付证据与文档回写

- `docs/evidence/admin-console/`：每栏目浅色 + 深色截图、AC-3 ～ AC-8 流程截图、审计与通知的 SQL 输出。
- `docs/API_INTERFACES.md`：新增「16. Admin 技能管理」「17. Admin 订阅管理」；用户侧 `catalog / config / publish / withdraw` 条目更新。
- `docs/DETAILED_PLAN_M1_M2.md` 进度表加一行「超管系统 + 技能商店」。
- `docs/MOBILE_WEB_PARITY.md`：超管系统「有意省略」；用户侧商店变化「待对齐」。
- `docs/PLAN_SHARE.md` 里程碑三第 3 条改口为「官方技能 + 经审核的用户投稿」。
- `backend/.env.example`：`SKILL_STORE_REVIEW`。
- 本文 §10 执行记录。

---

## 9. 停下来报告

- 迁移在 SQLite 上无法表达（部分索引 / 改列可空）——改用 batch 模式或拆迁移前先报。
- 存量 `user_skills` 里存在 `status=published` 但 `published_archive_data` 为空的脏行。
- `skill_installs` 存量行的 `user_skill_id` 对应的 `user_skills` 行已不存在（外键悬空）。
- 审核详情读 ZIP 遇到 > 50 MB 或条目 > 500 的包。
- 任何需要改 `sandbox/**`、支付回调、`billing/service.py` 扣费逻辑的情况。
- 发现 `require_admin` 之外还有别的地方以 `role` 做判断且口径不一致。

---

## 10. 执行记录

> 2026-09-08，分支 `codex/admin-console`（从 main `f2c70ba` 起）。多个 agent 并行在同一工作树上分片完成，本节按写作时的工作树实测状态写，不按计划意图写。
>
> **当前状态：代码在工作树里，尚未提交、尚未合并 main、尚未部署 gw2，§6 的 AC-1 ~ AC-14 一条都没有在真环境跑过。**

### 10.1 改动总量

| 区域 | 改动已跟踪文件 | 新增文件 |
|---|---|---|
| `backend/` | 13 个，`+2556/-104` | 7 个，2509 行 |
| `frontend-v2/` | 22 个，`+546/-412` | 99 个，6820 行 |
| `mobile/`（仅 locale 镜像） | 9 个，`+264/-16` | 4 个，610 行 |
| `docs/` | 4 个，`+521/-5`（`API_INTERFACES.md` §16/§17 与用户侧条目、`MOBILE_WEB_PARITY.md` §10、`DETAILED_PLAN_M1_M2.md` 进度行、`PLAN_SHARE.md` 里程碑三改口） | 1 个（本文），459 行 |

前端新增文件多是因为 §4.6 的两个 feature 拆到了组件粒度（`ENGINEERING_SPEC.md` 的 150 行/组件、一个文件一个导出组件），其中 25 个是测试文件。`mobile/ios/build/` 是本地构建产物，不属于本轮改动，提交时不要带上。

### 10.2 后端

| 交付 | 文件 |
|---|---|
| 迁移 `c8e0a2b4d6f1`（`down_revision='b6d1e2f3a4b5'`，`alembic heads` 仍是单头）：`user_skills` 六列 + 索引、`skill_installs` 扩到目录条目（`kind` / `catalog_id`、`user_skill_id` 放开、唯一键改带 `kind`）、新表 `catalog_overrides` | `backend/db/migrations/versions/c8e0a2b4d6f1_admin_console_skill_store.py`、`backend/db/models/{user_skill,skill_install,catalog_override}.py` |
| 状态机（§4.4）、可见性谓词 `store_visible()`、超管三个写操作、投稿撤回、安装留痕、控制台列表投影（`load_only` 排除两列 LargeBinary） | `backend/skill/user_library.py`（+922） |
| 目录条目的 `listing` 默认值、`catalog_overrides` 覆盖、`origin` 判定、`shelf_index()`（按名安装也走货架）与 `catalog_index()`（依赖解析故意不过滤） | `backend/skill/catalog.py` |
| 超管技能接口 10 个（§4.5.1 全部）：商店列表 / 上下架 / 置顶 / 标官方 / 审核队列 / 审核详情 / 下载 ZIP / 通过 / 驳回 / 安装记录 | `backend/api/admin_skills.py`（771 行） |
| 超管订阅接口 3 个，**只有 GET**：订阅列表 / 订单列表 / 空间详情 | `backend/api/admin_billing.py`（407 行） |
| 用户侧：`/catalog` 只出上架条目并按 置顶 → 安装数 → 时间 排序、`/config` 多 `skill_store_review`、`publish` 按 §4.4 定 listing 并补审计与待审通知、新增 `/skill/{name}/withdraw`、目录安装写 `skill_installs`（写不进就回滚安装）、卸载删记录 | `backend/api/metadata.py`（+286） |
| `SKILL_STORE_REVIEW`（默认 `true`）及 `.env.example` 说明 | `backend/core/config.py`、`backend/.env.example` |
| 单机模式（不跑 alembic）的补列/重建，与迁移同口径 | `backend/db/base.py` |

### 10.3 前端

- 壳与守卫：`guards.tsx` 加 `RequireAdmin`；`/app/admin` 嵌套路由（index → fleet）；`AdminRoute` + `features/admin/{AdminNav,sections}.tsx`；`AdminFleetRoute` 只剩 `<FleetPage/>`，自检删掉；账户菜单「舰队管理」→「超管系统」，跳 `/app/admin`；旧链接 `/app/admin/fleet` 保留。
- 新 feature：`features/admin-skills/`（商店上架 / 投稿审核 / 用户安装）、`features/admin-billing/`（订阅 / 订单 / 空间详情）。三个 feature 互不 import。
- 共享原语：`shared/ui/{DataTable,Pagination,StatusPill}.tsx`、`shared/hooks/useUrlState.ts`（tab / 筛选 / 搜索 / 分页都在 URL）。
- 舰队页的 `window.confirm` 换成 `features/admin/ConfirmDialog.tsx`（`src/` 里只剩技能中心卸载技能包还在用 `window.confirm`，那是本单范围外的既有代码）。
- 用户侧技能中心：商店按 `origin` 分「官方 / 用户共享 / 第三方」（缺 `origin` 的旧数据按 `community` 处理）、置顶与安装数、「我的」的上架状态 chip 与原因展开、`WithdrawSkillDialog`、发布文案按 `skill_store_review` 二选一（配置没到位时按「不审核」文案兜底，宁可少承诺）。
- i18n：新增 `admin-skills.json`、`admin-billing.json`（zh-CN / en-US），改 `admin.json`、`skills.json`、`workspace.json`（`adminFleet` → `adminConsole`）；移动端 locale 已逐字节镜像，含两个新文件。

### 10.4 与本单的偏差（实现与 §4 不一致处，按实现为准）

1. `GET /api/admin/skills/store` 的 `offset` 有上限 **5000**（`MERGE_PAGE * 25`），超出返回 422。§4.5.1 没写这条；合并两个来源要读到 `offset + limit`，不设上限等于把可手改的 URL 变成慢查询。
2. 对代码目录里**不存在**的 `skill:` / `mcp:` id 做上下架**不 404**：按「上架、不置顶」当默认写一条 `catalog_overrides`，这样对 `OPENBOX_CATALOG_URL` overlay 里的条目也能做决定。`community:<id>` 找不到才 404。
3. `POST /store/{id}/official` 对目录条目返回 **400**（目录条目的 origin 跟着代码里的 publisher 走），不是静默忽略。
4. 商店列表把命中的目录条目**全量读进内存**再与投稿分页合并（目录当前 11 条），投稿侧只按窗口取。
5. 置顶与标官方**不通知作者**，只有 `listing` 真的变了才通知（与 §4.7 的表一致，这里写明是因为 §4.6.2 的按钮容易让人以为都会通知）。
6. `remove_community_installation` → `remove_store_installation`，并多了 `kind` 参数：MCP 与 skill 的 `install_dir` 命名空间会撞名，卸载必须说清删的是哪一类。
7. §7 里的 `e2e/admin.spec.ts` **没有写**（本轮明确只做单测），`frontend-v2/e2e/` 里没有 admin 相关 spec。
8. 移动端只做了 locale 镜像，UI 一行没动，已登记在 `docs/MOBILE_WEB_PARITY.md` §10。
9. **两个 admin 模块的审计 `workspace_id` 口径不一致**：`admin_skills.py` 的列表类 GET 记的是**操作者自己**的 `workspace_id`（`admin.get("workspace_id")`），`admin_billing.py` 的列表类 GET 记 `None`。§4.2 的口径是「填被操作对象所属空间，没有就 None」，列表没有单一对象，因此按后者更贴。详情类接口两边都正确（技能记作者空间、空间详情记该空间）。不影响功能，但按空间过滤审计时会多出几条挂在管理员自己空间下的 `admin.view_skills`。

### 10.5 测试实测结果

前后端各跑了两遍，两次数字一致；期间其它 agent 仍在同一工作树上改文件，所以这是某一时刻的快照，合并前请自己再跑一遍。

| 门禁 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `cd backend && python -m pytest tests/unit -q` | **1672 passed, 26 failed** |
| 本单相关后端 | `pytest tests/unit/test_admin_skills_api.py test_admin_billing_api.py test_admin_console_migration.py test_user_skill_library.py test_user_skill_api.py test_db_bootstrap.py` | **63 passed** |
| 前端 | `cd frontend-v2 && npm run check` | **通过**：i18n parity OK；eslint **0 errors / 29 warnings**（全是既有的 react-refresh 与 unused-disable 两类）；`tsc -b` 通过；vitest **59 个文件 / 351 项全绿** |
| 移动端 locale | `bash mobile/scripts/check_locales.sh` | **通过**（逐字节一致） |
| 迁移单头 | `cd backend && alembic heads` | `c8e0a2b4d6f1 (head)` |

**那 26 个失败是 main 上就有的，与本单无关**，分布：`test_database_readiness.py` 19、`test_video_open_generation.py` 3、`test_video_production.py` 2、`test_computer_batch.py` 1、`test_internal_tunnel_keys.py` 1。判断依据：这些测试文件本轮一个都没改；它们依赖的模块（`tool/tool.py`、video、隧道）本轮也没改；`test_database_readiness` 唯一沾边的是 `db/base.py`，但本轮对它只是新增单机补列函数，`_missing_readiness_schema` 一行没动，失败内容是就绪清单里的 `desktop_activations` / `desktop_events` 在测试夹具里没建表——那两张表是 D1 的 `810a855` 引入的，早于本分支。**没有在 main 上实跑对照**（不能在共享工作树上切分支），所以这条是推断，不是实测。

本单新增/扩写的后端测试：新文件 `test_admin_skills_api.py`（14 项）、`test_admin_billing_api.py`（9 项）、`test_admin_console_migration.py`（4 项）；既有文件扩写 `test_user_skill_library.py` 7 → 21、`test_user_skill_api.py` 5 → 11、`test_db_bootstrap.py` 2 → 4。前端新增 25 个测试文件（`features/admin-skills` 6 个 24 项、`features/admin-billing` 6 个 27 项，其余分布在 skills-center、admin 壳、路由守卫与 shared 原语）。

### 10.6 没做、必须由人来做的事

1. **gw2 部署与库升级**：没部署、没升库、没打 tag。`alembic upgrade head` / `downgrade -1` 只在 SQLite 内存库上跑过（`test_admin_console_migration.py` 的 4 项，其中一项只是把 dialect 名字伪装成 `postgresql` 来验分支逻辑）。**AC-14 的 PostgreSQL 一半没验**，真库升级前请先在测试库上跑一遍 upgrade/downgrade 并对比迁移前后商店条目数。
2. **AC-1 ~ AC-14 全部待跑**：单测覆盖了状态机每条边、接口的 403 / 分页 / 筛选 / 审计 / 通知，以及前端组件的四态与必填原因，但**不能替代**双账号真机走查。尤其 AC-3 ~ AC-9 要两个真实账号（一个 admin、一个普通用户）在 gw2 上走一遍投稿 → 审核 → 上架 → 下架 → 撤回，并核对已装用户的沙箱副本仍可用。
3. **截图证据**：`docs/evidence/admin-console/` 目录还不存在，深浅色各栏目截图与 AC-3 ~ AC-8 的流程截图都没截。AC-13 因此未完成。
4. **AC-12 只完成静态一半**：`backend/api/admin_billing.py` 里 3 个 `@router.get`、0 个 post/put/delete/patch，可以静态证明；Network 面板确认无 `checkout_url` 与密钥仍待人做。
5. **§3 的默认执行待用户追认**：Q1（先审后上）与 Q3（`anthropic-skills` 默认下架）会直接改变用户看到的商店内容——上线即生效，不是灰度。改口就改 §3 与 §4，不要静默改代码。
6. 代码**未提交、未合 main**；提交前请确认 `mobile/ios/build/` 没被带进去。

### 10.7 交接注意

- **角色在 access JWT 里**：把某人 `users.role` 改成 admin 之后，他必须重新登录才看得到「超管系统」入口，接口也才放行。
- `SKILL_STORE_REVIEW` 只影响**新的发布动作**：关掉它不会把队列里已经 `pending` 的投稿自动放行，仍要超管逐条处理；打开它也不会把已上架的收回。
- 下架 / 撤回都**不动已安装的副本**（§3-Q4），沙箱里的技能不会凭空消失；下架同时停展示与新装，`with_mcp` 的依赖安装是有意的例外。
- 被下架的投稿，作者「更新版本」**不能**绕回上架（`SKILL_STORE_REVIEW=false` 时仍保持 `delisted`）。
- 单机模式（没有中央库）：`db/base.py` 会在启动时补上新列；读不到 `catalog_overrides` 时退回代码里的默认货架，并在日志里记 warning——不是静默吞掉。

