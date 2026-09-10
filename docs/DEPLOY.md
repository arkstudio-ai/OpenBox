# 部署说明（AWS 开发环境 / 阿里云生产环境）

两套线上环境跑的是**同一份 compose 与同一组配置文件**，只有域名、端口绑定和
少数 env 值不同。本文说明各自的拓扑、需要哪些配置文件、以及怎么发布与回滚。

Logto SSO 的取值另见 [LOGTO_PROD.md](LOGTO_PROD.md)。

## 当前两边前端：2026-09-10 22:39 `20260910-fe-928a228`（仅前端，为验证自动换新）

- 源码 `main@928a228`（与线上 `compact-loading-b3fa657` 同代码，仅 build id 不同），EC2 构建、scp 到 gw2，SHA-256 `347be307…` 两边一致。
- gw2 与 AWS 各只替换 frontend，12s healthy；备份 `backups/20260910-fe-928a228/activation-20260910T1439{37,39}Z/`。后端仍 `20260910-takeover-3db71bc`。
- 目的：让 PR #24 的"发布后旧页面自动换新"可以现在验证——发布前打开的页签（build id `b3fa657…`）在切回/聚焦后应自行换到 `20260910-fe-928a228`。
  发布后用浏览器实测：隐藏页签触发 focus 后自动刷新到新 id，无错误页。

## 当前两边发布：2026-09-10 风控/验证码接管（PR #25）+ 桌面运行时 20260910.2

- 22:33–22:35（北京时间）gw2 与 AWS 切到 `20260910-takeover-3db71bc`（backend + frontend），源码 `main@3db71bc`
  = `4a7a740`（andrew 的 admin-notifications，含 `b83ce59`/`1e3a0bf`，切换前线上正是这两个镜像）+ PR #25 合并 `a618840` + `RUNTIME_VERSION` 20260910.1→20260910.2。
  合并前：后端相关 531 项、前端 533 项、`flutter analyze` 无问题、`desktop_takeover_detail_test`/`question_dock_test` 35 项全过；失败项与既有基线一致。
- 无迁移（仍 `e4f6a8b0c2d4`）。gw2 backend 16s / frontend 12s healthy，切换时无活动会话；AWS 12s/12s。
  备份 gw2 `backups/20260910-takeover-3db71bc/activation-20260910T133330Z/`，AWS `…/activation-20260910T133252Z/`。
- **桌面运行时下发**：PR #25 改了 `container/dev-browser/{SKILL.md,src/client.ts}`，桌面 `--check` 只对本机 bundle 比对，必须 bump 版本才会重装。
  用合并后 checkout 的 `runtime_cloud_commands()` 生成 10 段脚本，云助手对生产库 14 台桌面（9 台已分配 + 5 台 prewarm，含 v4 镜像的 002）
  逐段逐台执行（`aliyun ecd run-command` 一次只能带一台，多台会报 InvalidDesktopId），22:38 起约 9 分钟，
  下发前全部 `20260910.1 ready`，下发后全部 `20260910.2 ready`；运营桌面上 SKILL.md 含 captcha 一节、client.ts 含 `detectChallenge`。
  驱动脚本见本次会话 scratchpad `fleet_push.py`。**金镜像 v4（`m-13pauczcu9swanxwn`）仍是 20260910.1**：新建桌面首次用浏览器时后端会自动修复到 .2，做 v5 时带上。
- 公网 `/`、`/api/auth/logto/config` 200，`index.html` 的 app-build 为 `20260910-takeover-3db71bc`。
- 回滚：override 两行 image 改回 `20260910-admin-notifications-b83ce59` / `20260910-admin-notifications-1e3a0bf`（gw2）；
  桌面运行时无需回退（老后端会自行修回其版本）。

## 当前两边发布：2026-09-10 运营问题三合一（发布链路 / 前端自动换新 / 禁画中画）

- 21:53–21:55（北京时间）**gw2 与 AWS 同时**切到 `20260910-ops-57e330b`（backend + frontend），源码 `main@57e330b`
  = `eda8b75`（andrew 20:38 发的 suggestion-loading）+ PR #23 发布链路修复 + PR #24 前端 build id 自动换新 + PR #26 禁画中画。
  EC2 `/opt/openbox/build-main` 构建，`scp`（`/root/.ssh/gw2ship`，21s）到 gw2 `releases/20260910-ops-57e330b/`，SHA-256
  两边核对一致（backend `e9a29f9b…`、frontend `0bbaffe7…`）。前端镜像以 `--build-arg VITE_BUILD_ID=20260910-ops-57e330b` 钉 id，
  公网 `index.html` 已带 `<meta name="app-build" content="20260910-ops-57e330b">`。
- gw2：无迁移（仍 `e4f6a8b0c2d4`），backend 16s healthy → frontend 12s healthy，串行切换；切换时无活动会话。
  备份 `/opt/openbox/backups/20260910-ops-57e330b/activation-20260910T125343Z/`（.env、backend.env、两份 compose、`preflight.dump` 55 张有数据表）。
  override 两行 image 与 `.env` 的 `OPENBOX_IMAGE_TAG` 均已改到本 tag（此前 .env 仍停在 `20260908-a5fix-7c891ee`）。
- AWS：从 `20260908-a5fix-7c891ee` 直接升到本 tag，启动时迁移 `b8e3f5a7c9d1 → e4f6a8b0c2d4` 自动跑完，backend 12s healthy；
  备份 `backups/20260910-ops-57e330b/activation-20260910T125313Z/`。AWS 未配移动推送与抖音键，属既有状态。
- 公网验证：`/`、`/index.html`、`/api/auth/logto/config` 均 200；镜像内 `desktop_script.py` 含 `DOM.setFileInputFiles`，技能目录含
  `douyin-desktop-publish`，前端 assets 含 `picture-in-picture 'none'`。
- **注意**：本次是前端首次带 build id，已打开的旧页签这一次仍要手动刷新一次；从下一次前端发布起才会自动换新。
- 回滚：override 两行 image 改回 `20260910-suggestion-loading-eda8b75`（gw2）/ `20260908-a5fix-7c891ee`（AWS，需先 `alembic downgrade b8e3f5a7c9d1`），
  `docker compose up -d --no-deps backend` 再 `frontend`。发布用的通用脚本见本次会话 scratchpad `deploy_common.sh`（备份→装载→改 override→
  backend 等 healthy→frontend 等 healthy→本地探测），未入库。

