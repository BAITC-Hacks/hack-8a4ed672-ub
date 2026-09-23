import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { esc, icon, time, platform } from './static/ui.js';

// Render-only harness: device capture and network are tested independently.
const source = (await readFile(new URL('./static/recording-ui.js', import.meta.url), 'utf8'))
  .replace(/^import .*;\n/gm, '').replaceAll('export function ', 'function ');
const context = vm.createContext({ esc, icon, time, platform, window: { addEventListener() {} } });
vm.runInContext(source, context);
const meeting = extra => ({ id: 'test', my_role: 'secretary', platform: 'google_meet',
  meeting_url: 'https://meet.google.com/abc-defg-hij', status: 'READY', consent_confirmed_at: '2026-09-23', ...extra });
const ready = extra => ({ can_approve: false, reasons: [], transcript_ready: false,
  analysis_ready: false, summary_ready: false, jobs: [], ...extra });

test('ready online meeting offers real tab and microphone recording', () => {
  const html = context.recordingPanel(meeting(), [], ready());
  assert.match(html, /id="start-tab"/);
  assert.match(html, /id="start-mic"/);
  assert.doesNotMatch(html, /Подключить ассистента/);
});
test('consent and a meeting URL are required before recording', () => {
  for (const extra of [{ consent_confirmed_at: null }, { meeting_url: null }]) {
    const html = context.recordingPanel(meeting(extra), [], ready());
    assert.match(html, /id="record-preparation"/);
    assert.doesNotMatch(html, /id="start-tab"/);
  }
});
test('stopping capture blocks a second capture even when meeting is READY', () => {
  const html = context.recordingPanel(meeting(), [{ id: 'capture', state: 'STOPPING' }], ready());
  assert.match(html, /id="stop-remote"/);
  assert.doesNotMatch(html, /id="start-tab"/);
});
test('read-only members never receive recording controls', () => {
  assert.equal(context.recordingPanel(meeting({ my_role: 'member' }), [], ready()), '');
});
test('long queued processing is distinct from work being performed', () => {
  const html = context.recordingPanel(meeting({ status: 'PROCESSING' }), [], ready({ jobs: [
    { kind: 'finalize_meeting', status: 'queued', updated_at: '2020-01-01T00:00:00Z' },
  ] }));
  assert.match(html, /В очереди/);
  assert.match(html, /Задание пока не взято в работу/);
  assert.doesNotMatch(html, /обработчик работает/);
});
test('processing reflects running jobs and completed data without fake percentages', () => {
  const html = context.recordingPanel(meeting({ status: 'PROCESSING' }), [], ready({
    transcript_ready: true, jobs: [{ kind: 'extract_final', status: 'running' }],
  }));
  assert.match(html, /Выполняется/);
  assert.match(html, /Готово/);
  assert.match(html, /обработчик работает/);
});
test('server readiness explanations are escaped and empty speech is not retryable analysis', () => {
  const html = context.recordingPanel(meeting({ status: 'FAILED' }), [], ready({ reasons: ['<script>bad</script>'] }));
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /id="retry-analysis"/);
});
