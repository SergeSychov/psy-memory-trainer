#!/usr/bin/env python3
"""Build and deploy inactive psy_08_telegram_quiz.

Quiz logic lives in n8n/lib/quiz_session.js (embedded into Code nodes).
Credentials: TelegramStudy, psy Supabase (psy_n8n). No LLM. No secrets in git.
Activate only after webhook check for @natasha_study_bot.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from _env import WORKFLOWS, api_request
from build_psy_01_03 import PG, code, ensure_workflow, iff, link, nid, pg, save_local, sticky

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "n8n" / "lib"

TG = {"telegramApi": {"id": "xxaZ5HTQ7zihMvte", "name": "TelegramStudy"}}
WF_NAME = "psy_08_telegram_quiz"
WF_VER = "psy_08_telegram_quiz_v2"
SLUG = "psy_08_telegram_quiz"
BOT_USERNAME = "natasha_study_bot"


def load_quiz_js() -> str:
    src = (LIB / "quiz_session.js").read_text(encoding="utf-8")
    src = src.replace("'use strict';", "")
    src = re.sub(r"module\.exports\s*=\s*\{[\s\S]*?\};?", "", src)
    return src.strip()


def tg_trigger(x: int, y: int) -> dict:
    # Trigger path id is instance-local; omit from git export.
    return {
        "id": nid(),
        "name": "In — Telegram",
        "type": "n8n-nodes-base.telegramTrigger",
        "typeVersion": 1.2,
        "position": [x, y],
        "credentials": TG,
        "parameters": {
            "updates": ["message", "callback_query"],
            "additionalFields": {},
        },
    }


def tg_send(
    name: str,
    x: int,
    y: int,
    *,
    text: str,
    chat_id: str,
) -> dict:
    """Plain sendMessage via Telegram node (no dynamic inline keyboard)."""
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1.2,
        "position": [x, y],
        "credentials": TG,
        "parameters": {
            "resource": "message",
            "operation": "sendMessage",
            "chatId": chat_id,
            "text": text,
            "additionalFields": {
                "appendAttribution": False,
                "parse_mode": "HTML",
            },
            "replyMarkup": "none",
        },
        "onError": "continueRegularOutput",
    }


def _btn(item_expr: str, row: int, col: int) -> dict:
    """One inline button; text/callback from keyboard.inline_keyboard[row][col]."""
    base = f"({item_expr}).keyboard.inline_keyboard[{row}][{col}]"
    return {
        "text": f"={{{{ {base}.text }}}}",
        "additionalFields": {
            "callback_data": f"={{{{ {base}.callback_data }}}}",
        },
    }


def _rows_from_kb(item_expr: str, n_rows: int, cols: int = 1) -> dict:
    rows = []
    for r in range(n_rows):
        buttons = [_btn(item_expr, r, c) for c in range(cols)]
        rows.append({"row": {"buttons": buttons}})
    return {"rows": rows}


def _rows_paged(item_expr: str, item_rows: int, nav_cols: int = 2) -> dict:
    """item_rows × 1 button, then one nav row with nav_cols buttons (Назад/Ещё)."""
    rows = []
    for r in range(item_rows):
        rows.append({"row": {"buttons": [_btn(item_expr, r, 0)]}})
    rows.append(
        {"row": {"buttons": [_btn(item_expr, item_rows, c) for c in range(nav_cols)]}}
    )
    return {"rows": rows}


def tg_send_markup(
    name: str,
    x: int,
    y: int,
    *,
    item_expr: str,
    n_rows: int,
    cols: int = 1,
    paged: bool = False,
) -> dict:
    """sendMessage with inline keyboard via per-button expressions.

    n8n ignores a dynamic object in ``inlineKeyboard``; each text/callback_data
    field must be its own expression. paged=True → n_rows item rows + 1 nav row.
    """
    inline = (
        _rows_paged(item_expr, n_rows, nav_cols=2)
        if paged
        else _rows_from_kb(item_expr, n_rows, cols)
    )
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1.2,
        "position": [x, y],
        "credentials": TG,
        "parameters": {
            "resource": "message",
            "operation": "sendMessage",
            "chatId": f"={{{{ ({item_expr}).chat_id }}}}",
            "text": f"={{{{ ({item_expr}).out_text }}}}",
            "additionalFields": {
                "appendAttribution": False,
                "parse_mode": "HTML",
            },
            "replyMarkup": "inlineKeyboard",
            "inlineKeyboard": inline,
        },
        "onError": "continueRegularOutput",
    }


def tg_ack(name: str, x: int, y: int) -> dict:
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1.2,
        "position": [x, y],
        "credentials": TG,
        "parameters": {
            "resource": "callback",
            "operation": "answerQuery",
            "queryId": "={{ $('Run — Normalize').item.json.callback_query_id }}",
            "additionalFields": {},
        },
        "onError": "continueRegularOutput",
    }


def switch_action(x: int, y: int, cases: list[str]) -> dict:
    """Switch on $json.action — one output per case, then fallback."""
    values = []
    for case in cases:
        values.append(
            {
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "loose",
                    },
                    "conditions": [
                        {
                            "id": nid(),
                            "leftValue": "={{ $json.action }}",
                            "rightValue": case,
                            "operator": {"type": "string", "operation": "equals"},
                        }
                    ],
                    "combinator": "and",
                },
                "renameOutput": True,
                "outputKey": case,
            }
        )
    return {
        "id": nid(),
        "name": "Run — Switch Action",
        "type": "n8n-nodes-base.switch",
        "typeVersion": 3.2,
        "position": [x, y],
        "parameters": {
            "rules": {"values": values},
            "options": {"fallbackOutput": "extra"},
        },
    }


NORMALIZE_JS = r"""
const raw = $input.first().json;
const msg = raw.message || (raw.callback_query && raw.callback_query.message) || null;
const cq = raw.callback_query || null;
const from = (cq && cq.from) || (msg && msg.from) || {};
const chat = (msg && msg.chat) || {};
const text = ((msg && msg.text) || '').trim();
const callback_data = (cq && cq.data) || '';
const callback_query_id = (cq && cq.id) || null;
let command = '';
if (text.startsWith('/')) {
  command = text.split(/\s+/)[0].split('@')[0].toLowerCase();
}
// Lightweight /start parse (full rules also in quiz_session.parseStartPayload).
let start_payload = { kind: 'not_start' };
if (command === '/start') {
  const parts = text.split(/\s+/).filter(Boolean);
  if (parts.length === 1) {
    start_payload = { kind: 'plain' };
  } else {
    const code = parts.slice(1).join('');
    start_payload = /^[A-Za-z0-9_-]{8,64}$/.test(code)
      ? { kind: 'invite', code }
      : { kind: 'bad_code' };
  }
}
return [{
  json: {
    update_type: cq ? 'callback' : 'message',
    telegram_user_id: from.id || null,
    chat_id: chat.id || (from.id || null),
    text,
    command,
    start_payload,
    from_first_name: from.first_name || null,
    callback_data,
    callback_query_id,
    from_username: from.username || null,
    from_name: [from.first_name, from.last_name].filter(Boolean).join(' ') || null,
  }
}];
"""


def route_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}

const norm = $('Run — Normalize').first().json;
const userRow = $input.first().json;
const known = !!(userRow.user_id);
const userId = userRow.user_id || null;
const displayName = userRow.display_name || null;
const isSystemAdmin = userRow.is_system_admin === true || userRow.is_system_admin === 't';
const workspaceId = userRow.workspace_id || null;
const ownedCoursesCount = Number(userRow.owned_courses_count || 0);
const session = {{
  id: userRow.session_id || null,
  status: userRow.session_status || null,
  lecture_id: userRow.session_lecture_id || null,
  question_ids: userRow.session_question_ids || [],
  cursor: Number(userRow.session_cursor || 0),
  current_question_id: userRow.session_current_question_id || null,
  asked_at: userRow.session_asked_at || null,
  correct_count: Number(userRow.session_correct_count || 0),
  answered_count: Number(userRow.session_answered_count || 0),
  miss_subtopics: userRow.session_miss_subtopics || [],
  last_callback_id: userRow.session_last_callback_id || null,
}};
if (typeof session.question_ids === 'string') {{
  try {{ session.question_ids = JSON.parse(session.question_ids); }} catch (_e) {{ session.question_ids = []; }}
}}
if (typeof session.miss_subtopics === 'string') {{
  try {{ session.miss_subtopics = JSON.parse(session.miss_subtopics); }} catch (_e) {{ session.miss_subtopics = []; }}
}}

const base = {{
  chat_id: norm.chat_id,
  telegram_user_id: norm.telegram_user_id,
  callback_query_id: norm.callback_query_id,
  user_id: userId,
  display_name: displayName,
  is_system_admin: isSystemAdmin,
  workspace_id: workspaceId,
  owned_courses_count: ownedCoursesCount,
  session,
  has_callback: !!norm.callback_query_id,
  from_first_name: norm.from_first_name || null,
}};

function reply(action, text, extra = {{}}) {{
  return [{{ json: {{ ...base, action, out_text: text, keyboard: null, ...extra }} }}];
}}

if (!norm.telegram_user_id || !norm.chat_id) {{
  return reply('noop', '');
}}

const startInfo = norm.start_payload || parseStartPayload(norm.text || '');
const openStatuses = new Set(['picking_course','picking_lecture','active','awaiting_next']);
const hasOpen = !!(session.id && openStatuses.has(session.status));
const isOwner = ownedCoursesCount > 0;

if (!known) {{
  if (norm.command === '/start') {{
    if (startInfo.kind === 'bad_code') {{
      return reply('help', inviteRefusalText('bad_code'));
    }}
    return reply('ensure_user', '', {{
      invite_code: startInfo.kind === 'invite' ? startInfo.code : null,
      from_first_name: norm.from_first_name || '',
    }});
  }}
  if (norm.update_type === 'callback') {{
    return reply('noop', '');
  }}
  return reply('help', 'Напишите /start');
}}

if (norm.command === '/start') {{
  if (startInfo.kind === 'bad_code') {{
    return reply('help', inviteRefusalText('bad_code'));
  }}
  if (startInfo.kind === 'invite') {{
    return reply('redeem_invite', '', {{ invite_code: startInfo.code }});
  }}
  return reply('welcome', welcomeText({{ isNew: false }}));
}}

if (norm.command === '/help') {{
  let text = 'Квиз без подсказок модели: вопросы уже в базе.\\n/quiz — выбрать курс и лекцию\\n/stop — остановить сессию\\nОтветы — кнопками A–D.';
  if (isOwner) {{
    text += '\\n/invite — ссылка-приглашение в свой курс\\n/invites — активные приглашения и отзыв';
  }}
  return reply('help', text);
}}

if (norm.command === '/stop') {{
  if (!hasOpen) {{
    return reply('stop_idle', 'Активной сессии нет. Начните новую: /quiz');
  }}
  return reply('stop', 'Сессию остановил. Когда будете готовы — /quiz', {{ stop_session_id: session.id }});
}}

if (norm.command === '/quiz') {{
  if (hasOpen && (session.status === 'active' || session.status === 'awaiting_next')) {{
    return reply('busy', 'У вас уже есть активная сессия. Завершите её или отправьте /stop.');
  }}
  return reply('pick_course', 'Выберите курс:', {{ course_offset: 0 }});
}}

if (norm.command === '/invite') {{
  if (!isOwner) {{
    return reply('help', 'Команда доступна владельцу курса.');
  }}
  return reply('invite_pick', 'Выберите курс для приглашения:', {{ invite_course_offset: 0 }});
}}

if (norm.command === '/invites') {{
  if (!isOwner) {{
    return reply('help', 'Команда доступна владельцу курса.');
  }}
  return reply('invites_list', '', {{ invites_offset: 0 }});
}}

if (norm.update_type === 'callback') {{
  if (session.last_callback_id && norm.callback_query_id && session.last_callback_id === norm.callback_query_id) {{
    return reply('noop', '');
  }}
  const cb = parseCallback(norm.callback_data);
  if (cb.kind === 'noop') {{
    return reply('noop', '');
  }}
  if (cb.kind === 'page_courses') {{
    return reply('pick_course', 'Выберите курс:', {{ course_offset: cb.offset }});
  }}
  if (cb.kind === 'page_inv_courses') {{
    if (!isOwner) return reply('help', 'Команда доступна владельцу курса.');
    return reply('invite_pick', 'Выберите курс для приглашения:', {{ invite_course_offset: cb.offset }});
  }}
  if (cb.kind === 'page_invites') {{
    if (!isOwner) return reply('help', 'Команда доступна владельцу курса.');
    return reply('invites_list', '', {{ invites_offset: cb.offset }});
  }}
  if (cb.kind === 'page_lectures') {{
    return reply('pick_lecture', 'Выберите лекцию:', {{
      course_id: cb.courseId,
      lecture_offset: cb.offset,
    }});
  }}
  if (cb.kind === 'inv_course') {{
    if (!isOwner) return reply('help', 'Команда доступна владельцу курса.');
    return reply('invite_create', '', {{ course_id: cb.courseId }});
  }}
  if (cb.kind === 'inv_revoke') {{
    if (!isOwner) return reply('help', 'Команда доступна владельцу курса.');
    return reply('invite_revoke', '', {{ invite_id: cb.inviteId }});
  }}
  if (cb.kind === 'course') {{
    return reply('pick_lecture', 'Выберите лекцию:', {{
      course_id: cb.courseId,
      lecture_offset: 0,
    }});
  }}
  if (cb.kind === 'lecture') {{
    if (hasOpen && (session.status === 'active' || session.status === 'awaiting_next')) {{
      return reply('busy', 'У вас уже есть активная сессия. /stop — сбросить.');
    }}
    return reply('start_quiz', '', {{ lecture_id: cb.lectureId }});
  }}
  if (cb.kind === 'answer') {{
    if (!hasOpen || session.status !== 'active' || String(session.id) !== String(cb.sessionId)) {{
      return reply('stale', '');
    }}
    if (!['A','B','C','D'].includes(cb.choice)) {{
      return reply('stale', '');
    }}
    return reply('answer', '', {{ choice: cb.choice, session_id: session.id }});
  }}
  if (cb.kind === 'next') {{
    if (!hasOpen || String(session.id) !== String(cb.sessionId)) {{
      return reply('stale', '');
    }}
    if (session.status !== 'awaiting_next') {{
      return reply('stale', '');
    }}
    return reply('next', '', {{ session_id: session.id }});
  }}
  return reply('stale', '');
}}

return reply('help',
  'Не понял сообщение. Команды: /start, /quiz, /stop, /help');
"""


