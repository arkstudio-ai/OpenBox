# Web v2 设计对齐与早期实施记录（2026 年 8 月）

> 从工程规范的附录 D–H 归档；功能、构建、依赖和交互结论对应当时版本。现行开发规则见[工程规范](../../../frontend-v2/docs/ENGINEERING_SPEC.md)。

## 附录 D · 设计稿对齐记录（2026-08-20）

依据「设计稿为准」原则，v2 实施时做出的映射与取舍。**原则：界面呈现严格随设计稿；数据一律来自真实后端；后端没有的能力宁可省略控件，不做假按钮。**

### D.1 版本与选型修订

| 原条款 | 修订 |
|---|---|
| §2.1 React Router 7.x | 实际安装 **react-router 8.x**（data 模式 API 同 7） |
| §2.1 TypeScript 5.7+ | 实际 **6.x**（`baseUrl` 已废弃，paths 用相对映射） |
| §2.1 i18next 25.x | 实际 **26.x**；懒加载后端用 `import.meta.glob` 自实现，未引额外包 |
| §10.1 基准语言 en-US | 保持；但**产品词表源头是设计稿的中英双语词表**，两边同步维护 |

### D.2 品牌与路由

- 品牌名 **bossip**，沿用设计稿 logo（b 方块 + 流光 wordmark）。
- 路由：`/`（官网）、`/login`、`/register`、`/callback`（SSO 回跳）、`/app`（空态/新对话）、`/app/s/:sessionId`（对话）、`/app/settings/:tab`。设计稿词汇「项目/对话」映射后端 `project` / `session`。

### D.3 设计 → 后端能力映射

| 设计稿元素 | 真实数据源 | 取舍 |
|---|---|---|
| 思考折叠条 | `reasoning` part | 完整实现 |
| 过程日志（8 类调用） | `tool` part（bash/read/glob/grep/edit/write/skill/mcp/web_*） | 完整实现；kind/glyph/tone 映射表在 chat 特性 |
| 计划清单卡 | `GET /session/{id}/todo` + `todo.updated` | **只读**：后端无用户增删步骤端点，「加一步 / 删步骤」不做 |
| 改动卡片 → 审阅 | `patch` part + `GET /session/{id}/diff` | 完整实现；审阅面板「全部通过/退回」**省略**（后端无 changeset approve API） |
| 积分 / 订阅 | `session.token_usage`（tokens / cost 聚合） | 设置页改为「用量与消耗」；充值/套餐/发票 UI 省略 |
| 附件 / 语音输入 | 无上传端点 | composer 中省略这两个按钮 |
| 邮箱链接登录 + 微信/Google | 后端为 账号密码 + Logto OIDC | 登录卡改为 账号密码 + 单个 SSO 按钮（logto enabled 时显示） |
| 项目菜单：复制 / 归档 / 置顶 | 无对应端点 | 菜单保留 重命名 / 新建对话 / 删除 |
| 对话行悬停：置顶 / 归档 | 无对应端点 | 改为 删除 |
| 顶栏分享 | 无分享链接服务 | 实现为「复制当前会话链接」+ toast |
| 文件面板内容预览 | **新增后端端点** `GET /api/containers/{id}/files/content?path=` | 转发容器 `/read_file` 并去行号（backend/api/files.py，本次唯一后端改动） |
| 终端 | WS `/ws/terminal/{containerId}?ticket=` PTY 字节透传 | 完整实现（xterm，懒加载） |
| 浏览器面板 | WS `/ws/dev-browser/auto?ticket=` 截图流 | 移植 v1 协议 + 设计稿皮肤 |
| 权限/提问交互（设计稿未覆盖） | `permission.asked` / `question.asked` | **必须补**：行内卡片，沿用设计语言（后端流程依赖它） |
| 字号缩放（root zoom） | — | 按对接文档建议改为 html 根字号 + rem |

### D.4 实施期发现并修复的后端问题（2026-08-20 联调）

| 问题 | 修复 | 位置 |
|---|---|---|
| `step-start` / `step-finish` part 从未落库：`save_part()` 未传 `is_new=True`，UPDATE 不存在的行被静默丢弃 → **会话 diff 功能自后端重构起整体断链**（v1 亦受影响），过程耗时也拿不到 | 两处调用补 `is_new=True` | `backend/agent/loop.py` |
| 无文件内容读取端点（工作面板文件预览无数据可用） | 新增 `GET /api/containers/{id}/files/content?path=`（转发容器 `/read_file` 并去行号） | `backend/api/files.py` |
| 前端 diff 查询未带 `?full=true` → 审阅面板无逐行 hunks | 查询带上 `full=true` | `features/workbench/api/diff.ts` |

### D.4.1 第二轮修复（2026-08-20 晚，用户验收反馈）

| 反馈 | 根因与修复 |
|---|---|
| 气泡没有左右分侧 | ChatFlow 行 wrapper 是普通 div，`self-end` 无效（父级非 flex）→ wrapper 加 `flex flex-col` |
| 流式输出不顺畅、难看 | react-markdown 每个 delta 全量重解析 → 换 **streamdown**（DEEIX-Chat 同款）：分块解析+块级 memo+不完整语法容错+内置流式光标与淡入动画+shiki 双主题代码块（经 shadcn 别名 token 穿 bossip 皮肤，`[data-mode="dark"]` 翻转 `--shiki-dark`）；补 `.text-shimmer`（运行态标题流光）、`.fold`（折叠 220ms 过渡）、工具条目 spinner 徽标+滑入、思考折叠流式尾行预览、TypingRow shimmer；滚动器换 ResizeObserver 贴底+顶/底渐隐遮罩+overscroll-contain+overflow-anchor:none+发送强制滚底 |
| 输入框样式功能不对 | 重写 Composer 对齐设计：附件 tiles 行 + 输入行（busy 时行内「停止生成」）+ 操作行 [附件 ＋ ▸ ⊙模型胶囊 ▸ 40px 发送圆钮]；附件=真实上传（粘贴亦可），气泡下方渲染附件胶囊 |
| 附件无后端（用户授权补） | 新增 `POST /api/containers/{id}/files/upload`（multipart → base64 分块经容器 /execute 落至 /workspace/uploads，≤8MB），消息尾部附 `[attachments]` 路径块由 UserBubble 解析为胶囊 |

依赖新增：`streamdown`（懒加载 Markdown chunk 内，首屏 122.8KB gzip 仍在预算内）。

### D.4.2 第三轮：聊天区 1:1 还原 DEEIX-Chat（2026-08-20 深夜）

用户要求聊天页与过程输出 UI/UX 与 DEEIX-Chat 完全一致，原实现作废重做。**右侧工作面板不在本次范围内，未改动。**

**拆解到的参考规格**（源：`~/workspace/DEEIX-Chat/frontend`）：

| 元素 | DEEIX 规格 | 本项目落地 |
|---|---|---|
| 助手消息 | **无头像、无气泡**，占满列宽；`text-[15px] leading-8 [overflow-wrap:anywhere]` | `AssistantTurn` 重写；尺寸用 rem token（`text-lg`）以保留字号档位缩放 |
| 用户消息 | 右对齐，`max-w-[70%] rounded-xl bg-muted/60 p-3`，移动端 88% | `UserBubble` 重写 |
| 消息列 | `mx-auto w-full max-w-[760px] space-y-6` | `ChatFlow` 对齐（760px / gap-6） |
| trace 行 | 无边框手风琴：13px 中粗标题 + 11px 弱化副标题 + 右侧 chevron（展开 rotate-180），`mb-2 w-full pr-4 sm:pr-6` | `TraceShell` 三 trace 共用 |
| 三条 trace | 处理中/**处理完成** + 准备 N tokens 上下文 · 正在思考/**思考完成** + 固定副标题 · 工具调用中/**工具调用** + N 次工具调用 | `ProcessTrace` / `ThinkingTrace` / `ToolChainTrace`，文案取自 DEEIX zh-CN 词表 |
| trace 顺序 | **聚合在正文之上**（process → think → tools），不与正文交错 | `buildTurnView` 聚合装配，取代原 `buildBlocks` 交错序列 |
| 折叠行为 | 运行中自动展开 + 标题 shimmer；正文开始输出后自动收起；手动切换优先 | `TraceShell` 渲染期派生，无 effect 同步 |
| 工具行 | `grid-cols-[0.875rem_8rem_1fr] gap-x-5 text-[12px]` 时间轴：连接线 + 圆点（`ring-4 ring-background`）+ 定宽名称列 + 详情列 | `ToolChainRows` |
| 长输出 | 超 8 行（`1.25rem` 行高）截断，底部渐隐 + 展开/收起 | `DetailText`（ResizeObserver 量高） |
| 流式占位 | 骨架条 `max-w-[680px] space-y-2.5`，非光标 | `StreamSkeleton` |

**删除**：`ReasoningFold` / `ProcessFold` / `ToolEntry` / `AnswerText` / `lib/turns.ts`（被 `lib/turn-view.ts` 取代）。

**数据映射**（保持真实后端，无编造）：`step-finish.input_tokens` → 准备 N tokens 上下文；`reasoning` parts → 思考 trace；`tool`+`subtask` parts → 工具链；`text` parts 拼接 → 正文。

E2E 新增 `chat-ui.spec.ts` 锁死该形态（气泡分侧、无头像、trace 标题+副标题+展开态）。

### D.4.3 第四轮：元信息条 / 工具输出 / 输入框（2026-08-21）

用工作流做了 7 维度全量对比（108 条差距），按优先级实施。

**用户点名的三项**

| 项 | 落地 |
|---|---|
| 消息元信息条 | 悬浮显隐（`group/msg` + `md:group-hover`）；徽标行 = 模型 · 输入/输出/缓存 tokens · 耗时；操作行 = 复制/点赞/点踩/复刻/时间戳。**重试与编辑按钮明确不做**（后端无重生成/编辑端点，本项目也无消息树） |
| 工具结构化输出 | 按 `resolveToolLayout` 分派 7 种布局：search（关键词 + 域名胶囊 + 标题摘要）、fetch、shell（命令/输出 + 退出码红调）、file（路径 + old→new 双块）、find、agent、generic（参数/结果 JSON）；长输出 8 行截断 + 渐隐 + 展开 |
| 输入框「丑黑边」 | **根因**：全局 `:focus-visible { outline: 2px solid var(--t-a700) }` 中 a700 在默认主题≈墨黑，且优先级压过 textarea 的 `outline-none`。**修法**：`textarea/input:focus-visible { outline: none }`，焦点表现交给容器 `focus-within:border-n400`（DEEIX InputGroup 形态） |

**输入框其余重写**：形变主按钮（停止收进发送槽，不再是两个控件）· 拖拽上传（dragenter 嵌套计数）· 附件卡片（类型图标 + 大小）· `+` 下拉（上传/截图 `getDisplayMedia`）· 仅附件可发送 · 发送快捷键 Enter/⌘Ctrl+Enter（存 preferences.extra）· 粘贴截图自动命名 · **@ 提及**（文件/技能）与 **/ 命令** 菜单（键盘导航优先于发送快捷键）

**本轮后端新增（均实测）**

| 能力 | 改动 |
|---|---|
| 消息反应 | `messages.reaction` 列 + 迁移 `d4f6a8c0e2b4` + `POST .../message/{mid}/reaction` |
| 工具 metadata 透传 | `ToolPartData.metadata`（bash `exit_code`、`truncated`、`duration`） |
| web_search 结构化结果 | Tavily/DuckDuckGo 均返回 `metadata{query, results:[{title,url,snippet}]}` —— 否则前端只能正则解析编号文本 |
| 消息 error 回显 | `get_messages` 补 `error=m.error` |
| 文件搜索 | `GET /api/containers/{cid}/files/search?q=` —— @ 提及的数据源 |

