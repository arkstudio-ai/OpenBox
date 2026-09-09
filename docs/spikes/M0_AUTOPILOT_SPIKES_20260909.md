# M0 spike 记录：自动营销模版（2026-09-09 晚）

> 对应 `docs/AUTO_MARKETING_AUTOPILOT_PLAN.md` §7 的 A2 / B1 / C1。环境：gw2 内测，账号 bbdwxh_admin，
> 云桌面 `ecd-glxi1nk433hliivri`（该桌面已绑定抖音创作者中心登录态，昵称 用户2087843173024）。
> 所有操作只读，未发布任何内容；花费：2 次 STT + 3 次多模态调用，约 0.3 积分。

## 执行方式（可复用）
- 桌面侧：`aliyun ecd run-command`（root）→ 若 `127.0.0.1:9222` 无 relay，则按后端 `_relay_start_script` 同样的命令
  `DEV_BROWSER_MODE=local DEV_BROWSER_CHROME_PORT=9333 npm run start-relay` 起 relay → `cd /opt/openbox/skills/dev-browser && npx tsx <<TS …`，
  `connect()` 拿到的就是桌面上带登录态的 Chrome。截图/帧/音频用本地生成的 OSS 预签名 PUT 上传到 `bossip-media-sh/spike/`。
- 分析侧：脚本 `docker cp` 进 gw2 backend 容器，用后端自己的 provider 配置调 `openai/gemini-3.7-flash`（litellm）和
  `_provider_transcribe`（fun-asr）；帧以预签名 GET URL 传给模型。脚本：`work/`（本次留在 scratchpad，M1 落成工具）。
- **坑**：GET 签名的 URL 不能用 HEAD 探活（方法不匹配 → 403），用 `Range: bytes=0-0` 的 GET。第一轮因此 0 帧进模型，见 B 的结论 3。

## A · 热点采集（A2）

1. **热点宝（douhot.douyin.com）不共享创作者中心登录态。** 打开即跳 `open.douyin.com/platform/oauth/pc/auth?client_key=awuu67pp4wjdf1hg&scope=user_info`，
   页面标题「使用抖音账号登录 生活服务热点中心」，要用抖音 App 扫码单独授权一次。→ A1 的站点条目要把它当**独立站点**：
   `login_url=https://douhot.douyin.com/`，探活 URL 待授权后抓（首页会请求的 XHR），cookie 域 `douhot.douyin.com`。用户在云桌面扫一次即可，之后走 TTL 探活。
   本次因无人扫码未进入榜单页；授权页已留在该桌面的 Chrome 里（页名 `douhot`）。
2. **公开热榜可用**：`https://www.douyin.com/hot` 在同一浏览器里直接可读，一屏拿到 10 条「热点视频」
   （id、时长、点赞数、标题+话题、作者、发布时间）和 20+ 条「抖音热榜」词条（标题+热度）。可作为热点宝授权前的默认源。
3. **媒体地址可取**：视频详情页 SSR 数据里有 `*.douyinvod.com` 直链，桌面 ffmpeg 4.4 带 `Referer: https://www.douyin.com/` 即可
   ffprobe/抽帧/抽音，不需要 cookie。3 条中 2 条（25s 口播）成功；1 条 1:26 的宠物展示视频在详情页未找到直链（可能走另一渲染结构或需滚动加载），
   需要 fallback（监听网络请求或从 `RENDER_DATA` 解析）。
4. 未测：频控。单账号 3 次页面访问无异常。A3 的缓存设计（同类目一天一抓）本身就把频次压到很低。

## B · 热点分析（B1）

样本：`7680914205477924260`（体制内口播，25.3s）、`7681228553366605102`（健康科普口播，24.7s）。各抽 8 帧（720 宽 JPEG）+ 单声道 16k mp3。

| 项 | 结果 |
|---|---|
| STT（fun-asr） | 两条全文准确，含语气词；`duration_ms` 正确 |
| 多模态（gemini-3.7-flash，8 帧 + 转写） | 输出合法 JSON；形态判定「口播」正确；人物/场景描述经与抽帧人工核对**准确**（红卫衣马尾女生+白书架；灰 polo 中年男+落地窗沙发）；`on_screen_text` 把顶部黄底标题、底部字幕、合规免责声明都读出来了；结构分段与转写对齐 |
| token | prompt ≈ 9.2k（8 帧 ≈ 8.8k）、completion ≈ 1.6–1.7k；耗时 11–14s |
| 成本（按现价目） | LLM ≈ 0.088 积分 + STT 0.05 = **≈ 0.14 积分/条** |
| 复刻要素 | `recreate_elements` 给到人物、场景、节奏、字幕样式、音乐，足以喂给口播技能的 prompt 骨架 |

