'use strict';

/**
 * Lecture source links: classification, canonicalisation, limits (step 2).
 * Pure functions — string in, value out. No network, no DB, no n8n.
 *
 * The sha256 of `canonical` is computed by the DB (sha256(convert_to(...))),
 * this module only normalises. URLs are never echoed in error values:
 * results carry machine codes (`error`) only.
 */

const MiB = 1024 * 1024;
const GiB = 1024 * MiB;

/** Every magic number of the lecture-intake feature lives here. */
const LIMITS = Object.freeze({
  // quota / throttling
  default_quota: 2,
  daily_cap: 10,
  max_failures_24h: 5,
  monthly_budget_usd: 5,
  stop_ratio: 0.8,
  request_timeout_hours: 8,
  // media
  max_duration_sec: 7200,
  duration_tolerance_sec: 60,
  chat_file_max_bytes: 20 * MiB,
  link_max_bytes: 4 * GiB,
  // text lecture
  text_min_chars: 2000,
  text_max_chars: 250000,
  // dialog
  session_ttl_min: 30,
  course_title_min: 2,
  course_title_max: 80,
  course_page_size: 4,
  // links
  url_max_length: 2048,
  max_urls_considered: 10,
  // channel-agnostic message limits
  button_id_max_bytes: 48,
  button_label_max: 40,
  max_button_rows: 6,
  max_buttons_per_row: 2,
  telegram_text_limit: 4096,
  event_id_max: 128,
  external_id_max: 128,
  file_ref_max: 512,
  file_name_max: 255,
  mime_max: 127,
  display_name_max: 64,
  command_max: 32,
});

const ERR = Object.freeze({
  UNSUPPORTED_LINK: 'unsupported_link',
  TOO_LARGE: 'too_large',
  TOO_LONG: 'too_long',
  TEXT_TOO_SHORT: 'text_too_short',
});

const TRACKING_PARAMS = new Set([
  'fbclid',
  'gclid',
  'dclid',
  'yclid',
  'ysclid',
  'msclkid',
  'igshid',
  'mc_cid',
  'mc_eid',
  '_openstat',
  'utm',
  'ref_src',
]);

const YANDEX_HOSTS = new Set(['disk.yandex.ru', 'disk.yandex.com', 'yadi.sk']);
const GDRIVE_HOST = 'drive.google.com';
const GOOGLE_DOCS_HOST_RE = /(^|\.)(docs|sheets|slides|forms)\.google\.com$/;
const YANDEX_KEY_RE = /^[A-Za-z0-9_-]{6,128}$/;
const GDRIVE_ID_RE = /^[A-Za-z0-9_-]{10,200}$/;
const IPV4_RE = /^\d{1,3}(\.\d{1,3}){3}$/;
const INTERNAL_SUFFIXES = [
  '.local',
  '.localhost',
  '.internal',
  '.localdomain',
  '.lan',
  '.home',
  '.corp',
  '.intranet',
  '.home.arpa',
];

function fail(error) {
  return { kind: null, canonical: null, error };
}

function isTrackingParam(name) {
  const n = String(name).toLowerCase();
  return n.startsWith('utm_') || TRACKING_PARAMS.has(n);
}

/**
 * Pull http(s)-looking links (and a few well-known bare hosts) out of free text.
 * Trailing punctuation is trimmed. Order of appearance is preserved.
 * @param {string} text
 * @returns {string[]}
 */
function extractUrls(text) {
  const src = typeof text === 'string' ? text : '';
  const out = [];
  const re =
    /(?:[a-zA-Z][a-zA-Z0-9+.-]{1,15}:\/\/|(?:disk\.yandex\.(?:ru|com)|yadi\.sk|drive\.google\.com)\/)[^\s<>"'«»]+/g;
  let m = re.exec(src);
  while (m && out.length < LIMITS.max_urls_considered) {
    let url = m[0].replace(/[.,;:!?)\]}]+$/, '');
    if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url)) url = `https://${url}`;
    if (url) out.push(url);
    m = re.exec(src);
  }
  return out;
}

function hasNonAscii(str) {
  // eslint-disable-next-line no-control-regex
  return /[^\x00-\x7f]/.test(str);
}

function hasControlOrSpace(str) {
  // eslint-disable-next-line no-control-regex
  return /[\x00-\x20\x7f]/.test(str);
}

function isIpLiteralHost(hostname) {
  if (hostname.startsWith('[') || hostname.includes(':')) return true;
  if (IPV4_RE.test(hostname)) return true;
  // WHATWG normalises 0x7f.1 / 2130706433 to dotted quads, but be defensive:
  if (/^(0x[0-9a-f]+|\d+)$/i.test(hostname)) return true;
  const last = hostname.split('.').pop();
  if (/^(0x[0-9a-f]+|\d+)$/i.test(last)) return true; // numeric TLD is never a real host
  return false;
}

function isInternalHost(hostname) {
  if (hostname === 'localhost') return true;
  if (!hostname.includes('.')) return true; // single-label (intranet) host
  return INTERNAL_SUFFIXES.some((s) => hostname.endsWith(s));
}

