# 移动端 iOS 1.0.42 (53) TestFlight 发布记录（2026-10-31）

本版只发 iOS / TestFlight，是 1.0.32 (43)（2026-10-10）之后的第一个 iOS 构建。10-13 至 10-29 的 1.0.34、1.0.35、1.0.36、1.0.38、1.0.39、1.0.40、1.0.41 七个版本都只出了 Android 正式签名包，iOS 一直停在 43；本版把这七个版本的客户端改动和 10-31 的权限卡修复一起带到 TestFlight。

- 构建源码 `main@f5ed4f5f`（`origin/main@71818450` + 版本号提交），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。
- 对应后端已是线上 `20261031-permexpiry-30423d0a`，无需配套服务端发布，也无数据库迁移。
- 与 Android 1.0.41 (52) 相比，客户端业务代码只多了 `9c807f47` 的权限卡过期处理；版本号从 1.0.41+52 抬到 1.0.42+53。

## 更新内容（对比 TestFlight 上一版 1.0.32 (43)）

### 语音助手

- 默认使用 Audio 3.1，按上下文理解需求、选择与确认，减少固定口令和机械播报；已明确选择的内容不再重复询问。
- 去掉固定通话时长上限，增加静默保活、断线恢复和有边界的超时处理。
- 语音助手保持独立对话、按需交办任务；手动创建的会话、任务进展、已回答卡片和近期素材在语音与文字之间保持一致，点别的任务卡不会误打断当前语音。
- 有旧确认卡时也可以直接表达新需求或改方案（例如从无图生成改为用刚上传的门店图片），不再反复确认旧脚本。
- 使用当前设置的助手名字和用户称呼，未设置时默认"老板"。
- 音频被其他应用短暂占用（导航、音乐压低音量）时不再误判为来电并挂断；真实音频中断显示通用提示，音频可用时自动恢复，并提供手动恢复按钮；原 60 秒中断自动挂断已移除。
- 区分"空闲聆听"与"听到你在说话"；播放状态保持到本机音频队列真正放完，而不是服务端一报完成就消失。
- 视频档位按配置逐一展示，MiniMax H3 保留"灵活"档名称，不再并入标准档。

### 聊天、任务卡与素材

- 问题卡补齐资源库选图、手机相册 / 文件上传、图片预览和素材草稿同步；素材可随回答一并提交，并交给语音助手接续处理。上传过程中不会提前提交。
- 一张待处理卡片独占该会话的下一轮；等待态指示在卡片出现后即停止，不再依赖忙碌事件追上。
- 普通会话、个人助理和语音共享可访问的附件；新上传图片支持 AI 自动描述，也可手动修改。
- 权限卡按 `expires_at` 到点置灰并提示已过期；服务端返回 404/410 时卡片自动收起并显示中文说明，不再出现英文 410。
- 输入框与底部留白收紧，"内容由 AI 生成，请注意核实"并入输入框底部提示行；去除文字输入时的矩形焦点框。
- 从后台返回或重新打开时恢复上次聊天位置，并按账号隔离保存。

### 登录、设置与会员

- 已登录状态启动直接进入应用；登录与注册分开为两个入口。
- 设置新增语音模型 / 音色 / 沟通方式与任务播报偏好、助手名字与称呼、Default / RunningHub 图片来源。
- 命令执行、云电脑和浏览器工具统一按会员资格判断；会员到期时明确提示开通或续费，保留原输入和附件；续费后自动恢复云电脑资格。

### iOS 原生

- `VoiceCallBridge.swift`：CallKit 通话在启动阶段失败时向 Flutter 回报 `start_failed` 而不是 `end`，并在重建 provider 时忽略旧 provider 的回调，避免误结束新通话。

## iOS / TestFlight 构建

- 干净 worktree `/Users/wxy/openbox-ios-1.0.42`（分支 `release/ios-1.0.42`，基于 `origin/main@71818450`），归档约 1 分钟（16:37–16:38），导出 1 分钟，上传 4 秒；首次导出被 `PLA Update available / No signing certificate "iOS Distribution"` 挡住，账户持有人在开发者网站同意新版 Program License Agreement 后重跑导出即通过。归档输出在工作树外 `/Users/wxy/openbox-ios-build-1.0.42/`。
- 流程：`flutter build ios --release --config-only` → `xcodebuild archive`（自动签名 + 发布 API 密钥）→ `xcodebuild -exportArchive`（`ExportOptions-export.plist`，不带密钥，Xcode 账号云签名）→ `xcrun altool --upload-app`（带 `--apple-id 6794282961 --bundle-id com.bossip.bipmobile --bundle-short-version-string 1.0.42 --bundle-version 53`）。
- 本机 Flutter 3.44.2 / Xcode 26.6。`flutter pub get --enforce-lockfile` 在本机不通过，报 `Unable to satisfy pubspec.yaml using pubspec.lock`：main 上的 `pubspec.lock` 自 10-10 `d0913421` 后没有再随依赖变化更新，已和 `pubspec.yaml` 不一致，因此本次按本机 Flutter 重新解析依赖构建（变化含 `flutter_math_fork`/`provider`/`nested`/`tuple` 退出、`val_latex`/`val_highlight`/`cupertino_ui`/`material_ui` 进入，共 309+/293- 行，原 diff 存档 `openbox-ios-build-1.0.42/pubspec.lock.local-drift.diff`）。刷新后的 `pubspec.lock` 随本次发布记录一并提交，使主线恢复 `--enforce-lockfile` 可复现。
- 包内版本 `1.0.42 (53)`，`codesign --verify --deep --strict` 通过，签名机构 `Apple Distribution: BBD ABC (5AN3L8LZL9)`；`aps-environment=production`、`get-task-allow=false`、`beta-reports-active=true`；最低 iOS 15.0，`ITSAppUsesNonExemptEncryption=false`；Dart 二进制含 `ai.bossipai.com.cn`、不含本地调试地址。
- IPA 32,138,974 字节，SHA-256 `2aee7d471b65b2983ea88702c9c5f5bf6623b765e72fbdbaa1de2e10185368e9`，桌面副本 `~/Desktop/BossIP-iOS-1.0.42-53/`。
- **2026-10-31 17:06（北京时间）上传成功**，Delivery UUID `3cd0be4a-99e3-4206-8098-852636c43b4d`。17:08 App Store Connect 处理完成（`processingState=VALID`，`internalBuildState=IN_BETA_TESTING`，自动通知已开，`usesNonExemptEncryption=false` 无需手点出口合规，构建 2027-01-29 过期）。 沿用内部「运营测试组」自动分发；未提交外部 Beta 或审核。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)。

## 验收要点

- 语音通话中打开地图导航或播放音乐：通话不应被挂断、不应提示"检测到来电"；音乐停止后自动恢复，或点"恢复"按钮恢复。
- 语音里交办一个视频任务，再到文字会话看同一任务：进度、确认卡和已回答状态应一致；回答卡片后语音应及时知道。
- 让一张权限卡放到过期（5 分钟）：按钮应置灰并提示已过期，点击不应出现英文错误。
- 已登录状态冷启动应直接进入应用；退出后登录页应分别有"登录"和"注册"。
- 问题卡里从相册选图上传、从资源库选图，应能预览并随回答提交。
- 对比 1.0.32 原有验收项（视频保存到相册 / 文件、旧链接 App 内下载）不应回归。

## 范围说明

- 未运行自动化测试、模拟器和真机业务场景；完成的是构建、签名校验、生产地址校验、上传与 App Store Connect 处理状态回读。
- Android 正式包仍是 1.0.41 (52)（2026-10-29），本版不出 Android。
