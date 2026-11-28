'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { LIMITS, extractUrls, classifySource, checkLimits } = require('./source_links');

const GID = 'AbCdEfGhIjKlMnOpQrStUvWxYz_-0123';

test('LIMITS has the agreed numbers and is frozen', () => {
  assert.equal(LIMITS.default_quota, 2);
  assert.equal(LIMITS.max_duration_sec, 7200);
  assert.equal(LIMITS.duration_tolerance_sec, 60);
  assert.equal(LIMITS.chat_file_max_bytes, 20 * 1024 * 1024);
  assert.equal(LIMITS.link_max_bytes, 4 * 1024 * 1024 * 1024);
  assert.equal(LIMITS.text_min_chars, 2000);
  assert.equal(LIMITS.text_max_chars, 250000);
  assert.equal(LIMITS.daily_cap, 10);
  assert.equal(LIMITS.max_failures_24h, 5);
  assert.equal(LIMITS.monthly_budget_usd, 5);
  assert.equal(LIMITS.stop_ratio, 0.8);
  assert.equal(LIMITS.session_ttl_min, 30);
  assert.ok(Object.isFrozen(LIMITS));
});

test('extractUrls: finds links, trims punctuation, keeps order', () => {
  const urls = extractUrls('Вот ссылка: https://example.com/a.mp4, и ещё (https://example.org/b).');
  assert.deepEqual(urls, ['https://example.com/a.mp4', 'https://example.org/b']);
});

test('extractUrls: bare Yandex/Drive hosts get https://', () => {
  assert.deepEqual(extractUrls('yadi.sk/i/abcdef123456'), ['https://yadi.sk/i/abcdef123456']);
  assert.deepEqual(extractUrls('глянь drive.google.com/file/d/X/view'), ['https://drive.google.com/file/d/X/view']);
});

test('extractUrls: empty / non-string / no links', () => {
  assert.deepEqual(extractUrls(''), []);
  assert.deepEqual(extractUrls(null), []);
  assert.deepEqual(extractUrls(undefined), []);
  assert.deepEqual(extractUrls(42), []);
  assert.deepEqual(extractUrls('просто текст без ссылок'), []);
});

test('extractUrls: surfaces dangerous schemes so classify can refuse them', () => {
  const urls = extractUrls('file:///etc/passwd ftp://example.com/x');
  assert.equal(urls.length, 2);
  for (const u of urls) assert.equal(classifySource(u).error, 'unsupported_link');
});

test('extractUrls: capped number of urls', () => {
  const text = Array.from({ length: 50 }, (_, i) => `https://example.com/${i}`).join(' ');
  assert.equal(extractUrls(text).length, LIMITS.max_urls_considered);
});

// ---- Yandex ----

test('yandex: /i/ on disk.yandex.ru, .com and yadi.sk', () => {
  for (const host of ['disk.yandex.ru', 'disk.yandex.com', 'yadi.sk']) {
    const r = classifySource(`https://${host}/i/AbCdEf123_-x`);
    assert.equal(r.kind, 'yandex_public');
    assert.equal(r.canonical, `https://${host}/i/AbCdEf123_-x`);
    assert.equal(r.error, undefined);
    assert.ok(!r.maybe_folder);
  }
});

test('yandex: /d/ is accepted but flagged maybe_folder', () => {
  const r = classifySource('https://yadi.sk/d/AbCdEf123_-x');
  assert.equal(r.kind, 'yandex_public');
  assert.equal(r.maybe_folder, true);
});

test('yandex: tracking params and fragments dropped, uppercase host normalised', () => {
  const r = classifySource('https://DISK.Yandex.RU/i/AbCdEf123?utm_source=tg&fbclid=1#frag');
  assert.equal(r.kind, 'yandex_public');
  assert.equal(r.canonical, 'https://disk.yandex.ru/i/AbCdEf123');
});

test('yandex: subpaths, client pages, short keys are refused', () => {
  for (const u of [
    'https://disk.yandex.ru/d/AbCdEf123/sub/file.mp4',
    'https://disk.yandex.ru/client/disk',
    'https://disk.yandex.ru/i/ab',
    'https://disk.yandex.ru/',
    'https://disk.yandex.ru/x/AbCdEf123',
  ]) {
    assert.equal(classifySource(u).error, 'unsupported_link', u);
  }
});

// ---- Google Drive ----

