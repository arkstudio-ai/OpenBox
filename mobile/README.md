# bossip mobile

OpenBox `frontend-v2`(bossip)的 Flutter 原生移动端,按 web 版 1:1 移植、针对手机尺寸重排。

## 运行

```bash
# 后端(仓库根目录;依赖 docker 里的 postgres/redis)
cd backend && uv run uvicorn main:app --host 0.0.0.0 --port 8080

# App(iOS 模拟器直连宿主 localhost;Android 模拟器需 --dart-define=API_BASE=http://10.0.2.2:8080)
cd mobile && flutter run
```

本地联调账号:`devtest / devtest1234`。API 地址通过 `--dart-define=API_BASE=…` 覆盖(默认 `http://localhost:8080`)。

最低 iOS 版本为 **14.0**(2026-08-26 由 13.0 上调):`file_picker` 12 的 darwin 实现要求 14.0,而 10/11 与 `flutter_secure_storage` 11 的 win32 约束冲突、装不上。

## 与 frontend-v2 的对应关系

| web | mobile |
|---|---|
| `src/styles/tokens.css`(`--t-*` 8 主题×明暗) | `lib/shared/appearance/tokens.dart`(`BossipTokens` ThemeExtension,逐色值 1:1) |
| `html[data-fs]` 根字号 92/100/109/120% | `MediaQuery.textScaler`(`type_scale.dart`) |
| `shared/appearance/store.ts` | `lib/shared/appearance/appearance_store.dart`(同样的 `bossip:appearance` 持久化键 + `PUT /api/auth/me/preferences` 尽力同步) |
| `src/locales/*/*.json`(i18next) | `assets/locales/`(**逐字节复制,勿单独修改**)+ `lib/shared/i18n/i18n.dart`(`ns:block.element`、`_one/_other` 复数、`{{var}}` 插值) |
| `shared/api/http.ts`(bearer + 401→refresh→重试一次) | `lib/shared/api/http_client.dart`(dio 拦截器,refresh cookie 走 PersistCookieJar) |
| `shared/ws/client.ts`(ticket 握手、指数退避) | `lib/shared/ws/ws_client.dart` |
| `features/chat/stores/stream.ts`(增量累积 + 快照单调合并) | `lib/features/chat/state/stream_store.dart` |
| `features/chat/lib/turn-view.ts`(连续 assistant 合并为一轮,traces 聚合) | `lib/features/chat/utils/turn_view.dart` |
| streamdown 流式 markdown | `gpt_markdown` + `flutter_highlight`(`widgets/markdown_view.dart`) |
| `features/chat/lib/content-view.ts`(答复/过程分离、产物成组) | `lib/features/chat/utils/content_view.dart`(同一套 `channel`/`finish` 判定与 group_id 规则) |
| `WorkLogTrace`(过程说明 + 工作证据时间线) | `widgets/traces/work_log_trace.dart`(computer 截图同样只留首/中/末三帧) |
| `ResultArtifacts` / `AudioPreview` | `widgets/result_artifacts.dart` / `widgets/audio_preview.dart`(音频用 `video_player`,平台自带 AVPlayer/ExoPlayer 就能放,不多引一个播放依赖) |
| `ToolOutput` + `ToolPrimitives`(9 种工具详情布局) | `widgets/traces/tool_output.dart` + `tool_primitives.dart`;web 详情列常驻展开,手机一行放不下,所以保留"点一下展开" |
| `SkillJobReceipts`(历史终态作业的只读回执 chip + embedded artifact 预览/下载;live dock/API 已退役) | `widgets/cards/skill_job_receipt.dart` + `shared/models/message_part.dart` 的 `SkillJobPart`/`SkillJobArtifact` 分支;媒体复用 `AttachmentGallery`,普通文件复用 `FileChipRow` |
| `SubagentLine`(子 agent 实时进度) | `widgets/traces/subagent_line.dart` + `state/subagent_progress.dart`(同样读子会话在 stream store 里的 part,只在为空时补一次 REST) |
| `ThinkingRow`(等待/重试行) | `widgets/typing_row.dart`(三点脉冲 + `status.thinking`/`status.retrying`) |
| `RunErrorNotice`(失败原因常驻在输入框上方) | `widgets/run_error_notice.dart` |
| `Toast.tsx`(顶部卡片、按字数计算停留时长、可关闭) | `shared/widgets/toast.dart`(同样的 info/success/warning/error 与去重规则) |
| 技能中心(`features/skills-center`,双栏 + 弹窗) | `/app/skills`(`SkillsScreen`):我的/商店切页 + 类型 chip + 搜索,五个弹窗全部改成底部抽屉;技能包折叠、依赖补装、发布确认、聊天创建都在 |
| 左侧 Sidebar | 抽屉 `SessionDrawer` |
| 右侧 WorkbenchPanel(菜单 tab + 审阅/终端/浏览器/文件/云桌面/定时) | 路由 `/app/w/:sessionId` = **菜单页**(`WorkbenchScreen` + `WorkbenchMenu`,与 web `MenuTab` 同一份入口与实时提示);点一行 push `WorkbenchSurfacePage` —— 手机没有 tab 条,返回手势和返回箭头就是 web 那条 tab 条的替代 |
| DesktopTab(Wuying Web SDK) | `desktop_bridge.dart`(SDK 引导页 + JS 桥)+ `desktop_tab.dart`(Flutter UI)。原生轮询 `/api/desktop/ticket`(202→task_id 重试),WebView 装载 SDK,JS channel 回报 connected/error。**桌面固定 XGA 1024×768**:后端 `obx-display` 钉死,客户端再用 `uiConfig.fixedResolution` 兜住 —— 手机的视口一直在变(旋转/全屏/键盘),不锁住 SDK 会反过来把远端分辨率改掉,agent 看的桌面就在它脚下变形了;iframe **直接定尺**而不是 CSS transform 缩放,变换过的画面会让 SDK 观测到与手指落点不同的坐标系。**横屏全屏**:`SystemChrome` 切 landscape + `immersiveSticky`,`onImmersive` 回调让 `WorkbenchSurfacePage` 摘掉 AppBar(不 push 新路由 —— 重新挂载 WebView 会把流打断);退出/dispose 都恢复竖屏。**指针**:`setMouseMode('Client')` 绝对坐标,手指点哪就点哪(相对模式需要指针锁定,WebView 给不了,实测点击直接失效,所以不提供)。**键盘**:`session.openSoftKeyboard(true)` 打开 SDK 自带的画面内键盘 —— 这是文字进 guest 的唯一通道,带 Esc/F1-F12/Ctrl/Alt 和切 guest 输入法的 中/En 键 |
| BrowserTab(dev-browser 截图流) | `browser_tab.dart`:原生 WS 客户端,JPEG 帧 → `Image.memory`(gapless),点击/滚动映射回页面像素坐标,4004 → 无沙箱 |
| Composer 的 ReasoningPicker(思考强度) | `utils/reasoning.dart`(纯函数 `resolveReasoning`,判定与 web hook 逐条一致)+ `picker_sheets.dart` 的 `showReasoningPicker`;只有声明了 variants 的模型才出这个胶囊。Dart 没有 `undefined`,所以用 `Variant?` 包装三态:**不传**=保留会话已存的强度,**`Variant(null)`**=显式清空回模型默认,**`Variant('high')`**=本轮用这一档 |
| Composer 的 `/`、`@` 提及菜单 | `utils/mention.dart`(触发规则逐条移植)+ `mention_menu.dart`;文件搜索 160ms 防抖,技能/命令同款分组;资源段由 app 层经 `ComposerResourceSlot` 注入(特性之间不互相 import) |
| 资源中心(`features/resources`,三栏) | `/app/resources`(`ResourcesScreen`):项目 chip + 来源 chip 折叠成两行筛选条,详情页 `ResourceDetailPage` 取代第三栏(图片/视频/音频/文本预览、改名/下载/删除);长按出操作单,`+` 走 `file_picker` 直传 OSS |
| hover meta 操作行(复制/点赞/点踩/重生成/复刻) | 长按助手轮 → 操作单(`turn_actions_sheet.dart`) |
| `AttachmentGallery`(图片附件宫格 + Lightbox) | `attachment_gallery.dart`:同款宫格/折叠/全屏(捏合缩放 + 下载);**视频**额外支持内联播放(web 只降级为胶囊);资产 URL 经 `/api/assets/{id}/url` 缓存 40 分钟 |
| Settings 路由(6 个 tab) | `/app/settings`(账号/外观/模型三段;用量/工具/浏览器设置为桌面范畴) |

