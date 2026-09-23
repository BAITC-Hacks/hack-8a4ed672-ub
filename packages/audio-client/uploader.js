// Resilient uploader for hattama.audio.v1: bounded unacked buffer, ACK-driven release, reconnect + resume,
// explicit gap_report on overflow (audio is never dropped silently).
import { encodeFrame, hello, nowWallUs, sourceOpen } from "./protocol.js";

export class IngestClient {
  /**
   * @param {{wsUrl:string, token?:string|null, captureSessionId?:string|null, client:{name:string,version:string,platform?:string},
   *          maxBufferSeconds?:number, onStatus?:(s:object)=>void}} opts
   */
  constructor(opts) {
    this.opts = { maxBufferSeconds: 120, ...opts };
    this.sources = new Map();
    this.ws = null;
    this.connected = false;
    this.stopping = false;
    this.stopped = false;
    this.retry = 0;
    this.onStatus = opts.onStatus || (() => {});
    this._stopWaiters = [];
  }

  addSource(sourceId, kind, sampleRate, label = "") {
    const s = {
      sourceId, kind, sampleRate, label, channelCount: 1,
      epoch: Math.floor(Date.now() / 1000) % 2147483647,
      epochStartWallUs: nowWallUs(),
      index: null, ready: false, seq: 0, startSample: 0, unacked: [], ackedSeq: -1, bufferedSamples: 0,
      pendingGap: null, closed: false, closeSent: false,
    };
    this.sources.set(sourceId, s);
    if (this.connected) this.ws.send(sourceOpen(s));
    return s;
  }

  pushPcm(sourceId, pcm) {
    const s = this.sources.get(sourceId);
    if (!s || s.closed) return;
    const frame = { seq: s.seq, start: s.startSample, count: pcm.length, pcm,
                    tsUs: s.epochStartWallUs + Math.round((s.startSample / s.sampleRate) * 1e6) };
    s.seq += 1;
    s.startSample += pcm.length;
    s.unacked.push(frame);
    s.bufferedSamples += pcm.length;
    const limit = this.opts.maxBufferSeconds * s.sampleRate;
    while (s.bufferedSamples > limit && s.unacked.length > 1) {
      const lost = s.unacked.shift();
      s.bufferedSamples -= lost.count;
      if (s.pendingGap) { s.pendingGap.to_sequence = lost.seq; s.pendingGap.lost_samples += lost.count; }
      else s.pendingGap = { from_sequence: lost.seq, to_sequence: lost.seq, start_sample: lost.start, lost_samples: lost.count };
      this.onStatus({ type: "overflow", sourceId, lostSeconds: lost.count / s.sampleRate });
    }
    if (this.connected && s.ready) this._sendFrame(s, frame);
    this.onStatus({ type: "buffer", sourceId, seconds: s.bufferedSamples / s.sampleRate });
  }

  _sendFrame(s, f) {
    this.ws.send(encodeFrame({ sourceIndex: s.index, channelCount: 1, captureEpoch: s.epoch, sequence: f.seq,
      sampleRate: s.sampleRate, sampleCount: f.count, captureTimestampUs: f.tsUs, startSample: f.start }, f.pcm));
  }

