import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

function worklet(chunkFrames = 4) {
  const posted = [];
  let Processor;
  runInNewContext(readFileSync(new URL('./pcm-worklet.js', import.meta.url), 'utf8'), {
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: message => posted.push(message) }; } },
    registerProcessor(_name, type) { Processor = type; },
  });
  return { processor: new Processor({ processorOptions: { chunkFrames } }), posted };
}

test('pause flushes a partial PCM tail before its acknowledgment and excludes paused speech', () => {
  const { processor, posted } = worklet();
  processor.process([[new Float32Array([0.5, -0.5])]]);
  assert.equal(posted.length, 0);
  processor.port.onmessage({ data: { type: 'pause', paused: true, requestId: 1 } });
  assert.equal(posted[0].type, 'chunk');
  assert.equal(posted[0].frames, 2);
  assert.equal(posted[1].type, 'paused');
  processor.process([[new Float32Array([1, 1, 1, 1])]]);
  assert.equal(posted.length, 2);
  processor.port.onmessage({ data: { type: 'pause', paused: false, requestId: 2 } });
  processor.process([[new Float32Array([0.25, 0.25])]]);
  processor.port.onmessage({ data: { type: 'flush', requestId: 3 } });
  const chunks = posted.filter(item => item.type === 'chunk');
  assert.equal(chunks.reduce((sum, item) => sum + item.frames, 0), 4);
  assert.deepEqual(Array.from(new Int16Array(chunks[1].pcm)), [8191, 8191]);
});

test('peak level spans render blocks until the whole chunk is emitted', () => {
  const { processor, posted } = worklet();
  processor.process([[new Float32Array([1, 0.1])]]);
  processor.process([[new Float32Array([0.1, 0.1])]]);
  assert.equal(posted[0].peak, 1);
});

test('muted microphone produces zeros rather than private speech', () => {
  const { processor, posted } = worklet();
  processor.port.onmessage({ data: { type: 'mute', muted: true } });
  processor.process([[new Float32Array([1, -1, 0.5, 0.5])]]);
  assert.deepEqual(Array.from(new Int16Array(posted[0].pcm)), [0, 0, 0, 0]);
});
