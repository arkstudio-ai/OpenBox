# 语音通话 PC 端（frontend-v2）：悬浮窗

状态：**实施文档**。日期：2026-10-07。契约见 [总规格](VOICE_CALL_SPEC.md)；后端见 [后端文档](VOICE_CALL_BACKEND.md)。本文讲网页端怎么做：入口、悬浮窗、交互、音频、文件、测试、顺序。所有“现有”引用均已核对源码。

## 1. 范围

- 入口按钮、悬浮通话窗（展开/药丸两种形态）、通话音频（采集、播放、打断、电平）、提示音、结束面板。
- 通话状态全局存在：切换路由、打开设置、看任务抽屉都不中断。
- 不做：拖动位置记忆（P3）、断线恢复（P2）、听写进输入框（另一件事）。

## 2. 入口

- 位置：顶栏 actions 区，与现有 `AssistantTopbarActions`（“我的任务”）并排、放在它前面；现有装配点是 `frontend-v2/src/app/layouts/WorkspaceLayout.tsx` 第 150 行附近的 `actions={... <AssistantTopbarActions /> ...}`，仅在 `isAssistant` 时渲染。改为 `<><VoiceCallButton /><AssistantTopbarActions /></>`。
- 样式与“我的任务”按钮一致（`border-hair text-n800 hover:bg-hairsoft h-8 rounded-full border px-3 text-sm`），图标 `lucide-react` 的 `Phone`，文字 `voice:button.start`（`sm` 以下只显示图标，`aria-label` 用 `voice:button.startLabel`）。
- 通话中：按钮变为 `voice:button.inCall` 样式（`bg-s100 text-s800`），点击 = 展开悬浮窗；不会再起第二通。
- `GET /api/agent/config` 的 `voice_enabled=false` 时不渲染按钮。
- 不支持的浏览器（无 `AudioWorkletNode` 或 `getUserMedia`）：按钮可见，点击后直接进入结束面板，文案 `voice:errors.unsupported`。

## 3. 悬浮窗

挂载点：`WorkspaceLayout` 根 `div` 内最后一个子元素 `<VoiceCallDock />`（对所有工作区页面可见；takeover 页面如设置、管理台也可见）。`idle` 状态不渲染任何 DOM。

位置与层级：`position: fixed; top: 60px; right: 16px`（顶栏下方，不压输入框；不随页面滚动）；`z-40`（低于 `Dialog`/`Sheet` 的 `z-50` 与 Toast 的 `z-60`）。宽 300 px；高度自适应（约 230 px）。药丸形态 44 px 高、内容自适应宽。`prefers-reduced-motion` 下关闭球体动画，只用静态状态点。

展开形态：

```
┌──────────────────────────────────────────┐
│ ◉  个人助理            02:14      [ v ]   │  ← 球体(28px, 随电平呼吸) · 标题 · 计时 · 收起
│ 我在听                                   │  ← 状态行 (phase) ；working 时右侧小字“在办…”
│ ─────────────────────────────────────── │
│ 你：帮我看看贪吃蛇项目进展                   │  ← 用户字幕, text-n600, 2 行截断
│ 助理：好的，你稍等一下，我去看看。            │  ← 助理字幕, text-ink, 3 行截断
│ ─────────────────────────────────────── │
│ [ 🎤 静音 ]           ¥0.0035   [ 📵 挂断 ] │  ← 控制行：静音切换 · 费用(小字, hover 看明细) · 挂断(红)
└──────────────────────────────────────────┘
```

药丸形态（点收起 `v` 或点球体）：

```
┌────────────────────────────┐
│ ◉ 02:14 · 我在听      [📵] │   ← 点任意处展开；挂断按钮单独可点
└────────────────────────────┘
```

颜色只用 tokens：卡片 `bg-card border-hair shadow`、文字 `text-ink/text-n600`、状态点 `listening=s600`、`thinking=n500`、`speaking=accent`、`working=a700`、挂断 `bg-danger text-white`（hover `bg-dangerink`）。圆角 `rounded-2xl`（药丸 `rounded-full`）。

球体：一个 28 px 圆（药丸 20 px），`listening` 时按麦克风电平放大 1.0–1.35 倍，`speaking` 时按播放电平放大，`thinking/working` 时慢速呼吸（1.6 秒周期），`greeting` 同 `speaking`。用 CSS 变量 `--level` 驱动 `transform: scale()`，每 50 ms 更新一次。

各状态画面：

