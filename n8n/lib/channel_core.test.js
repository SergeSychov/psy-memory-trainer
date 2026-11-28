'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { LIMITS } = require('./source_links');
const { normalizeIncoming, validateOutgoing, splitText, utf8Bytes } = require('./channel_core');

function base(extra) {
  return {
    channel: 'telegram',
    external_user_id: '111',
    external_chat_id: '111',
    event_id: '9001',
    kind: 'text',
    text: 'привет',
    ...extra,
  };
}

function out(extra) {
  return { to: { channel: 'telegram', external_chat_id: '111' }, text: 'Привет', ...extra };
}

function btn(id, label) {
  return { id, label: label === undefined ? 'Кнопка' : label };
}

// ---- normalizeIncoming ----

test('normalizeIncoming: text event, numbers become strings', () => {
  const ev = normalizeIncoming(base({ external_user_id: 111, external_chat_id: 222, event_id: 5 }));
  assert.equal(ev.external_user_id, '111');
  assert.equal(ev.external_chat_id, '222');
  assert.equal(ev.event_id, '5');
  assert.equal(ev.kind, 'text');
  assert.equal(ev.text, 'привет');
});

test('normalizeIncoming: command is stripped of slash / @bot and lowercased', () => {
  assert.equal(normalizeIncoming(base({ kind: 'command', command: '/New@MyBot', text: undefined })).command, 'new');
  assert.equal(normalizeIncoming(base({ kind: 'command', command: 'cancel', text: undefined })).command, 'cancel');
});

test('normalizeIncoming: bad commands rejected', () => {
  for (const command of ['', '/', '/9new', '/new now', '/new;drop', 'x'.repeat(40), 123, undefined]) {
    assert.throws(() => normalizeIncoming(base({ kind: 'command', command, text: undefined })), { code: 'invalid_event' });
  }
});

test('normalizeIncoming: channel pattern ^[a-z][a-z0-9_]{1,15}$', () => {
  for (const channel of ['telegram', 'max', 'web', 'app', 'car_audio', 'ab']) {
    assert.equal(normalizeIncoming(base({ channel })).channel, channel);
  }
  for (const channel of ['', 'a', 'Telegram', '1abc', 'with-dash', 'x'.repeat(17), 'тг', null, undefined]) {
    assert.throws(() => normalizeIncoming(base({ channel })), { code: 'invalid_event' }, String(channel));
  }
});

