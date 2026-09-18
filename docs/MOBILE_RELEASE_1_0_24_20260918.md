# 移动端 1.0.24 (35) 发布记录（2026-09-18）

Android 构建源码 `main@f548bfbd`，iOS 构建源码 `main@f80e324b`；两端 `API_BASE` / `WEB_BASE` 均显式为 `https://ai.bossipai.com.cn`。本版同步主线的上下文整理展示、问答卡片主按钮逐题继续、聊天流式状态衔接、视频发布方式设置和授权有效期确认；Android 另接入极光小米通道，并准备小米应用商店正式签名包。

## Android

- 包名：`com.bossip.bipmobile`。
- APK：`mobile/build/xiaomi-submission-1.0.24/BossIP-Android-1.0.24-35-Xiaomi-release.apk`，SHA-256 `403777fd740b38f859ef84726b72513ea38c8f1f2da68c4e024e9c8dd845934b`。
- 签名：长期发布证书，别名 `bossip-release`，RSA 4096，SHA-256 `16:16:50:A5:68:32:23:01:0B:68:AC:CB:FA:08:94:F2:14:98:74:6D:55:59:A7:E2:E1:BA:EB:BF:FF:01:61:96`，SHA-1 `85:61:A1:0C:1B:42:44:42:CA:7E:DC:0B:D5:D9:B2:39:47:D0:2C:6B`。私钥保存在 Git 忽略的本地凭据目录，密码另存于 macOS 钥匙串。
- APK Signature Scheme v2 验证通过，只有一个 signer；证书与固定发布证书一致，不是 Android Debug 证书；zipalign 验证通过。
- 合并清单包含 JPush、JCore、小米适配器、MiPush 服务、通知点击 Activity 和 BossIP 接收器。最终权限仅保留网络、通知、唤醒、震动以及 JPush / MiPush 自定义权限，移除了 SDK 可选的定位、电话状态、全包查询、Wi-Fi 和旧存储权限。
- 包内包含生产后端地址，未发现 localhost 或测试后端地址。

## iOS / TestFlight

- 从干净 `git archive f80e324b` 构建，依赖按锁文件解析，构建后 `Podfile.lock` 与仓库一致；Xcode 27 完成 Release 归档和 App Store 分发导出，沿用现有团队与发布 API 密钥，未更改账号权限。
- 归档：`mobile/build/releases/BossIP-1.0.24-35/BossIP-1.0.24-35.xcarchive`。
- IPA：`mobile/build/releases/BossIP-1.0.24-35/ipa/BossIP.ipa`，27,935,232 字节，SHA-256 `be5526ae0af0578e2c61d626094e5386caca688fc68a09191c7974be2d657804`；桌面副本 `BossIP-iOS-1.0.24-35/BossIP-iOS-1.0.24-35.ipa`。
- 包内版本 `1.0.24 (35)`、Bundle ID `com.bossip.bipmobile`、最低 iOS 15。`codesign --verify --deep --strict` 通过；最终签名和描述文件均为 `aps-environment=production`、`get-task-allow=false`，不限定设备名单；`ITSAppUsesNonExemptEncryption=false` 已写入实际包，生产后端地址已在编译后的 Dart 二进制确认。
- 于 **2026-09-18 21:49（北京时间）** 上传成功，Apple 回执零错误；Delivery UUID `5008f330-4d45-4a7c-952c-ca2ad5d08f7f`，App Store Connect App ID `6794282961`。
- 21:53 Apple 上传处理完成，回执没有错误或警告；API 回读 `processingState=VALID`、`usesNonExemptEncryption=false`、内部状态 `IN_BETA_TESTING`。从现有“运营测试组”构建列表确认已包含 35，沿用自动分发设置；简体中文测试说明已保存并回读核对。内部测试员现在可安装本构建。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)。本次目标为现有内部“运营测试组”，没有提交外部 Beta 或正式 App Store 审核。

## 小米控制台

- 小米开放平台应用：`Bossip`，AppID `2882303761520583240`，包名 `com.bossip.bipmobile`。
- 小米 Push 当前不能启用：控制台要求应用先在小米应用商店创建并发布上线。
- 应用商店发布草稿已上传正式 APK（后台成功解析版本 1.0.24、版本号 35）、512×512 图标和首张已登录工作台截图。已填写“效率办公 / 办公”分类、简介、应用介绍和 AI助手关键词，取消 OPPO / vivo / 荣耀的默认同步选项，仅准备小米发布。根据现有 AI 生成和内容发布功能，AI 服务项选择“两者均有”，没有填报未取得的标识材料。
- 后台已显示“保存成功”；草稿保留在 [小米发布页](https://dev.mi.com/xiaomihyperos/console/apps/distribute/app-create?action=create&isOffStore=false&appId=2882303761520583240&packageName=com.bossip.bipmobile&appType=0)，尚未提交审核。
- 用户确认 App 备案、版权证明、隐私政策及 AI 相关材料尚未办理。正式提交仍需这些材料、适用的安全评估材料、至少 4 张完整截图、专门授权给小米审核人员使用的测试账号，以及联系人手机验证。不能将联网 AI 应用申报为“无需备案（单机应用）”或“不涉及 AI”。控制台核验仍为“未备案”。
- 素材副本位于桌面 `BossIP-Xiaomi-1.0.24-35/`。`store-screenshots/01-workspace.png` 为已登录真实首页，`02-ai-assistant.png` 为真实引导页，均为 1080×1920 PNG、小于 5 MB；第二张上传及其余功能截图采集待继续。控制工具无法识别独立 Android 模拟器，已询问是否允许通过官方 ADB 继续操作；没有改动用户登录状态。
- 同次工作已将生产后端套餐恢复正式价格，部署记录见 [DEPLOY.md](DEPLOY.md)。移动端通过生产 API 获取价格，无需为此重打 APK。

## 验证范围

按发版要求未运行测试套件。Android 执行 release 构建、包信息/生产地址/合并 Manifest 检查、权限审计、签名验证、证书比对和 zipalign 验证；iOS 执行 Release 归档、分发导出、签名和描述文件检查、版本/生产地址/加密声明核对及正式上传。独立 `altool --validate-app` 在 Apple 校验资产已接收后卡于结果回读，终止后改由正式上传和 Apple 后续处理结果核验，未把该独立校验记为通过。未进行本轮最终包的 iPhone 真机验收或小米真机收推验收。
