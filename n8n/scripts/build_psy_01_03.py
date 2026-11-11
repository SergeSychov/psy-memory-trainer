#!/usr/bin/env python3
"""Build and deploy inactive psy_01 / psy_02 / psy_03 workflows. No secrets in output."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from _env import WORKFLOWS, api_request

PG = {"postgres": {"id": "fVodKFRegu3319i0", "name": "psy Supabase (psy_n8n)"}}
GDRIVE = {"googleDriveOAuth2Api": {"id": "S7mhg7CBYGpInlHx", "name": "Google Drive account"}}
WHISPER = {"httpBearerAuth": {"id": "WeUa8vWm5ugn7AeW", "name": "psy Whisper token"}}

WHISPER_BASE = "http://psy-whisper:8080"
WF_VER = {
    "psy_01": "psy_01_ingest_cloud_folder_v1",
    "psy_02": "psy_02_extract_audio_v1",
    "psy_03": "psy_03_transcribe_v2",
}


def nid() -> str:
    return str(uuid.uuid4())


def sticky(name: str, content: str, x: int, y: int, w: int = 280, h: int = 140, color: int = 5) -> dict:
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.stickyNote",
        "typeVersion": 1,
        "position": [x, y],
        "parameters": {"content": content, "width": w, "height": h, "color": color},
    }


def code(name: str, js: str, x: int, y: int) -> dict:
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [x, y],
        "parameters": {"jsCode": js},
    }


def pg(name: str, query: str, x: int, y: int, query_replacement: str | None = None) -> dict:
    options: dict = {}
    if query_replacement is not None:
        options["queryReplacement"] = query_replacement
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.6,
        "position": [x, y],
        "parameters": {
            "operation": "executeQuery",
            "query": query,
            "options": options,
        },
        "credentials": PG,
    }


def http(
    name: str,
    *,
    method: str,
    url: str,
    x: int,
    y: int,
    body: str | None = None,
    auth: str = "none",
    creds: dict | None = None,
    node_cred_type: str | None = None,
    generic_auth: str | None = None,
    timeout_ms: int = 300000,
    on_error: str | None = None,
) -> dict:
    params: dict = {
        "method": method,
        "url": url,
        "authentication": auth,
        "sendBody": body is not None,
        "options": {"timeout": timeout_ms},
    }
    if body is not None:
        params["contentType"] = "json"
        params["specifyBody"] = "json"
        params["jsonBody"] = body
    if auth == "predefinedCredentialType" and node_cred_type:
        params["nodeCredentialType"] = node_cred_type
    if auth == "genericCredentialType" and generic_auth:
        params["genericAuthType"] = generic_auth
    node = {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.5,
        "position": [x, y],
        "parameters": params,
    }
    if creds:
        node["credentials"] = creds
    if on_error:
        node["onError"] = on_error
    return node


def iff(name: str, left: str, right: str, x: int, y: int, op: str = "notEmpty") -> dict:
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [x, y],
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [
                    {
                        "id": nid(),
                        "leftValue": left,
                        "rightValue": right,
                        "operator": {"type": "string", "operation": op},
                    }
                ],
                "combinator": "and",
            }
        },
    }


def link(connections: dict, src: str, dst: str, src_output: int = 0) -> None:
    connections.setdefault(src, {}).setdefault("main", [])
    while len(connections[src]["main"]) <= src_output:
        connections[src]["main"].append([])
    connections[src]["main"][src_output].append({"node": dst, "type": "main", "index": 0})


def ensure_workflow(name: str, nodes: list, connections: dict) -> str:
    # find existing by name
    cursor = None
    while True:
        path = "/api/v1/workflows?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        page = api_request("GET", path)
        for w in page.get("data") or []:
            if w.get("name") == name:
                wid = w["id"]
                api_request(
                    "PUT",
                    f"/api/v1/workflows/{wid}",
                    {
                        "name": name,
                        "nodes": nodes,
                        "connections": connections,
                        "settings": {"executionOrder": "v1"},
                    },
                )
                if w.get("active"):
                    api_request("POST", f"/api/v1/workflows/{wid}/deactivate")
                return wid
        cursor = page.get("nextCursor")
        if not cursor:
            break
    created = api_request(
        "POST",
        "/api/v1/workflows",
        {
            "name": name,
            "nodes": nodes,
            "connections": connections,
            "settings": {"executionOrder": "v1"},
        },
    )
    wid = created["id"]
    # deactivate if API created active (should be inactive)
    detail = api_request("GET", f"/api/v1/workflows/{wid}")
    if detail.get("active"):
        api_request("POST", f"/api/v1/workflows/{wid}/deactivate")
    return wid


def save_local(slug: str, workflow_id: str, name: str, nodes: list, connections: dict) -> None:
    WORKFLOWS.mkdir(parents=True, exist_ok=True)
    (WORKFLOWS / f"{slug}.id").write_text(workflow_id + "\n", encoding="utf-8")
    payload = {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "active": False,
    }
    (WORKFLOWS / f"{slug}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


INIT_JS_01 = f"""return items.map((item, index) => {{
  const constants = {{
    workflow_version: '{WF_VER["psy_01"]}',
    whisper_base: '{WHISPER_BASE}',
    drive_list_depth: 1,
    skip_folder_prefix: '_',
  }};
  const course_id = item.json.course_id || item.json.query?.course_id || null;
  return {{
    json: {{ ...item.json, course_id, constants }},
    pairedItem: index,
  }};
}});"""


FLATTEN_JS = """const constants = $('Run — Init Constants').first().json.constants || {
  workflow_version: 'psy_01_ingest_cloud_folder_v1',
  skip_folder_prefix: '_',
};
const course = $('Load — Courses').item.json;
const files = items[0].json.files || [];
const prefix = constants.skip_folder_prefix || '_';
const out = [];
for (const f of files) {
  const path = f.folder_path || [];
  if (path.some((p) => String(p).startsWith(prefix))) continue;
  const name = f.name || '';
  const ext = (name.includes('.') ? name.split('.').pop() : 'bin').toLowerCase();
  out.push({
    json: {
      constants,
      course_id: course.id,
      file_id: f.id,
      title: name,
      media_kind: f.media_kind,
      duration_sec: f.duration_ms != null ? Math.round(Number(f.duration_ms) / 1000) : null,
      ext,
      size_bytes: f.size_bytes,
      source_meta_drive: {
        name: f.name,
        mime_type: f.mime_type,
        size_bytes: f.size_bytes,
        md5: f.md5,
        folder_path: path,
        modified_time: f.modified_time,
      },
    },
    pairedItem: 0,
  });
}
return out;"""


def build_psy_01(psy_02_id: str) -> tuple[str, list, dict]:
    nodes = []
    conn: dict = {}
    nodes.append(sticky("NOTE — In", "## In\nSchedule 15m + Manual + Execute(`course_id`). Inactive.", -200, 40))
    nodes.append(sticky("NOTE — Load/DB", "## Load / DB\nDrive list-media, skip `_` folders, upsert lecture, enqueue extract.", 600, 40, 360))
    nodes.append(sticky("NOTE — Fin", "## Fin\nKick psy_02 once per new enqueue (wait=false).", 1800, 40, 280))

    n_sched = {
        "id": nid(),
        "name": "In — Schedule",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [0, 240],
        "parameters": {
            "rule": {"interval": [{"field": "cronExpression", "expression": "*/15 * * * *"}]}
        },
    }
    n_manual = {
        "id": nid(),
        "name": "In — Manual",
        "type": "n8n-nodes-base.manualTrigger",
        "typeVersion": 1,
        "position": [0, 420],
        "parameters": {},
    }
    n_exec = {
        "id": nid(),
        "name": "In — Execute",
        "type": "n8n-nodes-base.executeWorkflowTrigger",
        "typeVersion": 1.1,
        "position": [0, 600],
        "parameters": {
            "inputSource": "workflowInputs",
            "workflowInputs": {
                "values": [{"name": "course_id"}],
            },
        },
    }
    n_init = code("Run — Init Constants", INIT_JS_01, 220, 420)
    n_courses = pg(
        "Load — Courses",
        """SELECT id, user_id, title, drive_folder_id
