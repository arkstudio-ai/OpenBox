# 部署说明（AWS 开发环境 / 阿里云生产环境）

两套线上环境跑的是**同一份 compose 与同一组配置文件**，只有域名、端口绑定和
少数 env 值不同。本文说明各自的拓扑、需要哪些配置文件、以及怎么发布与回滚。

Logto SSO 的取值另见 [LOGTO_PROD.md](LOGTO_PROD.md)。

## 当前阿里云发布：2026-09-09 QWH 云电脑恢复与页面持久化

- gw2 后端于 17:51:36–17:51:56 切换至 `20260909-qwh-56ef0f8`，源码 `main@56ef0f8`；前端继续使用 `20260909-admin-skills-d445b9f`。没有数据库迁移，AWS 开发环境未发布。
- 修复远程浏览器未连接时反复切换中继、页面名称只存内存、旧 5 GiB MemoryHigh 导致控制服务饥饿，以及后端镜像缺少桌面恢复源。
- 生产 14 台桌面运行时 `20260909.2` 全部通过云助手只读检查；QWH 的真实浏览器输入/点击、重连、重复回退 PID 不变、跨中继切换 target ID 保持一致均通过。手机实机打开仍待用户确认。
- 发布前保留数据库/配置和被中断会话记录；用户明确允许中断当时的视频会话后立即发布。四服务与应用配置验收见报告；旧镜像和桌面备份保留。
- 首次升级未保留全部旧内存别名，已按历史记录恢复 QWH 的三个关键页面；不能把它描述成所有旧别名无损迁移。后续迁移必须在运行时安装器停止旧中继前保存映射。
- 备份 `/opt/openbox/backups/20260909-qwh-recovery/`，镜像包 `/opt/openbox/releases/20260909-qwh-56ef0f8/`。详细因果、逐项验收及回滚限制见 [QWH 修复报告](QWH_BROWSER_RECOVERY_20260909.md)。

## 历史阿里云发布：2026-09-09 超管技能 CRUD 与虚拟机安装管理

- 14:25:30–14:26:33（北京时间）完成 gw2 后端 → 前端串行切换；源码
  `main@d445b9f`，统一镜像标签 `20260909-admin-skills-d445b9f`。仅部署阿里云应用，
  未部署 AWS、未发布手机安装包，未更改无影云桌面或本地开发配置。
- 新增管理端 Skill / MCP CRUD、回收站、ZIP 多选批量上传、批量删除与图标编辑；
  用户安装页区分历史流水和按桌面/用户手动扫描的实际安装，可安全卸载非内置项。
  业务边界与测试详情见 [技能管理验证说明](ADMIN_SKILL_MANAGEMENT_QA.md)。
- 本地以 `git archive d445b9f` 干净导出构建 `linux/amd64` 镜像；镜像内无本地
  `.env.*`、`openbox.json` 或运行时数据库。私有 OSS 中转包 SHA-256：
  `ddca06017c4b93706b42204cb6abf273f186d993477290dd8c7323765c70651b`。
  服务器装载后 image ID 与本机一致：backend
  `sha256:f2dd3724ea898c4b1345d6382cd0964c4844dbaa749f38b42baf240d4b4d5615`，
  frontend `sha256:8fd0be7f814d8cd20a6f0cbf15e0f1d24009bd20876f3180222006738e6f8696`。
- 数据库迁移 `d9e1f3a5b7c2 → e1f3a5b7c9d2`：创建 `skill_catalog_packages`，
  审计资源标识扩至 128。先在隔离副本验证升级/回退/再升级，46 张原有业务表的数据
  指纹不变；正式发布又在停写窗口核对同样的 46 张表，原有数据未被迁移改写。
- 切换前发现有新会话运行，未强行中断；等活动任务与运行租约释放后，设置 API / WS
  维护门禁、停止旧后端、做停写备份及迁移，再逐个启动新后端和前端。
- 只修改服务器 compose override 的两条 image。`.env`、基础 compose、
  `config/backend.env`、`config/openbox.json` SHA-256 不变，后端完整运行时环境变量
  前后一致：仍为 `prod`、生产 Logto、`per_user / per_desktop`、生产 relay / `18001`、
  `POOL_AUTO_PURCHASE=false`；未带入本地密码登录或开发桌面配置。
- 四服务 healthy、重启计数 0；postgres `98258cffb1f7`、redis `4c1a6fb5613a` 容器未变。
  公网首页及 100 个构建资源（共 101 个文件）均与镜像 SHA-256 相符；生产环境和
  Logto 配置正确，未登录管理接口为 401。容器内真实 ORM 只读读取目录/投稿/管理覆写
  通过；没有通过伪造登录或删除真实用户技能做上线验收。
- 最后一组公网采样 152 次：首页 151 次 200、1 次 502；API 106 次 200、45 次维护
  503、1 次 502。首个维护样本至恢复样本约 50.9 秒（14:25:31–14:26:22），此后恢复。
  前端仍是独占端口的单实例，替换时出现了一次短暂 502，不能声称零停机。
- 本轮发布复跑后端相关 186 项通过；前一轮前端 402 项、浏览器 12 个流程、独立
  PostgreSQL 管理接口 57 项均通过。Docker 前后端生产构建通过。
- 备份与报告在 `/opt/openbox/backups/20260909-admin-skills-d445b9f/`（0700），含
  `preflight.dump`、`stopped.dump`（均验证可列目录）、配置、迁移预演/激活/验收报告及
  公网采样/静态资源校验。停写 dump SHA-256 为
  `d9759f677f0bdc0c5af082954bf034f989cb3191bf8dc0e265b86629cddd6bf6`。
  镜像包保留在同名 `releases/` 目录，旧镜像保留；临时迁移副本和 OSS 中转对象已清理。
- 回滚注意：旧镜像启动迁移不认识 `e1f3a5b7c9d2`。一旦管理端写入包或删除标记，
  **不能直接 downgrade 删除新表，也不能无审查地让旧代码重新展示已删除条目**。
  发布脚本的自动回退仅限前端放流前、确认新表为空的维护窗口；后续回滚需停写备份、
  审查新数据与兼容性。前后端上一镜像均为 `20260909-ask-2183504`。

## 历史阿里云发布：2026-09-09 Ask 持久化与 Web 分页

- 10:34:38–10:35:20（北京时间）完成 gw2 后端 → 前端串行切换。统一镜像标签
  `20260909-ask-2183504`，源码 `main@2183504`。**本次未部署 AWS，未更新或重启云桌面，
  未发布 iOS / Android 安装包。**
- 发布前发现生产首页 `8b80e28` 尚未合入 main；以合并提交 `2183504` 保留已发布的
  落地页、SEO 和经营场景文案，同时包含 Ask 修复与已合入 main 的管理后台 / 技能商店。
  首页源码及静态 SEO 资源与原线上提交逐文件一致，没有随本次发布回退。
- 在本机 Docker 以 `linux/amd64` 构建，源码来自 `git archive` 干净导出，不从正在运行的
  本地工作目录复制配置。`334b8c1` 补齐后端 `.dockerignore` 的 `.env.*` 排除规则；
  镜像内确认无 `.env.wuying-dev`、`.env.local-ask`、本地 `openbox.json` 或运行时数据库。
  后端运行时只挂载服务器原有 `config/openbox.json` 和 secrets。
- 私有 OSS 中转包 SHA-256：
  `c1dc914e503bb10df2cda67f2b2ae39a83be312fa34b8438d1c602b5f2914c1f`；
  服务器校验后装载，本机与 gw2 image ID 完全相同：
  backend `sha256:2bcfb25a25973301d8b055203eea65324e1f8f31f03f5cf3d0838dd45c17b2d9`，
  frontend `sha256:1a3e7815afd79c5cda114abaaca51e1d9da38d086fb4b1ef096815cec594dbea`。
