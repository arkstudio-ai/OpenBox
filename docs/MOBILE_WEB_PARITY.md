# Web → 移动端对齐清单

> 性质：活文档，记录已经进入 `main`、但 Flutter 移动端尚未跟进的能力、依赖、决策和验收标准。
>
> 最近核对与实现：2026-09-10；移动修复开发基线 `a69b141`，功能提交 `ad132c5`，整合 main 基线 `d10e4ee`。云电脑登录态、授权通知、HTTPS 技能图标和多压缩包上传队列见 §12，原生超管与 UI 重构见 §13；合并状态以 Git 记录为准，生产部署另行进行。阿里云配置为 `APP_ENV=prod`。
> 最初核对范围为 `2c64409..49ba4ff`，并补计更早未移植的 `9dd7d79`（桌面按用户开通）。
>
> 2026-09-10 增量：`codex/next-step-suggestions`（开发基线 `a69b141`）同步实现 Web 与 Flutter 原生下一步建议；合并状态见 Git 记录，生产部署另行进行，见 §11。

## 维护规则

1. Web 新增跨端能力时，必须在同一个变更里完成移动端，或在本文登记为“待对齐/有意省略/待决策”。
2. 完成一项后，同时更新状态、验收结果和对应提交；不要只删除条目。
3. `frontend-v2/src/locales/*/*.json` 是 locale 唯一事实源。移动端文件逐字节复制，不在 `mobile/assets/locales/` 单独改文案。
4. “后端默认值让 App 暂时不报错”不等于已经对齐；仍需验证租户隔离、权限和缓存是否正确。

状态含义：`待对齐`、`进行中`、`待决策`、`已完成`、`有意省略`。

## 当前结论

最初 `2c64409..49ba4ff` 的 8 个 Web 提交已按下表处理。本次追加核对 `205226d..a69b141` 的 18 个 `frontend-v2/src` 提交：Ask 持久化/重连/分页、视频交付排序/折叠、媒体用量账单和技能商店来源/审核/撤回已经同步，未重复实现；本轮补齐云电脑站点登录管理、通知、技能图片图标和批量上传，并修复 locale 与文件大小门禁（见 §12）。超管系统已补齐原生入口与移动交互，见 §13。正式商店发布仍须完成既有的真机支付实测与 iOS 合规决策。

