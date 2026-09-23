import { api, session, user } from './api.js';
import { esc, icon, avatar, tone, time, date, today, platform, status, execution, deadline, bucket, empty, toast, modal, busy } from './ui.js';
import { recordingPanel, bindRecordingPanel, recordingBanner } from './recording-ui.js';

const app = document.getElementById('app');
let generation = 0, cleanup = () => {};
const canCreate = () => ['secretary', 'admin'].includes(user?.role);
const meetingPath = m => `#/m/${m.id}`;
const button = (label, glyph, attrs = '', cls = '') => `<button class="button ${cls}" ${attrs}>${glyph ? icon(glyph) : ''}${label}</button>`;
const options = (map, value) => Object.entries(map).map(([v, label]) => `<option value="${esc(v)}" ${v === value ? 'selected' : ''}>${esc(label)}</option>`).join('');
const back = () => `<a class="back-link" href="#/meetings">${icon('back')}Все встречи</a>`;
function render(html, cls = '') { app.className = cls; app.innerHTML = recordingBanner() + html; }
function shell() {
  const hash = location.hash; const tasks = hash.startsWith('#/actions');
  document.getElementById('header').innerHTML = `<a class="brand" href="#/meetings"><span class="brand-icon">${icon('logo')}</span>Хаттама</a>
    ${user ? `<nav class="desktop-nav" aria-label="Разделы"><a href="#/meetings" ${!tasks ? 'aria-current="page"' : ''}>Встречи</a><a href="#/actions" ${tasks ? 'aria-current="page"' : ''}>Поручения</a></nav>
    <div class="header-actions">${canCreate() ? button('Новая встреча', 'plus', 'id="new-record"', 'primary') : ''}<button id="account" class="account-button" aria-label="Профиль и настройки">${avatar(user.display_name)}</button></div>` : '<span class="header-note">Протокол встречи — без лишней работы</span>'}`;
  document.getElementById('new-record')?.addEventListener('click', () => createMeeting());
  document.getElementById('account')?.addEventListener('click', () => {
    const d = modal(user.display_name, `<p class="muted">${esc(user.email)}</p>${button('Выйти из аккаунта', '', 'id="logout"')}`);
    d.querySelectorAll('a').forEach(a => a.onclick = () => d.close());
    d.querySelector('#logout').onclick = e => busy(e.currentTarget, async () => {
      await api('POST', '/auth/logout'); session(null); d.close(); location.hash = '#/login';
    });
  });
}

function viewLogin() {
  session(null); shell();
  render(`<section class="login-card"><span class="brand-icon large">${icon('logo')}</span><h1>Всё важное<br>останется в протоколе</h1><p class="muted">Войдите, чтобы работать с протоколами<br>и следить за поручениями.</p><form id="login-form" class="form-stack"><label>Рабочая почта<input type="email" name="email" placeholder="name@company.kz" autocomplete="username" required></label><label>Пароль<input type="password" name="password" placeholder="Введите пароль" autocomplete="current-password" required></label><p id="login-error" class="error" role="alert"></p>${button('Войти', 'arrow', 'type="submit"', 'primary')}</form><p class="privacy-note">${icon('shield')}Записи и тексты обрабатываются локально</p></section>`, 'login-page');
  document.getElementById('login-form').onsubmit = e => {
    e.preventDefault(); const fd = new FormData(e.target);
    busy(e.submitter, async () => {
      try { session(await api('POST', '/auth/login', Object.fromEntries(fd))); location.hash = '#/meetings'; }
      catch (err) { document.getElementById('login-error').textContent = err.message; }
    });
  };
}

