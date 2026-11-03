# App 1.0.46 (57) 发布记录（2026-11-03）

配套服务端 `20261103-rooms4-bf377ba8`（见 [DEPLOY.md](DEPLOY.md)）。源码 `main@bf377ba8`，版本号提交在其上。

## 带上的改动

- **首次四步引导**（H 轮）：第一次打开助理页弹底部引导——你只跟我说 → 我开任务单派帮手 → 点任务单去现场 → 做好了我回报；可跳过；不叠在初次见面问答上；应用栏"?"随时重放；服务端记一次 `tour_seen`，Web 看过 App 不再弹。
- **任务卡显示档位**（G 轮）："项目 · 快速档 · 时间"。
- 文案 `assistant.tour.*` 与 Web 逐字节镜像。

## Android

- 正式证书 `bossip-release`（SHA-256 `16:16:50:A5…01:61:96`），`com.bossip.bipmobile` versionCode 57 / versionName 1.0.46，compileSdk 36。
- APK 105,332,810 字节，SHA-256 `13e88f5a4d0c479169010c4d0a0f77b1287260c2a8c8fb027cfa12a1a6d527f5`；7z 34,356,478 字节；在 `~/Desktop/BossIP-Android-1.0.46-57/`。

## iOS

- 归档 → 导出（云端 Apple Distribution 签名，`codesign --verify --deep --strict` 通过）→ `altool --upload-app`。
- IPA 32,182,481 字节，SHA-256 `e730a24ac6fb4fefb5b7f1ec0f269c5c1900e251aa600a06414444d5a3f93c91`；1.0.46 / 57。
- 2026-11-03 15:44（北京时间）上传成功，Delivery UUID `05d8e060-94bd-4b11-aafa-fabe70d92201`；内部测试组自动分发。桌面副本 `~/Desktop/BossIP-iOS-1.0.46-57/`。

## 验收要点（未做真机）

1. 一个从未见过引导的账号第一次进助理页：先走完初次见面问答（如有），然后弹四步引导，点"知道了"或"跳过"后不再弹；换到 Web 也不弹。
2. 应用栏"?"能随时重放引导。
3. 让助理做一条视频：不再先弹档位卡和位置卡，助理一句话说明放在哪、什么档；任务卡上显示"快速档"。
