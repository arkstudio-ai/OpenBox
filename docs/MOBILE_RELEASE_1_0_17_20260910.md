# 移动端 1.0.17（28）打包记录

构建源码：`bdf77ff`，版本由 `1.0.16+27` 升级为 `1.0.17+28`。两端显式设置 `API_BASE` 和 `WEB_BASE` 为 `https://ai.bossipai.com.cn`，包含当前 main 的移动通知、超管通知测试、建议排版和 28px 流光占位。

## Android

- Release APK：`mobile/build/releases/BossIP-1.0.17-28/BossIP-1.0.17-28.apk`，83,583,619 字节。
- 桌面压缩包：`BossIP-Android-1.0.17-28-20260910.7z`，25,657,830 字节。
- APK SHA-256：`5170a81e22dc75ee50e868f08a4dd1717d56206351698491e449840c5098a4b1`。
- 7z SHA-256：`9f028e883ce6548bf36aee2c0a1aff1d40b0ab3e4728d7e68141b0b62d15112a`。
- 包名、版本和 build number 已检查；`debuggable=false`，包含 arm64-v8a、armeabi-v7a、x86_64，三个 ABI 的二进制均确认包含生产地址。
- 沿用现有 Android Debug 证书签名，APK v2 签名验证通过；本次为内部测试 APK，未建立或替换正式 Android keystore。
- `7zz t` 通过，解压后的 APK SHA-256 与原包一致。

## iOS

归档构建通过，保存到 `mobile/build/releases/BossIP-1.0.17-28/BossIP-1.0.17-28.xcarchive`，Bundle ID 为 `com.bossip.bipmobile`，Team ID 为 `5AN3L8LZL9`，版本 `1.0.17 (28)`。归档开发签名通过 `codesign --verify --deep --strict` 验证。

**尚未生成 App Store 分发 IPA，尚未上传 TestFlight。**

用户完成 Xcode 登录后，BBD ABC 团队可见，但账户的 `Certificates, Identifiers & Profiles` 显示无访问权限。刷新团队后，命令行导出、上传模式和 Xcode Organizer 的 App Store Connect 分发均失败，错误为 `No Account for Team`、`No signing certificate "iOS Distribution" found`。

旧 `workspace/bossip` 的 `ios/ExportOptions.plist` 使用自动签名上传；本地旧版 `1.0.14 (25)` 的 `DistributionSummary.plist` 记录使用 `Cloud Managed Apple Distribution`。旧仓库、忽略文件、相关配置和 Git 历史中均未找到本地 App Store Connect API 私钥或 Apple Distribution `.p12`。现有 APNs 推送私钥不是 TestFlight 发布凭据。

后续需要有该团队签名权限的 Apple 账户或有效发布凭据。原归档与 `ExportOptions-export.plist`、`ExportOptions-upload.plist` 已保存在上述 releases 目录，可继续签名导出和上传；成功后仍须核对最终分发包的 `aps-environment=production`、实际版本及 TestFlight 处理状态。

## 校验

Flutter analyze 无问题，323 项 Flutter 测试通过，语言文件与网页逐字节一致，Dart 文件大小门禁通过。生产 `/api/environment` 返回 200。本轮没有修改或部署后端、网页、数据库及服务端配置。
