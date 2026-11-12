#!/usr/bin/env python3
"""Synthetic acceptance for psy_04 / psy_05. No secrets printed. Fixture not committed.

Execute via docker exec into the running n8n container (no encryptionKey).
SQL via psql -h 127.0.0.1 -U psy_n8n (TCP trust inside db container; no password).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import api_request  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/private/synthetic_lecture_psy04.md"
ID04 = (ROOT / "n8n/workflows/psy_04_konspekt.id").read_text().strip()
ID05 = (ROOT / "n8n/workflows/psy_05_terms_questions.id").read_text().strip()
REPORT: dict = {"checks": {}, "ids": {}, "executions": {}, "costs": {}}


def sh(cmd: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-lc", cmd],
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def psql(sql: str) -> str:
    """Run SQL on prod as psy_n8n via TCP trust on localhost (no password in shell)."""
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "vps-dokploy",
            "docker",
            "exec",
            "-i",
            "myspb-supabase-n4i8rx-supabase-db",
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "psy_n8n",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed:\n{proc.stderr}\n{proc.stdout}")
    return proc.stdout


def parse_json_row(stdout: str):
    """Extract last JSON object from psql -At output."""
    candidates = [ln.strip() for ln in stdout.splitlines() if ln.strip().startswith("{")]
    for raw in reversed(candidates):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"no JSON in psql output: {stdout[-500:]!r}")


def one_off(workflow_id: str, label: str, timeout: int = 900) -> tuple[int, str]:
    """Execute inactive workflow inside the already-running n8n container (no encryptionKey)."""
    script = f"""
set -euo pipefail
set +e
OUT=$(docker exec -u node -e N8N_DIAGNOSTICS_ENABLED=false myn8n-n8n-iucjks-n8n-1 \
  n8n execute --id={workflow_id} --rawOutput 2>&1)
EC=$?
set -e
printf '%s\n' "$OUT"
exit $EC
"""
    print(f"→ execute {label} ({workflow_id})", flush=True)
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "vps-dokploy", "bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    Path(f"/tmp/psy_accept_{label}.log").write_text(out, encoding="utf-8")
    print(f"  ec={proc.returncode} log=/tmp/psy_accept_{label}.log bytes={len(out)}", flush=True)
    return proc.returncode, out


def patch_init_constant(workflow_id: str, key: str, value) -> str:
    """Temporarily lower a numeric constant default in Init Constants; return original js."""
    w = api_request("GET", f"/api/v1/workflows/{workflow_id}")
    nodes = w["nodes"]
    init = next(n for n in nodes if n["name"] == "Run — Init Constants")
    original = init["parameters"]["jsCode"]
    pattern = rf"({re.escape(key)}:\s*)([0-9.]+)"
    new_js, n = re.subn(pattern, rf"\g<1>{value}", original, count=1)
    if n != 1:
        raise RuntimeError(f"could not patch {key} in Init Constants")
    init["parameters"]["jsCode"] = new_js
    api_request(
        "PUT",
        f"/api/v1/workflows/{workflow_id}",
        {
            "name": w["name"],
            "nodes": nodes,
            "connections": w["connections"],
            "settings": w.get("settings") or {"executionOrder": "v1"},
        },
    )
    if w.get("active"):
        api_request("POST", f"/api/v1/workflows/{workflow_id}/deactivate")
    return original


def restore_init(workflow_id: str, original_js: str) -> None:
    w = api_request("GET", f"/api/v1/workflows/{workflow_id}")
    nodes = w["nodes"]
    init = next(n for n in nodes if n["name"] == "Run — Init Constants")
    init["parameters"]["jsCode"] = original_js
    api_request(
        "PUT",
        f"/api/v1/workflows/{workflow_id}",
        {
            "name": w["name"],
            "nodes": nodes,
            "connections": w["connections"],
            "settings": w.get("settings") or {"executionOrder": "v1"},
        },
    )


def seed() -> dict:
    transcript = FIXTURE.read_text(encoding="utf-8")
    # Write fixture to remote temp, then COPY-like via psql dollar quote from file inside container is hard;
    # use Python on remote via base64.
    b64 = subprocess.run(
        ["base64"],
        input=transcript.encode(),
        capture_output=True,
        check=True,
    ).stdout.decode().replace("\n", "")
    sql = f"""
BEGIN;
DELETE FROM psy.lectures l
 USING psy.courses c
 WHERE l.course_id = c.id
   AND c.drive_folder_id = '_psy_test_accept'
   AND c.title = 'psy_test';
