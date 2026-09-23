// Хаттама Live — minimal dashboard (no framework, no CDN). Same-origin API with HttpOnly session cookie + CSRF header.
import { IngestClient } from "/lib/uploader.js";

const app = document.getElementById("app");
let csrf = null;
let me = null;
let liveSocket = null;
let localCapture = null;

// i18n scaffold: Russian UI; Kazakh strings are added here (fallback to Russian until translated & reviewed).
const I18N = { ru: {}, kk: { "Встречи": "Кездесулер", "Поручения": "Тапсырмалар" } };
const lang = localStorage.getItem("lang") || "ru";
const t = (s) => (I18N[lang] && I18N[lang][s]) || s;

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtMs = (ms) => { const s = Math.floor(ms / 1000); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };

async function api(method, path, body) {
  const r = await fetch(`/api/v1${path}`, {
    method, credentials: "same-origin",
    headers: { ...(body !== undefined ? { "content-type": "application/json" } : {}), ...(csrf ? { "x-csrf-token": csrf } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { csrf = null; location.hash = "#/login"; throw new Error("Требуется вход"); }
  const data = r.status === 204 ? null : await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || `HTTP ${r.status}`);
  return data;
}

function render(html) { app.innerHTML = html; app.focus(); }
function showError(e) { const el = document.getElementById("err"); if (el) el.textContent = e.message || String(e); else alert(e.message || e); }

// ------------------------------------------------------------------ views
async function viewLogin() {
  document.getElementById("nav").hidden = true;
  render(`<h1>Вход</h1><form id="f" class="panel" style="max-width:360px">
    <label>Email <input name="email" type="email" required autocomplete="username"></label>
    <label>Пароль <input name="password" type="password" required autocomplete="current-password"></label>
    <button class="primary">Войти</button><p id="err" class="error" role="alert"></p></form>`);
  document.getElementById("f").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const s = await api("POST", "/auth/login", { email: fd.get("email"), password: fd.get("password") });
      csrf = s.csrf_token; me = s.user; location.hash = "#/meetings";
    } catch (err) { showError(err); }
  };
}

async function viewMeetings() {
  const list = await api("GET", "/meetings");
  const today = new Date().toISOString().slice(0, 10);
  render(`<h1>${t("Встречи")}</h1>
  <table><thead><tr><th>Дата</th><th>Название</th><th>Статус</th><th>Поручения</th><th>Вопросы</th></tr></thead><tbody>
  ${list.map((m) => `<tr><td>${esc(m.meeting_date)} ${esc(m.start_time || "")}</td><td><a href="#/m/${m.id}">${esc(m.title)}</a></td>
    <td><span class="badge st-${m.status}">${m.status}</span></td><td>${m.actions}</td><td>${m.open_issues}</td></tr>`).join("") ||
    `<tr><td colspan="5">Встреч пока нет.</td></tr>`}</tbody></table>
  <h2>Новая встреча</h2>
  <form id="f" class="panel grid">
    <label>Название <input name="title" required maxlength="300"></label>
    <label>Дата встречи <input name="meeting_date" type="date" value="${today}" required></label>
    <label>Время <input name="start_time" type="time"></label>
    <label>Часовой пояс <input name="timezone" value="Asia/Almaty" required></label>
    <label>Язык <select name="language_mode"><option value="mixed">русский + казахский (смешанный)</option>
      <option value="ru">русский</option><option value="kk">казахский</option></select></label>
    <label>Платформа <select name="platform"><option value="google_meet">Google Meet</option><option value="teams_web">Teams Web</option>
      <option value="zoom_web">Zoom Web</option><option value="in_person">очная встреча</option></select></label>
    <label style="grid-column:1/-1">Участники — по одному в строке: <code>Имя; должность; отсутствует</code>; добавьте «(я)» к себе
      <textarea name="participants" rows="5" style="width:100%" placeholder="Ботагоз; менеджер по закупкам&#10;Ерлан; юрист; отсутствует&#10;Айжан (я); секретарь"></textarea></label>
    <div><button class="primary">Создать</button> <span id="err" class="error" role="alert"></span></div>
  </form>`);
  document.getElementById("f").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const participants = String(fd.get("participants") || "").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const [name, position, absent] = l.split(";").map((x) => (x || "").trim());
      return { display_name: name.replace("(я)", "").trim(), position: position || null, is_present: !/отсутств/i.test(absent || ""),
               is_self: name.includes("(я)") };
    });
    try {
      const m = await api("POST", "/meetings", { title: fd.get("title"), meeting_date: fd.get("meeting_date"),
        start_time: fd.get("start_time") || null, timezone: fd.get("timezone"), language_mode: fd.get("language_mode"),
        platform: fd.get("platform"), participants });
      location.hash = `#/m/${m.id}`;
    } catch (err) { showError(err); }
  };
}

