"use strict";

const ui = {
  button: document.getElementById("call-button"),
  label: document.getElementById("button-label"),
  status: document.getElementById("status"),
  timer: document.getElementById("timer"),
  error: document.getElementById("error"),
  orb: document.getElementById("orb"),
  user: document.getElementById("user-caption"),
  assistant: document.getElementById("assistant-caption"),
  cost: document.getElementById("cost-total"),
  costStatus: document.getElementById("cost-status"),
  costDetails: Object.fromEntries(["input_text", "input_audio", "output_text", "output_audio"].map(key => [key, document.getElementById(`cost-${key}`)])),
};
let current = null;
let costOwner = null;

function renderCost(cost) {
  const amount = value => `¥${Number(value || 0).toFixed(6)}`;
  ui.cost.textContent = amount(cost?.total_yuan);
  for (const [key, element] of Object.entries(ui.costDetails)) {
    element.textContent = amount(cost?.costs_yuan?.[key]);
  }
  if (!cost) {
    ui.costStatus.textContent = "等待开始通话";
    return;
  }
  const rounds = `已核算 ${cost.settled_rounds || 0} 轮`;
  if (cost.unreported_rounds || (cost.final && cost.pending)) {
    ui.costStatus.textContent = `${rounds} · 部分用量未返回，金额可能偏低`;
  } else if (cost.pending) {
    ui.costStatus.textContent = `${rounds} · 本轮金额待确认`;
  } else {
    ui.costStatus.textContent = `${rounds}${cost.final ? " · 通话已结束" : ""}`;
  }
}

function state(name, text) {
  document.body.dataset.state = name;
  ui.status.textContent = text;
}

function isCurrent(call) {
  return current === call && !call.closed;
}

function clearPlayback(call) {
  for (const source of call.playing) {
    source.onended = null;
    try { source.stop(); } catch {}
    source.disconnect();
  }
  call.playing.clear();
  call.nextAudioTime = 0;
}

async function stopCall(message = "通话已结束", error = "") {
  const call = current;
  current = null;
  if (call) {
    call.closed = true;
    clearTimeout(call.timeout);
    clearInterval(call.clock);
    clearPlayback(call);
    call.capture?.disconnect();
    call.input?.disconnect();
    call.silent?.disconnect();
    call.stream?.getTracks().forEach(track => track.stop());
    if (call.socket?.readyState === WebSocket.OPEN) {
      call.socket.send(JSON.stringify({ type: "stop" }));
      // Keep receiving the final estimate after releasing the microphone immediately.
      call.closingTimeout = setTimeout(() => {
        call.socket.close();
        if (costOwner === call && !call.lastCost?.final) {
          renderCost({ ...call.lastCost, final: true, pending: true });
        }
      }, 4000);
    } else {
      try { call.socket?.close(); } catch {}
    }
    if (call.context && call.context.state !== "closed") {
      // Do not allow an old cleanup promise to overwrite a newly started call.
      call.context.close().catch(() => {});
    }
  }
  ui.orb.style.setProperty("--level", "0");
  ui.label.textContent = "开始通话";
  ui.timer.hidden = true;
  ui.error.textContent = error;
  ui.error.hidden = !error;
  state("idle", message);
}

function playAudio(call, raw) {
  if (!isCurrent(call) || !call.ready || !raw.byteLength) return;
  const pcm = new DataView(raw);
  const buffer = call.context.createBuffer(1, raw.byteLength / 2, call.outputRate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < channel.length; i++) channel[i] = pcm.getInt16(i * 2, true) / 32768;
  const source = call.context.createBufferSource();
  source.buffer = buffer;
  source.connect(call.context.destination);
  const start = Math.max(call.context.currentTime + 0.025, call.nextAudioTime);
  call.nextAudioTime = start + buffer.duration;
  call.playing.add(source);
  source.onended = () => {
    call.playing.delete(source);
    source.disconnect();
    if (isCurrent(call) && !call.responding && call.playing.size === 0) state("listening", "正在听，你可以继续说话");
  };
  source.start(start);
  state("speaking", "OpenBox 正在说话，随时可以打断");
}

