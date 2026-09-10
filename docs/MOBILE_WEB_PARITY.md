# Web → 移动端对齐清单

> 性质：活文档，记录已经进入 `main`、但 Flutter 移动端尚未跟进的能力、依赖、决策和验收标准。
>
> 最近核对与实现：2026-09-07；最近核对的 main 基线 `205226d`。移动端已对齐订阅驱动开通，并在本轮补齐 A5 授权中心与抖音投稿；本轮新增代码尚未发布。阿里云配置为 `APP_ENV=prod`。
> 最初核对范围为 `2c64409..49ba4ff`，并补计更早未移植的 `9dd7d79`（桌面按用户开通）。

## 维护规则

1. Web 新增跨端能力时，必须在同一个变更里完成移动端，或在本文登记为“待对齐/有意省略/待决策”。
2. 完成一项后，同时更新状态、验收结果和对应提交；不要只删除条目。
3. `frontend-v2/src/locales/*/*.json` 是 locale 唯一事实源。移动端文件逐字节复制，不在 `mobile/assets/locales/` 单独改文案。
4. “后端默认值让 App 暂时不报错”不等于已经对齐；仍需验证租户隔离、权限和缓存是否正确。

状态含义：`待对齐`、`进行中`、`待决策`、`已完成`、`有意省略`。

## 当前结论

`2c64409..49ba4ff` 之间有 8 个提交改动 `frontend-v2/src`，合计 75 个文件、`+3962/-355`，当时没有移动端文件改动。前轮已完成 4 个用户功能块、订阅驱动开通和 locale 对齐；本轮继续补齐 A5 授权中心、投稿及聊天二维码（见 §9）。舰队管理继续作为有意省略的桌面运维能力。支付宝移动 SDK 已接线，生产后端已提供服务端签名接口，正式商店发布仍须在给齐商户配置后完成支付实测与 iOS 合规决策。

| 优先级 | 功能块 | Web 提交 | 状态 | 主要影响/依赖 |
|---|---|---|---|---|
| P0 | 工作空间多租户与协作 | `6622c61`，并含 `bfd08a4` 的请求作用域保护 | 已完成 | header/store、缓存隔离、只读会话、Team、邀请 deep link 均已接入 |
| P0 | Locale 同步 + 工作日志内联常开 | `a7b5816` 及以上功能提交 | 已完成 | locale 逐字节一致，已增加自动门禁 |
| P1 | 云桌面开通、状态与未就绪引导 | `9dd7d79`、`df805a6`、`a789456` | 已完成 | status、开通/重建、长轮询、通道状态和聊天引导已接入 |
| P0 | 订阅驱动的无影云开通与权益边界 | `798864e`、`129e758` | 已完成 | 全局持久化进度、回前台恢复、Free 提示、作用域隔离、到期撤销连接；替代上方旧手动开通交互，见 §8 |
| P1 | 积分、套餐、用量与订单 | `bfd08a4` | 已完成（技术接入） | 三页、余额和 App Pay SDK 已接；商户 key 与 iOS 上架策略仍是发布门禁 |
| P1 | A5 授权中心、抖音投稿与聊天二维码 | `c3563d9`、`12ad18f`、`a0f9b65` | 已完成（代码与模拟器）；真机验收待完成 | 多账号、权限、预计到期、视频投稿、同机跳转、二维码备选与服务端状态恢复；本轮未发布，见 §9 |
| P3 | 舰队管理 | `ae330c0` | 有意省略 | 管理员运维台，按移动端“能力不具备则省略控件”处理 |
| P3 | 超管系统（舰队 / 技能管理 / 订阅管理） | 分支 `codex/admin-console` | 有意省略 | 与舰队管理同一处理：Web-only 运营台，移动端不加入口和控件；`admin.json` / `admin-skills.json` / `admin-billing.json` 仍按 locale 规则逐字节复制。见 §10 |
| P2 | 用户侧技能商店改版（来源分区、上架状态、撤回、审核提示） | 分支 `codex/admin-console` | 已完成（代码与单测）；真机验收待完成 | 三个 widget 已接新字段，`listing.dart` / `store_sections.dart` 从 web 逐条移植并有 14 项单测。见 §10 |

## 1. Locale 同步与工作日志内联常开

### 对齐前差异（已解决）

- Web 的 `WorkLogTrace` 已不再使用折叠的 `TraceShell`，过程说明与最终答复同列、常开并按顺序累积。
- 移动端 `work_log_trace.dart` 仍使用 `TraceShell` 和 `chat:trace.work.summary`；`assistant_turn.dart` 仍传 `defaultOpen`。
- 移动端缺少 locale 自动对齐门禁，`mobile/scripts/` 目前只有文件大小检查。

