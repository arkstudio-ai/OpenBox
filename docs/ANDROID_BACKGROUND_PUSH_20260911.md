# Android 后台通知排查（2026-09-11）

## 已定位和修复

1. 本次诊断开始时，当前管理员账号的有效手机会话和推送绑定指向 `BossIP_Test_API36` 模拟器（Android `1.0.18+29`）。从 Web 发送的远程测试实际进入该模拟器的系统通知栏。用户随后确认漏收发生在实体手机。现有产品只允许一个有效手机登录和一个推送绑定，模拟器与实体手机不能同时接收该账号的通知。已停止模拟器 App，并请用户在实体手机重新登录、退到后台后进行真机验收。
2. 专项回归测试复现了另一处真实竞态：worker 领取通知后，延迟到达的 `hidden` / `paused` 上报会重新触发 3 秒后台稳定窗口。发送前复核曾将 `defer` 与 `suppress` 一并取消，导致通知永久丢失。现在 `defer` 释放租约并重新排队，退还尚未实际使用的发送次数；真正回到前台、退出登录、权限撤销和过期仍会取消投递。
3. 修正通知测试的时钟 fixture：测试接口逻辑移至 `notifications.testing` 后，原先仍 patch 已移除的 `api.push.now`，导致这组测试无法执行。

该竞态已通过测试稳定复现，但不能仅凭它断言实体手机漏收的唯一原因。

## 有限范围验证

- 新增两项竞态回归用例：修复前均失败（`cancelled/state_changed_before_send`），修复后通过，并验证通知最终只发送一次。
- `cd backend` 后执行 `.venv/bin/python -m pytest tests/integration/test_mobile_presence.py tests/integration/test_admin_push.py -q`：**24 passed**，未运行全局测试。
- 模拟器真实远程测试，使用生产通知测试入口，收件人限当前管理员账号：
  - 16:39:35（北京时间）创建，退到桌面后 16:39:45 写入系统通知栏，16:39:46 收到 App 回执。
  - 16:40:15 创建，屏幕关闭、App 上报 paused 后，16:40:26 写入系统通知栏并收到 App 回执。
  - 额外后台停留测试 16:41:34 创建，16:41:44 系统通知日志确认到达。
- 单独执行后台进程回收测试后，后续测试仅有供应商受理结果，未确认设备送达。当前 APK 未配置厂商/FCM 适配通道；此结果不计为后台送达通过。
- 用户已确认实体手机品牌为小米，具体型号及重新登录后的回执尚待提供。真机后台、锁屏及系统回收进程后的可靠性不能用模拟器结果替代。

## 发布

- 修复提交：`19cfdeba60c2f3904c028072295b07116f59b8bf`，已推送 `origin/main`。
- 本地 Docker 镜像：`openbox-backend:20260911-push-background-19cfdeb`，`linux/amd64`。
- 保留线上源码基线 `57830f8a7e1d02ef4211fc9112239fb5d17811e7`（含登录 Cookie 修复），仅追加本次 `notifications/runtime.py` 修改。线上 453 个文件与该基线校验一致；本地镜像 453 个文件与基线加本次补丁校验一致。
- 本次没有 Android 客户端代码改动，现有 `1.0.18+29` APK 无须重打包。
- 最终上线和健康检查结果见 [发布证据](evidence/android-background-push-20260911.json)。

## 跨品牌离线推送仍缺配置

本次通过已登录极光控制台核实，BossIP Android 的厂商通道为 **0/8**。本地只有配置模板，实际 `push-vendors.properties`、`agconnect-services.json`、`google-services.json` 和 `key.properties` 均不存在。项目已有小米、华为、荣耀、OPPO、vivo、魅族及 FCM 的条件依赖和配置入口，但当前 APK 没有启用这些适配器。

这些通道需要先由对应厂商为 `com.bossip.bipmobile` 签发参数，并同时完成极光控制台配置和 APK 集成；接入流程参见[极光官方厂商 SDK 指南](https://docs.jiguang.cn/jpush/client/Android/android_3rd_guide)。已请求用户提供已申请的配置文件路径或可用的开发者平台。不能用占位凭据或单个品牌测试代表全部安卓设备适配完成。

华硕通道按官方文档已包含在当前 JPush SDK 中，无需额外客户端适配器。本次尝试在控制台开启时，界面返回“修改失败”，复查开关仍关闭；没有把它记录为成功启用。

服务端修复已于北京时间 16:50 上线，四服务 healthy，推送 worker 无报错，APNs/JPush 配置正常，公网首页和 API 均返回 200。未重建前端、Postgres 和 Redis，数据库迁移版本不变。备份位于 `/opt/openbox/backups/20260911-push-background-19cfdeb/activation-20260911T084952Z`；回滚后端镜像为 `openbox-backend:20260911-login-cookie-57830f8`。配置和环境变量校验一致，OSS 临时中转对象已删除。
