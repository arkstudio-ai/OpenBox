# OpenBox Web

当前持续开发的 Web 工作台。新增功能与修复放在本目录；[`frontend/`](../frontend/README.md) 保留作旧版参考。
工程规则见[工程规范](docs/ENGINEERING_SPEC.md)，跨端接口见[后端 API](../docs/reference/API_INTERFACES.md)。

## 本地开发

从仓库根目录按[开发说明](../README.zh-CN.md#本地开发)准备环境，运行 `make backend`。
另开终端：

```bash
cd frontend-v2
npm ci
npm run dev
```

默认地址 `http://localhost:3000`，`/api` 和 `/ws` 代理到 `localhost:8080`。
端口和轨迹代理覆盖项见 [vite.config.ts](vite.config.ts)；客户端 API 基址由 `VITE_API_URL` 配置。
使用其他地址时同时检查代理、SSO 回调和 WebSocket。

## 结构与职责

| 目录 | 内容 |
|---|---|
| `src/app/`、`src/routes/` | 启动、路由、布局与跨功能装配 |
| `src/features/` | 对话、工作台、Agent / 团队、技能、资源、账号及后台等功能 |
| `src/shared/` | API、WS、通用组件、类型、外观与国际化 |
| `src/locales/`、`src/styles/` | 中英文文案与主题 token |
| `e2e/` | Playwright 浏览器验收 |

React、Vite、TypeScript 等依赖的范围以 [package.json](package.json) 为准，安装版本由锁文件确定。
原 v2 视觉稿属于历史设计资料，不是新 checkout 的运行依赖。

## 检查与构建

```bash
npm run check        # i18n、SEO、ESLint、TypeScript、Vitest
npm run format:check
npm run build
npm run preview
```

浏览器端到端测试使用 `npx playwright test`；先安装所需浏览器，准备后端和测试账号。
[Playwright 配置](playwright.config.ts)默认使用本机 3000 端口、一个 worker，认证准备逻辑在
[e2e/auth.setup.ts](e2e/auth.setup.ts)。它会操作测试环境，不是离线单元测试。

Agent / 团队呈现规则见[团队 API](../docs/reference/AGENT_TEAM_API_HANDOFF.md)，
能力与工具分类见[架构说明](../docs/architecture/AGENT_CAPABILITIES.md)。