每个语言目录的已知差异如下：

| 文件 | 差异 |
|---|---|
| `admin.json` | 移动端缺文件，36 个叶子 key |
| `billing.json` | 移动端缺文件，127 个叶子 key |
| `errors.json` | 缺 `DESKTOP_NOT_READY`、`INVITATION_EXPIRED`、`SESSION_READ_ONLY`、`WORKSPACE_FORBIDDEN`、`WORKSPACE_ROLE_REQUIRED` |
| `settings.json` | Web 多 23 个 Team/导航相关 key；移动端残留 10 个 Usage key |
| `workbench.json` | 缺 12 个开通、分配和通道状态 key |
| `workspace.json` | 缺 `adminFleet`、`billing`、`creditBalance`、`creditsUnavailable`、`readOnlySession`、`viewCredits`、`workspaceSwitcher` |
| `chat.json` | `meta.cost` 已改为“消耗积分/Credits”；`trace.work.summary` 已从 Web 删除 |

### 要做

- [x] 将 `WorkLogTrace` 改为内联常开，移除 `defaultOpen` 和 `chat:trace.work.summary` 的所有引用。
- [x] 保持 WorkLog 与最终答复的展示顺序和 Web 一致，并保留 computer 截图首/中/末三帧规则。
- [x] 从 Web 逐字节复制全部 locale 文件，并在 `i18n.dart` 注册 `admin`、`billing` namespace。
- [x] 新增 `mobile/scripts/check_locales.sh`，校验语言目录、文件列表和文件字节完全一致，并写入移动端日常校验命令。

### 验收

- 流式过程中内容从 final 重判为 progress 时，已经显示的段落不会消失或被折叠。
- `rg 'trace\.work\.summary|defaultOpen' mobile/lib` 不再命中工作日志旧实现。
- locale 检查脚本在当前 Web 文件上通过；改动任意一个移动端 locale 字节后必须失败。
- 未知后端错误码仍有通用兜底；上述 5 个新错误码均显示明确文案。

## 2. 工作空间多租户与协作

参考：[B1 工作空间审计](B1_WORKSPACE_AUDIT.md)。

### Web 已有

- 请求携带 `X-Workspace-Id`；access token refresh 后，只有用户和 workspace 都未变化才重试原请求。
- 持久化当前 workspace（键 `openbox:workspace-id`），提供空间列表、当前空间、待接受邀请、邀请成员、改角色、移除成员、接受邀请等 API。
- `WorkspaceLayout` 在空间列表就绪后才渲染应用壳；项目和会话缓存按 workspace 分区。
- 多空间切换器、Team 设置页和 `/invite/:token` 邀请入口已经上线。
- 他人的会话显示 owner，隐藏删除操作，并将 Composer 换成只读提示。

### 对齐前移动端现状（已解决）

- `http_client.dart` 不发 `X-Workspace-Id`。后端缺 header 时会回退到 `default_workspace_id`，所以基本功能不报错，但用户无法进入受邀团队空间。
- `Session` 未解析 `user_id`、`workspace_id`、`owner_username`；他人会话仍可出现重命名、删除和发送入口。
- 后端会以 `SESSION_READ_ONLY` 拒绝这些写操作；移动端当前只会把 403 显示成通用“没有权限”。
- App 只有 SSO callback scheme，没有邀请链接的 universal/app link 路由。

### 要做

- [x] 增加当前 workspace store；启动时先取空间列表，再恢复持久化选择，不合法时回退后端默认空间。
- [x] 有选中空间时为所有业务请求注入 `X-Workspace-Id`；bootstrap 请求允许不带 header。
- [x] refresh 前捕获用户与 workspace，二者任一变化都不得重放旧请求。
- [x] 切换 workspace 时清理/重取空间、项目、会话、桌面和计费状态，清空失效的已选项目并回到安全根路由。
- [x] 扩充 `Session` 模型；他人会话显示 `· owner_username`，隐藏重命名/删除，聊天页显示只读提示且不渲染 Composer。
- [x] 增加 Team 设置页：成员列表、邀请、改角色、移除成员和待接受邀请；按 owner/admin/member 权限控制操作。
- [x] 增加 Android HTTPS App Link 与两端自定义 scheme 邀请入口，在 App 内接受后刷新 workspace；iOS 通用链接仍需站点 AASA/Associated Domains 后续发布配置。

### 验收

