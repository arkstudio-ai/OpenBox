# 视频完成事件续接修复（2026-10-20）

## 线上故障证据

会话 `session_7YBTMA1GNRPX5PYFPBKM5VJTN3` 的视频 `video_01M5BQMHHM953ZKZWDKR1KBRE2` 在北京时间 12:09:44 完成。恢复程序此时遇到仍在运行的 Agent，跳过续接入队；Agent 在 12:09:51 返回“仍在后台渲染”后结束。完成的视频不再进入未完成任务扫描。14:48:44 用户追问才继续获取视频、转写和质检。数据库与 `openbox.video.recovery` 日志均已核对。

## 修复

- 完成事件在 Agent 忙时仍持久写入 `followup` 收件箱；现有驱动在下一轮接续，不抢占当前任务。
- 生成结果保存为 completed 时，同一记录写入 `completion_delivery_pending`。恢复扫描补偿近 7 天带标记、未挂回会话、未接收过续接消息的结果，覆盖完成提交后进程退出或入队失败。接收后由已有 Inbox 恢复机制处理唤醒失败。
- `vjob:<job_id>` 保证每个视频只有一条续接输入；有界分页避免暂时不可投递的结果阻塞后续结果。
- 自动续接绑定视频提交前已消费的原任务输入与意图版本，保留原始证据引用，确保正式任务结果可结算并进入助理汇报队列。存在轨迹时按提交 run/generation 精确匹配；未录制轨迹时按提交前已消费的输入匹配。
- 暂停、取消、归档、删除、所有权不符或意图已被替换的任务不会因此自动重启。没有待投递标记的历史完成记录不自动补发。

## 验证

98 项本地测试通过：

```text
tests/unit/test_video_completion_delivery.py
tests/unit/test_video_resume_and_card_deadline.py
tests/unit/test_video_job_recovery.py
tests/unit/test_voice_reports.py
tests/unit/test_assistant_control.py
tests/unit/test_assistant_results.py
tests/unit/test_video_compose.py
```

覆盖真实 SQL 收件箱、忙时完成后原轮次结束、新轮次消费、任务结果结算及助理 report-only 输入交付；开启/关闭轨迹两种情况均覆盖。另覆盖入队失败补偿、唤醒失败持久性、幂等、分页与任务控制。未调用付费视频/语音模型，未做真实语音通话验收。

## 发布

镜像 `openbox-backend:20261020-video-completion-da0bef1d` 基于线上 `20261016-video-material-cae33477-r2` 构建，仅替换两个模块：

- `video/job_recovery.py`：`da0bef1d14b15520e6d6fe4fa1575ef74138c1ab47ce96c5f7b97ec04a890d29`
- `tool/video_production.py`：`c676f32d8a8b660a7a34bc4db70ba95fbdab2f2dd7f8199f56b4eef13e82fe86`

线上 `video_production.py` 含当前工作区之外的既有修复，发布保留该生产文件，仅插入完成投递标记；改动所在 `_finalize_segment` 函数与本地测试基线哈希相同。其余生产代码和配置保留。无数据库迁移。

已于北京时间 **2026-10-20 17:18:49** 完成生产 gw2 后端切换。前两次尝试分别因活跃任务、备份期间有新任务进入而在切换前退出；最终在执行租约、Driver、在途视频、活跃通话均为 0 后切换。

- 镜像 ID：`sha256:157ae5cffd13ba8552381dce92ca2c2a5e61083af1373e73b67d2c52a4e147ce`。
- 配置和两库已备份并验证 dump 目录，目录：`/opt/openbox/backups/20261020-video-completion-da0bef1d/activation-20261020T091411Z`。
- 后端 healthy、重启 0；容器内两文件哈希符合上述清单；首页、environment、Logto 配置最终均 200。
- 前端、轨迹 worker、Qdrant、PostgreSQL、Redis 容器 ID 均保持不变；前端仅 reload Nginx 以刷新上游。
- 公网首页采样均 200；environment 在 17:18:11–17:18:37 的采样为 502，17:18:39 起恢复 200。单后端切换存在约 28 秒服务断档，不宣称零停机。
- 无数据库迁移、无付费模型调用，未做真实生成与语音通话验收。
- 启动后没有发现本次视频补偿扫描报错；3 条在 10-14/10-17 已接收的历史 `vjob:` 输入因原来缺少意图版本，仍由既有恢复器报 `ASSISTANT_INTENT_UNVERIFIED`。本次为新续接修正来源绑定，没有重写这些历史输入。10-10 旧会话的 tail-repair 报错也仍存在。
- 3 条无待投递标记的历史完成视频未被误唤醒，原问题会话的已挂载视频保持原状。

证据：[发布记录](evidence/video-completion-release-20261020.json)。回滚时恢复备份中的 override，执行 `docker compose up -d --no-deps backend`，待 healthy 后 reload 前端 Nginx；无需回滚数据库。