**明确不做**：重试/编辑/分支导航（需引入消息树模型）· 会话分享 · 语音输入 · Markdown 预览切换 · 排队消息（与「插话即打断重启」语义冲突）· 数学公式/mermaid/HTML 透传。

### D.4.4 第五轮：流式抖动 / 代码块 / 改动 diff（2026-08-21）

| 问题 | 根因与修复 |
|---|---|
| **折叠 trace 在流式时反复开合，屏幕跳动** | `TraceShell` 的 `open` 是从 `streaming` **派生**的，而活跃标志在一轮内反复翻转（工具1结束→工具2开始的间隙、推理与工具交替）→ 每翻转一次就开合一次。改为 **闩锁**（DEEIX 语义）：只有「活动开始」开、「本轮可作答」关一次，其余一律保持；并把 per-part 标志收敛为 phase 级（`preAnswer` / `thinkingLive` / `toolsLive`），顺带消除标题在「正在思考↔思考完成」间闪烁 |
| **代码块样式不对** | 开启 streamdown `lineNumbers`（仅 default 变体），并为其 `data-streamdown="code-block-*"` 槽位补样式：语言标签+复制在框外、框体 14px 圆角、浅灰底、行号列 `text-n500` 不可选 |
| **改动展示需要 diff 且可点开审阅** | 见下 |

**改动卡片（DiffPreview）**

- **后端原本从不产生 `patch` part** —— 改动信息只存在于会话累计 diff 里，无法归属到具体轮次。现于 `agent/loop.py` 快照变化处用 `snapshot.diff()` 落 `PatchPart`，并带上 `from_snapshot`/`to_snapshot`。
- 新增 `GET /api/agent/session/{sid}/diff/step?from_snapshot=&to_snapshot=`（复用 `snapshot.diff_full`）：会话 diff 是**累计**的，若用它渲染单步卡片，会出现「头部 +1−1、正文却是 3 行新增」的自相矛盾。按快照区间取才对得上。
- 前端 `buildDiffPreview` 把 hunks 压成预览：只留改动行，连续 context（含跨 hunk 跳跃）合并成一条「N 行未修改」条；超出 8 行的改动记入 `hiddenChanges`。点击卡片 → `emitAppEvent("workbench.open", {kind:"review"})` 打开侧栏审阅。
- **工具链里的文件输出同款**：`DiffRows` 抽为共用组件，`edit`/`multiedit` 用 `editPreview`（按首尾公共行收敛）渲染真行级 diff，取代原来的「删块+增块」两坨。

> 注意：快照的 work-tree 是**项目目录**（`project_directory(slug)`），项目目录之外的文件改动不会进快照，也就没有 patch part。写 E2E 时踩过这个坑。

单测：`diff-preview.test.ts` 覆盖 context 折叠、跨 hunk 合并、截断计数、纯新增/纯删除、无 hunks 等 9 例。

### D.4.5 第六轮：「点了审阅，侧边栏没有？」（2026-08-21）

用户点击改动卡片后，侧栏**确实打开了**（Playwright 四种场景 4/4 均通过），但打开的是一个**空壳**：头部写着「本轮改动 +0 −0」，下面没有任何文件。之前手动验收看起来正常，只是因为那次会话 diff 恰好已在缓存里。

| 问题 | 根因与修复 |
|---|---|
| **面板打开后短暂全空，读起来像「没反应」** | `ReviewTab` 的守卫是 `if (!isLoading && entries.length === 0)` —— 加载中会**跳过空状态**，直接用 `data ?? []` 渲染真实头部，于是 `+0 −0` 叠在空列表上。面板只在用户点进来时才发起请求（实测点击后 485ms 才发出，800ms 才填上），这段空窗全暴露给用户。改为 `if (isLoading)` 先渲染骨架屏（`aria-busy` + 脉冲卡片 + Spinner），**不显示尚不成立的计数** |
| **同上，从根上消除空窗** | 新增 `usePrefetchSessionDiff`，改动卡片 `onMouseEnter`/`onFocus` 即预取会话 diff。指向卡片到按下之间的时间足够跑完请求，实测面板**打开即满**（+2307 −2 / 6 个文件，0 空窗） |
| **点击的文件在列表下方看不见** | 侧栏若已打开且文件多，`setReviewFile` 只是换了展开项，视口没动 → 又是一次「点了没反应」。展开卡片 `scrollIntoView({block:"nearest"})` 滚入视野 |
| **卡片头部不可聚焦、鼠标无指针反馈** | `<div onClick cursor-default>` → `<button type="button" aria-expanded>` |
| **工具链里的文件 diff 长得一模一样却点不动** | 与改动卡片同款外观就该同款行为，否则是第二个「死点击」面。`FileOutput` 的每个 edit 块在已知 path 时包成 button，同样 `emitAppEvent("workbench.open", {kind:"review", file})` |
| **`<button>` 里塞 `<div>`（非法嵌套）** | 改动卡片是 `<button>`，而 `DiffRows` 渲染 `div`。`DiffRows` 全部改用 `span` + `block`/`flex`，两种宿主下视觉不变 |

> E2E 教训：原 `diff-card.spec.ts` 只断言「`本轮改动` 可见」，而**空壳面板也有这行字**，所以这个 bug 从测试里漏了过去。已加强为：点击文件必须**出现在列表里**，且必须是 `aria-expanded="true"` 的那张卡。

### D.4.6 第七轮：改动卡片瘦身 + 开面板白屏（2026-08-21）

**① 改动卡片不再内联 diff。** 新建一个 340 行的文件，卡片就在对话流里铺开一大块绿色，把用户真正要看的回答挤到屏幕外。改为**一行淡色列表**：`⊞ path/name  +340  审阅 →`（26px 高，`text-n600`，悬停转 `text-ink`/强调色）。hunk 属于审阅侧栏，不属于对话流。

连带的简化 —— 计数本来就在 `PatchPart.files[].additions/deletions` 上，卡片不再需要请求任何 diff：

- `PatchChip` 去掉 `useStepDiff` → **每个 patch part 少一次网络请求**；
- 随之失去调用方的 `buildDiffPreview` / `useStepDiff` / `ChangeRow.no` / `toChange` 一并删除（`DiffRows` 的行号槽从来没被 `editPreview` 填过，是一列 40px 的空白，同时去掉）。工具链里的 `editPreview` + `DiffRows` 保留：那是用户**主动展开**的详情面，diff 正该在那儿。
- 后端 `/diff/step` 端点保留（能力正确，只是前端当前不用）。

**② BUG：打开右侧栏整个工作区白屏 ~420ms 并重新加载。**

根因是 Suspense 边界的位置。`WorkbenchPanel` 是 `<main>` 的**兄弟节点**，不在布局那个只包住 `<Outlet/>` 的 `<Suspense>` 里；而 i18n 命名空间是**按需懒加载**的（`react: { useSuspense: true }` + vite glob backend），`ReviewTab` 的 `useTranslation("workbench")` 首次渲染必然挂起 —— 于是挂起一路冒泡到**路由级**边界（包 `lazy(WorkspaceLayout)` 的那个），整棵工作区被 fallback 顶掉。

代价不只是闪一下：边界恢复时**所有 effect 重新挂载**。实测一次点击打出 12 个请求 —— `POST /auth/ticket` ×2（WS 重连两次）、`/auth/me/preferences`、`/message` `/permission` `/question` `/session` 各两遍。空闲基线是 0 请求。

两处修复，缺一不可：

| 修复 | 作用 |
|---|---|
| `WorkspaceLayout` 用 `<Suspense fallback={null}>` 包住 `WorkbenchPanel` | **结构性**：面板里任何挂起都就地兜住，不再掀翻整个 app。以后面板加懒加载路由/命名空间也不会重蹈覆辙 |
| `usePanelEvents` 挂载时 `i18n.loadNamespaces("workbench")` 预热 | 面板还关着的时候就把命名空间取好，打开时根本不挂起，连局部 fallback 都不闪 |

修复后同一次点击：**1 个请求**（就是面板要的 `/diff`），0 白屏帧。

> 诊断手法值得复用：逐帧采样 `main` 的 `getBoundingClientRect().height`。Suspense 隐藏内容时祖先被置 `display:none`，子节点的 `getComputedStyle().display` **仍报原值**，但 rect 高度归零 —— 光看 computed style 会漏判。另外注意别只抓 `/api/` 请求，`.json` 语言包正是从这个筛子里漏掉的关键线索。

回归测试 `workbench.spec.ts`「opening the panel neither blanks the workspace nor refetches the session」：逐帧断言 `main` 高度不为 0，且期间不得出现 `/locales/`、`/auth/ticket`、`/api/agent/session` 请求。已用「回退修复 → 测试必须失败」验证过（回退后 35 帧白屏）。

### D.4.7 第八轮：思考流 / 文件面板范围 / 面包屑 / 新建对话（2026-08-21）

**① 思考过程大段空白（贪吃蛇提示词复现）。** WS 抓帧定位：`step-start` 之后 **24 秒没有任何事件**，然后一个 13,225 字符的工具参数一次性砸下来 —— 模型在思考+攒整个 write 调用，但一个字都没流回来，前端只有骨架屏。两处根因，都在后端：

| 根因 | 修复 |
|---|---|
| `openai/gemini-*`（走 OpenAI 兼容代理）被 `_detect_provider` 归为 openai，`_get_default_thinking_kwargs` 的 openai 分支没匹配 gemini → **压根没请求思考内容** | 对代理直接试了三种参数形状：`reasoning_effort` ✗、`thinkingConfig` ✗、**Anthropic 风格 `thinking:{type:"enabled",budget_tokens}` ✓**（`reasoning_content` 随流返回）。default 与 variant kwargs 都补上 gemini 分支，经 `extra_body` 透传 |
| LiteLLM 1.81 对该代理的函数调用参数**整块缓冲** | 升级 LiteLLM 1.81.11 → **1.97.0**（用户授权）。升级后工具参数逐块流式，屏幕上「展开代码 85 行 → 164 → 260」实时增长 |

复测：`part.created part=reasoning` + 增量正常到达，思考轨迹渲染出真实推理内容；原 24 秒空白被思考轨迹 + 逐块增长的工具卡填满。前端零改动 —— 渲染管线（ReasoningPart → ThinkingTrace）本来就是通的，只是数据从来没来过。

**② 文件面板显示整个 /workspace（应只显示当前项目目录）。** `/workspace` 是 agent 的整个活动空间；会话真正的工作目录是 `project_directory(slug)` = `/workspace/<slug>`。按用户要求加接口参数：`GET /api/agent/session/{sid}` 响应新增 **`directory`** 字段（`workdir_for_session`）。前端 workbench 新增 `useSessionWorkdir`（key `["session-workdir", userId, sessionId]`，staleTime 5min），`FilesTab` 以它为树根；detail 未返回前不渲染树（避免先闪整个 /workspace 再跳回项目）。无会话时（/app）回退 /workspace。

**③ 面包屑显示乱码（容器 ID）。** `FilesTab` 头部原来渲染 `running.name ›` —— 沙箱容器的随机名（`ecd-330zd5…`）。改为项目目录名（`default ›`）。`FilesTree` 面包屑同步收敛：首个 crumb 是项目根目录本身，**浏览永远爬不到项目之上**；打开项目外的文件（如 /workspace/uploads 的附件）时回退为该路径自身的 crumbs，仅保该处可导航。

**④ 新建对话丢失左侧项目选择/展开态。** 根因：顶部「新对话」按钮 `navigate("/app")` 把 `?project=` 参数丢了 → 首条消息建会话时 `project_id=null`；且 `expanded` 只在内存，刷新即失。修复：

- `useWorkspaceUi` 新增 **`selectedProject`**，与 `expanded` 一起**持久化**到 localStorage；
- 选中时机：点项目行（chevron/名称）、点会话行（其所属项目）、项目内「+」/菜单新建；选中项目名旁有强调色小圆点；
- 顶部「新对话」带上 `?project=<selected>`（选中项目已删除时校验回退）；删除项目时清掉指向它的选择；
- 树的展开/选中状态经过新建对话、发送首条消息、整页刷新均不变。

