# Ask 卡片分页验收

日期：2026-09-09。范围：React Web（桌面 / 手机网页）与 Flutter 客户端。
仅使用本地测试账号、Docker 数据库及已授权的无影开发配置；未操作其他用户会话，未部署线上。

## 交互规则

- 一个 ask 的多个问题逐页展示，一次只渲染一题；显示 `1/3` 页码和左右“上一题 / 下一题”按钮。
- 单选后自动进入下一题；自填在非空时按 Enter / 键盘“下一项”进入下一题。
  输入过程中、空白答案和中文输入法候选确认不会提前跳题。
- 多选题不在选中第一项后跳走，选完点“下一题”。可以手动翻页检查或修改此前答案。
- 最后一题不越界，也不自动提交。所有题都回答后才可点一次“确认”，按原题序提交完整答案。
  “跳过全部”仍处理整个 ask，翻页不是跳过或批准问题。
- 同一账号、同一 ask 的页码和答案在刷新 / 组件重建后保留；没有页码缓存时从首个未答题开始。
  损坏或越界页码安全回退，账号之间不共享页码。导航只写本地缓存，不请求后端、不增加草稿 revision。
- 提交 / 跳过期间禁用分页、输入和选项；失败后仍保留答案与当前页，支持重试。
  正常关闭时清理草稿页码，失效 ask 不复活。

这里的分页作用于**同一个 ask 内的多个问题**；独立 ask 的持久化、替代和恢复状态仍沿用
[durable-ask.md](durable-ask.md)，没有改变服务端接口或等待资源占用。

## 测试结果

| 检查 | 结果与覆盖范围 |
| --- | --- |
| Web `npm run check` | 397 tests passed；i18n、TypeScript、lint 通过，保留 30 条既有 lint warnings |
| Web `npm run build` | 通过 |
| Chromium `playwright.questions.config.ts` | 14 passed；320 / 390 / 1280px、按钮可见可点、自动翻页、手动回改、刷新、完整提交、跳过和故障重试 |
| Flutter `flutter analyze --no-pub` | No issues found |
| Flutter `flutter test --no-pub --reporter expanded` | 120 passed；包含 320 / 390px 布局、键盘、列表重建、缓存、账号切换、1–4 题边界和提交异常 |
| `bash mobile/scripts/check_locales.sh` | Web / Flutter 中英文资源逐字节一致 |
| 后端两个 durable question 文件 | 78 passed；持久化、并发、输入校验和恢复故障回归，本次未改后端 |

分页新增覆盖：单选自动 / 多选手动 / 自填 Enter，中文 IME 不误跳，最后一题不自动提交，
未答题可浏览但不能部分提交，导航不触发自动保存，非法页码回退，页码与完整答案一起恢复。
旧异常用例继续覆盖网络失败、HTTP 409 / 410 / 422 / 500、草稿冲突、提交中重建、
新 WebSocket ask 先于旧 HTTP 读取返回。测试中实际发现并修复了 Flutter 在 320px 下
分页按钮溢出的问题，回归验证按钮完整可点。

数量含全套测试及子集，不能简单相加作为独立用例总数。Chromium 使用真实渲染与受控 HTTP
fixture；Flutter 为单元 / widget 测试，未声称实体 iOS / Android 真机验收。

## 本地真实链路

- 本地账号：`ask_local_test`，自身账号密码登录，不使用 Logto。
- 会话：`session_7YBXY5WN8GK9M5GCSCFBFFPYWR`。
- Ask：`01M21YPVZK4FW3B8MF3B7KK1D9`（三题：时长单选、效果多选、语气自填）。
- 单选 `30秒` 后到 2/3，多选 `字幕`、`配乐` 后仍在 2/3；返回第一题改为 `60秒`，
  自动回到第二题，原多选保留；第三题填写 `清楚自然`。
- 整页刷新后仍为 3/3，自填和前页多选均保留。点击“确认”后卡片消失，Agent 最终复述：
  `60秒 / 字幕、配乐 / 清楚自然`。
- SQL 验证 `answered / applied=true`，完整有序答案为
  `[["60秒"], ["字幕", "配乐"], ["清楚自然"]]`；会话 `idle`，pending 为空，
  `run_id=NULL`、`lease_until=NULL`、`resume_pending=false`。
- 脱敏原始状态证据保留在本地 ignored `test-results/ask-local/followup-pagination.json`，
  不含密码、token 或密钥。

既有全量后端 7 个非 Ask 失败及未执行的容量 / 多节点停机 / 真机测试边界，见
[durable-ask-state-matrix.md](durable-ask-state-matrix.md)。本记录不代表所有可能异常或生产发布验收。

## 复跑

```sh
# frontend-v2
npm run check
npm run build
npx playwright test --config playwright.questions.config.ts --reporter=line

# mobile
flutter analyze --no-pub
flutter test --no-pub --reporter expanded
bash scripts/check_locales.sh

# backend
uv run pytest tests/unit/test_durable_questions.py tests/unit/test_durable_question_failures.py -q
```
