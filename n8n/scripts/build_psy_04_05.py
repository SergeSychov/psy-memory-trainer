#!/usr/bin/env python3
"""Build and deploy inactive psy_04_konspekt / psy_05_terms_questions and patch psy_03_transcribe.

Order: psy_05 (no kick) -> psy_04 (kicks psy_05) -> psy_03 v2 (enqueues konspekt, kicks psy_04).
System prompts are read from prompts/*.md at build time; validate_questions.js is embedded
inline into the psy_05 Code nodes (n8n Code cannot require() project files).
No secrets in output (credentials are {id, name} references only).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from _env import WORKFLOWS, api_request  # noqa: F401  (api_request re-exported for parity with build_psy_01_03)
from build_psy_01_03 import (  # reuse helpers / patterns
    PG,
    build_psy_03,
    code,
    ensure_workflow,
    http,
    iff,
    link,
    nid,
    pg,
    save_local,
    sticky,
)

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "prompts"
LIB = REPO / "n8n" / "lib"

DEEPSEEK = {"deepSeekApi": {"id": "xyaJwQg88odvtCfC", "name": "DeepSeek account"}}
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

WF_VER = {
    "psy_04": "psy_04_konspekt_v1",
    "psy_05": "psy_05_terms_questions_v7",
}

# psy_03 lives in n8n already; id only used as sanity check / fallback when .id is missing.
PSY_03_ID = "tRGq7gzjUbO52oq1"


# --------------------------------------------------------------------------------------
# build-time embeds
# --------------------------------------------------------------------------------------
def load_system_prompt(filename: str) -> str:
    text = (PROMPTS / filename).read_text(encoding="utf-8")
    start = text.index("## Текст system prompt")
    start = text.index("\n", start) + 1
    end = text.index("## Конец system prompt")
    return text[start:end].strip()


def js_literal(value: str) -> str:
    """JSON string is a valid JS string literal."""
    return json.dumps(value, ensure_ascii=False)


def load_validator_js() -> str:
    src = (LIB / "validate_questions.js").read_text(encoding="utf-8")
    src = src.replace("'use strict';", "")
    src = re.sub(r"module\.exports\s*=\s*\{[\s\S]*?\};?", "", src)
    return src.strip()


def load_subtopic_questions_js() -> str:
    src = (LIB / "subtopic_questions.js").read_text(encoding="utf-8")
    src = src.replace("'use strict';", "")
    src = re.sub(r"module\.exports\s*=\s*\{[\s\S]*?\};?", "", src)
    return src.strip()


# --------------------------------------------------------------------------------------
# node factories
# --------------------------------------------------------------------------------------
def exec_trigger(x: int, y: int) -> dict:
    return {
        "id": nid(),
        "name": "In — Execute",
        "type": "n8n-nodes-base.executeWorkflowTrigger",
        "typeVersion": 1.1,
        "position": [x, y],
        "parameters": {"inputSource": "passthrough"},
    }


def manual_trigger(x: int, y: int) -> dict:
    return {
        "id": nid(),
        "name": "In — Manual",
        "type": "n8n-nodes-base.manualTrigger",
        "typeVersion": 1,
        "position": [x, y],
        "parameters": {},
    }


def kick(name: str, workflow_id: str, x: int, y: int) -> dict:
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.executeWorkflow",
        "typeVersion": 1.2,
        "position": [x, y],
        "parameters": {
            "workflowId": {"__rl": True, "mode": "id", "value": workflow_id},
            "options": {"waitForSubWorkflow": False},
        },
        "onError": "continueRegularOutput",
    }


def deepseek(name: str, x: int, y: int) -> dict:
    """The only way DeepSeek is called: predefined credential deepSeekApi, body from $json.request_body."""
    return http(
        name,
        method="POST",
        url=DEEPSEEK_URL,
        x=x,
        y=y,
        body="={{ JSON.stringify($json.request_body) }}",
        auth="predefinedCredentialType",
        creds=DEEPSEEK,
        node_cred_type="deepSeekApi",
        timeout_ms=900000,
        on_error="continueErrorOutput",
    )


def splitter(name: str, x: int, y: int) -> dict:
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.splitInBatches",
        "typeVersion": 3,
        "position": [x, y],
        "parameters": {"batchSize": 1, "options": {}},
    }


def log_usage(name: str, x: int, y: int) -> dict:
    return pg(
        name,
        """INSERT INTO psy.llm_usage (
  lecture_id, workflow_name, model, prompt_tokens, completion_tokens, cost_usd,
  prompt_version, workflow_version, job_id, prompt_cache_hit_tokens, step
) VALUES (
  $1::uuid, $2, $3, $4::int, $5::int, $6::numeric,
  $7, $8, $9::uuid, $10::int, $11
)
RETURNING id""",
        x,
        y,
        """={{ [
  $json.usage_row.lecture_id,
  $json.usage_row.workflow_name,
  $json.usage_row.model,
  $json.usage_row.prompt_tokens,
  $json.usage_row.completion_tokens,
  $json.usage_row.cost_usd,
  $json.usage_row.prompt_version,
  $json.usage_row.workflow_version,
  $json.usage_row.job_id,
  $json.usage_row.prompt_cache_hit_tokens,
  $json.usage_row.step
] }}""",
    )


def claim_sql(step: str) -> str:
    return f"""UPDATE psy.jobs j
SET status = 'running',
    attempts = attempts + 1,
    started_at = now(),
    error = NULL,
    payload = COALESCE(payload, '{{}}'::jsonb) || jsonb_build_object('workflow_version', $1, 'claimed_at', now())