**顺带修复**：后端建会话的兜底标题是 `New session - <裸 ISO 时间戳>`，直接漏进侧栏显示。改为空串（前端本有本地化的「未命名对话」兜底，标题生成器随后补上）；`loop.py` 的标题生成条件同步改为「空或 legacy 前缀」；存量 3 条脏标题已清。

> 排查手法：Playwright `page.on("websocket")` 抓 WS 帧 + 每 2s 采样 `main` innerText,把「空白」翻译成「哪个时间段缺了哪类事件」;对代理**直接 curl 三种参数形状**比翻 LiteLLM 文档快得多。

E2E：`sidebar-project.spec.ts`（选择→新建对话→URL 带 project→POST body 校验→刷新后状态仍在）；`workbench.spec.ts` files 用例改为「任选会话 → 树根不是 workspace、头部无容器 ID、首个文件可打开」。

**补充（同日）**：面板菜单页的提示列原样渲染 `running.name` —— 当前部署下即无影云桌面 ID `ecd-…`（`SANDBOX_PROVIDER=wuying`，经 SSH 隧道接入）。机器 ID 对用户无意义：终端行改示「沙箱在线」（新增 `menu.online` 双语键），文件行示项目目录名（`useSessionWorkdir`），E2E 断言面板不得出现 `ecd-` 形态的 ID。

### D.4.8 云桌面标签（2026-08-21）

右侧面板新增第五个标签 **云桌面**：打开即流式进入沙箱所在的无影云桌面。对接方式抄自 workspace 中 bossip 项目（`apps/codex/v1/src/server/wuying.js` + `cloud-desktop.js`）的既有集成：

**链路**：`GET /api/desktop/ticket`（登录态）→ 后端用 ECD OpenAPI `GetConnectionTicket` 取一次性连接票据 → 前端把票据交给阿里 **WuyingWebSDK**（g.alicdn.com CDN 版 2.12.5-asp3.18.7），`createSession({openType:"inline", iframeId, userInfo:{ticket}, …})` 渲染进 iframe，1920×1080 远端画面按面板尺寸等比缩放。

**后端**（`api/desktop.py`，新增 `alibabacloud_ecd20200930` 依赖）：
- `GetConnectionTicket` 是**异步任务**：桌面上还没有该 end user 会话时首次调用只回 `taskId`+RUNNING，须携带 taskId 轮询到 FINISHED 才有票。请求内轮询预算 14s，超预算回 **202 + {taskId}**（带 Retry-After），前端携 taskId 重试 —— 与 bossip 的 202 通道一致；瞬时网络抖动在预算内按 RUNNING 续轮。
- 凭证链：`ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET` env → **aliyun CLI** profile（`~/.aliyun/config.json`，本机已配置且验证可用）。
- 配置新增 `WUYING_REGION_ID`（cn-hangzhou）、`WUYING_END_USER_ID`（本桌面的 end user，经 `aliyun ecd describe-desktops` 查得）。桌面/区域信息只进 SDK 连接载荷，**永不进 UI 文案**。
- 非 wuying provider 或缺配置 → 503 `{available:false, reason}`。

**前端**（`DesktopTab.tsx`，TabKind `desktop`，glyph `▣`）：
- 状态机 loading / connected / closed / error；连接后默认**只读观看**（agent 正在桌面里干活，抢鼠标须显式勾选「允许操控」，经 `enableInput/setInputEnabled/setTouchEnabled` 尽力生效）；断开/失败有重连按钮。
- SDK 脚本模块级单例加载；卸载时 `stop()/stopConnection()`；`ResizeObserver`+`MutationObserver` 维持等比缩放。
- 实测：真实 Ubuntu 桌面画面在面板内流式渲染成功（只读横幅 + 操控开关，零 ID 露出）。

E2E stub 掉票据接口（真流需要云凭证）：断言菜单行存在、错误态渲染、重连按钮、面板 DOM 不得出现 `ecd-` 形态 ID。

**工具条补全（同日）**：全屏观看 / 剪贴板同步 / 上传文件。SDK 方法名不靠猜 —— 直接下载 CDN bundle grep 确认：`setClipboardEnabled(bool)`、`uploadFile(file, showDialog)`、`enableInput/setInputEnabled/setTouchEnabled`。

- **全屏**：对整个 tab 根容器（含工具条）`requestFullscreen()`，被拒或不可用时降级为 `fixed inset-0 z-50` 覆盖层（Esc 可退）；`fullscreenchange` 兜底复位。缩放交给已有的 ResizeObserver，无需专门处理。
- **剪贴板同步**：连接时默认开（`setClipboardEnabled(true)`），工具条 checkbox 可关；iframe 早已带 `allow="clipboard-read; clipboard-write"`。
- **上传文件**：隐藏 `<input type=file>` → `session.uploadFile(file, true)`，`showDialog=true` 让远端桌面弹自己的进度 UI，本端无需再造。
- 重连语义：连接建立时从 `togglesRef` 应用当前开关状态（React 编译器规则禁止 render 期写 ref，镜像放在 effect 里）。

### D.4.9 附件走 OSS 直传（2026-08-21）

原链路的带宽问题：附件以 base64 分块经后端 `/execute` 灌进沙箱 —— 每个字节都挤 SSH 隧道，大文件不可用。新链路**字节永远不过后端**：

```
浏览器 ──presigned PUT──▶ OSS(openbox-assets-hz01, cn-hangzhou)
                             │
无影桌面 ◀──obx-file get──────┘   （阿里内网，快；隧道零流量）
```

**后端**：
- `file_assets` 表（Alembic `e5b7c9d1f3a6`）：上传台账 —— user/session、原名、OSS key、mime、size、pending→ready。字节不过后端，但**记录必须在库里**。
- `core/oss.py`：手写 V1 预签名（HMAC-SHA1 query 签名，bossip 同款 ~40 行，不引 SDK）。**关键坑（bossip 实测传承）**：string-to-sign 的 Content-Type 行必须与客户端实发严格一致 —— PUT 按申报 mime 签，浏览器必须原样发这个头；GET/HEAD 签空行。`core/aliyun.py` 抽出共享凭证链（env → aliyun CLI profile），desktop.py 同步复用。
- `api/assets.py`：`POST /api/assets`（建 pending 记录 + presigned PUT + 必发 headers）→ 浏览器直传 → `POST /{id}/complete`（HEAD 验对象真的落了，转 ready）→ `GET /{id}/url`（预览用新鲜 GET，可带 download disposition）。未配 OSS 时 503，前端自动回退旧链路。
- **`obx-file`** —— 用户要的"系统级 grep 式"终端工具：`sandbox/assets.py` 在沙箱装入 `/usr/local/bin`（sudo -n，退化 ~/.local/bin），`obx-file get <url> <dest>` / `obx-file put <src> <url>`（put 走空 CT 签名 + `-H 'Content-Type:'`），agent 可在终端手动使用。
- Prompt 链路：`PromptBody.attachments`（asset ids）→ 后台任务在 **run_loop 之前** `deliver()` 拉进 `/workspace/uploads/`（顺序保证：消息文本引用的路径 agent 一定能读到；单文件失败仅告警不炸整轮）→ 同时给用户消息挂 `FilePart{path, mime_type, asset_id, size}`。

**前端**：
- `useAttachments` 重写：OSS 优先（XHR 直传带真实进度百分比），`ApiError 503` 自动回退旧沙箱上传；图片附件在 composer 条里显示 objectURL 缩略图。
- 发送管线全程带 `attachments`：Composer → 两条路由 → useSendChat/useStartChat → prompt body。
- 聊天卡片 `AttachmentCard`：图片 → presigned GET 缩略图（`useAssetUrl`，staleTime 40min < 1h 过期）；其他 → 图标+名+大小卡；点击换新鲜下载 URL 打开。消息模型优先渲染 file parts，老消息回退 `[attachments]` 文本尾巴；顺带修掉「纯附件无文本消息不渲染」。

**桶开通**（aliyun CLI）：`aliyun oss mb` 建 `openbox-assets-hz01` + CORS（PUT/GET/HEAD、AllowedHeader *、expose ETag）。配置 `OSS_BUCKET/OSS_REGION/OSS_ENDPOINT`。

**实测端到端**：真 PNG 经 UI 直传 OSS（网络面板确认 PUT 打 `openbox-assets-hz01.oss-cn-hangzhou.aliyuncs.com`）→ 聊天卡片渲染 OSS 预览 → 桌面 `/workspace/uploads/e2e-pic.png` 2721 字节 magic 头正确。E2E `attachments.spec.ts` 内置生成 PNG 打真实栈；注意 send 门在上传完成（%→大小）后才开。

### D.4.10 多模态视觉 + 停止按钮（2026-08-21）

**多模态：模型在后端,必须让它看到像素。** 两条通路,都经 OSS,不挤隧道：

- **用户上传的图片**：`_to_llm_messages` 给用户消息的图片 FilePart 生成新鲜 presigned GET（key 确定性 `assets/{user}/{asset}/{name}`，零 DB 查询；URL 会过期所以**每次 LLM 调用重签**）。URL 挂在消息的 `_images` 带外字段 —— reminder/缓存/token 计数各 pass 继续处理纯字符串，`_finalize_message` 在 litellm 调用前的最后一刻转成 OpenAI 式 content 数组（Responses API 路径转 `input_image`）。**实测**：纯橙色 PNG 上传后问颜色，gemini 答「橙色」。
- **sandbox 产出的图片**：新工具 **`view_image`** —— 沙箱里 `file --mime-type` 嗅探 + 10MB 上限 → `obx-file put`（带 mime 的签名直传 OSS，obx-file 升级支持第三参 content-type）→ 落 `file_assets` 台账 → assistant 消息挂 FilePart（前端渲染预览卡）→ 消息装配时在 tool result 后追加 `_synthetic` user 消息携带图片（tool 角色消息不是处处能带图，user 消息全 API 合法）。**坑**：agent 工具是显式白名单（`AGENTS[...].tools`），注册进 registry 不等于模型能看到 —— 四个 agent 的白名单都要加。**实测**：agent 调 view_image 看沙箱里的橙色图，下一轮答「橙色」，聊天里出现图片卡。

**停止按钮不生效 —— 根因与 opencode 的差距。** opencode 的 abort 是 AbortSignal **直接取消在途请求**；我们是 `async for event: if abort.is_set(): break` —— 只在 **chunk 边界**检查。模型静默攒大工具调用的 10–30 秒里没有 chunk,break 永远走不到,停止形同虚设。三处修复：

| 修复 | 内容 |
|---|---|
| `_iter_until_abort` | 每次 `__anext__` 与 `abort.wait()` 赛跑；abort 到来即 cancel + `stream.aclose()`（撕掉 provider HTTP 流，opencode 语义） |
| 工具执行赛跑 | `hooks.wrap_execute` 包 task 与 abort 并发等待；长 bash 不再扛住停止按钮，弃单由 loop 的 ABORTED_TOOL_ERROR 清理收尾 |
| **per-run signal** | 原来 per-session 复用 Event：旧 run 已 set 的事件会误杀新 run（prompt_async 的 sleep(0.3) 只是缓解）。改为 opencode 式**每 run 一个新 signal**（`register_run` 覆盖槽位，`clear_abort(sid, signal)` 只清自己的），`trigger_abort` 打到最新 run；并修掉「loop 注册前按停被吞」的竞态 |

**实测**：流式输出中途按停,后续 **0 字符增长**、按钮即时回空闲(修复前 litellm 流会跑完)。E2E `stop.spec.ts`：发出后固定 2s 按停(几乎必然处于生成期,常为静默期 —— 正是回归场景)；等内容流出再点会赶上快答案跑完、按钮消失的时序。