- 数据库从 `b8e3f5a7c9d1` 经技能商店迁移 `c8e0a2b4d6f1` 升至 Ask 迁移
  `d9e1f3a5b7c2`。先在同机隔离副本执行“升级 → 回退 → 再升级”，44 张原表行数
  以及所检查的用户、工作空间、项目、会话、消息、账单等关键数据指纹保持一致。
  正式切换先为 API / WebSocket 设置临时 503 维护门禁，停止唯一旧后端，再做停写备份、
  执行迁移、启动新后端，健康后才替换前端；新旧问答 worker 未混跑。
- 只更新 `docker-compose.override.yml` 的两条 image。`.env`、`config/backend.env`、
  `config/openbox.json`、基础 compose 的 SHA-256 前后完全一致。继续保持
  `APP_ENV=prod`、`WUYING_MODE=per_user`、`WUYING_ROUTING=per_desktop`、
  生产 relay `106.15.105.236` / `18001`、生产 Logto、`POOL_AUTO_PURCHASE=false`；
  没有带入本地开发桌面 / `18003`、本地密码登录配置或本地数据库。
- 四服务 healthy、重启计数 0；postgres `98258cffb1f7`、redis `4c1a6fb5613a` 容器 ID
  未变。验证时 53 个会话均 idle，无遗留 pending / running 的旧 question，没有伪造批准。
  公网首页与本地镜像字节一致，**99 个静态资源全部 SHA-256 一致**；生产环境标识、
  Logto 配置、OG / robots 通过，匿名问答 / Agent 配置入口均 401。
- 发布期间及之后共 90 次匿名采样：首页 90 次为 200；API 12 次为维护 503、78 次为
  200，首个维护样本至恢复样本约 26.6 秒，之后持续恢复。采样未捕获首页失败，
  但单实例前端替换不能因此宣称绝对零瞬断。
- 此次复跑 Web check 397 tests、Chromium 14 tests、Flutter 120 tests 及 locale
  逐字节对齐均通过；Web Docker build 通过，生产 npm 依赖审计无已报漏洞。
  没有登录其他用户的生产会话，也未把匿名健康检查当作真实用户 Ask 端到端测试。
- 备份与报告：`/opt/openbox/backups/20260909-ask-2183504/`（权限 0700），含旧配置、
  `preflight.dump`、停写后的 `stopped.dump`、`rehearsal.json`、`activation.json`、
  `verification.json`；两个 dump 均经 `pg_restore --list` 验证。镜像包保留在
  `/opt/openbox/releases/20260909-ask-2183504/`。临时迁移副本和 OSS 中转对象已清理，
  正式数据库、历史镜像与备份保留；本地开发服务继续运行。
- **回滚注意**：旧镜像不认识新迁移，不可直接改旧 tag 后运行旧启动迁移。
  发布时的自动回退仅允许在维护门禁内、无新持久化问答或新类型技能安装时执行；
  上线产生新数据后，须先停写、备份并审查兼容性，不能直接降级删除 checkpoint 表。
  原 backend `20260908-a5fix-7c891ee` / frontend `20260908-landing-8b80e28` 仍保留。

## 历史发布：2026-09-08 20:30 A5 验收回归修复（AWS + 阿里云 + 15 台桌面）

- AWS：backend + frontend `20260908-a5fix-7c891ee`（`main@7c891ee`）；gw2：backend 同 tag（override 的 backend 行已改，`.bak-<戳>` 留档），frontend 仍是队友钉的 `20260908-landing-8b80e28`。无迁移。
- 修什么：`GET /api/platforms` 默认只返回 OAuth 平台（云电脑站点需 `?kinds=oauth,desktop`），旧前端打开授权中心不再因 `capabilities.includes` 崩溃。
- 桌面：dev-browser 技能（含登录态前置段）改用 `sandbox.browser_runtime.runtime_cloud_commands()` 生成的分片安装脚本经云助手下发 15 台，桌面 `--check` 保持 `20260907.4 ready`。**禁止直接把 `container/dev-browser/**` 文件拷到桌面**：`--check` 会因与 `dev-browser-sources.json` 不一致而失败，后端随即走镜像内做不到的修复路径，模型看到"浏览器无法启动"。
- 详见 `docs/A5_DESKTOP_LOGIN_STATE.md` §7.6。

## 历史前端发布：2026-09-08 经营定位落地页与静态 SEO

- 两边 frontend 均为 `openbox-frontend-v2:20260908-landing-3f7f308`，源码为 PR #2 的
  `3f7f308`（= `origin/main@6ed65f3` + 落地页一提交，PR 发布时尚未合并）。EC2
  `/opt/openbox/build-main` 构建（镜像 ID `28d7e2114d51`），`docker save` 落盘后
  scp 到 gw2 装载，传输包 SHA-256
  `fdf77a5ce0ea8cf7e13044b6105d4f0701f7853010a043d19a2bfe0cdcb0080c`。
- 仅重建 frontend（`docker compose up -d --no-deps frontend`）；backend 两边仍为
  `20260907-d1-c51a24c`，无迁移，postgres/redis 未动。随本次一起发出的还有 main 上
  未发布的前端提交 `78700b8`（移动端授权中心与投稿）。
- **AWS 的 `docker-compose.override.yml` 现在也把 backend 钉在
  `openbox-backend:20260907-d1-c51a24c`**，与 gw2 同一约定：`.env` 的
  `OPENBOX_IMAGE_TAG` 只驱动 frontend，发 backend 必须改 override 的 `image:`。
- 内容：落地页 `/` 改为老板/经营团队定位（参考 PR #1 的 VI 与文案，PR #1 不合并）；
  `index.html` 补静态 title/description/canonical/OG/JSON-LD，新增 `og-image.png`、
  `apple-touch-icon.png`、`robots.txt`（屏蔽 /app、/api/、/login、/register、/invite/）；
  工作台空白页三条引导改为经营场景。
- 验收：gw2 公网 `https://ai.bossipai.com.cn/` 200，title 为「BossIP | 云端 AI 经营工作台」，
  `/og-image.png` 200 image/png，`/robots.txt` 200，`/api/environment` 仍为 `prod`；
  AWS 经令牌闸门同样 200，环境 `dev`。浏览器无控制台错误。
- 备份：gw2 `/opt/openbox/backups/20260908-landing-3f7f308/activation-<戳>/`、AWS 同路径
  （各含 `.env`、两份 compose）。回滚：`.env` 的 tag 改回 `20260907-d1-c51a24c` 后
  `docker compose up -d --no-deps frontend`，旧镜像仍在机上。

## 上一发布：2026-09-08 D1 浏览器调试监控（AWS + 阿里云 + 15 台桌面）

- 最终镜像 `20260907-d1-c51a24c`（源码 `main@c51a24c`），AWS 与阿里云 gw2 后端均已切换。前端自
  `2620fcd` 起无变化，之后的前端 tag 只是重打标签；gw2 前端容器仍跑 `2620fcd` 镜像（内容相同）。
  迁移新增 `b6d1e2f3a4b5`（`desktop_events` 表），两边库均已到该 head。
- 内容：探针分层（transport/connect/http/parse）、失败自动采集桌面诊断快照并在工具错误里引用
  `[diag:<id>]`、`desktop_events` 时间线、Fleet 页「诊断」抽屉、设置页浏览器不可用原因。
  详见 `docs/D1_ECD_BROWSER_DEBUG.md` §7 与 `docs/WUYING_SANDBOX.md`「Reading a broken desktop」。
