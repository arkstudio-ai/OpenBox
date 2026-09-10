# 风控/验证码阻塞时提醒用户接管 + 一键跳转云桌面

日期：2026-09-09 · 状态：代码已落地（未提交、未发布），PC + 移动端卡片；本地浏览器验证已过

## 要解决的事

agent 用 dev-browser 驱动云桌面（ECD）里的 Chrome 时，遇到滑块/点选/算术验证码、短信验证码、
"安全验证 / 环境异常 / 访问过于频繁" 这类风控页就走不下去。之前的表现是反复重跑脚本或用
`computer` 乱点，最后以失败收场；用户在聊天里看不到"该你上了"的信号，也没有直达桌面的入口。

产品规则不变：**agent 永远不代过验证码**（`DETAILED_PLAN_M1_M2.md` 的既定原则）。要做的是让它
第一时间停下来、把人叫过来、把人送到桌面前，然后从原页面继续。

## 设计

不加 WS 事件、不加 REST 接口、不改数据库，全部复用现有积木：

```
模型识别风控 ─► desktop_takeover 工具 ─► question.ask(detail.kind="desktop_takeover")
                                            │  WS question.asked（现有）
                                            ▼
                 QuestionDock ── DesktopTakeoverDetail 卡片 ──「打开云桌面接管」超链接
                                            │  emitAppEvent("workbench.open",{kind:"desktop",control:true})
                                            ▼
                 usePanelStore.openKind("desktop",{desktopControl:true}) ─► DesktopTab 自动勾选「允许操控」
                                            │  用户处理完 →「我已完成，继续」
                                            ▼
                 工具返回 → 模型 client.page(同名) 重新 snapshot 后继续
```

### 后端

- `backend/tool/desktop_takeover.py`：新工具。参数 `reason`（captcha_slider / captcha_click /
  captcha_math / sms_code / login_required / risk_control / other）、`url`、`page`（dev-browser 页面名）、
  `instructions`。执行时用 `sandbox.browser.browser_status` 读 relay 模式：`local` = 页面在云桌面
  Chrome 上，卡片带桌面链接；`extension` = 页面在用户自己的 Chrome 上，卡片只提示"在你自己的浏览器里完成"。
  然后走 `question.ask()` 挂起等答，`Question.detail` 带 `kind/reason/url/host/page/instructions/browser`。
  三个选项：「我已完成，继续」「跳过这一步」「放弃任务」，自由输入也算"完成"（原话回传给模型）。
  用户点「跳过」（reject）由 `agent/hooks.py` 统一转成 `Rejected` 结果。
- 注册与暴露：`tool/registry.py`；`agent/agent.py` build agent 工具表 + 权限放行（同 `question`）；
  `agent/tool_exposure.py` 的 `browser` intent pack。当轮没命中 pack 时模型可用 `capability_search` 揭示。
- `agent/processor.py` 的 `PERSISTED_TOOL_METADATA_KEYS` 加 `takeover`，答完后的记录行能显示原因和站点。
- 提示词：`agent/prompts/system.py` 的 `_INTERNET_AND_BROWSER` 新增「Captcha / risk control: hand off
  to the user」；`tool/skill_tool.py` 的 `<browser_mode>` 块在 local/extension 两个分支各加一句。

### 桌面运行时（随 RUNTIME_VERSION 下发）

- `container/dev-browser/SKILL.md` 新增「When a site challenges you (captcha / risk control): hand off to
  the user」一节：症状清单、禁止拖滑块/识图/换 UA/清 cookie/循环刷新、最多刷新一次即调用工具、
  返回后回同一页面重新 snapshot。
- `container/dev-browser/src/client.ts` 新增 `detectChallenge(page)`：按极验 `.geetest_*`、阿里云
  `#nc_1_wrapper`、腾讯 `#tcaptcha_iframe`、Cloudflare `#challenge-form`/turnstile、hCaptcha、reCAPTCHA
  的可见元素和中英文文案启发式检测；`waitForPageLoad()` 返回值多一个 `challenge` 字段。只报不拦。
- `backend/sandbox/browser_runtime_repair.py` 的 `RUNTIME_VERSION` 已 bump 到 `20260909.1`，
  发布后需按 `docs/DEPLOY.md` 的浏览器运行时修复流程下发到现网桌面，否则旧桌面上的 SKILL.md 不会更新。

### 前端（frontend-v2）

- `shared/events/bus.ts`：`workbench.open` 的 `kind` 加 `desktop`，新增 `control?: boolean`。
- `shared/router/paths.ts`：`paths.desktopTakeover(sessionId)` = `/app/s/<id>?panel=desktop&control=1`，
  以及 `readPanelRequest()`。这是一个真实 URL，刷新可用，将来给手机/推送直接复用。
- `features/workbench/stores/panel.ts`：`OpenExtra.desktopControl` + `desktopControlRequest` 计数器
  （计数而不是布尔，用户手动关掉操控后再次请求仍能生效）。