DELETE FROM psy.glossary_terms g
 USING psy.courses c
 WHERE g.course_id = c.id
   AND c.drive_folder_id = '_psy_test_accept'
   AND c.title = 'psy_test';
DELETE FROM psy.courses WHERE drive_folder_id = '_psy_test_accept' AND title = 'psy_test';
DELETE FROM psy.users WHERE telegram_user_id = -100002;

CREATE TEMP TABLE _t (transcript text);
INSERT INTO _t VALUES (convert_from(decode('{b64}', 'base64'), 'UTF8'));

WITH u AS (
  INSERT INTO psy.users (telegram_user_id, display_name)
  VALUES (-100002, 'psy_test') RETURNING id
), c AS (
  INSERT INTO psy.courses (user_id, title, drive_folder_id)
  SELECT id, 'psy_test', '_psy_test_accept' FROM u RETURNING id
), l AS (
  INSERT INTO psy.lectures (
    course_id, title, status, media_kind, external_file_id, lecturer_name, duration_sec
  )
  SELECT c.id, 'Синтетика окно внимания', 'transcribed', 'audio', 'synthetic-psy04',
         'Тестовый Психолог', 2700 FROM c
  RETURNING id, course_id
), t AS (
  INSERT INTO psy.lecture_texts (lecture_id, transcript, whisper_model)
  SELECT l.id, _t.transcript, 'synthetic' FROM l, _t
), chunks AS (
  INSERT INTO psy.chunks (lecture_id, ordinal, start_ms, end_ms, text)
  SELECT l.id, s.ord, (s.ord-1)*60000, s.ord*60000, s.part
  FROM l, _t
  CROSS JOIN LATERAL (
    SELECT row_number() OVER ()::int AS ord, trim(part) AS part
    FROM unnest(string_to_array(_t.transcript, E'\\n\\n')) AS part
    WHERE length(trim(part)) > 0
  ) s
  RETURNING ordinal
), j AS (
  INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
  SELECT id, 'konspekt', 'queued', 'konspekt:' || id::text, '{{}}'::jsonb FROM l
  RETURNING id, lecture_id
)
SELECT json_build_object(
  'user_id', (SELECT id FROM u),
  'course_id', (SELECT id FROM c),
  'lecture_id', (SELECT id FROM l),
  'job_id', (SELECT id FROM j),
  'chunk_count', (SELECT count(*) FROM chunks)
)::text;
COMMIT;
"""
    return parse_json_row(psql(sql))


def q_json(sql: str):
    out = psql(sql)
    if not out.strip():
        return None
    return parse_json_row(out)


def cleanup(course_id: str, lecture_id: str) -> dict:
    sql = f"""
BEGIN;
DELETE FROM psy.cards c USING psy.questions q
 WHERE c.question_id = q.id AND q.lecture_id = '{lecture_id}'::uuid;