| 状态 | 状态行 | 控制行 |
| --- | --- | --- |
| `requesting_mic` | `voice:state.requestingMic` | 只有“取消”（= 挂断样式灰色） |
| `connecting` | `voice:state.connecting` + 小转圈 | 只有“取消” |
| `connected/greeting…speaking` | 对应 `voice:state.*`；`working` 时追加小字 `voice:state.working`/`late` | 静音、费用、挂断 |
| `ending` | `voice:state.ending` | 禁用 |
| `ended` | 结束面板：`voice:ended.<reason>`、`voice:ended.duration`、`voice:ended.cost`、有 `pending_turns` 时 `voice:ended.pendingHint`；按钮 `voice:actions.redial`（仅 `hangup/error/network/limit` 可重拨；`quota/concurrent` 不可）、`voice:actions.close` | — |

结束面板 8 秒后自动关闭（`error` 类不自动关），或点“关闭”。

## 4. 交互细节

- 开始：点入口 → `requesting_mic`（播“接通中”音在拿到麦克风之后、连接之前，避免权限弹窗时出声）→ `connecting` → `ready` 后播“已接通”音 → `greeting`。
- 展开/收起：收起只是形态变化，不影响通话；`Esc` 不做任何事（避免误挂断；现有 `overlay-stack` 的 Escape 约定与悬浮窗无关，悬浮窗不注册为 overlay）。
- 挂断：单击即挂，不二次确认（电话习惯）；按钮 `aria-label` 用 `voice:controls.hangUp`。
- 静音：切换后麦克风轨道 `enabled=false`，采集端继续发送零帧（服务端不感知）；球体不再跟随；按钮显示 `voice:controls.unmute`。
- 路由切换、打开抽屉/对话框：不影响；对话框 `z-50` 会盖在悬浮窗上，这是预期。
- 关闭标签页/刷新：`pagehide` 时发 `stop`（best-effort，与 demo 相同）；`beforeunload` 不弹确认。
- 另一个标签页再拨：收到 4009 → 结束面板 `voice:ended.concurrent`。
- 键盘：悬浮窗内按钮可 Tab 到达；药丸整体是一个 `button`。
- 无障碍：字幕区域 `aria-live="polite"`；状态行 `role="status"`。
- 费用：控制行显示 `total_yuan`（保留 4 位）；hover `title` 列出四项明细与 `settled_rounds`；`unreported_rounds>0` 时附 `voice:cost.partial`。
- 剩余时长：`ready.max_seconds` 减去已用 < 5 分钟时，计时器旁显示 `voice:duration.remaining`。

## 5. 与聊天列表联动

- 收到 `turn{state:accepted, message_id}`：若当前在助理页，把对话滚到该消息（复用现有 `useAssistantEvents` 的刷新：`assistant.turn.accepted` 事件本来就会让列表刷新，这里只需在列表里找到 `message_id` 并 `scrollIntoView`）。不在助理页时不跳转。
- 收到 `turn{state:delivered}`：不做额外动作（回复已由现有事件流进入列表）。
- 通话中输入框照常可用：用户打字发的消息与语音轮次共用一个 Inbox，先后顺序由服务端决定。

## 6. 音频实现

目录 `frontend-v2/src/features/voice/audio/`：

- `capture-worklet.js`：从 `demos/realtime-voice/capture-worklet.js` 原样复制到 `frontend-v2/public/voice/capture-worklet.js`，通过 `audioContext.audioWorklet.addModule("/voice/capture-worklet.js")` 加载（与 demo 一致，避开打包器对 worklet 的处理）。输出 16 kHz PCM16、每 100 ms 一包、附带电平。
- `capture.ts`：`getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true}})` → `MediaStreamSource` → worklet → 零增益节点接 `destination`（Chrome 需要图连通）。暴露 `onPacket(buffer, level)`、`setMuted(bool)`（静音时把 buffer 置零再发）、`stop()`。轨道 `ended` 事件 → `mic_lost`。
- `player.ts`：从 demo `playAudio/clearPlayback` 抽成类：`enqueue(ArrayBuffer)`（24 kHz Int16 → AudioBuffer，`start(max(now+0.025, nextTime))`）、`clear()`（停止所有 source、`nextTime=0`）、`level`（输出端挂 `AnalyserNode`，50 ms 取 RMS）、`onIdle` 回调（队列空且无播放时触发，用于 `speaking→listening` 的界面切换）。
- `tones.ts`：`connecting()`、`connected()`、`ended()`、`error()` 四个振荡器序列，参数见总规格 §8；用独立的短生命周期 `AudioContext` 或共享通话的 context 均可，但不进播放队列。
- 发送背压：`socket.bufferedAmount > 128000` 时结束通话（`voice:errors.backlog`），与 demo 一致。

`AudioContext` 在点击手势里创建并 `resume()`（Chrome 自动播放策略），一通电话一个 context，结束时 `close()`。

## 7. 文件与组件