async function viewMeeting(id) {
  const m = await api("GET", `/meetings/${id}`);
  const secretary = m.my_role === "secretary";
  render(`<h1>${esc(m.title)} <span class="badge st-${m.status}">${m.status}</span></h1>
  <p>${esc(m.meeting_date)} ${esc(m.start_time || "")} · ${esc(m.timezone)} · язык: ${esc(m.language_mode)} · версия протокола ${m.protocol_version}</p>
  <p>Участники: ${m.participants.map((p) => `${esc(p.display_name)}${p.is_present ? "" : " (отсутствует)"}${p.is_self ? " (я)" : ""}`).join(", ") || "—"}</p>
  <div id="consent"></div><div id="capture"></div><div id="live"></div><div id="review"></div><p id="err" class="error" role="alert"></p>`);
  if (m.consent_confirmed_at == null) {
    document.getElementById("consent").innerHTML = secretary ? `<div class="panel"><h2>Уведомление участников о записи</h2>
      <textarea id="ctext" rows="3" style="width:100%">${esc(m.default_consent_text)}</textarea>
      <label><input type="checkbox" id="cbox"> Я уведомил(а) участников о записи и расшифровке до начала захвата</label>
      <button id="cbtn" class="primary">Подтвердить</button></div>` : `<p>Секретарь ещё не подтвердил уведомление о записи.</p>`;
    const btn = document.getElementById("cbtn");
    if (btn) btn.onclick = async () => {
      if (!document.getElementById("cbox").checked) return showError(new Error("Отметьте подтверждение уведомления"));
      try { await api("POST", `/meetings/${id}/consent`, { confirmed: true, notice_text: document.getElementById("ctext").value }); route(); }
      catch (e) { showError(e); }
    };
  }
  if (secretary && ["READY", "LIVE"].includes(m.status)) renderCapture(m);
  if (["LIVE", "PROCESSING", "READY"].includes(m.status)) startLive(m);
  if (["PROCESSING", "NEEDS_REVIEW", "APPROVED", "FAILED"].includes(m.status)) renderReview(m);
}

function renderCapture(m) {
  const el = document.getElementById("capture");
  el.innerHTML = `<div class="panel"><h2>Источник звука</h2>
    <p>Звук собеседников из Google Meet / Teams Web / Zoom Web записывает расширение Chrome «Хаттама Live Companion».
      Настольные Zoom/Teams не поддерживаются. Очная встреча — микрофон этого компьютера.</p>
    <button id="ext">Расширение Chrome: получить код сопряжения</button>
    <button id="mic">Микрофон этого компьютера</button>
    <div id="pairing"></div>
    <button id="stop" class="danger">Остановить запись и начать обработку</button></div>`;
  document.getElementById("ext").onclick = async () => {
    try {
      const cs = await api("POST", `/meetings/${m.id}/capture-sessions`, { mode: "companion" });
      document.getElementById("pairing").innerHTML = `<p>Код для расширения (5 минут, одноразовый):</p><p class="code">${esc(cs.pairing.code)}</p>
        <p>Откройте вкладку встречи → значок расширения → введите сервер <code>${esc(location.origin)}</code> и код → «Начать запись этой вкладки».</p>`;
    } catch (e) { showError(e); }
  };
  document.getElementById("mic").onclick = () => startLocalMic(m).catch(showError);
  document.getElementById("stop").onclick = async () => {
    try {
      const active = await api("GET", `/meetings/${m.id}/capture-sessions/active`);
      if (localCapture) { await localCapture.stop(); localCapture = null; }
      else if (active.capture_session_id) await api("POST", `/capture-sessions/${active.capture_session_id}/stop`);
      setTimeout(route, 1500);
    } catch (e) { showError(e); }
  };
}

