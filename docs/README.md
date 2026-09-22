# OpenBox 文档

项目入口见[中文 README](../README.zh-CN.md)与[English README](../README.md)。
本目录按文档用途组织；所有子项目 README、技能说明和历史资料都收录在[全仓目录](CATALOG.md)。

## 从任务进入

| 要做什么 | 阅读入口 |
|---|---|
| 启动项目、选择配置 | [本地命令](contributing/DEV_COMMANDS.md)、[开发指南](../CONTRIBUTING.md) |
| 理解 Agent、工具、技能与团队的职责 | [能力架构](architecture/AGENT_CAPABILITIES.md)、[工具目录](reference/TOOLS.md)、[技能目录](reference/SKILLS.md) |
| 增加或调整能力 | [新增能力](contributing/ADDING_CAPABILITIES.md)、[后端 README](../backend/README.md) |
| 开发 Web / 接入移动客户端 | [Web README](../frontend-v2/README.md)、[移动 README](../mobile/README.md)、[API 参考](reference/API_INTERFACES.md)、[团队接口](reference/AGENT_TEAM_API_HANDOFF.md) |
| 排查桌面、连接与执行异常 | [无影](operations/WUYING_SANDBOX.md)、[团队恢复](operations/AGENT_TEAM_OPERATIONS.md)、[执行环境](../container/README.md) |
| 发布、计费、SSO、通知配置 | [运维目录](operations/README.md)、[部署文件](../deploy/README.md) |
| 查方案、验收或过去发布 | [方案](plans/README.md)、[验收与发布](reports/README.md)、[模型评测](evaluations/README.md) |

## 分类与状态

| 目录 | 维护范围 |
|---|---|
| [architecture/](architecture/README.md) | 当前架构、职责和生命周期；提案中的目标不自动成为当前事实 |
| [reference/](reference/README.md) | 接口、工具、技能、协议与字段约定 |
| [operations/](operations/README.md) | 部署、配置、排障与恢复 |
| [contributing/](contributing/README.md) | 开发、能力扩展、文档维护与操作边界 |
| [plans/](plans/README.md) | 设计、实施计划和执行单；完成状态按正文与实现记录判断 |
| [reports/](reports/README.md) | 带时间与范围的验收、实现、发布记录；不作为实时环境状态 |
| [research/](research/README.md) | 源码对比、选型与调研，结论受调研时点限制 |
| [archive/](archive/README.md) | 已退役、早期或搁置设计，保留决策背景 |
| [trajectory-rearch/](trajectory-rearch/README.md) | 轨迹改造专题，保留既有工作包、协议和证据路径 |
| [evaluations/](evaluations/README.md) | 冻结评测协议、抽样和结果数据 |
| [evidence/](evidence/README.md)、[spikes/](spikes/README.md)、[mockups/](mockups/README.md) | 原始证据、探测实验和交互草稿 |

## 维护约定

以源代码、配置示例和明确记录的验证结果核对文档，不将旧部署标签、价格或测试数量写成永久有效的结论。
私有凭据、本地环境文件与生成缓存不纳入公共索引。运行时 `SKILL.md` 和第三方资料保留约定位置。

目录迁移可查[旧路径映射](moved-documents.json)。新增或移动文档后按
[文档维护规则](contributing/DOCUMENTATION.md)更新索引并执行链接检查。
