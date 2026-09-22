# 执行环境与浏览器运行时

这里提供 Action Server 和浏览器运行时源码。后端通过 `SandboxClient` 访问配置的执行环境；
生产通常由无影桌面承载，本目录 Docker 镜像也供 Docker Provider 使用。

| 入口 | 职责 |
|---|---|
| [action_server.py](action_server.py) | 沙箱文件、命令、PTY 与运行时接口 |
| [Dockerfile](Dockerfile) | Python、Node、Chromium、FFmpeg 和运行时依赖，默认监听 8000 |
| [dev-browser/](dev-browser/README.md) | 浏览器 relay、客户端库与供 Agent 读取的技能 |
| [repair_browser_runtime.py](repair_browser_runtime.py) | 浏览器运行时修复辅助 |
| [obx_diag.py](obx_diag.py) | 环境诊断辅助 |

在仓库根目录执行 `make sandbox-image` 构建 Docker 沙箱镜像。
`SESSION_API_KEY`、工作目录和持久化挂载由 Provider 管理；构建镜像本身不会部署或切换无影桌面。
无影安装、通道、凭据与恢复流程见[无影指南](../docs/operations/WUYING_SANDBOX.md)。

浏览器运行时内容也会随后端镜像打包用于恢复，修改时检查
[后端恢复装配](../backend/sandbox/browser_runtime.py)和相关测试，而不只检查 Docker 沙箱。
供 Agent 执行时读取的 [SKILL.md](dev-browser/SKILL.md)保留在原位置，避免破坏安装协议。