- 桌面端：action server `2026.09.07-browser-diag-v1` 已下发 15 台（共享桌面 ecd-4zjxaq5g45dr5qr0i、
  gw2 的 9 台 assigned 重启生效、5 台 prewarm 只换文件，待分配时随通道安装启动）。
  文件经私有 OSS 预签名链接分发并校验 SHA-256，临时对象已删。**重启 action server 会连带杀掉
  它 cgroup 里的 Chrome/relay**（用户登录态在 profile 里不丢，页面状态丢），下一次浏览器使用自动重拉。
- 发布过程中被埋点当场抓到并修掉的三个问题（都在 `main`）：
  1. `cf5eb1d`：后端镜像只打包 `backend/`，`container/obx_diag.py` 不在镜像里 → 采集脚本改放
     `backend/sandbox/obx_diag.py`。
  2. `35707a8`：**所有历史镜像里都没有 `/container/dev-browser`**，镜像内的 runtime 修复路径
     （`runtime_install_script` / `ensure_desktop_browser_runtime`）从来跑不了，只是此前
     RUNTIME_VERSION 一直匹配没触发；`80fb70a` 升到 `.5` 后 gw2 两台桌面通道校验立刻循环失败
     （时间线里 `browser.runtime_check`/`runtime_repair` fail）。已退回 `20260907.4`，并让该路径报出
     明确错误。**根治需要把构建上下文改成仓库根目录，属于发布流程改动，未做。**
  3. `7d59c16` + `94b2a98`（`c51a24c` 再修采集器解析 Chrome 空格分隔的 argv）：共享桌面的 action server 自 8 月 31 日起带 `runner-isolation.conf`
     加固（无 CAP_SYS_PTRACE），重启后以 root 跑：`obx-x` 读不到 gnome-shell 的 environ 拿不到
     XAUTHORITY；且 `/tmp/obx-*.log` 属主仍是旧的 `sandbox` 用户，`fs.protected_regular=2` 让 root
     的 `>` 重定向失败并被吞进 /dev/null，Chrome/relay 根本没启动。`obx-x` 增加 xauth 文件回退，
     三个启动脚本先回收日志再重定向。修后共享桌面 `ensure_browser` 17s 成功。
- 备份：AWS `/opt/openbox/backups/20260907-d1-{2620fcd,35707a8,94b2a98,c51a24c}/<戳>/`，gw2
  `/opt/openbox/backups/20260907-d1-{2620fcd,94b2a98,c51a24c}/<戳>/`（含 pg_dump 与 override）。
- 回滚：改回 `.env`（AWS）/ `docker-compose.override.yml` 的 backend image（gw2）到
  `20260907-browser-selfheal-73ad1c9`，`docker compose up -d --no-deps backend`；迁移 `b6d1e2f3a4b5`
  只加表，旧代码不读它，无需降级。

## 当前阿里云发布：2026-09-07 dev-browser 平台修复

- 17:40（北京时间）阿里云 gw2 后端已切换至
  `openbox-backend:20260907-browser-1b5dbdd`，源码 `main@1b5dbdd`，运行时版本
  `20260907.3`。本机 Docker 构建 `linux/amd64`，私有 OSS 中转、校验 SHA-256
  后在服务器装载；未在阿里云服务器构建源码。
- 镜像 ID `sha256:9d57d3651229d9ba67b3b4372b27971f5ed85fd5a7fc02c8fcaa0219552d480f`，
  传输包 SHA-256 `3f28bee1ad8ac7d95736d66f4056e5ed22dd4f1195dc04ba550087d2e0c77b9c`。
  镜像检查通过，包含锁定运行时与 A5 的 `segno`，不包含本地 `skill_jobs.db`。
- 本次仅改 `docker-compose.override.yml` 的 backend image。frontend 保持
  `openbox-frontend-v2:20260907-a5p2-4667a8c`；frontend、postgres、redis 容器 ID
  未变，四服务 healthy，迁移仍为 `a5c0d1e2f3a4`，环境仍为 `prod`。
  `.env`、基础 compose、`config/backend.env`、`config/openbox.json` 均逐字节核对未变。
  **后续发布必须更新 backend 的 compose override，不能只修改 `.env` 的 tag。**
- 激活前的配置及经 `pg_restore --list` 校验的数据库备份位于
  `/opt/openbox/backups/20260907-browser-1b5dbdd/activation-20260907T094029Z/`，
  目录权限 `0700`。应用回滚可恢复其中的 compose override、仅重建 backend 并
  等待 healthy 后 reload Nginx；本次无迁移，不需要恢复数据库或用户浏览器数据。
  传输用临时 OSS 对象已删除，本地及服务器镜像包、历史镜像和备份保留。
- 发现另一类真实故障：桌面 `ecd-b9oizzx4rfhbsm1uh` 的 action service 原先
  `TasksMax=512`，`pids.events` 已记录 20 次触顶，Chrome 日志有与测试超时对应的
  `pthread_create: Resource temporarily unavailable`。Chrome/IBus/Node 的线程均计入
  上限；已在线提高至 2048，未重启 action service、未改变 6 GiB 的内存硬限制。
  原属性保存在该桌面的 `/opt/openbox/backups/browser-task-budget-4lvtcpmp/`。
  随后检测到该桌面 Chrome 已换为新进程，技能原样 `npx --no-install tsx` 的网页、
  中文输入、点击、快照全部通过；没有再对已恢复的新进程执行重复重启。
- 相同资源配置已接入版本化修复器及 bootstrap：持久化单独的资源 drop-in，在线
  修改不足的 live cgroup 限额，保留更高/无限的既有设置。只读检查不要求写锁或
  systemd D-Bus，兼容旧 action 容器。仅提高上限不能保证恢复已损坏的 renderer，
  此类浏览器仍需要单独确认后的备份重启，不能把端口通等同于页面操作通。
- 最终版本在未分配预备机的真实重启后，运行时 `20260907.3` 检查通过，开机服务
  `active / Result=success / ExecMainStatus=0`，持久化 TasksMax 为 2048。
  boot ID 为 `355f647c-33db-474b-9e19-040ade25b82a`；随后已归还预热池。
  没有重启任何已分配用户的云电脑。
- 本地 **197 项相关回归通过**，覆盖运行时、线程限额、启动/重启门禁、只读兼容、
  桌面激活、池、浏览器/拼音、租约、技能以及现有订阅/A5 功能。全量测试的旧夹具与
  私有模型配置失败仍单独记录在下文，不宣称全量测试通过。
- 旧机 `ecd-8zp47qagrsc95h67t` 还存在 `media.conf` 的 `TasksMax=512`；按 systemd
  的文件名顺序，它会覆盖早于自己的资源 drop-in，单独 `set-property` 也会在配置
  重载后回退。最终版本使用 `zz-openbox-browser-resources.conf`，保留原 media
  文件与内存设置；实际安装、重载后的只读检查及幂等执行均通过。
- 最终全量结果：**14/14** 台运行时 `20260907.3` 检查通过，开机检查均已启用。
  17:43 只读快照为 9 台 assigned、5 台 prewarm，激活记录 8 个 ready、1 个 suspended；
  预备机保留标记已清除，没有新增云电脑、账号或测试订单。
- **8/8** 个已激活账号经真实的租约及套餐权限检查，通过技能原样
  `npx --no-install tsx` 连接、公开网页访问、中文填入、按钮点击和无障碍快照。
  最终版本复测仍为 8/8，前后 Chrome CDP 标识相同，测试页全部关闭；
  包含验收期间刚从预热池分配的 `ecd-c4qndqrko3db7kjfz`。停用旧机只做管理员
  运行环境检查，未绕过订阅给它开通浏览器操作；其只读 action 环境也通过新版本检查。
