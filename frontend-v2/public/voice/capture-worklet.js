// Capture mono PCM16 at 16 kHz in 100 ms packets without blocking the UI thread.
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.packet = new Int16Array(1600);
    this.offset = 0;
    this.energy = 0;
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const sample of samples) {
      this.sum += sample;
      this.count += 1;
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      const value = Math.max(-1, Math.min(1, this.sum / this.count));
      this.packet[this.offset++] = value < 0 ? value * 32768 : value * 32767;
      this.energy += value * value;
      this.sum = 0;
      this.count = 0;
      if (this.offset === this.packet.length) {
        const buffer = this.packet.buffer;
        this.port.postMessage({ audio: buffer, level: Math.sqrt(this.energy / 1600) }, [buffer]);
        this.packet = new Int16Array(1600);
        this.offset = 0;
        this.energy = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
