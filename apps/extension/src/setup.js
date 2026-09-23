// Visible page: the only place where the microphone permission prompt can appear for the extension origin.
const $ = (id) => document.getElementById(id);
let stream = null;

async function listDevices() {
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  const { micDeviceId } = await chrome.storage.local.get("micDeviceId");
  $("devices").innerHTML = "";
  for (const d of devices) {
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || "Микрофон";
    o.selected = d.deviceId === micDeviceId;
    $("devices").append(o);
  }
}

async function preview(deviceId) {
  stream?.getTracks().forEach((t) => t.stop());
  stream = await navigator.mediaDevices.getUserMedia({ audio: deviceId ? { deviceId: { exact: deviceId } } : true });
  const ctx = new AudioContext();
  const an = ctx.createAnalyser();
  ctx.createMediaStreamSource(stream).connect(an); // analyser only: never routed to speakers
  const buf = new Float32Array(an.fftSize);
  const tick = () => {
    an.getFloatTimeDomainData(buf);
    const peak = buf.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
    $("level").value = peak;
    $("hint").textContent = peak < 0.01 ? "тишина — проверьте устройство" : "сигнал есть";
    requestAnimationFrame(tick);
  };
  tick();
}

$("grant").addEventListener("click", async () => {
  try {
    await preview();
    await listDevices();
  } catch (e) { $("error").textContent = `Доступ не выдан: ${e.message || e}`; }
});

$("devices").addEventListener("change", async (e) => {
  await chrome.storage.local.set({ micDeviceId: e.target.value });
  await preview(e.target.value);
});

listDevices().catch(() => {});
