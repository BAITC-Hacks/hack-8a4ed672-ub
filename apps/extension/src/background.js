// Service worker: owns no audio. Creates the offscreen document (which holds the MediaStream + WebSocket),
// relays popup commands, tracks the captured tab and shows REC/MIC badges. State lives in storage.session
// so a suspended/restarted service worker does not lose it.
const OFFSCREEN = "offscreen.html";

async function ensureOffscreen() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN,
    reasons: ["USER_MEDIA"],
    justification: "Захват звука вкладки встречи и микрофона для локального протоколирования",
  });
}

async function setStatus(patch) {
  const { status = {} } = await chrome.storage.session.get("status");
  const next = { ...status, ...patch, updatedAt: Date.now() };
  await chrome.storage.session.set({ status: next });
  const rec = next.capturing;
  await chrome.action.setBadgeText({ text: rec ? (next.micActive && !next.micMuted ? "MIC" : "REC") : "" });
  await chrome.action.setBadgeBackgroundColor({ color: next.micActive && !next.micMuted ? "#b91c1c" : "#1d4ed8" });
  return next;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target === "offscreen") return false;
  (async () => {
    switch (msg.type) {
      case "start": {
        const { pairing } = await chrome.storage.session.get("pairing");
        if (!pairing) throw new Error("Нет сопряжения с сервером");
        await ensureOffscreen();
        await setStatus({ capturing: true, tabId: msg.tabId, micActive: false, micMuted: true, error: null });
        await chrome.runtime.sendMessage({ target: "offscreen", type: "start", streamId: msg.streamId, pairing,
          version: chrome.runtime.getManifest().version });
        return { ok: true };
      }
      case "mic":
      case "mute":
      case "stop":
        await chrome.runtime.sendMessage({ target: "offscreen", ...msg });
        return { ok: true };
      case "offscreen-status": {
        const next = await setStatus(msg.status);
        if (msg.status.finished) {
          await setStatus({ capturing: false, micActive: false });
          try { await chrome.offscreen.closeDocument(); } catch (_) { /* already closed */ }
        }
        return next;
      }
      case "get-status": {
        const { status = {}, pairing = null } = await chrome.storage.session.get(["status", "pairing"]);
        return { status, pairing: pairing && { meeting_title: pairing.meeting_title, backend: pairing.backend,
                                               expires_at: pairing.expires_at } };
      }
      default:
        return { ok: false };
    }
  })().then(sendResponse, (e) => sendResponse({ ok: false, error: String(e.message || e) }));
  return true;
});

// Closing the meeting tab ends the tab source (the capture track also fires `ended`).
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { status = {} } = await chrome.storage.session.get("status");
  if (status.capturing && status.tabId === tabId) {
    await chrome.runtime.sendMessage({ target: "offscreen", type: "tab_closed" }).catch(() => {});
  }
});
