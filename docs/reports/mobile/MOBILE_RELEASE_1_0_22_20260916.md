# iOS 1.0.22 (33)：Ask 卡片滚动修复

> 文档类型：实施 / 验收记录。版本、测试数量和部署状态只对应文内记录时点。

## 问题与修复

1.0.21 (32) 已包含把 Ask 放回聊天消息列表的修改，但卡片正文仍被限制在 300 逻辑像素的独立滚动区内，视频审核详情还有第二层高度限制和滚动区。手指从长问题、选项或视频详情开始滑动时，内层接收手势，即使滚到边缘也不能带动外层聊天记录；iOS 上会表现为弹回、聊天不动，自填框还可能被裁切。Android 共用的 Flutter 实现同样存在这个遗漏。

- 移除问题正文和视频详情的嵌套纵向滚动区，所有内容随 `ChatFlow` 一起滚动。
- 保留按请求和页码隔离的组件身份、草稿、分页、确认和跳过逻辑。
- 长内容占用消息列表中的自然高度；滑动可以到达自填框、操作按钮和历史记录。
- 本次不改变服务端接口，不需要数据库迁移。

## 验证

- 修改前，iOS / Android 的长问题、视频详情四个回归用例均失败：从正文向下拖动后，聊天列表偏移保持不变。
- 修复后新增六个 widget 用例覆盖 iOS / Android：长问题双向滑动、视频详情双向滑动、截图中的四个中文长选项和自填框。自填用例还覆盖深色模式、1.2 倍字号、键盘占用 300 像素后的滑动、按钮可达及草稿保留。
- `flutter test --no-pub --reporter expanded`：**387 passed**，包含新增六个用例和既有分页、草稿、提交异常回归。
- `flutter analyze --no-pub`：无问题。文件行数检查、本地化一致性检查、`git diff --check` 通过。
- iPhone 17 Pro / iOS 26.5 模拟器：**三个场景全部通过**。使用真实 `ChatScreen` / `QuestionDock`，HTTP / WebSocket 为隔离 fixture，无真实模型或生产写入。验证及截图由以下命令生成。

```sh
cd mobile
flutter drive --no-pub \
  --driver test_driver/question_scroll_driver.dart \
  --target integration_test/question_scroll_test.dart \
  --dart-define=ASK_SCROLL_TEST_REVISION=20260916-keyboard \
  -d 0DF6D793-2B35-4AD2-93BF-4617916FEB46
```

截图位于 `mobile/build/question-scroll-screenshots/`。模拟器使用原生渲染和注入的触摸事件，键盘占位断言在 widget 测试中完成；这些结果不代表安装最终 IPA 后的实体 iPhone 验收。

## 发布

- 版本升级为 **1.0.22+33**，沿用 iOS 15 最低部署目标。
- App Store Connect 发布前回读：最新有效版本为 1.0.21 (32)，33 尚未占用。
- 归档源码：`429fb698aadcafd8b3e68195bf69bdaa2d535cd6`，PR [#48](https://github.com/arkstudio-ai/OpenBox/pull/48)。
- Release 归档及 App Store 分发导出成功，Xcode 27 / iOS 27 SDK。
- IPA：`mobile/build/releases/BossIP-1.0.22-33/ipa/BossIP.ipa`，27,527,251 字节；SHA-256 `dd1aadc97366c6c4340bbdf4e65c79c0daca4157a9ef7e1ac3c5b98a682e5e57`。
- 包内核实 `com.bossip.bipmobile` / `1.0.22 (33)` / 最低 iOS 15，生产地址 `https://ai.bossipai.com.cn`，没有测试入口或 fixture；`codesign --verify --deep --strict` 通过。
- 最终 IPA 签名和描述文件为生产推送，`get-task-allow=false`，不限定设备名单；沿用 `ITSAppUsesNonExemptEncryption=false`。
- Apple 上传前校验通过；于 **2026-09-16 10:54（上海时区）** 上传成功，回执零错误。
- Delivery UUID：`733f8b60-7154-4cd7-af4b-3c4611c701d8`，App Store Connect App ID：`6794282961`。
- Apple API 回读：`processingState=VALID`，`usesNonExemptEncryption=false`，内部状态 `IN_BETA_TESTING`。
- 从“运营测试组”的构建列表回读确认已包含本次构建，沿用该组的自动分发设置，没有更改群组或测试员。
- 简体中文测试说明已更新并回读核对，覆盖长选项、聊天双向滚动、自填、视频确认和分页。
- [App Store Connect / TestFlight](https://appstoreconnect.apple.com/apps/6794282961/testflight/ios)：**1.0.22 (33) 已可供现有内部测试组安装**。尚未做最终 IPA 的实体 iPhone 验收。
