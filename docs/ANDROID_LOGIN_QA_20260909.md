# Android 登录回跳修复与验收 — 2026-09-09

## 交付与结论

- App：`com.bossip.bipmobile`，版本 **1.0.16 / build 27**。
- 源码：`1e7580f`（认证回跳）与 `d8f9ebe`（键盘/侧栏回归）。
- APK：`BossIP-1.0.16-build27-aliyun.apk`，79,290,595 字节。
- APK SHA-256：`90050a0927984745cf8cc4bc4ba73cc4ead62b9a0786fbf602d636a57e9d6f56`。
- API 和 Web 均为 `https://ai.bossipai.com.cn`，身份服务为 `https://auth.bossipai.com.cn`。
- 使用用户授权的生产账号进行 Android 16 登录及 Ask 验证；不在报告、代码或交付包中保存密码、令牌、有效 OAuth 回调 URL。
- 本次没有修改/部署后端，没有更改数据库、线上无影云配置或本地 `.env.wuying-dev`。阿里云现有前后端容器健康且重启次数为 0。

这是 release 优化的侧载验收包，沿用仓库现有 debug 签名证书，APK v2 签名校验通过；不是应用商店正式签名包。安装时若旧版本使用不同证书，会被 Android 拒绝覆盖，不能通过改签或删除用户数据绕过。

## 根因与修复

在 Android 16 / Chrome 133 上，旧代码（`7ad0f9d`）可以完成 OAuth 和后端令牌交换，但 Chrome 仍停留前台超过 15 秒，手动关闭认证页才看到已登录工作台。

Custom Tabs 用 `NEW_TASK` 发回 custom-scheme URI，回调 Activity 与原 App 处于不同任务栈。默认插件完成 Dart 回调后处理的是新任务中的认证管理 Activity，未把原 Flutter 任务恢复到前台。

修复由唯一 `.AuthCallbackActivity` 接收回调，验证 action/scheme/host/port/path，单次取出 SDK 待处理回调，并通过 `ActivityManager.AppTask` 恢复原 `MainActivity`。不存在原任务时安全冷启动。SDK 的 state、PKCE、重定向和令牌校验保留，OAuth URI 不传给 Flutter 路由。未采用另建 `singleTask` 认证任务的方案。

另修复：同帧重复点击启动多次登录、后端返回不完整凭据时错误进入登录态、首页旧翻译键与窄屏英文排版，以及聊天键盘遮挡侧栏底部账号菜单。

## 验证范围与结果

环境：macOS Apple Silicon；Pixel 6 模拟器 1080×2400；Android 16 / API 36 / Chrome 133.0.6943.137，Android 15 / API 35。两个模拟器顺序运行，最终连通性复测采用明确的 DNS 配置。

| 场景 | 验证方式 | 结果 |
|---|---|---|
| 真实生产登录自动回 App | Android 16，实际用户名/密码与 Logto | 通过，同一 MainActivity/task，无需手动返回 |
| Home → 最近任务 → 完成登录 | Android 16 原生 Activity 任务栈（模拟器） | 通过，原 App 被恢复到前台 |
| 取消认证、再次尝试 | Android 16 关闭 Custom Tab；widget 回归 | 通过，保持未登录，按钮可继续使用 |
| 退出 OpenBox + Logto，再登录 | Android 16 实际 end-session | 通过，自动回 App，再登录显示凭证页 |
| 旋转屏幕后认证恢复 | Android 16 | 通过 |
| 浏览器不存在 | Android 16 临时禁用测试浏览器，完成后恢复 | 通过，可见失败提示，可重试，无错误登录态 |
| 登录时断网/恢复网络 | Android 16 临时关闭 Wi-Fi/数据后恢复 | 浏览器明确显示断网；网络恢复后页面可用，真实登录成功 |
| 错误 state | Android 16 注入测试回调 | 拒绝认证，未登录 |
| 错误 callback host | Android 16 注入测试回调 | 不消费当前认证，不错误登录 |
| App 进程被系统结束后的迟到回调 | Android 16 `am kill`，确认原 PID 消失 | 安全打开未登录 App，无崩溃，不凭回调伪造会话 |
| 无待处理请求的冷回调 | Android 15 最终 APK | 安全冷启动到未登录首页 |
| 重复点击、平台错误、401/429/503、不完整响应 | Flutter widget 测试（模拟 SDK/HTTP 边界） | 通过；异常不形成假登录态且可重试 |
| 配置/发现接口、无效 ID token、未登录 me | Android 15 和 16 原生 Dio 集成测试，真实生产 HTTP | 成功接口为 200；两个拒绝用例均为 401 |
| 三问题 Ask 与任务续跑 | Android 16，真实生产会话 | 单选/自填自动下一页；手动前后切换保留答案；确认后收到 `TEST COMPLETE` |
| 首页中英文、360px 布局 | Flutter 使用实际 locale 的 widget 测试 | 通过，无原始翻译键或横向溢出 |
| 打开侧栏收起键盘，侧栏搜索仍可输入 | 点击和滑动两种 widget 回归；最终 APK Android 16 复测 | 通过 |
| release APK 安装、冷启动、签名 | Android 15 和 16；apksigner | 通过 |