## 分层(镜像 web ENGINEERING_SPEC §3/§4)

```
lib/
  app/        # 组装层:根组件、路由、workspace 壳 —— 只有这里能同时 import 多个 feature
  features/   # auth / chat / workspace / workbench / skills / settings / landing
              #   各自 api/ state/ widgets/ utils/;feature 之间禁止互相 import,
              #   跨特性用 shared/events/bus.dart(事件:workspace.refresh、workbench.open)
  shared/     # api(dio/auth)、ws、models(后端 snake_case 契约)、appearance、i18n、router/paths、widgets、utils
```

规则:依赖方向 `app → features → shared` 单向;单文件 ≤ 800 行(`scripts/check_file_size.sh` 门禁);颜色只从 `context.tokens` 取,不许写死。

特性之间要协作时,由 **app 层拼装**(web 在 `routes/` 做同一件事):chat 侧声明它需要的形状(`ComposerResourceSlot`,只用 `shared/models` 里的类型),`features/resources` 出组件,`app/router.dart` 把两者接起来。

## 协议要点(实现时容易踩的)

- WS 信封 `{type,data}`:路由字段 camelCase(`sessionId`/`messageId`/`partId`),内嵌 message/part 对象 snake_case —— 两层分开解析,勿全局转换。
- `message.text_delta` 与 `part.delta` 都是**追加**语义;快照合并时 text/reasoning 取更长者、tool 取更高状态(pending<running<completed=error),防止 UI 回退。
- Web/Mobile 权限回复统一使用后端原生值 `once`/`always`/`reject`。
- 问题回复 `answers` 是嵌套数组:每个问题一个 label 数组,按序。
- 运行中每 1s 轮询 messages+session 快照兜底(WS 重连只补 `session.status`,不重放 delta)。
- 终端 WS:二进制帧 1 字节 tag + 载荷,`0x00` 数据、`0x01` resize(cols/rows 各为大端 uint16);文本帧只有 `{type:"error"}`。