test('gdrive: file/d/<id>/view, open?id=, uc?id= all give canonical file link', () => {
  const want = `https://drive.google.com/file/d/${GID}/view`;
  for (const u of [
    `https://drive.google.com/file/d/${GID}/view?usp=sharing`,
    `https://drive.google.com/file/d/${GID}/edit`,
    `https://drive.google.com/file/d/${GID}`,
    `https://drive.google.com/open?id=${GID}`,
    `https://drive.google.com/uc?id=${GID}`,
    `https://drive.google.com/uc?export=download&id=${GID}`,
    `https://drive.google.com/u/0/open?id=${GID}`,
  ]) {
    const r = classifySource(u);
    assert.equal(r.kind, 'gdrive_public', u);
    assert.equal(r.canonical, want, u);
  }
});

test('gdrive: folders, docs.google.com, bad ids are refused', () => {
  for (const u of [
    `https://drive.google.com/drive/folders/${GID}`,
    `https://drive.google.com/drive/u/0/folders/${GID}`,
    `https://docs.google.com/document/d/${GID}/edit`,
    `https://docs.google.com/spreadsheets/d/${GID}`,
    'https://drive.google.com/file/d/short/view',
    'https://drive.google.com/open',
    'https://drive.google.com/open?id=bad%20id%20with%20spaces',
    `https://drive.google.com/open?id=${'A'.repeat(201)}`,
    'https://drive.google.com/',
  ]) {
    assert.equal(classifySource(u).error, 'unsupported_link', u);
  }
});

// ---- direct ----

test('direct_url: plain https link, tracking params removed, others kept', () => {
  const r = classifySource('https://Files.Example.com/path/lecture.mp4?token=1&utm_medium=x&fbclid=2#t=10');
  assert.equal(r.kind, 'direct_url');
  assert.equal(r.canonical, 'https://files.example.com/path/lecture.mp4?token=1');
});

test('direct_url: port 443 is fine, other ports are refused', () => {
  assert.equal(classifySource('https://example.com:443/a.mp3').kind, 'direct_url');
  assert.equal(classifySource('https://example.com:8443/a.mp3').error, 'unsupported_link');
  assert.equal(classifySource('https://example.com:80/a.mp3').error, 'unsupported_link');
});

test('direct_url: trailing dot in host is normalised', () => {
  assert.equal(classifySource('https://example.com./a.mp3').canonical, 'https://example.com/a.mp3');
});

test('refuses dangerous / non-https schemes', () => {
  for (const u of [
    'javascript:alert(1)',
    'file:///etc/passwd',
    'http://example.com/a.mp4',
    'ftp://example.com/a.mp4',
    'data:text/plain;base64,AAAA',
    'gopher://example.com/',
    '//example.com/a.mp4',
    'example.com/a.mp4',
  ]) {
    assert.equal(classifySource(u).error, 'unsupported_link', u);
  }
});

test('refuses http: even for Yandex / Drive hosts', () => {
  assert.equal(classifySource('http://yadi.sk/i/AbCdEf123').error, 'unsupported_link');
  assert.equal(classifySource(`http://drive.google.com/file/d/${GID}/view`).error, 'unsupported_link');
});

test('refuses IP literals (v4, v6, metadata, obfuscated)', () => {
  for (const u of [
    'https://127.0.0.1/a.mp4',
    'https://[::1]/a.mp4',
    'https://[::ffff:127.0.0.1]/a.mp4',
    'https://169.254.169.254/latest/meta-data',
    'https://10.0.0.5/a.mp4',
    'https://192.168.1.1/a.mp4',
    'https://8.8.8.8/a.mp4',
    'https://2130706433/a.mp4',
    'https://0x7f.0.0.1/a.mp4',
    'https://0177.0.0.1/a.mp4',
    'https://127.1/a.mp4',
  ]) {
    assert.equal(classifySource(u).error, 'unsupported_link', u);
  }
});

test('refuses localhost and internal-looking hosts', () => {
  for (const u of [
    'https://localhost/a.mp4',
    'https://LOCALHOST/a.mp4',
    'https://foo.localhost/a.mp4',
    'https://printer.local/a.mp4',
    'https://db.internal/a.mp4',
    'https://svc.corp/a.mp4',
    'https://intranet/a.mp4',
    'https://whisper/a.mp4',
  ]) {
    assert.equal(classifySource(u).error, 'unsupported_link', u);
  }
});

test('refuses user:pass@host', () => {
  assert.equal(classifySource('https://user:pass@example.com/a.mp4').error, 'unsupported_link');
  assert.equal(classifySource('https://user@example.com/a.mp4').error, 'unsupported_link');
  assert.equal(classifySource('https://example.com@127.0.0.1/a.mp4').error, 'unsupported_link');
});