- `features/workbench/hooks/usePanelEvents.ts`：`kind === "desktop" && control` → `openKind("desktop", {desktopControl:true})`。
- `features/workbench/components/DesktopTab.tsx`：新 hook `useControlToggles` 接管原来的 toggles 镜像，
  并监听 `desktopControlRequest`：已连接就立刻 `setSessionControl(true)` + 聚焦 iframe，未连接则勾上
  复选框等 `onConnected` 应用。每个请求只处理一次，断线重连不会把用户关掉的操控再打开。
- `routes/workspace/ChatRoute.tsx`：进入聊天时读 `?panel=desktop&control=1`，直接调 store 打开面板并清掉参数。
- `features/chat/components/DesktopTakeoverDetail.tsx`（新）：挂在 `QuestionDock` 的每个问题下（与
  `VideoApprovalDetail` 同一模式）。显示原因徽章、站点；`local` 显示「打开云桌面接管」超链接（真实
  href + 点击时应用内打开），`extension` 只显示文案。问题正文已包含说明，卡片不重复。
- `features/chat/components/tool/QuestionAnswered.tsx`：有 `metadata.takeover` 时多一行「用户接管：滑块验证 · host」。
- `features/chat/components/Markdown.tsx`：`/app/` 开头的链接改为应用内跳转（`AppLink`），其余仍新开标签。
- i18n：`locales/{zh-CN,en-US}/chat.json` 新增 `takeover.*`。

### 移动端（Flutter）

- `lib/features/chat/widgets/cards/desktop_takeover_detail.dart`（新）：与 web 同一张卡片，挂在
  `question_dock.dart` 每个问题下；`local` 时按钮 `appEventBus.emit('workbench.open',
  {'kind':'desktop','sessionId':…,'control':true})`，`extension` 只显示文案。
- `app/workspace_shell.dart` 把 `control` 透传给 `Paths.workbench(sessionId, tab:'desktop', control:true)`
  （= `/app/w/<id>?tab=desktop&control=1`）；`app/router.dart` → `WorkbenchScreen.initialControl`
  → `WorkbenchSurfacePage.control` → `DesktopTab.autoControl` → `ScopedDesktopViewer` 在
  `connected` 事件后调一次 `_toggleControl(true)`（只生效一次，重连不重开）。
- 语言包：`mobile/assets/locales/*/chat.json` 从 frontend-v2 逐字复制（`scripts/check_locales.sh`
  现有的 auth-center.json / workspace.json 差异是本次之前就有的漂移，未动）。
- 应用在后台时的推送另开计划。

## 验证

已通过：

- 后端 `uv run pytest tests/unit`：新增 `tests/unit/test_desktop_takeover_tool.py`（注册/权限/pack、
  local vs extension 的 detail、三种答复的指令、无 sandbox / relay 未起的判定）；全套 305 通过，
  1 个既有失败（`test_computer_batch` 的假 ctx 缺 `sandbox_error`，与本次无关）。
- 前端 `npm run check`：i18n 对齐、lint、vitest 226 通过（新增 `DesktopTakeoverDetail.test.tsx`、
  `panel.test.ts`）。`tsc -b` 唯一报错是 `qrcode` 依赖未安装（`npm ci` 可解），非本次文件。
- `container/dev-browser`：`npx tsx` 能加载 `client.ts`，`detectChallenge`/`waitForPageLoad` 导出正常。
- 移动端：`flutter analyze` 无问题；`test/features/chat/desktop_takeover_detail_test.dart`
  覆盖 detail 解析、路径 control 参数、两种卡片渲染与按钮事件。
- 本地浏览器验证（`docker-compose.dev.yml` 起 postgres/redis，`.claude/launch.json` 的 `backend`
  项启动后端，注册 devtest 账号，经 Redis `bus:events` 注入一条 `question.asked`）：
  接管卡片在会话末尾渲染，原因徽章/站点/超链接正确（href 为
  `/app/s/<id>?panel=desktop&control=1`）；点击后工作台切到云桌面 tab；直接访问该 URL 同样打开
  云桌面 tab 且地址栏参数被清掉。本地没有云桌面，所以"自动勾选操控"和"我已完成→模型继续"
  两步只有单测覆盖，留到发布后验证。

发布后待做（需要真实云桌面 + 模型）：

1. 让 agent 打开一个带极验 demo 的页面（如 `https://www.geetest.com/demo/slide-popup.html`）并"完成验证"。
   预期：不拖滑块，调用 `desktop_takeover`，聊天末尾出现接管卡片。
2. 点「打开云桌面接管」：工作台切到云桌面 tab，顶栏显示「已允许操控」；直接访问
   `/app/s/<id>?panel=desktop&control=1` 效果相同且地址栏参数被清掉。
3. 桌面里手动过验证，回来点「我已完成，继续」：模型重新 snapshot 并继续，对话里保留「用户接管」记录。
4. 浏览器模式切 `remote` 并连上扩展再跑一次：卡片无桌面链接，只提示在自己浏览器完成。
5. 现网桌面执行运行时修复，确认 `/opt/openbox/skills/dev-browser/SKILL.md` 含新章节。