- 默认空间、多空间、被邀请成员三种账号均可进入正确空间；冷启动能恢复上次有效选择。
- refresh 期间切换账号或空间，不会把旧请求写入新作用域。
- 切换空间后，项目、会话、桌面状态和积分余额均无前一个空间的数据残留。
- owner/admin/member 的成员管理权限与后端一致；非本人会话没有任何写入口。

## 3. 云桌面开通、状态与未就绪引导

参考：[桌面执行通道](A1_DESKTOP_CHANNEL.md) 与 [舰队/预热池](A3_A4_FLEET_POOL.md)。

### 对齐前差异（已解决）

- Web 连接前先请求 `GET /api/desktop/status`：未开通时 owner/admin 可开通，member 显示权限提示；失败可重建；创建/启动/分配阶段最多等待 `120 × 5s`。
- 连接后 Web 每 30 秒刷新 status，显示 `up`、`pending`、`down`、`revoked` 通道状态。
- 聊天发送或新建会话遇到 `DESKTOP_NOT_READY` 时，Web 会提示并自动打开云桌面面板。
- 移动端直接请求 ticket，只有 loading/connected/error/closed 四态；`202` 最多等 `30 × 3s = 90s`，首次创建常需 2–3 分钟，容易提前超时。

### 要做

- [x] ticket 前增加 status 预检，覆盖 `not_provisioned`、`failed`、`creating`、`starting`、`assigning`、`running`。
- [x] owner/admin 提供开通/重建操作，member 显示无权限说明；状态文案区分预热池分配与新建。
- [x] 冷启动/分配轮询窗口与 Web 对齐为 `120 × 5s`；页面退出时用 generation/timer 取消后续更新。
- [x] 连接后每 30 秒刷新 status 并显示通道 pill；离开页面后停止 timer。
- [x] 聊天侧识别 `DESKTOP_NOT_READY`，显示专用提示并导航到云桌面页。

### 验收

- `not_provisioned` 下 owner/admin 与 member 看到正确且不同的动作。
- 冷创建超过 3 分钟仍能继续等待并最终连接；失败态可以安全重试。
- 四种通道状态能刷新且不会在离开页面后继续泄漏 timer。
- 从聊天触发未就绪错误后，用户能直接到达可开通/等待的桌面页面。

## 4. 积分、套餐、用量与订单

参考：[计费方案](BILLING_PLAN.md)。

### Web 已有

- `/app/billing/:tab?` 提供订购、用量、订单三页；用户行显示当前空间积分余额。
- 已接 balance、plans、subscription、summary、usage、providers 和订单创建/列表/checkout/refresh/cancel。
- 支持 free/pro/max 套餐、月付/年付、额外充值、待付款订单恢复与状态核对。
- 消息 token usage 新增 `credits`，UI 以积分而不是 USD cost 展示。

### 对齐前现状与约束

- 没有计费 API、状态或页面；`token_usage.dart` 仍只有旧的 `double cost`，且移动端原本就没有消息费用徽章。
- `url_launcher` 已在依赖中，可用系统浏览器打开支付页；回到 App 后需要监听 lifecycle resume 并主动 refresh 订单，不能照搬 Web 的 `pageshow`。
- 金额和积分必须保持后端返回的十进制定点字符串，不能先转 `double` 再展示或参与判断。
- 后端 `StepFinishPart` 已有 `credits`，但当前 Web 的同名 TypeScript 类型也未完全补齐；这是共享 wire contract 清理，不应误记成仅移动端差异。

### iOS 上线决策（发布前置，技术接入已完成）