FROM psy.courses
WHERE drive_folder_id IS NOT NULL
  AND ($1::uuid IS NULL OR id = $1::uuid)""",
        440,
        420,
        "={{ [ $('Run — Init Constants').first().json.course_id || null ] }}",
    )
    n_drive = http(
        "Load — Drive Media",
        method="POST",
        url=f"={{{{ $('Run — Init Constants').first().json.constants.whisper_base || '{WHISPER_BASE}' }}}}/drive/list-media",
        x=660,
        y=420,
        body="={{ JSON.stringify({ folder_id: $json.drive_folder_id, depth: ($('Run — Init Constants').first().json.constants.drive_list_depth || 1) }) }}",
        auth="predefinedCredentialType",
        node_cred_type="googleDriveOAuth2Api",
        creds=GDRIVE,
        timeout_ms=120000,
        on_error="continueErrorOutput",
    )
    n_flat = code("Load — Flatten Media", FLATTEN_JS, 880, 420)
    n_upsert = pg(
        "DB — Upsert Lecture",
        """INSERT INTO psy.lectures (
  course_id, external_file_id, title, media_kind, duration_sec, status, source_meta
) VALUES (
  $1::uuid, $2, $3, $4, $5, 'queued',
  jsonb_build_object('drive', $6::jsonb)
)
ON CONFLICT (course_id, external_file_id) DO NOTHING
RETURNING id, course_id, external_file_id, status""",
        1100,
        420,
        """={{ [
  $json.course_id,
  $json.file_id,
  $json.title,
  $json.media_kind,
  $json.duration_sec,
  JSON.stringify($json.source_meta_drive || {})
] }}""",
    )
    n_has = iff("DB — Has New Lecture", "={{ $json.id }}", "", 1320, 420, "notEmpty")
    n_enq = pg(
        "DB — Enqueue Extract",
        """INSERT INTO psy.jobs (
  lecture_id, step, status, idempotency_key, payload
) VALUES (
  $1::uuid, 'extract_audio', 'queued', 'extract_audio:' || $1::text,
  jsonb_build_object(
    'file_id', $2,
    'ext', $3,
    'size_bytes', $4,
    'workflow_version', $5
  )
)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id, lecture_id, step, status""",
        1540,
        300,
        """={{ [
  $json.id,
  $('Load — Flatten Media').item.json.file_id,
  $('Load — Flatten Media').item.json.ext,
  $('Load — Flatten Media').item.json.size_bytes,
  $('Run — Init Constants').first().json.constants.workflow_version
] }}""",
    )
    n_kick = {
        "id": nid(),
        "name": "Fin — Kick psy_02",
        "type": "n8n-nodes-base.executeWorkflow",
        "typeVersion": 1.2,
        "position": [1760, 300],
        "parameters": {
            "workflowId": {"__rl": True, "mode": "id", "value": psy_02_id},
            "options": {"waitForSubWorkflow": False},
        },
        "onError": "continueRegularOutput",
    }

    nodes.extend([n_sched, n_manual, n_exec, n_init, n_courses, n_drive, n_flat, n_upsert, n_has, n_enq, n_kick])
    for t in ("In — Schedule", "In — Manual", "In — Execute"):
        link(conn, t, "Run — Init Constants")
    link(conn, "Run — Init Constants", "Load — Courses")
    link(conn, "Load — Courses", "Load — Drive Media")
    link(conn, "Load — Drive Media", "Load — Flatten Media")
    link(conn, "Load — Flatten Media", "DB — Upsert Lecture")
    link(conn, "DB — Upsert Lecture", "DB — Has New Lecture")
    link(conn, "DB — Has New Lecture", "DB — Enqueue Extract", 0)
    link(conn, "DB — Enqueue Extract", "Fin — Kick psy_02")
    return "psy_01_ingest_cloud_folder", nodes, conn


INIT_JS_02 = f"""return items.map((item, index) => {{
  const constants = {{
    workflow_version: '{WF_VER["psy_02"]}',
    whisper_base: '{WHISPER_BASE}',
    max_attempts: 3,
    stale_running_hours: 4,
  }};
  return {{ json: {{ ...item.json, constants }}, pairedItem: index }};
}});"""


CLAIM_02 = """UPDATE psy.jobs j
SET status = 'running',
    attempts = attempts + 1,
    started_at = now(),
    error = NULL,
    payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object('workflow_version', $1, 'claimed_at', now())
