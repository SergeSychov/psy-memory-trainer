'use strict';

/**
 * Static checks for db/migrations/0008_lecture_sources_quota.sql (step 2, §5.8 / §6.4).
 * Reads the file only — no DB. If the file does not exist yet the file-level
 * tests are skipped locally and FAIL when CI=true (so CI cannot go green without it).
 * The checker itself is exercised on synthetic SQL, which always runs.
 */

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const FILE = path.join(__dirname, '..', '..', 'db', 'migrations', '0008_lecture_sources_quota.sql');

const REQUIRED_TABLES = ['user_identities', 'lecture_requests', 'quota_ledger', 'intake_sessions', 'outbox'];
const REQUIRED_FUNCTIONS = [
  'resolve_identity',
  'create_lecture_request',
  'confirm_lecture_request',
  'finalize_pending_requests',
  'cancel_lecture_request',
  'refund_lecture_request',
  'workspace_quota_remaining',
  'queue_position',
];

function stripComments(sql) {
  return sql.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/--[^\n]*/g, '');
}

function lineCount(code, re) {
  return code.split('\n').filter((l) => re.test(l.trim())).length;
}

/** Body of `CREATE TABLE IF NOT EXISTS psy.<name> ( ... );` (up to the closing `);`). */
function tableBody(code, name) {
  const m = new RegExp(`create\\s+table\\s+if\\s+not\\s+exists\\s+psy\\.${name}\\b([\\s\\S]*?)\\n\\s*\\);`, 'i').exec(code);
  return m ? m[1] : '';
}

