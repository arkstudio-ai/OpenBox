# 移动端 1.0.28 (39) 发布记录（2026-09-28）

双端内部测试包，内容是客户 09-26 三条 UI 意见在 App 端的落地（授权中心精简、定时任务独立页、云桌面入口与开发者模式），同时带上 1.0.26/1.0.27 的相册修复。构建源码为分支 `feat/fe-customer-feedback@09c62fb8`（= `main@359637ba` + PR #61 的四个提交 + 版本号），**PR #61 尚未合并**；`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。Web 端同一批改动已于当天上午发到 gw2（`20260928-fe-e0a23e9`），App 与线上后端接口兼容。

## 改动内容（App 端）

- 授权中心：删掉页顶通知面板（消息中心已承接）；每个账号只留一行到期时间，其余信息收进「详情」；平台授权规则只在未绑定时显示一次。
- 定时任务：新增任务详情页（状态、任务内容、计划与统计、动作、运行记录；点某次运行打开其过程会话）；列表卡片点进详情，不再内嵌运行记录；工作面板去掉「定时任务」项；顶栏胶囊改为进定时任务页；抽屉项目树不再混入定时运行会话。
- 云桌面：抽屉里提到中心行第一位；新增「开发者模式」偏好（设置 → 外观 → 工作面板，与 Web 共用服务端字段 `extra.developerMode`，默认关）。关时工作面板直接是云桌面；开时恢复审阅 / 终端 / 浏览器 / 文件。
- 详见 docs/DEVLOG.md 2026-09-28 段。

## Android（内部测试包，Debug 证书）

- 签名仍是 Android Debug 证书（SHA-256 `98d7531d…72df0`）；发布证书与小米推送配置只在 andrew 机器，正式包需他从合并后的 main 重打。装有正式签名包的手机需先卸载；装了 37/38 Debug 包的可直接覆盖。
- APK：`mobile/build/releases/BossIP-1.0.28-39/BossIP-Android-1.0.28-39-debugsigned.apk`，85,508,651 字节，SHA-256 `88461fe4854c88db19a49ccd85f266132d87c3a36ad41f2180187f98b148486b`；同目录 7z；桌面副本 `BossIP-Android-1.0.28-39/`。
- 包名 `com.bossip.bipmobile`，`versionName 1.0.28`、`versionCode 39`，targetSdk 36。apksigner 单一 signer、zipalign 通过；三个 ABI 均含生产地址，无 localhost。

## iOS / TestFlight

- 在分支工作树 `/Users/wxy/openbox-fe-feedback` 构建；`Podfile.lock` 的 CocoaPods 版本号噪音已回退。归档（自动签名 + 发布 API 密钥）与 App Store 分发导出（不带密钥，Xcode 登录账号云签名）均由 Xcode 26.6 完成，归档与导出物在会话 scratchpad，桌面副本 `BossIP-iOS-1.0.28-39/BossIP-iOS-1.0.28-39.ipa`，27,964,352 字节，SHA-256 `214b0324ce826020ba72a7b015b1b8c9526e0a153fb7e39a2f09ca21a330808f`。
- 包内版本 `1.0.28 (39)`、Bundle ID `com.bossip.bipmobile`；`codesign --verify --deep --strict` 通过；`aps-environment=production`、`get-task-allow=false`、`ITSAppUsesNonExemptEncryption=false`；Dart 二进制含生产地址、无 localhost。
- 于 **2026-09-28 18:15（北京时间）** 上传成功，Apple 回执零错误；Delivery UUID `2e8143c0-64a5-404b-98bc-37b7e688dd55`，App ID `6794282961`。目标仍是内部「运营测试组」自动分发，未提交外部 Beta 或审核。
- 上传后约 15 分钟 Apple 处理完成：API 回读 `processingState=VALID`、`usesNonExemptEncryption=false`、`internalBuildState=IN_BETA_TESTING`、`autoNotifyEnabled=true`（内部测试员已可安装）；外部状态 `READY_FOR_BETA_SUBMISSION`，未提交。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)。

## 验证范围

- `flutter analyze` 无问题；422 条测试通过（`desktop_api_test` 的 fen 定价用例在本分支前就失败，读 `backend/billing/plans.json` 期望 10 分测试价，与本次无关）。
- 未做真机验收。请重点验：抽屉第一行「云桌面」；设置 → 外观 → 「开发者模式」关闭时聊天页右上角工作面板直接进云桌面；定时任务列表点卡片进详情、点运行记录打开过程；授权中心账号行「详情」折叠。