| 优先级 | 功能块 | Web 提交 | 状态 | 主要影响/依赖 |
|---|---|---|---|---|
| P0 | 工作空间多租户与协作 | `6622c61`，并含 `bfd08a4` 的请求作用域保护 | 已完成 | header/store、缓存隔离、只读会话、Team、邀请 deep link 均已接入 |
| P0 | Locale 同步 + 工作日志内联常开 | `a7b5816` 及以上功能提交 | 已完成 | locale 逐字节一致，已增加自动门禁 |
| P1 | 云桌面开通、状态与未就绪引导 | `9dd7d79`、`df805a6`、`a789456` | 已完成 | status、开通/重建、长轮询、通道状态和聊天引导已接入 |
| P0 | 订阅驱动的无影云开通与权益边界 | `798864e`、`129e758` | 已完成 | 全局持久化进度、回前台恢复、Free 提示、作用域隔离、到期撤销连接；替代上方旧手动开通交互，见 §8 |
| P1 | 积分、套餐、用量与订单 | `bfd08a4` | 已完成（技术接入） | 三页、余额和 App Pay SDK 已接；商户 key 与 iOS 上架策略仍是发布门禁 |
| P1 | A5 授权中心、抖音投稿与聊天二维码 | `c3563d9`、`12ad18f`、`a0f9b65` | 已完成（代码与模拟器）；真机验收待完成 | 多账号、权限、预计到期、视频投稿、同机跳转、二维码备选与服务端状态恢复；本轮未发布，见 §9 |
| P1 | 舰队管理 | `ae330c0` | 已完成（代码与 iOS 模拟器） | 全局管理员可使用原生舰队管理，危险操作保留确认与审计保护，见 §13 |
| P1 | 超管系统（舰队 / 技能管理 / 订阅管理） | 分支 `codex/admin-console` | 已完成（代码、测试与 iOS 模拟器） | 原生权限入口、移动列表/表单、技能运营、舰队与只读账务；locale 继续逐字节同步，见 §13 |
| P2 | 用户侧技能商店改版（来源分区、上架状态、撤回、审核提示） | 分支 `codex/admin-console` | 已完成（代码与单测）；真机验收待完成 | 三个 widget 已接新字段，`listing.dart` / `store_sections.dart` 从 web 逐条移植并有 14 项单测。见 §10 |
| P1 | 云电脑站点登录态、登录页跳转与失效通知 | `0365150`、`7c891ee` | 已完成（代码、单测与 iOS 模拟器联调） | 显式查询 desktop 平台，OAuth/cookie 分离、轮询、检测、确认退出、预计到期、通知已读；第三方扫码成功与真实退出待人工验收，见 §12 |
| P2 | HTTPS 技能图标与多压缩包上传队列 | `d445b9f` | 已完成（代码与单测）；原生选择器已模拟器验收 | 图片失败回退、串行流式上传、部分失败重试、去重和容量限制；未向生产上传测试技能，见 §12 |
| P2 | 页面加载错误文案与移动端质量门禁 | `4d2a578` | 已完成 | en/zh `common.json` 字节同步；拆分 Ask 分页测试，所有 Dart 文件不超过 800 行 |
| P1 | 聊天输入框上方 AI 下一步建议 | 分支 `codex/next-step-suggestions` | 已完成（代码、组件测试与 iPhone 模拟器）；真机待验收 | 同一后端生成/持久化，原生横滑按钮、发送/草稿、可见性保护、长对话分页，见 §11 |

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

原先移动端有意省略舰队运维入口。2026-09-10 用户明确要求移动运营后，已补齐原生控制台，详见 §13；仅平台 admin 可访问，普通 workspace owner 不获得超管权限。

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

### 超管系统：原有省略决策已被 §13 替代

Web `/app/admin` 下的三个栏目（舰队管理 / 技能管理 / 订阅管理）全部只对 `users.role === "admin"` 开放，后端也只认这一个全局角色。2026-09-08 当时移动端不加入口；2026-09-10 用户明确要求实现并测试后，已改为完整原生入口与移动重排（§13）。`admin.json`、`admin-skills.json`、`admin-billing.json` 继续以 Web 为唯一文案源，并逐字节同步。

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

### 旧版本兼容边界（本节改版前，非当前实现状态）

- 新字段全部**可选**：`origin`、`listing`、`listing_note`、`featured`、`installs_count`、`is_official`、`skill_store_review` 都是新增键，旧客户端忽略即可。缺省语义：`origin` 按 `community`、`listing` 按 `listed` 处理。
- `GET /api/agent/catalog` 的**结构没变**（仍是 `{skills, mcp}`），只是内容变少了：未上架的条目不再返回。默认口径下 `anthropic-skills` 从商店消失（代码默认 `delisted`），7 个 MCP 保持上架。这是产品决定，不是移动端的兼容问题。
- **旧 App 的行为差异**：`SKILL_STORE_REVIEW=true`（默认）时，上传后进入待审核，但旧文案仍写「所有用户都能看到」。当前移动端已按审核配置显示正确文案，见上方完成项。
- 旧移动端没有撤回入口；当前移动端已提供确认撤回。已安装的副本在任何情况下都不受下架或撤回影响（§3-Q4），所以移动端沙箱里的技能不会凭空消失。

## 11. 下一步建议（2026-09-10）

