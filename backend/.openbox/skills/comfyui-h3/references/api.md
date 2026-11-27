# 本地 OpenBox 接入和参数

在本地后端的 `backend/.env` 指定 `COMFYUI_API_URL=https://<主机>:<公网端口>` 和 `COMFYUI_API_TOKEN_FILE=<本机凭据文件的绝对路径>`。也可直接设置 `COMFYUI_API_TOKEN`，有值时优先使用。直连服务接受 `Authorization: Bearer <token>`。私有证书通过 `COMFYUI_CA_FILE` 指定，只对 ComfyUI 生效，客户端仍校验证书和主机名。浏览器登录凭据和后端 API Token 用途不同。

设置 `COMFYUI_VIDEO_FAST_TIER=true`，重启本地后端后，视频「快速」绑定 `ComfyUI-MiniMax-H3`、默认 720p。设置 `RUNNINGHUB_VIDEO_ENABLED=false` 可暂停提交新的 RunningHub 视频任务，已提交的任务仍可查询。Agent 通过模型 ID 和当前用户的素材 ID 操作。

## 双机原版工作台

在本地环境设置 `COMFYUI_WORKERS_FILE`，指向 JSON 数组。每个条目包含稳定且唯一的 `id`、工作台 HTTPS API `url` 和本机 `token_file` 路径。凭据留在各自文件中。已有单机配置供历史作业恢复。重启 OpenBox 后从模型菜单核对工作台模式。

两个工作台都使用 Hybrid B25-49、原工作台的 SelfLift 和原生音轨。普通镜头 10 步。海报 12 步并启用原图的人脸细化节点，两台机器均需安装相同的节点、LoRA、Ultralytics 运行库和 `models/ultralytics/bbox/face_yolov8m.pt`。独立图片、动作或声线参考进入原生 H3 参考节点，同一次提交可以加首尾帧引导。原版请求的提示词、首尾帧、种子、画幅和时长可直接复用。

制作时继续使用同一个模型 ID。每次 submit 后端并行读取成员队列，按执行中、等待中和本地已预约的估算工作量选机。每个作业保存执行机，按照通常的 `job_id` 查询、恢复、验收和取回结果。某台队列不可达时，新作业可以分给其余可用机器。

## 单台直连 Turbo 工作流

后端核对素材归属，经 `POST /upload/image` 上传，`POST /prompt` 提交具体工作流，`GET /history/{id}` 读取状态，`GET /view` 取回本作业的 MP4 并存入 OpenBox 资源。每个作业记录提交时的服务标识，查询和入库使用同一个认证端点。

| 输入 | 直连服务的实际工作流 |
|---|---|
| 纯文字、单独首帧或尾帧、首尾帧 | FL2VA INT8 + FL2V Turbo 8-step v1.0 LoRA，8 步。`MiniMaxH3ImageToVideo` 接收按角色选定的图 |
| 独立参考图、视频或音频，可同时指定首尾帧 | Ref2VA INT8 + Ref2V Turbo 4-step v0.1 LoRA，4 步。`MiniMaxH3TimelinePlanner` 和 `MiniMaxH3TimelineEncoder` 读取参考；有首尾帧时，`MiniMaxH3AddGuide` 分别接在第 0 帧与末帧（−1） |
| 绑定画廊素材包的模板，普通镜头及其重生成 | 后端固定 Hybrid B25-49 INT8、DMAD 0.8 和 SelfLift 10 步（6+4）。输入只有首尾帧时使用图生视频条件，带参考图、视频或音频时使用时间线条件并叠加所需首尾帧。回执中的 `hybrid` 由后端选定 |
| `h3_profile="poster"` | 单镜头采用 Hybrid + DMAD 0.8、SelfLift 12 步（6+6）、低分辨率倍率 0.6，电影感 LoRA 0.5、写实人物 LoRA 0.8。使用原生 VAE 解码，直连服务未安装旧工作台的专有人脸后处理 |

两个标准 Turbo 模式都按加速图使用 Euler、simple、CFG 1 及视频/音频偏移 12/3。SOL 稀疏注意力按图中阈值生效，服务器负责双卡分工；调用方传同一条作业即可。[Turbo 加速图](../assets/h3-turbo.json)

参考视频转为 24 fps 静音画面。素材按上传顺序在各自媒体类型内编号。时间线从参考片开头取与生成窗口重合的动作；要参考原片较晚的动作，请先截出相应片段。独立音频转为 32 kHz 立体声，提供声线参考。输出保留模型生成的原生音轨。首尾帧通过图像引导传入，混合模式中的 `<Picture N>` 只指独立参考图。

