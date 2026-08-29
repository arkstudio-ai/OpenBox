---
name: video-production
description: 通过持久后台作业制作短视频：规划分段、审批后生成、转写质检、渲染成片。作业由平台 Worker 驱动，你只负责启动与解读。
allowed-tools: video_identity, video_project, skill_job
job-skill-keys: builtin:video-production
enabled-config-flag: skill_jobs_video_write
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
- `waiting_user`：普通问题由用户通过 Job Card 或 action=resume 回答；若卡片
  标记为 operator review（例如远端提交结果不明确），只能由运维审计入口处理，
  不要让用户猜测或要求 Agent 盲目重提。
- `failed` + `error_code=provider_failed`：该段生成失败，引导用户创建
  新修订后重新审批提交；**绝不**对同一修订重复 start。
- 取消用 action=cancel；供应商已成功的产物不会被销毁（cancel_race）。

## 无人说话的镜头（b-roll）

分段 `role="broll"` 表示这一段没有说话人 —— 空镜头、转场、特写。用它来做
「给我一段五秒的猫在草地上走」这类不含口播的内容。

- `script_text` 填**画面描述**而不是台词；它不参与"各段台词拼接需逐字等于
  已批准讲稿"的校验（全片都是 broll 时，该校验不适用）。
- prompt 不需要写固定镜头、中景、自然动作、语气 —— 这些是给说话的人用的。
  仍然必须写：全片一致的画面基底、无字幕，且正文不得出现 URL 或素材 ID。
- 不做转写质检：没有声音可听，硬跑只会得到相似度 0 的 `suspect`。
- **审批与消费闸门一条不少**：讲稿、分段、费用照常要用户确认。broll 不是
  绕开审批或付费的通道。