# Fixed Telegram rows: pageSize item rows + 1 nav row (Назад/Ещё). Must match quiz_session.js.
COURSE_PAGE_SIZE = 2
LECTURE_PAGE_SIZE = 4


def format_courses_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const courses = items.map((i) => i.json).filter((r) => r.id);
if (!courses.length) {{
  return [{{ json: {{ ...base, action: 'reply', out_text: 'Пока нет доступных курсов. Загрузка своих материалов скоро; если есть ссылка-приглашение — откройте её', keyboard: null, kb_rows: 0 }} }}];
}}
const built = buildCoursesPage(courses, base.course_offset || 0);
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text: built.out_text,
  keyboard: built.keyboard,
  kb_rows: built.kb_rows,
  list_total: built.total,
  list_offset: built.offset,
}} }}];
"""


def format_lectures_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const lectures = items.map((i) => i.json).filter((r) => r.id);
if (!lectures.length) {{
  return [{{ json: {{ ...base, action: 'reply', out_text: 'В этом курсе нет лекций с готовыми вопросами.', keyboard: null, kb_rows: 0 }} }}];
}}
const built = buildLecturesPage(lectures, base.lecture_offset || 0, base.course_id);
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text: built.out_text,
  keyboard: built.keyboard,
  kb_rows: built.kb_rows,
  list_total: built.total,
  list_offset: built.offset,
}} }}];
"""