test('refuses unicode / punycode hosts', () => {
  assert.equal(classifySource('https://пример.рф/a.mp4').error, 'unsupported_link');
  assert.equal(classifySource('https://xn--e1afmkfd.xn--p1ai/a.mp4').error, 'unsupported_link');
  assert.equal(classifySource('https://exаmple.com/a.mp4').error, 'unsupported_link'); // cyrillic "а"
});

test('refuses garbage, whitespace, control chars, non-strings', () => {
  for (const u of ['', '   ', 'not a url', 'https://', 'https:// example.com', 'https://exa\nmple.com/', 'https://example.com/a b', null, undefined, 5, {}]) {
    assert.equal(classifySource(u).error, 'unsupported_link', String(u));
  }
});

test('refuses very long strings quickly', () => {
  const long = `https://example.com/${'a'.repeat(5000)}`;
  const r = classifySource(long);
  assert.equal(r.error, 'unsupported_link');
  assert.equal(r.canonical, null);
  const huge = `https://example.com/${'a'.repeat(1e6)}`;
  assert.equal(classifySource(huge).error, 'unsupported_link');
  assert.equal(extractUrls(huge).length, 1);
});

test('result never echoes the input url in error results', () => {
  const r = classifySource('https://user:secret-pass@example.com/private/path');
  assert.equal(r.kind, null);
  assert.equal(r.canonical, null);
  assert.ok(!JSON.stringify(r).includes('secret-pass'));
  assert.ok(!JSON.stringify(r).includes('example.com'));
});

test('tracking-only variants canonicalise to the same value (dedupe)', () => {
  const a = classifySource('https://example.com/a.mp4?utm_source=a');
  const b = classifySource('https://example.com/a.mp4?utm_campaign=b&fbclid=zz');
  const c = classifySource('https://example.com/a.mp4');
  assert.equal(a.canonical, c.canonical);
  assert.equal(b.canonical, c.canonical);
});

// ---- limits ----

test('checkLimits: duration ceiling 7200 + 60 tolerance', () => {
  assert.deepEqual(checkLimits({ kind: 'direct_url', duration_sec: 7200 }), { ok: true });
  assert.deepEqual(checkLimits({ kind: 'direct_url', duration_sec: 7260 }), { ok: true });
  assert.deepEqual(checkLimits({ kind: 'direct_url', duration_sec: 7261 }), { ok: false, error: 'too_long' });
});

test('checkLimits: chat_file <= 20 MiB, link <= 4 GiB', () => {
  assert.equal(checkLimits({ kind: 'chat_file', size_bytes: 20 * 1024 * 1024 }).ok, true);
  assert.deepEqual(checkLimits({ kind: 'chat_file', size_bytes: 20 * 1024 * 1024 + 1 }), { ok: false, error: 'too_large' });
  assert.equal(checkLimits({ kind: 'gdrive_public', size_bytes: 4 * 1024 ** 3 }).ok, true);
  assert.deepEqual(checkLimits({ kind: 'gdrive_public', size_bytes: 4 * 1024 ** 3 + 1 }), { ok: false, error: 'too_large' });
  // 100 MiB is fine for a link but not for a chat file
  assert.equal(checkLimits({ kind: 'yandex_public', size_bytes: 100 * 1024 * 1024 }).ok, true);
  assert.equal(checkLimits({ kind: 'chat_file', size_bytes: 100 * 1024 * 1024 }).ok, false);
});

test('checkLimits: unknown / invalid values pass (re-checked later)', () => {
  assert.equal(checkLimits({ kind: 'direct_url' }).ok, true);
  assert.equal(checkLimits({ kind: 'direct_url', size_bytes: null, duration_sec: null }).ok, true);
  assert.equal(checkLimits({ kind: 'direct_url', duration_sec: NaN }).ok, true);
  assert.equal(checkLimits({ kind: 'direct_url', size_bytes: -1 }).ok, true);
  assert.equal(checkLimits().ok, true);
});

test('checkLimits: too_large is reported before too_long when both exceed', () => {
  assert.equal(checkLimits({ kind: 'chat_file', size_bytes: 1e12, duration_sec: 1e6 }).error, 'too_large');
});

test('checkLimits: text 2000..250000 chars', () => {
  assert.deepEqual(checkLimits({ kind: 'text', text_chars: 1999 }), { ok: false, error: 'text_too_short' });
  assert.equal(checkLimits({ kind: 'text', text_chars: 2000 }).ok, true);
  assert.equal(checkLimits({ kind: 'text', text_chars: 250000 }).ok, true);
  assert.deepEqual(checkLimits({ kind: 'text', text_chars: 250001 }), { ok: false, error: 'too_large' });
  assert.equal(checkLimits({ kind: 'text' }).error, 'text_too_short');
});