WHERE j.id = (
  SELECT id FROM psy.jobs
  WHERE step = '{step}'
    AND attempts < 3
    AND (
      status = 'queued'
      OR (status = 'running' AND started_at < now() - interval '4 hours')
    )
  ORDER BY created_at
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
RETURNING j.*"""


def backfill_sql(lecture_status: str, step: str) -> str:
    return f"""WITH ins AS (
  INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
  SELECT l.id, '{step}', 'queued', '{step}:' || l.id::text,
         jsonb_build_object('workflow_version', $1::text, 'source', 'backfill')
  FROM psy.lectures l
  WHERE l.status = '{lecture_status}'
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
)
SELECT count(*)::int AS backfilled FROM ins"""


FAIL_JOB_SQL = """WITH j AS (
  UPDATE psy.jobs
  SET status = CASE
        WHEN $4::boolean THEN 'failed'
        WHEN $2::int = 409 THEN 'queued'
        WHEN attempts >= 3 THEN 'failed'
        ELSE 'queued'
      END,
      attempts = CASE
        WHEN $4::boolean THEN 3
        WHEN $2::int = 409 THEN GREATEST(attempts - 1, 0)
        ELSE attempts
      END,
      error = $3,
      finished_at = CASE
        WHEN $4::boolean OR ($2::int <> 409 AND attempts >= 3) THEN now()
        ELSE NULL
      END,
      started_at = CASE
        WHEN NOT $4::boolean AND ($2::int = 409 OR attempts < 3) THEN NULL
        ELSE started_at
      END
  WHERE id = $1::uuid
  RETURNING id, status, attempts, lecture_id
), lec AS (
  UPDATE psy.lectures
  SET status = 'failed', error = $3
  WHERE id = (SELECT lecture_id FROM j)
    AND (SELECT status FROM j) = 'failed'
    AND NOT $5::boolean
  RETURNING id
)
SELECT * FROM j"""

FAIL_JOB_PARAMS = """={{ [
  $json.job_id,
  Number($json.status_code || 0),
  $json.error_message,
  Boolean($json.permanent),
  Boolean($json.keep_lecture)
] }}"""


def fail_nodes(x: int, y: int, extra_cases_js: str) -> tuple[dict, dict]:
    """Fin — Fail Context + Fin — Fail Job (shared contract for psy_04 / psy_05)."""
    js = (
        r"""const job = $('Run — Create Job').first().json;
const e = (items[0] && items[0].json) || {};
const err = e.error && typeof e.error === 'object' ? e.error : {};
let permanent = false;
let keepLecture = false;
let message = null;
__EXTRA__
if (message === null) {
  message = e.error_message || err.message || (typeof e.error === 'string' ? e.error : null) || e.message
    || JSON.stringify(e).slice(0, 500);
}
message = String(message).slice(0, 800);
let statusCode = Number(e.statusCode ?? e.httpCode ?? err.httpCode ?? err.status ?? err.statusCode ?? 0);
if (!Number.isFinite(statusCode)) statusCode = 0;
if (!statusCode) {
  const m = message.match(/^(\d{3})\b/);
  if (m) statusCode = Number(m[1]);
}
return [{
  json: {
    job_id: job.id,
    lecture_id: job.lecture_id,
    attempts: job.attempts,
    status_code: statusCode,
    error_message: message,
    permanent,
    keep_lecture: keepLecture,
  },
  pairedItem: 0,
}];"""
    ).replace("__EXTRA__", extra_cases_js)
    ctx = code("Fin — Fail Context", js, x, y)
    fail = pg("Fin — Fail Job", FAIL_JOB_SQL, x + 220, y, FAIL_JOB_PARAMS)
    return ctx, fail


# Shared JS: DeepSeek response → content + usage (with cache hit/miss) + usage_row for llm_usage.
PARSE_DEEPSEEK_JS = r"""function parseDeepSeek(resp, C, src, step, promptVersion) {
  const r = resp || {};
  const choice = (r.choices && r.choices[0]) || {};
  const content = String((choice.message && choice.message.content) || '');
  const u = r.usage || {};
  const hit = Number(u.prompt_cache_hit_tokens != null
    ? u.prompt_cache_hit_tokens
    : ((u.prompt_tokens_details && u.prompt_tokens_details.cached_tokens) || 0)) || 0;
  let miss = u.prompt_cache_miss_tokens != null ? Number(u.prompt_cache_miss_tokens) : null;
  const promptTokens = Number(u.prompt_tokens != null ? u.prompt_tokens : ((miss || 0) + hit)) || 0;
  if (miss == null || !Number.isFinite(miss)) miss = Math.max(promptTokens - hit, 0);
  const completion = Number(u.completion_tokens) || 0;
  const P = C.prices_usd_per_1m;
  const costExact = (miss * P.miss + hit * P.hit + completion * P.out) / 1e6;
  return {
    content,
    finish_reason: choice.finish_reason || null,
    cost_exact: costExact,
    usage_row: {
      lecture_id: src.lecture_id,
      job_id: src.job_id,
      workflow_name: C.workflow_name,
      workflow_version: C.workflow_version,
      model: r.model || C.llm_model,
      prompt_tokens: promptTokens,
      completion_tokens: completion,
      cost_usd: Number(costExact.toFixed(6)),
      prompt_version: promptVersion,
      prompt_cache_hit_tokens: hit,
      step,
    },
  };
}"""


def sub_code(template: str, **repl: str) -> str:
    out = template
    for key, value in repl.items():
        out = out.replace(f"__{key}__", value)
    return out


# ======================================================================================
# psy_04_konspekt
# ======================================================================================
INIT_JS_04 = r"""const input = (items[0] && items[0].json) || {};
const constants = {
  workflow_version: '__WF__',
  workflow_name: 'psy_04_konspekt',
  llm_model: 'deepseek-flash',
  thinking: { type: 'disabled' },
  temperature: 0.3,
  max_output_tokens: 8000,
  // DeepSeek peak prices, USD per 1M tokens (conservative; off-peak discount ignored).
  prices_usd_per_1m: { tier: 'peak', miss: 0.3, hit: 0.006, out: 1.2 },
  monthly_budget_usd: 5,
  budget_stop_ratio: 0.8,
  max_cost_usd_per_lecture: 0.3,
  konspekt_single_call_max_input_tokens: 80000,
  chars_per_token: 2,
  map_part_ratio: 0.5,
  map_overlap_segments: 2,
  est_output_tokens_single: 5000,
  est_output_tokens_part: 4000,
  est_output_tokens_reduce: 6000,
  reduce_system_tokens_margin: 1500,
  prompt_versions: { konspekt: 'konspekt_v2', reduce: 'konspekt_reduce_v1' },
  max_attempts: 3,
  stale_running_hours: 4,
};
// In — Execute may only LOWER these limits (never raise).
['konspekt_single_call_max_input_tokens', 'max_cost_usd_per_lecture', 'monthly_budget_usd'].forEach((key) => {
  const v = Number(input[key]);
  if (Number.isFinite(v) && v > 0 && v < constants[key]) constants[key] = v;
});
return [{ json: { constants }, pairedItem: 0 }];"""

LOAD_LECTURE_04 = """SELECT
  j.id AS job_id,
  j.lecture_id,
  j.attempts,
  l.course_id,
  l.title,
  l.duration_sec,
  l.lecturer_name,
  l.status AS lecture_status,
  COALESCE(lt.transcript, '') AS transcript,
  COALESCE((
    SELECT jsonb_agg(
      jsonb_build_object('ordinal', c.ordinal, 'start_ms', c.start_ms, 'end_ms', c.end_ms, 'text', c.text)
      ORDER BY c.ordinal
    )
    FROM psy.chunks c WHERE c.lecture_id = l.id
  ), '[]'::jsonb) AS chunks,
  COALESCE((
    SELECT string_agg(g.canonical, ', ' ORDER BY g.canonical)
    FROM psy.glossary_terms g WHERE g.course_id = l.course_id
  ), '') AS glossary,
  COALESCE((
    SELECT sum(u.cost_usd) FROM psy.llm_usage u
    WHERE u.created_at >= date_trunc('month', now())
  ), 0)::float8 AS spent_month_usd,
  COALESCE((
    SELECT sum(u.cost_usd) FROM psy.llm_usage u WHERE u.lecture_id = l.id
  ), 0)::float8 AS spent_lecture_usd
FROM psy.jobs j
JOIN psy.lectures l ON l.id = j.lecture_id
LEFT JOIN psy.lecture_texts lt ON lt.lecture_id = l.id
WHERE j.id = $1::uuid"""

BUILD_PROMPT_04_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const row = items[0].json;
const SYSTEM = __SYS_KONSPEKT__;
const cpt = C.chars_per_token || 2;
const est = (s) => Math.ceil(String(s || '').length / cpt);

const metaLines = ['МЕТАДАННЫЕ', 'Название: ' + (row.title || '[неизвестно]')];
if (row.duration_sec) metaLines.push('Длительность: ' + Math.round(Number(row.duration_sec) / 60) + ' мин');
if (row.lecturer_name) metaLines.push('Лектор: ' + row.lecturer_name);
const meta = metaLines.join('\n');
const glossary = row.glossary ? 'ГЛОССАРИЙ\n' + row.glossary : '';
const header = [meta, glossary].filter(Boolean).join('\n\n');
const transcript = String(row.transcript || '').trim();
const limit = C.konspekt_single_call_max_input_tokens;
const sysTokens = est(SYSTEM);

function requestBody(user) {
  return {
    model: C.llm_model,
    messages: [
      { role: 'system', content: SYSTEM },
      { role: 'user', content: user },
    ],
    thinking: C.thinking,
    max_tokens: C.max_output_tokens,
    temperature: C.temperature,
    stream: false,
  };
}

const common = {
  job_id: row.job_id,
  lecture_id: row.lecture_id,
  lecture_title: row.title || null,
  meta_text: meta,
  prompt_version: C.prompt_versions.konspekt,
  spent_month_usd: Number(row.spent_month_usd) || 0,
  spent_lecture_usd: Number(row.spent_lecture_usd) || 0,
};

const userSingle = header + '\n\nТРАНСКРИПТ\n' + transcript;
const singleIn = sysTokens + est(userSingle);
const out = [];

if (singleIn <= limit) {
  out.push({
    json: {
      ...common,
      mode: 'single',
      part_index: 1,
      parts_total: 1,
      step: 'konspekt',
      est_input_tokens: singleIn,
      est_output_tokens: C.est_output_tokens_single,
      request_body: requestBody(userSingle),
    },
    pairedItem: 0,
  });
  return out;
}

// map: split chunks by size with 1–2 segment overlap
let segs = (Array.isArray(row.chunks) ? row.chunks : [])
  .map((c) => String((c && c.text) || '').trim())
  .filter(Boolean);
if (!segs.length) {
  // fallback: pseudo-segments from the raw transcript
  segs = [];
  let rest = transcript;
  while (rest.length) {
    let cut = Math.min(rest.length, 1500);
    if (cut < rest.length) {
      const dot = rest.lastIndexOf('. ', cut);
      if (dot > 400) cut = dot + 1;
    }
    segs.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
}
const overhead = sysTokens + est(header) + 200;
const partTokens = Math.floor(limit * C.map_part_ratio) - overhead;
const partChars = Math.max(partTokens * cpt, 4000);
const overlap = Math.max(1, Math.min(2, C.map_overlap_segments || 1));
const bodies = [];
let i = 0;
while (i < segs.length) {
  let j = i;
  let size = 0;
  while (j < segs.length && (j === i || size + segs[j].length + 1 <= partChars)) {
    size += segs[j].length + 1;
    j++;
  }
  bodies.push(segs.slice(i, j).join('\n'));
  if (j >= segs.length) break;
  i = Math.max(j - overlap, i + 1);
}
const total = bodies.length;
bodies.forEach((text, k) => {
  const user = header + '\n\nТРАНСКРИПТ (часть ' + (k + 1) + ' из ' + total + ')\n' + text;
  out.push({
    json: {
      ...common,
      mode: 'map',
      part_index: k + 1,
      parts_total: total,
      step: 'konspekt_map',
      est_input_tokens: sysTokens + est(user),
      est_output_tokens: C.est_output_tokens_part,
      request_body: requestBody(user),
    },
    pairedItem: 0,
  });
});
return out;"""

BUDGET_GUARD_04_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const P = C.prices_usd_per_1m;
const first = items[0].json;
let projected = 0;
let partsOut = 0;
for (const it of items) {
  projected += (it.json.est_input_tokens * P.miss + it.json.est_output_tokens * P.out) / 1e6;
  partsOut += it.json.est_output_tokens;
}
if (first.mode === 'map') {
  const reduceIn = partsOut + C.reduce_system_tokens_margin;
  projected += (reduceIn * P.miss + C.est_output_tokens_reduce * P.out) / 1e6;
}
const lectureCap = C.max_cost_usd_per_lecture;
const monthCap = C.monthly_budget_usd * C.budget_stop_ratio;
const numbers = {
  projected_cost_usd: Number(projected.toFixed(5)),
  spent_lecture_usd: first.spent_lecture_usd,
  spent_month_usd: first.spent_month_usd,
  lecture_cap_usd: lectureCap,
  month_cap_usd: monthCap,
};
let reason = null;
if (first.spent_lecture_usd + projected > lectureCap) {
  reason = 'budget_exceeded: лимит на лекцию ' + lectureCap + ' USD (потрачено ' + first.spent_lecture_usd.toFixed(4)
    + ', прогноз ' + projected.toFixed(4) + ')';
} else if (first.spent_month_usd + projected > monthCap) {
  reason = 'budget_exceeded: 80% месячного бюджета ' + C.monthly_budget_usd + ' USD (потрачено '
    + first.spent_month_usd.toFixed(4) + ', прогноз ' + projected.toFixed(4) + ')';
}
if (reason) {
  return [{ json: { budget_exceeded: true, error_message: reason, lecture_id: first.lecture_id, job_id: first.job_id, ...numbers }, pairedItem: 0 }];
}
return items.map((it, i) => ({
  json: { ...it.json, budget_exceeded: false, ...numbers },
  pairedItem: i,
}));"""

PARSE_USAGE_04_JS = r"""const C = $('Run — Init Constants').first().json.constants;
__PARSE_FN__
const out = [];
items.forEach((it, i) => {
  const src = __SRC__;
  const p = parseDeepSeek(it.json, C, src, src.step, src.prompt_version);
  const text = p.content.trim();
  let errorMessage = null;
  if (!text) errorMessage = 'llm_empty_response';
  else if (p.finish_reason === 'length') errorMessage = 'llm_truncated: finish_reason=length (' + src.step + ')';
  out.push({
    json: {
      ok: !errorMessage,
      error_message: errorMessage,
      content: p.content,
      finish_reason: p.finish_reason,
      cost_exact: p.cost_exact,
      usage_row: p.usage_row,
      part_index: src.part_index || 1,
      parts_total: src.parts_total || 1,
      mode: src.mode || 'single',
      lecture_id: src.lecture_id,
      job_id: src.job_id,
    },
    pairedItem: i,
  });
});
return out;"""

CLEAN_MD_JS = r"""function cleanMd(s) {
  let t = String(s || '').trim();
  const m = t.match(/^```(?:markdown|md)\s*\n([\s\S]*?)\n```$/);
  if (m) t = m[1].trim();
  return t;
}"""

COLLECT_PARTS_04_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const parsed = $('LLM — Parse Usage').all().map((i) => i.json);
const total = parsed.length ? parsed[0].parts_total : 0;
// Some parts failed at the HTTP node: the error output already goes to Fin — Fail Context.
if (!parsed.length || parsed.length < total) return [];
__CLEAN__
parsed.sort((a, b) => a.part_index - b.part_index);
const build = $('LLM — Budget Guard').first().json;
const sum = (k) => parsed.reduce((s, p) => s + (Number(p.usage_row[k]) || 0), 0);
const cost = parsed.reduce((s, p) => s + p.cost_exact, 0);
const failed = parsed.filter((p) => !p.ok);
const mode = parsed[0].mode;
const base = {
  lecture_id: parsed[0].lecture_id,
  job_id: parsed[0].job_id,
  mode,
  meta_text: build.meta_text,
  prompt_versions: mode === 'single' ? [C.prompt_versions.konspekt] : [C.prompt_versions.konspekt, C.prompt_versions.reduce],
  llm_model: parsed[0].usage_row.model,
  map_cost_usd: cost,
  map_prompt_tokens: sum('prompt_tokens'),
  map_completion_tokens: sum('completion_tokens'),
  map_cache_hit_tokens: sum('prompt_cache_hit_tokens'),
  parts_total: total,
};
if (failed.length) {
  return [{ json: { ...base, ok: false, error_message: failed.map((p) => 'part ' + p.part_index + ': ' + p.error_message).join('; ') }, pairedItem: 0 }];
}
const save_payload = {
  konspekt_result: {
    mode,
    parts: total,
    prompt_versions: base.prompt_versions,
    llm_model: base.llm_model,
    workflow_version: C.workflow_version,
    prompt_tokens: base.map_prompt_tokens,
    completion_tokens: base.map_completion_tokens,
    cache_hit_tokens: base.map_cache_hit_tokens,
    cost_usd: Number(cost.toFixed(5)),
  },
};
if (mode === 'single') {
  return [{ json: { ...base, ok: true, konspekt_md: cleanMd(parsed[0].content), save_payload }, pairedItem: 0 }];
}
return [{ json: { ...base, ok: true, parts_md: parsed.map((p) => cleanMd(p.content)), save_payload }, pairedItem: 0 }];"""

REDUCE_PREP_04_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const col = items[0].json;
const SYSTEM = __SYS_REDUCE__;
const cpt = C.chars_per_token || 2;
const m = col.parts_md.length;
const body = col.parts_md.map((p, i) => (i + 1) + '. часть ' + (i + 1) + ' из ' + m + '\n\n' + p).join('\n\n---\n\n');
const user = col.meta_text + '\n\nЧАСТИ\n' + body;
return [{
  json: {
    lecture_id: col.lecture_id,
    job_id: col.job_id,
    mode: 'map',
    part_index: 1,
    parts_total: 1,
    step: 'konspekt_reduce',
    prompt_version: C.prompt_versions.reduce,
    est_input_tokens: Math.ceil((SYSTEM.length + user.length) / cpt),
    request_body: {
      model: C.llm_model,
      messages: [
        { role: 'system', content: SYSTEM },
        { role: 'user', content: user },
      ],
      thinking: C.thinking,
      max_tokens: C.max_output_tokens,
      temperature: C.temperature,
      stream: false,
    },
  },
  pairedItem: 0,
}];"""

FINALIZE_REDUCE_04_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const p = $('LLM — Parse Usage Reduce').first().json;
const col = $('LLM — Collect Parts').first().json;
__CLEAN__
const payload = JSON.parse(JSON.stringify(col.save_payload));
const r = payload.konspekt_result;
r.reduce_prompt_tokens = p.usage_row.prompt_tokens;
r.reduce_completion_tokens = p.usage_row.completion_tokens;
r.reduce_cache_hit_tokens = p.usage_row.prompt_cache_hit_tokens;
r.cost_usd = Number((r.cost_usd + p.cost_exact).toFixed(5));
return [{
  json: {
    ok: p.ok,
    error_message: p.error_message,
    lecture_id: col.lecture_id,
    job_id: col.job_id,
    mode: 'map',
    llm_model: p.usage_row.model,
    konspekt_md: p.ok ? cleanMd(p.content) : '',
    save_payload: payload,
  },
  pairedItem: 0,
}];"""

SAVE_KONSPEKT_SQL = """WITH params AS (
  SELECT
    $1::uuid AS lecture_id,
    $2::uuid AS job_id,
    $3::text AS konspekt_md,
    $4::text AS llm_model,
    $5::jsonb AS result_payload,
    $6::text AS workflow_version
), up AS (
  INSERT INTO psy.lecture_texts (lecture_id, konspekt_md, llm_model)
  SELECT lecture_id, konspekt_md, llm_model FROM params
  ON CONFLICT (lecture_id) DO UPDATE
    SET konspekt_md = EXCLUDED.konspekt_md,
        llm_model = EXCLUDED.llm_model
  RETURNING lecture_id
), lec AS (
  UPDATE psy.lectures l
  SET status = 'konspekt_ready', error = NULL
  FROM params p
  WHERE l.id = p.lecture_id
    AND EXISTS (SELECT 1 FROM up)
    AND l.status IN ('transcribed', 'konspekt_ready', 'failed')
  RETURNING l.id
), done_job AS (
  UPDATE psy.jobs j
  SET status = 'done',
      finished_at = now(),
      error = NULL,
      payload = COALESCE(j.payload, '{}'::jsonb) || p.result_payload
        || jsonb_build_object('workflow_version', p.workflow_version)
  FROM params p
  WHERE j.id = p.job_id AND EXISTS (SELECT 1 FROM up)
  RETURNING j.id
), enq AS (
  INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
  SELECT p.lecture_id, 'terms', 'queued', 'terms:' || p.lecture_id::text,
         jsonb_build_object('workflow_version', p.workflow_version, 'source', 'psy_04')
  FROM params p
  WHERE EXISTS (SELECT 1 FROM up)
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
)
SELECT
  (SELECT lecture_id FROM up) AS lecture_id,
  (SELECT id FROM lec) AS lecture_updated,
  (SELECT id FROM done_job) AS job_id,
  (SELECT id FROM enq) AS terms_job_id"""

FAIL_EXTRA_04 = r"""if (e.budget_exceeded === true) {
  permanent = true;
  keepLecture = true;
  message = e.error_message || 'budget_exceeded';
} else if (e.transcript !== undefined && !String(e.transcript || '').trim() && !e.error) {
  permanent = true;
  keepLecture = true;
  message = 'empty_transcript: у лекции нет текста транскрипта, LLM не вызывался';
}"""


def build_psy_04(psy_05_id: str) -> tuple[str, list, dict]:
    nodes: list = []
    conn: dict = {}
    sys_konspekt = js_literal(load_system_prompt("konspekt_v2.md"))
    sys_reduce = js_literal(load_system_prompt("konspekt_reduce_v1.md"))

    nodes.append(sticky("NOTE — In", "## In\nManual + Execute (passthrough). Optional lower-only overrides: `konspekt_single_call_max_input_tokens`, `max_cost_usd_per_lecture`, `monthly_budget_usd`. Inactive.", -40, 40, 360, 160))
    nodes.append(sticky("NOTE — Run", "## Run\nInit constants (prices peak: miss 0.3 / hit 0.006 / out 1.2 USD per 1M) → backfill konspekt jobs → claim `konspekt` (SKIP LOCKED).", 380, 40, 420, 160))
    nodes.append(sticky("NOTE — LLM", "## LLM\nsingle call or map (parts with 1–2 segment overlap) → reduce. DeepSeek only via `Shared — DeepSeek*`. Budget guard: lecture cap + 80% month; budget_exceeded keeps lecture status.", 1100, 40, 520, 180))
    nodes.append(sticky("NOTE — DB/Fin", "## DB / Fin\nlecture_texts.konspekt_md, lecture → konspekt_ready, enqueue `terms:<id>`, kick psy_05. Fail: 409→queued, 3rd attempt→failed.", 2500, 40, 460, 160))

    n_manual = manual_trigger(0, 300)
    n_exec = exec_trigger(0, 480)
    n_init = code("Run — Init Constants", sub_code(INIT_JS_04, WF=WF_VER["psy_04"]), 220, 380)
    n_backfill = pg(
        "Load — Backfill",
        backfill_sql("transcribed", "konspekt"),
        440,
        380,
        "={{ [ $('Run — Init Constants').first().json.constants.workflow_version ] }}",
    )
    n_claim = pg(
        "Run — Create Job",
        claim_sql("konspekt"),
        660,
        380,
        "={{ [ $('Run — Init Constants').first().json.constants.workflow_version ] }}",
    )
    n_has = iff("Run — Has Job", "={{ $json.id }}", "", 880, 380, "notEmpty")
    n_lecture = pg(
        "Load — Lecture",
        LOAD_LECTURE_04,
        1100,
        300,
        "={{ [ $('Run — Create Job').first().json.id ] }}",
    )
    n_has_tr = iff(
        "Load — Has Transcript",
        "={{ String($json.transcript || '').trim() }}",
        "",
        1320,
        300,
        "notEmpty",
    )
    n_build = code(
        "LLM — Build Prompt",
        sub_code(BUILD_PROMPT_04_JS, SYS_KONSPEKT=sys_konspekt),
        1540,
        220,
    )
    n_guard = code("LLM — Budget Guard", BUDGET_GUARD_04_JS, 1760, 220)
    n_budget_ok = iff("LLM — Budget OK", "={{ String($json.budget_exceeded) }}", "false", 1980, 220, "equals")
    n_http = deepseek("Shared — DeepSeek", 2200, 140)
    n_parse = code(
        "LLM — Parse Usage",
        sub_code(
            PARSE_USAGE_04_JS,
            PARSE_FN=PARSE_DEEPSEEK_JS,
            SRC="$('LLM — Budget Guard').itemMatching(i).json",
        ),
        2420,
        140,
    )
    n_log = log_usage("DB — Log Usage", 2640, 140)
    n_collect = code(
        "LLM — Collect Parts",
        sub_code(COLLECT_PARTS_04_JS, CLEAN=CLEAN_MD_JS),
        2860,
        140,
    )
    n_parts_ok = iff("LLM — Parts OK", "={{ String($json.ok) }}", "true", 3080, 140, "equals")
    n_single = iff("LLM — Is Single", "={{ $json.mode }}", "single", 3300, 140, "equals")
    n_reduce_prep = code(
        "LLM — Reduce Prep",
        sub_code(REDUCE_PREP_04_JS, SYS_REDUCE=sys_reduce),
        3520,
        300,
    )
    n_http_reduce = deepseek("Shared — DeepSeek Reduce", 3740, 300)
    n_parse_reduce = code(
        "LLM — Parse Usage Reduce",
        sub_code(
            PARSE_USAGE_04_JS,
            PARSE_FN=PARSE_DEEPSEEK_JS,
            SRC="$('LLM — Reduce Prep').first().json",
        ),
        3960,
        300,
    )
    n_log_reduce = log_usage("DB — Log Usage Reduce", 4180, 300)
    n_final_reduce = code(
        "LLM — Finalize Reduce",
        sub_code(FINALIZE_REDUCE_04_JS, CLEAN=CLEAN_MD_JS),
        4400,
        300,
    )
    n_reduce_ok = iff("LLM — Reduce OK", "={{ String($json.ok) }}", "true", 4620, 300, "equals")
    n_save = pg(
        "DB — Save Konspekt",
        SAVE_KONSPEKT_SQL,
        4840,
        140,
        """={{ [
  $json.lecture_id,
  $json.job_id,
  $json.konspekt_md,
  $json.llm_model,
  JSON.stringify($json.save_payload || {}),
  $('Run — Init Constants').first().json.constants.workflow_version
] }}""",
    )
    n_kick = kick("Fin — Kick psy_05", psy_05_id, 5060, 140)
    n_fail_ctx, n_fail = fail_nodes(3300, 560, FAIL_EXTRA_04)

    nodes.extend([
        n_manual, n_exec, n_init, n_backfill, n_claim, n_has, n_lecture, n_has_tr, n_build, n_guard,
        n_budget_ok, n_http, n_parse, n_log, n_collect, n_parts_ok, n_single, n_reduce_prep, n_http_reduce,
        n_parse_reduce, n_log_reduce, n_final_reduce, n_reduce_ok, n_save, n_kick, n_fail_ctx, n_fail,
    ])

    link(conn, "In — Manual", "Run — Init Constants")
    link(conn, "In — Execute", "Run — Init Constants")
    link(conn, "Run — Init Constants", "Load — Backfill")
    link(conn, "Load — Backfill", "Run — Create Job")
    link(conn, "Run — Create Job", "Run — Has Job")
    link(conn, "Run — Has Job", "Load — Lecture", 0)
    link(conn, "Load — Lecture", "Load — Has Transcript")
    link(conn, "Load — Has Transcript", "LLM — Build Prompt", 0)
    link(conn, "Load — Has Transcript", "Fin — Fail Context", 1)
    link(conn, "LLM — Build Prompt", "LLM — Budget Guard")
    link(conn, "LLM — Budget Guard", "LLM — Budget OK")
    link(conn, "LLM — Budget OK", "Shared — DeepSeek", 0)
    link(conn, "LLM — Budget OK", "Fin — Fail Context", 1)
    link(conn, "Shared — DeepSeek", "LLM — Parse Usage", 0)
    link(conn, "Shared — DeepSeek", "Fin — Fail Context", 1)
    link(conn, "LLM — Parse Usage", "DB — Log Usage")
    link(conn, "DB — Log Usage", "LLM — Collect Parts")
    link(conn, "LLM — Collect Parts", "LLM — Parts OK")
    link(conn, "LLM — Parts OK", "LLM — Is Single", 0)
    link(conn, "LLM — Parts OK", "Fin — Fail Context", 1)
    link(conn, "LLM — Is Single", "DB — Save Konspekt", 0)
    link(conn, "LLM — Is Single", "LLM — Reduce Prep", 1)
    link(conn, "LLM — Reduce Prep", "Shared — DeepSeek Reduce")
    link(conn, "Shared — DeepSeek Reduce", "LLM — Parse Usage Reduce", 0)
    link(conn, "Shared — DeepSeek Reduce", "Fin — Fail Context", 1)
    link(conn, "LLM — Parse Usage Reduce", "DB — Log Usage Reduce")
    link(conn, "DB — Log Usage Reduce", "LLM — Finalize Reduce")
    link(conn, "LLM — Finalize Reduce", "LLM — Reduce OK")
    link(conn, "LLM — Reduce OK", "DB — Save Konspekt", 0)
    link(conn, "LLM — Reduce OK", "Fin — Fail Context", 1)
    link(conn, "DB — Save Konspekt", "Fin — Kick psy_05")
    link(conn, "Fin — Fail Context", "Fin — Fail Job")
    return "psy_04_konspekt", nodes, conn


# ======================================================================================
# psy_05_terms_questions
# ======================================================================================
INIT_JS_05 = r"""const input = (items[0] && items[0].json) || {};
const constants = {
  workflow_version: '__WF__',
  workflow_name: 'psy_05_terms_questions',
  llm_model: 'deepseek-flash',
  thinking: { type: 'disabled' },
  temperature_terms: 0.2,
  temperature_questions: 0.5,
  max_output_tokens: 8000,
  // DeepSeek peak prices, USD per 1M tokens (conservative; off-peak discount ignored).
  prices_usd_per_1m: { tier: 'peak', miss: 0.3, hit: 0.006, out: 1.2 },
  monthly_budget_usd: 5,
  budget_stop_ratio: 0.8,
  max_cost_usd_per_lecture: 0.3,
  chars_per_token: 2,
  terms_max_input_tokens: 80000,
  est_output_tokens_terms: 6000,
  est_output_tokens_questions: 6000,
  est_input_tokens_questions: 8000,
  est_subtopics_for_budget: 8,
  // Contract v6: 8–12 unique per subtopic; fewer OK with shortfall (no clones).
  questions_min: 8,
  questions_target: 10,
  questions_max: 12,
  max_questions_per_subtopic: 12,
  max_letter_share: 0.35,
  stem_similarity_threshold: 0.72,
  questions_konspekt_max_chars: 5500,
  questions_transcript_max_chars: 7000,
  questions_thin_min_chars: 400,
  est_output_tokens_fill: 2500,
  est_input_tokens_fill: 12000,
  prompt_versions: {
    terms: 'extract_terms_v2',
    fill_definitions: 'fill_term_definitions_v2',
    questions: 'gen_questions_v5',
  },
  max_attempts: 3,
  stale_running_hours: 4,
  // Kick overrides (Execute passthrough): only_subtopic_ordinals, skip_terms_extract
  only_subtopic_ordinals: null,
  skip_terms_extract: false,
};
// In — Execute may only LOWER these limits (never raise).
['max_cost_usd_per_lecture', 'monthly_budget_usd'].forEach((key) => {
  const v = Number(input[key]);
  if (Number.isFinite(v) && v > 0 && v < constants[key]) constants[key] = v;
});
if (Array.isArray(input.only_subtopic_ordinals) && input.only_subtopic_ordinals.length) {
  constants.only_subtopic_ordinals = input.only_subtopic_ordinals
    .map((n) => Number(n))
    .filter((n) => Number.isInteger(n) && n >= 1);
}
if (input.skip_terms_extract === true || input.skip_terms_extract === 'true') {
  constants.skip_terms_extract = true;
}
return [{ json: { constants, kick: input }, pairedItem: 0 }];"""

LOAD_LECTURE_05 = """SELECT
  j.id AS job_id,
  j.lecture_id,
  j.attempts,
  l.course_id,
  l.title,
  l.duration_sec,
  l.lecturer_name,
  l.status AS lecture_status,
  COALESCE(lt.konspekt_md, '') AS konspekt_md,
  COALESCE(lt.transcript, '') AS transcript,
  COALESCE((
    SELECT jsonb_agg(jsonb_build_object('ordinal', c.ordinal, 'text', c.text) ORDER BY c.ordinal)
    FROM psy.chunks c WHERE c.lecture_id = l.id
  ), '[]'::jsonb) AS chunks,
  COALESCE((
    SELECT string_agg(g.canonical, ', ' ORDER BY g.canonical)
    FROM psy.glossary_terms g WHERE g.course_id = l.course_id
  ), '') AS glossary,
  COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
      'canonical', g.canonical,
      'aliases', COALESCE(g.aliases, ARRAY[]::text[]),
      'definition', g.definition
    ) ORDER BY g.canonical)
    FROM psy.glossary_terms g
    WHERE g.course_id = l.course_id
      AND g.definition IS NOT NULL
      AND length(trim(g.definition)) > 0
  ), '[]'::jsonb) AS course_terms,
  (
    SELECT count(*) FROM psy.cards c
    JOIN psy.questions q ON q.id = c.question_id
    WHERE q.lecture_id = l.id
      AND q.kind = 'life_mcq'
      AND q.superseded_at IS NULL
  )::int AS cards_count,
  COALESCE((
    SELECT sum(u.cost_usd) FROM psy.llm_usage u
    WHERE u.created_at >= date_trunc('month', now())
  ), 0)::float8 AS spent_month_usd,
  COALESCE((
    SELECT sum(u.cost_usd) FROM psy.llm_usage u WHERE u.lecture_id = l.id
  ), 0)::float8 AS spent_lecture_usd
FROM psy.jobs j
JOIN psy.lectures l ON l.id = j.lecture_id
LEFT JOIN psy.lecture_texts lt ON lt.lecture_id = l.id
WHERE j.id = $1::uuid"""

BUILD_TERMS_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const row = items[0].json;
const SYSTEM = __SYS_TERMS__;
const cpt = C.chars_per_token || 2;
const est = (s) => Math.ceil(String(s || '').length / cpt);

const metaLines = ['МЕТАДАННЫЕ', 'Название: ' + (row.title || '[неизвестно]')];
if (row.duration_sec) metaLines.push('Длительность: ' + Math.round(Number(row.duration_sec) / 60) + ' мин');
if (row.lecturer_name) metaLines.push('Лектор: ' + row.lecturer_name);
const head = [
  metaLines.join('\n'),
  'ГЛОССАРИЙ\n' + (row.glossary || ''),
  'КОНСПЕКТ\n' + String(row.konspekt_md || '').trim(),
].join('\n\n');

let transcript = String(row.transcript || '').trim();
let truncated = false;
const room = (C.terms_max_input_tokens - est(SYSTEM) - est(head) - 200) * cpt;
if (transcript.length > room) {
  transcript = transcript.slice(0, Math.max(room, 0)) + '\n[транскрипт усечён по лимиту токенов]';
  truncated = true;
}
const user = head + '\n\nТРАНСКРИПТ\n' + transcript;

const P = C.prices_usd_per_1m;
const inTokens = est(SYSTEM) + est(user);
const termsCost = (inTokens * P.miss + C.est_output_tokens_terms * P.out) / 1e6;
const questionCall = (C.est_input_tokens_questions * P.miss + C.est_output_tokens_questions * P.out) / 1e6;
const projected = termsCost + C.est_subtopics_for_budget * questionCall;
const spentLecture = Number(row.spent_lecture_usd) || 0;
const spentMonth = Number(row.spent_month_usd) || 0;
const lectureCap = C.max_cost_usd_per_lecture;
const monthCap = C.monthly_budget_usd * C.budget_stop_ratio;
let reason = null;
if (spentLecture + projected > lectureCap) {
  reason = 'budget_exceeded: лимит на лекцию ' + lectureCap + ' USD (потрачено ' + spentLecture.toFixed(4)
    + ', прогноз терминов+вопросов ' + projected.toFixed(4) + ')';
} else if (spentMonth + projected > monthCap) {
  reason = 'budget_exceeded: 80% месячного бюджета ' + C.monthly_budget_usd + ' USD (потрачено '
    + spentMonth.toFixed(4) + ', прогноз ' + projected.toFixed(4) + ')';
}
if (reason) {
  return [{ json: { budget_exceeded: true, error_message: reason, lecture_id: row.lecture_id, job_id: row.job_id }, pairedItem: 0 }];
}
return [{
  json: {
    budget_exceeded: false,
    lecture_id: row.lecture_id,
    job_id: row.job_id,
    course_id: row.course_id,
    step: 'terms',
    prompt_version: C.prompt_versions.terms,
    transcript_truncated: truncated,
    projected_cost_usd: Number(projected.toFixed(5)),
    request_body: {
      model: C.llm_model,
      messages: [
        { role: 'system', content: SYSTEM },
        { role: 'user', content: user },
      ],
      thinking: C.thinking,
      response_format: { type: 'json_object' },
      max_tokens: C.max_output_tokens,
      temperature: C.temperature_terms,
      stream: false,
    },
  },
  pairedItem: 0,
}];"""

PARSE_TERMS_JS = r"""__SUBTOPIC_LIB__
const C = $('Run — Init Constants').first().json.constants;
const src = $('LLM — Build Terms Request').first().json;
const L = $('Load — Lecture').first().json;
__PARSE_FN__
const p = parseDeepSeek(items[0].json, C, src, 'terms', C.prompt_versions.terms);

function parseJson(text) {
  let t = String(text || '').trim();
  const m = t.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/);
  if (m) t = m[1];
  try {
    return { value: JSON.parse(t) };
  } catch (e) {
    const a = t.indexOf('{');
    const b = t.lastIndexOf('}');
    if (a >= 0 && b > a) {
      try {
        return { value: JSON.parse(t.slice(a, b + 1)) };
      } catch (e2) {
        /* fall through */
      }
    }
    return { error: 'invalid_json' };
  }
}

