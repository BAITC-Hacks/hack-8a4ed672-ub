// Protocol hattama.audio.v1 — binary frame encoder (see packages/contracts/audio-frame-v1.md).
// Little-endian, 48-byte header, PCM s16le payload. Verified against packages/contracts/test-vectors.
export const PROTOCOL = "hattama.audio.v1";
export const HEADER_LENGTH = 48;
const MAGIC = [0x48, 0x54, 0x41, 0x31]; // "HTA1"

/**
 * @param {{sourceIndex:number, channelCount:number, captureEpoch:number, sequence:bigint|number,
 *          sampleRate:number, sampleCount:number, captureTimestampUs:bigint|number, startSample:bigint|number}} f
 * @param {Int16Array} pcm interleaved samples, length = sampleCount * channelCount
 */
export function encodeFrame(f, pcm) {
  if (pcm.length !== f.sampleCount * f.channelCount) throw new Error("payload length mismatch");
  const buf = new ArrayBuffer(HEADER_LENGTH + pcm.length * 2);
  const v = new DataView(buf);
  MAGIC.forEach((b, i) => v.setUint8(i, b));
  v.setUint8(4, 1); // version
  v.setUint8(5, 1); // kind: PCM s16le
  v.setUint16(6, HEADER_LENGTH, true);
  v.setUint16(8, f.sourceIndex, true);
  v.setUint16(10, f.channelCount, true);
  v.setUint32(12, f.captureEpoch, true);
  v.setBigUint64(16, BigInt(f.sequence), true);
  v.setUint32(24, f.sampleRate, true);
  v.setUint32(28, f.sampleCount, true);
  v.setBigInt64(32, BigInt(f.captureTimestampUs), true);
  v.setBigUint64(40, BigInt(f.startSample), true);
  const out = new Int16Array(buf, HEADER_LENGTH);
  if (isLittleEndianHost()) out.set(pcm);
  else for (let i = 0; i < pcm.length; i++) v.setInt16(HEADER_LENGTH + i * 2, pcm[i], true);
  return buf;
}

let _le;
function isLittleEndianHost() {
  if (_le === undefined) _le = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
  return _le;
}

export function hello({ token = null, captureSessionId = null, client }) {
  return JSON.stringify({ type: "hello", protocol: PROTOCOL, token, capture_session_id: captureSessionId, client });
}

export function sourceOpen({ sourceId, kind, sampleRate, channelCount, captureEpoch, epoch, epochStartWallUs, label = "" }) {
  return JSON.stringify({
    type: "source_open", source_id: sourceId, kind, sample_rate: sampleRate, channel_count: channelCount,
    capture_epoch: captureEpoch ?? epoch, epoch_start_wall_us: Math.round(epochStartWallUs), label,
  });
}

export function nowWallUs() {
  return Math.round((performance.timeOrigin + performance.now()) * 1000);
}
