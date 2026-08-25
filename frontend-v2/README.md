# OpenBox Frontend v2

OpenBox 当前主推且持续开发的 Web UI，基于 [bossip 设计稿](design-reference/) 完成 v2 重构。原 `frontend/` 仅作为旧版迁移参考保留，新增功能与修复统一进入本目录。工程规范见 [docs/ENGINEERING_SPEC.md](docs/ENGINEERING_SPEC.md)。

## 主要能力

- 流式 Agent 对话，完整展示工具调用、思考过程、权限、问题、计划和 Todo 状态
- 集成 Diff 审阅、PTY 终端、浏览器、远程桌面和文件工作台
- 中英文国际化、8 套主题、深浅色模式及 4 档字号
- 基于 React 19、Vite 8、TypeScript 6、Tailwind CSS 4、React Router 8

## 开发

```bash
npm ci
npm run dev        # http://localhost:3000（代理 /api /ws → localhost:8080）
```

后端：仓库根目录 `make backend`（FastAPI, :8080）。

## 质量门禁

```bash
npm run check      # i18n parity + lint + tsc + vitest
npx playwright test  # E2E（需后端 + devtest 账号）
```

## 结构

```
src/
├── app/        # 装配：providers / router / layouts / bootstrap
├── routes/     # 路由薄壳
├── features/   # workspace / chat / workbench / auth / settings
├── shared/     # api / ws / ui / i18n / appearance / lib / types
├── locales/    # zh-CN · en-US（key 全量对齐，CI 校验）
└── styles/     # Tailwind 4 @theme token（8 主题 × 深浅色 × 4 字号）
```
