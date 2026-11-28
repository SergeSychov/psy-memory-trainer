'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { LIMITS } = require('./source_links');
const { validateOutgoing, utf8Bytes } = require('./channel_core');
const {
  STEPS,
  BTN,
  ERROR_TEXTS,
  CONFIRM_FAIL_TEXTS,
  step,
  onEffectResult,
  renderNotification,
  errorText,
  parseButton,
  confirmationText,
  sanitizeText,
  formatSize,
  formatDuration,
} = require('./lecture_intake');

const NOW = Date.parse('2026-11-28T10:00:00Z');
const SECRET_URL = 'https://example.com/secret-path-xyz/lecture.mp4?token=abc123';
const COURSES = [
  { id: '00000000-0000-0000-0000-000000000001', title: 'Курс А' },
  { id: '00000000-0000-0000-0000-000000000002', title: 'Курс Б' },
];

let seq = 0;
function ev(kind, extra) {
  seq += 1;
  return {
    channel: 'telegram',
    external_user_id: '111',
    external_chat_id: '111',
    event_id: `e${seq}`,
    kind,
    ...extra,
  };
}
const cmd = (command) => ev('command', { command });
const text = (t) => ev('text', { text: t });
const btn = (id) => ev('button', { button_id: id });
const file = (f) => ev('file', { file: { ref: 'ref1', kind: 'audio', ...f } });

function ctx(extra) {
  return { now: NOW, courses: COURSES, quota_remaining: 2, active_request: null, ...extra };
}

function allText(res) {
  return res.outgoing.map((m) => m.text).join('\n');
}