- 生产域名首页请求成功，`/api/environment` 返回 `{"name":"prod"}`；最后复核四个
  服务均 healthy、后端源码标记 `1b5dbdd`。未接入图形桌面时的浏览器仍为无头模式，
  不会显示在 Web SDK 的桌面画面中；工具会明确告知，且不会为切换显示方式关闭会话。

## 平台修复机制：2026-09-07 dev-browser

- 同一修复器接入云电脑初始化、预热池验收、通道激活、开机前检查和浏览器首次使用；
  修复失败时不返回 Ready，保留原桌面供重试，不新增购买或重建。
- 固定 npm 锁文件，使用 `npm ci --ignore-scripts`，隔离 npm 配置、串行修复、暂存验证
  后切换；已健康的环境不重复安装。Chrome 仅放行自有专用 profile，不读取旧 worker
  登录信息；保留正在运行的浏览器及拼音环境。
- 未连接 Web SDK、尚无图形会话时，使用独立低权限用户的无头 Chrome。浏览器自动化
  与可见桌面可用性分别报告，不创建第二个 X 桌面，也不强制切换正在运行的浏览器。
- 已在未分配预热机 `ecd-0b7gj174mc6f23ctq` 上验证修复、幂等执行、真实网页访问、
  中文输入、点击、无障碍快照及一次真实重启后的恢复。测试期间从预热分配中保留该机，
  没有创建新账号、订单或额外云电脑。批量及应用发布结果见后续验收记录。
- 相关回归 **156 项通过**。全量测试仍存在数据库 readiness 测试夹具、旧工具上下文
  与私有视频模型配置的失败，隔离的原始 main 也能复现这些类别；不能称为全量测试通过。
- 黄金镜像制作脚本也增加相同运行时和开机检查。未从任何现有用户桌面创建新镜像；
  即使配置继续使用旧镜像，新建/重建实例也必须经过运行时修复和真实 CDP/relay 验收。

## 单桌面修复：2026-09-07 dev-browser 运行环境

- 仅修复目标桌面 `ecd-4y9s9igraz7hc58ea`，未重启云电脑、action server、隧道或
  gw2 应用容器，未改动套餐、订阅、数据库、前后端镜像和其他桌面。
- 旧 BossIP Chrome 启动包装脚本把 OpenBox profile 改写到旧 worker 的目录；该
  目录属于另一 Linux 用户且权限为 `0700`，Chrome 无法读写，退回默认目录后又被
  Chrome 151 的远程调试限制拦截。修复只放行当前用户自有、非符号链接的
  `$HOME/.config/obx-chrome`，不开放旧 worker 目录权限。
- 为缺失的 `node/npm/npx` 命令接入镜像已有 Node `22.23.1` 运行时。relay 依赖在
  独立临时目录安装，禁止安装脚本，校验 `tsx`、Playwright、Hono 后再放入正式目录；
  未升级系统 Node 或其他应用依赖。bootstrap 原先的 `npm install | tail` 会掩盖
  npm 不存在的错误，现改为调用可备份、失败即停止的修复脚本。
- 14:54（北京时间）在桌面租约内正常结束该用户的旧 Chrome，备份并复制其本人的
  默认 profile 到独立 OpenBox profile，再通过生产的 `ensure_browser` 启动。
  默认 profile 原目录保留，未读取/迁移旧 worker 的登录数据；未修改拼音相关代码。
- 14:56 实际运行技能所用 `npx --no-install tsx` 客户端：打开 Example Domain、
  中文填入、按钮点击、页面无障碍快照均通过，临时测试页已关闭。
  `9333/9222` 均仅监听 `127.0.0.1`，relay 返回 `chromeAvailable=true`，
  IBus 拼音引擎和候选面板进程存在，action server uptime 连续。
  本地运行时修复、浏览器、技能执行和桌面租约相关测试共 **37 项通过**。
- 回滚材料均留在该桌面，不在 gw2：
  `/opt/openbox/backups/browser-runtime-20260907T064920Z-kgjhva_r/` 保存旧 Chrome
  包装脚本；`browser-runtime-20260907T065222Z-x9v_sa4z/` 保存成功安装记录；
  `/opt/openbox/backups/browser-profile-20260907T065417Z-d_pts_yp/default-profile.tar.gz`
  保存浏览器原数据。目录权限为 `0700`。
- 此处是 14:56 单桌面修复的历史记录；后续平台修复将真源归并到
  `backend/sandbox/browser_runtime_repair.py`，见上方平台修复机制及后续验收记录。

## 历史配置：2026-09-07 切换 prod 环境标识

- 按用户确认，将阿里云 gw2 的 `/opt/openbox/config/backend.env` 中
  `APP_ENV=staging` 改为 `APP_ENV=prod`。公网 `/api/environment` 已返回
  `{"name":"prod"}`，浏览器刷新后不再显示“内测环境”角标。前端在页面会话中缓存
  环境值，已打开的页面需要刷新。后续发布应保留 `APP_ENV=prod`。
- 当时前后端镜像统一标签为 `20260907-subscription-129e758`，源码提交为
  `129e758`，包含订阅驱动的无影云自动开通及统一测试价格。本次仅修改环境标识，
  未重建镜像；专业版、旗舰版的月付和年付总价仍均为 **0.10 元**，免费版仍为 0 元。
  `BILLING_MODE=shadow`、访问控制、订阅、积分与桌面配置均未改动。
- 切换前的配置与数据库备份位于
  `/opt/openbox/backups/20260907-env-prod/activation-20260907T043314Z/`。
  12:33:15 至 12:33:36（北京时间）仅重建 backend，健康后 reload 前端 Nginx；
  frontend、postgres、redis 容器未重建，四个服务均 healthy，数据库 revision 仍为
  `a4b6c8d0e2f5`。仅回退本次配置时恢复该备份的 `config/backend.env`，再执行
  `docker compose up -d --no-deps backend`；等待 healthy 后 reload 前端 Nginx，
  不需要回退镜像或恢复数据库。

## 上一生产版本：2026-09-07 环境角标与 10:22 短暂 502 复盘

- 阿里云 gw2 当前统一标签为 `20260907-main-33edc66`，源码提交为
  `33edc66`（包含此前 `acbc1b2` 的视频模型强约束）。后端 image ID 为
  `sha256:7c912f8a019ad0e1a3949aa4fdfe6b23fe659982c05aed6b5fb7be370c11b665`，
  前端 image ID 为
  `sha256:b4ec35d1d7ad41e125c0fe6f8de279a4dc22035b98627568f430f4c265816854`；
  `/api/environment` 返回 `staging`。
- 10:22 发布时执行了整栈 `docker compose up -d`，唯一的 backend/frontend
  被同时替换。backend 创建于 10:22:02、启动于 10:22:14；frontend 创建于
  10:22:04、启动于 10:22:30。gw2 的 `:80` 在旧 frontend 停止与新 frontend
  启动之间没有可用容器，腾讯 Lighthouse 前置 Nginx 因 upstream 不可达短暂返回
  502。根因是**单实例 Compose 的发布断档**，不是应用持续崩溃、数据库故障或迁移失败。
- 10:24 后连续 12 轮公网首页与 `/api/auth/logto/config` 探测全部为 200；两个容器
  均为 healthy、`RestartCount=0`，会话、账单、舰队与 WebSocket 请求正常。数据库仍为
  `f3a5b7c9d1e4 (head)`，无需回滚。
- 本次发布没有留下独立的版本化激活备份；最近的有效备份仍是
  `/opt/openbox/backups/20260907-video-model-acbc1b2/activation-101224/`。后续发布必须先
  备份，再按下文“发布可用性要求”逐服务切换，禁止直接无差别重建整栈。

## 上一生产版本：2026-09-07 用户视频模型强约束