def start_quiz_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const candidates = items.map((i) => i.json).filter((r) => r.id);
const picked = selectQuizQuestions(candidates, {{
  limit: QUIZ_SIZE,
  seed: `${{base.user_id}}:${{base.lecture_id}}:${{Date.now()}}`,
}});
if (picked.length < 1) {{
  return [{{ json: {{
    ...base,
    action: 'reply',
    out_text: 'Не нашлось вопросов для этой лекции.',
    keyboard: null,
    skip_insert: true,
  }} }}];
}}
const first = picked[0];
const question_ids = picked.map((q) => q.id);
return [{{ json: {{
  ...base,
  action: 'insert_session',
  skip_insert: false,
  question_ids,
  question_ids_json: JSON.stringify(question_ids),
  first_question: first,
  out_text: formatQuestionMessage(first, 0, question_ids.length),
}} }}];
"""


def after_insert_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const planned = $('Run — Select Questions').first().json;
const row = $input.first().json;
if (planned.skip_insert) {{
  return [{{ json: {{ ...planned, keyboard: null }} }}];
}}
const sessionId = row.id || row.session_id;
if (!sessionId) {{
  return [{{ json: {{
    ...planned,
    action: 'reply',
    out_text: 'Не удалось начать сессию. Попробуйте /quiz ещё раз.',
    keyboard: null,
  }} }}];
}}
return [{{ json: {{
  ...planned,
  action: 'reply',
  session_id: sessionId,
  keyboard: choiceKeyboard(sessionId),
}} }}];
"""


def grade_answer_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const q = $input.first().json;
if (!q || !q.id) {{
  return [{{ json: {{ ...base, action: 'stale', out_text: '', keyboard: null, skip_write: true }} }}];
}}
const chosen = String(base.choice || '').toUpperCase();
const correct = chosen === String(q.correct_choice || '').toUpperCase();
const tallied = tallyAnswer({{
  session: base.session,
  correct,
  subtopicTitle: q.subtopic_title || '',
}});
const response_ms = computeResponseMs(base.session.asked_at, new Date().toISOString());
const rating = ratingForAnswer(correct);
const feedback = formatFeedback({{ correct, chosen, question: q }});
const total = (base.session.question_ids || []).length || QUIZ_SIZE;
return [{{ json: {{
  ...base,
  action: 'write_answer',
  skip_write: false,
  question_id: q.id,
  term_id: q.term_id || null,
  chosen,
  correct,
  rating,
  response_ms,
  correct_count: tallied.correctCount,
  answered_count: tallied.answeredCount,
  miss_subtopics: tallied.missSubtopics,
  miss_subtopics_json: JSON.stringify(tallied.missSubtopics),
  out_text: feedback,
  keyboard: nextKeyboard(base.session.id),
  total_questions: total,
}} }}];
"""


def next_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const session = base.session;
const ids = session.question_ids || [];
const nextCursor = Number(session.cursor || 0) + 1;
const total = ids.length || QUIZ_SIZE;

if (nextCursor >= total) {{
  return [{{ json: {{
    ...base,
    action: 'finish',
    next_cursor: nextCursor,
    out_text: formatSummary({{
      correctCount: session.correct_count,
      total,
      missSubtopics: session.miss_subtopics,
    }}),
    keyboard: null,
    next_question_id: null,
  }} }}];
}}

const q = $input.first().json;
if (!q || !q.id) {{
  return [{{ json: {{
    ...base,
    action: 'finish',
    next_cursor: nextCursor,
    out_text: formatSummary({{
      correctCount: session.correct_count,
      total,
      missSubtopics: session.miss_subtopics,
    }}),
    keyboard: null,
    next_question_id: null,
  }} }}];
}}

return [{{ json: {{
  ...base,
  action: 'show_next',
  next_cursor: nextCursor,
  next_question_id: q.id,
  out_text: formatQuestionMessage(q, nextCursor, total),
  keyboard: choiceKeyboard(session.id),
}} }}];
"""