async function startLocalMic(m) {
  const cs = await api("POST", `/meetings/${m.id}/capture-sessions`, { mode: "local_mic" });
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule("/lib/pcm-worklet.js");
  const node = new AudioWorkletNode(ctx, "pcm-chunker", { processorOptions: { chunkFrames: Math.round(ctx.sampleRate * 0.2) } });
  const sink = ctx.createGain(); sink.gain.value = 0; sink.connect(ctx.destination); // never routed to speakers
  ctx.createMediaStreamSource(stream).connect(node); node.connect(sink);
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const client = new IngestClient({ wsUrl: `${proto}//${location.host}/api/v1/ingest/ws`, captureSessionId: cs.id,
    client: { name: "hattama-web", version: "0.1.0", platform: navigator.platform },
    onStatus: (s) => { const el = document.getElementById("localStatus"); if (el) el.textContent = `${s.type}${s.durableSeconds ? `: сохранено ${s.durableSeconds.toFixed(1)} c` : ""}`; } });
  client.addSource("room", "local_microphone", ctx.sampleRate, "Микрофон этого компьютера");
  node.port.onmessage = (e) => { if (e.data.type === "chunk") client.pushPcm("room", new Int16Array(e.data.pcm)); };
  client.connect();
  localCapture = { stop: async () => { stream.getTracks().forEach((tr) => tr.stop()); await client.stop(); ctx.close(); } };
  document.getElementById("pairing").innerHTML = `<p class="mic-warning">● Идёт запись микрофона этого компьютера. Не закрывайте и не обновляйте эту страницу
    (для онлайн-встреч используйте расширение — оно не зависит от страницы). <span id="localStatus"></span></p>`;
}

function startLive(m) {
  const el = document.getElementById("live");
  el.innerHTML = `<div class="panel"><h2>Live</h2><div id="srcs"></div><div id="metrics"></div><div id="segs"></div>
    <h3>Предварительные поручения</h3><ul id="prelim"></ul></div>`;
  const segs = new Map();
  const partials = new Map();
  const sources = {};
  const draw = () => {
    const all = [...segs.values(), ...partials.values()].sort((a, b) => a.start_ms - b.start_ms);
    document.getElementById("segs").innerHTML = all.slice(-60).map((s) => `<div class="seg ${s.state === "partial" ? "partial" : ""}">
      <span class="meta">${fmtMs(s.start_ms)} · ${esc(s.source_id)} · ${esc(s.language || "")} · ${s.state === "partial" ? "черновик" : "стабильно"}</span><br>${esc(s.text)}
      ${(s.review_reasons || []).length ? `<div class="reasons">${esc(s.review_reasons.join(", "))}</div>` : ""}</div>`).join("") || "<p>Реплик пока нет.</p>";
    document.getElementById("srcs").innerHTML = Object.entries(sources).map(([k, v]) => `<p><b>${esc(k)}</b> ${esc(v.state || "")}
      ${v.rms_dbfs != null ? `уровень ${v.rms_dbfs} dBFS` : ""} ${v.durable_seconds != null ? `· сохранено ${v.durable_seconds} c` : ""}
      ${v.gaps ? `<span class="error">· разрывов: ${v.gaps}</span>` : ""}</p>`).join("") || "<p>Источник не подключён.</p>";
  };
  api("GET", `/meetings/${m.id}/live-state`).then((st) => {
    st.segments.forEach((s) => segs.set(s.id, s));
    document.getElementById("prelim").innerHTML = st.preliminary_actions.map((a) => `<li>${esc(a.action)} — ${esc((a.assignee || {}).name || "?")}</li>`).join("");
    draw();
    if (liveSocket) liveSocket.close();
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    liveSocket = new WebSocket(`${proto}//${location.host}/api/v1/meetings/${m.id}/events/ws`);
    liveSocket.onopen = () => liveSocket.send(JSON.stringify({ after: st.last_event_id }));
    liveSocket.onmessage = (ev) => {
      for (const e of JSON.parse(ev.data).events) {
        const p = e.payload;
        if (e.type === "segment.partial") partials.set(p.segment_id, { ...p, id: p.segment_id, state: "partial" });
        if (e.type === "segment.stable") { partials.delete(p.segment_id); segs.set(p.segment_id, { ...p, id: p.segment_id, state: "stable" }); }
        if (e.type === "source.level") sources[p.source_id] = { ...(sources[p.source_id] || {}), ...p };
        if (e.type === "source.state") sources[p.source_id] = { ...(sources[p.source_id] || {}), state: p.state };
        if (e.type === "source.gap") sources[p.source_id] = { ...(sources[p.source_id] || {}), gaps: ((sources[p.source_id] || {}).gaps || 0) + 1 };
        if (e.type === "asr.metrics") document.getElementById("metrics").textContent =
          `Очередь распознавания (${p.source_id}): ${p.backlog_s} c · RTF шага ${p.step_rtf ?? "—"} · обработано ${p.processed_s} c из ${p.durable_s} c`;
        if (e.type === "action.preliminary") document.getElementById("prelim").insertAdjacentHTML("beforeend", `<li>${esc(p.action)} — ${esc((p.assignee || {}).name || "?")}</li>`);
        if (e.type === "meeting.status" || (e.type === "capture.state" && p.state === "STOPPED")) setTimeout(route, 800);
      }
      draw();
    };
  }).catch(showError);
}

