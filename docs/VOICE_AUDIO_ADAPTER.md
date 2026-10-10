# Qwen-Audio 3.1 实时语音适配

验证日期：2026-10-10。OpenBox 的实时语音链路现在同时支持
`qwen3.8-omni-flash-realtime` 和 `qwen-audio-3.1-realtime-plus`。
现有默认配置继续使用 Omni；选择 Audio 时，协议、音色和计费随模型切换。

## 配置

```dotenv
VOICE_ENABLED=true
VOICE_MODEL=qwen-audio-3.1-realtime-plus
VOICE_VOICE=longanqian_v3.1
VOICE_MAX_HISTORY_TURNS=20
VOICE_ENABLE_SPEECH_EMOTION=true
```

API Key 仍通过后端 `DASHSCOPE_API_KEY` 或 `voice.api_key` 读取。
`VOICE_MAX_HISTORY_TURNS` 范围为 1–50。Audio 使用 `smart_turn`；
`vad_threshold`、`silence_ms` 和 Omni 的 `smooth_output` 不发送给 Audio。
OpenBox 的通话摘要、旧对话项清理和会话轮换继续使用现有链路。

Audio 官方列出的 13 个系统音色均已实测，设置页提供对应的真实模型试听录音。
历史保存的 Tina 等 Omni 音色会回退到 Audio 默认音色；读取时保留原偏好，便于切回 Omni。
已在百炼创建的 Audio 复刻音色可以通过 `VOICE_VOICE` 配置，ID 必须属于这个模型。
通话记录的音色字段扩到 255 字符，以保存完整复刻 ID。
升级已有数据库前先运行：

```sh
cd backend
.venv/bin/python -m alembic upgrade head
```

迁移 `pc06b7c8d9e0` 只扩展字段；回退时若存在超过 32 字符的音色 ID，会拒绝缩短字段，防止截断。

## 协议差异

Audio 在空会话上只接收 `response.instructions` 时实测报错：会话中没有用户消息。
适配层为开场、进度、结果和结束语创建单次播报指令项，再发送 `response.create`。
回复完成、取消、失败或请求被拒绝后删除该项，并将它排除在用户转录和摘要之外。
工具结果保持原来的 `function_call_output` 和 `call_id`，后续回复直接读取结果。

Audio 分别按文本输入 5、音频输入 40、文本输出 40、音频输出 150 元/百万 token 统计。
部分经典音色会输出 PCM 却省略 `output_tokens_details.audio_tokens`。
此时保留已报告的文本费用，仅对缺失的音频用量估算，并标记为 provisional/unreported；
之后收到完整 usage 会替换估算，避免重复累计。

## 本地访问

本次验证的 worktree：`audio-realtime-adapter/OpenBox`，分支 `codex/audio-realtime-adapter`。
后端监听 `127.0.0.1:8081`；HTTP 前端监听 `0.0.0.0:3001`；HTTPS 前端监听 `0.0.0.0:3443`。
局域网访问地址为 `https://192.168.2.17:3443`。

Vite 支持通过环境变量启用 HTTPS，证书和私钥不进入仓库：

```sh
cd frontend-v2
PORT=3443 VITE_BACKEND_PROXY_TARGET=http://127.0.0.1:8081 \
DEV_HTTPS_CERT=/absolute/path/cert.pem DEV_HTTPS_KEY=/absolute/path/key.pem npm run dev
```

证书须包含访问设备使用的 IP 或主机名。本机原有证书包含 `192.168.2.17`、`127.0.0.1` 和 `localhost`，
但为自签名证书；浏览器和手机需要用户自行信任。验证脚本显式信任该证书并校验主机名，未关闭 TLS 校验。

## 验证

- 后端语音单元测试、语音 WebSocket 集成测试及媒体计费测试：341 项通过。
- 前端生产构建和 6 个语音/设置测试文件中的 77 项测试。
- 13 个音色逐个调用真实 Audio 模型生成试听，HTTPS 获取全部试听成功。
- 使用 `tem.md` 测试账号经 HTTPS 登录、获取通话票据，经 WSS 收到正确 Audio 模型标识和音频。
- WSS 测试包含连续语音、插话后的 `playback.clear`、继续回复、任务列表查询和结束计费，未返回错误。
- 真实模型通过生产 Bridge 调用 `assistant_ask` 和 `tasks_overview`；后台执行结果由测试替身提供，
  验证了延迟播报、单次结果交付、真实摘要生成和切换会话后的上下文延续。
- PostgreSQL 已应用迁移；在回滚事务内验证完整复刻 ID 的写入和读回。

可重复执行的真实 Bridge 测试（会产生 API 费用）：

```sh
cd backend
.venv/bin/python tests/manual/voice_live_check.py --model qwen-audio-3.1-realtime-plus
```

## 官方资料

- [实时语音使用说明](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)
- [客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)
- [Audio 3.1 模型与价格](https://help.aliyun.com/zh/model-studio/qwen-audio-3-1-realtime-plus)
- [音色名称资料](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list)
