import { BrowserRecorder } from './recorder.js';
import { api } from './api.js';
import { esc, icon, time, platform, toast, busy, modal } from './ui.js';

let recorder = null;
let refreshPage = () => {};
let lastStatus = '';
const activeStatuses = new Set(['requesting', 'recording', 'paused', 'saving', 'recoverable']);
const isOnline = m => ['google_meet', 'zoom_web', 'teams_web'].includes(m.platform);
const control = (label, id, glyph = '', cls = '') => `<button type="button" class="button ${cls}" id="${id}">${glyph ? icon(glyph) : ''}${label}</button>`;

export function recordingBanner() {
  const s = recorder?.snapshot();
  if (!s || !activeStatuses.has(s.status)) return '';
  return `<a class="recording-banner" href="#/m/${esc(s.meetingId)}">${icon('mic')}Запись открыта · <span data-record-time>${time(s.elapsedSeconds * 1000)}</span><span>Вернуться к записи →</span></a>`;
}

function preparation(m) {
  const online = isOnline(m);
  return `<form id="record-preparation" class="form-stack"><div class="section-heading"><h2>Подготовка к записи</h2><span class="badge blue">1 из 3</span></div>
    <p class="muted">${online ? `Откройте ${esc(platform(m.platform))} в отдельной вкладке, затем разрешите Хаттаме записывать её звук.` : 'Запишите встречу с микрофона этого устройства. Итоги и поручения появятся после завершения.'}</p>
    ${online ? `<label>Ссылка ${esc(platform(m.platform))}<input name="meeting_url" type="url" maxlength="2000" value="${esc(m.meeting_url || '')}" placeholder="https://…" required></label>` : ''}
    ${!m.consent_confirmed_at ? '<label class="check-label"><input name="consent" type="checkbox" required><span>Я предупредил участников о записи и подготовке протокола.</span></label>' : '<p class="muted">Уведомление участников подтверждено.</p>'}
    <p class="error" id="prepare-error" role="alert"></p><div class="record-actions"><button type="submit" class="button primary">${icon('check')}Подготовить запись</button></div></form>`;
}