## 当前阿里云前端：2026-09-10 聊天下一步建议（仅前端，无迁移）

- 19:28:33–19:29:18（北京时间）frontend 发布 `20260910-suggestions-1c46bce`，源码 `1c46bce`。
  已合入 `fe-nav-profile`，保留线上导航并补齐建议展示；用户原对话刷新后输入框上方显示 3 条建议。
- 修正严格权限构建目录造成的静态资源 403；496 项前端测试、9 项 Chromium 建议回归、实际镜像
  nginx 回归通过；公网 106 个文件 SHA-256 匹配，浏览器无控制台错误。
- 后端维持 `20260910-mobile-push-0513dcb`，backend/postgres/redis 容器与运行配置未变，
  四服务 healthy；无迁移，数据库仍为 `e4f6a8b0c2d4`，推送密钥及其他配置保持原值。
- 单实例切换采样约 30.3 秒 502 后恢复，后续样本均 200；OSS 临时对象已清理。
  备份、镜像指纹、验收与回滚见 [完整发布记录](FRONTEND_SUGGESTIONS_DEPLOY_20260910.md)。

## 当前阿里云后端发布：2026-09-10 移动端通知

- 17:56（北京时间）后端已发布 `openbox-backend:20260910-mobile-push-0513dcb`，
  本地 Docker 构建 `linux/amd64`，基于 `main@0513dcb` 并保留下文登录指引的三处现网修复。
- 用户授权中断当时 3 条运行任务后完成切换。数据库备份、独立副本迁移预演通过，
  正式库从 `d2f4a6c8e0b2` 升至 `e4f6a8b0c2d4`，新增五张移动通知表。
- 极光与 APNs 使用本地凭据，新增六项推送环境变量及苹果私钥只读挂载；其余配置按键值
  校验一致。前端继续运行 `20260910-nav-3791e77`，frontend、postgres、redis 容器 ID 未变。
- 四服务 healthy，公网首页和环境 API 正常，匿名通知 API 返回 401；未做真机送达测试。
  切换期间 API 曾短暂返回 502，17:55:58 的采样已恢复 200。
- [完整发布与回滚记录](MOBILE_PUSH_DEPLOY_20260910.md)；
  [验收证据](evidence/mobile-push-deploy-20260910.json)。后续全量发布仍须保留三处登录指引修复。

## 当前阿里云基础镜像：2026-09-10 v4 制作与还原验证（默认新建配置尚未切换）

- 新镜像 `m-13pauczcu9swanxwn`，名称 `openbox-image-v4-40g-shanghai-20260910`，
  上海地域，40 GiB 系统盘，Linux Ubuntu 22.04.5；16:39（北京时间）状态为 `Available`。
  源机为未分配的 `bossip-sh-002`（`ecd-5pvbuskezql1d4h5m`），制作前置为 reserve，
  清理浏览器资料、工作区、实例凭据及主机密钥；使用 `DiskType=SYSTEM` 和
  `AutoCleanUserdata=true`。制作前完整镜像检查通过。
- 镜像内运行环境 `20260910.1`，dev-browser 已包含本页下节的登录指引修改。
  Python 3.10.12、Node v22.23.1、Chrome 151.0.7922.71；核心执行服务与技能文件
  和制作前源机一致。源机公共软件此前已与用户 e 正在使用的 012 核对一致。
- 用新镜像实际还原 002 并验证生产执行通道、Chrome CDP 和浏览器 relay，均通过。
  未连接云桌面客户端时没有 X 会话，与现有未分配备用机一致；本次验证的是生产支持的
  headless 模式，未实际连接客户端验证交互式 1920×1080 桌面，显示守护配置检查通过。
  还原后平台新生成的 SSH 身份在测试结束后清理；再次运行完整检查全部通过，
  1456 个基线包齐全，无用户文件、浏览器 Cookie 数据库或实例凭据残留。
- 16:54 将 002 恢复为 prewarm，云标签和数据库的镜像 ID 均为新镜像；备用池恢复
  5 台，已分配 9 台，用户 e 的执行通道仍为 up。本次没有新购桌面或重启后端。
- **全局 `WUYING_IMAGE_ID` 仍为 `m-ihn7zmzukytina8qj`（v3），尚未切换。**
  新镜像现已可供后续新建或重建使用；标准新建流程仍会先同步后端随包技能。
  下节三处源码修改已随本次代码提交纳入 main，后续全量后端构建可直接使用 main。
- [制作和验收证据](evidence/golden-image-v4-20260910.json)；服务器元数据与对应源码
  保存在 `/opt/openbox/releases/image-v4-20260910/`，原 v3 镜像保留。

## 当前阿里云发布：2026-09-10 登录限制移除（后端与 14 台云电脑）

- 用户确认对生产环境所有用户统一移除 dev-browser 和共用 `desktop_login` 中的三项
  指令限制：禁止自行打开登录页、代填密码或验证码、通过截图判断登录态；强制
  `status → open → probe` 顺序改为可独立使用的辅助操作。登录检测与身份归属代码未变。
- 15:59:33–16:00:00（北京时间）backend 切换至
  `openbox-backend:20260910-login-guidance-324366c2`，健康检查通过。基于现场运行的
  `20260910-autopilot-824b295` 追加 `tool/desktop_login.py`、dev-browser `SKILL.md` 和
  `sandbox/browser_runtime_repair.py` 三个文件，原文件哈希均与本地修改前版本一致。
  此为增量镜像，下次全量发布须包含这三处源码变更。
  镜像 ID `sha256:7ae71962406dc008d736db55b6e52aeba84402bc6acec22cea6e61192e922342`。
- 生产库内 14 台桌面全部同步技能源包并通过 `--check`，运行环境版本升至 `20260910.1`，
  确保旧镜像新建的桌面也会加载新技能。技能 SHA-256 全部为
  `44d2cbe460fdaedda326f41f21f9ed3b0950ef7a57bbef04155ba43ce8217cb2`。
  16:02 从生产后端经真实通道读取 9 台已分配桌面的 `/skills/dev-browser`，均返回新版；
  包含用户 e 的 `ecd-b9oizzx4rfhbsm1uh`，来源均为 builtin。
- 验证：技能格式校验通过；desktop_login_tool、browser_runtime、browser_runtime_repair
  共 **57 passed**；公网首页与 `/api/environment` 为 200。数据库仍为 `d2f4a6c8e0b2`，
  frontend `20260910-nav-3791e77`、postgres、redis 的容器 ID 未变，环境变量键值一致。
