# 视频排版与前端资源故障回归（2026-09-09）

## 变更范围与交付状态

修复 Web（frontend-v2）与 Flutter 的视频结果排版，以及 Web 路由资源加载失败后的恢复。
修复阶段的线上操作仅为只读诊断，没有重启线上服务、修改账号/无影云配置或调用付费生成。
测试重用已有独立验收视频；没有访问其他用户的浏览器会话。

开发在隔离 worktree 完成，再将修复应用到已同步 origin/main 的主工作区。
发布前再次同步至 `491d545`，已包含 `998219e` 的最新图片/转写计费发布，
并在合入后复跑 Web 433 项、Flutter 149 项测试，保留最新功能，不用较旧版本覆盖线上。
用户后续授权发布后，修复已提交并推送至 `main@4d2a578`；阿里云已于 20:35 完成 frontend 发布。
Flutter 源码同步入库，App 使用独立发版流程，本次没有发布 Android/iOS 新安装包。
实际发布结果见本文发布记录与 `docs/DEPLOY.md`。

## 视频显示规则

| 情况 | 分段区域 |
| --- | --- |
| 分段已输出、仍在生成/合成 | 展开，不提供收起按钮 |
| 合成失败、没有最终视频文件 | 保持展开 |
| 只有最终文字、普通文件或图片 | 保持展开 |
| 最终视频仅有占位信息、无资源 ID | 保持展开 |
| 最终视频文件实际挂到结果里 | 位于最终视频上方，默认折叠 |
| 用户手动展开后，普通流式更新/重绘 | 保留展开选择 |
| 新版本最终视频到达 | 新成片对应的分段区域重新默认折叠 |
| 最终视频被移除 | 分段恢复展开 |
| 刷新后读取已完成的历史结果 | 默认折叠，可手动展开 |

判断依据是当前结果中 `video_final` 的视频 MIME 与非空 asset_id，
不依赖 Agent 是否结束或是否已经输出最终文字。没有按文件名猜测成片。
分段按 ordinal 排序，相同/未知序号保持源顺序；旧数据把分段 role 标为 final 时不重复渲染。
Web 使用桌面双列、窄屏单列；两端收起/展开操作区域至少 44 px。
折叠内容不挂载视频预览，加载已完成结果时不会额外请求分段资源。

## 页面崩溃证据与修复

截图包含 `Failed to fetch dynamically imported module`，并显示 JS 请求拿到了 HTML。
线上只读复现：不存在的 `/assets/__openbox_diagnostic_missing__.js` 返回 200、text/html；
首页与 `/app/auth-center` 也没有 Cache-Control。此时前后端容器 healthy，
`GET /api/environment` 返回 200/prod，当前前端容器日志未见持续 upstream 连接失败。
截图中的 API 502 与资源 MIME 错误应区分，不能仅凭截图认定所有 502 的历史原因。

- `/assets/` 不再走 SPA HTML 回退：缺失 JS/CSS 返回 404、text/plain、no-store。
- HTML 入口 no-store/no-cache，带 hash 的静态资源 immutable 缓存。
- 路由遇到明确的动态模块/CSS 加载错误，每个标签页 5 分钟内最多自动刷新一次；
  多个失效 chunk 共用限制，防止无限刷新。当前 path/query/hash 保留，不重新提交业务任务。
- 断网或 sessionStorage 不可用时不自动刷新，提供说明、手动刷新和返回按钮。
  普通 API 502、通用网络异常、代码错误不被误判为旧版本 chunk。
- 离线浏览器测试发现错误页语言包也可能失效，已将两种语言的 common 文案随基础代码加载；
  其他功能语言包仍按需加载，错误页无需再次联网下载文案。
- nginx 通过 Docker DNS 定期重新解析 backend，避免重建后端改变 IP 后继续使用旧地址；
  不吞掉真正的上游 503，也不自动重发业务 POST。

