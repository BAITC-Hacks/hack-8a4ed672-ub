// node --test packages/audio-client — cross-language check against the Python codec's test vectors.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { encodeFrame } from "./protocol.js";

const vectors = JSON.parse(readFileSync(new URL("../contracts/test-vectors/audio-frame-v1.json", import.meta.url)));

test("JS encoder matches shared test vectors", () => {
  for (const v of vectors.vectors) {
    const f = v.frame;
    const bytes = Buffer.from(f.payload_hex, "hex");
    const pcm = new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length));
    const out = encodeFrame({ sourceIndex: f.source_index, channelCount: f.channel_count, captureEpoch: f.capture_epoch,
      sequence: BigInt(f.sequence), sampleRate: f.sample_rate, sampleCount: f.sample_count,
      captureTimestampUs: BigInt(f.capture_timestamp_us), startSample: BigInt(f.start_sample) }, pcm);
    assert.equal(Buffer.from(out).toString("hex"), v.hex, v.name);
  }
});

test("rejects wrong payload length", () => {
  assert.throws(() => encodeFrame({ sourceIndex: 0, channelCount: 1, captureEpoch: 0, sequence: 0, sampleRate: 16000,
    sampleCount: 2, captureTimestampUs: 0, startSample: 0 }, new Int16Array(3)));
});
