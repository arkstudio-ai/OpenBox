# 视频排版与前端资源故障回归（2026-09-09）

## 变更范围与交付状态

修复 Web（frontend-v2）与 Flutter 的视频结果排版，以及 Web 路由资源加载失败后的恢复。
修复阶段的线上操作仅为只读诊断，没有重启线上服务、修改账号/无影云配置或调用付费生成。
测试重用已有独立验收视频；没有访问其他用户的浏览器会话。

开发在隔离 worktree 完成，再将修复应用到已同步 origin/main 的主工作区。
发布前再次同步至 `491d545`，已包含 `998219e` 的最新图片/转写计费发布，
并在合入后复跑 Web 433 项、Flutter 149 项测试，保留最新功能，不用较旧版本覆盖线上。
用户已授权提交、推送并发布；阿里云仅替换 frontend，Flutter 源码同步入库，App 使用独立发版流程。
实际发布结果见本文后续发布记录与 `docs/DEPLOY.md`。

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