- Flutter `SuggestionsPart` 解析同一份后端数据，忽略无效模式/空值/过长内容，最多保留三个不重复建议。生成模型统一读取服务端 `suggestion_model`，未配置时使用该轮聊天实际模型；客户端不增加模型调用。
- 原生 `SuggestionChips` 位于 Composer 卡片上方，44 logical pixels 最小触摸高度，横向滑动，支持中英文、明暗主题、大字号与读屏动作。
- `send` 走现有发送控制器和 REST，沿用当前模型/variant/video/agent；`draft` 只填入并聚焦。草稿、附件、上传、运行、等待、错误、只读、历史翻阅时不展示。点击有防重复和会话作用域保护，失败恢复不覆盖新草稿。
- WS 延迟结果和快照一致合并；逐页读取完整消息，超过 200 条时仍使用最新回复。新建议出现和键盘改变视口时维持底部定位。
- locales 与 Web 逐字节一致，顺带补齐基线已缺失的 `common.pageLoad` 文案。
- 新增 28 项单元/组件回归，覆盖 320/390/430 宽度、两语言/主题、150% 字号、键盘视口、点击和草稿、失败与跨会话、隐藏条件、真实发送参数、WS 和 402 条分页。
- 全量 `flutter test --no-pub` 201 项通过；`flutter analyze`、locale 逐字节一致及 800 行门禁全部通过。
- Android debug APK 与 iOS debug simulator 构建通过，iOS 产物包含 arm64；iPhone 17 Pro 模拟器原生集成测试通过，截图位于 `mobile/build/suggestion-screenshots/`。测试使用完全隔离的 REST/WS，不访问生产或真实模型；真机系统输入法/触摸仍待验收。
- 底部定位的回归修正了旧问答测试的滚动容器选择；分页问答用例机械拆入 `question_dock_pager_cases.dart`，保留全部原有断言并满足单文件 800 行门禁，不修改问答业务逻辑。

2026-09-10 流光占位增量（`eda8b75`）：Web 与 iOS/Android 共用 pending/completed/unavailable 状态和截止时间；原生占位与按钮同高，支持小屏横滑、键盘、明暗主题和减少动态效果。全量 308 项测试、analyze、release bundle、locale 和行数门禁通过；补齐已有导航的 3 个中英文 locale key。本次前后端已部署阿里云，原生安装包未发布。

完整配置、交互与测试命令见 [下一步建议说明](next-step-suggestions.md)。

## 12. 云电脑登录态与技能上传补齐（2026-09-10）

### 本轮实现

- 授权中心请求 `GET /api/platforms?kinds=oauth,desktop`，单独展示云电脑站点；`desktop_cookie` 账号不混入 OAuth 卡组。展示站点、昵称、店铺/角色、最后检测时间和独立的预计到期时间；待侦察站点禁用登录，只有 owner/admin 可确认退出登录。
- 「去登录」调用站点 open API，再由 app 组装层跳转云桌面；每 5 秒串行检测，最长 3 分钟，成功后停止。滚出懒加载列表仍保留等待状态；App 后台取消在途操作并暂停检测，回前台在原截止时间内恢复。退出页面或切换账号/工作空间取消请求，丢弃迟到导航和结果。
- 授权通知展示未读数量、标题与正文，只有显式点击「已读」才写入；防重复点击，成功后刷新。所有新增 API 使用已有账号/workspace 双重作用域保护。
- 技能图标支持 HTTPS 图片；加载失败显示拼图，保留 emoji 与缺省首字母图标，不把 API 认证头发给图片站点。
- 压缩包选择改为可追加的多文件队列：按文件名、大小和修改时间去重；最多 20 个、单个 32 MiB、总计 128 MiB；校验完成后才读取内容。上传逐个流式发送，成功项不重传，失败项手动重试；网络结果不明时给出提示，不自动重放。单文件自定义名称在重试时保留，批量使用各自文件名。
- 上传中禁用模式切换和关闭，页面销毁取消当前请求并停止后续文件；跨账号/workspace 不再派发队列请求。队列在压缩包/粘贴/Git/MCP 模式间切换时保留，结束后复用原有依赖补装检查。
- 从 Web 逐字节同步 en/zh `common.json` 的 `pageLoad` 文案。把超 800 行的 `question_dock_test.dart` 分出分页用例，保留同一测试入口，不重复发现用例。

### 验收证据与边界