function parseHttps(raw) {
  if (typeof raw !== 'string') return null;
  const s = raw.trim();
  if (!s || s.length > LIMITS.url_max_length || hasControlOrSpace(s)) return null;
  let u;
  try {
    u = new URL(s);
  } catch (_e) {
    return null;
  }
  if (u.protocol !== 'https:') return null;
  if (u.username || u.password) return null;
  if (u.port && u.port !== '443') return null;
  return u;
}

function canonicalQuery(u) {
  const kept = [];
  for (const [k, v] of u.searchParams.entries()) {
    if (!isTrackingParam(k)) kept.push([k, v]);
  }
  if (!kept.length) return '';
  const p = new URLSearchParams();
  for (const [k, v] of kept) p.append(k, v);
  return `?${p.toString()}`;
}

function classifyYandex(u) {
  const parts = u.pathname.split('/').filter(Boolean);
  if (parts.length !== 2 || !['i', 'd'].includes(parts[0])) return fail(ERR.UNSUPPORTED_LINK);
  if (!YANDEX_KEY_RE.test(parts[1])) return fail(ERR.UNSUPPORTED_LINK);
  const host = u.hostname === 'yadi.sk' ? 'yadi.sk' : u.hostname;
  return {
    kind: 'yandex_public',
    canonical: `https://${host}/${parts[0]}/${parts[1]}`,
    // /d/<key> may be a folder: the service probe decides, v1 refuses folders.
    maybe_folder: parts[0] === 'd',
  };
}

function classifyGdrive(u) {
  const parts = u.pathname.split('/').filter(Boolean);
  let id = null;
  if (parts[0] === 'file' && parts[1] === 'd' && parts.length >= 3) {
    id = parts[2];
  } else if (
    (parts.length === 1 && (parts[0] === 'open' || parts[0] === 'uc')) ||
    (parts.length === 3 && parts[0] === 'u' && /^\d{1,2}$/.test(parts[1]) && (parts[2] === 'open' || parts[2] === 'uc'))
  ) {
    id = u.searchParams.get('id');
  }
  if (!id || !GDRIVE_ID_RE.test(id)) return fail(ERR.UNSUPPORTED_LINK);
  return {
    kind: 'gdrive_public',
    canonical: `https://drive.google.com/file/d/${id}/view`,
  };
}

function classifyDirect(u) {
  const host = u.hostname.replace(/\.$/, '');
  if (!host || hasNonAscii(host) || host.includes('xn--')) return fail(ERR.UNSUPPORTED_LINK);
  if (isIpLiteralHost(host) || isInternalHost(host)) return fail(ERR.UNSUPPORTED_LINK);
  if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(host)) {
    return fail(ERR.UNSUPPORTED_LINK);
  }
  return {
    kind: 'direct_url',
    canonical: `https://${host}${u.pathname}${canonicalQuery(u)}`,
  };
}

/**
 * Classify a single URL string.
 * @param {string} url
 * @returns {{kind: ('yandex_public'|'gdrive_public'|'direct_url'|null), canonical: (string|null), error?: string, maybe_folder?: boolean}}
 */
function classifySource(url) {
  const raw = typeof url === 'string' ? url.trim() : '';
  // Reject non-https schemes (javascript:, file:, http:, ftp:, data:) and unicode hosts up front.
  const u = parseHttps(raw);
  if (!u) return fail(ERR.UNSUPPORTED_LINK);
  const host = u.hostname.replace(/\.$/, '');
  if (YANDEX_HOSTS.has(host)) return classifyYandex(u);
  if (host === GDRIVE_HOST) return classifyGdrive(u);
  if (GOOGLE_DOCS_HOST_RE.test(host)) return fail(ERR.UNSUPPORTED_LINK);
  return classifyDirect(u);
}

function finiteNumber(n) {
  return typeof n === 'number' && Number.isFinite(n) && n >= 0 ? n : null;
}

/**
 * Check size / duration against product limits. Unknown values pass here
 * (they are re-checked after probe / ffprobe).
 * @param {{size_bytes?: number, duration_sec?: number, kind?: string, text_chars?: number}} p
 * @returns {{ok: boolean, error?: string}}
 */
function checkLimits(p = {}) {
  const kind = p.kind;
  if (kind === 'text') {
    const chars = finiteNumber(p.text_chars != null ? p.text_chars : p.size_bytes);
    if (chars == null || chars < LIMITS.text_min_chars) return { ok: false, error: ERR.TEXT_TOO_SHORT };
    if (chars > LIMITS.text_max_chars) return { ok: false, error: ERR.TOO_LARGE };
    return { ok: true };
  }
  const size = finiteNumber(p.size_bytes);
  const maxBytes = kind === 'chat_file' ? LIMITS.chat_file_max_bytes : LIMITS.link_max_bytes;
  if (size != null && size > maxBytes) return { ok: false, error: ERR.TOO_LARGE };
  const dur = finiteNumber(p.duration_sec);
  if (dur != null && dur > LIMITS.max_duration_sec + LIMITS.duration_tolerance_sec) {
    return { ok: false, error: ERR.TOO_LONG };
  }
  return { ok: true };
}

module.exports = {
  LIMITS,
  ERR,
  extractUrls,
  classifySource,
  checkLimits,
  isTrackingParam,
};
