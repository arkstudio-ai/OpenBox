# 前端操作优化：新对话 / 返回主页 / Profile 页 —— 规划

> 2026-09-09。分支 `fe-nav-profile`（从 `origin/main` `36f0683` 起，工作树 `/Users/wxy/openbox-fe-nav`）。
> 只动 `frontend-v2/`（主推前端）；`frontend/` 是旧版参考，不动。移动端对齐另立。
> 工程约束：`frontend-v2/docs/ENGINEERING_SPEC.md`（路径常量化 §8.2、URL 即状态 §8.4、feature 边界 §4、i18n 门禁 §10.8）。

---

## 1. 三个问题

1. **只有"新建项目"，没有"新对话"。** 侧栏顶部主按钮是"新建项目"（弹一个输入框），开对话只能靠项目行 hover 出来的小"+"或项目菜单里的"新建对话"。新用户找不到"我要开始聊"这个入口。
2. **各中心没有回主页的路。** 资源中心 / 授权中心 / 技能中心 / 定时任务 / 订购 / 设置 / 舰队管理进去以后，顶栏没有返回，侧栏 logo 不可点，只能点某条历史会话或项目行的"+"才能出来。
3. **没有 Profile 页。** 用户行（侧栏底部）点名字跳"用量"，"…"菜单只有 设置 / 舰队管理 / 退出；设置里的"账号"tab 只有 4 行只读字段。用户没有一个地方看到"我是谁、我有什么"。

三者其实是一件事：**"新对话" = 回到 `/app`（问候 + 输入框）= 主页**。做出这个入口，再让每个中心都能回到它，Profile 页则是从用户行进去的另一个"中心"。

---

## 2. 现状（main `36f0683`）

### 2.1 路由与入口

| 事实 | 位置 |
|---|---|
| `/app` 索引路由 = `EmptyChatRoute`（问候语 + 建议 + 输入框，首次发送才建会话）。**它就是主页**，但没有任何常驻入口指向它 | `app/router/router.tsx:61`、`routes/workspace/EmptyChatRoute.tsx` |
| 侧栏主按钮"新建项目"只开草稿输入框 | `features/workspace/components/Sidebar.tsx:96-108` |
| "新建对话"只在项目行 hover 的"+"与项目菜单里，跳 `/app?project=<id>` | `features/workspace/components/ProjectRow.tsx:98-108, 133-141` |
| `BrandMark` 纯展示，不可点 | `shared/ui/BrandMark.tsx` |
| 切换工作空间会 `navigate(paths.app)`——目前唯一"回主页"的代码路径 | `WorkspaceSwitcher.tsx:22` |
| 顶栏没有返回；页面识别靠 `pathname.includes("/settings")` 一串字符串判断，**技能中心和舰队管理不在判断里**，这两页顶栏显示"未命名对话 · 未归类" | `features/workspace/components/Topbar.tsx:32-52` |
| 侧栏只有"订购"一行有 `aria-current` 高亮，其他中心进入后侧栏无当前态 | `Sidebar.tsx:170-181` |
| `EmptyChatRoute` 只读 `?project=`，不回退到侧栏当前选中项目；也**没把项目名传给 `EmptyState`**，提示永远是"这条对话会存在「未归类」下" | `EmptyChatRoute.tsx:14,27`、`features/chat/components/EmptyState.tsx:21` |
| 文案 key `workspace.newChat`（"新对话"）与 `workspace.backToChat`（"返回对话"）已存在但**无人使用** | `locales/*/workspace.json:5,29` |

### 2.2 设计稿本来是怎样的

`frontend-v2/design-reference/Agent 聊天.dc.html`：

- 侧栏顶部主按钮是 **"新对话"**（`newChatHere`，accent-200 底色的圆角胶囊），"新建项目"是其下的普通行。
- 设置页顶栏右侧有 **"返回对话"** 文字链接（`isSettings && backToChat`），点了 `screen: 'chat'`。
- 设置导航为：账号 / **订阅与积分** / 模型 / 工具与权限 / 浏览器 / 外观。

`6bdcd76`（08-20，"make projects the unit the workspace is organised around"）把项目提成组织单位时，把顶部主按钮换成了"新建项目"，"新对话"下沉到项目行，"返回对话"没有实现。项目为单位的决定是对的（共享工作目录、快照、回收都依赖它），但入口层级换错了：项目是**容器**，对话才是**高频动作**。