export function recordingPanel(m, captures, readiness) {
  if (m.my_role !== 'secretary') return '';
  const s = recorder?.snapshot();
  const own = s?.meetingId === m.id && activeStatuses.has(s.status);
  const current = captures.find(c => ['CREATED', 'ACTIVE', 'STOPPING'].includes(c.state));
  let body = '';
  if (['DRAFT', 'READY'].includes(m.status) && !own && !current) {
    if (!m.consent_confirmed_at || (isOnline(m) && !m.meeting_url)) body = preparation(m);
    else body = `<div id="record-ready"><div class="section-heading"><h2>Всё готово к записи</h2><span class="badge blue">1 из 3</span></div>
      ${m.meeting_url ? `<a class="meeting-link button soft" href="${esc(m.meeting_url)}" target="_blank" rel="noopener noreferrer">${icon('link')}Открыть ${esc(platform(m.platform))}</a>` : ''}
      <p class="muted">${isOnline(m) ? 'Нажмите «Начать запись вкладки», выберите вкладку встречи и включите «Также предоставить доступ к аудио вкладки». Используйте наушники.' : 'Разрешите доступ к микрофону. Во время встречи здесь будут таймер, уровень звука и текст.'}</p>
      ${isOnline(m) ? '<label class="check-label"><input id="include-mic" type="checkbox" checked><span>Также записывать мой микрофон <small class="muted">Он записывается независимо от выключения звука в Meet/Zoom.</small></span></label>' : ''}
      <div class="record-actions">${isOnline(m) ? control('Начать запись вкладки', 'start-tab', 'play', 'primary') : ''}${control(isOnline(m) ? 'Только микрофон' : 'Начать запись', 'start-mic', 'mic', isOnline(m) ? '' : 'primary')}</div>
      <p class="small-text muted">Для записи вкладки используйте Chrome или Edge на компьютере. Для очной встречи достаточно микрофона.</p>
      <p class="error" id="capture-error" role="alert"></p></div>`;
  } else if (own || ['LIVE'].includes(m.status) || current) {
    body = `<div class="section-heading"><h2>Запись встречи</h2><span class="badge red" id="record-badge">${current?.state === 'CREATED' && !own ? 'Ожидаем подключение' : 'Запись'} · 2 из 3</span></div>
      <div class="record-timer"><span class="rec-dot"></span><strong data-record-time>00:00</strong><span id="record-status" role="status">Подключаемся…</span></div>
      <div class="record-levels"><label>Звук встречи <meter id="tab-level" min="0" max="1" value="0"></meter></label><label>Микрофон <meter id="mic-level" min="0" max="1" value="0"></meter></label></div>
      <p class="muted small-text" id="record-connection">Проверяем соединение с сервером…</p><p class="error" id="capture-error" role="alert"></p>
      <div class="record-actions">${own ? control('Пауза', 'pause-record') + control('Завершить запись', 'stop-record', 'stop', 'danger') + control('Повторить сохранение', 'retry-save', 'upload') : control('Завершить запись', 'stop-remote', 'stop', 'danger')}</div>
      ${!own ? '<p class="muted small-text">Захват открыт в другой вкладке или через расширение. Завершение сохранит полученный сервером звук и запустит обработку.</p>' : '<p class="muted small-text">Держите эту страницу открытой до окончания сохранения. При временном разрыве связи звук накапливается в буфере.</p>'}`;
  } else if (m.status === 'PROCESSING') {
    body = `<div class="section-heading"><h2>Готовим протокол</h2><span class="badge blue">3 из 3</span></div><p class="muted">Запись сохранена. Распознаём речь, выделяем решения и поручения.</p><div id="processing-steps">${processingSteps(readiness)}</div><p class="error" id="capture-error" role="alert"></p>`;
  } else if (['FAILED', 'NEEDS_REVIEW'].includes(m.status) && !readiness.can_approve) {
    body = `<h2>${m.status === 'FAILED' ? 'Обработка не завершилась' : 'Перед утверждением'}</h2><ul class="readiness-reasons">${readiness.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>
      <p class="muted small-text">${readiness.transcript_ready ? 'Проверьте текст, исполнителей и вопросы. При ошибке анализа его можно запустить повторно.' : 'Нет готовой расшифровки. Проверьте запись или проведите новую встречу.'}</p>
      ${readiness.transcript_ready ? `<div class="record-actions">${control('Повторить анализ', 'retry-analysis', 'play')}</div>` : ''}`;
  }
  if (!body) return '';
  const live = own || m.status === 'LIVE';
  return `<section class="panel recording-panel" id="record-panel" data-meeting="${m.id}">${body}</section>${live ? '<section class="panel live-transcript"><div class="section-heading"><h2>Текст встречи</h2><span class="badge blue">В реальном времени</span></div><p class="muted small-text" id="live-note">Первые фразы появятся после начала речи.</p><div id="live-segments"></div></section>' : ''}`;
}

function processingSteps(readiness) {
  const flags = [readiness.transcript_ready, readiness.analysis_ready, readiness.summary_ready];
  const jobs = readiness.jobs || [];
  const active = jobs.find(j => j.status === 'running') || jobs.find(j => j.status === 'queued');
  const stalled = active?.status === 'queued' && Date.now() - new Date(active.updated_at).getTime() > 45000;
  const names = { finalize_meeting: 'Подготовка аудио', final_asr: 'Распознавание речи', diarize: 'Определение говорящих', extract_final: 'Анализ и итоги' };
  const stages = [['finalize_meeting', 'final_asr', 'diarize'], ['extract_final'], ['extract_final']];
  return `<ol class="processing-steps">${['Распознавание речи', 'Извлечение поручений', 'Подготовка итогов'].map((label, i) => {
    const job = jobs.find(j => stages[i].includes(j.kind) && ['queued', 'running', 'failed'].includes(j.status));
    const detail = flags[i] ? 'Готово' : job?.status === 'running' ? 'Выполняется' : job?.status === 'failed' ? 'Ошибка обработки' : job?.status === 'queued' ? 'В очереди' : 'Ожидает предыдущий этап';
    return `<li class="${flags[i] ? 'complete' : ''}">${icon(flags[i] ? 'check' : 'clock')}<span>${label}</span><small>${detail}</small></li>`;
  }).join('')}</ol>${active ? `<p class="small-text muted" role="status">${esc(names[active.kind] || 'Обработка')}: ${active.status === 'running' ? 'обработчик работает' : 'ожидает запуска обработчика'}.</p>` : ''}${stalled ? '<p class="small-text error" role="status">Задание пока не взято в работу. Если ожидание продолжается, проверьте, что на сервере запущены ASR- и pipeline-обработчики. Запись сохранена; повторно записывать её не нужно.</p>' : ''}`;
}