- 阿里云 gw2 已部署统一标签 `20260907-video-model-acbc1b2`，源码提交为
  `acbc1b2`。后端 `linux/amd64` 镜像 ID 为
  `sha256:14d53bf01a2e21ccb87fc86b293dea0e0bef6dd9cb970236843ba6def7035300`；
  前端没有代码变化，沿用上一版 image ID
  `sha256:d787f253315811ad34b272fcd0e6a889380e7a15cdc4dcc3927465bf9b001364`
  并只追加统一标签。
- 修复生产实锤的静默换模型：会话已选 `MiniMax-H3 / 720p`，Agent 却显式用
  `doubao-seedance-2-0-260128 / 1080p` 报价并付费提交。现在
  `sessions.video_model/video_resolution` 是工具层强约束；Agent 参数不一致会在任何
  provider 请求和扣费前拒绝，省略参数则精确继承用户选择。工具 Schema、实际视频
  SKILL、model-guide 和 C5 偏差记录同步更新。
- 生产零费用复测直接复用原事故会话：豆包覆盖请求返回 `VideoRequestError`，省略
  参数解析为 `MiniMax-H3 / 720p`。新增 4 项强约束测试、视频选择存储测试及舰队相关
  12 项测试通过；整套测试剩余的 5 项私有视频配置基线失败与上一版本记录一致，WS
  套件顺序污染项单独重跑通过。
- Logto 用户 `andrewwang`（`01M1PC7RCXXJWQFZZX2HVCCY2H`）的 OpenBox 全局角色
  已从 `user` 提升为 `admin`，默认空间角色仍为 `owner`。舰队权限读取 OpenBox access
  JWT，不读取 Logto 控制台角色；需刷新 token 或重新登录，旧 access token 最长 15
  分钟失效。
- 本次没有数据库迁移，发布后仍为 `f3a5b7c9d1e4 (head)`；四个 compose 服务均
  healthy，公网首页与 Logto config 为 200，匿名舰队接口为 401。有效备份在
  `/opt/openbox/backups/20260907-video-model-acbc1b2/activation-101224/`，数据库 dump
  SHA-256 为
  `f73c41084a2a3664b74d111fad95d2450171d29dbfe4d9f683a48336a66334cb`；
  回滚标签为 `20260907-a3-1c242da`。

## 上一生产版本：2026-09-07 舰队页显示实时 ECD 绑定用户

- 阿里云 gw2 已部署统一标签 `20260907-a3-1c242da`，源码提交为
  `1c242da`。后端镜像 ID 为
  `sha256:a0ecd1a9a7d0b5fc880366665c5b46f8606e5b6818bf6d222024b7c732294a3c`，
  前端镜像 ID 为
  `sha256:d787f253315811ad34b272fcd0e6a889380e7a15cdc4dcc3927465bf9b001364`。
- `/admin/fleet` 的桌面表新增“ECD 绑定用户”：绑定关系实时读取 ECD
  entitlement，用户名优先关联 OpenBox workspace 所属用户，外部账号回退到 ECD
  EndUser 昵称；无法关联时保留 EndUser ID。ECD 读取失败显示“未知”，不会把 DB
  历史字段冒充实时状态。
- 本次没有数据库迁移，发布后仍为 `f3a5b7c9d1e4 (head)`。相关后端 12 项测试、
  前端 206 项测试及 i18n/lint/typecheck 通过。生产真实管理员请求返回 200，20 条
  桌面记录中当前 2 个 ECD 绑定均成功解析出用户名；匿名舰队接口仍返回 401，四个
  compose 服务均 healthy。
- 有效发布备份在
  `/opt/openbox/backups/20260907-a3-1c242da/activation-094023/`，数据库 dump
  SHA-256 为
  `45892477e6be04e0e4b34eb4dd721946178c89f0334d6c847cb81c57e2d7f543`；
  回滚标签为 `20260907-main-61227cc`。首次宿主机校验工具缺失产生的未完成备份已
  改名为 `incomplete-093956`，不得用于回滚。

## 上一生产版本：2026-09-06 Logto 完整退出修复

- 阿里云生产统一标签已更新为
  `20260906-logto-logout3-3586742`。后端镜像 ID 为
  `sha256:f27f1ba00de0491623eaacd830cbe6dd1a1b2cbfb21649db797b1d250f590114`，
  前端镜像 ID 为
  `sha256:ed042053905cf18749e659158ac5c964e1b8791225702eee4da3914b02b8f0d7`；
  均为本地构建的 `linux/amd64` 镜像。
- 根因不是 Logto 控制台缺少回调地址，而是 Web 点击退出时先清 Zustand 并渲染
  `/login`，自动 SSO 入口在 Logto end-session 完成前又发起 authorize，形成竞态并
  把同一账号重新登录。对照 `workspace/bossip` 后改为一次完整页面导航到
  `GET /api/auth/logto/logout`：后端在同一个 302 响应中撤销/删除 OpenBox refresh
  Cookie，再跳 Logto `{issuer}/session/end`；前端不再提前渲染登录页。
- Web authorize 使用 `prompt=login consent`：`login` 防止持久浏览器残留 SSO Cookie
  静默恢复刚退出的账号，`consent` 保留 `offline_access` 所需语义。移动端在
  2026-09-08 Android 实测后改为 `prompt=consent`；其 SDK 已启用 ephemeral Custom
  Tab，退出还会调用 `signOut(postLogoutRedirectUri)`，并在失败路径强制删除 SDK 的
  access/refresh/ID token。移动端强制 `login` 会在当前生产 Logto 上卡进
  `/oidc/session/end/confirm` 的空白 `Submitting Callback` 页。
- 自动验证：Web i18n/lint/typecheck 与 31 个测试文件、206 项测试通过（lint 只有
  24 条既有 warning）；移动端 locale 逐字节门禁、`flutter analyze`、26 项测试通过；
  后端认证相关 29 项测试通过。Android release APK 和 iOS no-codesign release 均
  已重新构建。
- 生产验证：四个容器 healthy，首页与 Logto config 均为 200，退出路由为 302 且
  同时返回 `refresh_token Max-Age=0` 和 Logto end-session Location；Web 生产静态包含
  `login consent`。真实 Chrome 先在同一退出实现中从 `andrewwang` 点击登出并稳定
  回首页；最终标签部署后再点击登录，停在 Logto“登录你的账号”页，不再静默回到
  原账号。
- 本次没有数据库迁移，发布前后均为 `f3a5b7c9d1e4 (head)`。最终备份在
  `/opt/openbox/backups/20260906-logto-logout3-3586742/activation-233415/`，数据库
  dump 的 SHA-256 校验已通过；回滚标签为
  `20260906-logto-logout2-38ef802`。中间标签
  `20260906-logto-logout-c5c86bf` 仍有 SPA 自动登录竞态，不能作为后续基线；
  `20260906-logto-logout2-38ef802` 已解决退出问题，但授权 prompt 尚未合并
  `consent`，仅用作紧急回滚。三个批次的 OSS 中转对象已全部删除；本地临时镜像
  压缩包已移入废纸篓，服务器上的镜像与版本化备份继续保留。

## 一、两套环境

> **环境定位（2026-09-07 拍板）**
> - **AWS（`ai.ueejavelin.org`）= 开发环境**：给开发者自测，随时可能重部署、重置数据，不承诺可用性；执行面走上海共享桌面（`WUYING_MODE=shared`）。
> - **阿里云 gw2（`ai.bossipai.com.cn`）= 生产环境标识（`APP_ENV=prod`）**：2026-09-07 用户测试确认后切换；数据与桌面池按生产标准管理（备份、迁移、回滚、告警）。当前付费套餐仍使用 0.10 元测试价格，计费模式仍为 `shadow`；环境标识切换不自动修改价格或访问控制。
> 两边都从 `main` 构建；先发 AWS 再发 gw2，gw2 部署前看一眼 `.env` 当前 tag，避免互相覆盖。