已有的 HTTPS Jupyter `/proxy/<端口>` 安装继续使用 `Authorization: token <token>` 和原 Short Drama Workbench。该路由常规 10 步，支持「首帧加可选尾帧」或「全参考」。当前路由可通过 `video_generate(action="models")` 核对。[旧版工作流](../assets/h3-workflow.json)

## Agent 调用参数

用 `video_generate(action="validate", model="ComfyUI-MiniMax-H3", ...)` 核对一段镜头，随后用相同镜头参数和该镜头的 `idempotency_key` 调用 `submit`。每个 `input_assets` 项包含本镜实际使用的 `asset_id` 和 `role`。例如，画廊演员和商品都已在两张核准原画中时，传 `[{"asset_id":"<首帧 ID>","role":"first_frame"},{"asset_id":"<尾帧 ID>","role":"last_frame"}]`。要复刻原片的中间动作可加 `reference_video`，需要原画未呈现的商品细节可加 `reference_image`，指定声线可加 `reference_audio`。加入独立参考时切换为六段 R2V 提示词。

首尾帧的 `asset_id` 从 `image_gen` 回执原样复制，也可以填写本会话中唯一的完整文件名。校验找不到图片并给出相近项时，核对镜头和文件名，用对应的准确 ID 或文件名重试，已有的图片可以直接复用。

| OpenBox 参数 | ComfyUI 参数或含义 |
|---|---|
| `prompt` | 1–12000 字符。基础模式写三字段，R2V（含混合首尾帧）写素材标记和六段结构，详见 Skill |
| `input_assets` | 双机或直连可空，或按需组合 `first_frame`、`last_frame`、`reference_image`、`reference_video`、`reference_audio`。首、尾各至多一张。总计至多 8 个，视频和音频各至多 3 段 |
| 参考图或视频 | PNG、JPEG、WebP 每张至多 12 MB。MP4、MOV、WebM 每段 2–15 秒且至多 50 MB，按 24fps 画面作为参考 |
| 参考音频 | WAV、MP3、M4A、FLAC、OGG、AAC 或 WebM 音频，每文件至多 24 MB。单段 2–15 秒，总长至多 15 秒，需要至少一份视觉素材。后端转换为 32 kHz 立体声 WAV |
| `duration` | 整数 2–15 秒；原生 24 fps，帧数向上补齐至 `17k+5` |
| `ratio` | 显式 `9:16` 或 `16:9` |
| `resolution` | `480p` 对应约 0.43 MP，`720p` 对应约 0.69 MP，`768p` 对应约 1.03 MP。直连 720p 竖屏为 640×1120、横屏为 1120×640；最终成片画布在剪辑层确定 |
| `seed` | 可选正整数，映射至工作流种子 |
| `h3_profile` | 普通镜头省略或传 `standard`，后端沿用模板选定的主模型。锁版宣传段在原画做好文字后传 `poster`，使用上表的 12 步预设 |
| `poster_motion_region` | 双机工作台或直连 `poster` 可选。首尾传同一原画，按当前画面给出可运动的矩形 `[左边距, 上边距, 宽, 高]`，从左上角起算，单位是 0 至 1 的画面比例。宽高为正，矩形须位于画面内并避开固定文字。SelfLift 在两阶段采样中保留框外原画，同时生成原生音轨。整幅画面需要移动时省略 |
| `generate_audio` / `watermark` | 固定 `true` / `false` |
| 输出 | 24 fps、H.264 MP4、原生立体声 |

场景描述用英文，角色台词保留原文，如 `<d>[Chinese] …</d>`。有参考音频的镜头用 R2V，在 `input_assets` 放实际音频资产 ID，并在提示词用 `<Audio N>` 绑定角色。声线只需复用同一个音频资产；每段各自填写新对白。宣传文案按视频生成 Skill 先做进原画。

代理响应 `302`、`401` 或 `403` 时，核对本地代理地址和授权。提交后用 `video_generate(action="await_ready", job_ids=[...])` 等这组镜头。带 `background_review=true` 的作业在视频入库后自动读取动作、画面文字和带时间的对白，正常时一次返回所请求镜头的 `review`；失败项可提前返回。对照确认稿使用回执，针对具体缺项补查。普通作业仍在任一镜头完成时返回。`status` 可用于恢复时核对任务 ID。