function paintState(s) {
  document.querySelectorAll('[data-record-time]').forEach(el => el.textContent = time(s.elapsedSeconds * 1000));
  const panel = document.getElementById('record-panel');
  if (panel?.dataset.meeting !== s.meetingId) return;
  const text = { requesting: 'Разрешите доступ к звуку', recording: 'Записываем', paused: 'На паузе', saving: 'Сохраняем запись…', recoverable: 'Не всё сохранено — повторите отправку', stopped: 'Запись сохранена', error: 'Запись не началась' };
  const setText = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
  setText('record-status', text[s.status] || s.status);
  setText('record-connection', s.connected ? `Подключено · в буфере ${Number(s.bufferedSeconds || 0).toFixed(1)} сек.` : `Соединение восстанавливается · в буфере ${Number(s.bufferedSeconds || 0).toFixed(1)} сек.`);
  setText('capture-error', s.error || '');
  const pause = document.getElementById('pause-record');
  if (pause) { pause.hidden = !['recording', 'paused'].includes(s.status); pause.textContent = s.status === 'paused' ? 'Продолжить запись' : 'Пауза'; }
  const stop = document.getElementById('stop-record'); if (stop) stop.disabled = !['recording', 'paused'].includes(s.status);
  const retry = document.getElementById('retry-save'); if (retry) retry.hidden = s.status !== 'recoverable';
  const tabMeter = document.getElementById('tab-level'), micMeter = document.getElementById('mic-level');
  if (tabMeter) tabMeter.value = s.levels?.tab || s.levels?.tab_audio || 0;
  if (micMeter) micMeter.value = s.levels?.mic || s.levels?.microphone || 0;
}

