// AudioWorklet: real audio samples -> mono Int16 chunks of `chunkFrames` frames, posted to the main thread.
// Runs at the AudioContext sample rate; resampling to 16 kHz happens on the server with a stateful filter.
class PcmChunker extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.chunkFrames = (options.processorOptions && options.processorOptions.chunkFrames) || 4800;
    this.buf = new Int16Array(this.chunkFrames);
    this.pos = 0;
    this.muted = false;
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "mute") this.muted = !!e.data.muted;
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channels = input.length;
    const frames = input[0].length;
    let peak = 0;
    for (let i = 0; i < frames; i++) {
      let s = 0;
      for (let c = 0; c < channels; c++) s += input[c][i];
      s = this.muted ? 0 : s / channels;
      const a = Math.abs(s);
      if (a > peak) peak = a;
      const v = Math.max(-1, Math.min(1, s));
      this.buf[this.pos++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.pos === this.chunkFrames) {
        this.port.postMessage({ type: "chunk", pcm: this.buf.buffer, frames: this.chunkFrames, peak }, [this.buf.buffer]);
        this.buf = new Int16Array(this.chunkFrames);
        this.pos = 0;
        peak = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-chunker", PcmChunker);