const result = {
  lecture_id: src.lecture_id,
  job_id: src.job_id,
  usage_row: p.usage_row,
  cost_exact: p.cost_exact,
  finish_reason: p.finish_reason,
  transcript_truncated: src.transcript_truncated,
  ok: false,
  error_message: null,
  subtopics: [],
  terms: [],
};

function fail(msg) {
  result.error_message = msg;
  return [{ json: result, pairedItem: 0 }];
}

if (!p.content.trim()) return fail('llm_empty_response (terms)');
const pj = parseJson(p.content);
if (pj.error) {
  return fail(p.finish_reason === 'length' ? 'llm_truncated: finish_reason=length (terms)' : 'terms: invalid_json');
}
const data = pj.value;
if (!data || typeof data !== 'object' || !Array.isArray(data.subtopics) || !Array.isArray(data.terms)) {
  return fail('terms: schema (нужны subtopics[] и terms[])');
}

// empty_subtopics is soft: synthesize before binding terms (avoids TypeError on
// subs[0] when terms exist). Course fallback / no_terms stay in Terms Ready.
// Do not name this `norm`: SUBTOPIC_LIB already declares function norm().
const extracted = normalizeTermsExtract(data, { lectureTitle: L.title || '' });
result.ok = true;
result.subtopics = extracted.subtopics;
result.terms = extracted.terms;
result.lecture_terms_empty = extracted.lecture_terms_empty;
if (extracted.synthesizedSubtopic) {
  result.error_message = 'terms: empty_subtopics';
}
if (!extracted.terms.length) {
  result.error_message = result.error_message || 'terms: empty_terms';
}
return [{ json: result, pairedItem: 0 }];"""

UPSERT_TERMS_SQL = """WITH params AS (
  SELECT $1::uuid AS course_id, $2::uuid AS lecture_id, $3::jsonb AS terms
), src AS (
  SELECT t.canonical, t.aliases, t.definition, t.life_example
  FROM params p
  CROSS JOIN LATERAL jsonb_to_recordset(p.terms) AS t(
    canonical text, aliases jsonb, definition text, life_example text
  )
), up AS (
  INSERT INTO psy.glossary_terms AS g (course_id, lecture_id, canonical, aliases, definition, life_example)
  SELECT p.course_id, p.lecture_id, s.canonical,
         ARRAY(SELECT jsonb_array_elements_text(COALESCE(s.aliases, '[]'::jsonb))),
         s.definition, s.life_example
  FROM params p
  CROSS JOIN src s
  ON CONFLICT (course_id, canonical) DO UPDATE SET
    lecture_id = COALESCE(g.lecture_id, EXCLUDED.lecture_id),
    aliases = ARRAY(
      SELECT DISTINCT a
      FROM unnest(g.aliases || EXCLUDED.aliases) AS a
      WHERE btrim(a) <> '' AND a <> g.canonical
      ORDER BY a
    ),
    definition = CASE
      WHEN NULLIF(btrim(g.definition), '') IS NULL THEN EXCLUDED.definition
      WHEN g.lecture_id IS NOT DISTINCT FROM EXCLUDED.lecture_id AND EXCLUDED.definition IS NOT NULL
        THEN EXCLUDED.definition
      ELSE g.definition
    END,
    life_example = CASE
      WHEN NULLIF(btrim(g.definition), '') IS NULL THEN EXCLUDED.life_example
      WHEN g.lecture_id IS NOT DISTINCT FROM EXCLUDED.lecture_id AND EXCLUDED.definition IS NOT NULL
        THEN EXCLUDED.life_example
      ELSE g.life_example
    END
  RETURNING g.id
)
SELECT count(*)::int AS terms_upserted FROM up"""

UPSERT_SUBTOPICS_SQL = """WITH params AS (
  SELECT $1::uuid AS lecture_id, $2::jsonb AS subtopics
), ins AS (
  INSERT INTO psy.subtopics (lecture_id, ordinal, title)
  SELECT p.lecture_id, s.ordinal, s.title
  FROM params p
  CROSS JOIN LATERAL jsonb_to_recordset(p.subtopics) AS s(ordinal int, title text)
  ON CONFLICT (lecture_id, ordinal) DO UPDATE SET title = EXCLUDED.title
  RETURNING id, ordinal, title
), del AS (
  DELETE FROM psy.subtopics st
  USING params p
  WHERE st.lecture_id = p.lecture_id
    AND st.ordinal NOT IN (SELECT ordinal FROM ins)
    AND NOT EXISTS (
      SELECT 1 FROM psy.cards c
      JOIN psy.questions q ON q.id = c.question_id
      WHERE q.lecture_id = p.lecture_id
    )
  RETURNING st.id
)
SELECT i.id AS subtopic_id, i.ordinal, i.title, (SELECT count(*) FROM del)::int AS deleted_subtopics
FROM ins i
ORDER BY i.ordinal"""

LOAD_SPEND_SQL = """SELECT
  $1::uuid AS subtopic_id,
  $2::int AS ordinal,
  $3::text AS title,
  COALESCE((
    SELECT sum(u.cost_usd) FROM psy.llm_usage u
    WHERE u.created_at >= date_trunc('month', now())
  ), 0)::float8 AS spent_month_usd,
  COALESCE((
    SELECT sum(u.cost_usd) FROM psy.llm_usage u WHERE u.lecture_id = $4::uuid
  ), 0)::float8 AS spent_lecture_usd,
  COALESCE((
    SELECT jsonb_agg(q.stem ORDER BY q.created_at, q.id)
    FROM psy.questions q
    WHERE q.lecture_id = $4::uuid
      AND q.kind = 'life_mcq'
      AND q.superseded_at IS NULL
  ), '[]'::jsonb) AS used_stems,
  COALESCE((
    SELECT jsonb_agg(
      lower(trim(COALESCE(g.canonical, q.thesis, ''))) || '||' || lower(trim(regexp_replace(
        CASE q.correct_choice
          WHEN 'A' THEN q.choice_a
          WHEN 'B' THEN q.choice_b
          WHEN 'C' THEN q.choice_c
          ELSE q.choice_d
        END, '\\s+', ' ', 'g')))
      ORDER BY q.created_at, q.id
    )
    FROM psy.questions q
    LEFT JOIN psy.glossary_terms g ON g.id = q.term_id
    WHERE q.lecture_id = $4::uuid
      AND q.kind = 'life_mcq'
      AND q.superseded_at IS NULL
  ), '[]'::jsonb) AS used_term_answers,
  COALESCE((
    SELECT jsonb_object_agg(letter, cnt)
    FROM (
      SELECT q.correct_choice AS letter, count(*)::int AS cnt
      FROM psy.questions q
      WHERE q.lecture_id = $4::uuid
        AND q.kind = 'life_mcq'
        AND q.superseded_at IS NULL
      GROUP BY q.correct_choice
    ) s
  ), '{}'::jsonb) AS letter_counts"""

BUILD_QUESTIONS_JS = r"""__SUBTOPIC_LIB__
const C = $('Run — Init Constants').first().json.constants;
const L = $('Load — Lecture').first().json;
const mergeThin = $('LLM — Merge Thin Subtopics').item.json;
const sd = $getWorkflowStaticData('global');
const T = {
  ...mergeThin,
  terms: (sd.filledTerms && sd.filledTerms.length) ? sd.filledTerms : (mergeThin.terms || []),
};
const spend = items[0].json;
const loopSub = $('Run — Loop Subtopics').item.json;
const runIndex = typeof $runIndex === 'number' ? $runIndex : 0;
const subList = $('DB — Upsert Subtopics').all().map((i) => i.json);
const sub = {
  ...loopSub,
  ...spend,
  subtopic_id: spend.subtopic_id || loopSub.subtopic_id || loopSub.id,
  ordinal: Number(spend.ordinal != null ? spend.ordinal : loopSub.ordinal),
  title: spend.title || loopSub.title,
};
const SYSTEM = __SYS_Q__;
const cpt = C.chars_per_token || 2;
const est = (s) => Math.ceil(String(s || '').length / cpt);

