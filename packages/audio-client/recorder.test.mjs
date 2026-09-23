import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

// Execute the production module with deterministic device and transport boundaries.
class Client {
  constructor(opts) { this.opts = opts; this.sources = new Map(); this.events = []; this.buffered = 0; this.saved = true; }
  addSource(sourceId, kind) { const source = { sourceId, kind }; this.sources.set(sourceId, source); return source; }
  connect() { this.opts.onStatus({ type: 'connected' }); }
  pushPcm(_id, pcm) { this.events.push('pcm'); this.buffered += pcm.length / 48000; }
  unackedSeconds() { return this.buffered; }
  setSourceState() {}
  async stop() {
    this.events.push('stop');
    if (this.saved) { this.buffered = 0; this.opts.onStatus({ type: 'session_stopped' }); }
    return this.saved;
  }
  disconnect() {}
}
globalThis.__RecorderTestClient = Client;
const source = readFileSync(new URL('../../apps/web/static/recorder.js', import.meta.url), 'utf8')
  .replace("import { IngestClient } from '/lib/uploader.js';", 'const IngestClient = globalThis.__RecorderTestClient;')
  .replace("import { api as defaultApi } from './api.js';", 'const defaultApi = null;');
const { BrowserRecorder } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function track(kind) {
  return { kind, stopped: false, stop() { this.stopped = true; }, addEventListener() {} };
}
function stream(audio = true, video = false) {
  const tracks = [...(audio ? [track('audio')] : []), ...(video ? [track('video')] : [])];
  return { getTracks: () => tracks, getAudioTracks: () => tracks.filter(t => t.kind === 'audio') };
}
class Context {
  constructor() { this.sampleRate = 48000; this.state = 'running'; this.audioWorklet = { addModule: async () => {} }; }
  async resume() { this.state = 'running'; }
  async close() { this.state = 'closed'; }
  createGain() { return { gain: {}, connect() {}, disconnect() {} }; }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
}
class Node {
  constructor() {
    this.port = {
      close() {},
      postMessage: message => queueMicrotask(() => {
        if (message.paused) this.port.onmessage({ data: { type: 'chunk', pcm: new Int16Array([1, 2]).buffer, peak: 0.1 } });
        this.port.onmessage({ data: { type: 'paused', requestId: message.requestId } });
      }),
    };
  }
  connect() {}
  disconnect() {}
}

function setup(t, overrides = {}) {
  const mic = stream(), tab = stream(true, true), requests = [], states = [], listeners = new Map();
  const values = {
    isSecureContext: true,
    window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) },
    location: { href: 'http://localhost:8000/#/m/test', protocol: 'http:' },
    navigator: { platform: 'test', mediaDevices: { getUserMedia: async () => mic, getDisplayMedia: async () => tab } },
    AudioContext: Context, AudioWorkletNode: Node,
    ...overrides,
  };
  const originals = new Map();
  for (const [name, value] of Object.entries(values)) {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  }
  const recorder = new BrowserRecorder({ meetingId: 'test', onState: state => states.push(state), api: async (...args) => {
    requests.push(args); return { id: 'capture-test' };
  } });
  t.after(async () => {
    clearInterval(recorder.timer);
    await recorder._releaseMedia();
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  return { recorder, mic, tab, requests, states, listeners };
}

test('microphone flow flushes the final chunk, then saves and releases every resource', async t => {
  const { recorder, mic, requests, listeners } = setup(t);
  await recorder.start();
  assert.equal(requests[0][2].mode, 'local_mic');
  assert.equal(recorder.status, 'recording');
  assert.equal(recorder.client.sources.get('mic').kind, 'local_microphone');
  const ctx = recorder.ctx;
  const saved = await recorder.stop();
  assert.equal(saved.status, 'stopped');
  assert.deepEqual(recorder.client.events, ['pcm', 'stop']);
  assert.ok(mic.getTracks().every(t => t.stopped));
  assert.equal(ctx.state, 'closed');
  assert.equal(listeners.has('beforeunload'), false);
});

test('Meet/Zoom tab and optional microphone stay separate on the same capture session', async t => {
  const { recorder, requests } = setup(t);
  await recorder.start({ source: 'tab', includeMicrophone: true });
  assert.equal(requests[0][2].mode, 'browser_tab');
  assert.equal(recorder.client.sources.get('tab').kind, 'tab_audio');
  assert.equal(recorder.client.sources.get('mic').kind, 'microphone');
  assert.equal(recorder.client.sources.get('tab').epochStartWallUs, recorder.client.sources.get('mic').epochStartWallUs);
});

test('tab without shared audio is rejected before session creation and releases the screen', async t => {
  const tab = stream(false, true);
  const { recorder, requests } = setup(t, {
    navigator: { mediaDevices: { getUserMedia: async () => stream(), getDisplayMedia: async () => tab } },
  });
  await assert.rejects(recorder.start({ source: 'tab' }), /передавать звук вкладки/);
  assert.equal(requests.length, 0);
  assert.ok(tab.getTracks().every(t => t.stopped));
  assert.equal(recorder.status, 'error');
});

test('permission rejection gives an actionable message without creating a capture', async t => {
  const denied = Object.assign(new Error('Denied'), { name: 'NotAllowedError' });
  const { recorder, requests } = setup(t, {
    navigator: { mediaDevices: { getUserMedia: async () => { throw denied; } } },
  });
  await assert.rejects(recorder.start(), /Разрешите его в настройках сайта/);
  assert.equal(requests.length, 0);
  assert.equal(recorder.active, false);
});

test('pause freezes timer, resume returns to recording, and stop remains definitive during resume', async t => {
  const { recorder, states } = setup(t);
  await recorder.start();
  await recorder.pause();
  assert.equal(recorder.status, 'paused');
  assert.equal(recorder.runningSince, null);
  let releaseResume;
  recorder.ctx.resume = () => new Promise(resolve => { releaseResume = resolve; });
  const resuming = recorder.resume();
  const stopping = recorder.stop();
  assert.equal(recorder.status, 'saving');
  releaseResume();
  await Promise.all([resuming, stopping]);
  assert.equal(recorder.status, 'stopped');
  const savingIndex = states.findIndex(state => state.status === 'saving');
  assert.equal(states.slice(savingIndex).some(state => state.status === 'recording'), false);
  assert.equal(recorder.runningSince, null);
});

test('unconfirmed save retains buffered audio and unload guard until retry succeeds', async t => {
  const { recorder, listeners } = setup(t);
  await recorder.start();
  recorder.client.saved = false;
  assert.equal((await recorder.stop()).status, 'recoverable');
  assert.ok(recorder.snapshot().bufferedSeconds > 0);
  assert.equal(listeners.has('beforeunload'), true);
  recorder.client.saved = true;
  assert.equal((await recorder.retryStop()).status, 'stopped');
  assert.equal(listeners.has('beforeunload'), false);
});

test('unexpected server finalization immediately releases active microphone and context', async t => {
  const { recorder, mic } = setup(t);
  await recorder.start();
  const ctx = recorder.ctx;
  recorder.client.opts.onStatus({ type: 'session_stopped' });
  assert.equal(recorder.status, 'stopped');
  assert.ok(mic.getTracks().every(t => t.stopped));
  assert.equal(ctx.state, 'closed');
  assert.equal(recorder.ctx, null);
});