- `flutter analyze --no-pub` 无问题；`flutter test --no-pub --reporter expanded` **194 项全部通过**（基线 173 项）。新增覆盖轮询截止/前后台/非重叠/懒加载保活/作用域切换、确认退出、通知、窄屏布局、队列去重/限额/取消/部分重试/不确定结果以及 HTTPS 图标回退。
- `./scripts/check_locales.sh`、`./scripts/check_file_size.sh` 和 `git diff --check` 通过。
- Android debug APK 与 iOS debug simulator 构建通过；iOS 可执行文件包含 `arm64` 与 `x86_64`。API 和 Web 均显式指定 `https://ai.bossipai.com.cn`。
- iPhone 17 Pro / iOS 26.5 模拟器使用指定测试账号的现有登录会话：授权中心加载 OAuth 与 5 个云电脑站点，「全部检测」返回；「去登录」打开云电脑上的抖音创作者登录页，云桌面显示通道在线，返回保留等待扫码。检测产生的失效通知可见，显式标为已读后消失；切到系统主屏再回 App，等待状态和检测正常恢复。未扫码满 3 分钟后显示超时提示、恢复重新登录入口，测试结束返回工作台。
- 模拟器检查发现站点卡片随短内容缩窄，已修为铺满面板并增加 320pt 宽度回归断言。技能我的/商店加载、压缩包队列、系统文件选择器打开/取消及粘贴/Git/MCP 模式切换均通过。
- 生产目录当前没有 HTTPS 技能图标样例，渲染分支及坏链回退使用自动化测试验证。多文件实际传输/重试与退出登录使用隔离测试，**未向生产上传、发布技能，未解绑或清除第三方站点会话**；未代用户完成第三方扫码，也未执行真实支付。测试凭证不入库。

## 明确不需要重复移植

- `4a87777` / `66bb9de` 的 1920×1080 固定分辨率：移动端 `desktop_bridge.dart` 已用 `fixedResolution`/`maxResolution` 做到等价效果。
- `7f7d487` 的 content-view 拆函数和测试 mock：行为不变。
- `99da03d..1b5dbdd` 的云电脑浏览器线程预算与修复门禁：位于后端/云桌面运行时，移动端复用同一远程服务，无需重复实现或修改本地浏览器。
- `4cd8725` 的默认项目名与项目创建时间倒序：后端统一生效，移动端无需专门改动。

## 13. 移动端超管控制台（2026-09-10）

用户明确要求实现、在 iOS 模拟器测试并优化手机交互，替代 §5 / §10 的历史 Web-only 决策。功能提交 `ad132c5`，开发基线 `a69b141`，整合 main 基线 `d10e4ee`；合并状态以 Git 记录为准，未部署。

### 已实现

- 侧边栏底部账号“更多”菜单新增“超管系统”；`/app/admin` 与 `/app/admin/:section` 双重守卫只认全局 `users.role == "admin"`。控制台及其详情、编辑、确认页处于同一个隔离 Navigator；账号、workspace 或权限变化时移除旧页面并取消未完成请求，拒绝迟到响应。
- 舰队：预热池水位与采购闸、采购预检/补齐、收养、桌面搜索/池态筛选/分页、释放/重建/退役、告警确认/静默、已有诊断快照、手动采集、日志与事件时间线。舰队仅在前台且处于当前页时每 30 秒更新；不自动采集诊断或执行写操作。
- 技能商店：分页与筛选、HTTPS 图标、新建/编辑 Skill 或 MCP、ZIP 上传队列、上下架、置顶、社区条目标记官方、批量软删除与逐项失败反馈、回收站恢复、安装历史。编辑携带原始 `expected_revision`，仅改展示信息时不发送未变化的归档内容；冲突不覆盖新版本，输入保留。
- 审核：待审核/已驳回队列、纯文本 SKILL.md、文件清单与截断/拒读提示、通过/驳回、上下架、原始 ZIP 下载。归档流式写入临时目录后交给 iOS/Android 原生保存面板，不渲染或执行包内内容、不跨源转发认证头，导出前再校验作用域。
- 安装管理：明确区分历史记录与虚拟机实际状态；选定桌面及空间成员后才手动扫描，部分失败显示“未知”而非“未安装”。卸载核对完整目录并必填原因，系统项不可卸载；失败后必须重新扫描，成功后回读状态。
- 订阅管理保持与 Web 一致的只读边界：订阅/订单搜索、渠道/状态/类型/日期筛选、分页、空间详情、订阅历史、订单、积分流水与用量。金额以整数分格式化，Decimal 积分保持服务端原值；账单不做后台轮询或焦点自动重取，切换详情页签复用同一次审计查询。

