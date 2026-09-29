# 2026-09-29 协议中心、AI 标识与异步标题发布

## 版本与构建

- 发布源码：`main@50274cff2428dbba67199b093cd7ee56231c6835`，已推送远程。标签：`20260929-legal-50274cf`。
- 目标：阿里云上海 ECS `gw2`（`i-uf66pcsepxpc23v5qsts`）、`https://ai.bossipai.com.cn`。本机 Docker 从干净 `git archive` 构建完整 `linux/amd64` 镜像，前端使用 `nginx:1.31.5-alpine`；没有打入本地 `.env`、模型配置或密钥。
- 后端镜像内标题、协议路由与入口文件的 SHA-256 与发布源码一致，业务/轨迹迁移 head 仍为 `d0a2c4e6f8b1` / `t0004_worker_efficiency`。本次不新增迁移，也不更改无影 Skill。
- 合并验证已通过 Web 998 项测试、后端 34 项回归及原生双端构建；原生既有价格断言失败及其边界见 [实施记录](../LEGAL_CENTER_IMPLEMENTATION.md)。本次仅发布服务端与 Web，未分发新 App。

## 备份与切换

- 镜像经私有 OSS 中转，在服务器复核包哈希、镜像 ID、架构与源码 revision 后加载；发布包和脚本保存在 `/opt/openbox/releases/20260929-legal-50274cf/`，两个 OSS 临时镜像对象已删除。
- 首次切换检查发现活跃对话，未替换容器；等待期间用户明确要求“立即发布”并确认接受当前对话中断。已停止原空闲等待命令，仅取消本次发布的任务归零门禁，保留配置校验、备份和失败回退。第一次维护切换前仍有 1 个对话执行租约和 1 个视频任务；后续重试时三类活动计数均已为 0。
- 备份目录：`/opt/openbox/backups/20260929-legal-50274cf/activation-20260929T082204Z`。配置、Compose、容器信息和业务/轨迹两个数据库 dump 均保留，dump 已通过 `pg_restore --list` 校验。
- 先以回环端口临时前端验证 18 个匿名协议页面、生产 API 和 Nginx 配置。第一次切换后的后端健康请求超时，保护脚本已完整回退，未恢复旧库覆盖新数据。重新备份后再依次更新 trajectory-worker → backend → frontend，并增加后端至少 120 秒启动观察与连续 6 轮直接健康/API 检查；300 秒内无法稳定仍会自动回退。后端新增协议确认接口先于新登录页面生效；北京时间 **2026-09-29 16:25:23** 完成切换。
- 仅更改 override 的三项镜像引用；数据库与 Redis 容器 ID 不变，其余生产配置哈希不变。各服务健康与 worker 的 writer/db/spool/blob_store 检查通过。启动停顿的根因未在本次修改，不能将延长启动观察视作根因已解决。

## 公网验证

- 160 个页面与 JS/CSS 资源逐一核对，SHA-256 与本地构建镜像一致。18 个中英文协议目录 URL 可匿名直接访问，正文不依赖脚本或登录。
- 首页、生产环境 API 与 Logto 配置返回 200；匿名 agent 配置及 `POST /api/auth/me/legal-consent` 返回预期 401，没有提交真实协议确认或生成任务。
- 发布期与后续共采样 699 次，非 200 样本 43 次；切换完成后 156 次采样，非 200 样本 0 次。完整时间与状态见同名 JSON 证据；单实例替换不承诺零停机。
- 运行时检查中，后端与 worker 日志均无 traceback 或 ERROR。本次沿用已验证的淡透明媒体预览标识；下载文件本体的烧录与隐式元数据仍属 [已说明的范围边界](../LEGAL_CENTER_IMPLEMENTATION.md)。

## 回退

旧镜像 `openbox-backend:20260929-turbo-ask-ce9ae05` 与完整配置备份仍在服务器。若需回退，先确认当前任务归零，将备份中的 `docker-compose.override.yml` 恢复，按 worker、backend、frontend 逐项 `docker compose up -d --no-deps <service>` 并验证健康。新后端与新前端的协议确认行为应配套切回；没有数据库迁移，不应恢复旧数据库覆盖发布后的用户数据。
