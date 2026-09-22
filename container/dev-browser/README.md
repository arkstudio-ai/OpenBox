# Dev Browser 运行时

本目录是浏览器控制的 relay 与客户端库，不是 Web 工作台。运行方法提供给 Agent 的
[SKILL.md](SKILL.md)使用；维护者从本页进入源码和部署关系。

| 文件 | 职责 |
|---|---|
| [src/relay.ts](src/relay.ts) | HTTP / WS relay 与浏览器连接 |
| [src/client.ts](src/client.ts) | 技能脚本使用的客户端 |
| [src/page-state.ts](src/page-state.ts)、[src/snapshot/](src/snapshot/) | 页面状态与结构快照 |
| [scripts/start-relay.ts](scripts/start-relay.ts) | `local`、`extension`、`auto` 模式启动入口 |
| [references/scraping.md](references/scraping.md) | 页面数据读取示例 |
| [tests/](tests/) | 页面状态与 relay 恢复回归 |

后端的[运行时装配](../../backend/sandbox/browser_runtime.py)负责把这些文件提供给执行环境。
已配置的无影环境由服务管理 relay，使用技能时不要重复启动一个冲突的进程。
独立开发时，在本目录安装 [package.json](package.json) 的依赖，先阅读启动脚本，再用 `npm run start-relay` 配合所需参数。
脚本按 `--mode`、`--host`、`--port` 等参数启动服务。
默认 relay 端口为 9222，本地 Chrome CDP 端口为 9333；模式与地址以环境实际配置为准。

`npm test` 声明使用 Vitest，但本包尚未单独声明该测试依赖。运行维护测试前应在测试环境提供 Vitest，
不能把只安装运行依赖等同于测试环境已就绪。配套浏览器扩展见[扩展说明](../../extension/README.md)。
