# App 1.0.47 (58) 发布记录（2026-11-03）

配套服务端 `20261103-rooms5-9da1cf93`（见 [DEPLOY.md](DEPLOY.md)）。源码 `main@9da1cf93`，版本号提交在其上。

## 带上的改动

- **通话页的任务单页终于出现**（I 轮）：1.0.45/1.0.46 只加了参数没放进牌组，这次补上。打电话时当前这件活的任务单是牌组第二页。
- **"盯现场"开关**：任务单上方一个开关，打开后你说的话直接给这件活、它的回复直接播；活做完或被停下服务端自动关掉。协议新增客户端→服务端 `room {task_id|null}`。
- **这件活的提问/授权并到任务单页**：不再各占一页，别的活的仍单独成页。
- 文案 `voice.transcript.watch*` 与 Web 逐字节镜像。

## Android

- 正式证书 `bossip-release`（SHA-256 `16:16:50:A5…01:61:96`），`com.bossip.bipmobile` versionCode 58 / versionName 1.0.47，compileSdk 36。
- APK 105,431,222 字节，SHA-256 `05e9728b4d683ab0c6dd4611ea4dc9cf325e124e107fe8e10a6385eeea2e71eb`；7z 34,340,552 字节；在 `~/Desktop/BossIP-Android-1.0.47-58/`。

## iOS

- 归档 → 导出（云端 Apple Distribution 签名，`codesign --verify --deep --strict` 通过）→ `altool --upload-app`。
- IPA 32,182,612 字节，SHA-256 `fd12dc5be24203b09a91e535ec2397997c3f9f1628f2dee49adc7122caed88a2`；1.0.47 / 58。
- 2026-11-03 16:12（北京时间）上传成功，Delivery UUID `80a9ad8c-3c88-4ee3-939c-8bcf05a4797f`；内部测试组自动分发。桌面副本 `~/Desktop/BossIP-iOS-1.0.47-58/`。

## 验收要点（未做真机）

1. 打电话交代一件活：牌组第二页是这件活的任务单，上方有"盯现场"开关。
2. 打开开关后说一句要求：电话里直接回的是这件活的帮手；活做完后开关自动变回关。
3. 这件活的帮手提问出现在任务单下方同一页；别的活的提问仍单独一页。
4. 老服务端（未切 rooms5）上开关发出的消息会被忽略，页面不报错。
