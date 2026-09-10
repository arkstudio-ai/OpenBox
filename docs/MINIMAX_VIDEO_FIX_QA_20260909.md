# MiniMax 视频提交故障修复与验收（2026-09-09）

## 故障证据

会话 `session_7YBXX47JK061XV1D1ASW5MKCMK` 在北京时间 18:35–18:36
发起 9 次 MiniMax-H3 请求，均为 HTTP 400，没有上游任务 ID。网关 channel 114
的明确报错为「文生视频 ratio 不能为空或 adaptive (2013)」。

生产挂载的 `config/openbox.json` 缺少 MiniMax 的 `wire_shape`，退回 `flat`。
旧代码据此生成顶层 `resolution: null, ratio: 9:16`；网关的 MiniMax 适配器
从 `size` 解析画幅，忽略了该顶层 ratio。代码中的 size 分支已有，但未被配置启用。

第二个问题在真实验收前置校验中暴露：`VideoGenerateArgs.resolution` 仍只允许
480p/720p/1080p，配置声明的 MiniMax 原生 512p/768p/2k 无法通过工具 schema。
这个前置失败发生在 provider POST 之前，没有创建生成任务或重复付费提交。

## 修复范围

- 对未声明 wire_shape 的旧 MiniMax-H3/sd2 配置兼容为 size；显式协议覆盖保留。
- 工具 schema 接受六种已实现的分辨率，模型级校验继续限制实际可选范围。
- size 适配器拒绝当前无法准确表达的画幅以及视频/音频参考，不静默改变请求。
- HTTP 400/422 明确标记为 rejected，返回自有安全文案、错误代码、可验证的请求 ID。
  未知上游文本和签名链接不回显；超时、429、5xx 不误报为明确拒绝。
- 失败记录持久化；相同用户、会话、Agent run、模型和路由下，后续自动提交被拦截。
  更换提示词、幂等键或 allow_duplicate 不能绕过；新用户轮次不被永久锁定。
  run_id 排除于请求幂等哈希之外，已有成功/进行中任务仍按原 key 复用。
- 生产 MiniMax 配置明确为 size、480p/512p/768p/2k、9:16/16:9、4–15 秒；
  当前适配器仅转发图片参考，因此不再宣称支持视频参考。

旧会话保存的 720p/1080p 不被后台改写。刷新后应明确选择该模型的原生档位，
例如 768p；不把网关向上取整的实际结果继续标为精确 720p/1080p。

## 测试

- 发布分支：332 项视频、配置、合成计费、进程重启恢复及真实 PostgreSQL 回归通过。
- 修复推送时的 main 分支（abc0c4d）：294 项视频、配置、重启恢复、PostgreSQL
  及工具描述/暴露预算测试通过。
  两组有重叠，不能相加作为独立用例总数。
- HTTP 客户端 + 持久化测试覆盖六种分辨率的横/竖屏，成功任务跨轮次仅提交一次。
- PostgreSQL 使用独立本地 Docker 容器和临时 schema，不访问生产数据库做负向测试。
- 修复依赖本机私有 openbox.json 的旧测试，改用无密钥的显式协议夹具。
- 本地 linux/amd64 Docker 构建通过；镜像不含本地 .env/openbox.json/技能任务数据库。
  镜像内检查原生参数 schema、视频合成模块和浏览器恢复资产均通过。

## 源码与部署

- main 代码提交：`e4670f6`、`abc0c4d`。
- 保留已在线 video_compose 的发布分支代码提交：`70ca569`、`1bc6743`。
  发布时两条分支的视频提供商与生成工具修复代码一致；修复未自行合并其他功能分支。
  验收期间 main 另行通过 PR #3 合入 video_compose（d08ce20）；记录推送前已同步该提交，
  最终 main 与实际发布分支仅文档有差异，运行时代码和测试代码一致。
- 最终生产镜像：`openbox-backend:20260909-minimax-1bc6743`。
- 镜像 ID：`sha256:9702e8eab97afcbf09b20dc6623e56221d7f7fc3dfc41961f2527be85c06ad6a`。
- 镜像传输包 SHA-256：`7e2fd66e1d79e222d14dec0d10a43167b5a87086983c5234c7b5651b0f6fbbe0`。