WHERE j.id = (
  SELECT id FROM psy.jobs
  WHERE step = 'extract_audio'
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


def build_psy_02(psy_03_id: str) -> tuple[str, list, dict]:
    nodes = []
    conn: dict = {}
    nodes.append(sticky("NOTE — Run", "## Run\nClaim one extract_audio job (SKIP LOCKED).", -40, 40))
    nodes.append(sticky("NOTE — Audio", "## Audio\nHealth → Drive fetch → extract. 409 → requeue.", 700, 40, 320))
    nodes.append(sticky("NOTE — Fin", "## Fin\nAudio ready + enqueue transcribe + kick psy_03. Fail path cleans inbox.", 1700, 40, 340))

    n_manual = {"id": nid(), "name": "In — Manual", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [0, 300], "parameters": {}}
    n_exec = {"id": nid(), "name": "In — Execute", "type": "n8n-nodes-base.executeWorkflowTrigger", "typeVersion": 1, "position": [0, 480], "parameters": {}}
    n_init = code("Run — Init Constants", INIT_JS_02, 220, 360)
    n_claim = pg(
        "Run — Create Job",
        CLAIM_02,
        440,
        360,
        "={{ [ $('Run — Init Constants').first().json.constants.workflow_version ] }}",
    )
    n_has = iff("Run — Has Job", "={{ $json.id }}", "", 660, 360, "notEmpty")
    n_health = http(
        "Audio — Check Health",
        method="GET",
        url=f"={{{{ $('Run — Init Constants').first().json.constants.whisper_base || '{WHISPER_BASE}' }}}}/health",
        x=880,
        y=300,
        timeout_ms=30000,
    )
    n_busy = {
        "id": nid(),
        "name": "Audio — Is Busy",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [1100, 300],
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [
                    {
                        "id": nid(),
                        "leftValue": "={{ String($json.busy) }}",
                        "rightValue": "true",
                        "operator": {"type": "string", "operation": "equals"},
                    }
                ],
                "combinator": "and",
            }
        },
    }
    n_requeue_busy = pg(
        "Audio — Requeue Busy",
        """UPDATE psy.jobs
SET status = 'queued',
    attempts = GREATEST(attempts - 1, 0),
    error = 'whisper busy (409)',
    started_at = NULL
WHERE id = $1::uuid
RETURNING id, status, attempts""",
        1320,
        160,
        "={{ [ $('Run — Create Job').item.json.id ] }}",
    )
    n_prep = code(
        "Audio — Prep Paths",
        """const job = $('Run — Create Job').item.json;
const constants = $('Run — Init Constants').first().json.constants;
const payload = typeof job.payload === 'string' ? JSON.parse(job.payload) : (job.payload || {});
const ext = payload.ext || 'bin';
const lecture_id = job.lecture_id;
const inbox_path = `inbox/${lecture_id}.${ext}`;
const audio_path = `audio/${lecture_id}.16k.opus`;
return [{
  json: {
    ...job,
    constants,
    payload,
    file_id: payload.file_id,
    ext,
    lecture_id,
    inbox_path,
    audio_path,
  },
  pairedItem: 0,
}];""",
        1320,
        360,
    )
    n_fetch = http(
        "Audio — Fetch From Drive",
        method="POST",
        url=f"={{{{ $json.constants.whisper_base || '{WHISPER_BASE}' }}}}/drive/fetch",
        x=1540,
        y=360,
        body="={{ JSON.stringify({ file_id: $json.file_id, output_path: $json.inbox_path }) }}",
        auth="predefinedCredentialType",
        node_cred_type="googleDriveOAuth2Api",
        creds=GDRIVE,
        timeout_ms=3600000,
        on_error="continueErrorOutput",
    )
    n_extract = http(
        "Audio — Extract",
        method="POST",
        url=f"={{{{ $('Audio — Prep Paths').item.json.constants.whisper_base || '{WHISPER_BASE}' }}}}/extract-audio",
        x=1760,
        y=300,
        body="={{ JSON.stringify({ input_path: $('Audio — Prep Paths').item.json.inbox_path, output_path: $('Audio — Prep Paths').item.json.audio_path, delete_input: true }) }}",
        auth="genericCredentialType",
        generic_auth="httpBearerAuth",
        creds=WHISPER,
        timeout_ms=3600000,
        on_error="continueErrorOutput",
    )
    n_ready = pg(
        "DB — Audio Ready",
        """WITH lec AS (
  UPDATE psy.lectures
  SET status = 'audio_ready',
      duration_sec = COALESCE($2::int, duration_sec),
      source_meta = COALESCE(source_meta, '{}'::jsonb) || jsonb_build_object(
        'audio', jsonb_build_object('path', $3, 'duration_sec', $2)
      ),
      error = NULL
  WHERE id = $1::uuid
  RETURNING id
), done_job AS (
  UPDATE psy.jobs
  SET status = 'done', finished_at = now(), error = NULL
  WHERE id = $4::uuid
  RETURNING id
), enq AS (
  INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
  VALUES (
    $1::uuid, 'transcribe', 'queued', 'transcribe:' || $1::text,
    jsonb_build_object('audio_path', $3, 'workflow_version', $5)
  )
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
)
SELECT (SELECT id FROM lec) AS lecture_id,
       (SELECT id FROM done_job) AS job_id,
       (SELECT id FROM enq) AS transcribe_job_id""",
        1980,
        300,
        """={{ [
  $('Audio — Prep Paths').item.json.lecture_id,
  Math.round(Number($json.duration_sec || 0)),
  $('Audio — Prep Paths').item.json.audio_path,
  $('Audio — Prep Paths').item.json.id,
  $('Run — Init Constants').first().json.constants.workflow_version
] }}""",
    )
    n_kick = {
        "id": nid(),
        "name": "Fin — Kick psy_03",
        "type": "n8n-nodes-base.executeWorkflow",
        "typeVersion": 1.2,
        "position": [2200, 300],
        "parameters": {
            "workflowId": {"__rl": True, "mode": "id", "value": psy_03_id},
            "options": {"waitForSubWorkflow": False},
        },
        "onError": "continueRegularOutput",
    }
    n_fail_ctx = code(
        "Fin — Fail Context",
        """const job = $('Run — Create Job').item.json;
const prep = (() => { try { return $('Audio — Prep Paths').item.json; } catch (e) { return {}; } })();
const errItem = items[0].json || {};
const statusCode = errItem.statusCode || errItem.httpCode || errItem.error?.status || null;
const message = errItem.message || errItem.error?.message || errItem.error || JSON.stringify(errItem).slice(0, 500);
return [{
  json: {
    job_id: job.id,
    lecture_id: job.lecture_id || prep.lecture_id,
    attempts: job.attempts,
    inbox_path: prep.inbox_path || null,
    status_code: statusCode,
    error_message: String(message),
    constants: $('Run — Init Constants').first().json.constants,
  },
  pairedItem: 0,
}];""",
        1760,
        560,
    )
    n_fail_sql = pg(
        "Fin — Fail Job",
        """WITH j AS (
  UPDATE psy.jobs
  SET status = CASE
        WHEN $2::int = 409 THEN 'queued'
        WHEN attempts >= 3 THEN 'failed'
        ELSE 'queued'
      END,
      attempts = CASE WHEN $2::int = 409 THEN GREATEST(attempts - 1, 0) ELSE attempts END,
      error = $3,
      finished_at = CASE WHEN $2::int <> 409 AND attempts >= 3 THEN now() ELSE NULL END,
      started_at = CASE WHEN $2::int = 409 OR attempts < 3 THEN NULL ELSE started_at END
  WHERE id = $1::uuid
  RETURNING id, status, attempts, lecture_id
), lec AS (
  UPDATE psy.lectures
  SET status = 'failed', error = $3
  WHERE id = (SELECT lecture_id FROM j)
    AND (SELECT status FROM j) = 'failed'
  RETURNING id
)
SELECT * FROM j""",
        1980,
        560,
        """={{ [
  $json.job_id,
  Number($json.status_code || 0),
  $json.error_message
] }}""",
    )
    n_cleanup = http(
        "Fin — Cleanup Inbox",
        method="POST",
        url=f"={{{{ $('Fin — Fail Context').item.json.constants.whisper_base || '{WHISPER_BASE}' }}}}/files/delete",
        x=2200,
        y=560,
        body="={{ JSON.stringify({ path: $('Fin — Fail Context').item.json.inbox_path }) }}",
        auth="genericCredentialType",
        generic_auth="httpBearerAuth",
        creds=WHISPER,
        timeout_ms=60000,
        on_error="continueRegularOutput",
    )

    nodes.extend([
        n_manual, n_exec, n_init, n_claim, n_has, n_health, n_busy, n_requeue_busy,
        n_prep, n_fetch, n_extract, n_ready, n_kick, n_fail_ctx, n_fail_sql, n_cleanup,
    ])
    link(conn, "In — Manual", "Run — Init Constants")
    link(conn, "In — Execute", "Run — Init Constants")
    link(conn, "Run — Init Constants", "Run — Create Job")
    link(conn, "Run — Create Job", "Run — Has Job")
    link(conn, "Run — Has Job", "Audio — Check Health", 0)
    link(conn, "Audio — Check Health", "Audio — Is Busy")
    link(conn, "Audio — Is Busy", "Audio — Requeue Busy", 0)
    link(conn, "Audio — Is Busy", "Audio — Prep Paths", 1)
    link(conn, "Audio — Prep Paths", "Audio — Fetch From Drive")
    link(conn, "Audio — Fetch From Drive", "Audio — Extract", 0)
    link(conn, "Audio — Fetch From Drive", "Fin — Fail Context", 1)
    link(conn, "Audio — Extract", "DB — Audio Ready", 0)
    link(conn, "Audio — Extract", "Fin — Fail Context", 1)
    link(conn, "DB — Audio Ready", "Fin — Kick psy_03")
    link(conn, "Fin — Fail Context", "Fin — Fail Job")
    link(conn, "Fin — Fail Job", "Fin — Cleanup Inbox")
    return "psy_02_extract_audio", nodes, conn


