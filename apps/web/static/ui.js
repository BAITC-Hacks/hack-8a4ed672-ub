export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const paths = {
  logo: '<path d="M3 10h4l2-5 4 13 3-8h5M3 21h18"/>',
  mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>',
  micOff: '<path d="m2 2 20 20M9 9v3a3 3 0 0 0 5 2M9 5a3 3 0 0 1 6 0v4M5 10v2a7 7 0 0 0 12 5M19 10v2M12 19v3M8 22h8"/>',
  meetings: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  tasks: '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="m8 10 2 2 5-5M8 16h8"/>',
  arrow: '<path d="m9 6 6 6-6 6"/>', back: '<path d="M20 12H4m6-6-6 6 6 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>', plus: '<path d="M12 5v14M5 12h14"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M5 20h14"/>', upload: '<path d="M12 16V3m-5 5 5-5 5 5M5 20h14"/>',
  link: '<path d="m10 14 4-4M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
  text: '<path d="M4 5h16M4 10h16M4 15h16M4 20h10"/>',
  bell: '<path d="M6 16v-6a6 6 0 0 1 12 0v6l2 3H4zM10 22h4"/>',
  check: '<path d="m5 12 4 4L19 6"/>', clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  play: '<path d="m9 5 11 7-11 7z"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v1"/>',
  edit: '<path d="m14 5 5 5M4 20l5-1L21 7l-5-5L4 14z"/>',
  people: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 6"/>',
  shield: '<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6zM8 12l3 3 5-6"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
};
export const icon = (name, cls = '') => `<svg class="icon ${cls}" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.info}</svg>`;
export const initials = name => (String(name || '?').trim().split(/\s+/).slice(0, 2).map(w => [...w][0]).join('')).toUpperCase();
export const tone = name => [...String(name || '')].reduce((n, c) => n + c.codePointAt(0), 0) % 6;
export const avatar = (name, cls = '') => `<span class="avatar tone-${tone(name)} ${cls}" title="${esc(name)}">${esc(initials(name))}</span>`;
export const time = ms => { const s = Math.max(0, Math.floor(Number(ms || 0) / 1000)); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };
export const date = (value, options = {}) => value ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', ...options }).format(new Date(`${String(value).slice(0, 10)}T12:00:00`)) : 'Без даты';
export const today = (tz = 'Asia/Almaty') => new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
export const platform = key => ({ google_meet: 'Google Meet', teams_web: 'Microsoft Teams', zoom_web: 'Zoom', in_person: 'Очная встреча', other: 'Аудиозапись' }[key] || key);
export const statuses = { DRAFT: ['Черновик', 'amber'], READY: ['Готова к записи', 'blue'], LIVE: ['Идёт запись', 'red'], PROCESSING: ['Обрабатываем…', 'blue'], NEEDS_REVIEW: ['На проверке', 'amber'], APPROVED: ['Утверждено', 'green'], FAILED: ['Ошибка обработки', 'red'] };
export const status = key => `<span class="badge ${statuses[key]?.[1] || ''}">${key === 'LIVE' ? '<span class="rec-dot"></span>' : ''}${esc(statuses[key]?.[0] || key)}</span>`;
export const execution = { open: 'Не начато', in_progress: 'В работе', done: 'Готово', cancelled: 'Отменено' };
export function deadline(a) {
  const d = a.deadline || {};
  if (d.normalized_date) return `до ${date(d.normalized_date, { month: 'short' })}${d.ambiguous || d.conflict ? ' · уточнить' : ''}`;
  if (d.kind === 'event') return d.raw_text || d.anchor_event || 'После события';
  return d.raw_text || 'Без срока';
}
export function bucket(a, now = new Date()) {
  if (['done', 'cancelled'].includes(a.execution_state)) return 'done';
  if (a.overdue) return 'overdue';
  const d = a.deadline || {};
  if (!d.normalized_date || a.conditional || a.on_hold || d.ambiguous || d.conflict || !a.deadline_confirmed) return 'later';
  const local = new Intl.DateTimeFormat('en-CA', { timeZone: d.timezone || 'Asia/Almaty', year: 'numeric', month: '2-digit', day: '2-digit' }).format(now);
  const end = new Date(`${local}T12:00:00Z`);
  end.setUTCDate(end.getUTCDate() + (7 - (end.getUTCDay() || 7)));
  return d.normalized_date <= end.toISOString().slice(0, 10) ? 'week' : 'later';
}
export const empty = (title, description, glyph = 'meetings') => `<div class="empty">${icon(glyph)}<h3>${esc(title)}</h3><p>${esc(description)}</p></div>`;
let toastTimer;
export function toast(message, error = false) {
  const el = document.getElementById('toast'); clearTimeout(toastTimer);
  el.textContent = message; el.className = `toast ${error ? 'error-toast' : ''}`; el.hidden = false;
  toastTimer = setTimeout(() => el.hidden = true, error ? 9000 : 4500);
}
export function modal(title, content, { drawer = false } = {}) {
  const el = document.getElementById('dialog'); if (el.open) el.close();
  el.className = drawer ? 'drawer' : '';
  el.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2><button class="icon-button" aria-label="Закрыть" data-close>${icon('close')}</button></div><div class="dialog-body">${content}</div>`;
  el.querySelector('[data-close]').onclick = () => el.close();
  el.onclick = e => { if (e.target === el) { const r = el.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) el.close(); } };
  el.showModal(); return el;
}
export async function busy(button, work) {
  if (button?.disabled) return;
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  try { return await work(); }
  catch (e) { toast(e.message || 'Не удалось выполнить действие', true); }
  finally { if (button?.isConnected) { button.disabled = false; button.removeAttribute('aria-busy'); } }
}