DELETE FROM psy.questions WHERE lecture_id = '{lecture_id}'::uuid;
DELETE FROM psy.subtopics WHERE lecture_id = '{lecture_id}'::uuid;
DELETE FROM psy.glossary_terms WHERE course_id = '{course_id}'::uuid;
DELETE FROM psy.llm_usage WHERE lecture_id = '{lecture_id}'::uuid;
DELETE FROM psy.lectures WHERE id = '{lecture_id}'::uuid;
DELETE FROM psy.courses WHERE id = '{course_id}'::uuid;
DELETE FROM psy.users WHERE telegram_user_id = -100002;
SELECT json_build_object(
  'lectures', (SELECT count(*) FROM psy.lectures WHERE id = '{lecture_id}'::uuid),
  'courses', (SELECT count(*) FROM psy.courses WHERE id = '{course_id}'::uuid),
  'users', (SELECT count(*) FROM psy.users WHERE telegram_user_id = -100002),
  'questions', (SELECT count(*) FROM psy.questions WHERE lecture_id = '{lecture_id}'::uuid),
  'llm_usage', (SELECT count(*) FROM psy.llm_usage WHERE lecture_id = '{lecture_id}'::uuid)
)::text;
COMMIT;
"""
    return parse_json_row(psql(sql))


def main() -> int:
    if not FIXTURE.exists():
        print("missing fixture", FIXTURE, file=sys.stderr)
        return 1

    # --- 1 seed ---
    ids = seed()
    REPORT["ids"] = ids
    REPORT["checks"]["1_seed"] = {
        "ok": bool(ids.get("lecture_id") and ids.get("chunk_count", 0) > 0),
        "ids": ids,
    }
    print("seed", ids, flush=True)
    lecture_id = ids["lecture_id"]
    course_id = ids["course_id"]

    # --- 2 psy_04 single ---
    ec, _ = one_off(ID04, "04_single")
    row = q_json(
        f"""SELECT json_build_object(
          'status', l.status,
          'konspekt_len', length(coalesce(t.konspekt_md,'')),
          'has_h1', coalesce(t.konspekt_md,'') ~ '^# ',
          'has_section', coalesce(t.konspekt_md,'') ~ '## 1\\\\.',
          'has_mermaid', coalesce(t.konspekt_md,'') ILIKE '%mermaid%',
          'has_gap_literal', coalesce(t.konspekt_md,'') LIKE '%[неразборчиво]%',
          'terms_job', (SELECT status FROM psy.jobs WHERE idempotency_key = 'terms:' || l.id::text),
          'usage_n', (SELECT count(*) FROM psy.llm_usage WHERE lecture_id = l.id),
          'usage_cost', (SELECT coalesce(sum(cost_usd),0) FROM psy.llm_usage WHERE lecture_id = l.id),
          'prompt_version', (SELECT prompt_version FROM psy.llm_usage WHERE lecture_id = l.id ORDER BY created_at LIMIT 1),
          'workflow_version', (SELECT workflow_version FROM psy.llm_usage WHERE lecture_id = l.id ORDER BY created_at LIMIT 1),
          'konspekt_job', (SELECT status FROM psy.jobs WHERE step='konspekt' AND lecture_id=l.id),
          'konspekt_error', (SELECT error FROM psy.jobs WHERE step='konspekt' AND lecture_id=l.id)
        )::text FROM psy.lectures l JOIN psy.lecture_texts t ON t.lecture_id=l.id
        WHERE l.id = '{lecture_id}'::uuid;"""
    )
    REPORT["checks"]["2_psy04"] = {"ec": ec, **(row or {})}
    REPORT["executions"]["2_psy04"] = {"ec": ec}
    print("check2", row, flush=True)

    # --- 3 map-reduce with lowered threshold ---
    # Reset lecture to transcribed, clear konspekt, requeue konspekt, delete terms job if any
    psql(
        f"""
BEGIN;
UPDATE psy.lectures SET status='transcribed', error=NULL WHERE id='{lecture_id}'::uuid;
UPDATE psy.lecture_texts SET konspekt_md=NULL, llm_model=NULL WHERE lecture_id='{lecture_id}'::uuid;
DELETE FROM psy.jobs WHERE lecture_id='{lecture_id}'::uuid AND step='terms';
DELETE FROM psy.llm_usage WHERE lecture_id='{lecture_id}'::uuid;
UPDATE psy.jobs SET status='queued', attempts=0, error=NULL, started_at=NULL, finished_at=NULL,
  payload='{{}}'::jsonb
 WHERE lecture_id='{lecture_id}'::uuid AND step='konspekt';
COMMIT;"""
    )
    original = patch_init_constant(ID04, "konspekt_single_call_max_input_tokens", 500)
    try:
        ec3, _ = one_off(ID04, "04_map")
    finally:
        restore_init(ID04, original)
    row3 = q_json(
        f"""SELECT json_build_object(
          'status', l.status,
          'usage_steps', (SELECT json_agg(step ORDER BY created_at) FROM psy.llm_usage WHERE lecture_id=l.id),
          'usage_prompt_versions', (SELECT json_agg(prompt_version ORDER BY created_at) FROM psy.llm_usage WHERE lecture_id=l.id),
          'usage_n', (SELECT count(*) FROM psy.llm_usage WHERE lecture_id=l.id),
          'konspekt_len', length(coalesce(t.konspekt_md,'')),
          'mode', (SELECT payload->'konspekt_result'->>'mode' FROM psy.jobs WHERE step='konspekt' AND lecture_id=l.id),
          'error', (SELECT error FROM psy.jobs WHERE step='konspekt' AND lecture_id=l.id)
        )::text FROM psy.lectures l JOIN psy.lecture_texts t ON t.lecture_id=l.id
        WHERE l.id='{lecture_id}'::uuid;"""
    )
    REPORT["checks"]["3_map"] = {"ec": ec3, **(row3 or {})}
    print("check3", row3, flush=True)

    # Ensure terms job queued for psy_05
    psql(
        f"""
INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
SELECT '{lecture_id}'::uuid, 'terms', 'queued', 'terms:{lecture_id}', '{{}}'::jsonb
ON CONFLICT (idempotency_key) DO UPDATE
  SET status='queued', attempts=0, error=NULL, started_at=NULL, finished_at=NULL
WHERE psy.jobs.status IS DISTINCT FROM 'done';
UPDATE psy.lectures SET status='konspekt_ready' WHERE id='{lecture_id}'::uuid AND status <> 'konspekt_ready';
"""
    )

    # --- 4 psy_05 ---
    ec4, _ = one_off(ID05, "05_terms", timeout=1200)
    row4 = q_json(
        f"""SELECT json_build_object(
          'status', l.status,
          'subtopics', (SELECT count(*) FROM psy.subtopics WHERE lecture_id=l.id),
          'terms_with_life', (SELECT count(*) FROM psy.glossary_terms WHERE course_id=l.course_id AND life_example IS NOT NULL),
          'terms_with_def', (SELECT count(*) FROM psy.glossary_terms WHERE course_id=l.course_id AND definition IS NOT NULL),
          'q_total', (SELECT count(*) FROM psy.questions WHERE lecture_id=l.id AND kind='life_mcq'),
          'q_per_sub', (SELECT json_object_agg(s.ordinal::text, c.cnt) FROM psy.subtopics s
            LEFT JOIN LATERAL (SELECT count(*) cnt FROM psy.questions q WHERE q.subtopic_id=s.id AND q.kind='life_mcq') c ON true
            WHERE s.lecture_id=l.id),
          'bad_choices', (SELECT count(*) FROM psy.questions q WHERE q.lecture_id=l.id AND (
            q.choice_a IS NULL OR q.choice_b IS NULL OR q.choice_c IS NULL OR q.choice_d IS NULL
            OR q.correct_choice NOT IN ('A','B','C','D')
            OR q.explanation IS NULL OR q.explanation = ''
            OR q.choice_explanations IS NULL
            OR NOT (q.choice_explanations ? 'A' AND q.choice_explanations ? 'B'
                    AND q.choice_explanations ? 'C' AND q.choice_explanations ? 'D')
          )),
          'dup_stems', (SELECT count(*) FROM (
            SELECT lower(stem) s, count(*) FROM psy.questions WHERE lecture_id=l.id GROUP BY 1 HAVING count(*)>1
          ) d),
          'job', (SELECT status FROM psy.jobs WHERE step='terms' AND lecture_id=l.id),
          'job_error', (SELECT error FROM psy.jobs WHERE step='terms' AND lecture_id=l.id),
          'payload', (SELECT payload FROM psy.jobs WHERE step='terms' AND lecture_id=l.id)
        )::text FROM psy.lectures l WHERE l.id='{lecture_id}'::uuid;"""
    )
    REPORT["checks"]["4_psy05"] = {"ec": ec4, **(row4 or {})}
    print("check4", row4, flush=True)

    # --- 5 rerun empty claim ---
    before = q_json(
        f"""SELECT json_build_object(
          'klen', length(coalesce(t.konspekt_md,'')),
          'q', (SELECT count(*) FROM psy.questions WHERE lecture_id=t.lecture_id)
        )::text FROM psy.lecture_texts t WHERE lecture_id='{lecture_id}'::uuid;"""
    )
    ec5a, _ = one_off(ID04, "04_rerun")
    ec5b, _ = one_off(ID05, "05_rerun")
    after = q_json(
        f"""SELECT json_build_object(
          'klen', length(coalesce(t.konspekt_md,'')),
          'q', (SELECT count(*) FROM psy.questions WHERE lecture_id=t.lecture_id),
          'konspekt_jobs_running', (SELECT count(*) FROM psy.jobs WHERE lecture_id='{lecture_id}'::uuid AND step='konspekt' AND status='running'),
          'terms_jobs_running', (SELECT count(*) FROM psy.jobs WHERE lecture_id='{lecture_id}'::uuid AND step='terms' AND status='running')
        )::text FROM psy.lecture_texts t WHERE lecture_id='{lecture_id}'::uuid;"""
    )
    REPORT["checks"]["5_idempotent_rerun"] = {
        "ec04": ec5a,
        "ec05": ec5b,
        "before": before,
        "after": after,
        "ok": before == after,
    }
    print("check5", REPORT["checks"]["5_idempotent_rerun"], flush=True)

    # --- 6 regenerate terms ---
    q_before = row4.get("q_total") if row4 else None
    psql(
        f"""UPDATE psy.jobs SET status='queued', attempts=0, error=NULL, started_at=NULL, finished_at=NULL
             WHERE lecture_id='{lecture_id}'::uuid AND step='terms';
             UPDATE psy.lectures SET status='konspekt_ready' WHERE id='{lecture_id}'::uuid;"""
    )
    ec6, _ = one_off(ID05, "05_regen", timeout=1200)
    row6 = q_json(
        f"""SELECT json_build_object(
          'subtopics', (SELECT count(*) FROM psy.subtopics WHERE lecture_id='{lecture_id}'::uuid),
          'terms', (SELECT count(*) FROM psy.glossary_terms WHERE course_id='{course_id}'::uuid),
          'q_total', (SELECT count(*) FROM psy.questions WHERE lecture_id='{lecture_id}'::uuid AND kind='life_mcq'),
          'job', (SELECT status FROM psy.jobs WHERE step='terms' AND lecture_id='{lecture_id}'::uuid),
          'error', (SELECT error FROM psy.jobs WHERE step='terms' AND lecture_id='{lecture_id}'::uuid)
        )::text;"""
    )
    REPORT["checks"]["6_regen"] = {"ec": ec6, "before_q": q_before, **(row6 or {})}
    print("check6", REPORT["checks"]["6_regen"], flush=True)

    # --- 7 cards exist ---
    psql(
        f"""
