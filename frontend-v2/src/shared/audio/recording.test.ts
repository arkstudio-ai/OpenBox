import { describe, expect, it } from "vitest"
import { wavBlob } from "./recording"

describe("recording WAV encoding", () => {
  it("encodes bounded mono PCM with a correct length and header", async () => {
    const pcm = new Int16Array([0, 32767, -32768])
    const blob = wavBlob([pcm.buffer])
    const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as ArrayBuffer)
      reader.onerror = reject
      reader.readAsArrayBuffer(blob)
    })
    const view = new DataView(buffer)
    expect(String.fromCharCode(...new Uint8Array(buffer, 0, 4))).toBe("RIFF")
    expect(view.getUint32(24, true)).toBe(16000)
    expect(view.getUint32(40, true)).toBe(6)
    expect(view.getInt16(48, true)).toBe(-32768)
    expect(wavBlob([new ArrayBuffer(16_000 * 2 * 70)]).size).toBe(44 + 16_000 * 2 * 60)
  })
})
