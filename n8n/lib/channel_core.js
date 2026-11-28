'use strict';

/**
 * Channel-agnostic message types for lecture intake (step 2, §4.1).
 * Pure functions — no n8n, no DB, no network.
 *
 * IncomingEvent  — what any adapter (Telegram, MAX, web, app) hands to the core.
 * OutgoingMessage — what the core hands back; plain text, adapters escape/format.
 *
 * Error messages never contain field values (they may hold URLs or ids).
 */

const { LIMITS } = require('./source_links');

const CHANNEL_RE = /^[a-z][a-z0-9_]{1,15}$/;
const COMMAND_RE = /^[a-z][a-z0-9_]*$/;
const EVENT_KINDS = ['command', 'text', 'button', 'file'];
const FILE_KINDS = ['audio', 'video', 'document'];
const EXPECT_VALUES = ['text', 'file', 'link', null];

function bad(field) {
  const err = new Error(`invalid_event: ${field}`);
  err.code = 'invalid_event';
  err.field = field;
  return err;
}

function badOut(field) {
  const err = new Error(`invalid_outgoing: ${field}`);
  err.code = 'invalid_outgoing';
  err.field = field;
  return err;
}

function utf8Bytes(str) {
  return Buffer.byteLength(String(str), 'utf8');
}

function codePointLength(str) {
  return Array.from(String(str)).length;
}

function truncateCodePoints(str, max) {
  const arr = Array.from(String(str));
  return arr.length > max ? arr.slice(0, max).join('') : String(str);
}

function cleanLine(str, max) {
  // eslint-disable-next-line no-control-regex
  const s = String(str).replace(/[\x00-\x1f\x7f]+/g, ' ').trim();
  return truncateCodePoints(s, max);
}

function idString(value, field, max) {
  if (typeof value === 'number' && Number.isFinite(value)) value = String(value);
  if (typeof value !== 'string') throw bad(field);
  const s = value.trim();
  if (!s || s.length > max) throw bad(field);
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(s)) throw bad(field);
  return s;
}

function normalizeFile(raw) {
  if (!raw || typeof raw !== 'object') throw bad('file');
  const ref = idString(raw.ref, 'file.ref', LIMITS.file_ref_max);
  if (!FILE_KINDS.includes(raw.kind)) throw bad('file.kind');
  const file = { ref, kind: raw.kind };
  if (typeof raw.name === 'string' && raw.name.trim()) file.name = cleanLine(raw.name, LIMITS.file_name_max);
  if (typeof raw.mime === 'string' && raw.mime.trim()) {
    file.mime = cleanLine(raw.mime, LIMITS.mime_max).toLowerCase();
  }
  if (raw.size_bytes != null) {
    const n = Number(raw.size_bytes);
    if (!Number.isFinite(n) || n < 0) throw bad('file.size_bytes');
    file.size_bytes = Math.floor(n);
  }
  return file;
}

/**
 * Validate and trim a raw adapter event. Throws Error{code:'invalid_event'}.
 * @param {object} raw
 * @returns {object} IncomingEvent
 */
