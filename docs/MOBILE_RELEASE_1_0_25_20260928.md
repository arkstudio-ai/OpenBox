# 移动端 1.0.25 (36) 发布记录（2026-09-28）

本版只发 iOS / TestFlight，修一个问题：iOS 上传附件选不到相册（Android 正常）。构建源码 `main@53971679`（修复 `8c12b18a` + 版本号 `53971679`），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。

## 修复内容

- 根因：聊天输入框附件和资源中心「+」都调用不带类型的 `pickFiles()`，即 `FileType.any`。Android 的系统选择器自带图库入口；iOS 上它是「文件」App 的文档选择器，看不到照片图库，照片与视频只能走 `FileType.media` 对应的 `PHPicker`。
- 改法：新增 `mobile/lib/features/resources/utils/pick_source.dart`，iOS 先弹「从相册选择 / 浏览文件」，Android 行为不变；两处上传入口共用。技能压缩包上传只收 zip，未改。
- `Info.plist` 补 `NSPhotoLibraryUsageDescription`（PHPicker 本身免授权，属保险）。新增 widget 测试 `mobile/test/features/resources/pick_source_test.dart` 覆盖三种路由。

## iOS / TestFlight

- 从 `origin/main@53971679` 的独立工作树构建（本机主工作树落后主线且含未提交改动，未用）。依赖按锁文件解析，构建后 `Podfile.lock` 只有 CocoaPods 版本号噪音（1.17.0 → 1.16.2），已回退。
- Xcode 26.6 完成 Release 归档（自动签名 + 发布 API 密钥）和 App Store 分发导出（不带密钥，走 Xcode 登录账号云签名，同 1.0.19 起的做法）。
- 归档：`mobile/build/releases/BossIP-1.0.25-36/BossIP-1.0.25-36.xcarchive`（在临时工作树 `/Users/wxy/openbox-ios-album`）。
- IPA：`mobile/build/releases/BossIP-1.0.25-36/ipa/BossIP.ipa`，27,973,003 字节，SHA-256 `f5d67b806ef96ebc2cb4868463851d649b98eeda48ad5e2a052e1413dd87910d`；桌面副本 `BossIP-iOS-1.0.25-36/BossIP-iOS-1.0.25-36.ipa`。
- 包内版本 `1.0.25 (36)`、Bundle ID `com.bossip.bipmobile`、最低 iOS 15。`codesign --verify --deep --strict` 通过；`aps-environment=production`、`get-task-allow=false`；`ITSAppUsesNonExemptEncryption=false` 与相册用途说明均在实际包内；生产后端地址已在编译后的 Dart 二进制确认，无 localhost。
- 于 **2026-09-28 08:26（北京时间）** 上传成功，Apple 回执零错误；Delivery UUID `aa7e05b8-a7e9-4375-bc6a-eb12ac83e112`，App Store Connect App ID `6794282961`。
- 上传后约 3 分钟 Apple 处理完成：API 回读 `processingState=VALID`、`usesNonExemptEncryption=false`、`internalBuildState=IN_BETA_TESTING`、`autoNotifyEnabled=true`（沿用「运营测试组」自动分发，内部测试员可安装）；外部状态 `READY_FOR_BETA_SUBMISSION`，未提交。该 API 密钥读 `betaGroups` 返回 403，分组归属仍以网页为准。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)。目标仍是内部「运营测试组」，没有提交外部 Beta 或正式审核。

## 验证范围

- `flutter analyze` 与新增 widget 测试通过（iOS 选相册 → media、iOS 选文件 → any、Android 直接 any）。未跑完整测试套件。
- 模拟器装了 debug 包但停在登录页（生产 SSO），未在模拟器内点到相册选择器；真机验收待运营测试组安装 36 后确认：附件 → 从相册选择 → 能看到照片图库并上传成功，HEIC 以 `image/heic` 原样上传。

## 环境备注

- `credentials/apple-publishing/config.env` 里 `ASC_KEY_PATH` 是相对仓库根的路径，`xcodebuild -authenticationKeyPath` 要绝对路径，需自行拼接。
- 本机无 PyJWT；回读 App Store Connect API 用 `openssl dgst -sha256 -sign` 手工生成 ES256 JWT（DER 签名转 r||s）。