const usedStems = Array.isArray(spend.used_stems) ? spend.used_stems : [];
const usedTermAnswers = Array.isArray(spend.used_term_answers) ? spend.used_term_answers : [];
const letterCountsRaw = spend.letter_counts && typeof spend.letter_counts === 'object'
  ? spend.letter_counts
  : {};
const letterCounts = { A: 0, B: 0, C: 0, D: 0 };
for (const Ltr of ['A', 'B', 'C', 'D']) {
  letterCounts[Ltr] = Number(letterCountsRaw[Ltr]) || 0;
}

const parts = buildQuestionsRequestParts({
  subtopic: { ordinal: sub.ordinal, title: sub.title },
  allTerms: T.terms || [],
  courseTerms: L.course_terms || [],
  konspektMd: L.konspekt_md,
  chunks: L.chunks,
  transcript: L.transcript || '',
  constants: C,
  subtopicIndex: Math.max(0, subList.findIndex((s) => Number(s.ordinal) === sub.ordinal)),
  subtopicCount: Math.max(subList.length, 1),
  usedPlots: usedStems,
  usedTermAnswers,
  ownOnly: true,
});

const base = {
  ...spend,
  lecture_id: L.lecture_id,
  job_id: L.job_id,
  course_id: L.course_id,
  subtopic_id: sub.subtopic_id,
  ordinal: sub.ordinal,
  title: sub.title,
  spent_month_usd: Number(spend.spent_month_usd) || 0,
  spent_lecture_usd: Number(spend.spent_lecture_usd) || 0,
  term_canonicals: parts.term_canonicals,
  terms_fallback: parts.terms_fallback,
  terms_source: parts.terms_source,
  theses: parts.theses || [],
  existing_stems: usedStems,
  used_stems: usedStems,
  used_term_answers: usedTermAnswers,
  letter_counts: letterCounts,
  step: 'questions',
  prompt_version: C.prompt_versions.questions,
  loop_run_index: runIndex,
  skip: Boolean(parts.skip),
  skip_reason: parts.skip_reason,
  thesis_mode: Boolean(parts.thesis_mode),
};

