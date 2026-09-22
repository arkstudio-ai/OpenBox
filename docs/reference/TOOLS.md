# 工具目录

当前固定注册 **51 个内置工具，分为 11 个职责领域**。唯一注册分组入口是
[`backend/tool/catalog.py`](../../backend/tool/catalog.py)，各目录的 `load_tools()` 声明本领域成员。
职责不等于权限、计费等级或可用状态。完整能力边界见 [Agent 能力架构](../architecture/AGENT_CAPABILITIES.md)。

## 内置工具

| 目录 | 数量 | 工具 ID |
|---|---:|---|
| `workspace/` 文件与执行 | 10 | `bash`、`read`、`write`、`edit`、`apply_patch`、`glob`、`grep`、`multiedit`、`view_image`、`share_file` |
| `web/` 联网 | 2 | `web_search`、`web_fetch` |
| `desktop/` 桌面与浏览器 | 4 | `computer`、`browser_mode`、`desktop_login`、`desktop_takeover` |
| `knowledge/` 技能与记忆 | 4 | `skill`、`skill_search`、`skill_manage`、`creator_context` |
| `planning/` 待办与规划 | 4 | `todo_read`、`todo_write`、`plan_enter`、`plan_exit` |
| `collaboration/` Agent 与团队 | 14 | `task`、`batch`、`agent_manage`、`team_propose`、`agent_catalog_search`、`agent_catalog_get`、`team_member_start`、`team_member_interrupt`、`team_task_create`、`team_task_update`、`team_view`、`team_message_send`、`team_wait`、`team_finish` |
| `interaction/` 用户交互 | 1 | `question` |
| `automation/` 自动化 | 1 | `cron` |
| `media/` 图片与视频 | 5 | `image_gen`、`video_generate`、`video_transcribe`、`video_compose`、`video_analyze` |
| `marketing/` 发布与营销 | 4 | `hot_trends`、`desktop_publish`、`douyin_publish`、`autopilot_run` |
| `discovery/` 工具发现与协议 | 2 | `capability_search`、`invalid` |

查看代码实际声明的分组，不连接桌面或执行工具：

```bash
cd backend
uv run python -m tool.catalog
```

一个文件可以提供多个工具：例如 `knowledge/skill_tool.py` 提供加载与搜索，
`media/video_production.py` 提供生成与转写。辅助模块跟随所属领域：文件时间记录和敏感路径
规则在 `workspace/`，视频供应商适配在 `media/`。

## 公共接口与动态工具

```text
backend/tool/
├── catalog.py          # 内置领域及只读库存
├── registry.py         # 注册、查找、平台插件协调
├── tool.py             # ToolInfo / ToolContext / ToolResult / define_tool
├── truncation.py       # 公共输出截断
├── <domain>/           # 上表中的 11 个领域
└── integrations/
    ├── mcp_tool.py
    ├── platform_plugins.py
    └── plugin_lifecycle.py
```

- `integrations/` 是接入实现，不是第 12 组固定工具。平台插件导出的工具和 MCP 目录由各自
  生命周期加载。插件应继续从 `tool.tool` 导入公共定义，不依赖内置实现的私有路径。
- 团队 MCP 的 `mcp_find_tool`、`mcp_call_tool`、`mcp_read_resource` 由受授权范围约束的
  适配层提供，不属于上述固定 51 个。
- 结构化输出工具由 `agent/structured_output.py` 按请求 schema 构造。
- 供应商原生工具搜索由 `agent/native_tool_search.py` 适配，不是额外的固定业务工具。
- 注册、Agent 选择、权限允许、模型当前可见、依赖在线分别是不同状态。

内部导入路径已从 `tool.read`、`tool.skill_tool` 等迁移为 `tool.workspace.read`、
`tool.knowledge.skill_tool` 等。持久化工具 ID、参数 schema、执行标记和公共插件接口保持不变。
新增或修改工具请按 [开发指南](../contributing/ADDING_CAPABILITIES.md)操作。

## Agent 固定能力与技能依赖

`workspace/` 的 10 个工具由 `CORE_TOOL_IDS` 统一声明，固定加入可复用 Agent 定义和新建
团队模板的委派范围。Web 单独展示“系统核心工具”，不能取消，切换预设也不会移除。
工具存在不等于操作已获授权：命令、文件修改、部署限制和运行授权仍按原有规则检查。
内部团队协调者使用独立协议工具集合，继续通过成员执行工作。

显式选择技能时，`agent_catalog/requirements.py` 合并该技能的 `allowed_tools`、
`requires_mcp`，并加入技能加载工具；Web 自动勾选并锁定依赖项，后端在编译前同样补齐。
取消所有依赖该项的技能后可以调整该项。MCP 服务级依赖固定为该服务的 `*` 范围，
执行时仍与团队已批准的 MCP 范围取交集。不能委派的工具会阻止启用，不能静默漏掉依赖。

## 主要依赖

此表说明依赖关系，不是运行健康检查结果。是否可用还取决于用户范围、授权和具体 action。

| 能力 | 主要依赖与说明 |
|---|---|
| 文件读写、搜索、`bash` | 当前 SandboxClient / Action Server；生产执行环境通常为无影 |
| `view_image`、`share_file` | 沙箱文件、OSS 传输、文件资产记录；图片还需模型支持图像输入 |
| `web_search`、`web_fetch` | 后端网络；搜索使用配置的搜索服务及实现中的回退路径 |
| `computer` | 桌面执行通道；截图交付依赖资产与 OSS 流程 |
| `browser_mode` | 偏好读写可独立完成；实际浏览器操作依赖桌面 Chrome 或用户扩展与 relay |
| `desktop_login`、`desktop_takeover` | 登录状态、平台账号和交互流程；远程打开/接管依赖桌面 |
| 技能 | 对应 Provider 可用；宿主、个人库和无影来源分别解析；技能脚本使用其执行工具 |
| 图片与视频 | 对应供应商、账号积分、资产/OSS；具体 action 可能还需沙箱和 FFmpeg |
| 发布与营销 | 平台账号或登录状态、素材、发布通道；桌面发布依赖远程浏览器 |
| Agent / 团队 | 可访问定义、模型、工具和技能范围、运行状态、任务与事件持久化 |
| MCP / 插件 | 服务连接、OAuth 或平台安装状态，以及当前 Agent/团队允许的范围 |

当前沙箱预检位于 `tool/tool.py` 与 `agent/loop.py`，工具执行和传输异常由
`agent/hooks.py` 等处理。分类目录不扩大权限，也不新增自动重试。