| | 开发（AWS） | 生产标识（阿里云） |
|---|---|---|
| 域名 | https://ai.ueejavelin.org | https://ai.bossipai.com.cn |
| 主机 | EC2 `i-0eaae88c8b67d9bb5` `OpenClaw-NewAPI` | ECS `i-uf66pcsepxpc23v5qsts` `openbox-gw2-sh` |
| 地址 | `54.254.36.226`（ap-southeast-1） | `106.15.105.236` / 内网 `10.100.1.83`（cn-shanghai） |
| 入口 | 本机 caddy（:80/:443，含 TLS） | 腾讯 Lighthouse `106.52.167.53` 回源到 :80 |
| 前端端口 | `127.0.0.1:18081:80`（只给 caddy） | `80:80`（安全组只放行 Lighthouse 回源） |
| 远程执行 | AWS SSM（`AWS-RunShellScript`） | 阿里云云助手（`ecs RunCommand`） |
| 源码 | 有 `/opt/openbox/src`（git checkout），可就地构建 | **无源码**，镜像从外部装载 |
| 沙箱 | `SANDBOX_PROVIDER=wuying` | `SANDBOX_PROVIDER=wuying` |






### 2026-09-08 19:30：A5 二期 P2（desktop_login 工具 + dev-browser 技能前置检查）

- AWS：`20260908-a5p2-499e8a9`（backend + frontend）；gw2：backend `20260908-a5p2-499e8a9`，frontend 未动（同上一条）。无迁移。
- 桌面侧：`/opt/openbox/skills/dev-browser/SKILL.md` 已按 main 同步到 15 台桌面。

### 2026-09-08 19:10：A5 二期 P1（云电脑登录态卡组 + 定时探活）

- AWS：`20260908-a5p1-9848834`（backend + frontend）。gw2：backend `20260908-a5p1-9848834`，**frontend 未动**（仍为队友钉的 `20260908-landing-8b80e28`，其提交 `8b80e28` 不在 main）。
- 无新迁移（仍 `b8e3f5a7c9d1`）。新增内部任务 `desktop_login_probe`（6h）。
- 两边 `docker-compose.override.yml` 的 backend 行已改到本 tag；gw2 的 frontend 行未改。

### 2026-09-08 18:20：A5 二期 P0（云电脑登录态）

- 两边 backend `20260908-a5p0-f160cf7`（`main@f160cf7`），迁移 `b6d1e2f3a4b5 → b8e3f5a7c9d1`（`platform_accounts` 加 `desktop_id`、`probe_detail`）。gw2 发布前备份 `backups/pre-a5p0-20260908181953.sql.gz`。
- **两边现在都有 `docker-compose.override.yml` 钉 backend 镜像**（AWS 的钉在 `20260908-d1-a0fd70a`、gw2 的钉在 `20260908-browser-4a9725f` 之后又被改过），`.env` 的 `OPENBOX_IMAGE_TAG` 对 backend 已不生效；本次把两边 override 的 backend 行改到本 tag（各留 `.bak-<戳>`）。gw2 的 override 还钉了 frontend `20260908-landing-8b80e28`，本次未动前端。
- EC2 → gw2 传镜像改为 `docker save | gzip > /tmp/x.tgz` → `scp` → `docker load`（管道直传会卡）。
- 验证：gw2 容器内对用户工作空间跑桌面登录态探活 1.4 秒返回，三站 `bound`，`desktop_events` 有 `platform.probe`。

### 2026-09-08：A5 技能同步到全部云桌面

- 15/15 台（含共享桌面与 prewarm）`/opt/openbox/skills/{video-production,douyin-publish}` 已同步到 `origin/main` 版本；旧目录备份在各桌面 `/opt/openbox/backups/`。下发方式见 `docs/A5_AUTHORIZATION_CENTER.md` §9.5。
- 库里 4 条 `running` 桌面在阿里云已不存在（`InvalidDesktopId`）：`ecd-iu2s0ki7ez79l46sm`、`ecd-ahkizte0nthlevmrn`、`ecd-ghjuuilmgiylybv0u`、`ecd-ahkizte0nsxv3r30i`。

### 2026-09-07：A5 授权中心（抖音开放平台 OAuth + H5 投稿）

- **17:05 第三版（P2）**：两边 `20260907-a5p2-4667a8c`（`main@4667a8c`，含队友 `4a9725f` 的浏览器运行时修复），新增 `douyin_publish` 工具与 `douyin-publish` 技能，后端依赖加 `segno`。**注意**：gw2 上多了一个 `docker-compose.override.yml`（队友 16:48 用它把 backend 镜像钉在 `20260907-browser-4a9725f`），`.env` 的 tag 对 backend 不再生效；本次把 override 里的 backend 镜像改成本 tag（备份 `docker-compose.override.yml.bak-<戳>`）。后续发 backend 要同时改 override 或删掉里面的 `image:` 行。
- **16:30 第二版**：两边升到 `20260907-a5-12ad18f`（`main@12ad18f`，无新迁移）——投稿 schema 对齐抖音官方序列化、优先 `get_share` 短链、账号行显示预计到期。
- 两边统一标签 `20260907-a5-a043dea`，源码提交为 `main@a043dea`（含 origin/main 的订阅桌面工作 `129e758`/`579b3af`）。EC2 `/opt/openbox/build-main` 构建，`docker save | gzip | ssh gw2 docker load` 传输（约 47s）。
- 数据库迁移 `a4b6c8d0e2f5 → a5c0d1e2f3a4`（新表 `platform_accounts`、`publish_jobs`、`notifications`），启动时自动执行；两边发布前均有 `backups/pre-a5-<戳>.sql.gz`。
- gw2 `config/backend.env` 新增 `PUBLIC_BASE_URL`、`DOUYIN_CLIENT_KEY`、`DOUYIN_CLIENT_SECRET`（15:42 追加，随本次重启生效）；AWS 未配抖音键，`/api/platforms` 上该平台 `configured=false`，`/api/webhooks/douyin` 返回 503 `douyin not configured`，属预期。
- 公网验证：`POST https://ai.bossipai.com.cn/api/webhooks/douyin` 的 `verify_webhook` 回 `{"challenge":…}`；错签名事件 401；`/api/platform-accounts/douyin/callback?state=bad` 302 到 `/app/auth-center?platform=douyin&error=PLATFORM_STATE_INVALID`；前端 `index-Bg0WoWVO.js` 含 `auth-center` 路由。
- 顺带：`backend/.openbox/skill_jobs.db` 已从仓库移除并忽略（`d1f8bdc`）。

### 2026-09-06：支付宝 App Pay 服务端签名接口（上一版本记录）

