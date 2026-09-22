# 新增和整理 Agent 能力

先阅读 [能力架构](../architecture/AGENT_CAPABILITIES.md)、[工具清单](../reference/TOOLS.md)
和 [技能说明](../reference/SKILLS.md)。新增内容按职责放置，避免将工具、技能文本、运行内核
和业务服务全部堆进一个模块。

## 新增内置工具

1. 选择 `backend/tool/` 下已有功能领域。小功能放在现有模块；确实独立的实现再增加文件。
2. 使用 `tool.tool` 的 `ToolInfo`、`ToolContext`、`ToolResult`、`define_tool`。保留清晰的
   参数 schema，返回结构化错误和真实执行结果。
3. 在领域 `__init__.py` 的 `load_tools()` 中导入并返回工具。不要在模块导入时直接调用
   `register()`，不要在全局导入时连接桌面、访问付费服务或运行命令。
4. 只有新增领域才修改 `tool/catalog.py`。它负责代码分组，不负责授权、依赖探测或计费。
5. 按需更新 Agent 白名单、权限规则、团队委派策略和暴露策略；这一步必须明确评估。
   放入某个目录本身不会让 Agent 获得权限。
6. 更新工具清单与相应回归。工具 ID 用于持久化历史和授权，不能把 Python 路径迁移当成
   重命名工具 ID 的理由。

通用协议和注册路径保持为 `tool.tool`、`tool.registry`。实现导入使用职责路径，例如：

```python
from tool.tool import ToolContext, ToolResult, define_tool
from tool.workspace.read import read_tool
from tool.media.video_providers import resolve_route
```

使用类别库存查看实现，不注册外部插件或运行工具：

```bash
cd backend
uv run python -m tool.catalog
```

## 业务逻辑与依赖

工具层负责参数、调用上下文和结果映射。视频任务状态、发布路由、技能安装、账号资料等
业务规则优先放进已有领域服务。公共运行能力留在 `agent/`、`session/`、`permission/` 等目录。

依赖按 action 判断。区分“缺少配置”“没有权限”“暂时离线”和“执行结果未知”；分类标签
不能成为自动重试依据。供应商和桌面调用继续遵守当前运行资格、副作用和账号积分边界。
未明确声明可安全并行的工具，不应仅因为放入 `batch` 就并行执行。

## 技能和外部集成

- 新工作方法优先考虑技能，而不是复制一个仅描述流程的工具。技能附属脚本与资源跟随包。
- 系统内置包放 `backend/skill/builtins/<group>/<name>/`，并在该目录的 `catalog.json`
  登记；新增类别同时填写中英文标签。具体约束见[内置技能维护指南](../../backend/skill/builtins/README.md)。
  不要再向后端启动目录的 `.openbox/skills/` 或容器运行时目录散落内置 `SKILL.md`。
- 技能名称、来源和用户范围都要稳定；加载技能不会增加工具权限。
- 展示名与简介使用 `display_name` / `display_description` 的 `zh-CN`、`en-US` 映射。
  后端统一通过 `skill/display.py` 校验，Web 使用 `shared/lib/skill-display.ts` 选择语言和回退；
  不要将翻译写进 `name`、`skill_refs`、卸载/下载标识或模型发现描述。
- 平台插件通过 `tool/integrations/platform_plugins.py` 的 manifest 与生命周期加载。
  自定义工具仍使用公共 `tool.tool` 定义接口。
- MCP 工具/资源通过对应的连接与适配器进入目录，不能硬编码成新的内置工具。
- Agent、团队模板和 Cron 定义属于组合与触发配置；它们引用工具和技能，不复制实现。

## 验证

```bash
cd backend
uv sync --extra test
uv run python -m skill.builtin
uv run pytest tests/unit/test_builtin_tool_catalog.py -q
uv run pytest tests/unit/test_builtin_skills.py -q
uv run pytest tests/unit -q
```

针对工具行为变化运行对应测试；目录迁移要核对导入、monkeypatch 目标、工具 ID、schema、
权限标记、动态加载和插件生命周期。涉及服务持久化时再运行相关集成测试。无需为纯目录
迁移调用真实图片/视频付费接口或重新跑长时间模型评测。

Web 代码变更按 `frontend-v2/README.md` 执行检查。文档变更核对相对链接、代码路径和命令。
工具数量、目录表与源代码以 `tool.catalog` 为准；内置技能以 `skill/builtins/catalog.json`
为准，`skill.builtin` 校验清单与实际包是否一致。

## 文档归属

| 类型 | 放置位置 |
|---|---|
| 当前整体职责、边界和数据流 | `docs/architecture/` |
| 当前工具、技能和协议参考 | `docs/reference/` |
| 开发与扩展流程 | `docs/contributing/` |
| 现有运维/API 专题 | 保持已有路径，从 `docs/README.md` 链接 |
| 设计提案、实施记录、验收证据 | 在索引中标记用途，保留原始上下文 |
| 已退休方案 | `docs/archive/` |

历史执行手册中的阶段约束不等于当前架构约定。引用旧路径时先核对当前源码；涉及本次
工具迁移的旧文档可从其顶部提示进入新的工具目录。
