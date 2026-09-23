// Offscreen document: long-lived capture + WebSocket. Survives popup close and dashboard reloads.
import { IngestClient } from "./lib/uploader.js";

let ctx = null;
let tabStream = null;
let micStream = null;
let micNode = null;
let client = null;
let sink = null;
let micMuted = true;

function report(status) {
  const clean = Object.fromEntries(Object.entries(status).filter(([, v]) => v !== undefined));
  chrome.runtime.sendMessage({ type: "offscreen-status", status: clean }).catch(() => {});
}

function wsUrl(backend) {
  const u = new URL(backend);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = "/api/v1/ingest/ws";
  return u.toString();
}

async function addWorklet(stream, sourceId) {
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm-chunker", { processorOptions: { chunkFrames: Math.round(ctx.sampleRate * 0.2) } });
  node.port.onmessage = (e) => {
    if (e.data.type === "chunk") {
      client.pushPcm(sourceId, new Int16Array(e.data.pcm));
      report({ [`${sourceId}Peak`]: Math.round(e.data.peak * 100) / 100 });
    }
  };
  src.connect(node);
  node.connect(sink); // gain 0: keeps the node pulled by the graph without a second audible playback
  return { src, node };
}

async function start(streamId, pairing, version) {
  tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } }, video: false,
  });
  ctx = new AudioContext();
  await ctx.audioWorklet.addModule("lib/pcm-worklet.js");
  sink = ctx.createGain();
  sink.gain.value = 0;
  sink.connect(ctx.destination);
  client = new IngestClient({
    wsUrl: wsUrl(pairing.backend), token: pairing.token,
    client: { name: "hattama-companion", version, platform: navigator.userAgentData?.platform || "chrome" },
    onStatus: (s) => {
      if (s.type === "stop_requested") stop("stop_requested");
      if (s.type === "session_stopped") finish();
      report({ last: s.type, ...(s.type === "ack" ? { durableSeconds: s.durableSeconds, bufferedSeconds: s.bufferedSeconds } : {}),
               ...(s.type === "error" ? { error: `${s.code}: ${s.message}` } : {}),
               ...(s.type === "gap" ? { gaps: (window.__gaps = (window.__gaps || 0) + 1) } : {}),
               connected: s.type === "connected" ? true : s.type === "disconnected" ? false : undefined });
    },
  });
  client.addSource("tab", "tab_audio", ctx.sampleRate, "Звук вкладки встречи");
  const tab = await addWorklet(tabStream, "tab");
  // Tab capture mutes the tab for the user; route it back ONCE so the user keeps hearing the meeting.
  tab.src.connect(ctx.destination);
  tabStream.getAudioTracks()[0].addEventListener("ended", () => {
    client.closeSource("tab", "track_ended");
    report({ tabEnded: true, error: "Захват вкладки завершён (вкладка закрыта или захват остановлен)" });
  });
  client.connect();
  report({ capturing: true, sampleRate: ctx.sampleRate });
}

async function setMic(enabled, deviceId) {
  if (!ctx) return;
  if (enabled && !micStream) {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: deviceId ? { exact: deviceId } : undefined, echoCancellation: true, noiseSuppression: true },
    });
    client.addSource("mic", "microphone", ctx.sampleRate, "Собственный микрофон");
    const mic = await addWorklet(micStream, "mic"); // NOT connected to speakers
    micNode = mic.node;
    micMuted = false;
    micStream.getAudioTracks()[0].addEventListener("ended", () => client.closeSource("mic", "track_ended"));
    report({ micActive: true, micMuted: false });
  } else if (!enabled && micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    client.closeSource("mic", "user_stop");
    micStream = null;
    micNode = null;
    report({ micActive: false, micMuted: true });
  }
}

function setMute(muted) {
  if (!micNode) return;
  micMuted = muted;
  micNode.port.postMessage({ type: "mute", muted }); // zeros keep the timeline; nothing private is sent
  client.setSourceState("mic", muted ? "muted" : "unmuted");
  report({ micMuted: muted });
}

async function stop(reason = "user_stop") {
  if (!client) return;
  tabStream?.getTracks().forEach((t) => t.stop());
  micStream?.getTracks().forEach((t) => t.stop());
  const ok = await client.stop(reason);
  report({ stopConfirmed: ok, unackedSeconds: client.unackedSeconds() });
  finish();
}

function finish() {
  ctx?.close();
  report({ finished: true, capturing: false });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.target !== "offscreen") return false;
  (async () => {
    try {
      if (msg.type === "start") await start(msg.streamId, msg.pairing, msg.version);
      else if (msg.type === "mic") await setMic(msg.enabled, msg.deviceId);
      else if (msg.type === "mute") setMute(msg.muted);
      else if (msg.type === "stop") await stop("user_stop");
      else if (msg.type === "tab_closed") client?.closeSource("tab", "tab_closed");
      sendResponse({ ok: true });
    } catch (e) {
      report({ error: String(e.message || e) });
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true;
});
