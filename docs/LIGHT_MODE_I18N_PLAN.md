# Light 模式：三语（中 / 英 / 越）与首页语音助手卡

> 2026-11-08 起草。对照 `LIGHT_MODE_PLAN.md` §4、`LIGHT_MODE_IMPL_PLAN.md`。本文只定方案与分期，代码按 §6 的工作包分 PR 进 `light-mode`。

## 0. 一页结论

1. **三种语言**：`zh-CN` / `en-US` / `vi-VN`。语言只有一个来源（App 的语言设置），三处消费：App 界面、服务端下发的文案（首页入口、通知、语音兜底句）、助手（文字助理、专题页帮手、语音通话）。
2. **纯度规则落成机器规则**：中文版零英文、英文版零中文、越南语版允许夹英文。实现上就是「回退表」+「CI 门」：`zh-CN` 和 `en-US` 不回退到任何语言，缺 key 直接挡 CI；`vi-VN` 缺 key 回退到 `en-US`。服务端文案和模型输出同样按这张表办。
3. **首页**：新增「语音助手」卡，放到现在「做菜单」的位置；「做菜单」挪到「你拍我剪」右边的空位。首页成为：第一行「做一条视频」大卡，第二行「语音助手 | 做海报」，第三行「你拍我剪 | 做菜单」，两行填满。
4. **先后**：语音助手卡最小（1 天），先发；语言管道与 CI 门（2 天）其次；越南语文案与服务端文案（2 周）；助手三处（1–2 周）最后，其中越南语口播和越南语旁白要先实测供应商能力再承诺。

## 1. 现状（代码事实，2026-11-08 查 `origin/light-mode`）

### 1.1 App

- i18n 在 `mobile/lib/shared/i18n/i18n.dart`：`supportedLangs = ['zh-CN','en-US']`，回退固定 `en-US`；语言来自 `bossip:lang` 存储，否则设备语言以 `zh` 开头取中文，其余一律英文。`app.dart` 的 `supportedLocales` 也写死两种。
- 文案包两处：`assets/locales/` 28 个命名空间，字节级镜像 `frontend-v2/src/locales`（`scripts/check_locales.sh` 守门）；`assets/locales-mobile/` 3 个 App 专属命名空间 `light` / `first_video` / `onboarding`。
- 体量：web 镜像 28 个命名空间约 5,170 条字符串；App 专属 3 个共 324 条（light 145、onboarding 105、first_video 74）。
- Light 的页面（`features/light/*`）**没有硬编码中文**，全部走 `light:*` key。全 App 仅剩几处硬编码：`shared/utils/format.dart` 相对时间（中/英两套写死）、`chat/utils/task_status.dart` 日期「年月日」、`auth/widgets/lang_pill.dart` 的「中」。
- 语言选择入口：登录页 `lang_pill`（中 / EN 两粒）、设置页 `appearance_section`。
- 纯度现状：`zh-CN` 包里含拉丁词的字符串 437 条（多在 admin-*、skills、wiki、legal 这些 Pro/Web 命名空间；Light 可达的命名空间里主要是「bossip」「AI」「MB」「tokens」「Chrome」这类）；`en-US` 包里含中文的 14 条（legal 8、chat 3、settings 3，如「语言 / Language」「简体中文」）。
- App 不向后端发送语言：`http_client.dart` 只带 `Authorization` 与 `X-Workspace-Id`。推送设备注册带 `locale`（`notifications/schema.py`），这是后端目前唯一知道用户语言的地方，且只用于推送。

### 1.2 服务端下发的文案（目前只有中文，或中英两列）

- 首页入口 `backend/light/entries.json`：title / subtitle / price_hint / steps.label / subtypes.label / samples.label|description|needs / opening_card.text|chips 全是中文，App 原样渲染。`Entry.title` 限 20 字、`price_hint` 60 字，越南语会更长。
- 入口状态标签 `light/prompt.py::AVAILABILITY_LABELS`（给助理看的目录）中文。
- 推送 `notifications/templates.py`：中 / 英两列，按设备 `locale` 选，没有越南语列。
- 语音兜底句 `voice/phrases.py`：`LANGS = ("zh","en")`，非 zh 一律当 en。
- 音色说明 `voice/voices.py`：中文名 + 中英两段描述。

### 1.3 助手三处的语言规则