if (parts.skip) {
  return [{ json: { ...base, budget_exceeded: false }, pairedItem: 0 }];
}

const user = parts.user_base;
const P = C.prices_usd_per_1m;
const inTokens = est(SYSTEM) + est(user);
const projected = (inTokens * P.miss + C.est_output_tokens_questions * P.out) / 1e6;
const lectureCap = C.max_cost_usd_per_lecture;
const monthCap = C.monthly_budget_usd * C.budget_stop_ratio;
let reason = null;
if (base.spent_lecture_usd + projected > lectureCap) {
  reason = 'budget_exceeded: лимит на лекцию ' + lectureCap + ' USD перед подтемой ' + sub.ordinal
    + ' (потрачено ' + base.spent_lecture_usd.toFixed(4) + ')';
} else if (base.spent_month_usd + projected > monthCap) {
  reason = 'budget_exceeded: 80% месячного бюджета ' + C.monthly_budget_usd + ' USD перед подтемой ' + sub.ordinal
    + ' (потрачено ' + base.spent_month_usd.toFixed(4) + ')';
}
if (reason) {
  return [{ json: { ...base, skip: false, budget_exceeded: true, error_message: reason }, pairedItem: 0 }];
}
return [{
  json: {
    ...base,
    skip: false,
    budget_exceeded: false,
    est_input_tokens: inTokens,
    user_base: user,
    konspekt_fragment_chars: (parts.konspekt_fragment || '').length,
    request_body: {
      model: C.llm_model,
      messages: [
        { role: 'system', content: SYSTEM },
        { role: 'user', content: user },
      ],
      thinking: C.thinking,
      response_format: { type: 'json_object' },
      max_tokens: C.max_output_tokens,
      temperature: C.temperature_questions,
      stream: false,
    },
  },
  pairedItem: 0,
}];"""

NOTE_NO_TERMS_SQL = """UPDATE psy.jobs
SET payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object(
  'no_terms_subtopics',
  COALESCE(payload->'no_terms_subtopics', '[]'::jsonb) || jsonb_build_array(
    jsonb_build_object(
      'subtopic_ordinal', $1::int,
      'title', $2::text,
      'reason', COALESCE(NULLIF($4::text, ''), 'no_terms_or_theses')
    )
  )
)
WHERE id = $3::uuid
RETURNING id, payload->'no_terms_subtopics' AS no_terms_subtopics"""

MERGE_THIN_JS = r"""__SUBTOPIC_LIB__
const item = items[0].json;
const L = $('Load — Lecture').first().json;
const C = $('Run — Init Constants').first().json.constants;
// skip_terms_extract: keep DB subtopic list intact (do not fold ordinals).
const merged = C.skip_terms_extract
  ? { subtopics: item.subtopics || [], terms: item.terms || [], merges: [] }
  : mergeThinSubtopics(
  item.subtopics || [],
  item.terms || [],
  L.konspekt_md || '',
  {
    thinMinChars: C.questions_thin_min_chars,
    konspektMaxChars: C.questions_konspekt_max_chars,
  },
);
const det = fillDefinitionsFromText(
  merged.terms,
  L.konspekt_md || '',
  L.transcript || '',
);
// Drop broken definitions so fill LLM can rewrite them.
const cleaned = det.terms.map((t) => {
  if (needsDefinitionRefill(t.definition, t.canonical)) {
    return { ...t, definition: null, life_example: null };
  }
  return t;
});
const emptyDefs = cleaned.filter((t) => needsDefinitionRefill(t.definition, t.canonical));
return [{
  json: {
    ...item,
    subtopics: merged.subtopics,
    terms: cleaned,
    thin_merges: merged.merges,
    definitions_filled_heuristic: det.filled,
    definitions_rejected_heuristic: det.rejected || [],
    empty_definitions: emptyDefs.map((t) => ({
      canonical: t.canonical,
      aliases: t.aliases || [],
    })),
    needs_fill: emptyDefs.length > 0,
  },
  pairedItem: 0,
}];"""

PARSE_QUESTIONS_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const b = $('__META_NODE__').item.json;
__PARSE_FN__
__VALIDATOR__

function parseJson(text) {
  let t = String(text || '').trim();
  const m = t.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/);
  if (m) t = m[1];
  try {
    return { value: JSON.parse(t) };
  } catch (e) {
    const a = t.indexOf('{');
    const z = t.lastIndexOf('}');
    if (a >= 0 && z > a) {
      try {
        return { value: JSON.parse(t.slice(a, z + 1)) };
      } catch (e2) {
        /* fall through */
      }
    }
    return { error: 'invalid_json' };
  }
}

const p = parseDeepSeek(items[0].json, C, b, '__STEP__', C.prompt_versions.questions);
let validation;
if (!p.content.trim()) {
  validation = { ok: false, errors: ['invalid_json', 'llm_empty_response'], validQuestions: [] };
} else {
  const pj = parseJson(p.content);
  if (pj.error) {
    validation = {
      ok: false,
      errors: ['invalid_json'].concat(p.finish_reason === 'length' ? ['llm_truncated'] : []),
      validQuestions: [],
    };
  } else {
    validation = validateQuestionsPayload(pj.value, {
      termCanonicals: b.term_canonicals || [],
      existingStems: b.existing_stems || b.used_stems || [],
      existingTermAnswers: b.used_term_answers || [],
      minQuestions: C.questions_min || 8,
      maxQuestions: C.questions_max || 12,
      similarityThreshold: C.stem_similarity_threshold || 0.72,
      allowThesis: true,
      thesisOnly: Boolean(b.thesis_mode),
    });
  }
}
return [{
  json: {
    usage_row: p.usage_row,
    cost_exact: p.cost_exact,
    finish_reason: p.finish_reason,
    validation_ok: Boolean(validation.ok),
    errors: validation.errors || [],
    valid_questions: validation.validQuestions || [],
  },
  pairedItem: 0,
}];"""

FINALIZE_Q_JS = r"""__VALIDATOR__
function packQuestions(list, cap, opts) {
  const fin = finalizeQuestions(list, cap, opts || {});
  return fin;
}
"""

DECIDE_Q_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const b = $('LLM — Build Questions Request').item.json;
const p = $('LLM — Parse Questions').item.json;
__FINALIZE__
const minQ = C.questions_min || 8;
const target = C.questions_target || 10;
const maxQ = C.max_questions_per_subtopic || C.questions_max || 12;
const valid = p.valid_questions || [];
const soft = new Set(['lt_min_questions', 'lt_10_questions', 'shortfall_true', 'gt_max_questions']);
const errs = (p.errors || []).filter((e) => !soft.has(e));
const finOpts = {
  existingStems: b.used_stems || [],
  existingTermAnswers: b.used_term_answers || [],
  letterCounts: b.letter_counts || {},
  maxLetterShare: C.max_letter_share || 0.35,
  similarityThreshold: C.stem_similarity_threshold || 0.72,
  seedPrefix: String(b.subtopic_id || b.ordinal || ''),
};
const base = {
  ...b,
  lecture_id: b.lecture_id,
  job_id: b.job_id,
  course_id: b.course_id,
  subtopic_id: b.subtopic_id,
  ordinal: b.ordinal,
  title: b.title,
  term_canonicals: b.term_canonicals,
  spent_month_usd: b.spent_month_usd,
  spent_lecture_usd: b.spent_lecture_usd,
  step: 'questions_retry',
  prompt_version: b.prompt_version,
  first_cost_usd: p.cost_exact,
  first_valid_count: valid.length,
  loop_run_index: b.loop_run_index,
};
if (valid.length >= target) {
  const fin = packQuestions(valid, maxQ, finOpts);
  return [{ json: {
    ...base,
    needs_retry: false,
    questions_json: JSON.stringify(fin.questions),
    question_count: fin.questions.length,
    case_action_share: fin.case_action_share,
    dropped_dupes: (fin.dropped || []).length,
  }, pairedItem: 0 }];
}

