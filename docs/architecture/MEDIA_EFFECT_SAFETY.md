# 图片生成与 external-effect ledger

本文对应当前 main 整合分支。旧重构分支中的 TokenSpace material、STT、视频取消
及远端 Action Server 改造未随 Agent 内核一起迁移，不能作为当前实现的保证。

`backend/agent/effect_ledger.py` 与迁移 `c6f9a1d3e5b7` 提供持久化操作及证据：

- 稳定 effect id / request hash 将重复工具调用关联至同一操作。
- intent、dispatch 和 reconciliation 使用独立 claim token、generation 与数据库时间 lease。
- 提交前记录 `submitting`；恢复查询已有结果，不重新执行未知结果的提交请求。
- 旧 claim 不能在新 owner 接管后提交结果；没有可查询证据时进入人工核对状态。
- 证据有大小与字段限制；业务结果投影可以与最终账本状态在同一事务提交。

图片生成是本次接入的适配器。输出 FileAsset 继承会话 workspace，使用稳定的 asset/OSS
标识，保留 main 的计费和 trajectory 采集。完整的 ready assets 可用于对账；同步图片
接口若响应丢失且没有可恢复的 provider handle，系统不会假设失败后再次付费提交。

现有视频、Cron、OSS 和其他服务保持 main 的实现。本分支的 generic ledger 不覆盖
这些流程，也不保证撤销已被远端接受的命令或全系统 exactly-once。