function handleEvent(call, event) {
  if (!isCurrent(call)) return;
  switch (event.type) {
    case "ready":
      clearTimeout(call.timeout);
      call.ready = true;
      call.outputRate = event.output_sample_rate;
      call.startedAt = Date.now();
      ui.label.textContent = "结束通话";
      ui.timer.hidden = false;
      ui.timer.textContent = "00:00";
      call.clock = setInterval(() => {
        if (!isCurrent(call)) return;
        const seconds = Math.floor((Date.now() - call.startedAt) / 1000);
        ui.timer.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      }, 1000);
      state("listening", "已接通，直接说话就好");
      break;
    case "speech.started":
      clearPlayback(call);
      call.responding = false;
      ui.user.textContent = "";
      state("listening", "正在听你说话");
      break;
    case "speech.stopped":
      state(event.invalid ? "listening" : "thinking", event.invalid ? "正在听，你可以继续说话" : "OpenBox 正在回应");
      break;
    case "response.started":
      call.responding = true;
      ui.assistant.textContent = "";
      state("thinking", "OpenBox 正在回应");
      break;
    case "response.done":
      call.responding = false;
      if (event.cancelled) clearPlayback(call);
      if (call.playing.size === 0) state("listening", "正在听，你可以继续说话");
      break;
    case "transcript.user":
      ui.user.textContent = event.final ? event.text : ui.user.textContent + event.text;
      break;
    case "transcript.assistant":
      ui.assistant.textContent = event.final ? event.text : ui.assistant.textContent + event.text;
      break;
    case "error":
      stopCall("未能继续通话", event.message);
      break;
  }
}

async function startCall() {
  if (current) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    stopCall("当前浏览器暂不支持", "请用最新版 Chrome 或 Edge，通过本机地址打开页面。");
    return;
  }
  const call = { closed: false, ready: false, playing: new Set(), responding: false, nextAudioTime: 0, outputRate: 24000 };
  current = call;
  costOwner = call;
  renderCost(null);
  ui.error.hidden = true;
  ui.user.textContent = "";
  ui.assistant.textContent = "";
  ui.label.textContent = "取消连接";
  state("connecting", "请允许麦克风，正在准备通话");
  try {
    // Resume during the click gesture so model audio can play without another click.
    call.context = new AudioContext({ latencyHint: "interactive" });
    await call.context.resume();
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    if (!isCurrent(call)) {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    call.stream = stream;
    await call.context.audioWorklet.addModule("/capture-worklet.js");
    if (!isCurrent(call)) return;
    call.input = call.context.createMediaStreamSource(stream);
    call.capture = new AudioWorkletNode(call.context, "pcm-capture", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    call.silent = call.context.createGain();
    call.silent.gain.value = 0;
    call.input.connect(call.capture).connect(call.silent).connect(call.context.destination);
    call.capture.port.onmessage = ({ data }) => {
      if (!isCurrent(call)) return;
      ui.orb.style.setProperty("--level", String(Math.min(1, data.level * 9)));
      if (call.ready && call.socket.readyState === WebSocket.OPEN) {
        if (call.socket.bufferedAmount > 128000) {
          stopCall("网络暂时不稳定", "音频发送积压，请重新开始通话。");
          return;
        }
        call.socket.send(data.audio);
      }
    };
    stream.getAudioTracks()[0].addEventListener("ended", () => {
      if (isCurrent(call)) stopCall("麦克风已断开", "请检查麦克风后重新开始通话。");
    });
    state("connecting", "正在接通 OpenBox");
    call.socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
    call.socket.binaryType = "arraybuffer";
    call.timeout = setTimeout(() => {
      if (isCurrent(call)) stopCall("连接超时", "请检查网络后重新开始通话。");
    }, 25000);
    call.socket.onmessage = ({ data }) => {
      try {
        if (data instanceof ArrayBuffer) {
          if (isCurrent(call)) playAudio(call, data);
          return;
        }
        const event = JSON.parse(data);
        if (event.type === "cost") {
          call.lastCost = event;
          if (costOwner === call) renderCost(event);
          if (event.final && call.closed) {
            clearTimeout(call.closingTimeout);
            call.socket.close();
          }
        } else if (isCurrent(call)) {
          handleEvent(call, event);
        }
      } catch {
        if (isCurrent(call)) stopCall("音频处理失败", "请重新开始通话。");
      }
    };
    call.socket.onerror = () => {
      if (isCurrent(call)) stopCall("连接失败", "请确认本机 Demo 服务仍在运行，然后重新开始通话。");
    };
    call.socket.onclose = () => {
      clearTimeout(call.closingTimeout);
      if (costOwner === call && call.lastCost && !call.lastCost.final) {
        renderCost({ ...call.lastCost, final: true });
      }
      if (isCurrent(call)) stopCall("通话已断开", "请点击开始通话重新连接。");
    };
  } catch (error) {
    if (!isCurrent(call)) return;
    const messages = {
      NotAllowedError: "麦克风权限未开启，请在浏览器地址栏允许麦克风后重试。",
      NotFoundError: "没有找到麦克风，请连接麦克风后重试。",
      NotReadableError: "麦克风暂时无法使用，请检查是否被其他应用占用。",
    };
    stopCall("未能开始通话", messages[error.name] || "音频设备初始化失败，请使用最新版 Chrome 或 Edge 重试。");
  }
}

ui.button.addEventListener("click", () => current ? stopCall() : startCall());
window.addEventListener("pagehide", () => stopCall());
