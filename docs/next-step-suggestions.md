# 聊天输入框上方的下一步建议

正常回复完成后，输入框上方可显示 0–3 个 AI 生成的建议。没有合适建议时不显示，不用固定文案凑数。

## 配置

在 `backend/openbox.json`（或实际部署加载的 `openbox.json` / `openbox.jsonc`）中增加可选顶层字段：

```json
{
  "suggestion_model": "openai/your-model-id"
}
```

模型 ID 沿用现有 `provider/model` 规则和 provider 凭据。省略、空字符串或纯空白时，使用刚完成这一轮聊天实际使用的模型，即用户在聊天框选择并发送的模型；不使用 `mcp_filter_model`，也不优先于会话选择套用全局 `model`。修改部署配置后按现有方式重启后端。

建议只影响文案生成模型。点击按钮后的聊天仍使用点击时聊天框当前选择的模型、思考强度、视频设置和执行模式。

## 交互

- 完整建议：点击后作为正常用户消息发送，保留现有权限确认流程。
- 需要补充或检查的建议：带编辑图标，点击填入输入框并聚焦，不自动发送。
- 输入框有草稿或附件、聊天运行/失败/中止、等待提问或权限确认、只读会话、向上翻阅历史时隐藏。
- 双击不会重复发送；发送失败后将完整建议恢复为可编辑草稿，不覆盖用户刚输入的新文字。
- 一行最多三个，窄屏可横向滚动，支持键盘聚焦、中文/英文、亮色/暗色主题。
- 建议生成期间显示三个胶囊形流光占位块，与最终按钮保持同一行高度；生成完成直接替换，不影响输入和发送。开启系统“减少动态效果”时显示静态占位。Web 与 Flutter iOS/Android 同步支持。

## Flutter 原生移动端

Android / iOS 的 `mobile/` 已接入同一份 `suggestions` 消息 part，不是 WebView 嵌入，也不在手机端另外请求模型。`openbox.json` 的独立模型配置及不配置时跟随当前聊天模型的规则由同一后端统一处理。

- 输入卡片上方独立一行，最多三个按钮，横向滑动；触摸高度至少 44 logical pixels，左右与输入文字对齐。
- 完整建议走原来的 `prompt_async` 发送入口，使用点击时的模型、思考强度、视频设置与 agent；编辑建议仅填草稿，聚焦并将光标放到结尾。
- 草稿、附件、上传中、忙碌、等待输入/权限、失败、只读和翻阅历史时隐藏；返回底部恢复。键盘造成视口变化时继续贴底，切换会话不会恢复旧请求的失败草稿。
- 支持延迟 `part.created` / `part.updated` 和重新打开后的快照恢复；移动端完整读取消息分页，长对话不会停在前 200 条。
- 中英文文案逐字节同步 Web，颜色使用现有主题 tokens；不改变移动端原有的模型/思考强度选择器布局。

## 生成与持久化

后端在当前执行正常结束并释放运行锁后启动辅助调用，不阻塞正文、不改变聊天状态。复用现有模型适配与计费通道（`kind=suggestions`），只接受 `StructuredOutput` 结果，不执行工具。调用超时为 45 秒；失败静默隐藏，不影响已完成的回复。

默认从最近五个真实用户请求中提取最多三个已完成的“请求＋最终回复”对；检测到“之前/第二个方案”等回指时，最多保留五对。携带初始目标提示、上一份目标/约束摘要、每轮产物和最新任务状态；工具步骤中生成的文件也会计入。跳过合成提醒、压缩消息、推理、进度文本和工具日志，不受聊天 API 前 200 条消息的分页限制。正文 JSON 限制为 10,000 UTF-8 字节，给提示词/结果 schema 留出空间，整体约 4K tokens（估算，并非特定模型的精确 tokenizer 计数）。

辅助调用前先将 `status=pending`、空 `items` 和 `expires_at` 保存为对应最终回复上的 `suggestions` part，再发送 `part.created`。结束时更新同一 part ID，通过 `part.updated` 发送 `completed`（含空结果）或 `unavailable`（失败、取消或代次过期），清空截止时间。刷新通过消息快照恢复；客户端不会用晚到的 pending 快照覆盖终态。缺少 status 的旧数据按 completed 兼容。

截止时间为调用开始后 60 秒（模型超时 45 秒加 15 秒余量），Web 与原生 App 最多等待 60 秒；进程退出或网络中断后也会收起过期占位。失败/pending 摘要不覆盖之前有效的上下文摘要。前端会逐页取齐消息再使用快照，避免超过 200 条时误把旧回复视为最新回复。空结果也会缓存。无需新增数据库字段或迁移。建议和摘要不会进入主聊天模型的历史指令；用户主动点击发送的 prompt 才会成为普通用户消息。

生成前和写入时均检查会话归属、当前执行代次、空闲状态、最新回复 ID 和是否已有结果；写入检查与新消息/停止共用事务锁，防止旧生成结果落到新一轮。前端也只读取最后一条成功回复上的建议。Cron、子会话、失败/中止/等待输入及结构化输出专用回复不触发生成。不会在刷新时补跑或为旧历史批量生成建议。

## 验证

```sh
cd backend
python -m pytest tests/unit/test_suggestions.py -q
```

```sh
cd frontend-v2
npm run check
npm run build
npm exec playwright -- test --config playwright.suggestions.config.ts
```

测试使用可控模型输出和拦截的 HTTP，不调用真实模型、不消耗额度。浏览器回归覆盖位置、点击发送、模型选择、草稿、失败恢复、隐藏条件、滚动、刷新、延迟事件和两种语言/主题/尺寸；截图在 Playwright 的 `test-results` 中。

原生验证：

```sh
cd mobile
flutter analyze --no-pub
./scripts/check_file_size.sh
./scripts/check_locales.sh
flutter test --no-pub
flutter build apk --debug --no-pub --dart-define=API_BASE=http://10.0.2.2:8080
flutter build ios --simulator --debug --no-pub --dart-define=API_BASE=http://127.0.0.1:8080
flutter drive --no-pub --driver=test_driver/suggestions_driver.dart \
  --target=integration_test/suggestions_test.dart -d <iOS-simulator-id> \
  --dart-define=API_BASE=http://127.0.0.1:8080
```

新增 28 项 Flutter 单元/组件测试：包括 320/390/430 logical pixels × 中英文 × 明暗主题 × 150% 字号、键盘视口变化、读屏标签、连续点击、上传/附件、失败恢复、切换会话、真实发送入口参数、滚动和 402 条消息分页。另有 iPhone 原生集成测试，使用隔离的 REST/WS fixtures 检查实际渲染、横向滚动和输入聚焦；不登录或访问生产。截图输出在 `mobile/build/suggestion-screenshots/`。真机触摸与系统输入法组合尚未实测。

2026-09-10 验证结果：全量 201 项 Flutter 测试、analyze、locale 和 800 行门禁通过；Android debug APK、iOS arm64 simulator 构建及 iPhone 原生集成测试通过。本功能在 `codex/next-step-suggestions` 开发，合并状态见 Git 记录；生产部署另行进行。

2026-09-10 流光占位增量（`eda8b75`）：显式持久化生成状态，补齐断线恢复、终态防回退、截止时间、无结果/失败收起、不可点击、草稿继续发送和减少动态效果。Flutter 全量 308 项测试、analyze 和 release bundle 编译通过；包含 iOS/Android 320px、150% 字号、明暗主题和键盘布局。Web 511 项单测、14 项浏览器测试通过；后端建议 45 项及执行相关 30 项回归通过。本次未发布原生安装包，已有 App 需重新打包安装才能显示流光动画。
