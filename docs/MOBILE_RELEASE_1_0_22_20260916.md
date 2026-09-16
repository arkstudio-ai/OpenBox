# iOS 1.0.22 (33)：Ask 卡片滚动修复

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
- iPhone 17 Pro / iOS 26.5 模拟器：使用真实 `ChatScreen` / `QuestionDock`，HTTP / WebSocket 为隔离 fixture，无真实模型或生产写入。三个场景的验证及截图由以下命令生成。

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
- 构建、上传和 Apple 处理结果将在完成后补录。本节的准备状态不代表已发布。