const DL = (d) => !d || d.kind === "missing" ? "срок не указан" : d.kind === "event" ? `после события: ${d.anchor_event || d.raw_text}` :
  d.normalized_date ? `${d.normalized_date}${d.ambiguous || d.conflict ? " (?)" : ""} — «${d.raw_text || ""}»` : `«${d.raw_text}» (дата не определена)`;

async function renderReview(m) {
  const el = document.getElementById("review");
  const [tr, actions, issues, summary] = await Promise.all([api("GET", `/meetings/${m.id}/transcript`), api("GET", `/meetings/${m.id}/actions`),
    api("GET", `/meetings/${m.id}/issues`), api("GET", `/meetings/${m.id}/summary`)]);
  const secretary = m.my_role === "secretary";
  const sources = [...new Set(tr.segments.map((s) => s.source_id))];
  el.innerHTML = `
  ${m.status === "PROCESSING" ? `<p class="panel">Идёт финальная обработка: распознавание всей записи, разметка говорящих, извлечение поручений.</p>` : ""}
  <div class="panel"><h2>Поручения (${actions.length})</h2>${actions.map((a) => `<div class="seg" id="a-${a.id}">
    <b>${a.number}. ${esc(a.action)}</b> <span class="badge">${esc(a.review_state)}</span> <span class="badge">${esc(a.execution_state)}</span>
    ${a.conditional ? `<span class="badge">условное: ${esc(a.condition || "")}</span>` : ""} ${a.on_hold ? `<span class="badge">приостановлено</span>` : ""}
    <div>Исполнитель: ${esc((a.assignee || {}).name || "—")} (${esc(a.assignee_type)}) · Срок: ${esc(DL(a.deadline))}${a.overdue ? ` <span class="error">просрочено</span>` : ""}</div>
    ${a.review_reasons.length ? `<div class="reasons">На проверку: ${esc(a.review_reasons.join("; "))}</div>` : ""}
    <div class="meta">Основания: ${a.evidence.map((e) => `<button class="link" data-seek="${e.start_ms}">[${esc(e.field)} ${fmtMs(e.start_ms || 0)}] «${esc(e.quote)}»${e.match !== "exact" ? ` (${esc(e.match)})` : ""}</button>`).join(" ")}</div>
    ${secretary && m.status === "NEEDS_REVIEW" ? `<details><summary>Исправить</summary><form data-action="${a.id}" data-version="${a.version}">
      <label>Действие <input name="action" value="${esc(a.action)}" size="60"></label>
      <label>Исполнитель <select name="pid"><option value="">— как есть —</option>${m.participants.map((p) => `<option value="${p.id}">${esc(p.display_name)}</option>`).join("")}</select>
        или имя/подразделение <input name="aname"></label>
      <label>Срок (дата) <input type="date" name="date" value="${esc((a.deadline || {}).normalized_date || "")}"></label>
      <label><input type="checkbox" name="nodl"> срок не указан</label>
      <button name="op" value="save">Сохранить</button> <button name="op" value="confirm" class="primary">Подтвердить</button>
      <button name="op" value="reject" class="danger">Не поручение</button></form></details>` : ""}
    </div>`).join("") || "<p>Поручений нет.</p>"}</div>
  <div class="panel"><h2>Вопросы на уточнение (${issues.filter((i) => i.status === "open").length})</h2>
    ${issues.map((i) => `<div class="seg"><span class="badge">${esc(i.severity)}</span> ${esc(i.question)} — <i>${esc(i.status)}</i>
      ${secretary && i.status === "open" ? `<button data-resolve="${i.id}">Решено</button>` : ""}</div>`).join("") || "<p>Нет.</p>"}</div>
  <div class="panel"><h2>Саммари</h2>${summary.content ? ["facts", "decisions", "assumptions", "risks", "open_questions"].map((k) =>
    (summary.content[k] || []).length ? `<h3>${{ facts: "Факты и показатели", decisions: "Решения", assumptions: "Предположения", risks: "Риски", open_questions: "Открытые вопросы" }[k]}</h3>
    <ul>${summary.content[k].map((it) => `<li>${esc(it.text)}${it.flags.length ? ` <span class="reasons">[${esc(it.flags.join("; "))}]</span>` : ""}</li>`).join("")}</ul>` : "").join("") : "<p>Саммари ещё нет.</p>"}</div>
  <div class="panel"><h2>Расшифровка</h2>
    ${sources.map((s) => `<p>${esc(s)}: <audio id="au-${esc(s)}" controls preload="none" src="/api/v1/meetings/${m.id}/audio/${encodeURIComponent(s)}.wav"></audio></p>`).join("")}
    <input id="q" placeholder="Поиск по тексту"> ${tr.segments.map((s) => `<div class="seg" data-text="${esc(s.text.toLowerCase())}">
      <span class="meta"><button class="link" data-seek="${s.start_ms}" data-src="${esc(s.source_id)}">${fmtMs(s.start_ms)}</button> · ${esc(s.source_id)} · ${esc(s.language || "")} ${s.edited ? "· исправлено" : ""}</span>
      ${secretary && m.status === "NEEDS_REVIEW" ? `<div contenteditable="true" data-seg="${s.id}" data-rev="${s.rev}">${esc(s.text)}</div>` : `<div>${esc(s.text)}</div>`}
      ${s.review_reasons.length ? `<div class="reasons">${esc(s.review_reasons.join(", "))}</div>` : ""}</div>`).join("") || "<p>Расшифровки пока нет.</p>"}</div>
  <div class="panel"><h2>Протокол</h2>
    <button data-export="pdf">Скачать PDF${m.status === "APPROVED" ? "" : " (черновик)"}</button> <button data-export="docx">Скачать DOCX${m.status === "APPROVED" ? "" : " (черновик)"}</button>
    ${secretary && m.status === "NEEDS_REVIEW" ? `<button id="approve" class="primary">Утвердить протокол</button> <button id="retry">Повторить анализ</button>` : ""}
    ${secretary && m.status === "APPROVED" ? `<button id="newver">Создать новую версию для правок</button>` : ""}</div>`;
  el.querySelectorAll("[data-seek]").forEach((b) => b.onclick = () => {
    const au = document.querySelector(b.dataset.src ? `#au-${CSS.escape(b.dataset.src)}` : "audio");
    if (au) { au.currentTime = Number(b.dataset.seek) / 1000; au.play(); }
  });
  el.querySelectorAll("[data-seg]").forEach((d) => d.onblur = async () => {
    try { const s = await api("PATCH", `/segments/${d.dataset.seg}`, { rev: Number(d.dataset.rev), text: d.textContent }); d.dataset.rev = s.rev; }
    catch (e) { showError(e); }
  });
  el.querySelectorAll("form[data-action]").forEach((f) => f.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    const op = e.submitter.value;
    const body = { version: Number(f.dataset.version), action: fd.get("action") };
    if (fd.get("pid")) body.assignee_participant_id = fd.get("pid");
    else if (fd.get("aname")) body.assignee_name = fd.get("aname");
    if (fd.get("nodl")) body.deadline_missing = true; else if (fd.get("date")) body.deadline_date = fd.get("date");
    if (op === "confirm") body.review_state = "confirmed";
    if (op === "reject") body.review_state = "rejected";
    try { await api("PATCH", `/actions/${f.dataset.action}`, body); route(); } catch (err) { showError(err); }
  });
  el.querySelectorAll("[data-resolve]").forEach((b) => b.onclick = async () => {
    const resolution = prompt("Как решён вопрос?");
    if (resolution) { await api("POST", `/issues/${b.dataset.resolve}/resolve`, { resolution }).catch(showError); route(); }
  });
  el.querySelectorAll("[data-export]").forEach((b) => b.onclick = async () => {
    try { const ex = await api("POST", `/meetings/${m.id}/exports`, { format: b.dataset.export }); location.href = ex.download; } catch (e) { showError(e); }
  });
  document.getElementById("q").oninput = (e) => el.querySelectorAll("[data-text]").forEach((d) => d.hidden = !d.dataset.text.includes(e.target.value.toLowerCase()));
  const ap = document.getElementById("approve");
  if (ap) ap.onclick = async () => { try { await api("POST", `/meetings/${m.id}/approve`, { version: m.version }); route(); } catch (e) { showError(e); } };
  const rt = document.getElementById("retry");
  if (rt) rt.onclick = () => api("POST", `/meetings/${m.id}/retry-analysis`).then(() => alert("Анализ поставлен в очередь")).catch(showError);
  const nv = document.getElementById("newver");
  if (nv) nv.onclick = () => api("POST", `/meetings/${m.id}/new-version`).then(route).catch(showError);
}

