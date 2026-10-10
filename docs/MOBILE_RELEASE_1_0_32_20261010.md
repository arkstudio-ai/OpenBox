# 移动端 1.0.32 (43) 发布记录（2026-10-10）

本版只发 iOS / TestFlight，内容是视频"下载不了"的修复（PR #65，分支 `fix/video-delivery-links`）：构建源码 `f7db0303`（修复 `d0913421` + 版本号 `f7db0303`，基于 `origin/main@464f4eff`），`API_BASE` / `WEB_BASE` 显式为 `https://ai.bossipai.com.cn`。

## 修复内容

- 图库查看器下载：有进度条，保存成功提示"已保存到「文件」"，新增"保存到相册"按钮（`gal` 2.3.3；拒绝相册权限时回退到系统保存框并提示）。
- 助手回复里的资产链接（`/api/assets/<id>/download?token=…`）在 App 内直接下载，不再跳外部浏览器撞过期 token。
- `Info.plist` 新增 `NSPhotoLibraryAddUsageDescription`。
- 配套后端（同一 PR）：助手不再贴链接，改为重新挂视频卡片（`video_generate action=attach` / `share_file asset_id`）。

## iOS / TestFlight

- 在分支工作树 `/Users/wxy/openbox-video-delivery` 构建；归档输出到工作树外 `/Users/wxy/openbox-ios-build-1.0.32/`。
- 流程：`flutter build ios --release --config-only` → `xcodebuild archive`（自动签名 + 发布 API 密钥，约 1 分钟，有缓存）→ `xcodebuild -exportArchive`（不带密钥，Xcode 账号云签名）→ `altool --upload-app`。
- 两次被 Apple 协议挡住：导出报 `PLA Update available / No signing certificate "iOS Distribution"`，上传报 `FORBIDDEN.REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED (see /business)`。账户持有人先在开发者网站同意新的 Program License Agreement，再到 App Store Connect「协议、税务和银行业务」确认一次，之后导出与上传都通过。
- `altool --upload-app` 不带 `--apple-id` 时报 `Cannot determine the Apple ID from Bundle ID`（协议未同意期间），带上 `--apple-id 6794282961 --bundle-id com.bossip.bipmobile --bundle-short-version-string 1.0.32 --bundle-version 43` 可用。
- 包内版本 `1.0.32 (43)`；`codesign --verify --deep --strict` 通过；`aps-environment=production`、`get-task-allow=false`；相册权限文案在包内。IPA 29,086,859 字节，SHA-256 `99d581baee319d55fc9ab793a165c2308a5ae9245c6f859280773c22fb7e952b`，桌面副本 `~/Desktop/BossIP-iOS-1.0.32-43/`。
- **2026-10-10 12:19（北京时间）上传成功**，Delivery UUID `bcc918bb-4ffa-42dd-8b4a-29bf98c3a625`。沿用内部「运营测试组」自动分发；未提交外部 Beta 或审核。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)。

## 验收要点

- 任一已生成视频 → 点开全屏 → 右上角"相册"图标：首次弹相册权限，允许后提示"已保存到相册"，相册里能看到视频；"下载"图标：看到进度条，弹系统保存框，保存后提示"已保存到「文件」"。
- 旧对话里助手贴的"点击下载/播放第 N 集"链接：点击直接走下载，不再跳浏览器。
- 后端上线后，在该用户会话里说"把第 15 集再发给我"，应收到视频卡片而不是链接。

## Android / 正式签名包（2026-10-10 12:36）

- 本机首次用正式发布证书 `bossip-release` 出包：用户提供的签名包已放到 `credentials/android-publishing/bossip-release.jks` 与 `mobile/android/key.properties`（均被 .gitignore 忽略），`push-vendors.gradle` 读到 `key.properties` 后把 release 构建切到该证书；小米推送 ID 来自已入库的 `push-vendors.client.properties`。
- 构建源码与 iOS 43 相同（`0551f86a`，PR #65 合并提交），干净 detached worktree，`flutter build apk --release`，`API_BASE` / `WEB_BASE` 显式指向 `https://ai.bossipai.com.cn`，Gradle 约 3 分钟。
- 校验：v2 签名、单一 signer，证书 SHA-256 `16:16:50:A5:68:32:23:01:0B:68:AC:CB:FA:08:94:F2:14:98:74:6D:55:59:A7:E2:E1:BA:EB:BF:FF:01:61:96`（与 `bossip-release` 钥匙库一致，有效期至 2095-02-28）；`com.bossip.bipmobile` versionName 1.0.32 / versionCode 43，targetSdk 36，非 debuggable，16 KiB zipalign 通过；三个 ABI（arm64-v8a、armeabi-v7a、x86_64）均含生产地址；清单含小米推送元数据。
- 产物：`mobile/build/releases/BossIP-Android-1.0.32-43-20261010/BossIP-Android-1.0.32-43-20261010.apk`（89,957,080 字节，SHA-256 `5f364db29959dbc0e16dd4613911665e9f54e4d552553cdb45bf873ee6e9434b`），桌面压缩包 `BossIP-Android-1.0.32-43-20261010.7z`（28,108,897 字节，7z 完整性通过）。
- 这是正式签名包，可直接覆盖安装商店版；装过本机 Debug 测试包的手机要先卸载。未做 Android 真机验收；Android 9 及以下机型"保存到相册"可能因无存储权限回退到系统保存框。
