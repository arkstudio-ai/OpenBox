---
name: video-production
description: 通过持久后台作业制作短视频：规划分段、审批后生成、转写质检、渲染成片。作业由平台 Worker 驱动，你只负责启动与解读。
allowed-tools: video_identity, video_project, skill_job
---

# 视频制作（v2 · 后台作业版）

> 本文件在 `SKILL_JOBS_VIDEO_WRITE` 打开后替换旧版十步流程。与旧版的
> 根本区别：**你不再等待任何外部任务**。提交即结束回合，进度由
> Job Card 和回执直接呈现给用户。

## 工作流

1. 用 `video_project` 完成创建/规划/审批（与旧版一致：脚本、分段
   计划、消费审批必须先通过）。
2. 逐段生成：`skill_job` action=start，skill=`builtin:video-production`，
   operation=`segment.generate`，input=`{"production_id": …, "segment_id": …}`。
   返回 `job_id` 与 `background=true` 后，**向用户报告 job_id 并结束
   回合**。不要循环 wait，不要重复 start——同一段的重复调用只会
   幂等复用同一个付费任务。
3. 转写质检：operation=`segment.transcribe`（无影提取音频 + STT 相似
   度判定），同样是启动即结束。
4. 渲染成片：operation=`production.render`，input=`{"production_id": …}`。
5. 查询进度只在用户主动询问时用 action=get / result；一次有界 wait
   （≤15 秒）仅当用户明确要求"等一下看看"。

## 状态解读

- `waiting_external`：供应商/无影仍在工作，平台会自己推进，无需你做任何事。
- `waiting_user`：作业在等用户输入（如提交结果不明确需人工核实），
  转述卡片里的问题即可；回答走 Job Card 或 action=resume。
- `failed` + `error_code=provider_failed`：该段生成失败，引导用户创建
  新修订后重新审批提交；**绝不**对同一修订重复 start。
- 取消用 action=cancel；供应商已成功的产物不会被销毁（cancel_race）。