### D.4.11 浏览器双模式:云端 CDP + 远程扩展（2026-08-21）

**背景判断。** 调研后确认 `container/dev-browser` 不是 fork —— 它是自研的 **CDP relay**:把 Chrome 扩展伪装成一个 CDP 端点（监听 9222,暴露 `/cdp` 给 Playwright、`/extension` 给扩展,自己实现了 `Target.attachToTarget` 等命令转发）。这个设计押对了方向:Chrome 136+ 封了默认 profile 下的 `--remote-debugging-port` 且每次连接弹确认框,而扩展路线绕开了它,还能**保住用户真实登录态**。

但它只解决了「操控用户自己电脑的浏览器」,没解决「操控无影云桌面的浏览器」——所以 agent 在云上开浏览器只能用 `computer` 一张张截图点。

**本轮实现两种模式并存:**

| 模式 | 驱动的浏览器 | 链路 | 有用户登录态 |
|---|---|---|---|
| `local` | 云桌面自己的 Chrome | Playwright → **Chrome 原生 CDP**（零 relay 跳） | 否 |
| `remote` | 用户自己的 Chrome | Playwright → relay `/cdp` → 扩展 WS → 后端 → 隧道 | **是** |
| `auto`（默认） | 优先远程,**掉线自动回落 local** | — | 视实际而定 |

**关键设计决策:云端不装插件,直接裸 CDP。** Chrome 137+ 封杀了 `--load-extension`,企业策略装 CRX 太重;而云桌面本来就没有「用户登录态」需要保护,插件在这条路上纯是负担。local 模式下 relay 只做**页面命名簿**（name→targetId,经 Chrome 的 `/json/*` HTTP 接口）,`wsEndpoint` 直接返回 Chrome 自己的 `webSocketDebuggerUrl` —— **Playwright 直连 Chrome,relay 不在数据面上**。这正是「走 CDP」要的性能形态。

云端 Chrome 用**独立 profile** `~/.config/obx-chrome` 启动:Chrome 136+ 拒绝在默认 profile 上开远程调试,独立 profile 是唯一可行解,而这里恰好没有登录态损失。

**回落是产品要求,不是异常处理。** `ensure_browser()` 在 remote/auto 下发现扩展未连接时,主动拉起云端 Chrome 并把 relay 切到 local,**不抛错** —— 用户关掉浏览器不该让任务死掉。只有两条路都失败才报错。

**模式选择落到配置。** 偏好存在既有的 `UserPreference.extra["browser_mode"]`（不新建表),`session/browser_pref.py` 是唯一读写口。注意**词汇分裂**:产品说 `remote`,relay 内部叫 `extension`,`relay_mode()` 是唯一翻译点,两套名字不互相泄漏。

三条落地路径:
- 设置页 UI（`GET/PUT /api/browser/preference`）
- **AI 提问后写回配置**:新增 `browser_mode` 工具（`get`/`set`）。它跑在后端,能直接读写偏好 —— 当任务需要用户身份而当前是云端浏览器时,agent 用 `question` 问,再用 `set` 记下来,下次不用重问
- 技能加载时注入 `<browser_mode>` 块,告诉模型**实际**跑在哪个浏览器上、以及是否发生了回落

**顺带补上系统提示的缺口:** 之前 `computer` 工具没进「web_search → web_fetch → dev-browser」这个阶梯,模型会拿它去干浏览器的活（实际发生过:agent 用 6 次截图点开百度）。现在明确:页面内的事一律走 dev-browser（结构化读取,token 少一个数量级,点的是元素不是猜的像素）,`computer` 只负责页面之外——原生应用、系统对话框,以及 canvas 这类结构表达不了的东西。

### D.4.12 上下文环 / 厂商标识 / 模型能力（2026-08-21）

**四项 UI 诉求 + 一个截图里暴露的真 bug。**

| 诉求 | 落地 |
|---|---|
| 左右侧栏加淡边框 | `Sidebar` 加 `border-e`、`WorkbenchPanel` 非浮层态加 `border-s`,都用 `--t-hair`(1px `#eeece9`)。用逻辑属性而非 `border-r/l`,RTL 下自动翻面 |
| 模型名不要 `openai/` 前缀 | 见下「前缀不是作者」 |
| 每个模型配真实上下文上限 | `ModelConfig.context_limit` 原本就有但全空;补齐 20 个模型,并把 `/api/agent/config` 改为**后端解析后再下发**(`get_model_context_limit`),前端不再从 id 猜 |
| 顶栏 token 挪到输入框旁做成圆环 | 新 `ContextRing`,悬浮出详情 |

**前缀不是作者。** 网关是 OpenAI 兼容的,于是**所有**模型 id 都是 `openai/` 开头 —— Claude、DeepSeek、Gemini 一律如此。消息徽章直接渲染 `session.model`,于是出现 `openai/deepseek-v4-flash` 这种自相矛盾的字样。三处修:

- `modelLabel(id, models)`:优先用配置里的显示名;配置里没有(会话钉在已下线模型上)则取 id 末段,至少去掉前缀
- `ModelPicker` 同样修 —— 只修徽章会漏,选择器上的 id 更显眼
- `ModelLogo` 按**模型名**取厂商标(lobehub/icons MIT 单色集,verbatim 内联)。**关键在于先剥掉 provider 前缀再匹配**:否则 `openai` 这个词命中的是网关前缀,任何没被识别的模型都会被盖上 OpenAI 的标 —— 单测 `model.test.ts` 专门锁住这条
- 下拉列表里那列 `provider` 每行都写着 `openai`,信息量为零且误导,换成选中态对勾

**ContextRing:圆环量的是「离压缩还有多远」。** 顶栏那句 `14.5k tokens` 要读懂得先记住当前模型的窗口 —— 而窗口会随旁边的选择器一起变。所以环放在模型名边上,`used/limit` 画成弧,详情(已用/上限/剩余/临近压缩提示)进 tooltip,中英文都有。上限取**选中模型**的 `context_limit`,取不到再退回 `token_usage.limit`(该会话上次真实跑用的窗口),两者都没有才隐藏。`formatTokens` 顺带补 M 档 —— `1,000k` 比 `1M` 难读。

**截图里的真 bug:`This model does not support image`。** DeepSeek 基础版是纯文本的,视觉是另一个 `-vision-*` 模型。而对话**比模型活得久**:在视觉模型上截的图会一直留在历史里,用户切到 DeepSeek 后照样发过去,换来一个网关 400 打死整轮。新增 `agent/vision.py`:

- `supports_vision(id)`:配置 `vision` 字段优先,其次家族启发式(`deepseek`/`qwq` 纯文本,`-vision`/`-vl` 例外);**未知模型默认视为多模态** —— 静默丢图比报错更坏,那会让模型煞有介事地描述一块它没看见的屏幕(这个坑之前踩过)
- `resolve_images(messages, model_id)`:模型看不了图就**根本不去 OSS 取**,直接换成一条告诉模型「有图被扣下了,别猜」的说明

> 这是老对手的第 8 个变体:**存进去的东西比供应商活得久**(模型名、tool-call id、推理变体、图片引用,现在是图片本身)。老规矩 —— 落库时归一,发送前再兜一次底。

**上下文数字别信二手资料。** 研究 agent 给 GPT 系列报了 1,050,000(还附了"官方"链接),用户一句「我用的是 codex 供应的,只有 256k」直接证伪 —— 而最初那次网页搜索里其实就写着 256K。改用**让网关自己说**:发一个必定超限的请求(1.3M tokens),被拒不计费,而报错原文会直接给出真实上限:

```
prompt is too long: 1300080 tokens > 1000000 maximum         # claude-opus-5 等 6 个
This model's maximum context length is 1048576 tokens...     # deepseek 全部 4 个
```

实测把二手资料又修掉两处:**DeepSeek 全线 1,048,576**(含被文档标成 128k 的两个 legacy id),Claude 全线 **1,000,000** —— 后者与研究一致,前者不是。

顺带用小请求把 20 个模型过了一遍连通性,结果值得记一笔:6 个 GPT 与 4 个 DeepSeek 正常;`gpt-5.2` / `gpt-5.3-codex` 返回「Codex + ChatGPT 账号不支持此模型」(账号层面,不是抖动)—— 已从配置移除;8 个 Claude 全部 `503 无可用渠道`(供应商侧额度/渠道问题,与代码无关)—— **保留**,因为这类是可恢复的,而运行时早有 `model_resolve` 兜底。

启发式(`_heuristic_context_limit`)只在配置没写时兜底,规则表是**有序**的 —— `gpt-5.4-mini` 里含 `gpt-5.4`,顺序错了就会给 400k 的模型配 1M 预算,压缩永不触发、每次长跑都死在供应商 400 上。未知模型一律给保守的 200k:猜大才是会出事的那个方向。

### D.4.13 tool-call id 的第三条规则（2026-08-21）

切换模型后报 `Responses API 400: Invalid 'input[25].id': 'fc_2126559__thought__..._JuxL_'. Expected an ID that contains letters, numbers, underscores, or dashes, but this value contained additional characters.`

**报错信息是错的。** 那个 id 62 字符、只有字母数字下划线 —— 长度和字符集两条既有防线都放行。用二分法向真实 API 逼问,真正的规则是**结尾不能是 `_`**:

| 通过 | 拒绝 |
|---|---|
| `fc_abcDEF` `fc_abcDEF9` | `fc_abcDEF_` |
| `fc_abcDEF-`（短横可以） | `fc_abcDEF__` |
| `fc__abcDEF`（前缀后紧跟下划线可以） | `fc_`（它本身也以 `_` 收尾） |
| `fc_` + 61 字符（总长 64） | 总长 65+ |

**而 `sanitize_call_id` 自己就是这种 id 的生产者** —— 它把 `/`、`+` 替换成 `_`,再截断到 64,两个动作都可能让 id 停在 `_` 上。Gemini 的 URL-safe base64 签名本来也带 `_`。

修复不再逐条追加"已知的坏情况",改成**正向白名单**:

```python
_FC_ID_OK = re.compile(r"fc_[A-Za-z0-9_-]{0,60}[A-Za-z0-9]")   # 总长 4..64,末位必须字母数字
```

不匹配就整体替换成 `fc_` + `sha256(raw)[:32]`（hex 天然满足末位约束）。必须是**纯函数** —— 同一个 id 会到达两次（`function_call` 与 `function_call_output`），API 靠 id 配对。写入侧 `sanitize_call_id` 补 `.rstrip("_-")`,并给全分隔符的输入兜底成 `"call"`（空 id 会把调用配到别人的结果上）。

**实测那条出问题的会话**（54 条消息 / 70 个 call id / 35 对）：修复前 4 个会原样发出触发 400,修复后 0 个非法,35 个唯一 id 归一后仍是 35 个 —— 无碰撞,配对完好。历史数据不需要迁移,发送前的兜底是确定性的。

> **让这个 bug 绿灯上线的元凶**:`test_call_id_compat.py` 里有一份 `ensure_fc_id` 的**手抄副本**（`"""Mirror of the normaliser in agent.llm"""`）。测试测的是副本,真实实现怎么漂移它都不知道。已改为 `from agent.llm import ensure_fc_id` —— 并把该函数从 `_stream_responses_api` 内部提到模块级,就是为了能被导入。

### D.4.14 重新生成（2026-08-21）

**出错的那一轮，用户手里没有任何可点的东西。** 报错卡片本身没有按钮，而下面的操作行在桌面端是 `md:opacity-0`、悬浮才显形 —— 一轮只产出错误卡片时，唯一的出路是重新打一遍 prompt。