def build() -> str:
    quiz_lib = load_quiz_js()
    nodes: list[dict] = []
    connections: dict = {}

    nodes.append(
        sticky(
            "NOTE — Quiz",
            f"## {WF_NAME}\n`{WF_VER}`\nSelf-serve /start + workspaces/invites.\nНет LLM. Activate only this WF.",
            -400,
            200,
            420,
            220,
            4,
        )
    )
    nodes.append(tg_trigger(0, 300))
    nodes.append(code("Run — Normalize", NORMALIZE_JS, 240, 300))

    nodes.append(
        pg(
            "Load — User Session",
            """
SELECT
  ($1::bigint) AS telegram_user_id,
  u.id AS user_id,
  u.display_name,
  COALESCE(u.is_system_admin, false) AS is_system_admin,
  w.id AS workspace_id,
  (
    SELECT count(*)::int
    FROM psy.courses c
    WHERE c.workspace_id = w.id
  ) AS owned_courses_count,
  s.id AS session_id,
  s.status AS session_status,
  s.lecture_id AS session_lecture_id,
  s.question_ids AS session_question_ids,
  s.cursor AS session_cursor,
  s.current_question_id AS session_current_question_id,
  s.asked_at AS session_asked_at,
  s.correct_count AS session_correct_count,
  s.answered_count AS session_answered_count,
  s.miss_subtopics AS session_miss_subtopics,
  s.last_callback_id AS session_last_callback_id
FROM (SELECT 1) AS _
LEFT JOIN psy.users u ON u.telegram_user_id = $1::bigint
LEFT JOIN psy.workspaces w ON w.owner_user_id = u.id
LEFT JOIN LATERAL (
  SELECT *
  FROM psy.quiz_sessions qs
  WHERE qs.user_id = u.id
    AND qs.status IN ('picking_course','picking_lecture','active','awaiting_next')
  ORDER BY qs.created_at DESC
  LIMIT 1
) s ON TRUE;
""".strip(),
            480,
            300,
            query_replacement="={{ [$json.telegram_user_id] }}",
        )
    )

    nodes.append(code("Run — Route", route_js(quiz_lib), 720, 300))

    cases = [
        "welcome",
        "help",
        "busy",
        "stop",
        "stop_idle",
        "ensure_user",
        "redeem_invite",
        "invite_pick",
        "invite_create",
        "invites_list",
        "invite_revoke",
        "pick_course",
        "pick_lecture",
        "start_quiz",
        "answer",
        "next",
        "stale",
        "noop",
    ]
    nodes.append(switch_action(960, 300, cases))

    # Shared simple replies
    y_simple = 0
    for i, action in enumerate(["welcome", "help", "busy", "stop_idle"]):
        send = tg_send(
            f"Out — {action}",
            1280,
            y_simple + i * 120,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
        nodes.append(send)
        link(connections, "Run — Switch Action", send["name"], src_output=cases.index(action))

    # stop: update then reply
    nodes.append(
        pg(
            "DB — Stop Session",
            """
UPDATE psy.quiz_sessions
SET status = 'stopped', current_question_id = NULL, asked_at = NULL, updated_at = now()
WHERE id = $1::uuid
  AND user_id = $2::uuid
  AND status IN ('picking_course','picking_lecture','active','awaiting_next')
RETURNING id;
""".strip(),
            1280,
            560,
            query_replacement="={{ [$json.stop_session_id, $json.user_id] }}",
        )
    )
    nodes.append(
        tg_send(
            "Out — stop",
            1520,
            560,
            text="={{ $('Run — Route').item.json.out_text }}",
            chat_id="={{ $('Run — Route').item.json.chat_id }}",
        )
    )
    link(connections, "Run — Switch Action", "DB — Stop Session", src_output=cases.index("stop"))
    link(connections, "DB — Stop Session", "Out — stop")

    # ensure_user (+ optional redeem)
    nodes.append(
        pg(
            "DB — Ensure User",
            """
WITH ups AS (
  INSERT INTO psy.users (telegram_user_id, display_name)
  VALUES ($1::bigint, NULLIF(BTRIM($2::text), ''))
  ON CONFLICT (telegram_user_id) DO UPDATE
    SET telegram_user_id = EXCLUDED.telegram_user_id
  RETURNING id, display_name, (xmax = 0) AS is_new
),
ins_ws AS (
  INSERT INTO psy.workspaces (owner_user_id)
  SELECT id FROM ups
  ON CONFLICT (owner_user_id) DO NOTHING
  RETURNING id
)
SELECT
  ups.id AS user_id,
  ups.display_name,
  ups.is_new,
  COALESCE(
    (SELECT w.id FROM psy.workspaces w WHERE w.owner_user_id = ups.id LIMIT 1),
    (SELECT id FROM ins_ws LIMIT 1)
  ) AS workspace_id
FROM ups;
""".strip(),
            1280,
            680,
            query_replacement=(
                "={{ [$json.telegram_user_id, $json.from_first_name || ''] }}"
            ),
        )
    )
    nodes.append(
        code(
            "Run — After Ensure",
            f"""
{quiz_lib}
const route = $('Run — Route').first().json;
const row = $input.first().json || {{}};
const isNew = row.is_new === true || row.is_new === 't' || row.is_new === true;
const inviteCode = route.invite_code || null;
return [{{ json: {{
  ...route,
  user_id: row.user_id || route.user_id,
  display_name: row.display_name || route.display_name,
  workspace_id: row.workspace_id || route.workspace_id,
  is_new: isNew,
  invite_code: inviteCode,
  action: inviteCode ? 'redeem_invite' : 'welcome',
  out_text: inviteCode ? '' : welcomeText({{ isNew }}),
  keyboard: null,
}} }}];
""",
            1520,
            680,
        )
    )
    nodes.append(
        iff(
            "IF — Ensure Has Invite",
            "={{ String($json.invite_code || '') }}",
            "",
            1640,
            680,
            op="notEquals",
        )
    )
    nodes.append(
        tg_send(
            "Out — Ensure Welcome",
            1880,
            560,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    link(connections, "Run — Switch Action", "DB — Ensure User", src_output=cases.index("ensure_user"))
    link(connections, "DB — Ensure User", "Run — After Ensure")
    link(connections, "Run — After Ensure", "IF — Ensure Has Invite")
    link(connections, "IF — Ensure Has Invite", "DB — Redeem Invite", src_output=0)
    link(connections, "IF — Ensure Has Invite", "Out — Ensure Welcome", src_output=1)

    # redeem_invite
    nodes.append(
        pg(
            "DB — Redeem Invite",
            """
WITH params AS (
  SELECT
    $1::uuid AS user_id,
    encode(extensions.digest($2::text, 'sha256'), 'hex') AS code_hash
),
inv AS (
  SELECT i.*
  FROM psy.invites i
  JOIN params p ON i.code_hash = p.code_hash
),
already AS (
  SELECT
    inv.id AS invite_id,
    inv.course_id,
    c.title AS course_title
  FROM inv
  JOIN params p ON TRUE
  JOIN psy.courses c ON c.id = inv.course_id
  JOIN psy.workspaces w ON w.id = c.workspace_id
  WHERE w.owner_user_id = p.user_id
     OR EXISTS (
       SELECT 1 FROM psy.course_members cm
       WHERE cm.course_id = inv.course_id AND cm.user_id = p.user_id
     )
),
bump AS (
  UPDATE psy.invites i
  SET uses_count = i.uses_count + 1
  FROM inv, params p
  WHERE i.id = inv.id
    AND i.revoked_at IS NULL
    AND (i.expires_at IS NULL OR i.expires_at > now())
    AND (i.max_uses IS NULL OR i.uses_count < i.max_uses)
    AND NOT EXISTS (SELECT 1 FROM already)
  RETURNING i.id, i.course_id
),
mem AS (
  INSERT INTO psy.course_members (course_id, user_id, role, invite_id)
  SELECT b.course_id, p.user_id, 'student', b.id
  FROM bump b
  CROSS JOIN params p
  ON CONFLICT (course_id, user_id) DO NOTHING
  RETURNING course_id, invite_id
)
SELECT
  (already.invite_id IS NOT NULL OR mem.course_id IS NOT NULL) AS ok,
  (already.invite_id IS NOT NULL) AS already_member,
  (mem.course_id IS NOT NULL) AS redeemed,
  COALESCE(already.course_id, mem.course_id, inv.course_id) AS course_id,
  COALESCE(already.course_title, c.title) AS course_title,
  inv.id AS invite_id,
  inv.revoked_at,
  inv.expires_at,
  inv.max_uses,
  inv.uses_count,
  (inv.id IS NULL) AS missing
FROM params
LEFT JOIN inv ON TRUE
LEFT JOIN already ON TRUE
LEFT JOIN mem ON TRUE
LEFT JOIN psy.courses c ON c.id = COALESCE(mem.course_id, inv.course_id);
""".strip(),
            1760,
            780,
            query_replacement=(
                "={{ ["
                "($json.user_id || $('Run — Route').item.json.user_id), "
                "($json.invite_code || $('Run — Route').item.json.invite_code)"
                "] }}"
            ),
        )
    )
    nodes.append(
        code(
            "Run — Format Redeem",
            f"""
{quiz_lib}
const route = $('Run — Route').first().json;
const ensured = (() => {{
  try {{ return $('Run — After Ensure').first().json; }} catch (_e) {{ return null; }}
}})();
const base = ensured && ensured.user_id ? ensured : route;
const row = $input.first().json || {{}};
let out_text;
if (row.ok === true || row.ok === 't') {{
  out_text = welcomeText({{
    joinedCourse: true,
    courseTitle: row.course_title || '',
  }});
}} else if (row.missing === true || row.missing === 't' || !row.invite_id) {{
  out_text = inviteRefusalText('not_found');
}} else {{
  out_text = inviteRefusalText(inviteRefusal({{
    id: row.invite_id,
    revoked_at: row.revoked_at,
    expires_at: row.expires_at,
    max_uses: row.max_uses,
    uses_count: row.uses_count,
  }}) || 'not_found');
}}
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text,
  keyboard: null,
}} }}];
""",
            2000,
            780,
        )
    )
    nodes.append(
        tg_send(
            "Out — Redeem",
            2240,
            780,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    link(connections, "Run — Switch Action", "DB — Redeem Invite", src_output=cases.index("redeem_invite"))
    link(connections, "DB — Redeem Invite", "Run — Format Redeem")
    link(connections, "Run — Format Redeem", "Out — Redeem")

    # /invite — pick owned course
    nodes.append(
        pg(
            "Load — Owned Courses",
            """
SELECT c.id, c.title
FROM psy.courses c
JOIN psy.workspaces w ON w.id = c.workspace_id
WHERE w.owner_user_id = $1::uuid
ORDER BY c.title;
""".strip(),
            1280,
            900,
            query_replacement="={{ [$json.user_id] }}",
        )
    )
    nodes.append(
        code(
            "Run — Format Invite Courses",
            f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const courses = items.map((i) => i.json).filter((r) => r.id);
if (!courses.length) {{
  return [{{ json: {{
    ...base,
    action: 'reply',
    out_text: 'У вас пока нет своих курсов.',
    keyboard: null,
    kb_rows: 0,
  }} }}];
}}
const built = buildInviteCoursesPage(courses, base.invite_course_offset || 0);
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text: built.out_text,
  keyboard: built.keyboard,
  kb_rows: built.kb_rows,
}} }}];
""",
            1520,
            900,
        )
    )
    nodes.append(
        iff(
            "IF — Has Invite Courses",
            "={{ String($json.kb_rows || 0) }}",
            "0",
            1640,
            900,
            op="notEquals",
        )
    )
    nodes.append(
        tg_send(
            "Out — No Invite Courses",
            1760,
            820,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    nodes.append(
        tg_send_markup(
            "Out — Invite Courses",
            1760,
            900,
            item_expr="$json",
            n_rows=COURSE_PAGE_SIZE,
            paged=True,
        )
    )
    nodes.append(tg_ack("Ack — Invite Courses", 1760, 980))
    link(connections, "Run — Switch Action", "Load — Owned Courses", src_output=cases.index("invite_pick"))
    link(connections, "Load — Owned Courses", "Run — Format Invite Courses")
    link(connections, "Run — Format Invite Courses", "IF — Has Invite Courses")
    link(connections, "IF — Has Invite Courses", "Out — Invite Courses", src_output=0)
    link(connections, "IF — Has Invite Courses", "Out — No Invite Courses", src_output=1)
    link(connections, "Run — Format Invite Courses", "Ack — Invite Courses")

    # create invite (plaintext code returned once; not logged elsewhere)
    nodes.append(
        pg(
            "DB — Create Invite",
            """
WITH gen AS (
  SELECT translate(encode(extensions.gen_random_bytes(12), 'base64'), '+/=', '-_') AS code
),
owned AS (
  SELECT c.id, c.title
  FROM psy.courses c
  JOIN psy.workspaces w ON w.id = c.workspace_id
  WHERE c.id = $1::uuid AND w.owner_user_id = $2::uuid
),
ins AS (
  INSERT INTO psy.invites (course_id, created_by, code_hash, expires_at)
  SELECT owned.id, $2::uuid, encode(extensions.digest(gen.code, 'sha256'), 'hex'), now() + interval '30 days'
  FROM owned
  CROSS JOIN gen
  RETURNING id, course_id, expires_at, uses_count, max_uses
)
SELECT
  ins.id AS invite_id,
  ins.course_id,
  owned.title AS course_title,
  ins.expires_at,
  gen.code AS invite_code
FROM ins
JOIN owned ON owned.id = ins.course_id
CROSS JOIN gen;
""".strip(),
            1280,
            1060,
            query_replacement="={{ [$json.course_id, $json.user_id] }}",
        )
    )
    nodes.append(
        code(
            "Run — Format Invite Link",
            f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const row = $input.first().json || {{}};
const code = row.invite_code || '';
const link = buildInviteLink('{BOT_USERNAME}', code);
let out_text;
if (!link) {{
  out_text = 'Не удалось создать приглашение. Проверьте, что курс ваш, и попробуйте ещё раз.';
}} else {{
  out_text = 'Ссылка-приглашение (сохраните, повторно не покажу):\\n' + link +
    '\\n\\nСрок: 30 дней. Участник открывает ссылку и пишет /start.';
}}
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text,
  keyboard: null,
}} }}];
""",
            1520,
            1060,
        )
    )
    nodes.append(
        tg_send(
            "Out — Invite Link",
            1760,
            1060,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    nodes.append(tg_ack("Ack — Invite Create", 1760, 1140))
    link(connections, "Run — Switch Action", "DB — Create Invite", src_output=cases.index("invite_create"))
    link(connections, "DB — Create Invite", "Run — Format Invite Link")
    link(connections, "Run — Format Invite Link", "Out — Invite Link")
    link(connections, "Run — Format Invite Link", "Ack — Invite Create")

    # /invites list + revoke
    nodes.append(
        pg(
            "Load — Invites",
            """
SELECT
  i.id,
  i.uses_count,
  i.max_uses,
  i.expires_at,
  c.title AS course_title
FROM psy.invites i
JOIN psy.courses c ON c.id = i.course_id
JOIN psy.workspaces w ON w.id = c.workspace_id
WHERE w.owner_user_id = $1::uuid
  AND i.revoked_at IS NULL
  AND (i.expires_at IS NULL OR i.expires_at > now())
  AND (i.max_uses IS NULL OR i.uses_count < i.max_uses)
ORDER BY i.created_at DESC;
""".strip(),
            1280,
            1220,
            query_replacement="={{ [$json.user_id] }}",
        )
    )
    nodes.append(
        code(
            "Run — Format Invites",
            f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const invites = items.map((i) => i.json).filter((r) => r.id);
if (!invites.length) {{
  return [{{ json: {{
    ...base,
    action: 'reply',
    out_text: 'Активных приглашений нет. Создайте: /invite',
    keyboard: null,
    kb_rows: 0,
  }} }}];
}}
const built = buildInvitesListPage(invites, base.invites_offset || 0);
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text: built.out_text,
  keyboard: built.keyboard,
  kb_rows: built.kb_rows,
}} }}];
""",
            1520,
            1220,
        )
    )
    nodes.append(
        iff(
            "IF — Has Invites Kb",
            "={{ String($json.kb_rows || 0) }}",
            "0",
            1640,
            1220,
            op="notEquals",
        )
    )
    nodes.append(
        tg_send(
            "Out — Invites Text",
            1760,
            1160,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    nodes.append(
        tg_send_markup(
            "Out — Invites",
            1760,
            1220,
            item_expr="$json",
            n_rows=LECTURE_PAGE_SIZE,
            paged=True,
        )
    )
    nodes.append(tg_ack("Ack — Invites", 1760, 1300))
    link(connections, "Run — Switch Action", "Load — Invites", src_output=cases.index("invites_list"))
    link(connections, "Load — Invites", "Run — Format Invites")
    link(connections, "Run — Format Invites", "IF — Has Invites Kb")
    link(connections, "IF — Has Invites Kb", "Out — Invites", src_output=0)
    link(connections, "IF — Has Invites Kb", "Out — Invites Text", src_output=1)
    link(connections, "Run — Format Invites", "Ack — Invites")

    nodes.append(
        pg(
            "DB — Revoke Invite",
            """
