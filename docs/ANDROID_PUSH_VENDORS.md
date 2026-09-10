# Android 厂商推送配置

厂商 SDK、极光适配器、通知点击解析和正式签名入口已接好。缺少厂商配置不阻塞开发构建；补齐某个厂商的参数后，构建自动包含对应通道。无需再修改业务通知、设备绑定或 Flutter 页面。

沿用应用 `com.bossip.bipmobile` 和极光 AppKey `20c609b5064f10d52d8351d8`。JPush 6.2.1、JCore 5.5.1，七个官方厂商适配器均为 6.2.1。构建仓库、传递依赖和混淆规则已配置。[极光厂商接入文档](https://docs.jiguang.cn/jpush/client/Android/android_3rd_guide)、[Android SDK 更新记录](https://docs.jiguang.cn/jpush/jpush_changelog/updates_Android)。

## 你需要填写的配置

1. 将 `mobile/android/push-vendors.properties.example` 复制为同目录 `push-vendors.properties`，填写要启用的厂商；不使用的厂商留空。
2. 华为、FCM 使用各自控制台下载的官方 JSON 文件，放在下表路径。构建会校验包名；无需手工改 JSON。
3. 将 `mobile/android/key.properties.example` 复制为同目录 `key.properties`，填正式签名。各厂商登记的包名、应用及证书指纹应与最终安装包一致。
4. 在旧 AppKey 对应的极光控制台启用这些厂商通道，配置厂商服务端凭据及已获批的消息分类、渠道。客户端配置完成后重新打包安装；极光后台及后端参数可单独更新。

| 通道 | 客户端输入 | 自动接入 |
| --- | --- | --- |
| 华为 | `mobile/android/app/agconnect-services.json` | AGConnect 配置资源、HMS Push、极光华为适配器 |
| 荣耀 | `HONOR_APPID` | 荣耀 Push 和极光荣耀适配器 |
| 小米 | `XIAOMI_APPID`、`XIAOMI_APPKEY` | 小米 SDK、极光小米适配器及 Manifest 元数据 |
| OPPO | `OPPO_APPID`、`OPPO_APPKEY`、`OPPO_APPSECRET` | OPPO SDK、极光 OPPO 适配器及所需依赖 |
| vivo | `VIVO_APPID`、`VIVO_APPKEY` | vivo SDK、极光 vivo 适配器及 Manifest 元数据 |
| 魅族 | `MEIZU_APPID`、`MEIZU_APPKEY` | 魅族 SDK、极光魅族适配器及 Manifest 元数据 |
| FCM（可选） | `mobile/android/app/google-services.json` | Google Services 配置资源、Firebase Messaging、极光 FCM 适配器 |

属性值填写厂商提供的原值；构建会补上极光 OPPO / 魅族适配器要求的 `OP-` / `MZ-` 前缀，已带前缀时不重复添加。小米适配器 Manifest 中附加的反斜杠由应用的元数据覆盖修正。单个厂商只填一部分参数会报配置不完整，不会生成半配置的包。

CI 可用 `BOSSIP_PUSH_<属性名>` 环境变量覆盖属性文件，例如 `BOSSIP_PUSH_XIAOMI_APPID`。构建日志仅输出启用的通道名称，不输出参数值。

`key.properties` 的 `storeFile` 支持绝对路径或相对于 `mobile/android/` 的路径，还需填写 `storePassword`、`keyAlias`、`keyPassword`。未配置时保留项目原有的开发签名；提供后自动用于 release。华为 AGConnect 的旧式 AGP 检测已适配本项目 AGP 9.0.1；后续升级 AGP 时应同步根构建脚本中的版本。

上述私有属性文件、JSON 文件、签名文件均排除 Git。OPPO `APPSECRET` 是其客户端适配器所需参数；极光 Master Secret、厂商服务端密钥和 FCM 服务账号私钥应放在后端或极光控制台，不能替代客户端配置文件。

## 服务端厂商分类与渠道

服务端仍只向当前有效绑定的一个 JPush Registration ID 发送。默认策略固定为：极光连接可用时走极光，离线后走厂商；同时拥有国内厂商与 FCM token 的设备优先使用国内厂商兜底。无需购买强制厂商下发能力。业务提醒按系统消息 `classification=1` 发送。[极光 REST API 的厂商参数](https://docs.jiguang.cn/jpush/server/push/rest_api_v3_push)。

后端可选环境变量 `BOSSIP_JPUSH_VENDOR_OPTIONS` 接收 JSON 对象，补充你已申请的渠道及分类。未配置或 `{}` 时不添加这些自定义参数，不影响 API 启动。支持：

| 厂商 | 可填写字段 |
| --- | --- |
| 小米 | `channel_id` |
| 华为 | `channel_id`、`category`、`importance`、`receipt_id` |
| 荣耀 | `importance` |
| OPPO | `channel_id`、`category`、`notify_level` |
| vivo | `category`、`callback_id` |

例如，取得小米渠道后，填入 `{"xiaomi":{"channel_id":"你已获批的渠道ID"}}`。不要把示例文字当真实值。`importance` 可为 `LOW` / `NORMAL`（华为另外支持 `HIGH`）；OPPO `notify_level` 为整数 `1` / `2` / `16`，填写时必须同时指定 `category`。这些值需要对应厂商后台实际配置，不预设或伪造分类权限。

参数只允许上述字段，非法配置在启动时明确失败。收件人、点击目标、前台抑制、发送策略及消息有效期继续由服务端控制。普通通知 channel ID 固定为 `bossip_system_notifications`，需在要求审核本地通知渠道的厂商后台登记同一 ID。

## 状态、绑定与点击

所有适配器共用 JPush Registration ID，由 JPush 管理厂商 token 注册和刷新，不创建第二套用户绑定。换手机登录会撤销旧移动会话和旧通知绑定；新手机即使尚未授权，也不会继续向旧绑定发送。

SDK 在用户同意通知说明之后初始化，华为和 FCM 的推送自动初始化均保持关闭，由极光统一启用。App 前台或仍可见但失焦时不发送系统通知；后台稳定 3 秒后允许，连续 90 秒无有效上报推断离线。完整边界见 [移动通知说明](MOBILE_NOTIFICATIONS.md)。

通知点击均进入现有 MainActivity：华为的 Intent data，以及极光和其他厂商的 `JMessageExtra`，都会解析 `n_extras`。之后等待登录与工作空间加载、核对当前用户和会话权限、去重并跳转；保留原有独立登录回调 Activity。[极光 Intent 点击协议](https://docs.jiguang.cn/jpush/practice/intent)。

## 本地验证与配置后验收

2026-09-10，七厂商全启用的 debug 和 release 构建及合并清单校验通过；3 项 Intent 解析 JVM 测试通过，后端 provider 32 项测试通过。厂商测试未使用真实参数或发送通知。

`mobile/scripts/check_push_vendors.py` 使用临时假参数验证全部七个厂商的依赖和合并 Manifest；`--release` 进一步验证正式构建及混淆。脚本遇到已有私有配置时会退出，结束后清理自己创建的假配置与 APK；不会安装 APK 或发送通知。配置自己的真实参数后直接运行正常的 Flutter 构建。

```bash
cd mobile
python3 scripts/check_push_vendors.py
python3 scripts/check_push_vendors.py --release
# 在以上检查清理假配置后，构建正常开发包：
flutter build apk --debug
```

配置完成后的真机检查：设置页开启通知并查看绑定，点击服务器测试后在 10 秒内锁屏或切后台；随后验证冷启动点击、回前台抑制、双向换机登录以及退出后的停推。分别覆盖 iOS 开发包 / TestFlight 和实际分发的 Android 厂商设备。构建通过与供应商接受请求不代表设备已经收到；系统设置的“强制停止”仍受 Android 系统限制。
