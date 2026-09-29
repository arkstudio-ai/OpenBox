# 2026-09-29 Turbo 口播工作流与 Ask 素材入口发布

## 版本与范围

- 应用源码：`main@ce9ae059f06fa39fd382def3bd2383d2be76e274`，先推送 `origin/main`，再从干净 `git archive` 构建。
- 功能提交：`639a0470`。MiniMax Turbo 素材方式询问、首尾帧准备、多段素材提前确认、批量生成，以及 Ask 卡片的资源库选择、OSS 上传、附件草稿和持久化交接。
- 合并保留已在现网运行的 `4d3a1b74` / `e0a23e93`，包括独立定时任务页、云桌面页和开发者面板设置；避免从原 main 发布时覆盖这些功能。
- 发布标签：`20260929-turbo-ask-ce9ae05`。目标为阿里云上海 `gw2`（`i-uf66pcsepxpc23v5qsts`）、`https://ai.bossipai.com.cn`。AWS 和移动端未发布。

## 构建与切换

本机 Docker 构建 `linux/amd64` 的完整 backend / frontend 镜像。前端固定 `nginx:1.31.5-alpine`，`VITE_BUILD_ID` 为发布标签，两镜像包含源码 revision 标签。未打包本地环境文件、密钥、模型配置或运行数据库。

| 项目 | 值 |
| --- | --- |
| backend image | `sha256:b22be65195ce7254360d5591241027270ac3802f06d737cf586c602c8565edd8` |
| frontend image | `sha256:10482f2a31382152122b187ddbed4c99e1f2d7f83b5815c2564271bf0287a518` |
| 合并镜像包 | 218,093,163 字节 |
| 镜像包 SHA-256 | `9be06a04e6b7f4d587ee44f37efba6c5877f8614be60d44d3cd07e4789632286` |
| 业务数据库 head | `d0a2c4e6f8b1`，未变化 |
| 轨迹数据库 head | `t0004_worker_efficiency`，未变化 |

经私有 OSS 中转，gw2 再次校验包哈希后装载，两个 image ID 均与本机一致。发布包留在 `/opt/openbox/releases/20260929-turbo-ask-ce9ae05/`；中转对象在验收后移除。

备份目录为 `/opt/openbox/backups/20260929-turbo-ask-ce9ae05/activation-20260929T071257Z/`，包括环境文件、模型配置、Compose、容器详情与两个数据库 dump。业务库 dump 为 49,696,342 字节，轨迹库 dump 为 44,172,361 字节，均通过 `pg_restore --list`。

切换前及 backend 切换前再次检查：有效执行租约和活动视频任务均为 0。新前端先用仅监听回环端口的临时容器验证首页、生产环境 API 和 Nginx 配置，然后顺序更新 trajectory-worker → backend → frontend；各自约 6.6 / 11.1 / 10.9 秒达到 healthy。应用切换于北京时间 **15:13:48** 完成。

只修改 override 中三项 image。PostgreSQL、Redis 容器未重建，生产环境文件、模型配置、基础 Compose 和轨迹 overlay 的 SHA-256 均未变化。无数据库迁移。

## 无影 Skill 同步

发布前冻结上海区 20 个桌面 ID，发布后重新查询仍为同一集合，没有漏掉新增桌面。具体清单和哈希见同名 JSON 证据文件。

- 7 个应用内置 Skill、共 21 个文件同步至每台桌面的 `/opt/openbox/skills/`；其中 `video-production` 包含 14 个文件。
- 使用独立 staging、SHA-256 校验和原子目录交换。旧版本保存在 `/opt/openbox/skill-backups/20260929-turbo-ask-ce9ae05/`，上传包和 staging 也保留。没有删除桌面数据，没有改动 `/data/skills` 用户技能。
- **20/20 台文件哈希验证通过**。15 台原本启用了 Action Server 的桌面，其 `/skills/video-production` 返回内容与发布源码一致；5 台预热桌面的执行服务维持原来的未启动状态，验证其磁盘文件。
- `dev-browser/SKILL.md` 在 19 台已与 main 一致。旧共享桌面 `ecd-4zjxaq5g45dr5qr0i` 另外补齐 `SKILL.md`、`src/relay.ts`、`src/client.ts`、`src/page-state.ts`，保留其依赖、用户浏览器和服务状态；`detectChallenge` 导入检查通过。旧文件备份在 `.../20260929-turbo-ask-ce9ae05-dev-browser-v2/`。

本次同步覆盖发布时现有桌面，未重烘黄金镜像。后续新增桌面或更新 Skill 时，应继续核验磁盘中的脚本和引用文件版本，不能只检查后端返回的 Skill 文本。

## 验证与已知问题

- 后端相关回归 **279 passed / 2 skipped**；前端合并后的相关回归 **55 passed**；TypeScript、i18n 和 Docker 构建通过。
- 镜像内业务代码和 Skill 文件哈希、数据库迁移 head、Nginx 配置、实际运行的 Ask 附件 schema 均通过。
- 公网首页、环境 API、Logto 配置与入口静态资源通过；匿名 `/api/agent/config` 返回预期 401；未触发付费生成或向真实会话提交测试素材。
- 五容器运行正常，worker 的 writer / db / spool / blob_store 均为 true。
- **本次并非零停机**：15:13:21–15:13:30 的 5 次 API 抽样返回 502，首页抽样保持 200。
- **后续发生独立的接口停顿**：约 15:14:35–15:16:49，后端自身健康探测和公网 API 超时，期间 CPU 较低，主事件循环的周期日志也出现约 134 秒空白；恢复时出现桌面激活租约和 Agent 租约过期告警。未通过重启恢复，进程自行恢复。15:18:02–15:20:07 的 24 轮、48 次接口检查全部 200，最慢 1.363 秒。不能仅凭 Docker 的 healthy 标记判定这段时间可用，因为其失败重试窗口较长。
- 独立进程的 SDK 冷启动探针发现 LiteLLM 拉取远程模型价格表时发生 TLS 握手超时，导入约 9 秒后回退到内置数据；这不足以证明其解释了全部 134 秒停顿，根因仍未闭环。
- 之前本地口播任务分析中的模型传输、历史图片重复传送及执行流输出缺失问题没有在本次追加修复，不能把本次部署视作整体耗时问题已解决。

## 回退

镜像和 Skill 旧版本均已保留。应用不涉及迁移，若需回退，先等待当前执行租约归零，再恢复备份中的 override，逐个 `docker compose up -d --no-deps <service>` 并检查健康和公网接口。原镜像为 backend `20260924-runninghub-703e0f3`、frontend `20260928-fe-e0a23e9`、worker `20260920-tiers-d5b3244`。

不要恢复发布前数据库覆盖后续用户数据。Skill 回退应交换当前与备份目录，保留双方内容；恢复旧 Ask 后端前还需检查是否已有新格式的待处理附件回答。