/** @returns {string[]} list of human-readable violations (empty = clean) */
function analyze(sql) {
  const v = [];
  const code = stripComments(sql);
  const lower = code.toLowerCase();

  // --- transaction shape
  if (lineCount(code, /^begin;$/i) !== 1) v.push('expected exactly one BEGIN;');
  if (lineCount(code, /^commit;$/i) !== 1) v.push('expected exactly one COMMIT;');
  if (lineCount(code, /^rollback;?$/i) !== 0) v.push('ROLLBACK must not be in the file');
  const trimmed = code.trim().toLowerCase();
  if (!trimmed.startsWith('begin;')) v.push('BEGIN; must be the first statement');
  if (!trimmed.endsWith('commit;')) v.push('COMMIT; must be the last statement');

  // --- role / extension safety
  if (/\bextensions\s*\./i.test(code)) v.push('extensions.* is forbidden (psy_n8n has no USAGE)');
  if (/create\s+extension/i.test(code)) v.push('CREATE EXTENSION is forbidden');
  if (/\bset\s+role\b|\balter\s+role\b|\bcreate\s+role\b/i.test(code)) v.push('role changes are forbidden in the migration');

  // --- additive only
  if (/\bdrop\s+(table|schema|column|function|index|view|type)\b/i.test(code)) v.push('destructive DROP is forbidden');
  if (/\btruncate\b/i.test(code)) v.push('TRUNCATE is forbidden');
  if (/\bdelete\s+from\b/i.test(code)) v.push('DELETE FROM is forbidden');
  const dropConstraints = code.match(/drop\s+constraint\s+(if\s+exists\s+)?[\w."]+/gi) || [];
  for (const d of dropConstraints) {
    if (!/lectures_media_kind_check/i.test(d)) v.push(`unexpected DROP CONSTRAINT: ${d.replace(/\s+/g, ' ')}`);
  }

  // --- idempotent DDL
  for (const m of code.matchAll(/create\s+table\s+(?!if\s+not\s+exists)([\w."]+)/gi)) v.push(`CREATE TABLE without IF NOT EXISTS: ${m[1]}`);
  for (const m of code.matchAll(/create\s+(?:unique\s+)?index\s+(?!if\s+not\s+exists)([\w."]+)/gi)) v.push(`CREATE INDEX without IF NOT EXISTS: ${m[1]}`);
  for (const m of code.matchAll(/add\s+column\s+(?!if\s+not\s+exists)([\w."]+)/gi)) v.push(`ADD COLUMN without IF NOT EXISTS: ${m[1]}`);
  for (const m of code.matchAll(/create\s+(?:or\s+replace\s+)?(?:constraint\s+)?trigger\s+([\w."]+)/gi)) {
    const name = m[1].replace(/^psy\./, '');
    if (!new RegExp(`drop\\s+trigger\\s+if\\s+exists\\s+${name.replace(/\./g, '\\.')}\\b`, 'i').test(code)) {
      v.push(`CREATE TRIGGER needs a preceding DROP TRIGGER IF EXISTS: ${m[1]}`);
    }
  }

  // --- required objects
  for (const t of REQUIRED_TABLES) {
    if (!new RegExp(`create\\s+table\\s+if\\s+not\\s+exists\\s+psy\\.${t}\\b`, 'i').test(code)) v.push(`missing table psy.${t}`);
  }
  for (const f of REQUIRED_FUNCTIONS) {
    if (!new RegExp(`create\\s+or\\s+replace\\s+function\\s+psy\\.${f}\\s*\\(`, 'i').test(code)) v.push(`missing function psy.${f}`);
    if (!new RegExp(`grant\\s+execute\\s+on\\s+function\\s+psy\\.${f}\\b`, 'i').test(code)) v.push(`missing GRANT EXECUTE for psy.${f}`);
  }

  // --- decisions of §5
  if (!/alter\s+table\s+psy\.users\s+alter\s+column\s+telegram_user_id\s+drop\s+not\s+null/i.test(code)) v.push('users.telegram_user_id must drop NOT NULL');
  if (!/lectures_media_kind_check/i.test(code) || !/'video'\s*,\s*'audio'\s*,\s*'text'/i.test(code)) v.push("lectures.media_kind must allow 'text'");
  if (!/unique\s*\(\s*channel\s*,\s*external_id\s*\)/i.test(code)) v.push('user_identities needs UNIQUE (channel, external_id)');
  if (!/channel\s*~\s*'\^\[a-z\]\[a-z0-9_\]\{1,15\}\$'/i.test(code)) v.push('user_identities.channel needs the pattern CHECK');
  if (!/unique\s*\(\s*request_id\s*,\s*reason\s*\)/i.test(code)) v.push('quota_ledger needs UNIQUE (request_id, reason)');
  if (!/dedupe_key\s+text[^,]*unique|unique\s*\(\s*dedupe_key\s*\)/i.test(code)) v.push('outbox.dedupe_key must be UNIQUE');
  const intake = tableBody(code, 'intake_sessions');
  if (!/unique\s*\(\s*user_id\s*\)|user_id\s+uuid[^,]*\bunique\b/i.test(intake) && !/create\s+unique\s+index[^;]*intake_sessions\s*\(\s*user_id\s*\)/i.test(code)) {
    v.push('intake_sessions needs UNIQUE (user_id)');
  }
  const partial = (re) => re.test(code);
  if (!partial(/unique\s+index[^;]*lecture_requests\s*\(\s*user_id\s*\)\s*where\s+status\s+in\s*\(\s*'queued'\s*,\s*'processing'\s*\)/i)) {
    v.push('one active request per user: partial UNIQUE (user_id) WHERE status IN (queued, processing)');
  }
  if (!partial(/unique\s+index[^;]*lecture_requests\s*\(\s*user_id\s*\)\s*where\s+status\s*=\s*'confirming'/i)) {
    v.push('one confirming request per user: partial UNIQUE (user_id) WHERE status = confirming');
  }
  if (!partial(/unique\s+index[^;]*lecture_requests\s*\(\s*workspace_id\s*,\s*source_hash\s*\)\s*where\s+status\s+in\s*\(\s*'queued'\s*,\s*'processing'\s*,\s*'done'\s*\)/i)) {
    v.push('dedupe: partial UNIQUE (workspace_id, source_hash) WHERE status IN (queued, processing, done)');
  }
  for (const kind of ['yandex_public', 'gdrive_public', 'direct_url', 'chat_file', 'text']) {
    if (!code.includes(`'${kind}'`)) v.push(`source_kind '${kind}' missing`);
  }
  for (const st of ['confirming', 'queued', 'processing', 'done', 'failed', 'cancelled', 'expired']) {
    if (!code.includes(`'${st}'`)) v.push(`request status '${st}' missing`);
  }
  for (const s of ['pick_course', 'new_course_title', 'await_source', 'confirm']) {
    if (!code.includes(`'${s}'`)) v.push(`intake step '${s}' missing`);
  }
  for (const k of ['request_done', 'request_failed', 'request_expired']) {
    if (!code.includes(`'${k}'`)) v.push(`outbox kind '${k}' missing`);
  }
  if (!/on\s+conflict/i.test(code)) v.push('ON CONFLICT is required (idempotent inserts)');
  if (!/insert\s+into\s+psy\.quota_ledger[\s\S]{0,400}?on\s+conflict/i.test(code)) v.push('quota_ledger inserts must use ON CONFLICT (idempotent charge/refund)');
  if (!/'lecture_done'/.test(code) || !/'refund'/.test(code)) v.push("ledger reasons 'lecture_done' and 'refund' required");
  if (!/for\s+update/i.test(code)) v.push('confirm must serialise with SELECT ... FOR UPDATE');
  if (!/gen_random_uuid\s*\(/i.test(code)) v.push('use built-in gen_random_uuid()');

  // --- grants
  if (!/pg_roles/i.test(code) || !/psy_n8n/.test(code)) v.push('GRANT block for psy_n8n (guarded by pg_roles) is required');
  if (!/grant\s+[\w\s,]*(select|insert)[\w\s,]*on\s+[\s\S]{0,200}?to\s+psy_n8n/i.test(code) && !/grant[\s\S]{0,300}psy_n8n/i.test(code)) {
    v.push('table grants to psy_n8n missing');
  }

  // --- nothing sensitive in a public file
  const sensitive = [
    [/https?:\/\//i, 'URL'],
    [/[\w.+-]+@[\w-]+\.[\w.-]+/, 'e-mail address'],
    [/\b(?!4294967296\b)\d{9,}\b/, 'long numeric id (telegram id?)'], // 4294967296 = 4 GiB limit
    [/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i, 'uuid literal'],
    [/password|passwd|secret|bearer|api[_-]?key|webhook|credential|private[_ ]key/i, 'secret-like word'],
    [/\btoken\b/i, "word 'token'"],
    [/[Сс]ергей|[Нн]аталь[яи]/, 'person name'],
    [/whisper_token|telegram_bot|deepseek_api/i, 'env var name with secret'],
  ];
  // look at raw SQL (comments included): names/ids must not hide in comments either
  for (const [re, label] of sensitive) {
    if (re.test(sql)) v.push(`sensitive content: ${label}`);
  }

  void lower;
  return v;
}

// ------------------------------------------------------------ checker self-tests

const GOOD_SQL = (() => {
  const fns = REQUIRED_FUNCTIONS.map((f) => `CREATE OR REPLACE FUNCTION psy.${f}() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\nGRANT EXECUTE ON FUNCTION psy.${f}() TO psy_n8n;`).join('\n');
  return `-- apply once: psql -v ON_ERROR_STOP=1 -f file
BEGIN;
ALTER TABLE psy.users ALTER COLUMN telegram_user_id DROP NOT NULL;
CREATE TABLE IF NOT EXISTS psy.user_identities (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), channel text NOT NULL CHECK (channel ~ '^[a-z][a-z0-9_]{1,15}$'), external_id text NOT NULL, UNIQUE (channel, external_id));
ALTER TABLE psy.lectures DROP CONSTRAINT IF EXISTS lectures_media_kind_check;
ALTER TABLE psy.lectures ADD CONSTRAINT lectures_media_kind_check CHECK (media_kind IN ('video','audio','text'));
CREATE TABLE IF NOT EXISTS psy.lecture_requests (id uuid, user_id uuid, workspace_id uuid, source_hash text,
  source_kind text CHECK (source_kind IN ('yandex_public','gdrive_public','direct_url','chat_file','text')),
  status text CHECK (status IN ('confirming','queued','processing','done','failed','cancelled','expired')));
CREATE UNIQUE INDEX IF NOT EXISTS lr_active ON psy.lecture_requests (user_id) WHERE status IN ('queued','processing');
CREATE UNIQUE INDEX IF NOT EXISTS lr_confirming ON psy.lecture_requests (user_id) WHERE status = 'confirming';
CREATE UNIQUE INDEX IF NOT EXISTS lr_dedupe ON psy.lecture_requests (workspace_id, source_hash) WHERE status IN ('queued','processing','done');
CREATE TABLE IF NOT EXISTS psy.quota_ledger (id uuid, request_id uuid, reason text, UNIQUE (request_id, reason));
CREATE TABLE IF NOT EXISTS psy.intake_sessions (
  id uuid,
  user_id uuid NOT NULL,
  step text CHECK (step IN ('pick_course','new_course_title','await_source','confirm')),
  CONSTRAINT intake_sessions_user_id_key UNIQUE (user_id)
);
CREATE TABLE IF NOT EXISTS psy.outbox (id uuid, kind text CHECK (kind IN ('request_done','request_failed','request_expired')), dedupe_key text UNIQUE);
CREATE FUNCTION psy.noop() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
DROP TRIGGER IF EXISTS intake_sessions_set_updated_at ON psy.intake_sessions;
CREATE TRIGGER intake_sessions_set_updated_at BEFORE UPDATE ON psy.intake_sessions FOR EACH ROW EXECUTE FUNCTION psy.noop();
${fns}
INSERT INTO psy.quota_ledger (request_id, reason) VALUES (NULL, 'lecture_done') ON CONFLICT DO NOTHING;
INSERT INTO psy.quota_ledger (request_id, reason) VALUES (NULL, 'refund') ON CONFLICT DO NOTHING;
SELECT 1 FROM psy.workspaces FOR UPDATE;
DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA psy TO psy_n8n; END IF; END $$;
COMMIT;
`;
})();

test('checker: synthetic good migration has no violations', () => {
  assert.deepEqual(analyze(GOOD_SQL), []);
});

test('checker: catches extensions.*, missing COMMIT, extra BEGIN, CREATE TABLE without IF NOT EXISTS', () => {
  assert.ok(analyze(GOOD_SQL.replace('gen_random_uuid()', 'extensions.gen_random_uuid()')).some((x) => /extensions/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace(/COMMIT;\n$/, '')).some((x) => /COMMIT/.test(x)));
  assert.ok(analyze(`BEGIN;\n${GOOD_SQL}`).some((x) => /exactly one BEGIN/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace('CREATE TABLE IF NOT EXISTS psy.outbox', 'CREATE TABLE psy.outbox')).length > 0);
});

test('checker: catches destructive statements and missing idempotency guards', () => {
  assert.ok(analyze(GOOD_SQL.replace('COMMIT;', 'DROP TABLE psy.users;\nCOMMIT;')).some((x) => /DROP/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace('COMMIT;', 'TRUNCATE psy.users;\nCOMMIT;')).some((x) => /TRUNCATE/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace('COMMIT;', 'DELETE FROM psy.users;\nCOMMIT;')).some((x) => /DELETE/.test(x)));
  assert.ok(analyze(GOOD_SQL.replaceAll('ON CONFLICT DO NOTHING', '')).length > 0);
  assert.ok(analyze(GOOD_SQL.replace('CREATE UNIQUE INDEX IF NOT EXISTS lr_active', 'CREATE UNIQUE INDEX lr_active')).some((x) => /INDEX/.test(x)));
});

test('checker: catches sensitive content (urls, ids, names, secrets, uuids)', () => {
  const add = (s) => GOOD_SQL.replace('COMMIT;', `-- ${s}\nCOMMIT;`);
  for (const bad of ['see https://example.com', 'id 123456789012', 'user@example.com', 'Сергей', 'Наталья', 'password', 'Bearer abc', 'token', '123e4567-e89b-12d3-a456-426614174000', 'webhook']) {
    assert.ok(analyze(add(bad)).some((x) => /sensitive/.test(x)), bad);
  }
});

test('checker: catches missing required objects', () => {
  assert.ok(analyze(GOOD_SQL.replace('CONSTRAINT intake_sessions_user_id_key UNIQUE (user_id)', 'x int')).some((x) => /intake_sessions/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace('DROP TRIGGER IF EXISTS intake_sessions_set_updated_at ON psy.intake_sessions;', '')).some((x) => /TRIGGER/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace(/psy\.queue_position/g, 'psy.queue_pos')).some((x) => /queue_position/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace('UNIQUE (request_id, reason)', '')).some((x) => /quota_ledger/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace("WHERE status IN ('queued','processing');", ';')).some((x) => /one active request/.test(x)));
  assert.ok(analyze(GOOD_SQL.replace(/'text'\)\);\nCREATE TABLE IF NOT EXISTS psy\.lecture_requests/, "'x'));\nCREATE TABLE IF NOT EXISTS psy.lecture_requests")).length > 0);
});

// ------------------------------------------------------------ the real file

const exists = fs.existsSync(FILE);
const missingMode = process.env.CI ? false : 'db/migrations/0008_lecture_sources_quota.sql not created yet';

test('0008 file exists (required in CI)', { skip: exists || !missingMode ? false : missingMode }, () => {
  assert.ok(exists, 'db/migrations/0008_lecture_sources_quota.sql is missing');
});

test('0008: static checks (transaction, extensions, additive, objects, no sensitive data)', { skip: exists ? false : missingMode }, () => {
  const sql = fs.readFileSync(FILE, 'utf8');
  const violations = analyze(sql);
  assert.deepEqual(violations, [], `\n- ${violations.join('\n- ')}`);
});

test('0008: header tells how to apply it', { skip: exists ? false : missingMode }, () => {
  const sql = fs.readFileSync(FILE, 'utf8');
  const head = sql.split('\n').slice(0, 40).join('\n');
  assert.match(head, /ON_ERROR_STOP=1/);
  assert.match(head, /^\s*--/m);
});
