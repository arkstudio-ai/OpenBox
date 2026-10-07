# 对话轮次恢复修复发布记录

2026-09-29 17:38:53（上海时区），`main@81ab7e5c4a2ad9b03a36cd6898ef56f37fdfaa84` 已发布至阿里云 gw2，生产入口为 https://ai.bossipai.com.cn/ 。发布 tag 为 `20260929-turn-anchor-81ab7e5`。本次仅替换 backend；网页和原生移动端使用同一套后端修复，无需为此分发新的 App。

## 根因与修改

截图会话“视频制作协助”在用户中途补充输入、回答确认问题后，由新的执行租约继续。旧代码未记录新的 `turn.started`，把补充消息 ID 直接当作逻辑轮次 ID；原用户消息仍属于最初轮次，导致历史校验抛出 `assistant tail has no User turn anchor`。该异常在上一轮维护重启前已经出现。

另一个会话“制作视频请求”的旧轮次中断回复晚于新用户消息入库。旧校验按相邻消息分组，误把晚到回复当成缺少用户锚点的独立轮次。新输入还可能被旧轮次较新的回复 ID 误判为已处理。

- `session/agent_event_log.py` 根据已有用户消息继承逻辑轮次，并为恢复执行持久化 `turn.started`。对于旧版本缺少启动事件的精确异常形态，仅从更早且仍存在的用户锚点恢复关联；显式跨轮次或缺少父消息仍拒绝。
- 历史修复按逻辑轮次分组，支持不同执行租约及非相邻回复；公开消息和模型消息顺序保持原样，历史事件不重写。
- `agent/loop.py` 在判断回复完成前检查父消息，防止旧轮次回复吞掉新输入。

## 验证

后端相关回归 **272 passed，2 条既有 Pydantic 弃用警告**。覆盖多次提问恢复、中途补充、旧事件兼容、延迟中断回复、重复读取、错误父消息拒绝，以及长历史、压缩、任务接续和终止判断。测试耗时 28.95 秒，`git diff --check` 通过。

切换前用新镜像连接生产库执行只读校验，两个真实故障会话均通过轮次与父消息校验。切换后在实际生产容器使用应用恢复逻辑，并在提交前断言所有原有 Message、Part、AgentEvent 均逐项不变：

| 会话 | 消息数 | 事件数 | 处理结果 |
| --- | --- | --- | --- |
| 视频制作协助 | 22 → 22 | 166 → 166 | 兼容读取成功，无数据修改 |
| 制作视频请求 | 97 → 98 | 748 → 751 | 追加 1 条 aborted 回复及其 3 条事件 |

两个会话随后分别两次调用实际 `load_canonical_model_surface` 成功，消息和事件数量保持稳定。没有修改既有内容、工具状态或视频素材，没有调用模型、重放工具或重新提交视频生成。会话此前的错误状态属于历史执行结果，用户可刷新后继续发送消息。

## 镜像、备份与切换

- 从干净 `git archive` 在本机 Docker 构建 `linux/amd64`，镜像为 `openbox-backend:20260929-turn-anchor-81ab7e5`；镜像内两个修复文件 SHA-256 与源码一致，完整 revision 标签一致，未打入生产配置。
- Image ID：`sha256:bd821f270f4bcedb59cabe55a53d5bef21bdf8dc5d93f09bf0387c3874ab3c00`。
- 发布包 188,823,426 bytes；SHA-256：`82bedc5b556238ec5fc2188970409a36901942cc43a8ecbbf3925fea50dc5d72`。私有 OSS 中转、服务器校验并加载后，仅重建 backend。临时 OSS 对象已删除并通过 `NoSuchKey` 确认。
- 备份目录：`/opt/openbox/backups/20260929-turn-anchor-81ab7e5/activation-20260929T093623Z/`。包含配置、有效 compose、容器快照、业务和 trace 数据库 dump；两个 dump 均通过 `pg_restore --list` 与 SHA-256 校验。
- 沿用用户此前确认的立即维护重启授权；实际切换前两次检查活动会话租约、driver、视频任务均为 0。新后端 10.9 秒 healthy，并继续通过至少 120 秒的健康和 API 稳定性检查。
- 业务数据库 head 为 `d0a2c4e6f8b1`，trace 为 `t0004_worker_efficiency`，未迁移。生产配置仅改变 backend 镜像 pin；其余四个容器 ID、镜像和配置不变。

## 线上结果与限制

17:35:20–17:41:17，共 207 次公网探测：首页 69/69 为 200，两个 API 各 67/69 为 200。维护切换期间 17:36:39 和 17:36:44 的 API 探测返回 502，17:36:49 起全部恢复 200；这是单实例维护发布。

发布后五容器均 healthy，重启计数为 0、OOM 为 false；worker 的 writer/db/spool/blob_store 全部健康。后端和 worker 自切换以来无 traceback、ERROR 或本次轮次锚点错误。没有主动发起付费模型或视频请求，验证范围为实际故障历史恢复、上下文加载、回归测试和线上运行状态。

如需应急回退，恢复上述备份中的 `docker-compose.override.yml`，只重建 backend 并等待健康；旧镜像为 `openbox-backend:20260929-legal-50274cf`。数据库版本兼容，不应覆盖发布后的业务数据。回退会恢复旧轮次处理逻辑，并可能再次触发同类问题。

结构化证据见 [turn-anchor-release-20260929.json](turn-anchor-release-20260929.json)。本地详细命令、测试和探测记录保留在忽略目录 `test-results/local-dev/release-20260929-turn-anchor-81ab7e5/`，服务器发布脚本、镜像包和恢复记录保留在 `/opt/openbox/releases/20260929-turn-anchor-81ab7e5/`。