UPDATE psy.invites i
SET revoked_at = now()
FROM psy.courses c
JOIN psy.workspaces w ON w.id = c.workspace_id
WHERE i.id = $1::uuid
  AND i.course_id = c.id
  AND w.owner_user_id = $2::uuid
  AND i.revoked_at IS NULL
RETURNING i.id;
""".strip(),
            1280,
            1380,
            query_replacement="={{ [$json.invite_id, $json.user_id] }}",
        )
    )
    nodes.append(
        code(
            "Run — Format Revoke",
            """
const base = $('Run — Route').first().json;
const row = $input.first().json || {};
const ok = !!(row.id || row.invite_id);
return [{ json: {
  ...base,
  action: 'reply',
  out_text: ok ? 'Приглашение отозвано.' : 'Не удалось отозвать (нет доступа или уже отозвано).',
  keyboard: null,
}}];
""",
            1520,
            1380,
        )
    )
    nodes.append(
        tg_send(
            "Out — Revoke",
            1760,
            1380,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    nodes.append(tg_ack("Ack — Revoke", 1760, 1460))
    link(connections, "Run — Switch Action", "DB — Revoke Invite", src_output=cases.index("invite_revoke"))
    link(connections, "DB — Revoke Invite", "Run — Format Revoke")
    link(connections, "Run — Format Revoke", "Out — Revoke")
    link(connections, "Run — Format Revoke", "Ack — Revoke")

    # pick_course
    nodes.append(
        pg(
            "Load — Courses",
            """