方案依据：[Vite 发布后动态模块失效说明](https://vite.dev/guide/build#load-error-handling)、
[nginx 变量上游与 URI 转发规则](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)、
[nginx DNS 缓存配置](https://nginx.org/en/docs/http/ngx_http_core_module.html#resolver)。

## 验证结果

- 主工作区同步最新计费代码后，全量 Web 检查：69 个测试文件、433 项测试通过；
  i18n 对齐、ESLint（0 errors，30 条现存 warnings）和 TypeScript 通过。
  `npm run build` 的生产构建通过；隔离 worktree 的前一基线为 432 项，未将两组重复计数。
- Flutter 全量：149 项测试通过；修改文件与新增测试的 analyze 通过。
- Chromium：9 项端到端测试通过。覆盖 320/390/1280 px、流式成片到达、失败不折叠、
  手动操作、历史刷新、分段/最终视频预览、200 HTML/404 chunk、持续失败、断网恢复和存储受限。
- Docker nginx 回归通过：真实生产构建的首页/SPA 路由缓存，JS/CSS MIME 和缓存，
  缺失资源 404，API 原始 URI/query/POST body/503，WebSocket 101，后端换 IP 后无需重启前端。
- 桌面展开、手机折叠与手机错误页截图已人工检查，没有横向溢出或操作按钮截断。
- 初始复现：分段组件测试 5 项失败；修复后通过。离线错误页测试先复现内部字段名问题，
  改为基础文案随包加载后通过。不是通过跳过失败状态完成验收。

复现命令：

```sh
cd frontend-v2
npm run check
npm run build
npx playwright test --config playwright.ui-recovery.config.ts
npm run test:nginx
cd ../mobile
flutter test --no-pub
flutter analyze --no-pub lib/features/chat/widgets/result_artifacts.dart lib/features/chat/utils/content_view.dart test/features/chat/result_artifacts_test.dart
```

nginx 测试使用专属临时 Docker 网络及无业务逻辑的 Python upstream，结束后按创建 ID 清理。
可通过 NGINX_TEST_IMAGE/PYTHON_TEST_IMAGE 指定已有本地镜像；不读取生产配置。
浏览器测试可通过 UI_QA_VIDEO 指定本地验收 MP4，默认不依赖真实媒体或后端。

## 阿里云发布记录

- 2026-09-09 20:35:34–20:35:59（北京时间），gw2 仅替换 frontend，最终标签
  `openbox-frontend-v2:20260909-ui2-4d2a578`，来源 `4d2a578178954d008f5d5cf0ca383a070ea19a9d`。
  backend 保持 `20260909-media-998219e`；backend/postgres/redis 容器 ID 和重启次数未变，
  四服务均 healthy，数据库 revision 仍为 `e1f3a5b7c9d2`。
- 源码经 `git archive` 干净导出，本机 Docker 构建 `linux/amd64`，不包含本地 env/无影云配置。
  最终 nginx 基础镜像固定为生产同版 `1.31.3-alpine`（NJS 1.0.0）；切换前后完整前端环境变量相同。
  `.env`、基础 compose、`config/backend.env`、`config/openbox.json` SHA-256 均未变；
  compose override 仅 frontend image 一处差异。
- 镜像 SHA-256 `4b31bd54c1b40efe8ff32473027bf56a419ece9141ef7da062afaca1589b1970`，
  私有 OSS 中转包 SHA-256 `895fd69949d70f632ef82145589ed4bb4fc534999d023c4892a04f27be39d5d6`，
  本机与服务器装载后一致。最终镜像再次通过完整 Docker nginx 回归；切换前还在 gw2
  的独立 loopback 端口验证生产后端路由、HTML no-store、缺失资源 404，再清理临时容器。
  两次构建的 OSS 临时中转对象已删除，服务器/本地镜像包及回滚备份保留。
- 公网首页与静态文件共 **104 个文件逐个 SHA-256 与最终镜像一致**。
  `/`、`/index.html`、`/app/auth-center` 及会话页面入口均 no-store；缺失 JS/CSS
  为 404、text/plain、no-store；真实 JS/CSS MIME 正确且 immutable。
  `/api/environment` 为 200/prod，Logto 仍使用生产身份服务，匿名 `/api/auth/me` 为 401。
  没有登录其他用户的会话，未以匿名检查冒充真实用户任务端到端验证。
- 备份 `/opt/openbox/backups/20260909-ui2-4d2a578/activation-20260909T123532Z/`（0700），
  含旧配置、镜像信息、activation.json 与经 `pg_restore --list` 校验的 `preflight.dump`；
  dump SHA-256 `111e378047537c81cdfb1cd462796517875d9d7f888a2b5f8f4d0895db6d31e0`。
  本次无数据库迁移，不需要恢复数据库。回滚仅把 frontend image 改回
  `openbox-frontend-v2:20260909-media-998219e`，执行 `up -d --no-deps frontend`，随后重新验收。

### 首次回滚与发布边界

首次 `20260909-ui-4d2a578` 镜像使用浮动 `nginx:alpine`，拉取到 nginx 1.31.5/NJS 1.0.1，
与生产 1.31.3/1.0.0 不同；容器已 healthy，但完整环境变量保护检查拒绝继续，自动恢复旧前端。
未放宽检查：重新使用生产同版运行时打包，并把预计环境变量一致性检查前移到替换容器之前，
同时增加生产 loopback 临时实例验证，最终发布成功。首次备份与失败记录保留在
`/opt/openbox/backups/20260909-ui-4d2a578/activation-20260909T123115Z/`。

首次切换及回滚的 2 分钟公网采样：首页与 API 各 108 次，其中各 20 次 502、88 次 200；
两段失败窗口对应两次单实例 frontend 替换，不能宣称零停机。
最终切换的 2 分钟采样：首页与 API 各 109 次，其中各 10 次 502、99 次 200；
20:35:35.071 首个失败，20:35:45.842 恢复成功（约 10.8 秒），之后的采样持续为 200。
已打开旧版页面的用户需刷新一次才能使用新版本的恢复逻辑。

构建阶段全依赖审计有一条已有 `js-yaml` 高危告警，其依赖链属于 ESLint 开发工具；
`npm audit --omit=dev` 为 0 条，最终 nginx 镜像不包含 Node/ESLint 工具链。
本次没有顺带更新依赖锁文件，也没有宣称整套镜像经过漏洞扫描。

## 追加修复：独立单段视频通过 share_file 交付

用户随后授权检查自己的已登录 Chrome 会话。确认页面已加载上一版生产 bundle，
不是旧缓存：同一轮实际输出是 `video_generate` 的 `video_segment/intermediate`，
随后 `share_file` 重新上传为新的 asset_id、关系为 `shared_file/result`；没有 `video_final`。
上一版只覆盖了显式成片，漏掉这个直接交付路径，因此两张视频卡仍然展开。

本次 Web/Flutter 使用相同的脱敏三消息 fixture 补回归，不改数据库或历史关系：

- 保留显式 `video_final` 的即时折叠，也识别非分段视频上的显式 `role=final`。
- 对缺少成片标记的旧/新直接生成流程，仅在本轮最终答复完成、无错误、非运行/等待时兜底：
  必须恰有一个成功完成的独立生成分段，无 production/segment 归属；后续成功的
  `share_file` 结果必须实际附上视频，资源 ID 相同，或精确匹配生成记录的 `workspace_path`。
- 匹配后仅将展示分组视为最终视频，原始 parts/assets/relation 不变。文件名相同、
  最终文字、普通视频附件本身都不足以认定成片。明确中间素材、分镜预览、多分段歧义、
  未完成、失败、等待输入、不附加文件等均不触发兜底折叠。
- 分段仍位于最终视频上方。最终交付前展开，交付后默认折叠；支持手动展开/收起，
  普通重绘保留选择，历史刷新默认折叠。此兼容规则不把未明确交付的所有视频都当成成片。

验证：修复前真实结构用例复现 3 项失败；修复后 Web 70 文件 / 457 项全部通过，
TypeScript、i18n、ESLint 通过（0 errors，29 条已有 warnings）。Flutter 全量 173 项通过，
修改文件及新增测试 analyze 无问题。Chromium 12 个流程全部通过，覆盖 320/390/1280 px、
独立附件与显式成片两条路径、预览/失败不折叠、交付折叠、手动操作、视频弹层、历史刷新、
隐藏分段不请求资源、页面无横向溢出以及旧版 chunk 恢复。窄屏与桌面截图已人工检查。

移动端额外仓库门禁仍存在基线问题：两种语言 common.json 尚未与 Web 同步，
原有 `question_dock_test.dart` 为 1083 行、超过 800 行限制；已核对 HEAD 即如此，
本次未改这些文件，不把这些门禁描述为通过。浏览器首轮测试拦截范围误匹配 Vite 源码路径，
已限定为根 `/api/` 后完整重跑；最终 12 项为无重试通过。

发布及真实浏览器验收结果将记录在后续追加记录中。Flutter 源码随代码推送，
手机安装包不随本次 Alibaba Web 镜像自动发布。
