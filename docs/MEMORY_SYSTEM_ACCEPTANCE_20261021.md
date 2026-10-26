# 记忆系统升级验收规范

本规范用于证明用户说过或设置过的内容能够正确保存、在合适场景主动使用、纠正后及时生效、删除后不被旧任务重新带回，并能从历史、知识库和技能中可靠取得需要的内容。验收同时检查用户看到的结果与实际的数据和模型输入，不能只以助手说“记住了”作为通过证据。

实施顺序见 [记忆系统升级实施计划](MEMORY_SYSTEM_UPGRADE_PLAN_20261021.md)。以下表格保留原验收门槛。最终判定及证据矩阵见第三十一节；此前按日期追加的未通过结论和失败原件是历史记录，继续保留，不把单元测试数量当成全部场景通过。

**最新结论（2026-10-26）：本次记忆 R1/R2 在冻结范围内达到小范围灰度的功能与性能验收标准，尚未发布。** 最后开放的 A01、A17 以及暂停状态虚报、确认参数异常已修复并复测。结论绑定第三十一节的源码、配置和证据，不代表默认关闭的开关已在线上启用，不扩大成生产部署或所有设备认证。按第二十八节及用户后续限定，使用内置浏览器、授权 PCM 模拟麦克风、iOS 模拟器和指定开发无影；网关、线上会员和云生命周期不在本轮范围内。仓库认知索引治理单独记录，不能冒充已对齐。

| 项目 | 要求 |
| --- | --- |
| 基线 | `a95a302324bfea81437a1b60f2f2871bf8303753`，分支 `codex/memory-system-upgrade` |
| 测试范围 | **只跑改动直接相关的定向测试和必要回归，不跑全量** |
| 环境 | 隔离本地测试库、测试账号、真实前后端；模型替身与真实模型结果分开记录 |
| 浏览器 | Codex 内置浏览器实际操作网页，按普通用户方式输入和点击 |
| 语音 | 真实麦克风或经过授权的虚拟麦克风音频输入；单纯注入文字转录不算真实语音验收 |
| 移动端 | 相关 Flutter 页面与协议发生改动时补对应定向测试和模拟器或实机验收 |
| 发布单元 | R1 为读写同步及历史知识闭环，R2 为经验技能闭环；完整交付要求两者通过 |

## 一 环境 样本和证据

使用两个隔离测试账号、同一账号的两个项目、一个共享项目及至少两个并行会话。准备有界样本：个人偏好、项目事实、带条件和数字的资料、同名图片、文档两个版本、未完成 ask、完成与失败的任务记录。复刻反馈时只保留必要语义和时间顺序，不提交线上手机号、密码、token、原始私人聊天或完整数据库导出。

原始证据放入 Git 忽略的 `.local-dev/memory-upgrade-20261021/`，保存前核实忽略规则。仓库只提交脱敏的验收索引和必要最小样本。每次验收记录实际 commit、schema head、开关、账号代号、模型及版本、网络和缓存条件、开始结束时间，避免前后对比落在不同配置。

每个用例至少记录：用例 ID、执行者、输入和操作、预期、实际结果、通过/失败/阻塞/未执行、请求或 operation ID、权威对象及 revision、消费者已应用版本、相关 trace、截图或音频证据位置、缺陷与复测记录。界面提示、后台任务成功、索引就绪和提供商已应用是不同证据。

API、数据库和工具可用于造测试前置条件、核对结果及故障注入；这些步骤不能冒充浏览器点击或真实语音。自然语言脚本只表达日常需求，不加入“必须调用某工具”“把某字段设成某值”等内部提示。模型调用未产生预期行为时保留失败现场，不靠反复换提示词筛出一次成功。

表中的句子是可替换的语义样例，不是运行规则。验收要证明表达变化后行为仍正确，而不是证明助手能回答表中原句。减少上下文和缩短等待必须与召回质量、版本正确性在同一批相关场景中比较，不能通过漏读必要资料取得性能通过。

## 二 记忆写入和入口一致性

表中“下一回复”指在保存提交之后才开始构建上下文的回复。已发出的模型请求或已播放语音单独标记，不能作为新版本生效的证据。表格定义门槛，各项实际进度以文末执行记录为准。

| ID | 普通用户操作与场景 | 通过条件 |
| --- | --- | --- |
| A01 | 设置页填“林姐”，在普通会话、个人助理和语音自然交谈；随后清空称呼 | 三入口采用用户称呼；未设置时为“老板”；助手名字不混作用户称呼，没有 XX；沿用通话开场称呼规则，不句句加称呼 |
| A02 | 在个人助理说“以后用英语和我聊”，保存后打开全新普通会话和语音，说“帮我想个周末安排” | 无需再次提醒就使用英语；原文、适用范围、回执及实际请求上下文对应，不能只证明数据库里存在 |
| A03 | 先记“我喜欢徒步，不喜欢太拥挤的地方”，新会话问“周末有什么安排建议” | 自然结合偏好，不凭空补地点或计划；只读少量相关背景，不倾倒全部私人历史 |
| A04 | 全局英语，当前通话说“这通电话用中文”，结束后新建文字和语音会话 | 当前通话中文，新会话仍英语；临时覆盖未写成永久全局设置 |
| A05 | 全局简洁，项目 A 要“这份方案写详细点”；项目 B 问一个简单问题 | A 的任务采用详细要求，B 仍简洁；无项目范围扩大 |
| A06 | 说“客户说以后叫他王总”，并引用客户喜欢的风格 | 不覆盖当前用户称呼与偏好；如保存客户事实，主体和来源可核验 |
| A07 | 前文明确说了店铺营业时间，接着说“记住刚才那个”；再测前文同时有多个候选 | 明确指代时绑定真实前文并保存；歧义时询问且不假报成功；空提取不得返回已记住 |
| A08 | 原话只要求“这篇用英语”，故障注入的模型提案却总结成“以后始终用英语” | 提案被拒绝或修正到有证据范围；不是仅因原话子串存在就赋予用户确认 |
| A09 | 保存后模拟提交响应丢失，使用相同操作重试；再自然重复同一事实 | 一个权威效果、同一可查询回执；允许 `no_change`，不能产生多条或重复通知 |
| A10 | 旧提取任务阻塞时，用户在设置页改称呼或风格，再释放旧任务 | 新设置保留；旧任务因字段版本或来源顺序被拒绝/重新评估，不按处理时间覆盖 |
| A11 | 在普通会话、个人助理和语音分别改同一事实；两入口同时修改同一旧版本 | 串行更新读到最新值；并发旧版本出现明确冲突，历史和来源完整，不无声丢失一次修改 |
| A12 | 暂停记忆保存后正常聊天并提出记忆请求，再恢复 | 暂停期间不新建记忆且如实说明；已有记忆的读取按既有产品设置，不能把“暂停保存”误作删除；恢复后可正常保存 |
| A13 | 通话保持连接，在网页另一个会话新增核心事实，再纠正和删除；分别让最终转写早于和晚于提供商开始响应 | 三种变更均触发版本核验；下一次适用回复使用已应用新版本，删除内容不再注入；有真实提供商的响应控制及应用确认时序证据；超时可见，不把发送成功视为应用成功；无法控制时不能声称已通过 |
| A14 | 同一用户切项目、共享会话、普通会话与个人助理；另一个账号尝试相同检索 | 只看到当前入口允许的资料；普通项目不加载私人跟进或其他用户记忆；缓存键变化不会串号 |
| A15 | 用户在网页手动建任务；再问语音“刚才那个任务怎么样了” | 从任务服务读到当前任务，能正确关联；历史摘要不冒充实时任务状态，也不自动重复建任务 |
| A16 | 语音询问 ask 后网页点击作答；另一次播放语音时点击无关卡片 | 前者不重复要求同一确认，后者不误打断；轮询兜底和通知重复均不产生重复操作 |
| A17 | 旧无图视频任务等脚本确认，上传水果店图片后说“我想用这张店里的照片重新做个视频” | 识别新需求或修改，按必要信息正常交流；不会把这句话当旧卡片答案，也不会陷入旧状态确认循环；转交有真实回执 |

R1 的 A01–A17 必须完成。其中 A01、A02、A04、A13、A15–A17 需要真实音频与网页共同验收；其他项至少有相应 UI 路径和定向后端证据。涉及提案校验和竞态的用例须有可重复故障注入，不能只靠人工碰撞一次。

### 开放表达与主动召回

对当前改动涉及的语义类别构造有界的独立验收样本，与用于实现调试的表达分开。预先标注必须使用、允许使用、禁止使用的证据及其对象版本，而不是标准答案的逐字文案。冻结后不得将独立样本原句写入路由或提示词；若针对失败样本修改了方案，保留该回归，并补充新的未见表达验证泛化。无需运行全仓测试或扫描全部线上历史。

| ID | 验证方式 | 通过条件 |
| --- | --- | --- |
| RCL01 | 同一需求改用同义词、不同语序、口语、省略及没有“记忆/以前/任务”等词的表达 | 在前提相同且信息充分时召回等价的必要证据；不要求任何固定触发词 |
| RCL02 | 保持当前句子相同，改变前文主体、项目、指代对象或任务目标 | 随真实上下文改变读取内容；上下文仍不充分时询问，不能固定映射到某种记忆 |
| RCL03 | 新会话提出普通建议或工作需求，没有明确要求查看历史，但个人约束会影响答案 | 适用约束主动进入上下文并被正确使用；同一偏好在无关场景不被强行插入 |
| RCL04 | 多轮中途纠正、切话题、换素材；让旧语义判断或检索晚到 | 新意图优先，旧结果不污染新回合；旧 ask 不将无关回答解释为确认 |
| RCL05 | 用开发样例之外的行业、项目内容和未见实体表达相同类型的信息需求 | 不依赖水果店等业务白名单；按证据和实际任务理解；不存在的事实不编造 |
| RCL06 | 改变否定、转述、引用和限制范围的表达；保留包含相关关键词但实际不要求查历史的负例 | 正确执行真正的历史使用限制；转述或关键词出现不自动授权或禁用记忆；明确隐私开关始终生效 |
| RCL07 | 语义判断超时、低把握或返回不完整；分别提供明确对象、普通不确定需求和可能禁止历史的需求 | 明确对象可按权限读取，普通不确定需求只作有界补查，可能禁止的历史不被外发；不以结果为空假报资料不存在 |
| RCL08 | 增加同类干扰资料、过期事实和多个相关来源，再收紧上下文预算 | 核对候选、最终注入和回答三段；关键证据不能仅因截断或去重丢失；无法满足预算时记录限制，不伪报质量通过 |

每类分别报告必要证据覆盖、无关证据混入和回答应用情况，同时记录输入 token、检索/模型调用次数及内部耗时，不能只给一个总体平均分。权限、删除、版本和明确限制属于零失败的硬门槛；语义质量的有限样本门槛、样本数量与重跑次数在 M0 实施前冻结，禁止验收后放宽。替身可验证接口、边界和竞态，但不能证明语义泛化；RCL01–RCL06 必须有真实模型证据，其中包含真实音频转录的自然表达。

### 语义模型与读取契约（保留 JEV 用例编号）

按用户后续澄清，当前 Qwen 使用阿里百炼官方通道，JEV 的目标提供方是 UEE；当前实际读取判断仍为 `qwen3.8-flash`。保留原用例编号便于追溯，读取、权限、质量和性能门槛不放宽。历史 UEE 探测和开发回归不能替代当前通道的独立端到端验收。

| ID | 验证方式 | 通过条件 |
| --- | --- | --- |
| JEV01 | 对当前官方 Qwen 通道核对请求/返回模型、紧凑传输与归一化读取契约；注入缺键、错类型、NaN、非法选项、截断、原话中不存在的限制证据 | 校验与声明契约一致；异常有明确退化状态；目录返回不能代替真实调用证据；JEV 若启用需另验原生协议与 UEE 路由，不能借用 Qwen 成绩 |
| JEV02 | 在冻结的独立中文样本上对照基线与当前 Qwen 读取路由，覆盖 RCL01–RCL06；分别计候选召回、最终注入和回答使用 | 对隐含个人/项目约束、历史事件、知识和素材分别报告必要证据覆盖与误召回；给出样本数和失败项；不能因模型更“自信”就判通过，不能用开发表达代替独立样本 |
| JEV03 | 对高/低置信度 skip、retrieve、unknown、超时和无效响应，分别提供已允许读取与可能禁止历史的输入 | 低置信度 skip 不被误作确定无需求；允许范围内有界补查或精读；限制不确定时不外发可能被禁止的历史；任务状态不靠旧记忆或猜测补齐 |
| JEV04 | 候选同时含相似但无关资料、不同条件、过期信息、矛盾证据和模型指令；对照 Qwen 重排、JEV 筛选及必要组合 | 必要证据与冲突保留，引用不可伪造，选择只影响当前上下文；记录额外调用和实际质量收益，无证据的双重重排不开启；来源数据不因筛选被删除 |
| JEV05 | 慢连接、慢响应、排队、重试、取消和已过期回合回包；同时测语义路由与检索并行及补读 | 端到端总截止真实生效，不把单阶段 HTTP timeout 当总时间；普通轮次至多一次、复杂轮次至多两次语义判断实际请求，重试计入；记录全部输入量与费用，超时不伪报查无资料 |
| JEV06 | 普通会话、个人助理和真实语音面对同类自然需求；核心事实已刷新和深层记忆需查两种情况 | 共享读取与版本规则，符合各自权限；有实际通道 called/skip/cache/降级 trace；语音不每句转交，首次回答的时序仍满足 A13；文本注入不能算真实语音通过 |
| JEV07 | 切换固定模型、问题 schema、阈值或权限/内容版本后复用缓存，再回退旧配置 | 缓存身份随对应版本变化，旧判断不混入；可回退到已验收契约且保留用户新记忆；本地默认开关不冒充线上生效状态 |
| JEV08 | 省略前文、提供真实前文、改变用户最新目标；把受限资料放在候选而非允许输入中 | 证明主动召回收益来自恰当背景和读取能力；旧 ask 不锁定新意图；未知可读范围不先发历史给模型判断；图片描述走已验证链路，不默认开启实验性视觉或长上下文 |

阈值和上线门槛按每个判断的实际后果冻结。读取选择、事实支持和用户授权分别验证，不能用一个 0.65 或 0.95 覆盖全部维度。只运行涉及上述变更的定向测试；这些用例不要求扫描全部线上聊天或运行全仓测试。

## 三 历史 知识和素材

| ID | 操作与前置条件 | 通过条件 |
| --- | --- | --- |
| K01 | 项目资料含“周六营业到 22 点；雨天不做外摆；外卖满 30 元免配送费”，只提取其中一条为 memory 后问其余两条 | 剩余细节仍可从原文或 Wiki 命中；同来源去重不误删事实；保留条件、单位和引用 |
| K02 | 上传文件，分别在解析中、来源可读但索引未就绪、全部就绪时询问内容 | 状态真实；未解析不编造，已可读部分能按 ID 查；索引未完成不误说文件不存在 |
| K03 | v1 资料含“周六 21 点关门”，v2 改成 22 点；让 v1 编译/索引任务晚于 v2 返回 | 新回复只按当前有效版本回答；旧 Wiki、chunk 和缓存被挡住，旧任务不能重新发布旧结论 |
| K04 | 一个来源的一段只支持局部结论，故障注入 Wiki 生成器扩大成全局规则；随后撤销来源权限 | 不支持的结论不发布，允许引用原文降级；撤权后 Wiki 和向量命中不能绕过 SQL 读取 |
| K05 | “上次海报是怎么做成的”，历史先有失败步骤，后有修正和成功记录 | 区分计划、失败和成功，能回到实际事件；回答不重新执行付费任务，不把工具加载当成功 |
| K06 | 上传两张同名图片，等待 AI 描述后手工改一张说明，再让延迟 AI 结果返回；重命名文件 | 描述真实可用、手工修改不被覆盖；ID 关联保持，无法判断哪张时正常询问，不凭文件名乱选 |
| K07 | 语音问长文件名、文档末尾的关键条件及相关图片；再在另一入口调用相同素材 | 文件名、条件、数字完整；模型可精确读取，口播不念内部 ID；附件权限和用途确认保留，上传不代表同意重绘 |
| K08 | 分页列出多于 Top K 数量的全部资料；再以不匹配关键词检索；对记忆和其来源分别执行遗忘操作 | 清单完整且可翻页；未命中不等于不存在；旧来源重放不能复活，日后主动重述与拒绝候选的区别符合既有测试；原聊天是否保留明确可见 |

K02、K06、K07 至少包含一次内置浏览器实际上传。可使用授权的公开图片或本地测试图并记录来源。K07 需要真实语音。涉及视频业务时优先验证资料与决策链；模型替身的渲染结果不能标成真实视频生成验收，付费完整生成若未执行应明确列出范围。

## 四 经验技能

| ID | 场景 | 通过条件 |
| --- | --- | --- |
| E01 | 一次有最终结果和验证记录的操作产生可复用经验，另一次只是加载技能或尝试失败 | 前者形成有来源的候选，后者不冒充成功方案；不自动写入公共技能 |
| E02 | 候选经定向验证后启用为私有版本，再修改步骤 | 新版本有验证记录、版本号和适用条件；已运行任务保留其绑定版本，新加载使用新版本，不能只比较技能名判断变化 |
| E03 | 候选依赖的材料删除或被发现不可靠，随后撤回技能版本 | 按依赖关系将相关候选或技能标记待重验/停用；不再注入旧方案；保留审计而不泄漏已删除原文 |
| E04 | 无可用 sandbox 或会员到期，随后恢复权限；另一个用户尝试加载该技能 | 候选可保存，执行说明真实权限限制；权限恢复按既有机制验证；他人无法读取私有技能；私有启用不改变市场发布快照 |

E01–E04 是 R2 门槛，不能在只完成 R1 时勾选。无需为本轮计划重新修改会员系统，发生回归才定位修复相关路径。

## 五 故障注入与必要回归

| ID | 可重复故障 | 通过条件 |
| --- | --- | --- |
| F01 | 权威提交后、设置应用前或 Outbox 投递前进程退出；分别在提交前退出 | 可恢复，提交状态准确，未提交不说已保存；重试恰好一个业务效果 |
| F02 | 更新期间重复/乱序 job，失去租约后旧 worker 回写 | 当前版本与较新的用户决定不被覆盖；来源和 CAS 校验仍生效 |
| F03 | 提案形成后用户删除来源、拒绝候选或撤权，再释放模型响应 | 提交复核阻止写入；旧内容不能通过提案、合并或整理复活 |
| F04 | Qdrant 不可用、索引延迟、旧 generation 回包、Wiki 编译失败 | 有限且可见地降级；可读原文仍能精确取，不能返回旧/越权资料；删除清理不因发布开关关闭而丢失 |
| F05 | 变更事件丢失、重复、乱序，语音应用确认延迟或丢失、断线重连 | 下一回复边界重查版本，版本不倒退；应用状态真实，重连恢复最新快照；不产生额外交办 |
| F06 | 删除期间后台回填或重建，源文档在索引 IO 期间变化 | SQL 再校验和墓碑挡住陈旧响应，派生清理最终完成，有对应日志 |
| F07 | 正常语音讨论/否定/询问状态，明确交办，视频完成时原会话忙，长通话多轮 | 保留 main 的漏转交保护、同一发言去重、完成结果持久回送和称呼行为；记忆刷新不制造重复任务或重复播报 |

单元和集成测试可以使用替身精确制造故障，实际浏览器链路仍须独立通过。F07 只选与改动交集有关的既有用例，不扩展到整个视频/语音测试目录。

## 六 迁移与回滚验收

| ID | 演练步骤 | 必须保留的证据和门槛 |
| --- | --- | --- |
| MIG01 | 从起点 schema 和最小代表性数据升级到目标 head，核对旧数据及新写入 | 迁移 ID、命令、前后行数/引用/抽样摘要、旧客户端兼容结果；无丢失、无越权扩大、无把未知来源升级成人工确认 |
| MIG02 | 回填中断并恢复、重复执行；期间用户更新同一对象；含旧 job、候选和墓碑 | 游标、幂等结果、当前版本；不重复创建、不覆盖新修改、不恢复拒绝或删除数据 |
| MIG03 | 升级后用新版本产生修改，关闭新功能并回退到兼容应用，随后再次升级 | 新修改仍在且可正确解释；必要时只回退功能不降库；outbox 和删除继续处理；R1 核心冒烟通过 |
| MIG04 | 在隔离库验证 schema downgrade/upgrade 可行范围，检查不兼容字段 | 明确哪些降级会丢信息及禁止条件；不可逆时有保留 schema 的回滚路径；禁止把隔离库快照覆盖到正在写入的生产库 |

至少在 PostgreSQL 完成 MIG01–MIG04，并对支持的 SQLite 路径作相应定向测试。每个演练记录连接目标的脱敏身份，确认未操作生产数据库。没有真实演练记录不能标记“迁移无风险”。

## 七 内部耗时验收

先定义时间口径，再作比较。`T_wall` 是请求进入服务至可提交模型请求或工具结果的墙钟时间；语音额外记录说话结束至首音频时间。外部模型/embedding/rerank 调用标为独立 span；只有关键路径上且没有与本地工作重叠的外部等待可从总时长剥离。数据库和队列等待单列，属于应用需要解释和优化的部分，不借“网络”名义全部排除。

初始门槛是待验证的验收目标，不是现状测量结果。M0 在同配置、同数据规模下记录基线，冻结下表数值和上下文 token 上限；若环境证明不适用，必须在实施前写明理由和替代数值，不能在测试失败后临时放宽标准。

| ID | 采样与口径 | 初始通过门槛 |
| --- | --- | --- |
| PERF01 | 简单问候、偏好读取和普通事实新会话，真实 SQL，外部模型替身；相同场景基线/改造后各 30 次暖请求，冷启动另列至少 5 次 | 新增常驻记忆阶段暖请求 p95 ≤ 150 ms；简单路径剥离外部等待后的内部 p95 ≤ 500 ms；不新增每次必跑的外部模型调用 |
| PERF02 | 知识召回及通话内更新，各选固定相关场景，基线/改造后各 30 次暖请求；真实提供商少量验证另列 | 内部 p95 相对同场景基线增加不超过 10% 且不超过 100 ms；记录每个串行步骤理由；没有未解释的单段 ≥ 100 ms 空档 |
| PERF03 | 同一正常环境下提交记忆，观察网页新回复和空闲语音提供商应用确认；丢事件/超时另测 F05 | 新请求不读旧版本；空闲通话应用确认正常场景 5 次均 ≤ 2 秒，超限判失败并定位；提供商不可用时不得假报同步完成 |
| PERF04 | 各入口记录实际模型输入 token、常驻条目、召回条目、原文读取次数和后台排队，同时验证未见表达的必要证据覆盖 | 初始新增记忆上下文预算为常规 2048 tokens、必要精读后 4096 tokens，须按实施计划 4.7 在 M0 联合验证后冻结；候选数量、补读次数及 1 秒检索截止一并验证；不能以漏召回取得预算通过，关键名字、数字与权限字段完整，失败/降级可区分 |

30 次采样仅针对列明的性能路径，不是全量测试。样本不足时报告单次值或范围，不报告伪造的 p95。真实模型请求单独报告端到端结果；供应商响应慢与内部等待不能互相掩盖。没有分段 trace 就不能回答“除去模型和网络到底花在哪里”。

## 八 定向测试清单与命令约束

下表是可选的现有测试入口，不是要求一次全部执行。每次提交先按实际改动勾选文件或 node ID，再运行；新增行为补必要的针对性测试。新增测试名在创建之前只能标为计划，不能冒充已有测试。

| 改动区域 | 现有入口举例 | 对应验收 |
| --- | --- | --- |
| 设置、称呼、普通会话读取 | `backend/tests/unit/test_assistant_profile.py`、`test_memory_personalization.py`、`test_main_context_parity.py`、`test_chat_identity.py` | A01–A06 |
| 模型提案、提取、并发与回执 | `backend/tests/unit/test_assistant_memory_source_policy.py`、`test_assistant_memory_extraction.py`、`test_memory_reconciliation.py`、`test_memory_pipeline.py` | A07–A12、F01–F03 |
| 读取权限、语义路由、实时校验、历史 | `backend/tests/unit/test_memory_personal_scope.py`、`test_memory_provider_rechecks.py`、`test_memory_router_context.py`、`test_memory_consumer_quality.py`、`test_memory_retrieval_runtime.py` | A13–A14、RCL01–RCL08、JEV01–JEV08 的对应程序边界、K01、K05、F04–F06；混合类型等新增行为的测试待实现，真实语义效果另验 |
| 文档、Wiki、删除 | `backend/tests/unit/test_memory_documents.py`、`test_memory_wiki.py`、`test_memory_source_clearing.py`、`test_memory_forget_restate.py` | K02–K04、K08 |
| 通话和已有业务保护 | `backend/tests/unit/test_voice_updates.py`、`test_voice_cards.py`、`test_voice_revisions.py`、`test_voice_provider.py`、`test_voice_handoff_guard.py`、`test_video_completion_delivery.py`；集成入口 `backend/tests/integration/test_voice_ws.py` | A13、A15–A17、F05、F07 |
| 网页回执和知识页面 | `frontend-v2/src/features/chat/lib/memory-receipt.test.ts`、`frontend-v2/src/features/chat/components/AssistantMemoryReceipts.test.tsx`、`frontend-v2/src/features/memory/knowledge/MemoryList.test.tsx`、`frontend-v2/src/features/memory/wiki/WikiReading.test.tsx` | 回执与对应知识 UI |
| 网页实时状态 | `frontend-v2/src/shared/appearance/useAssistantProfileLive.test.ts`、`frontend-v2/src/features/voice/lib/session.test.ts`、`frontend-v2/src/features/voice/lib/reducer.test.ts` | A01、A13、A16 |
| Flutter 相关页面与语音 | `mobile/test/features/memory/knowledge_memory_test.dart`、`knowledge_scope_test.dart`、`knowledge_files_test.dart`、`mobile/test/features/voice/voice_call_controller_test.dart`、`mobile/test/features/settings/voice_section_test.dart` | 仅受影响移动端能力 |

同一单元格中未重复写出的目录沿用该单元格前一文件目录。素材描述、经验技能、数据库迁移的测试入口在对应实现阶段按实际修改列明；表中没有列出的目录也不能以此为由整目录运行。

以下仅展示命令格式，**本计划阶段不执行**；实际参数应替换成当前改动清单内的文件或节点：

```bash
# 在 backend 目录：示例为只验证设置服务
uv run --extra test pytest -q tests/unit/test_assistant_profile.py

# 在 frontend-v2 目录：示例为只验证回执解析
npm run test -- src/features/chat/lib/memory-receipt.test.ts

# 在 mobile 目录：示例为只验证知识范围
flutter test test/features/memory/knowledge_scope_test.dart
```

禁止默认执行裸 `pytest`、整个 `tests` 目录、裸 `npm test`、会串起全量测试的 `npm run check`、裸 `flutter test`，也不使用 `test_memory_*` 等宽泛匹配把整个子系统扫一遍。构建、类型检查或定向 lint 只在改动需要时执行并单独记录；不能借必要回归自动扩大成全量。

## 九 浏览器验收顺序

先完成 A01–A03 的“设置或告知 → 回执 → 新会话主动应用”，再保持通话连接测试 A04、A13 和网页更新。随后实际上传资料与图片，验证 K02、K06、K07，再进入旧 ask 与新水果店需求的 A15–A17。各步骤使用清楚的自然请求，不在用户输入里植入工具名或内部字段。

每个闭环至少完整执行一次，涉及真实模型表达和跨入口同步的 A02、A13、A17 各独立执行三次并记录全部结果。正确的数据契约和语言范围每次都须满足；建议文案和措辞允许不同。存在一次失败时保留证据、修复并重新执行受影响用例，不能只展示成功的一次。

移动端如受影响，补设置保存、知识读取、通话内更新、后台恢复以及账号/项目切换；保留上一聊天位置和系统返回键的已有行为。未改动的页面不要求全量重新验收。内置浏览器无法覆盖的音频/移动端部分必须列为单独环境，不能从网页成功推断通过。

## 十 上线判定与验收记录

以下任何一项存在即不满足对应发布单元上线标准：越权或跨用户串数据；保存虚报；新设置被旧任务覆盖；删除内容复活；三入口出现已知必现差异；重复任务/支付；迁移损坏或回滚丢新数据；真实语音核心路径未验证；内部耗时未解释或超门槛。截图好看、测试数量多、接口返回 200 都不能代替这些检查。

R1 要求 A01–A17、RCL01–RCL08、JEV01–JEV08、K01–K08、F01–F07、MIG01–MIG04、PERF01–PERF04 的适用项全部完成；R2 再要求 E01–E04 及其影响的前述用例复测。标记“不适用”必须写出与实现差异相关的理由，不能用来跳过核心契约。只有 R1 通过时结论应为“R1 可灰度，经验技能未完成”，不能写“全部完成”。

2026-10-22 执行记录如下；同一测试文件的重复运行不相加为总通过数量：

| 记录项 | 实际值 |
| --- | --- |
| 实现提交和迁移版本 | 分支工作树，尚未提交；基线 a95a302；schema ph84a5b6c7d8，前序 ph73f4a5b6c7 |
| 验收范围及发布单元 | R1/R2 部分实现与本地验证；未发布 |
| 已执行定向命令与结果 | 见下列逐项记录；无全量 pytest、全量前端测试或全量 Flutter 测试 |
| 真实浏览器及语音证据 | 内置浏览器 localhost:3002；真实音频经 AudioContext 虚拟麦克风进入应用，使用真实语音模型 ASR；未用文本冒充转写 |
| 移动端受影响范围与证据 | 账号归属与版本相关 5 个明确文件 41 passed；实际模拟器/真机页面未验收，不能用 widget 测试代替 |
| 迁移和回滚记录 | 隔离 PostgreSQL 升降级、摘要比对、非空拒绝降级通过；完整兼容后端关闭功能→HTTP 读写/CAS/幂等→重启再开启功能，旧/新数据摘要一致；未运行旧 main 二进制 |
| 内部耗时与外部耗时 | 独立真实 SQL + 即时模型替身包暖 p95 43.04 ms；旧 stable-background 单独读取 p95 8.75 ms，两者范围不同，不能作为旧/新整个链路对照。UEE 实际请求见下 |
| 通过 失败 阻塞 未执行项 | 60 条真实模型矩阵已执行；读取边界符合预期，但同时发现后台提取污染，修复后完成 14 条真实正反例和浏览器定向复测；经验修改/重验/启用/停用已走 UI；历史超时证据保留 |
| 剩余限制与缺陷 | 网关延迟影响主动召回；未缓存的范围判断仍需要外部调用；全部跨入口竞态、同范围性能对照和移动端实际页面未完成；不能把 60 条读取成绩解释为整个系统通过 |
| 是否达到上线标准及依据 | **未达到**：记忆核心路径和空闲通话更新有新证据，原内部速度门槛与 R1/R2 全部适用项仍未通过 |

完成实现后维护受影响的仓库认知，区分本次变更和已有治理欠账。验收记录只报告真实完成的工作，任何计划、替身结果或历史发布记录都不能填成当次真实环境通过。

### 本次证据索引

所有原始证据均在忽略目录 `.local-dev/memory-upgrade-20261021/`，仅包含隔离测试账号。前端 3002、后端 8082、数据库 `openbox_memory_upgrade_20261021`。迁移专用库为 `openbox_memory_migration_rehearsal_20261022_v2`，未修改生产数据。测试音频注入结束后已刷新页面，恢复浏览器正常麦克风实现。

| 场景 | 实际结果和限制 | 证据 |
| --- | --- | --- |
| 新会话称呼 | 页面与回复使用“林姐”；设置值为 brief。不能据此推断语种及所有偏好均已通过 | 新会话截图与本地 profile |
| 门店 TXT 上传阅读 | 浏览器上传 332 B 测试文件，显示“可以阅读”，全文、来源及长文件名完整 | knowledge-file-ready.png |
| 原生语音记忆 | “周末一般坐地铁，很少开车”由真实 ASR 捕获，后台保存 ACTIVE 记忆；对应 call 的 VoiceTurn=0、个人助理新增消息=0 | native-snapshot.json、qa-receipts.json、voice-native-after-fix.png |
| 语音后台耗时 | 原生提取首试成功，23.947 秒；提取 10.914 秒、核验 5.227 秒、去重 6.249 秒。余量包含 worker 调度，不能全部称为 CPU 时间 | qa-receipts.json |
| 经验候选与个人技能 | 日常文字反馈形成 TXT 上传流程候选；浏览器填入真实已执行的验证记录，再启用到第 3 版。没有执行额外云桌面步骤，也没有据此证明 E03/E04 | experience-enabled.png、qa-receipts.json |
| UEE 实际模型 | 配置与返回模型均 qwen3.8-flash；原 JEV 通道探测失败不是当前方案接通证明 | uee-router-observed.json |
| 新会话主动召回失败 | 两种普通周末建议说法分别在 2.5 秒与诊断 4 秒路由预算超时；最终注入无兴趣/出行记忆。保留失败，未换提示词筛选成功 | weekend-read-debug.json、weekend-diagnostic-debug.json、weekend-uee-timeout.png、weekend-uee-diagnostic.png |
| 网络与网关耗时 | 第二组 Qwen 第一次请求 TLS 4.904 秒、上游响应头 2.237 秒；连接复用后分别等待上游 3.169/2.105 秒。单次现有系统代理对照 TLS 1.166 秒、上游 2.498 秒，仅诊断，未据此改全局代理 | uee-router-observed.log、uee-system-proxy-comparison.log |
| 主模型网关波动 | Gemini 两次 60 秒超时，响应头等待分别 52.331/56.270 秒。此阶段含网关排队、上游网络和模型工作，不能当纯模型计算 | backend-uee6.log |
| 旧预算真实模型性能 | packet-performance.json 的成功数据使用过直接百炼分类器，用户要求 UEE 后不作为当前接入验收成绩；原失败与配置差异保留 | packet-performance*.json |

定向检查记录：

- `test_memory_configured_read.py` 最新 15 passed，覆盖网关密钥隔离、身份缓存、严格响应及失败计时；与 `test_memory_read_protocol.py` 的上一批合计 19 passed。见 gateway-observation-tests.log、gateway-cache-tests.log。
- `test_voice_native_remember.py` 原 35 passed，既有相关回归 14 passed；新增第二次读取回执缺失/变质的 2 个用例通过。语音删除 ACK 相关 43 passed。上述为定向文件与节点，不代表全部语音场景。
- Wiki 完成态校验与提取阶段超时定位相关 14 passed，见 provider-completion-tests.log。
- SQLite 迁移 3 passed；PostgreSQL 租约/重放/来源遗忘竞态 8 passed，见 sqlite-migration-tests.log、postgres-recovery-tests.log。
- ExperienceList 前端定向 9 passed，前端 `tsc -b --pretty false` 通过；Flutter 上述两个文件 12 passed。
- migration-rehearsal.json、feature-rollback.json 分别记录数据库演练与功能回滚。旧 parser 可读新设置 JSON；这不等于旧完整后端的部署回滚已验证。

