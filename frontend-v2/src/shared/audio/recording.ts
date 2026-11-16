/** Short mono recordings use the same 16 kHz PCM worklet as live calls. */
export function recordingSupported() {
  return typeof window.AudioContext === "function" && typeof window.AudioWorkletNode === "function"
    && typeof navigator.mediaDevices?.getUserMedia === "function"
}

export function wavBlob(chunks: readonly ArrayBuffer[]): Blob {
  const size = Math.min(chunks.reduce((n, chunk) => n + chunk.byteLength, 0), 16_000 * 2 * 60)
  const buffer = new ArrayBuffer(44 + size)
  const view = new DataView(buffer)
  const tag = (offset: number, value: string) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)))
  tag(0, "RIFF"); view.setUint32(4, 36 + size, true); tag(8, "WAVE"); tag(12, "fmt ")
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, 16_000, true); view.setUint32(28, 32_000, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); tag(36, "data"); view.setUint32(40, size, true)
  const bytes = new Uint8Array(buffer)
  let offset = 44
  for (const chunk of chunks) {
    const part = new Uint8Array(chunk).subarray(0, bytes.length - offset)
    bytes.set(part, offset); offset += part.length
  }
  return new Blob([buffer], { type: "audio/wav" })
}

export class DialogueRecorder {
  private closed = false
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private nodes: AudioNode[] = []
  private chunks: ArrayBuffer[] = []
  async start(): Promise<void> {
    const context = this.context = new AudioContext({ latencyHint: "interactive" })
    // Resume in the original pointer/key gesture, before the permission await.
    const resumed = context.resume().catch(() => undefined)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1,
        echoCancellation: true, noiseSuppression: true }, video: false })
      if (this.closed) { stream.getTracks().forEach((track) => track.stop()); return }
      this.stream = stream
      await resumed
      if (context.state !== "running") throw new Error("Microphone context did not start")
      await context.audioWorklet.addModule("/voice/capture-worklet.js")
      if (this.closed) return
      const input = context.createMediaStreamSource(stream)
      const worklet = new AudioWorkletNode(context, "pcm-capture", { numberOfInputs: 1,
        numberOfOutputs: 1, outputChannelCount: [1] })
      const sink = context.createGain(); sink.gain.value = 0
      worklet.port.onmessage = ({ data }: MessageEvent<{ audio: ArrayBuffer }>) => {
        if (!this.closed && this.chunks.length < 600) this.chunks.push(data.audio)
      }
      input.connect(worklet).connect(sink).connect(context.destination)
      this.nodes = [input, worklet, sink]
    } catch (error) { this.cancel(); throw error }
  }
  finish(): Blob {
    const result = wavBlob(this.chunks)
    this.cancel()
    return result
  }
  cancel() {
    this.closed = true
    this.stream?.getTracks().forEach((track) => track.stop())
    this.nodes.forEach((node) => node.disconnect())
    this.nodes = []
    if (this.context && this.context.state !== "closed") void this.context.close().catch(() => undefined)
  }
}
