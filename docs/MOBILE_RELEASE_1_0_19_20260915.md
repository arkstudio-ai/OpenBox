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

本次未出 iOS 包。App Store Connect 发布密钥（`credentials/apple-publishing/`，见 [1.0.17 记录](MOBILE_RELEASE_1_0_17_20260910.md)）不在本机，本机也没有分发描述文件；需在持有密钥的机器上按 1.0.17 的 archive → export → upload 流程补发 `1.0.19 (30)`。