function normalizeIncoming(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw bad('event');
  if (typeof raw.channel !== 'string' || !CHANNEL_RE.test(raw.channel)) throw bad('channel');
  if (!EVENT_KINDS.includes(raw.kind)) throw bad('kind');

  const ev = {
    channel: raw.channel,
    external_user_id: idString(raw.external_user_id, 'external_user_id', LIMITS.external_id_max),
    external_chat_id: idString(raw.external_chat_id, 'external_chat_id', LIMITS.external_id_max),
    event_id: idString(raw.event_id, 'event_id', LIMITS.event_id_max),
    kind: raw.kind,
  };

  if (raw.kind === 'command') {
    if (typeof raw.command !== 'string') throw bad('command');
    // "/new@SomeBot" -> "new"
    const cmd = raw.command.trim().replace(/^\//, '').split('@')[0].toLowerCase();
    if (!cmd || cmd.length > LIMITS.command_max || !COMMAND_RE.test(cmd)) throw bad('command');
    ev.command = cmd;
  }

  if (raw.kind === 'text') {
    if (typeof raw.text !== 'string') throw bad('text');
    // keep one char over the lecture-text ceiling so "too large" stays detectable
    ev.text = truncateCodePoints(raw.text, LIMITS.text_max_chars + 1);
  } else if (typeof raw.text === 'string' && raw.text.trim()) {
    // caption on a file, args after a command
    ev.text = truncateCodePoints(raw.text, LIMITS.text_max_chars + 1);
  }

  if (raw.kind === 'button') {
    if (typeof raw.button_id !== 'string' || !raw.button_id) throw bad('button_id');
    if (utf8Bytes(raw.button_id) > LIMITS.button_id_max_bytes) throw bad('button_id');
    ev.button_id = raw.button_id;
  }

  if (raw.kind === 'file') ev.file = normalizeFile(raw.file);

  if (typeof raw.display_name === 'string' && raw.display_name.trim()) {
    ev.display_name = cleanLine(raw.display_name, LIMITS.display_name_max);
  }
  return ev;
}

function validateButton(btn, seen) {
  if (!btn || typeof btn !== 'object') throw badOut('button');
  if (typeof btn.label !== 'string' || !btn.label.trim()) throw badOut('button.label');
  if (codePointLength(btn.label) > LIMITS.button_label_max) throw badOut('button.label');
  if (typeof btn.id !== 'string' || !btn.id) throw badOut('button.id');
  if (utf8Bytes(btn.id) > LIMITS.button_id_max_bytes) throw badOut('button.id');
  if (seen.has(btn.id)) throw badOut('button.id_duplicate');
  seen.add(btn.id);
}

/**
 * Check an OutgoingMessage against channel-agnostic limits. Throws on violation,
 * returns the message unchanged when it is valid.
 * @param {object} msg
 * @param {{max_text?: number}} [opts] max_text defaults to the Telegram limit
 */
function validateOutgoing(msg, opts = {}) {
  if (!msg || typeof msg !== 'object') throw badOut('message');
  const to = msg.to;
  if (!to || typeof to !== 'object') throw badOut('to');
  if (typeof to.channel !== 'string' || !CHANNEL_RE.test(to.channel)) throw badOut('to.channel');
  if (typeof to.external_chat_id !== 'string' || !to.external_chat_id) throw badOut('to.external_chat_id');
  if (typeof msg.text !== 'string' || !msg.text.trim()) throw badOut('text');
  const maxText = opts.max_text == null ? LIMITS.telegram_text_limit : opts.max_text;
  if (msg.text.length > maxText) throw badOut('text_too_long');
  if (msg.expect !== undefined && !EXPECT_VALUES.includes(msg.expect)) throw badOut('expect');

  if (msg.buttons !== undefined && msg.buttons !== null) {
    if (!Array.isArray(msg.buttons)) throw badOut('buttons');
    if (msg.buttons.length > LIMITS.max_button_rows) throw badOut('buttons.rows');
    const seen = new Set();
    for (const row of msg.buttons) {
      if (!Array.isArray(row) || !row.length) throw badOut('buttons.row');
      if (row.length > LIMITS.max_buttons_per_row) throw badOut('buttons.per_row');
      for (const btn of row) validateButton(btn, seen);
    }
  }
  return msg;
}

/**
 * Split text into chunks of at most `limit` UTF-16 units (Telegram counts that
 * way), preferring paragraph, then line, then word boundaries. Never splits a
 * surrogate pair. Whitespace at chunk edges is trimmed.
 * @param {string} text
 * @param {number} [limit]
 * @returns {string[]}
 */
function splitText(text, limit = LIMITS.telegram_text_limit) {
  const src = text == null ? '' : String(text);
  const max = Math.max(1, Math.floor(Number(limit) || LIMITS.telegram_text_limit));
  if (src.length <= max) return src.trim() ? [src] : [];
  const out = [];
  let rest = src;
  while (rest.length > max) {
    let cut = -1;
    const window = rest.slice(0, max + 1);
    for (const sep of ['\n\n', '\n', ' ']) {
      const idx = window.lastIndexOf(sep);
      if (idx > 0) {
        cut = idx;
        break;
      }
    }
    if (cut <= 0) {
      cut = max;
      const code = rest.charCodeAt(cut - 1);
      if (code >= 0xd800 && code <= 0xdbff && cut > 1) cut -= 1; // keep surrogate pair whole
    }
    const piece = rest.slice(0, cut).trim();
    if (piece) out.push(piece);
    rest = rest.slice(cut).replace(/^\s+/, '');
  }
  if (rest.trim()) out.push(rest);
  return out;
}

module.exports = {
  CHANNEL_RE,
  EVENT_KINDS,
  FILE_KINDS,
  utf8Bytes,
  codePointLength,
  truncateCodePoints,
  normalizeIncoming,
  validateOutgoing,
  splitText,
};