### 移动交互

三大模块保留底部导航，模块内使用轻量下划线页签。审核队列、实际安装/历史记录改用有标签的选择器，避免重复的多层 ChoiceChip。所有颜色和字号复用 appearance tokens 与 FontSizes。

- 舰队概览分开呈现预热水位、桌面分布与容量操作；点选状态进入带明确筛选标记的桌面列表。列表用分隔行，详情中的诊断入口固定到底部，危险操作逐项说明影响。
- 技能商店常驻“新增 / 上传 ZIP / 回收站 / 批量选择”文字入口；条目集中展示名称、状态和简介，管理菜单与详情/安装数入口分开。完整简介与标识在详情抽屉中可读、可复制；进入选择模式才显示复选框，删除操作固定到底部，失败项保持可见选择。
- 订阅、订单、账本和用量使用紧凑列表与响应式标签/数值排版。金额原字符串、只读边界、手动搜索及惰性加载保持不变，页面返回保留查询和列表位置。
- 编辑表单分为基本信息、商店展示和正文；保存、审核决定、纳管、筛选及危险确认均有独立底部操作区。弹层内容可以滚动，长错误有独立滚动区；键盘打开时搜索让出工具栏空间。未知保存结果也禁止在原编辑页重复提交。
- 授权中心的云电脑站点改为组内分隔行；普通技能上传复用同款页签，队列显示状态、文件大小和独立滚动区，20 个文件时仍能触达上传按钮。串行流式上传、仅失败重试、容量限制和原生导出不变。

### 功能版验收与边界（UI 重构前）

- 全量 Flutter 测试 **230 项通过**（本节新增 36 项）；覆盖 320px 布局、全局角色、权限撤销/取消迟到请求、分页查询契约、只读账单生命周期、审核拒读与原因校验、编辑冲突、ZIP multipart/原生导出及卸载失败保护。未修改前一批 194 项回归的业务期望。
- `flutter analyze --no-pub`、locale 逐字节门禁、Dart 单文件 ≤800 行、Web i18n parity 与 `git diff --check` 通过；Android debug APK、iOS simulator debug 构建通过，显式使用 `https://ai.bossipai.com.cn`。
- iPhone 17 Pro / iOS 26.5 实连生产：已登录账号显示超管菜单；舰队/桌面详情与已有诊断可读取；释放确认按钮在目标未填时禁用并取消退出；技能商店、上传界面、审核空队列可打开；搜索测试账号桌面并手动扫描成功，系统技能显示不可卸载；订阅查询、空间详情、订单金额/详情、流水、用量及“已支付”筛选正常。
- 生产审核队列为空；审核通过/驳回、编辑保存、上架、采购、释放、重建、退役、删除与卸载等写操作仅使用隔离测试验证，未在生产真实执行。未做真机验收，也未进行真实付款或发布。

### UI 重构验证与交接（2026-09-10 追加）