async function viewActions() {
  const list = await api("GET", "/actions");
  render(`<h1>${t("Поручения")}</h1><table><thead><tr><th>Поручение</th><th>Встреча</th><th>Исполнитель</th><th>Срок</th><th>Статус</th></tr></thead><tbody>
  ${list.map((a) => `<tr><td>${esc(a.action)}${a.review_reasons.length ? `<div class="reasons">требует проверки</div>` : ""}</td>
    <td><a href="#/m/${a.meeting_id}">${esc(a.meeting_title)}</a></td><td>${esc((a.assignee || {}).name || "—")}</td>
    <td>${esc(DL(a.deadline))}${a.overdue ? ` <span class="error">просрочено</span>` : ""}</td>
    <td><select data-exec="${a.id}">${["open", "in_progress", "done", "cancelled"].map((s) => `<option ${s === a.execution_state ? "selected" : ""}>${s}</option>`).join("")}</select></td></tr>`).join("") ||
    `<tr><td colspan="5">Поручений нет.</td></tr>`}</tbody></table><p id="err" class="error"></p>`);
  app.querySelectorAll("[data-exec]").forEach((s) => s.onchange = () => api("POST", `/actions/${s.dataset.exec}/execution`, { execution_state: s.value }).catch(showError));
}

async function viewNotifications() {
  const list = await api("GET", "/notifications");
  render(`<h1>Уведомления</h1>${list.map((n) => `<div class="seg"><b>${esc(n.title)}</b> · ${esc(n.at)}<br>${esc(n.body)}</div>`).join("") || "<p>Нет уведомлений.</p>"}`);
}