async function viewMeetings(token) {
  const list = await api('GET', '/meetings'); if (token !== generation) return;
  render(`<section class="welcome"><h1>Добрый день, ${esc(user.display_name.split(' ')[0])}</h1><p>Встречи, решения и поручения — в одном месте.</p>${canCreate() ? `<div class="start-actions">${button('Новая встреча', 'plus', 'id="record-meeting"', 'primary large')}</div>` : ''}</section>
  <section><div class="section-heading"><h2>Недавние <span class="muted number">${list.length}</span></h2><label class="search-field">${icon('search')}<input id="meeting-search" type="search" placeholder="Найти встречу" aria-label="Поиск встреч"></label></div><div class="meeting-list" id="meeting-list"></div></section>`, 'page meetings-page');
  const draw = q => {
    const filtered = list.filter(m => m.title.toLocaleLowerCase().includes(q.toLocaleLowerCase()));
    document.getElementById('meeting-list').innerHTML = filtered.map(m => `<a href="${meetingPath(m)}" class="meeting-row"><span class="date-tile ${m.status === 'NEEDS_REVIEW' ? 'fresh' : ''}"><b>${date(m.meeting_date, { day: 'numeric', month: undefined })}</b><small>${date(m.meeting_date, { day: undefined, month: 'short' }).replace('.', '')}</small></span><span class="meeting-info"><strong>${esc(m.title)}</strong><span>${esc(platform(m.platform))}${m.start_time ? ` · ${esc(m.start_time)}` : ''}</span></span><span class="meeting-count">${m.actions} поручений</span>${status(m.status)}${icon('arrow', 'row-arrow')}</a>`).join('') || empty(q ? 'Ничего не найдено' : 'Здесь будут ваши встречи', q ? 'Попробуйте другое название.' : 'Создайте встречу. Здесь появятся её протокол и поручения.');
  };
  draw(''); document.getElementById('meeting-search').oninput = e => draw(e.target.value);
  document.getElementById('record-meeting')?.addEventListener('click', () => createMeeting());
}

function createMeeting() {
  const d = modal('Новая встреча', `<form id="meeting-form" class="form-stack"><label>Название встречи<input name="title" placeholder="Например, планёрка команды" maxlength="300" required autofocus></label><div class="form-grid"><label>Дата<input type="date" name="meeting_date" value="${today()}" required></label><label>Время<input type="time" name="start_time"></label></div><div class="form-grid"><label>Платформа<select name="platform">${options({ in_person: 'Очная встреча', google_meet: 'Google Meet', teams_web: 'Microsoft Teams', zoom_web: 'Zoom', other: 'Другое' }, 'in_person')}</select></label><label>Язык<select name="language_mode">${options({ mixed: 'Русский + қазақша', ru: 'Русский', kk: 'Қазақша' }, 'mixed')}</select></label></div><label>Часовой пояс<input name="timezone" value="Asia/Almaty" required></label><label>Участники<textarea name="participants" rows="3" placeholder="Имя; должность; email&#10;Добавьте (я) к своему имени"></textarea><small class="muted">По одному в строке. Email связывает исполнителя с его аккаунтом.</small></label><p class="form-error error" role="alert"></p><div class="dialog-actions">${button('Отмена', '', 'type="button" data-cancel')}${button('Создать встречу', 'plus', 'type="submit"', 'primary')}</div></form>`);
  d.querySelector('[data-cancel]').onclick = () => d.close();
  const platformSelect = d.querySelector('[name="platform"]');
  platformSelect.closest('.form-grid').insertAdjacentHTML('afterend', '<label id="meeting-url-field" hidden>Ссылка на встречу<input name="meeting_url" type="url" maxlength="2000" placeholder="https://meet.google.com/abc-defg-hij"><small class="muted">Откройте эту встречу в браузере. Хаттама запишет звук выбранной вкладки.</small></label>');
  const updatePlatform = () => {
    const online = ['google_meet', 'zoom_web', 'teams_web'].includes(platformSelect.value);
    const field = d.querySelector('#meeting-url-field'); field.hidden = !online;
    field.querySelector('input').required = online;
    field.querySelector('input').disabled = !online;
    field.querySelector('input').placeholder = platformSelect.value === 'zoom_web' ? 'https://zoom.us/j/123456789' : platformSelect.value === 'teams_web' ? 'https://teams.microsoft.com/l/meetup-join/…' : 'https://meet.google.com/abc-defg-hij';
  };
  platformSelect.onchange = updatePlatform; updatePlatform();
  d.querySelector('form').onsubmit = e => {
    e.preventDefault(); const fd = new FormData(e.target);
    busy(e.submitter, async () => {
      const err = d.querySelector('.form-error'); err.textContent = '';
      try {
        const participants = String(fd.get('participants')).split('\n').map(x => x.trim()).filter(Boolean).map(line => {
          const [name, position, email] = line.split(';').map(x => x.trim());
          return { display_name: name.replace('(я)', '').trim(), position: position || null, user_email: email || null, is_self: name.includes('(я)') };
        });
        const m = await api('POST', '/meetings', { title: fd.get('title').trim(), meeting_date: fd.get('meeting_date'), start_time: fd.get('start_time') || null, timezone: fd.get('timezone'), language_mode: fd.get('language_mode'), platform: fd.get('platform'), meeting_url: fd.get('meeting_url')?.trim() || null, participants });
        d.close(); location.hash = `#/m/${m.id}`;
      } catch (error) { err.textContent = error.message; }
    });
  };
}

