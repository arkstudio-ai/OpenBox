# OpenBox Mobile

OpenBox 的 Flutter 原生客户端，与 Web 共用后端 API、账号与工作空间。当前阶段优先维护接口接入，
移动 UI 设计与进一步优化单独安排；页面不会因 Web 有新功能就自动具备同等能力。

## 本地开发

使用满足 [pubspec.yaml](pubspec.yaml) 的 Flutter / Dart SDK。先按[根目录开发说明](../README.zh-CN.md#本地开发)
准备配置和依赖，并在仓库根目录启动后端：

```bash
make backend
```

在另一个终端启动 App：

```bash
cd mobile
flutter pub get
flutter run --dart-define=API_BASE=http://localhost:8080 --dart-define=WEB_BASE=http://localhost:3000
```

Android 模拟器访问宿主机请将两个地址的 `localhost` 改为 `10.0.2.2`；真机使用可达的开发机地址。
测试账号由当前测试环境提供，不在文档中保存账号密码。

## 环境与接口

[Env](lib/shared/config/env.dart) 默认指向 `https://ai.bossipai.com.cn`，本地测试必须显式传入覆盖项：

| 构建参数 | 用途 |
|---|---|
| `API_BASE` | REST 与派生的 WebSocket 服务地址 |
| `WEB_BASE` | Web 页面、回跳等入口地址 |
| `SSO_REDIRECT_URI` | Native SSO 回调；默认 `com.bossip.bipmobile://callback` |
| `SSO_POST_LOGOUT_REDIRECT_URI` | 退出回跳；默认与登录回调相同 |

修改 SSO scheme 时同步 Native Logto 应用、平台配置与构建参数。
套餐价格、积分、订阅与云桌面开通状态读取后端，不在客户端维护另一套事实。

## 开发与验证入口

- [接入与平台约定](docs/INTEGRATION.md)：SSO、Web 对应关系、分层、流式消息、权限回复、文件交付和原生平台差异。
- [团队 API](../docs/reference/AGENT_TEAM_API_HANDOFF.md)、[通知 API](../docs/reference/MOBILE_NOTIFICATIONS.md)、[消息中心](../docs/architecture/MESSAGE_CENTER.md)。
- [Web 对齐与验收记录](../docs/reports/mobile/MOBILE_WEB_PARITY.md)：历史完成范围和待办证据；新增工作按当前范围安排。
- [移动发版记录](../docs/reports/mobile/README.md)：版本号、构建环境和对应验证结果。

在 `mobile/` 下按改动范围验证：

```bash
./scripts/check_locales.sh
./scripts/check_file_size.sh
flutter analyze
flutter test
```

平台集成变动再增加 `flutter build apk --debug`、`flutter build ios --simulator --debug` 及相应真机验证。
本地化 JSON 与 Web 保持字节一致，先修改 Web 再同步，不分别维护翻译。
[vendored video_thumbnail](third_party/video_thumbnail/README.md) 的来源和修改范围单独保留。
