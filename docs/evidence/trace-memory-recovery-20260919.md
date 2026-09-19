# Trace 内存修复与隔离数据恢复（2026-09-19）

本次修复对应 [初始现场记录](trace-incident-20260919.md)，生产主机为上海 gw2，ECS `i-uf66pcsepxpc23v5qsts`。时间均为北京时间。

此前 PR #52 已修复 checkpoint 413 后的前端事件分页回退，并缩小已知素材的录制输入；这些代码确实已上线。本次另一个问题是后台构建快照时展开整个会话、序列化全部 JSON 再哈希，在共用进程中产生远超压缩体积的内存峰值。现场确认 worker 被容器 OOM killer 杀死 81 次。没有 OOM 瞬间的 Python 分配栈，因此不将某一行认定为唯一触发源。

## 实施的修复

- PR [#55](https://github.com/arkstudio-ai/OpenBox/pull/55)，修复提交 `23054429`，合入 `main@3fdb243a`。
- 后台快照按字节预算读取、展开记录，流式写临时文件、计算兼容的摘要，逐页压缩和上传；超限或超时仅退避快照生成。原有快照格式保持兼容。
- 共享 worker 退出不再被当作 ingest 批次损坏的证据。遗留 in-flight 标记触发持久化重试退避，不再因三次进程退出隔离正常批次；已知格式错误/明确批次失败仍按既有规则处理。
- HTTP 8 MiB 读取/响应限制、worker 1 CPU / 1 GiB 限额保持生效。没有数据库迁移。

完整 Trace 回归（含 PostgreSQL 路由、schema、真实子进程崩溃恢复）为 **1,117 passed / 3 skipped**。新增测试覆盖 canonical 摘要兼容、历史回放、大小保护、取消时文件生命周期、投影水位变化，以及约 150 MiB 快照在低于 32 MiB tracemalloc 峰值下构建。

## 发布与真实数据验证

从已合并提交的干净 `git archive` 构建 linux/amd64 镜像 `openbox-backend:20260919-trace-memory-3fdb243`。六个变更源码文件的 SHA-256 与镜像一致，业务/Trace migration heads 分别为 `d0a2c4e6f8b1` / `t0004_worker_efficiency`。

- 镜像 ID：`sha256:7863b77dc5879eb449c7632b70c524e5f1f3b1ec6a1f7db73cfb0eeeb0272284`（本机与 gw2 一致）。
- 镜像包：191,354,473 字节，SHA-256 `acfedebc380da79773148b493ac7bf0b3131470247e9d760e12861e0e71b77b9`。经私有 OSS 内网下载、校验、装载；临时中转对象已删除，gw2 发布包和旧镜像保留。本机专用回归 PostgreSQL 容器及其临时卷已移除。
- 配置与数据库备份：`/opt/openbox/backups/20260919-trace-memory-recovery/activation/`；两份 custom-format dump 均通过 `pg_restore -l`。Trace dump 25,486,153 字节，SHA-256 `fd414bab61122f8ae7540db52ef68d4c7c4479262473c6c576cdd248b3e1afe1`；业务 dump 24,811,649 字节，SHA-256 `a495f34aea8a4a8a21364963e8335cc7f94aad2b4b509eb95ce1ba178ff25c8d`。
- 发布前隔离探针：强制只读 PostgreSQL，独立容器限制 512 MiB / 1 CPU，不写快照、不启动第二个 worker。目标会话水位 16,236、3,257 条记录、184,745,020 字节展开记录，生成 50 页、最大页 4,189,925 字节；22.43 秒，峰值 RSS 165,508 KiB。摘要 `45f0d63dcaa1bd2de92bc254650d35a403fec6ebef6bd5f2e241ab03e2405bad`。
- 10:34:10 发布健康验收通过。Compose 差异断言仅允许 `trajectory-worker.image` 变化，再执行 `up -d --no-deps trajectory-worker`。backend、frontend、postgres、redis 没有替换；业务库没有恢复或写入修复数据。
- 10:34:31 快照由 11,155 推进到 **17,236**；目标会话稍后 committed/projected 均到 **18,199**。checkpoint 落后 963，小于默认生成间隔。worker 采样 190.9–244 MiB，重启数 0，健康项 writer/db/spool/blob_store 全 true。
- 发布后再次以隔离只读进程复用生产 repository 和同样的 8 MiB / 10 秒保护验证完整事件读取：header 成功，checkpoint 413 触发既有回退，分页从 500 缩小到 250，**65 页完整读到 18,199 条连续事件**，最大响应 7,233,153 字节，耗时 12.45 秒，峰值 RSS 225,736 KiB。

公网首页 200，匿名 Trace 接口按预期 401。上述内容读取验证在服务器只读诊断中完成，没有宣称完成登录后的浏览器 UI 验收。快照接口对该超大会话继续返回 413 是既有保护行为，完整回放走已经上线的分页回退。

## 隔离批次恢复

10:18 起保全 11 个 `batch_crashed` JSONL 与 11 个 reason 文件（2,887,742 字节），原件和逐文件 SHA-256 清单保存在：

`/opt/openbox/backups/20260919-trace-memory-recovery/quarantine-originals/`

恢复前再次校验全部原件哈希，冻结原 producer 的文件号 `1166,1172,1184,1199,1200,1347,1355,1363,1375,1376,1420`。只接受 5,500 条 asset.meta、4 条 session.meta 和另一个会话的 19 条事件；没有删除类控制。再次确认 19 个 event_id 均未入库。

10:37:10 将副本作为新的有序 producer `20260919000000-trace-recovery-0-23054429` 发布给单个生产 worker。只重排 spool 行号；事件 ID、内容、发生时间与元数据控制时间全部保留。元数据沿用 updated_at 判定，不以旧记录覆盖较新版本。原隔离文件与备份保留。

验收：producer 消费到 5,524（含最后一条 goodbye），goodbye=true、abandoned=false；**19/19 事件入库，内容哈希全部一致，19 个唯一序列位置**。恢复会话 committed/projected 均为 13,102。恢复清单与验收分别保存为 `recovery-published.json`、`recovery-verified.json`；没有改写已有序列或删除历史缺口标记。

回滚代码只需恢复旧 worker 镜像 pin 后单独重建 worker，不需回滚数据库；旧镜像仍有本次 OOM 路径。已补录事件是正常、去重保护的 Trace 数据，回滚代码也应保留它们及当前数据库，不能恢复发布前 dump 覆盖新数据。

云助手记录：备份 `t-sh06xhv9at3yxog`，镜像校验装载 `t-sh06xhvf5yiz1fk`，只读内存探针 `t-sh06xhvj29cnojk`，发布 `t-sh06xhvmjiady4g`，完整回放 `t-sh06xhvr5relaf4`，恢复发布 `t-sh06xhvwww4hekg`，恢复验收 `t-sh06xhw12xnwirk`。报告不含认证凭据或用户消息正文。