// one retry: fix (nothing valid) or top-up (some valid); then accept whatever is valid
const need = Math.max(minQ - valid.length, 1);
let extra = '';
if (errs.length) extra += '\n\nОШИБКИ_ВАЛИДАЦИИ\n' + errs.slice(0, 15).map((e) => '- ' + e).join('\n');
if (valid.length) {
  extra += '\n\nДОБОР\nУже есть ' + valid.length + ' валидных уникальных вопросов. Нужно ещё около '
    + need + ' (цель ' + minQ + '–' + maxQ + ', без клонов). Верни в questions только новые и не повторяй сюжеты:\n'
    + valid.map((q) => '- ' + q.stem).join('\n');
  if ((b.used_stems || []).length) {
    extra += '\nТакже не повторяй УЖЕ_ИСПОЛЬЗОВАНО из других подтем (' + b.used_stems.length + ' шт.).';
  }
} else {
  extra += '\n\nДОБОР\nНужно ' + minQ + '–' + maxQ + ' уникальных вопросов по тезисам этой подтемы. Верни полный исправленный JSON без клонов.';
}
const msgs = b.request_body.messages;
const retryBody = {
  ...b.request_body,
  messages: [msgs[0], { role: 'user', content: b.user_base + extra }],
};
const P = C.prices_usd_per_1m;
const est = Math.ceil((msgs[0].content.length + b.user_base.length + extra.length) / (C.chars_per_token || 2));
const projected = (est * P.miss + C.est_output_tokens_questions * P.out) / 1e6;
const spentLecture = b.spent_lecture_usd + p.cost_exact;
const spentMonth = b.spent_month_usd + p.cost_exact;
const budgetOk = spentLecture + projected <= C.max_cost_usd_per_lecture
  && spentMonth + projected <= C.monthly_budget_usd * C.budget_stop_ratio;
if (!budgetOk) {
  const fin = packQuestions(valid, maxQ, finOpts);
  return [{ json: {
    ...base,
    needs_retry: false,
    retry_skipped: 'budget',
    questions_json: JSON.stringify(fin.questions),
    question_count: fin.questions.length,
    case_action_share: fin.case_action_share,
  }, pairedItem: 0 }];
}
return [{
  json: {
    ...base,
    needs_retry: true,
    existing_stems: (b.used_stems || []).concat(valid.map((q) => q.stem)),
    first_valid: valid,
    first_errors: errs.slice(0, 15),
    request_body: retryBody,
  },
  pairedItem: 0,
}];"""

MERGE_RETRY_JS = r"""const C = $('Run — Init Constants').first().json.constants;
const d = $('LLM — Decide Questions').item.json;
const r = $('LLM — Parse Questions Retry').item.json;
__FINALIZE__
const maxQ = C.max_questions_per_subtopic || C.questions_max || 12;
const merged = (d.first_valid || []).concat(r.valid_questions || []);
const fin = packQuestions(merged, maxQ, {
  existingStems: d.used_stems || [],
  existingTermAnswers: d.used_term_answers || [],
  letterCounts: d.letter_counts || {},
  maxLetterShare: C.max_letter_share || 0.35,
  similarityThreshold: C.stem_similarity_threshold || 0.72,
  seedPrefix: String(d.subtopic_id || d.ordinal || ''),
});
return [{
  json: {
    ...d,
    lecture_id: d.lecture_id,
    job_id: d.job_id,
    course_id: d.course_id,
    subtopic_id: d.subtopic_id,
    ordinal: d.ordinal,
    title: d.title,
    retried: true,
    question_count: fin.questions.length,
    questions_json: JSON.stringify(fin.questions),
    case_action_share: fin.case_action_share,
    dropped_dupes: (fin.dropped || []).length,
  },
  pairedItem: 0,
}];"""

UPSERT_QUESTIONS_SQL = """WITH params AS (
  SELECT $1::uuid AS lecture_id, $2::uuid AS subtopic_id, $3::uuid AS course_id, $4::jsonb AS questions
), src AS (
  SELECT q.*
  FROM params p
  CROSS JOIN LATERAL jsonb_to_recordset(p.questions) AS q(
    ordinal int, source_kind text, term_canonical text, thesis text, stem text,
    choice_a text, choice_b text, choice_c text, choice_d text,
    correct_choice text, explanation text, choice_explanations jsonb
  )
), no_cards AS (
  -- Refuse rewrite only when THIS subtopic already has cards (lecture-wide block removed).
  SELECT NOT EXISTS (
    SELECT 1 FROM psy.cards c
    JOIN psy.questions qq ON qq.id = c.question_id
    WHERE qq.subtopic_id = (SELECT subtopic_id FROM params)
      AND qq.kind = 'life_mcq'
      AND qq.superseded_at IS NULL
  ) AS ok
), gen AS (
  SELECT COALESCE(MAX(q.generation), 0) + 1 AS n
  FROM psy.questions q
  WHERE q.lecture_id = (SELECT lecture_id FROM params)
), up AS (
  INSERT INTO psy.questions (
    lecture_id, subtopic_id, term_id, stem,
    choice_a, choice_b, choice_c, choice_d,
    correct_choice, explanation, choice_explanations, kind, ordinal_in_subtopic,
    generation, superseded_at, source_kind, thesis
  )
  SELECT p.lecture_id, p.subtopic_id, g.id, s.stem,
         s.choice_a, s.choice_b, s.choice_c, s.choice_d,
         s.correct_choice, s.explanation, s.choice_explanations, 'life_mcq', s.ordinal,
         (SELECT n FROM gen), NULL,
         COALESCE(NULLIF(s.source_kind, ''), CASE WHEN s.term_canonical IS NULL OR btrim(s.term_canonical)='' THEN 'thesis' ELSE 'term' END),
         NULLIF(btrim(s.thesis), '')
  FROM params p
  CROSS JOIN src s
  LEFT JOIN psy.glossary_terms g
    ON g.course_id = p.course_id
   AND s.term_canonical IS NOT NULL
   AND btrim(s.term_canonical) <> ''
   AND g.canonical = s.term_canonical
  WHERE (SELECT ok FROM no_cards)
    AND (
      (COALESCE(s.source_kind, 'term') = 'term' AND g.id IS NOT NULL)
      OR (
        COALESCE(s.source_kind, 'thesis') = 'thesis'
        AND NULLIF(btrim(s.thesis), '') IS NOT NULL
      )
    )
  ON CONFLICT (subtopic_id, ordinal_in_subtopic) WHERE (kind = 'life_mcq' AND superseded_at IS NULL)
  DO UPDATE SET
    term_id = EXCLUDED.term_id,
    stem = EXCLUDED.stem,
    choice_a = EXCLUDED.choice_a,
    choice_b = EXCLUDED.choice_b,
    choice_c = EXCLUDED.choice_c,
    choice_d = EXCLUDED.choice_d,
    correct_choice = EXCLUDED.correct_choice,
    explanation = EXCLUDED.explanation,
    choice_explanations = EXCLUDED.choice_explanations,
    generation = EXCLUDED.generation,
    source_kind = EXCLUDED.source_kind,
    thesis = EXCLUDED.thesis
  RETURNING id
), trim_old AS (
  UPDATE psy.questions qd
  SET superseded_at = now()
  FROM params p
  WHERE qd.subtopic_id = p.subtopic_id
    AND qd.kind = 'life_mcq'
    AND qd.superseded_at IS NULL
    AND (SELECT count(*) FROM src) > 0
    AND qd.ordinal_in_subtopic > (SELECT max(ordinal) FROM src)
    AND (SELECT ok FROM no_cards)
  RETURNING qd.id
)
SELECT
  (SELECT count(*) FROM up)::int AS written,
  (SELECT count(*) FROM trim_old)::int AS superseded_trim,
  (SELECT ok FROM no_cards) AS no_cards"""

FINISH_JOB_SQL = """WITH params AS (
  SELECT $1::uuid AS lecture_id, $2::uuid AS job_id, $3::text AS workflow_version,
         $4::int AS terms_count, $5::jsonb AS prompt_versions, $6::boolean AS transcript_truncated
), counts AS (
  SELECT st.ordinal, st.title, count(q.id)::int AS n
  FROM psy.subtopics st
  LEFT JOIN psy.questions q ON q.subtopic_id = st.id
    AND q.kind = 'life_mcq'
    AND q.superseded_at IS NULL
  WHERE st.lecture_id = (SELECT lecture_id FROM params)
  GROUP BY st.id, st.ordinal, st.title
), agg AS (
  SELECT
    COALESCE(sum(n), 0)::int AS total_questions,
    count(*)::int AS subtopics,
    COALESCE(
      jsonb_agg(jsonb_build_object('subtopic_ordinal', ordinal, 'title', title, 'questions', n) ORDER BY ordinal)
        FILTER (WHERE n < 8),
      '[]'::jsonb
    ) AS short_questions
  FROM counts
), usage AS (
  SELECT count(*)::int AS llm_calls, COALESCE(sum(cost_usd), 0)::float8 AS cost_usd
  FROM psy.llm_usage
  WHERE job_id = (SELECT job_id FROM params)
), lec AS (
  UPDATE psy.lectures l
  SET status = 'questions_ready', error = NULL
  FROM params p
  WHERE l.id = p.lecture_id
    AND (SELECT total_questions FROM agg) > 0
    AND l.status IN ('konspekt_ready', 'questions_ready', 'failed')
  RETURNING l.id
), done_job AS (
  UPDATE psy.jobs j
  SET status = CASE WHEN (SELECT total_questions FROM agg) > 0 THEN 'done' ELSE 'failed' END,
      finished_at = now(),
      error = CASE WHEN (SELECT total_questions FROM agg) > 0 THEN NULL ELSE 'no_questions_generated' END,
      attempts = CASE WHEN (SELECT total_questions FROM agg) > 0 THEN j.attempts ELSE GREATEST(j.attempts, 3) END,
      payload = COALESCE(j.payload, '{}'::jsonb) || jsonb_build_object(
        'workflow_version', p.workflow_version,
        'prompt_versions', p.prompt_versions,
        'terms_count', p.terms_count,
        'subtopics_count', (SELECT subtopics FROM agg),
        'questions_count', (SELECT total_questions FROM agg),
        'short_questions', (SELECT short_questions FROM agg),
        'transcript_truncated', p.transcript_truncated,
        'llm_calls', (SELECT llm_calls FROM usage),
        'cost_usd', (SELECT cost_usd FROM usage)
      )
  FROM params p
  WHERE j.id = p.job_id
  RETURNING j.id, j.status
)
SELECT
  (SELECT id FROM lec) AS lecture_id,
  (SELECT id FROM done_job) AS job_id,
  (SELECT status FROM done_job) AS job_status,
  (SELECT total_questions FROM agg) AS questions_count,
  (SELECT subtopics FROM agg) AS subtopics_count,
  (SELECT short_questions FROM agg) AS short_questions,
  (SELECT llm_calls FROM usage) AS llm_calls,
  (SELECT cost_usd FROM usage) AS cost_usd"""

FAIL_EXTRA_05 = r"""if (e.budget_exceeded === true) {
  permanent = true;
  keepLecture = true;
  message = e.error_message || 'budget_exceeded';
} else if (Number(e.cards_count) > 0) {
  permanent = true;
  keepLecture = true;
  message = 'cards exist, regenerate refused';
} else if (e.konspekt_md !== undefined && !String(e.konspekt_md || '').trim() && !e.error) {
  permanent = true;
  keepLecture = true;
  message = 'empty_konspekt: у лекции нет конспекта, LLM не вызывался';
} else if (e.hard_parse_error === true || e.proceed === false) {
  // Temporary terms-extract failure: retry (not attempts=3), keep lecture status.
  permanent = false;
  keepLecture = true;
  message = e.error_message || e.gate_reason || 'terms_extract_failed';
}"""


def build_psy_05() -> tuple[str, list, dict]:
    nodes: list = []
    conn: dict = {}
    sys_terms = js_literal(load_system_prompt("extract_terms_v2.md"))
    sys_q = js_literal(load_system_prompt("gen_questions_v5.md"))
    sys_fill = js_literal(load_system_prompt("fill_term_definitions_v2.md"))
    validator = load_validator_js()
    subtopic_lib = load_subtopic_questions_js()
    finalize_embedded = sub_code(FINALIZE_Q_JS, VALIDATOR=validator)

    nodes.append(sticky("NOTE — In", "## In\nManual + Execute (passthrough). Lower-only overrides: `max_cost_usd_per_lecture`, `monthly_budget_usd`. Inactive.", -40, 40, 360, 140))
    nodes.append(sticky("NOTE — Run/Load", "## Run / Load\nBackfill `terms` jobs for `konspekt_ready` → claim `terms`. Refuse when **active** cards exist (superseded life_mcq ignored).", 380, 40, 460, 140))
    nodes.append(sticky("NOTE — LLM", "## LLM\n`extract_terms_v2` (or skip+load) → fill v2 → `gen_questions_v5` (term|thesis). Kick: `only_subtopic_ordinals`, `skip_terms_extract`.", 1000, 40, 560, 180))
    nodes.append(sticky("NOTE — DB/Fin", "## DB / Fin\nUpsert active life_mcq (term_id nullable for thesis); excess → `superseded_at`. short_questions = n < 8.", 3100, 40, 480, 160))

    LOAD_EXISTING_05 = """SELECT
  $1::uuid AS lecture_id,
  COALESCE((
    SELECT jsonb_agg(jsonb_build_object('ordinal', st.ordinal, 'title', st.title) ORDER BY st.ordinal)
    FROM psy.subtopics st WHERE st.lecture_id = $1::uuid
  ), '[]'::jsonb) AS subtopics,
  COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
      'canonical', g.canonical,
      'aliases', to_jsonb(COALESCE(g.aliases, ARRAY[]::text[])),
      'definition', g.definition,
      'life_example', g.life_example,
      'subtopic_ordinal', 1
    ) ORDER BY g.canonical)
    FROM psy.glossary_terms g
    WHERE g.lecture_id = $1::uuid
  ), '[]'::jsonb) AS terms"""

    n_manual = manual_trigger(0, 300)
    n_exec = exec_trigger(0, 480)
    n_init = code("Run — Init Constants", sub_code(INIT_JS_05, WF=WF_VER["psy_05"]), 220, 380)
    n_backfill = pg(
        "Load — Backfill",
        backfill_sql("konspekt_ready", "terms"),
        440,
        380,
        "={{ [ $('Run — Init Constants').first().json.constants.workflow_version ] }}",
    )
    n_claim = pg(
        "Run — Create Job",
        claim_sql("terms"),
        660,
        380,
        "={{ [ $('Run — Init Constants').first().json.constants.workflow_version ] }}",
    )
    n_has = iff("Run — Has Job", "={{ $json.id }}", "", 880, 380, "notEmpty")
    n_lecture = pg(
        "Load — Lecture",
        LOAD_LECTURE_05,
        1100,
        300,
        "={{ [ $('Run — Create Job').first().json.id ] }}",
    )
    n_no_cards = iff("Load — No Cards", "={{ String($json.cards_count) }}", "0", 1320, 300, "equals")
    n_has_konspekt = iff(
        "Load — Has Konspekt",
        "={{ String($json.konspekt_md || '').trim() }}",
        "",
        1540,
        300,
        "notEmpty",
    )
    n_skip_terms = iff(
        "Run — Skip Terms Extract",
        "={{ String($('Run — Init Constants').first().json.constants.skip_terms_extract) }}",
        "true",
        1650,
        300,
        "equals",
    )
    n_load_existing = pg(
        "Load — Existing Subtopics Terms",
        LOAD_EXISTING_05,
        1760,
        400,
        """={{ [
  $('Load — Lecture').first().json.lecture_id
] }}""",
    )
    n_existing_ready = code(
        "LLM — Existing Ready",
        r"""const L = $('Load — Lecture').first().json;
