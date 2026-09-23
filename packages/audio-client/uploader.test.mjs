import assert from 'node:assert/strict';
import { test } from 'node:test';
import { IngestClient } from './uploader.js';

class Socket {
  static OPEN = 1; static CONNECTING = 0; static CLOSING = 2;
  constructor() { this.readyState = 0; this.sent = []; }
  open() { this.readyState = 1; this.onopen(); }
  send(value) { this.sent.push(typeof value === 'string' ? JSON.parse(value) : value); }
  receive(value) { this.onmessage({ data: JSON.stringify(value) }); }
  close(code = 1006) { this.readyState = 3; this.onclose({ code }); }
  messages(type) { return this.sent.filter(value => value.type === type); }
}

function setup(t, online = true) {
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = Socket;
  const client = new IngestClient({ wsUrl: 'ws://localhost/audio', captureSessionId: 'session', client: { name: 'test', version: '1' } });
  const source = client.addSource('mic', 'local_microphone', 16000);
  t.after(() => { client.disconnect(); globalThis.WebSocket = previousWebSocket; });
  client.connect();
  if (online) connect(client, source);
  return { client, source, ws: client.ws };
}

function connect(client, source, resume = 0) {
  client.ws.open();
  client.ws.receive({ type: 'welcome', limits: {} });
  client.ws.receive({ type: 'source_ready', source_id: 'mic', source_index: 0, capture_epoch: source.epoch, resume_from_sequence: resume });
}
function ack(ws, source, sequence = 0) {
  ws.receive({ type: 'ack', source_index: 0, capture_epoch: source.epoch, durable_sequence: sequence, durable_sample: (sequence + 1) * 1600 });
}
function closed(ws, source, sequence = 0) {
  ws.receive({ type: 'source_closed', source_index: 0, capture_epoch: source.epoch, durable_sequence: sequence });
}

test('source_open includes the same epoch used by PCM frames', t => {
  const { client, source, ws } = setup(t);
  client.pushPcm('mic', new Int16Array(1600));
  const frame = ws.sent.find(value => value instanceof ArrayBuffer);
  assert.equal(ws.messages('source_open')[0].capture_epoch, source.epoch);
  assert.equal(new DataView(frame).getUint32(12, true), source.epoch);
});

test('stop waits for durable audio ACK and source_closed before finalization', async t => {
  const { client, source, ws } = setup(t);
  client.pushPcm('mic', new Int16Array(1600));
  const stopped = client.stop();
  assert.equal(ws.messages('source_close').length, 0);
  assert.equal(ws.messages('stop_session').length, 0);
  ack(ws, source);
  assert.equal(ws.messages('source_close').length, 1);
  assert.equal(ws.messages('stop_session').length, 0);
  closed(ws, source);
  assert.equal(ws.messages('stop_session').length, 1);
  ws.receive({ type: 'session_stopped' });
  assert.equal(await stopped, true);
  assert.equal(client.unackedSeconds(), 0);
});

test('offline stop reconnects, replays unacked audio and finalizes', async t => {
  const { client, source, ws } = setup(t);
  ws.close();
  client.pushPcm('mic', new Int16Array(1600));
  const stopped = client.stop();
  const resumed = client.ws;
  assert.notEqual(resumed, ws);
  connect(client, source);
  assert.equal(resumed.sent.filter(value => value instanceof ArrayBuffer).length, 1);
  ack(resumed, source);
  closed(resumed, source);
  assert.equal(resumed.messages('stop_session').length, 1);
  resumed.receive({ type: 'session_stopped' });
  assert.equal(await stopped, true);
});

test('timed out stop keeps audio for a later retry instead of reporting success', async t => {
  const { client, source, ws } = setup(t);
  client.pushPcm('mic', new Int16Array(1600));
  assert.equal(await client.stop('user_stop', 1), false);
  assert.equal(client.unackedSeconds(), 0.1);
  const retry = client.stop();
  ack(ws, source); closed(ws, source);
  ws.receive({ type: 'session_stopped' });
  assert.equal(await retry, true);
});

test('lost close acknowledgment reopens source on reconnect', async t => {
  const { client, source, ws } = setup(t);
  client.pushPcm('mic', new Int16Array(1600)); ack(ws, source);
  const stopped = client.stop();
  assert.equal(source.closeSent, true);
  ws.close(); client.connect(); connect(client, source, 1);
  const resumed = client.ws;
  assert.equal(resumed.messages('source_open').length, 1);
  assert.equal(resumed.messages('source_close').length, 1);
  closed(resumed, source);
  resumed.receive({ type: 'session_stopped' });
  assert.equal(await stopped, true);
});

test('normal websocket close without session_stopped is not proof of saved audio', async t => {
  const { client, ws } = setup(t);
  client.pushPcm('mic', new Int16Array(1600));
  const stopped = client.stop('user_stop', 1);
  ws.close(1000);
  assert.equal(await stopped, false);
  assert.equal(client.unackedSeconds(), 0.1);
});

test('fatal ingest errors preserve buffer and stop retrying', async t => {
  const { client, ws } = setup(t);
  client.pushPcm('mic', new Int16Array(1600));
  const stopped = client.stop();
  ws.receive({ type: 'error', code: 'unauthorized', message: 'Session expired', fatal: true });
  assert.equal(await stopped, false);
  assert.equal(client.unackedSeconds(), 0.1);
  assert.equal(client.stopped, false);
});