**结论 1**：8 帧 + 转写这条链路足够支撑 `video_analyze`，成本极低，M1 直接产品化。
**结论 2**：形态判定字段有效（口播/展示区分靠画面）；需要更多非口播样本验证（本次唯一的展示类样本媒体未抽到）。
**结论 3（重要）**：**没有帧时模型会自信地编造画面**——第一轮因探活 bug 0 帧进模型，它把口播人物编成「中年男性中式书房」，
把根本没有媒体的宠物视频编成「毛笔书法写『顺』字」。工具层必须硬性要求 ≥ N 帧才允许出分析，帧缺失就报错，绝不静默退化成纯文本分析。

## C · 桌面自动发布（C1，只做侦察）

- `https://creator.douyin.com/creator-micro/content/upload` 用登录态直接可达，页面含：发布视频/图文/全景/文章四个入口，
  单个 `input[type=file]`（`accept=video/*,.mp4,.mov,…`，非 multiple），文案「点击上传 或直接将视频文件拖入此区域」，
  规则提示：≤1 小时、≤16G、支持 4K、建议画幅 16:9 / 9:16 / 3:4 / 4:3。截图已存 OSS `spike/creator-upload-1.png`。
- 未做：真实上传→填标题/话题→AI 声明→发布。原因：会在该账号产生草稿/作品，需要你指定测试号并确认后再做。
- 下一步（需确认）：用测试号走一遍到「发布」按钮前一步（不点发布），记录上传耗时、表单字段的 DOM、AI 声明开关位置、可见范围与定时发布选项。

## A（续）· 热点宝授权后（用户扫码后 22:10）

- 登录态：cookie 全部落在 `.douhot.douyin.com`（`sessionid_douhot`、`sid_tt_douhot`、`uid_tt_douhot`…），**过期时间约 +60 天**（1794146215 ≈ 2026-11-08）；
  与 `.douyin.com` 的创作者中心 cookie 完全独立 → 站点条目 `cookie_domains=(".douhot.douyin.com",)`。
- **探活接口**：`GET https://douhot.douyin.com/douhot/v1/user/user_info` → HTTP 200，`{code:0, data:{user_id, douyin_uid, nickname, avatar_url, follower_count, …}}`，
  `code_path="code"`, `ok_values=(0,)`，昵称/粉丝数字段现成，可直接做 `LightProbe` 与 `profile_probe`。失效判据待观察（未登录时 code 非 0）。
- 榜单页 `https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all`（标题「抖音热榜广场」）：视频榜（总榜 / 低粉爆款 / 高完播 / 高涨粉 / 高点赞）、
  话题榜、飙升话题榜、搜索榜、飙升搜索榜，时间窗 近1小时/1天/3天/7天，带垂类筛选；每条含标题+话题、作者+粉丝数、发布时间、播放量。
- **数据接口**（均带 `msToken, X-Bogus, _signature` 签名参数 → 只能在页面上下文里通过浏览器发起，不能裸 HTTP 调）：
  `/douhot/v1/material/video_billboard`、`/douhot/v1/material/challenge_billboard`、`/douhot/v1/dashboard/hot_search/query_list`、
  `/douhot/v1/common/category`（垂类）、`/douhot/v1/material/content_tag`。A3 的采集方式定为：dev-browser 打开页面 → `page.evaluate(fetch(...))` 复用页面签名，或直接读 DOM。
- 截图：`spike/douhot-2.png`（我的数据首页）、`spike/douhot-3.png`（热榜广场）。

## C（续）· 上传演练（测试号，未发布）

- 用桌面 ffmpeg 生成 5s 720x1280 测试片（1.7 MB），`page.setInputFiles` 到 `input[type=file]` → 页面自动跳到
  `/creator-micro/content/post/video?enter_from=publish_page`，表单在 60s 内就绪（小文件实际远快于此，本次探测正则写错未测到精确值）。
- 表单结构：**作品描述** = 标题 input（placeholder「填写作品标题，为作品获得更多流量」，0/30）+ 简介 contenteditable（0/1000，支持 `#话题` `@好友`，
  下方有推荐话题）；官方活动；设置封面（竖 3:4 / 横 4:3，AI 推荐封面）；添加合集；**自主声明**（弹窗单选：`内容由AI生成` / 个人观点 / 转载 /
  `内容含营销推广信息` / 虚构演绎 / 无需添加）；扩展信息：添加标签（位置）、**关联热点**（输入热点词）；发布设置：谁可以看（公开/好友/仅自己）、
  保存权限、发布时间（立即 / **定时发布**）；底部按钮 `发布` / `暂存离开`；右侧「发文助手 · 快速检测：作品未见异常」。
