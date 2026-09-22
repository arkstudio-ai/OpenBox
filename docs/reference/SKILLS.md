# 技能、指令与资源目录

技能是带有说明和可选辅助资源的任务方法。加载技能不会授予工具权限；实际动作仍经过工具
与服务的权限检查。完整职责图见 [Agent 能力架构](../architecture/AGENT_CAPABILITIES.md)。

## 随仓库交付的技能

以下是 `backend/.openbox/skills/` 中的 7 个技能，不代表某位用户的全部已安装技能。

| 功能 | 技能 | 位置 |
|---|---|---|
| 技能制作 | `skill-creator` | [说明](../../backend/.openbox/skills/skill-creator/SKILL.md) |
| 定时任务 | `scheduled-tasks` | [说明](../../backend/.openbox/skills/scheduled-tasks/SKILL.md) |
| 图片生成与编辑 | `imagegen` | [说明](../../backend/.openbox/skills/imagegen/SKILL.md) |
| 视频制作 | `video-production` | [说明](../../backend/.openbox/skills/video-production/SKILL.md) |
| 桌面发布 | `douyin-desktop-publish` | [说明](../../backend/.openbox/skills/douyin-desktop-publish/SKILL.md) |
| 扫码投稿与授权 | `douyin-publish` | [说明](../../backend/.openbox/skills/douyin-publish/SKILL.md) |
| 自动营销 | `marketing-autopilot` | [说明](../../backend/.openbox/skills/marketing-autopilot/SKILL.md) |

这些技能按名称保持现有可发现路径。功能领域用于目录说明和检索，不需要为分类改变技能
名称或把它们移动到工具实现目录。容器中提供的浏览器技能资源另见 `container/dev-browser/`；
是否安装和可用取决于执行环境。

## 来源与生命周期

运行时通过 `skill/provider.py` 的作用域快照解析技能：

| Provider | 来源 |
|---|---|
| `host-project` | 对应项目工作目录中的技能 |
| `personal-user-library` | 当前用户的个人技能库 |
| `wuying-scoped` | 当前用户/租户执行环境中的技能 |
| `host-builtin` | 宿主侧全局与内置技能 |

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