function assertSafeText(str, label) {
  assert.ok(!/<\/?[a-z][^>]*>/i.test(str), `HTML in ${label}`);
  assert.ok(!/https?:\/\//i.test(str), `URL in ${label}`);
  assert.ok(!str.includes('secret-path-xyz'), `user URL echoed in ${label}`);
  assert.ok(!str.includes('abc123'), `user URL token echoed in ${label}`);
}

/** Apply a reducer result and return the next session. */
function run(session, event, c) {
  const res = step(session, event, c || ctx());
  return res;
}

/** Drive a flow up to await_source with an existing course. */
function toAwaitSource() {
  let r = run(null, cmd('new'));
  r = run(r.session, btn(BTN.course(0)));
  return r.session;
}

/** Drive up to the confirm step through a Yandex link. */
function toConfirm(quota = 2) {
  let session = toAwaitSource();
  let r = run(session, text('https://yadi.sk/i/AbCdEf123_-x'));
  assert.equal(r.effects[0].type, 'probe_source');
  r = onEffectResult(r.session, { type: 'probe_source', ok: true, name: 'lecture.mp4', size_bytes: 52428800, duration_sec: 4500 }, ctx({ quota_remaining: quota }));
  assert.equal(r.effects[0].type, 'create_request');
  r = onEffectResult(r.session, { type: 'create_request', ok: true, request_id: 'req-1', quota_remaining: quota }, ctx());
  return r;
}

// ------------------------------------------------------------ /new

test('/new with courses: pick_course session, course buttons with new: prefix, <=48 bytes', () => {
  const r = run(null, cmd('new'));
  assert.equal(r.session.step, 'pick_course');
  assert.ok(STEPS.includes(r.session.step));
  assert.equal(r.session.data.courses.length, 2);
  assert.equal(r.outgoing.length, 1);
  assert.deepEqual(r.effects, []);
  const ids = r.outgoing[0].buttons.flat().map((b) => b.id);
  assert.ok(ids.includes('new:course:0') && ids.includes('new:course:1') && ids.includes('new:newcourse') && ids.includes('new:cancel'));
  for (const id of ids) {
    assert.ok(id.startsWith('new:'));
    assert.ok(utf8Bytes(id) <= 48);
  }
  assert.doesNotThrow(() => validateOutgoing(r.outgoing[0]));
  assert.equal(r.outgoing[0].to.external_chat_id, '111');
  assert.ok(r.session.expires_at);
  assert.equal(Date.parse(r.session.expires_at), NOW + LIMITS.session_ttl_min * 60000);
});

test('/new strips the bot suffix and works for any channel', () => {
  const r = step(null, { ...cmd('/new@PsyBot'), channel: 'max' }, ctx());
  assert.equal(r.session.step, 'pick_course');
  assert.equal(r.outgoing[0].to.channel, 'max');
});

test('/new without courses goes straight to new_course_title', () => {
  const r = run(null, cmd('new'), ctx({ courses: [] }));
  assert.equal(r.session.step, 'new_course_title');
  assert.match(r.outgoing[0].text, /назвать/);
  assert.equal(r.outgoing[0].expect, 'text');
});

test('course list is paged: <=6 rows, <=2 per row, navigation works', () => {
  const many = Array.from({ length: 11 }, (_, i) => ({ id: `id-${i}`, title: `Курс ${i}` }));
  let r = run(null, cmd('new'), ctx({ courses: many }));
  let rows = r.outgoing[0].buttons;
  assert.ok(rows.length <= 6);
  assert.ok(rows.flat().some((b) => b.id === 'new:page:4'));
  assert.ok(!rows.flat().some((b) => b.label.includes('Назад')));
  r = run(r.session, btn('new:page:4'), ctx({ courses: many }));
  rows = r.outgoing[0].buttons;
  assert.ok(rows.length <= 6);
  assert.ok(rows.flat().some((b) => b.id === 'new:course:4'));
  assert.ok(rows.flat().some((b) => b.id === 'new:page:0'));
  assert.ok(rows.flat().some((b) => b.id === 'new:page:8'));
  r = run(r.session, btn('new:page:99999'));
  assert.doesNotThrow(() => validateOutgoing(r.outgoing[0]));
});

test('long course title is shortened to the 40-char label limit', () => {
  const r = run(null, cmd('new'), ctx({ courses: [{ id: 'x', title: 'Я'.repeat(80) }] }));
  assert.doesNotThrow(() => validateOutgoing(r.outgoing[0]));
});

test('quota 0 -> refusal, no session, no effects', () => {
  const r = run(null, cmd('new'), ctx({ quota_remaining: 0 }));
  assert.equal(r.session, null);
  assert.deepEqual(r.effects, []);
  assert.match(allText(r), /закончились/);
});

test('unlimited quota (null) is allowed to start', () => {
  const r = run(null, cmd('new'), ctx({ quota_remaining: null }));
  assert.equal(r.session.step, 'pick_course');
});

test('active request already exists -> status instead of a new dialog', () => {
  const r = run(null, cmd('new'), ctx({ active_request: { request_id: 'r1', status: 'queued', queue_position: 2 } }));
  assert.equal(r.session, null);
  assert.deepEqual(r.effects, []);
  assert.match(allText(r), /в очереди/);
  assert.match(allText(r), /Перед вами: 2/);
  const p = run(null, cmd('new'), ctx({ active_request: { request_id: 'r1', status: 'processing' } }));
  assert.match(allText(p), /обрабатывается/);
  assert.equal(p.session, null);
});

test('/new restarts an open dialog and releases its old request', () => {
  const confirm = toConfirm();
  const r = step(confirm.session, cmd('new'), ctx());
  assert.equal(r.session.step, 'pick_course');
  assert.deepEqual(r.effects, [{ type: 'cancel_request', request_id: 'req-1', notify: false }]);
});

// --------------------------------------------------------- course choice

test('pick existing course -> await_source with the source prompt', () => {
  const r0 = run(null, cmd('new'));
  const r = run(r0.session, btn('new:course:1'));
  assert.equal(r.session.step, 'await_source');
  assert.equal(r.session.data.course_id, COURSES[1].id);
  assert.match(r.outgoing[0].text, /Курс Б/);
  assert.match(r.outgoing[0].text, /20 МБ/);
  assert.match(r.outgoing[0].text, /2000/);
  assert.equal(r.outgoing[0].expect, 'link');
});

test('course index out of range -> stale button, session kept', () => {
  const r0 = run(null, cmd('new'));
  const r = run(r0.session, btn('new:course:7'));
  assert.equal(r.session.step, 'pick_course');
  assert.match(r.outgoing[0].text, /устарела/);
});

test('new course: button -> title prompt -> title accepted -> await_source', () => {
  let r = run(null, cmd('new'));
  r = run(r.session, btn(BTN.NEW_COURSE));
  assert.equal(r.session.step, 'new_course_title');
  r = run(r.session, text('  Моя   новая психология  '));
  assert.equal(r.session.step, 'await_source');
  assert.equal(r.session.data.new_course_title, 'Моя новая психология');
  assert.equal(r.session.data.course_id, null);
  assert.match(r.outgoing[0].text, /\(новый\)/);
});

test('course title validation: too short, too long, with link, not text', () => {
  const base = run(run(null, cmd('new')).session, btn(BTN.NEW_COURSE)).session;
  for (const bad of ['а', '   ', 'я'.repeat(LIMITS.course_title_max + 1), `Курс ${SECRET_URL}`, 'курс yadi.sk/i/AbCdEf123']) {
    const r = run(base, text(bad));
    assert.equal(r.session.step, 'new_course_title', bad);
    assert.deepEqual(r.effects, []);
    assertSafeText(allText(r), 'title error');
  }
  const f = run(base, file({ name: 'a.mp3' }));
  assert.equal(f.session.step, 'new_course_title');
});

test('course title: angle brackets are stripped (no HTML injection)', () => {
  const base = run(run(null, cmd('new')).session, btn(BTN.NEW_COURSE)).session;
  const r = run(base, text('Курс <b>жирный</b>'));
  assert.equal(r.session.data.new_course_title, 'Курс bжирный/b');
  assert.ok(!/[<>]/.test(allText(r)));
});

test('text instead of pressing a button in pick_course -> hint with buttons', () => {
  const r0 = run(null, cmd('new'));
  const r = run(r0.session, text('привет'));
  assert.equal(r.session.step, 'pick_course');
  assert.ok(r.outgoing[0].buttons.length);
});

// ----------------------------------------------------------- links

test('link in await_source -> probe_source effect, nothing sent to the user but a progress note', () => {
  const r = run(toAwaitSource(), text('вот: https://yadi.sk/i/AbCdEf123_-x спасибо'));
  assert.equal(r.session.step, 'await_source');
  assert.equal(r.session.data.pending, 'probe');
  assert.deepEqual(r.effects, [{ type: 'probe_source', source_kind: 'yandex_public', url: 'https://yadi.sk/i/AbCdEf123_-x', maybe_folder: false }]);
  assert.match(allText(r), /Проверяю ссылку/);
  assert.ok(!allText(r).includes('yadi.sk'));
});

test('several links: first is used and the user is told', () => {
  const r = run(toAwaitSource(), text('https://example.com/a.mp4 и https://example.org/b.mp4'));
  assert.equal(r.effects[0].url, 'https://example.com/a.mp4');
  assert.match(allText(r), /несколько ссылок/);
});

test('unsupported / dangerous links get a human error without echoing the link', () => {
  const s = toAwaitSource();
  for (const bad of [SECRET_URL.replace('https', 'http'), 'https://127.0.0.1/secret-path-xyz?token=abc123', 'https://user:abc123@example.com/secret-path-xyz', 'ftp://example.com/secret-path-xyz', 'https://drive.google.com/drive/folders/AbCdEfGhIjKl?token=abc123']) {
    const r = run(s, text(bad));
    assert.deepEqual(r.effects, [], bad);
    assert.equal(r.session.step, 'await_source');
    assert.ok(!r.session.data.pending);
    assert.match(allText(r), /ссылка не подходит/);
    assertSafeText(allText(r), 'unsupported link');
  }
});

test('short text without link -> hint', () => {
  const r = run(toAwaitSource(), text('просто привет'));
  assert.deepEqual(r.effects, []);
  assert.match(allText(r), /Не вижу ссылки/);
  assert.match(allText(r), /2000/);
  const mid = run(toAwaitSource(), text('а'.repeat(500)));
  assert.match(allText(mid), /маловато/);
  assert.match(allText(mid), /500 символов/);
});

test('probe ok -> create_request effect carries url + metadata, course ref, no quota spend', () => {
  let r = run(toAwaitSource(), text('https://disk.yandex.ru/i/AbCdEf123_-x'));
  r = onEffectResult(r.session, { type: 'probe_source', ok: true, name: 'lecture.mp4', size_bytes: 1000, duration_sec: 3600, mime: 'video/mp4' }, ctx());
  assert.equal(r.effects.length, 1);
  const e = r.effects[0];
  assert.equal(e.type, 'create_request');
  assert.equal(e.source_kind, 'yandex_public');
  assert.equal(e.course_id, COURSES[0].id);
  assert.equal(e.new_course_title, null);
  assert.equal(e.source.url, 'https://disk.yandex.ru/i/AbCdEf123_-x');
  assert.equal(e.source.duration_sec, 3600);
  assert.equal(r.session.data.pending, 'create');
});

test('probe errors map to human texts and let the user retry', () => {
  for (const code of ['not_public', 'unsupported_link', 'too_large', 'too_long', 'download_failed']) {
    const s = run(toAwaitSource(), text('https://example.com/a.mp4')).session;
    const r = onEffectResult(s, { type: 'probe_source', ok: false, error: code }, ctx());
    assert.equal(r.session.step, 'await_source');
    assert.equal(r.session.data.pending, null);
    assert.equal(r.outgoing[0].text, ERROR_TEXTS[code]);
    assert.deepEqual(r.effects, []);
  }
  const s = run(toAwaitSource(), text('https://example.com/a.mp4')).session;
  const unknown = onEffectResult(s, { type: 'probe_source', ok: false, error: 'weird_new_code' }, ctx());
  assert.equal(unknown.outgoing[0].text, errorText('pipeline_failed'));
});

test('probe ok but longer than 2h(+60s) or too big -> refused with the right text', () => {
  const s = run(toAwaitSource(), text('https://example.com/a.mp4')).session;
  const long = onEffectResult(s, { type: 'probe_source', ok: true, duration_sec: 7261 }, ctx());
  assert.equal(long.outgoing[0].text, ERROR_TEXTS.too_long);
  assert.deepEqual(long.effects, []);
  const edge = onEffectResult(s, { type: 'probe_source', ok: true, duration_sec: 7260 }, ctx());
  assert.equal(edge.effects[0].type, 'create_request');
  const big = onEffectResult(s, { type: 'probe_source', ok: true, size_bytes: 4 * 1024 ** 3 + 1 }, ctx());
  assert.equal(big.outgoing[0].text, ERROR_TEXTS.too_large);
});

test('probe file name containing a link is sanitised in the card', () => {
  let r = run(toAwaitSource(), text('https://example.com/a.mp4'));
  r = onEffectResult(r.session, { type: 'probe_source', ok: true, name: `<b>${SECRET_URL}</b>.mp4` }, ctx());
  r = onEffectResult(r.session, { type: 'create_request', ok: true, request_id: 'r' }, ctx());
  assertSafeText(allText(r), 'card');
  assert.ok(!/[<>]/.test(allText(r)));
});

// ----------------------------------------------------------- files

test('chat file <= 20 MB -> upload_inbox effect', () => {
  const r = run(toAwaitSource(), file({ name: 'lec.mp3', mime: 'audio/mpeg', size_bytes: 5 * 1024 * 1024 }));
  assert.equal(r.session.data.pending, 'upload');
  assert.equal(r.effects[0].type, 'upload_inbox');
  assert.equal(r.effects[0].file.ref, 'ref1');
  assert.equal(r.effects[0].as_text, false);
  const exact = run(toAwaitSource(), file({ size_bytes: LIMITS.chat_file_max_bytes }));
  assert.equal(exact.effects[0].type, 'upload_inbox');
});

test('chat file > 20 MB -> "send a link" and no effects', () => {
  const r = run(toAwaitSource(), file({ size_bytes: LIMITS.chat_file_max_bytes + 1 }));
  assert.deepEqual(r.effects, []);
  assert.match(allText(r), /20 МБ/);
  assert.match(allText(r), /ссылк/);
  assert.equal(r.session.step, 'await_source');
});

test('documents: audio/video accepted, text files as text, others refused', () => {
  const doc = (name, mime) => run(toAwaitSource(), file({ kind: 'document', name, mime, size_bytes: 1000 }));
  assert.equal(doc('lec.m4a', 'application/octet-stream').effects[0].as_text, false);
  assert.equal(doc('x.bin', 'video/mp4').effects[0].type, 'upload_inbox');
  assert.equal(doc('notes.txt', 'text/plain').effects[0].as_text, true);
  assert.equal(doc('notes.MD', '').effects[0].as_text, true);
  const bad = doc('slides.pdf', 'application/pdf');
  assert.deepEqual(bad.effects, []);
  assert.match(allText(bad), /не подходит/);
});

test('upload ok (media) -> create_request chat_file with inbox_path', () => {
  let r = run(toAwaitSource(), file({ name: 'lec.mp3', size_bytes: 1000 }));
  r = onEffectResult(r.session, { type: 'upload_inbox', ok: true, inbox_path: 'inbox/abc.mp3', name: 'lec.mp3', size_bytes: 1000 }, ctx());
  assert.equal(r.effects[0].type, 'create_request');
  assert.equal(r.effects[0].source_kind, 'chat_file');
  assert.equal(r.effects[0].source.inbox_path, 'inbox/abc.mp3');
});

test('upload ok (text file) -> create_request text; too short text refused', () => {
  const doc = () => run(toAwaitSource(), file({ kind: 'document', name: 'n.txt', mime: 'text/plain', size_bytes: 3000 }));
  const okText = 'слово '.repeat(500);
  let r = onEffectResult(doc().session, { type: 'upload_inbox', ok: true, inbox_path: 'inbox/n.txt', name: 'n.txt', text: okText }, ctx());
  assert.equal(r.effects[0].source_kind, 'text');
  assert.equal(r.effects[0].source.text, okText);
  r = onEffectResult(doc().session, { type: 'upload_inbox', ok: true, name: 'n.txt', text: 'коротко' }, ctx());
  assert.deepEqual(r.effects, []);
  assert.match(allText(r), /маловато/);
});

test('upload failure -> human text, back to await_source', () => {
  const s = run(toAwaitSource(), file({ size_bytes: 10 })).session;
  const r = onEffectResult(s, { type: 'upload_inbox', ok: false, error: 'internal' }, ctx());
  assert.equal(r.outgoing[0].text, ERROR_TEXTS.internal);
  assert.equal(r.session.step, 'await_source');
  assert.equal(r.session.data.pending, null);
});

test('file while choosing a course -> hint, session kept', () => {
  const s = run(null, cmd('new')).session;
  const r = run(s, file({ size_bytes: 10 }));
  assert.equal(r.session.step, 'pick_course');
  assert.deepEqual(r.effects, []);
});

// ----------------------------------------------------------- text lecture

test('text of >= 2000 chars -> create_request text without probing', () => {
  const body = 'Это текст лекции. '.repeat(150); // 2700 chars
  const r = run(toAwaitSource(), text(body));
  assert.equal(r.effects.length, 1);
  assert.equal(r.effects[0].type, 'create_request');
  assert.equal(r.effects[0].source_kind, 'text');
  assert.equal(r.effects[0].source.chars, Array.from(body).length);
  assert.equal(r.session.data.pending, 'create');
});

test('long text that contains a link is still a text lecture', () => {
  const body = `${'Абзац лекции. '.repeat(160)} см. https://example.com/x`;
  const r = run(toAwaitSource(), text(body));
  assert.equal(r.effects[0].source_kind, 'text');
});

test('text exactly 2000 chars passes, 1999 is refused', () => {
  assert.equal(run(toAwaitSource(), text('я'.repeat(2000))).effects[0].source_kind, 'text');
  const r = run(toAwaitSource(), text('я'.repeat(1999)));
  assert.deepEqual(r.effects, []);
});

test('text over 250000 chars is refused', () => {
  const r = run(toAwaitSource(), text('я'.repeat(300000)));
  assert.deepEqual(r.effects, []);
  assert.match(allText(r), /слишком длинный/);
});

// ----------------------------------------------------------- confirmation

test('create ok -> confirmation card with remaining quota and the success-only phrase', () => {
  const r = toConfirm(2);
  assert.equal(r.session.step, 'confirm');
  assert.equal(r.session.data.request_id, 'req-1');
  const card = r.outgoing[0].text;
  assert.match(card, /Осталось лекций в квоте: 2/);
  assert.match(card, /Квота списывается только если лекция успешно обработана/);
  assert.match(card, /Курс А/);
  assert.match(card, /Яндекс Диск/);
  assert.match(card, /50 МБ/);
  assert.match(card, /1 ч 15 мин/);
  assert.deepEqual(r.outgoing[0].buttons, [[{ id: 'new:ok', label: '▶ Начать' }, { id: 'new:cancel', label: 'Отмена' }]]);
  assertSafeText(card, 'card');
});

test('confirmation card: unlimited quota, new course, text source', () => {
  const t = confirmationText({ course_title: 'Новый', is_new_course: true, source_kind: 'text', chars: 2500 }, null);
  assert.match(t, /Квота: без ограничений/);
  assert.match(t, /\(новый курс\)/);
  assert.match(t, /2500 символов/);
  assert.match(t, /списывается только если лекция успешно обработана/);
  const one = confirmationText({ source_kind: 'chat_file' }, 1);
  assert.match(one, /Осталось лекций в квоте: 1/);
});

test('create_request failures: duplicate stays in await_source; no_quota ends the dialog', () => {
  const s = run(toAwaitSource(), text('https://example.com/a.mp4'));
  const p = onEffectResult(s.session, { type: 'probe_source', ok: true }, ctx());
  const dup = onEffectResult(p.session, { type: 'create_request', ok: false, error: 'duplicate' }, ctx());
  assert.equal(dup.session.step, 'await_source');
  assert.match(allText(dup), /уже есть/);
  const nq = onEffectResult(p.session, { type: 'create_request', ok: false, error: 'no_quota' }, ctx());
  assert.equal(nq.session, null);
  assert.match(allText(nq), /закончились/);
  const unknown = onEffectResult(p.session, { type: 'create_request', ok: false, error: 'zzz' }, ctx());
  assert.equal(unknown.outgoing[0].text, ERROR_TEXTS.internal);
});

test('"Начать" -> confirm_request effect once; second tap ignored', () => {
  const c = toConfirm();
  const r = run(c.session, btn(BTN.OK));
  assert.deepEqual(r.effects, [{ type: 'confirm_request', request_id: 'req-1' }]);
  assert.equal(r.session.data.pending, 'confirm');
  const again = run(r.session, btn(BTN.OK));
  assert.deepEqual(again.effects, []);
  assert.deepEqual(again.outgoing, []);
});

test('confirm ok -> queued message with position, quota not spent yet, session closed', () => {
  const c = toConfirm();
  const r1 = run(c.session, btn(BTN.OK));
  const r = onEffectResult(r1.session, { type: 'confirm_request', ok: true, lecture_id: 'l1', queue_position: 3 }, ctx());
  assert.equal(r.session, null);
  assert.match(allText(r), /Перед вами: 3/);
  assert.match(allText(r), /Квота спишется, только если обработка завершится успешно/);
  const first = onEffectResult(r1.session, { type: 'confirm_request', ok: true, queue_position: 0 }, ctx());
  assert.ok(!/Перед вами/.test(allText(first)));
});

test('confirm refusals: every reason has its own text and releases the request', () => {
  const reasons = Object.keys(CONFIRM_FAIL_TEXTS);
  assert.deepEqual(reasons.sort(), ['bad_state', 'budget_stop', 'busy_user', 'daily_cap', 'no_quota', 'not_owner', 'too_many_failures']);
  for (const reason of reasons) {
    const c = toConfirm();
    const r1 = run(c.session, btn(BTN.OK));
    const r = onEffectResult(r1.session, { type: 'confirm_request', ok: false, reason }, ctx());
    assert.equal(r.session, null, reason);
    assert.equal(r.outgoing[0].text, CONFIRM_FAIL_TEXTS[reason]);
    if (reason === 'bad_state' || reason === 'not_owner') assert.deepEqual(r.effects, []);
    else assert.deepEqual(r.effects, [{ type: 'cancel_request', request_id: 'req-1', notify: false }]);
  }
});

test('text in confirm step -> reminder with the same buttons', () => {
  const c = toConfirm();
  const r = run(c.session, text('ок'));
  assert.equal(r.session.step, 'confirm');
  assert.match(allText(r), /Начать/);
  assert.deepEqual(r.effects, []);
});

test('"Начать" outside the confirm step is a stale button', () => {
  const s = toAwaitSource();
  const r = run(s, btn(BTN.OK));
  assert.deepEqual(r.effects, []);
  assert.match(allText(r), /устарела/);
});

// ----------------------------------------------------------- cancel

test('cancel at every step ends the session; request is released only when it exists', () => {
  const states = [];
  const s1 = run(null, cmd('new')).session;
  states.push(['pick_course', s1]);
  states.push(['new_course_title', run(s1, btn(BTN.NEW_COURSE)).session]);
  states.push(['await_source', toAwaitSource()]);
  states.push(['confirm', toConfirm().session]);

  for (const [name, session] of states) {
    for (const how of [cmd('cancel'), btn(BTN.CANCEL)]) {
      const r = run(session, how);
      assert.equal(r.session, null, name);
      assert.match(allText(r), /отменено/, name);
      if (name === 'confirm') {
        assert.deepEqual(r.effects, [{ type: 'cancel_request', request_id: 'req-1', notify: false }]);
      } else {
        assert.deepEqual(r.effects, [], name);
      }
    }
  }
});

test('/cancel without session: nothing to cancel / queued request is cancelled / processing is not', () => {
  assert.match(allText(run(null, cmd('cancel'))), /нечего отменять/);
  const q = run(null, cmd('cancel'), ctx({ active_request: { request_id: 'r9', status: 'queued' } }));
  assert.deepEqual(q.effects, [{ type: 'cancel_request', request_id: 'r9', notify: true }]);
  const p = run(null, cmd('cancel'), ctx({ active_request: { request_id: 'r9', status: 'processing' } }));
  assert.deepEqual(p.effects, []);
  assert.match(allText(p), /отменить нельзя/);
});

test('cancel_request result: notified only when asked; ok vs refused wording', () => {
  const c = { to: { channel: 'telegram', external_chat_id: '111' } };
  assert.deepEqual(onEffectResult(null, { type: 'cancel_request', ok: true, notify: false }, c).outgoing, []);
  assert.match(allText(onEffectResult(null, { type: 'cancel_request', ok: true, notify: true }, c)), /Квота не списана/);
  assert.match(allText(onEffectResult(null, { type: 'cancel_request', ok: false, notify: true }, c)), /уже обрабатывается/);
});

test('request created after the user cancelled is released', () => {
  const s = run(toAwaitSource(), text('https://example.com/a.mp4')).session;
  const p = onEffectResult(s, { type: 'probe_source', ok: true }, ctx());
  const cancelled = run(p.session, cmd('cancel'));
  assert.equal(cancelled.session, null);
  const late = onEffectResult(null, { type: 'create_request', ok: true, request_id: 'late-1' }, ctx({ to: { channel: 'telegram', external_chat_id: '111' } }));
  assert.deepEqual(late.effects, [{ type: 'cancel_request', request_id: 'late-1', notify: false }]);
});

// ----------------------------------------------------------- idempotency / expiry

test('same event_id twice -> second application is a no-op', () => {
  const s = run(null, cmd('new')).session;
  const e = btn('new:course:0');
  const first = step(s, e, ctx());
  assert.equal(first.session.step, 'await_source');
  const dup = step(first.session, e, ctx());
  assert.deepEqual(dup.outgoing, []);
  assert.deepEqual(dup.effects, []);
  assert.equal(dup.session, first.session);
});

test('"Начать" with the same event_id does not create a second confirm effect', () => {
  const c = toConfirm();
  const e = btn(BTN.OK);
  const first = step(c.session, e, ctx());
  const dup = step(first.session, e, ctx());
  assert.equal(first.effects.length, 1);
  assert.deepEqual(dup.effects, []);
});

test('expired session: text and buttons say it expired; /new starts fresh; request released', () => {
  const c = toConfirm();
  const late = ctx({ now: NOW + (LIMITS.session_ttl_min + 1) * 60000 });
  const t = step(c.session, text('привет'), late);
  assert.equal(t.session, null);
  assert.match(allText(t), /Время ожидания/);
  assert.deepEqual(t.effects, [{ type: 'cancel_request', request_id: 'req-1', notify: false }]);
  const b = step(c.session, btn(BTN.OK), late);
  assert.equal(b.session, null);
  assert.deepEqual(b.effects.map((e) => e.type), ['cancel_request']);
  assert.match(allText(b), /Время ожидания/);
  const n = step(c.session, cmd('new'), late);
  assert.equal(n.session.step, 'pick_course');
  const x = step(c.session, cmd('cancel'), late);
  assert.equal(x.session, null);
});

test('session is alive right up to the TTL', () => {
  const s = run(null, cmd('new')).session;
  const r = step(s, btn('new:course:0'), ctx({ now: NOW + LIMITS.session_ttl_min * 60000 - 1000 }));
  assert.equal(r.session.step, 'await_source');
});

test('every accepted event extends the session', () => {
  const s = run(null, cmd('new')).session;
  const later = NOW + 10 * 60000;
  const r = step(s, btn('new:course:0'), ctx({ now: later }));
  assert.equal(Date.parse(r.session.expires_at), later + LIMITS.session_ttl_min * 60000);
});

test('no session: text / file / stale button hints, nothing created', () => {
  for (const e of [text('привет'), file({ size_bytes: 1 })]) {
    const r = run(null, e);
    assert.equal(r.session, null);
    assert.deepEqual(r.effects, []);
    assert.match(allText(r), /\/new/);
  }
  assert.match(allText(run(null, btn('new:ok'))), /устарела/);
});

// ----------------------------------------------------------- misc commands / input

test('/status: no active request, queued, processing, unknown (effect)', () => {
  assert.match(allText(run(null, cmd('status'))), /нет лекций в обработке/);
  assert.match(allText(run(null, cmd('status'))), /Осталось лекций в квоте: 2/);
  assert.match(allText(run(null, cmd('status'), ctx({ active_request: { status: 'queued', queue_position: 1 } }))), /Перед вами: 1/);
  assert.match(allText(run(null, cmd('status'), ctx({ active_request: { status: 'processing' } }))), /обрабатывается/);
  const u = run(null, cmd('status'), { now: NOW });
  assert.deepEqual(u.effects, [{ type: 'show_status' }]);
  assert.ok(!/undefined/.test(allText(run(null, cmd('status'), { now: NOW, active_request: null }))));
});

test('/status keeps an open session untouched', () => {
  const s = toAwaitSource();
  const r = run(s, cmd('status'));
  assert.equal(r.session, s);
});

test('unknown command -> help line, session untouched', () => {
  const s = toAwaitSource();
  const r = run(s, cmd('whatever'));
  assert.equal(r.session, s);
  assert.match(allText(r), /\/new/);
});

test('invalid events throw invalid_event without echoing values', () => {
  assert.throws(() => step(null, { channel: 'Bad', kind: 'text' }, ctx()), { code: 'invalid_event' });
  try {
    step(null, { ...text('x'), channel: SECRET_URL }, ctx());
    assert.fail();
  } catch (e) {
    assert.ok(!e.message.includes('secret-path-xyz'));
  }
});

test('parseButton understands only the new: grammar', () => {
  assert.deepEqual(parseButton('new:ok'), { action: 'ok' });
  assert.deepEqual(parseButton('new:cancel'), { action: 'cancel' });
  assert.deepEqual(parseButton('new:newcourse'), { action: 'newcourse' });
  assert.deepEqual(parseButton('new:course:3'), { action: 'course', idx: 3 });
  assert.deepEqual(parseButton('new:page:8'), { action: 'page', offset: 8 });
  for (const bad of ['quiz:ok', 'course:1', 'new:', 'new:course:', 'new:course:-1', 'new:course:1x', 'new:page:99999999', 'new:zzz', '', null, 5]) {
    assert.equal(parseButton(bad), null, String(bad));
  }
});

test('while a probe/upload is in flight, extra messages are ignored', () => {
  const r = run(toAwaitSource(), text('https://example.com/a.mp4'));
  const again = run(r.session, text('https://example.com/b.mp4'));
  assert.deepEqual(again.effects, []);
  assert.deepEqual(again.outgoing, []);
});

test('effect result for a stale pending state is ignored', () => {
  const s = toAwaitSource();
  const r = onEffectResult(s, { type: 'probe_source', ok: true }, ctx());
  assert.deepEqual(r.effects, []);
  assert.deepEqual(r.outgoing, []);
  assert.deepEqual(onEffectResult(s, null, ctx()).effects, []);
});

// ----------------------------------------------------------- text hygiene

test('every static user text is plain: no HTML, no links', () => {
  const texts = [...Object.values(ERROR_TEXTS), ...Object.values(CONFIRM_FAIL_TEXTS)];
  for (const t of texts) assertSafeText(t, t.slice(0, 30));
  for (const t of texts) assert.ok(/[а-яё]/i.test(t), 'Russian text');
});

test('a full happy path never leaks the user link and always produces valid messages', () => {
  let session = null;
  const all = [];
  const feed = (r) => {
    for (const m of r.outgoing) {
      assert.doesNotThrow(() => validateOutgoing(m));
      for (const b of (m.buttons || []).flat()) {
        assert.ok(b.id.startsWith('new:'));
        assert.ok(utf8Bytes(b.id) <= 48);
      }
      all.push(m.text);
    }
    session = r.session;
    return r;
  };
  feed(step(session, cmd('new'), ctx()));
  feed(step(session, btn('new:newcourse'), ctx()));
  feed(step(session, text('Моя психология'), ctx()));
  let r = feed(step(session, text(SECRET_URL), ctx()));
  r = feed(onEffectResult(session, { type: 'probe_source', ok: true, name: 'lecture.mp4', size_bytes: 1e8, duration_sec: 3000 }, ctx()));
  r = feed(onEffectResult(session, { type: 'create_request', ok: true, request_id: 'r1', quota_remaining: 1 }, ctx()));
  r = feed(step(session, btn('new:ok'), ctx()));
  assert.equal(r.effects[0].type, 'confirm_request');
  feed(onEffectResult(session, { type: 'confirm_request', ok: true, queue_position: 1 }, ctx()));
  assert.equal(session, null);
  assertSafeText(all.join('\n'), 'happy path');
  assert.ok(all.some((t) => /Осталось лекций в квоте: 1/.test(t)));
});

test('the url only travels inside effects / session data, not in messages', () => {
  const r = run(toAwaitSource(), text(SECRET_URL));
  assert.equal(r.effects[0].url, 'https://example.com/secret-path-xyz/lecture.mp4?token=abc123');
  assertSafeText(allText(r), 'probe start');
});

// ----------------------------------------------------------- notifications

test('renderNotification: done', () => {
  const t = renderNotification('request_done', { lecture_title: 'Тема 1', quota_remaining: 1 });
  assert.match(t, /Тема 1/);
  assert.match(t, /готова/);
  assert.match(t, /\/quiz/);
  assert.match(t, /Осталось лекций в квоте: 1/);
  assert.ok(!/квоте/.test(renderNotification('request_done', {})));
  assert.match(renderNotification('request_done', { quota_remaining: null }), /без ограничений/);
});

test('renderNotification: failed — each code has text, quota stays', () => {
  for (const code of Object.keys(ERROR_TEXTS)) {
    const t = renderNotification('request_failed', { error_code: code });
    assert.ok(t.includes(ERROR_TEXTS[code]), code);
    assert.match(t, /Квота не списана/);
    assertSafeText(t, code);
  }
  const t = renderNotification('request_failed', { error_code: 'budget_exceeded' });
  assert.ok(t.includes(ERROR_TEXTS.pipeline_failed));
  assert.match(renderNotification('request_failed'), /Квота не списана/);
});

test('renderNotification: expired and unknown kind', () => {
  assert.match(renderNotification('request_expired', {}), /Квота не списана/);
  assert.throws(() => renderNotification('weird', {}), { code: 'unknown_notification_kind' });
});

test('renderNotification: titles are sanitised (no links, no HTML)', () => {
  for (const kind of ['request_done', 'request_failed']) {
    const t = renderNotification(kind, { lecture_title: `<i>${SECRET_URL}</i> Тема`, error_code: 'timeout' });
    assertSafeText(t, kind);
    assert.ok(!/[<>]/.test(t));
    assert.match(t, /Тема/);
  }
});

// ----------------------------------------------------------- helpers

test('sanitizeText removes links, tags, control chars, collapses spaces, truncates', () => {
  assert.equal(sanitizeText('a  https://example.com/x?y=1  b'), 'a … b');
  assert.equal(sanitizeText('yadi.sk/i/AbCdEf'), '…');
  assert.equal(sanitizeText('<b>жир</b>\n\tтекст'), 'bжир/b текст');
  assert.equal(sanitizeText(null), '');
  assert.equal(Array.from(sanitizeText('я'.repeat(50), 10)).length, 10);
  assert.equal(sanitizeText('lecture.final.mp4'), 'lecture.final.mp4');
});

test('formatSize / formatDuration', () => {
  assert.equal(formatSize(500), '500 Б');
  assert.equal(formatSize(2048), '2 КБ');
  assert.equal(formatSize(5 * 1024 * 1024 + 300000), '5,3 МБ');
  assert.equal(formatSize(3 * 1024 ** 3), '3 ГБ');
  assert.equal(formatSize(null), null);
  assert.equal(formatSize(-1), null);
  assert.equal(formatDuration(20), 'меньше минуты');
  assert.equal(formatDuration(45 * 60), '45 мин');
  assert.equal(formatDuration(3600), '1 ч');
  assert.equal(formatDuration(4500), '1 ч 15 мин');
  assert.equal(formatDuration(undefined), null);
});

test('error texts mention concrete limits from LIMITS', () => {
  assert.match(ERROR_TEXTS.too_long, /2 часа/);
  assert.match(ERROR_TEXTS.too_large, /4 ГБ/);
  assert.match(ERROR_TEXTS.too_large, /20 МБ/);
});
