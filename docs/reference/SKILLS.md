# 技能、指令与资源目录

技能是带有说明和可选辅助资源的任务方法。加载技能不会授予工具权限；实际动作仍经过工具
与服务的权限检查。完整职责图见 [Agent 能力架构](../architecture/AGENT_CAPABILITIES.md)。

## 随仓库交付的技能

所有内置技能统一位于 `backend/skill/builtins/<group>/<name>/`，由
[catalog.json](../../backend/skill/builtins/catalog.json) 登记。目前共 6 类、8 个技能，
不代表某位用户的全部已安装技能。

| 功能 | 技能 | 位置 |
|---|---|---|
| 技能制作 `authoring` | `skill-creator` | [说明](../../backend/skill/builtins/authoring/skill-creator/SKILL.md) |
| 自动化 `automation` | `scheduled-tasks` | [说明](../../backend/skill/builtins/automation/scheduled-tasks/SKILL.md) |
| 图片与视频 `media` | `imagegen`、`video-production` | [图片](../../backend/skill/builtins/media/imagegen/SKILL.md)、[视频](../../backend/skill/builtins/media/video-production/SKILL.md) |
| 内容发布 `publishing` | `douyin-desktop-publish`、`douyin-publish` | [云桌面发布](../../backend/skill/builtins/publishing/douyin-desktop-publish/SKILL.md)、[扫码投稿兜底](../../backend/skill/builtins/publishing/douyin-publish/SKILL.md) |
| 营销 `marketing` | `marketing-autopilot` | [说明](../../backend/skill/builtins/marketing/marketing-autopilot/SKILL.md) |
| 浏览器 `browser` | `dev-browser` | [说明](../../backend/skill/builtins/browser/dev-browser/SKILL.md) |

分类只改变源码归属，不改变技能名称。新增包必须登记到清单；扩展步骤与校验命令见
[内置技能维护指南](../../backend/skill/builtins/README.md)。`.openbox/skills/` 用于项目自定义，
不再承担系统内置包入口。浏览器 TypeScript 运行时代码仍在 `container/dev-browser/`，
其技能说明和参考资源已统一到上述目录。

后端按 Python 模块位置加载内置包，服务从任意目录启动、无影离线时也能列出和读取说明。
技能中心显示全部可发现技能，内置项带清单中的分类标签，并完整换行展示描述。
`/api/agent/skill` 列表与详情采用相同覆盖规则，云镜像中的旧内置副本不覆盖当前后端版本。
执行技能仍取决于相关工具、权限和服务是否可用。

## 中英文展示与稳定标识

技能的 `name` 是调用、Agent 配置、同名覆盖和历史快照的稳定标识。界面单独使用
`display_name`（每种语言最多 120 字符）与 `display_description`（最多 1000 字符）。
这两个字段都是 `zh-CN` / `en-US` 文本映射；优先使用当前语言，再使用另一语言，
最后回退到原始名称/描述。搜索同时匹配两种语言和原始标识。

内置技能的文案统一维护在 `skill/builtins/catalog.json`。个人/项目技能可在 `SKILL.md`
frontmatter 中提供，例如：

```yaml
name: report-writer
description: Write reports from supplied research.
display_name:
  zh-CN: 报告撰写
  en-US: Report writing
display_description:
  zh-CN: 整理研究资料，撰写结构清晰的报告。
  en-US: Organize research into a clearly structured report.
```

添加技能的粘贴、Git、压缩包和聊天创建入口支持填写两种语言；字段均可选，留空保留
已有文案。批量上传按文件分别填写。安装覆盖文案写入包根目录的 `openbox-display.json`，
导出/导入时随资源一起携带，不修改原始 `SKILL.md` 正文、`name` 或发现描述。
多技能包使用 `package_display_name` / `package_display_description` 描述整包，成员仍
各自保留名称、展示文案和调用引用，不把包的名称复制给每个成员。

Action Server 原生返回有界展示字段；旧版执行环境由 `skill/sandbox_display.py` 从原文
和包内文案文件补齐，按客户端与内容修订缓存。它只影响管理展示，不替换模型发现目录。
个人库将展示字段保存在现有 metadata JSON 中，发布时随版本冻结；修改草稿不会更改已发布文案。

## 来源与生命周期

运行时通过 `skill/provider.py` 的作用域快照解析技能：

| Provider | 来源 |
|---|---|
| `host-project` | 对应项目工作目录中的技能 |
| `personal-user-library` | 当前用户的个人技能库 |
| `wuying-scoped` | 当前用户/租户执行环境中的技能 |
| `host-global` | 宿主机全局自定义技能 |
| `host-builtin` | 兼容既有宿主应用项目根目录的自定义技能 |
| `builtin-package` | `skill/builtins/catalog.json` 中登记的系统内置包 |

作用域优先于 Provider rank；项目或用户安装可覆盖同名系统内置包。
已登记内置技能在无影镜像中的同名 `source=builtin` 副本由后端包替代，未知远程内置项仍保留。
同名覆盖、版本验证、过期快照、缓存和离线保留规则见
[Skill Provider 生命周期](../architecture/SKILL_PROVIDER_LIFECYCLE.md)。不要根据后端进程工作目录推断用户项目。

技能商店候选、个人草稿、已发布版本、已安装版本、当前 Agent 选择是不同概念：

- `skill/catalog.py` 管理商店候选，例如网页研究、仓库讲解、浏览器测试以及 MCP 服务条目。
  候选条目存在并不代表已安装，也不代表依赖已连接。
- `skill/user_library.py` 管理个人内容及发布流程；`skill/catalog_admin.py` 管理商店运营。
- `skill/snapshot.py` 和 `snapshot_resource.py` 固定团队执行所使用的内容与附属资源。
- `tool/knowledge/skill_tool.py` 是模型的加载/搜索入口，`skill_manage.py` 是创建/导出入口。
- 技能声明的工具名称用于说明和缺失能力提示，不能改变白名单或委派权限。

## 技能包内容

```text
<skill-name>/
├── SKILL.md            # 名称、适用场景、工作方法和必要约束
├── references/         # 按需读取的参考资料
├── scripts/            # 通过允许的执行工具运行的辅助程序
└── assets/             # 模板、样例或任务素材
```

后三个目录均可选。脚本依赖的解释器、工具、服务或执行环境仍需实际满足；读取说明不应
暗中安装服务、增加权限或开启付费操作。包校验在 `skill/package_validation.py`，资源读取
在 `skill/snapshot_resource.py`。

## 与其他内容的边界

| 对象 | 职责 | 位置 |
|---|---|---|
| 系统提示词 | 运行时通用行为与模型适配 | `agent/prompts/` |
| 项目指令 | 当前项目长期规则，如 AGENTS.md / CLAUDE.md / CONTEXT.md | `session/instruction.py` |
| 快捷命令 | 带参数的任务文本模板 | `command/command.py` |
| 用户记忆 | 资料、偏好、事实及更新流程 | `memory/` |
| 附件与资源 | 被读取、分析或交付的内容 | 资产服务、技能资源、MCP 资源 |
| Agent 配置 | 模型、角色指令、技能与工具组合、执行限制 | `agent_catalog/` |

当前没有因这次分类新增知识库/RAG 引擎、通用插件技能打包协议或新的用户可安装 Hook 系统。
相关能力应在有具体需求时接入所属服务，不能仅因为目录中出现一个名称就宣称已实现。
