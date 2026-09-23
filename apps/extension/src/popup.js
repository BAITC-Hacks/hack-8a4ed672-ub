const $ = (id) => document.getElementById(id);
const send = (msg) => chrome.runtime.sendMessage(msg);

function showError(text) { $("error").textContent = text || ""; }

async function refresh() {
  const { status = {}, pairing } = await send({ type: "get-status" });
  $("pair").hidden = !!pairing || status.capturing;
  $("ready").hidden = !pairing || !!status.capturing;
  $("live").hidden = !status.capturing;
  if (pairing) $("meeting").textContent = pairing.meeting_title;
  $("conn").textContent = status.connected === false ? "нет соединения — буферизация" : status.connected ? "подключено" : "—";
  $("durable").textContent = `${(status.durableSeconds || 0).toFixed(1)} c`;
  $("buffered").textContent = `${(status.bufferedSeconds || 0).toFixed(1)} c`;
  $("tabLevel").value = status.tabPeak || 0;
  $("micLevel").value = status.micPeak || 0;
  const micOn = status.micActive && !status.micMuted;
  $("micState").textContent = status.micActive ? (status.micMuted ? "подключён, звук выключен" : "ЗАПИСЫВАЕТСЯ") : "выключен";
  $("micBox").className = micOn ? "mic-on" : "mic-off";
  $("micBtn").textContent = status.micActive ? "Отключить микрофон" : "Включить микрофон";
  $("muteBtn").hidden = !status.micActive;
  $("muteBtn").textContent = status.micMuted ? "Включить звук микрофона" : "Выключить звук микрофона";
  showError(status.error);
  return status;
}

$("pairBtn").addEventListener("click", async () => {
  showError("");
  try {
    const backend = new URL($("backend").value).origin;
    const granted = await chrome.permissions.request({ origins: [`${backend}/*`] });
    if (!granted) throw new Error("Без доступа к серверу подключение невозможно");
    const r = await fetch(`${backend}/api/v1/pairing/exchange`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ code: $("code").value.trim(), client_name: "hattama-companion",
                             client_version: chrome.runtime.getManifest().version }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    // short-lived token: kept in session storage only (cleared when the browser closes)
    await chrome.storage.session.set({ pairing: { ...data, backend } });
    await refresh();
  } catch (e) { showError(String(e.message || e)); }
});

$("startBtn").addEventListener("click", async () => {
  showError("");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const res = await send({ type: "start", streamId, tabId: tab.id });
    if (!res.ok) throw new Error(res.error);
    setTimeout(refresh, 500);
  } catch (e) { showError(`Захват не начат: ${e.message || e}`); }
});

$("micBtn").addEventListener("click", async () => {
  const status = await refresh();
  const { micDeviceId } = await chrome.storage.local.get("micDeviceId");
  const res = await send({ type: "mic", enabled: !status.micActive, deviceId: micDeviceId });
  if (!res.ok) showError(`Микрофон: ${res.error}. Сначала разрешите доступ на странице настройки.`);
  setTimeout(refresh, 300);
});

$("muteBtn").addEventListener("click", async () => {
  const status = await refresh();
  await send({ type: "mute", muted: !status.micMuted });
  setTimeout(refresh, 200);
});

$("stopBtn").addEventListener("click", async () => {
  await send({ type: "stop" });
  setTimeout(refresh, 1000);
});

refresh();
setInterval(refresh, 1000);
