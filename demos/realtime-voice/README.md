# OpenBox 实时语音 Demo

独立的本机页面，点击「开始通话」即可与 `qwen-audio-3.1-realtime-plus` 进行实时语音对话。包含接通、结束、实时字幕和插话停止播报。麦克风需要浏览器授权，建议使用耳机。

## 启动

在 OpenBox 根目录执行：

```bash
backend/.venv/bin/python -B demos/realtime-voice/server.py
```

用 Chrome 或 Edge 打开 <http://127.0.0.1:8790>，允许麦克风后开始通话。`Ctrl+C` 停止服务。

服务复用 `backend/.venv` 中的 FastAPI、uvicorn、websockets 和 python-dotenv，不需要启动 OpenBox 主服务。默认从 `backend/.env` 读取 `DASHSCOPE_API_KEY`，也支持该环境变量覆盖。Key 不发送给浏览器。

如需换端口：

```bash
OPENBOX_VOICE_DEMO_PORT=8791 backend/.venv/bin/python -B demos/realtime-voice/server.py
```

## 音频链路

浏览器麦克风 → AudioWorklet（16 kHz、单声道 PCM16，每包 100 ms）→ 本机 WebSocket 代理 → 百炼实时模型 → 24 kHz PCM16 → 浏览器连续播放。

采用 `smart_turn` 判断语音轮次。收到用户开始说话事件时，清空播放队列并隔离上一条回复的残余音频。结束通话或关闭页面会释放麦克风、音频节点和上游连接。

当前使用已验证的北京兼容域名 `dashscope.aliyuncs.com`，仅监听 `127.0.0.1`，校验同源 WebSocket，不向浏览器提供任意上游请求能力。仅供本机体验；未来集成平台时再加入用户鉴权、额度和正式 provider adapter。

此 Demo 不调用平台工具，不创建任务，不保存录音或对话；实际音频发送给百炼处理，会按账号规则计费。

官方协议参考：[Qwen-Audio 实时语音对话](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)、[WebSocket 接入指南](https://help.aliyun.com/zh/model-studio/fun-audiochat-realtime-websocket-api)。

## 实时费用

页面显示本次通话的累计预估费用，可展开查看语音输入、语音输出、文本输入、文本输出的明细。结束通话后保留金额；开始新通话或刷新页面会重新计算。

每轮收到百炼 `response.done.response.usage` 后，使用官方四类 Token 明细核算。历史上下文已包含在各轮输入用量里，不再自行重复加算。回复生成期间，按已生成 PCM 音频时长折算输出费用作为临时估算，完整用量返回后替换为核算金额。

采用 2026-10-01 核实的北京区公开单价（元/百万 Token）：文本输入 5、语音输入 40、文本输出 40、语音输出 150。该金额不扣除免费额度或账号折扣，不等同于实际账单。价格变化时需要更新 `pricing.py` 中的 `RATES`。

被打断或异常中止的回复可能不返回用量。这些回复保留已生成音频的估算，并在页面提示「部分用量未返回，金额可能偏低」，不会直接当作免费。挂断立即停止麦克风和播放，并短暂等待最后的用量事件。不同回复及重复的终止事件按响应 ID 隔离，避免重复累加。

价格参考：[百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)；用量字段参考：[Qwen-Audio 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events)。