用户已确认使用当前 UEE `qwen3.8-flash`，不再等待模型别名或分组答复。下一步针对原性能差距、独立表达质量及跨入口竞态继续验收；不得以扩大超时、成功单例或迁移通过替代这些门槛。

### UEE 当前通道确认后的早期修复与复测（历史 v4 记录）

连接池按事件循环和代理策略共享，不再按 timeout 分池；每个请求单独携带鉴权与截止时间。语义判断采用 `configured-read-grounded-v4`，缓存身份包含传输/提示版本。读取限制必须绑定本轮原话，行动限制不自动禁止读取，也不授予执行权限；`creator_context` 的三种旧读取以及旧结果重放不能绕过同一权限/预算。读取范围判定 6 秒、其后检索 1 秒分别取消超时分支，这仍不等于原 1 秒端到端目标达标。

| 场景 | 实际结果和限制 | 证据 |
| --- | --- | --- |
| 24 条语义回归 | 当前 v4 的限制判断 24/24，按实际生产规则的必要读取判断 24/24；中位 1609.5 ms，p95 2531 ms，最大 3649 ms。是开发回归集，不是独立 60 条端到端质量成绩；原始输出及先前失败版本全部保留 | uee-semantic-matrix.json、uee-semantic-matrix-assessed.json、uee-semantic-matrix-v2.json、uee-semantic-matrix-v3.json |
| 新会话主动召回 | “这个星期天我想放松放松，你觉得我适合怎么过？”自然回复结合爬山与安静偏好，无工具调用；记忆包 1329 tokens、核心 455，判断 3189 ms、召回 481 ms、完整包 3848 ms | uee-new-chat-recall.png、uee-new-chat-debug.json |
| 用户限制历史 | “今天只看我这段话，不参考之前的记录……”判 current_only，旧事实/core 均为空，召回阶段无历史读取，无绕行工具；设置身份仍按产品约定保留 | uee-restricted-chat.png、uee-restricted-debug.json |
| 旧工具越过限制的修复 | 修复前路由超时后模型曾经通过 creator_context 取得 6 条记忆；现统一到共享读取，14 个相关参数化边界用例覆盖受限读取和旧结果重放 | cold-router-timeout-evidence.json、creator-bypass-before-fix.png、test_memory_tool_projection.py |
| 原生语音主动召回 | WAV 经虚拟麦克风与真实 ASR 输入“我周末想出去走走，你帮我想个适合我的安排吧”；判断 1173.5 ms，记忆到提供商应用确认共 2088 ms，实际进入播报；对应新增个人助理消息和交办均 0。未取得完整口播逐字转录，不能只凭通话摘要证明每句话；该单例超过原 2 秒门槛 | voice-uee-recall.wav、uee-native-call.png、backend-uee9.log、qa-receipts.json |
| 个人助理跨入口读文件 | “店里的拍摄资料，镜头长度怎么定来着？”准确回答每镜头不超过 5 秒、总长 20 秒；随后普通追问返回“青禾鲜果店门店视频制作与到货宣传注意事项（最终确认版本）.txt”完整文件名 | uee-assistant-document.png、uee-assistant-filename.png、uee-assistant-debug.json |
| 召回质量局限 | 资料提问的候选仍混有周末偏好；文件名追问曾多查任务状态及 assets.list 后再 memory.search。回答正确不等于候选精度、读取次数及全部等待都已最优 | uee-assistant-debug.json、backend-uee10.log |

本轮定向测试批次有重叠，不能累加为独立测试总数：共享连接/适配器/Wiki/管线及相关节点 50 passed；扩展相关文件 102 passed、43 skipped、1 failed（旧低置信度 skip 断言与既定有界补读语义不一致，修正后相关节点通过）；投影/读取协议/适配器等 74 passed；v3 适配器及相关写读边界 67 passed；最新 v4 适配器、语音上下文和提取节点 41 passed。上述 skips 也不算通过。

另修复预取统计：提前启动的记忆任务使用独立计时，正式回复 await 时仅合并仍重叠的外部 IO；已完成预取不重复扣除，SQL/解析与取消仍正确计时。`test_agent_latency.py`、`test_recall_ahead.py`、`test_memory_read_protocol.py` 定向合计 **22 passed**，证据 `prefetch-latency-tests.log`。修复前 agent 日志把后台 UEE 等待计入内部耗时，不能直接拿旧 `internal_ms` 作为纯内部性能成绩；模型网关分段 trace 和记忆包的阶段时间仍保留。

重启当前本地后端后，浏览器自然追问“拍到水果的时候，文案里有什么不能说的吗？”，准确回答资料中禁止夸张词、未提供价格不可编造两点。此轮总耗时 **12191.0 ms**，已观测外部等待 **11036.1 ms**，剩余内部墙钟时间 **1154.9 ms**；其中上下文阶段内部 **417.3 ms**、请求准备 **270.8 ms**、模型结果消费 **124.6 ms**、结束步骤/运行 **215.7 ms**，其他启动/历史等约 **126.6 ms**。预取等待本身 4408.1 ms，其中提供商等待 4066.9 ms、运行网络 25.1 ms、剩余内部 316.0 ms。外部时段按重叠区间合并，不把整段预取都当网络；此值不是 CPU 时间，也不包含浏览器播放/入口队列和未观测 IO，单例不能充当 p95，亦未达到 PERF01 500 ms 门槛。证据：`backend-uee11.log`、`uee-assistant-latency-debug.json`、`uee-assistant-latency-retest.png`。

这次回复的记忆包是 **partial / rerank_deadline**：重排没有在当前检索预算内完成，仍从有权读取的资料取得正确事实；包 1593 tokens，判断 3489 ms、召回 915 ms、收尾 105 ms。浏览器答案正确只证明本例有界降级可用，不能把它改标为全链路完整召回通过。

### 2026-10-22 后续实现与真实验收（读取 v7 / 提取 v11）

当前读取模型仍为 UEE `openai/qwen3.8-flash`，没有换分组或隐式回退。`configured-read-grounded-v7` 在紧凑返回中增加语义指代判断：自足的新问题不带入上个主题，省略了对象的追问保留必要前文。当前时间、执行进度和待办状态才需要实时任务读取，文件名称/正文使用资料读取。此判断来自语义模型，不用固定用户表达做路由。

| 本批实际验收 | 结果与解释 | 原始证据 |
| --- | --- | --- |
| 冻结的 60 条独立表达 | 六组各 10 条，真实聊天 API、真实主模型和 UEE 判断器，每例独立会话；涉及指代的 10 例先发自然前文。manifest SHA256 为 `7ba7eb0712a591c0d7506ed05dbb6db6e727b988d0099085f7dab7091407467c`；两个并发请求，无终态错误 | holdout-60-manifest.json、holdout-60-results.json |
| 读取与必要证据 | 读范围 60/60 符合预期；11/11 受限请求实际模型上下文没有历史事实；20/20 指定必要证据进入初始上下文；46 complete、11 restricted、3 partial（版本变化重建/重排截止），最大记忆块 2006 tokens | holdout-60-assessed.json |
| 矩阵发现的写入缺陷 | 测试期间记忆数由 11 增至 21；其中改写材料营业时间、一次性措辞要求被误保存。保留原始数据，不把动态变化的测试库描述成固定无污染样本；该矩阵不算完整管线通过 | holdout-60-results.json 的 fixture_before/after |
| 提取修复 | `source-only-v11` 区分稳定事实、长期要求、持续项目决定与单次指令/引述/假设；独立核验同时判定事实依据和原话行为，拒绝内容不再留下待提升候选。真实 UEE 8 个反例、6 个正例均符合预期 | write-admission-real-v11.json |
| 浏览器负例/正例 | 改写“每天十一点开门，晚上八点关门”正常答复，提取作业成功且结果为空；自然告知水彩花草爱好，生成 ACTIVE 记忆，完整来源和核验保留 | browser-rewrite-admission.json、browser-hobby-admission.json |
| 普通会话→个人助理 | 未在提问中写出水彩，只问“在家空一个小时做什么”，助理主动建议画一小幅水彩花草 | assistant-watercolor-recall.png |
| 普通会话→真实语音 | WAV 通过浏览器 MediaStream 进入真实 ASR，识别“今天不想出门，在家有一个小时空闲……”；语音明确用到水彩爱好。逐字转录来自真实 provider 返回，经既有 live_context 保存 | voice-watercolor-live-transcript.json、voice-memory-idle-confirmed.png |
| 语音写入→新普通会话 | 真实 ASR 告知喜欢古筝曲、画画时常听，后台形成 ACTIVE 记忆；新会话只问画画时放什么声音，回答主动结合古筝与水彩 | voice-latest-extraction.json、new-chat-voice-music-recall.png |
| 语音中的临时限制 | “这次别参考我以前说过的爱好……”判 restricted，回答回到通用休息建议；对应提取结果为空。本次三轮语音没有个人助理交办或新增消息 | voice-cross-entry-live-transcript.json、voice-complete-receipts.log |
| 空闲通话更新 | SQL 变更时钟触发本地快照刷新；不调用语义模型，不自动生成回答；提供商 ACK 后推进应用版本。五次正常 HTTP 更新到 ACK 分别 309.2、222.5、313.1、213.3、231.1 ms，全部 ≤2 秒；随后恢复初始设置。这里测的是空闲更新，不能替代单次发言的准备时间 | voice-idle-update-performance.json、backend-uee16.log |
| 回复边界竞态 | 延后播报的回答发送前再次查 SQL 版本；读取中/ACK 中的新发言使旧刷新失效；ACK 失败不推进版本且可重试；实际通话通知与详略开关和 prompt 一起应用 | test_voice_memory_refresh.py、voice-idle-flags-tests.log |
| 经验完整 UI 生命周期 | 改步骤→candidate v4（旧技能不可读）→实际验证 v5→enabled v6（技能可读）→retired v7（技能不可读）；直接技能读取与列表均核对，内容与验证记录保留 | experience-candidate-v4.json、experience-enabled-v6.json、experience-retired-v7.json、experience-revalidated.png、experience-retired.png |
| 账号切换与迟到响应 | 网页和 Flutter 将版本、GET、保存和事件绑定账号；新账号较低版本可正常加载，旧账号迟到事件不能覆盖。网页相关两文件 14 passed、tsc 通过；Flutter 五个指定文件 41 passed | frontend-profile-and-experience-tests.log、mobile-profile-scope-tests.log、tsc-final.log |
| 最终代码的真实通话设置 | 内置浏览器在通话中关闭“主动告诉我任务进展”，界面显示关闭，日志确认实际桥接开关 reports=False；ACK 阶段 220 ms，随后恢复原设置。该行验证设置应用，不冒充已触发完整任务完成播报场景 | voice-reports-setting-applied.png、backend-uee17.log |
| 迁移健康与完整兼容进程回滚 | 健康检查覆盖三张新表及必要列；兼容完整进程关闭功能时可读旧记忆、更新设置、幂等重放并拒绝旧 revision，重启再开启后数据哈希一致，保留 ph84a5b6c7d8 | readiness-retest.log、rollback-http-disabled.json、rollback-http-enabled.json |

最新定向测试不叠加重复批次计数：读取/语音上下文/首次空闲刷新批次 22 passed；随后新增最终回复边界、ACK 重试与本地通知开关的同一文件 7 passed。提取、个人化、敏感内容及协调批次的两处测试夹具问题已修正；最终单独运行入库语义文件和敏感掩码节点，11 passed（admission-final-tests.log）。此前 46 passed/1 failed 的 readiness 批次是夹具漏建现有 FileAsset 表，补齐后迁移/readiness 的 5 个节点通过。失败日志保留，不算成通过数，也不运行全仓测试。

目前仍不能签署 R1/R2 全部通过：五次空闲更新达标，不代表整个语音响应达标；本次三次发言准备分别约 5625、6753、2596 ms，前两次有完整/降级包差异。普通/个人助理剥离已观测外部等待后，内部墙钟单例约 946–1534 ms，原 500 ms 门槛仍未达到。新模块的即时分类替身性能不能当整条主循环性能，所有数字保留各自口径。全部跨入口竞态、同范围旧/新性能采样、实际移动端页面还没有完整证据。

### 2026-10-22 收尾增量：读取 v8、原生移动端与配对性能

本节更新上一阶段仍在进行的项目；前文失败数据及原门槛保留。读取模型固定 UEE `openai/qwen3.8-flash`，提取器 `source-only-v11`。所有原始证据仍在同一忽略目录，没有把测试账号、密码或原始聊天提交到 Git。

| 项目 | 本次可证明的结果 | 证据文件 |
| --- | --- | --- |
| 提取修复后的完整 60 例聊天矩阵 | 读取策略 55/60；20/20 指定必要证据进入初始上下文；11 个受限案例均未注入历史。42 complete、10 restricted、3 partial、5 unavailable；最大记忆块 2017 tokens。保留 3 次传输异常，后续查询找到回复也不擦除异常 | holdout-60-v11-final-results.json、holdout-60-v11-final-assessed.json |
| 写入污染复测 | 70 个提取作业全部结束，pending=0；记忆数 23→23。原先污染数据仍保留，不擅自清库或使样本变干净 | holdout-60-v11-final-results.json、holdout-60-v11-final.log |
| v8 协议修复 | 无效结构/引文最多修复一次，共用原始 6 秒截止；HTTP/网络错误不重试。冻结 60 例的真实分类器策略 59/60，1 次超时；中位 2192 ms、p95 4571 ms。是分类器检查，不是全聊天矩阵重跑成绩 | holdout-router-v8.json、router-protocol-repair-deadline-tests.log |
| Android 实际跨端事件 | 独立 QA AVD、原生应用、真实 API/WS：设置页保存后由第二客户端更新，手机页面自动显示新值；知识库可见同账号上传文件 | mobile-native-bookmark-fixed.log、mobile-live-update.png、mobile-shared-document.png |
| Android 冷启动恢复 | 修复引导浮层导致未保存位置的问题后，重启应用恢复原会话，实际显示包含古筝/水彩的历史答复。只显示加载中的旧截图不作通过证据 | mobile-native-history-restored.log、mobile-cold-history-verified.png |
| 移动端定向静态/回归检查 | WS 生命周期 12 passed；聊天位置 4 passed；8 个改动相关文件静态分析 No issues。原生集成不使用 API/provider 替身 | mobile-ws-lifecycle-tests-v2.log、mobile-bookmark-regression-v2.log、mobile-final-analyze-v2.log |
| 最终真实语音主动召回 | 浏览器虚拟麦克风播放 WAV，真实 ASR 识别“今天不想出门，在家有一个小时空闲”；实际口播主动结合水彩花草和古筝。VoiceTurn=0、个人助理新增消息=0；准备 5503 ms，不能用空闲同步耗时代替 | voice-final-live-transcript.json、voice-final-browser.png、backend-uee25-continue.log |
| 最终空闲语音版本同步 | 5 次普通 HTTP 更新→提供商 ACK 为 362.7、380.3、271.7、220.0、230.9 ms，均 ≤2 秒；测试后恢复 brief | voice-idle-update-final.json、voice-idle-update-final.log |
| 手动创建任务→语音感知 | 内置浏览器新建花店周末视频脚本需求；真实语音问“做到哪了”，准确回答脚本已准备、等待确认；没有新建文字交办。此处是自然文字确认，不能据此算 ask 卡片点击同步通过 | voice-task-progress-live-transcript.json、voice-manual-task-browser.png |
| 长文件名和关键条件 | 普通追问返回完整“青禾鲜果店门店视频制作与到货宣传注意事项（最终确认版本）.txt”；镜头/总长及禁用词、价格条件正确。部分包仍为超时降级，保留 debug 状态 | browser-final-filename.png、browser-final-memory-v8.png、browser-runtime-fixed-debug.json |
| 半句回复 | 后端持久化和 UI 均只有“林姐，店里日常拍”、7 output tokens、finish=stop；用户自然追问后答复完整。尚无该次原始上游报文，不断言已证明具体网关/模型根因；该失败未算通过 | reply-terminal-inspection.json、browser-runtime-fixed.png |
| 三张表逆迁移保护 | 仅变更时钟非空也拒绝降级，删表前阻止，表和 revision 保留；独立 PostgreSQL scratch schema 在事务中演练，整体回滚测试事务，未操作生产 | migration-clock-guard-postgres.json、migration-clock-guard-tests.log（5 passed） |
| 真实 PostgreSQL 并发设置 | 10 轮，每轮 3 个相同旧 revision 并发更新只有 1 个 200，其余 409；随后 3 次同操作重放均返回原结果，共 60 个请求 | profile-concurrency-postgres.json |
| 兼容完整进程回滚 | 关闭新功能后真实 HTTP 读写/重放/冲突，再开启后摘要一致；保留新 schema。旧 main 二进制未被当作已验收回滚版本 | rollback-http-disabled.json、rollback-http-enabled.json、migration-rehearsal.json |

本次性能修复的定向检查：内置工具 schema 缓存与 payload 9 passed；证据刷新和读取相关 96 passed/43 skipped（跳过不是通过）；路由协议/总截止 45 passed；平台工具快照与目录单次观测等 85 passed；技能来源/目录权限相关 32 passed。各批有重叠，不相加为独立测试总量。见 schema-cache-final-tests.log、memory-refresh-transaction-tests.log、router-protocol-repair-deadline-tests.log、memory-runtime-latency-regression.log、catalogue-authority-regression.log。没有运行全仓测试。

#### 配对性能口径与结论

基线是原 `a95a302324bfea81437a1b60f2f2871bf8303753`；两个变体用同一个不可变快照的新 PostgreSQL 克隆、独立 Redis DB，逐例交替先后顺序。每变体先 5 次启动样本，另 3 组各 30 次暖样本，总计 190 个 HTTP 请求。真实 SQL/HTTP、外部模型/索引替身、无 profiler；因此证明内部耗时，不证明自然语言质量。实际浏览器 QA 同时运行，非独占机器实验。

| 路径 | main：入口→主模型请求就绪 p95 | 当前：同口径 p95 | 差值 | 当前主循环内部 p95（不同边界） |
| --- | ---: | ---: | ---: | ---: |
| 问候 | 721.23 ms | 580.05 ms | −19.57% | 379.61 ms |
| 偏好读取 | 541.03 ms | 562.58 ms | +3.98% | 353.16 ms |
| 知识召回 | 550.85 ms | 422.73 ms | −23.26% | 316.43 ms |

当前核心上下文阶段 p95 为 99.0/102.9/103.9 ms，符合 150 ms 子门槛；知识路径相对 main 符合 PERF02 的比较部分。问候与偏好入口仍超过 500 ms，且未缓存表达仍有外部语义判定，**PERF01 整体不通过**。不得换成主循环边界后宣称达标，也不把三个阶段各自的 p95 相加。证据：perf-paired-final-comparison.json、perf-paired-final-samples.json、perf-paired-baseline.log、perf-paired-new.log。早期串行比较和优化中间批次保留于 perf-main-comparison.json、perf-final-comparison.json。

真实故障网络中，原 115.76 秒回复包含模型等待约 22.38 秒、目录请求约 60.14 秒、工具前/后快照约 30.07 秒，其余约 3.14 秒。优化后另一回复 72.93 秒包含模型等待约 50.79 秒、目录约 20.04 秒、快照 0，其余约 2.10 秒。两次模型等待不同，不能当受控性能对照；它们只定位重复目录和无关快照，后者已由定向测试验证消除。其余不是纯 CPU 时间，数据库/队列/序列化仍属于应用耗时。

#### 发布判定

仍为 **不满足 R1/R2 全量验收，不能签署正式上线**。已完成实现和证据的项目不再重复要求用户确认；没有通过或尚无完整证据的项目继续单列：

- PERF01 入口绝对值及避免每次未缓存输入新增外部判定尚未达标；模型/网关超时也影响真实主动召回。
- 60 条最新端到端质量矩阵存在路由与传输失败；v8 的分类器修复结果不能覆盖全部最终回答质量。
- A16 的真实卡片点击及无关卡片不打断、A17 的真实图片改口、K06 的双同名附件/迟到描述、E04 的云端权限恢复仍需对应完整现场，不能拿旧发布记录或单元测试代替。

### 2026-10-22 继续验收：卡片回执、素材发现与语音交接

本节为后续增量，保留前面全部失败结果。仍使用 UEE `openai/qwen3.8-flash`；语音由内置浏览器的模拟麦克风 WAV 进入真实 ASR，未用预置转录代替。

| 现场与修复 | 当前证据与边界 |
| --- | --- |
| 手动点击后查询当前决定 | 修复已处理卡片不再出现在 pending 后丢失答案的问题；限定任务的 `handled_decisions` 返回真实答案，并按时间核对后续决定。真实花店任务口播正确说明最新“周末解压”；读卡阶段没有个人助理交办。`voice-card-receipt-final.json`。无关卡片点击时故事未被打断，但调试事件缓冲曾截断，不能冒充完整传输证明。 |
| 同名图片与人工描述 | 两份同名公开测试图片使用不同资源 ID；人工说明标记选定的原图。真实语音正确识别选定记录；改名不改变 ID、人工状态和说明。AI 描述曾发生 TLS/上游失败，UI 重试后成功，失败保留。`same-name-assets-evidence.json`、`voice-assets-description-after-fix.json`。 |
| 改名后的查询落空 | 旧名称查询无命中时，服务端补充同一授权范围、同种类的少量当前候选，保留 `query_matched=false`，不按顺序自动代选。未核实/受限读取不能说成“查过没有”。相关定向批次 49 passed，`asset-voice-lookup-final-tests.log`。 |
| 素材描述被预算挤掉 | 真实 route 判定素材有用，但通用知识命中和任务概览先占预算，导致素材目录被丢弃。按语义 needs 保留当前需要的素材/经验发现记录，先裁无关召回或大任务概览，仍按完整记录裁剪、记忆块不超过 2048 tokens。修复后同名案例 1853 tokens，保留两份说明；36 项定向测试通过，`asset-budget-priority-tests.log`、`voice-assets-catalog-budget-before-fix.json`。该次口播仍念内部 ID 并多问确认，随后修复播报契约，不能把此现场标为全通过。 |
| A17 原任务未收到新规划 | 第一次实际改口已保存 exact `revision_request`，但新记忆开关使 `project_main_messages` 跳过整个旧背景区，连本轮语音交接信息也丢了。个人助理只在自己会话写新脚本，原普通任务旧卡仍 pending。已将本轮来源与历史读取分开；受限历史下只保留当前要求/目标，不带旧通话。`a17-origin-before-fix.json`、`a17-parts-before-fix.json`；投影/修改/口播定向 26 passed，`voice-revision-projection-retest.log`。 |
| A17 确认后参数漂移 | 修复投影后实际调用 `requests.revise`，正确显示工作区共享发送确认；点击后模型却加了错误附件 ID，再弹一张确认。已补前置资源归属/状态校验，以及仅针对卡片回答恢复的原参数校验；诊断漂移，不自动执行或扩大原授权。真实失败为 `a17-confirmation-drift-before-fix.json`；修改/确认/投影定向 34 passed，`revision-confirmation-drift-retest.log`。此处单元通过不代替后续浏览器复测。 |

以上批次有重叠，不相加为独立测试总数。没有运行全量测试。K06 的真实迟到 AI 与人工编辑竞态、A17 三个独立最终成功闭环及 E04 仍未取得完整通过证据；内部性能和冻结召回矩阵门槛也没有因本节修复而自动变为通过。
- 半句回复仍需原始上游终态证据；不能靠检测句号或重复请求来假定完整性。
- 仓库 AOCI 正式索引维护状态另记；未通过的治理状态不伪报对齐。

前后端启动地址为本地 3002/8082。音频验收使用的临时 MediaStream 替换在挂断后通过页面刷新恢复；QA 设置修改恢复到原详略值。迁移与回滚的具体命令和兼容边界见实施计划第六节。本轮未合并、未推送、未部署，也未改线上用户数据。

### 2026-10-23 补齐现场证据及新增缺陷

以下增量不覆盖前面的历史失败。读取仍为 UEE `openai/qwen3.8-flash`；后台提取最后升级为 `source-only-v12`。所有证据位于同一 Git 忽略目录，代码仍为本分支未提交工作树。页面为真实前端 3002、后端 8082；浏览器通过临时 MediaStream 播放 WAV 进入真实 ASR，没有伪造转录。模拟麦克风在挂断并刷新页面后恢复。

| 用例/范围 | 已验证事实及限制 | 证据文件 |
| --- | --- | --- |
| A16 无关卡片不打断 | 实际播放约 48.94 秒故事期间，在另一个任务卡输入“文案再短一点，每个镜头一句话。先给我看，不做视频。”。1215 个调试事件未截断；点击后继续收到 208 个音频帧，点击后 `playback.clear=0`；后续通知在故事播放结束后开始。没有新增语音交办 | a16-unrelated-live-window.json、a16-unrelated-transcript.json |
| K06 迟到 AI 描述 | 将真实图片识别返回延后 60 秒；人工描述在释放前 14 秒已保存，AI 返回后人工描述仍保留。此前同名图片的实际语音识别证据继续有效，未把所有 K06 条款笼统标为通过 | caption-race-events.json、completion-scope-evidence.json、caption-race-manual-preserved.png |
| A17 原图改口 | 周末、秋季、早安三个任务分别取得原会话新脚本、旧卡作废、单次共享发送确认的证据，未调用图片/视频生成。门店介绍案例曾需自然追问才取得全文，保留为受修复影响案例，不冒充独立首试成功 | a17-final-success.json、a17-autumn-final.json、a17-morning-recovery-confirmed.json、a17-intro-confirmed-no-draft.json、a17-intro-full-review.json |
| 当前页面提示 | 没有页面提示时曾把“早安”错认成“秋季”。网页/Flutter 与后端增加当前普通会话提示，重新核验权限、当前卡代次与预算，并冻结到当前发言。仅提示不代替目标核对与确认 | voice-view-backend-final-tests.log、voice-view-web-tests.log、voice-view-mobile-final-tests.log；a17-morning-first.json 保留失败 |
| 漏交办兜底 | 真实提供商两次只口头答应、无工具交办；语义确认有请求后，一次原生重试仍未执行时，原始发言通过原有接收通道提交一次。随后内部重试音频改为静默，实际纯字幕复测取得一个 VoiceTurn 与一个 task_revise，网页只确认一次，原任务出现完整 4 镜头脚本 | a17-morning-view-first.json、a17-morning-view-second.json、a17-morning-recovery-confirmed.json、a17-subtitles-final.json、a17-subtitles-complete-script.png |
| A17 样式修改的残余缺陷 | “字幕改大、白色字”正确修改同一任务并取得全文，无媒体生成；但模型同时改了几句字幕和出现时间，违反确认卡的保留范围。交接通过不等于产物范围通过，此例未标全通过 | a17-style-final.json、a17-style-final.log |
| 完整审阅正文 | 问题协议增加审阅正文，网页与 Flutter 用现有 Markdown 呈现；不能只显示“脚本准备好了”的摘要。模型仍可能不声明 review 类型，不能把 schema 当成内容真实性保证 | question-review-web-final-tests.log、question-review-mobile-complete-tests.log、a17-morning-complete-script.png |
| 更新记忆绕过期限核验 | 查询发现 v11 将不同一次性视频要求更新为同一长期记录。v12 首步只看新原话；更新已有记忆也要求合法长期意图，对话写入不再预设提案有长期效力。真实提取首步仍出现误提案，但独立核验拒绝；另外两条长期规则/习惯与一条长期纠正均保留 | one-off-memory-provenance.json、source-only-v12-real-provider.json、source-only-v12-admission-provider.json |
| v12 真实语音写入反例 | 浏览器原话“这个早安视频的字幕改大一点，用白色字，先把脚本改好给我看，视频不要做。”；原生提取 SUCCEEDED，result_memory_ids 为空，旧记录 revision 保持 5；同时实际 task_revise 成功 | source-only-v12-browser-capture.json、a17-style-final.json |
| E04 云端环境 | 原先超时的 tunnel 现在鉴权后 `/alive` 与 `/catalog` 均 200，约 3309/764 ms；`/health` 404 是端点不存在，未鉴权 403 不是会员到期。当前 shared 配置的探测不等于 per-desktop 续费恢复，E04 此部分仍未通过 | cloud-runtime-probe-20261023.json、cloud-health-authenticated-20261023.json |

语音接收回执只证明请求已交给个人助理，不证明原任务已开始重写。个别实际口播/文本仍出现过早完成或“不会花钱”的说法，不能以本地 billing=off 推断生产脚本推理免费。最新字幕复测的语音回执在挂断后记为 cancelled，只表示通话未完整交付最终播报；原任务的修改及新卡由独立 SQL 和 UI 证据确认，不把 cancelled 改写成 delivered。第一次纯字幕复测的调试帧已截断，未用它证明完整音频时序；A16 使用上表未截断的独立记录。

#### 最新定向测试

| 文件范围 | 本次结果 | 日志 |
| --- | --- | --- |
| voice view context、memory context/refresh、turns、memory read protocol 五个明确后端文件 | 74 passed | voice-view-backend-final-tests.log |
| voice handoff guard、turns、memory context 三个文件，含重试不播报 | 62 passed；上一批含 view context 的四个文件 73 passed，有重叠 | voice-handoff-silent-recovery-tests.log、voice-handoff-recovery-final-tests.log |
| Web voice session.test.ts | 30 passed；tsc 通过 | voice-view-web-tests.log、voice-view-tsc.log |
| Flutter voice_call_controller_test.dart、chat_location_test.dart | 33 passed；四个改动文件 analyze 无问题 | voice-view-mobile-final-tests.log、voice-view-mobile-analyze.log |
| 审阅卡 Web 三文件、Flutter 三文件 | 分别 41 / 43 passed；相应类型/静态检查通过 | question-review-web-final-tests.log、question-review-mobile-complete-tests.log、tsc-review-final.log、mobile-review-analyze-final.log |
| reader 目录与权限相关四个文件 | 50 passed | catalog-read-latency-tests.log |
| admission_intent、reconciliation、write_protocol、pipeline 四个文件 | 63 passed、2 skipped；覆盖源码级新/旧记忆准入、原始提取输入边界、真实 SQL 拒绝一次性覆盖、既有纠正回归 | source-only-v12-targeted-tests.log |

各批次有重叠，不相加为独立用例数；skipped 不记通过。未跑全仓测试或宽泛文件匹配。移动端本轮页面提示已有定向测试，但没有新增模拟器的视图切换语音实测，保留该限制。

#### 冻结表达矩阵与新增表达

冻结 60 例的 manifest SHA-256 仍为 `7ba7eb0712a591c0d7506ed05dbb6db6e727b988d0099085f7dab7091407467c`。最新完整聊天矩阵保留 58/60 读政策、17/20 初始必要证据、11 个受限请求不注入历史，36 complete、11 restricted、11 partial、2 条传输异常未取得完整包，最大 2007 tokens。ref-02 单独重试成功、ref-04 再次超时，不将重试拼为 60/60。见 holdout-60-v8-final-retest.json、holdout-60-v8-final-retest-assessed.json、holdout-v8-timeout-recheck.json。

预算修复后仅重测受影响的 document-01/08/10：3/3 政策符合，最终回答人工检查正确，严格初始证据条件 2/3，三例均为 partial，最大 2003 tokens。document-10 返回完整文件名但初始包未同时含原冻结标准的 20 秒/5 秒条件，因此保留失败，不放宽标准。见 holdout-document-budget-retest.json、holdout-document-budget-retest-assessed.json。

另冻结两条此前未用表达：“休息天想放松一下，但又不想挤在人堆里，你给我拿个主意吧。”与“要给店里的上新拍条短片，剪辑师问我片子多长、每段画面留几秒，我怎么回他？”。两个真实聊天 API 请求均成功，分别主动结合徒步/安静偏好、准确回答 20 秒/每镜不超过 5 秒；包为 partial/complete，分别 1580/2039 tokens。前者有 rerank_deadline，不能冒充无降级成功，也不能作为换句重试覆盖冻结矩阵。见 unseen-expressions-20261023-manifest.json、unseen-expressions-20261023-results.json。

延迟提取收尾核对：上述冻结矩阵和补充表达共有 70 个作业全部 SUCCEEDED，pending=0、result_memory_ids 全为空，记忆数 24→24。同账号并行语音测试另将一条历史任务记忆从 revision 3 改到 5，已单独定位为 v11 缺陷并修复；因此不能声称整段测试期间所有记忆完全未变化。见 final-extraction-settlement-20261023.json、one-off-memory-provenance.json。

#### 同口径内部性能复测

在同一个不可变迁移快照上新建 baseline/new 两个隔离 PostgreSQL 克隆，逐例交替顺序；每变体 5 次启动样本，另 3 组各 30 次暖请求，总计 190 个 HTTP 请求。真实 SQL/HTTP，外部模型与索引为替身，无 profiler；浏览器 QA 同机运行。入口到主模型请求就绪包含内部 SQL、队列、序列化等耗时，不表示纯 CPU，也不是用户看到完整回复的总耗时。

| 路径 | baseline p95 | 当前 p95 | 相对变化 | 当前核心上下文 p95 |
| --- | ---: | ---: | ---: | ---: |
| 问候 | 625.91 ms | 397.76 ms | −36.45% | 74.9 ms |
| 偏好 | 482.32 ms | 377.89 ms | −21.65% | 97.2 ms |
| 知识 | 515.62 ms | 447.98 ms | −13.12% | 103.2 ms |

当前五次启动样本为 798.41、360.07、335.19、292.00、359.18 ms，单独列出不混入暖请求。三个路径的数值门槛通过，不能将全部变化归因于单一优化，也不把各阶段 p95 相加。证据：perf-paired-20261023-samples.json、perf-paired-20261023-comparison.json。当前语义读取对未缓存输入仍有额外外部判断，所以 PERF01 的“不新增每次必跑外部模型调用”尚未通过。真实早安修改曾有 60 秒首进展超时，其中响应头等待约 58.07 秒、TLS 约 1.92 秒，单列为网关/网络等待，不挪入内部 CPU 耗时。

#### 当前结论与剩余工作

**不满足 R1/R2 全部验收，不能据此发布正式版。** 尚未完成的门槛包括：未缓存表达的额外模型往返、冻结矩阵失败与降级项、任务产物严格保留未修改部分、所有 A02/A13 独立重复及最新移动端视图提示现场、per-desktop 会员恢复与私有经验实际执行、半句回复的原始上游终态证据。已有单元或 shared 探测不替代这些现场。

