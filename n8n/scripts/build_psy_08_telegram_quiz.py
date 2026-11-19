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
WF_VER = "psy_08_telegram_quiz_v1"
SLUG = "psy_08_telegram_quiz"


def load_quiz_js() -> str:
    src = (LIB / "quiz_session.js").read_text(encoding="utf-8")
    src = src.replace("'use strict';", "")
    src = re.sub(r"module\.exports\s*=\s*\{[\s\S]*?\};?", "", src)
    return src.strip()


def tg_trigger(x: int, y: int) -> dict:
    return {
        "id": nid(),
        "name": "In — Telegram",
        "type": "n8n-nodes-base.telegramTrigger",
        "typeVersion": 1.2,
        "position": [x, y],
        "webhookId": nid(),
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
    keyboard: str | None = None,
) -> dict:
    params: dict = {
        "resource": "message",
        "operation": "sendMessage",
        "chatId": chat_id,
        "text": text,
        "additionalFields": {
            "appendAttribution": False,
            "parse_mode": "HTML",
        },
    }
    if keyboard:
        params["replyMarkup"] = "inlineKeyboard"
        params["inlineKeyboard"] = keyboard
    else:
        params["replyMarkup"] = "none"
    return {
        "id": nid(),
        "name": name,
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1.2,
        "position": [x, y],
        "credentials": TG,
        "parameters": params,
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
return [{
  json: {
    update_type: cq ? 'callback' : 'message',
    telegram_user_id: from.id || null,
    chat_id: chat.id || (from.id || null),
    text,
    command,
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
const allowed = userRow.allowed === true || userRow.allowed === 't' || userRow.allowed === true;
const userId = userRow.user_id || null;
const displayName = userRow.display_name || null;
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
  session,
  has_callback: !!norm.callback_query_id,
}};

function reply(action, text, extra = {{}}) {{
  return [{{ json: {{ ...base, action, out_text: text, keyboard: null, ...extra }} }}];
}}
function replyKb(action, text, keyboard, extra = {{}}) {{
  return [{{ json: {{ ...base, action, out_text: text, keyboard, ...extra }} }}];
}}

if (!norm.telegram_user_id || !norm.chat_id) {{
  return reply('noop', '');
}}

if (!allowed) {{
  if (norm.command === '/start' || norm.command === '/quiz' || norm.command === '/help' || norm.command === '/stop') {{
    return reply('pending_reg',
      'Здравствуйте! Бот пока доступен только участникам курса. Мы получили ваш запрос на доступ — дождитесь подтверждения и напишите /start ещё раз.');
  }}
  if (norm.update_type === 'callback') {{
    return reply('noop', '');
  }}
  return reply('refuse',
    'Извините, этот бот доступен только студентам курса. Если вы Наталья или Сергей — напишите Сергею для доступа.');
}}

const openStatuses = new Set(['picking_course','picking_lecture','active','awaiting_next']);
const hasOpen = !!(session.id && openStatuses.has(session.status));

if (norm.command === '/start') {{
  const name = displayName || 'студент';
  return reply('welcome',
    `Привет, ${{name}}!\\n\\nКоманды:\\n/quiz — квиз по лекции (10 вопросов)\\n/stop — прервать текущую сессию\\n/help — справка`);
}}

if (norm.command === '/help') {{
  return reply('help',
    'Квиз без подсказок модели: вопросы уже в базе.\\n/quiz — выбрать курс и лекцию\\n/stop — остановить сессию\\nОтветы — кнопками A–D.');
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
  return reply('pick_course', 'Выберите курс:');
}}

if (norm.update_type === 'callback') {{
  if (session.last_callback_id && norm.callback_query_id && session.last_callback_id === norm.callback_query_id) {{
    return reply('noop', '');
  }}
  const cb = parseCallback(norm.callback_data);
  if (cb.kind === 'course') {{
    return reply('pick_lecture', 'Выберите лекцию:', {{ course_id: cb.courseId }});
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
  'Не понял сообщение. Команды: /quiz, /stop, /help');
"""


def format_courses_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const courses = items.map((i) => i.json).filter((r) => r.id);
if (!courses.length) {{
  return [{{ json: {{ ...base, action: 'reply', out_text: 'Пока нет лекций с готовыми вопросами.', keyboard: null }} }}];
}}
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text: 'Выберите курс:',
  keyboard: coursesKeyboard(courses),
}} }}];
"""


def format_lectures_js(quiz_lib: str) -> str:
    return f"""
{quiz_lib}
const base = $('Run — Route').first().json;
const lectures = items.map((i) => i.json).filter((r) => r.id);
if (!lectures.length) {{
  return [{{ json: {{ ...base, action: 'reply', out_text: 'В этом курсе нет лекций с готовыми вопросами.', keyboard: null }} }}];
}}
return [{{ json: {{
  ...base,
  action: 'reply',
  out_text: 'Выберите лекцию:',
  keyboard: lecturesKeyboard(lectures),
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
            f"## {WF_NAME}\n`{WF_VER}`\nWhitelist = `psy.users`.\nНет LLM. Activate only this WF.",
            -400,
            200,
            360,
            200,
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
  (u.id IS NOT NULL) AS allowed,
  u.id AS user_id,
  u.display_name,
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
        "pending_reg",
        "refuse",
        "welcome",
        "help",
        "busy",
        "stop",
        "stop_idle",
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
    for i, action in enumerate(
        ["pending_reg", "refuse", "welcome", "help", "busy", "stop_idle"]
    ):
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
            780,
            query_replacement="={{ [$json.stop_session_id, $json.user_id] }}",
        )
    )
    nodes.append(
        tg_send(
            "Out — stop",
            1520,
            780,
            text="={{ $('Run — Route').item.json.out_text }}",
            chat_id="={{ $('Run — Route').item.json.chat_id }}",
        )
    )
    link(connections, "Run — Switch Action", "DB — Stop Session", src_output=cases.index("stop"))
    link(connections, "DB — Stop Session", "Out — stop")

    # pick_course
    nodes.append(
        pg(
            "Load — Courses",
            """
SELECT c.id, c.title
FROM psy.courses c
WHERE EXISTS (
  SELECT 1 FROM psy.lectures l
  WHERE l.course_id = c.id AND l.status = 'questions_ready'
)
ORDER BY c.title;
""".strip(),
            1280,
            1000,
        )
    )
    nodes.append(code("Run — Format Courses", format_courses_js(quiz_lib), 1520, 1000))
    nodes.append(
        tg_send(
            "Out — Courses",
            1760,
            1000,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
            keyboard="={{ $json.keyboard }}",
        )
    )
    nodes.append(tg_ack("Ack — Courses", 1760, 1120))
    link(connections, "Run — Switch Action", "Load — Courses", src_output=cases.index("pick_course"))
    link(connections, "Load — Courses", "Run — Format Courses")
    link(connections, "Run — Format Courses", "Out — Courses")
    link(connections, "Run — Format Courses", "Ack — Courses")

    # pick_lecture
    nodes.append(
        pg(
            "Load — Lectures",
            """
SELECT l.id, l.title, l.source_meta, l.status
FROM psy.lectures l
WHERE l.course_id = $1::uuid
  AND l.status = 'questions_ready'
ORDER BY COALESCE(l.source_meta #>> '{drive,folder_path,0}', l.title, l.id::text);
""".strip(),
            1280,
            1240,
            query_replacement="={{ [$json.course_id] }}",
        )
    )
    nodes.append(code("Run — Format Lectures", format_lectures_js(quiz_lib), 1520, 1240))
    nodes.append(
        tg_send(
            "Out — Lectures",
            1760,
            1240,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
            keyboard="={{ $json.keyboard }}",
        )
    )
    nodes.append(tg_ack("Ack — Lectures", 1760, 1360))
    # mark picking / close previous pick sessions lightly inside start; here just list
    link(connections, "Run — Switch Action", "Load — Lectures", src_output=cases.index("pick_lecture"))
    link(connections, "Load — Lectures", "Run — Format Lectures")
    link(connections, "Run — Format Lectures", "Out — Lectures")
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
ORDER BY s.ordinal, q.ordinal_in_subtopic NULLS LAST, q.created_at;
""".strip(),
            1280,
            1560,
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
        tg_send(
            "Out — First Question",
            2240,
            1560,
            text="={{ $json.out_text }}",
            chat_id="={{ $json.chat_id }}",
            keyboard="={{ $json.keyboard }}",
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
        tg_send(
            "Out — Feedback",
            2000,
            1880,
            text="={{ $('Run — Grade Answer').item.json.out_text }}",
            chat_id="={{ $('Run — Grade Answer').item.json.chat_id }}",
            keyboard="={{ $('Run — Grade Answer').item.json.keyboard }}",
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
        tg_send(
            "Out — Next Or Summary",
            2000,
            2200,
            text="={{ $('Run — Next Step').item.json.out_text }}",
            chat_id="={{ $('Run — Next Step').item.json.chat_id }}",
            keyboard="={{ $('Run — Next Step').item.json.keyboard }}",
        )
    )
    nodes.append(tg_ack("Ack — Next", 2000, 2320))
    link(connections, "Run — Switch Action", "Load — Next Question", src_output=cases.index("next"))
    link(connections, "Load — Next Question", "Run — Next Step")
    link(connections, "Run — Next Step", "DB — Advance Or Finish")
    link(connections, "DB — Advance Or Finish", "Out — Next Or Summary")
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
        ):
            n["alwaysOutputData"] = True

    workflow_id = ensure_workflow(WF_NAME, nodes, connections)
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