### 2.3 用户身份与相关数据（Profile 页可用的既有接口）

| 数据 | 接口 / hook | 备注 |
|---|---|---|
| 用户 | `GET /api/auth/me` → `_safe_user(_to_dict(user))` | 实际返回 `id, username, email, avatar_url, role, oauth_provider, default_workspace_id, is_active, monthly_cost_limit, created_at, updated_at…`（只剔 `password_hash`）。**前端 `AuthUser` 类型只声明了 `id/username/email/role`**，头像等字段被类型丢掉 |
| 头像来源 | Logto `picture` claim → `users.avatar_url`，**仅首次建用户时写入**（`auth/routes.py:330,344`） | 没有改昵称/头像的接口；`PgUserRepo.update(**fields)` 通用更新已存在 |
| 偏好 | `GET/PUT /api/auth/me/preferences`（`theme, default_model, default_agent, sidebar_open, extra`） | `features/settings/api/settings.ts` |
| 积分 / 套餐 / 用量 / 订单 | `useCreditBalance, useBillingSubscription, useBillingSummary, useUsageEvents, usePaymentOrders` | `shared/api/billing.ts` |
| 工作空间 | `useWorkspacesQuery`（列表+默认）、`useCurrentWorkspaceQuery`（成员+邀请）、`usePendingInvitationsQuery`、`acceptInvitation` | `shared/api/workspaces.ts`；`kind: personal/team`，`role: owner/admin/member` |
| 云桌面 | `/api/desktop/status`、`/api/desktop/provision` | `shared/api/desktop.ts`；09-03 拍板桌面归 workspace，一订阅一台 |
| 平台授权 | `features/auth-center/api/platform-accounts.ts` | 抖音 OAuth + 云电脑登录态 |
| 会话数 / 项目数 | `useSessionsQuery, useProjectsQuery` | 侧栏已有，`userLine` 文案已在用 |
| 管理员 | `user.role === "admin"` → `/app/admin/fleet`；`/api/admin/users` | `UserRow.tsx:38` |

移动端 `mobile/lib/features/settings/` 已有 `account_section.dart`（只读行），结构与 Web 设置页一致，Profile 做完后可对齐。

---

## 3. 方案

### 3.1 A 线：新对话 = 主页，所有中心可返回

**A1 主页语义固定为 `/app`。** 不新增 `/app/home` 之类的路由。`EmptyChatRoute` 改成：
- 目标项目 = `?project=` → 侧栏当前选中项目（`useWorkspaceUi.selectedProject`，且仍存在）→ 未归类。
- 把项目名传给 `EmptyState`，修掉"永远未归类"的提示。
- 首次发送后 `navigate(paths.chat(id))` 不变。

**A2 侧栏顶部主按钮改回"新对话"**，按设计稿做 accent 胶囊；点击 = `navigate(paths.newChat(selectedProject))`。"新建项目"降为第二行（普通行 + `Plus` 图标），草稿输入框逻辑不变。
- `paths.ts` 增加 `newChat: (projectId?) => projectId ? "/app?project=…" : "/app"`，`ProjectRow` 两处手拼的 `${paths.app}?project=` 改用它（消灭硬编码，§8.2）。
- 复用已存在的 `workspace.newChat` 文案 key。

**A3 Logo 可点回主页。** `Sidebar` 里的 `BrandMark` 包一层 `Link to={paths.app}`；`BrandMark` 本身保持纯展示。

**A4 顶栏"返回对话"。** 所有非会话页（设置 / 订购 / 定时任务 / 资源中心 / 授权中心 / 技能中心 / 舰队管理 / Profile）顶栏右侧出"返回对话"（设计稿位置，复用 `backToChat` key，`ArrowLeft` 图标 + 文字）。目标：
- `useWorkspaceUi.lastSessionId`（新增，持久化；`ChatRoute` 挂载时写）仍在会话列表里 → `paths.chat(lastSessionId)`；
- 否则 → `paths.app`。
- 不用 `navigate(-1)`：从 Logto 回跳或直接打开链接时历史栈不可靠。

