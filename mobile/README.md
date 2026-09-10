# bossip mobile

OpenBox `frontend-v2`(bossip)的 Flutter 原生移动端,按 web 版 1:1 移植、针对手机尺寸重排。

Web 已上线但移动端尚未跟进的能力、依赖与验收标准，统一维护在
[`docs/MOBILE_WEB_PARITY.md`](../docs/MOBILE_WEB_PARITY.md)；完成移植时必须同步更新该清单。

## 运行

```bash
# 后端(仓库根目录;依赖 docker 里的 postgres/redis)
cd backend && uv run uvicorn main:app --host 0.0.0.0 --port 8080

# App 默认连接阿里云生产；本地联调时显式覆盖
cd mobile && flutter run --dart-define=API_BASE=http://localhost:8080
# Android 模拟器访问宿主机用 http://10.0.2.2:8080
```

本地联调账号:`devtest / devtest1234`。API 地址通过 `--dart-define=API_BASE=…` 覆盖；默认值为生产 `https://ai.bossipai.com.cn`，避免安装包误连设备自己的 localhost。

### 2026-09-10：移动端超管控制台

- 平台 admin 可从侧边栏底部账号“更多 → 超管系统”进入舰队、技能与订阅管理，普通用户和 workspace owner 不显示入口。
- 底部导航配合轻量页签，审核/安装的次级选择器取代重复标签栏；舰队概览分区、紧凑列表行、技能详情抽屉、显式回收站/批量选择、分组表单及固定底部操作适配手机。授权中心云桌面站点和上传队列也统一了密度与字号；支持完整技能运营、审核、安装管理及舰队操作，订阅/订单/账本/用量与 Web 一样只读。
- 230 项 Flutter 测试、静态分析、locale/800 行门禁及双端 debug 构建通过。iOS 26.5 实连生产验证菜单、舰队查询、技能页面及订阅/订单/用量；危险写操作使用隔离数据测试，不在生产真实执行。完整范围见 [对齐清单 §12](../docs/MOBILE_WEB_PARITY.md#12-移动端超管控制台2026-09-10)。
- 同日经 Codex CLI 完成 UI 重构，最终全量 **246 项测试通过**（UI 重构新增 16 项，admin 共 46 项），analyze、locale 字节一致、800 行门禁、Web i18n 和 diff 检查均通过；Android debug 与最终 iOS simulator debug 构建通过。iPhone 17 Pro / iOS 26.5 已实测超管入口、舰队/桌面、技能搜索与详情、分组编辑表单、批量选择及取消确认、审核/安装视图、只读订阅查询和授权中心。真实软件键盘曾暴露搜索框重建丢失焦点，补根级回归并修复后，屏幕键盘输入、删除、搜索提交及导航恢复均通过；编辑与危险确认的底部按钮也保持可见。未向生产提交修改、删除、发布、购买或卸载；未知编辑结果仍须退出并回读，不能在原页重复提交。

### 2026-09-10：云电脑登录态与技能上传对齐

- 授权中心补齐云电脑站点登录、检测、等待扫码、预计到期、确认退出与失效通知；账号/workspace 切换取消旧请求，后台暂停检测，回前台按原截止时间恢复。
- 技能图标支持 HTTPS 图片与失败回退；压缩包支持多文件队列、串行流式上传、部分失败重试和容量限制，保留粘贴/Git/MCP 模式。
- 194 项 Flutter 测试、静态分析、locale/800 行门禁及 Android/iOS debug 构建通过；iOS 26.5 模拟器实连生产检查云桌面登录页跳转、通知已读与文件选择器。测试范围、生产写入边界见 [`对齐清单 §11`](../docs/MOBILE_WEB_PARITY.md#11-云电脑登录态与技能上传补齐2026-09-10)。

### 2026-09-07：订阅开通与阿里云联调

本轮 iOS 模拟器构建显式连接阿里云（HTTPS/WSS 同源），不连接 AWS 或本机后端：

```bash
cd mobile
flutter build ios --simulator --debug \
  --dart-define=API_BASE=https://ai.bossipai.com.cn \
  --dart-define=WEB_BASE=https://ai.bossipai.com.cn
```

- Free 可继续普通对话；云桌面、终端、浏览器和文件入口显示订阅提示，不再提供旧的手动创建沙箱按钮。
- 付款确认后由后端自动准备云电脑；全局开通弹窗读取持久化进度，退出页面/切到后台不会取消任务，回前台重新查询。
- 侧栏“云桌面”可直接打开已有云电脑，不必先创建对话。SDK ticket、状态、就绪确认按账号和工作空间隔离。
- 订阅到期或切换作用域时停止当前 SDK 查看连接并撤掉工具面板；不会关机、删除或释放原云电脑。
- 套餐价格只读后端目录，当前 Pro/Max 月付和年付总价均为 0.10 元。阿里云 `APP_ENV=prod` 不会自动恢复原价。
- 模拟器可验证页面、状态和云桌面连接，但不会执行真实支付；支付宝原生客户端唤起仍须在安装支付宝的真机上测试。

### 2026-09-07：授权中心与抖音投稿

- 侧栏“资源中心”下新增“授权中心”，覆盖工作空间多账号、检测、到期提示、重新授权和确认解绑；绑定/解绑限 owner/admin，成员可检测及投稿。
- 绑定优先显式打开手机上的抖音授权页，保留原生产 OAuth 回调；授权完成后返回 App 查看服务端账号状态。二维码可作为备选，支持原生保存 PNG，不把“已打开抖音”当成成功。
- 投稿从资源中心选视频，填写标题、话题和公开范围；生成后可打开抖音或用二维码，最终发布仍由用户在抖音确认。后台暂停轮询，回前台重新查询；失败的 POST 不自动重放。
- 最近投稿来自服务端，支持杀进程后恢复任务状态；短期签名链接只留内存，不自动重新生成，冷启动后可查结果但不能恢复已丢失的快捷链接。
- 聊天投稿二维码保持完整正方形，下方显示授权/结果入口。新后端工具 metadata 的 `expiresAt` / `launchUrl` 提供同机快捷打开；旧后端和历史消息保持二维码兜底。
- 本轮通过 65 项 Flutter 回归、23 项相关后端测试和 Android/iOS debug 构建；iOS 模拟器已连接生产验证页面及二维码保存流程。真实授权、抖音原生唤起和实际发布仍待真机验收，本轮代码未部署。详细范围与证据见对齐清单 §9。

## 单点登录(Logto)

登录/注册两屏由 `SsoGate` 接管,走 [`logto_dart_sdk`](https://pub.dev/packages/logto_dart_sdk):
SDK 在系统浏览器里跑 PKCE,拿到 id_token 后交给 `POST /api/auth/logto/id-token`,
服务端验签(JWKS)再发 OpenBox 自己的 JWT —— 与 web 落在同一个账号(都按 Logto 的 `sub` 归并)。

生效需要两件事,缺任一条 app 就退回账号密码表单(部署没接 SSO 时也是这个表单):

1. Logto 控制台建一个 **Native** 应用(手机存不住 client secret,不能复用 web 那个
   Traditional Web 应用),回调地址填 `com.bossip.bipmobile://callback` ——
   与 `Env.ssoRedirectUri`、`AndroidManifest.xml` 里 CallbackActivity 的 scheme 一致;
   换 scheme 用 `--dart-define=SSO_REDIRECT_URI=…`,三处要一起改。
2. 后端 `.env` 填 `LOGTO_NATIVE_APP_ID=<该 Native 应用的 App ID>`,它同时是
   id_token 的第二个合法 audience。

退出也必须把同一个 URI 注册到 Logto Native 应用的 **Post sign-out redirect
URIs**。`AuthController.signOut()` 先撤销 OpenBox refresh cookie 并清理本地
用户/workspace/WS，再调用 Logto SDK `signOut()` 结束浏览器 SSO 会话；SDK
令牌在失败兜底里仍会删除。默认退出回跳是
`Env.ssoPostLogoutRedirectUri = com.bossip.bipmobile://callback`，可用
`--dart-define=SSO_POST_LOGOUT_REDIRECT_URI=…` 覆盖。

登录请求固定使用 `prompt=consent`，保留 SDK 请求离线 refresh token 所需的授权
语义。`logto_dart_sdk` 4 / `flutter_web_auth_2` 5 在 Android 使用 ephemeral Custom
Tab，退出时也会显式完成 Logto end-session；不要额外追加 `login`。生产实测该值会把
刚通过密码验证的交互送进 `/oidc/session/end/confirm`，并卡在空白的
`Submitting Callback` 页面，无法正常回跳 App。

iOS 无需额外 SSO 配置(ASWebAuthenticationSession 直接吃 callbackUrlScheme)。当前生产 Native App ID、redirect 与本地管理隧道见 [`docs/LOGTO_PROD.md`](../docs/LOGTO_PROD.md)。

最低 iOS 版本为 **14.0**(2026-08-26 由 13.0 上调):`file_picker` 12 的 darwin 实现要求 14.0,而 10/11 与 `flutter_secure_storage` 11 的 win32 约束冲突、装不上。

## 与 frontend-v2 的对应关系

| web | mobile |
|---|---|
| `src/styles/tokens.css`(`--t-*` 8 主题×明暗) | `lib/shared/appearance/tokens.dart`(`BossipTokens` ThemeExtension,逐色值 1:1) |
| `html[data-fs]` 根字号 92/100/109/120% | `MediaQuery.textScaler`(`type_scale.dart`) |
| `shared/appearance/store.ts` | `lib/shared/appearance/appearance_store.dart`(同样的 `bossip:appearance` 持久化键 + `PUT /api/auth/me/preferences` 尽力同步) |
| `src/locales/*/*.json`(i18next) | `assets/locales/`(**逐字节复制,勿单独修改**)+ `lib/shared/i18n/i18n.dart`(`ns:block.element`、`_one/_other` 复数、`{{var}}` 插值) |
| `shared/api/http.ts`(bearer + workspace + 401→refresh→重试一次) | `lib/shared/api/http_client.dart`(dio 拦截器,注入 `X-Workspace-Id`;refresh cookie 走 PersistCookieJar,且用户/workspace 任一切换都不重放旧请求) |
| `shared/ws/client.ts`(ticket 握手、指数退避) | `lib/shared/ws/ws_client.dart` |
| `features/chat/stores/stream.ts`(增量累积 + 快照单调合并) | `lib/features/chat/state/stream_store.dart` |
| `features/chat/lib/turn-view.ts`(连续 assistant 合并为一轮,traces 聚合) | `lib/features/chat/utils/turn_view.dart` |
| streamdown 流式 markdown | `gpt_markdown` + `flutter_highlight`(`widgets/markdown_view.dart`) |
| `features/chat/lib/content-view.ts`(答复/过程分离、产物成组) | `lib/features/chat/utils/content_view.dart`(同一套 `channel`/`finish` 判定与 group_id 规则) |
| `WorkLogTrace`(过程说明 + 工作证据时间线，内联常开) | `widgets/traces/work_log_trace.dart`(同列常开并按顺序累积；computer 截图同样只留首/中/末三帧) |
| `ResultArtifacts` / `AudioPreview` | `widgets/result_artifacts.dart` / `widgets/audio_preview.dart`(音频用 `video_player`,平台自带 AVPlayer/ExoPlayer 就能放,不多引一个播放依赖) |
| `ToolOutput` + `ToolPrimitives`(9 种工具详情布局) | `widgets/traces/tool_output.dart` + `tool_primitives.dart`;web 详情列常驻展开,手机一行放不下,所以保留"点一下展开" |
| `SkillJobReceipts`(历史终态作业的只读回执 chip + embedded artifact 预览/下载;live dock/API 已退役) | `widgets/cards/skill_job_receipt.dart` + `shared/models/message_part.dart` 的 `SkillJobPart`/`SkillJobArtifact` 分支;媒体复用 `AttachmentGallery`,普通文件复用 `FileChipRow` |
| `SubagentLine`(子 agent 实时进度) | `widgets/traces/subagent_line.dart` + `state/subagent_progress.dart`(同样读子会话在 stream store 里的 part,只在为空时补一次 REST) |
| `ThinkingRow`(等待/重试行) | `widgets/typing_row.dart`(三点脉冲 + `status.thinking`/`status.retrying`) |
| `RunErrorNotice`(失败原因常驻在输入框上方) | `widgets/run_error_notice.dart` |
| `Toast.tsx`(顶部卡片、按字数计算停留时长、可关闭) | `shared/widgets/toast.dart`(同样的 info/success/warning/error 与去重规则) |
| 技能中心(`features/skills-center`,双栏 + 弹窗) | `/app/skills`(`SkillsScreen`):我的/商店切页 + 类型 chip + 搜索,五个弹窗全部改成底部抽屉;技能包折叠、依赖补装、发布确认、聊天创建都在 |
| 左侧 Sidebar | 抽屉 `SessionDrawer`；手机没有 hover，项目行常驻显示 `+` 新建对话入口，不把核心动作藏在长按菜单里 |
| Workspace switcher / Team / invite | `active_workspace_store.dart` + 抽屉切换器 + 设置页 Team + `/invite/:token`；所有业务缓存按当前 workspace 隔离，他人会话只读 |
| Logto logout | `shared/api/logto_session.dart` + `auth_store.dart`：OpenBox session 与 Logto SSO session 同时退出；移动端登录使用 `prompt=consent`，由 ephemeral 浏览会话与显式 end-session 避免复用旧账号；Android 完成回跳，iOS ephemeral session 与安全存储令牌一并清理 |
| `/app/billing/:tab?` | `/app/billing/:tab` 订购/用量/订单三页；余额显示在用户行，订单回前台主动核对；支付宝 Android/iOS 原生 SDK 接线见 `docs/BILLING_PLAN.md` §6 |
| `/app/auth-center` / `douyin_publish` 二维码 | `features/auth_center` + `shared/api/platform_accounts_api.dart`：多账号、同机授权/投稿、QR 原生保存、最近投稿及回前台状态恢复；聊天 QR 完整显示并附快捷操作 |
| 右侧 WorkbenchPanel(菜单 tab + 审阅/终端/浏览器/文件/云桌面/定时) | 路由 `/app/w/:sessionId` = **菜单页**(`WorkbenchScreen` + `WorkbenchMenu`,与 web `MenuTab` 同一份入口与实时提示);点一行 push `WorkbenchSurfacePage` —— 手机没有 tab 条,返回手势和返回箭头就是 web 那条 tab 条的替代 |
| DesktopTab(Wuying Web SDK) | `desktop_bridge.dart`(SDK 引导页 + JS 桥)+ `desktop_tab.dart`(Flutter UI)。原生轮询 `/api/desktop/ticket`(202→task_id 重试),WebView 装载 SDK,JS channel 回报 connected/error。**桌面固定 1920×1080**:客户端用 `uiConfig.fixedResolution`/`maxResolution` 锁住分辨率 —— 手机的视口一直在变(旋转/全屏/键盘),不锁住 SDK 会反过来把远端分辨率改掉,agent 看的桌面就在它脚下变形了;iframe **直接定尺**而不是 CSS transform 缩放,变换过的画面会让 SDK 观测到与手指落点不同的坐标系。**横屏全屏**:`SystemChrome` 切 landscape + `immersiveSticky`,`onImmersive` 回调让 `WorkbenchSurfacePage` 摘掉 AppBar(不 push 新路由 —— 重新挂载 WebView 会把流打断);退出/dispose 都恢复竖屏。**指针**:`setMouseMode('Client')` 绝对坐标,手指点哪就点哪(相对模式需要指针锁定,WebView 给不了,实测点击直接失效,所以不提供)。**键盘**:`session.openSoftKeyboard(true)` 打开 SDK 自带的画面内键盘 —— 这是文字进 guest 的唯一通道,带 Esc/F1-F12/Ctrl/Alt 和切 guest 输入法的 中/En 键 |
| BrowserTab(dev-browser 截图流) | `browser_tab.dart`:原生 WS 客户端,JPEG 帧 → `Image.memory`(gapless),点击/滚动映射回页面像素坐标,4004 → 无沙箱 |
| `EmptyChatRoute` | 空会话直接发送首条消息，Free 或开通中均不阻断普通 LLM 对话；不显示旧 Docker 时代的“创建沙箱”卡。真正需要 sandbox 的工具由后端返回权限/未就绪错误，移动端用对应文案展示 |
| Composer 的 ReasoningPicker(思考强度) | `utils/reasoning.dart`(纯函数 `resolveReasoning`,判定与 web hook 逐条一致)+ `picker_sheets.dart` 的 `showReasoningPicker`;只有声明了 variants 的模型才出这个胶囊。Dart 没有 `undefined`,所以用 `Variant?` 包装三态:**不传**=保留会话已存的强度,**`Variant(null)`**=显式清空回模型默认,**`Variant('high')`**=本轮用这一档 |
| Composer 的 `/`、`@` 提及菜单 | `utils/mention.dart`(触发规则逐条移植)+ `mention_menu.dart`;文件搜索 160ms 防抖,技能/命令同款分组;资源段由 app 层经 `ComposerResourceSlot` 注入(特性之间不互相 import) |
| 资源中心(`features/resources`,三栏) | `/app/resources`(`ResourcesScreen`):项目 chip + 来源 chip 折叠成两行筛选条,详情页 `ResourceDetailPage` 取代第三栏(图片/视频/音频/文本预览、改名/下载/删除);下载留在 App 内并调系统保存面板,长按出操作单,`+` 走 `file_picker` 直传 OSS |
| hover meta 操作行(复制/点赞/点踩/重生成/复刻) | 长按助手轮 → 操作单(`turn_actions_sheet.dart`) |
| `AttachmentGallery`(图片附件宫格 + Lightbox) | `attachment_gallery.dart`:同款宫格/折叠/全屏(捏合缩放 + 原生保存);**视频**额外支持内联播放(web 只降级为胶囊);资产 URL 经 `/api/assets/{id}/url` 缓存 40 分钟 |
| Settings 路由(账号/团队/模型/浏览器/外观) + 独立 Billing 路由 | `/app/settings`(账号/团队/外观/模型；浏览器设置为桌面范畴) + `/app/billing/:tab` |

## 分层(镜像 web ENGINEERING_SPEC §3/§4)

```
lib/
  app/        # 组装层:根组件、路由、workspace 壳 —— 只有这里能同时 import 多个 feature
  features/   # auth / auth_center / billing / chat / resources / workspace / workbench / skills / settings / landing
              #   各自 api/ state/ widgets/ utils/;feature 之间禁止互相 import,
              #   跨特性用 shared/events/bus.dart(事件:workspace.refresh、workbench.open)
  shared/     # api(dio/auth)、ws、models(后端 snake_case 契约)、appearance、i18n、router/paths、widgets、utils
```

规则:依赖方向 `app → features → shared` 单向;单文件 ≤ 800 行(`scripts/check_file_size.sh` 门禁);颜色只从 `context.tokens` 取,不许写死。

特性之间要协作时,由 **app 层拼装**(web 在 `routes/` 做同一件事):chat 侧声明它需要的形状(`ComposerResourceSlot`,只用 `shared/models` 里的类型),`features/resources` 出组件,`app/router.dart` 把两者接起来。

## 协议要点(实现时容易踩的)

- WS 信封 `{type,data}`:路由字段 camelCase(`sessionId`/`messageId`/`partId`),内嵌 message/part 对象 snake_case —— 两层分开解析,勿全局转换。
- 授权中心 REST 的账号和投稿响应使用 camelCase；投稿请求使用 `file_asset_id` / `private_status` 等 snake_case，不能复用全局键名转换。签名 schema / OAuth state 是短期能力，不记录日志、不写偏好设置。
- `message.text_delta` 与 `part.delta` 都是**追加**语义;快照合并时 text/reasoning 取更长者、tool 取更高状态(pending<running<completed=error),防止 UI 回退。
- 权限回复动作用后端原生值 `once`/`always`/`reject`(web 端目前发的 `allow/allow_always` 会被 REST 400 拒)。
- 问题回复 `answers` 是嵌套数组:每个问题一个 label 数组,按序。
- 运行中每 1s 轮询 messages+session 快照兜底(WS 重连只补 `session.status`,不重放 delta)。
- 终端 WS:二进制帧 1 字节 tag + 载荷,`0x00` 数据、`0x01` resize(cols/rows 各为大端 uint16);文本帧只有 `{type:"error"}`。

## 提交前校验

```bash
cd mobile
./scripts/check_locales.sh
flutter analyze
flutter test
./scripts/check_file_size.sh
flutter build apk --debug
flutter build ios --simulator --debug
```

iOS 模拟器产物必须含 `arm64`；支付宝 SDK 按当前测试策略只进入 Profile/Release 真机配置，Debug 模拟器使用外部收银台回退。`AlipaySDK-iOS 15.8.30` 本身已有 arm64 simulator slice，但模拟器没有支付宝客户端；即使 Debug 链接 SDK，也只能验证桥接/H5 兜底，原生客户端唤起必须在安装了支付宝的 iPhone 上验收。资源和聊天附件下载通过 `shared/download/native_download.dart` 流式落临时文件，再交给 iOS Document Picker / Android `ACTION_CREATE_DOCUMENT`，不得重新改成 `url_launcher`。

## 移动端偏差记录(对齐 web 附录 D 的做法:能力不具备则省略控件)

Web 新功能造成的临时失步统一记录在
[`docs/MOBILE_WEB_PARITY.md`](../docs/MOBILE_WEB_PARITY.md)，本节只保留由手机形态或平台能力导致的长期差异。

- 模型选择器显示 provider 名称,不画 `ModelLogo` 的各家矢量图标(信息等价,少一个 SVG 依赖)。
- 技能中心的"下载 ZIP"落到 app 的 Documents 目录并把路径回报在 toast 里 —— 手机没有下载栏,路径就是唯一有用的回执。
- 附件**上传**已接(资源中心与 composer 的 `+` 都走 `file_picker` → 预签名 PUT 直传 OSS,不经后端;8MB legacy 兜底不需要)。附件**下载**走两端原生保存面板、不跳浏览器。语音输入仍缺;屏幕截图入口是桌面范畴,省略。附件**展示**已齐:图片宫格 + Lightbox + 视频播放。
- 顶栏"分享"(web 为复制 URL)在移动端无意义,省略。
- 云桌面:剪贴板开关/文件上传未做(剪贴板在连上时直接打开,操控开关、横屏全屏、画面内键盘已有);浏览器 tab 的键盘输入未做(截图流点击/滚动/导航已有)。
- **手机输入法直通云桌面做不了**(2026-08-31 逐条验证):无影 Web SDK 的全部消息接口(62 个)只有输入开关、指针、分辨率、文件和剪贴板,**没有任何注入文本/按键的 API**;`customASPAction` 只能打到 `htmlEngine` 上,`setImeCommit`/`sendKeyDown` 不在那上面。另一头 WKWebView 也不会为 SDK 那个跨域 iframe 里的隐藏 IME 输入框弹 iOS 软键盘(程序化 focus 不满足用户手势要求)—— 实测点云端输入框只拿到光标,键盘不弹。要真做端云一体输入法只有两条路:换无影原生 ASP iOS SDK,或后端加一个 `xdotool type` 接口。当前方案是用 SDK 自带的画面内键盘。
- 全 app 锁竖屏(`main.dart` 的 `setPreferredOrientations`),只有云桌面全屏例外 —— 其余页面都是按竖屏排的,自由旋转是以前没设置而已。
- 消息列表 >50 行虚拟化未做专门处理(ListView.builder 本身惰性构建)。