首次切换的单次就绪断言在 nginx 异步 reload 时读到旧 worker 的维护响应，
发布保护自动回退；补成连续三次就绪后再发布。不是应用启动失败。
19:26–19:28 的 120 次公网采样：首页全部 200；API 为 80 次 200、31 次维护 503、
9 次 502。不得宣称零中断。发布前无正在运行的会话/有效运行租约或 finalizing 视频。

备份位于 gw2 `/opt/openbox/backups/20260909-minimax-70ca569/`、
`20260909-minimax-70ca569-retry1/`，包含经 pg_restore --list 校验的数据库备份及配置。
最终发布于北京时间 19:34:52–19:35:13 完成；备份位于
`/opt/openbox/backups/20260909-minimax-1bc6743/`。
最终切换的 120 次公网采样（19:34:47–19:37:14）：首页全部 200；
API 为 105 次 200、15 次维护 503，无 502。维护结束后连续就绪检查通过。
前端、PostgreSQL、Redis 容器 ID 不变；数据库迁移版本仍为 `e1f3a5b7c9d2`。
`.env`、backend.env、基础 compose 保持不变；最终补丁切换时 openbox.json 也逐字节不变。
已有 video_compose 功能及无影云生产配置保留，没有部署本地开发配置或重启云电脑。

## 真实生成验收

仅在请求者的账号 `andrewwang` 下建立一个独立测试会话，未运行或修改原故障会话。
使用生产工具直接提交一次 MiniMax-H3 / 768p / 9:16 / 4 秒请求：

- 测试会话：`session_7YBXX15BKYQBXF0ZEWA5QKZM7H`。
- 本地任务：`video_01M22Z7H5BQH27B10S9HXFAAVS`。
- 上游任务：`task_xokuKrNG10l8LQP3hVmFrcjiv3Vj0bgZ`。
- 资源：`asset_01M22Z7H5BQH27B10S9HXFAAVT`。
- 19:35:40 提交、约 1.3 秒后确认 queued；已通过原先失败的提交阶段。
- 后续只调用 status，没有提交新任务；19:36:41 确认 completed、资源 ready，
  成品已入 OSS 并挂到测试会话。提交至确认完成为 61.2 秒，包含轮询间隔和入库，
  不作为精确的上游纯生成耗时。
- 生产只读核查：测试会话仅 1 个任务、1 个上游任务 ID、1 个 completed；
  原故障会话仍为 idle，原有 9 个失败记录和 0 个上游任务 ID 均未变化。
- 下载本次独立测试的成品进行全文件音视频解码：退出码 0，无解码错误；
  H.264、768×1344、24 fps、107 帧，AAC 32 kHz 立体声，容器时长约 4.46 秒。
  这是上游对 4 秒、9:16 请求实际返回的原生尺寸和时长，并非精确 9:16/4.00 秒。
  第 1 秒画面抽检可见草地上的小狗与红球，与测试提示相符。
- 文件大小 972,895 字节；SHA-256：
  `9f80f1feaee6edd9227d460a4df1ba5eb218dfcaeab3d07c3d923dd22224f2ed`。
- 最终生产后端 healthy，`/health` 返回 ok，`/api/environment` 返回 prod；
  运行代码的 SHA-256 与发布镜像内容一致。

测试会话：<https://ai.bossipai.com.cn/app/s/session_7YBXX15BKYQBXF0ZEWA5QKZM7H>。

## 临时资源

独立 PostgreSQL 回归容器已停止并自动删除，没有操作开发者现有数据库。
OSS 的两份 `_deploy-tmp/20260909-minimax-70ca569/backend.tgz` 和
`_deploy-tmp/20260909-minimax-1bc6743/backend.tgz` 仅用于发布传输，验收后删除；
本地及服务器上的镜像包、生产备份、测试成品和会话保留，可重新上传传输包。