Apple 当前 [App Review Guidelines 3.1.1](https://developer.apple.com/app-store/review/guidelines/) 原则上要求数字功能、订阅和 App 内货币使用 In-App Purchase，并按 storefront、entitlement、跨平台或企业服务等例外分别处理。因此不能默认把支付宝外链 1:1 搬到 iOS。

- [ ] 产品/法务确定 iOS 路径：IAP、仅展示已在其他端购买的权益、符合地区 entitlement 的外链，或暂不提供购买。
- [x] 技术路径按当前决定先接支付宝官方 App SDK；私钥只在服务端签名，App 不携带商户私钥，SDK 结果不作为到账依据。
- [x] 将实现、模拟器回退和发布门禁补进 `BILLING_PLAN.md`；待商户 key 到齐后再做真机付款与审核材料。

### 建议拆分

第一阶段（已完成）：

- [x] 增加当前 workspace 的积分余额、用量明细、汇总和订单列表。
- [x] 用户行显示余额；余额不可用时稳定降级，不把网络错误显示成 0。
- [x] `TokenUsage.credits` 使用 nullable decimal string；金额和积分展示不经 `double`。

第二阶段（SDK/订单状态机已完成，真实付款待 key）：

- [x] Android/iOS 优先使用支付宝官方 App SDK；开发模拟器或后端未声明 App Pay 能力时才回退外部收银台。
- [x] App 回到前台和订单页停留期间主动 refresh，恢复待支付订单，并支持继续支付、查询状态和取消。
- [x] 后端增加 `POST /api/billing/orders/{order_id}/app-checkout`，只返回服务端 RSA2 签名的短期 SDK payload；订单最终状态仍仅信任签名通知/主动查询。
- [ ] 商户 key 到齐后，在 Android 与 iOS 真机分别完成支付、取消、回调延迟和回前台恢复测试。

### 验收

- 余额、用量、订单严格随 workspace 切换，不串数据；大额/小数积分无浮点误差。
- App 杀后台再恢复、支付取消、支付成功但回调延迟时，均可主动核对到最终订单状态。
- 重复点击或网络重试不会生成不受控的重复订单。

## 5. 舰队管理

Web `/app/admin/fleet` 是仅 `user.role === "admin"` 可见的运维台，覆盖预热池、桌面列表、告警、释放、重建、退役和收养。

移动端维持 `有意省略`：不添加入口和控件，`admin.json` 仍按 locale 同步规则复制。若以后出现明确的移动运维场景，再单独立项，不能把普通 workspace owner 与平台 admin 权限混在一起。

## 6. 原生下载（2026-09-06 追加并完成）

移动端资源中心、聊天 Lightbox 和普通文件附件此前拿到 OSS 预签名 URL 后调用 `url_launcher`，会离开 App 并由浏览器接管。现已改为：

- [x] 使用无认证头的独立 Dio 客户端在 App 内流式下载到临时文件，避免把 Bearer token 发到 OSS，也避免大文件整块进入内存。
- [x] iOS 通过 `UIDocumentPickerViewController(forExporting:)` 弹出系统“存储到文件”；Android 通过 `ACTION_CREATE_DOCUMENT` 选择目标后流式复制。
- [x] 文件名做路径剥离、非法字符替换和长度限制；完成或取消后清理临时目录。
- [x] 资源中心详情/操作菜单、聊天媒体查看器和 `FileChipRow` 全部改走同一服务；PDF 的“打开”动作仍保留系统查看器语义，不与“下载”混淆。

技能 ZIP 原来就直接写入 App Documents、不会跳浏览器，因此本次不改变它的既有行为。

## 7. 移动端 Composer 精简（2026-09-07 有意差异）

按移动端产品决定，Composer 不显示 Web 上的“执行/方案”模式选择按钮。这是有意的跨端差异，后续做 Web parity 时不得自动补回。

- [x] 只隐藏移动端入口，不删除 agent 字段、状态或后端协议。
- [x] 已有会话继续沿用会话保存的 agent；新会话继续使用 App/服务端默认 agent，因此历史记录加载和消息发送不受影响。
- [x] Composer 不再单独显示“思考强度”按钮；聊天模型列表对有思考档位的模型显示二级入口，选择模型后再选择该模型自己的思考强度，与视频模型 → 分辨率的交互一致。
- [x] 模型与思考强度作为一对保存；无思考档位的模型直接选中，取消二级菜单则两者都不改变。
- [x] Web 的模式选择器和独立思考强度选择器保持不变；附件与发送入口保持原样。

## 8. 订阅驱动无影云与 prod 联调（2026-09-07）

本节替代 §3 中旧的“手动开通/重新创建”交互；机器购买、分配、续费与恢复只由后端持久化 activation worker 负责，移动端不自行发起购买。

- [x] 移动端共享解析 `entitled`、`retained`、`subscription_ends_at`、activation request/step/retry、channel 状态；账号 + workspace 同时作为缓存键和请求校验条件。
- [x] 全局进度弹窗覆盖订购及其他已登录页面，付款后刷新进度；可关闭后继续聊天、再次查看、回前台恢复。就绪确认按服务地址/用户/空间/request 持久化。
- [x] 全局弹窗使用独立 Overlay，覆盖 `MaterialApp.builder` 位于 Navigator 之上的真实结构；不因显示/关闭弹窗重建聊天页面。
- [x] Free 云桌面显示“付费套餐专享无影云”与“查看套餐”；开通中自动等待，仅 backend `can_retry` 且 owner/admin 可显式重试，不提供“重新创建机器”。
- [x] 订阅到期、空间/账号切换时取消旧 ticket 请求、停止旧 SDK 并清空 WebView；过期检查也在回前台执行。仅断开查看连接，不删除或关停底层云电脑。
- [x] 终端、浏览器、文件入口使用同一权益门禁和作用域隔离的 container 缓存，移除旧的手动 Docker 创建入口；侧栏新增“云桌面”直达入口。
- [x] Locale 从 Web 逐字节同步；价格继续从后端读取，Pro/Max 月付和年付均显示 0.10 元，Free 为 0 元。
- [x] iOS 模拟器构建显式指定 `API_BASE` 与 `WEB_BASE` 为 `https://ai.bossipai.com.cn`；复用现有登录账号，不创建真实支付订单或购买新桌面。

新增回归覆盖：Free/开通中不请求 ticket 或自动 provision、到期状态切换、显式重试角色门禁、关闭/重开/前后台恢复、就绪确认持久化及空间隔离、聊天元素保持、API 作用域拒绝与付款刷新、精确分价及订阅到期边界。生产到期/付款状态不为测试而修改。

验收结果：`flutter analyze` 无问题、全部 **44 项 Flutter tests** 通过、locale 逐字节和文件大小门禁通过、iOS arm64 simulator 构建通过。在 **iPhone 17 Pro / iOS 26.5** 上安装并连接阿里云，实测现有登录保留、就绪弹窗与持久化确认、月付/年付 0.10 元、侧栏云桌面直达、只读桌面画面与通道在线、全屏/竖屏恢复、Home 后回前台均正常。未进行真实付款或购买/删除机器；Free 与到期流程使用自动化模拟状态验证。

## 9. A5 授权中心、抖音投稿与聊天二维码（2026-09-07）

### 本轮实现

- [x] 侧栏在“资源中心”下增加“授权中心”，路由 `/app/auth-center`；支持 `?job=<id>` 从聊天进入指定投稿结果，冷入口的返回按钮可回到聊天。
- [x] 读取平台配置、工作空间多账号、权限 scopes、当前与预计到期时间、剩余续期次数和最近检测时间。owner/admin 可绑定、重授权、确认解绑，member 可检测与投稿。平台配置缺失时禁用外部操作，但仍允许管理员解绑本地记录。
- [x] 复用既有 authorize/callback 协议；按钮显式打开带 `is_call_app=1` 的官方授权页，保留原 `state` 与生产 HTTPS 回调，不另造开放重定向。二维码是备选，可通过 iOS/Android 原生保存面板导出带白色静区的 PNG。
- [x] 授权完成仍回 Web，用户回到 App 后主动拉取账号状态；打开抖音不等于授权成功。授权链接按 10 分钟失效处理，不持久化到本地偏好设置。
- [x] 投稿选择资源中心最近 100 个视频中的可用项（ready、正数大小且 ≤128 MiB、mp4/mov/3gp MIME），支持标题、去重话题和公开范围。双击只产生一次请求，响应不确定时不自动重放 POST，提示先查询最近投稿。
- [x] 新生成的有效投稿链接可显式打开抖音或显示/保存二维码。详情每 5 秒查询服务端，回前台立即核对、后台取消请求与轮询，终态停止轮询；不根据客户端跳转推断已发布。
- [x] 最近投稿读取服务端最近 50 条记录；冷启动可恢复 pending/published/failed/expired 状态和作品 ID，不依赖内存里的旧页面。签名 schema 不落盘，冷启动不会自动重新生成投稿；待确认的旧任务需继续使用已打开的抖音或原聊天二维码。
- [x] 聊天 `douyin_publish` 产物保留为二维码而不是过程截图；以正方形、完整图片展示，下方直接提供“查看投稿结果/授权中心”。新 metadata 提供有效期限与链接时，增加“打开抖音/在手机上授权”按钮，无需展开工具详情。
- [x] API 请求在发出前及响应后校验账号 + workspace；Dio 在注入 Bearer 前再次拒绝过期作用域，避免队列中的旧请求携带新账号凭据。页面以账号/空间/角色为 key，切换时销毁旧表单、请求与二维码。
- [x] `auth-center.json` 与 `workspace.json` 从 Web 同步，zh/en 逐字节一致。二维码图片的黑白色属于机器可读内容，不随主题改色；其他控件沿用 tokens。

### 兼容与发布边界

授权中心直接使用已上线 REST 接口，不要求服务端新增移动回调。工具返回 metadata 新增两个可选字段：授权 `expiresAt`、投稿 `launchUrl`（已有短期签名 schema 的等价表示，不包含 OAuth access/refresh token）。这两个字段需要随本轮后端代码发布，聊天才具备同机快捷按钮；旧消息/旧服务端仍显示原二维码及结果查询入口。

本轮没有改变套餐价格、支付协议、自动开机策略或云桌面进程，也未提交推送或部署服务器。iOS/Android 同机客户端唤起、真实授权以及点击“发布”后的 Webhook 回写必须在装有抖音的真机上验收，模拟器不能替代这一步。

### 本轮验收

- `flutter analyze` 无问题；全部 **65 项 Flutter tests** 通过（原 44 项回归 + 21 项新增）。覆盖 API 方法/参数、跨用户/空间旧响应、排队请求换号、POST 不自动重试、角色/配置门禁、解绑确认、防双击、前后台恢复、冷启动历史、失败/过期二维码、PNG 静区和聊天完整二维码。
- 后端 `test_douyin_publish_tool.py` + `test_platform_accounts.py` 共 **23 项**通过；Web `npm run check` 通过（**217 项测试**、TypeScript 与 locale 门禁；lint 0 errors，保留 25 个已有 warnings）。
- locale 逐字节门禁、Dart 单文件 ≤800 行门禁、`git diff --check` 通过；Android debug APK 与 iOS arm64 simulator 构建通过，`API_BASE`/`WEB_BASE` 均显式指定 `https://ai.bossipai.com.cn`。
- iPhone 17 Pro / iOS 26.5 模拟器保留现有登录，连接生产验证授权中心空态、授权 QR 生成及系统保存面板；取消保存后临时文件已清理。最终安装包复测通过侧栏入口、返回聊天、Home 后回前台，原订购/云桌面入口保留。未绑定真实抖音账号、未发布视频。安全截图：[授权中心空态](evidence/mobile-auth-center-20260907/production-empty.png)；不存档含有效 OAuth state 的二维码截图。
- [ ] Android/iOS 真机：已装/未装抖音、取消授权、授权成功后返回 App、投稿取消/成功、回调延迟、杀进程后从最近投稿查到最终结果。

## 10. 超管系统与技能商店改版（2026-09-08）

来源：`docs/ADMIN_CONSOLE_SKILL_STORE_PLAN.md`，分支 `codex/admin-console`。这一轮把技能商店从「第三方目录 + 即发即上架」改成「官方技能 + 经审核的用户投稿」，并把 Web 的「舰队管理」升级成「超管系统」。

### 超管系统：有意省略

Web `/app/admin` 下的三个栏目（舰队管理 / 技能管理 / 订阅管理）全部只对 `users.role === "admin"` 开放，后端 `/api/admin/skills/*`、`/api/admin/billing/*` 也只认这一个角色。移动端与 §5 舰队管理同样处理：**不加入口、不加控件**，`admin.json` 与新增的 `admin-skills.json`、`admin-billing.json` 仍按 locale 规则从 `frontend-v2/src/locales/` 逐字节复制（App 不用也要复制过门禁）。若以后确有移动运营场景，单独立项，不能把普通 workspace owner 与平台 admin 权限混在一起。

### 用户侧技能商店：已完成（真机待验）

2026-09-08 补齐，与 Web 同构。纯逻辑从 web 逐条移植成两个新文件，界面三处接上：

| 文件 | 对应 web | 内容 |
|---|---|---|
| `mobile/lib/features/skills/utils/listing.dart` | `lib/listing.ts` | `ListingChip` 五态、色调表、`listingChipFor` / `explainsItself` / `isResubmission` / `canWithdraw` |
| `mobile/lib/features/skills/utils/store_sections.dart` | `lib/store-sections.ts` | 按 `origin` 分货架、置顶→安装数→字母排序、`total` 与 `sections` 分开（区分「商店是空的」与「你搜不到」） |
| `widgets/store_list.dart` | `StoreList` + `StoreEntryRow` | 官方 / 用户共享 / 第三方 三段；每行自报 kind、置顶标、安装数（0 不显示） |
| `widgets/skill_group_section.dart` | `SkillGroupRow` + `ListingBadge` | 单个状态 chip（不是两个 badge 并排）、驳回/下架原因折叠展开、撤回按钮、按状态切「重新提交」 |
| `widgets/create_publish_sheets.dart` | `PublishSkillDialog` + `WithdrawSkillDialog` | 发布文案按 `skill_store_review` 二选一、重提时回显上次原因、新增 `WithdrawSkillSheet` |

配套改动：`BadgeTone` 增加 `danger` 档（驳回要比下架响）；`InstalledSkill` 增 `listing` / `listingNote` / `isOfficial`，`CatalogEntry` 增 `origin` / `featured` / `official` / `installsCount`；`SkillsApi` 增 `withdrawSkill()` 与 `storeReviewRequired()`。

**`skill_store_review` 走 skills 自己的 API 层**（`skillStoreReviewProvider`），没有借 chat 的 `appConfigProvider`——移动端此前没有跨 feature import 的先例，web 那边也是靠 eslint boundaries 硬拦的，不在这里开第一个口子。配置没到位时按 `false` 兜底，理由同 web：宁可少承诺。

验收：`flutter analyze` 无问题；`flutter test` 85 项全过（新增 `test/features/skills/store_listing_test.dart` 14 项）；文件大小与 locale 门禁通过。**真机未验**。

### 兼容边界（当前 App 不改也能跑）

- 新字段全部**可选**：`origin`、`listing`、`listing_note`、`featured`、`installs_count`、`is_official`、`skill_store_review` 都是新增键，旧客户端忽略即可。缺省语义：`origin` 按 `community`、`listing` 按 `listed` 处理。
- `GET /api/agent/catalog` 的**结构没变**（仍是 `{skills, mcp}`），只是内容变少了：未上架的条目不再返回。默认口径下 `anthropic-skills` 从商店消失（代码默认 `delisted`），7 个 MCP 保持上架。这是产品决定，不是移动端的兼容问题。
- **旧 App 的行为差异**：`SKILL_STORE_REVIEW=true`（默认）时，用户在移动端点「上传到商店」后技能进入待审核，商店里当时看不到，而移动端文案仍写「所有用户都能看到」。这不会报错，但会误导——这是本项待对齐里优先级最高的一条。
- 移动端目前没有撤回入口，作者只能在 Web 上撤回；已安装的副本在任何情况下都不受下架或撤回影响（§3-Q4），所以移动端沙箱里的技能不会凭空消失。

## 明确不需要重复移植

- `4a87777` / `66bb9de` 的 1920×1080 固定分辨率：移动端 `desktop_bridge.dart` 已用 `fixedResolution`/`maxResolution` 做到等价效果。
- `7f7d487` 的 content-view 拆函数和测试 mock：行为不变。
- `99da03d..1b5dbdd` 的云电脑浏览器线程预算与修复门禁：位于后端/云桌面运行时，移动端复用同一远程服务，无需重复实现或修改本地浏览器。
- `4cd8725` 的默认项目名与项目创建时间倒序：后端统一生效，移动端无需专门改动。

## 实施结果

1. **Locale + WorkLog**：已原子完成并增加 byte-for-byte 门禁。
2. **Workspace**：基础作用域、协作 UI、只读态与邀请入口已完成。
3. **Desktop**：status、开通/重建、长轮询、通道状态和聊天引导已完成。
4. **Billing**：查询、购买、订单恢复、两端 SDK 与生产签名接口已完成；只余商户 key 后真机实付和 iOS 上架决策。
5. **Fleet**：继续省略并保留偏差记录。
6. **Native download**：已完成，不再借浏览器下载。
7. **Logto logout**：Web 用单次服务端 302 原子完成 OpenBox Cookie 撤销与 Logto end-session，避免先渲染 `/login` 触发自动授权的竞态；移动端同时清理 OpenBox session、Logto SSO session，并在异常路径强制删除 SDK 本地令牌。Web 登录固定 `prompt=login consent`；移动端因 SDK 使用 ephemeral 浏览会话而固定 `prompt=consent`，避免生产 Logto 把刚验证的 Android 登录误送进 end-session 中转页，同时保留 `offline_access` 的授权语义。
8. **移动端 Composer 控件**：移动端有意隐藏“执行/方案”入口，并把思考强度并入模型二级菜单；Web 保持原交互。
9. **移动端新对话与执行环境入口**：项目行常驻且右侧对齐 `+`（手机没有 hover），点击进入该项目的空会话；空会话不再根据旧 `/api/containers` 列表显示“创建沙箱”，首条消息由后端按 workspace 自动连接无影，真正未就绪时继续走 `DESKTOP_NOT_READY` 云桌面引导。
10. **订阅驱动无影云**：全局进度、Free 权益边界、到期断开查看连接、作用域隔离、侧栏直达与阿里云 prod 联调已完成，替代旧的手动开通流程，见 §8。
11. **Android Logto 单一回调**：邀请链接只保留生产 HTTPS App Link；删除 MainActivity 上缺少 host 的自定义 scheme 过滤器。Android 在过滤器未声明 host 时会忽略 `pathPrefix`，旧配置因此同时匹配 `com.bossip.bipmobile://callback`，形成两个授权跳转入口。2026-09-09 起仅由 `.AuthCallbackActivity` 接收：仍交给 SDK 验证 OAuth，并显式恢复原 App task，修复授权成功后 Chrome 仍覆盖 App 的问题（详见 [Logto 配置与回跳契约](LOGTO_PROD.md#android-callback-task-restoration-1016--2026-09-09)）。
12. **超管系统 + 技能商店**：超管系统与舰队管理一样按 Web-only 处理，移动端只复制 locale；用户侧商店的来源分区、上架状态、撤回与审核文案登记为待对齐，接口对旧客户端保持兼容，见 §10。

## 完成记录

| 日期 | 功能块 | 提交 | 验收证据 |
|---|---|---|---|
| 2026-09-10 | 双端通知 API 与单手机登录 | `codex/mobile-notifications`，未部署 | APNs/极光桥接与设置入口、最新手机登录生效、通知换绑与投递队列；PostgreSQL 迁移及多进程并发测试、Flutter 回归、Android debug/iOS simulator 构建。已接主任务终态、待处理、定时结果及可信发布回调；明确 resumed/inactive/后台/超时离线边界、前台抑制与取消队列。真机远程送达及 Android 厂商通道仍待验收，见 [移动通知说明](MOBILE_NOTIFICATIONS.md) |
| 2026-09-08 | Android Logto prompt 修复 | 本轮改动 | API 36 模拟器完整复现密码验证成功后卡在 `Submitting Callback`；保持 PKCE/state 不变、仅从 `login consent` 改为 `consent` 后 custom-scheme 回调、Token 交换与工作台加载成功；同一账号退出后再次登录仍显示凭证页 |
| 2026-09-08 | Android Logto 回调去重 | 本轮改动 | Manifest 回归测试、`flutter analyze` 与 Gradle merged-manifest 构建通过；合并清单确认 MainActivity 只处理 HTTPS 邀请，`com.bossip.bipmobile://callback` 只剩 `flutter_web_auth_2.CallbackActivity` |
| 2026-09-07 | 订阅驱动无影云与 prod 移动端对齐 | 本轮改动，Web/服务端基线 `129e758` | 44 项 Flutter 测试、analyze、locale/文件大小门禁与 iOS 模拟器构建通过；iPhone 17 Pro 实连阿里云验证就绪提示、0.10 元月付/年付、桌面画面及前后台/全屏恢复 |
| 2026-09-06 | Locale + WorkLog | 本次提交 | locale byte diff 通过；`flutter analyze` 与 Flutter tests 通过 |
| 2026-09-06 | Workspace + Team + invite | 本次提交 | 请求作用域与 refresh 双保护；路由/权限静态校验通过 |
| 2026-09-06 | Desktop | 本次提交 | 双端原生构建通过；iOS 模拟器实际安装启动 |
| 2026-09-06 | Billing + Alipay App SDK | 本次提交；后端镜像 `20260906-app-pay-f9247f1` | 支付/舰队/迁移相关 183 项测试通过；生产 route 匹配 401、服务健康，并保留 `c8fea7f` 生产保护 |
| 2026-09-06 | 原生下载 | 本次提交 | 文件名单测、Android debug APK、iOS arm64 simulator 构建通过；模拟器实际弹出系统保存面板、回调 `saved=true`，并核对目标文件落盘 |
| 2026-09-06 | Logto 双层退出 | 本次提交；生产 `20260906-logto-logout3-3586742` | 对照 `workspace/bossip` 修正 Web 退出竞态；后端单次 302/Cookie 撤销、当时两端 `prompt=login consent` 与移动端 OpenBox+Logto 编排回归通过；移动端 prompt 后由 2026-09-08 Android 实测修订；生产两应用的 Post sign-out redirect URI 已从 Logto 数据库回读确认 |
| 2026-09-07 | iOS 历史记录渲染 + Composer 精简 | 本次提交 | 历史会话含工作日志、工具状态、最终答复和截图均正常展示；Composer 隐藏“执行/方案”，思考强度并入模型二级菜单，agent/variant 发送协议保留；Flutter analyze、tests 与 iOS 模拟器交互通过 |
| 2026-09-07 | 移动端项目新对话入口 + 空会话执行环境判断 | 本次提交 | 项目行常驻新对话按钮；移除旧容器列表触发的误导性“创建沙箱”卡，保留首条消息的无影未就绪引导 |