**A5 顶栏页面识别重写。** 把 `Topbar.tsx:32-52` 那串 `includes()` 换成一张 `useMatch` 表：`{ pattern, title, subtitle }[]`，补上技能中心、舰队管理、Profile 三页；`standalone` 由这张表得出，`statusSlot` / 面板按钮的显隐规则跟着它。

**A6 侧栏当前态。** 资源中心 / 授权中心 / 技能中心 / 定时任务 / 订购 / Profile 六行统一用 `useMatch` 加 `aria-current="page"` + `bg-n200 font-medium`（订购行已是这个样子，抽成一个 `NavRow` 小组件即可）。

**A7 清理。** `workspace.newChat` / `backToChat` 启用后不再是死 key；`WorkspaceSwitcher` 的 `navigate(paths.app)` 改成 `paths.newChat()` 语义一致。

A 线不碰后端，不碰移动端。

### 3.2 B 线：Profile 页

#### 3.2.1 定位与边界

- **Profile 回答"我是谁、我有什么"**：身份、套餐与积分、工作空间与邀请、云桌面、平台授权、用量摘要、管理员入口。
- **设置回答"怎么用"**：模型、浏览器、外观、团队成员管理。
- 设置里的"账号"tab **并入 Profile**：`SETTINGS_TABS` 去掉 `account`，`/app/settings/account` 302 到 `/app/profile`（与现有 `usage → billing/usage` 的重定向同款），设置默认 tab 改为 `models`。
- 订购、用量、授权中心、技能中心、定时任务**仍是各自的页**，Profile 只放摘要卡 + 跳转，不复制它们的列表。

#### 3.2.2 路由与入口

| 项 | 值 |
|---|---|
| 路由 | `/app/profile`（`paths.profile`, `routePatterns.profile = "profile"`），懒加载，挂在 `WorkspaceLayout` 下 |
| feature | 新建 `features/profile/`（`components/ hooks/ index.ts`），数据全部通过既有 `shared/api/*` 与其他 feature 暴露的 hook，在 route 层组合（§4.2），**不新增跨 feature import** |
| i18n | 新命名空间 `profile`（`locales/zh-CN/profile.json` + `en-US`），`check:i18n` 过 |
| 入口 | 侧栏 `UserRow`：点头像/名字 → `/app/profile`（现在跳用量，改掉）；"…"菜单加第一项"个人主页"；顶栏当前态 = "个人主页 · 账号与偏好" |
| 面板 | 同订购页：不渲染右侧 `WorkbenchPanel`（`WorkspaceLayout` 里 `isBilling` 的判断扩成 `isStandaloneWide`） |

#### 3.2.3 页面内容（自上而下，单列 ≤ 860px，与设置/订购同栅格）

**① 身份卡（P0）**
- 头像（`avatar_url`，缺省首字母圆片，沿用 `UserRow` 样式）、用户名、邮箱（未绑定显示"未绑定"）、角色徽标（`user / admin / developer`）、登录方式（`oauth_provider = logto` → "Logto 单点登录"，否则"密码"）、注册时间（`created_at`，相对时间 + 绝对日期 tooltip，§10.6）。
- 操作：**退出登录**（复用 `UserRow.signOut` 的整页跳转逻辑）。
- P1 追加：**编辑昵称 / 更换头像**（见 3.2.4 后端）。

**② 套餐与积分卡（P0）**
- 当前套餐名 + 有效期（`useBillingSubscription`）、积分余额（`useCreditBalance`）、本期已消耗（`useBillingSummary`，默认当月）。
- 三个链接：订购套餐 → `/app/billing/purchase`；用量明细 → `/app/billing/usage`；订单 → `/app/billing/orders`。
- 免费/无套餐显示"订购专业版解锁云桌面"一句引导（文案与 `billing.plans` 口径一致）。

**③ 工作空间卡（P0）**
- 我所在的空间列表：名称、类型（个人/团队）、我的角色、当前标记；点击 = `setCurrent + navigate(paths.newChat())`（与 `WorkspaceSwitcher` 同行为，切完回主页）。
- 待接受邀请（`usePendingInvitationsQuery`）：空间名、角色、过期时间、"接受"按钮（`acceptInvitation` 后刷新并切换）。
- 当前空间为团队空间且我是 owner/admin → 链接"管理成员" → `/app/settings/team`。
- 创建新团队空间：后端是否已有 `POST /api/workspaces` **待查**；没有则 P2。