async function viewDiagnostics() {
  render(`<h1>Диагностика</h1><p>Проверка оборудования и локальных моделей…</p>`);
  const d = await api("GET", "/diagnostics");
  const hw = d.hardware;
  render(`<h1>Диагностика</h1><div class="panel"><p>${esc(hw.os_name)} ${esc(hw.os_release)} (${esc(hw.runtime)}) · RAM ${hw.ram_total_gib} GiB (доступно ${hw.ram_available_gib})</p>
    ${hw.gpus.map((g) => `<p>GPU ${esc(g.name)}: ${g.memory_total_mib} MiB, свободно ${g.memory_free_mib}, драйвер ${esc(g.driver_version)}</p>`).join("") || "<p>GPU не обнаружена</p>"}
    <p>Рекомендуемый профиль: <b>${esc(d.recommended_profile.profile)}</b>, активный: <b>${esc(d.active_profile)}</b></p>
    <ul>${[...d.recommended_profile.reasons, ...d.recommended_profile.warnings].map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>
    <table><tbody>${d.checks.map((c) => `<tr><td>${esc(c.status)}</td><td>${esc(c.name)}</td><td>${esc(c.detail)}</td></tr>`).join("")}</tbody></table>`);
}

// ------------------------------------------------------------------ router
async function route() {
  if (liveSocket) { liveSocket.close(); liveSocket = null; }
  const hash = location.hash || "#/meetings";
  try {
    if (!csrf && hash !== "#/login") {
      try { const s = await api("GET", "/auth/me"); csrf = s.csrf_token; me = s.user; } catch (_) { return viewLogin(); }
    }
    if (hash === "#/login") return viewLogin();
    document.getElementById("nav").hidden = false;
    const mm = hash.match(/^#\/m\/([a-f0-9]{32})$/);
    if (mm) return await viewMeeting(mm[1]);
    if (hash === "#/actions") return await viewActions();
    if (hash === "#/notifications") return await viewNotifications();
    if (hash === "#/diagnostics") return await viewDiagnostics();
    return await viewMeetings();
  } catch (e) { render(`<p class="error">${esc(e.message || e)}</p>`); }
}

document.getElementById("logout").onclick = async () => { await api("POST", "/auth/logout").catch(() => {}); csrf = null; location.hash = "#/login"; };
window.addEventListener("hashchange", route);
route();
