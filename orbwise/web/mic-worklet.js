/* AudioWorklet: Mikrofon (z. B. 48 kHz Float) → 16 kHz Mono Int16 in Blöcken zu 1280 Samples (80 ms). */
class PcmDownsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.block = new Int16Array(1280);
    this.idx = 0;
    this.pos = 0;
    this.sum = 0;
    this.count = 0;
    this.sq = 0;
    this.sqCount = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0];
    for (let i = 0; i < ch.length; i++) {
      const s = ch[i];
      this.sum += s;
      this.count++;
      this.sq += s * s;
      this.sqCount++;
      this.pos += 1;
      while (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.count ? this.sum / this.count : 0));
        this.sum = 0;
        this.count = 0;
        this.block[this.idx++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.idx === this.block.length) {
          const level = Math.sqrt(this.sq / Math.max(1, this.sqCount));
          this.sq = 0;
          this.sqCount = 0;
          this.port.postMessage({ pcm: this.block.buffer, level }, [this.block.buffer]);
          this.block = new Int16Array(1280);
          this.idx = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-downsampler", PcmDownsampler);