INIT_JS_03 = f"""return items.map((item, index) => {{
  const constants = {{
    workflow_version: '{WF_VER["psy_03"]}',
    whisper_base: '{WHISPER_BASE}',
    language: 'ru',
  }};
  return {{ json: {{ ...item.json, constants }}, pairedItem: index }};
}});"""


CLAIM_03 = """UPDATE psy.jobs j
SET status = 'running',
    attempts = attempts + 1,
    started_at = now(),
    error = NULL,
    payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object('workflow_version', $1, 'claimed_at', now())
WHERE j.id = (
  SELECT id FROM psy.jobs
  WHERE step = 'transcribe'
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


def build_psy_03(psy_04_id: str | None = None) -> tuple[str, list, dict]:
    """psy_03_transcribe v2: + enqueue konspekt job and (when psy_04_id is known) Fin — Kick psy_04."""
    nodes = []
    conn: dict = {}
    nodes.append(sticky("NOTE — ASR", "## ASR\nClaim transcribe job, glossary → initial_prompt, whisper.", -40, 40, 360))
    nodes.append(
        sticky(
            "NOTE — DB",
            "## DB\nIdempotent lecture_texts + chunks upsert. Keep opus.\nv2: enqueue `konspekt:<lecture_id>` + kick psy_04 (wait=false).",
            1400,
            40,
            360,
        )
    )

    n_manual = {"id": nid(), "name": "In — Manual", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [0, 300], "parameters": {}}
    n_exec = {"id": nid(), "name": "In — Execute", "type": "n8n-nodes-base.executeWorkflowTrigger", "typeVersion": 1, "position": [0, 480], "parameters": {}}
    n_init = code("Run — Init Constants", INIT_JS_03, 220, 360)
    n_claim = pg(
        "Run — Create Job",
        CLAIM_03,
        440,
        360,
        "={{ [ $('Run — Init Constants').first().json.constants.workflow_version ] }}",
    )
    n_has = iff("Run — Has Job", "={{ $json.id }}", "", 660, 360, "notEmpty")
    n_gloss = pg(
        "Load — Glossary",
        """SELECT
  j.id AS job_id,
  j.lecture_id,
  j.attempts,
  j.payload,
  l.course_id,
  COALESCE(j.payload->>'audio_path', 'audio/' || l.id::text || '.16k.opus') AS audio_path,
  (
    SELECT string_agg(g.canonical, ', ' ORDER BY g.canonical)
    FROM psy.glossary_terms g
    WHERE g.course_id = l.course_id
  ) AS initial_prompt
