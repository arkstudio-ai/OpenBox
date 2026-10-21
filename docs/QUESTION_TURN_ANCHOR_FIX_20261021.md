# 问答续接选错轮次导致任务持续阻塞（2026-10-21）

## 故障与原因

线上一个视频制作会话在回答确认问题后，持续报
`Assistant parent crosses its logical turn`。恢复任务每隔约 15 秒重试同一坏尾部，
后续输入无法执行。此前生成的四段视频仍然存在。

`run_loop` 的无租约入口曾查询 SQL 中最后一个 user 作为触发消息。
等待用户回答时，新输入可能以 `TASK_WAITING_INPUT` 被拒绝，但对应的 user 行仍保留在
SQL 审计投影中，且已从模型上下文排除。随后问答续接会将 driver 绑定到被排除的 user，
而新 assistant 的 parent 来自模型可见的上一条 user。两者属于不同逻辑轮次，
违反 canonical event fold 的严格校验。

## 修改

`backend/agent/loop.py` 在申请租约前加载 canonical model surface，并选择最后一个
非 compaction 的可见 user。此时使用 `repair_tail=False`，避免在获得恢复租约前
修复正在等待续接的问题。跨轮校验与持久化事件契约不变。

新增回归覆盖 0、1、2 条被拒绝输入，核对选中的锚点、保留的问答事件、续接后的
assistant parent 与所有本轮事件的 turn_id。

定向验证共 **96 passed**，覆盖：

- `tests/unit/test_turn_anchor_recovery.py`
- `tests/unit/test_durable_questions.py`
- `tests/unit/test_durable_question_failures.py`

未运行全量测试。

## 发布和历史会话恢复

代码提交 `27dfeb876bf9a23e9efca5bbffe92d9ced884542`。
2026-10-21 10:36:38 UTC，gw2 后端切换到
`openbox-backend:20261021-question-anchor-27dfeb87`。
在实际生产基底 `20261021-voice-address-131cb413` 上只覆盖 `agent/loop.py`，
保留其余已上线修复。无数据库迁移。

发布前验证配置和业务库、轨迹库备份，并确认无活跃执行租约、有效 driver、在途视频或通话。
门禁仅排除本次已定位的 `finalizing/error` 历史恢复标记。仅重建 backend，健康后检查并
reload Nginx；前端、worker、数据库、Redis 与 Qdrant 容器不变。

历史数据恢复先在事务中完整预演并回滚，再按精确 ID 和状态断言执行一次：

1. 唯一坏 assistant 只有一个 `step-start`，没有文本、tokens、cost、子消息或内部 part。
   两项已经成功的沙箱准备副作用保留。通过 surface archive 保存快照，并追加 canonical
   removal 事件，只移除该空回复及其唯一 part 的 SQL 读模型。
2. 三条旧意图排队请求记录为 `SUPERSEDED_BY_LATEST_INTENT`，保留取消审计；
   最新的第 8 版意图继续保留。旧发布请求不会被再次自动执行。
3. 原有 612 条 canonical 事件未改写，新增事件后 head 为 616。
   公共投影与 SQL 表面一致，模型上下文可加载。
4. 由正常维护与 inbox runner 自动推进最新请求，最终 `settled/succeeded`。
   session 为 `waiting_input`，driver 为 `idle`，新问题为 `pending`。

浏览器已确认最新请求要求的 **32 秒动漫方案**及 **24.64 积分生成费用确认卡**正常显示。
会话标题仍是最初的 50 秒需求。确认卡未代用户提交；新的付费视频生成、合成与发布尚未验收。
历史四段素材保留，未新增发布任务。

发布后 backend healthy、重启次数 0，公网 `/api/environment` 为 200。
切换期间未做连续采样，不能据此声称零中断。trajectory-worker 发布前已有的 unhealthy
状态不在本次修复范围。

## 回滚

将 compose override 中 **backend** 镜像恢复为
`openbox-backend:20261021-voice-address-131cb413`，仅执行 backend 的
`up -d --no-deps`，等待 healthy 后 `nginx -t` 并 reload 前端 Nginx。
不要覆盖其他服务的后续部署，也不要回滚已继续产生业务记录的整个数据库。
精确恢复过程的 archive 和原始事件保留，若需反向处理须另行评估当前会话状态。

机器可核对的镜像、哈希、备份路径及恢复事件见
[发布证据](evidence/question-anchor-release-20261021.json)。