const row = items[0].json;
let terms = row.terms || [];
if (typeof terms === 'string') { try { terms = JSON.parse(terms); } catch (e) { terms = []; } }
let subtopics = row.subtopics || [];
if (typeof subtopics === 'string') { try { subtopics = JSON.parse(subtopics); } catch (e) { subtopics = []; } }
// Existing glossary rows have no subtopic_ordinal in DB — keep placeholder;
// thesis MCQ for empty-term subtopics does not need own terms.
terms = (Array.isArray(terms) ? terms : []).map((t) => ({
  canonical: t.canonical,
  aliases: t.aliases || [],
  definition: t.definition,
  life_example: t.life_example,
  subtopic_ordinal: Number(t.subtopic_ordinal) || 1,
}));
return [{
  json: {
    ok: true,
    proceed: true,
    subtopics: Array.isArray(subtopics) ? subtopics : [],
    terms,
    lecture_terms_empty: !terms.length,
    course_fallback: false,
    hard_parse_error: false,
    gate_reason: 'skip_terms_extract',
    transcript_truncated: false,
  },
  pairedItem: 0,
}];""",
        1980,
        400,
    )
    n_build_terms = code(
        "LLM — Build Terms Request",
        sub_code(BUILD_TERMS_JS, SYS_TERMS=sys_terms),
        1760,
        220,
    )
    n_budget_terms = iff("LLM — Budget OK Terms", "={{ String($json.budget_exceeded) }}", "false", 1980, 220, "equals")
    n_http_terms = deepseek("Shared — DeepSeek Terms", 2200, 140)
    n_parse_terms = code(
        "LLM — Parse Terms",
        sub_code(PARSE_TERMS_JS, PARSE_FN=PARSE_DEEPSEEK_JS, SUBTOPIC_LIB=subtopic_lib),
        2420,
        140,
    )
    n_log_terms = log_usage("DB — Log Usage Terms", 2640, 140)
    n_terms_ready = code(
        "LLM — Terms Ready",
        sub_code(
            r"""__SUBTOPIC_LIB__
const parsed = $('LLM — Parse Terms').item.json;
const L = $('Load — Lecture').first().json;
const gate = finalizeTermsExtractGate({
  parseOk: Boolean(parsed.ok),
  errorMessage: parsed.error_message || null,
  subtopics: parsed.subtopics || [],
  terms: parsed.terms || [],
  courseTerms: L.course_terms || [],
  lectureTitle: L.title || '',
});
return [{
  json: {
    ...parsed,
    ok: gate.proceed,
    proceed: gate.proceed,
    subtopics: gate.subtopics,
    terms: gate.terms,
    lecture_terms_empty: gate.lecture_terms_empty,
    course_fallback: gate.course_fallback,
    hard_parse_error: gate.hard_parse_error,
    gate_reason: gate.reason,
    error_message: gate.proceed ? parsed.error_message : (gate.reason || parsed.error_message),
  },
  pairedItem: 0,
}];""",
            SUBTOPIC_LIB=subtopic_lib,
        ),
        2860,
        140,
    )
    # proceed=false on hard parse → Fin — Fail Context (retryable, keep_lecture).
    n_terms_ok = iff("Run — Terms Proceed", "={{ String($json.proceed) }}", "true", 3080, 140, "equals")
    n_merge_thin = code(
        "LLM — Merge Thin Subtopics",
        sub_code(MERGE_THIN_JS, SUBTOPIC_LIB=subtopic_lib),
        3180,
        140,
    )
    n_needs_fill = iff(
        "Run — Needs Fill Defs",
        "={{ String($json.needs_fill) }}",
        "true",
        3300,
        140,
        "equals",
    )
    n_build_fill = code(
        "LLM — Build Fill Defs",
        sub_code(
            r"""__SUBTOPIC_LIB__
const C = $('Run — Init Constants').first().json.constants;
const L = $('Load — Lecture').first().json;
const M = items[0].json;
const SYSTEM = __SYS_FILL__;
const cpt = C.chars_per_token || 2;
const est = (s) => Math.ceil(String(s || '').length / cpt);
const empty = M.empty_definitions || [];
const termsBlock = empty.map((t) => {
  const al = (t.aliases || []).length ? ' (' + t.aliases.join(', ') + ')' : '';
  return '- ' + t.canonical + al;
}).join('\n');
const user = [
  'ТЕРМИНЫ_БЕЗ_ОПРЕДЕЛЕНИЯ\n' + termsBlock,
  'ФРАГМЕНТ КОНСПЕКТА\n' + String(L.konspekt_md || '').slice(0, 12000),
  'ФРАГМЕНТ ТРАНСКРИПТА\n' + String(L.transcript || '').slice(0, 12000),
].join('\n\n');
const P = C.prices_usd_per_1m;
const inTokens = est(SYSTEM) + est(user);
const projected = (inTokens * P.miss + (C.est_output_tokens_fill || 2500) * P.out) / 1e6;
const spentLecture = Number(L.spent_lecture_usd) || 0;
const spentMonth = Number(L.spent_month_usd) || 0;
const lectureCap = C.max_cost_usd_per_lecture;
const monthCap = C.monthly_budget_usd * C.budget_stop_ratio;
let reason = null;
if (spentLecture + projected > lectureCap) {
  reason = 'budget_exceeded: лимит на лекцию перед fill_definitions';
} else if (spentMonth + projected > monthCap) {
  reason = 'budget_exceeded: 80% месяца перед fill_definitions';
}
if (reason) {
  return [{ json: { ...M, skip_fill: true, budget_exceeded: true, error_message: reason }, pairedItem: 0 }];
}
return [{
  json: {
    ...M,
    skip_fill: false,
    budget_exceeded: false,
    lecture_id: L.lecture_id,
    job_id: L.job_id,
    course_id: L.course_id,
    step: 'fill_definitions',
    prompt_version: C.prompt_versions.fill_definitions,
    request_body: {
      model: C.llm_model,
      messages: [
        { role: 'system', content: SYSTEM },
        { role: 'user', content: user },
      ],
      thinking: C.thinking,
      response_format: { type: 'json_object' },
      max_tokens: Math.min(C.max_output_tokens, 4000),
      temperature: C.temperature_terms,
      stream: false,
    },
  },
  pairedItem: 0,
}];""",
            SUBTOPIC_LIB=subtopic_lib,
            SYS_FILL=sys_fill,
        ),
        3420,
        40,
    )
    n_budget_fill = iff(
        "LLM — Budget OK Fill",
        "={{ String($json.budget_exceeded) }}",
        "false",
        3540,
        40,
        "equals",
    )
    n_http_fill = deepseek("Shared — DeepSeek Fill Defs", 3660, 40)
    n_parse_fill = code(
        "LLM — Parse Fill Defs",
        sub_code(
            r"""__SUBTOPIC_LIB__
const C = $('Run — Init Constants').first().json.constants;
const src = $('LLM — Build Fill Defs').first().json;
__PARSE_FN__
const p = parseDeepSeek(items[0].json, C, src, 'fill_definitions', C.prompt_versions.fill_definitions);
function parseJson(text) {
  let t = String(text || '').trim();
  const m = t.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/);
  if (m) t = m[1];
  try { return { value: JSON.parse(t) }; } catch (e) {
    const a = t.indexOf('{'); const b = t.lastIndexOf('}');
    if (a >= 0 && b > a) {
      try { return { value: JSON.parse(t.slice(a, b + 1)) }; } catch (e2) { /* fall */ }
    }
    return { error: 'invalid_json' };
  }
}
let applied = { terms: src.terms || [], applied: [] };
if (p.content.trim()) {
  const pj = parseJson(p.content);
  if (!pj.error) applied = applyFilledDefinitions(src.terms || [], pj.value);
}
return [{
  json: {
    ...src,
    terms: applied.terms,
    definitions_filled_llm: applied.applied,
    usage_row: p.usage_row,
    cost_exact: p.cost_exact,
  },
  pairedItem: 0,
}];""",
            SUBTOPIC_LIB=subtopic_lib,
            PARSE_FN=PARSE_DEEPSEEK_JS,
        ),
        3780,
        40,
    )
    n_log_fill = log_usage("DB — Log Usage Fill Defs", 3900, 40)
    n_after_fill = code(
        "LLM — After Fill Defs",
        r"""const filled = $('LLM — Parse Fill Defs').first().json;
const sd = $getWorkflowStaticData('global');
sd.filledTerms = filled.terms || [];
sd.fillSkipped = false;
return [{ json: { ...filled, fill_skipped: false }, pairedItem: 0 }];""",
        4020,
        40,
    )
    n_skip_fill_pass = code(
        "LLM — Skip Fill Pass",
        r"""const M = $('LLM — Merge Thin Subtopics').first().json;
const sd = $getWorkflowStaticData('global');
sd.filledTerms = M.terms || [];
sd.fillSkipped = true;
return [{ json: { ...M, definitions_filled_llm: [], fill_skipped: true }, pairedItem: 0 }];""",
        3420,
        220,
    )
    n_up_terms = pg(
        "DB — Upsert Terms",
        UPSERT_TERMS_SQL,
        4240,
        140,
        """={{ [
  $('Load — Lecture').first().json.course_id,
  $('Load — Lecture').first().json.lecture_id,
  JSON.stringify($json.terms)
] }}""",
    )
    n_up_subs = pg(
        "DB — Upsert Subtopics",
        UPSERT_SUBTOPICS_SQL,
        4460,
        140,
        """={{ [
  $('Load — Lecture').first().json.lecture_id,
  JSON.stringify($('LLM — Merge Thin Subtopics').first().json.subtopics)
] }}""",
    )
    n_filter_subs = code(
        "Run — Filter Subtopics",
        r"""const C = $('Run — Init Constants').first().json.constants;