- 阿里云生产后端已更新为 `openbox-backend:20260906-app-pay-f9247f1`；在本机以 `--platform linux/amd64` 构建，镜像 manifest ID 为 `sha256:1094bd9f7cfa1092e5c34c75350983f7ccad5aa42bdafbd02da177f45849654f`。
- 构建基线不是单独的 `main@49ba4ff`：发布复核发现生产旧标签还包含 `c8fea7f` 的预热池购买门禁与续费保护，因此先将该分支相对 main 的 14 个后端文件逐文件恢复并用 `git diff c8fea7f` 核对完全一致，再叠加 App Pay。中间镜像 `20260906-app-pay-6310856` 已被本标签纠正替换，不能作为后续基线。
- 前端代码未改。线上 compose 使用统一 `OPENBOX_IMAGE_TAG`，因此将原 `openbox-frontend-v2:20260906-a3-c8fea7f` 的同一 image ID 仅追加新 tag 后重建；没有重新构建或替换前端内容。
- 后端增加 `POST /api/billing/orders/{order_id}/app-checkout`，为 Android/iOS 官方支付宝 SDK 生成服务端 RSA2 签名 payload。商户私钥继续只从服务器 secret 读取；App SDK 返回值不作为到账证据。
- 本次无数据库迁移，发布前后均为 `f3a5b7c9d1e4 (head)`；未修改 `backend.env`、支付密钥或业务数据。
- 发布前 billing + fleet + migration 相关回归 `183 passed`；全量 unit 为 `1457 passed / 5 failed`，剩余 5 项全部位于本次未改动且依赖本机私有配置的 video 模型断言，已保留为既有测试债，不影响本次支付/舰队回归。
- 生产 `backend/frontend/postgres/redis` 均 healthy；公网首页与 Logto config 为 200；新 App Pay 路由未登录请求由发布前 404 变为 401，证明路由已在公网生效，容器内路由断言同时通过。
- 最终发布备份：`/opt/openbox/backups/20260906-app-pay-f9247f1/activation-223456/`，含 `.env`、后端配置、两份 compose 与 PostgreSQL custom-format dump；中间切换的备份也保留在 `backups/20260906-app-pay-6310856/`。回滚标签为 `20260906-a3-c8fea7f`，旧镜像仍保留。OSS 中转对象已在每次发布结束后删除。

### 2026-09-06：正式价格与项目名称修复（上一版本记录）

- 阿里云前后端镜像已更新为 `20260906-project-prices-4cd8725`，源码提交 `4cd8725`；数据库升级至 `f3a5b7c9d1e4`。
- 清空 `config/backend.env` 的 `BILLING_PLANS_FILE`，恢复专业版 599 元/月、7188 元/年，旗舰版 2100 元/月、25200 元/年。`BILLING_MODE=shadow` 保持不变。
- 注册创建的默认项目统一命名为“默认空间”，补修 1 条遗留 `Default` 记录；项目列表按创建时间倒序，时间相同时按 ID 倒序。
- 以本次发布前实际容器为准，保留 `WUYING_MODE=per_user`、`WUYING_ROUTING=per_desktop`、`POOL_ENABLED=true`、`POOL_AUTO_PURCHASE=false`。桌面池此前已由另一项更新启用，本次没有改动该开关；仅修改套餐目录环境变量。9 个无影相关源码文件内容一致，原 12 条桌面记录及归属关系保留。
- 189 项相关测试通过；先在数据库副本验证迁移。浏览器确认“默认空间”、月付/年付原价、原专业版有效期和 290 积分余额；已支付的 0.10 元测试订单与订阅快照保持不变。前后端健康检查通过。
- 本次配置和数据库备份：`/opt/openbox/backups/20260906-project-prices-4cd8725/activation-101803/`。回退代码时可先用本次镜像执行 `alembic downgrade e2f4a6b8c0d2`，该数据修复迁移的回退不会重新改回英文名称。

### 2026-09-06：阿里云积分与支付宝首次发布（历史记录）

- 发布目标为 **`https://ai.bossipai.com.cn` / `i-uf66pcsepxpc23v5qsts`**。
- 前后端镜像：`20260906-billing-fleet-aaf1fff`，代码提交 `aaf1fff`，本地构建 `linux/amd64` 后传入服务器。
- 此版本合并积分/支付宝代码与阿里云原有 `60a7d59` 桌面池代码；不能用只包含 billing 的旧镜像覆盖该环境。
- 数据库由 `a3f1e5c7d9b2` 升至合并版本 `e2f4a6b8c0d2`。先在数据库副本验证，原用户、工作空间、对话、项目、桌面和桌面池数据保持一致。
- 保留 `WUYING_MODE=per_user`、`WUYING_ROUTING=per_desktop`、`POOL_ENABLED=false`、`POOL_AUTO_PURCHASE=false` 及所有原运行配置。9 条桌面记录与归属关系保留；未执行真实桌面关机/开机操作。
- 支付通知为 `https://ai.bossipai.com.cn/api/billing/webhooks/alipay`，支付返回为 `/app/billing/orders`。密钥独立放在服务器 `secrets/alipay/`，以只读方式挂载，未打入镜像。
- 首次发布时按用户要求保留专业版 **0.10 元**、旗舰版 **0.20 元**测试价格；`BILLING_MODE=shadow`，模型用量记账但不扣余额。当前价格见上方更新记录。
- 验证：前端 205 项、后端相关 195 项测试通过；公网前端与构建产物一致，支付宝真实签名查询成功，公网回调拒绝无效签名。尚未实际付款。
- 配置和数据库备份位于服务器 `/opt/openbox/backups/20260906-billing-fleet-aaf1fff/`。新增 billing 表后回退旧镜像时，不要直接执行旧镜像的 `alembic upgrade head`；旧镜像不能识别新迁移编号。

AWS 在目标纠正前已更新到 `20260906-billing-9bfb283`，本次阿里云发布没有回退该更新。两边配置、数据库和支付通知域名各自独立。

请求链路（两边一致）：

```
浏览器 → 反向代理 → frontend 容器(nginx)
                      ├── /        静态 SPA
                      ├── /api/    proxy_pass → backend:8080
                      └── /ws/     proxy_pass → backend:8080（WebSocket 升级）
```

`frontend-v2` 不内嵌后端地址：`VITE_API_URL` 留空即走同源 `/api`，由 nginx 反代。

## 二、服务器目录与配置文件

两台机器的 `/opt/openbox/` 布局相同：

```
/opt/openbox/
├── docker-compose.yml            # 编排主文件（★ 目前只存在于服务器，未进仓库）
├── docker-compose.override.yml   # 各环境差异（端口绑定 / 挂载凭证）
├── .env                          # OPENBOX_IMAGE_TAG、OPENBOX_DB_PASSWORD
├── config/
│   ├── backend.env               # 后端环境变量（★ 与本地 backend/.env 同构）
│   └── openbox.json              # 模型/供应商配置（★ 与本地 backend/openbox.json 同构）
├── secrets/
│   ├── aliyun-config.json        # 后端调用阿里云用的 AK（挂到 /run/secrets/）
│   └── wuying_ed25519            # 无影 relay 隧道私钥
└── src/                          # 仅 AWS 开发机有：git checkout，用于就地构建
```

### 各文件与本地的对应关系

| 服务器文件 | 本地对应 | 是否入库 | 说明 |
|---|---|---|---|
| `config/backend.env` | `backend/.env` | ❌ 忽略 | 同构。模板见 `backend/.env.example` |
| `config/openbox.json` | `backend/openbox.json` | ❌ 忽略 | 同构。模板见 `backend/openbox.jsonc.example` |
| `.env` | 无 | ❌ | 只有 `OPENBOX_IMAGE_TAG` 和 `OPENBOX_DB_PASSWORD` |
| `secrets/*` | 无 | ❌ | 密钥，永不入库 |
| `docker-compose.yml` | 无 | ❌ | **仅存在于服务器**，见下方「已知问题」 |

`backend.env` 当前约 49 个键，覆盖：LLM（`OPENBOX_MODEL/API_KEY/BASE_URL`）、无影沙箱
（`WUYING_*`，约 20 个）、Logto（`LOGTO_*`）、OSS、Blob、DB/Redis、JWT、Tavily 等。

### compose 注入的环境变量（覆盖 `backend.env` 同名键）

`docker-compose.yml` 的 `environment:` 优先级高于 `env_file:`，这几个键以 compose 为准：

