# 移动端 1.0.26 (37) 发布记录（2026-09-28）

本版只出 Android 内部测试包，修一个问题：部分安卓机型（小米、华为、OPPO、vivo 等厂商 ROM）上传附件时进不了相册。构建源码 `main@95ae5769`（修复 `4b7ffae6` + 版本号 `95ae5769`），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。iOS 逻辑与 1.0.25 (36) 相同，未重发。

## 修复内容

- 1.0.25 把「从相册选择 / 浏览文件」二选一只开在 iOS，安卓仍直接进系统文档选择器（`ACTION_OPEN_DOCUMENT`）。原生 Android 的文档选择器侧栏有图片/视频入口，但厂商 ROM 用自家文件管理器替换它，相册入口被藏或没有，所以「部分安卓」复现。
- 现在两端都先问一次。安卓「从相册选择」走 `FileType.media`，即带图片/视频类型的 `ACTION_GET_CONTENT`，图库 App 会出现在系统选择器里；file_picker 插件自带 Android 11+ 的 `<queries>` 包可见性声明，无需改 manifest。
- 测试增至 4 条（iOS 相册 → media、iOS 文件 → any、安卓相册 → media、退出菜单不选文件）。

## Android（内部测试包，Debug 证书）

- **签名只是 Android Debug 证书**（SHA-256 `98d7531d5057dd5b3649f27ddf2896c7e736581273f98191c9440cea7ee72df0`，与 1.0.19 / 1.0.23 内测包相同）。长期发布证书 `bossip-release`、`key.properties` 和小米推送的 `push-vendors.properties` 都只在 andrew 的机器上，本机没有。
- 因此：**装有 1.0.24（发布证书签名）的手机不能直接覆盖安装，必须先卸载**；本包不含小米推送通道（清单里的 `MIPUSH_RECEIVE` 权限来自极光 SDK 的条件依赖，没有小米 AppID 不会生效）；不能用于小米商店提交。正式签名包需 andrew 从 `main@95ae5769` 用 `key.properties` 重打。
- APK：`mobile/build/releases/BossIP-1.0.26-37/BossIP-Android-1.0.26-37-debugsigned.apk`，85,541,443 字节，SHA-256 `c8512ff6d572db138f08d950bbd566445616a22135a8dc30173dc7dc37214a45`；同目录 7z `BossIP-Android-1.0.26-37-debugsigned-20260928.7z`；桌面副本 `BossIP-Android-1.0.26-37/`。
- 包名 `com.bossip.bipmobile`，`versionName 1.0.26`、`versionCode 37`，targetSdk 36。APK Signature Scheme v2 验证通过，单一 signer；zipalign 验证通过。
- 最终权限：网络、通知、网络状态、唤醒、震动及 JPush / MiPush 自定义权限，无定位、电话状态、全包查询、存储权限。
- 三个 ABI（arm64-v8a、armeabi-v7a、x86_64）的 Dart 二进制均含生产地址，无 localhost。

## 验证范围

- `flutter analyze` 与 4 条 widget 测试通过；未跑完整测试套件。
- 执行 release 构建、badging/生产地址/权限检查、签名验证、zipalign 验证。未做安卓真机验收，请在一台厂商 ROM 的机器（小米或华为）上验：附件 → 从相册选择 → 系统选择器里出现图库 → 选图上传成功。
