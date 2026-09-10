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

已使用现有的 App Store Connect 管理员团队 API 密钥完成 App Store 分发签名，并于 2026-09-10 22:37（北京时间）上传成功。

- IPA：`mobile/build/releases/BossIP-1.0.17-28/ipa/BossIP.ipa`，27,409,219 字节。
- IPA SHA-256：`133eb9c9946a9ab6051af12cd5cdf5ad91dcfed2c7d464cfad772bc2d01e2ef0`。
- 分发证书：`Apple Distribution: BBD ABC (5AN3L8LZL9)`，`codesign --verify --deep --strict` 通过。
- 最终 IPA 的版本为 `1.0.17 (28)`；签名和描述文件均为 `aps-environment=production`，`get-task-allow=false`，描述文件没有设备名单。
- App Store Connect App ID：`6794282961`（控制台名称 `bip-mobile`）；Delivery UUID：`907ccccb-6ecf-46e1-8408-39f7dc007b92`。
- 上传返回 0 个错误、1 个警告，Apple 已完成处理，`processingState=VALID`。警告 `90068` 提醒自 2027 年春季起最低 iOS 版本需为 15.0，目前的 14.0 不影响本次上传。
- 已核对版本 25 的 `usesNonExemptEncryption=false`；本次生产依赖中的加密库、原生 Pods 没有变化，未新增自定义加密功能。通过发布 API 为版本 28 设置相同的加密豁免信息，回读验证通过。
- TestFlight 状态为 `IN_BETA_TESTING`。网页重新加载后确认，新构建已自动关联现有内部群组“运营测试组”，6 名测试员可访问；未修改测试员或群组配置。
- 已保存简体中文测试说明，覆盖通知前后台行为、切换手机后的绑定、超管通知测试及聊天建议/流光占位。

### Apple 发布权限

BBD ABC 的会员注册身份是个人。受邀账号已经具备 App Store Connect 的管理、所有 App、新建 App、个人 API 密钥等可分配权限，但个人会员不能向受邀用户授予 `Certificates, Identifiers & Profiles` 访问权限。这是此前 Xcode 账户方式分发失败的原因，不能通过额外勾选权限解决；参考 [Apple 角色权限说明](https://developer.apple.com/help/app-store-connect/reference/account-management/role-permissions/)。在开发者网站手动管理证书、Bundle ID 和推送能力时，使用账户持有人账号。

已在当前机器核实并保存现有管理员团队发布密钥及其配置，位于 Git 忽略的 `credentials/apple-publishing/`，目录权限为 `0700`、文件为 `0600`。后续自动签名和上传可复用该配置及本次的 `ExportOptions-export.plist` / `ExportOptions-upload.plist`。发布 API 私钥与 APNs 推送私钥用途不同；私钥不提交到远程仓库。本次没有新建或撤销密钥、证书，也没有更改账号权限。

## 校验

Flutter analyze 无问题，323 项 Flutter 测试通过，语言文件与网页逐字节一致，Dart 文件大小门禁通过。生产 `/api/environment` 返回 200。本轮没有修改或部署后端、网页、数据库及服务端配置。
