import { describe, expect, it, vi } from "vitest"
import { PcmPlayer } from "./player"

class FakeSource {
  buffer: { duration: number } | null = null
  onended: (() => void) | null = null
  startedAt: number | null = null
  stopped = false
  connect = vi.fn()
  disconnect = vi.fn()
  start(when: number) {
    this.startedAt = when
  }
  stop() {
    this.stopped = true
  }
  /** The browser reaching the end of this chunk. */
  finish() {
    this.onended?.()
  }
}

class FakeContext {
  currentTime = 10
  destination = {}
  sources: FakeSource[] = []
  decoded: Float32Array[] = []
  createBuffer(_channels: number, length: number, rate: number) {
    const data = new Float32Array(length)
    this.decoded.push(data)
    return { duration: length / rate, getChannelData: () => data }
  }
  createBufferSource() {
    const source = new FakeSource()
    this.sources.push(source)
    return source
  }
  createAnalyser() {
    return {
      fftSize: 0,
      connect: vi.fn(),
      disconnect: vi.fn(),
      getFloatTimeDomainData: (out: Float32Array) => out.fill(0.5),
    }
  }
}

/** 24 kHz PCM16 of `ms` milliseconds, every sample `value`. */
function pcm(ms: number, value = 16384): ArrayBuffer {
  const samples = new Int16Array((24_000 * ms) / 1000).fill(value)
  return samples.buffer
}

function player(onIdle = vi.fn()) {
  const context = new FakeContext()
  return { context, onIdle, player: new PcmPlayer(context as unknown as BaseAudioContext, 24_000, onIdle) }
}

describe("PcmPlayer", () => {
  it("schedules chunks back to back in arrival order, the first a little ahead of now", () => {
    const { context, player: out } = player()
    expect(out.enqueue(pcm(100))).toBeCloseTo(10.025)
    expect(out.enqueue(pcm(200))).toBeCloseTo(10.125)
    expect(out.enqueue(pcm(50))).toBeCloseTo(10.325)
    expect(context.sources.map((source) => source.startedAt)).toEqual([
      expect.closeTo(10.025),
      expect.closeTo(10.125),
      expect.closeTo(10.325),
    ])
    expect(out.busy).toBe(true)
    expect(out.remaining()).toBeCloseTo(0.375)
  })

  it("decodes little-endian PCM16 into floats and skips empty or odd trailing bytes", () => {
    const { context, player: out } = player()
    const raw = new ArrayBuffer(5)
    new DataView(raw).setInt16(0, -32768, true)
    new DataView(raw).setInt16(2, 16384, true)
    expect(out.enqueue(raw)).not.toBeNull()
    expect(Array.from(context.decoded[0])).toEqual([-1, 0.5])
    expect(out.enqueue(new ArrayBuffer(1))).toBeNull()
  })

  it("starts fresh after a gap rather than in the past", () => {
    const { context, player: out } = player()
    out.enqueue(pcm(100))
    context.sources[0].finish()
    context.currentTime = 20
    expect(out.enqueue(pcm(100))).toBeCloseTo(20.025)
  })

  it("clear() stops every source, and a cut chunk never reports back", () => {
    const { context, onIdle, player: out } = player()
    out.enqueue(pcm(100))
    out.enqueue(pcm(100))
    expect(out.clear()).toBe(2)
    expect(context.sources.every((source) => source.stopped)).toBe(true)
    expect(context.sources.every((source) => source.onended === null)).toBe(true)
    expect(out.busy).toBe(false)
    expect(onIdle).not.toHaveBeenCalled()
    // What arrives next starts from now, not after the dropped queue.
    expect(out.enqueue(pcm(100))).toBeCloseTo(10.025)
    expect(out.clear()).toBe(1)
    expect(out.clear()).toBe(0)
  })

  it("calls onIdle once the last queued chunk has played", () => {
    const { context, onIdle, player: out } = player()
    out.enqueue(pcm(100))
    out.enqueue(pcm(100))
    context.sources[0].finish()
    expect(onIdle).not.toHaveBeenCalled()
    context.sources[1].finish()
    expect(onIdle).toHaveBeenCalledTimes(1)
    expect(out.busy).toBe(false)
  })

  it("reads the output level only while something plays", () => {
    const { player: out } = player()
    expect(out.level()).toBe(0)
    out.enqueue(pcm(100))
    expect(out.level()).toBeCloseTo(0.5)
  })
})