test('normalizeIncoming: kind and required ids validated', () => {
  assert.throws(() => normalizeIncoming(base({ kind: 'voice' })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(base({ external_user_id: '' })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(base({ external_chat_id: undefined })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(base({ event_id: 'x'.repeat(LIMITS.event_id_max + 1) })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(base({ event_id: 'a\nb' })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(null), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming('text'), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming([]), { code: 'invalid_event' });
});

test('normalizeIncoming: text must be a string; over-long text truncated to ceiling+1', () => {
  assert.throws(() => normalizeIncoming(base({ text: 5 })), { code: 'invalid_event' });
  const ev = normalizeIncoming(base({ text: 'я'.repeat(LIMITS.text_max_chars + 500) }));
  assert.equal(Array.from(ev.text).length, LIMITS.text_max_chars + 1);
});

test('normalizeIncoming: button id limit is 48 UTF-8 bytes (unicode counts per byte)', () => {
  const ok = 'new:' + 'a'.repeat(44);
  assert.equal(utf8Bytes(ok), 48);
  assert.equal(normalizeIncoming(base({ kind: 'button', button_id: ok, text: undefined })).button_id, ok);
  assert.throws(() => normalizeIncoming(base({ kind: 'button', button_id: ok + 'a', text: undefined })), { code: 'invalid_event' });
  // 24 Cyrillic letters = 48 bytes, 25 = 50 bytes though only 25 chars
  assert.equal(normalizeIncoming(base({ kind: 'button', button_id: 'я'.repeat(24), text: undefined })).button_id.length, 24);
  assert.throws(() => normalizeIncoming(base({ kind: 'button', button_id: 'я'.repeat(25), text: undefined })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(base({ kind: 'button', button_id: '', text: undefined })), { code: 'invalid_event' });
});

test('normalizeIncoming: file event keeps known fields, lowercases mime, cleans name', () => {
  const ev = normalizeIncoming(
    base({
      kind: 'file',
      text: undefined,
      file: { ref: 'AgAD123', name: 'лек\nция.MP3', mime: 'Audio/MPEG', size_bytes: 1234.7, kind: 'audio', extra: 'x' },
    })
  );
  assert.deepEqual(ev.file, { ref: 'AgAD123', kind: 'audio', name: 'лек ция.MP3', mime: 'audio/mpeg', size_bytes: 1234 });
});

test('normalizeIncoming: file validation', () => {
  const f = (file) => base({ kind: 'file', text: undefined, file });
  assert.throws(() => normalizeIncoming(f(undefined)), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(f({ ref: '', kind: 'audio' })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(f({ ref: 'x', kind: 'sticker' })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(f({ ref: 'x', kind: 'audio', size_bytes: -5 })), { code: 'invalid_event' });
  assert.throws(() => normalizeIncoming(f({ ref: 'x', kind: 'audio', size_bytes: 'big' })), { code: 'invalid_event' });
  const long = normalizeIncoming(f({ ref: 'x', kind: 'video', name: 'n'.repeat(1000) }));
  assert.equal(long.file.name.length, LIMITS.file_name_max);
});

test('normalizeIncoming: caption on file is kept as text; display_name trimmed', () => {
  const ev = normalizeIncoming(
    base({ kind: 'file', text: 'подпись', file: { ref: 'x', kind: 'document' }, display_name: ' Имя '.repeat(40) })
  );
  assert.equal(ev.text, 'подпись');
  assert.ok(ev.display_name.length <= LIMITS.display_name_max);
});

test('normalizeIncoming: error messages never contain input values', () => {
  const secret = 'https://example.com/private-token-123';
  try {
    normalizeIncoming(base({ channel: secret }));
    assert.fail('should throw');
  } catch (e) {
    assert.equal(e.code, 'invalid_event');
    assert.ok(!e.message.includes('private-token-123'));
  }
  try {
    normalizeIncoming(base({ kind: 'command', command: secret, text: undefined }));
    assert.fail('should throw');
  } catch (e) {
    assert.ok(!e.message.includes('private-token-123'));
  }
});

// ---- validateOutgoing ----

test('validateOutgoing: minimal and full valid messages', () => {
  assert.doesNotThrow(() => validateOutgoing(out()));
  assert.doesNotThrow(() =>
    validateOutgoing(out({ buttons: [[btn('new:ok', 'Начать'), btn('new:cancel', 'Отмена')]], expect: 'link' }))
  );
  assert.equal(validateOutgoing(out()).text, 'Привет');
  assert.doesNotThrow(() => validateOutgoing(out({ expect: null })));
});

test('validateOutgoing: destination and text required', () => {
  assert.throws(() => validateOutgoing(null), { code: 'invalid_outgoing' });
  assert.throws(() => validateOutgoing({ text: 'x' }), { code: 'invalid_outgoing' });
  assert.throws(() => validateOutgoing(out({ to: { channel: 'Bad', external_chat_id: '1' } })), { code: 'invalid_outgoing' });
  assert.throws(() => validateOutgoing(out({ to: { channel: 'telegram', external_chat_id: '' } })), { code: 'invalid_outgoing' });
  assert.throws(() => validateOutgoing(out({ text: '' })), { code: 'invalid_outgoing' });
  assert.throws(() => validateOutgoing(out({ text: '   ' })), { code: 'invalid_outgoing' });
  assert.throws(() => validateOutgoing(out({ text: 5 })), { code: 'invalid_outgoing' });
});

test('validateOutgoing: text limit 4096 by default, overridable', () => {
  assert.doesNotThrow(() => validateOutgoing(out({ text: 'a'.repeat(4096) })));
  assert.throws(() => validateOutgoing(out({ text: 'a'.repeat(4097) })), /text_too_long/);
  assert.doesNotThrow(() => validateOutgoing(out({ text: 'a'.repeat(10000) }), { max_text: 20000 }));
});

test('validateOutgoing: at most 6 rows', () => {
  const rows = (n) => Array.from({ length: n }, (_, i) => [btn(`new:b${i}`)]);
  assert.doesNotThrow(() => validateOutgoing(out({ buttons: rows(6) })));
  assert.throws(() => validateOutgoing(out({ buttons: rows(7) })), /buttons.rows/);
});

test('validateOutgoing: at most 2 buttons per row, no empty rows', () => {
  assert.doesNotThrow(() => validateOutgoing(out({ buttons: [[btn('a:1'), btn('a:2')]] })));
  assert.throws(() => validateOutgoing(out({ buttons: [[btn('a:1'), btn('a:2'), btn('a:3')]] })), /per_row/);
  assert.throws(() => validateOutgoing(out({ buttons: [[]] })), { code: 'invalid_outgoing' });
  assert.throws(() => validateOutgoing(out({ buttons: 'x' })), { code: 'invalid_outgoing' });
});

test('validateOutgoing: label <= 40 characters (code points), non-empty', () => {
  assert.doesNotThrow(() => validateOutgoing(out({ buttons: [[btn('a:1', 'я'.repeat(40))]] })));
  assert.doesNotThrow(() => validateOutgoing(out({ buttons: [[btn('a:1', '😀'.repeat(40))]] })));
  assert.throws(() => validateOutgoing(out({ buttons: [[btn('a:1', 'я'.repeat(41))]] })), /button.label/);
  assert.throws(() => validateOutgoing(out({ buttons: [[btn('a:1', '')]] })), /button.label/);
});

test('validateOutgoing: button id <= 48 UTF-8 bytes (not characters)', () => {
  assert.doesNotThrow(() => validateOutgoing(out({ buttons: [[btn('x'.repeat(48))]] })));
  assert.throws(() => validateOutgoing(out({ buttons: [[btn('x'.repeat(49))]] })), /button.id/);
  assert.doesNotThrow(() => validateOutgoing(out({ buttons: [[btn('я'.repeat(24))]] }))); // 48 bytes
  assert.throws(() => validateOutgoing(out({ buttons: [[btn('я'.repeat(25))]] })), /button.id/); // 50 bytes
  assert.throws(() => validateOutgoing(out({ buttons: [[btn('')]] })), /button.id/);
});

test('validateOutgoing: duplicate button ids rejected, bad expect rejected', () => {
  assert.throws(() => validateOutgoing(out({ buttons: [[btn('a:1')], [btn('a:1')]] })), /duplicate/);
  assert.throws(() => validateOutgoing(out({ expect: 'voice' })), /expect/);
});

// ---- splitText ----

test('splitText: short text is returned as is; empty gives []', () => {
  assert.deepEqual(splitText('коротко'), ['коротко']);
  assert.deepEqual(splitText(''), []);
  assert.deepEqual(splitText('   '), []);
  assert.deepEqual(splitText(null), []);
});

test('splitText: chunks never exceed the limit and keep all words', () => {
  const words = Array.from({ length: 2000 }, (_, i) => `слово${i}`);
  const text = words.join(' ');
  const chunks = splitText(text, 4096);
  assert.ok(chunks.length > 1);
  for (const c of chunks) assert.ok(c.length <= 4096);
  assert.equal(chunks.join(' ').replace(/\s+/g, ' '), text);
});

test('splitText: prefers paragraph boundaries', () => {
  const p1 = 'а'.repeat(60);
  const p2 = 'б'.repeat(60);
  assert.deepEqual(splitText(`${p1}\n\n${p2}`, 100), [p1, p2]);
});

test('splitText: hard-cuts a word longer than the limit; keeps surrogate pairs whole', () => {
  const chunks = splitText('x'.repeat(25), 10);
  assert.deepEqual(chunks.map((c) => c.length), [10, 10, 5]);
  const emoji = '😀'.repeat(10); // 20 UTF-16 units
  const parts = splitText(emoji, 5);
  for (const p of parts) {
    assert.ok(p.length <= 5);
    assert.equal(p.length % 2, 0, 'no lone surrogate');
  }
  assert.equal(parts.join(''), emoji);
});

test('splitText: default limit is the Telegram limit', () => {
  const chunks = splitText('a'.repeat(4096 * 2 + 1));
  assert.deepEqual(chunks.map((c) => c.length), [4096, 4096, 1]);
});
