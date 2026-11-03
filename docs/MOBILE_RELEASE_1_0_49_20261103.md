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

- 归档成功（1.0.49 / 60，`/Users/wxy/openbox-ios-build-1.0.49/BossIP-iOS-1.0.49-60.xcarchive`）。
- **导出未完成**：Xcode 登录账号的钥匙串凭据失效（`DVTDeveloperAccountManager: Invalid credentials in keychain … missing Xcode-Username` → `exportArchive No Accounts`），账号列表为空；改用 API 密钥导出报 `Cloud signing permission error`（密钥 ZH3HN7FF5A 没有「访问云端管理的分发证书」权限）。本机钥匙串只有 Apple Development 证书，没有分发证书。
- 解法二选一：在 Xcode → Settings → Accounts 重新登录 Apple ID 后重跑导出；或在 App Store Connect → 用户和访问 → 集成 → 团队密钥里给该密钥勾选「访问云端管理的分发证书」，然后带密钥导出。导出通过后 `altool --upload-app`（参数同 1.0.48）。

## 桌面清理

1.0.42–1.0.48 双端包、Light 线 1.1.0 (100)–(103) 已移到废纸篓；保留 1.0.49 (60) 与 Light 线 1.1.0 (104)。