先查了"刷新就消失"这个说法：**没能复现**。错误确实落库（`assistant_info.error` → `messages.error` 列 → `get_messages` 返回 → `InlineErrorCard`），实测造一个失败轮次再刷新，卡片还在。真正成立的是另一条：`mergeTurns` 里 `last.meta = metaOf(m)` **无条件**用最新消息的 meta 覆盖整个 turn，同一 turn 内后续消息会把 error 抹掉。已改为 `error: m.error ?? last.meta.error` —— 带 error 的消息在现行 loop 里必然是本轮最后一条（`StepOutcome.ERROR` 直接终止），所以这更像防御，但覆盖语义本身是错的。

**后端 `POST /session/{sid}/regenerate/{message_id}`**：删掉该 assistant 消息**及其之后的所有消息**，保留触发它的 user 消息 —— 这正好是 `run_loop` 期待的状态（它找最后一条 user 消息作答），所以不新建 user 消息、prompt 不会重复。

- 硬删而非 `superseded` 标记：软删会留在**每一次**历史读取里（压缩、token 计数、模型自己的上下文），除非所有读取路径都学会过滤，而漏掉一处就是静默的上下文 bug。
- BUSY 时按 `prompt_async` 的同一约定处理：`trigger_abort` 掐掉在跑的 loop 再删。**在活跃 loop 脚下删消息是唯一绝对不能做的事**。
- 实测：旧消息删除、**全库 0 个孤儿 part**、user 消息保留、新回复正常产出。

**前端两个入口**：错误卡片上一颗常驻按钮（失败时不该让人去悬浮找），操作行里一颗图标按钮（用户截图指的位置）。

**关键设计点：重试要跟随选择器。** 最常见的重试动机就是"这个模型挂了，换一个"，如果重新生成永远复用刚失败的模型，按钮在它最该有用的场景里等于没用。为此把 composer 的未发送选择从组件 state 提到 `stores/model-choice.ts`（按会话 key 存），错误卡片和操作行都读它，有选择才带 `model` 参数、没有就沿用会话模型。**实测**：Claude 全线 503 → 切到 DeepSeek V4 Flash → 点重新生成 → 换模型答出来了。

**删除这一轮**（`DELETE /session/{sid}/message/{message_id}`）。重新生成解决的是"再试一次"，但用户往往已经**手动重发绕过去了** —— 那张失败卡片就杵在历史中间，既删不掉也收不起。它还不是白占地方：一条只有 `step-start` 的 assistant 消息会跟着每一次后续请求当上下文送出去。

删除粒度取"**失败的一轮整体**"：错误消息 + 产生它的那条提问（仅当没有别的回复挂在那条提问上）。只删回复会留下一条无人应答的提问，那比错误卡片更碍眼。按钮因此叫「删除这一轮」而不是一个光秃秃的 ×——行为要和标签对得上。

接口**只接受带 error 的 assistant 消息**（实测:删 user 消息 / 删正常回复 / 删不存在的 id 一律返回 0，消息数不变）。这是"消掉一次失败"，不是通用的历史改写入口——后者会把工具调用配对拆散。

> `setMessages` 的合并规则是"谁的 parts 多留谁"，服务端删了消息之后它会把刚删掉的那一轮**原样复活**。所以 `useRegenerate` 和 `useDismissFailedTurn` 成功后都先 `clearMessages(sessionId)` 再失效查询。
>
> 顺带清掉了一个孤儿 i18n 键：`chat.regen`（"重新生成"）在语言包里躺了很久、代码里零引用 —— 按钮是设计里有、从没实现。现在归到 `meta.regenerate` 与操作行其它按钮同组。

**同轮修掉审计确认的两个跨供应商缺陷**（见 D.4.13 的同类问题）：

| | 位置 | 问题 |
|---|---|---|
| M1 | `llm.py` Responses 构造 | 一轮里既有叙述又有工具调用时，只发出 `function_call`，**模型自己的说明被静默丢弃**。没有任何东西会拒绝一个更短的历史，所以这是沉默的上下文丢失 |
| M2 | `llm.py` deepseek 分支 | `reasoning_effort` 原样转发。`variant` 跟着消息走，在 GPT 上选的 `max`/`xhigh` 会原封不动到达 DeepSeek；而 `reasoning_effort` 是 DeepSeek **支持**的参数，`drop_params` 不会剔除它，只会被 API 拒绝。已按 openai 分支同款钳制 |

> 修 M1 时顺手把消息构造从 `_stream_responses_api` 内部提成模块级 `build_responses_input()`。**原因是我差点重犯 D.4.13 那个元 bug** —— 逻辑埋在大函数里，测试唯一的办法就是再抄一份，而抄的那份会一直绿。

### D.4.15 够不着的能力（2026-08-21）

用户手动关掉了无影云桌面上的 Chrome。agent 正在 computer use 模式下干活，需要浏览器 —— 于是开始**找图标**：点 dock 坐标 (12,90)、(12,112)、(12,44)，按 `super`，输入 "Chrome"，按回车，最后按了 `alt+F4`。整轮没找到任何浏览器。

**后端一直有正确的恢复能力。** `ensure_browser()` 会用正确的 profile、policy 和 CDP 端口拉起 Chrome，而且 `ensure_chrome()` **探活端口、不信缓存**，用户手动关掉后再调用就能拉回来。relay 也不需要重启 —— 它每次请求都重新发现 CDP 端点（`discoverChromeWsEndpoint()`，还会记录 "Chrome restarted; CDP endpoint changed"）。

问题在于 `ensure_browser()` **只有加载 dev-browser skill 这一条路能触达**。模型站在 computer use 里，没有任何一个动作能打开浏览器，找图标就成了它唯一的选择。

> **而且找到了更糟。** 从桌面图标启动的 Chrome **没有 `--remote-debugging-port`**，dev-browser 根本连不上。图标搜索要么直接失败，要么留下一个"屏幕上看着正常、实际驱动不了"的浏览器，真正的失败推迟到好几步之后、离原因很远的地方才爆出来。

**缺陷类叫「够不着的能力」**：系统有这个能力，但 agent 站在当前位置伸手够不到。于是它用错误的工具即兴发挥，而即兴发挥要么失败，要么静默地造出一个坏状态。

修复是给 `computer` 加一个 `open_browser` 动作，直接委托给 `ensure_browser()`。放在 `computer` 而不是别的工具上，理由很简单：**模型需要它的时候正站在 `computer` 面前**，把出口开在它看不见的地方等于没开。三处说明（工具描述 / 系统提示 / SKILL.md）都写明：不要找图标，以及为什么找到了反而更糟。

**顺带撞出同一类的第二个缺口**：`general` agent 的白名单里有 `computer` 和 `browser_mode`，却**没有 `skill`**。加上 `open_browser` 之后它能把浏览器打开，然后没有任何办法驱动它 —— 只能退回去点像素。这个组合本身就自相矛盾：`browser_mode` 这个工具只有在你要驱动浏览器时才有意义。已补 `skill`；它不授予任何新权限（该 agent 早有 `bash`，skill 能让它做的事它本来就能做），给的是说明书。已加测试锁死这条不变量：**任何有 `computer` 的 agent 必须同时有 `skill`**。

> 没能在用户的真实无影云桌面上端到端验证 —— 后端重启后内存里的沙箱映射就没了，而重新 acquire 会走 docker provider 去创建容器，那是另一条路且会在用户机器上留下东西。验证靠的是 6 个单测，加上把 `ensure_chrome` 与 relay 的恢复逻辑逐行读完（原本怀疑 relay 会缓存死连接，读完确认不会，于是**没有**加多余的加固）。

### D.4.16 云桌面换回品牌版 Chrome（2026-08-21）

云桌面上一直装着 **Chrome for Testing 152**（391MB）和 unpacked 的 dev-browser 扩展，理由是「Chrome 137+ 移除了 `--load-extension`，只有 CFT 还认」。

**但 D.4.11 早就定了相反的事:云端不装插件，直接裸 CDP。** local 模式下 relay 只做页面命名簿，`wsEndpoint` 直接返回 Chrome 自己的 `webSocketDebuggerUrl`，Playwright 直连 Chrome —— **relay 不在数据面上，扩展从头到尾没被用过**。扩展只在 `extension` 模式下有意义，而那是用户**自己机器上**的浏览器，这台桌面根本不负责启动它。

也就是说，为了一个没人用的 flag，桌面上多背了 391MB 的第二个 Chrome。实现和设计文档不一致，这次按文档收敛。

| | 改前 | 改后 |
|---|---|---|
| 自动化浏览器 | `/opt/chrome-for-testing/chrome` (152) | `/usr/bin/google-chrome-stable` (151) |
| `--load-extension` | 传（CFT 认） | 不传 |
| 策略目录 | `chrome_for_testing` + `chrome` 两份 | 只留 `/etc/opt/chrome/policies/managed` |

**关键前提先验证了才动手**：Chrome 136+ 只在**默认 profile** 下封 `--remote-debugging-port`，而我们本来就用独立的 `.config/obx-chrome`。用备用端口 9334 起了一个品牌版 Chrome 实测 —— `CDP_OK`，拿到 `webSocketDebuggerUrl`，确认可行之后才改代码。

清理时的一个坑：`/usr/local/bin/google-chrome` 是指向 CFT 的软链，而 `/usr/local/bin` 在 PATH 里**排在 `/usr/bin` 前面**。直接删 CFT 会留下一条断链，命令行敲 `google-chrome` 反而坏掉。已连同软链一并移除，现在 `google-chrome` 正确解析到 `/opt/google/chrome/google-chrome`。

**验收是在全部删干净之后重跑的**（杀掉浏览器 → `ensure_browser` 冷启动 → 真实 dev-browser 脚本）：`Chrome/151.0.7922.173`、`TITLE=Example Domain`、a11y 快照 5 个 ref。释放 391MB。

> `bootstrap` 脚本不装 CFT，全仓也没有别处引用 —— 那两样是早前手工装上去的，删掉不会自己回来。

**换成品牌版之后才暴露出来的两件事**，都是截图看出来的，不是测试报出来的：

1. **"Restore pages? Chrome didn't shut down correctly" 气泡。** 自动化浏览器是被信号杀掉的，所以每次重启 Chrome 都认为上次崩了。这个气泡由**浏览器**绘制，盖在页面右上角，CDP 碰不到它。`--disable-session-crashed-bubble` 在新版 Chrome 上已经失效，改成启动前把 profile 里的 `exit_type` 标成 `Normal`。
2. **然后它变成了静默恢复上次会话** —— 实测一次被杀的会话带回来 **10 个陈旧标签**，既占内存又污染 relay 的页面列表。管理策略 `RestoreOnStartup=5` **拦不住**（试过，标签照旧）。

最后按「自动化浏览器本来就该每次干净启动」来收口：启动前删掉 profile 里的会话文件（`Sessions/`、`Current Session`、`Current Tabs`、`Last Session`、`Last Tabs`）—— 没有崩溃可报告，也没有东西可恢复，两条路一起堵死。这些是纯临时状态，cookie、登录态和历史记录在别的文件里，不受影响。**实测冷启动标签数 12 → 1。**

> 还有一个只有看截图才会发现的残留：dock 里仍挂着一个 Chrome for Testing 图标。原因是 CFT 进程**还活着**（Linux 允许进程跑在已删除的二进制上，`/proc/PID/exe` 显示 `(deleted)`）。它们逃过了 `pkill` 是因为我按 `obx-chrome` 这个 profile 路径匹配，而这些是从 dock 图标手工启动的、用的默认 profile。改按 `/proc/*/exe` 精确定位后清掉 9 个。

### D.4.17 「Open xdg-open?」——一次没能证实的修复（2026-08-21）

用户报了应用跳转弹窗。**这一节的结论是「原因已查清，但修复未能验证」，过程比结论更值得记。**