  connect() {
    if (this.stopped) return;
    const ws = new WebSocket(this.opts.wsUrl);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      ws.send(hello({ token: this.opts.token || null, captureSessionId: this.opts.captureSessionId || null,
                      client: this.opts.client }));
    };
    ws.onmessage = (ev) => this._onMessage(JSON.parse(ev.data));
    ws.onclose = (ev) => {
      this.connected = false;
      for (const s of this.sources.values()) s.ready = false;
      this.onStatus({ type: "disconnected", code: ev.code });
      if (this.stopped || ev.code === 4401 || ev.code === 4403 || ev.code === 4409) {
        this._resolveStop(ev.code === 1000);
        return;
      }
      const delay = Math.min(10000, 1000 * 2 ** Math.min(this.retry++, 4));
      this.onStatus({ type: "reconnecting", inMs: delay });
      setTimeout(() => this.connect(), delay);
    };
  }

  _onMessage(m) {
    switch (m.type) {
      case "welcome":
        this.connected = true;
        this.retry = 0;
        this.onStatus({ type: "connected", limits: m.limits });
        for (const s of this.sources.values()) if (!s.closeSent) this.ws.send(sourceOpen(s));
        break;
      case "source_ready": {
        const s = this.sources.get(m.source_id);
        if (!s) return;
        s.index = m.source_index;
        s.ready = true;
        if (s.pendingGap) {
          this.ws.send(JSON.stringify({ type: "gap_report", source_index: s.index, capture_epoch: s.epoch,
            reason: "client_buffer_overflow", ...s.pendingGap }));
          s.pendingGap = null;
        }
        const resume = m.resume_from_sequence;
        this._release(s, resume - 1);
        for (const f of s.unacked) this._sendFrame(s, f);
        if (s.closed && !s.closeSent) this._sendClose(s, s.closeReason);
        break;
      }
      case "ack": {
        for (const s of this.sources.values()) {
          if (s.index === m.source_index && s.epoch === m.capture_epoch) {
            this._release(s, m.durable_sequence);
            this.onStatus({ type: "ack", sourceId: s.sourceId, durableSeconds: m.durable_sample / s.sampleRate,
                            bufferedSeconds: s.bufferedSamples / s.sampleRate });
          }
        }
        break;
      }
      case "gap_recorded":
        this.onStatus({ type: "gap", ...m });
        break;
      case "stop_requested":
        this.onStatus({ type: "stop_requested" });
        break;
      case "session_stopped":
        this.stopped = true;
        this.onStatus({ type: "session_stopped" });
        this._resolveStop(true);
        break;
      case "error":
        this.onStatus({ type: "error", code: m.code, message: m.message, fatal: m.fatal });
        if (m.fatal) this.stopped = m.code !== "superseded";
        break;
      default:
        break;
    }
  }

  _release(s, durableSeq) {
    s.ackedSeq = Math.max(s.ackedSeq, durableSeq);
    while (s.unacked.length && s.unacked[0].seq <= s.ackedSeq) s.bufferedSamples -= s.unacked.shift().count;
  }

  setSourceState(sourceId, state) {
    const s = this.sources.get(sourceId);
    if (s && this.connected && s.ready) {
      this.ws.send(JSON.stringify({ type: "source_state", source_index: s.index, state, at_us: nowWallUs() }));
    }
  }

  closeSource(sourceId, reason) {
    const s = this.sources.get(sourceId);
    if (!s || s.closed) return;
    s.closed = true;
    s.closeReason = reason;
    if (this.connected && s.ready) this._sendClose(s, reason);
  }

  _sendClose(s, reason) {
    s.closeSent = true;
    this.ws.send(JSON.stringify({ type: "source_close", source_index: s.index, capture_epoch: s.epoch,
      final_sequence: s.seq - 1, reason }));
  }

  /** Stop capture: close sources (server fsyncs and acks), then end the session. */
  stop(reason = "user_stop", timeoutMs = 15000) {
    this.stopping = true;
    return new Promise((resolve) => {
      this._stopWaiters.push(resolve);
      if (this.connected) this.ws.send(JSON.stringify({ type: "stop_session", reason }));
      for (const s of this.sources.values()) this.closeSource(s.sourceId, "stop_requested");
      setTimeout(() => this._resolveStop(false), timeoutMs);
    });
  }

  _resolveStop(ok) {
    const waiters = this._stopWaiters;
    this._stopWaiters = [];
    for (const w of waiters) w(ok);
    if (ok && this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.close(1000);
  }

  unackedSeconds() {
    let total = 0;
    for (const s of this.sources.values()) total += s.bufferedSamples / s.sampleRate;
    return total;
  }
}
