// AudioWorklet: real audio samples -> mono Int16 chunks of `chunkFrames` frames, posted to the main thread.
// Runs at the AudioContext sample rate; resampling to 16 kHz happens on the server with a stateful filter.
class PcmChunker extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.chunkFrames = (options.processorOptions && options.processorOptions.chunkFrames) || 4800;
    this.buf = new Int16Array(this.chunkFrames);
    this.pos = 0;
    this.muted = false;
    this.paused = false;
    this.peak = 0;
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "mute") this.muted = !!e.data.muted;
      if (e.data && e.data.type === "pause") {
        this.paused = !!e.data.paused;
        if (this.paused) this.flush();
        this.port.postMessage({ type: "paused", paused: this.paused, requestId: e.data.requestId });
      }
      if (e.data && e.data.type === "flush") {
        this.flush();
        this.port.postMessage({ type: "flushed", requestId: e.data.requestId });
      }
    };
  }

  flush() {
    if (!this.pos) return;
    const pcm = this.buf.slice(0, this.pos);
    this.port.postMessage({ type: "chunk", pcm: pcm.buffer, frames: this.pos, peak: this.peak }, [pcm.buffer]);
    this.pos = 0;
    this.peak = 0;
  }

  process(inputs) {
    if (this.paused) return true;
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channels = input.length;
    const frames = input[0].length;
    for (let i = 0; i < frames; i++) {
      let s = 0;
      for (let c = 0; c < channels; c++) s += input[c][i];
      s = this.muted ? 0 : s / channels;
      const a = Math.abs(s);
      if (a > this.peak) this.peak = a;
      const v = Math.max(-1, Math.min(1, s));
      this.buf[this.pos++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.pos === this.chunkFrames) {
        this.port.postMessage({ type: "chunk", pcm: this.buf.buffer, frames: this.chunkFrames, peak: this.peak }, [this.buf.buffer]);
        this.buf = new Int16Array(this.chunkFrames);
        this.pos = 0;
        this.peak = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-chunker", PcmChunker);
