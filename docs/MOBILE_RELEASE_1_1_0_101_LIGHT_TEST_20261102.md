# 移动端 Light 线测试包 1.1.0 (101) 发布记录（2026-11-02，阶段 A）

**测试版，不是正式发布。** 连独立测试机 `https://light-test.bossipai.com.cn`（后端 `20261102-light-520b4184`），不进 main、不发 gw2（`docs/LIGHT_MODE_IMPL_PLAN.md` §10）。上一版 1.1.0 (100) 见 `MOBILE_RELEASE_1_1_0_LIGHT_TEST_20261102.md`。

- **只出 Android。** iOS 没上 TestFlight：App Store Connect 的内部组「运营测试组」开着自动分发，1.1.0 版本号高于主线 1.0.43，一上传就会把运营同事手机上的生产 App 自动更新成连 light-test 的测试包。要上 iOS，需先给 Light 线单独建一个不自动分发的测试组（或关掉该组的自动分发），由用户决定。
- 版本 **1.1.0 (101)**（Light 线 1.1.x / 100+）。包名 `com.bossip.bipmobile`，正式证书 `bossip-release` 签名（SHA-256 `16:16:50:A5:68:32:23:01:0B:68:AC:CB:FA:08:94:F2:14:98:74:6D:55:59:A7:E2:E1:BA:EB:BF:FF:01:61:96`），**会覆盖正式版**。
- 产物：`mobile/build/releases/BossIP-Android-1.1.0-101-light-test-20261102/`（apk 108,412,534 字节，SHA-256 `603e59918129a85c8a233ef5257034bd4c6c14a8677a49bc1ee90cac73b51b92`），桌面副本 `~/Desktop/BossIP-Android-1.1.0-101-light-test/`（apk + .sha256 + README.txt）。
- 校验：v2 签名单一 signer = 正式证书；versionName 1.1.0 / versionCode 101，targetSdk 36；`libapp.so` 含 `https://light-test.bossipai.com.cn`（另一处 `https://ai.bossipai.com.cn` 是判断「测试版」标的常量）。
- 构建：`flutter build apk --release --dart-define=API_BASE=https://light-test.bossipai.com.cn --dart-define=WEB_BASE=https://light-test.bossipai.com.cn`，Gradle 94 秒；签名文件从主仓库 `credentials/android-publishing/` 与 `mobile/android/key.properties` 拷进工作树（gitignore）。

## 相比 1.1.0 (100) 的变化

- 样片：开屏自动播一条真实成片（随包 1.7 MB）；首条视频选模板页有真实截帧，点播放看样片；Light 样片画廊 16 条真实样片（做视频 9 / 菜单 4 / 海报 3），视频可全屏播放。
- 做一条视频：免费档走新技能 `light-video`，一张 review 卡后出片并叠店名；结果卡带「保存 / 发到抖音 / 再做一条」，免费档「发到抖音」提示需要云电脑并去开通页。
- 做菜单（`menu-maker`）/ 做海报（`poster-maker`）专用技能。
- Light 卡片：「(Recommended)」显示为「推荐」小标；不显示「Agent 已暂停」；结果卡不显示文件名；出片后步骤条点亮最后一步；积分显示一位小数。
- 首条视频成片页「发到抖音」去开通页后能返回（原来是死路）。

## 模拟器实测（iPhone 17 Pro 连 light-test，账号 `light-full-1102a`）

- 注册 → 六屏：店名 / 模板（播放样片）/ 相册选菜品图 → 制作中 → 成片（约 1.5 分钟，保留参考图的菜，店名与主打叠字正确）→ 发到抖音（免费档）→ 开通页。
- Light 首页 → 做一条视频 → 店内环境 · 傍晚亮灯 → 相册选店内图 → review 卡 → 生成 3 分 08 秒、叠字 IMS 排队 2 分 36 秒（超出一轮等待，合成完成后由 F5 唤醒对话贴片）→ 结果卡。
- 做菜单 → 烟火红 → 相册选旧菜单 → 菜品表 review → 手机竖图 → 出图（菜名价格正确）→ 结果卡。
- 暗色：首页、样片画廊、对话 tab。

## 未验证

- Android 真机与 Android 模拟器（本机无 Android 模拟器）；相机拍照（模拟器无相机）。
- 中文输入在模拟器里打不进（工具限制），实测店名用的拼音；中文店名叠字 11-02 已经 API 实测过。

## 1.1.0 (102)（2026-11-02 17:42，阶段 B）

- 新增省心托管：结果卡「以后省心托管这个」→ 三问（每天 10:00 / 只出片 / ≤10 积分为默认；免费档「直接发」锁定）→ 托管中的事（首页黑色横幅、我的）可暂停 / 继续 / 打开对话 / 取消；Light 模式下点通知直接打开对应专题对话。
- 后端 `20261102-light-20e41c94`。apk 108,528,574 字节，SHA-256 `528a7927d512b3d2b3bd66e40b9506dceb59c2a7b14ac07019939845b638b217`，正式证书签名，versionCode 102；桌面 `~/Desktop/BossIP-Android-1.1.0-102-light-test/`。
- 实测：模拟器从 Light 做视频的结果卡开托管、手动触发一次（79 秒在原对话出一条带店名的新片，账本 3.75 + 0.045，通知 +1）、暂停、继续（下次 11-03 10:00）。

## 1.1.0 (103)（2026-11-02 18:11，阶段 C）

- 新增你拍我剪：首页「你拍我剪」转为可用（免费档也可）；专题页相册可多选视频（最多 10 段，单段 ≤200 MB），逐段上传显示「正在上传第 n/N 段」；方案卡 → 成片 → 推送。
- 后端 `20261102-light-6c2d97f0`（迁移 `pj06c7d8e9f0`）。apk 108,610,638 字节，SHA-256 `7a3ed9310d418780b45761dc24503d085fc0232636052013b02e926802fc2fa1`，正式证书签名，versionCode 103；桌面 `~/Desktop/BossIP-Android-1.1.0-103-light-test/`。
- 实测：免费档账号 5 段（含横拍、HEVC .mov）→ 方案卡 → 60 秒成片，账单「素材分析 2」「成片 5」与方案卡一致；总耗时约 6 分钟（细节 `docs/LIGHT_FOOTAGE_EDIT.md` §3）。