SELECT c.id, c.title
FROM psy.courses c
WHERE (
  c.workspace_id IN (SELECT w.id FROM psy.workspaces w WHERE w.owner_user_id = $1::uuid)
  OR EXISTS (
    SELECT 1 FROM psy.course_members cm
    WHERE cm.course_id = c.id AND cm.user_id = $1::uuid
  )
)
AND EXISTS (
  SELECT 1 FROM psy.lectures l
  WHERE l.course_id = c.id AND l.status = 'questions_ready'
)
ORDER BY c.title;
""".strip(),
            1280,
            1560,
            query_replacement="={{ [$json.user_id] }}",
        )
    )
    nodes.append(code("Run — Format Courses", format_courses_js(quiz_lib), 1520, 1000))
    # Empty list → plain text; otherwise fixed-row inline keyboard (per-button expressions).
    nodes.append(
        iff(
            "IF — Has Courses",
            "={{ String($json.kb_rows || 0) }}",
            "0",
            1640,
            1000,
            op="notEquals",
        )
    )
    nodes.append(
        tg_send(
            "Out — No Courses",
            1760,
            880,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    nodes.append(
        tg_send_markup(
            "Out — Courses",
            1760,
            1000,
            item_expr="$json",
            n_rows=COURSE_PAGE_SIZE,
            paged=True,
        )
    )
    nodes.append(tg_ack("Ack — Courses", 1760, 1120))
    link(connections, "Run — Switch Action", "Load — Courses", src_output=cases.index("pick_course"))
    link(connections, "Load — Courses", "Run — Format Courses")
    link(connections, "Run — Format Courses", "IF — Has Courses")
    link(connections, "IF — Has Courses", "Out — Courses", src_output=0)
    link(connections, "IF — Has Courses", "Out — No Courses", src_output=1)
    link(connections, "Run — Format Courses", "Ack — Courses")

    # pick_lecture
    nodes.append(
        pg(
            "Load — Lectures",
            """
SELECT l.id, l.title, l.source_meta, l.status
FROM psy.lectures l
JOIN psy.courses c ON c.id = l.course_id
WHERE l.course_id = $1::uuid
  AND l.status = 'questions_ready'
  AND (
    c.workspace_id IN (SELECT w.id FROM psy.workspaces w WHERE w.owner_user_id = $2::uuid)
    OR EXISTS (
      SELECT 1 FROM psy.course_members cm
      WHERE cm.course_id = c.id AND cm.user_id = $2::uuid
    )
  )