function actionCard(a, { sticky = false, editable = false, meeting = false } = {}) {
  const name = a.assignee?.name || 'Не назначен'; const ev = a.evidence?.find(e => e.start_ms != null);
  return `<article class="task-card ${sticky ? `sticky tone-${tone(name)}` : ''} ${a.execution_state === 'done' ? 'completed' : ''}"><div class="task-card-heading"><h3>${esc(a.action)}</h3>${ev && meeting ? `<button class="time-chip" data-evidence="${a.id}" aria-label="Открыть основание поручения">${icon('play')}${time(ev.start_ms)}</button>` : ''}</div>${a.conditional ? `<span class="task-note">${esc(a.condition || 'Условное поручение')}</span>` : ''}${a.on_hold ? '<span class="badge amber">Приостановлено</span>' : ''}<div class="task-person">${avatar(name, 'small')}<span>${esc(name)}</span><span class="due ${a.overdue ? 'red' : ''}">${esc(deadline(a))}</span></div>${!sticky && a.meeting_title && a.can_view_meeting ? `<a class="task-meeting" href="#/m/${a.meeting_id}">${icon('meetings')}${esc(a.meeting_title)}</a>` : ''}${a.review_reasons?.length ? `<span class="task-note">${icon('info')}Требует проверки</span>` : ''}<div class="task-controls">${editable ? `<button class="text-button" data-edit="${a.id}">${icon('edit')}Проверить</button>` : ''}${a.can_update_execution ? `<label class="sr-only" for="exec-${a.id}">Статус поручения ${esc(a.action)}</label><select class="execution-select" id="exec-${a.id}" data-execution="${a.id}">${options(execution, a.execution_state)}</select>` : `<span class="muted small-text">${esc(execution[a.execution_state] || 'Предварительное')}</span>`}</div></article>`;
}
function bindActionControls(root, actions, m) {
  root.querySelectorAll('[data-execution]').forEach(el => el.onchange = () => {
    const a = actions.find(a => a.id === el.dataset.execution), next = el.value;
    busy(el, async () => { try { await api('POST', `/actions/${a.id}/execution`, { execution_state: next }); await route(); } catch (e) { el.value = a.execution_state; throw e; } });
  });
  root.querySelectorAll('[data-edit]').forEach(el => el.onclick = () => editAction(actions.find(a => a.id === el.dataset.edit), m));
}