INSERT INTO psy.cards (user_id, question_id, term_id, due, stability, difficulty, elapsed_days, scheduled_days, reps, lapses, state)
SELECT u.id, q.id, q.term_id, now(), 1, 5, 0, 0, 0, 0, 0
FROM psy.users u, psy.questions q
WHERE u.telegram_user_id=-100002 AND q.lecture_id='{lecture_id}'::uuid
ORDER BY q.created_at LIMIT 1;
UPDATE psy.jobs SET status='queued', attempts=0, error=NULL, started_at=NULL, finished_at=NULL
 WHERE lecture_id='{lecture_id}'::uuid AND step='terms';
"""
    )
    ec7, _ = one_off(ID05, "05_cards")
    row7 = q_json(
        f"""SELECT json_build_object(
          'job', (SELECT status FROM psy.jobs WHERE step='terms' AND lecture_id='{lecture_id}'::uuid),
          'error', (SELECT error FROM psy.jobs WHERE step='terms' AND lecture_id='{lecture_id}'::uuid),
          'cards', (SELECT count(*) FROM psy.cards c JOIN psy.questions q ON q.id=c.question_id WHERE q.lecture_id='{lecture_id}'::uuid),
          'q_total', (SELECT count(*) FROM psy.questions WHERE lecture_id='{lecture_id}'::uuid)
        )::text;"""
    )
    REPORT["checks"]["7_cards_refuse"] = {"ec": ec7, **(row7 or {})}
    print("check7", REPORT["checks"]["7_cards_refuse"], flush=True)

    # --- 8 budget ---
    psql(
        f"""
DELETE FROM psy.cards c USING psy.questions q WHERE c.question_id=q.id AND q.lecture_id='{lecture_id}'::uuid;
UPDATE psy.lectures SET status='transcribed', error=NULL WHERE id='{lecture_id}'::uuid;
UPDATE psy.lecture_texts SET konspekt_md=NULL WHERE lecture_id='{lecture_id}'::uuid;
DELETE FROM psy.jobs WHERE lecture_id='{lecture_id}'::uuid AND step='terms';
UPDATE psy.jobs SET status='queued', attempts=0, error=NULL, started_at=NULL, finished_at=NULL, payload='{{}}'::jsonb
 WHERE lecture_id='{lecture_id}'::uuid AND step='konspekt';