- 文字助理 `assistant/runtime.py` 的 ASSISTANT_PROMPT：「Use the user's language (Chinese unless they write in another language)」；再加 `assistant/profile.py` 的 `language` 偏好（用户在设置里填的自由文本，最长 20 字，设了就优先）。后端不知道界面语言，一个越南老板打开 App 不说话，助理开场默认中文。
- Light 专题页帮手 `light/prompt.py::LIGHT_MODE_TEXT` 与 `entry_fragment`：规则本身是中文，且没有「对用户用哪种语言」的指令；question / review 卡的标题、选项由模型生成，跟着提示词语言走。技能 `light-video` / `menu-maker` / `poster-maker` / `footage-edit` 的 SKILL.md 里产出文字（花字、字幕、菜单、海报文案、旁白）都默认中文。
- 语音 `voice/prompt.py::response_preferences`：有 `language` 就下「Speak your reply in …」，没有就跟用户说话语言。供应商是百炼 `qwen-audio-3.1-realtime-plus`（默认）/ Omni；音色表里「多语种」音色未验证过越南语。
- 视频旁白 `video/ims_catalog.py::KNOWN_VOICES`：IMS AI_TTS 全部是普通话音色，没有越南语。字幕字体只验证过 `AlibabaPuHuiTi`，越南语声调符号是否完整未验。
- 菜单 / 海报图上的文字由生图模型画，越南语带声调的拉丁字母能否画对未验。

## 2. 语言模型（设计）

### 2.1 一个来源、两条管道

- **来源**：App 的语言设置 `bossip:lang`，取值 `zh-CN` / `en-US` / `vi-VN`。首次进入按设备语言：`zh*`→中文、`vi*`→越南语、其余→英文。
- **同步管道（每次请求）**：`http_client.dart` 给每个请求加 `Accept-Language: <lang>`。后端中间件解析成 `request.state.ui_language`（只认这三个值，其余按 `en-US`），`GET /api/light/entries`、`POST /api/light/start`（开场卡）、错误 message 等按它渲染。
- **异步管道（没有请求在手时）**：切换语言或启动时 `PUT /api/me/preferences {"ui_language": ...}` 写进 `user_preferences.extra.ui_language`（浅合并 key，与 `ui_mode` 同一处）。推送、省心托管的定时产出、语音通话接通、助理系统提示词从这里读。设备注册的 `locale` 继续保留给推送做兜底。
- **优先级**（助手说哪种语言）：用户这次明确要求 > `profile.language`（设置里填的交谈语言，少数用户用）> `ui_language` > 用户输入语言。`profile.language` 的占位提示改成「默认跟随界面语言」。

### 2.2 回退表与纯度门

| 语言 | 缺 key 回退 | 允许出现的外文 |
| --- | --- | --- |
| zh-CN | 无（CI 失败） | 允许名单：品牌 `bossip`；单位 `MB` / `GB` 这类改写成「兆」「吉」后不再需要 |
| en-US | 无（CI 失败） | 无 |
| vi-VN | en-US | 英文整句、英文专有名词都允许 |

- `i18n.dart` 的 `_resolve` 改成按上表取回退链；web 的 `fallbackLng` 同步改成对象形式 `{ 'vi-VN': ['en-US'], default: [] }`。
- `scripts/check_locales.sh` 扩成 `check_locales.py`，新增三条：① 三种语言 key 集合一致（vi 可以缺，zh/en 不可以）；② `zh-CN` 中 Light 可达命名空间（见 §2.3）里不得有拉丁词，允许名单显式列出；③ `en-US` 里不得有 CJK。扫描范围先限 Light 可达命名空间，Pro/Web 命名空间只报告不阻断。
- 服务端也进门：`tests/unit/test_light_i18n.py` 对 `entries.*.json`、`notifications/templates.py`、`voice/phrases.py` 做同样的三条检查。

### 2.3 Light 可达命名空间（越南语本期要翻的范围）

App 专属：`light`、`first_video`、`onboarding`（324 条）。
镜像自 web 且 Light 四个 tab 与其二级页会碰到：`common`、`errors`、`auth`、`auth-center`、`chat`、`voice`、`billing`、`inbox`、`settings`、`legal`、`memory`、`resources`、`workspace`（约 2,030 条）。
合计约 2,350 条。其余 12 个命名空间（admin-*、wiki、skills、knowledge、cron、workbench、landing、companion，约 2,800 条）是 Pro/Web 界面，本期越南语不翻，按回退表显示英文，符合规则 2。

### 2.4 服务端文案的多语言形态