- 首次切换因按列表顺序比较环境变量而触发自动回滚；改为按键和值比较后重试通过，
  确认仅排列顺序变化。最终备份目录
  `/opt/openbox/backups/20260910-login-guidance-324366c2/activation-20260910T075933Z/`
  含经 `pg_restore -l` 验证的数据库备份、配置和实际运行环境快照；发布文件与源码补丁在
  `/opt/openbox/releases/20260910-login-guidance-324366c2/`。
  桌面备份与验证明细见 [发布证据](evidence/login-guidance-20260910.json)。
- 已有对话里加载过的旧技能文本仍属于历史上下文；重新加载技能或开启新对话即可使用新指引。
- 新增备用机默认使用 `m-ihn7zmzukytina8qj`（`openbox-image-v3-40g-shanghai`，
  阿里云状态 `Available`）；本次后端发布时未重建基础镜像，稍后的 v4 制作见上节。
  生产 `ensure_prewarm` 会先执行
  `verify_prewarm` → `ensure_desktop_browser_runtime`，更新至当前后端随包版本并校验，
  成功后才建立可分配的 prewarm 记录。手动绕过此流程克隆的桌面不能视为已更新。
- 16:10:08 复核时发现后续发布短暂切换到 `20260910-fixes-0351b90`，其运行环境仍为
  `20260909.2` 且技能源包为旧版；随后线上恢复到本节的登录指引镜像，容器启动时间
  16:10:43。16:13 再次通过线上代码检查全部 14 台（含 5 台备用），均为 `20260910.1`
  且 ready，检查期间后端版本稳定。后续发布必须纳入本节三处源码修改，防止再次覆盖。
  本次后端发布未新购桌面；稍后的 v4 制作使用已有备用机完成实际还原与通道验收，见上节。

## 当前默认模型：2026-09-10 Gemini 3.8 Flash

- AWS 开发环境与阿里云生产环境的默认模型均已改为 `Gemini 3.8 Flash`，菜单顺序统一为：
  `Gemini 3.8 Flash`、`GPT-5.6 Luna`、`DeepSeek V4 Pro`、`DeepSeek V4 Flash`、
  `Qwen3.8 Max`、`Qwen3.8 Flash`。MCP filter 仍使用 Luna，避免改变内部工具筛选行为。
- 自建 new-api 对外公开友好 ID `gemini-3.8-flash`，channel 116 映射到上游精确 ID
  `gemini-3.8-flash-high`；暂沿用 Gemini Flash 文本占位价 `ModelRatio=0.4`、
  `CompletionRatio=3`。OpenBox 价目表也将 3.8 暂映射到已核验的 3.7 Flash 档位。
- 两边 backend 均为 healthy；分别从 AWS 与 gw2 使用各自实际 provider 配置发起小请求，
  均返回 HTTP 200、模型 `gemini-3.8-flash`、内容 `OK`。new-api 日志对应记录为 token
  `openbox-shared`、channel 116，并确认发生了到 `gemini-3.8-flash-high` 的模型映射。
- AWS 变更前配置备份：
  `/opt/openbox/backups/20260910-gemini38-default/20260910080804/`；gw2 变更前配置与数据库备份：
  `/opt/openbox/backups/20260910-gemini38-default/20260910161023/`；gw-1 的 channel 116 与
  options 备份：`/opt/bossip/backups/newapi-gemini38-20260910160430/`。

## 历史阿里云状态：2026-09-10 16:08 F 修复发布后回滚到同事镜像（main 尚未包含同事的发布）