**先说查清的部分。** 弹窗由浏览器进程的 `ExternalProtocolHandler` 绘制，它的 `GetBlockState()` **从不查 `URLBlocklist`** —— 只看一份硬编码的 denied scheme 列表、`mailto` 例外、`AutoLaunchProtocolsFromOrigins` 策略，以及每个来源的用户偏好。`URLBlocklist` 走的是另一条路（`PolicyBlocklistNavigationThrottle`，导航节流层），而渲染进程发起的跳转会经 `HandleExternalProtocol` 直接进 handler，不过节流层。

**所以我们那份 29 条 scheme 的 blocklist 对这个弹窗从来没有作用。** 顺带还有个语法问题：Chrome 的过滤器格式是 `[scheme://]host[...]`，我们写的 `douyin:*` 即使在节流层也匹配不上。

**再说我错在哪。** 我先做实验：加 `bitbrowser:*` 弹窗还在，改成 `bitbrowser://*` 弹窗消失 —— 于是宣布修好了。**这个证据是假的。** 补做阴性对照才发现：把 URL 策略**整个删掉**，弹窗同样不出现，`SCHEME_ATTEMPTS=0` —— 那次点击压根没触发探测。我把「没复现」当成了「被挡住」。

> 教训很直接：**一个「问题消失了」的观察，在拿到阴性对照之前不是证据。** 尤其当触发条件本身不稳定的时候。

**然后我连复现都做不到了。** 试了五种方式，全部静默忽略、只得到 `chrome-error://chromewebdata/`，一次弹窗都没有：

| 尝试 | 结果 |
|---|---|
| 脚本 `location.href = "bitbrowser://cc/"` | 无弹窗（缺用户手势，被静默拦） |
| 注入 `<a>` 真实点击（有手势） | 无弹窗 |
| 裸 Chrome：无策略文件、无 `--disable-features`、全新 profile | 无弹窗 |
| 专门注册一个 `x-scheme-handler/bitbrowser` 处理程序 | 无弹窗 |
| creator.douyin.com 实站点击 | 探测未触发 |

**结论与当前状态：**

- `URLBlocklist` 改成了默认拒绝（`["*"]` + 自动化需要的 allowlist）。它**不解决弹窗**，代码注释里已写明这一点，免得下一个人再误信。它买到的是节流层的实际拦截，且比 29 条空转条目严格更好。实测 example.com 与 creator.douyin.com 均正常（点击后 132 个 a11y ref）。
- 移除了 `--disable-features=ExternalProtocolDialog` —— Chromium 里**不存在**这个 feature，未知名字会被静默忽略，它一直是空操作。
- 唯一作用于弹窗那条代码路径的是 `AutoLaunchProtocolsFromOrigins`，但它靠**真的启动**外部程序来消除提示。在用户与 agent 共用的桌面上，这比弹窗更糟，**因此没有启用**。
- 我一度加了「只对 Chrome 进程生效的空操作 `xdg-open`」作为兜底，但 `env PATH=` 在这条 sudo 链上没能传进 Chrome 进程（实测其 PATH 里没有该目录）。**没验证成功的机制不交付**，已连同桌面上的残留目录一并撤除。

其余排除项：`page.route("**/*")` 拦不到（自定义 scheme 的导航不走网络拦截层，`ABORTED=0`）；CDP 无任何命令可以拦截或关闭这个对话框（它不是 JS 对话框，`Page.javascriptDialogOpening` 不会触发）。

> 现有的兜底仍然有效且是文档化的：dev-browser 脚本连续两次超时 → 用 `computer` 截图 → Escape 或点 Cancel → 回到脚本继续（见 D.4.15 与 SKILL.md）。

### D.4.18 dev-browser 慢的真正原因（2026-08-21）

用户报「用 dev-browser 很慢，中间应该出错了」。那一轮**任务其实成功了**（二维码拿到了），总耗时 181 秒。按 part 时间戳逐段拆开，答案很干脆：

```
  0.0s  用户提问
  9.8s  tool:skill dev-browser
111.8s  step-finish                 ← 光加载 skill 就 101.9 秒，占全程 56%
118.7s  bash: npm run start-relay
...
180.8s  完成
```

**根因不是「慢」，是一个一直在白等的 bug。** 冷启动逐段计时：

| 步骤 | 耗时 |
|---|---|
| 隧道往返基线 | 0.13s |
| `ensure_x_helper` / 策略安装 | 各 0.1s |
| **chrome 启动命令返回** | **32.08s**（timeout=30，即卡到超时） |
| chrome 端口可用 | 1.07s（1 次轮询） |
| **relay 启动命令返回** | **62.19s**（timeout=60，同样卡到超时） |
| relay 可用 | 1.12s（1 次轮询） |

进程早就起来了，是 `client.execute()` 一直挂到超时才返回 —— 每次冷启动白等 90 秒。

**而问题不在脚本的分离写法。** 逐一实测，`setsid`、`nohup`、`sudo -u` 包装、`( … & ) >/dev/null 2>&1 </dev/null`——**五种写法全部挂住**，连最裸的 `( setsid sleep 25 … & )` 也一样。结论是这个 action server 的 exec **会等所有后代进程**，fd 重定向和会话分离都不管用。

所以修法是不再等：启动命令给一个很短的 settle 超时，就绪与否**只由端口轮询判定**（那本来就是唯一有意义的信号）。前提是超时不能杀进程 —— 实测确认：用 3 秒超时发起一个 20 秒的标记进程，exec 在 5 秒返回 `exit=-1`，而标记进程**活满全程**。

**实测收益**：`ensure_browser` 冷启动 95s → **12.9s**；冷桌面下加载 skill 101.9s → **19.7s**；「浏览器就绪 + 首个脚本跑完」合计 17.7s。热路径不变（0.24s）。

**同一轮里另外两处浪费也修了：**

1. **模型在 `page.evaluate()` 里写了 TypeScript**，得到 `SyntaxError: Unexpected token ':'`，白跑一个来回。SKILL.md 本来就有这条警告，但埋在中段，而模型是从顶部的模板复制的。已把警告移到复制点，并写清为什么容易踩：`npx tsx` 会编译外层文件，所以顶层加注解没问题 —— 正是这一点让人以为回调里也可以。（我自己当天也犯了同一个错，这本身就说明埋着的警告不起作用。）
2. **模型手工跑了 `npm run start-relay`**，因为 SKILL.md 让它这么做 —— 但加载 skill 时 `_browser_readiness` 已经把 relay 起好了，而手工那条会**先杀掉正在跑的那个**。已改成「不需要 Setup，除非 `<browser_mode>` 报了失败」。

> 修复过程中我自己引入过一个缺陷：`_fire_and_forget` 最初用 `except Exception` 兜底，把真实错误（隧道断开、二进制缺失）也吞掉，变成一次 15 秒轮询后一句含糊的「没起来」。**是测试套件从 3.1s 变成 23.1s 暴露的**，已收窄成只容忍超时。

### D.4.19 任务卡：让清单说清「这一步在做什么」（2026-08-22）

原来的清单是 composer 上方一张 `PlanCard`：REST 拉当前 todo，运行结束就消失。它说了**有哪些任务**，上面的工具链说了**发生了什么**，但没人说**哪件事属于哪一步**。

**做法：把归属交给消息流，而不是 todo 存储。**

`todo_write` 每次写入都往会话追加一个 `TodoPart`（**append，不是 upsert**）。todo 存储只保留最新一份清单，答不出「这条命令跑的时候哪个任务在进行中」；而 part 流按 `created_at` 有序，相邻两个 `TodoPart` 之间的工具调用，就属于当时 `in_progress` 的那个任务。回放、刷新、历史会话都只依赖这一条线索。

- 清单出现之前 / 全部收尾之后的调用留在卡片外，不硬塞进某一步。
- **没有 todo part 的回合渲染完全照旧**，连 `todo_write` 行都保留 —— 那些会话没有卡片可以承载它们。
- 进度条是假的，但**是确定性的假**：`f(started_at, 已完成调用数)`，纯函数，不读组件内计时器。所以两个标签页一致，刷新接着走而不是归零。为此 `TodoItem` 新增持久化的 `started_at` —— part 级时间戳前端根本拿不到（`ToolTime.start` 是死代码，`parts.created_at` 不下发）。

**双作者的清单。** 用户能加一步、能划掉一步，于是全量替换必须变成**合并**。四样东西要在模型的重写里活下来:条目 id（否则展开态和进度条每次重置）、`started_at`、用户加的条目、用户的取消。匹配先按 subject 全表跑一遍再按位置兜底 —— 一遍会让前面条目的位置猜测抢走后面条目的真匹配。

**三个只有真机跑才会暴露的问题：**

1. **卡片要刷新才出现。** part 事件按 user id 路由，`save_part` 漏传 `user_id` 就默认 `"default"` —— 数据落库了（所以刷新有），事件发给了一个没人连着的用户。其余每一处 `save_part` 都传了，只有这里没传。
2. **模型把活全干完再一次性标完成。** 清单最后是对的，过程一片空白，归属也就无从谈起。**工具描述自己就是元凶**：第 8 条白纸黑字写着「全部做完后再调一次把剩余项标记为已完成」，与下方「不要攒批」直接矛盾。改写描述、把节奏规则提到最前，另加两道在犯错当下就说话的兜底：`todo_write` 自己的返回值，和「清单有待办却无人进行中」时的提醒。
3. **模型把用户加的任务取消掉了**（"已完成的任务没有后续待办事项"）。条目挡住了「被省略」，没挡住「被取消」—— 用户唯一能做的操作，恰好是模型能撤销的。现在合并拒绝这个状态转移并在工具结果里说明理由。各管各的：用户取消模型的，模型完成用户的，谁都删不掉对方的。

> 方法上值得记一笔：这三个都不是读代码读出来的，是**挂上 WebSocket 跑真模型**跑出来的 —— 单测全绿的时候，②和③正在稳定复现。那次录下来的调用序列现在是一条 fixture 测试。

> 已知并接受的脆弱点：模型改写任务措辞会让 id 漂移（该任务的进度与展开态重置）。位置匹配能缓解，不根治。

### D.5 E2E 约定

- 后端登录限流 5 次/分钟/IP → E2E 采用 **setup project + storageState**：一次真实表单登录（本身即登录用例），其余 spec 复用 refresh cookie 恢复会话；`workers: 1` 串行。
- 无 headless-shell 网络下载依赖：`channel: "chromium"` 用完整浏览器跑新无头模式。
- 已覆盖：落地页访客视角、密码登录、发消息→WS 流式回复、语言切换全界面、主题切换+服务端回灌+还原、文件面板（真实沙箱树+内容预览）。

### D.6 工程结论

- `shared/api` 允许平台资源客户端（auth-store、containers、http、ws），修订反模式 12。
- 跨特性联动（聊天「审阅」→ 打开工作面板）走 `shared/events/bus.ts` 应用内事件，不做特性横向 import。
- 流式消息 store（chat 特性）与 Query 缓存物理隔离；初始快照走 Query，增量走 WS（§7.4 落地形态）。

---

## 附录 E · 定时任务页（2026-08-25）

> 背景：后端 cron 子系统补齐生产化能力（服务层校验/配额/最小间隔 5 分钟、原子认领、连败 10 次自动停用、NO_REPLY 静默约定、token 计量、整点错峰、webhook 投递含 SSRF 防护），v2 同步补上管理界面。设计稿没有该页面，视觉沿用设置页既有模式（导航 pill + 卡片列表），不新造样式。

- 页面位于 **设置 → 定时任务**（`features/cron`，spec §3.1 目录清单中的预留领域），列出该用户**全部**任务（跨会话）——对齐市场形态（ChatGPT Tasks / Manus / Devin 均为全局管理页 + 会话内自然语言创建双入口；会话内入口由后端 `cron` 工具承担）。
- 表单四种计划模式（每天/每周/按间隔/Cron 表达式）在提交前于前端按后端同一规则预校验（最小间隔常量 `MIN_INTERVAL_MINUTES` 镜像后端 `cron_min_interval_seconds`）；后端拒绝时按 §10.7 显示兜底文案，不透传 detail。
- 运行历史行内展示 token 消耗与「无事可报」（NO_REPLY）徽标；「查看过程」链接到临时会话（保留 24h，文案已提示）。
- 会话下拉复用工作区 `["sessions", userId]` 缓存键（§7.2 同键共享，非跨特性 import）。
- 新增 i18n 命名空间 `cron`（zh-CN/en-US 同步交付）；`settings` 命名空间加 `nav.cron`/`hint.cron`。
- 语言偏好经外观 store 已持久化至 `preferences.extra.locale`，后端注入文案（[定时任务]/摘要标签/静默指令)据此本地化。