FROM psy.jobs j
JOIN psy.lectures l ON l.id = j.lecture_id
WHERE j.id = $1::uuid""",
        880,
        300,
        "={{ [ $('Run — Create Job').item.json.id ] }}",
    )
    n_prep = code(
        "ASR — Prep Request",
        """const row = items[0].json;
const constants = $('Run — Init Constants').first().json.constants;
const prompt = row.initial_prompt;
const body = {
  audio_path: row.audio_path,
  language: constants.language || 'ru',
};
if (prompt) body.initial_prompt = prompt;
return [{
  json: {
    ...row,
    constants,
    request_body: body,
    has_initial_prompt: Boolean(prompt),
  },
  pairedItem: 0,
}];""",
        1100,
        300,
    )
    n_asr = http(
        "ASR — Transcribe",
        method="POST",
        url=f"={{{{ $json.constants.whisper_base || '{WHISPER_BASE}' }}}}/transcribe",
        x=1320,
        y=300,
        body="={{ JSON.stringify($json.request_body) }}",
        auth="genericCredentialType",
        generic_auth="httpBearerAuth",
        creds=WHISPER,
        timeout_ms=10800000,
        on_error="continueErrorOutput",
    )
    n_save = pg(
        "DB — Save Transcript",
        """WITH params AS (
  SELECT
    $1::uuid AS lecture_id,
    $2::uuid AS job_id,
    $3::text AS transcript,
    $4::text AS whisper_model,
    $5::int AS duration_sec,
    $6::jsonb AS segments,
    $7::text AS workflow_version
), upsert_text AS (
  INSERT INTO psy.lecture_texts (lecture_id, transcript, whisper_model)
  SELECT lecture_id, transcript, whisper_model FROM params
  ON CONFLICT (lecture_id) DO UPDATE
    SET transcript = EXCLUDED.transcript,
        whisper_model = EXCLUDED.whisper_model
  RETURNING lecture_id
), upsert_chunks AS (
  INSERT INTO psy.chunks (lecture_id, ordinal, start_ms, end_ms, text)
  SELECT p.lecture_id, s.ordinal, s.start_ms, s.end_ms, s.text
  FROM params p
  CROSS JOIN LATERAL jsonb_to_recordset(p.segments) AS s(
    ordinal int, start_ms int, end_ms int, text text
  )
  ON CONFLICT (lecture_id, ordinal) DO UPDATE
    SET start_ms = EXCLUDED.start_ms,
        end_ms = EXCLUDED.end_ms,
        text = EXCLUDED.text
  RETURNING ordinal
), max_ord AS (
  SELECT COALESCE(MAX(ordinal), 0) AS m FROM upsert_chunks
), del AS (
  DELETE FROM psy.chunks c
  USING params p, max_ord m
  WHERE c.lecture_id = p.lecture_id AND c.ordinal > m.m
  RETURNING c.ordinal
), lec AS (
  UPDATE psy.lectures l
  SET status = 'transcribed',
      duration_sec = COALESCE((SELECT duration_sec FROM params), l.duration_sec),
      error = NULL
  FROM params p
  WHERE l.id = p.lecture_id
  RETURNING l.id
), done_job AS (
  UPDATE psy.jobs
  SET status = 'done', finished_at = now(), error = NULL
  WHERE id = (SELECT job_id FROM params)
  RETURNING id
), enq_konspekt AS (
  INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
  SELECT p.lecture_id, 'konspekt', 'queued', 'konspekt:' || p.lecture_id::text,
         jsonb_build_object('workflow_version', p.workflow_version, 'source', 'psy_03')
  FROM params p
  WHERE EXISTS (SELECT 1 FROM done_job)
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
)
SELECT
  (SELECT id FROM lec) AS lecture_id,
  (SELECT id FROM done_job) AS job_id,
  (SELECT count(*) FROM upsert_chunks) AS chunk_count,
  (SELECT id FROM enq_konspekt) AS konspekt_job_id""",
        1540,
        220,
        """={{ [
  $('ASR — Prep Request').item.json.lecture_id,
  $('ASR — Prep Request').item.json.job_id,
  $json.text,
  $json.model,
  Math.round(Number($json.duration_sec || 0)),
  JSON.stringify($json.segments || []),
  $('Run — Init Constants').first().json.constants.workflow_version
] }}""",
    )
    n_kick = {
        "id": nid(),
        "name": "Fin — Kick psy_04",
        "type": "n8n-nodes-base.executeWorkflow",
        "typeVersion": 1.2,
        "position": [1760, 220],
        "parameters": {
            "workflowId": {"__rl": True, "mode": "id", "value": psy_04_id or ""},
            "options": {"waitForSubWorkflow": False},
        },
        "onError": "continueRegularOutput",
    }
    n_fail_ctx = code(
        "Fin — Fail Context",
        """const job = $('Run — Create Job').item.json;