export function bindRecordingPanel(m, captures, readiness, rerender) {
  refreshPage = rerender;
  let disposed = false, polling = false;
  const error = message => { const el = document.getElementById('capture-error') || document.getElementById('prepare-error'); if (el) el.textContent = message; else toast(message, true); };
  document.getElementById('record-preparation')?.addEventListener('submit', e => {
    e.preventDefault(); const fd = new FormData(e.target);
    busy(e.submitter, async () => {
      try {
        if (isOnline(m)) await api('PATCH', `/meetings/${m.id}`, { version: m.version, meeting_url: fd.get('meeting_url').trim() });
        if (!m.consent_confirmed_at) await api('POST', `/meetings/${m.id}/consent`, { confirmed: fd.get('consent') === 'on' });
        await rerender();
      } catch (e) { error(e.message); }
    });
  });
  const start = source => {
    if (recorder?.active) { error('В этой вкладке уже идёт запись. Сначала завершите её.'); return; }
    const includeMicrophone = source === 'microphone' || !!document.getElementById('include-mic')?.checked;
    recorder = new BrowserRecorder({ meetingId: m.id, api, onState: s => {
      paintState(s);
      if (s.status === lastStatus) return;
      lastStatus = s.status;
      if (['recording', 'stopped'].includes(s.status)) {
        if (s.status === 'stopped') toast('Запись сохранена. Началась обработка.');
        if (location.hash === `#/m/${m.id}`) void refreshPage();
      }
    } });
    lastStatus = '';
    // Keep the media request inside the trusted click; no network await before start().
    const promise = recorder.start({ source, includeMicrophone });
    document.querySelectorAll('#start-tab, #start-mic').forEach(b => b.disabled = true);
    promise.catch(e => error(e.message)).finally(() => {
      document.querySelectorAll('#start-tab, #start-mic').forEach(b => b.disabled = false);
    });
  };
  document.getElementById('start-tab')?.addEventListener('click', () => start('tab'));
  document.getElementById('start-mic')?.addEventListener('click', () => start('microphone'));
  document.getElementById('pause-record')?.addEventListener('click', e => busy(e.currentTarget, async () => {
    if (recorder.snapshot().status === 'paused') await recorder.resume(); else await recorder.pause();
  }));
  document.getElementById('stop-record')?.addEventListener('click', e => busy(e.currentTarget, () => recorder.stop()));
  document.getElementById('retry-save')?.addEventListener('click', e => busy(e.currentTarget, () => recorder.retryStop()));
  document.getElementById('stop-remote')?.addEventListener('click', () => {
    const cs = captures.find(c => ['CREATED', 'ACTIVE', 'STOPPING'].includes(c.state)); if (!cs) return;
    const d = modal('Завершить запись?', '<p>Захват звука прекратится, сохранённая запись отправится на обработку.</p><div class="dialog-actions"><button class="button" data-cancel>Отмена</button><button class="button primary" data-stop>Завершить</button></div>');
    d.querySelector('[data-cancel]').onclick = () => d.close();
    d.querySelector('[data-stop]').onclick = e => busy(e.currentTarget, async () => { await api('POST', `/capture-sessions/${cs.id}/stop`); d.close(); await rerender(); });
  });
  document.getElementById('retry-analysis')?.addEventListener('click', e => busy(e.currentTarget, async () => { await api('POST', `/meetings/${m.id}/retry-analysis`); await rerender(); }));
  const state = recorder?.snapshot(); if (state?.meetingId === m.id) paintState(state);
  const poll = async () => {
    if (disposed || polling) return;
    polling = true;
    try {
      const fresh = await api('GET', `/meetings/${m.id}/live-state`);
      if (disposed) return;
      if (fresh.meeting_status !== m.status) { await rerender(); return; }
      if (m.status === 'PROCESSING' || (m.status === 'NEEDS_REVIEW' && readiness.processing)) {
        const ready = await api('GET', `/meetings/${m.id}/readiness`);
        if (disposed) return;
        if (m.status === 'NEEDS_REVIEW' && (ready.can_approve !== readiness.can_approve || ready.processing !== readiness.processing)) { await rerender(); return; }
        const el = document.getElementById('processing-steps'); if (el) el.innerHTML = processingSteps(ready);
      }
      const el = document.getElementById('live-segments');
      if (el) {
        el.innerHTML = fresh.segments.slice(-12).map(s => `<p class="live-line ${s.state === 'partial' ? 'muted' : ''}"><small>${time(s.start_ms)}</small><span>${esc(s.text)}</span></p>`).join('');
        const note = document.getElementById('live-note'); if (note) note.textContent = fresh.segments.length ? 'Черновая расшифровка. После встречи текст будет уточнён.' : 'Первые фразы появятся после начала речи.';
      }
      if (!recorder?.active && m.status === 'LIVE') {
        const cs = fresh.capture[0];
        if (cs?.started_at) document.querySelectorAll('[data-record-time]').forEach(el => el.textContent = time(Date.now() - new Date(cs.started_at).getTime()));
        const el = document.getElementById('record-connection'); if (el) el.textContent = cs?.connected ? 'Источник звука подключён' : 'Источник отключён. Можно завершить и обработать сохранённый звук.';
      }
    } catch (e) { if (!disposed) error(`Не удалось обновить состояние: ${e.message}`); }
    finally { polling = false; }
  };
  const timer = ['READY', 'LIVE', 'PROCESSING'].includes(m.status) || (m.status === 'NEEDS_REVIEW' && readiness.processing) ? setInterval(poll, 3000) : null;
  if (m.status === 'LIVE') void poll();
  return () => { disposed = true; if (timer) clearInterval(timer); };
}

window.addEventListener('beforeunload', e => {
  if (recorder?.active) { e.preventDefault(); e.returnValue = ''; }
});