```yaml
DATABASE_URL: postgresql+asyncpg://openbox:${OPENBOX_DB_PASSWORD}@postgres:5432/openbox
REDIS_URL:    redis://redis:6379/0
WUYING_ENDPOINT: http://host.docker.internal:18001
LOGTO_REDIRECT_URI:             https://ai.ueejavelin.org/callback   # 生产为 ai.bossipai.com.cn
LOGTO_POST_LOGOUT_REDIRECT_URI: https://ai.ueejavelin.org            # 生产为 ai.bossipai.com.cn
```

改 Logto 的 **endpoint / app id / secret** 要动 `config/backend.env`；
改**回调地址**要动 `docker-compose.yml`。

## 三、发布流程

后端容器启动命令是 `alembic upgrade head && uvicorn ...`，**迁移在启动时自动执行**。

### AWS 开发机（有源码，就地构建）

```bash
cd /opt/openbox/src && git pull
docker build -f backend/Dockerfile -t openbox-backend:<TAG> .
docker build -t openbox-frontend-v2:<TAG> frontend-v2/
cd /opt/openbox && sed -i "s/^OPENBOX_IMAGE_TAG=.*/OPENBOX_IMAGE_TAG=<TAG>/" .env
docker compose up -d
```

### 阿里云生产机（无源码，镜像装载）

生产机**没有登录任何镜像仓库**，镜像靠 `docker save` → 传输 → `docker load`。
Tag 规则：`<日期>-<批次>-<git短sha>`，例如 `20260904-a2-d9d7401`。

本地构建须指定 `linux/amd64`（Mac 是 arm64，服务器是 x86_64）：

```bash
docker build --platform linux/amd64 -f backend/Dockerfile -t openbox-backend:<TAG> .
docker build --platform linux/amd64 -t openbox-frontend-v2:<TAG> frontend-v2/
docker save openbox-backend:<TAG>     | gzip -1 > backend.tgz
docker save openbox-frontend-v2:<TAG> | gzip -1 > frontend.tgz
```

生产机 22 端口只放行构建机 `54.254.36.226`，本地无法直接 scp。可走私有 OSS 中转
（同区 `cn-shanghai` 桶 + 内网端点，快且免流量），服务器端不留任何凭证：

```bash
aliyun ossutil cp -f backend.tgz oss://<私有桶>/_deploy-tmp/<TAG>/backend.tgz --acl private
aliyun ossutil presign oss://<私有桶>/_deploy-tmp/<TAG>/backend.tgz \
  --expires-duration 2h -e https://oss-cn-shanghai-internal.aliyuncs.com
```

然后在生产机（云助手）执行：

```bash
curl -sS -o /tmp/b.tgz '<预签名URL>' && gunzip -c /tmp/b.tgz | docker load && rm -f /tmp/b.tgz
cd /opt/openbox
cp config/backend.env config/backend.env.bak-$(date +%Y%m%d%H%M%S)
cp .env .env.bak-$(date +%Y%m%d%H%M%S)
sed -i "s/^OPENBOX_IMAGE_TAG=.*/OPENBOX_IMAGE_TAG=<TAG>/" .env
docker compose up -d --no-deps <实际变更的服务>
```

**用完请删除 OSS 临时对象。**

### 发布可用性要求

当前 gw2 是单机 Compose：frontend 独占宿主机 `:80`，backend 也只有一个实例。因此
`docker compose up -d` 同时替换 backend/frontend 时一定存在无 upstream 的窗口，公网会
短暂 502。2026-09-07 10:22 已实际发生一次。发布必须遵守：

1. **禁止例行发布直接执行裸 `docker compose up -d`。** postgres/redis 不得跟随应用
   发布重建，只允许显式指定真正变更的服务。
2. 仅后端变更时执行
   `docker compose up -d --no-deps backend`，轮询 backend health 到 healthy 后再做公网 API
   验证；不要因为统一 tag 而重建内容未变化的 frontend。
3. 仅前端变更时只替换 frontend。当前固定 `:80` 的单实例拓扑仍会有短暂断档，发布前应
   明确维护窗口；替换后必须等 frontend healthy，再验证公网首页与静态资源。
4. 前后端都变更时按 **backend → 等待 healthy → frontend → 等待 healthy** 串行切换，
   不得同时重建。该做法只能缩短并隔离断档，不能实现真正零停机。
5. 每次切换前创建配置与数据库的版本化备份，并记录旧 tag；切换期间持续探测首页和一个
   API，任一服务未在预期时间内 healthy 就立即恢复旧 tag。

彻底解决方案是把 gw2 入口改为**宿主机稳定 Nginx + 蓝绿应用端口**（或迁移到支持至少
2 个副本滚动更新的编排平台）：新版本先在备用端口启动并通过 health check，再原子切换
upstream、reload Nginx，最后停止旧版本。完成蓝绿前，前端发布不能宣称零停机。

### 验证

```bash
docker compose ps                       # backend / frontend 应为 healthy
curl -s -o /dev/null -w "%{http_code}\n" https://ai.bossipai.com.cn/
curl -s https://ai.bossipai.com.cn/api/auth/logto/config | jq
```

### 回滚

旧镜像会保留在机器上，回滚只需改回 tag：

```bash
cd /opt/openbox
cp config/backend.env.bak-<戳> config/backend.env    # 若改过配置
sed -i "s/^OPENBOX_IMAGE_TAG=.*/OPENBOX_IMAGE_TAG=<上一个TAG>/" .env
docker compose up -d
```

## 四、已知问题

### 1. 生产 compose 未纳入版本管理

`/opt/openbox/docker-compose.yml` 与 `docker-compose.override.yml` 只存在于两台服务器上。
仓库根目录的 `docker-compose.yml` 是**本地开发用**的另一份（从源码 build、挂
`docker.sock`、用的还是旧版 `frontend/`），**不是线上那份**，不要混用。

### 2. 生产代码没有对应的 git 提交（2026-09-04 发现）

生产库的 alembic 版本停在 `f7b9d1e3a5c8`，而该 revision 及以下 5 个迁移
在本地、GitHub 两个远程、AWS 构建机的 checkout 中**均不存在**：

```
c3e5f7a9b1d4_workspace_core            e6a8c0d2f4b7_workspace_cloud_desktops
d4f6a8b0c2e5_workspace_business_scope  c1d3e5f7a9b2_add_desktop_channels
f7b9d1e3a5c8_prepaid_desktop_metadata
```

镜像 tag 里的 `d9d7401`（以及 `bf568d0`/`e9ec7ff`/`f0b7540`/`a789456`）都不是任何
仓库中的提交。**该版本代码从未入库，唯一副本在镜像内。**

后果：直接用 `main` 构建并部署会导致 `alembic upgrade head` 报
`Can't locate revision identified by 'f7b9d1e3a5c8'` 而无法启动；即使能启动，也会
造成 workspace/云桌面模块的功能回退。

恢复办法（backend 的 Dockerfile 是 `COPY . .`，源码在镜像 `/app` 内）：

```bash
docker create --name recover openbox-backend:20260904-a2-d9d7401
docker cp recover:/app ./recovered-src
docker rm recover
```

**在把这份代码整理入库之前，不要从本地向生产发布。**

> **2026-09-04 更新**：上述 5 个迁移与对应代码已随 `main`（`b1f77ae`）入库并推到 GitHub；生产 gw2 已用 `main` 构建的 `20260904-main-b1f77ae` 部署，库升到 `c3e5a7b9d1f4`。本条已解决，可直接从 `main` 构建发布。构建走 AWS 机的 `/opt/openbox/build-main`（`git worktree`，不动 `src/` 的本地改动），传输用 `docker save | gzip | ssh gw2 docker load`（EC2 的 `/root/.ssh/gw2ship`）。