const errItem = items[0].json || {};
const statusCode = errItem.statusCode || errItem.httpCode || errItem.error?.status || null;
const message = errItem.message || errItem.error?.message || errItem.error || JSON.stringify(errItem).slice(0, 500);
return [{
  json: {
    job_id: job.id,
    lecture_id: job.lecture_id,
    attempts: job.attempts,
    status_code: statusCode,
    error_message: String(message),
  },
  pairedItem: 0,
}];""",
        1540,
        480,
    )
    n_fail = pg(
        "Fin — Fail Job",
        """WITH j AS (
  UPDATE psy.jobs
  SET status = CASE
        WHEN $2::int = 409 THEN 'queued'
        WHEN attempts >= 3 THEN 'failed'
        ELSE 'queued'
      END,
      attempts = CASE WHEN $2::int = 409 THEN GREATEST(attempts - 1, 0) ELSE attempts END,
      error = $3,
      finished_at = CASE WHEN $2::int <> 409 AND attempts >= 3 THEN now() ELSE NULL END,
      started_at = CASE WHEN $2::int = 409 OR attempts < 3 THEN NULL ELSE started_at END
  WHERE id = $1::uuid
  RETURNING id, status, attempts, lecture_id
), lec AS (
  UPDATE psy.lectures
  SET status = 'failed', error = $3
  WHERE id = (SELECT lecture_id FROM j)
    AND (SELECT status FROM j) = 'failed'
  RETURNING id
)
SELECT * FROM j""",
        1760,
        480,
        """={{ [
  $json.job_id,
  Number($json.status_code || 0),
  $json.error_message
] }}""",
    )

    nodes.extend([n_manual, n_exec, n_init, n_claim, n_has, n_gloss, n_prep, n_asr, n_save, n_fail_ctx, n_fail])
    if psy_04_id:
        nodes.append(n_kick)
    link(conn, "In — Manual", "Run — Init Constants")
    link(conn, "In — Execute", "Run — Init Constants")
    link(conn, "Run — Init Constants", "Run — Create Job")
    link(conn, "Run — Create Job", "Run — Has Job")
    link(conn, "Run — Has Job", "Load — Glossary", 0)
    link(conn, "Load — Glossary", "ASR — Prep Request")
    link(conn, "ASR — Prep Request", "ASR — Transcribe")
    link(conn, "ASR — Transcribe", "DB — Save Transcript", 0)
    if psy_04_id:
        link(conn, "DB — Save Transcript", "Fin — Kick psy_04")
    link(conn, "ASR — Transcribe", "Fin — Fail Context", 1)
    link(conn, "Fin — Fail Context", "Fin — Fail Job")
    return "psy_03_transcribe", nodes, conn


def main() -> None:
    # Create/update in dependency order: 03 first (no kick), then 02, then 01
    psy_04_id_path = WORKFLOWS / "psy_04_konspekt.id"
    psy_04_id = psy_04_id_path.read_text(encoding="utf-8").strip() if psy_04_id_path.exists() else None
    slug3, nodes3, conn3 = build_psy_03(psy_04_id)
    id3 = ensure_workflow(slug3, nodes3, conn3)
    save_local(slug3, id3, slug3, nodes3, conn3)
    print(json.dumps({"created": slug3, "id": id3}, ensure_ascii=False))

    slug2, nodes2, conn2 = build_psy_02(id3)
    id2 = ensure_workflow(slug2, nodes2, conn2)
    save_local(slug2, id2, slug2, nodes2, conn2)
    print(json.dumps({"created": slug2, "id": id2}, ensure_ascii=False))

    slug1, nodes1, conn1 = build_psy_01(id2)
    id1 = ensure_workflow(slug1, nodes1, conn1)
    save_local(slug1, id1, slug1, nodes1, conn1)
    print(json.dumps({"created": slug1, "id": id1}, ensure_ascii=False))


if __name__ == "__main__":
    main()
