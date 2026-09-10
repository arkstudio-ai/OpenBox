# 移动通知接口与单手机登录

已接通通知基础设施、业务触发、生命周期上报和设置入口，支持 iOS APNs、Android 极光，以及最后一部手机登录生效。服务端配置与初次迁移见 [发布记录](MOBILE_PUSH_DEPLOY_20260910.md)。

系统通知只在 App 不可见或超时推断离线时允许投递；所有前台页面都保持安静。Android 七个厂商适配器及配置入口已接入，参数配置见 [Android 厂商推送配置](ANDROID_PUSH_VENDORS.md)。

## 登录与绑定规则

- 原生登录请求携带 `X-Client-Type: mobile`、`X-Installation-Id`。安装 ID 是本机生成的随机值，不是硬件标识；格式为 16–128 位字母、数字、`_`、`-`。
- 手机登录、注册、原生 Logto 登录返回 `mobile_session_id`。JWT 的 `client=mobile`、`sid` 必须与数据库中该用户的唯一有效移动会话一致。
- 新手机登录即撤销旧移动会话、关闭旧通知绑定、取消旧绑定的 pending/sending 投递。新手机尚未授权或没有 token，也会撤销旧手机。
- 刷新保留同一个 `sid`，只能延长仍有效的会话；旧手机不能通过刷新或晚到的注册请求抢回登录。会话有效期与现有刷新令牌配置一致，默认 7 天。
- 同一个安装切换账号，也会撤销该安装原账号的移动会话。绑定 token 有唯一约束，不能同时属于两个用户。重复登记幂等；换 token 或重新启用通知会生成新的 `bindingId`。
- 退出登录撤销当前移动会话并清理本机通知。旧 `bindingId` 的解绑请求不会禁用新绑定。
- HTTP 请求和新 WebSocket ticket 使用时验证会话；已有 WebSocket 每条客户端命令前验证，并每 5 秒检查是否需要断开。手机在前台每 15 秒及恢复前台时检查登录，即使未开启通知也生效。
- 新签发的网页令牌为 `client=web`，网页登录不参与手机互踢。升级前没有 `client` 字段的历史令牌无法可靠区分网页/手机：账号首次使用新移动登录后，此类令牌需要重新登录一次，不能静默升级绕过互踢。

并发登录、绑定移动、注销和投递领取通过短 PostgreSQL transaction advisory lock 串行化；不在锁内请求 APNs/极光。数据库保留 `mobile_sessions`、`mobile_presence`、`push_devices`、`push_messages`、`push_deliveries` 五张表。手机侧还用登录版本号拦截旧请求，防止旧刷新响应覆盖新登录的 cookie 或重放写请求。

## App 状态的准确边界

状态只针对该用户当前有效的手机登录，网页或桌面端在线与否不参与判断。App 生命周期和网络连接分开处理，服务端不读取 WebSocket 断连来判断是否退出。

| 状态 / 场景 | 服务端状态 | 系统通知 |
| --- | --- | --- |
| `resumed`：前台使用任意页面 | `foreground` | 禁止；当前对话可显示页内提示 |
| `inactive`：仍可见但失焦，如权限弹窗、通知栏、控制中心、分屏失焦 | `foreground_inactive` | 禁止 |
| `hidden` / `paused`：所有视图不可见，如回桌面、切其他 App、锁屏后 | `background` | 收到状态上报后稳定 3 秒才允许 |
| 前台心跳中断不超过 45 秒 | 保留前台状态 | 禁止，不把短暂断网当作退出 |
| 前台心跳中断超过 45 秒、不足 90 秒 | `reconnecting` | 暂缓，等待恢复或超时 |
| 启动时无视图或收到 `detached`，不足 90 秒 | `unknown` | 暂缓；`detached` 不等于确定退出 |
| 前台或未知状态连续 90 秒无有效上报 | `offline_inferred` | 允许尝试推送，不能证明进程已退出 |
| 主动退出账号 / 被新手机登录替换 / 关闭通知 | 会话或绑定已撤销 | 禁止；取消该绑定队列 |