**④ 云桌面卡（P0，按当前工作空间）**
- `/api/desktop/status`：状态（未开通 / 开通中 / 运行中 / 已停止 / 已过期）、地区、到期时间、上次心跳。
- 未开通且套餐允许 → "开通"按钮（复用 `DesktopActivationDialog` 的流程）；不允许 → 引导去订购。
- 放在工作空间卡下方，标题写明"「{workspace}」的云桌面"，与 09-03"桌面归 workspace"口径一致。

**⑤ 平台授权卡（P0）**
- 已绑定账号数、按平台分组的状态摘要（正常 / 即将到期 / 已失效），复用 `auth-center` 的状态色。
- 链接 → `/app/auth-center`。

**⑥ 用量摘要卡（P0）**
- 项目数、会话数（`useProjectsQuery / useSessionsQuery`）、本月 token 与积分消耗（`useBillingSummary`）。
- 纯展示，不做图表。

**⑦ 偏好快捷卡（P0）**
- 语言、颜色模式、字号三个内联开关（复用 `AppearancePage` 里的控件，不复制逻辑，把控件抽到 `features/settings` 的导出）；默认模型 / 默认 agent 只显示当前值 + 链接 → `/app/settings/models`。

**⑧ 管理员卡（P0，仅 `role === "admin"`）**
- 舰队管理 → `/app/admin/fleet`；后续超管控制台入口（`831bfcb` 已有的 superadmin console）也挂这里。

**⑨ 安全卡（P2）**
- 登录设备 / 活跃会话列表与"下线"——后端没有 refresh token 明细接口，需新增。
- 修改密码——仅密码用户；Logto 用户跳 Logto 账号中心。
- 注销账号——`PgUserRepo.soft_delete` 已有，缺 API 与确认流程，且要与套餐/桌面回收联动，单独立项。

#### 3.2.4 后端改动

