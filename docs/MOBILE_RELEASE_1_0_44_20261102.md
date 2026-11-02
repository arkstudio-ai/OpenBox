# App 1.0.44 (55) 发布记录（2026-11-02）

配套服务端 `20261102-rooms-a9278fd2`（见 [DEPLOY.md](DEPLOY.md)）。源码 `main@a9278fd2`，版本号提交在其上。

## 带上的改动（全部为方案 [VOICE_SEAM_PLAN_20261031.md](VOICE_SEAM_PLAN_20261031.md) 第 11 节）

- 通话页卡片栏首页常驻**对话卡**：通话通道显示本通电话里你说的和助理答的（进行中的回复斜体实时刷出，听你说时显示"在听你说…"）；助理点名某个任务后出现"任务间：「X」"第二通道，显示那间执行会话的最近来往、"任务正在生成视频，好了会叫你"进度和"进这间说"按钮；口头进门后自动切到任务间通道并提示"你在这间说话"。
- 执行页顶部常驻"**你在任务间：「任务标题」· 状态 · 回到总助理**"，进入时弹一次"已进入「X」"。
- "我的任务"里同一件事的多轮任务折成一项（"共 N 轮，显示最新"），出错超过一天的提示"建议不再跟进"。
- 文案与 Web 逐字节镜像（`chat.json` 新增 `room.*`、`assistant.taskList.chain/stale`；`voice.json` 新增 `transcript.*`，并补回 main 上只有 App 有的 `controls.resume`/`interruption.audio`）。

## Android

- 工作树 `/Users/wxy/openbox-app-1.0.44`（`origin/main@a9278fd2` + bump），拷入 `credentials/android-publishing/bossip-release.jks` 与 `mobile/android/key.properties`。
- `flutter build apk --release --dart-define=API_BASE=https://ai.bossipai.com.cn --dart-define=WEB_BASE=https://ai.bossipai.com.cn`。
- `apksigner verify` 通过，签名 `CN=BossIP Android Release`，证书 SHA-256 `16:16:50:A5:68:32:23:01:0B:68:AC:CB:FA:08:94:F2:14:98:74:6D:55:59:A7:E2:E1:BA:EB:BF:FF:01:61:96`（可覆盖升级 1.0.43）；`aapt2` 读出 `com.bossip.bipmobile` versionCode 55 / versionName 1.0.44，compileSdk 36；`libapp.so` 含生产地址。
- APK 105,332,006 字节，SHA-256 `679931107de3e914c942a5555046fd7b6b7c1a2954b723b327db227b545793bc`；7z 34,348,876 字节；两者在 `~/Desktop/BossIP-Android-1.0.44-55/`。

## iOS

- `flutter build ios --release --config-only`（同上 dart-define）→ `xcodebuild archive`（API 密钥）1 分钟 → `-exportArchive`（不带密钥，云端 Apple Distribution 签名）→ `altool --upload-app`。这次没有撞 Apple 协议门。
- IPA 32,170,208 字节，SHA-256 `cb63bd641df8021ef99d330b05b42352caa80758f7c07a4fd7b68b08d0b7ac10`；`CFBundleShortVersionString` 1.0.44 / `CFBundleVersion` 55，MinimumOSVersion 15.0，`ITSAppUsesNonExemptEncryption=false`，`aps-environment=production`，`codesign --verify --deep --strict` 通过。
- 2026-11-02 17:40:22（北京时间）上传成功，Delivery UUID `b0a3b285-7b13-4ed4-8ea3-aebac1e11f29`；内部测试组自动分发。桌面副本 `~/Desktop/BossIP-iOS-1.0.44-55/`。

## 验收要点（未做真机）

1. 打电话，说一句话：对话卡出现你的话和助理的回答，助理说话时字实时刷出。
2. 让助理看某个任务或交办一件事：卡顶出现"任务间：「X」"，切过去能看到那间的最近来往。
3. 说"进 X 那间"：卡自动切到任务间通道并提示"你在这间说话"；再说一句要求，任务页里出现这句输入；说"回去"退出。
4. 从任务卡进执行页：顶部有"你在任务间：「X」· 状态 · 回到总助理"，点按钮回到助理。
5. 在任务页按停止：电话里十几秒内听到"「X」刚刚被停下了，是你在任务页按了停止"。