const only = C.only_subtopic_ordinals;
const items = $('DB — Upsert Subtopics').all().map((i) => i.json);
const list = (!only || !only.length)
  ? items
  : items.filter((row) => only.includes(Number(row.ordinal)));
if (!list.length) {
  return [{ json: { skip_all: true, ordinal: 0, title: '', subtopic_id: null }, pairedItem: 0 }];
}
return list.map((row, idx) => ({ json: row, pairedItem: idx }));""",
        4580,
        140,
    )
    n_loop = splitter("Run — Loop Subtopics", 4700, 140)
    n_spend = pg(
        "DB — Load Spend",
        LOAD_SPEND_SQL,
        4060,
        220,
        """={{ [
  $json.subtopic_id || $json.id,
  $json.ordinal,
  $json.title,
  $('Load — Lecture').first().json.lecture_id
] }}""",
    )
    n_build_q = code(
        "LLM — Build Questions Request",
        sub_code(BUILD_QUESTIONS_JS, SYS_Q=sys_q, SUBTOPIC_LIB=subtopic_lib),
        4280,
        220,
    )
    n_should_gen = iff(
        "Run — Should Generate",
        "={{ String($json.skip) }}",
        "false",
        4400,
        220,
        "equals",
    )
    n_note_no_terms = pg(
        "DB — Note No Terms",
        NOTE_NO_TERMS_SQL,
        4400,
        400,
        """={{ [
  Number($json.ordinal),
  $json.title,
  $json.job_id,
  $json.skip_reason || 'no_terms_or_theses'
] }}""",
    )
    n_budget_q = iff("LLM — Budget OK Questions", "={{ String($json.budget_exceeded) }}", "false", 4620, 140, "equals")
    n_http_q = deepseek("Shared — DeepSeek Questions", 4840, 140)
    n_parse_q = code(
        "LLM — Parse Questions",
        sub_code(
            PARSE_QUESTIONS_JS,
            META_NODE="LLM — Build Questions Request",
            STEP="questions",
            PARSE_FN=PARSE_DEEPSEEK_JS,
            VALIDATOR=validator,
        ),
        4840,
        140,
    )
    n_log_q = log_usage("DB — Log Usage Questions", 5060, 140)
    n_decide = code(
        "LLM — Decide Questions",
        sub_code(DECIDE_Q_JS, FINALIZE=finalize_embedded),
        5280,
        140,
    )
    n_needs_retry = iff("LLM — Needs Retry", "={{ String($json.needs_retry) }}", "true", 5500, 140, "equals")
    n_http_retry = deepseek("Shared — DeepSeek Questions Retry", 5720, 40)
    n_parse_retry = code(
        "LLM — Parse Questions Retry",
        sub_code(
            PARSE_QUESTIONS_JS,
            META_NODE="LLM — Decide Questions",
            STEP="questions_retry",
            PARSE_FN=PARSE_DEEPSEEK_JS,
            VALIDATOR=validator,
        ),
        5940,
        40,
    )
    n_log_retry = log_usage("DB — Log Usage Questions Retry", 6160, 40)
    n_merge = code(
        "LLM — Merge Retry",
        sub_code(MERGE_RETRY_JS, FINALIZE=finalize_embedded),
        6380,
        40,
    )
    n_up_q = pg(
        "DB — Upsert Questions",
        UPSERT_QUESTIONS_SQL,
        6600,
        220,
        """={{ [
  $json.lecture_id,
  $json.subtopic_id,
  $json.course_id,
  $json.questions_json
] }}""",
    )
    n_finish = pg(
        "DB — Finish Job",
        FINISH_JOB_SQL,
        4060,
        0,
        """={{ [
  $('Load — Lecture').first().json.lecture_id,
  $('Run — Create Job').first().json.id,
  $('Run — Init Constants').first().json.constants.workflow_version,
  $('LLM — Merge Thin Subtopics').first().json.terms.length,
  JSON.stringify($('Run — Init Constants').first().json.constants.prompt_versions),
  Boolean($('LLM — Merge Thin Subtopics').first().json.transcript_truncated)
] }}""",
    )
    n_finish["executeOnce"] = True
    # Shift LLM chain further right after Should Generate / Note No Terms.
    for n in (
        n_budget_q, n_http_q, n_parse_q,
        n_log_q, n_decide, n_needs_retry, n_http_retry, n_parse_retry, n_log_retry, n_merge, n_up_q,
    ):
        n["position"][0] += 440
    n_fail_ctx, n_fail = fail_nodes(1980, 600, FAIL_EXTRA_05)

    nodes.extend([
        n_manual, n_exec, n_init, n_backfill, n_claim, n_has, n_lecture, n_no_cards, n_has_konspekt,
        n_skip_terms, n_load_existing, n_existing_ready,
        n_build_terms, n_budget_terms, n_http_terms, n_parse_terms, n_log_terms, n_terms_ready, n_terms_ok,
        n_merge_thin, n_needs_fill, n_build_fill, n_budget_fill, n_http_fill, n_parse_fill, n_log_fill,
        n_after_fill, n_skip_fill_pass, n_up_terms, n_up_subs, n_filter_subs, n_loop, n_spend, n_build_q,
        n_should_gen, n_note_no_terms, n_budget_q, n_http_q, n_parse_q, n_log_q, n_decide, n_needs_retry,
        n_http_retry, n_parse_retry, n_log_retry, n_merge, n_up_q, n_finish, n_fail_ctx, n_fail,
    ])

    link(conn, "In — Manual", "Run — Init Constants")
    link(conn, "In — Execute", "Run — Init Constants")
    link(conn, "Run — Init Constants", "Load — Backfill")
    link(conn, "Load — Backfill", "Run — Create Job")
    link(conn, "Run — Create Job", "Run — Has Job")
    link(conn, "Run — Has Job", "Load — Lecture", 0)
    link(conn, "Load — Lecture", "Load — No Cards")
    link(conn, "Load — No Cards", "Load — Has Konspekt", 0)
    link(conn, "Load — No Cards", "Fin — Fail Context", 1)
    link(conn, "Load — Has Konspekt", "Run — Skip Terms Extract", 0)
    link(conn, "Load — Has Konspekt", "Fin — Fail Context", 1)
    # skip_terms_extract=true → load existing; false → extract_terms LLM
    link(conn, "Run — Skip Terms Extract", "Load — Existing Subtopics Terms", 0)
    link(conn, "Run — Skip Terms Extract", "LLM — Build Terms Request", 1)
    link(conn, "Load — Existing Subtopics Terms", "LLM — Existing Ready")
    link(conn, "LLM — Existing Ready", "LLM — Merge Thin Subtopics")
    link(conn, "LLM — Build Terms Request", "LLM — Budget OK Terms")
    link(conn, "LLM — Budget OK Terms", "Shared — DeepSeek Terms", 0)
    link(conn, "LLM — Budget OK Terms", "Fin — Fail Context", 1)
    link(conn, "Shared — DeepSeek Terms", "LLM — Parse Terms", 0)
    link(conn, "Shared — DeepSeek Terms", "Fin — Fail Context", 1)
    link(conn, "LLM — Parse Terms", "DB — Log Usage Terms")
    link(conn, "DB — Log Usage Terms", "LLM — Terms Ready")
    link(conn, "LLM — Terms Ready", "Run — Terms Proceed")
    link(conn, "Run — Terms Proceed", "LLM — Merge Thin Subtopics", 0)
    link(conn, "Run — Terms Proceed", "Fin — Fail Context", 1)
    link(conn, "LLM — Merge Thin Subtopics", "Run — Needs Fill Defs")
    # needs_fill=true → LLM fill; false → skip pass. Budget fail on fill → skip (keep heuristic defs).
    link(conn, "Run — Needs Fill Defs", "LLM — Build Fill Defs", 0)
    link(conn, "Run — Needs Fill Defs", "LLM — Skip Fill Pass", 1)
    link(conn, "LLM — Build Fill Defs", "LLM — Budget OK Fill")
    link(conn, "LLM — Budget OK Fill", "Shared — DeepSeek Fill Defs", 0)
    link(conn, "LLM — Budget OK Fill", "LLM — Skip Fill Pass", 1)
    link(conn, "Shared — DeepSeek Fill Defs", "LLM — Parse Fill Defs", 0)
    link(conn, "Shared — DeepSeek Fill Defs", "LLM — Skip Fill Pass", 1)
    link(conn, "LLM — Parse Fill Defs", "DB — Log Usage Fill Defs")
    link(conn, "DB — Log Usage Fill Defs", "LLM — After Fill Defs")
    link(conn, "LLM — After Fill Defs", "DB — Upsert Terms")
    link(conn, "LLM — Skip Fill Pass", "DB — Upsert Terms")
    link(conn, "DB — Upsert Terms", "DB — Upsert Subtopics")
    link(conn, "DB — Upsert Subtopics", "Run — Filter Subtopics")
    link(conn, "Run — Filter Subtopics", "Run — Loop Subtopics")
    link(conn, "Run — Loop Subtopics", "DB — Finish Job", 0)
    link(conn, "Run — Loop Subtopics", "DB — Load Spend", 1)
    link(conn, "DB — Load Spend", "LLM — Build Questions Request")
    link(conn, "LLM — Build Questions Request", "Run — Should Generate")
    # skip=false → generate; skip=true → note no_terms_available, no DeepSeek
    link(conn, "Run — Should Generate", "LLM — Budget OK Questions", 0)
    link(conn, "Run — Should Generate", "DB — Note No Terms", 1)
    link(conn, "DB — Note No Terms", "Run — Loop Subtopics")
    link(conn, "LLM — Budget OK Questions", "Shared — DeepSeek Questions", 0)
    link(conn, "LLM — Budget OK Questions", "Fin — Fail Context", 1)
    link(conn, "Shared — DeepSeek Questions", "LLM — Parse Questions", 0)
    link(conn, "Shared — DeepSeek Questions", "Fin — Fail Context", 1)
    link(conn, "LLM — Parse Questions", "DB — Log Usage Questions")
    link(conn, "DB — Log Usage Questions", "LLM — Decide Questions")
    link(conn, "LLM — Decide Questions", "LLM — Needs Retry")
    link(conn, "LLM — Needs Retry", "Shared — DeepSeek Questions Retry", 0)
    link(conn, "LLM — Needs Retry", "DB — Upsert Questions", 1)
    link(conn, "Shared — DeepSeek Questions Retry", "LLM — Parse Questions Retry", 0)
    link(conn, "Shared — DeepSeek Questions Retry", "Fin — Fail Context", 1)
    link(conn, "LLM — Parse Questions Retry", "DB — Log Usage Questions Retry")
    link(conn, "DB — Log Usage Questions Retry", "LLM — Merge Retry")
    link(conn, "LLM — Merge Retry", "DB — Upsert Questions")
    link(conn, "DB — Upsert Questions", "Run — Loop Subtopics")
    link(conn, "Fin — Fail Context", "Fin — Fail Job")
    return "psy_05_terms_questions", nodes, conn


# ======================================================================================
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=["psy_05", "all"],
        default="all",
        help="Deploy only psy_05 to avoid regenerating psy_03/04 node ids",
    )
    args = parser.parse_args()
    results: dict[str, str] = {}

    slug5, nodes5, conn5 = build_psy_05()
    id5 = ensure_workflow(slug5, nodes5, conn5)
    save_local(slug5, id5, slug5, nodes5, conn5)
    results[slug5] = id5

    if args.only == "psy_05":
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    slug4, nodes4, conn4 = build_psy_04(id5)
    id4 = ensure_workflow(slug4, nodes4, conn4)
    save_local(slug4, id4, slug4, nodes4, conn4)
    results[slug4] = id4

    slug3, nodes3, conn3 = build_psy_03(id4)
    id3 = ensure_workflow(slug3, nodes3, conn3)
    if id3 != PSY_03_ID:
        print(json.dumps({"warning": "psy_03 id differs from expected", "expected": PSY_03_ID, "actual": id3}))
    save_local(slug3, id3, slug3, nodes3, conn3)
    results[slug3] = id3

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