### E.1 会话内入口与运行档案(2026-08-25 第二轮)

- **运行会话实体化**:cron 临时会话为 `sessions.kind='cron'` 的真实会话(alembic f1a2b3c4d5e6),不入侧栏(靠 parent_id)、不占会话配额(count_by_user 排除)、保留策略=每任务最近 20 次 transcript + 30 天上限(reaper 按窗口修剪,超窗 run 行保留摘要但 temp_session_id 置空,UI 随之隐藏「查看过程」)。
- **工作面板「定时任务」tab**(◷):workbench 只扩展 TabKind/菜单/骨架;内容组件 `CronPanelTab` 由装配层(WorkspaceLayout)以 `cronTab` slot 注入——workbench 与 cron 两特性零横向 import。
- **顶栏状态胶囊 `CronStatusPill`**:经 Topbar `statusSlot` 注入;当前会话无任务时不渲染;点状态色(执行中呼吸/成功 sage/失败 danger+连败角标/停用灰)+ 距下次执行时间;hover Tooltip 两行(上次/下次);点击经 `workbench.open`(kind:"cron")应用内事件打开面板 tab。
- **WS 实时**:`cron.job.*` 七个事件入 `shared/ws/events.ts` 契约;`useCronLiveEvents`(pill 挂载)收到即失效 cron 查询——运行开始/结束秒级反映到 pill 与面板。
- **运行档案**:每次运行(含静默与失败)追加至项目工作区 `cron/<YYYY-MM-DD>.md`(按任务时区),任务 prompt 内置提示 agent 可查阅该目录(廉价跨次记忆);文件面板可见、可 git 提交。

### E.2 项目级定时任务与独立入口(2026-08-25 第三轮)

- **归属改为项目**(alembic a7b8c9d0e1f2):`cron_jobs.project_id` 为归属主键语义;`session_id` 降级为可空「通知会话」——聊天里创建的任务自动携带并把结果注回该会话,页面创建的任务只落运行记录与 cron/ 日志。会话删除仅置空通知引用(任务存活);项目删除级联软删其任务。配额随之改为每项目 10(`cron_max_jobs_per_project`)。
- **入口迁移**:设置页 cron tab 移除;新增独立路由 `/app/cron`(懒加载 CronRoute),侧栏「新建对话」下方新增「◷ 定时任务」入口;Topbar 对该路由显示专属标题并隐藏面板开关。
- **侧栏「新建对话」按钮**按用户反馈去除 a200 填充,改为 `border-hair` 描边 + hover hairsoft。
- 面板 tab 与顶栏胶囊改为**按当前会话所属项目过滤**(经共享 `["sessions", userId]` 缓存解析 project_id)——同项目的所有会话都能看到该项目的任务与状态。

### E.3 侧栏中的定时任务会话(2026-08-25 第四轮)

- 侧栏顶部三行改为 DEEIX 参考样式:左对齐行 + 图标列,「新对话」的 + 号裹圆形浅底 chip(hover 轻放大),搜索改为行式无边框输入,「定时任务」入口为裸图标行。
- **cron 运行会话回到侧栏**:`list_sessions` 放行 `kind='cron'`(无论是否带通知会话的 parent),task 子代理会话(normal+parent)仍隐藏;`SessionRow` 对 `kind==='cron'` 在标题前渲染小时钟图标(带 aria-label)。临时会话标题去掉「[定时]」前缀——图标接管身份标识,标题即任务名。配额与保留策略不变(不计配额、每任务 20 次 + 30 天)。
- **E.3 补充(同日)**:历史 cron 会话标题的「[定时] /[Cron] 」前缀经数据迁移 b8c9d0e1f2a3 剥除;项目组头下新增图标分段筛选(💬 会话默认 / ◷ 定时运行,后者带数量角标),仅在该项目存在 cron 会话时出现,搜索时忽略筛选以免藏住命中项;筛选状态不持久化,刷新回默认「会话」。

### E.4 聊天创建定时任务(2026-08-25 第五轮)

- **skill**:`backend/.openbox/skills/scheduled-tasks/SKILL.md`(host 侧,agent 的 skill 工具已做容器+host 合并,容器同名优先)——引导流程:先 3-4 句说明工作方式(项目归属/cron 日志/回发对话/静默),再问「安排什么+何时运行」,确认后调 cron 工具;内含时区换算、5 分钟下限、NO_REPLY 建议与边界说明。`/api/agent/skill` 列表同步改为容器+host 合并,与 agent 视角一致。
- **入口**:定时任务页「新建定时任务」改为下拉(shared/ui/Menu):「手动创建」=原表单;「聊天创建」=ChatCreateDialog 先选项目 → POST 创建该项目会话(标题「设置定时任务」)→ prompt_async 预设引导词 → 跳转会话,agent 端由 skill 接管引导。
- 引导词与全部文案入 cron 命名空间(zh/en)。实测:agent 主动加载 skill,开场即「说明方式 + 两问」,与 ChatGPT Tasks 引导形态一致。

---

## 附录 F · 资源中心（2026-08-26）

> 背景：附件此前只有「上传即发送」一条命，OSS 里的对象没有任何浏览入口，模型经 `share_file`/`view_image` 交回的产出也只存在于某条历史消息里。本轮把 `file_assets` 从「上传流水账」升级为**对象存储的索引**，并按 workspace 中 DEEIX-Chat「文件」页的形态做出资源中心（`features/resources`）。**管理的是 OSS，不是本地目录** —— 沙箱容器会被回收，资源不会。

### F.1 数据模型（alembic c9d0e1f2a3b4）

`file_assets` 增四列一索引：

| 列 | 语义 |
|---|---|
| `project_id` | 一级分类。上传时取显式入参，缺省取会话所属项目；为空即「未归档」 |
| `source` | 二级分类：`user`（人上传）/ `agent`（模型在沙箱里产出后推出去的） |
| `transient` | 计算机使用的逐动作截图。留给会话 transcript，**不进资源中心列表** |
| `is_deleted` / `deleted_at` | 软删除。OSS 对象真删，行留着——历史消息的 file part 指着它 |

- 存量数据由迁移回填：`parts.data->>'asset_id'` 挂在 assistant 消息上 ⇒ `source='agent'`；该 part 的 `transient` ⇒ 同步置位。
- **归档时机**：`_attach_file_parts`（发消息时）为尚未归档的附件补上项目与会话——「@ 引用一个未归档资源并发出去」这件事本身就是归档动作；已有归属的资源**不重挂**，避免旧资源被新会话夺走。

### F.2 后端接口（`/api/assets`）

- `GET ""`：`project`(id/`all`/`none`) × `source` × `kind` × `q` × `sort`，返回带**新鲜预签名 GET** 的列表；`kind` 由 mime+文件名派生（`api/asset_kinds.py`，唯一分类点，前端图标/筛选/预览模式共用），因此在 Python 侧过滤而非 SQL；单次扫描上限 `_SCAN_CAP=3000`。
- `GET "/usage"`：已用字节 / 配额（`OSS_USER_QUOTA_BYTES`，0=不限）。**接口在，UI 不在**——存储卡片按验收反馈撤掉了（§F.8），端点留着，卡片要回来时不用重开。
- `PATCH "/{id}"` 改名（只动展示名，OSS key 不可变）；`DELETE "/{id}"` 软删行 + `OssClient.delete` 真删对象（新增 `presign_delete`）。
- `GET "/{id}/text"`：**唯一一处让字节过后端的接口**。桶不允许浏览器跨域读，`<iframe>` 对多数文本类型会触发下载，所以文本/代码预览由后端代取，256 KB 封顶。图片/视频/音频/PDF 一律直接吃预签名 URL，不经后端。

### F.3 前端形态

- 路由 `/app/resources`（懒加载），侧栏「资源中心」入口带当前项目；Topbar 对该路由显示专属标题并隐藏面板开关。
- 三栏：**项目分类栏**（一级=项目，展开后的二级=全部/我上传的/模型产出——这个嵌套*就是*需求里的二级分类）→ **列表栏**（标题+搜索+上传，全选/筛选/排序；宽度可拖，见 §F.8）→ **预览栏**（图片/视频/音频内联播放，PDF iframe，文本/代码走 §F.2 的接口，其余给下载卡片）。
- 筛选、排序、搜索、当前打开的资源**全部进 URL**（§8.4）：`?project=&source=&kind=&q=&sort=&id=`。
- 列表按 100 条一页增长（footer「加载更多（已显示 / 总数）」，`hasMore` 为假时才说「已加载全部 N 个」）——页大小不进 URL（它是滚动位置那一类的临时状态），但**进 query key**，否则加大页数会命中同一份缓存。
- 上传走 `shared/api/upload.ts` 的 `putToStorage`（纯传输、无业务语义，chat 与 resources 共用），落在当前浏览的项目下。

### F.4 输入框引用（@ / + / /）

需求是「@ 之后默认进当前项目，也能切别的项目」，三个入口功能一致。实现上**只有一条代码路径**：

- `features/resources` 出 `useResourceMention(sessionId, fallbackProject)`（数据：项目列表、当前 scope、来源、条目）；`features/chat` 的 `useMentionMenu` 接 `scope` 渲染成菜单首段，`MentionScopeBar` 提供项目下拉 + 来源 chip。两个特性零横向 import，**在 `routes/workspace/*` 组合**（§4.2 首选手段）。
- 选中资源**不插文本**：删掉触发段、把已在 OSS 的对象直接挂成附件（`useAttachments.addResource`，无传输），发送时后端照常 `obx-file get` 拉进沙箱。
- `+ → 资源中心` 与 `/` 菜单都落到同一个 `@`：`insertMentionTrigger` 在光标处补一个 `@`（必要时前置空格，否则 `resolveTrigger` 会把它当成前一个词的一部分而不触发）。

### F.5 实测（2026-08-26 浏览器验收）

- 上传 → 列表刷新 → 文本预览 → 改名 → 删除：删除后 `oss.head` 返回 `None`，对象确实不在桶里。
- `@color-test` → 选中 → 发送：用户气泡挂出缩略图，模型答「图片的主色调是橙色。」——引用的资源真的到了模型眼前。
- 未归档资源在 Default 项目的会话里 @ 引用并发出后，`project_id`/`session_id` 双双回填（F.1 的归档时机）。
- 深浅两色模式各验收一次（§9.3）。来源徽标改用 `text-a700`——`--t-a800` 在深色下没有反色定义，`bg-a100 text-a800` 是深底深字（该问题在 `tool-map.ts` 与 `QuestionDock` 上是既有的，另案处理）。
- E2E：`e2e/resources.spec.ts` 三例（项目→来源两级筛选打到真实 `/api/assets`；`@` 选中即挂附件且不写入文本；`+ → 资源中心` 落到同一个 `@`）。composer 的 `+` 加 `data-testid="composer-tools"`——它与元信息条的「更多」重名，靠 aria-label 选不中。
- 顺带修掉 `ProjectTree.tsx` 里 4 处既有 lint 报错（筛选枚举值被 `no-literal-string` 当成未翻译文案，提为具名常量），`npm run check` 现已全绿。

### F.6 移动端(2026-08-26 同日)

> `mobile/` 是 1:1 移植的 Flutter 端,不是 web 的响应式布局(workspace 本身是桌面尺寸,只有落地页有断点)。资源中心同步移植,针对手机重排。