- 16:08:03–16:08:23（北京时间）gw2 backend 切换至 `20260910-fixes-0351b90`（`main@0351b90`，PR [#21](https://github.com/arkstudio-ai/OpenBox/pull/21) F 首轮验收修复，无迁移），
  healthy、容器内烟测通过（锁定校验拒绝 480p/换模型、预算自动预留、档位分辨率按能力表换算为 1080p 并给出 note、cron 默认时区 Asia/Shanghai）。
- **随即发现切换前线上跑的是同事的 `20260910-login-guidance-324366c2`（15:46 构建）与 frontend `20260910-nav-3791e77`，两者的源提交都不在 `origin/main`**
  （backend 提交本地/远端都找不到，是未推送分支；frontend 在 `origin/fe-nav-profile` 未合并）。main 不是线上超集，我的发布覆盖了同事的后端改动。
- 16:10:41–16:11:02 **回滚**：backend 改回 `openbox-backend:20260910-login-guidance-324366c2`（服务器上仍有该镜像），healthy，`alembic current` 仍 `d2f4a6c8e0b2`，公网采样 200；frontend 未动。
  镜像指纹对比：同事镜像相对 main 只差 `agent/loop.py`、`core/config.py`、`tool/desktop_login.py` 三个文件（他们的改动），且缺 main 上另一位同事已合的 `agent/suggestions*.py`——两边互不是超集。
- **后果**：PR #21 的 F 修复（工具层锁定/预算强制、中断处理、时区）已在 main 但**不在 gw2**。待同事把 `login-guidance` 分支推送并合入 main 后，从 main 重新构建发布即可（无迁移）。
- 备份 `/opt/openbox/backups/20260910-fixes-0351b90/activation-20260910T080755Z/`；镜像包 `releases/20260910-fixes-0351b90/`；OSS 中转对象已删。
- 教训（重申）：**发布前必须当场核对 `docker-compose.override.yml` 里的 image 源提交是否在 origin/main**，不能依赖几小时前的检查；不在就先停，找到分支合并后再发。

## 历史阿里云发布：2026-09-10 前端导航——新对话即主页、各中心可返回（仅前端，无迁移）

- 14:57:05–14:57:26（北京时间）gw2 frontend 切换至 `20260910-nav-3791e77`，源码为分支 `fe-nav-profile@3791e77`
  （PR [#20](https://github.com/arkstudio-ai/OpenBox/pull/20)，= `main@7b884b1` 合并进该分支后的提交；**PR 尚未合并**，
  线上前端是 main 的超集）。backend 保持 `20260910-autopilot-824b295`，postgres/redis 未动；无迁移。frontend 21 秒 healthy。
  **本次未部署 AWS。**
- 内容：侧栏第一行改回「新对话」（落当前选中项目），「新建项目」降为第二行；logo 可点回 `/app`；六个中心行进入时高亮；
  顶栏按路由识别页面（补技能中心、超管系统），主页显示「新对话 · 项目名」，所有中心页出「返回对话」
  （回最近打开且仍存在的会话，否则回 `/app`）；`/app` 的项目归属改为 URL → 侧栏当前项目 → 未归类，问候语显示项目名。
  规划见 `docs/FRONTEND_NAV_PROFILE_PLAN.md`。
- 本地 `git archive 3791e77` 干净导出，`docker build --platform linux/amd64 --build-arg NGINX_IMAGE=nginx:1.31.3-alpine frontend-v2/`
  （线上 nginx 1.31.3，基础镜像同版）。包 SHA-256 `3b78c895d0cb87fa204da7e6d5cabc4341470e5ca9ddc2b297169e351263188e`，
  image ID `sha256:e79db1de0a3ce7b9703a5cea4491c20fd2e6b360bcefd66e5599a6babf06b0e4`，服务器装载后一致。
  OSS 中转走 `oss://bossip/_deploy-tmp/<TAG>/`，**`aliyun ossutil` 的 cp / presign 必须带 `--region cn-shanghai`**
  （CLI 默认区是 cn-hangzhou，V4 签名带错区域会 403）；预签名 URL 只对 GET 有效，HEAD 会 403，探活用 `curl -r 0-0`。中转对象已删。
- 切换前本机 loopback 金丝雀：首页 200、hash 资源 200、不存在的资源 404、`/app/skills` 200、镜像内含新文案。
  切换前 0 个 busy 会话。备份 `/opt/openbox/backups/20260910-nav-3791e77/activation-20260910T065703Z/`
  （0700；`preflight.dump` 经 `pg_restore -l` 校验；配置、compose、`old_images.txt`）；镜像包在 `releases/20260910-nav-3791e77/`。
  只改 override 的 frontend image（`.bak-20260910T065703Z`）。
- 切换期间公网每秒采样 102 次，首页与 `/api/environment` 全部 200，本次未捕获断档（单实例替换仍不承诺零瞬断）。
  切换后公网 **106 个文件逐一 SHA-256 与镜像一致**，首页字节一致，SPA 路由 200、缺失资源 404、环境标识 `prod`；
  浏览器打开生产工作台顶栏显示「新对话 · 默认空间」，无控制台错误。Web `npm run check` 466 项通过。
- 回滚：override 的 frontend image 改回 `openbox-frontend-v2:20260910-trends-7959132`，`docker compose up -d --no-deps frontend`；无需恢复数据库。

## 历史阿里云发布：2026-09-10 自动营销 D 阶段——模版预算授权 + marketing-autopilot 技能（仅后端，含迁移）

- 14:46:46–14:47:05（北京时间）gw2 backend 切换至 `20260910-autopilot-824b295`，源码 `main@824b295`
  （PR [#17](https://github.com/arkstudio-ai/OpenBox/pull/17) D1/B4 + PR [#18](https://github.com/arkstudio-ai/OpenBox/pull/18) D2/D3/D4）。
  **含迁移** `d2f4a6c8e0b2`（`cron_jobs.template`），镜像内 `ScriptDirectory.get_heads()` 单 head 后才发；`alembic current` = `d2f4a6c8e0b2 (head)`。
  backend 19 秒 healthy；frontend 保持 `20260910-trends-7959132`；公网 5 组采样全 200。
- 内容：`AutopilotTemplate` 模版（预算上限/条数/三档模型/容差/发布方式/形态/黑名单）落 `cron_jobs.template` 并严格校验，执行器注入「模版参数（预算授权）」块；
  `autopilot_run` 工具（预算 reserve 超限即终止、judge_shot 按容差判定、report 读账单）；技能 `marketing-autopilot`（定时无卡 / 对话选档卡）；`references/recipes.md` 七种形态配方。
- 包 SHA-256 `0f7274903af1d5efd1083271e95ce3297cada6b9446c4e3d2ff336ad7d2e2624`，image ID `sha256:3e43b6629d418311b89357c92e767bdeddab627956a16d723bda2ab0f9f8bb74`，服务器装载后一致；OSS 中转对象已删。
  备份 `/opt/openbox/backups/20260910-autopilot-824b295/activation-20260910T064637Z/`（0700；`preflight.dump` 经 `pg_restore -l` 校验）。
- 容器内验收：38 个工具含 `autopilot_run`；7 个技能含 `marketing-autopilot`；`tiers` 三档参考价；`start` 由模版定 `wan3.0-video@720p`、可负担 1 条；
  `reserve 27` 允许、`reserve 9` 拒绝并给出终止说明；`report` 生成含预算到顶提示；`validate_template` 拒绝非法档位。
  **真实端到端一次自动营销运行（F）未做。**
- 回滚：`docker compose run --rm --no-deps --entrypoint alembic backend downgrade b8d0f2a4c6e8`，再把 override 的 backend image 改回
  `openbox-backend:20260910-publish2-2e44c01`，`up -d --no-deps backend`。降级只丢 `cron_jobs.template` 一列。

## 历史阿里云发布：2026-09-10 云电脑自动发布 desktop_publish（仅后端，含迁移；第一次尝试自动回滚）

- 01:23:12–01:23:32（北京时间）gw2 backend 切换至 `20260910-publish2-2e44c01`，源码 `main@2e44c01`
  （PR [#13](https://github.com/arkstudio-ai/OpenBox/pull/13) desktop_publish + PR [#14](https://github.com/arkstudio-ai/OpenBox/pull/14) 迁移 id 修正）。
  **含迁移** `b8d0f2a4c6e8`（`publish_jobs.details`、`platform_accounts.auto_publish_disabled_at/_reason`），`alembic current` = `b8d0f2a4c6e8 (head)`。
  frontend 保持 `20260910-trends-7959132`，postgres/redis 未动。backend 20 秒 healthy，公网 5 组采样全 200。
- **第一次尝试失败并自动回滚**（01:16:23–01:19:28，镜像 `20260910-publish-2334751`，`main@2334751`）：容器启动 `alembic upgrade head` 报
  `Revision a6c8e0f2b4d6 is present more than once` / 多 head——新迁移随手取的 id 与既有 `a6c8e0f2b4d6_media_gen_routing_dedupe.py` 重复。
  健康检查 3 分钟未过 → 脚本按预案回滚：镜像回 `20260910-trends-7959132`，库始终停在 `f2a4c6e8b0d3`（升级未执行，无 schema 变更），
  回滚期间约 3 分钟后端不可用。补救：改 id 为 `b8d0f2a4c6e8`，镜像内 `ScriptDirectory.get_heads()` 单 head 后再发；
  新增单测 `tests/unit/test_migration_heads.py`（PR [#15](https://github.com/arkstudio-ai/OpenBox/pull/15)）拦截重复 id / 多 head。
  **教训：新迁移发布前必须先看 `alembic heads`。**
- 内容：`desktop_publish` 工具 + `douyin-desktop-publish` 技能——用云电脑上已登录的创作者中心自动发布；mode 三层优先级（账号熔断 > 请求 > `desktop_publish.default_mode`）、
  每账号每日上限/最小间隔/发布时段、风控词熔断 + 站内通知 + 降级到投稿包；每次尝试记 `publish_jobs(platform=douyin_creator)`。
- 包 SHA-256 `f38fcd91e2d26f63115ba2929c2421f9f663e8603e0180891bcf05ecc393bb4a`，image ID 与本机一致（见 `releases/20260910-publish2-2e44c01/`）。
  两次尝试的 OSS 中转对象均已删除。备份 `/opt/openbox/backups/20260910-publish-2334751/activation-20260909T171616Z/` 与
  `/opt/openbox/backups/20260910-publish2-2e44c01/activation-20260909T172304Z/`（0700；`preflight.dump` 经 `pg_restore -l` 校验）。
- 容器内真机验收（管理员工作空间，走 action-server 路由）：`precheck` 正确判定登录 ok、`mode=auto`、01:24 不在发布时段 → `can_auto_publish=false`；
  `publish dry_run=true` 26.7 秒完成——把 14.4 秒 IMS 成片投递到云电脑、桌面 Chrome 填表（AI 声明、仅自己可见）、截图、暂存离开，记 `status=draft`；
  `status` 列出该记录。脚本级真机：两次「仅自己可见」真实发布成功（回读作品 id）、假验证码降级演练命中。
- 回滚：**先降迁移再换镜像**——`docker compose run --rm --no-deps --entrypoint alembic backend downgrade f2a4c6e8b0d3`，
  再把 override 的 backend image 改回 `openbox-backend:20260910-trends-7959132`，`up -d --no-deps backend`。降级只丢两列/一列附加字段。

## 历史阿里云发布：2026-09-10 热点采集工具 hot_trends（前后端，含迁移）

- 00:43:42–00:44:29（北京时间）gw2 backend → frontend 串行切换至 `20260910-trends-7959132`，源码 `main@7959132`
  （PR [#11](https://github.com/arkstudio-ai/OpenBox/pull/11) 合并提交）。**含迁移** `f2a4c6e8b0d3`（新表 `hot_trend_snapshots`、`hot_media_links`），
  容器启动时 `alembic upgrade head` 自动执行，切换后 `alembic current` = `f2a4c6e8b0d3 (head)`。backend 22 秒 healthy，frontend 23 秒 healthy。
- 内容：平台工具 `hot_trends`（热点宝 / 公开热榜双源、全体客户共享的按日快照、每源限流、按真实采集次数落账 `kind=hot_trends`）、
  `platforms/desktop/service.run_command_on_desktop` 抽出共用、`HotTrendsConfig`；前端账单页补 `hot_trends`/`video_analyze` 词条与「条数」。
- 本地 `git archive 7959132` 干净导出；backend `docker build --platform linux/amd64 -f backend/Dockerfile .`，
  frontend `docker build --platform linux/amd64 --build-arg NGINX_IMAGE=nginx:1.31.3-alpine frontend-v2/`。
  包 SHA-256 backend `6295fbb228e950e2b2f5462434c3c350357d058879dbb3d351527d8e67d9f53f`、frontend `970b68b0639ce19b388f7affd5b2d3b8eeca137ebe48c24734755c002575605a`；
  image ID backend `sha256:06cd321c7453c0502513636d01d7190951ca5f46113008816d010506594b2a62`、frontend `sha256:580dc567996fc23a4dcdff7f5779b7dd55f2b178645ce42ab8ed1f9bcbf480dc`，服务器装载后一致。OSS 中转对象已删。
- 切换前 0 个活动会话。备份 `/opt/openbox/backups/20260910-trends-7959132/activation-20260909T164327Z/`（0700；迁移前 `preflight.dump` 经 `pg_restore -l` 校验；配置、compose、`old_images.txt`）；
  镜像包在 `releases/20260910-trends-7959132/`。只改 override 两条 image。公网首页/API 5 组采样全 200，切换后无 traceback。
- 容器内真机验收（管理员工作空间云桌面）：`sources` 列出两源九榜；`list source=auto` 走热点宝视频总榜 24h，8.4 秒 40 条、`credits=0.2`；
  `list douhot 美食 72h` 8.3 秒 49 条；之后 `sources` 带出 38 个垂类；`resolve` 公开视频页 7.5 秒拿到 douyinvod 直链；`usage_events` 两条 `kind=hot_trends` shadow 0.20。
  已知：无云电脑/未绑热点宝的工作空间 `source=auto` 会落到公开热榜并需要自己的云电脑，尚不能直接读别人采好的热点宝快照（见计划 §7 A3 备注）。
- 回滚：**先降迁移再换镜像**——`docker compose run --rm --no-deps --entrypoint alembic backend downgrade e1f3a5b7c9d2`，
  再把 override 改回 backend `20260909-analyze3-076bf36` / frontend `20260909-direct-video-0a92fd8`，`up -d --no-deps` backend → frontend。
  两张新表只被 hot_trends 写入，降级丢弃的只是缓存。

## 历史阿里云发布：2026-09-10 video_analyze 私有桶预签名 + 重试清错（仅后端，两次）

- 00:00:03–00:00:21（北京时间）gw2 backend 切换至 `20260909-analyze3-076bf36`，源码 `main@076bf36`
  （PR [#10](https://github.com/arkstudio-ai/OpenBox/pull/10) 合并提交）；此前 23:54:56–23:55:12 已切过一版
  `20260909-analyze2-0509757`（`main@0509757`，PR [#9](https://github.com/arkstudio-ai/OpenBox/pull/9)）。
  两次都无迁移，`alembic current` 仍为 `e1f3a5b7c9d2`；frontend 保持 `20260909-direct-video-0a92fd8`，postgres/redis 未动。
- 起因：`20260909-analyze-d054271` 上线后在容器内用管理员真实云桌面跑 `video_analyze`，源为上次 IMS 成片的
  `https://bossip.oss-cn-shanghai.aliyuncs.com/assets/<user>/…mp4`，桌面 ffprobe 拿裸 URL 访问私有桶 → 403。
  PR #9：配置桶的 https / 内网 host / `oss://` 三种写法先鉴权（必须是本人 `assets/<user>/`）再 `presign_get`
  交给 ffmpeg，缓存键仍是未签名对象；桶内他人对象直接拒绝。PR #10：同源先失败后成功复用同一 `video_jobs` 行，
  完成时清掉旧 `error`，输出不再同时出现 `status=completed` 与 `error=`。
- 本地 `git archive` 干净导出，`docker build --platform linux/amd64 -f backend/Dockerfile .`。
  analyze2：包 SHA-256 `d59b7d064b05b6ec11a1a12c6d1e866fc28aba040a399ec7b3f2de8eb3e146b0`，image ID
  `sha256:4908f5821da20eec8f4bb724d8713a158d19a47cb974c0be1bd9c576e09e8d86`；
  analyze3：包 SHA-256 `1317af916029fa125ed29d4405e0cb07611ce9f4e504ed8b9700ba85aa3ce1a5`，image ID
  `sha256:43e6a682d769fa43874db656d8cd953a30feef60ad5710cdabe2e93bf14f2f75`；服务器装载后均与本机一致。
  OSS `_deploy-tmp/` 中转对象已全部删除。
- 两次切换前均 0 个活动会话。备份 `/opt/openbox/backups/20260909-analyze2-0509757/activation-20260909T155449Z/`、
  `/opt/openbox/backups/20260909-analyze3-076bf36/activation-20260909T155956Z/`（0700；`preflight.dump` 经 `pg_restore -l` 校验；
  配置、compose、`old_images.txt`）；镜像包在 `releases/<tag>/`。仅 override 的 backend image 改变。
- 真机验收（容器内构造 ToolContext，走管理员云桌面 sandbox）：14.4 秒成片抽 8 帧、转写 59 字、`gemini-3.7-flash` 拆解
  形式=口播、钩子/文案与原稿一致，耗时约 30 秒；`oss://` 与 https 写法命中同一缓存；`force=True` 新建任务且无 `error=` 行。
  注：临时 ToolContext 的 session 不存在，`UsageMeter.start` 返回 None，故 `credits=0.05` 只含转写；正式会话中视觉调用按
  `kind=video_analyze` 落账。公网首页/API 采样全 200，切换后无 traceback。
- 回滚：override 的 backend image 改回 `openbox-backend:20260909-analyze-d054271`（或再往前 `20260909-media-998219e`），
  `docker compose up -d --no-deps backend`；无需恢复数据库。

## 历史阿里云发布：2026-09-09 热点宝授权站点 + video_analyze 多模态拆解（仅后端）

- 23:37:16–23:37:35（北京时间）gw2 backend 切换至 `20260909-analyze-d054271`，源码 `main@d054271`
  （PR [#8](https://github.com/arkstudio-ai/OpenBox/pull/8) A1+B3 与 PR [#6](https://github.com/arkstudio-ai/OpenBox/pull/6) 首稿确认卡修复
  的合并提交）。无数据库迁移，`alembic current` 仍为 `e1f3a5b7c9d2`。backend 19 秒 healthy。frontend 保持同事发布的
  `20260909-direct-video-0a92fd8`（已核对在 main 上，本次 main 是线上超集），postgres/redis 未动。
- 内容：授权中心新增 `douyin_hot`（抖音热点宝，独立 OAuth 会话，cookie 域 `.douhot.douyin.com`，探针 `user_info` code==0，
  60 天不活跃 TTL）；新工具 `video_analyze`（抽帧 + 转写 → `openai/gemini-3.7-flash` 结构化拆解，缓存到 `video_jobs kind=analyze`，
  按 `video_analyze` 计量；帧数不足 `min_frames` 直接失败而不让模型臆测）；`video-production` skill 把口播长度校对提前到卡 1 之前。
- 本地 `git archive d054271` 干净导出，`docker build --platform linux/amd64 -f backend/Dockerfile .`；中转包 SHA-256
  `5ec10a254fb9ff6f95c254a7fcc661a964686ca8f90140ecaae9cab376b41cf8`，装载后 image ID
  `sha256:e658bd7a8d26d5457d9d6c32e1c98789f50f9296c4b7d5d4695e0b14a66a80ab` 与本机一致。OSS 中转对象已删，`_deploy-tmp/` 为空。
- 切换前 0 个活动会话。备份 `/opt/openbox/backups/20260909-analyze-d054271/activation-20260909T153708Z/`（0700；`preflight.dump`
  经 `pg_restore -l` 校验；配置、compose、`old_images.txt`）；镜像包在 `releases/20260909-analyze-d054271/`。仅 override 的 backend image 改变。
- 容器内验收：35 个工具，含 `video_analyze`、`video_compose`；`video_analysis` 配置 8 帧/720 宽/转写开；站点列表含 `douyin_hot`。
  用管理员云桌面对 bbdwxh_admin 工作区做 level-2 探针，热点宝状态 `bound`（19 个 cookie，会话 cookie 到期 2026-11-08，测试号
  `用户2087843173024`）。公网首页/API 4 组采样全 200，切换后无 traceback。
- 回滚：override 的 backend image 改回 `openbox-backend:20260909-media-998219e`，`docker compose up -d --no-deps backend`；无需恢复数据库。

## 历史阿里云发布：2026-09-09 独立视频附件交付的分段折叠（仅前端）

- 21:04:10–21:04:36（北京时间）gw2 frontend 发布 `20260909-direct-video-0a92fd8`，源码
  `main@0a92fd8` 已推送。修复 `video_generate → share_file → 最终答复` 没有 `video_final`
  标记时不折叠的问题；只对来源匹配且已完成的独立单段交付兜底，不把预览/失败/等待当成成片。
- 保留此前分段在成片上方、手动展开、缺失 chunk 恢复与 nginx DNS 修复。Web 457 项、Flutter
  173 项、Chromium 12 个流程通过。用户授权的真实 Chrome 历史会话刷新后默认折叠，
  桌面/390 px 窄屏手动操作与最终视频预览均通过，验收后恢复浏览器原尺寸。
  Flutter 源码已推送，本次未发布 Android/iOS 安装包。
- 本地干净导出 `git archive 0a92fd8` 构建 `linux/amd64`；镜像级回归、生产 loopback canary、
  前端运行时完整 env 一致性检查通过后仅切换 frontend。backend 保持 `20260909-media-998219e`，
  backend/postgres/redis 的容器与重启计数均未变，四服务 healthy；无迁移。仅 override 的
  frontend image 改变，其余生产配置哈希不变；AWS、Logto、无影云未修改。
- 镜像 ID `sha256:cfa579b5661d2a2fd53969976238b62b5b588dc4f884167b75537cf131ebc267`；
  包 SHA-256 `72a40990c25ef452eb1b500f603e43a5028773a913e17c5fbbcd6930c4208a13`。
  公网 104 个文件逐一匹配镜像，缓存/MIME/缺失资源/生产登录配置检查通过；OSS 临时包已删除。
- 备份 `/opt/openbox/backups/20260909-direct-video-0a92fd8/activation-20260909T130408Z/`，
  含已验证的数据库 dump、配置和激活报告；本地及 `releases/20260909-direct-video-0a92fd8/`
  的镜像包保留。回滚仅恢复前端 `20260909-ui2-4d2a578`，执行 `up -d --no-deps frontend`。
- 切换期间首页/API 各 107 次采样、10 次 502，约 10.8 秒后恢复且后续持续 200；
  详情及仓库已有移动端门禁问题见 [追加 QA 记录](VIDEO_LAYOUT_AND_FRONTEND_RECOVERY_QA_20260909.md)。

## 历史阿里云发布：2026-09-09 视频结果排版与前端资源恢复（仅前端）

- 20:35:34–20:35:59（北京时间）gw2 frontend 切换至 `20260909-ui2-4d2a578`，源码 `main@4d2a578`。
  backend 保持 `20260909-media-998219e`，backend/postgres/redis 容器和重启次数未变，四服务 healthy；无迁移。
  AWS、无影云桌面、Logto 配置均未改；Flutter 源码已推送，但本次没有发布手机安装包。
- Web 分段位于成片上方，仅实际最终视频到达后默认折叠，可手动展开；缺失 JS/CSS 返回 404 而非 SPA HTML，
  HTML no-store、hash 资源 immutable，动态模块错误限次自动刷新并提供手动恢复；nginx 定期重新解析 backend。
- `git archive 4d2a578` 干净导出，本机 `linux/amd64` 构建。最终 nginx 基础镜像固定为 `1.31.3-alpine`，
  与生产运行时环境一致。首次浮动 alpine 拉取到较新版，环境变量保护检查触发自动回滚；记录与旧镜像保留。
  第二次在替换前核对预计环境变量，并用 loopback 临时实例验证，再执行仅 frontend 的切换。
- 镜像 ID `sha256:4b31bd54c1b40efe8ff32473027bf56a419ece9141ef7da062afaca1589b1970`；中转包 SHA-256
  `895fd69949d70f632ef82145589ed4bb4fc534999d023c4892a04f27be39d5d6`。公网首页/静态文件 104 个与镜像逐个一致，
  缺失资源 404/no-store、生产环境/Logto、匿名 me 401 均通过；本次不以匿名检查代替用户登录后的端到端验收。
- 备份 `/opt/openbox/backups/20260909-ui2-4d2a578/activation-20260909T123532Z/`（0700），含已验证的数据库 dump、
  旧配置和 activation.json；镜像包保留在 `releases/20260909-ui2-4d2a578/`。仅 override 的 frontend image 改变。
  两次构建的 OSS 临时中转对象均已删除，镜像与备份保留。
  回滚只改回 `openbox-frontend-v2:20260909-media-998219e`，执行 `docker compose up -d --no-deps frontend`；无需恢复数据库。
- 最终切换 2 分钟采样：首页/API 各 109 次，其中 10 次 502、99 次 200；失败至恢复约 10.8 秒，之后持续正常。
  首次尝试及回滚还各有一次约 11 秒中断，不宣称零停机。完整测试、首次回滚与采样记录见
  [视频排版与前端恢复 QA](VIDEO_LAYOUT_AND_FRONTEND_RECOVERY_QA_20260909.md)。

## 历史阿里云发布：2026-09-09 图片/转写落账 + 账单页四类媒体事件（前后端）

- 20:18:29–20:19:11（北京时间）gw2 backend → frontend 串行切换至 `20260909-media-998219e`，源码 `main@998219e`
  （PR [#5](https://github.com/arkstudio-ai/OpenBox/pull/5) 合并提交）。无数据库迁移，`alembic current` 仍为 `e1f3a5b7c9d2`。
  backend 19 秒 healthy，frontend 23 秒 healthy；切换后公网首页与 `/api/environment` 连续 6 组采样全 200。
- 接替 `20260909-billing-6a193bd`；main 在两次发布之间无他人提交。`config/*`、`.env`、基础 compose 未改，只改 override 两条 image。
- 内容：`image_gen` 每次调用落账 `usage_events(kind=image_gen)`（按张，幂等键 `image:<part_id>`）；`video_transcribe` 完成时按
  `duration_ms` 落账 `kind=video_transcribe`；`rates.json` `media.image-gen` / `media.stt` 占位价；web 账单行媒体类型扩到四种。
  mobile 账单行同批改为按媒体量渲染，但 App 走独立发版，本次未发布。
- 本地 `git archive 998219e` 干净导出构建；中转包 SHA-256 backend `b323571a…de4a464c`、frontend `054b051f…edf385e2`；
  服务器装载后 image ID backend `sha256:29949b2c…369cc5a1`、frontend `sha256:a3dfef87…4676f19e` 与本机一致。OSS 中转对象已删。
- 切换前无运行中会话、无在途视频/转写任务。备份 `/opt/openbox/backups/20260909-media-998219e/activation-20260909T121821Z/`
  （0700；`preflight.dump` 经 `pg_restore -l` 校验；配置、compose、`old_images.txt`）；镜像包在 `releases/20260909-media-998219e/`。
- 容器内验收：34 个工具；四类报价函数 image 0.30 / stt 0.05 / wan3 720p 5s 3.00 / compose 14.4s 0.03；前端 bundle 含「图片生成」词条。
- 回滚：override 两条 image 改回 `20260909-billing-6a193bd`，backend → frontend 串行 `up -d --no-deps`。无迁移。

## 历史阿里云发布：2026-09-09 视频生成落账 + 账单页媒体事件（前后端）

- 19:52:25–19:53:07（北京时间）gw2 **backend → frontend 串行**切换至 `20260909-billing-6a193bd`，源码 `main@6a193bd`
  （PR [#4](https://github.com/arkstudio-ai/OpenBox/pull/4) 合并提交）。无数据库迁移，`alembic current` 仍为 `e1f3a5b7c9d2`。
  backend 19 秒 healthy，frontend 23 秒 healthy；切换后公网首页与 `/api/environment` 连续 6 组采样全部 200
  （切换瞬间的单实例断档窗口未被采样覆盖，不宣称零停机）。
- **接替的是同事的 `20260909-minimax-1bc6743`**（分支 `codex/minimax-video-submit-fix`，未直接合 main）。发布前逐文件核对：
  该分支的全部修复文件与 `main@6a193bd` 逐字节一致（main 上对应 `e4670f6`/`abc0c4d`），main 只多出本次计费与前端改动及文档，
  因此本次发布是线上代码的严格超集，MiniMax 修复未丢。`config/openbox.json` 未改（含同事写入的 MiniMax `size` 配置与 `video_compose` 段）。
- 内容：`video_generate estimate` 返回 `estimated_credits`；片段完成按申请时长落账 `usage_events(kind=video_generate)`；
  `rates.json` `media.video-gen` 价目（上游刊例成本价占位，运营改数即改售价）；账单页 `video_*` 事件显示时长/计费单位/档位。
- 本地 `git archive 6a193bd` 干净导出构建 `linux/amd64`；中转包 SHA-256 backend `89c11d47…f81f925e`、frontend `517ebd39…cfc781f5`；
  服务器装载后 image ID backend `sha256:d07db9d3…6328ec8`、frontend `sha256:a503afae…d5d75d` 与本机一致。OSS 中转对象已删。
- 切换前确认无运行中会话、无在途视频任务。备份 `/opt/openbox/backups/20260909-billing-6a193bd/activation-20260909T115217Z/`
  （0700；`preflight.dump` 经 `pg_restore -l` 校验；配置、compose、`old_images.txt`）；镜像包在 `releases/20260909-billing-6a193bd/`。
- 容器内验收：34 个工具含 `video_compose`；`quote_generation('wan3.0-video','720p',5)=3.00`、`quote_compose(720,1280,14.4)=0.03`；
  MiniMax `wire_shape=size`；前端 bundle `assets/billing-*.js` 含「视频合成」词条。
- 回滚：override 两条 image 改回 `openbox-backend:20260909-minimax-1bc6743` / `openbox-frontend-v2:20260909-admin-skills-d445b9f`
  （见 `old_images.txt`），backend → frontend 串行 `up -d --no-deps`。无迁移，不需恢复数据库。

## 历史阿里云发布：2026-09-09 video_compose（IMS 云端合成 + 合成计费）

- 18:31:16–18:31:34（北京时间）gw2 后端切换至 `20260909-compose-93a6e62`，源码 `feat/video-compose-ims@93a6e62`
  （基于 `main@ac0861c`，PR [#3](https://github.com/arkstudio-ai/OpenBox/pull/3)）；前端继续 `20260909-admin-skills-d445b9f`。
  **无数据库迁移**，`alembic current` 前后均为 `e1f3a5b7c9d2`。只重建 backend，18 秒 healthy。
- 内容：`video_compose` 平台原子工具（自有时间线 → IMS Timeline 编译层，六条规则）、IMS 客户端、补扫、
  合成计费（`rates.json` media 段，validate 报价 / enforce 余额门 / 成功后按实际时长落账 `usage_events.kind=video_compose`）、
  技能第 8 步双路径 + 第四张「合成确认」卡。详见 `docs/VIDEO_RENDER_ENGINE_SELECTION.md`。
- 配置：`config/openbox.json` 新增 `video_compose` 段（region 留空跟 `OSS_REGION=cn-shanghai`），其余
  `backend.env` / `.env` / 基础 compose 未改；只改 override 的 backend image。
- 本地以 `git archive 93a6e62` 干净导出、`docker build --platform linux/amd64 -f backend/Dockerfile .` 构建；
  中转包 SHA-256 `cbf52b043ddfc96904498e4302125b0199b3fa6a934d9b3c8bfc6c31909c3104`，服务器装载后
  image ID `sha256:930af19cd4ae6f08c60d8f30be43ad308f45c823db665b77b50c8bc6fef6b04d` 与本机一致。OSS 中转对象已删。
- 切换前确认无运行中会话、无在途视频任务。备份 `/opt/openbox/backups/20260909-compose-93a6e62/activation-20260909T103107Z/`
  （0700，含 `preflight.dump` 3.3 MB、经 `pg_restore -l` 校验、配置与 compose 文件、`old_image.txt`）；镜像包在
  `releases/20260909-compose-93a6e62/`。
- 容器内验收：34 个内置工具含 `video_compose`；`get_config().video_compose` 读到新段；OSS `bossip/cn-shanghai`；
  用只读 `GetMediaProducingJob` 查历史任务，IMS 自 gw2 可达。`/api/environment` 仍为 `prod`（角标）。
- 用户验收（18:47–18:58，账号 bbdwxh_admin）：两段 720p 生成与 STT 卡正常；合成前 `validate` 报 0.03 积分并出第四张
  「合成确认」卡；点「可以」后才提交，32 秒完成，成片卡与 `credits=0.03` 输出正确；账单新增 `video_compose` 0.03（shadow）。
  已知偏差：视频生成估价无金额（生成侧计费未做，另立任务）；首稿确认卡出现两次（待复现定性）。
- 回滚：override 的 backend image 改回 `openbox-backend:20260909-qwh-56ef0f8`（`old_image.txt`），恢复备份的
  `openbox.json`，`docker compose up -d --no-deps backend`。无迁移，不需恢复数据库。

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
6. 前端镜像应在替换容器**之前**核对预计运行时环境，并通过 loopback 临时实例验证。
   浮动 `nginx:alpine` 可能拉到与线上不同版本；本次不升级运行时时用 `NGINX_IMAGE`
   固定线上版本，不应等容器替换后才发现基础镜像环境变化。

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
