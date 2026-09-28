# 移动端 1.0.27 (38) 发布记录（2026-09-28）

Android 内部测试包，接 1.0.26 (37) 的反馈：37 的「从相册选择」在厂商 ROM 上仍唤起文件管理器，只是跳到了相册文件夹。构建源码 `main@b82d9030`（修复 `739b966d` + 版本号 `b82d9030`），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。iOS 行为与 1.0.25 (36) 相同，未重发。

## 根因与修法

- 37 的相册入口用 `FileType.media`，file_picker 插件把它发成主类型 `*/*` 的 `ACTION_GET_CONTENT`，图片/视频只放在 `EXTRA_MIME_TYPES` 里。图库 App 登记的是 `image/*`、`video/*`，匹配不上 `*/*`，唯一接单的是能处理 `*/*` 的厂商文件管理器，于是按类型跳到相册文件夹。
- 现在安卓菜单拆为「照片 / 视频 / 浏览文件」：照片走 `FileType.image`（`image/*`），视频走 `FileType.video`（`video/*`），这两种类型所有图库 App 都登记了，系统选择器会列出图库（或直接进图库）。iOS 保持「从相册选择 / 浏览文件」（PHPicker 图片+视频一起选）。
- 菜单按平台生成（`pick_source.dart`），测试钉住安卓两条媒体路线和 iOS 单一相册路线。

## Android（内部测试包，Debug 证书）

- 签名仍是 Android Debug 证书（SHA-256 `98d7531d…72df0`）；发布证书 `bossip-release` 与小米推送配置只在 andrew 机器，正式包需他从 `main@b82d9030` 重打。装有 1.0.24 正式签名包的手机需先卸载；装了 37 的可直接覆盖。
- APK：`mobile/build/releases/BossIP-1.0.27-38/BossIP-Android-1.0.27-38-debugsigned.apk`，85,541,523 字节，SHA-256 `c83662a42ae4439744d40bb3b1613c5962938491f738e896eed9d797158b7548`；同目录 7z；桌面副本 `BossIP-Android-1.0.27-38/`。
- 包名 `com.bossip.bipmobile`，`versionName 1.0.27`、`versionCode 38`，targetSdk 36。v2 签名验证通过、单一 signer、zipalign 通过；三个 ABI 均含生产地址，无 localhost。权限集与 1.0.26 相同。

## 验证范围

- `flutter analyze` 与 4 条 widget 测试通过；未跑完整套件、未做真机验收。
- 请在 37 复现问题的那台机器上验：附件 → 照片 → 系统选择器出现图库（若弹出「文件管理 / 相册」选择框，选相册）→ 选图上传成功；再验「视频」入口。若厂商弹选择框时默认记住了文件管理器，需在系统设置里清除默认应用。
