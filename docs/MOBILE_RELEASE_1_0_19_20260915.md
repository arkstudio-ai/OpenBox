# 移动端 1.0.19 (30) 发版记录（2026-09-15）

构建源码 `main@0d64dc1`（版本由 `1.0.18+29` 升级为 `1.0.19+30`），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。
配套后端为同日归零发布的 `20260915-main-73a311b`（见 [DEPLOY.md](DEPLOY.md)），此版 App 依赖其中的消息中心接口与按轮分页历史接口，不要装到更旧的后端上。

## 本版包含（相对 1.0.18+29，源码 `617f77e`）

- 消息中心：站内收件箱、专题页、点推送进收件箱（PR #30，`75c68ce`）；超管公告与专题后台（`e772495`）。
- 聊天历史按轮分页：打开会话只取最新几轮并增量追赶，不再每秒全量下载历史（PR #35，`cab4357`）。
- 建议卡标签在窄屏上的排版修复（`0b99bf1`）。
- 移动端源码共 49 个文件变更。

## Android

- Release APK：`mobile/build/releases/BossIP-1.0.19-30/BossIP-1.0.19-30.apk`，84,209,113 字节，SHA-256 `309bb8f434116cee8f4cc50b2b7a40f2cd339d626127415370274c6684989fd9`。
- 桌面 7z：`BossIP-Android-1.0.19-30-20260915.7z`，25,080,708 字节，SHA-256 `528d9bfef9b31539f5091dd1dd7e9b54c40d67ab4763650c04760e038a6d6f63`，含 APK、README 与 SHA256SUMS；`7zz t` 通过。
- 包名 `com.bossip.bipmobile`，versionCode 30 / versionName 1.0.19，compileSdk/targetSdk 36，minSdk 24；badging 无 `application-debuggable`；ABI arm64-v8a / armeabi-v7a / x86_64 三个 `libapp.so` 均含生产地址。
- 沿用 Android Debug 证书签名（SHA-256 `98d7531d…72df0`），APK v2 签名验证通过；仍为内部测试包，未建立正式 keystore，未上架。厂商推送通道与 1.0.18 相同（未接入）。
- 构建环境：本机首次搭建 Android 工具链（Homebrew `openjdk@17`、`android-commandlinetools`，SDK 在 `/opt/homebrew/share/android-commandlinetools`，platform 36 / build-tools 36.0.0 / NDK 28.2.13676358，`flutter config --android-sdk … --jdk-dir …`）。
  `flutter test` 353 项、`flutter analyze` 无告警。首次 Gradle 依赖下载约 27 分钟。
- 未做：真机安装与消息中心、分页的真机验收；本机无 Android 模拟器。

## iOS

归档 `mobile/build/releases/BossIP-1.0.19-30/BossIP-1.0.19-30.xcarchive`（Release，`xcodebuild archive -allowProvisioningUpdates`），Bundle ID `com.bossip.bipmobile`，Team `5AN3L8LZL9`，版本 `1.0.19 (30)`，归档开发签名 `codesign --verify --deep --strict` 通过。

- IPA：`mobile/build/releases/BossIP-1.0.19-30/ipa/BossIP.ipa`，27,551,672 字节，SHA-256 `58097af3ef658b45a6e12bcafdc66223da079e7af37f77b773609e0d50d9447e`（桌面另存一份 `BossIP-iOS-1.0.19-30/`）。
- 分发签名 `Apple Distribution: BBD ABC (5AN3L8LZL9)`，`aps-environment=production`，`get-task-allow=false`，描述文件 `iOS Team Store Provisioning Profile: com.bossip.bipmobile`（无设备名单，2027-07-01 到期）。
- `xcrun altool --validate-app` 通过；`--upload-app` 于 2026-09-15 17:02（北京时间）上传成功，Delivery UUID `0402c0d3-cd52-4eda-9c3d-e1593f05e84c`；Apple 处理完成 `processingState=VALID`。
  警告 `90068`（最低 iOS 14.0，2027 年春季起需 15.0）与 1.0.17 相同，暂不影响。
- **待人工一步**：TestFlight 状态 `MISSING_EXPORT_COMPLIANCE`。本机的 API 密钥 `ZH3HN7FF5A`（团队密钥，可读 App/用户列表、可上传）对 `PATCH /v1/builds` 返回 403，无法像 1.0.17 那样用 API 设置 `usesNonExemptEncryption=false`；
  需在 App Store Connect → TestFlight → iOS 构建 30 → 管理合规 中选择「否（不使用非豁免加密）」。内部群组「运营测试组」为自动分发，合规通过后 6 名测试员即可安装。
  本次同时把 `ITSAppUsesNonExemptEncryption=false` 写进 `ios/Runner/Info.plist`，下一个构建起不再需要这一步。
- 签名路径与 1.0.17 的差异：本机钥匙串没有分发证书私钥，`-exportArchive` 用 API 密钥做云端签名报 `Cloud signing permission error`；
  改用本机 Xcode 已登录的账号（`-allowProvisioningUpdates` 不带 `-authenticationKey*`）导出成功，云端管理的 Apple Distribution 证书由该账号提供。
  上传则用 API 密钥走 `altool`。发布密钥与导出配置已按 1.0.17 约定放在本机 Git 忽略的 `credentials/apple-publishing/`（`AuthKey_ZH3HN7FF5A.p8`、`config.env`、`ExportOptions-export.plist`、`ExportOptions-upload.plist`，目录 0700 文件 0600）。
- 未做：真机验收。