- 全部字段可由 DOM 定位（文本/placeholder/role 稳定）；`自主声明` 需先点开弹窗再选单选。截图 `spike/upload-form-1/2/3.png`。
- 演练以「暂存离开」结束，测试号内容管理里留有一条草稿，未发布任何内容。
- 结论：自动发布技能的全部输入都有落点：标题 ≤30 字（**比开放平台 H5 投稿的 55 字更短**，文案生成要按 30 字约束）、简介 ≤1000 字含话题、
  AI 声明必选 `内容由AI生成`（营销类再加 `含营销推广信息`）、关联热点可填热点词、定时发布可用于节奏控制。

## 对计划的修正
- A1 热点宝站点条目 = 独立 OAuth 授权，需用户扫码一次（模版创建流程里加一步「授权热点宝」）。
- A3 的 `hot_trends` 第一版数据源改为**公开热榜 + 热点宝（已授权时）**双源，热榜零门槛。
- B3 加硬约束：帧数不足即失败；分析结果缓存按视频 id。
- 媒体直链 fallback 列入 A3。
- 热点宝站点条目参数已齐（cookie 域、探活接口、判据）；采集必须走页面上下文（签名参数）。
- 自动发布：标题按 ≤30 字生成；`自主声明=内容由AI生成`；`关联热点` 直接回填当次跟的热点词；`定时发布` 承担节奏控制。

## A3 采集接口（2026-09-10 凌晨，`hot_trends` 落地前的最后一轮探测）

- 热点宝榜单页 `square/hotspot` 自己发起的数据请求（Playwright `page.on('response')` 抓到）：
  `POST /douhot/v1/material/video_billboard` body `{"sub_type":1001,"date_window":24,"page":1,"page_size":10,"tag_version":"v2"}`
  （sub_type 1001 总榜 / 1002 低粉爆款 / 1003 高完播 / 1004 高涨粉 / 1005 高点赞；date_window 1/24/72/168）；
  `POST /material/challenge_billboard`（2001 话题榜 / 2002 飙升）；`POST /dashboard/hot_search/query_list`
  body `{"date_window":24,"page_num":1,"page_size":20,"sub_type":3001|3002}`；`GET /material/content_tag?sort_key=category_priority` 是 v2 垂类树。
- **签名只覆盖 URL**：在页面上下文里对 `performance.getEntriesByType('resource')` 里已签名的同一 URL 再发 POST、换任意 body 都返回 code 0
  （72h/50 条、1005/1h、第 2 页均成功）；改 URL 参数则 `url doesn't match`。→ 采集不用点 UI，页面加载完复用签名 URL 即可。
- 垂类过滤字段是 `tags`，取值形如 `[{"value":628,"children":[{"value":62804},…]}]`（`content_tag` 的 value）；
  `tags:[628]`/字符串/单 int → code 5 参数不合法；只给一级 `{"value":628}` 过滤很弱，带二级 children 才是明确的美食内容；
  只给二级 `{"value":62804}` total 0。UI 里的级联选择器是 Semi Cascader，脚本点不开，字段靠枚举猜出来的。
- 视频条目字段：`item_id, item_title, item_cover_url, item_duration(ms), nick_name, fans_cnt, play_cnt, publish_time, score, item_url(直链), like_cnt, follow_cnt, follow_rate, like_rate, media_type`（4=视频；图文帖 duration 0、item_url 指向 douyinstatic 图包，`video_analyze` 不能吃）。
  话题：`challenge_id, challenge_name, play_cnt, publish_cnt, score, trends[]`；搜索：`key_word, search_score, trends[]`。
- 公开 `www.douyin.com/hot` 只有「热点视频」区（`ul[data-e2e=scroll-list] li`：时长、点赞、标题(img alt)、@作者、相对日期），本次页面上没有 `SSR_RENDER_DATA` 里的结构化榜单，也没有词条榜。
- 后端驱动方式定为：沿用 `platforms/desktop/cdp.py` 的「桌面上跑一段自带脚本连 9333」模式，新增 `trends/desktop_page.py`（后台 target 打开页面 → 等 host+readyState → 等指定资源出现 → `Runtime.evaluate` 页面内 JS → 关 target）；
  `platforms/desktop/service.run_command_on_desktop` 抽出来给两者共用（lease、timeline span、错误映射一致）。真机四条脚本（美食视频榜 6 条 code 0、话题榜、公开热榜 20+ 条、直链解析拿到 douyinvod）均通过；
  直链解析要跳过页面里的 `uuu_265.mp4`（H.265 探测用静态片）。
- 未做：24 小时连续频控观测；热点宝登录失效时脚本会因 `location.host` 变成 open.douyin.com 而报 `redirected`，工具层先看授权中心状态再采。