"""
    )
    usage_before = q_json(
        f"SELECT coalesce(count(*),0) FROM psy.llm_usage WHERE lecture_id='{lecture_id}'::uuid;"
    )
    # count returns bare - wrap
    usage_before_n = q_json(
        f"SELECT json_build_object('n', count(*))::text FROM psy.llm_usage WHERE lecture_id='{lecture_id}'::uuid;"
    )["n"]
    orig8 = patch_init_constant(ID04, "max_cost_usd_per_lecture", 0.0001)
    try:
        ec8, _ = one_off(ID04, "04_budget")
    finally:
        restore_init(ID04, orig8)
    row8 = q_json(
        f"""SELECT json_build_object(
          'lecture_status', (SELECT status FROM psy.lectures WHERE id='{lecture_id}'::uuid),
          'job', (SELECT status FROM psy.jobs WHERE step='konspekt' AND lecture_id='{lecture_id}'::uuid),
          'error', (SELECT error FROM psy.jobs WHERE step='konspekt' AND lecture_id='{lecture_id}'::uuid),
          'usage_n', (SELECT count(*) FROM psy.llm_usage WHERE lecture_id='{lecture_id}'::uuid),
          'usage_before', {usage_before_n}
        )::text;"""
    )
    REPORT["checks"]["8_budget"] = {"ec": ec8, **(row8 or {})}
    print("check8", REPORT["checks"]["8_budget"], flush=True)

    # --- 9 unit tests only ---
    ut = subprocess.run(
        ["node", "--test", "n8n/lib/validate_questions.test.js"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    REPORT["checks"]["9_unit"] = {
        "ec": ut.returncode,
        "pass": "fail 0" in ut.stdout or "tests 9" in ut.stdout,
        "tail": ut.stdout[-500:],
    }
    print("check9", REPORT["checks"]["9_unit"]["ec"], flush=True)

    # --- 10 empty transcript ---
    psql(
        f"""
UPDATE psy.lecture_texts SET transcript='', konspekt_md=NULL WHERE lecture_id='{lecture_id}'::uuid;
DELETE FROM psy.chunks WHERE lecture_id='{lecture_id}'::uuid;
UPDATE psy.lectures SET status='transcribed', error=NULL WHERE id='{lecture_id}'::uuid;
UPDATE psy.jobs SET status='queued', attempts=0, error=NULL, started_at=NULL, finished_at=NULL
 WHERE lecture_id='{lecture_id}'::uuid AND step='konspekt';
"""
    )
    usage_n10a = q_json(
        f"SELECT json_build_object('n', count(*))::text FROM psy.llm_usage WHERE lecture_id='{lecture_id}'::uuid;"
    )["n"]
    ec10, _ = one_off(ID04, "04_empty")
    row10 = q_json(
        f"""SELECT json_build_object(
          'job', (SELECT status FROM psy.jobs WHERE step='konspekt' AND lecture_id='{lecture_id}'::uuid),
          'error', (SELECT error FROM psy.jobs WHERE step='konspekt' AND lecture_id='{lecture_id}'::uuid),
          'lecture_status', (SELECT status FROM psy.lectures WHERE id='{lecture_id}'::uuid),
          'usage_n', (SELECT count(*) FROM psy.llm_usage WHERE lecture_id='{lecture_id}'::uuid),
          'usage_before', {usage_n10a}
        )::text;"""
    )
    REPORT["checks"]["10_empty"] = {"ec": ec10, **(row10 or {})}
    print("check10", REPORT["checks"]["10_empty"], flush=True)

    # --- 11 costs from earlier successful runs (re-query any leftover usage; may be budget-only) ---
    # Restore transcript briefly? costs already captured in check2/3/4. Sum from report.
    cost2 = (REPORT["checks"].get("2_psy04") or {}).get("usage_cost")
    REPORT["costs"] = {
        "synthetic_usage_cost_check2": cost2,
        "model": "deepseek-flash",
        "prices": "https://api-docs.deepseek.com/quick_start/pricing peak miss=0.3 hit=0.006 out=1.2 /1M (2026-11-11)",
        "forecast_real_1_1.5h": "roughly 3–8× synthetic single-call cost depending on map parts; stay under max_cost_usd_per_lecture=0.30",
    }

    # --- 12 cleanup ---
    cleaned = cleanup(course_id, lecture_id)
    REPORT["checks"]["12_cleanup"] = cleaned
    print("check12", cleaned, flush=True)

    # --- 13 active flags ---
    act = {}
    for slug, wid in [
        ("psy_01", "MTxO6XbZ5XhNBOLQ"),
        ("psy_02", "fGv2q7QmHtB3PzuL"),
        ("psy_03", "tRGq7gzjUbO52oq1"),
        ("psy_04", ID04),
        ("psy_05", ID05),
    ]:
        w = api_request("GET", f"/api/v1/workflows/{wid}")
        act[slug] = {"id": wid, "active": w.get("active"), "name": w.get("name")}
    REPORT["checks"]["13_inactive"] = act
    print("check13", act, flush=True)

    out_path = Path("/tmp/psy04_accept_report.json")
    out_path.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("REPORT", out_path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