async function viewActions(token) {
  const list = await api('GET', '/actions'); if (token !== generation) return;
  const open = list.filter(a => !['done', 'cancelled'].includes(a.execution_state));
  const people = [...new Set(list.map(a => a.assignee?.name).filter(Boolean))];
  let person = '', scope = 'all', query = '';
  const columns = [['overdue', 'Просрочено', 'red'], ['week', 'На этой неделе', 'amber'], ['later', 'Позже / без срока', 'blue'], ['done', 'Готово', 'green']];
  render(`<div class="page-heading"><div><h1>Поручения <span class="heading-count">${open.length} открытых</span></h1></div></div><div class="tasks-toolbar"><div class="people-filter"><button class="filter-pill active" data-person="">Все</button>${people.map(p => `<button class="avatar-filter" data-person="${esc(p)}" aria-label="Поручения: ${esc(p)}" aria-pressed="false">${avatar(p)}</button>`).join('')}</div><div class="task-search"><label class="search-field">${icon('search')}<input id="task-search" type="search" placeholder="Найти поручение" aria-label="Поиск поручений"></label><label class="sr-only" for="scope">Область поручений</label><select id="scope"><option value="all">Все поручения</option><option value="mine">Мои поручения</option></select></div></div><div class="kanban" id="kanban"></div>`, 'page tasks-page');
  let mine = null;
  const draw = () => {
    const rows = (scope === 'mine' ? mine || [] : list).filter(a => (!person || a.assignee?.name === person) && a.action.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
    const groups = Object.fromEntries(columns.map(([key]) => [key, rows.filter(a => bucket(a) === key)]));
    document.getElementById('kanban').innerHTML = columns.map(([key, label, color]) => `<section id="column-${key}" class="kanban-column" aria-label="${label}"><h2><span class="dot ${color}"></span>${label}<span>${groups[key].length}</span></h2>${groups[key].map(a => actionCard(a)).join('') || '<p class="column-empty">Пока пусто</p>'}</section>`).join('');
    bindActionControls(app, list);
  };
  draw();
  app.querySelectorAll('[data-person]').forEach(b => b.onclick = () => { person = person === b.dataset.person ? '' : b.dataset.person; app.querySelectorAll('[data-person]').forEach(x => { x.classList.toggle('active', x.dataset.person === person); x.setAttribute('aria-pressed', String(x.dataset.person === person)); }); draw(); });
  document.getElementById('task-search').oninput = e => { query = e.target.value; draw(); };
  document.getElementById('scope').onchange = e => busy(e.currentTarget, async () => { if (e.target.value === 'mine' && !mine) mine = await api('GET', '/actions?scope=mine'); if (token !== generation) return; scope = e.target.value; draw(); });
}

async function viewProtocol(id, token) {
  const [m, transcript, actions, issues, summary, captures, readiness] = await Promise.all([
    api('GET', `/meetings/${id}`), api('GET', `/meetings/${id}/transcript`), api('GET', `/meetings/${id}/actions`),
    api('GET', `/meetings/${id}/issues`), api('GET', `/meetings/${id}/summary`),
    api('GET', `/meetings/${id}/capture-sessions`), api('GET', `/meetings/${id}/readiness`),
  ]);
  if (token !== generation) return;
  const editable = m.my_role === 'secretary' && m.status === 'NEEDS_REVIEW';
  const openIssues = issues.filter(i => i.status === 'open');
  const totalMs = transcript.segments.reduce((end, s) => Math.max(end, s.end_ms), 0);
  const summaryLabels = { decisions: 'Решения', facts: 'Факты и показатели', assumptions: 'Предположения', risks: 'Риски', open_questions: 'Открытые вопросы' };
  const summaryHtml = Object.entries(summaryLabels).map(([key, label]) => {
    const rows = summary.content?.[key] || [];
    return rows.length ? `<div class="summary-group">${key !== 'decisions' ? `<h3>${label}</h3>` : ''}<ul class="summary-list">${rows.map(row => `<li>${esc(row.text)}${row.flags?.length ? `<small class="muted">${esc(row.flags.join(' · '))}</small>` : ''}</li>`).join('')}</ul></div>` : '';
  }).join('');
  const speakerRows = transcript.speakers.filter(s => !s.merged_into).map(s => ({ ...s, name: s.binding?.status === 'confirmed' && s.binding?.name ? s.binding.name : s.label, duration: transcript.segments.filter(x => x.speaker_id === s.id).reduce((sum, x) => sum + Math.max(0, x.end_ms - x.start_ms), 0) }));
  const maxDuration = Math.max(1, ...speakerRows.map(s => s.duration));
  render(`${back()}<div class="protocol-heading"><div><div class="title-line"><h1>${esc(m.title)}</h1>${status(m.status)}</div><p class="muted">${date(m.meeting_date)}${m.start_time ? ` · ${esc(m.start_time)}` : ''}${totalMs ? ` · ${Math.ceil(totalMs / 60000)} мин` : ''} · ${esc(platform(m.platform))}</p></div><div class="protocol-actions">${button('Транскрипт', 'text', 'id="transcript"')}${button('Скачать', 'download', `id="download" ${!readiness.summary_ready ? 'disabled title="Сначала дождитесь итогов встречи"' : ''}`)}${editable ? button('Утвердить', 'check', `id="approve" ${!readiness.can_approve ? `disabled title="${esc(readiness.reasons.join('. '))}"` : ''}`, 'primary') : ''}</div></div>
  ${recordingPanel(m, captures, readiness)}
  <div class="protocol-overview"><section class="panel summary-panel"><h2>Итоги</h2>${summaryHtml || empty('Итоги ещё не готовы', 'Здесь появятся основные решения и факты из встречи.', 'text')}</section><section class="panel people-panel"><div class="section-heading"><h2>${speakerRows.length ? 'Кто говорил' : 'Участники'}</h2><span class="muted">${speakerRows.length || m.participants.length}</span></div><div class="speaker-list">${speakerRows.length ? speakerRows.map(s => `<div class="speaker-row">${avatar(s.name)}<span class="grow">${esc(s.name)}</span>${s.binding?.status === 'confirmed' || !editable ? `<meter class="speaker-meter" min="0" max="${maxDuration}" value="${s.duration}" aria-label="Время речи ${esc(s.name)}"></meter><small>${time(s.duration)}</small>` : `<button class="button compact" data-speaker="${s.id}">Кто это?</button>`}</div>`).join('') : m.participants.map(p => `<div class="speaker-row">${avatar(p.display_name)}<span class="grow">${esc(p.display_name)}<small class="muted">${esc(p.position || (p.is_self ? 'Это вы' : 'Участник'))}${p.is_present ? '' : ' · отсутствует'}</small></span></div>`).join('') || '<p class="muted">Участники не указаны.</p>'}</div></section></div>
  <section><div class="section-heading"><h2>Поручения <span class="muted number">${actions.length}</span></h2>${actions.length ? '<a class="text-link" href="#/actions">Все поручения →</a>' : ''}</div><div class="sticky-grid">${actions.map(a => actionCard(a, { sticky: true, editable, meeting: true })).join('') || `<div class="panel full-width">${empty('Поручений пока нет', 'После анализа здесь будут задачи с исполнителями и сроками.', 'tasks')}</div>`}</div></section>
  ${openIssues.length ? `<section class="panel issues-panel"><h2>Нужно уточнить <span class="badge amber">${openIssues.length}</span></h2>${openIssues.map(i => `<div class="issue-row"><span class="issue-symbol ${i.severity === 'blocker' ? 'red' : 'amber'}">${icon('info')}</span><div class="grow"><strong>${esc(i.question)}</strong><small class="muted">${i.severity === 'blocker' ? 'Нужно решить до утверждения' : 'Требует внимания'}</small></div>${editable ? `<button class="button compact" data-resolve="${i.id}">Уточнить</button>` : ''}</div>`).join('')}</section>` : ''}
  <div class="protocol-footer"><span>${icon('shield')}Версия протокола ${m.protocol_version} · ${m.status === 'APPROVED' ? 'Утверждено' : 'Черновик'}</span>${m.my_role === 'secretary' && m.status === 'APPROVED' ? button('Создать новую версию', 'edit', 'id="new-version"') : ''}</div>`, 'page protocol-page');
  bindActionControls(app, actions, m);
  cleanup = bindRecordingPanel(m, captures, readiness, route);
  document.getElementById('transcript').onclick = () => openTranscript(m, transcript);
  app.querySelectorAll('[data-evidence]').forEach(b => b.onclick = () => {
    const a = actions.find(a => a.id === b.dataset.evidence); openTranscript(m, transcript, a.evidence.find(e => e.start_ms != null), a);
  });
  document.getElementById('download').onclick = () => {
    const d = modal('Скачать протокол', `<p class="muted">${m.status === 'APPROVED' ? 'Утверждённая версия протокола.' : 'Документ будет помечен как черновик.'}</p><div class="export-options">${button('Документ PDF', 'download', 'data-export="pdf"')}${button('Word (DOCX)', 'download', 'data-export="docx"')}</div>`);
    d.querySelectorAll('[data-export]').forEach(b => b.onclick = () => busy(b, async () => {
      const ex = await api('POST', `/meetings/${m.id}/exports`, { format: b.dataset.export });
      const a = document.createElement('a'); a.href = ex.download; a.download = ''; document.body.append(a); a.click(); a.remove(); toast('Протокол готов к скачиванию.'); d.close();
    }));
  };
  document.getElementById('approve')?.addEventListener('click', () => {
    const d = modal('Утвердить протокол', `<p>Подтвердите, что итоги, исполнители и сроки проверены.</p><p class="muted">После утверждения для правок потребуется новая версия.</p><div class="dialog-actions">${button('Отмена', '', 'data-cancel')}${button('Утвердить', 'check', 'id="confirm-approve"', 'primary')}</div>`);
    d.querySelector('[data-cancel]').onclick = () => d.close();
    d.querySelector('#confirm-approve').onclick = e => busy(e.currentTarget, async () => { await api('POST', `/meetings/${m.id}/approve`, { version: m.version }); d.close(); toast('Протокол утверждён.'); await route(); });
  });
  document.getElementById('new-version')?.addEventListener('click', e => busy(e.currentTarget, async () => { await api('POST', `/meetings/${m.id}/new-version`); await route(); }));
  app.querySelectorAll('[data-resolve]').forEach(b => b.onclick = () => {
    const issue = issues.find(i => i.id === b.dataset.resolve);
    const d = modal('Уточнить вопрос', `<p>${esc(issue.question)}</p><form class="form-stack"><label>Решение<textarea name="resolution" maxlength="2000" rows="4" required autofocus></textarea></label><p class="muted">Если вопрос связан с поручением, внесите правку в его карточке.</p>${button('Сохранить решение', 'check', 'type="submit"', 'primary')}</form>`);
    d.querySelector('form').onsubmit = e => { e.preventDefault(); const resolution = new FormData(e.target).get('resolution'); busy(e.submitter, async () => { await api('POST', `/issues/${issue.id}/resolve`, { resolution }); d.close(); await route(); }); };
  });
  app.querySelectorAll('[data-speaker]').forEach(b => b.onclick = () => {
    const d = modal('Кто говорил?', `<form class="form-stack"><label>Участник<select name="participant_id" required><option value="">Выберите участника</option>${m.participants.map(p => `<option value="${p.id}">${esc(p.display_name)}</option>`).join('')}</select></label>${button('Подтвердить', 'check', 'type="submit"', 'primary')}</form>`);
    d.querySelector('form').onsubmit = e => { e.preventDefault(); const participant_id = new FormData(e.target).get('participant_id'); busy(e.submitter, async () => { await api('POST', `/speakers/${b.dataset.speaker}/binding`, { participant_id }); d.close(); await route(); }); };
  });
}

function editAction(a, m) {
  const d = modal('Проверить поручение', `<form class="form-stack"><label>Поручение<textarea name="action" maxlength="1000" rows="3" required>${esc(a.action)}</textarea></label><label>Исполнитель<select name="participant_id"><option value="">Оставить: ${esc(a.assignee?.name || 'не назначен')}</option>${m.participants.map(p => `<option value="${p.id}">${esc(p.display_name)}</option>`).join('')}</select></label><label>Или другой исполнитель<input name="assignee_name" placeholder="Имя или подразделение" maxlength="200"></label><label>Срок<input type="date" name="deadline_date" value="${esc(a.deadline?.normalized_date || '')}"></label><label class="check-label"><input type="checkbox" name="deadline_missing"><span>Срок не указан</span></label>${a.deadline?.raw_text ? `<p class="muted">В записи: «${esc(a.deadline.raw_text)}»</p>` : ''}${a.review_reasons?.length ? `<div class="notice amber">${esc(a.review_reasons.join(' · '))}</div>` : ''}<div class="dialog-actions wrap">${button('Не поручение', '', 'type="submit" name="op" value="reject"', 'danger-ghost')}${button('Сохранить', '', 'type="submit" name="op" value="save"')}${button('Подтвердить', 'check', 'type="submit" name="op" value="confirm"', 'primary')}</div></form>`);
  d.querySelector('form').onsubmit = e => {
    e.preventDefault(); const fd = new FormData(e.target); const body = { version: a.version, action: fd.get('action').trim() };
    if (fd.get('participant_id')) body.assignee_participant_id = fd.get('participant_id');
    else if (fd.get('assignee_name').trim()) body.assignee_name = fd.get('assignee_name').trim();
    if (fd.get('deadline_missing')) body.deadline_missing = true; else if (fd.get('deadline_date')) body.deadline_date = fd.get('deadline_date');
    if (e.submitter.value === 'confirm') body.review_state = 'confirmed';
    if (e.submitter.value === 'reject') body.review_state = 'rejected';
    busy(e.submitter, async () => { await api('PATCH', `/actions/${a.id}`, body); d.close(); toast('Поручение сохранено.'); await route(); });
  };
}

function openTranscript(m, tr, evidence, action) {
  const editable = m.my_role === 'secretary' && m.status === 'NEEDS_REVIEW';
  const sources = [...new Set(tr.segments.map(s => s.source_id))];
  const d = modal('Транскрипт встречи', `${action ? `<div class="evidence-note"><strong>${esc(action.action)}</strong><p>«${esc(evidence.quote)}»</p></div>` : ''}<label class="search-field transcript-search">${icon('search')}<input type="search" id="transcript-search" placeholder="Найти в тексте" aria-label="Поиск по расшифровке"></label><div class="audio-sources">${sources.map((s, i) => `<label class="audio-source"><span>${esc(s)}</span><audio data-source="${esc(s)}" id="audio-${i}" controls preload="none" src="/api/v1/meetings/${m.id}/audio/${encodeURIComponent(s)}.wav"></audio></label>`).join('')}</div><div id="transcript-segments">${tr.segments.map(s => {
    const speaker = tr.speakers.find(p => p.id === s.speaker_id); const name = speaker?.binding?.status === 'confirmed' ? speaker.binding.name : speaker?.label || s.source_id;
    return `<article class="transcript-segment ${evidence?.segment_id === s.id ? 'highlight' : ''}" data-text="${esc(s.text.toLocaleLowerCase())}" id="segment-${s.id}"><div class="transcript-meta">${avatar(name, 'small')}<strong>${esc(name)}</strong><span>${esc(s.language?.toUpperCase() || '')}</span><button class="text-button" data-seek="${s.start_ms}" data-source="${esc(s.source_id)}">${time(s.start_ms)}</button></div><p>${esc(s.text)}</p>${s.edited ? '<small class="muted">Исправлено</small>' : ''}${s.review_reasons?.length ? `<small class="muted">${esc(s.review_reasons.join(' · '))}</small>` : ''}${editable ? `<button class="text-button small-text" data-edit-segment="${s.id}">Исправить текст</button>` : ''}</article>`;
  }).join('') || empty('Расшифровка ещё не готова', 'Текст появится после распознавания речи.', 'text')}</div><p id="transcript-no-results" class="muted" hidden>Ничего не найдено.</p>`, { drawer: true });
  const seek = (source, ms) => {
    const audio = [...d.querySelectorAll('audio')].find(a => a.dataset.source === source);
    if (!audio) return;
    const play = () => { audio.currentTime = Number(ms) / 1000; audio.play().catch(() => toast('Не удалось воспроизвести запись. Проверьте доступность аудио.', true)); };
    if (audio.readyState) play(); else { audio.addEventListener('loadedmetadata', play, { once: true }); audio.load(); }
  };
  d.querySelectorAll('audio').forEach(a => { a.onplay = () => d.querySelectorAll('audio').forEach(other => { if (other !== a) other.pause(); }); a.onerror = () => toast('Аудиозапись недоступна.', true); });
  d.onclose = () => d.querySelectorAll('audio').forEach(a => a.pause());
  d.querySelectorAll('[data-seek]').forEach(b => b.onclick = () => seek(b.dataset.source, b.dataset.seek));
  d.querySelector('#transcript-search').oninput = e => {
    let shown = 0; d.querySelectorAll('[data-text]').forEach(row => { row.hidden = !row.dataset.text.includes(e.target.value.toLocaleLowerCase()); if (!row.hidden) shown++; }); d.querySelector('#transcript-no-results').hidden = shown > 0 || !tr.segments.length;
  };
  d.querySelectorAll('[data-edit-segment]').forEach(b => b.onclick = () => {
    const segment = tr.segments.find(s => s.id === b.dataset.editSegment); const row = b.closest('article');
    if (row.querySelector('form')) return;
    row.insertAdjacentHTML('beforeend', `<form class="form-stack segment-edit"><label class="sr-only" for="edit-${segment.id}">Текст реплики</label><textarea id="edit-${segment.id}" rows="4" maxlength="10000" required>${esc(segment.text)}</textarea><div class="dialog-actions">${button('Отмена', '', 'type="button" data-cancel')}${button('Сохранить', 'check', 'type="submit"', 'primary compact')}</div></form>`);
    const form = row.querySelector('form'); b.hidden = true;
    form.querySelector('[data-cancel]').onclick = () => { form.remove(); b.hidden = false; };
    form.onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => { const next = await api('PATCH', `/segments/${segment.id}`, { rev: segment.rev, text: form.querySelector('textarea').value }); Object.assign(segment, next); row.querySelector('p').textContent = next.text; row.dataset.text = next.text.toLocaleLowerCase(); form.remove(); b.hidden = false; toast('Текст сохранён.'); }); };
  });
  if (evidence) {
    const target = tr.segments.find(s => s.id === evidence.segment_id) || tr.segments.find(s => s.start_ms <= evidence.start_ms && s.end_ms >= evidence.start_ms);
    if (target) { d.querySelector(`#segment-${target.id}`)?.scrollIntoView({ block: 'center' }); seek(target.source_id, evidence.start_ms); }
  }
}

async function route() {
  const token = ++generation; cleanup(); cleanup = () => {};
  const dialog = document.getElementById('dialog'); if (dialog.open) dialog.close();
  const hash = location.hash || '#/meetings';
  if (hash === '#/login') return viewLogin();
  render('<div class="loading" role="status"><span class="spinner"></span>Загружаем…</div>', 'page');
  try {
    if (!user) session(await api('GET', '/auth/me'));
    if (token !== generation) return;
    shell();
    const match = hash.match(/^#\/m\/([a-f0-9]{32})$/);
    if (match) await viewProtocol(match[1], token);
    else if (hash === '#/actions') await viewActions(token);
    else await viewMeetings(token);
    if (token === generation) { document.title = `${app.querySelector('h1')?.textContent || 'Встречи'} — Хаттама`; app.focus({ preventScroll: true }); }
  } catch (e) {
    if (token !== generation) return;
    if (!user) return viewLogin();
    render(`<div class="panel">${empty('Не удалось загрузить страницу', e.message, 'info')}${button('Повторить', '', 'id="retry-page"')}</div>`, 'page');
    document.getElementById('retry-page').onclick = route;
  }
}
window.addEventListener('hashchange', route);
route();
