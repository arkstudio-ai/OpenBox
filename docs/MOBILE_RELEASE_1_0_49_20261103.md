# App 1.0.49 (60) 发布记录（2026-11-03）

源码 `main@3df3dc8b`，版本号提交在其上（分支 `app/1.0.49`）。配套服务端仍是 `20261103-videofix-12cecc23`（gw2 rooms4 + videofix 补丁；I 轮的 rooms5 镜像已就位未切换，见 [DEPLOY.md](DEPLOY.md)）。

## 带上的改动（相对 1.0.48）

- **通话抽屉去重**（PR #133）：通话条上已有静音和挂断，抽屉底部那排大圆钮是重复的，去掉。抽屉底只留一行：左边本次消耗，右边通话条没有的扬声器/听筒切换；被打断暂停时换成「恢复通话」。`voice_call_controls.dart` 删除。
- **抽屉自带 Material**（PR #134）：抽屉在 Navigator 之上没有 Material 祖先，文字出现黄色双下划线（1.0.48 在 iOS 模拟器实测可见）；包一层透明 Material 后消失。
- **iOS 模拟器可拨通**（PR #134）：模拟器的 callservicesd 拒绝所有 CXStartCallAction（`com.apple.CallKit.error.requesttransaction:5`），拨打必失败。`#if targetEnvironment(simulator)` 下桥接不走 CallKit，直接起 AVAudioSession。真机路径不变。模拟器实测：接通、逐字稿（助理左/我右）、花费行、扬声器按钮都对。

## Android

- 正式证书 `bossip-release`（SHA-256 `16:16:50:A5…01:61:96`），`com.bossip.bipmobile` versionCode 60 / versionName 1.0.49，compileSdk 36。
- APK 105,218,230 字节，SHA-256 `e2a790eced69369e85d075070903c9638f14b084b84dc33db18d644d25b526c5`；7z 34,335,132 字节；在 `~/Desktop/BossIP-Android-1.0.49-60/`。

## iOS

- 归档 → 导出（云端 Apple Distribution 签名，`codesign --verify --deep --strict` 通过）→ `altool --upload-app`。
- IPA 32,162,254 字节，SHA-256 `339a45989f235c1fab9e810756678557ce00539d7c36a97ac884ad43175eac03`；1.0.49 / 60。
- 2026-11-04 01:12（北京时间）上传成功，Delivery UUID `73355fcb-5c1d-4303-90f6-476b930d3953`；内部测试组自动分发。桌面副本 `~/Desktop/BossIP-iOS-1.0.49-60/`。
- **中间卡过一次**：首次导出报 `No Accounts`（Xcode 的 Apple ID 登录态丢失，钥匙串凭据 `missing Xcode-Username`），带 API 密钥导出报 `Cloud signing permission error`（密钥 ZH3HN7FF5A 无云端分发证书权限）。在 Xcode → Settings → Accounts 重新登录后导出即过。

## 桌面清理

1.0.42–1.0.48 双端包、Light 线 1.1.0 (100)–(103) 已移到废纸篓；保留 1.0.49 (60) 与 Light 线 1.1.0 (104)。
