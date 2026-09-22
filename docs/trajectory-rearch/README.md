# 会话轨迹改造专题

本专题保留既有工作包与证据路径，区分协议、改造前调查与阶段结果。

| 阅读目标 | 文档 |
|---|---|
| 改造规范与后续修订 | [SPEC](SPEC.md)、[容量与隔离计划](CHAT_CAPACITY_PLAN.md) |
| Wave 3 分工与集成约束 | [WAVE3](WAVE3.md) |
| 改造前源码调查 | [源码映射](maps/README.md) |
| 分阶段实施结果 | [阶段报告](reports/README.md) |
| 性能验证 | [百万事件压测](LOADTEST-2026-09-15.md)、[混合压测](LOADTEST-2026-09-15-MIXED.md) |
| 现行部署与恢复 | [Runbook](../../deploy/gw2/RUNBOOK.md)、[长输入恢复](../operations/TRACE-LARGE-INPUT-REPLAY.md) |
| 客户端接口 | [轨迹协议](../reference/SESSION_TRAJECTORY_PROTOCOL.md)、[管理 UI 规格](../reference/SESSION_TRAJECTORY_UI_SPEC.md) |

源码映射里的行号和“当前状态”属于调研时点；以当前实现排障时，先确认文档对应的版本。
