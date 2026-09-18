# 移动端 1.0.24 (35) 小米上架包（2026-09-18）

构建源码 `main@f548bfbd`，`API_BASE` / `WEB_BASE` 均显式为 `https://ai.bossipai.com.cn`。本版在 1.0.23 (34) 基础上接入极光小米通道，并准备小米应用商店正式签名包。

## Android

- 包名：`com.bossip.bipmobile`。
- APK：`mobile/build/xiaomi-submission-1.0.24/BossIP-Android-1.0.24-35-Xiaomi-release.apk`，SHA-256 `403777fd740b38f859ef84726b72513ea38c8f1f2da68c4e024e9c8dd845934b`。
- 签名：长期发布证书，别名 `bossip-release`，RSA 4096，SHA-256 `16:16:50:A5:68:32:23:01:0B:68:AC:CB:FA:08:94:F2:14:98:74:6D:55:59:A7:E2:E1:BA:EB:BF:FF:01:61:96`，SHA-1 `85:61:A1:0C:1B:42:44:42:CA:7E:DC:0B:D5:D9:B2:39:47:D0:2C:6B`。私钥保存在 Git 忽略的本地凭据目录，密码另存于 macOS 钥匙串。
- APK Signature Scheme v2 验证通过，只有一个 signer；证书与固定发布证书一致，不是 Android Debug 证书；zipalign 验证通过。
- 合并清单包含 JPush、JCore、小米适配器、MiPush 服务、通知点击 Activity 和 BossIP 接收器。最终权限仅保留网络、通知、唤醒、震动以及 JPush / MiPush 自定义权限，移除了 SDK 可选的定位、电话状态、全包查询、Wi-Fi 和旧存储权限。
- 包内包含生产后端地址，未发现 localhost 或测试后端地址。

## 小米控制台

- 小米开放平台应用：`Bossip`，AppID `2882303761520583240`，包名 `com.bossip.bipmobile`。
- 小米 Push 当前不能启用：控制台要求应用先在小米应用商店创建并发布上线。
- 应用商店的发布新版本草稿已创建；正式提交仍需完成 APK、图标、截图、隐私政策、App 备案、软件著作权、AI 生成内容标识材料、审核账号和联系人手机验证等项目。

## 验证范围

按发版要求未运行测试套件。只执行了 release 构建、包信息检查、生产地址检查、合并 Manifest 检查、权限审计、签名验证、证书比对和 zipalign 验证；未进行小米真机收推验收。