- 首页入口：`entries.json` 拆成 `entries.zh-CN.json` / `entries.en-US.json` / `entries.vi-VN.json`，结构相同，`id`、`skill`、`requires`、`steps.id`、`subtypes.id`、`samples.id`、`media_url` 这些非文案字段只允许在 zh-CN 里定义，其余两份只写文案；`light_entries(lang)` 加载时把文案覆盖到结构上，缺的 vi 文案按回退表取 en。`Entry.title`、`Step.label`、`Sample.label` 的长度上限从 20 放到 40，App 卡片已 `maxLines: 1 + ellipsis`。样片本身（`media_url`）不分语言。
- 通知模板：`TEMPLATES` 从四元组改成 `{"zh-CN": (title, body), "en-US": …, "vi-VN": …}`，`render_template(kind, name, locale)` 走回退表；设备 `locale` 和 `ui_language` 不一致时以 `ui_language` 为准。
- 语音兜底句 `phrases.py`：`LANGS` 加 `vi`，缺的回退 en。
- 助理目录 `AVAILABILITY_LABELS`、`availability_label()` 按语言输出；`ASSISTANT_LIGHT_RULES` 与 `LIGHT_MODE_TEXT` 是给模型看的规则，保持中文不动，但每个片段最前面加一行「对用户的所有输出（回复、question/review 卡的标题与选项、成片里的文字）用 {语言}；越南语可保留英文专有名词」。

### 2.5 助手三处

1. **文字助理**：ASSISTANT_PROMPT 的「Chinese unless…」改为「the user's interface language ({ui_language}) unless a conversation language is set in their profile or they ask otherwise」，`user_section` 把 `ui_language` 写进去。Light 的 `assistant_section` 目录行按语言渲染。
2. **专题页帮手与技能**：四个 Light 技能 SKILL.md 各加「产出语言」一节：菜单与海报上的文字、花字、字幕、旁白脚本、方案卡与 question 卡文字一律用会话语言；越南语成片的店名、菜名保留老板原文。`light-video` 的提示词尾句（「画面中不要出现任何文字…」）不变，因为它是对生成模型说的。
3. **语音通话**：`FrontFacts.language` 为空时填 `ui_language`，让越南语用户接通就是越南语开场。前提是供应商能说越南语（§5 第 2 条）。

### 2.6 越南语特有的能力缺口（先实测再承诺）

| 能力 | 现状 | 验法 | 若不支持 |
| --- | --- | --- | --- |
| 实时语音口播越南语 | 百炼 Audio 3.1 / Omni 多语种音色未验 | 在 light-test 用 `voice.model` 两档各打一通，`Speak your reply in "vi-VN"` | 越南语版语音助手卡保留，副标题注明「通话用英文」；或改为灰卡 |
| 越南语旁白 TTS | IMS AI_TTS 无越南语音色 | 查 `aliyun ice list-smart-voice-groups` 是否有 vi；无则试 CosyVoice / 第三方 | 越南语版「加旁白」默认关、只上字幕 |
| 字幕字体声调符号 | 只验过 AlibabaPuHuiTi | 用 `fe_e2e_style.py` 渲染一句带 ă â ê ô ơ ư đ 与五声调的字幕 | 换 Noto Sans 系并进 `VERIFIED_FONTS` |
| 菜单 / 海报图上的越南语文字 | 生图模型画字未验 | 同一提示词出 3 张，核对声调 | 文案由我们渲染叠加，不让生图模型画字 |
| 语音识别越南语 | 未验 | 同第一行 | 越南语用户只用文字 |

## 3. 首页：语音助手卡

### 3.1 用户看到什么

- 位置：第二行左（现「做菜单」位）。标题「语音助手」，副标题「打个电话，说一句就办」，图标电话。没有价格提示。
- 点一下：直接拨通（走 `openVoiceCall`：首次解释麦克风 → 悬浮窗权限 → 展开通话面板并 `start()`），不跳转到助理 tab。通话面板本来就是全局的（`VoiceCallHost`），在首页也能展开。
- 部署没开语音（`appConfig.voiceEnabled=false`）或助理 scope 还没就绪：卡变灰，副标题「语音暂未开通」，点了去助理 tab。
- 新顺序：`video`（大卡）→ `voice_assistant` → `poster` → `footage_edit` → `menu`。两行半卡：「语音助手 | 做海报」「你拍我剪 | 做菜单」。

### 3.2 系统做什么

- 服务端：`entries.json` 加一条 `{"id":"voice_assistant","kind":"primary","action":"voice_call","title":"语音助手","subtitle":"打个电话，说一句就办"}`。`Entry` 新增可选字段 `action: Literal["voice_call"] | None`；带 `action` 的条目 `skill` 可空、`steps` 可空、不需要 `opening_card`；`mode.start()` 对它返回 409 `LIGHT_ENTRY_ACTION`。助理目录（`entries_directory`）跳过 `action` 条目，避免助理给出「直达链接」。
- App：`LightEntry` 加 `action`；`home_screen._open` 在 `action == 'voice_call'` 时调 `openVoiceCall`；`entry_card.lightEntryIcon` 加 `voice_assistant → Icons.phone_outlined`；灰卡原因 key `light:home.voiceUnavailable`。
- 测试：`test_light_mode.py` 断言顺序与 `start()` 的 409；App widget test 断言第二行第一张是 `light-entry-voice_assistant` 且点击调用 `openVoiceCall`。
- 不动：`LightShell` 的助理 tab 顶栏电话按钮保留，两处入口同一个控制器。

