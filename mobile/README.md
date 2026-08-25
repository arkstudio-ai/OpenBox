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
| 左侧 Sidebar | 抽屉 `SessionDrawer` |
| 右侧 WorkbenchPanel(审阅/终端/浏览器/文件/云桌面) | 路由页 `/app/w/:sessionId`(`WorkbenchScreen`,同款五 tab) |
| DesktopTab(Wuying Web SDK) | `desktop_tab.dart`:原生轮询 `/api/desktop/ticket`(202→task_id 重试),WebView 装载同版本 SDK 引导页,JS channel 回报 connected/error,允许操控开关经 `__setControl` 注入 |
| BrowserTab(dev-browser 截图流) | `browser_tab.dart`:原生 WS 客户端,JPEG 帧 → `Image.memory`(gapless),点击/滚动映射回页面像素坐标,4004 → 无沙箱 |
| Composer 的 `/`、`@` 提及菜单 | `utils/mention.dart`(触发规则逐条移植)+ `mention_menu.dart`;文件搜索 160ms 防抖,技能/命令同款分组 |
| hover meta 操作行(复制/点赞/点踩/重生成/复刻) | 长按助手轮 → 操作单(`turn_actions_sheet.dart`) |
| `AttachmentGallery`(图片附件宫格 + Lightbox) | `attachment_gallery.dart`:同款宫格/折叠/全屏(捏合缩放 + 下载);**视频**额外支持内联播放(web 只降级为胶囊);资产 URL 经 `/api/assets/{id}/url` 缓存 40 分钟 |
| Settings 路由(6 个 tab) | `/app/settings`(账号/外观/模型三段;用量/工具/浏览器设置为桌面范畴) |

## 分层(镜像 web ENGINEERING_SPEC §3/§4)

```
lib/
  app/        # 组装层:根组件、路由、workspace 壳 —— 只有这里能同时 import 多个 feature
  features/   # auth / chat / workspace / workbench / settings / landing
              #   各自 api/ state/ widgets/ utils/;feature 之间禁止互相 import,
              #   跨特性用 shared/events/bus.dart(事件:workspace.refresh、workbench.open)
  shared/     # api(dio/auth)、ws、models(后端 snake_case 契约)、appearance、i18n、router/paths、widgets、utils
```

规则:依赖方向 `app → features → shared` 单向;单文件 ≤ 800 行(`scripts/check_file_size.sh` 门禁);颜色只从 `context.tokens` 取,不许写死。

## 协议要点(实现时容易踩的)

- WS 信封 `{type,data}`:路由字段 camelCase(`sessionId`/`messageId`/`partId`),内嵌 message/part 对象 snake_case —— 两层分开解析,勿全局转换。
- `message.text_delta` 与 `part.delta` 都是**追加**语义;快照合并时 text/reasoning 取更长者、tool 取更高状态(pending<running<completed=error),防止 UI 回退。
- 权限回复动作用后端原生值 `once`/`always`/`reject`(web 端目前发的 `allow/allow_always` 会被 REST 400 拒)。
- 问题回复 `answers` 是嵌套数组:每个问题一个 label 数组,按序。
- 运行中每 1s 轮询 messages+session 快照兜底(WS 重连只补 `session.status`,不重放 delta)。
- 终端 WS:二进制帧 1 字节 tag + 载荷,`0x00` 数据、`0x01` resize(cols/rows 各为大端 uint16);文本帧只有 `{type:"error"}`。

## 移动端偏差记录(对齐 web 附录 D 的做法:能力不具备则省略控件)

- SSO/Logto 登录未做(当前 web LoginForm 也已移除 SSO 按钮;移动端需要 PKCE + 回调 scheme,后续补)。
- 附件**上传**、语音输入暂缺(composer 文本 + `/`/`@` 提及;上传链路 OSS 直传 + 8MB legacy 兜底待接)。附件**展示**已齐:图片宫格 + Lightbox + 视频播放。
- 顶栏"分享"(web 为复制 URL)在移动端无意义,省略。
- 云桌面:剪贴板开关/文件上传/独立全屏未做(tab 本身已是全屏;操控开关已有);浏览器 tab 的键盘输入未做(截图流点击/滚动/导航已有)。
- 消息列表 >50 行虚拟化未做专门处理(ListView.builder 本身惰性构建)。