- 新增 16 项布局/交互回归：320px 中英文与明暗主题、长正文及标识、选择/回收站、删除失败反馈、状态下钻、安装视图惰性读取、键盘与安全区、固定审核按钮、筛选日历随作用域销毁、未知编辑结果防重复、账务查询保留、20 文件队列滚动。日期选择器明确使用超管内层 Navigator，不把弹层留在全局路由上。
- iOS 反馈收尾新增 1 项根级 `AdminConsole(skills)` 回归（包含在上述 16 项中）：外层不包 Scaffold，先聚焦并记录输入状态，之后仅改变键盘 insets。修复前复现原 `EditableTextState` 被销毁；搜索栏增加稳定 key 后，展开、调整高度、收起均保持同一 state、FocusNode、焦点、文本和选区。根级尺寸不变时底部导航仍正确隐藏/恢复，`MediaQuery.viewInsetsOf` 保持显式订阅。安装视图断言改为真实扫描端点 `/api/admin/skills/desktops/:id/skills`，保留惰性读取检查。
- 全部 admin 测试 **46 项通过**；全量 `flutter test --no-pub --reporter expanded` **246 项通过**；`flutter analyze --no-pub`、`./scripts/check_locales.sh`、`./scripts/check_file_size.sh`、`node scripts/check-i18n.mjs` 与 `git diff --check` 均通过。没有删减已有测试的业务断言。
- UI 实现由 Codex CLI 完成，组件预览使用隔离数据，输出在 `mobile/build/admin-ui/`。真实 iOS 验收发现搜索栏在键盘切换时被重建，反馈给同一 CLI 会话后补回归、修复并重新验收；生产部署另行进行。
- iPhone 17 Pro / iOS 26.5 已实连指定后端验证：全局管理员入口、舰队概览、桌面列表/详情、技能显式搜索、详情抽屉及分组编辑表单、批量选择/退出、删除确认必填原因与取消、审核队列与安装视图切换、只读订阅查询、授权中心站点分隔列表。编辑与危险确认弹层在真实软件键盘上方保持操作按钮可见；没有实际保存或删除。
- 搜索焦点修复已 hot reload 实测通过：展开软件键盘时搜索框保持同一节点与输入，次要工具栏和底部导航隐藏；使用屏幕键盘输入、删除字符并点击系统“搜索”，只读结果正确更新，键盘收起后导航恢复。暗色/中英文/320px 和长内容由隔离组件测试覆盖；未做真机或线上危险写操作测试。
- 最终 Android debug APK 与 iOS simulator debug 构建通过，均显式使用 `https://ai.bossipai.com.cn`；最终 iOS 版本已重新安装冷启动，保留登录会话。构建产生的无关 Podfile checksum 漂移已恢复。

### 合入 main 前的整合验证（2026-09-10 追加）

- 移动改动 `ad132c5` 已整合 main `d10e4ee`，保留主分支的下一步建议、移动通知和单手机会话功能；本文分节与测试拆分冲突已解决，未删减原有业务断言。
- 整合后全量 Flutter 测试 **291 项通过**；`flutter analyze --no-pub`、locale 逐字节门禁、Dart 单文件 ≤800 行、Web i18n parity 与相对 main 的 `git diff --check` 均通过。
- Android debug APK 与 iOS simulator debug 均构建成功，显式使用 `https://ai.bossipai.com.cn`。本次整合验证不包含生产部署、真机远程推送送达或线上危险写操作。

## 实施结果

