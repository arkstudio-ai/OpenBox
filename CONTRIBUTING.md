# 开发与文档维护

先阅读[项目说明](README.zh-CN.md)和[本地命令](docs/contributing/DEV_COMMANDS.md)。
本仓库按后端、Web、移动客户端、执行环境和浏览器扩展划分职责；模块内的 README 是对应开发入口。

## 改动落点

- 后端：[运行与测试](backend/README.md)、[能力架构](docs/architecture/AGENT_CAPABILITIES.md)、[新增能力](docs/contributing/ADDING_CAPABILITIES.md)。
- Web：[frontend-v2](frontend-v2/README.md)与[工程规范](frontend-v2/docs/ENGINEERING_SPEC.md)。
- 移动：[客户端说明](mobile/README.md)与[接口接入](mobile/docs/INTEGRATION.md)。
- 执行环境：[container](container/README.md)、[Dev Browser](container/dev-browser/README.md)、[扩展](extension/README.md)。
- 部署：[发布文件](deploy/README.md)与[Runbook](deploy/gw2/RUNBOOK.md)。

工具、技能、指令、资源、可复用 Agent、团队与外部连接是不同对象，不集中写进单一工具注册文件。
业务状态放所属服务，工具入口负责参数、权限、调度与结果；依赖异常需要可识别的返回值。
云桌面与浏览器的数据删除边界见 [backend/AGENTS.md](backend/AGENTS.md)。

## 验证与交付

按实际影响运行模块内检查：后端单元 / 契约测试，Web 的 `npm run check`，移动的静态检查和对应平台测试。
涉及数据库或供应商的测试使用明确的测试环境，不把外部调用混进纯目录重构回归。
说明改动前后的行为、验证范围和未覆盖项；协议变化同步接口文档，运维变化同步 Runbook。

文档改动遵循[文档维护规则](docs/contributing/DOCUMENTATION.md)，在仓库根目录执行：

```bash
python3 scripts/check_docs.py --write-index
python3 scripts/check_docs.py
```

检查不访问远程链接或实际服务，也不会执行文档中的示例命令。