ORDER BY COALESCE(l.source_meta #>> '{drive,folder_path,0}', l.title, l.id::text);
""".strip(),
            1280,
            1700,
            query_replacement="={{ [$json.course_id, $json.user_id] }}",
        )
    )
    nodes.append(code("Run — Format Lectures", format_lectures_js(quiz_lib), 1520, 1240))
    nodes.append(
        iff(
            "IF — Has Lectures",
            "={{ String($json.kb_rows || 0) }}",
            "0",
            1640,
            1240,
            op="notEquals",
        )
    )
    nodes.append(
        tg_send(
            "Out — No Lectures",
            1760,
            1160,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    nodes.append(
        tg_send_markup(
            "Out — Lectures",
            1760,
            1240,
            item_expr="$json",
            n_rows=LECTURE_PAGE_SIZE,
            paged=True,
        )
    )
    nodes.append(tg_ack("Ack — Lectures", 1760, 1360))
    link(connections, "Run — Switch Action", "Load — Lectures", src_output=cases.index("pick_lecture"))
    link(connections, "Load — Lectures", "Run — Format Lectures")
    link(connections, "Run — Format Lectures", "IF — Has Lectures")
    link(connections, "IF — Has Lectures", "Out — Lectures", src_output=0)
    link(connections, "IF — Has Lectures", "Out — No Lectures", src_output=1)
    link(connections, "Run — Format Lectures", "Ack — Lectures")

    # start_quiz
    nodes.append(
        pg(
            "Load — Candidates",
            """
SELECT
  q.id,
  q.subtopic_id,
  s.title AS subtopic_title,
  s.ordinal AS subtopic_ord,
  q.stem,
  q.choice_a, q.choice_b, q.choice_c, q.choice_d,
  q.correct_choice,
  q.explanation,
  q.choice_explanations,
  q.source_kind,
  q.thesis,
  q.term_id,
  gt.canonical AS term_canonical,
  gt.definition AS term_definition,
  EXISTS (
    SELECT 1
    FROM psy.cards c
    JOIN psy.reviews r ON r.card_id = c.id
    WHERE c.user_id = $2::uuid AND c.question_id = q.id
  ) AS answered_before
FROM psy.questions q
JOIN psy.subtopics s ON s.id = q.subtopic_id
LEFT JOIN psy.glossary_terms gt ON gt.id = q.term_id
WHERE q.lecture_id = $1::uuid
  AND q.superseded_at IS NULL
  AND EXISTS (
    SELECT 1
    FROM psy.lectures lec
    JOIN psy.courses c ON c.id = lec.course_id
    WHERE lec.id = q.lecture_id
      AND (
        c.workspace_id IN (SELECT w.id FROM psy.workspaces w WHERE w.owner_user_id = $2::uuid)
        OR EXISTS (
          SELECT 1 FROM psy.course_members cm
          WHERE cm.course_id = c.id AND cm.user_id = $2::uuid
        )
      )
  )
ORDER BY s.ordinal, q.ordinal_in_subtopic NULLS LAST, q.created_at;
""".strip(),
            1280,
            1860,
            query_replacement="={{ [$json.lecture_id, $json.user_id] }}",
        )
    )
    nodes.append(code("Run — Select Questions", start_quiz_js(quiz_lib), 1520, 1560))
    nodes.append(
        iff(
            "IF — Has Questions",
            "={{ String($json.skip_insert) }}",
            "true",
            1640,
            1480,
            op="notEquals",
        )
    )
    nodes.append(
        pg(
            "DB — Insert Session",
            """
WITH closed AS (
  UPDATE psy.quiz_sessions
  SET status = 'stopped', updated_at = now()
  WHERE user_id = $1::uuid
    AND status IN ('picking_course','picking_lecture','active','awaiting_next')
  RETURNING id
)
INSERT INTO psy.quiz_sessions (
  user_id, lecture_id, status, question_ids, cursor,
  current_question_id, asked_at, telegram_chat_id, last_callback_id
) VALUES (
  $1::uuid,
  $2::uuid,
  'active',
  (
    SELECT COALESCE(array_agg(value::uuid), '{}'::uuid[])
    FROM jsonb_array_elements_text($3::jsonb) AS value
  ),
  0,
  $4::uuid,
  now(),
  $5::bigint,
  NULLIF($6::text, '')
)
RETURNING id;
""".strip(),
            1760,
            1560,
            query_replacement=(
                "={{ [$json.user_id, $json.lecture_id, $json.question_ids_json, "
                "$json.first_question.id, $json.chat_id, "
                "$('Run — Normalize').item.json.callback_query_id || ''] }}"
            ),
        )
    )
    nodes.append(code("Run — After Insert", after_insert_js(quiz_lib), 2000, 1560))
    nodes.append(
        tg_send_markup(
            "Out — First Question",
            2240,
            1560,
            item_expr="$json",
            n_rows=1,
            cols=4,
        )
    )
    nodes.append(tg_ack("Ack — Start", 2240, 1680))
    nodes.append(
        tg_send(
            "Out — No Questions",
            1880,
            1400,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
        )
    )
    link(connections, "Run — Switch Action", "Load — Candidates", src_output=cases.index("start_quiz"))
    link(connections, "Load — Candidates", "Run — Select Questions")
    link(connections, "Run — Select Questions", "IF — Has Questions")
    link(connections, "IF — Has Questions", "DB — Insert Session", src_output=0)
    link(connections, "IF — Has Questions", "Out — No Questions", src_output=1)
    link(connections, "DB — Insert Session", "Run — After Insert")
    link(connections, "Run — After Insert", "Out — First Question")
    link(connections, "Run — After Insert", "Ack — Start")

    # answer branch
    nodes.append(
        pg(
            "Load — Current Question",
            """
SELECT
  q.id,
  q.subtopic_id,
  s.title AS subtopic_title,
  q.stem,
  q.choice_a, q.choice_b, q.choice_c, q.choice_d,
  q.correct_choice,
  q.explanation,
  q.choice_explanations,
  q.source_kind,
  q.thesis,
  q.term_id,
  gt.canonical AS term_canonical,
  gt.definition AS term_definition
FROM psy.questions q
JOIN psy.subtopics s ON s.id = q.subtopic_id
LEFT JOIN psy.glossary_terms gt ON gt.id = q.term_id
WHERE q.id = $1::uuid;
""".strip(),
            1280,
            1880,
            query_replacement="={{ [$json.session.current_question_id] }}",
        )
    )
    nodes.append(code("Run — Grade Answer", grade_answer_js(quiz_lib), 1520, 1880))
    nodes.append(
        pg(
            "DB — Write Answer",
            """
WITH sess AS (
  UPDATE psy.quiz_sessions
  SET
    status = 'awaiting_next',
    correct_count = $3::int,
    answered_count = $4::int,
    miss_subtopics = (
      SELECT COALESCE(array_agg(value), '{}'::text[])
      FROM jsonb_array_elements_text($5::jsonb) AS value
    ),
    last_callback_id = NULLIF($6::text, ''),
    updated_at = now()
  WHERE id = $1::uuid
    AND user_id = $2::uuid
    AND status = 'active'
  RETURNING id
),
card AS (
  INSERT INTO psy.cards (user_id, question_id, term_id, last_review_at, reps)
  SELECT $2::uuid, $7::uuid, NULLIF($8::text, '')::uuid, now(), 1
  FROM sess
  ON CONFLICT (user_id, question_id) DO UPDATE
    SET last_review_at = now(),
        reps = psy.cards.reps + 1
  RETURNING id
)
INSERT INTO psy.reviews (
  card_id, user_id, rating, correct, chosen_choice, channel, response_ms, session_id
)
SELECT
  card.id,
  $2::uuid,
  $9::smallint,
  $10::boolean,
  $11::char,
  'telegram',
  NULLIF($12::text, '')::int,
  $1::uuid
FROM card
RETURNING id;
""".strip(),
            1760,
            1880,
            query_replacement=(
                "={{ [$json.session.id, $json.user_id, $json.correct_count, $json.answered_count, "
                "$json.miss_subtopics_json, $('Run — Normalize').item.json.callback_query_id || '', "
                "$json.question_id, $json.term_id || '', $json.rating, $json.correct, "
                "$json.chosen, $json.response_ms == null ? '' : String($json.response_ms)] }}"
            ),
        )
    )
    nodes.append(
        tg_send_markup(
            "Out — Feedback",
            2000,
            1880,
            item_expr="$('Run — Grade Answer').item.json",
            n_rows=1,
            cols=1,
        )
    )
    nodes.append(tg_ack("Ack — Answer", 2000, 2000))
    link(connections, "Run — Switch Action", "Load — Current Question", src_output=cases.index("answer"))
    link(connections, "Load — Current Question", "Run — Grade Answer")
    link(connections, "Run — Grade Answer", "DB — Write Answer")
    link(connections, "DB — Write Answer", "Out — Feedback")
    link(connections, "DB — Write Answer", "Ack — Answer")

    # next branch
    nodes.append(
        pg(
            "Load — Next Question",
            """
SELECT
  q.id,
  q.subtopic_id,
  s.title AS subtopic_title,
  q.stem,
  q.choice_a, q.choice_b, q.choice_c, q.choice_d,
  q.correct_choice,
  q.explanation,
  q.choice_explanations,
  q.source_kind,
  q.thesis,
  q.term_id,
  gt.canonical AS term_canonical,
  gt.definition AS term_definition
FROM psy.quiz_sessions qs
JOIN LATERAL (
  SELECT (qs.question_ids)[qs.cursor + 2] AS qid
) nxt ON TRUE
LEFT JOIN psy.questions q ON q.id = nxt.qid
LEFT JOIN psy.subtopics s ON s.id = q.subtopic_id
LEFT JOIN psy.glossary_terms gt ON gt.id = q.term_id
WHERE qs.id = $1::uuid AND qs.user_id = $2::uuid;
""".strip(),
            1280,
            2200,
            query_replacement="={{ [$json.session.id, $json.user_id] }}",
        )
    )
    nodes.append(code("Run — Next Step", next_js(quiz_lib), 1520, 2200))
    nodes.append(
        pg(
            "DB — Advance Or Finish",
            """
UPDATE psy.quiz_sessions
SET
  cursor = $3::int,
  current_question_id = NULLIF($4::text, '')::uuid,
  asked_at = CASE WHEN NULLIF($4::text, '') IS NULL THEN NULL ELSE now() END,
  status = CASE WHEN NULLIF($4::text, '') IS NULL THEN 'completed' ELSE 'active' END,
  last_callback_id = NULLIF($5::text, ''),
  updated_at = now()
WHERE id = $1::uuid
  AND user_id = $2::uuid
  AND status = 'awaiting_next'
RETURNING id, status;
""".strip(),
            1760,
            2200,
            query_replacement=(
                "={{ [$json.session.id, $json.user_id, $json.next_cursor, "
                "$json.next_question_id || '', "
                "$('Run — Normalize').item.json.callback_query_id || ''] }}"
            ),
        )
    )
    nodes.append(
        iff(
            "IF — Next Has Keyboard",
            "={{ String($('Run — Next Step').item.json.keyboard && $('Run — Next Step').item.json.keyboard.inline_keyboard ? $('Run — Next Step').item.json.keyboard.inline_keyboard.length : 0) }}",
            "0",
            1880,
            2200,
            op="notEquals",
        )
    )
    nodes.append(
        tg_send_markup(
            "Out — Next Question",
            2000,
            2200,
            item_expr="$('Run — Next Step').item.json",
            n_rows=1,
            cols=4,
        )
    )
    nodes.append(
        tg_send(
            "Out — Summary",
            2000,
            2320,
            text="={{ $('Run — Next Step').item.json.out_text }}",
            chat_id="={{ $('Run — Next Step').item.json.chat_id }}",
        )
    )
    nodes.append(tg_ack("Ack — Next", 2000, 2440))
    link(connections, "Run — Switch Action", "Load — Next Question", src_output=cases.index("next"))
    link(connections, "Load — Next Question", "Run — Next Step")
    link(connections, "Run — Next Step", "DB — Advance Or Finish")
    link(connections, "DB — Advance Or Finish", "IF — Next Has Keyboard")
    link(connections, "IF — Next Has Keyboard", "Out — Next Question", src_output=0)
    link(connections, "IF — Next Has Keyboard", "Out — Summary", src_output=1)
    link(connections, "DB — Advance Or Finish", "Ack — Next")

    # stale / noop — only ack callback if present
    nodes.append(tg_ack("Ack — Stale", 1280, 2480))
    nodes.append(tg_ack("Ack — Noop", 1280, 2600))
    link(connections, "Run — Switch Action", "Ack — Stale", src_output=cases.index("stale"))
    link(connections, "Run — Switch Action", "Ack — Noop", src_output=cases.index("noop"))

    # Wire head
    link(connections, "In — Telegram", "Run — Normalize")
    link(connections, "Run — Normalize", "Load — User Session")
    link(connections, "Load — User Session", "Run — Route")
    link(connections, "Run — Route", "Run — Switch Action")

    for n in nodes:
        if n["name"] in (
            "Load — Courses",
            "Load — Lectures",
            "Load — Candidates",
            "Load — Next Question",
            "Load — Current Question",
            "Load — Owned Courses",
            "Load — Invites",
            "DB — Create Invite",
            "DB — Revoke Invite",
            "DB — Redeem Invite",
            "DB — Ensure User",
        ):
            n["alwaysOutputData"] = True

    local_only = "--local" in sys.argv
    # Strip instance-local trigger path ids before writing JSON to git.
    _path_key = "web" + "hookId"
    for node in nodes:
        node.pop(_path_key, None)

    if local_only:
        id_path = WORKFLOWS / f"{SLUG}.id"
        workflow_id = id_path.read_text(encoding="utf-8").strip() if id_path.exists() else "local"
        save_local(SLUG, workflow_id, WF_NAME, nodes, connections)
        print(
            json.dumps(
                {
                    "action": "exported_local",
                    "id": workflow_id,
                    "name": WF_NAME,
                    "version": WF_VER,
                    "nodes": len(nodes),
                },
                ensure_ascii=False,
            )
        )
        return workflow_id

    workflow_id = ensure_workflow(WF_NAME, nodes, connections)
    for node in nodes:
        node.pop(_path_key, None)
    save_local(SLUG, workflow_id, WF_NAME, nodes, connections)
    # keep inactive
    detail = api_request("GET", f"/api/v1/workflows/{workflow_id}")
    if detail.get("active"):
        api_request("POST", f"/api/v1/workflows/{workflow_id}/deactivate")
    print(
        json.dumps(
            {
                "action": "built",
                "id": workflow_id,
                "name": WF_NAME,
                "active": False,
                "version": WF_VER,
                "nodes": len(nodes),
            },
            ensure_ascii=False,
        )
    )
    return workflow_id


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
