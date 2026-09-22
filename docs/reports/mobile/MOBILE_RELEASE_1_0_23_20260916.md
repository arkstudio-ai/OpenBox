# 移动端 1.0.23 (34) 发版记录（2026-09-16）

> 文档类型：实施 / 验收记录。版本、测试数量和部署状态只对应文内记录时点。

构建源码 `main@e3f6acb`（版本由 `1.0.22+33` 升级为 `1.0.23+34`），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。
配套后端 `20260916-main-453b494`（同日发布到 gw2，见 [DEPLOY.md](../../operations/DEPLOY.md)）：新手引导进度字段 `preferences.onboarding`。装到更旧的后端上引导会每次启动重放。

## 本版包含（相对 1.0.22+33）

- 新手引导 M1 + M2（PR #39、#40，方案 MOBILE_ONBOARDING_PLAN.md（历史引用，当前仓库未收录：`MOBILE_ONBOARDING_PLAN.md`））：首启三屏、欢迎 sheet、行业示例卡、侧栏/工作面板/云桌面/输入框蒙层、接管卡片与授权中心说明、通知权限前置说明页、消息中心与四类聊天卡片首次说明条、定时任务/技能/资源中心空态改造、积分气泡、设置里重看引导。
- 通知权限不再在落地页未登录时弹出，改为进入 App、欢迎结束后先出说明页。

## Android

- Release APK：`mobile/build/releases/BossIP-1.0.23-34/BossIP-1.0.23-34.apk`，84,997,719 字节，SHA-256 `d16a506b0da1d1b52e6f5f4fef869befa95714636c519fee0437950e4695973c`。
- 桌面 7z：`BossIP-Android-1.0.23-34-20260916.7z`，25,506,561 字节；`7zz t` 通过。包名 `com.bossip.bipmobile`，versionCode 34，三个 ABI 均含生产地址，无 `application-debuggable`，Debug 证书签名（内测包）。

## iOS

- 归档 `BossIP-1.0.23-34.xcarchive`，IPA 27,939,735 字节，SHA-256 `e5231ea0e299556a5941eefe5426da345dfb17a44fd2a7e1ffe382b4bc278cb9`（桌面另存 `BossIP-iOS-1.0.23-34/`）。
- 导出用本机 Xcode 登录账号做云端 Apple Distribution 签名，上传用 API 密钥 `ZH3HN7FF5A`；15:48 上传成功，Delivery UUID `18577177-1300-4891-9923-5e79ace91959`，`processingState=VALID`。
- `ITSAppUsesNonExemptEncryption=false`（1.0.19 起写进 Info.plist）生效：`usesNonExemptEncryption=false` 自动落定，内部状态 `IN_BETA_TESTING`，无需再手点合规；「运营测试组」可安装。
- 坑：归档前不要回退 `Podfile.lock`，否则 `Check Pods Manifest.lock` 报 sandbox 不同步；导出完成后再 `git checkout -- Podfile.lock`。

## 未做

- 真机验收（引导流程在 iPhone 17 Pro 模拟器连生产后端跑通：欢迎、行业卡、侧栏 8 步、输入框气泡、积分气泡、消息中心与授权中心说明卡）。
