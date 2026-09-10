# 建议生成流光占位发布（2026-09-10）

2026-09-10 20:37:29–20:38:18（北京时间）更新阿里云前后端，源码 `eda8b7502326e614ddf18d656fc4c9d2415a2a04`。输入框上方在建议生成时显示三个流光占位块，结果到达后替换为按钮，失败、空结果或过期时收起。Web 与 Flutter iOS/Android 同步实现，保留输入、发送及既有可见性规则。

## 发布与配置

- 本地 Docker 构建 `linux/amd64`：`openbox-backend:20260910-suggestion-loading-eda8b75`、`openbox-frontend-v2:20260910-suggestion-loading-eda8b75`；运行基础版本保持 Python 3.12.14 / nginx 1.31.3。
- 仅替换 compose override 中的两处 image。配置文件、环境变量、挂载、运行命令及推送密钥均保持原值；PostgreSQL 和 Redis 容器未重建。四个服务均 healthy。
- 无数据库结构变更，迁移版本仍为 `e4f6a8b0c2d4`。状态和截止时间写入已有 `parts.data`；旧客户端继续读取旧格式的完成结果。
- 切换前正在执行的任务数为 0。备份位于 `/opt/openbox/backups/20260910-suggestion-loading/activation-20260910T120943Z`，包含配置和数据库 dump，已通过 `pg_restore --list` 验证。
- 预检修正了发布脚本将镜像默认命令与 compose 覆盖命令直接比较的问题；最终分别校验镜像默认值和实际运行配置，保留线上原有启动方式。

## 验证

后端建议测试 45 项、执行相关回归 30 项；Web 511 项单测、14 项浏览器回归；Flutter 308 项测试、静态分析、release bundle 编译、locale 字节一致与 800 行门禁均通过。移动端覆盖 iOS/Android 的 320px 小屏、150% 字号、明暗主题、键盘和减少动态效果，流光位移与输入位置保持均有断言。另补齐基线缺失的三个导航 locale key，不影响前后端发布镜像。

服务器临时前端容器与公网均逐一验证 106 个静态文件及 SHA-256；实际镜像通过 Nginx 缓存、资源权限、接口、WebSocket 和后端 IP 变化回归。新后端以只读事务解析 10 份旧建议数据，并成功读取 8 组 PostgreSQL 上下文。公网流光样式已包含，缺失 JS 正确返回非 HTML 404。确认用户原会话没有草稿后刷新，三个已生成建议正常显示、控制台无错误；未向生产发送测试消息。

切换采集 48 组首页与环境接口状态：首页 5 次 502，约 10.8 秒后恢复；接口 11 次 502，发生在后端和前端两段切换中，首个失败至最终恢复约 33.7 秒。20:38:03 后样本全部 200。私有 OSS 中转文件已清理。

原生 iOS/Android 安装包本次未发布；现有 App 需重新打包安装才能获得流光动画。新后端与旧客户端兼容。

完整记录见 [部署证据](evidence/suggestion-loading-deploy-20260910.json)。

## 回滚

只将两处 image 恢复为 `openbox-backend:20260910-mobile-push-0513dcb` 与 `openbox-frontend-v2:20260910-suggestions-1c46bce`，按先后端、后前端分别执行 `docker compose up -d --no-deps` 并验证健康状态。旧镜像和备份仍保留，无需回退数据库或改动密钥。
