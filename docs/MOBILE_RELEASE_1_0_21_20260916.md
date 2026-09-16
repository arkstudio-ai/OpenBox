# iOS 1.0.21 (32) TestFlight 发布记录

## 源码与兼容范围

- 从 `main@eeb25fd3` 开始，版本由 `1.0.20+31` 升级为 **1.0.21+32**；归档源码 `c3c4897`。
- 保留上一轮已经验证的聊天断线恢复、后台返回恢复、长会话分页及原生管理功能，不增加管理端 trace 回放页面。功能验收见 [1.0.20 生产 iOS 验收](MOBILE_RELEASE_1_0_20_20260916.md)。
- `API_BASE` / `WEB_BASE` 显式指定 `https://ai.bossipai.com.cn`。
- 构建工具为 **Xcode 27.0 (27A266a)**，SDK 为 iOS 27。该工具链拒绝低于 iOS 15 的部署目标，因此本版最低系统升级为 **iOS 15.0**；iOS 14 设备无法安装本版。
- Runner 和 CocoaPods 的最低部署目标统一为 15.0；Podfile 的处理保留高于 15.0 的依赖要求。Pods 依赖版本未变，锁文件仅更新 Podfile 校验和。依据见 [Apple SDK 和系统要求](https://developer.apple.com/xcode/system-requirements)。

## 构建与签名

- 归档：`mobile/build/releases/BossIP-1.0.21-32/BossIP-1.0.21-32.xcarchive`。
- IPA：`mobile/build/releases/BossIP-1.0.21-32/ipa/BossIP.ipa`，27,527,483 字节。
- IPA SHA-256：`44dee8fe0b9390b76f78c63ddf7a04372ddb65028063574c4c0f2a4306ce5889`。
- Bundle ID `com.bossip.bipmobile`；包内版本 **1.0.21 (32)**，`MinimumOSVersion=15.0`。
- 分发签名 `Apple Distribution: BBD ABC (5AN3L8LZL9)`；归档与导出 IPA 均通过 `codesign --verify --deep --strict`。
- 最终签名与描述文件均为 `aps-environment=production`、`get-task-allow=false`，描述文件不限定设备名单；没有新增或更改 App 能力。
- `ITSAppUsesNonExemptEncryption=false` 已包含在实际 IPA 内，沿用现有声明；生产地址已在编译后的 Dart 二进制中核实。
- 本次发布凭据通过 App Store Connect API 读取验证，归档和导出均使用现有团队 API 密钥成功完成，未改动账号权限。

## 验证与发布状态

- `flutter analyze --no-pub` 无问题；Podfile Ruby 语法检查、CocoaPods 安装、Release 归档和 App Store 分发导出均通过。
- 业务代码与上一轮模拟器验收相同；本轮修改版本及原生构建目标，没有把上轮模拟器结果表述为本 IPA 的真机验收。
- `xcrun altool --validate-app` 通过；上传于 **2026-09-16 08:53（上海时区）** 成功，Apple 回执为零错误。
- Delivery UUID：`0a478b0f-8d2b-4d19-b332-0d9368a18348`；App Store Connect App ID：`6794282961`。
- Apple API 回读：版本 **1.0.21 (32)**，`processingState=VALID`，`usesNonExemptEncryption=false`，内部状态 **`IN_BETA_TESTING`**；无需补交出口合规。
- 从现有内部群组“运营测试组”的构建列表回读，确认已包含本次构建；沿用自动分发设置，没有修改群组或测试员。
- 已写入并回读验证简体中文测试说明，覆盖最低 iOS 15、多轮聊天、后台恢复、冷启动和历史分页。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)。本版已可供现有内部测试组安装；尚未提交外部 Beta 审核或正式 App Store 审核，也未进行本 IPA 的真机验收。

本轮仅发布 iOS；Android、生产后端、数据库及 trace 录制范围没有改动。