## 4. 翻译生产方式

- 机器初翻（模型）→ 越南语母语审校 → 入库。初翻把 `en-US` 作为源（越南语允许夹英文，术语对齐英文更稳），`zh-CN` 作为参考。
- 术语表先定（10–20 个词）：助理、帮手、任务单、积分、套餐、云电脑、省心托管、样片、成片、你拍我剪、做菜单、做海报、发到抖音、掌柜、成绩复盘。中文版术语不变，英文版复用现有，越南语由审校定稿并写进 `docs/LIGHT_MODE_I18N_GLOSSARY.md`。
- 中文纯度整改：Light 可达命名空间里的「AI 生成」→「智能生成」、「200 MB」→「200 兆」、「Chrome / Edge」→「最新版浏览器」；`bossip` 作为品牌名保留。`legal` 命名空间 81 条含英文（法律文书）单独过一遍，法律术语的英文括注去掉。

## 5. 需要拍板

1. 品牌词 `bossip` 是否算英文。建议：唯一例外，不改。
2. 实测后若供应商不能越南语口播，越南语版语音助手卡怎么办。建议：保留卡，副标题注明「通话用英文」，不隐藏功能。
3. 越南语旁白没有音色时，「你拍我剪」越南语版默认不加旁白、只上字幕。建议：是。
4. 越南语翻译谁审校（母语）。
5. Pro/Web 的 12 个命名空间越南语不翻、回退英文。建议：是；web 的语言切换本期不加越南语。
6. 首页顺序「你拍我剪 | 做菜单」（做菜单填到你拍我剪右侧的空位）。建议：是。
7. 越南语版的「发到抖音」入口是否保留。抖音越南用户几乎没有，建议越南语版隐藏 `douyin_publish` / `review` 两个次入口，由 `entries.vi-VN.json` 的 `hidden: true` 控制。

## 6. 分期与工作包

| 期 | 内容 | 估时 | 验收 |
| --- | --- | --- | --- |
| I0 首页语音卡 | §3 全部 | 1 天 | light-test 真机：首页两行满，点语音卡直接接通 |
| I1 语言管道与门 | `Accept-Language` 中间件、`ui_language` 偏好、回退表、`check_locales.py` 三条门、App 加 `vi-VN`（`supportedLangs` / `supportedLocales` / 登录页三粒 / 设置页）、`format.dart` 与 `task_status.dart` 的硬编码改 key | 2 天 | 切越南语后界面英文（尚未翻）且无崩溃；CI 门对现状先只报告 |
| I2 App 文案 | §2.3 约 2,350 条越南语；中文纯度整改；英文 14 条中文 | 1.5 周 | 门对 Light 可达命名空间阻断且绿；三语各走一遍四个入口 |
| I3 服务端文案 | `entries.*.json` 三份、通知模板、语音兜底句、助理目录标签、`hidden` 入口 | 3 天 | `test_light_i18n.py` 绿；越南语 App 首页与开场卡全越南语 |
| I4 助手三处 | §2.5；§2.6 五项实测并按结果落第 2、3 条拍板 | 1–2 周 | 越南语用户：文字助理、专题页帮手、语音三处首句都是越南语；成片字幕声调正确 |

I0 独立，先合先发。I1 是 I2–I4 的前提。I2 与 I3 并行。App 版本从 1.1.x 升到 1.2.0（新增语言算功能）。

## 7. 风险

- 文案长度：越南语比中文长 1.5–2 倍，`LightEntryCard` 标题 `maxLines: 1`、步骤条 5 步一行、question 卡选项按钮可能挤。I2 期在 375 宽模拟器逐页看，必要时标题允许两行。
- 模型纯度：模型回复里夹英文（中文版）或夹中文（英文版）CI 门管不到。I4 期加轨迹抽检脚本，对最近 50 轮 Light 对话统计跨语言字符比例，超过阈值进超管消息中心（复用失败轮告警通道）。
- 成片文字：越南语字幕与海报文字是用户直接拿去用的，声调错一个就不能用。§2.6 两项实测不过，宁可降级为「我们渲染文字」。
- 旧版本 App：不带 `Accept-Language` 的请求按 `ui_language` 偏好，再没有按 `zh-CN`，与现在一致。
