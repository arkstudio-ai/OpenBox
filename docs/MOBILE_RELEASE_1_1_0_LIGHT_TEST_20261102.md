# 移动端 Light 线测试包 1.1.0 (100) Android 发布记录（2026-11-02）

**这是测试版，不是正式发布。** Light 模式整条线（PR #105–#108、#110、#112）在集成分支 `light-mode` 上单独开发、单独测，不进 main、不发 gw2（`docs/LIGHT_MODE_IMPL_PLAN.md` §10）。本包只给内部试用 Light 模式，连的是独立测试机 `https://light-test.bossipai.com.cn`（`APP_ENV=staging`，空库，与生产账号、数据互不相通），**装了也用不了生产数据**。

- 只出 Android；iOS 本次不上 TestFlight（没有空闲名额）。
- 版本号 **1.1.0 (100)**：Light 线单独占 1.1.x / 100 起的构建号，避开主线 1.0.x（最近 1.0.43 (54)）；主线下一版照常 1.0.44 (55)。
- 构建源码：分支 `claude/mobile-light-m2-35e88e`（PR #112，base `claude/mobile-first-video-005920` = PR #108，含 M1 六屏 + M2 四 tab 壳/首页/专题页），`API_BASE` / `WEB_BASE` 显式为 `https://light-test.bossipai.com.cn`。
- 对应后端：light-test 上的 `feat/light-mode-backend@050fa783`（= #107 + #110，镜像 `20261101-light-050fa783`，含迁移 `pi95b6c7d8e9`）。

## 怎么看出是测试包

- 登录页 / 落地页 / 专业模式抽屉的 bossip 品牌旁多一个红字小标「测试版」（`BrandMark`，`Env.isProduction` 为假时显示）。
- Light「我的」tab 底部写 `bossip · 测试版 · light-test.bossipai.com.cn`。
- 包名仍是 `com.bossip.bipmobile`、正式证书 `bossip-release` 签名：**会覆盖手机上的正式版**，想保留正式版请换一台机器装；装过 Debug 签名测试包的手机要先卸载。

## 测什么

- 新注册账号默认进 Light 首页（四 tab：首页 / 对话 / 消息 / 我的）；免费档「发到抖音 / 成绩复盘 / 掌柜」是灰卡「需要云电脑」，「你拍我剪」即将上线。
- 首页「做一条视频」→ 样片画廊（餐品展示 / 店内环境 / 探店）→「就做这种」→ 专题对话：顶部步骤条、开场卡与快捷 chips、拍照 / 相册、Light 大芯片卡；出片会扣测试机共享积分（H3 约 ¥1.35 / 15 秒），每人试一两条即可。
- 「做菜单」「做海报」走 imagegen；「对话」tab 的「全部对话」能看到专题会话；「我的 → 界面模式」切「专业」回今天的界面，设置 → 外观里能切回 Light。
- 首条视频引导（M1 六屏）从注册进入，免费一条。

## 构建与校验

- 构建：本机 Flutter 3.44.2，分支工作树 `.claude/worktrees/mobile-light-m2-35e88e`，`flutter build apk --release --dart-define=API_BASE=https://light-test.bossipai.com.cn --dart-define=WEB_BASE=https://light-test.bossipai.com.cn`，Gradle 约 3 分钟（2026-11-02 00:26–00:29）。`credentials/android-publishing/bossip-release.jks` 与 `mobile/android/key.properties` 从主仓库拷入工作树（均被 .gitignore 忽略）。
- 校验：v2 签名、单一 signer，证书 SHA-256 `16:16:50:A5:68:32:23:01:0B:68:AC:CB:FA:08:94:F2:14:98:74:6D:55:59:A7:E2:E1:BA:EB:BF:FF:01:61:96`（= 正式证书 `bossip-release`）；`com.bossip.bipmobile` versionName 1.1.0 / versionCode 100，targetSdk 36，非 debuggable，16 KiB zipalign 通过；三个 ABI（arm64-v8a、armeabi-v7a、x86_64）的 `libapp.so` 都含 `https://light-test.bossipai.com.cn`、不含 localhost；各含一处 `https://ai.bossipai.com.cn`，那是 `Env.productionBase` 常量（判断「测试版」标记用），不是请求地址；清单含小米推送元数据。
- 产物：`mobile/build/releases/BossIP-Android-1.1.0-100-light-test-20261102/BossIP-Android-1.1.0-100-light-test-20261102.apk`（106,389,411 字节，SHA-256 `1716e7f0accb47ddd948c4bd8f75a77b4c6bf651ced591d572c8a69926748228`）；7z `BossIP-Android-1.1.0-100-light-test-20261102.7z`（34,638,330 字节，SHA-256 `34018f3f03ea5c4d4ecc8f81aa35125f55fcda5fe6593185813e5596498dfec6`，完整性通过）；桌面副本 `~/Desktop/BossIP-Android-1.1.0-100-light-test/`（apk + 7z + README.txt）。
- 代码变化（同在 PR #112 分支）：`pubspec.yaml` 1.0.42+53 → 1.1.0+100；`Env.isProduction` / `Env.apiHost`；`BrandMark` 非生产时带「测试版」标；Light「我的」底部显示测试域名；文案 `light:testBuild`。

## 范围说明

- 未做 Android 真机验收（本机只有模拟器）；未跑真实出片。
- 测试机没有云电脑、抖音开放平台回调域名未加，发到抖音 / 掌柜 / 成绩复盘不可用（设计上就是灰卡）。
- 测试机包月到 2026-12-01，到期前要么续，要么这包就连不上了。
