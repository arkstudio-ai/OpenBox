# 移动端 1.0.43 (54) 双端发布记录（2026-10-31）

本版配套服务端 `20261031-seam-c00f2b0d`（填缝 P0，PR [#111](https://github.com/arkstudio-ai/OpenBox/pull/111)），iOS 发 TestFlight，Android 出正式签名包。

- 构建源码 `main@c00f2b0d` + 版本号提交（分支 `release/app-1.0.43`），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。
- 对比 iOS 1.0.42 (53) / Android 1.0.41 (52)：客户端改动全部来自 PR #111，无数据库迁移。

## 更新内容

### 任务执行页

- 任务停下时若有一步的结果没来得及确认，执行页输入框上方直接出现"我已核对，继续"，确认后再发一条消息任务就接着做；之前这个按钮只在 Web 的任务卡上，App 没有，用户只能被引导去点一个不存在的按钮。
- 发送被服务端以"结果未确认"拒绝时，同一个入口立即出现，不再只有一条"操作冲突"的通用提示。
- 任务会话里的停止按钮改为红色，并先弹一次内联确认：它停的是助理派发的整个任务，不是当前一轮。
- 任务卡菜单新增"我已核对，继续"（任务处于待核对状态时出现）。

### 语音通话

- 通话页球体下方新增待处理卡片栏：确认、提问、权限和其他对话里等待的问题一页一张，可左右切换、直接作答；打开对话会先收起通话页，电话不挂。无待处理时不占位。
- 卡片栏复用个人助理页同一套读取，电话里念到的卡和看到的卡是同一份。

### 文案

- 新增"这件事已经有一个任务在做了，请在原任务里继续"等错误说明；"结果未确认"的说明改为指向本页按钮。10-22 起已有的四条任务状态错误文案随本版首次进入 App 安装包。

## 构建

- 干净 worktree `/Users/wxy/openbox-app-1.0.43`（分支 `release/app-1.0.43`，基于 `main@c00f2b0d`），本机 Flutter 3.44.2 / Xcode 26.6；`flutter pub get --enforce-lockfile` 通过（1.0.42 已修复 lock 漂移）。

### Android 正式签名包

- `flutter build apk --release`，Gradle 约 7 分钟（含 Kotlin 插件兼容告警，不影响产物）。
- `apksigner verify` 通过，签名 `CN=BossIP Android Release`，证书 SHA-256 `16:16:50:A5:68:32:23:01:0B:68:AC:CB:FA:08:94:F2:14:98:74:6D:55:59:A7:E2:E1:BA:EB:BF:FF:01:61:96`（长期发布证书 `bossip-release`，可覆盖升级 1.0.41）；`aapt2` 读出 `com.bossip.bipmobile` versionCode 54 / versionName 1.0.43，compileSdk 36。
- 三个 ABI 的 `libapp.so` 均含 `ai.bossipai.com.cn`、不含本地调试地址。
- APK 105,216,522 字节，SHA-256 `02f9f3b3bca46e8cd95f822ce7493cca4723e4ffae29bc183853e52aba08d563`；7z 34,334,464 字节；两者在 `~/Desktop/BossIP-Android-1.0.43-54/`。

### iOS / TestFlight

- 归档 23:35–23:36（北京时间），导出 1 分钟，本次没有再遇到 PLA 协议门；上传 4 秒。
- 包内版本 `1.0.43 (54)`，`codesign --verify --deep --strict` 通过，签名机构 `Apple Distribution: BBD ABC (5AN3L8LZL9)`；`aps-environment=production`、`get-task-allow=false`、`beta-reports-active=true`；最低 iOS 15.0，`ITSAppUsesNonExemptEncryption=false`；Dart 二进制含生产地址、不含本地地址。
- IPA 32,157,196 字节，SHA-256 `b9f58bcadb22664f30dac140081fa133693dc32844916e5cf4481909fe6bfd95`，桌面副本 `~/Desktop/BossIP-iOS-1.0.43-54/`。
- **2026-10-31 23:39:49 上传成功**，Delivery UUID `2ba6c541-e83a-401e-806f-ddc2c12157d7`。23:43 App Store Connect 处理完成（`processingState=VALID`，`expired=False`），沿用内部「运营测试组」自动分发；未提交外部 Beta 或审核。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)。

## 验收要点

- 在任务执行页点停止：应先看到"停下整个任务？"确认；确认后任务取消。
- 任务取消且有一步结果未确认时：执行页输入框上方应出现"我已核对，继续"，点后再发一条消息任务应恢复；旧版会一直收到"操作冲突"。
- 语音通话中有待确认卡片时：通话页下方应出现卡片栏，可切换并直接作答；点"打开对话"应收起通话页且电话不断。
- 通话中无待处理事项时：通话页应与之前一致，无空白卡片栏。

## 范围说明

- 未运行真机业务场景；完成的是单元/widget 测试、构建、签名校验、生产地址校验与上传。