## 移动端偏差记录(对齐 web 附录 D 的做法:能力不具备则省略控件)

- SSO/Logto 登录未做(当前 web LoginForm 也已移除 SSO 按钮;移动端需要 PKCE + 回调 scheme,后续补)。
- 模型选择器显示 provider 名称,不画 `ModelLogo` 的各家矢量图标(信息等价,少一个 SVG 依赖)。
- 技能中心的"下载 ZIP"落到 app 的 Documents 目录并把路径回报在 toast 里 —— 手机没有下载栏,路径就是唯一有用的回执。
- 附件**上传**已接(资源中心与 composer 的 `+` 都走 `file_picker` → 预签名 PUT 直传 OSS,不经后端;8MB legacy 兜底不需要)。语音输入仍缺;屏幕截图入口是桌面范畴,省略。附件**展示**已齐:图片宫格 + Lightbox + 视频播放。
- 顶栏"分享"(web 为复制 URL)在移动端无意义,省略。
- 云桌面:剪贴板开关/文件上传未做(剪贴板在连上时直接打开,操控开关、横屏全屏、画面内键盘已有);浏览器 tab 的键盘输入未做(截图流点击/滚动/导航已有)。
- **手机输入法直通云桌面做不了**(2026-08-31 逐条验证):无影 Web SDK 的全部消息接口(62 个)只有输入开关、指针、分辨率、文件和剪贴板,**没有任何注入文本/按键的 API**;`customASPAction` 只能打到 `htmlEngine` 上,`setImeCommit`/`sendKeyDown` 不在那上面。另一头 WKWebView 也不会为 SDK 那个跨域 iframe 里的隐藏 IME 输入框弹 iOS 软键盘(程序化 focus 不满足用户手势要求)—— 实测点云端输入框只拿到光标,键盘不弹。要真做端云一体输入法只有两条路:换无影原生 ASP iOS SDK,或后端加一个 `xdotool type` 接口。当前方案是用 SDK 自带的画面内键盘。
- 全 app 锁竖屏(`main.dart` 的 `setPreferredOrientations`),只有云桌面全屏例外 —— 其余页面都是按竖屏排的,自由旋转是以前没设置而已。
- 消息列表 >50 行虚拟化未做专门处理(ListView.builder 本身惰性构建)。
