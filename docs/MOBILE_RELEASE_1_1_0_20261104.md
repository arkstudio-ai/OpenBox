# App 1.1.0 (104) Light 线 iOS TestFlight 发布（2026-11-04）

Light 线（集成分支 `light-mode`，不进 main）的 iOS 测试包首次上 TestFlight；与 11-03 的 Android 1.1.0 (104) 同源，接口指向 light-test。

## 源码与构建

- 源码 `light-mode@03920da2`（已吸收 main 3ed4e708、含 #130/#136），版本 `1.1.0+104`，不另 bump。
- 本机 Flutter 3.44.2 / Xcode 26.6，干净 worktree `/Users/wxy/openbox-ios-light-1.1.0`。
- `flutter build ios --release --config-only --dart-define=API_BASE=https://light-test.bossipai.com.cn --dart-define=WEB_BASE=https://light-test.bossipai.com.cn` → `xcodebuild archive`（自动签名 + 发布 API 密钥）→ `xcodebuild -exportArchive`（`ExportOptions-export.plist`，Xcode 账号云签名）→ `xcrun altool --upload-app`（带 `--apple-id 6794282961 --bundle-id com.bossip.bipmobile --bundle-short-version-string 1.1.0 --bundle-version 104`）。归档 1 分钟、导出 1 分钟、上传 8 秒（03:26，Delivery `38a20443-8ceb-4eac-8f1b-481c7d171f37`），Apple 约 5 分钟处理为 VALID。
- IPA 34.4 MB，sha256 `3980f845e20452e6…`，副本 `~/Desktop/BossIP-iOS-1.1.0-104-light-test/`。`Info.plist` 为 1.1.0 / 104 / `com.bossip.bipmobile`。

## TestFlight 分组重建（防止测试包换掉运营手机的生产 App）

- 原内部组「运营测试组」开着自动分发，任何新构建处理完即推给 8 名运营测试员；1.1.0 (104) 版本号高于他们的 1.0.49 (60)，会把生产 App 换成连 light-test 的测试包。App Store Connect API 不能改已有组的 `hasAccessToAllBuilds`（PATCH 409 `ATTRIBUTE.NOT_ALLOWED`），也不能把构建挂到自动分发的内部组（422）。
- 用户拍板重建：新建内部组「运营测试组」（`5cfec383-82de-44c9-a929-0f2aa20b2742`，不自动分发），加入同样 8 人，挂上原组全部 28 个未过期构建（14–60），核对后删除旧组 `96cb2746…`。**此后主线每次上传 1.0.x 都要手动把新构建挂给这个组**（ASC 网页或 `POST /v1/betaGroups/5cfec383…/relationships/builds`），运营测试员才看得到。
- 新建内部组「Light 测试」（`491a06a7-ed83-4d5c-b7de-57b169d7db07`，不自动分发），只挂 1.1.0 (104)；暂无成员，由用户按需加（内部测试员必须是团队成员）。
- 上传后核对：Light 测试组构建 = [104]；运营测试组构建 = 14–60，不含 104；`internalBuildState=READY_FOR_BETA_TESTING`。

## 未做

- 真机未验（相机、相册上传、「不要配乐 / 换配乐」）；外部测试组未挂（外部分发要过 Beta App Review）。
- Light 线并回主线后，1.1.x 与 1.0.x 的版本号要合并规划（Light 线独占 1.1.x / 100+）。

## 1.1.0 (105) — 2026-11-04 05:00，同日第二版

- 源码 `light-mode@765a71c1`（含 #138 只留助理 tab、#139 问答遮罩式弹层 + 渐隐、#140 首页提示行、#141 助理感知、#142 样例库），版本 `1.1.0+105`，接口仍指向 light-test（后端同步发 `20261104-light-765a71c1`）。
- **Android**：`flutter build apk --release`，正式签名 `bossip-release`（SHA-256 `16:16:50:A5…01:61:96`），versionCode 105，108.6 MB，sha256 `c950c11b5353de15…`，在 `~/Desktop/BossIP-Android-1.1.0-105-light-test/`。
- **iOS**：归档 + 导出 + 上传 1 分钟（Delivery `4c015757-6dc2-4a81-b107-bd572b21d462`），Apple 处理为 VALID 后挂到内部组「Light 测试」；该组现在是 [104, 105]，「运营测试组」仍是 14–60，不含 Light 构建。IPA 34 MB，sha256 `78b2c367d906a3f6…`，在 `~/Desktop/BossIP-iOS-1.1.0-105-light-test/`。
- 构建插曲：Docker Desktop 守护进程没在跑，后端首次构建失败，`open -a Docker` 后重跑即可。
