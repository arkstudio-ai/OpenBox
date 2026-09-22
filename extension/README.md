# 浏览器扩展

WXT 扩展连接用户浏览器与 OpenBox 的 Dev Browser relay，供浏览器模式执行使用。
它与云桌面浏览器是不同的连接来源，后端根据配置和连接状态选择。

## 开发与打包

在 `extension/` 下执行：

```bash
npm install
npm run dev
npm run test:run
npm run build
npm run zip
```

Firefox 对应 `dev:firefox`、`build:firefox` 与 `zip:firefox`。
构建产物目录由 WXT 输出，加载开发扩展或分发压缩包时使用当次构建结果，不用仓库里的旧包判断源码状态。

## 代码与连接

- [background](entrypoints/background.ts) 组装连接与消息处理，弹窗在 `entrypoints/popup/`。
- `services/ConnectionManager.ts` 管理连接；`CDPRouter.ts`、`TabManager.ts` 和 `StateManager.ts` 分别处理调试路由、标签页和状态。
- [wxt.config.ts](wxt.config.ts) 定义 manifest，包含 debugger、tabGroups、storage、alarms、cookies 与站点访问权限。
- 配套 [Dev Browser relay](../container/dev-browser/README.md) 和[无影连接说明](../docs/operations/WUYING_SANDBOX.md)描述服务端环境。

连接地址与认证由当前环境提供，在扩展弹窗核对；成功连接、浏览器授权、工具调用可用是需要分别检查的状态。
