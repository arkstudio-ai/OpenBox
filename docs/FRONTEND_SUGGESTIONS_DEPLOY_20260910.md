# 阿里云聊天建议补发记录（2026-09-10）

2026-09-10 19:28:33–19:29:18（北京时间）仅更新阿里云生产前端，恢复聊天输入框上方的下一步建议。
用户截图中的原对话刷新后可见 3 个建议按钮，浏览器控制台无错误。

## 原因与修复

上次移动推送发布已更新后端，包含生成、保存和推送 suggestions 的代码；前端仍为
`openbox-frontend-v2:20260910-nav-3791e77`，未包含建议展示组件。生产数据库中已有 5 组
非空建议，用户截图中的原会话也已有 2 组，因此本次无需后端修改或历史数据补写。

本次从 `origin/main@0f215b6` 合并 `origin/fe-nav-profile@b4ffa3b`（合并提交 `fbd06e6`），
同时保留已上线的导航和 main 中的建议功能。镜像预检发现严格权限构建目录中的 public
文件会保留 0600 权限，导致 nginx worker 读取静态图标时返回 403；在 Dockerfile 中统一
镜像内静态目录为 0755、文件为 0644，并为 nginx 回归脚本增加实际镜像模式及公共文件
HTTP 内容校验。该问题在预检阶段修复，失败候选镜像未切入生产。

## 发布产物

- 源码：`1c46bce7b7b47f42482be3e694d790389f43059d`。
- 前端：`openbox-frontend-v2:20260910-suggestions-1c46bce`。
- 架构：本地 Docker 构建 `linux/amd64`，nginx 基础版本保持 `1.31.3-alpine`。
- 镜像 ID：`sha256:1e418316d102bae651594c79e6b82e8fdce189b34f44507aacb85bf85c67694e`。
- 压缩包 SHA-256：`0caea30a21a19f326d3ef737105bf29191f9b0611a106e3f5985b8dc3c446566`。
- 服务器发布目录：`/opt/openbox/releases/20260910-suggestions-1c46bce/`。
- 备份目录：`/opt/openbox/backups/20260910-suggestions-1c46bce/activation-20260910T112448Z/`。
  配置、容器快照及数据库 dump 均已保留；4,467,552 字节 dump 经 `pg_restore -l` 校验。

只替换 compose override 的 frontend image，并执行 `docker compose up -d --no-deps --timeout 30 frontend`。
后端维持 `openbox-backend:20260910-mobile-push-0513dcb`；backend、postgres、redis 的容器 ID
和运行配置均未变化，四项服务均 healthy。无数据库迁移，迁移版本仍为 `e4f6a8b0c2d4`。
`.env`、`config/backend.env`、`config/openbox.json`、主 compose 文件哈希不变，极光与苹果
推送配置沿用已发布的值；前端完整运行环境不变。本次未部署 AWS 或移动端安装包。

## 验证

- `npm run check` 通过：75 个测试文件、496 项测试；类型和 i18n 检查通过，ESLint 0 错误、29 项已有警告。
- 建议功能 Chromium 回归 9 项通过：位置、当前模型发送、草稿/忙碌/问答/权限/只读状态、失败恢复、
  向上滚动隐藏、缓存刷新、异步建议、超过 200 条历史消息，以及中英文/明暗主题/窄屏布局。
- `FRONTEND_TEST_IMAGE=openbox-frontend-v2:20260910-suggestions-1c46bce npm run test:nginx`
  对实际镜像执行通过；覆盖静态资源权限和内容、SPA 缓存、缺失资源 404、API 请求与状态、
  WebSocket 握手、后端 IP 变化后的 nginx 解析。构建源 public 文件刻意使用 0600/0700 验证修复。
- 上线前服务器 loopback 临时容器通过全部 106 个静态文件的 HTTP 与 SHA-256 检查；上线后
  公网 106 个文件再次逐一匹配，SPA 路由正常、缺失 JS 返回非 HTML 404、生产环境接口未变。
- 用户真实 Chrome 原会话在确认没有未发送草稿后刷新：输入框上方「下一步建议」含 3 个按钮，
  截图与无障碍树均确认位置正常；控制台错误数 0。未发送生产测试消息。
- 切换时公网首页和环境接口各捕获 14 次 502，采样从 19:28:34.529 至 19:29:04.824 恢复，
  约 30.3 秒。共 82 组双端点采样，恢复后的样本全部 200。后端进程没有中断。
- 两个本次 OSS 中转对象均已删除，两个发布前缀均为空。

完整机器可读记录见 [部署证据](evidence/frontend-suggestions-deploy-20260910.json)。

## 回滚

仅将 override 中 frontend image 恢复为 `openbox-frontend-v2:20260910-nav-3791e77`，执行
`docker compose up -d --no-deps frontend` 后检查容器 healthy 和网页。不恢复数据库或其他
配置；旧镜像与完整备份均保留。回滚后建议按钮会再次不可见。