- **重排**:三栏压成一屏 —— 一级(项目)与二级(来源)折叠成两行 chip 条,第三栏(预览)变成 pushed 页 `ResourceDetailPage`;web 的 hover 操作变长按操作单,内联改名变对话框,`全选` 进入多选态(勾选框 + 「删除 N 项」)。
- **特性边界**:mobile 与 web 同规矩(特性之间禁止互相 import)。chat 侧声明 `ComposerResourceSlot`(只用 `shared/models` 的类型),`features/resources` 出组件,`app/router.dart` 拼装 —— 与 web 在 `routes/workspace/*` 做的是同一件事。
- **上传**:新增 `file_picker` 依赖,资源中心的 `+` 与 composer 的 `+` 都直传 OSS(预签名 PUT,不经后端),关掉了 README 里记的「附件上传暂缺」。代价:iOS 最低版本 13.0 → **14.0**(`file_picker` 12 的 darwin 实现要求 14;10/11 与 `flutter_secure_storage` 11 的 win32 约束冲突)。
- **locale**:`assets/locales/` 按规矩逐字节从 `src/locales/` 复制(含新增的 `resources.json`),`i18n.dart` 的命名空间表加 `resources`。
- **iOS 模拟器实测**(iPhone 17 Pro Max):项目/来源两级筛选、搜索、类型筛选、多选、长按改名与删除(删除后 `oss.head` 返回 `None`)、文本/图片/视频预览、`+ → 资源中心` 与直接键入 `@` 的两条路径、切换项目 scope、`file_picker` 上传落库(`status=ready`,归到当前会话的项目)。最后 `@` 引用一张图发出去,模型答 `Purple` —— 引用的资源确实到了模型眼前。深浅两色 × 中英文各验收一次。

### F.7 顺带修掉的时间戳缺陷(2026-08-26)

资源中心是第一个显示 `file_assets` 时间的地方,一显示就露馅:所有时间都比真实时间早 8 小时(开发机在 UTC+8)。

- **成因**:`add_file_assets` 迁移把两列建成 `sa.DateTime()`(naive),而 `Base.type_annotation_map` 声明 `datetime → DateTime(timezone=True)`。SQLAlchemy 因此按 timestamptz 绑参,asyncpg 把我们传进去的 naive 值当**本地时间**解释再转 UTC,于是每次写入都偏移一个时区。全库 14 张表只有 `file_assets` 是 naive —— 这一列建错了。
- **修**:迁移 `d0e1f2a3b4c5` 把两列改成 `timestamptz`(与其余 12 张表一致),写入端改传 aware 的 `datetime.now(timezone.utc)`(`api/assets.py`、`sandbox/assets.py`)。读取端 `_utc()` 兼容迁移前留下的 naive 值。
- **历史数据不在迁移里改**:偏移量等于当年写入进程所在时区,SQL 无从还原;跑在 UTC 的部署本来就是对的,不是的应按自己的偏移自行修。本机的 167 行已用一次性 SQL 校正。

### F.8 验收反馈两条(2026-08-26 第二轮)

- **撤掉存储空间卡片**(web + 移动端):列表底部那张「已用 0% / 剩余 5 GB」信息量太低,还占掉一块本可以放文件的高度。前端删干净(组件、query、`ResourceUsage` 类型、`storage.*` 文案),**后端 `GET /api/assets/usage` 保留** —— 端点本身没问题,要把卡片放回来时不必重开一遍。
- **列表栏宽度可拖**:文件名长短差得远(`screen-part_01M0HJBGVH6YAWC4H4Y5PH509X.png` vs `a.png`),固定 `w-72` 要么截断要么浪费。`ColumnResizer` 复用侧栏那套机制(监听挂 window 上,指针甩出 8px 条也不脱手;拖动期间锁 `user-select`),宽度进 `features/resources/stores/ui.ts` 并持久化到 localStorage,夹在 220–560px。与侧栏唯一的差别:分隔线加了 hover 底色 —— 侧栏可以指望人知道那里能拖,一条列分隔线不能。
- 移动端没有分栏,只跟第一条(卡片一并删掉);`ResourceUsage` 模型与 `resourceUsageProvider` 同步删除,locale 重新逐字节同步。
- 实测:web 拖 288→423→夹到下限 220,刷新后仍是拖过的宽度;`user-select` 在 mouseup 后已复原(不会留下满页选不中文字)。移动端重装后列表直通底部,`存储空间` 节点数 0。

---

## 附录 G · 移动端工作面板菜单(2026-08-26 第三轮)

> 反馈:移动端的工作面板是一条横向滚动的 tab 条(审阅/终端/浏览器/文件/云桌面/定时…),要求参考 web 改成「菜单页 → 点进 tab 页」。

- **web 本来就是这个形态**:`WorkbenchPanel` 打开时是一个 **menu tab**(`MenuTab.tsx`),六行入口各带一条实时提示(待审数 / 沙箱在线 / 项目目录),点一行把这个 menu tab **就地变成**那个 kind;tab 条负责回头。移动端此前把 menu 这一层丢了,直接铺 tab 条。
- **移动端重排**:`WorkbenchScreen` = 菜单页(`WorkbenchMenu`,与 web 同一组 kind、同一份 glyph `± ›_ ⊕ ▤ ▣ ◷`、同一套 hint 逻辑),点一行 **push** `WorkbenchSurfacePage`。手机没有 tab 条,**返回手势 + 返回箭头就是那条 tab 条的替代**。
- **为什么是 push 而不是一个 state 标志**:先做的版本是「menu/surface 用一个 `_kind` 切换 + `PopScope(canPop:false)` 拦返回」,实测 iOS 上边缘滑动**直接失效** —— `canPop:false` 会让系统压根不启动交互式 pop,那个「回到菜单」的回调永远不触发,手势就这么被吞了。改成真正的 route 之后,滑动、返回箭头、返回栈全部是系统原生行为。
- **深链**:cron 胶囊(`tab=cron`)与聊天里的「审阅 →」(`tab=review`)在菜单之上 push 目标面,所以返回仍然落在菜单;`Paths.workbench` 的默认值从 `review` 改为 `menu`,顶栏面板图标现在开在菜单。`workbench.open` 事件此前忽略 payload 里的 `kind`,一并修好。
- **文案**:菜单页需要一个页面标题(web 的 menu 在 tab 里,已有标题),新增 `workbench:menu.title`(工作面板 / Workbench)。locale 文件是两端唯一真相且移动端逐字节复制,所以这个只有移动端消费的 key 也住在 `src/locales/`。
- **实测**(iPhone 17 Pro Max):菜单六行 + 三条实时提示(2 处待审 / 沙箱在线 / default)对齐 web;审阅打开真实 diff、终端连上真实 shell;边缘滑动从面回菜单、再滑一次退出面板;「审阅 →」深链直达且返回落在菜单;深浅两色 × 中英文各验收一次。

---

## 附录 H · 内置图片生成（2026-08-26）

> 目标：让 build agent 直接完成文生图与图生图，同时沿用 OpenBox 的 OSS 资产链路。供应商密钥只留在后端，聊天消息、资源中心和无影云工作目录引用同一个 OSS 对象。

### H.1 配置与能力边界

- `openbox.json` 新增 `image_generation`：`provider`、`model`、`default_size`、`default_quality`、`output_format`、`timeout_seconds`。密钥与 `base_url` 仍从既有 `provider.<name>` 读取，不在 skill、提示词或无影云目录复制。
- 默认模型为 `gpt-image-2`；无输入图调用 `/images/generations`，传入一个或多个 OSS 图片时调用 `/images/edits`，可选 PNG mask 作用于第一张输入图。
- `image_gen` 注册在后端并恒定列入 build agent 的固定工具白名单，因此普通 build 对话无需先加载技能即可使用；plan/explore/general 均不包含它。技能只注入说明，任何 frontmatter 字段都不能改变工具集合。
- `image_gen` 不依赖沙箱才能调用，也禁止通用 batch 并行，避免对同一消息附件和付费生成产生竞态。工具暴露由 agent 白名单决定，permission 只做限制；技能内容不参与两者。
- 失败不自动重试。图片 API 的响应存在已生成但客户端未收到的歧义，自动重试可能产生第二张图片与第二次计费。

### H.2 Skill 与 OSS 数据流

- host skill 位于 `backend/.openbox/skills/imagegen/SKILL.md`；短 description 用于发现，完整正文按需加载。frontmatter 的 `allowed-tools: [image_gen]` 只说明该技能围绕哪个既有工具展开，供文档与列表展示，对运行时工具可用性零效果。
- skill 按 OpenAI 官方 imagegen skill 改写：保留提示词结构、编辑不变量、输入图角色、尺寸/质量/格式规则，但要求 agent 统一调用 OpenBox 的 `image_gen`，不得在 shell 里临时拼 SDK/HTTP 请求。
- 文生图：后端请求供应商 → 校验真实返回格式 → 预签名 PUT 到 OSS → 新建 `file_assets(source='agent', transient=false, status='ready')` → 给当前 assistant message 写 `FilePart`。聊天图片卡和资源中心因此无需新增前端传输协议。
- 图生图：`input_images` 接收 `asset_id`（首选）或 `/workspace/uploads/<name>`；后端先校验用户所有权和 ready 状态，再从 OSS 取源图并以 multipart 传给编辑接口。只在无影云本地存在的图片先用 `view_image` 推到 OSS，返回的 `asset_id` 再用于编辑。
- 输出以 OSS 为耐久真相；若当前 turn 有无影云实例，再通过 `obx-file get` 拉到 `/workspace/generated_images/<name>`。无影云拉取失败不会删除已经付费生成且已入资源中心的对象。
- `image_gen` 已经负责聊天挂图，agent 不应再对结果调用 `view_image` 或 `share_file`，否则会制造重复资源。

### H.3 限制与校验

- 输入支持 PNG/JPEG/WebP，单图 50 MB、组合 100 MB；mask 必须为 PNG 且不超过 4 MB；单次同一提示可生成 1–4 个变体。
- `gpt-image-2` 尺寸支持 `auto` 或 `WIDTHxHEIGHT`：两边为 16 的倍数、最长边不超过 3840、宽高比不超过 3:1、总像素在 655,360–8,294,400 之间。质量为 low/medium/high/auto，输出格式为 PNG/JPEG/WebP。
- 当前配置的 `gpt-image-2` 不提供原生透明背景，工具会明确拒绝 `background=transparent`，不会静默换模型。
- 输出 MIME 与扩展名按实际文件魔数确定；供应商可对请求尺寸做归一化。浏览器验收中请求 `1024x1024`，供应商实际返回两张 `1254x1254` PNG，OpenBox 保存并展示真实文件。

### H.4 验收（真实供应商 + 无影云 + OSS）

- 直接启动后端 `:8080` 与前端 `:3000`，启动日志确认 sandbox provider 为 `wuying` 且 reconcile 成功；未构建 Docker/sandbox 镜像。
- 浏览器真实对话加载 `imagegen` skill 后生成 `openbox-imagegen-e2e.png`；图片卡正常显示，资源中心「模型产出」可见，对象已拉到无影云工作目录。
- 继续以第一张的 `asset_id` 调图生图，仅把蓝色立方体改为橙色；生成 `openbox-imagegen-edit-e2e.png`，证明 OSS 输入中间层与 `/images/edits` 路径可用。
- 数据库与 OSS 复核：两条资产均为 `ready/source=agent/transient=false`，数据库和 OSS 字节数分别一致为 1,134,953 与 1,201,770；文件头均为有效 `1254x1254` PNG。
- 后端 unit suite、skill validator、配置加载、工具注册与前端检查均纳入最终验收；回归测试明确断言 build 默认工具集合包含 `image_gen`、plan/explore/general 不包含，且加载任何 skill 都不会改变工具集合。