```
frontend-v2/src/features/voice/
  index.ts                       # 导出 VoiceCallButton, VoiceCallDock, useVoiceCall
  api/voice.ts                   # fetchVoiceTicket(): POST /api/auth/ticket {audience:"voice"}；voiceSocketUrl(ticket)
  lib/types.ts                   # CallState, Phase, EndReason, ServerEvent（字段与总规格 5.3 一一对应）
  lib/reducer.ts                 # (state, event) => state：纯函数，处理 ready/phase/caption/turn/cost/limit/error/ended
  lib/session.ts                 # VoiceCallSession：非 React 类，持有 ws/capture/player/tones；start()/stop()/setMuted()；把事件喂给 store
  store.ts                       # zustand：{ call: CallState, expanded, muted, start, hangUp, toggleMute, setExpanded }
  hooks/useVoiceCall.ts          # 组件用的选择器与动作
  components/VoiceCallButton.tsx # 顶栏入口
  components/VoiceCallDock.tsx   # fixed 容器：按形态渲染 Expanded / Pill / Ended
  components/VoiceCallExpanded.tsx
  components/VoiceCallPill.tsx
  components/VoiceCallEnded.tsx
  components/VoiceOrb.tsx        # 球体（接收 level 与 phase）
  components/VoiceCaptions.tsx   # 两行字幕
  audio/capture.ts  audio/player.ts  audio/tones.ts
frontend-v2/public/voice/capture-worklet.js
frontend-v2/src/locales/{zh-CN,en-US}/voice.json
```

`VoiceCallSession` 不复用现有 `WsClient`（那是带自动重连、ticket 重取、事件分发的长连接；通话是一次性连接，重连语义不同），但票据获取方式与它相同（`fetch(`${env.apiBase}/api/auth/ticket`, {Authorization: Bearer})`，401 时 `refreshAccessToken()` 重试一次）。socket 地址用现有 `wsBase()`。

`store` 形状：

```ts
interface CallState {
  status: "idle"|"requesting_mic"|"connecting"|"connected"|"ending"|"ended"
  phase: "greeting"|"listening"|"thinking"|"speaking"|"working"
  working: boolean; late: boolean
  callId: string|null; startedAt: number|null; maxSeconds: number|null
  captions: { user: string; assistant: string }
  turns: Record<string, { state: string; messageId?: string }>
  cost: CostSnapshot|null
  ended: { reason: EndReason; durationSeconds: number; pendingTurns: number; cost: CostSnapshot|null }|null
  level: { mic: number; out: number }
}
```

## 8. i18n

命名空间 `voice`，文件内容以总规格 §7 为准（zh-CN/en-US 两份），放到 `frontend-v2/src/locales/<lng>/voice.json`。现有 i18n 用 `import.meta.glob` 懒加载所有 `locales/*/*.json`，无需注册；组件用 `useTranslation("voice")`。手机端同步复制（门禁）。

## 9. 测试（vitest）

| 文件 | 用例 |
| --- | --- |
| `lib/reducer.test.ts` | ready→connected/greeting；phase 事件覆盖；caption 增量与整句替换；turn 累积；limit→ended(limit)；error→ended(error)；ended 事件透传 reason/pending |
| `audio/player.test.ts` | enqueue 顺序调度；clear 后旧 source 不再播放；onIdle 触发（用假 AudioContext） |
| `lib/session.test.ts` | 握手顺序（票据→连接→ready 前不发音频）；`stop` 发送与 4 秒兜底关闭；4009/4029 映射；pagehide 发 stop（假 WebSocket + 假 capture/player） |
| `components/VoiceCallDock.test.tsx` | idle 不渲染；connected 展开形态显示状态与字幕；收起显示药丸；挂断按钮调用 hangUp；ended 面板文案与重拨可见性 |
| `components/VoiceCallButton.test.tsx` | voice_enabled=false 不渲染；通话中变为“通话中”并展开 |
| 现有 `WorkspaceLayout.isolation.test.tsx` | 补：悬浮窗在 takeover 页面也挂载 |

## 10. 任务顺序

1. `voice.json` 两份 + 手机端同步复制（先过门禁）。
2. `lib/types.ts`、`lib/reducer.ts` + 单测。
3. `audio/` 三个文件（从 demo 搬）+ `player.test.ts`。
4. `lib/session.ts` + `store.ts` + `session.test.ts`。
5. 组件：Dock/Expanded/Pill/Ended/Orb/Captions + 测试。
6. 入口按钮与 `WorkspaceLayout` 装配；`AppConfig` 类型加 `voice_enabled`。
7. QA 真机（Chrome/Edge）跑总规格 §10 全部 14 条；记录到方案文档 §7。