Ask 测试只创建验收对话并请求三个简单问题，没有调用云桌面、生成视频或发布内容；验收对话保留供回看。

自动化结果：`flutter test` **137 项通过**；`flutter analyze` 无问题；mobile locale 与 Web 逐字节一致；`git diff --check` 通过。

单文件行数门禁不是全绿：既有 `test/features/chat/question_dock_test.dart` 为 1083 行（限制 800），在基线 `7ad0f9d` 中已存在。本次未删除或拆改该测试以掩盖问题；其功能测试通过。

## 性能采样

以下是小样本端到端耗时，不是压测、容量指标或所有网络的承诺。HTTP 由 Android 原生 Dio 在 debug 集成测试中访问真实 HTTPS 接口；无并发施压。

| 项目 | 样本 | 结果 |
|---|---|---|
| Android 16 登录：点击密码页 Continue → MainActivity 前台 | 修复后 3 次成功样本 | 1.281 / 1.634 / **2.153 秒**；最后一次为最终交付 APK |
| 最终 APK 冷启动，Android 15 | 1 次 | **1.233 秒**（Activity launch time） |
| 最终 APK 冷启动，Android 16 | 1 次 | **2.168 秒**（Activity launch time） |
| Android 15 无待处理请求冷回调 → App | 1 次 | **1.256 秒**，保持未登录 |
| Android 15 生产配置接口 | 10 次 | 中位数 **109ms**，最大 **119ms**，全部 200 |
| Android 15 Logto discovery | 10 次 | 中位数 **115.5ms**，最大 **1649ms**，全部 200 |
| Android 16 生产配置接口 | 10 次 | 中位数 **116.5ms**，最大 **634ms**，全部 200 |
| Android 16 Logto discovery | 10 次 | 中位数 **414ms**，最大 **7951ms**，全部 200 |
| 三问题 Ask | 1 个真实会话 | App 显示首段运行 6.1s、总运行 8.7s；不含人工答题等待时间 |

前期双模拟器/初始网络状态曾触发 10 秒超时，也出现过 4.529 秒启动样本。顺序重启并明确 DNS 后完成上述复测。宿主机相同 Logto 接口的同期样本约 188–212ms；目前不能据模拟器长尾把慢请求归因于生产后端，也不宣称所有请求都在 1 秒内。

阿里云只读复核：`openbox-backend:20260909-ask-2183504` 与 `openbox-frontend-v2:20260909-ask-2183504` 运行且健康。抽样窗口中原生 token exchange 200×4、401×2；401 对应本轮两个 Android 的无效令牌安全测试。最终 APK 的额外真实登录也在客户端确认完成。

## 仍未覆盖的边界

- 未连接物理 Android 手机：小米/华为/OPPO 等 OEM 浏览器、后台限制和深链设置不能由模拟器代替验收。
- Android 15 验证了安装/启动、生产接口和失效回调，但其 Chrome 首次使用要求接受新服务条款，未代用户接受，故未完成该设备的真实浏览器登录。
- 新版 Chromium 同样停在首次条款页；现代 Auth Tabs 的 ActivityResult 路径未完成真实登录测试。已移除本轮临时安装的测试 Chromium。
- 429/503、平台错误等由确定性测试注入，未故意使生产服务限流/故障。未模拟全站停机、生产数据库断开或对其他用户进行测试。
- 不承诺“所有异常、所有机型均覆盖”；已覆盖本次复现缺陷及表列安全/恢复路径。

## 为什么仍使用浏览器登录

Logto 有原生/Flutter SDK，但官方 Flutter 方案在 Android 使用系统认证浏览器（Custom Tabs），登录结束应该自动回 App，并非停留在浏览器。
Logto 官方不提供无界面的用户名/密码登录注册 API；原生 SDK 不等于原生凭据表单。保留标准 OIDC/PKCE 流程并修复自动回跳，不绕过认证边界。

参考：[Flutter 官方集成](https://docs.logto.io/quick-starts/flutter)、[登录注册能力边界](https://docs.logto.io/end-user-flows/sign-up-and-sign-in)。

## 重建与回归

在 `mobile/` 运行：

```sh
flutter test
flutter analyze
bash scripts/check_locales.sh
flutter test integration_test/production_network_test.dart -d emulator-5554 \
  --dart-define=API_BASE=https://ai.bossipai.com.cn \
  --dart-define=WEB_BASE=https://ai.bossipai.com.cn
flutter build apk --release \
  --dart-define=API_BASE=https://ai.bossipai.com.cn \
  --dart-define=WEB_BASE=https://ai.bossipai.com.cn
```

集成测试之后执行 release build 时保留默认 pub 步骤，重新生成非测试插件注册；不要沿用集成测试注册结果直接 `--no-pub` 打包。测试中的 API/WEB 均显式指向生产，不加载无影云开发配置。
