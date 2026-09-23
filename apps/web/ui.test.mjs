import test from 'node:test';
import assert from 'node:assert/strict';
import { bucket, esc, deadline, time } from './static/ui.js';

const now = new Date('2026-09-23T23:30:00Z');
const task = (extra = {}) => ({ execution_state: 'open', deadline_confirmed: true,
  deadline: { kind: 'date', normalized_date: '2026-09-27', timezone: 'Asia/Almaty' }, ...extra });
test('board keeps ambiguous, conditional and unconfirmed dates out of this week', () => {
  assert.equal(bucket(task(), now), 'week');
  assert.equal(bucket(task({ deadline_confirmed: false }), now), 'later');
  assert.equal(bucket(task({ conditional: true }), now), 'later');
  assert.equal(bucket(task({ on_hold: true }), now), 'later');
  assert.equal(bucket(task({ deadline: { normalized_date: '2026-09-25', ambiguous: true } }), now), 'later');
  assert.equal(bucket(task({ deadline: { normalized_date: '2026-09-28' } }), now), 'later');
});
test('completed and overdue actions use server status', () => {
  assert.equal(bucket(task({ overdue: true }), now), 'overdue');
  assert.equal(bucket(task({ overdue: true, execution_state: 'done' }), now), 'done');
  assert.equal(bucket(task({ execution_state: 'cancelled' }), now), 'done');
});
test('a Sunday evening in UTC already belongs to Monday in Almaty', () => {
  assert.equal(bucket(task({ deadline: { normalized_date: '2026-09-30', timezone: 'Asia/Almaty' } }), new Date('2026-09-27T21:00:00Z')), 'week');
});
test('untrusted meeting text is escaped, durations and unknown deadlines stay honest', () => {
  assert.equal(esc('<img src=x onerror="x">'), '&lt;img src=x onerror=&quot;x&quot;&gt;');
  assert.equal(time(3723000), '62:03');
  assert.equal(deadline({ deadline: { kind: 'missing' } }), 'Без срока');
});