迁移/回滚仍采用已演练的兼容构建、保留新 schema 路径，用户新写入保留；未把旧 main 二进制标为通过。AOCI 完整索引约 33 万 tokens，本次完整认知验证未完成；正式增量维护仍需按工具的治理状态处理，未签署全仓对齐。代码、证据和未通过项保留，不需要用户重新回答已确认的网关、测试范围或授权问题。

## 2026-10-23 继续验收：任务修改、文档与跨日爱好召回

以下证据位于本地忽略目录 `.local-dev/memory-upgrade-20261021/`，没有导出生产账号秘密或改线上数据。保留每次失败，测试批次有重叠，不相加为独立总数。

| 场景 | 本次结果与边界 | 证据 |
| --- | --- | --- |
| A17 咖啡店字幕改色 | 内置浏览器通过真实 ASR 说“白色改淡黄色，其他不动”。新旧卡全文严格等于一次颜色替换，12 秒、3 镜头和全部文案不变；原卡失效、新卡待审；只有 question 工具，没有生成媒体。不是全部三次独立场景通过 | a17-coffee-live.json、a17-coffee-assessed.json、a17-coffee-revised-browser.png |
| A17 延迟与口头表述 | 真实上游先超时 60 秒再重试；总运行 117495 ms，提供商等待 109638 ms、运行网络 3825 ms、剩余内部墙钟约 4032 ms。助理曾在产物到达前说“改好了、不产生消耗”；已修改指令，但不能把该次错误表述记为通过 | backend-uee58-native-batch-final.log、a17-coffee-live.json |
| 文档首次上下文 | 再问“店里的宣传资料有给出水果售价吗？”，首次已带原始文档，不再调用 memory_read_more，正确回答未提供售价、不能编造。仍是带 rerank_deadline 的 partial 包；UI 的 4.9 秒只覆盖最终模型尝试，不包含此前 60 秒超时 | document-native-batch-browser.json、document-native-batch-browser.png |
| 读取权限查询优化 | 38 份候选的只读分析由 47 次 SQL 降至 32 次。冷样本约 157.6→159.2 ms、暖样本约 40→37.5 ms，不能宣称显著墙钟提速。跨提供商等待的权限复核未省略 | retrieval-authority-profile-before.json、retrieval-authority-profile.json |
| 爱好遗漏根因 | “今天不想出门，在家有一个小时空闲……”被旧词表误判为仅查今天产生的记录，昨天保存的水彩爱好被过滤。即使给予 8 秒并成功重排，仍只有视频和资料候选，证明不只是重排超时 | voice-authority-final.json、voice-relevance-unbounded-probe.json |
| 修复后的真实语音 | 同一 WAV 经浏览器麦克风输入、真实 ASR 和原生语音模型，首包第一项为昨天保存的水彩花草爱好，回复主动推荐水彩；转交回合 0、个人助理命令 0。包为 partial，仍存在 rerank_deadline | voice-temporal-browser.json、backend-uee59-semantic-time.log |
| 修复后的新普通会话 | 同一句自然问题，在新普通会话首次回答即推荐水彩花草，未调用补充工具。只证明本例跨会话命中，不替代完整召回矩阵 | temporal-texts-browser.json、temporal-chat-browser.png |
| 文字助理换说法 | “今天下午不去店里……”触发真实 route_deadline，助理错误声称“还没记过喜好”。记为失败；已移除冲突指令并给三入口补齐读取失败语义，另外执行本地单次故障注入验收 | temporal-texts-browser.json、backend-uee60-semantic-time-final.log |

语音记忆准备本次约 6306 ms，读取判断约 5134 ms（网关观察约 5128.5 ms），检索 834 ms、最终核验 78 ms、准备 51 ms。检索中仍含外部 IO，不能将这些阶段之和称为纯内部 CPU；也没有完整浏览器首音频事件证据，本次 CDP 缓冲截断不能用来证明首音频延迟。真实转写和回复由服务端通话记录保存，结论不依赖被截断的事件流。

新普通会话总运行 **20368.8 ms**，已观测外部等待 **17568.4 ms**，剩余内部墙钟 **2800.3 ms**，其中完成步骤约 814.7 ms、完成运行约 714.6 ms、历史约 465.8 ms、模型消费约 429.7 ms。UI 的 11 秒只表示模型步骤。该现场不达 500 ms 简单路径内部目标，不能拿隔离暖样本达标覆盖它。首包记忆预算约 1.6k tokens，但完整主模型输入为 27819 tokens；二者不是同一口径。

真实语义分类保留 v9/v10 的错分及超时。v11 在 8 条针对时间的样例上，7 条严格类别与时间过滤符合预期；剩余“只根据这句话”选择 `none` 而非预设 `event_time`，严格分类仍记未通过，但读取限制正确为 `current_only`，没有硬过滤或历史授权。另抽取原冻结集 6 条权限/否定/任务改口反例，5 条返回正确、1 条网关 6 秒超时并保持不授权。没有扩大超时，没有自动改成允许读取。证据：temporal-router-real.json、temporal-router-v10-real.json、temporal-router-v11-real.json、holdout-router-v11-restrictions.json。

本轮列名运行的测试：

- `test_memory_configured_read.py`、`test_memory_time_context.py`、`test_memory_read_protocol.py`、`test_memory_source_time.py`：90 passed（temporal-v11-tests.log）。
- 时间诊断追加后，`test_memory_time_context.py`、`test_memory_read_protocol.py`、`test_memory_document_priority.py`：31 passed / 7 skipped（temporal-diagnostics-tests.log）。
- `test_sql_source_timestamp_obeys_local_day_in_sqlite_and_postgres`：SQLite 与隔离 PostgreSQL 各通过，2 passed；验证同一旧记录在历史日期查询中排除，在活动日期查询中仍可使用（temporal-postgres-tests.log）。
- 读取失败说明改动后，`test_assistant_intro.py`、`test_memory_personalization.py`、`test_voice_memory_context.py`、`test_memory_read_protocol.py`：52 passed（read-failure-guidance-tests.log）。
- 前段提案与检索验收：73 passed / 5 skipped、87 passed / 50 skipped；原生来源批处理最终 8 passed；原始文档优先在隔离 PostgreSQL 最终 7 passed。文件分别为 retrieval-reserve-revision-tests.log、retrieval-bounded-io-tests.log、native-authority-invalid-tests.log、document-deadline-postgres-final.log。

第一次时间定向批次是 142 passed / 43 skipped / 1 failed，失败为旧测试桩未接收新增显式时间依据；调整后又暴露旧断言只允许一份返回，与当前可保留原始来源的检索契约冲突。修正为核验指定记忆及其原时间戳，同时保留另一日期不得返回的断言；单节点复测通过（temporal-source-final.log）。这些失败记录未覆盖或删除。

**R1/R2 仍未全部通过，不发布。** 除上述真实超时和失败说明复测外，原先列明的 A02/A13 独立重复、A17 全部独立最终成功闭环、移动端最新现场、per-desktop 会员恢复/私有经验实际执行，以及未缓存输入固定外部判断仍未关闭。当前改动没有新数据库 schema；保留已有迁移与兼容回滚演练结论，不将其扩大到旧 main 二进制。

### 读取失败说明的真实与可控演练

第一次计划注入故障的启动器通过 `os.execv` 更换了解释器，进程内补丁并未生效。该轮实际是 UEE 真实读取超时，修复后的文字助理正确说“这次没能读取到你的偏好”，不再说没保存或要求用户重录。该结果归为真实故障验证，不冒称可控注入；证据 read-failure-browser.json、read-failure-browser.png、backend-uee61-read-fault.log。

修正本地测试启动器，在同一进程中启动服务后，使用另一句普通表达“今天剩下这一小时我想留给自己，不出门，你按我的兴趣给我出个点子吧”。日志确认仅一次注入，当前包 `unavailable / route_deadline`、无历史条目、无补充工具；真实主模型最终说明“这次没能读取到你的偏好，我先给你一个通用建议”，没有捏造保存状态。证据 read-failure-injected-final.json、read-failure-injected-browser.png、backend-uee63-read-fault-confirmed.log。主模型另外发生 503、60 秒超时和重试，整轮约 126.7 秒；其中人为注入的 6 秒不能当作正常内部性能样本。测试后停用故障启动器并恢复正常本地服务。

正常启动增加一次后台连接预热，复用已有 HTTP 客户端池，不发送用户文本或密钥、不调用模型、不阻塞健康检查，关闭时取消并回收。真实 HEAD 返回 404 只证明连接完成，不证明模型可用；观察到约 2.75/5.40 秒的连接过程由后台完成，不能据此声称网关响应时间已解决。`test_memory_configured_read.py`、`test_database_readiness.py` 定向 80 passed（transport-startup-tests.log）。

## 2026-10-23 继续验收：检索计划 v12 与内部等待定位

本节记录最后一批源码修正和真实失败，不覆盖先前结果。读取通道仍是 UEE `openai/qwen3.8-flash`；没有放宽 6 秒读取判断、1 秒检索和 2048 token 初始记忆预算，也没有运行全仓测试。

| 检查 | 结果与边界 | 证据 |
| --- | --- | --- |
| 上一批正常个人助理恢复 | 原先失败的“今天下午不去店里……”在正常服务下主动建议古筝与水彩，初始证据正确、未补充调用；仍为 partial/rerank_deadline | temporal-assistant-recovered.json、temporal-assistant-recovered.png、backend-uee64-final.log |
| 继续定位补查 | “下午想休息一个小时，不出门，你按我的兴趣帮我安排一下吧。”在 v11 首包没有水彩，主模型补调 memory.search 后才找到；诊断启动器只加计时，没有模型替身或人为延迟 | span-probe-browser.json、backend-uee65-span-probe.log |
| v12 同句个人助理 | 既有判断器返回“用户兴趣偏好 室内休息活动”，首次上下文已有古筝/画画 Wiki，最终回答正确、没有补读。水彩的具体类型也出现在本会话历史中，不能据此声称该次首包包含水彩原记录；上游 503 重试保留 | semantic-query-assistant-browser.json、backend-uee66-semantic-query.log |
| 两次全新普通会话 | 均为 route_deadline，第一次 TLS 约 1.21 秒、上游头约 4.70 秒；第二次主要耗在 TLS（约 5.96 秒）。回复如实说明本次未读取到；记为主动召回失败，不能以同账号文字助理成功替代 | semantic-query-final-text-evidence.json、semantic-query-chat-timeout.png、semantic-query-final-chat-timeout.png |
| 建议按钮遗漏与修复 | 主回复已正确降级，但原按钮仍提示“告知兴趣重新定制”。修复建议生成指令后，单次可控故障中真实回复不再要求重录，也没有无效历史工具调用；按钮为“换成轻度拉伸方案”“换成深度小憩方案”“重试读取偏好记录” | read-failure-suggestions-final.json、read-failure-suggestions-final.png、backend-uee68-suggestion-fault.log |
| 最终代码真实语音 | WAV 通过浏览器麦克风输入，真实 ASR 准确识别“今天不想出门，在家有一个小时空闲……”；第一条证据为水彩花草，实际原生回复推荐水彩；转交回合 0、个人助理命令 0。准备约 3259 ms，判断 1991 ms、检索 821 ms、收尾 107 ms，包 1569 tokens，仍有 dense_deadline/rerank_deadline | voice-query-v12-browser.json、voice-query-v12-events.json、backend-uee69-final.log |
| 冻结矩阵定向分类 | 原冻结集 10 条，覆盖个人建议、文档、限制、否定和改口；读取策略 10/10 符合，1 条用了同截止内的协议修复。只验证分类器，不替代端到端 60 条矩阵；最大 5731 ms，不宣称快速稳定达标 | holdout-router-v12-query.json、holdout-router-v12-query.log |
| 写入及迁移收尾 | 本批 6 个提取作业全部 SUCCEEDED，产生的 memory IDs 全为空，现存记忆 24 条；未删除旧污染反例。迁移版本仍 ph84a5b6c7d8，通话 ended，本地服务 /health 为 ok | query-v12-extraction-settlement.json |

最后的语音 CDP 缓冲包含截断，且挂断时 UI 仍显示播放状态。因此本次只证明真实转写、服务端完成回复、首包命中及没有转交，**不证明完整浏览器音频播放时序或最后音频全部播放完**。挂断后已刷新页面，取消单页虚拟麦克风替换；正常后端已恢复，不保留故障注入或诊断包装。

### 内部耗时的实际组成

`internal_ms` 是扣除已观测外部等待后的墙钟时间，包含 SQL、排队、解析、结果持久化及应用控制的重试退避，不等于 CPU 时间，也不包含入口前排队。新增退避跨度不改变原统计口径。

| 实际运行 | 外部等待 | 内部墙钟 | 说明 |
| --- | ---: | ---: | --- |
| 正常个人助理恢复 e379035e | 23563.0 ms | 7535.3 ms | 请求准备 2376.0 ms、上下文内部 1207.1 ms、模型阶段内部 2544.8 ms；该次尚无独立退避跨度，不能凭后次比例分摊 |
| 计时诊断 55436156 | 22771.3 ms | 4214.7 ms | 请求准备累计 965.2 ms、上下文内部 758.5 ms；发生补查和第二轮模型，单次测量不是 p95 |
| v12 文字助理 fa8a60af | 65419.6 ms | 3932.0 ms | 一轮主模型逻辑调用，503 传输重试仍存在；请求准备 407.5 ms。不同网络/流程，不能当受控前后提速比例 |
| 最终新会话 da14cbb2 | 91059.0 ms | 8364.5 ms | **5437.8 ms 是 provider.retry_backoff，其余约 2926.7 ms**；退避仍计内部，不隐藏用户等待 |

数据汇总 `continuation-latency-20261023.json`。本批直连/现有系统代理各 3 次独立无鉴权 HEAD 探测也观察到 TLS 波动；响应 404 仅表示端点不支持 HEAD，不是模型健康证明。没有据此自动切换网关、分组或全局代理，见 uee-transport-paired-20261023.json/.log。

### 最新定向验证与发布结论

- 读取适配器、读取协议两个明确文件：76 passed（semantic-query-targeted-tests.log）。覆盖可选检索词坏格式、原始时间依据、受限读取不受检索词影响、仅一次判断。
- 缓存新增精确节点、Agent 重试文件、Agent 计时文件：22 passed（semantic-cache-retry-timing-tests.log）。相同实际输入可复用；前文是否存在及内容版本变化仍失效；当前检索引用新前文；退避可取消且不被算作外部网络。
- 建议生成、助理介绍、记忆个性化三个明确文件：79 passed（read-failure-suggestions-tests.log）。随后完成上述真实故障按钮验收。

测试批次可能重叠，不相加为独立总量；失败和重试证据均保留。本轮没有新增 schema，先前迁移和兼容功能回滚范围不扩大到旧 main 二进制。**R1/R2 仍未全部通过，不能签署上线**：真实读取和主模型网关稳定性、未缓存输入固定外部判断、冻结矩阵剩余失败，以及原 A02/A13/A17 的完整独立重复、最新移动端和 per-desktop/私有经验执行门槛尚未全部关闭。

## 2026-10-23 通道路由纠正：Qwen 官方、JEV UEE

用户澄清 Qwen 应使用已有阿里官方 Key，JEV 才使用 UEE 代理。核查发现 Qwen 的模型目录未声明 provider，继承了 `openai` 槽位的 UEE 地址。已在本地私有配置和受版本控制的示例中将两个 Qwen 模型显式绑定 `bailian`，使用 `BAILIAN_KEY` 环境引用；没有把实际密钥写入示例或证据。既有模型 ID、读取协议 v12、权限门禁及总截止保持不变。仅本地 QA 的 NO_PROXY 增加 `dashscope.aliyuncs.com`，官方请求不经本机 UEE 代理路径。

| 验证 | 结果 | 证据（同一忽略目录） |
| --- | --- | --- |
| 官方接口真实初探 | 两次判断均成功，约 2522 / 1969 ms，返回 `qwen3.8-flash`；分别正确允许个性化读取和限制为当前输入 | aliyun-direct-initial-probe.json |
| 冻结样本定向子集 | personal/document/restriction/negation 共 10 条，策略 10/10，全部单次请求，1807–3260 ms；不是完整端到端矩阵或长期稳定性证明 | holdout-router-v12-aliyun.json/.log |
| 路由与凭据隔离 | Qwen Max/Flash 使用百炼槽位；MockTransport 检查 URL、模型和 Authorization，官方 Key 缺失时不借用 UEE 凭据。早期模拟 JEV 聊天响应的用例仅检查凭据，不能作为 JEV 协议兼容证据，已替换为错误协议拒绝用例 | test_memory_configured_read.py；aliyun-provider-routing-tests.log（历史批次） |
| 定向单测 | 读取适配器文件及 config 的无退役模型精确节点，60 passed；仅既有 Pydantic 弃用警告 | aliyun-provider-routing-tests.log |
| 内置浏览器全新普通会话 | 快速档 Qwen，输入“下午想休息一个小时，不出门，你按我的兴趣帮我安排一下吧。”；官方读取判断成功 2053 ms，官方主回答请求成功 12501 ms，无传输重试 | backend-aliyun70-final.log、aliyun-chat-browser.png |
| 本轮召回仍有遗漏 | 判断后检索 1085 ms 超过 1 秒预算，状态 partial/recall_deadline、items 为空；只利用 core 中的爬山偏好，没有带入水彩记录。本例记为路由连通通过、完整主动召回未通过 | aliyun-chat-browser-evidence.json；会话 session_7YBTCKNT5V31YW01NXAS84C2XH |
| JEV 经 UEE 错误接口探测 | `openai/jev-1.14.0` 的聊天调用返回 HTTP 503（约 2590 ms）；后续 AWS 核查证明该插件不走聊天协议且未声明 1.14，不能用此结果判定 JEV 通道不可用。实际读模型仍为官方 Qwen | provider-split-verification.json；uee-aws-readonly-20261023.json |

本例 Agent 总耗时约 21590 ms、已观测外部等待 13489 ms、内部墙钟约 8101 ms。主模型原始流式 trace 和外部等待计量覆盖范围不同，不能相减混算；本例未出现退避，因此不能套用上一节 5.44 秒退避解释。官方通道解决了错误路由，但不能证明内部处理、检索截止及全部记忆质量门槛已经解决。本次无新增数据库迁移，未部署线上。正式部署必须同步私有模型目录的 provider 绑定，不能只更新代码。

官方参数核对：[百炼 Chat Completions 文档](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)说明 Qwen 3.8 接受 `reasoning_effort=none` 关闭思考；现有读取判断请求可继续使用该参数。

## 2026-10-23 JEV / AWS 只读核查与协议纠正

结论纠正：此前普通聊天接口返回 503，不能推出 AWS 的 JEV 通道不可用。AWS 只读核查显示 TypeSafe 插件 1.1.0 为 enabled/active/registered，通道 21 和三个模型的分组能力均启用，运行时没有插件错误。`jev-1.13.0` 累计有 2770 条成功消费记录，最近一条是北京时间 **2026-10-23 10:01:32**；latest/preview 各有两条历史成功记录。这是已有运行证据，不是本轮重新发起的模型成功调用。