1. **Locale + WorkLog**：已原子完成并增加 byte-for-byte 门禁。
2. **Workspace**：基础作用域、协作 UI、只读态与邀请入口已完成。
3. **Desktop**：status、开通/重建、长轮询、通道状态和聊天引导已完成。
4. **Billing**：查询、购买、订单恢复、两端 SDK 与生产签名接口已完成；只余商户 key 后真机实付和 iOS 上架决策。
5. **Fleet**：已按明确移动运营要求补齐原生入口与操作，见 §13。
6. **Native download**：已完成，不再借浏览器下载。
7. **Logto logout**：Web 用单次服务端 302 原子完成 OpenBox Cookie 撤销与 Logto end-session，避免先渲染 `/login` 触发自动授权的竞态；移动端同时清理 OpenBox session、Logto SSO session，并在异常路径强制删除 SDK 本地令牌。Web 登录固定 `prompt=login consent`；移动端因 SDK 使用 ephemeral 浏览会话而固定 `prompt=consent`，避免生产 Logto 把刚验证的 Android 登录误送进 end-session 中转页，同时保留 `offline_access` 的授权语义。
8. **移动端 Composer 控件**：移动端有意隐藏“执行/方案”入口，并把思考强度并入模型二级菜单；Web 保持原交互。
9. **移动端新对话与执行环境入口**：项目行常驻且右侧对齐 `+`（手机没有 hover），点击进入该项目的空会话；空会话不再根据旧 `/api/containers` 列表显示“创建沙箱”，首条消息由后端按 workspace 自动连接无影，真正未就绪时继续走 `DESKTOP_NOT_READY` 云桌面引导。
10. **订阅驱动无影云**：全局进度、Free 权益边界、到期断开查看连接、作用域隔离、侧栏直达与阿里云 prod 联调已完成，替代旧的手动开通流程，见 §8。
11. **Android Logto 单一回调**：邀请链接只保留生产 HTTPS App Link；删除 MainActivity 上缺少 host 的自定义 scheme 过滤器。Android 在过滤器未声明 host 时会忽略 `pathPrefix`，旧配置因此同时匹配 `com.bossip.bipmobile://callback`，形成两个授权跳转入口。2026-09-09 起仅由 `.AuthCallbackActivity` 接收：仍交给 SDK 验证 OAuth，并显式恢复原 App task，修复授权成功后 Chrome 仍覆盖 App 的问题（详见 [Logto 配置与回跳契约](LOGTO_PROD.md#android-callback-task-restoration-1016--2026-09-09)）。
12. **超管系统 + 技能商店**：移动超管三大模块已实现并重排手机交互，取代历史 Web-only 决策，见 §13。用户侧来源分区/审核文案见 §10，HTTPS 图标与批量压缩包上传见 §12。
13. **云电脑登录态 + 通知**：移动端已补齐站点登录、检测、轮询、确认退出、预计到期与通知已读；iOS 模拟器实连生产验证登录页跳转、等待状态及通知，见 §12。

## 完成记录

| 日期 | 功能块 | 提交 | 验收证据 |
|---|---|---|---|
| 2026-09-10 | Codex CLI 移动 UI 重构与键盘焦点修复 | 本轮 worktree，基线 `a69b141` | 246 项测试、静态分析、翻译/800 行门禁、双端 debug 构建；iOS 26.5 真实键盘、表单、超管与只读查询验收，详见 §13 |
| 2026-09-10 | 移动超管：舰队 / 技能 / 订阅 | 本轮 worktree，基线 `a69b141` | 230 项 Flutter 测试、静态分析、locale/800 行门禁、双端构建；iOS 实连生产验证菜单与只读流程，危险写操作隔离测试，详见 §13 |
| 2026-09-10 | 云电脑登录态、通知、HTTPS 技能图标与批量上传 | 本轮 worktree，基线 `a69b141` | 194 项 Flutter 测试、analyze、locale/800 行门禁、Android/iOS 构建；iOS 26.5 实连生产检查登录页跳转、通知已读与原生文件选择器，完整边界见 §12 |
| 2026-09-10 | 双端通知 API 与单手机登录 | `codex/mobile-notifications`，未部署 | APNs/极光桥接与设置入口、最新手机登录生效、通知换绑与投递队列；PostgreSQL 迁移及多进程并发测试、Flutter 回归、Android debug/iOS simulator 构建。已接主任务终态、待处理、定时结果及可信发布回调；明确 resumed/inactive/后台/超时离线边界、前台抑制与取消队列。真机远程送达及 Android 厂商通道仍待验收，见 [移动通知说明](MOBILE_NOTIFICATIONS.md) |
| 2026-09-10 | 原生下一步建议 | `codex/next-step-suggestions`，生产部署另行进行 | 201 项 Flutter 测试（新增 28 项）、analyze、locale/行数门禁、Android APK 与 iOS arm64 simulator 构建通过；iPhone 原生集成回归和截图通过，使用隔离 fixtures，真机待验收 |
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