| 期 | 改动 | 说明 |
|---|---|---|
| P0 | 无 | 全部读接口已存在。前端 `AuthUser` 类型补 `avatar_url? oauth_provider? created_at?` |
| P1 | `PATCH /api/auth/me` `{ username?, avatar_url? }` | 用户名走现有 slug/唯一性校验（`ix_users_username_active`），冲突返回 409；只允许改自己 |
| P1 | 头像上传 | 复用资源中心的 OSS 上传（`features/resources/hooks/useResourceUpload.ts` 走的接口），限 2 MB、image/*，落到 `avatars/<user_id>/` 前缀，返回公网 URL 写入 `avatar_url` |
| P1 | Logto 登录时若本地 `avatar_url` 为空则回填 `picture` | 现在只在建用户时写一次 |
| P2 | 会话/设备列表、注销账号、`POST /api/workspaces`（若无） | 见 ⑨ 与 ③ |

#### 3.2.5 移动端

`mobile/lib/features/settings/` 已有账号区；Web Profile 定稿后按同一信息架构补一页"我的"（Tab 或抽屉入口）。本分支不做。

---

## 4. 分期与估时

| 期 | 内容 | 估时 |
|---|---|---|
| A | A1–A7：新对话主按钮、logo 回主页、顶栏返回、顶栏识别表、侧栏当前态、清理 | 0.5–1 天 |
| B-P0 | Profile 路由 + 九张卡里的 ①②③④⑤⑥⑦⑧（无后端）、账号 tab 并入、UserRow 入口、i18n、单测 | 1.5–2 天 |
| B-P1 | `PATCH /me` + 头像上传 + 身份卡编辑 | 1 天 |
| B-P2 | 安全卡、创建空间、移动端"我的" | 另排 |

A 先独立成一个 PR 合入（改动小、收益立刻可见），B-P0 第二个 PR，B-P1 第三个。

---

## 5. 验收

**A 线**
1. 任意中心页（资源 / 授权 / 技能 / 定时 / 订购 / 设置 / 舰队）顶栏都有"返回对话"；有最近会话时回该会话，否则回 `/app`。
2. 侧栏第一行是"新对话"，点击进 `/app`，问候语下的提示写的是当前选中项目名；发送后会话落在该项目下。无选中项目时落"未归类"。
3. 点 logo 回 `/app`。
4. 技能中心、舰队管理顶栏显示正确标题，不再是"未命名对话 · 未归类"。
5. 在任一中心页，侧栏对应行高亮。
6. `npm run check` 全绿；`grep -rn '"/app' src` 除 `paths.ts` 外无硬编码路径。

**B-P0**
1. `/app/profile` 可直接打开、刷新不丢；侧栏点头像/名字进入；顶栏显示"个人主页"。
2. 身份卡显示 Logto 头像与邮箱；无头像用户显示首字母。
3. 套餐卡数字与 `/app/billing` 三个 tab 一致；无套餐显示引导。
4. 有待接受邀请的账号在 Profile 能接受并自动切换空间。
5. 桌面卡按当前空间显示；切换空间后卡片内容随之变化。
6. `/app/settings/account` 重定向到 `/app/profile`；设置导航不再有"账号"。
7. admin 看到管理员卡，普通用户看不到。
8. zh-CN / en-US 两套文案齐全，`check:i18n` 过；不渲染右侧工作面板。

**B-P1**
1. 改昵称后侧栏、顶栏问候语立即更新；重名返回 409 并有提示。
2. 上传头像 > 2 MB 或非图片被拒；成功后 `/me` 返回新 URL。

---

## 6. 开工前待拍板

1. **新对话落在哪个项目**：推荐"侧栏当前选中项目，无则未归类"（即 A1）。备选：永远未归类，由用户在输入框上方选项目。
2. **"新建项目"位置**：推荐保留为侧栏第二行。备选：折进项目列表标题右侧的"+"，侧栏顶部只留"新对话"。
3. **账号 tab 并入 Profile**（推荐）还是两边都留。
4. **昵称/头像可编辑**是否进第一期（需后端 P1）。推荐 P0 先只读，P1 紧接着做。
5. **云桌面卡**放 Profile（按当前空间）还是只在工作空间/订购页出现。推荐放 Profile。
6. Profile 是否要"安全"卡（设备列表 / 注销）进 P2，还是先不做。

---

## 7. 执行记录

- 2026-09-09：分支与本文档建立。代码未动。
- 2026-09-10：§6 第 1–3 项拍板为推荐方案。**A 线落地**（本分支，只动 `frontend-v2/`）：
  - `paths.newChat(projectId?)`；`ProjectRow` / `WorkspaceSwitcher` 不再手拼路径。
  - 侧栏：第一行"新对话"（a200 圆片，落当前选中项目），第二行"新建项目"（`FolderPlus`，草稿框紧随其下），logo 包 `Link` 回 `/app`；六个中心行抽成 `NavRow`，`useMatch` 高亮。
  - 顶栏：`lib/standalonePage.ts` 用 `matchPath` 表识别页面（补技能中心、超管系统）；`hooks/useTopbarHeading.ts` 出标题，主页显示"新对话 · 项目名"；非会话页出"返回对话"，目标 = `ui.lastSessionId`（`WorkspaceLayout` 写、持久化）仍存在则该会话，否则 `/app`；窄屏只留箭头。
  - `EmptyChatRoute` 用 `lib/newChatProject.ts`：`?project=` → 侧栏当前项目（须仍存在）→ 未归类，并把项目名传给问候语。
  - 文案新增 `home` / `skillCenterHint` / `adminConsoleHint`；启用原本闲置的 `newChat` / `backToChat`。
  - 单测：`standalonePage`、`newChatProject`、`ui.lastSessionId` 共 10 条；`npm run check` 全绿（lint 0 错误，406 测试）。
  - 本地验证（docker-compose.dev 的 postgres/redis + 后端 + `devtest` 账号）：新对话 → `/app?project=<id>` 且提示写项目名；技能中心顶栏标题/副标题正确、侧栏高亮、返回 → `/app` 顶栏"新对话 · 默认空间"；logo → `/app`；375px 宽返回链接仅图标。
  - 未验：返回到"最近会话"的分支需要真实会话（本机无模型密钥），逻辑由单测覆盖；e2e `sidebar-project.spec.ts` 同样依赖模型，未跑。