当前服务注册 `/typesafe/v1/systemone`，而非 `/v1/chat/completions` 或根路径 `/v1/systemone`。源码中的通用聊天分发会排除 TaskPlugin 通道；通用渠道测试按钮也会直接拒绝 TaskPlugin。因此“default 无可用渠道”和“Task Plugin channel test is not supported”都不足以证明上游 JEV 故障。[官方原生协议](https://docs.typesafe.ai/api/system-one)使用 `model/state/questions` 与 `model/answers/usage`，不接受本项目配置型读取器的聊天消息契约。当前插件只声明 `jev-1.13.0`、`jev-latest`、`jev-preview`，未声明 `jev-1.14.0`。

本地修正：移除把 JEV 当聊天模型的错误配置说明及模拟成功用例；配置型读取器在遇到 `jev-*` 时返回 `read_router_protocol_mismatch`，不预热、不发聊天请求、不转用原生接口或其他凭据，也不放开未确定的记忆读取。Qwen 的阿里官方绑定保持不变。没有修改 AWS、线上数据库或线上模型通道，没有新增迁移。

| 验证 | 结果 | 证据（`.local-dev/memory-upgrade-20261021/`） |
| --- | --- | --- |
| 官方直连 | `/v1/models` 返回 451，正文明确为地区限制；未据此判定 Key 无效、模型慢或不可用，未更换出口重试 | jev-official-direct-20261023.json |
| AWS 只读核查 | 当前插件路由、通道状态、模型能力、已有调用记录核对完成；无线上修改 | uee-aws-readonly-20261023.json |
| 定向回归 | configured_read、provider_client、read_protocol 三个文件及错误原生响应的精确节点，**97 passed**，仅两个既有 Pydantic 弃用警告 | jev-protocol-targeted-tests.log |
| 插件离线契约 | 本地保存的 1.1.0 插件与当前后端原生问题/响应契约核对；三个模型的请求、响应、计费用量及五个拒绝场景，**8 passed**，使用假凭据和 `.invalid` 地址，无外部调用 | jev-plugin-contract-offline.json/.mjs |

剩余边界：三个模型本轮的实时连通性与延迟没有重新验收；原生适配器目前读取官方凭据，不能假定只改 URL 即完成 UEE 接入。原生读契约还缺少配置型读取器的时间依据、限制原话及检索词扩展能力；切换前需完成能力对齐及独立样本质量验证。当前实际读模型仍是官方 Qwen，本节不改变 R1/R2 未通过的整体结论。

## 2026-10-23 继续记忆验收：语言决策、检索与原生语音

本轮只在本地 QA 数据库 `openbox_memory_upgrade_20261021`、网页 3002、后端 8082 操作。普通会话选择快速档官方 Qwen；个人助理为排除主模型网关干扰曾临时改为官方 Qwen，随后恢复原 Gemini 配置。没有修改生产数据，没有运行全量测试。以下证据位于 `.local-dev/memory-upgrade-20261021/`，历史失败文件保留。

### 发现及修复

1. **固定交谈语言不能依赖普通记忆排名。** 修复前文字助理确认“以后英语、简短回复”，实际存为普通记忆，新会话仍用中文。现在语言写入当前账号设置，复用来源核验、revision、CAS 和幂等回执。设置页、提取器、工具、普通会话、网页/Flutter 和语音都消费同一字段；清空表示明确恢复跟随本次语言，旧英语记忆不能将它复活。
2. **旧后台消费者可能覆盖新字段。** 14:51 保存中文后，14:52 的旧提取消费者只保存长短偏好，却整体替换 JSON，使语言及其决定元数据消失。查明昨天启动的 PID 40012 已停止监听但仍消费同一 QA 数据库；温和退出无效后终止该本次验收遗留进程。其他工作树的 8084 使用另一数据库，未触碰。新写入逻辑保留未知字段，对外只暴露当前合法字段，新增设置/提取/初识三条写路径回归。15:30 重新保存英语，经新会话和后台作业后仍保持；15:39 恢复默认语言，最终 revision 38，明确清空回执仍在。
3. **检索重复查询与串行等待。** 批量读取核验原始来源，合并撤销状态查询，避免空候选的 Wiki/文档扫描；授权范围检查后关键词 SQL 与查询向量请求重叠执行，失败或取消时回收在途请求。候选内容仍通过 SQL 来源及版本验证，重排后重新核验。没有增加 6 秒路由、1 秒检索和 2048 token 记忆包限制。

### 内置浏览器真实验收

| 场景 | 实际结果与判定 | 证据 |
| --- | --- | --- |
| 文字助理自然设置英语 | “以后和我聊天都用英语，回答短一点就行。”写入账号语言；工具回执 revision 33，后台提取规范化后 34 | language-current-settings-after.json、language-assistant-tool-receipts.json |
| 新普通会话识别文件 | 问“店里那份拍摄说明，完整文件名是什么？单个镜头最长几秒？”自动英语回答，完整保留 `青禾鲜果店门店视频制作与到货宣传注意事项（最终确认版本）.txt` 和每镜头最多 5 秒；使用一次补充读取。**最终答案通过，首包仍 partial/recall_deadline** | language-document-new-chat.json/.log、language-document-new-chat-20261023.png |
| 原生语音遵循英语 | 中文音频询问在家一小时做什么，真实 ASR 后英语回答并主动提及水彩花草；没有转交文字助理 | language-voice-live-transcript.json、language-voice-playback-complete.png |
| 通话中改为中文 | 文字助理保存中文后当前通话刷新 ACK 约 264 ms，随后实际中文播报并提到水彩。264 ms 仅为刷新确认，不是用户发言到首音频；随后旧消费者覆盖问题另记失败。HMR 结束了该通话，未冒称第二段用户音频已验证 | backend-memory-language74.log、language-voice-live-transcript.json |
| 停止旧消费者后，英语保存与持久化 | 浏览器设置页保存、刷新保留英语；全新会话自动英语回答，后台处理后字段仍在 | language-settings-persisted.png、recall-stable-worker-before.png |
| 全新普通会话主动兴趣召回 | 原句“下午想休息一个小时，不出门，你按我的兴趣帮我安排一下吧。”仍只给茶、阅读、爬山照片等建议，漏水彩；工具调用 0。**失败**，不会把语言正确当作记忆完整通过 | session_7YBTC55DFZRMV2Z177S210VN1P；recall-stable-worker-before.json/.log/.png |
| 恢复默认语言且禁止历史 | “这次不参考以前的记录，只把‘水果今天到货’说得自然一点。”使用中文，`no_history/restricted`、历史 items 0、补充工具 0。称呼仍来自当前账号设置；对改写增加“新鲜”一词不作严格事实忠实通过的扩张结论 | language-reset-restricted.json/.log/.png、language-settings-final.json |
| 最终原生语音复测 | 音频“今天不想出门，在家有一个小时空闲，你觉得我做点什么会比较开心？”经实际 WebAudio 麦克风 → ASR → 官方原生模型，中文回答主动提及**水彩画花草和古筝**。记忆状态 complete，无降级；VoiceTurn 0、同期个人助理新消息 0，未交办文字助理 | call 01M5KVVD80DSRR0S1KQ06TMSYW；language-reset-voice-events.json、language-reset-voice-transcript.json、language-reset-voice-memory.json |
| 语音播放与清理 | 浏览器收到 99 个音频数据帧，等待 UI 从“在说话”回到“我在听”后截图，挂断并确认服务端 ended，刷新移除单页虚拟麦克风 | language-reset-voice-playback-complete.png；backend-memory-language76-final.log |

全部音频用普通用户表述，没有在请求中透露“水彩/古筝”答案；没有通过注入转写字符串代替麦克风链路。语音成功与普通会话失败分别记录。

### 性能与质量边界

普通会话失败样本：路由 2732 ms、检索 946 ms、最终处理 432 ms；关键词阶段 434 ms（与向量阶段重叠）、授权 218 ms，留给重排约 59 ms，最终复核 234 ms。重排截止后 RRF 候选偏向“用户想要休闲建议”等历史请求，实际爱好没有进入最终包。该条记录存在，不能解释为“没保存”。

独立定位请求允许 3 秒 IO（**仅诊断脚本，产品配置未改**），实际 757 ms 完成：向量 362、索引 31、首次核验 122、重排 141、末次核验 80 ms；52 条 SQL 总计约 257 ms，水彩原话和对应记忆排前两位。该样本与浏览器并发负载不同，不能据此声称 1 秒生产预算稳定通过或计算优化百分比。见 recall-diagnostic-full-rank.json/.log。

最终语音样本：路由 2132 ms、检索 605 ms、最终处理 56 ms；原生准备日志约 2885 ms。检索中向量 139、索引 16、授权 82、重排 272、末次检查 81 ms，包含外部等待，不能称为纯内部耗时。记忆包 1344 tokens、核心 460；该次完整召回通过。

文字入口仍有显著内部墙钟：普通会话总 19005 ms、已观测外部等待 10380.8 ms、内部 8624.2 ms；受限读取会话总 11530.9 ms、外部 6470 ms、内部 5061 ms。后者历史阶段约 1418、模型消费阶段内部约 1379、收尾运行约 1014、收尾步骤约 527 ms。必须继续定位读取/持久化/并发负载，不能把它们归给模型或用语音样本抵消。见 memory-latency-language76.log。

记忆包样本 365/1344/1589/1711 tokens 均不超过 2048；普通会话完整主模型输入仍约 2.6–2.8 万 tokens，工具 Schema 等其他部分未包含在记忆预算中。没有 p95、长稳或全矩阵通过结论。

### 本轮定向检查

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 检索运行时与读取协议 | 76 passed / 46 skipped；隔离 PostgreSQL 的 10 个精确节点另行通过 | recall-parallel-targeted.log、recall-postgres-parallel-targeted.log |
| 写入、账号偏好、读取、语音上下文、管道 | 88 passed / 2 skipped | language-contract-targeted.log |
| 个性化、语言提取精确节点、原生写入及语音刷新 | 56 passed | language-extraction-voice-targeted.log |
| 明确恢复默认语言与旧回执重放 | 69 passed | language-reset-targeted.log |
| 跨版本字段保留、设置与初识写路径 | 55 passed | language-forward-compat-targeted.log |
| 网页设置和实时同步 | 2 文件 11 passed；`tsc -b` 成功 | language-web-targeted-final.log、language-web-typecheck.log |
| Flutter 偏好 revision 与设置页 | 2 文件 10 passed，`--no-pub`；不等于最新 APK/真机验收 | language-mobile-targeted-final.log |

批次有重叠，不相加为独立用例总数。首次网页断言遗漏既有 CAS 参数导致失败，修正为同时严格核对修改字段、expected_revision 与 operation_id 后通过，原失败日志保留。`git diff --check` 通过。

数据库当前 head 为 `ph84a5b6c7d8`。语言字段使用已有 JSON，无本轮新增 schema；保留已有迁移/兼容回滚演练结论，不扩大到旧 main 二进制。发布需确保 API 和后台消费者使用同一新版本；旧版本回滚前须带上未知字段保留补丁或停止其偏好写入，不能假定旧代码会保留新字段。

**结论：本轮偏好持久化、默认值恢复、受限读取、文件最终答案和原生语音定向场景通过；普通会话主动召回与内部耗时仍不通过，R1/R2 不签署上线。** 原冻结矩阵剩余失败、独立重复、最新移动端现场和 per-desktop/私有经验执行门槛继续保留，没有因本轮成功场景而自动关闭。

## 2026-10-23 源码结构修复与最终验收

本节更新前一节的普通会话结论，不覆盖历史失败。环境仍是本地 3002/8082、独立 QA 账号与数据库，读取和普通会话使用官方 Qwen。个人助理原 Gemini 配置未改，原生语音为 `qwen-audio-3.1-realtime-plus`。无生产数据写入、删除、提交或部署；只运行下列定向测试。

### 根因与改动

| 根因证据 | 源码修复 | 验证边界 |
| --- | --- | --- |
| 高频旧反馈和一次性请求挤占 512 token 核心包；真实水彩与古筝记录存在但排名靠后 | 按事实类型、来源、范围和期限分层；明确个人事实优先；不删除旧反例 | 单元验证干扰项高命中也不能挤掉核心事实；原失败句及新表达真实复测 |
| 不同存储类别各自归一排名，弱候选被重复加权；背景与搜索重复读取 | 共享语料 BM25、按检索通道 RRF；共用首次读取与授权；发布有界核心检查点 | 重新排序、重排中撤权与核心过期、来源版本复核均有定向 PostgreSQL 测试 |
| 普通聊天先装入 40 个工具，工具代理占约 22112 tokens | 默认使用已有 portable 按需工具发现，常驻 15 个工具约 7631–7871 tokens | 权限没有放开，发现与执行回归通过；不等于所有业务工具端到端验收 |
| 文件问题首次真实运行仍报 `32624 > 32000` | 工具预算计入实际提供商 function 包装、数组等开销 | 同一失败会话点击重新生成后实际 31190 字符、19 工具，回答成功 |
| 19 个缺少成功终态的 Inbox 条目被恢复任务反复锁定、回放；CPU 采样另见侧栏递归序列化开销 | 恢复先过滤真实成功终态，维护扫描每 30 秒最多一次；侧栏类型化响应；SQL 聚合观测 | 不删除条目；成功回合漏回执仍恢复；加入前历史不回填；私有字段不暴露 |

CPU 诊断只用于定位：样本 339 秒内 `jsonable_encoder` 累计约 29.7 秒，异步/greenlet 累计统计不能分摊成每次用户请求。早期被动观察也发现多个浏览器后台请求频繁，未证明其全部来源；最终 QA 页的独立样本显示余额按约 15 秒轮询，没有据此宣称所有页面请求频率问题已解决。

### 内置浏览器结果

证据均在 `.local-dev/memory-upgrade-20261021/`。输入没有写入爱好答案、内部工具名或特殊指令。

| 场景 | 真实结果 | 证据 |
| --- | --- | --- |
| 原失败句：“下午想休息一个小时，不出门，你按我的兴趣帮我安排一下吧。” | 第一次结构修复后建议古筝与画花草；最终正常启动器下明确建议水彩花草、音乐只写“轻音乐”。两次均没有补查工具。最终搜索仍 `partial/recall_deadline`，依靠已核验核心包，不能称深层搜索完整通过 | structural-original-query.json、structural-original-text.json；structural-final-original.json/.png；最终会话 session_7YBTBRZ4TDKE5WNFRMM5YBM3A6 |
| 不同说法：“最近有点累，想在屋里安安静静待一会儿，有什么适合我的消遣？” | 主动推荐水彩花草与古筝，无额外补读；`partial/rerank_deadline` 如实保留 | structural-paraphrase.json/.png |
| 文档：“店里的短视频我准备开拍了，一个镜头最长能拍几秒？之前的资料叫什么来着？” | 首次工具预算失败；修复后同会话重新生成，正确回答最长 5 秒，给出带“最终确认版本”的完整标题主体。本次未说出 `.txt` 后缀，不将其算作含扩展名的完整文件名复测；初始包因证据/目录预算为 partial | structural-document-budget-failure.png；structural-document-fixed.json/.png；会话 session_7YBTBWJP3152E7QD3J42V1DR7M |
| 限制：“这次不参考以前的记录，只把‘水果今天到货’说得自然一点。” | `restricted`、历史 items 0、补查工具 0，中文回答。当前称呼来自账号设置；“新鲜”属于生成的宣传措辞，本例只判历史读取隔离，不扩张为严格原文事实忠实通过 | structural-restricted.json/.png；会话 session_7YBTBQC02ZKB0BT6BEHPKENVKR |
| 原生语音：“今天不想出门，在家有一个小时空闲，你觉得我做点什么会比较开心？” | WAV 经浏览器 WebAudio 麦克风→真实 ASR→原生模型，准确转写并主动推荐水彩花草和古筝。`complete`、无降级、包 1301 tokens；转交回合 0，同期个人助理新消息 0 | call 01M5M90SC5MVYN978VJM5REDMB；structural-voice-transcript.json、structural-voice-memory.json、structural-voice-events.json |
| 语音播报与清理 | 浏览器接收 82 个音频数据帧，包含开场及回复；确认 UI 回到“我在听”后截图再挂断，服务端 ended。刷新后确认一次性麦克风和 WebSocket 观察器已清除 | structural-voice-playback-complete.png；backend-memory-structural82-final.log |

早期 `structural-original-query.png` 实际捕获了用户切换后的知识库页面，已排除出该用例截图证据；该次回答由消息正文与调试记录核对。最终普通会话截图已逐张检查与结果对应。

原生语音记忆准备日志 2548 ms，其中读取包记录 setup 31、route 1570、recall 719、finalize 50 ms；不同测量边界不能强行求和等同。检索包括向量 204、关键词 55、索引 24、首次授权 76、重排 304、最终核验 81 ms，部分阶段重叠；不是纯内部时间。语音使用既有代理连接，未把它误称为本机官方无代理链路。

### 性能样本及未通过项

| 样本 | 主模型输入 tokens | 内部墙钟 ms | 外部等待 ms | 说明 |
| --- | ---: | ---: | ---: | --- |
| 修复前原句 | 28015 | 8624.2 | 10380.8 | 前一节基线；语言设置与后台负载不同，不是严格受控配对 |
| 排序/工具缩减后原句 | 16739 | 7607.2 | 9185.3 | 显示只减少上下文尚未解决后台开销 |
| 后台/预算修复后文档 | 18755 | 1612.1 | 8909.9 | 不同任务，且有被动 HTTP 观察器；不作为原句同比 |
| 最终正常服务原句 | 15469 | 3737.9 | 69710.7 | 官方主请求约 55 秒，其中上游响应头约 47.2 秒；另有 sandbox 网络约 15.0 秒，重叠按并集计量 |
| 最终正常服务受限读取 | 14930 | 5492.5 | 5757.4 | 收尾约 1948 ms、请求准备约 841 ms；内部仍有波动 |

最终原句内部主要为：历史 811、请求准备 963、模型流消费 642、上下文 355、工具目录内部 283、收尾步骤及运行 351 ms。SQL 仅记录阶段次数与时长；并发耗时不能简单累加成墙钟。内部日志包含数据库、排队、解析及持久化，不含入口前等待，也不把应用重试退避伪装成外部网络。主模型 trace 与整轮外部等待口径不同，不相减混算。

当前可确认：减少上下文、核心兴趣召回、工具预算、无效恢复回放这几处修复有效。**尚未确认稳定速度目标与完整深层召回**：最终文字仍有 1 秒搜索截止，内部样本仍为 3.7–5.5 秒，官方上游仍有慢响应。没有反复更换提示词筛成绩，没有扩大检索截止或放松权限来换通过。此前冻结矩阵、独立重复、最新移动端现场及 per-desktop/私有经验执行门槛仍保留，R1/R2 不签署上线。

### 本批定向回归、配置与回滚

| 范围 | 结果 | 日志 |
| --- | --- | --- |
| 分层召回、读取、个性化、配置、工具暴露及预算 | 155 passed / 49 skipped | structural-targeted.log |
| 分层与权限检索 PostgreSQL 精确节点 | 13 passed | structural-postgres-targeted.log |
| 按需发现和工具执行循环 | 18 passed | structural-discovery-targeted.log |
| 延迟计时和数据库就绪检查 | 39 passed | structural-latency-targeted.log |
| 提取恢复、工具预算、提供商载荷、发现与执行循环 | 101 passed / 2 skipped | structural-recovery-budget-targeted-fixed.log |
| 侧栏响应及助理基础接口 | 10 passed | structural-sidebar-targeted.log |
| 成功回合恢复、无效终态过滤、遗漏作业、加入前历史隔离 PostgreSQL 节点 | 4 passed | structural-recovery-postgres-fixed.log |

测试批次有重叠，不相加为独立总数。SQLite 不支持的 PostgreSQL 锁定节点使用隔离 PostgreSQL 精确补测。开发中 JSON 适配和测试前置准入时间的失败日志保留；修正后通过。没有全量测试，`git diff --check` 通过。

本批没有新增 schema，head 仍为 `ph84a5b6c7d8`，没有业务数据删除。新增默认配置可用 `OPENBOX_TOOL_EXPOSURE_MODE=legacy_eager` 回退为旧工具暴露；此前兼容构建回滚与消费者同版本要求不变。最终后端使用正常 `start_backend.py`，不保留 CPU profiler 或诊断中间件，前后端继续运行供本地复核。

最终数据库收尾核对：上述 5 个文字会话各 1 个提取作业均为 SUCCEEDED，结果 memory IDs 均为空；语音状态 ended，schema head 已实际查询确认。见 `structural-final-settlement.json`。这只证明本批没有产生新记忆写入效果，不替代所有历史污染或未来模型提案的质量保证。


## 十六 2026-10-23：目录按需获取、截止保留与网关重试修复

### 源码变更与边界

1. `ResolvedStepTools.deferred` 和 `agent/deferred_tools.py` 使普通会话的 portable 模式先只装本地工具。调用 `capability_search`、`skill_search` 或 `skill` 才取得同一授权目录；不启动后台预热，不把未知目录解释为不存在。新能力只能写入发现凭证、在下一轮请求获得执行资格，不能扩大当前响应的执行白名单。继承权限的子代理、native_auto 和个人助理仍走原授权路径。
2. 技能工具常驻简短使用说明，通过搜索返回目录。真实泰语咨询最初只给通用建议；补回通用的“专门任务先查工作流程、每个新请求重载、技能不扩权”规则。后续普通中文要求检查已安装流程时，两次真实 `skill_search` 成功。没有制作视频、安装技能或改动发布内容；这只验收目录读取，不宣称泰语制作能力通过。
3. 深层候选经过排序、相关性和预算筛选后提供同步检查点。外层截止取消 SQL 时保留候选，发送模型前仍用当前权限、来源正文与版本重新验证。截止仍为 1 秒，不放宽相关性阈值；过期候选复核后被移除。完整结果与检查点共用打包逻辑，诊断明确 partial/known gaps。
4. `create_assistant_message` 可在同一受运行锁保护的事务写入初始 StepStart；提交后先发布 message.created 再发布 part.created。失败整体回滚。去掉紧邻 canonical 受锁读取之前的重复 lease 查询，保留 canonical 内部锁和运行校验。流式 UI 仍逐字更新，全文持久化间隔为 1 秒，计时从上次提交结束算起，避免慢提交立即触发下次提交。首块、结束和出错时仍保存，错误时额外保留完整已收到的 reasoning。
5. `ModelResponseDeadline` 覆盖首个有效输出、流内空闲和累计请求等待；使用量、空块不能刷新期限，消费者数据库/工具执行不计入等待预算，取消关闭源流、已观察到的 usage 仍只结算一次。默认普通请求 30/60/180 秒；明确高推理或压缩为 90/60/600 秒。可按 ModelConfig 的 `first_progress_timeout_seconds`、`stream_idle_timeout_seconds`、`response_timeout_seconds` 覆盖。真实个人助理测试发现旧 `GatewayTimeout(RetryableError)` 会重试，现统一为非重试的进度截止异常，并把同一限额传到 HTTP read 和网关观察器。图片描述保留独立 30 秒首进度上限。短暂连接故障仍遵循原有有限重试规则，没有更换提供商或无限放宽超时。

### 真实页面与音频结果

证据目录仍为 `.local-dev/memory-upgrade-20261021/`，输入不含期望爱好答案或内部工具名。原有失败现场保留。

| 用例 | 结果与局限 | 会话 / 证据 |
| --- | --- | --- |
| 原句安排居家一小时 | 推荐画画、水彩相关活动；后一次触发一次 memory_search 补读，深层包 partial，不判完整召回或稳定速度通过 | session_7YBTBFTV4DZVV9WY1VNQW2KVWX / lazy-original；session_7YBTBDX62T14R7M0AZPNHWTK61 / lazy-final-original |
| 问单镜头时长与资料名称 | 直接回答最多 5 秒、20 秒竖屏，资料标题含“最终确认版本”，无补查。未输出 .txt 后缀，不将它写成完整扩展名验收 | session_7YBTBESCEY9HCQK4D55072TFCE / lazy-document.json、.png |
| 看当前云桌面画面 | 真实 computer 调用成功，页面显示截图及对应内容；未点击、发布或删除桌面内容 | session_7YBTBE6VBFHCS0WS3CAKA57M3A / lazy-desktop.png |
| 已安装工作流程 | 首次咨询给一般建议，未调用目录。后续检查已安装流程触发两次 skill_search，目录读取正常；不能用此结果证明所有技能可执行 | session_7YBTBDM0EBWGKQR219N2SDZAFQ / lazy-skill-discovery.json、.png |
| 本次不参考以前记录 | restricted、items 0、无历史补查，正常回答当前文案；当前称呼来自账号设置 | session_7YBTBCD4YZ19NE7362J7QG1WND / lazy-restricted.json、.png |
| 个人助理自然放松建议 | 主动提到古筝、水彩花草；旧网关首进度截止后自动重试，外部等待 117.14 秒。此例直接促成最后的异常分类修复；不作为修复后性能成绩 | 主助理 session_7YBTG6JY0G8VD6VMQG4EY91Y8Z / lazy-assistant-before-gateway-fix.png、backend-memory-lazy-acceptance.log |
| 最终代码模拟麦克风 | 7.018 秒音轨经 WebAudio→真实 ASR，准确识别“今天不想出门，在家有一个小时空闲，你觉得我做点什么会比较开心？”；真实语音回答主动提到水彩画花草与古筝。记忆 complete、1292 tokens/core 413、无降级；route 2006 / recall 690 / finalize 57 ms，这不是纯内部耗时 | call 01M5MN6T4KQ3NZ3JC4W19ZXSP2 / lazy-voice-events.json、lazy-voice-memory.json、lazy-voice-playback-complete.png |
| 语音收尾 | 73 个音频帧含开场与回答，播放后 UI 回到“我在听”。转交文字助理回合 0、同期主助理新消息 0。已挂断 ended，页面刷新移除模拟麦克风和 WS 观察器 | lazy-final-settlement.json；最终后端 backend-memory-release-candidate.log |

语音截图中的长通话时长包含测试操作之间的静音等待，不代表单轮回答耗时。转写与回答取自服务端通话记录，没有向 ASR 直接注入文字。

### 速度证据与未达标项

`lazy-performance-samples.json` 保存不同运行的原始内部/外部并集统计。目录获取在纯普通会话中为 0.2–0.5 ms；技能发现和云桌面实际执行仍发生授权网络读取。各场景不做受控同比：

| 样本 | 内部 ms | 外部等待 ms |
| --- | ---: | ---: |
| 目录延后、流式节奏调整前的原句 | 5576.6 | 11209.2 |
| 调整后的资料问答 | 3200.8 | 10013.2 |
| 调整后的原句（一次补读） | 6248.1 | 16166.2 |
| 云桌面实际执行 | 7534.4 | 12219.4 |
| 已安装技能查询 | 3269.0 | 19258.7 |
| 不查历史 | 3814.5 | 5637.1 |
| 个人助理，旧网关重试尚未修复 | 7527.4 | 117144.9 |

内部仍有 SQL、持久化和多步补读成本，不能宣称已彻底解决所有速度问题。前后输出长度、模型调用次数和后台负载不同，不能将资料用例的 98 条流消费 SQL 与原句的 239 条简单当成同输入提速比例。当前上下文样本约 13922–18569 tokens，与此前 28015 的完整工具目录相比减小，但没有再证明同输入稳定减半。深层召回的 1 秒截止和相关性重排仍会出现 partial；这里只保证已完成候选不被无谓丢弃以及最终权限正确，不将 partial 改成 complete。

### 定向测试、数据库与回滚

- 主批：180 passed / 11 skipped，`lazy-final-targeted.log`。跳过的 PostgreSQL 参数使用独立数据库 `openbox_memory_targeted_20261023` 补测，11 passed，`lazy-postgres-final.log`。
- 候选复核/撤权 PostgreSQL 精确节点：4 passed，`checkpoint-postgres-final.log`（与上述有重叠）。
- 最终技能描述与读取契约：35 passed，`lazy-contract-final.log`。
- 最后统一网关后的 SDK 流、超时、处理器、重试、图片描述、计费相关节点：89 passed，`gateway-deadline-final.log`。
- 批次重叠，不相加为独立总数；开发中失败日志保留。未跑全量测试，`git diff --check` 通过。
- 本批不新增 schema；实际查询 head 仍为 `ph84a5b6c7d8`。7 个新普通会话提取作业均 SUCCEEDED，result_memory_ids 为空，未改写或删除原有事实。语音 ended；本轮没有线上部署、提交或推送。
- 回退目录按需行为可使用已有 `OPENBOX_TOOL_EXPOSURE_MODE=legacy_eager`；其余改动可回退本批代码，模型配置新增字段为可选，无须回退数据库。此前迁移/兼容回滚结论与未完成的发布门槛仍有效。

结论：本批确定缺陷已实现定向修复并真实复测；稳定延迟、全冻结矩阵、移动端最新现场和每租户沙箱隔离等原门槛没有由本批替代，R1/R2 全部上线仍不签署。


## 十七 2026-10-24：运行时、提取稳定性与发布复核

环境为本地 3002/8082、独立 QA 数据库 `openbox_memory_upgrade_20261021`，分支仍为 `codex/memory-system-upgrade`。读取和提取使用阿里官方 `qwen3.8-flash`；专业主模型保持 Gemini 经 UEE，原生语音保持 Qwen Realtime。没有更换主模型、放宽读取期限或权限，没有生产部署。下列原始证据均在 `.local-dev/memory-upgrade-20261021/`，账号、日志及导出不进入版本库。

### 已修复的源码问题

| 问题 | 改动及可复核证据 |
| --- | --- |
| 465 个会话的重连恢复帧触发数百次全列表查询，占用数据库和事件循环 | 恢复帧带 `recovery` 标志；网页及 Flutter 恢复状态但不逐条刷新全局查询。两次重连的会话列表请求从 947 次降为 20 次（含正常刷新，不是统一负载基准）。后端先启动收发泵再独立执行恢复，1101 会话不再堵住容量 1000 的发送队列。逐帧授权保留 |
| 后台标题、建议任务继承前台耗时上下文 | `question/runtime.py` 的独立辅助任务清除前台计时上下文，结束后恢复。增加并发时间回归；修正前数字不能与修正后作为严格同比 |
| 模型 SDK 读到 SSE `[DONE]` 就关闭，HTTP 正文尚未结束，连接池丢弃连接 | `agent/sse_transport.py` 只对已经完整结束的 SSE 消费剩余 HTTP 尾部，上限 100 ms/64 KiB；取消和未结束响应立即关闭。真实本地 TCP + SDK 的三次请求从三条连接变为一条，真实网关紧接的建议请求也复用连接。不是后台续写、补请求或自动重试 |
| 提取模型返回合法 JSON，却不是约定字段结构，3 个作业进入 DEAD | 已验证的官方 Qwen 路径使用严格 JSON Schema；其他通道不猜测支持能力、不静默重试。来源、引用和独立核验仍然执行。精确重放这 3 个本地作业后，原 60 条场景涉及的 64 个作业全部成功 |
| 核验将整个活动期内反复执行的规则误判为一次性任务 | 真实诊断复现 `one_off_instruction`。核验区分“作用于多个未来产物的规则”和“某个产物的要求”，有限期限不等于一次性任务；不按固定用户表达匹配。增加 `source-scope-v2`、意图类别计数和拒绝数量，不记录额外原文。原 17 例先为 16/17，通过修改及 8 条新表达后为 25/25；原失败保留 |

连接释放原则参见 [HTTPX 异步客户端说明](https://www.python-httpx.org/async/)；严格结构化输出能力参见 [阿里云 Qwen 结构化输出](https://help.aliyun.com/en/model-studio/qwen-structured-output)。官方能力说明不替代真实请求测试。

### 真实浏览器与语音

| 场景 | 实际结果 | 证据 |
| --- | --- | --- |
| 新会话“今天不想出门，在家待一小时，你觉得我做什么比较好？” | 主动建议水彩画花草；追问“那放点什么音乐呢？”主动推荐古筝。未把答案写进用户问题 | `release-sse-browser-hobby.png`、`release-sse-browser-memory.json`；会话 `session_7YBTAX6YWF9VZN7D3ANKJPYR8E` |
| 同会话切到文档问题，要求完整文件名及镜头时长 | 输出 `青禾鲜果店门店视频制作与到货宣传注意事项（最终确认版本）.txt`，最长 5 秒；文件后缀本次实际出现 | `release-sse-browser-document.png`；同上会话 |
| 模拟麦克风的普通居家建议 | WebAudio 音轨经过真实 ASR，回答主动使用水彩花草与古筝；记忆 complete、1485 tokens，准备 2277 ms（含外部等待）；转交文字助理 0，同期主助理新消息 0 | `release-voice-memory.json`、`release-sse-browser-voice.png`；call `01M5N385K2N8Y0NZ9053552BKJ` |
| 语音结束与清理 | 服务端 ended；浏览器挂断后刷新，移除一次性模拟麦克风。截图中的通话时间包括测试静音间隔，不等于回答延迟 | `release-voice-memory-final.log` |
| 项目“秋冬展览”告知：展期到十二月底，每周活动介绍均中英双语 | 真实后台保存 `PROJECT_CONTEXT` 到本项目，有年末期限，未改成账号全局语言设置；全新会话只说“纸艺体验，写一句简短介绍”，直接生成中英双语 | `release-project-rule-saved.png`、`release-project-rule-recalled.png/.json`；来源 `session_7YBTAVGMVTR177VGJM355P65HV`，新会话 `session_7YBTATZVEW6SGG29BZYHYCHSTZ` |

普通聊天的初始记忆包为 1629/1194/1671 tokens，均在 2048 上限内；第 1 轮仍有重排截止，第 3 轮仍有候选/目录预算裁剪，不写成全部深层候选完整返回。主模型完整输入约 1.55–1.72 万 tokens，不能用记忆包预算冒充完整上下文大小。

项目范围的补充验收：默认空间的新会话 `session_7YBTATHJ7BRHTSG9E9H30TT3P8` 正常输出中文，Debug source_refs 不含展览规则；项目内的 `session_7YBTATZVEW6SGG29BZYHYCHSTZ` 则实际包含该规则。见 `release-project-rule-isolation.png` 和 `release-final-settlement.json`。前一次只切换侧栏、未重新创建默认空间页面的会话仍属于展览项目，已排除出跨项目证据；没有为测试删除或移动该会话。最终所查 13 个提取作业均 SUCCEEDED，仅展览规则产生一个新项目事实，语音 ended，schema head 实际查询不变。

### 冻结矩阵与速度

- 本批首次冻结 60 条真实请求：读取政策 59/60，明确要求的初始证据 20/20，11 条限制读取无历史事实；32 complete、11 restricted、16 partial、1 unavailable。5 条主模型请求超时、1 条路由截止。证据 `release-holdout-60.json`、`release-holdout-60-assessed.json`；清单 SHA-256 `7ba7eb0712a591c0d7506ed05dbb6db6e727b988d0099085f7dab7091407467c`。
- 5 条主请求超时达到 90 秒首进度截止，连接/TLS 与上游响应头均有 trace，上游响应头等待约 80–88 秒。SSE 修复后只重测这 5 条：5/5 完成、政策 5/5，2 complete、1 restricted、2 partial；墙钟 11.6–16.4 秒。见 `release-timeout-cases-after-sse.json` 和 `-assessed.json`。**不将前后批次拼成 60/60，不据此声称上游已稳定。**
- 修正计时后，同一普通会话前三轮整轮内部为 1570.8/1166.6/1088.3 ms，外部并集为 9424.9/8447.4/7916.4 ms。新项目规则的跨会话回答内部 1051.6 ms，外部 18679.7 ms；主模型请求 16489.2 ms，其中 TLS 2782.6、上游响应头 13443.7 ms。页面显示的 17 秒属于另一统计边界，不能混算。
- 上述项目回答在可提交主模型前，setup/history/context/request_prepare 等内部合计约 664 ms；整轮还包含流消费、持久化和收尾。**不能拿 1.05 秒整轮数值直接对照 PERF01 的 500 ms 请求准备门槛，也不能把数据库和重试退避移到外部等待。**
- 最后的默认空间隔离验收首次请求收到 UEE `503 auth_unavailable: no auth available`，一次有限重试后完成。整轮内部 3431.1 ms，其中 `provider.retry_backoff` 为 2233.8 ms；外部等待 8294.2 ms。页面仅显示成功步骤 4.6 秒，不能用它隐去首次失败和退避。此错误说明网关当时没有可用上游认证，不仅仅是应用 SQL 或模型生成慢；服务端错误还附有上游请求失败信息，具体池内原因需网关侧证据。
- 先前替身提供商配对基准数值仍留存，但本批没有重新做同配置 30 暖/5 冷配对，不能声称当前真实环境 p95 已稳定通过。未缓存表达仍必经一次额外语义判断，PERF01 的该条款未完成。

### Android 与数据库演练

- Android 模拟器运行实际应用与真实 HTTP/WS：设置保存、另一客户端更改称呼后的实时更新、知识库文件可见、聊天及历史恢复通过。`release-mobile-retained.log` 和 `release-mobile-cold-after-boot.log`；截图 `release-mobile-settings-saved.png`、`release-mobile-live-update.png`、`release-mobile-knowledge-file.png`、`release-mobile-chat-before-restart.png`、`release-mobile-cold-restore.png`。
- 首次冷启动脚本失败不能当产品丢登录：Flutter test 默认卸载应用；两阶段改用 `--no-uninstall`。保留安装后两次启动又受模拟器残留权限 Activity 影响，均为 No tests ran，未记作通过。重启模拟器、不擦数据后，实际冷启动恢复测试通过。原日志保留。此项是 Android 模拟器，不冒充实体手机或最新 APK 发布验收。
- 在新建隔离库 `openbox_memory_migration_rehearsal_20261024` 从旧 schema 升到 `ph84a5b6c7d8`，重复升级、空表降级再升级和数据摘要比对通过。产生新回执后，破坏性降级被拒绝且摘要不变。见 `release-migration.json/.log`。
- 兼容应用使用功能关闭/开启两次真实 HTTP 演练：新设置保留、幂等回执不重复写入、过时 revision 返回 409、原记忆可读，重新开启后数据保留。见 `release-rollback-http-disabled.json`、`release-rollback-http-enabled.json`。这验证当前兼容构建的功能回滚，**不是旧 main 二进制回滚**。
- 本批源码修复不新增 schema。API 与后台消费者须部署同版本；没有删除或修改生产账户数据。

### 本批定向检查

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| WS 恢复与连接权限 | 32 passed | `release-ws-backend.log` |
| 网页恢复事件 4 文件 | 26 passed；`tsc -b` 通过 | `release-ws-web.log`、`release-ws-web-types.log` |
| Flutter 恢复定向测试与受影响文件分析 | 2 passed；分析无问题 | `release-ws-mobile.log`、`release-ws-mobile-analyze-final.log` |
| 并发计时与问题运行时 | 54 passed | `release-timing-tests.log` |
| 实际 TCP/SDK 连接复用、网关及进度期限 | 27 passed | `release-sse-tests-verified.log` |
| 严格提取契约、准入及管道 | 55 passed / 2 skipped（PostgreSQL 特有节点） | `release-structured-extraction-tests.log` |
| 有期限规则、独立核验、记忆修订与结构契约 | 41 passed | `release-grounding-scope-tests.log` |
| 实际 Android 应用与保留数据冷启动 | 两阶段各 1 passed | `release-mobile-retained.log`、`release-mobile-cold-after-boot.log` |

批次有交叉，不相加成独立总数；没有全量仓库测试。首次失败日志保留。真实模型提取 25 条只验证本批准入类别，不替代全产品语义准确率。

### 发布结论与剩余门槛

本批已修复的问题有程序回归和真实调用证据，但 **R1/R2 全部上线仍不通过**。未关闭项是：未缓存读取判断的额外往返与当前环境性能门槛；60 条原矩阵的完整修复后复测及深层召回稳定性；A02/A13/A17 所要求的独立重复、时序与产物保护证据；最新移动语音页面提示链路；E04 的真实每租户云桌面及私有经验执行、会员恢复验收。本地 shared 云桌面不能替代每租户隔离。保留全部原门槛，不因本轮成功子集自动勾选，也不承诺尚未验证的上线日期。

## 十八 2026-10-24：范围隔离、设置保存及网关修复

### 源码与定向证据

- 调试阶段与最终状态在一个锁定事务中提交，避免同一轮多次提交和部分诊断落库；Wiki 原始来源保留真实种类及文档身份，展示指令不把旧任务约束伪装成文件规定；预算裁剪优先保留最高相关原文，避免图片目录挤掉必要文件证据。`release-atomic-postgres.log`、`release-provenance-regression.json`、`release-budget-regression.json` 保留独立验证和初次失败。
- `documents_only` 是独立读取范围，使用真实 SQL 权限、当前状态和来源版本检查，时间范围按文档上传时间计算。受限路径不加载个人核心事实、历史、任务或 Wiki 派生；来源撤回后不可重放旧结果。SQLite 7 项和新 PostgreSQL 库 7 项通过。最宽相关批次 `release-document-scope-final-tests.log` 为 165 passed / 6 deselected；最终读取协议批次 `release-scope-v17-final-tests.log` 为 100 passed。重叠测试不累加。
- 读取 v17 使用官方 Qwen 支持的固定位置 JSON Schema，未改模型、提供商、6 秒判断截止或权限门槛。范围表达的 12 条冻结集 SHA256 为 `49990ae898607874c5821f95aa64b72da36e8aac69fd4b02288077b4b6535c32`；v15 为 10/12、v16 为 11/12，最终 v17 为 12/12 且均一次提供商调用，见 `release-document-scope-semantic-v17.json`。引用翻译、撤回限制、动作限制和历史请求均包含在内，未修改预期答案。单文件排除暂不能表示时返回 uncertain，不扩大到全类文档。
- 语音 v17 的调用 `01M5NG7JJFR3N24X5PDC48YY9D` 实际识别“我们以后用英语聊吧，你说的短一些就好。”，读取一次约 1478 ms、记忆准备 1745 ms，零个人助理转交，但后台仅保存简短回复，语言被独立核验拒绝。原提案未保留，重放不能唯一证明当时拒绝的具体语义；不能断言某一句摘要文本就是唯一原因。源码已改为只核验设置最终写入的字段/值，不拿未持久化的摘要作为设置证据。ordinary memory 仍核验整个命题、时效和范围，不能把临时设置保存成永久默认。
- grounding v3 定向 62 passed，12 条真实提取+核验正反例通过（manifest SHA256 `8d6b4457fea15599c82eeb9a7f532325f10f41faa946882311be588f29c8ceab`）。随后真实语音 `01M5NHAA7420Y7GJMPX1XP4G2M` 又产生 `invalid_experience` 死信，保留在 `release-voice-language-grounding-v3.json`。不是网络错误，也没有误报保存成功。
- extraction v14 将设置与经验隔离：canonical setting 删除不属于该操作的经验附属提案；不会据设置核验启用/创建经验。空经验允许 null，真正的经验字符串/步骤约束与存储校验对齐。普通经验错误仍拒绝；真实值和来源核验不放松。相关批次 44 passed，新的 12 条真实模型用例通过，见 `release-setting-experience-v14-tests.log`、`release-setting-grounding-v14-real.json`。只经既有有权限、幂等重放服务恢复上述 1 个本地失败任务，结果 SUCCEEDED，language=English，回执 revision=57；不是直接改设置表。证据 `release-setting-experience-replay.json`、`release-voice-language-extract-v14-replay.json`。随后新增现场和新会话验证均通过，见下一节。

### 实际浏览器、语音与性能

- 文档限定自然问题“这次就看看我上传的文档，不用以前的聊天和个人记忆。门店那份视频说明叫什么名字，单个镜头最长多久？”在新会话返回完整文件名和 5 秒单镜头上限，UI 约 10 秒/19303 输入 tokens/1 次工具调用。证据 `release-documents-only-browser.png`，会话 `session_7YBTAGJ7K59E8HQFZMFDD2BG6J`。这次采用 v16 路由、相同文档范围适配器，不冒充 v17 完整矩阵。
- 手机/自然光来源问答及文档预算已用真实页面验证，分别见 `release-provenance-browser.png` 和 `release-evidence-budget-browser.png`；原先预算导致必要证据缺失的失败保留。
- extraction v14 后全新真实语音 `01M5NJ5FNBE9EF210KG913JNQZ` 同样说“我们以后用英语聊吧，你说的短一些就好”，实际 ASR 后正常保存 English + brief（revision=59），任务 SUCCEEDED；没有重放、直接改表或个人助理转交。新普通会话 `session_7YBTADPK3E4HFACHKX3KYPF6HT` 使用中文问室内轻松活动，助手主动用英语回答并结合水彩兴趣。证据 `release-voice-language-v14-fresh.json`、`release-setting-v14-fresh-new-chat.png`，UI 约 9.7 秒 / 16968 输入 tokens。测试后正常设置 API 恢复 language 为空（revision=60）；前一失败及有限修复证据保留。
- 活跃语音连续 5 次正常设置 API 更新到真实提供商 ACK 为 144.1/110.2/103.9/88.7/207.7 ms，恢复简短设置为 71.8 ms。见 `release-voice-idle-update-performance.json`。这是实际通话加正常 API 更新，不是 5 次 UI 点击，也不替代 A13 的新增/纠错/删除和响应竞态。
- `release-perf-atomic-20261024.json` 使用相同 SQL 快照的两个隔离 PostgreSQL 克隆、Redis 10/11、即时外部提供商替身，基线 `a95a302` 与当前代码交替执行。每路径每版本 30 暖请求，加每版本 5 冷启动，共 190 样本。入口到主模型请求准备完成的 p95（毫秒）：问候 507.73→409.93、偏好 603.13→423.59、知识 503.99→419.55；新核心上下文 p95 137.3/100.6/104.7。新冷启动 407.66/419.61/410.38/374.44/497.55。数值子门槛通过，不相加阶段 p95，也不把全部差异归因于单个改动。仍每次未缓存输入调用语义读取器，PERF01 整体尚未通过。
- 最新完整冻结 60 条已在读取 v17 / grounding v3 / extraction v14 重跑，清单 SHA256 不变：主请求无错误 60/60、读取政策 60/60、11 个受限场景不注入历史、必要初始证据 20/20；42 complete / 11 restricted / 7 partial，最大记忆包 2045 tokens。证据 `release-final-scope-60.json`、`release-final-scope-60-assessed.json`。7 个 partial 分别包括重排截止、候选/目录裁剪和上下文变化重建，不伪称全部完整。机械检查通过不等同于 60 条答案语义全部合格；人工复核发现历史一次性文案改写仍会作为过往要求返回，需要进一步核对旧数据的准入范围。最终 60 条及两次浏览器新会话共 62 个会话的 72 个提取作业全部 SUCCEEDED，见 `release-final-scope-settlement.json`。

### UEE 运行配置修复及可回滚性

故障在通道 19 的东京 CPA 代理，OAuth 刷新遭遇 EOF，导致 `auth_unavailable`。先保持该通道隔离，只将其全局 proxy-url 改为空走服务器直连，精确备份 `/opt/cpa/config.yaml.before-memory-proxy-repair-20261024`；热加载后仍有缓存无凭据状态，重启被隔离的 19 后实际认证 Gemini 成功。连续 3 个新加坡→19 的流式请求均正常 stop/DONE，再用官方状态 API 恢复通道 19；通道 20 配置不变，未重启 New API。证据 `release-cpa19-proxy-repair.json`、`release-cpa19-restart-probe.json`、`release-uee-channel19-restored.json`。

变更前配置 SHA256 `2116e1a0d1a305f463de703313aaa2f1c234fc611c3a494901db21052026a3c8`；后 `4cb55976b16a1c7a405b446efe88fc6045f9f6ac85e794258666362ffa939664`。回滚先以官方 API 将 19 设为隔离 status=2，保持 20；确认后摘要仍匹配才恢复指定备份并重启 19，不能覆盖第三方配置。恢复约 45 分钟后的只读核验显示 19/20 各 27 次成功、零原鉴权故障，19 OAuth 已刷新出新的有效期限，见 `release-postrepair-inspect_uee_after_restore.json`、`release-postrepair-inspect_cpa19_models.json`。这不是长周期可靠性承诺。

另外，本机一次请求在系统代理 TLS 建连耗时 10 秒并重试。QA 子进程的 NO_PROXY 加入 UEE 域名以验证直连，系统 VPN 未修改；这是本机路由调整，不能与服务器代理故障混为一谈。Qwen 仍走百炼官方；JEV 未启用。

### 当前发布边界

本批没有新 schema，沿用 `ph84a5b6c7d8` 和原独立迁移/兼容构建回滚证据；不声称旧 main 二进制回滚已测。业务源码未提交、推送、部署，但确实已修改线上 UEE 运维配置，不能笼统说“没有线上变更”。全部测试为相关定向集合，没有全库测试。冻结矩阵的全部语义判定、A02/A13/A17 完整独立现场、移动端最新语音视图、E04 每租户私有经验/会员恢复及避免每轮额外模型调用仍待完成，当前不签署 R1/R2 全部上线。

## 十九 2026-10-24 iOS 模拟器实际验收

环境为 iPhone 17 Pro、iOS 26.5（23F77），设备名 OpenBox Assistant QA；保留既有安装及登录，使用本地 8082 后端/3002 网页和独立 QA 账号、数据库。所有 Flutter 集成测试指定单一文件并使用 `--no-uninstall --no-pub`，没有运行全量测试。证据位于 `.local-dev/memory-upgrade-20261021/`，凭据不进入文档。

### 发现及修复

1. **系统启动失败后空白语音球。** 实际正常 CallKit 路径报音频激活 `NSOSStatusErrorDomain:-50`，随后系统结束事件将请求中的页面变为 idle。Swift 区分启动失败和通话结束，provider reset 后重建；Dart 将外部启动失败/取消保留为 ended，并使晚到的权限、音频或 socket 回调失效。修复后的模拟器显示“通话中断了”“重新拨打”“关闭”，而非空白页。原 OS 日志 `release-ios-callkit-system.log`；失败 UI 和最终证据 `release-ios-native-failure-native-start-failure-visible.png`、`release-ios-native-failure-evidence.json`、`release-ios-native-failure-fixed-v2.log`。
2. **当前聊天有 ID、无正文。** 服务端虽收到了页面切换，但只注入标题和待办 ID；历史读取还把记忆表 PERSONAL 枚举用于会话的 workspace/private 字段，过滤掉合法聊天。历史目录和读取现使用正确会话条件，仍验证账号、工作区、项目、删除状态和遗忘来源。语义上需要聊天原文时，当前视图最多读取 4 条消息、1800 字符，排除内部分析和工具结果，保留完整条目与来源版本，继续受 2048 token 总预算约束。
3. **回顾聊天被误当执行进度。** v17 对真实自然发言给出 history=0、task=read。v18 补足普通聊天正文的需求定义，向规划器提供当前有无聊天视图的布尔信息并加入缓存身份；不提前发送正文，不引入关键词白名单或新的模型请求。退出聊天页明确标记当前对象不可用，不能凭标题或个人爱好复述。原错误语音保留在 `release-ios-voice-before-view-fix.json`、`release-ios-voice-before-route-fix.json`。

### 实际结果及范围

| 场景 | 实际结果 | 证据 |
| --- | --- | --- |
| 移动设置保存及跨端同步 | 设置 UI 保存称呼后正常 API 核对；另一客户端正常更新，移动 UI 由真实事件刷新；最后恢复原称呼 | `release-ios-memory-live.log`、`release-ios-settings-saved.png`、`release-ios-live-update.png` |
| 知识库与聊天恢复 | 显示青禾资料；保留安装重新启动，登录和上次聊天位置仍在 | `release-ios-knowledge-file.png`、`release-ios-memory-resume.log`、`release-ios-cold-restore.png` |
| 当前聊天回顾 | 真实 ASR 识别“我现在打开的这段聊天，刚才说的内容再简单讲一下。”，回复正确复述居家水彩、花叶、茶及轻音乐；无旧任务确认 | call `01M5NSAQHWGD8M9TZJ0S4M2H5C` |
| 切到另一聊天 | 同一句自然音频，回复完整《青禾鲜果店门店视频制作与到货宣传注意事项（最终确认版本）.txt》，单镜头不超过 5 秒、总 20 秒竖屏 | call `01M5NSPDQXMQY936ZTRG284JNK`；`release-ios-v18-switched-chat.png` |
| 离开聊天进入知识页面 | 服务端 view=None，回复说明没有当前聊天正文并询问所指内容，没有把上个聊天内容当作当前已知事实 | call `01M5NSR37GRAX01D9PG1YYD713`；`release-ios-v18-left-chat.png` |
| 实际前后台切换 | CUA 按 Home，09:47:29 观察 paused；09:51:31 点击 BossIP 返回，原 call ID 及 connected 保持，页面恢复；有连续 20 秒后台采样及返回检查 | `release-ios-v18-evidence.json`、`release-ios-v18-background-return.png` |

最终单文件集成测试 `release-ios-memory-voice-v18-acceptance.log` 为 1 passed（包含三段通话）；实际转写、回复和 ended 状态在 `release-ios-voice-v18-observed.json`。三次转交个人助理均为 0，实际输入/输出 PCM 均非零。记忆准备分别 2595/2621/2428 ms，**包含外部判断与检索等待，不是纯内部耗时**；记忆包分别 2036/1874/2048 tokens，均 partial，不能因答案正确把裁剪/检索降级记为 complete。

**音频限制必须保留：** 本机没有可用物理麦克风，完整音频流程通过既有文件 PCM 输入和显式 `VOICE_SIMULATOR_AUDIO_ONLY` 调试模式，真实 HTTP/WS、ASR、语音模型和原生音频输出均参与。这个模式跳过 CallKit，证据明确记录 `callkit_exercised=false`；前后台保持的是该调试音频连接，不证明真机后台麦克风或 CallKit 正常通话。正常 CallKit 的实际失败提示已另测。调试开关由 `kDebugMode` 限定，release 不启用；最终删除本次 QA 音频输入并重新运行无这两个开关的普通调试版，恢复到原聊天页面。

前两轮操作窗口未及时完成 Home/返回，测试失败；不是产品通过，也不是产品主动断线证据。后改为 10 分钟外部操作者等待，并在 runAsync 后再次断言真实返回，防止记录后续成功截图掩盖失败。最终实际完成并通过，原失败日志全部保留；产品网络和响应限额没有延长。

### 定向回归与未关闭项

- 后端 6 个直接相关文件：`test_memory_configured_read.py`、`test_memory_read_protocol.py`、`test_voice_view_context.py`、`test_voice_memory_context.py`、`test_voice_memory_refresh.py`、`test_memory_document_scope.py`，**143 passed**，日志 `release-ios-view-route-tests.log`。覆盖视图快照、权限/删除/遗忘过滤、缓存身份、限额和受限读取。
- Flutter 的 `voice_call_controller_test.dart`、`chat_location_test.dart`，**35 passed**；相关语音目录和两个集成文件定向 analyze 无问题。日志 `release-ios-voice-targeted-v2.log`、`release-ios-final-analyze.log`。iOS 原生编译以及实际错误页面复测已执行。
- v18 的既有文档范围冻结集 **12/12**、均一次提供商调用，见 `release-document-scope-semantic-v18.json`。新增 12 条读取需求样本，6 条聊天正文需要均识别；严格整体 **11/12**，原句仍额外给 task=read。正文读取缺失已修复，但不必要任务状态查询仍需优化；失败原始结果在 `release-ios-view-routes-v18.json`，不扩大成全部语义质量通过。
- 历史权限修正后、读取仍为 v17 时，原冻结 60 条完整重跑：60 个主请求无错误、读取政策 60/60、11 个受限场景无历史注入、必要初始证据 20/20；33 complete / 11 restricted / 16 partial，最大 2045 tokens。记录 `release-ios-scope-60.json`、`release-ios-scope-60-assessed.json`。不能借此宣称最终 v18 全矩阵已跑；人工检查仍保留既有一次性文案被当作过往要求的范围问题。对应 60 条和两次前序浏览器会话的 72 个提取作业为 SUCCEEDED，见 `release-ios-scope-settlement.json`；作业成功不证明每条提案语义正确。
- 本批没有新增 schema，数据库实际 head 仍为 `ph84a5b6c7d8`。沿用第十八节迁移/兼容回退边界，未操作生产数据库、未部署业务代码。早前线上 UEE 运维配置修复不由本节否认。

**结论：本次 iOS 的设置、知识、位置恢复、错误提示和模拟音频视图协同已验收；不能宣称所有 R1/R2 上线门槛完成。** 真机 CallKit/麦克风、避免每轮额外语义调用、A02/A13/A17 的完整独立现场以及 E04 每租户环境等既有待验项继续保留。

## 二十 2026-10-24 模拟麦克风与跨端记忆增量验收

继续使用上述隔离 QA 账号、数据库和 iOS 模拟器。新增 `mobile/integration_test/memory_voice_microphone_test.dart`，唯一替换的应用依赖是麦克风 PCM 输入，按 16 kHz 单声道 PCM16、100 ms 分块输入普通中文问句；实际 API、WebSocket、ASR、语音模型、状态机和原生音频输出参与。连接之间正常挂断和重新点击通话入口；通过 `VOICE_SIMULATOR_AUDIO_ONLY` 跳过 CallKit，因此不扩大为真机麦克风或后台录音通过。集成测试只验证音频输入、输出和连接；另以实际转写、SQL 状态、记忆版本和网页结果判定语义。

### 发现和修复

| 实际问题 | 根因与修复 |
| --- | --- |
| 网页已保存英语，新普通会话用英语，语音仍用中文 | 通用“使用用户语言”和旧通话内容压过当前设置。`voice/prompt.py` 明确区分全局语言与本通话覆盖；`voice/bridge.py` 在记忆证据之后追加当前权威语言、简洁和称呼规则。空闲刷新用本次新包构建指令，仍等真实提供商 ACK 后才推进已应用版本，没有增加模型请求 |
| 问文字文件名却选择最近的 JPG | 原目录只发现 FileAsset 媒体，遗漏 MemoryDocument 文字上传；单改澄清提示后仍复现。`memory/readers.py` 按语义素材或知识需要读取有权访问的文字文档候选，最多两个文档与两个媒体，保留原四条目录额度；`memory/packet.py` 在总预算内保留文档候选及已检索的原文证据，不按用户固定表达匹配 |
| 已删除文档可能仍出现在文件目录 | 新 SQL 定向用例抓到普通 documents 读取没有排除 DELETED。目录与普通文档清单均补删除过滤；所有者、工作区、项目权限和最终重新核验仍保留，未改变数据库 schema |
| 测试启动配置导致整个记忆读取关闭 | `MEMORY_ROUTE_JEV` 是保留旧名的通用语义读取开关，并非只控制 JEV；本轮一次错误关闭使 Qwen 读取也 unavailable。已恢复标准 QA 启动配置并添加配置说明；实际读取模型仍是官方百炼 `qwen3.8-flash`。该失败批次单独保留，不计为修复后通过 |

语言控制使用会话指令，未将 TTS 音频语言误当回复语言设置。官方接口说明参见 [阿里云 Fun-AudioChat 客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)，真实通过依据仍是本轮录音转写及输出。

### 实际场景结果

| 场景 | 实际观察及证据 |
| --- | --- |
| 网页新增、纠正、遗忘，原语音保持连接 | 普通会话先保存周末陶艺课，再改木雕课；同一通电话分别答出最新课程。网页正常点击“忘记”确认后，再问不再说出课程内容。对应记忆 revision=4、DEPRECATED 且 deleted_at 非空，后台没有重新激活；会话 `session_7YBTA2JAM785V1PEXFSJJ4P95Z`，call `01M5NX8GC5W01HV23C0D3CHN3K`，`release-ios-live-web-forgotten.png`。本项仅完成一个实际周期，不代替 A13 的三次独立重复和受控竞态 |
| 语音保存习惯，新普通会话主动使用 | 语音自然说平时咖啡不加糖并希望之后推荐时参考；保存为 ACTIVE 事实、revision=2。全新网页会话仅问“现在想喝点东西，给我推荐一杯吧”，主动推荐无糖美式或冷萃；该通话没有文字助理转交。证据 `release-ios-live-web-drink-recall.png`、`release-ios-live-final-audit.json`，网页会话 `session_7YBT9Z48Q7A2NJ65Q83EE5JAKC` |
| 英语设置与兴趣主动召回 | 个人助理正常保存“以后和我聊天都用英语，回答短一点就行”。全新网页会话自然问周末活动，用英语建议花草水彩；最终三通新电话也用英语问候并推荐水彩，前两通同时引用古筝爱好，没有要求用户提示记忆或工具名称 |
| 本通话中文覆盖 | 第一通说“这通电话咱们用中文聊”，随后饮品问题继续中文；第二通重新连接恢复英语。临时中文未覆盖全局 English。切换语言的一句仍重复了一次“林姐”，因此不声称“称呼只在开场出现”的口播细节完全通过 |
| 完整文件名和 ASR 相似音 | 原自然音频被 ASR 写成“亲和活动文件”。修复目录后，同一音频直接答出 `青禾鲜果店门店视频制作与到货宣传注意事项（最终确认版本）.txt`；随后自然补充“写着镜头时长的那份文字说明”也给出完整文件名与后缀。此前误选 JPG 和仅改提示仍失败的记录均保留；本次正确结果不能证明所有歧义都能自动解决 |
| 遗忘后的新通话 | 第三通询问周末课程，回答遵循先前不记录、不提及的要求，没有复述被遗忘的课程。SQL 检查课程没有 ACTIVE 记录 |
| 临时状态与仅当前消息 | 语音说今天累且不要记为长期习惯；网页新会话只允许当前消息并临时想喝甜奶茶。网页读政策为 current_only、记忆 items 为空，正常推荐甜奶茶，没有强套无糖咖啡。后台没有新增奶茶或疲惫的长期事实，原咖啡习惯保留 |

最终批次 `release-ios-live-final-v21.log` 为 **1 passed**，包含 **3 通电话、10 段音频**；v21 是本地验收批次名，读取契约仍为 `configured-read-grounded-v18`。三通 call 为 `01M5P3G94MRM7FD43MY8X0H120`、`01M5P3HCVGWD9KP133FXJHR74B`、`01M5P3J5M06SHSRZKMD4ACJA6W`，均 ended、转交个人助理为 0。十次均有非零麦克风/播放信号并正常回到 listening。完整转写、逐项人工结论和计时在 `release-ios-live-final-assessment.json`；原始各批次在 `release-ios-live-observed.json`。

最终十次记忆准备为 **393–3485 ms，包含外部模型与检索等待**，不能写成纯内部耗时；包体 **966–1682 tokens**，未扩大 512 core/2048 total 上限；4 次 complete、6 次 partial，裁剪或检索降级状态不因回答正确而抹掉。音频从开始输入到播报结束的记录还包括用户说话、模型、网络、播放和两秒稳定观察，不用于响应延迟指标。

网页饮品现场虽正确使用语音保存的习惯，但出现过一次重试：`backend-release-ios-response-policy-v19-enabled.log` 明确记录主模型请求在 TLS 握手阶段等待 10003 ms 后超时，尚无响应状态或首进展，随后正常传输重试成功。该轮总计 25930.5 ms，已观测外部等待 21956.9 ms，内部墙钟 3973.6 ms，其中重试退避 2297.8 ms；退避没有被移出内部口径。这个现场证明记忆应用正确，**不证明网关稳定性或整体响应速度通过**；不是已修复的模型首进展截止再次自动重试。页面保留“第 1 次重试”提示和完整原始失败证据。

### 定向测试、恢复和仍需补齐的门槛

- 当前语言、刷新、原生记忆回执和开场相关四文件：**69 passed**，`release-ios-final-policy-targeted.log`。文件目录、读取协议、语音刷新三文件：**40 passed**，`release-ios-file-discovery-rerun.log`。批次有重叠，不相加为独立总数。
- 首轮 128 passed / 1 failed 是旧测试只返回 settings，缺少现在必要的记忆包字段；已补真实最小包契约，相关文件 37 passed。另一个新增 SQL 用例先捕获已删除文档泄漏，修复后上述 40 项通过。最初集成测试的旧指令重复执行、SemanticsHandle 清理问题也已修正，失败日志保留，不冒充产品通过。
- 新集成文件定向 analyze 无问题，iOS 原生构建和最终音频集成通过；`git diff --check` 通过。未运行全量测试。
- 结束后通过正常 profile API 将 QA 语言从 English 恢复为空，长度恢复原 brief，revision 65→66；音频输入目录移入 Git 忽略的本地证据目录，停止后台观察器，重新运行不含模拟麦克风/跳过 CallKit 开关的普通调试版。CUA 确认登录和个人助理历史页面正常，见 `release-ios-live-normal-restored.png`。没有清空模拟器、重置账号或删除普通聊天历史。
- 本批没有新 migration；实际 schema head 仍是 `ph84a5b6c7d8`，沿用既有独立迁移和保留数据的兼容回退演练，不扩大为旧 main 二进制回滚已测。

**发布结论仍为未全部达标。** 本轮所列模拟麦克风与跨端功能已有实际通过证据；仍需关闭真机 CallKit/物理麦克风、最终源码完整冻结矩阵、A02/A13/A17 的完整独立现场及受控时序、E04 的真实每租户私有经验/会员恢复、PERF01 未缓存输入额外语义往返及本轮 TLS 偶发超时的稳定性验证。一次重复称呼和已有不必要任务状态查询也保留为未关闭细节。没有部署本轮业务代码或修改生产账号数据。

## 二十一 2026-10-24：发送前重试、动作回执与跨端复测

本节为上一节之后的增量。沿用 `codex/memory-system-upgrade` 工作区及隔离 QA 账号，没有提交、推送或发布业务代码，没有新增迁移。本次对线上 UEE 仅进行只读运行元数据核查；第十八节此前已做的运维变更仍按原记录保留，不能将整个历史描述为零线上变更。以下证据位于 Git 忽略的 `.local-dev/memory-upgrade-20261021/`。

### 已实施修正及未启用的实验

1. **发送前连接失败的退避。** HTTP 观测现在记录实际模型请求是否开始发送，只有真实 TCP/TLS 失败且尚无上游请求发送、HTTP 状态或模型进展时，第一次可重试错误才使用 100–250 ms 随机退避。限流 `Retry-After`、未知发送状态、已经发送及部分输出失败继续原规则，重试次数不增加，模型进展截止仍不可自动重试。通过真实 SDK 对回环 TLS 停滞的故障注入及浏览器现场检查，没有依据错误文字猜测发送状态。
2. **动作限制与执行结果分开。** 冻结用例 `negation_and_scope-09` 的原话“先暂停制作吧，我只是来问我原先的宣传用词要求。”被正确识别为动作限制，但之前没有把这一语义传给主回复，助手无暂停回执却回答“制作已暂停”。现在将已核实的动作限制及原话放入当前上下文，明确读取背景不代表执行授权、用户要求不代表操作成功。没有新增词表判断或外部调用。最终浏览器复测正常回答文案要求，没有虚构暂停结果。
3. **工具隔离及关闭开关。** 隔离会话补齐 `memory_prepare`、`memory_read_more` 的能力过滤。两个读取入口先检查 retrieval_v2 开关，关闭时不会从已有上下文绕过；个人助理的 `memory.prepare` 只对普通助理模式开放，报告及协调入口不可借此读取。
4. **通话称呼状态。** 以实际开场结束状态更新语音提供商规则，正在通话时直接回答，不在每句重叫称呼。最终批次的实质回答已改善，但临时切换中文的确认中仍出现一次“林姐”，因此严格“不重复称呼”仍未通过，不用提示词存在替代实际效果。
5. **按需语义读取实验保持关闭。** 实现了仅文字入口的 `lazy_read_planning`：首次只带当前设置；主模型需要个性化背景时调用空参数准备工具，服务端以原始用户输入和原始身份执行既有语义与权限核验，同一回合只准备一次。语音路径不变。真实问候能省去读取判断，但个性化问题多了一次主模型往返，并重复发送约 15k–16k 主模型上下文；咖啡问题还出现下一轮模型截止。不能据此宣布更快。默认与最终服务均为 `MEMORY_LAZY_READ_PLANNING=false`，示例配置也标明实验未通过速度验收。PERF01 仍开放。

### 相关定向测试

| 批次 | 范围 | 结果与证据 |
| --- | --- | --- |
| 按需读取及工具 | `test_memory_lazy_read.py`、`test_memory_read_protocol.py`、`test_assistant_runtime.py`、`test_memory_tools.py`、`test_tool_runtime.py` | **61 passed**；`release-lazy-read-targeted-v4.log` |
| 传输及重试 | `test_gateway.py`、`test_agent_loop_retry.py`、`test_sse_transport.py`、`test_stream_deadline.py` | **44 passed**；`release-presend-retry-targeted.log` |
| 最终动作/语音规则 | `test_memory_read_protocol.py`、`test_memory_lazy_read.py`、`test_voice_memory_context.py`、`test_voice_memory_refresh.py` | **51 passed**；`release-action-speech-policy-v24.log` |

批次之间有重叠，不能相加作为独立测试总数。旧工具夹具缺少实际解析结果字段、工具集扩大后的断言失败均已修正，初始失败日志保留。只运行这些相关集合和下述记忆专项集，没有运行全量测试。

### 冻结集与内置浏览器

- 默认非 lazy 路径的 v23 冻结集沿用 60 条原清单，SHA256 `7ba7eb0712a591c0d7506ed05dbb6db6e727b988d0099085f7dab7091407467c`。**读取政策 60/60、必要初始证据 20/20、11 个受限场景未注入历史**；12 complete、11 restricted、37 partial，最大记忆包 **2001 tokens**，未扩大 2048 上限。3 条主模型请求失败（`personal-01/04/07`），不能把有召回包记成用户已收到答案。见 `release-final-default-v23.json`、`release-final-default-v23-assessed.json`。
- 人工检查已完成答案，发现上述虚报暂停并修复为 v24。随后 4 条受影响用例中 3 条完成，原 `-09` 在 API 批次因主模型截止失败；再用内置浏览器按同一句普通用户表述复测，返回原宣传用词限制且没有声称已暂停。两组结果分开保留，不能合并成同一批 4/4。见 `release-action-policy-v24.json`、`release-action-policy-browser-v24.png`；浏览器会话 `session_7YBT9CEXKQD9MHN6YNFAF4JAM1`。60 条结果来自 v23，v24 只重跑受影响部分，不能标为最终版本 60 条全部无错。
- 网页设置页保存交谈语言 English 后，普通新会话以中文问“今天只想待在家里，帮我想个能放松的小活动吧。”，实际用英语回答并主动结合水彩和音乐兴趣。见 `release-v24-English-settings.png`、`release-cross-client-English-v24.png`；会话 `session_7YBT9B8WAYARK6CAADWBC8HT3E`。页面明确出现第一次连接重试，功能结果通过不代表传输稳定性通过。
- lazy 实验保留原失败现场 `release-lazy-read-provider-timeout.png`，不改变问题措辞反复筛成功。该路径仍关闭。

### iOS 模拟麦克风及网页同步

模拟器为 OpenBox Assistant QA / iOS 26.5；只替换麦克风 PCM 输入，实际经过 WebSocket、ASR、记忆读取、语音提供商生成及原生播放。测试使用模拟器 audio-only 开关，**未覆盖物理麦克风和 CallKit**。

v23 前置批次完成 3 通电话、6 段音频（饮品、完整文档名、周末、遗忘后的课程等），见 `release-ios-mic-v23.log`。最终 v24 批次 `release-ios-mic-v24.log` 为 **1 passed**，包含下列 **3 通电话、6 段音频**，三通均 ended，转交个人助理为 0，每段均有非零输入与输出信号、音频播完并回到 listening：

| 实际场景 | 最终观察 |
| --- | --- |
| 保持已连接通话，网页保存 English，再用中文询问周末及饮品 | 回答切为英语，主动结合水彩、古筝、无糖咖啡 |
| 新电话开场为英语，说“这通电话咱们用中文聊”，再问饮品 | 本通改中文，饮品偏好正确；切换确认仍多叫一次称呼，细节保留失败 |
| 再新建电话，以中文问周末及文档名 | 恢复全局英语；完整保留 `青禾鲜果店门店视频制作与到货宣传注意事项（最终确认版本）.txt` |

三通为 `01M5PK0MJRRCEYETVAF7WW7Z6V`、`01M5PMSECEGXQ66QXH60FNXGFR`、`01M5PMT6PYBH015Z7DSE35ZGZ9`。逐段转写及人工判定在 `release-ios-v24-assessment.json`，截图和设备结果在 `release-ios-v24-artifacts/`。这 3 通功能测试**不等于** A02/A13/A17 每项完整独立重复 3 次，也没有在本轮补齐受控 ASR/提供商竞态。

### 耗时与网关证据

最终英语网页现场总内部墙钟 **2155.3 ms**、已观测外部等待 **32832.1 ms**。一次 TLS 建连失败约 **10002.5 ms**，尚未开始发送模型请求；修复后的首次重试退避为 **212.7 ms**，此前相同类别现场约 2297.8 ms。两次为不同实际请求，不能据此声称全部内部阶段都同比下降。重试仍计内部时间，没有重新分类制造提速。

本次内部阶段为：setup 80.9、runtime 68.4、history 164.7、context 497.5、request preparation 138.7、模型流消费内部工作 719.5、两段 finalize 149.7/335.2 ms（四舍五入会有差异）；退避是流消费内部子项，不再叠加。前五阶段合计约 950 ms，这一真实样本不能用前文即时模型替身的约 420 ms p95 代替，也不能把全部 2.15 秒与“入口准备 500 ms”门槛直接比较。最终真实负载下的 p95 尚未完成。

同时间窗口只读 UEE 运行记录：通道 19/20 均 active，分别有 95/84 条成功记录；19 最长 15 秒，20 存在 **619 秒**记录，输入 16392 / 输出 156 tokens。见 `release-uee-v23-readonly.json`、`release-uee-v24-slow-readonly.json`。没有逐个请求 ID 的完整跨端关联，因此这证明同窗口仍有上游长等待，不能断言每个本地超时都对应该条。成功型日志中的 timeout 字段为零也不能证明用户未超时。没有为了通过而增加模型截止或修改线上通道配置。

只测握手的直连/既有代理比较见 `release-transport-v22.json`：UEE 冷 TLS 约 1238/3254 ms，百炼约 38/158 ms；连接复用探测也完成。这不是模型请求可用性测试，不能证明长周期稳定；没有改变用户系统 VPN 设置。Qwen 仍走百炼官方，JEV 未启用。

### 恢复、迁移与上线门槛

- 通过正常设置 API 恢复 QA 语言为空，revision **67→68**，其他偏好保持原值，见 `release-v24-profile-restored.json`。全部语音已结束，停止观察器；设备音频输入移入 `release-ios-v24-device-inputs-archived/`，重新启动不含模拟输入/跳过 CallKit 开关的普通调试版。CUA 确认个人助理页面与历史正常，见 `release-ios-normal-restored-v24.png`；没有清空设备或聊天。
- 实际本地数据库仍为 `openbox_memory_upgrade_20261021`，head **ph84a5b6c7d8**，迁移文件 SHA256 `d40be0d0a88d6a21d2ac45ab34f3adf037e7149597ac5cad0b8f62329a0b17b3`，见 `release-v24-schema-check.json`。本轮未改 schema；沿用独立 PostgreSQL 的升级/重复升级/空库降级再升级/非空拒绝破坏性降级，以及兼容构建功能开关关闭再开启演练（`release-migration.json`、`release-rollback-http-disabled.json`、`release-rollback-http-enabled.json`）。这些不是旧 main 二进制回滚结果。API 与后台消费者仍须同版本发布。
- 最终源码/证据摘要见 `release-v24-final-state.json`；`git diff --check` 通过。当前工作区还有此前任务的其他改动，本节不将它们重新宣称为本轮修复。

**当前不能签署全部上线。** 已有通过证据的功能继续保留，但网关稳定性、PERF01 固定额外读取往返、最终版本全部答案无错误、真实负载内部 p95、真机 CallKit/物理麦克风/后台恢复、A02/A13/A17 全部独立场景及竞态、E04 真实租户沙箱权限/续费恢复/私有经验仍未闭环。已有不必要任务状态查询和本次一次重复称呼仍保留。不会用本地共享桌面、单元测试或成功重试替代这些门槛。

## 二十二 2026-10-24：本地会员与 wuyingyundev 私有经验验收

本节按用户最新范围执行：本地启用会员，sandbox 使用 `backend/.env.wuying-dev` 指定开发实例；只验收记忆相关行为。用户明确停止会员到期、续费和云实例生命周期实验，因此不以这些未测项目阻止本轮记忆验收，也不将它们标为通过。未提交、推送或发布业务代码。证据在 Git 忽略的 `.local-dev/memory-wuying-20261024/`；私有认证与通道密钥不进入报告。

### 环境与真实边界

- 本地前端 `http://127.0.0.1:3002`，后端 `http://127.0.0.1:8082`；隔离 PostgreSQL 库 `openbox_memory_upgrade_20261021`。schema head 仍为 `ph84a5b6c7d8`，本轮无新迁移，前述迁移演练的结论不扩大。
- QA 账号的原工作空间通过本地管理 API 补开旗舰月会员，至 2026-11-24 19:57（北京时间），回执 `local-membership-grant.json`。实际开发实例验收工作空间也有有效 max 会员，至 2026-10-25 18:53；真实 `require_sandbox_subscription` 检查通过，见 `final-real-verification.json`。没有绕过 sandbox 会员判断或修改生产账号。
- sandbox 路由为 `per_user` / `per_desktop`，绑定开发实例 `ecd-4y9s9igraz7hc58ea`，CLI 实际主机名 `9s9igraz7hc58ea`；不走 shared fallback。只实测了这一台开发实例，另一个本地账号用来验证应用权限，不声称测了两台真实桌面。
- 本地测试启动器原计划停用激活消费者及巡检，见 `cloud-testing-stopped.json`。**10 月 25 日更正：** 原启动链的 `execv` 丢失了进程内替换；日志中实际存在针对 QA 原工作空间的激活重试，在缺少 `WUYING_IMAGE_ID` 的本地配置校验处被拒，不能把旧记录当成“消费者始终未启动”的证据。现改为迁移后在同一进程启动服务，替换确实保留；本轮未进行云到期、续费或替换实验。停用仍仅属于忽略目录中的验收启动器。
- 本地模型价格未核定，最初 `BILLING_MODE=enforce` 的请求被价格校验拒绝；后续使用 `shadow` 测记忆。sandbox 会员门禁仍真实启用，模型积分结算不属于本轮通过项。

### 现场发现及修复

1. **已保存的方法没有主动进入候选。** 原语义路由已判定允许且需要记忆，但 `experience_useful=0.3` 低于类型阈值，导致首包没有任何经验目录；模型先查文件，再额外搜索记忆。现对允许读取且确需记忆的请求，本地补查最多两条相关的已启用经验标题及适用条件。低分补查不附加无关结果，过滤单个常见汉字的弱重合；完整步骤仍须按需读取，并核验所有者、工作空间、版本、技能摘要及来源。保留语义路由和 2048 token 预算，不新增业务触发词白名单或额外模型调用。改动在 `memory/experiences.py`、`readers.py`、`packet.py`。
2. **CLI 成功退出但输出缺行。** `container/action_server.py` 原流式 reader 在子进程退出后只补读一行即结束；`ls` 有时只返回目录总数和 `.`，模型误以为文件未生成，重复检查。现在在原 0.5 秒宽限内继续读取 stdout/stderr 至 EOF，挂起读任务取消后等待清理。仅将这一个 reader 修正同步到指定开发实例并重启其 action server，未部署整套代码；原文件保存在 `/opt/action_server/releases/memory_stream_20261024/original-action_server.py`。前后摘要见 `dev-stream-preimage.json`、`dev-stream-patch.json`。真实流式输出 **500 行中文完整、exit=0**，见 `dev-stream-verified.json`。
3. **本地开发页面动态资源过期。** 结果页曾因 Vite 缓存引用的动态块不存在而显示资源加载失败。改用验收进程独立缓存目录启动 3002 前端，刷新恢复，未重新提交任务；属于本地启动配置修正，没有改生产前端构建策略。

### 内置浏览器与真实执行证据

前置数据是一条有来源的测试记忆和“读书会签到表整理办法”候选，通过生产 service/API 创建；**不将受控候选创建当成自动经验提取通过**。在真实开发机生成并读回测试 CSV 后，内置浏览器记录验证、启用为私人技能（revision 3），见 `memory-dev-acceptance.json`、`experience-enabled.png`。

三个新会话均使用同一句普通用户请求：**“今天读书会林晓到了，陈雨还没回复。帮我弄一份签到表。”** 没有指定内部工具、读取记忆或系统字段。

| 现场 | 实际结果 |
| --- | --- |
| 经验发现修复前 | 会话 `session_7YBT9225ZPPQJHYWEQ5ARM71DB` 首包无经验目录，经过额外搜索后生成正确表格；9 次工具调用，页面总计 2m31s。价格校验的首次失败也保留，不记为一次无错通过 |
| 经验发现修复后、CLI 修复前 | 会话 `session_7YBT9018V4J9B3J9PXMNXWTWRW` 首次已读取正确经验，但因命令输出丢行反复检查，29 次工具调用、7m26s 后通过 UI 停止。此轮失败保留 |
| 两处修复后的最终会话 | `session_7YBT8YR91QRPHCK5FWS4KJVKCF` 初始包含正确经验目录，第一项工具调用为读取该经验 revision 3。包 **620 tokens**，complete、无裁剪。生成 `读书会签到表_20261024_v2.csv`，表头“姓名、到场状态”，林晓“已到”、陈雨“待确认”；原表保留，实际 UTF-8 读回一致。浏览器显示完成，见 `final-browser-result.png`、对应会话 JSON 和 `final-real-verification.json` |
| 修改经验及重新启用 | 浏览器补充“不记录联系电话”并保存，revision 4 进入待验证；当即读取不再返回旧经验，旧私人技能不可用。随后在开发机重新读回两份 CSV，记录真实核验并启用为 revision 6。见 `experience-revised.png`、`experience-reenabled.png` 及 `experience-revised-checks.json`、`experience-reenabled-checks.json` |
| 私人边界 | 在 revision 3、4、6 分别检查：另账号列表不含该经验，伪造工作空间 403，猜 ID 启用 404，另账号不能加载私有技能；所有者可用状态与经验状态一致。每阶段 6 项均通过。检查使用本地真实 SQL/HTTP，不是 UI 或第二台云实例实测 |

最终页面总用时 **2m11s、9 次工具调用**，其中仍包含多余的 Git 和目录检查；不能用这一次总耗时或两轮差值宣称速度达标。记忆准备 **2616 ms**，含语义路由 **2160 ms** 和检索外部等待，不是纯内部耗时。页面显示的整体上下文 **18033 tokens** 与 620 token 记忆包是不同指标，不能混写为整个模型上下文只有 620 tokens。

### 定向回归及本轮结论

- `test_memory_experiences.py`、`test_memory_read_protocol.py`、`test_memory_document_scope.py`：**39 passed**，`targeted-memory-discovery-final.log`。覆盖不同表达、低类型分、无关输入、当前消息限定、其他所有者、来源撤回后的失效等相关边界。
- `test_action_server_stream_output.py`、`test_action_server_execution_identity.py`：**15 passed**，`targeted-stream-output-final.log`。覆盖退出先发生时的双流完整读取、500 行中文及继承管道未关闭的有界等待。
- 上述两组共 **54 项**；只跑相关定向测试，未跑全量。初始夹具失败日志保留，修正后通过；`git diff --check` 通过。

**本轮“本地会员 + 指定开发无影机 + 私有经验主动调用/修改失效/权限隔离”通过。** 保留前后端供继续使用。本轮未复测自动经验形成、线上到期续费、两台真实租户实例隔离、真机语音及完整性能矩阵；它们不能由这次局部通过推导为通过。仍不能将当前分支整体宣称为 R1/R2 全部上线达标。开发实例 reader 有原文件回退备份，但本轮没有再次执行在线回退演练。

## 二十三 2026-10-25：文件任务、自动经验及语音遗忘修复

沿用本地 QA 会员、隔离数据库与 `wuyingyundev` 开发实例。本轮未提交、推送或发布，没有排查模型网关，没有进行线上会员或云生命周期实验。以下证据均在 Git 忽略的 `.local-dev/memory-final-20261025/`，认证资料不进入报告。

### 修复内容

1. **中文文件名与历史审阅。** Git 统计改用 NUL 分隔，统一差异头按 Git 的字节转义解码，覆盖中文、空格、引号、反斜线、制表符和换行。新增 patch 标记 `path_encoding=literal`，旧消息仅在公开读取投影时解码，避免新文件名二次解码，不改写历史数据。浏览器打开旧会话 `session_7YBT8YR91QRPHCK5FWS4KJVKCF` 的审阅，完整显示 `读书会签到表_20261024_v2.csv` 及实际内容，见 `chinese-review-fixed.png`。
2. **无关 Git 检查与上下文。** 发现服务端仓库及操作员主目录维护指令自动混入最终用户任务。服务端运行现在关闭这两种自动发现，显式部署配置中的授权政策仍保留。普通文件任务使用与交付物相关的操作，复用已取得的路径和回执。写文件回执补齐实际路径，并按 UTF-8 字节计算大小。不是按用户固定表达触发分流。
3. **真实遗忘缺陷。** 首轮 iOS 同一通话新增和纠正正常，但网页遗忘后仍复述旧答案：新记忆包已经生效，旧答案却留在提供商会话中。新增按账号、工作空间读取的撤回记录序号，利用现有不可变 tombstone，无新表。纠正或遗忘时，等待提供商确认清理旧临时消息，再应用最新指令；清理失败不继续用旧上下文回答。当前用户音频保留，原聊天和任务不删除。原始服务端通话记录保留，但旧片段不再进入检索上下文、通话摘要或转交内容。新增普通记忆、修改称呼/语言不会触发这项清理。
4. **跨入口缓存。** 空通话上下文也覆盖共享缓存；文字助手读取时核验 SQL 撤回序号，旧序号或在新记忆模式下没有序号的历史缓存不复用，避免等待下一次语音才能失效。相关定向测试包含删除后、语音尚未刷新时的拒绝读取。
5. **验收启动器。** 修正第二十二节所述 `execv` 丢失替换问题；本地迁移和 uvicorn 同进程运行，停用云激活消费者及巡检的验收替换保留。初始激活失败日志保留，不将最初状态表述为始终停用。Flutter 测试入口通过正常工作空间切换与关闭“桌面已就绪”提示进入通话，不绕过认证、会员或工作空间权限。

### 普通任务与自动经验的实际结果

- 输入“今天读书会周敏到了，赵宁还没回复。帮我弄一份签到表。”，会话 `session_7YBT8KGWCE83WXQAXH6MFEWC8F` 实际生成并读回两列 CSV：周敏已到、赵宁待确认。6 次工具，无 Git；页面 57 秒，出现一次重试，保留该事实。首包记忆 620 tokens，初始完整上下文 13314 tokens。见 `simple-task-first.png` 和会话 JSON。
- 用户在同一会话自然反馈已核验文件，并说明以后沿用流程。实际提取任务 `faf7ba6985214be203b5d4b422b80a54924e95e34b6794832785204c6de0eb24` 成功，经来源验证生成经验 `experience_01M5QD4SA6Z5MFTNE1STAQ4MXS`，初始为候选，未自动启用。来源是该真实反馈 `ms_e2e470e89a9281c1f6953d38dcc8563809c4a5dc2d793b08` revision 80。另形成相应记忆和 Wiki。
- 在真实开发实例读回文件后，内置浏览器记录验证并启用：经验 revision 3，私人技能 `skill_01M5QDVE1773R3FJ2KJG9TMVTD`。见 `auto-experience-candidate.png`、`auto-experience-enabled.png`、`auto-experience-file-proof.json`、`auto-experience-provenance.json`。这次自动形成候选来自实际对话，不是上一节的预置候选；人工验证启用不冒充自动验证。
- 新会话 `session_7YBT8J1FQSM1EPMFMRJNB5F3GK` 输入“读书会又开场了，孙悦已经来了，吴桐还没回消息。帮我做张这次的签到表。”，无需提醒规则，实际另存 `读书会签到表_20261025_v2.csv`，孙悦已到、吴桐待确认，无电话列。4 次工具为查文件、读模板、写新表、读回，无 Git。首包已含原始反馈、记忆、Wiki 及两条经验目录，1818 tokens，未超过 2048；模型无需再调用经验详情工具，因此不声称显式执行了新私人技能。见 `simple-task-final.png` 和会话 JSON。
- 页面显示 39 秒、完整上下文 13897 tokens；相比此前现场的 9 次工具、2m11s、18033 tokens 有改善，但不是配对性能基准。此次后端整轮 43603.2 ms，已观测外部等待 37873.8 ms，剩余内部墙钟 5729.4 ms。其中模型/工具循环内部约 2032.5 ms，请求准备 1037.4 ms，上下文 845.2 ms；有多轮 SQL 与提交开销。内部墙钟不等于 CPU，文件多轮任务也不能替代简单路径 p95，不能据此签署全部速度门槛。

### iOS 模拟麦克风、网页与旧确认卡

只替换麦克风 PCM；真实 ASR、WebSocket、语音提供商及原生播放，使用模拟器 audio-only 模式，未覆盖真机麦克风和 CallKit。两个测试日程均为本轮 QA 新建的虚构样本；只遗忘这两个样本，保留原话、普通聊天及原有其他记忆。

| 现场 | 结果与证据 |
| --- | --- |
| 修复前通话 `01M5QDP11CE828KE4MZXCDYB5A` | 新增、修改成功；遗忘后仍复述旧日程，语义失败。`voice-observed-before-fix.json`、`ios-before-fix-device-inputs/`。音频传输测试成功不算语义通过 |
| 修复后同一通话 `01M5QFJMVYWN59XWM0P3QJ1Z1Y` | 同一句音频“我周末上午一般有什么固定安排？”：新增后回答南风体育馆羽毛球；网页纠正后回答周日 9:30–11:30 星河公园跑步；遗忘后回答“这次没查到您周末上午的固定安排记录”。三段均有非零输入/输出，播完回 listening。`voice-fixed-new.json`、`voice-fixed-edit.json`、`voice-fixed-forget.json`、`ios-fixed-device-inputs/` |
| 提供商应用证据 | 修改时撤回序号 2→3、版本 179；遗忘时 3→4、版本 180。日志显示旧会话项清理确认后应用，约 265/521 ms 的局部刷新，不能当作语音端到端或 p95。`backend-revocation.log` |
| 新电话 `01M5QH23BJ1Q0698C9NYJ6XGC9` | 同一句音频没有复活被遗忘日程，实际回答没有查到记录。该批两个电话、四段音频，集成测试通过，见 `ios-mic-revocation.log` |
| 加入共享缓存修复后的电话 `01M5QH92AFFEJP76NZ53V0G6TQ` | 重新启动后，同一句问题仍没有旧日程；共享上下文记录带撤回序号 4。`voice-final-smoke-transcript.json`、`ios-final-smoke.log`，后者 1 passed、两段音频 |
| 旧 ask 与无关问题 | 浏览器自然提出 15 秒读书会视频需求，得到真实剧本审核卡；语音已主动告知其待确认。随后音频“现在先别做视频，我只想知道签到时没回消息的人应该怎么记。”正确回答“待确认、不猜测”，不虚报暂停、不重复催确认、没有转交个人助理。原会话仍只有一个 question 工具，状态 `waiting_input`，无媒体生成。会话 `session_7YBT8EQFST461G0NTMR2B9PG6Q`；`pending-card-retained.png`、`pending-card-after.log`、对应会话 JSON |

本轮四通 QA 电话最终均 ended，转交数均为 0，见 `final-scope-status.json`。遗忘样本均为 revision 3、DEPRECATED，撤回序号 4，见 `final-authority.json`。模拟麦克风测试结束后保留设备数据并重新运行普通调试版，不带模拟输入或跳过 CallKit 开关。旧审核卡保留供检查，没有自动确认或取消。

### 定向测试与边界

| 范围 | 结果 |
| --- | --- |
| Git 差异真实往返、历史公开投影、服务端指令边界、系统提示 | **11 passed**，`snapshot-runtime-final.log` |
| 已有语音刷新/删除确认、修改审核、转交及经验边界 | **87 passed**，`memory-race-tests.log` |
| 新撤回修复、原生准备、刷新和提供商确认 | **57 passed**，`revocation-targeted.log` |
| 摘要、转交、通话工具与记忆读取必要回归 | **102 passed**，`revocation-consumers.log` |
| 最终撤回/缓存、语音准备/刷新/工具 | **61 passed**，`revocation-final-tests.log` |
| 拒绝升级前无撤回版本缓存 | **5 passed**，`live-cache-final-tests.log` |
| Flutter 单个音频集成入口静态检查 | 无问题，`mobile-harness-analyze.log` |

批次有重叠，不相加为独立总数。新增专门测试覆盖真实 SQL 撤回序号、账号/空间隔离、旧检索上下文屏蔽、删除确认超时、用户发言与清理并发、旧摘要失效及当前音频保留。只运行上述定向范围，未跑全量。

**本轮已关闭上述具体缺陷，尚不能签署分支整体上线。** A13 这次是通话保持连接时逐项变更，不能冒充所有真实提供商并发时序已验收；A17 这次覆盖无关问题不误答旧卡，不代替三组独立图片改口的最终矩阵。未缓存语义读取的额外往返、最终真实负载内部 p95、完整冻结场景矩阵和真机音频/后台恢复仍需原门槛验证；本轮不会以网关或用户排除的云生命周期工作作为继续测试理由。没有新增数据库迁移，实际 head 仍为 `ph84a5b6c7d8`；之前迁移和兼容回退演练范围不扩大为旧 main 二进制回滚。源码摘要见 `source-sha256.json`，最终 `git diff --check` 通过。


## 二十四 2026-10-25 持续验收：状态回执、原任务恢复与记忆同步

本节证据目录：`.local-dev/memory-release-20261025/`。只在本地测试库及 `wuyingyundev` 验证，不操作生产会员或云桌面生命周期；未提交、推送或发布。延续第七节冻结门槛，不因测试失败放宽。

### 实测发现与已修复

1. **同一个任务读数被误判过期。** SQL `datetime` 与工具 JSON 中的时间字符串直接比较，导致有效的 `control_revision` 在下一次模型请求中被剔除。真实浏览器恢复水果店任务时出现猜测版本 1/2/3、连续冲突并建议重建。现按相同 JSON 投影比较；真实版本变化、成员撤销仍拒绝重放。同时只把历史读取计入历史总预算，实时任务读数使用每次有界预算，保留隐私策略和工具总次数限制。隐私拒绝不能被“预算不足”掩盖。
2. **恢复后仍显示旧错误。** 新执行已在等待新方案确认，卡片仍把上一轮附件失败展示为当前结果。任务观察数据新增 `latest_result.is_current`，网页和 Flutter 隐藏已过时的摘要，原报告仍可查看。没有篡改历史消息。
3. **桌面未连接的附件错误不明确。** 服务端保留 `SANDBOX_NOT_READY` 原因；网页和 Flutter 直接显示可理解的原因及原任务/附件保留情况，不鼓励用户重复上传或另建任务。
4. **实际选择的模型/视频档次不可见。** 文字助理任务读取及语音任务概览现在返回当前会话设置，并明确仅对后续提交有效、不是执行授权。
5. **重复数据库访问。** 记忆使用计数与展示记录合并为一次事务；账户设置、暂停范围、空间共享人数在同一读取中取得；预取与主循环使用同一入口。阅读会话与当前成员权限共同用 SQL 校验，不复用 ORM 身份映射中的旧会话；项目权限与成员查询合并；原生语音来源按来源、通话、私有主会话及确定性回执做批量关联。权限、删除、版本、来源哈希检查均保留。
6. **Flutter 多记忆标记渲染冲突。** 渲染期间的 provider 失效改为帧后刷新并检查组件/范围；模拟器真实音频链路已恢复，历史失败记录保留。

### 真实界面与音频证据

- 水果店任务 `01M5RCHJQV3Z08YEV4WP4XFNF4`，执行会话 `session_7YBT7KED88QPCK3NKNQ0SCS2AH`：普通输入“继续刚才那个水果店方案吧”恢复同一任务，已带入 `fruit-store-late-ai.jpg`、15 秒、视频号、无人出镜、字幕音乐。界面得到新方案卡；语音主动收到新方案通知。未自动确认付费生成。证据 `fruit-recovered-same-task.png`、`task-current-summary-fixed.png`。此前附件失败及版本冲突仍保留，不能把整条初次流程记为无故障通过。
- A13 三轮完整新增/修改/忘记：第一通 `01M5RH0SSN0NWPJY728QDSZX4A`，周一摄影课→游泳→无记录；第二通完整补测 `01M5RKSGNKAAR193DR4XX695FV`，周二瑜伽→陶艺→无记录；第三通 `01M5RMHVXNMDH4C20857EANRHQ`，周三羽毛球→书法→无记录。变更通过内置浏览器，提问通过模拟器 PCM 麦克风，ASR、模型及音频输出真实调用。每段播完恢复 listening。
- 事件顺序：第一轮测试壳把真实 ASR 先交付，第二轮把真实 response_started 先交付，第三轮保持原生顺序（本次实际 response_started 先到）。壳不伪造识别或回复。`voice-order-events.jsonl`、`voice-order-assessed.json` 记录 received/delivered，不能把控制后的顺序宣称为提供商原始顺序。
- 第二轮初次运行 `01M5RJTTB1DWWTHRE2432HWP30` 只完成新增/修改，集成程序达到 40 分钟截止而失败，忘记后的音频未执行；已单独保留 `ios-live-state-timed-out-artifacts/`，以后一通完整补测补齐，未删除失败样本。补测集成 `ios-authority-fixed.log` 为 **1 passed**，6 段音频，证据 `ios-three-cycles-artifacts/`、`voice-transcript-snapshots.jsonl`。第一通的逐项实测摘要另存 `a13-cycle-one-assessed.json`。
- 记忆版本 193–198 的空闲通话应用耗时分别为 236/306/495/193/359/719 ms，均有真实提供商指令确认；这些是刷新开始到确认的局部耗时，不能冒充数据库提交到确认的端到端 PERF03。
- iOS 只替换麦克风采集为 PCM，并绕过模拟器不支持的 CallKit；**不能代替真实 iPhone 麦克风、系统来电、后台恢复验收**。本机未发现连接的真机。

### 本轮定向回归

| 范围 | 证据与结果 |
| --- | --- |
| 任务回执时间比较、版本/成员变化、读取预算隔离 | `live-state-replay-final.log`：4 passed；此前失败保留在 `live-state-replay.log` |
| 任务观察历史/当前区分及读取边界 | `current-summary-backend.log`：11 passed |
| 网页卡片恢复与旧报告保留 | `current-summary-web-locked.log`：31 passed；使用原 package-lock 依赖，未保留包管理器变更 |
| Flutter 任务摘要 | `current-summary-mobile.log`：1 passed |
| 记忆使用事务/核心范围/补读与预取 | `memory-io-final-tests.log`：34 passed |
| 当前任务设置跨文字/语音 | `current-task-settings.log`：13 passed |
| 阅读会话当前权限与旧 ORM 对象拒绝 | `authority-joined-tests.log`：43 passed |
| 原生语音证据、个人范围、读取与检索 | `scope-batch-tests.log`：124 passed / 46 skipped；跳过项不算通过 |
| 最终批量绑定与上述直接相关范围 | `scope-batch-final-tests.log`：66 passed |
| 桌面错误投影及两端展示 | `desktop-cause-backend.log`：3 passed；`desktop-cause-web.log`：5 passed；`desktop-cause-mobile.log`：3 passed |
| 事件锁与代际 fencing | `event-lock-after.log`：46 passed；`postgres-event-fence.json`：真实 PostgreSQL 5 passed |

批次存在重叠，不相加为独立总数。没有全量测试，没有因速度要求删除权限或来源检查。

### 性能与上线判定（持续更新）

`dispatch-perf-io-summary.json`、`dispatch-perf-authority-summary.json` 都保留了未达标的 190 次配对结果。后者三个新版本暖请求的 HTTP 入口→模型准备 P95 为 **606.47 / 515.09 / 616.05 ms**，尚未通过 500 ms 门槛；常驻上下文为 **190.3 / 123.4 / 200.1 ms**，也有场景未通过 150 ms。不可用平均值替代。`perf-sql-callers.log` 是诊断壳采样，含调用来源追踪开销，不作为正式性能结果。

最新项目/原生语音批量查询的 190 次配对记录在 `dispatch-perf-scope.json` / `dispatch-perf-scope-summary.json`：新版本三个场景 HTTP→模型准备 P95 为 553.34 / 555.22 / 631.93 ms，常驻上下文为 154.3 / 137.7 / 128.3 ms；仍未通过整体性能门槛。补充列表前缀重放定向测试 `live-state-prefix-tests.log` 为 5 passed。当前不能签署整个分支上线：冻结性能门槛、A02/A17 最终矩阵及真机音频门槛必须分别核对；已有局部通过不覆盖这些未完成项。本轮未新增 migration，需在最终源码状态核对 head 与现有迁移/回退演练证据。


## 二十五 2026-10-25 最终矩阵复测及新发现（持续执行）

证据目录仍为 `.local-dev/memory-release-20261025/`。使用隔离 QA 库、真实开发无影机和 Codex 内置浏览器；不运行全量测试，不操作生产会员或云生命周期，不提交、推送、发布。局部成功不替代第七节冻结门槛。

### 新修复与实际证据

- 当前设置在最终模型派发前作为权威系统片段刷新，避免旧聊天中的语言偏好覆盖刚保存的设置。模拟器实际音频 `profile-chinese-pixels-retest` 以中文回答周末建议、称呼林姐，并使用当前艺术展览兴趣；`manual-created-task-awareness` 正确读到网页手建的视频会话及其停止状态。`ios-pixels-manual-artifacts/`、`ios-a17-pixels-corrected.log` 为真实 ASR/提供商/播放证据；不能计为 A02 三次完整独立闭环。
- 改稿审核绑定本轮附件的真实像素、归属、当前版本和视频档次目录，审核后重新核验。图片描述缺失不再推导为模型未见图片。审核使用独立可配置的 Qwen Max，普通读取仍用 Qwen Flash；没有更换语音或主聊天通道。Flash 原 8 例只通过 5 例；Max 同组 8/8，独立 12 例初次 11/12，修正“语义相同的待审核表述”误判后 12/12。失败样本和时间均保留，见 `review-scope-live-cases-qwen-max.json`、`review-holdout-12.json`、`review-holdout-12-v2.json`。
- 审核内容不符后只允许有限次 question 修正；连续三次失败或审核不可用后停止工具执行并解释原因。同一提供商批次也执行边界，不能转为无关 Bash 排查。`bounded-review-final-tests.log`：69 passed；审核/当前设置/真实图片传输/工作流相关回归 `review-role-final-tests.log`：99 passed。批次重叠不相加。
- 默认视频分辨率的空字符串不再挤掉配置默认值；内置浏览器新会话显示快速 768p。`video-default-display-tests.log`：19 passed。
- 18:06 的真实图片改口已让旧卡失效且保持未作答，但新方案失败：任务执行 33 次工具、约 28 分钟，反复遇到视频工具未装载；页面随后崩溃。通过页面停止了该本地任务，未自动确认付费制作。`direct-photo-after-crash.json`、`browser-direct-photo-revision.json` 保留失败；此例不计 A17 通过。
- 根因是发现回执承诺下一轮提供工具，而预算器仍按推测意图包优先，反复把刚发现的工具移走。修复后把本运行刚发现且经当前权限/摘要核验的工具优先装载，保持同一回复不可执行新发现工具、单工具和总量硬上限；发现提交前检查下一步能否装入，不能装入时明确失败，不再承诺可用。真实开发实例目录对照 `real-dev-tool-materialization.json`：原逻辑视频工具不可执行，修复后可执行，定义 31881/32000 字符。`requested-tool-priority-tests.log`：42 passed；还需真实浏览器完成新方案验证。
- 折叠思考与工具轨迹不再挂载隐藏正文，避免持续解析长 Markdown/工具详情。恢复后可打开原 33 次工具的长会话并停止任务。`folded-trace-verified.log`：16 passed；前两批测试壳依赖缺失和文本空白断言失败均保留，不掩盖为产品通过。
- 模拟麦克风问不存在的修鞋店，实际回复“这次没查到……记录”，没有扩大为全局不存在。`ios-bounded-absence-artifacts/`、`ios-revision-bounded.log` 为本次单段音频通过；ASR 将“星桥”转为“新桥”，原样保留，不冒充实体识别完全正确。

### 性能、矩阵与剩余上线门槛

- 早期未隔离 Redis Pub/Sub 的性能记录不用于正式配对结论；增加事件通道 namespace 并按账号和权限继续校验。真实 Redis 与定向测试证据见 `event-scope-tests.log`。
- 定位到初始化长寿命对象造成的周期性 Python GC 停顿，新增默认关闭的启动后 heap freeze 开关。启用后的隔离 190 次配对在 `dispatch-perf-gcf.json`/`dispatch-perf-gcf-summary.json`：三个暖路径准备 P95 为 367.08/292.73/363.57 ms，常驻上下文 P95 为 94.2/86.0/84.6 ms；数值门槛通过。只是短时基准，不证明长期无内存增长。未缓存输入仍增加一次语义判断，PERF01 整体仍开放，未启用此前速度更差的 lazy 实验。
- PERF03 五次数据库回执到提供商确认分别为 703.7/327.3/330.8/349.2/218.2 ms，全部小于 2 秒；前四次和第五次分属不同通话，不能表述为一通连续五次。证据 `perf03-five-assessed.json`。
- 60 条独立读取矩阵的限制 60/60、必要证据 20/20、最大 2026 tokens；语义人工复核 59/60，保留“有限查询扩大成全局没有记录”失败。修复后的模拟音频是补充验证，不替代完整矩阵最终签署。证据 `holdout-60-assessed.json`、`holdout-60-semantic-review.json`。
- A13 三次完整闭环已有第二十四节证据。A02/A17 最终三次独立闭环、当前代码完整冻结矩阵及真机 CallKit/物理麦克风/后台恢复仍未全部完成。模拟器明确跳过 CallKit，不能替代真机。
- 本轮未新增 schema。沿用已有迁移和兼容功能回退演练，不扩大成旧 main 二进制回滚成功。本地测试会员按用户授权延长用于验收，审计在 `local-membership-extended.json`；不影响生产账号。

**当前仍不签署 R1/R2 全部上线。** 后续结果必须追加具体回执，不因局部测试数量或页面恢复而自动勾选剩余门槛。

## 二十六 2026-10-25 原子设置、读取边界与图片上下文

证据根目录 `.local-dev/memory-release-20261025/`。业务仍在 `codex/memory-system-upgrade`，只使用本地 QA 与 `wuyingyundev`；未操作生产会员、未删除云端或浏览器现有数据，未提交、推送或部署。只跑相关定向测试。本节覆盖前序过时结论，但保留失败证据。

### 设置保存及跨入口顺序

先前 19:19 的第三次语言修改返回 `needs_context`，助理却声称已记住，约 20 秒后后台提取才保存；该次不是原子成功，不能继续作为 A02 通过证据。设置现在用原始来源、主体、永久/临时范围及实际字段值做独立类型校验，只有成功提交才可报告已保存。需要澄清的提案返回 `ASSISTANT_PREFERENCE_NOT_SAVED`。同一人类来源的提取与直接写入按字段去重：后台重复提取返回 `replayed=true`，不重复增加设置 revision/context version；更新后的 UI 设置仍优先。

真实保存顺序为：20:38 英语 `profile=83/context=223`；20:51 中文 `84/224`；21:26 英语 `85/227`；21:35 中文 `86/232`。四次后台提取均复用原版本。第一、第三、第四次分别有保存后的新普通会话和新通话，正确使用英语/英语/中文及艺术展览兴趣。第二次新文字会话正确；新通话虽为中文，读取判断 6 秒超时后只给通用建议，**不能算完整主动召回通过**。当前本地语言已恢复中文。

- 文字：`settings-current-english-browser.png`、`settings-current-chinese-browser.png`、`settings-third-english-browser.png`、`settings-fourth-chinese-browser.png`。
- 通话：`01M5SHGT1R717VCXYKKSYS1K4Z`、`01M5SKXZWDKHD5HYJQG0C25CP9`、`01M5SMGG6B6MCGW5D4A4XV7JEF`，ASR、模型、原生播放真实，未转交文字助理。只替换麦克风为 PCM，不能覆盖真机 CallKit/后台音频。
- `ios-setting-scope-live.log` 为 1 passed。`ios-read-boundary-v19.log` 因操作者未在 40 分钟内结束测试壳而失败；其中第三、第四次音频证据保留，但**不能将整个集成批次改记为通过**。证据目录 `settings-ios-first-live/`、`settings-ios-final-live/`。已恢复无模拟音频参数的普通调试版。
- 第四通回复仍在开场后再次称呼“林姐”，A01 不句句称呼的严格门槛仍需最终复核。

### 任务恢复与图片膨胀根因

取消的任务只有在驱动、认领输入、待恢复问题和外部效果均处于可恢复状态时才能恢复。恢复回执要求继续原任务，不重放旧审批或旧输入。一次 `NOT_RUNNING` 后，工具执行边界只允许查询、恢复、补充同一任务及澄清；禁止借重新提交逃逸，连续失败有界停止，同一提供商批次也执行该约束。

19:45 真实浏览器已恢复原水果店任务，但初始化目录失败，外部效果进入 `manual_review`；没有手工清除失败效果。21:32 用户式输入继续原会话后，新执行成功准备开发目录，查看真实照片并生成 15 秒新方案和待审卡。只进行了免费估价和报价，没有视频生成。证据 `stopped-recovery-live.json`、`photo-plan-recovered-v19.json`、`photo-plan-recovered-v19.png`。这是一个恢复成功案例，不能补足 A17 的所有独立闭环。

这次方案耗时 11 分 19 秒、输入 72,662 tokens，分析发现 `read` 把 PNG 当 UTF-8 文本，产生 27,642 字符二进制乱码。修复后已知图片后缀在任何远程读取前直接返回 `view_image` 指引，其它二进制返回适合的读取/提取说明；无后缀或错后缀仍检查二进制内容。历史错误输出只在模型投影中替换，原始审计不改写。

`binary-replay-measurement.json` 估计该单段由 17,036 tokens 降为 63，使用本地估算器，不冒充提供商计费数。22:32 浏览器自然问“先别制作，我想确认一下：照片里有没有顾客？”，正确回答照片无人、没有执行生成，输入 48,858 tokens。它与上一条是不同请求，不能把全部差值归因于本次修复。`photo-inquiry-binary-fixed.png` 保存界面。

该次耗时 22,331.2 ms，关键路径外部等待 18,218.7 ms，内部 4,112.6 ms；内部包括历史 761.8 ms、上下文 450.5 ms、模型请求准备 234.5 ms、收尾 854.4 ms 等，不能冒充简单新对话的 500 ms 门槛已通过。普通执行会话的新消息会将原待审卡标记 superseded，三张卡均未作答、未 applied，见 `photo-questions-after-inquiry.json`；“仍未批准制作”不等于旧卡仍可点击。

### 读取隐私、失败透明与语义验收

旧路径即使判断 `memory.needed=false` 仍自动带入稳定私人背景，现已移除；版本刷新也按本轮读取需要和政策重新决定。`restricted-browser-v19.json`/截图证明“先把关于我的背景放下，仅就这句‘春天来了’写一个改写”只用当前内容，注入核心和召回均为 0。查询不可用时明确告诉用户这次未取得记忆，再给通用建议；不能假装已个性化、声称从未保存或承诺后台重试。

完整 v19 冻结矩阵 `holdout-v19-final.json` 保留 58/60 政策匹配、11 条明确受限请求全部无历史注入、最大 2018 tokens；16 complete、12 restricted、31 partial、1 unavailable。1 条主模型超时；1 条读取判断超时，最终回复已正确说明未取得记录；1 条“照这些条件，还要买什么吗？”被错误判为 current_only，要求用户重复已聊过的条件。不能将这些记为 60/60 通过。

旧必要证据文本检查为 19/20：文件名用例只读取目录身份并正确给出完整文件名，旧检查器却统一要求正文同时含“20 秒”和“5 秒”。保留原检查结果；目录引用身份可见，不以回复正确反推全部模型输入已验证。

两种新提示、替换 Max、独立范围/声明验证实验没有优于基线，分别保留 `router-v20-independent.json`、`router-v20-max.json`、`scope-focused-max.json`、`scope-claims-max.json`；没有默认启用，也没有增加常驻第二个验证模型。v21 沿用 v19 语义指令，增加类型协议一致性校验：`current_only` 不能同时要求依赖近期对话。矛盾计划只能在原 6 秒总限额内重判一次，仍矛盾则关闭读取并解释，不自动放开历史，不匹配固定用户措辞。19 条真实分类定向用例 19/19，`router-v21-consistency.json`；新的完整矩阵和浏览器结果另行追加。

### 定向测试与发布边界

| 本轮相关范围 | 结果证据 |
| --- | --- |
| 设置原子结果、独立范围验证、来源去重 | `settings-scope-final-tests.log` / `settings-scope-related-final.log`，以及保存回执相关批次；原失败保留 |
| 读取需要、权限、当前设置与不可用说明 | `read-boundary-final-v2.log`：131 passed |
| 图片/二进制读取、历史投影、开发实例绑定及工具预算 | `binary-read-final.log`：78 passed |
| 模型输入及记忆投影相关回归 | `final-replay-regressions.log`：90 passed |
| 读取计划自相矛盾、一次修复截止与受限语义边界 | `read-contract-consistency-tests.log`：119 passed |

批次重叠不合计。本段没有新增 schema，沿用已记录的 PostgreSQL 升级、幂等、降级保护及关闭/重启功能演练；不能扩大成旧 main 二进制回滚通过。此前 heap-freeze 隔离数值基准通过不等于消除了首次额外语义调用。**当前仍不能签署整体上线**：PERF01 架构条款、最终完整矩阵、A01/A17 独立现场和真机音频尚有缺口。

### 10 月 25 日深夜至 26 日追加：原文候选配额及最终现场

`holdout-v21-final.json` 的 60 条完整专项复跑为读取政策 **60/60**、11 条明确受限请求均零历史注入、记忆包最大 **2016 tokens**。原必要证据检查为 18/20，主模型错误 3 条（`document-03`、`reference-02`、`reference-04`），原件保留。57 条有回复的实际语义已逐条查看；其中 filename 用例给出完整文件名，不能把旧文本检查器不含“20 秒/5 秒”的结果偷偷改为通过。

进一步诊断发现 `document-03` 的必要原文确实未进入上下文，不能仅归因于主模型超时：授权的原始文件排在重排配额之外，重复问句和派生主题占满了有限候选；文件没有重排分数，最后被相关性过滤器排除。原有“打包时优先原文”太晚。现在对语义上需要知识的请求，在合并与重排截断之前为最强的一个已检索、已授权原始文件保留配额；只替换边界末位，保留更高排名的候选顺序。仍需重排相关性、当前 ACL、版本和最终来源校验；不扩大候选数，不新增外部调用，不在代码里匹配用户问题词句。

- 同一原文从 `rerank_score=null`、未进入最终包，变为 **0.9328** 并进入包；`document-recall-v21-diagnostic.json`、`document-recall-primary-reserved.json`、`document-recall-primary-final-order.json` 保留前后诊断。此为真实索引/重排与 SQL 读取，未调用主回答模型。
- 首版候选保留后的相关 21 条真实回答为 **21/21 政策、10/10 必要文档证据、0 模型错误、最大 1961 tokens**，逐条核对数字、原文与完整文件名；见 `holdout-primary-candidate-final.json` 及 assessed 文件。最终将保留位置调整到配额末位后再测 4 个直接受影响用例：`document-03`、`document-10`、`reference-02` 回答正确，`reference-04` 主模型超时，见 `holdout-primary-order-final.json`。不能把两批拼成“最终版本全部通过”。
- 程序回归 `primary-candidate-tests.log`：100 passed / 59 skipped；调整顺序后 `primary-candidate-order-final.log`：65 passed / 55 skipped。跳过的是未设置 PostgreSQL 专用测试连接的参数，不计通过。随后在明确隔离的本地 PostgreSQL 库补跑原文候选、相关性过滤和权限变化：`primary-candidate-postgres-final.log` **24 passed**（含 SQLite/PG），没有生产写入。批次重叠不求和。
- 内置浏览器在开发无影空间新建自然对话，先说明一部手机/自然光/不买器材，再问“照这些条件，还要买什么吗？”，实际回答“不用买”，未要求重复背景。会话 `session_7YBT66AEJ7XBWX6F95XRG9WKA5`，`browser-reference-v21.png` 与 debug 文件。
- 内置浏览器独立上传测试资料《禾木面包店到货短片拍摄说明（正式确认稿）.txt》，页面显示“可以阅读”，新会话初次问答主模型超时，原失败截图 `browser-primary-first-timeout.png` 保留。最终代码下自然接问总时长和全名，正确返回 **4 秒/镜头、18 秒总长和完整文件名**，不会复用青禾资料的 5/20 秒。会话 `session_7YBT64E8M26G9MB8Z0JNPYSKXR`，截图 `browser-primary-final.png`、debug 文件和 `browser-primary-upload-ready.png`。UI 5.3 秒为模型步骤展示值；服务完整 trace 外部等待 7069.2 ms、内部 1443.2 ms，不能当作整体内部 500 ms 通过。
- iOS 当前 v21 的一个完整通话 `01M5ST2Q804ZZ4W70JYXNGFRQW`，依次说“帮我想个周末安排”“那我上午去看展，下午做什么好”“这次先别参考我的兴趣，只解释一下什么叫自然光拍摄”：真实 ASR/播放完成，三段均回 listening，零文字转交，第三段 `no_history` 且读取包 items=0，通话已 ended。`ios-read-consistency-final.log` **1 passed**，`voice-v21-final.json` 与 `ios-v21-final-artifacts/`。接通问候和第一条答复仍各出现一次“林姐”，后两条没有重复；严格开场只称呼一次的问题仍保留。现已恢复无模拟音频参数的正常调试版，不能把本结果扩大到真机。

本轮最终 schema 只读核验在 `schema-final-20261026.json`，仍为 `ph84a5b6c7d8`，没有新增 migration。前述迁移/兼容功能回滚演练仍有效但有原边界。未操作模型网关，未部署。继续开放：首次未缓存语义调用的架构门槛、最终全场景签署、严格开场称呼与 A17 独立重复、真实 iPhone 音频生命周期。上述缺口不能通过重复筛选成功样本或放宽原规范来关闭。

## 二十七 2026-10-26 词表与语音指令确认修复

证据目录仍为 `.local-dev/memory-release-20261025/`。本节不修改冻结门槛，不覆盖之前的失败；只在本地测试账号、隔离数据库和开发无影环境执行。

### 关键路径测量及离线词表

增加模型请求准备的 canonical surface、消息投影、checkpoint、记忆授权和 token 测量分段。真实浏览器问面包店是否需要找人拍摄，正确按资料回答手机、自然光、无人出镜，不要求额外人员或器材。该请求外部等待 11346.8 ms、内部 1469.0 ms；准备 267.8 ms 中 token 计算仅 1.56 ms，不能据此把延迟归咎于计数，也没有加入可能掩盖权限变更的请求缓存。

排查同时发现原词表缓存探测只检查目录中有任意文件，实际词表缺失或损坏时可能调用带下载能力的加载器。新增 `core/local_tokenizer.py` 按配置优先级读取精确缓存文件，校验固定 SHA-256 后从已读字节构建 Encoding；不调用网络加载器，不修改坏文件。缺失或校验失败沿用可见的估算回退。Docker 构建下载后用同一加载器验证，进程启动在线程预热，实际请求不下载词表。完整镜像构建尚未执行，不能以本地等价验证冒充镜像验收。

- `tokenizer-vocabulary-20261026.json` 记录明确下载步骤、3613922 字节及校验摘要；生产使用同一固定词表。冷加载 111.7 ms，启动预热 117.8–185.3 ms，均不记为暖请求内部耗时。
- `request-profile-tests-20261026.log` 的 7 failed / 31 passed 保留：旧上下文测试样本按真实词表标定，本地缺词表使用估算导致不同分段。显式提供已校验词表后 `context-budget-local-vocabulary-20261026.log` 为 50 passed；不宣称无词表时该组已通过。
- `tokenizer-offline-tests-20261026.log`：35 passed，覆盖错误目录、无关文件、损坏内容、显式禁用、加载后文件变化和禁止网络加载。
- 新浏览器会话 `session_7YBT60N75R6N9MM3GNGPDN33HB` 问“面包店到货视频那个说明，每段最多停多久？完整文件名也给我。”，回答 4 秒及完整《禾木面包店到货短片拍摄说明（正式确认稿）.txt》。`browser-tokenizer-recall-20261026.png` 与 debug 文件保留。外部 8445.4 ms、内部 1722.8 ms，准备 471.3 ms、token 计算 13.74 ms；UI 6.2 秒是步骤值，不能当作请求全程。两次问题不同，不作受控速度对比。

### 最终刷新与 VAD 阻塞

最终记忆刷新曾先构建新设置文本，却在发送指令时仍从旧 `memory_packet` 生成权威语言规则；确认后又标记新版本已生效。现将候选 packet 显式传入指令构建，只在真实 ACK 后提交本地版本。确认失败保留旧版本；新语音代际到来时禁止旧回复继续播放。复现测试 `voice-final-refresh-before-20261026.log` 为 2 failed，修复后的 `voice-final-refresh-guard-tests-20261026.log` 为 26 passed。

第一次真实复测通话 `01M5T02QVDEAW0RJB2A5V0KR2D` 仍失败：开始讲话时 packet 被清空，中间清理流程同步等待不完整指令的 ACK，重复出现 2 秒超时，最终通话以 error/1011 结束。`ios-final-refresh-20261026.log`、`voice-refresh26-final.json` 和 `ios-refresh26-final-artifacts/` 保留失败。模型读取本身当次 1171 ms 成功，不能把整次问题归因于网关。

现在记忆准备期间不发送没有完整 packet 的中间指令，自动语音输出继续隔离；只有最终完整指令经确认才恢复回答。增加仅记录长度、序号和耗时的 ACK 诊断，不记录正文，仍要求精确内容相同。定向覆盖旧 ACK、空白不同的 ACK、当前 ACK 和没有消费业务事件时的独立确认；`voice-vad-update-tests-20261026.log` 为 74 passed，`voice-instruction-ack-tests-20261026.log` 为 2 passed。

最终通话 `01M5T0XTGJG9RRAAC67KEF5RZQ` 完整通过：

1. 用户中文“帮我想个周末安排”，按英语设置回答，使用艺术展览兴趣；没有转交文字助理。
2. 保持通话，在内置浏览器把语言改为中文，保存 context version 243；中文接问上午看展后下午做什么，下一句以中文回答。
3. “这次先别参考我的兴趣，只解释一下什么叫自然光拍摄”，回复通用解释，读取政策 `no_history`，核心及召回均为 0。

三段均完成真实 ASR、模型回复和原生音频播放，回到 listening；通话 ended，转交数为 0。ACK 分别为 164/171/170/108/107/105 ms，均精确匹配，本通没有 ACK 超时。准备完成 2345/2477/2447 ms 包含外部模型等待，不能计为纯内部耗时。称呼这次只在开场出现，仍不以一通成功替代全部独立重复。`ios-vad-final-20261026.log` 为 1 passed；`voice-vad26-final.json`、`ios-vad26-final-artifacts/` 保存音频和状态证据。设置已恢复中文，并恢复无模拟麦克风参数的普通调试版。模拟器绕过 CallKit，只替换麦克风采集，不能代替真实 iPhone 系统音频生命周期。

### 其他回归与剩余项目

旧消费者测试的四条假设与已有契约相反：不应拿无关 REFERENCE 填满常驻记忆，项目引用必须可区分，同一来源不同事实不能被粗暴去重。更新测试并新增同内容同项目去重、跨项目保留的对照；没有为使测试变绿而改变实际读取行为。`memory-tokenizer-budget-tests-20261026.log` 原 4 failed / 62 passed / 15 skipped 保留；相关修复后批次 `memory-voice-final-refresh-tests-20261026.log` 为 91 passed / 15 skipped。随后在明确隔离的 PostgreSQL 补跑原文候选、相关性与权限变化，`tokenizer-source-postgres-20261026.log` 为 24 passed。以上批次重叠，不相加为独立通过数；跳过项不算通过。

本节没有新增 schema。最终 60 个冻结记忆场景正在运行，结果另行追加。R1/R2 仍开放：每轮额外语义调用、最终矩阵及性能对照、A17 独立完整流程、真机 CallKit/麦克风/后台恢复。兼容功能回退与旧 main 二进制回滚分开记录，尚未执行后者。


## 二十八 2026-10-26：按需读取计划、计算去重与执行边界

证据目录继续使用 `.local-dev/memory-release-20261025/`，本节全部为本地 QA、隔离数据库和指定开发实例。没有提交、推送或部署；没有全量测试。

### 已实现及已验证

- 新 `DialogueReadPlan` 由实际主对话模型提交需要哪些背景、读取限制及原话依据。服务端继续以原始用户输入、原始身份执行既有契约校验；模型不能提供替代身份或改写原请求。原话不匹配、语义关系自相矛盾、非法数值均拒绝；同一轮只准备一次，后续计划不能扩大权限。轨迹记录实际模型、`planner=dialogue`、`called=false`、额外判断请求数 0。默认开关仍关闭，本地验收专用启动器明确启用。
- 原默认路径最终 60 例 `holdout-final-tokenizer-20261026.json`：政策 60/60，必要证据 20/20，11 条明确受限请求均零历史，最大 2035 tokens。人工复核 57/60：一条只承诺遵守而未回答细节、一条虚报已暂停任务、一条主模型超时。不能写成用户结果 60/60。后续修复不抹掉该批失败。
- 独立按需读取 18 例冻结在 `dialogue-independent-18-manifest.json`；初次 17/18 符合语义预期，失败为虚报暂停。隐含兴趣、文档原文、引用限制、撤回限制、同句不同前文及不存在的事实均有真实模型证据；所有调用均没有额外 Qwen 读取判断。限制类未调用读取工具的场景保持 `deferred/uncertain`，只可判断零历史行为正确，不冒充明确分类通过。
- 读取工具回执明确 `operation=read_only/state_changes=[]`；当前动作限制只说明应遵守的行为，不作为已经暂停、取消或执行的回执。修复后自然用户问题能得到资料细节，不只回答“以后遵守”。`dialogue-action-receipt-retest-20261026.json`、`dialogue-claim-regression-20261026.json` 保留回归；其中“20 秒”被改述为“20 秒内”的数值精确性仍需最终语义复核。
- 内置浏览器新对话主动结合当前开发空间的艺术展览兴趣；接问面包店说明返回每镜头最多 4 秒，未声称已经暂停任务。`browser-dialogue-active-20261026.png`、`browser-read-only-receipt-20261026.png`。早期名为 `browser-dialogue-plan-first` 的现场实际被 QA 环境覆盖为 eager，单独保留，不计作按需模式通过。
- 预算循环在同一次同步检查内复用未变化内容的脱敏/分词结果，任何删减立即失效；移除紧接最终刷新后的重复预算调用。没有缓存权限、版本或删除状态。`budget-render-tests-20261026.log`：69 passed。
- 同一个工具边界把执行票据与驱动租约放在一条实时 SQL 中读取；保持用户、会话、代次、run、owner、有效期及撤销检查，近到期仍续租，非匹配上下文回退原检查。状态写入把消息与 Part 身份放入同一查询，保留行锁、创建幂等冲突、更新归属和事务事件。`joint-fence-tests-20261026.log`：42 passed；`part-write-batch-tests-20261026.log`：46 passed。PostgreSQL 首批因旧测试造数的外键顺序失败，改用已有完整夹具后 9 passed，最终只取必要字段版 `joint-fence-columns-postgres-20261026.log` 也为 9 passed；失败原件仍保留。
- 发现真实输入中的记忆摘要 ID 恰含手机号形状的数字，被通用脱敏改写。现仅由已授权对象的类型化投影标记不透明引用，保留其精确 ID；普通用户文本、伪装 ID 字符串、文件名内手机号及运行凭据仍脱敏。`reference-identity-tests-20261026.log`：57 passed / 9 skipped；跳过参数不计通过。

### 性能证据及未关闭项

- 第一版 eager/按需比较 `dispatch-perf-dialogue2-20261026-assessed.json` 为 250 个相关样本；个性化最终请求准备 P95 556.64 ms。该测试关闭 `MEMORY_WIKI`，知识原文 0/30，因此不能用来签署知识质量。此配置问题已定位并保留原始结果。
- 启用原文读取、停用测试后台编译器后，使用冻结 `a95a302`、相同 SQL 快照及即时外部提供商，再配对 30 暖/路径和每版本 5 个进程首请求。`dispatch-perf-frozen-20261026-assessed.json`：新路径内部入口至最终回答派发 P95 为问候 254.11、偏好 538.26、普通事实 234.60、知识 630.19 ms；必要偏好及原文均 30/30。偏好仍超过 500，知识较基线 522.13 增加超 10%，不通过。计入真实准备工具和第二轮主模型，不用第一次派发伪装最终就绪。
- 最新合并查询及引用修复后的配对正在独立新库复测；结果追加在下文，不把先前成绩当作最终源码已通过。真实主模型全量记忆专项矩阵、A01 严格开场称呼和 A17 三次独立改图闭环仍须按受影响范围完成。

### 范围与门槛解释

原冻结规范明确允许“真实麦克风或经过授权的虚拟麦克风”，移动端为“模拟器或实机”；用户明确要求 iOS 模拟器。此前执行记录把额外真机 CallKit 测试写成全部记忆验收的硬门槛，不符合这两条原规范，本节纠正这一解释，**不修改任何冻结数值或核心功能门槛**。仍须完成模拟器中的真实 ASR、语音模型、播放、后台恢复与跨入口协同；模拟器结果始终不能表述为物理麦克风或真机 CallKit 已验证。主机本次设备清单无连接真机，作为未覆盖范围记录。

用户已明确排除生产会员到期/续费及云生命周期实验；E04 本轮适用范围是开发无影机、私有经验读取执行与应用权限隔离，按第二十二、二十三节证据核验，不重新操作线上用户。A13 三次完整变更闭环及受控事件次序见第二十四节，不因历史追加文字重复列为从未完成。数据库 schema 本轮未新增，迁移/兼容回滚仍按已有实际演练范围。

### 第二十八节增量：合并边界后的配对结果

`dispatch-perf-joint-20261026-assessed.json`：同快照、同关闭后台工作配置，真实 SQL，即时外部替身；每路径 30 暖请求，每版本 5 个进程首请求另列。最终模型请求包含按需准备工具步骤，不只计首次模型请求。

| 路径 | 基线内部 P95 ms | 新内部 P95 ms | 新常驻阶段 P95 ms | 证据 |
| --- | ---: | ---: | ---: | --- |
| 问候 | 353.61 | 377.03 | 53.7 | 30/30 无无关记忆 |
| 个性化 | 609.44 | 530.41 | 127.3 | 30/30 必要兴趣证据 |
| 一般事实 | 437.53 | 216.17 | 57.8 | 30/30 无无关记忆 |
| 知识 | 635.81 | 595.05 | 162.2 | 30/30 原始文件 20 秒/5 秒 |

知识相对门槛通过（-6.41%，-40.76 ms），PERF01 个性化 500 ms 门槛仍失败。包括 1422 ms 的单条慢样本均保留。继续将同边界的设置/记忆时钟/撤回时钟/成员状态合并为一次新鲜 SQL 读取，不缓存权限或跳过来源核验；补齐 known_gaps 精读引用 ID 的同类误脱敏修复。相关 6 个测试文件 100 项通过，PostgreSQL 和新配对结果另记。


### 第二十八节增量：来源权威与实际语音复测

完整冻结 60 例首轮在 `dialogue-final60-20261026.json`，实际 58/60 答案符合要求，20 个必要个人/文档场景均有证据，11 个受限最终请求没有注入禁用历史；最大记忆上下文 2039 tokens。两条答案仍错：将总长 20 秒误说成上限，以及没有执行回执却说已暂停。保留该失败批次；`assessed-v2` 只修正评估器混入上一轮允许读取记录的问题，不改原始结果。

已将来源类型随证据传递：原文、用户历史陈述和 Wiki 派生摘要分别标记。静态规则要求数字、单位、确切值及上下限保持原意，历史摘要和只读回执不得充当当前操作成功。没有加入用例原句、业务关键词路由或答案替换。`authority-projection-tests-20261026.log` 为 62 passed。修复后两条失败回归与事先冻结的 8 条新表达全部通过，见 `dialogue-authority-regression10-20261026-assessed.json`；这是相关回归，不重写此前 60 例成绩。

状态合并查询后的第二组 250 请求结果在 `dispatch-perf-state-20261026-assessed.json`：问候、偏好、常识内部 P95 为 228.65/507.19/233.46 ms，常驻上下文 56.8/101.9/51.3 ms。偏好仍超过 500 ms，不能记为 PERF01 通过。随后将同一工具范围判定的 4 项视频状态探测合成一条新鲜 SELECT，失败时仍逐源回退，未缓存权限；25 项相关回归及新增 2 项状态新鲜性/独立故障测试通过。最终配对另行追加。

03:23–03:41 的 iOS 模拟麦克风批次 `ios-state26-microphone.log` 为 1 passed，5 段均真实 ASR、语音提供商及原生播放，回到 listening。前两段询问未保存的周二晚/周末上午安排，正确回答本次没查到；不能拿它们算偏好召回。后两段主动召回艺术展览兴趣、完整面包店文件名及单镜头 4 秒，见 `voice-state26-live.json` 和 `ios-state26-artifacts/`。通知多次带称呼、一次普通回复再次带称呼，仍如实保留，不能宣称严格只在开场称呼已闭环。

邻里鲜果店案例中，语音知道网页手建的任务，修改请求实际发往原会话，旧卡 superseded 且没有答案。但首次图片只在输入框暂存、未发送，任务错误使用了桌面旧素材，审核未通过。之后在内置浏览器正式发送照片得到 15 秒新方案，但没有补出新确认卡；这是部分恢复，不算 A17 完整成功。证据 `a17-state26-review-live.json`、`a17-state26-sent-photo-plan.png`；未自动确认或生成付费视频。

### 10 月 26 日 04:10 追加：设置闭环及数据库读取

A02 新批次三轮完整通过：每轮先在内置浏览器个人助理用不同的日常表达保存全局英语设置，再创建全新普通会话和全新 iOS 通话；两入口均无需提醒即用英语回答，并主动结合看展、不逛商场、不烘焙的当前偏好。设置 revision/context version 依次为 89/260、91/262、93/264；后台提取同源重放未重复增加版本。三通分别为 `01M5T9YHGATSQSJWMG6AZGG5SG`、`01M5TAD1YJP8SD7X4P1Z0BB1A8`、`01M5TAMTY33662876BFB9WM2SK`，三段均有真实 ASR、模型回复和原生播放，responded/drained 为 true，恢复 listening。`ios-final-language-20261026.log` 为 1 passed；`a02-final26-acceptance.json` 绑定三轮保存回执、浏览器会话、截图及逐通转录。测试后已保存恢复中文，context version 265。模拟器只替换麦克风采集，不声称物理麦克风或 CallKit 已通过。

性能继续按失败记录推进，未放宽阈值：先前 `dispatch-perf-authority-20261026-assessed.json` 的偏好路径 P95 556.52 ms 仍失败。随后把来源会话、被移除消息和原始 Part 的独立查询合并；保留用户归属、会话有效性、删除及内容哈希校验，结果仅存在于一次授权检查中。又将历史种子版本检查合成一次有界查询，并将空闲语音的当前权限与设置、版本、撤回时钟合在同一 SQL 快照。

相关结果：`source-batch-regression-20261026.log` 28 passed；`source-batch-postgres-verified-20261026.log` 11 passed；`canonical-seed-regression-20261026.log` 28 passed；`authority-snapshot-directed-20261026.log` 65 passed、13 skipped；`authority-snapshot-postgres-20261026.log` 4 passed。跳过项不计为通过；文件之间重复用例不相加。修正前的查询计数和 PostgreSQL 夹具外键失败日志保留，最终测试使用真实存在的其他用户验证撤权。

通话设置消费者的同场景 30 次配对结果 `voice-update-snapshot-20261026.json`：旧版内部 P95 6.985 ms，新版 3.843 ms，下降 44.98%，30 次均实际验证新称呼已进入提供商指令，满足 PERF02 此消费者路径相对门槛。此口径从已提交设置后的消费者入口到提供商 ACK，使用真实 SQL 与即时提供商替身，不含事件传递/轮询或物理音频耗时；真实提交到应用 ACK 的五次结果仍见 PERF03 原始证据。此前 9.739/9.686 ms 两轮相对失败均保留。文字路径新配对另行追加，当前仍不签署整体上线。

### 10 月 26 日 04:38 追加：固定性能、最终语义矩阵与现场缺陷

证据目录为 `.local-dev/memory-release-20261025/`；本轮没有更改 schema，没有提交、推送或部署业务代码。

- `dispatch-perf-snapshot-20261026-assessed.json`：基线 a95a302 与新实现使用同范围独立数据库快照、固定外部即时替身、相同启动与缓存条件；250 个请求全部保留。每条路径每版本 30 个温请求、另有各 5 个进程首请求。HTTP 进入到最终回答模型就绪包含按需 memory 工具及第二次主模型准备，仅扣除已观测外部等待。新版本问候/偏好/普通事实/知识的内部 P95 分别为 216.39/491.45/253.05/513.25 ms，前三者通过 500 ms 门槛；知识对比 649.64 ms，减少 136.39 ms（20.99%）。必要偏好和知识均 30/30 保留，简单问题没有额外历史。旧失败测量保留，未丢弃异常值。
- `voice-update-snapshot-20261026.json`：每版本 30 个固定温样本，真实 SQL、提供商即时 ACK 替身；消费者开始刷新到 ACK 的 P95 为旧 6.985 / 新 3.843 ms。此数字不包括事件传播、轮询、物理音频，不能冒充完整通话延迟；完整实时设置传播仍以已有真实五次证据为准。
- `dialogue-release60-20261026.json` 及 `-assessed.json`：最终原冻结 60 例真实模型复测，逐条人工核对答案，60/60 符合当前语义判据，20/20 必要资料可用，11 个明确受限输入没有外发历史，最大记忆块 2042 tokens。状态为 41 partial、15 deferred、3 restricted、1 complete；部分/延后读取仍如实呈现，不能说所有来源已全部读完。旧 58/60 失败批次及后续新表达回归均保留。
- 城南店现场暴露 `tasks.followup` 的确认续接缺口：第一次确认后，模型把展示卡的文件说明追加到了原始 text，造成摘要变化和第二张确认卡。原请求、两次工具输入、双重文件说明及取消记录保留在 `a17final26-city-repeat-confirm.json`。已按当前卡片回答来源绑定原始工具/参数，在任何第二次提问前拒绝漂移；不自动执行或替换参数，不放宽目标、附件或权限校验。正常新用户输入不会沿用旧绑定。`send-replay-regression-20261026.log` 为 58 passed，初次失败含测试前置条件和过时私有函数引用，原日志保留。
- 语音仅加强会话级身份文字未奏效，保留 `voice-a17final26-city-initial.json`。进一步将当前表达策略绑定每一次原生回答，后台播报的表达策略放在最后；Audio 只增加短暂表达控制，不重复整包记忆，结束或取消后退役。`voice-response-policy-regression-20261026.log` 为 71 passed。重启后 `voice-a17final26-response-policy.json`：开场“林姐”，随后兴趣建议、自然光解释、阴天追问均没有重复称呼，短答和实际已存偏好匹配；模拟器三段音频均 responded/drained、回到 listening。后台通知和完整换图仍继续现场检查。

以上数字与功能证据只关闭对应条目。A17 独立完整现场结束前，发布结论仍为未全部达标。


## 二十九 2026-10-26 05:40：附件、旧方案基线与退出可靠性

本段继续关闭 A17/A01，尚未签署整体上线。全部证据位于 Git 忽略的 `.local-dev/memory-release-20261025/`。

### 已修复及定向验证

- 语音通过 `tasks.followup` 继续原任务时，旧待审卡可能已经被用户上传照片的消息退役，原先只保护 `requests.revise` 的校验因此失效。现在新输入入队时在会话锁内绑定真实旧卡及摘要；执行时重新校验所有者、原文、代发回执及当前运行，再约束未要求修改的内容。绑定不等于批准，幂等重放不换基线。`followup-baseline-20261026.log` 34 passed；独立 PostgreSQL `review-baseline-postgres-20261026.log` 44 passed，批次重叠不累加。
- 附件记忆补读返回了原始 datetime，下一次请求重新核验证据时 JSON 哈希抛异常。现统一输出带时区的 ISO 时间；真实 SQL 测试同时证明原证据可复核、描述改动会使旧结果失效。`asset-reader-postgres-20261026.log` 1 passed；`lifecycle-assets-voice-focused-20261026.log` 54 passed。
- 通话尚未有第一句用户输入时，后台通知没有记忆包，漏掉了“不重复称呼”的表达规则。现没有记忆包时仍执行当前通话的表达策略；原生延迟确认也只报告收到请求，不猜旧任务进度。`voice-before-input-policy-20261026.log` 28 passed，`voice-final-receipt-20261026.log` 80 passed。
- 发现旧本地服务进程退出 HTTP 后仍持续运行恢复工作者，并抢走了两次故障后的待处理输入。该轮不可作为最新代码验收。旧进程已退役；退出改为同时通知所有后台消费者停止，排空后才关闭连接池，并记录每项停止进度。`lifecycle-final-isolated-20261026.log` 17 passed，包含一个后台停止被阻塞时恢复服务仍收到停止请求的真实 lifespan 编排测试。先前三次生命周期测试因误启动真实 SQLite 后台工作者挂起/被中断，日志保留，不记为通过；fixture 现隔离工作目录并关闭与轨迹测试无关的工作者。实际服务停止复核还须在本轮收尾完成。

### 已完成的真实现场

- 附件失败重试：只保留当前服务进程，对小桥原会话精确注入三次临时投递失败；网页显示附件未送达，点击一次“重新生成”后真实照片送达并得到识别结果。旧输入 `inbox_01M5TFHBXPCF2BRS96P90M1SSR` 保留 `delivery_error` 与 3 次尝试；新输入 `inbox_01M5TFJYX7SSSYYXNTZHSXJ101` 成功，仍是同一 task 和 `asset_01M5TFGZRT4S619CPDNT8JBEQZ`。`a17final26-retry-final-delivered.json`、`retry-three-faults-20261026.png`、`retry-delivered-final-20261026.png`。回答将“先别做”表述为“已暂停制作”，仍需核对执行状态表达，不能把遵从暂缓当成外部停止动作的证明。
- 林间果铺：新建普通会话、先生成无图待审方案、网页发送照片、iOS PCM 模拟麦克风自然说换图并保留其他要求、网页只确认一次代发。原卡 `01M5TFHEKRRR2KZWQQDQ1B9T48` 为 superseded 且未作答，新卡 `01M5TFXA9NJKJ67XVXAA1RKBJT` pending 且未作答；同一原会话 `session_7YBT5GFTAMD0KG8JY9B7JYSQ17` 的代发输入有真实成功回执。新卡完整保留四句字幕、4/4/4/3 秒分镜、木吉他/尤克里里音乐及无人要求，画面换成实际照片内容，没有执行视频生成。`a17final26-forest-final.json`、`forest-final-review-20261026.png`、`voice-a17final26-forest-final.json`。
- 首次通知：新通话 `01M5TFFJ98ABW3QT0DPE8STVVP` 开场称呼林姐，用户尚未发言时播报林间新方案没有重复称呼；后续原生答复和任务播报也未重复。`voice-a17final26-first-notice-fixed.json`。这是实际 ASR/提供商音频/原生播放，只有麦克风输入被替换为 PCM，不扩大成物理麦克风或 CallKit 验收。

第二、三条独立改图及最终收尾仍在执行。未新增数据库迁移，未操作生产会员或云生命周期，未修改网关，未部署。

## 三十 2026-10-26：历史复核默认值与来源授权

本节继续使用本地 QA 和开发无影，证据目录不变。没有扩大测试范围，也没有覆盖前面的失败记录。

- 青禾案例第一次失败：模型把任务会话中的历史人类消息和个人助理本轮人类消息混合作为代发授权。服务端正确拒绝，但错误回执没有区分有效、无效引用，模型重复读取历史。现在回执列出原请求中可验证和不可用的 ID，并明确 `command_applied=false`；工具契约说明历史只作背景，不能用其他会话消息替代当前私有助理中的操作授权。任何无效来源仍整笔拒绝，不自动删除来源后执行。`source-authority-final-20261026.log` 48 passed；独立 PostgreSQL `source-authority-postgres-20261026.log` 3 passed。最初夹具失败原件保留。
- 第二次失败定位到参数默认值：`history.read` 实际执行经过公开 Schema，默认 `include_tools=false`；当模型省略该字段时，复核原始 JSON 却采用底层读取函数的 `true` 默认值。同一份新鲜历史被误判变化，模型反复读取直至预算耗尽。现在所有助理读取复核先用原工具 Schema 校验和补齐默认值，再重新检查权限、版本与实际结果。没有缓存权限、放宽预算或跳过实时复核。省略字段的红测失败、显式 false/true 对照成功保留在 `read-defaults-reproduced-v2-20261026.log`；最终相关 51 passed，独立 PostgreSQL 11 passed，见 `read-defaults-final-20261026.log`、`read-defaults-postgres-20261026.log`。源内容变化仍使旧结果失效。
- 修复后沿用同一段自然 PCM 重测青禾：语音交给原任务，一张代发确认卡，旧剧本卡 `01M5TFX3E6KXYN99GZREK5E247` superseded 且未作答，新卡 `01M5THCHAHJHNBMH8VA9WW3RQW` pending 且未作答。新稿保持三条原字幕、三段音乐及 3×5 秒/9:16/无人要求，画面改为实际上传照片；未生成视频。成功输入 `inbox_01M5THB0V3V03FNSFGV5ZMNGEN` 仍对应原 task `01M5TG8MNCY9Y6D1CF91H0T8MG`。`a17final26-orchard-final.json`、`orchard-final-review-20261026.png`、`voice-a17final26-orchard-final.json`。早期两次失败单独保留为 source-failure/defaults-failure，不计作成功。
- 真实退出已补证：进程 73121 的 16 项后台停止全部完成，应用 shutdown 阶段 160 ms，进程消失；随后进程 77453 的 SIGTERM 到退出观测 421.7 ms。证据为 `backend-shutdown-observation-20261026.json`、`backend-shutdown-source-20261026.json` 及对应日志，不再以单元测试替代实际退出。
- 实际本地 ARM64 Docker 镜像已构建，`docker-build-final-20261026.log` 成功；断网容器能加载固定词表并构造 4 个浏览器恢复文件，见 `docker-offline-smoke-20261026.log`。该镜像包含历史复核修复，后续状态表述改动另建镜像绑定；本地 ARM64 不冒充阿里云 AMD64 已部署验收。
- 普通任务中“先别开始”仍被答成“已完全暂停”，此失败截图为 `wait-state-before-20261026.png`。只读回执不能证明执行过停止；已加强读取计划对整句行动限制的语义说明，以及只读回执的执行边界。`action-receipt-targeted-20261026.log` 59 passed。新增冻结 8 条未见表达和原浏览器问题尚待真实模型复测，不以单元测试关闭此缺陷。

橙光未发送照片的第三组独立现场及最终签署仍在进行。数据库仍为 `ph84a5b6c7d8`，本增量没有新迁移；既有兼容功能回滚证据不等于旧 main 二进制回滚。

## 三十一 2026-10-26 最终验收与灰度交付

**结论：本次记忆 R1/R2 在已冻结及用户明确限定的范围内，通过功能与性能验收，可以准备小范围灰度；本轮没有提交、推送或发布。** 前面各日期的失败结论、错误回答和异常日志继续保留。本结论基于修复后相同操作的复测及新增独立表达，不要求反复改提示词取得成功，也不声称任意用户表达、任意供应商或生产环境永不出错。

证据主目录为 `.local-dev/memory-release-20261025/`，既有证据在 `.local-dev/memory-upgrade-20261021/`、`.local-dev/memory-final-20261025/`、`.local-dev/memory-wuying-20261024/`。全部为忽略目录中的本地 QA 数据。`release-closure-20261026.json` 绑定基线、当前工作树文件摘要、schema、镜像及关键证据；工作树未提交，不能只用基线 commit 代表已验收代码。

### 最后两个现场缺陷已关闭

**“先别做”不是“已经暂停”。** 原浏览器自然输入不变：“照片你先帮我看看有哪些水果，视频先不要开始做，等我再说。”修复前答“视频制作已完全暂停”，实际只有只读工具。修复后先列出照片水果，再明确“视频制作尚未启动……不会开始做，一切等您通知再说”；个人助理和实际语音通知也表述为暂未启动。读取计划理解完整的询问与行动限制，只读回执明确 `operation=read_only` / `state_changes=[]`，执行完成仍须对应动作回执。证据：`wait-state-before-20261026.png`、`wait-state-fixed-20261026.png`、`a17final26-wait-state-fixed.json`。

修复前冻结的独立 8 条未见表达全部经真实模型 API 回答并逐条人工核对，8/8 满足答案与执行边界，原资料/任务状态未变。只使用记忆准备或最多一次精读，没有执行工具，没有假报已暂停/停止；文件名、20 秒总长、每镜头 5 秒及设备要求正确。第 6 条为普通竖屏知识，结果是 deferred/uncertain、零历史读取，行为通过不等于分类为 current_only。证据：`dialogue-wait8-manifest.json`、`dialogue-wait8-20261026.json`、`dialogue-wait8-20261026-assessed.json`。这 8 条是 API 语义验收，原问题另有内置浏览器复测，二者不混称。

**用户确认后，参数格式异常不再误报卡片已变。** 橙光第一轮确认后，模型把整份工具参数多包了一层 `arguments` 字符串；授权钩子比工具 Schema 校验更早运行，把缺少顶层目标/版本误报为目标发生变化。修复后仅对当前已确认、尚未消费的修订返回 `ASSISTANT_CONFIRMATION_INPUT_CHANGED`，附原始不可变参数让模型按公开 Schema 重提。没有自动解包执行、改写用户批准内容或跳过权限/版本/来源校验。错目标、错版本和多包一层都不会产生动作；正确重提仍执行完整检查。原失败在 `a17final26-orange-revision-failure.json` 和 `orange-revision-failure-20261026.png`。

定向红测在 `revision-format-red-20261026.log`。第一修复批次为 34 passed、1 failed，剩余项是测试对 JSON 字段顺序作了错误字符串假定；改成解析对象比较并实际使用纠正回执后，该节点 1 passed（`revision-format-recovered-20261026.log`）。独立 PostgreSQL 同节点另为 1 passed（`revision-format-postgres-20261026.log`）。不能将这几个日志改写成一次无失败的 35 项批次。

### 三组独立原任务换图闭环

全部先用普通网页手动建无图待审视频方案，再用真实图片和日常语音要求改用实拍，保留字幕、音乐、时长和无人要求。PCM 经过真实 ASR、语音提供商及原生播放；网页实际点击一次代发确认。每组旧卡都 superseded 且没有答案，新卡都 pending 且没有答案，没有点击“认可剧本”或运行付费媒体生成。不是三组首次都成功；青禾和橙光的修复前失败保留，最终使用相同音频重测。

| 案例 | 最终结果 | 证据 |
| --- | --- | --- |
| 林间果铺 | 图片已发送后修改原会话；四句字幕、四段音乐及 4/4/4/3 秒保持；使用正确照片，单次代发确认 | `a17final26-forest-final.json`、`forest-final-review-20261026.png`、`voice-a17final26-forest-final.json` |
| 青禾果坊 | 历史默认值与授权来源修复后，同一原 task 成功续接；三句字幕、三段音乐及 3×5 秒保持，无历史反复读取循环 | `a17final26-orchard-final.json`、`orchard-final-review-20261026.png`、`voice-a17final26-orchard-final.json` |
| 橙光鲜果 | 照片仅在输入框暂存、未点发送；助手在确认后将实际 asset 附回原会话，并原子关联原会话任务；四句字幕、音乐、尾板及 3/4/4/4 秒保持，无重复创建会话 | `a17final26-orange-final.json`、`orange-final-review-20261026.png`、`voice-a17final26-orange-final.json` |

统一核验索引为 `a17final26-three-cases-assessed.json`。三组语音开场按设置称呼林姐，随后回复与任务播报不重复称呼；林间还覆盖第一次用户发言之前收到后台通知的边界。音频传输测试通过与业务审核通过分别取证，不能用 `responded=true` 代替卡片及内容核验。

### 冻结门槛的证据映射

| 门槛 | 本轮结论与证据范围 |
| --- | --- |
| A01–A06：设置、称呼、主动偏好、临时与项目范围 | 浏览器、设置/权限定向回归及原生音频通过；A02 三轮完整保存→新文字→新电话在 `a02-final26-acceptance.json`；A04 临时中文、下一新通话恢复英语在第二十节；A01 最终语音及通知见本节与第二十九节 |
| A07–A12：来源、指代、提案、回执、乱序与暂停保存 | source-only 提取、主体/范围核验、真实 SQL CAS/幂等及反例回归见第十、十八、二十四、二十六节；真实设置/遗忘回执与 API 状态相符。后续来源授权 48 项及 PostgreSQL 3 项另见第三十节 |
| A13–A16：实时同步、隔离、手建任务与卡片 | `voice-order-assessed.json` 与三周期原始事件证明新增/修改/遗忘及 ASR/响应两种先后时序；手建任务与 A17 三组均被语音识别；无关卡片播放现场 `a16-unrelated-live-window.json` 含点击后 208 帧、无 playback.clear，相关卡回答不会重复催同一卡，详见第十八、二十四、二十五节 |
| A17：新需求不被旧 ask 锁住 | 本节三组最终现场通过；旧卡未被误答，代发有真实成功回执；三次附件故障后单次 UI 重试恢复同一任务见第二十九节 |
| RCL01–RCL08；JEV01–JEV08 的当前读取契约适用项 | `dialogue-release60-20261026-assessed.json` 60/60 人工答案审查、20/20 必要资料、11 个受限输入零历史；来源修复后独立 10 例及本节独立 8 例分别通过。程序侧严格 Schema、限制、取消、撤权及缓存身份定向回归见第十八至三十节。未启用 JEV，不以 Qwen/主对话成绩证明 JEV 或双重重排有效 |
| K01–K08：历史、文件、Wiki、附件与遗忘 | 实际浏览器上传、跨会话完整文件名和数字、真实音频精读见第十九、二十、二十六节；来源版本、Wiki 支持证据、权限、分页与删除回放定向证据见第十至十八节；A17 新图片及描述识别见本节。语义有限样本通过不代表任意图片识别绝对准确 |
| E01–E04 的记忆范围 | 第二十二、二十三节：真实反馈自动形成候选、人工记录验证并启用、修改后立即失效/重验、私有账号边界及开发无影新任务复用。仅指定开发实例；会员到期续费和生产云生命周期按用户要求不执行、不记为通过 |
| F01–F07：恢复、失效及既有业务保护 | 有关回执/恢复/撤回/权限/提供商 ACK 的定向故障及竞态见第十、十七、二十三至三十节；本轮另有三次附件失败恢复、真实服务退出、原确认参数拒绝/重提。测试跳过项不计通过 |
| MIG01–MIG04 | 隔离 PostgreSQL 与 SQLite 已实际升级、重复执行、空表降级、非空拒绝损坏性降级；保留 schema 的兼容后端关闭/重开功能后新旧数据一致，见 `migration-rehearsal.json` / `feature-rollback.json`。最终只读核对 head 为 `ph84a5b6c7d8`；本增量无新迁移 |
| PERF01–PERF04 | 同快照 250 个固定配对请求满足原阈值；消费者 30 次配对满足相对门槛；5 次真实提交到提供商确认均小于 2 秒。60 例最大新增记忆包 2042 tokens，必要精读保持原 4096 上限。完整主模型上下文仍包含历史/工具，不能称只有 2042 tokens。数值、计算边界及失败原件见第二十五、二十八节 |
| 移动端与构建 | Android/iOS 页面、聊天恢复与协议定向验收见前序记录；当前 iOS 音频集成日志 `ios-read-defaults-final-20261026.log` 为 1 passed。结束后恢复普通 `lib/main.dart` 调试版，仅传 API_BASE/WEB_BASE，不传模拟麦克风或 audio-only 开关；实际显示原个人助理历史及登录状态，`ios-normal-restored-release-20261026.log/.png` |

这是一份适用项映射，不将历史不同版本/重复节点累加为测试总数。最后增量分别为历史默认值 51 项、对应 PostgreSQL 11 项；行动回执 59 项；确认恢复 34 项与修正节点 1 项及 PostgreSQL 1 项；生命周期 17 项等，原日志均保留。未运行全量测试。

### 发布配置、迁移与回退

1. **发布同一个快照。** API、提取、索引、恢复工作者使用同版本，先退役旧消费者，不能仅靠端口释放判定旧进程已退出。实际退出证据 `backend-shutdown-observation-20261026.json` / `-source-20261026.json` / `-defaults-20261026.json`：前一轮应用 shutdown 为 160 ms，后两轮 SIGTERM 到进程退出观测为 421.7 ms 和 4371.3 ms；口径不混用。最后一轮包含活动通话收尾。
2. **先备份、后扩展。** 按第六节记录当前生产 head 与备份，再执行 Alembic 至 `ph84a5b6c7d8`，核对表、引用、行数及 `/health`。本轮没有触碰生产库。非空新增表不执行有损 downgrade；不把隔离库快照覆盖正在写入的库。
3. **灰度明确限制账号。** `MEMORY_ALLOWED_USER_IDS` 必须为批准的有限账号集合；本地 QA 空列表表示不限本地用户，不可原样复制。使用本轮已验收的 `MEMORY_V2_WRITE`、`MEMORY_AUTO_EXTRACT`、`MEMORY_INDEX_SYNC`、`MEMORY_RETRIEVAL_V2`、`MEMORY_ROUTE_JEV`、`MEMORY_SEMANTIC_RECALL`、`MEMORY_LAZY_READ_PLANNING`、`MEMORY_RERANK`、`MEMORY_WIKI`、`MEMORY_EXPERIENCE_LEARNING`；均显式 true，`MEMORY_BACKFILL=false`，不默认扫历史。`MEMORY_ROUTE_JEV` 是历史开关名，不表示实际调用 JEV。
4. **模型与预算不漂移。** 当前语音读取判断/后台提取为已有官方百炼 Qwen；文字按需计划由主对话模型提出，服务端校验后读取，不再为每句额外调用 Qwen。Qwen provider 显式绑定 bailian；JEV 仍未启用。保持 512/2048/4096 token 预算、36 个合并候选/8 个召回项，以及原 1 秒检索、5 秒精读截止。不会为了通过测试放大超时。
5. **性能条件与监控。** 本轮配对性能启用 `OPENBOX_FREEZE_STARTUP_HEAP=1`、按需工具暴露及烘焙词表。上线灰度应同时观察 RSS、GC、队列与内部 P95；短时基准不证明长期内存增长不存在。最新本地 ARM64 镜像 `openbox-backend:memory-release-20261026` 构建成功，断网容器可读固定词表及 4 个浏览器恢复资产，4 个最后修复的源码摘要与工作树匹配，见 `docker-release-build-20261026.log`、`docker-release-offline-20261026.json`。尚未构建/部署阿里云 AMD64 版本。
6. **停止和兼容回退。** 发现越权、已删内容复活、错误保存回执、重复执行或数据损坏立即停止扩大灰度，关闭受影响的新写入/提取/经验功能并切回已演练的兼容构建，保留新 schema 与审计数据；撤回、权限与回执恢复仍须运行。读取问题可关闭 lazy 路径恢复兼容读取，但其历史延迟不能宣称同样满足新性能门槛。API 与后台一同切换。旧 main 二进制不包含本轮未知字段和版本保护，未验证直接回退，禁止以本报告作此保证。

### 明确未覆盖的边界

- 真机物理录音、CallKit、锁屏长期通话和商店正式包；按原规范，授权虚拟麦克风及模拟器已满足本次记忆验收，未测部分不冒充通过。
- 生产负载、阿里云 AMD64 发布和长期在线观察；本轮达到的是指定范围的小范围灰度准备标准，不是全量立即放量的证明。
- 用户已排除的网关稳定性、生产会员到期/续费与云实例生命周期；没有继续改这些线上系统。
- 完整付费视频渲染、模型积分结算：本轮验证记忆、素材识别、任务修改和确认边界，最终卡停在用户审阅，未执行媒体付费生成。
- AOCI 认知加载与索引治理：本地索引约 33.2 万 tokens、48 块，当前没有取得完整可靠认知收据，不能声称已完成全仓认知；工程结论来自直接源码及运行证据。最终维护回执单独留证，未对齐不得填成已完成，也不擅自生成无证据的语义条目。

本地前后端保留在 `http://127.0.0.1:3002` / `http://127.0.0.1:8082`，普通 iOS 调试版已恢复；测试图片、新旧卡及失败证据保留供复核。后续发布属于单独执行动作，本报告不代替实际发布与生产冒烟记录。

## 三十二 2026-10-26：整合 main 修复与定向回归

本节只验证 main 增量与记忆实现的兼容性，不替代第三十一节的完整验收边界。当前分支为 `codex/memory-system-upgrade`，合并前为 `ebe2278c6c3b06b9f1b35fd7d674773a62a07f89`，共同基线为 `a95a302324bfea81437a1b60f2f2871bf8303753`。先逐项阅读 main 的变更、测试及发布说明，再合并 `390d3be28cbed66b90ce62e36d85c8e5141ec714`；收尾重新 fetch 后 main 未变化。证据目录为 `.local-dev/main-integration-20261026/`，`merge-inputs.json` 保留合并输入及原有未跟踪文件摘要。

### main 的六组实际变化

| 提交 | 原问题与合并后的行为 | 本轮对应验证 |
| --- | --- | --- |
| `27dfeb87` | 确认卡续接不能把 SQL 最后一条被拒绝或隐藏的用户消息当作当前轮次；改用模型真实可见的规范用户锚点 | 0/1/2 条拒绝消息回归、持久问题测试、隔离 PostgreSQL、真实网页选项确认 |
| `d0c0e956` | 无影桌面只打开本地输入状态，实际 SDK 仍不收键鼠；同时同步 SDK 和本地输入开关 | `DesktopTab` 定向组件测试；本轮未重新操作生产云桌面 |
| `7b0f3cd5` | 语音引用旧任务/卡片、同一结果重复播报、收到音频即误记已播放 | 任务版本与卡片失效、播报去重、真实 PCM 播放完成 ACK；旧 Flutter 客户端保持原降级协议 |
| `84a76b69` | 用户停止任务后，新输入被永久当作停止状态拒绝 | 仅当前用户在执行页的新输入可重开已取消任务；旧队列不重放，暂停任务仍须显式继续，运行/版本和未知副作用保护保留 |
| `66a5dc0f` | 新 IP 外观及对应音色默认值、设置保存后的缓存联动 | 34 个外观的音色映射、手选音色优先、只改模型不固化音色、设置 CAS 和既有记忆字段保留 |
| `941a9193` | 语音口头答应却漏转交、重复模型补救仍未执行、已确认视频选项丢失 | 明确交办原话持久投递一次；模型/素材选择沿用原有效确认，新素材或新费用仍重新确认 |

### 合并时修复的冲突与兼容缺陷

8 个冲突文件按两侧契约整合，保留记忆准备、撤权、当前人类原话授权和版本校验，同时接入 main 的任务快照与播放状态模块。遗漏交办在语义校验通过后立即进入原接收流程，不再等播报结束；在同一锁内复核最新发言，幂等键绑定通话、提供商会话和发言 ID。闲聊、偏好保存以及纯查询不会因为出现固定关键词而自动交给个人助理。

额外复现并修复三项兼容问题：

1. **无关任务变化取消已准备好的记忆回答。** 记忆回答未使用全局任务上下文，不能因为其他任务更新而被撤销。现在只对本次实际读取过的任务/卡片检查版本，后台任务和卡片同步继续工作；新发言清除上一句的读取标记，受限回答不混入无关私有任务。
2. **慢任务快照阻塞语音最终识别与回答。** 记忆路径已具备准备和提供商确认屏障，不再在语音结束及普通回答开始时额外等待全局任务查询。实际调用过 `tasks_overview` 时仍执行新鲜度核验，即使返回空列表也不能绕过；核验失败只说明无法确认当前进展。
3. **外观默认音色覆盖已配置的克隆音色。** 从未选择外观的旧账号保留已有 Audio 克隆音色；保存过外观后按外观默认，显式手选音色始终优先。普通旧部署默认值不能盖过外观。

`memory-freshness-before.log` 保留前两项修复前的 2 个失败；`backend-voice-ws-before.log` 保留克隆音色失败及旧审计字段断言失败。其余失败来自测试未同步设置 CAS 字段、回答表达策略或工具接收标记；均修正断言后复测，没有放宽产品校验。原日志保留。

### 实际定向验证

以下批次存在重复节点，**不累加为独立测试总数**。没有运行全量测试。

| 范围 | 结果与证据 |
| --- | --- |
| main 语音、报告、音色、外观 | 137 passed，`backend-core.log` |
| 确认续接、停止、恢复、视频选择和附件 | 178 passed，`backend-task.log` |
| 最新记忆上下文、刷新、撤权及全部语音 WS 集成文件 | 61 passed，`memory-ws-final.log`；覆盖最终按发言重置、慢快照及空任务结果 |
| 记忆与任务播放/卡片同步、遗漏交办 | 91 passed，`final-voice-barriers.log`；后续发言重置强化由上一行覆盖 |
| 克隆音色与 WS、撤权修复 | 42 passed，`backend-voice-final.log` |
| 设置保留与原生/记忆两种卡片状态路径 | 19 passed，`cross-feature-final.log` |
| 新隔离 PostgreSQL 数据库 | 24 passed，`postgres-targeted.log`，覆盖停止后续接、拒绝消息锚点和卡片状态事务；不在活动 QA 库运行测试清理 |
| 前端桌面、外观、语音协议和错误展示 | 初批 56 passed / 1 failed，`frontend-main.log`；失败的设置 CAS 断言修正后相关 3 文件 17 passed，`frontend-profile.log`，最后语音设置文件 6 passed，`frontend-voice-final.log` |
| 前端静态验证 | `tsc -b` 退出 0；变更 TS 文件 ESLint 的一个测试变量问题修正后复核通过，见 `frontend-types.log`、`frontend-eslint.log`、`frontend-eslint-final.log` |
| Flutter 既有客户端兼容 | 控制器、reducer、音色设置 56 passed，`flutter-compat.log`；增加“不宣告未实现的播放完成 ACK”用例后控制器文件 33 passed，`flutter-protocol-final.log` |

### 内置浏览器与持久状态验收

- **外观与记忆设置共存：** 从银发西装切到咖啡橘猫，页面自动显示男声龙安鲁风；称呼“林姐”、语言及既有偏好保留。测试自行改动的外观和回答长度已恢复。证据 `browser-appearance.png`、`browser-profile-preserved.png`。
- **普通会话偏好与确认续接：** 自然输入组织读书会，回答主动推荐安静、有文化氛围的地点；随后要求可点击选项，在独立验收页选择 Independent bookstore 并确认，原会话正常回复已选地点。数据库只有一张 answered/applied 卡，最终助手消息仍绑定正确用户锚点，无消息错误或重复提问。证据 `browser-card-resumed.png`、`browser-receipts.json`。该轮最终回答确认地点，没有直接生成开场白，因此只计确认续接通过，不计完整活动文案交付。
- **停止、刷新、重新提问：** 创建专用读书会任务，网页要求扩写，在生成过程中点击“停止生成”。刷新后任务实际为 canceled/canceled；同页发送“先不用方案了，只写一句轻松的欢迎词”，正常得到一句欢迎词。仍为同一 task，旧输入保留 aborted，新输入只有一条 succeeded，没有重新执行旧内容。证据 `stop-click-before-followup.json`、`browser-stop-followup.png`、`browser-receipts.json`。初次只读核验脚本误用 completed 状态名及未解码 JSON null，原始记录另存 `browser-receipts-raw-v1.json`；修正核验口径后五项持久状态检查均通过，未改数据库结果。

### 迁移、运行版本与交付边界

- main 本次没有新迁移，整合也没有新增 schema；本地只读 head 仍为 `ph84a5b6c7d8`。第三十一节的迁移及兼容回退证据继续适用，无需为本次合并新增降级操作。
- 浏览器验收期间发现用户也在原页面测试通话和英语偏好，因此后续使用独立标签页，保留用户通话与设置，没有再次重启其正在使用的后端。最后的语音任务快照跳过及按发言重置改动已通过最新源码的单元/WS 回归，但当前 `8082` 进程尚未加载这部分最终优化；下一次正常重启后生效。本轮网页的停止/卡片/外观路径与最终源码一致。
- 本轮未重跑真实麦克风、模拟器完整通话、生产云生命周期或网关稳定性测试；Flutter 的结果是协议与组件定向测试，不冒充真机验收。没有推送、部署或生成正式安装包。
- AOCI 仍不能作为已对齐的完整仓库认知证据；最终维护单独保存回执，不把治理待办算成本次业务回归通过。原有未跟踪 `frontend-v2/pnpm-workspace.yaml` 保留且不纳入提交。

结论：六组 main 变更完成整合，已发现的三项源码兼容缺陷修复并通过相关回归；网页核心状态流程通过。结论限于本节覆盖范围，不能把本次合并记录替代发布、生产冒烟或最终版本真实通话记录。

## 三十三 2026-10-26：重启后的真实通话与多轮卡片补测

证据目录：`.local-dev/post-merge-live-20261026/`。沿用本地 QA 账号和隔离数据库 `openbox_memory_upgrade_20261021`；没有操作生产账户、会员或无影实例生命周期。外置磁盘意外断开后恢复原 PostgreSQL 数据和本地 Redis，再启动前后端；没有重建或清空数据库。最终前后端加载当前源码，语音模拟入口已从日常前端启动方式移除。

### 发现并修复的三个问题

1. **临时口播语言被保存设置覆盖。** 保存英语时说“我们这次先用中文聊聊”，旧实现仍回复英语。现在现有语义判断请求同时给出带原话依据的语言要求，区分本次回答、本通话、恢复默认、否定和引用；不增加一次模型调用，不写长期设置，不改变读取权限。新通话、用户修改保存设置及单次回答结束各有明确清理边界。未启用语义记忆的兼容路径保留原提示约定。
2. **普通会话点完第二张卡后忘记前文。** 先选读书会地点，再选“轻松温暖”的两句开场白，旧实现回复自我介绍。规范用户消息锚点正确，缺失的是进程内的按需读取状态：续接重新进入 deferred，却保留“已准备记忆”的旧工具回执。现在从同一规范用户回合的第一条已完成准备调用恢复经过校验的语义计划，再按当前 SQL 权限和数据版本重读；不采用旧工具正文、旧权限或旧记忆包。其他会话、其他用户回合、伪装工具名、未完成/无效计划均不复用。保存设置改变、会话撤权、只读当前输入等边界有回归。
3. **手动提交答案没有明确同步给语音。** 补测时地点已选、风格已选，语音仍追问同一风格。现语义判断需要当前会话历史时，附带这个可访问会话最近两张卡的真实提交回执，明确区分已作答与后续执行完成；不再只提供待办为空。读取重新检查会话权限，过滤已移除或遗忘来源，完整项受 1600 字符和记忆包总预算限制；不增加模型判断，不允许回执授予执行权限。

### 真实浏览器、音频及持久状态证据

| 场景 | 实际结果与证据 |
| --- | --- |
| 保存英语，本通话要求中文，继续自然追问 | 最终两次 Web 通话先用英语问候；要求中文后正常中文接话、讲故事。上一通话临时语言没有带入下一通话，数据库仍为 `address=林姐`、`language=English`。`call-final-hello.json`、`call-final-weekend.json`、`call-new-call-default.json`、`call-decision-final.json`、`acceptance-summary.json` |
| 主动偏好召回与连续追问 | 通话 `01M5V2K4PBRFQMREM4PSNYF7CQ` 在没有重述偏好的情况下建议画展；追问下雨时延续室内画廊/博物馆方案。之前一次判断超时只作降级记录，不能计作召回成功。`call-final-weekend.json`、`call-final-call-before-end.json`；失败保留在 `call-web-fixed-weekend.json` |
| 说故事时手动创建普通任务会话 | 第一段故事 11:07:09.001 开始播放、11:07:27.023 客户端确认结束；人工消息在 11:07:17.606 入库，确实发生在播放期间。故事完整播放，后续能获知新任务正在等地点选择。`web-events.jsonl`、`call-final-task-created.json`、`call-final-task-aware.json` |
| 两张普通卡连续提交 | 新会话 `session_7YBT4V7E14FB1EB1VGNDB6B3EW`：选书店后得到开场白，再选轻松温暖风格后正确改成两句话，两张卡均 answered/applied，助手绑定对应人类消息。失败会话和截图保留。`card-receipts-before.json`、`card-receipts-final.json`、`card-resume-before.png`、`card-resume-after.png` |
| 播放期间点击另一任务卡片 | 通话 `01M5V4XDTA4YXHXAG3Y9NA5EJE` 故事播放 11:52:37.833—11:52:56.650，风格卡在 11:52:47.556 提交。区间内没有 `playback.clear`，客户端回传同一响应完整播放 ACK。`acceptance-summary.json` 通过时间顺序断言。较早两次点击实际发生在播放之后，不能当作并发证明 |
| 已选答案语音核对 | 使用同一段自然录音“我刚才已经选好地点了，还需要我选吗？”修复前仍追问风格；修复后明确回答“已经选好了独立书店咖啡馆，开场白也已经按轻松温暖风格准备好了”。通话 `01M5V69PK3VQ7BX5PS960R3YBY`，无重复交办，最终已结束。`call-selected-card-check.json`、`call-decision-final.json`、`call-ended.json` |
| iOS 模拟麦克风传输 | 本轮前段的 iOS 三句 PCM 测试通过真实 ASR、提供商音频和播放器 drain，无恢复/交办；`ios-live.log`、`ios-artifacts/results.json`。该次未保留逐字转写，且早于后续语言修复，因此只证明传输链路，不扩大成最终语言、真机或 CallKit 验收 |

Web 模拟只替换麦克风输入为自然语句 PCM；保留实际浏览器入口、AudioWorklet、WebSocket、ASR、模型、播放队列和 SQL。验收没有向助手发送内部工具名或指定路由提示词。模拟工具栏是未入库的本地调试脚本，不进入产品构建。

### 定向测试与性能

- 语言语义真实请求 6/6 通过，覆盖否定、引用、单次和整通电话及恢复默认；早期失败结果单独保留，最终为 `language-semantics-witness.json`。
- `resume-red-final.log`：修复前 2 failed / 7 passed；`decision-red.log`：回执同步缺失红测。初版夹具和不存在的测试路径不计作产品失败或测试通过。
- `final-targeted.log`：263 passed，覆盖读取协议、同回合恢复、语音语言、记忆刷新、规范锚点、持久卡片及已有确认恢复。该批次早于最后的卡片回执增量。
- 卡片回执增量：`decision-focused.log` 82 passed；最终相关视图、卡片与任务读取 `decision-final.log` 79 passed。语言兼容收尾 `language-compat-final.log` 55 passed。批次有重叠，不累加为独立测试总数。仅有既有 Pydantic 配置弃用警告，没有运行全量测试。
- 新读书会四次执行内部耗时分别为 **1696.7 / 1049.6 / 1441.7 / 1173.6 ms**，已扣除模型和运行时网络等待；其中两次卡片恢复为 **1049.6 / 1173.6 ms**。这是本次样本，不是生产 P95。语义计划恢复的 `provider_attempts=0`，没有再请求分类模型。原始证据为 `backend-resume.log` 和 `acceptance-summary.json`。

### 最终运行状态与边界

- 没有新增数据库迁移，本地 head 仍为 `ph84a5b6c7d8`；第三十一节的备份、扩展迁移和兼容回退要求不变。
- 关闭测试通话后正常 SIGTERM 退出：后端观测 310 ms、测试前端 107 ms。随后恢复普通前端启动器，前后端地址为 `http://127.0.0.1:3002` / `http://127.0.0.1:8082`。`shutdown-final.json`、`backend-accepted.log`、`frontend-accepted.log`。
- 本节补齐的是本地真实交互和相关回归，不宣称验证生产负载、网关稳定性、物理麦克风、锁屏或正式移动包。未推送、部署，也未做线上数据修补。
- AOCI 完整索引仍未取得可靠全量认知收据；源码工程与实测证据单独成立，收尾维护回执单独保存，不把治理未对齐写成通过。既有未跟踪 `frontend-v2/pnpm-workspace.yaml` 保留。

## 三十四 2026-10-26：语音、个人助理文字与执行会话的实时同步

本轮按用户要求停止使用 AOCI；不改生产数据、不测试生产云实例或会员生命周期。证据保存在 `.local-dev/voice-sync-20261026/`，部分早期原始转写同时保留在 `.local-dev/post-merge-live-20261026/`。沿用本地隔离 QA 账号、数据库和开发无影环境。

### 修复内容及边界

1. **执行会话被错误排除。** 语音创建的任务使用 `assistant_isolated`，此前被普通历史过滤规则排除。现在只有当前通话绑定的个人助理、同一用户和工作空间、未归档任务可以读取自己的执行会话可见内容；不改变长期记忆提取资格，用户主动设置的 `isolated`、他人会话、已删除和已遗忘来源仍不可读。
2. **个人助理文字入口没有当前页面提示。** `/app/assistant` 没有普通会话 ID，现在按通话绑定的 main 读取最近文字和已提交卡片，不能通过客户端提示重定向到别人的 main。
3. **工具步骤挤掉人工修改。** 原来先取最近四条消息，再过滤内部工具和思考；任务执行多步时，用户刚输入的修改会全部消失。现在可见历史先按可显示的文本/文件筛选，再取窗口，仍过滤移除及遗忘来源；内部工具步骤不会占用对话名额。SQL 兼容 SQLite 与 PostgreSQL。
4. **长卡片挤掉最新事实。** 压缩来源引用和旧问题说明，保留完整选项答案；超预算时优先移除完整旧消息和旧回执，再按原有权限与预算规则降级，不截断用户答案，不提高 2048 token 上限。
5. **状态未变就不刷新内容。** 即使长期记忆版本或任务状态枚举不变，当前文字、提交回执及任务正文变化也会使快照刷新；`voice.work` 的版本包含最近消息和当前设置。刷新只读本地 SQL，不增加一轮模型路由。
6. **最新待办优先。** 当前 SQL 的 `conversation_state`、`waiting_for_user` 和待确认 ID 优先于历史回复里的“正在整理/已完成”；解释待办前核对当前卡片。已提交选择不重复确认，可选建议不能说成必填缺项。
7. **界面调整。** Web 形象由 64 px 放大为 96 px，点击区域 104 px；展开面板为 480×700，首次默认右上角并保留已保存拖动位置。Flutter 收起通话条新增静音/解除静音，使用原控制器及国际化文案；按用户要求，本次不运行移动端验收或测试。

### 真实浏览器与模拟麦克风

通过内置浏览器操作，只有麦克风输入替换成自然语句 PCM；实际 AudioWorklet、WebSocket、ASR、语义模型、音频播放及 SQL 均保留。没有向产品发送工具名称或指定内部路由的提示词。

- 语音创建 `社区读书会场地选择与开场白`，人工依次选择场地、风格和通知；任务会话为 `session_7YBT4Q7J4T4QJM7C6QMRXGQXTT`。修复前误把“新建项目”当成“已选场地”，也出现点完卡片仍要求确认、文字修改后要求用户重述；失败转写均保留。
- 通话 `01M5VD4TBRQC3K97SW9GHGVTSR`：在任务聊天中改为周日下午三点的亲子活动，生成风格卡，语音主动提示新卡片；人工选“活泼亲切风”后，语音知道选择及新时间。但从个人助理文字入口改人数后立即追问仍要求重述，因此该通话只计部分通过，随后修复可见窗口筛选顺序。
- 通话 `01M5VEJ94FMY2QCAN7DX45TDP4`：人工提交活动通知卡后立即问“我刚才在页面上选好了，现在还需要我做什么？”，语音回答方案已确认采纳、该事项无其他待办。随后在个人助理完整页面输入“人数改成12人，时间改成周日下午四点”，立即模拟追问“我在文字里改了要求，读书会现在要怎么安排？”，语音准确回答 **12人、周日下午四点、小区活动室，通知正在整理**；生成结束后另行播报完成。
- 上述通话已结束，`forwarded_turns=0`：状态追问未再次转交文字助手、未创建重复任务。最终通知正文为 12 人、16:00；当时四张卡均 `answered/applied`，十二条消息无错误。保存的称呼仍为“林姐”、语言仍为 `English`，临时中文通话没有写坏长期设置。
- `call-final-visible-ended.json`、`call-live-main-immediate.json`、`card-receipts-voice-sync-final.json` 和 `web-events.jsonl` 为对应证据。最后额外的“新卡片优先于旧进度”补测另存本目录，不把早期错误转写计作成功。
- 最终补测通话 `01M5VFTXK1MGZJQSBG55X9E528`：在任务里要求增加一个破冰游戏，生成两项选择；自然问“现在还缺什么”，语音准确回答“只差您确认尾声的破冰游戏选哪个”。人工点“绘本角色猜猜看”并确认后立即再问，语音回答“不需要再做其他操作”，任务生成一句游戏说明。该通话已结束，未转交新的文字任务；最终五张卡全部 `answered/applied`、消息无错误。证据 `call-pending-state-ended.json`、`card-receipts-voice-sync-accepted.json`；`acceptance-summary.json` 九项持久状态断言通过。

### 定向回归与性能口径

- `final-regression.log`：**166 passed**，覆盖当前视图、执行会话权限、最新正文、同版本刷新、任务版本、卡片回执、读协议和语言。收尾待办状态增强为 `pending-state-final.log`：**57 passed**。有重叠，不累加为独立测试数量；没有跑全量测试。
- `frontend-targeted.log`：**23 passed**；变更前端文件 ESLint 通过。Flutter 本轮仅添加代码，没有运行测试。
- 扩大相关读协议回归时发现两条旧测试不兼容现有实现：无二次外部等待仍期待重复 SQL 校验，工具夹具缺少 `deferred` 字段。前者改为参数化校验“每次外部等待后重新授权”，后者使用真实 `ResolvedStepTools`，没有改变业务逻辑来迎合断言。中间 SQL JSON 适配及夹具断言错误均在最终回归前修正，失败日志保留。
- 最终完整联动通话三个语音包为 **2033 / 2024 / 2010 tokens**，均未超过 2048；准备总耗时 **3428 / 2472 / 3216 ms** 包含模型及网络，不能当作内部耗时。任务通知生成样本内部耗时 **893.9 ms**；个人助理修改交办样本为 **4376.1 ms**，均采用已有日志扣除外部等待后的口径。这些是本次样本，不是生产 P95，不声称已消除全部延迟。

### 运行与交付

本轮没有 schema 修改或新迁移，本地 head 为 `ph84a5b6c7d8`；既有迁移与回退演练记录继续适用。本轮没有提交到远程或发布生产；原有未跟踪 `frontend-v2/pnpm-workspace.yaml` 保留。结论限于本轮 Web 真实联动及相关回归，不能替代正式移动包、生产发布或负载验收。

最终后端运行本轮最新源码，普通前端恢复至 `http://127.0.0.1:3002`，后端为 `http://127.0.0.1:8082`；模拟麦克风脚本已从当前前端启动方式移除，浏览器保留验收任务页面。`frontend-normal-final.log`、`backend-pending-final.log` 为最终运行日志，`accepted-browser.png` 为页面截图。本轮三项需求中的 Web 联动与悬浮形象已验收，Flutter 收起静音按钮已实现并按用户要求跳过运行验证。

## 三十五 2026-10-26：独立判断回退与 Qwen 内部网关

本节记录当前分支最终配置能力，覆盖前面历史批次中的路由选择；历史测试结论仍以各批次的源码、环境和口径为准。

- JEV 原生独立判断与 Qwen provider 分开配置。JEV 不确定或超时后最多调用一次 `qwen3.8-flash`；默认判断截止分别为 3 秒、4 秒，失败后不追加第三阶段模型，也不扩大用户允许的读取范围。JEV 的 UEE 路径不因 Qwen 迁移而改变。
- Qwen 对话、提取、独立设置校验、向量、重排及实时语音支持内部网关。新增 `MEMORY_API_KEY`、`MEMORY_RERANK_PROTOCOL=standard`、`MEMORY_JSON_SCHEMA_BASE_URLS`；自定义网关缺少显式凭证时不发送官方回退密钥。语音网关配置要求清空 `VOICE_WORKSPACE_ID`，避免被重新定向到官方主机。
- 新增 `deploy/bailian-realtime-compat/`，用于修正阿里实时语音用量明细字段与当前 New API 的兼容差异。原始事件保留，固定上游和模型范围，不暴露新公网端口，不把上游异常中的认证信息传给下游。当前网关对未结算 PCM 片段仍有估算，不宣称逐 token 精确对账。

### 真实请求及页面验证

证据在 `.local-dev/qwen-internal-gateway-20261026/`，未将本地凭证、测试数据库或运行日志提交到仓库。

1. `qwen3.8-flash` / `qwen3.8-max` 严格 JSON Schema、`qwen3.7-text-embedding` 1024 维、`qwen3.7-text-rerank` 标准协议均通过内部网关真实请求。两种实时语音模型均识别合成 PCM“你好”并返回音频，测试用量明细可被网关读取。
2. 内置浏览器个人助理保存“周末放松时优先考虑做陶艺”；数据库确认该记忆为 ACTIVE / CONFIRMED，索引为 INDEXED。独立新会话自然询问最近兴趣，主动推荐陶艺，实际召回记录包含新记忆，判断、向量与重排成功。浏览器另验证语音连接与正常挂断；这次浏览器通话未输入真实人声，不能与前述直接 WebSocket 合成语音测试混为一项。
3. 生产后端所在机器到内部网关的合成请求：向量、重排、语音握手均通过。前两项单次分别为 169 / 177 ms，仅是连通性样本，不是生产 P95。
4. 网关相关记忆/配置定向测试 160 项通过，独立兼容代理及凭证边界测试 10 项通过；没有运行全量测试。Compose 配置校验和差异空白检查通过。

### 发布边界

- 网关通道及兼容容器已启用。生产业务后端仍是旧版本，尚未切换向量、重排与语音配置；已生成权限为 0600 的待发布环境文件和配置补丁，未覆盖运行配置、未重启生产后端。
- 发布这条完整记忆分支仍须执行既有 `ph84a5b6c7d8` 迁移，并保证工作者与 API 同版本；本节网关增量自身没有新增数据库迁移。已有数据时保留新 schema，不能有损降级。
- 先备份现有配置，发布本次后端支持，再合并网关环境覆盖和 provider 配置，重建容器后验收。不要把只含网关变量的待发布文件当作完整 `backend.env` 使用。
- 万相视频和 Fun-ASR 转写仍共用官方密钥；仅切换 Qwen 不能删除该共享密钥。彻底从业务后端移除阿里官方凭证还需要迁移这两项独立能力。
- 回退脚本只读预检通过，尚未实际回退线上通道；回退时先恢复业务端路由，再停用新增网关通道，保留历史计费记录。测试专用管理令牌已停用、临时 SSH 授权已撤销。

### PR 合并前补充回归

合并前定向回归发现紧预算下的回归：没有 `task_state` 的语音包仍被标记为“任务状态需要精读”，新增的降级说明使本能容纳的最新进度和人工答案超预算，随后整块被移除。现仅在确实移除任务状态时标记对应缺口；不扩大预算、不截断用户答案。测试同时覆盖任务会话与个人助理文字入口。最终迁移、JEV 回退、实时视图和网关定向回归为 81 passed；补充协议及分层回归为 43 passed / 4 skipped（需单独 PostgreSQL 环境的用例）；前端悬浮位置、工作空间事件和语音会话定向回归为 41 passed。批次有重叠，不累加计数。没有运行全量测试。