前台（含 inactive）每 15 秒上报一次；生命周期改变立即上报。系统杀进程或强制停止不保证生命周期回调，断网也会造成心跳丢失，因此无法可靠区分“杀进程”和“前台长期断网”。离线只是一项保守推断。正常锁屏会进入后台生命周期，若上报丢失则走 90 秒超时；实际无网络时无法实时送达。[Flutter 生命周期定义](https://api.flutter.dev/flutter/dart-ui/AppLifecycleState.html)、[Android Activity 生命周期](https://developer.android.com/guide/components/activities/activity-lifecycle)。

上报使用安装内持久化递增 `sequence`，写入本机存储后才发请求；服务端只接受当前移动会话中更大的序号。旧后台请求不能覆盖更新的前台状态，重复心跳不能延长在线时限，旧设备即使提交更大的序号也会被 401 拒绝。状态接口返回服务器序号，供进程恢复时校准。

业务事件默认等候 3 秒再领取。前台上报会取消尚未发出的业务通知（含已领取但未发出的通知）；暂缓中的通知收到明确后台上报后会重新排期。Worker 在领取和请求推送供应商前都检查生命周期、当前绑定、访问权限、TTL 和业务条件：问题已回答、旧任务被新一轮取代、授权已恢复的通知不再发出。

客户端也拦截前台展示：iOS `willPresent` 返回空展示选项；Android 以原生活动是否可见、屏幕是否亮起、是否锁定判断，`onPause` 不等于 `onStop`，不依赖缓存页面或 Flutter 连接状态。已提交给供应商的通知不能保证撤回；原生拦截用于覆盖返回前台的发送竞争，不能把供应商接受等同于送达。[Apple 前台通知回调](https://developer.apple.com/documentation/usernotifications/unusernotificationcenterdelegate/usernotificationcenter%28_%3Awillpresent%3Awithcompletionhandler%3A%29)。

## HTTP API

以下接口需要有效的移动端 Bearer token；接收用户来自认证，客户端不能指定其他用户。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/push/status` | 当前绑定、系统通知启用状态、服务端可用通道和 `presence` 判断 |
| `PUT /api/push/presence` | 上报 `state` 和递增 `sequence`，响应是否应用及当前通知策略 |
| `POST /api/push/devices` | 幂等登记当前手机 token 和权限状态 |
| `DELETE /api/push/devices/{bindingId}` | 关闭匹配的当前绑定，重复/旧绑定请求无副作用 |
| `POST /api/push/test` | 仅超管兼容入口；给当前登录手机排队测试，延迟 10 秒，202，30 秒限一次 |
| `GET /api/push/messages/{id}` | 查询自己通知的投递状态，不返回 token 或通知正文 |

登记请求示例（Android；iOS 使用 `platform=ios`、`provider=apns`、真实十六进制 token 和签名环境）：

```json
{
  "platform": "android",
  "provider": "jpush",
  "token": "REPLACE_WITH_ACTUAL_REGISTRATION_ID",
  "bundleId": "com.bossip.bipmobile",
  "appVersion": "1.0.0+1",
  "locale": "zh-CN",
  "apnsEnvironment": "production",
  "notificationsEnabled": true
}
```

`registered` 表示当前登录有登记记录；`notificationsEnabled` 表示该绑定允许投递；`providerConfigured` 表示服务端配置该通道；`deliveryEnabled` 要求后两者同时为真。`deliveryEnabled` 表示配置就绪，当前是否允许投递还要看 `presence.pushAllowed`。注册成功不能证明设备已收到通知。

测试返回 `{ "id": "…", "status": "pending" }`。状态包括 `pending`、`sending`、`accepted`、`failed`、`cancelled`、`unbound`。`accepted` 只代表供应商接受请求，不代表真机到达；超管测试页按这个含义显示。

主要错误码：`AUTH_MOBILE_SESSION_REPLACED` / `AUTH_MOBILE_LOGIN_REQUIRED`（401，必须重新登录）；`PUSH_DEVICE_NOT_READY` / `PUSH_NOT_CONFIGURED`（409）；`PUSH_TEST_RATE_LIMITED`（429）。设备字段由 Pydantic 校验，不接受平台/provider 错配、非法 token 或额外用户字段。

## 超管通知测试

Web 与 iOS/Android 入口均为「超管系统 → 通知测试」。普通用户保留通知开关与正常业务通知，不显示测试按钮；旧版 App 的测试接口也会拒绝普通用户。服务端每次操作读取数据库中的账号状态和平台 `admin` 角色，工作空间 owner 不具备此权限。权限撤销会取消未发送的测试。

- `GET /api/admin/push?locale=zh-CN`：当前超管绑定手机、通知开关、App 状态、通道配置、10 种模板预览和最近 20 次测试。Web 与手机均可读取；有绑定时，预览使用手机语言。
- `POST /api/admin/push/test`：只接受 `template`、当前 `bindingId`、UUID `requestId`；只发给请求者自己的当前手机。无自定义收件人或文案，额外字段拒绝。重复 requestId 幂等；换机后旧绑定拒绝。延迟 10 秒，30 秒限一次，5 分钟过期。
- `POST /api/admin/push/messages/{id}/receipt`：仅有效超管移动会话，可回传 `received` / `opened`。必须是本人实际已投递测试且绑定仍有效；Web、旧手机和未发送的通知不能回执。打开回执不会被晚到的接收回执降级。

测试遵守与业务通知相同的前台抑制规则。发送后 10 秒内切后台或锁屏；仍在前台则取消并显示原因。`accepted` 只证明供应商受理，`received` 表示 App 回调确认收到，`opened` 表示点击通知；iOS 后台通知可能仅在点击后回传。点击测试通知进入超管测试页。原生端另有本机通知预览，它不经过服务端或供应商，不生成远程送达回执。

复用现有 `push_messages.payload` 保存测试回执，无新增数据库迁移（head 仍为 `e4f6a8b0c2d4`）。接口只返回通道配置布尔状态，不返回 token、私钥或供应商凭据。

## 业务触发与模板

中英文模板集中在 `backend/notifications/templates.py`，锁屏只显示任务名及固定文案，不复制 LLM 原文、问题内容、工具输出、底层错误或凭据。收件人是本轮任务发起者、定时任务创建者或投稿发起者；不向工作空间全员广播。

| 事件 | 自动触发点与边界 |
| --- | --- |
| `task_completed` | 主会话的一轮执行正常结束且产生文本或结构化结果，在执行结算事务中写入；同一 run 只记一次 |
| `task_failed` | 主会话最终错误、重试耗尽、执行上限或不可恢复的执行中断；等待重试期间不发 |
| `input_required` | 持久化 question 检查点确实等待回答；回复、拒绝或替换后取消未发通知 |
| `approval_required` | plan / memory 确认检查点，或权限请求确实阻塞执行；不根据普通问句猜测 |
| `cron_completed` | 调度器确认成功且有非空、非 `NO_REPLY` 结果；临时 cron 会话不再重复发任务完成 |
| `cron_failed` | 一次性任务不再重试，或周期任务被调度器停用 / 已无后续执行；退避期间不发 |
| `platform_auth_expired` | 授权失效正在影响发起者的有效任务或关联的待发布任务；后台维护发现一般过期不发 |
| `publish_done` | 抖音 `create_video` 回调确认发布；重复回调不重复生成站内或系统通知 |
| `publish_failed` | 固定模板和事务入口已提供，仅允许可信平台终态接入；现有抖音 H5 回调没有失败事件，暂未虚构自动触发 |

任务名中文最多 16 字符、英文最多 32 字符（含省略号），控制字符与换行清理；正文为一句固定文案。授权通知显示抖音、小红书等平台名称，不直接展示内部标识。测试业务模板加「测试」前缀，避免被误认作真实业务结果。

创作者中心目前的 `ok` / `published` 记录还包含“进入作品列表、审核中”的回读结果，`failed` 也包含传输结果不明，均不作为平台最终结果发通知；登录失效影响发布时会触发重新授权提醒。扫码链接创建、提交、审核中、定时发布排队、链接过期、dry-run 都不会推送“发布成功/失败”。

定时任务的 `delivery.notifications_enabled`（默认 `true`）控制此类移动推送，可通过现有创建/编辑任务 API 设置；`delivery.mode=none` 仅关闭外部 webhook / channel，含义保持不变。移动端账号通知总开关仍是所有通知的总闸。

不通知：流式文本、普通进度、内部子任务、cron 临时会话、自动重试过程、普通 idle 状态、用户主动停止、静默定时结果。通知入队与业务结果在同一事务中提交，权限等待则在发布交互事件前入队并在回复/取消时清理。

## 事务内发送入口

```python
from notifications.store import enqueue_notification

# 与业务结果使用同一个短数据库事务。
async with get_db_session() as db:
    # 持久化本次业务结果……
    await enqueue_notification(
        db,
        user_id=recipient_id,
        event_key=f"task:{run_id}:completed",
        kind="task_completed",
        title="任务完成",
        body="你的结果已准备好。",
        workspace_id=workspace_id,
        session_id=session_id,
        ttl_seconds=3600,
    )
```

支持 `system_test`、`task_completed`、`task_failed`、`approval_required`、`input_required`、`cron_completed`、`cron_failed`、`platform_auth_expired`、`publish_done`、`publish_failed`。`event_key` 是同一用户的业务幂等键；事务回滚时通知也回滚。标题最多 120 字符、正文最多 500 字符，payload 超过 provider 字节限制会失败。正常业务使用集中模板及业务校验条件；通用入口保留给明确的事务内生产者。默认业务 TTL 为 1 小时，测试为 5 分钟。

消息包含 `schemaVersion=1`、`source=openbox`、`eventId`、`recipientId`、`bindingId`、`workspaceId`、`sessionId`、可选 `actionId`。入队和领取时都检查账号、有效工作空间成员及会话。只给入队时的有效绑定生成投递，不把历史通知补发给下一部登录的手机。

Worker 领取并二次校验后释放数据库事务，再发网络请求；60 秒租约、最多 8 次尝试、指数退避、最长 24 小时 TTL。进程退出后可回收过期租约，旧尝试结果不能覆盖新尝试或新绑定。网络/429/5xx 可重试，明确无效的 token 会禁用该绑定；凭据或通用参数错误不会直接删除用户设备。供应商的接受响应丢失后重试仍可能产生重复投递，因此不能承诺恰好送达一次。

## 双端行为

设置 → 账号 → 系统通知，提供开关、状态、系统设置、本机展示测试和服务器推送测试。两种测试均延迟 10 秒，提示用户切后台或锁屏；仍处于前台时抑制。本地测试不验证远程通道，Android 本地测试要求进程仍在。只有用户主动开启时才请求系统授权；Android 沿用旧版极光 SDK 的同意流程。启动、登录、回前台和 token 更新时同步绑定。拒绝通知不影响手机登录互踢。

iOS 沿用 Bundle ID `com.bossip.bipmobile`、Team `5AN3L8LZL9`，新增 APNs entitlement 和独立桥接。APNs 的 sandbox/production 从签名 provisioning profile 获取，再按构建类型兜底。保留现有支付宝、文件导出和 Scene 登录回调。

Android 沿用极光 AppKey `20c609b5064f10d52d8351d8`，使用 JPush 6.2.1 / JCore 5.5.1、通知 channel、小图标与后台 service。华为、荣耀、小米、OPPO、vivo、魅族及可选 FCM 官方适配器按配置启用，包含依赖、签名、混淆和厂商 Intent 点击解析。保留新版独立 `AuthCallbackActivity`。系统权限和 channel 关闭状态都会反映到绑定。

所有前台页面禁止系统通知；当前会话可显示页内提示，后台由 APNs/极光展示。点击使用白名单路由，等待登录和工作空间加载，校验会话权限后切换作用域。冷启动重复点击事件去重；失效目标给出提示，不执行通知中任意 URL 或批准操作。

## 配置与上线

先运行 `uv sync`、`uv run alembic upgrade head`，再启动新后端。现有 backend Dockerfile 启动时也会先跑迁移。合并后的 head 为 `e4f6a8b0c2d4`，汇合通知生命周期 `c3d5e7f9a1b2` 与定时模板 `d2f4a6c8e0b2`；从任一已有分支升级都会补齐另一分支，保留已执行迁移。

已于 2026-09-10 从旧 BossIP 的 `bossip-webfront-api` 部署配置和只读挂载取得原 APNs 私钥及极光 Master Secret，写入私有配置，合并时迁移到主工作目录 `/Users/wang/workspace/OpenBox`。实际部署使用的 APNs Key ID 是 `8BUB654RH8`，修正旧文档中的 `9W29AV52YC`。本地私钥格式、P-256 签名和双通道配置加载已验证；未发送真实推送。

本地 `.p8` 放在 `credentials/mobile-push/`，服务端变量写入 `backend/.env`；文件权限均为 `0600`，私钥目录为 `0700`，均排除 Git 和镜像构建。已有的其他环境变量保持不变。容器部署时仍需把私钥只读挂载到容器内，并相应调整 `BOSSIP_APNS_KEY_PATH`。

```dotenv
BOSSIP_APNS_KEY_PATH=/run/secrets/bossip-apns.p8
BOSSIP_APNS_KEY_ID=8BUB654RH8
BOSSIP_APNS_TEAM_ID=5AN3L8LZL9
BOSSIP_APNS_TOPIC=com.bossip.bipmobile
BOSSIP_JPUSH_APP_KEY=20c609b5064f10d52d8351d8
BOSSIP_JPUSH_MASTER_SECRET=<existing-server-secret>
```

APNs `.p8` 需要挂载到后端容器内的对应路径，只读且不进入镜像或 Git。整组不配置则禁用该 provider，绑定接口仍可使用，设置页提示服务端未配置；只填一部分会使启动失败。APNs 用 HTTP/2 token auth，极光用 HTTPS REST v3 Basic auth。后端需要访问 APNs 两个环境及 `api.jpush.cn`。

所有新后端实例应一起升级：混跑旧后端会有实例无法执行手机会话校验。独占登录约束作用于新版 OpenBox；旧 bossip 服务和数据库仍独立，需要上线时退役升级设备在旧服务端的通知绑定，避免两个后端都向同一个应用推送。

iOS 正式包仍需用旧团队有效的推送签名配置打包并验证 TestFlight production。Android 厂商 SDK 与配置入口已完成，由用户后续填写厂商参数、正式签名并启用极光后台通道；填写后自动编入对应适配器，不需要再改业务代码。`key.properties` 自动替换原有的开发签名。可选 `BOSSIP_JPUSH_VENDOR_OPTIONS` 配置已获批的分类及渠道 ID，发送策略固定为连接可用时走极光、离线后走厂商。详细文件路径、字段与验证方法见 [Android 厂商推送配置](ANDROID_PUSH_VENDORS.md)。

系统设置中的“强制停止”是单独的受限状态，不能承诺绕过。[Android stopped 状态](https://developer.android.com/about/versions/15/behavior-changes-all#stopped-state)。服务器取消队列不能撤回已经提交给 APNs/极光的通知；离线退出也无法立即让服务端得知退出。厂商系统直接展示的通知不一定经过应用回调，因此发送后的竞争也不能保证由客户端拦截。

## 验证

合并 `main`（上游 `97eab46`）后再次验证：后端 335 项、Flutter 218 项通过，analyze、locale、Dart 800 行门禁与 diff 检查通过。PostgreSQL 从空库、定时模板 head、通知生命周期 head 三种起点均升级至 `e4f6a8b0c2d4`，多进程互踢、绑定、投递及状态乱序测试通过；Android debug 与 iOS simulator 重新构建成功。私钥和六个推送变量已迁移到主工作目录，均保持私有且排除 Git。

2026-09-10 本地验证：后端相关回归 259 项通过；加入厂商参数后 provider 专项 32 项通过（含原先 17 项，不重复累加）。Flutter 全量 190 项通过，`flutter analyze` 无问题，locale 逐字节校验通过。独立 PostgreSQL 全量迁移至 `c3d5e7f9a1b2`，多进程竞争测试通过，覆盖 5 个并发登录、3 个投递 worker 和乱序生命周期上报。Android 普通 debug APK、七厂商全启用 debug / release（含混淆）、iOS simulator 构建成功；Android 点击解析 3 项 JVM 测试通过。厂商构建验证只使用临时假参数，已清理测试 APK 并重新生成普通开发包；未发送真实通知。相关测试文件：

- `backend/tests/integration/test_mobile_push_api.py`
- `backend/tests/integration/test_mobile_presence.py`
- `backend/tests/integration/test_mobile_notification_events.py`
- `backend/tests/integration/test_mobile_push_postgres.py`（显式提供已迁移、可丢弃的 `PUSH_TEST_DATABASE_URL`）
- `backend/tests/unit/test_push_providers.py`
- `mobile/test/shared/api/mobile_session_test.dart`
- `mobile/test/shared/notifications/push_controller_test.dart`
- `mobile/test/shared/notifications/push_presence_test.dart`
- `mobile/test/app/notification_host_test.dart`
- `mobile/android/app/src/test/kotlin/com/bossip/bipmobile/PushIntentPayloadTest.kt`
- `mobile/scripts/check_push_vendors.py`（临时假配置验证七个厂商组件；`--release` 验证正式构建）

通知分支初次验证时，基线 `question_dock_test.dart` 的 1083 行超过 800 行门禁；随后主分支已拆分该测试，合并保留其完整用例。本轮新增和修改的 Dart 文件均在限制内。locale 检查发现并同步了原先缺失的中英文 `common.pageLoad` 文案，与 Web 源文件保持一致。

上线前仍需用两部真机验证真实推送到达与点击：iOS 开发包/TestFlight、Android 前台/后台/锁屏/冷启动、双向换机登录、关闭通知和退出登录。本地测试及供应商接受状态不替代这项验收。
