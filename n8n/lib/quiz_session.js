'use strict';

/**
 * Telegram quiz helpers for psy_08.
 * Pure functions — embedded into n8n Code nodes and covered by node:test.
 */

const QUIZ_SIZE = 10;
const CHOICES = ['A', 'B', 'C', 'D'];

function hashSeed(str) {
  let h = 2166136261;
  const s = String(str || '');
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return function rng() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle(arr, rng) {
  const out = arr.slice();
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rng() * (i + 1));
    const tmp = out[i];
    out[i] = out[j];
    out[j] = tmp;
  }
  return out;
}

function lectureLabel(row) {
  const meta = row && row.source_meta;
  let folderPath = null;
  if (meta && typeof meta === 'object') {
    folderPath = meta.drive && meta.drive.folder_path;
  } else if (typeof meta === 'string' && meta.trim()) {
    try {
      const parsed = JSON.parse(meta);
      folderPath = parsed.drive && parsed.drive.folder_path;
    } catch (_e) {
      folderPath = null;
    }
  }
  if (Array.isArray(folderPath) && folderPath.length) {
    return String(folderPath[folderPath.length - 1]);
  }
  if (row && row.title && String(row.title).trim() && String(row.title) !== 'audio_16k.opus') {
    return String(row.title).trim();
  }
  return 'Лекция';
}

/**
 * Prefer unanswered questions, diversify by subtopic, fill to limit.
 * @param {Array<object>} candidates
 * @param {{limit?: number, seed?: string|number}} [opts]
 */
function selectQuizQuestions(candidates, opts = {}) {
  const limit = opts.limit == null ? QUIZ_SIZE : Number(opts.limit);
  const rng = mulberry32(hashSeed(opts.seed == null ? String(Date.now()) : opts.seed));
  const list = Array.isArray(candidates) ? candidates : [];
  const unanswered = list.filter((q) => !q.answered_before);
  const answered = list.filter((q) => q.answered_before);

  function diversify(pool) {
    const bySub = new Map();
    for (const q of shuffle(pool, rng)) {
      const key = String(q.subtopic_id || q.id);
      if (!bySub.has(key)) bySub.set(key, []);
      bySub.get(key).push(q);
    }
    const keys = shuffle([...bySub.keys()], rng);
    const out = [];
    let progressed = true;
    while (out.length < limit && progressed) {
      progressed = false;
      for (const key of keys) {
        const bucket = bySub.get(key);
        if (bucket && bucket.length) {
          out.push(bucket.shift());
          progressed = true;
          if (out.length >= limit) break;
        }
      }
    }
    return out;
  }

  let selected = diversify(unanswered);
  if (selected.length < limit) {
    const used = new Set(selected.map((q) => String(q.id)));
    const fill = diversify(answered.filter((q) => !used.has(String(q.id))));
    selected = selected.concat(fill).slice(0, limit);
  }
  return selected.slice(0, limit);
}

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function choiceText(question, letter) {
  const key = `choice_${String(letter).toLowerCase()}`;
  return question[key] || '';
}

function formatQuestionMessage(question, index, total) {
  const n = index + 1;
  const lines = [
    `<b>Вопрос ${n} из ${total}</b>`,
    '',
    escapeHtml(question.stem),
    '',
    `<b>A.</b> ${escapeHtml(choiceText(question, 'A'))}`,
    `<b>B.</b> ${escapeHtml(choiceText(question, 'B'))}`,
    `<b>C.</b> ${escapeHtml(choiceText(question, 'C'))}`,
    `<b>D.</b> ${escapeHtml(choiceText(question, 'D'))}`,
  ];
  return lines.join('\n');
}

/** Telegram Bot API reply_markup shape (not n8n Telegram-node fixedCollection). */
function inlineKeyboardRows(buttonsRows) {
  return {
    inline_keyboard: (buttonsRows || []).map((row) =>
      (row || []).map((btn) => ({
        text: String(btn.text || ''),
        callback_data: String(btn.callback_data || ''),
      })),
    ),
  };
}

function choiceKeyboard(sessionId) {
  return inlineKeyboardRows([
    CHOICES.map((letter) => ({
      text: letter,
      callback_data: `ans:${sessionId}:${letter}`,
    })),
  ]);
}

function nextKeyboard(sessionId) {
  return inlineKeyboardRows([[{ text: 'Дальше', callback_data: `next:${sessionId}` }]]);
}

/** Item rows per page (n8n Out keeps pageSize + 1 nav row fixed). */
const COURSE_PAGE_SIZE = 2;
const LECTURE_PAGE_SIZE = 4;

function coursesKeyboard(courses) {
  return inlineKeyboardRows(
    (courses || []).map((c) => [
      {
        text: String(c.title || 'Курс').slice(0, 60),
        callback_data: `course:${c.id}`,
      },
    ]),
  );
}

function lecturesKeyboard(lectures) {
  return inlineKeyboardRows(
    (lectures || []).map((l) => [
      {
        text: lectureLabel(l).slice(0, 60),
        callback_data: `lecture:${l.id}`,
      },
    ]),
  );
}

function pageCaption(prefix, offset, pageSize, total) {
  if (!total) return prefix;
  if (total <= pageSize) return prefix;
  const from = offset + 1;
  const to = Math.min(offset + pageSize, total);
  return `${prefix} (${from}–${to} из ${total})`;
}

/**
 * Fixed Telegram keyboard layout key: "<itemsOnPage>/<nav_variant>".
 * No noop item pads — outs are selected by this key in n8n.
 */
function keyboardVariant(itemsOnPage, offset, pageSize, total) {
  const size = Math.max(1, Number(pageSize) || 1);
  const start = Math.max(0, Number(offset) || 0);
  const tot = Math.max(0, Number(total) || 0);
  const items = Math.max(0, Math.min(Number(itemsOnPage) || 0, size));
  const hasPrev = start > 0;
  const hasNext = start + size < tot;
  let nav_variant = 'none';
  if (hasPrev && hasNext) nav_variant = 'both';
  else if (hasPrev) nav_variant = 'prev';
  else if (hasNext) nav_variant = 'next';
  return {
    key: `${items}/${nav_variant}`,
    items_on_page: items,
    nav_variant,
    has_nav: nav_variant !== 'none',
    has_prev: hasPrev,
    has_next: hasNext,
    kb_rows: items + (nav_variant === 'none' ? 0 : 1),
  };
}

/** @deprecated alias — prefer keyboardVariant */
function pageKeyboardVariant(itemsCount, offset, pageSize, total) {
  const v = keyboardVariant(itemsCount, offset, pageSize, total);
  return {
    ...v,
    item_rows: v.items_on_page,
    shown: v.items_on_page,
  };
}

function navButtons({ kind, offset, pageSize, total, courseId }) {
  const start = Math.max(0, Number(offset) || 0);
  const size = Math.max(1, Number(pageSize) || 1);
  const prevOffset = Math.max(0, start - size);
  const nextOffset = start + size;
  const variant = keyboardVariant(0, start, size, total);
  let prevCb = 'noop';
  let nextCb = 'noop';
  if (kind === 'courses') {
    prevCb = `page:courses:${prevOffset}`;
    nextCb = `page:courses:${nextOffset}`;
  } else if (kind === 'inv_courses') {
    prevCb = `page:invcourses:${prevOffset}`;
    nextCb = `page:invcourses:${nextOffset}`;
  } else if (kind === 'invites') {
    prevCb = `page:invites:${prevOffset}`;
    nextCb = `page:invites:${nextOffset}`;
  } else {
    const id = String(courseId || '');
    prevCb = `page:lectures:${id}:${prevOffset}`;
    nextCb = `page:lectures:${id}:${nextOffset}`;
  }
  const buttons = [];
  if (variant.has_prev) {
    buttons.push({ text: 'Назад', callback_data: prevCb });
  }
  if (variant.has_next) {
    buttons.push({ text: 'Ещё', callback_data: nextCb });
  }
  return buttons;
}

function clampPageOffset(offset, pageSize, total) {
  const size = Math.max(1, Number(pageSize) || 1);
  let start = Math.max(0, Number(offset) || 0);
  if (total <= 0) return 0;
  const maxStart = Math.max(0, Math.floor((total - 1) / size) * size);
  if (start > maxStart) start = maxStart;
  return start;
}

function finalizePagedKeyboard(keyboard, { kind, offset, pageSize, total, courseId, itemsOnPage }) {
  const items =
    itemsOnPage == null
      ? (keyboard.inline_keyboard || []).length
      : Number(itemsOnPage) || 0;
  const variant = keyboardVariant(items, offset, pageSize, total);
  if (variant.has_nav) {
    keyboard.inline_keyboard.push(
      navButtons({ kind, offset, pageSize, total, courseId }),
    );
  }
  return {
    ...variant,
    keyboard,
  };
}

function buildCoursesPage(courses, offset) {
  const all = (courses || []).filter((c) => c && c.id);
  const total = all.length;
  const pageSize = COURSE_PAGE_SIZE;
  const start = clampPageOffset(offset, pageSize, total);
  const page = all.slice(start, start + pageSize);
  const keyboard = coursesKeyboard(page);
  const fin = finalizePagedKeyboard(keyboard, {
    kind: 'courses',
    offset: start,
    pageSize,
    total,
    itemsOnPage: page.length,
  });
  return {
    keyboard: fin.keyboard,
    total,
    offset: start,
    shown: page.length,
    pageSize,
    kb_rows: fin.kb_rows,
    nav_variant: fin.nav_variant,
    has_nav: fin.has_nav,
    items_on_page: fin.items_on_page,
    keyboard_variant: fin.key,
    out_text: pageCaption('Выберите курс:', start, pageSize, total),
  };
}

function buildLecturesPage(lectures, offset, courseId) {
  const all = (lectures || []).filter((l) => l && l.id);
  const total = all.length;
  const pageSize = LECTURE_PAGE_SIZE;
  const start = clampPageOffset(offset, pageSize, total);
  const page = all.slice(start, start + pageSize);
  const keyboard = lecturesKeyboard(page);
  const fin = finalizePagedKeyboard(keyboard, {
    kind: 'lectures',
    offset: start,
    pageSize,
    total,
    courseId,
    itemsOnPage: page.length,
  });
  return {
    keyboard: fin.keyboard,
    total,
    offset: start,
    shown: page.length,
    pageSize,
    kb_rows: fin.kb_rows,
    nav_variant: fin.nav_variant,
    has_nav: fin.has_nav,
    items_on_page: fin.items_on_page,
    keyboard_variant: fin.key,
    out_text: pageCaption('Выберите лекцию:', start, pageSize, total),
  };
}

function explanationFor(question, chosen) {
  const map = question.choice_explanations;
  if (map && typeof map === 'object') {
    const fromChoice = map[chosen] || map[String(chosen).toLowerCase()];
    if (fromChoice) return String(fromChoice);
  }
  if (question.explanation) return String(question.explanation);
  return '';
}

function anchorLine(question) {
  if (question.source_kind === 'term') {
    const term = question.term_canonical || 'термин';
    const def = question.term_definition || '';
    if (def) {
      return `Термин: <b>${escapeHtml(term)}</b> — ${escapeHtml(def)}`;
    }
    return `Термин: <b>${escapeHtml(term)}</b>`;
  }
  const thesis = question.thesis || '';
  if (thesis) {
    return `Тезис: ${escapeHtml(thesis)}`;
  }
  return '';
}

function formatFeedback({ correct, chosen, question }) {
  const right = String(question.correct_choice || '').toUpperCase();
  const pick = String(chosen || '').toUpperCase();
  const expl = explanationFor(question, pick);
  const lines = [];
  if (correct) {
    lines.push('<b>Верно</b>');
  } else {
    lines.push('<b>Неверно</b>');
    lines.push(`Правильный вариант: <b>${escapeHtml(right)}</b>. ${escapeHtml(choiceText(question, right))}`);
  }
  if (expl) {
    lines.push('');
    lines.push(escapeHtml(expl));
  }
  const anchor = anchorLine(question);
  if (anchor) {
    lines.push('');
    lines.push(anchor);
  }
  return lines.join('\n');
}

function formatSummary({ correctCount, total, missSubtopics }) {
  const lines = [`<b>Итог: ${correctCount}/${total}</b>`];
  const misses = (missSubtopics || []).filter(Boolean);
  if (misses.length) {
    lines.push('');
    lines.push('Подтемы с ошибками:');
    for (const title of misses) {
      lines.push(`• ${escapeHtml(title)}`);
    }
  } else {
    lines.push('');
    lines.push('Ошибок по подтемам нет — отличная сессия.');
  }
  lines.push('');
  lines.push('Новая сессия: /quiz');
  return lines.join('\n');
}

function ratingForAnswer(correct) {
  return correct ? 3 : 1;
}

function computeResponseMs(askedAt, now) {
  if (!askedAt) return null;
  const start = new Date(askedAt).getTime();
  const end = now ? new Date(now).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return Math.max(0, Math.round(end - start));
}

const INVITE_CODE_RE = /^[A-Za-z0-9_-]{8,64}$/;

function parseStartPayload(text) {
  const raw = String(text || '').trim();
  if (!raw) return { kind: 'empty' };
  const parts = raw.split(/\s+/).filter(Boolean);
  const cmd = String(parts[0] || '').split('@')[0].toLowerCase();
  if (cmd !== '/start') return { kind: 'not_start' };
  if (parts.length === 1) return { kind: 'plain' };
  const code = parts.slice(1).join('');
  if (!INVITE_CODE_RE.test(code)) return { kind: 'bad_code' };
  return { kind: 'invite', code };
}

function inviteRefusal(invite, now) {
  if (!invite || !invite.id) return 'not_found';
  if (invite.revoked_at) return 'revoked';
  const ts = now ? new Date(now).getTime() : Date.now();
  if (invite.expires_at) {
    const exp = new Date(invite.expires_at).getTime();
    if (Number.isFinite(exp) && exp <= ts) return 'expired';
  }
  if (invite.max_uses != null) {
    const max = Number(invite.max_uses);
    const used = Number(invite.uses_count || 0);
    if (Number.isFinite(max) && used >= max) return 'exhausted';
  }
  return null;
}

function inviteRefusalText(reason) {
  switch (reason) {
    case 'revoked':
      return 'Это приглашение отозвано.';
    case 'expired':
      return 'Срок действия приглашения истёк.';
    case 'exhausted':
      return 'Приглашение уже использовали максимальное число раз.';
    case 'bad_code':
      return 'Некорректный код приглашения. Проверьте ссылку и попробуйте ещё раз.';
    case 'not_found':
    default:
      return 'Приглашение не найдено. Проверьте ссылку или попросите новую у владельца курса.';
  }
}

function helpText({ ownsCourses } = {}) {
  let text =
    'Квиз без подсказок модели: вопросы уже в базе.\n/quiz — выбрать курс и лекцию\n/stop — остановить сессию\nОтветы — кнопками A–D.';
  if (ownsCourses) {
    text +=
      '\n/invite — ссылка-приглашение в свой курс\n/invites — активные приглашения и отзыв';
  }
  return text;
}

function welcomeText({ isNew, joinedCourse, courseTitle, ownsCourses } = {}) {
  if (joinedCourse) {
    const title = escapeHtml(courseTitle ? String(courseTitle) : 'курс');
    return `Вы подключены к курсу «${title}».\n\nКоманды:\n/quiz — квиз по лекции\n/stop — прервать сессию\n/help — справка`;
  }
  if (isNew) {
    return 'Добро пожаловать!\n\nУ вас своё пространство с бесплатной квотой на лекции.\n/quiz — квиз (если есть доступные курсы)\n/help — справка\n\nЕсли есть ссылка-приглашение — откройте её, чтобы подключиться к курсу.';
  }
  let text =
    'Снова здравствуйте!\n\nКоманды:\n/quiz — квиз по лекции\n/stop — прервать сессию\n/help — справка';
  if (ownsCourses) {
    text += '\n/invite, /invites — для владельца курса';
  }
  return text;
}

function buildInviteLink(botUsername, code) {
  const bot = String(botUsername || '').replace(/^@/, '');
  const payload = String(code || '');
  if (!bot || !INVITE_CODE_RE.test(payload)) return null;
  // Invite alphabet is ASCII; Telegram start payload max 64 bytes.
  if (payload.length > 64) return null;
  return `https://t.me/${bot}?start=${payload}`;
}

function inviteCoursesKeyboard(courses) {
  return inlineKeyboardRows(
    (courses || []).map((c) => [
      {
        text: String(c.title || 'Курс').slice(0, 60),
        callback_data: `inv:course:${c.id}`,
      },
    ]),
  );
}

function buildInviteCoursesPage(courses, offset) {
  const all = (courses || []).filter((c) => c && c.id);
  const total = all.length;
  const pageSize = COURSE_PAGE_SIZE;
  const start = clampPageOffset(offset, pageSize, total);
  const page = all.slice(start, start + pageSize);
  const keyboard = inviteCoursesKeyboard(page);
  const fin = finalizePagedKeyboard(keyboard, {
    kind: 'inv_courses',
    offset: start,
    pageSize,
    total,
    itemsOnPage: page.length,
  });
  return {
    keyboard: fin.keyboard,
    total,
    offset: start,
    shown: page.length,
    pageSize,
    kb_rows: fin.kb_rows,
    nav_variant: fin.nav_variant,
    has_nav: fin.has_nav,
    items_on_page: fin.items_on_page,
    keyboard_variant: fin.key,
    out_text: pageCaption('Выберите курс для приглашения:', start, pageSize, total),
  };
}

function invitesListKeyboard(invites, startIndex = 0) {
  return inlineKeyboardRows(
    (invites || []).map((inv, i) => [
      {
        text: `Отозвать ${startIndex + i + 1}`,
        callback_data: `inv:revoke:${inv.id}`,
      },
    ]),
  );
}

function formatInviteListLines(invites, startIndex = 0) {
  const lines = ['Активные приглашения:'];
  const list = invites || [];
  if (!list.length) {
    lines.push('Пока нет. Создайте: /invite');
    return lines.join('\n');
  }
  list.forEach((inv, i) => {
    const title = escapeHtml(String(inv.course_title || 'Курс')).slice(0, 40);
    const used = Number(inv.uses_count || 0);
    const max = inv.max_uses == null ? '∞' : String(inv.max_uses);
    let exp = 'без срока';
    if (inv.expires_at) {
      const d = new Date(inv.expires_at);
      if (Number.isFinite(d.getTime())) {
        exp = d.toISOString().slice(0, 10);
      }
    }
    lines.push(`${startIndex + i + 1}. ${title} — ${used}/${max}, до ${exp}`);
  });
  lines.push('');
  lines.push('Кнопка «Отозвать N» под сообщением — по номеру в списке.');
  return lines.join('\n');
}

function buildInvitesListPage(invites, offset) {
  const all = (invites || []).filter((i) => i && i.id);
  const total = all.length;
  const pageSize = LECTURE_PAGE_SIZE;
  const start = clampPageOffset(offset, pageSize, total);
  const page = all.slice(start, start + pageSize);
  if (!total) {
    return {
      keyboard: { inline_keyboard: [] },
      total: 0,
      offset: 0,
      shown: 0,
      pageSize,
      kb_rows: 0,
      nav_variant: 'none',
      has_nav: false,
      items_on_page: 0,
      keyboard_variant: '0/none',
      out_text: formatInviteListLines([], 0),
    };
  }
  const keyboard = invitesListKeyboard(page, start);
  const fin = finalizePagedKeyboard(keyboard, {
    kind: 'invites',
    offset: start,
    pageSize,
    total,
    itemsOnPage: page.length,
  });
  return {
    keyboard: fin.keyboard,
    total,
    offset: start,
    shown: page.length,
    pageSize,
    kb_rows: fin.kb_rows,
    nav_variant: fin.nav_variant,
    has_nav: fin.has_nav,
    items_on_page: fin.items_on_page,
    keyboard_variant: fin.key,
    out_text: formatInviteListLines(page, start),
  };
}

function parseCallback(data) {
  const raw = String(data || '');
  if (raw === 'noop') {
    return { kind: 'noop' };
  }
  if (raw.startsWith('page:courses:')) {
    const offset = Number(raw.slice('page:courses:'.length));
    if (!Number.isFinite(offset) || offset < 0) return { kind: 'unknown', raw };
    return { kind: 'page_courses', offset: Math.floor(offset) };
  }
  if (raw.startsWith('page:invcourses:')) {
    const offset = Number(raw.slice('page:invcourses:'.length));
    if (!Number.isFinite(offset) || offset < 0) return { kind: 'unknown', raw };
    return { kind: 'page_inv_courses', offset: Math.floor(offset) };
  }
  if (raw.startsWith('page:invites:')) {
    const offset = Number(raw.slice('page:invites:'.length));
    if (!Number.isFinite(offset) || offset < 0) return { kind: 'unknown', raw };
    return { kind: 'page_invites', offset: Math.floor(offset) };
  }
  if (raw.startsWith('page:lectures:')) {
    const rest = raw.slice('page:lectures:'.length);
    const idx = rest.lastIndexOf(':');
    if (idx <= 0) return { kind: 'unknown', raw };
    const courseId = rest.slice(0, idx);
    const offset = Number(rest.slice(idx + 1));
    if (!courseId || !Number.isFinite(offset) || offset < 0) return { kind: 'unknown', raw };
    return { kind: 'page_lectures', courseId, offset: Math.floor(offset) };
  }
  if (raw.startsWith('inv:course:')) {
    return { kind: 'inv_course', courseId: raw.slice('inv:course:'.length) };
  }
  if (raw.startsWith('inv:revoke:')) {
    return { kind: 'inv_revoke', inviteId: raw.slice('inv:revoke:'.length) };
  }
  if (raw.startsWith('course:')) {
    return { kind: 'course', courseId: raw.slice(7) };
  }
  if (raw.startsWith('lecture:')) {
    return { kind: 'lecture', lectureId: raw.slice(8) };
  }
  if (raw.startsWith('ans:')) {
    const parts = raw.split(':');
    if (parts.length !== 3) return { kind: 'unknown', raw };
    return {
      kind: 'answer',
      sessionId: parts[1],
      choice: String(parts[2] || '').toUpperCase(),
    };
  }
  if (raw.startsWith('next:')) {
    return { kind: 'next', sessionId: raw.slice(5) };
  }
  return { kind: 'unknown', raw };
}

function uniquePreserve(list) {
  const out = [];
  const seen = new Set();
  for (const item of list || []) {
    const key = String(item);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

function tallyAnswer({ session, correct, subtopicTitle }) {
  const correctCount = Number(session.correct_count || 0) + (correct ? 1 : 0);
  const answeredCount = Number(session.answered_count || 0) + 1;
  const miss = uniquePreserve(
    correct ? session.miss_subtopics || [] : [...(session.miss_subtopics || []), subtopicTitle].filter(Boolean),
  );
  return { correctCount, answeredCount, missSubtopics: miss };
}

module.exports = {
  QUIZ_SIZE,
  CHOICES,
  COURSE_PAGE_SIZE,
  LECTURE_PAGE_SIZE,
  INVITE_CODE_RE,
  hashSeed,
  mulberry32,
  shuffle,
  lectureLabel,
  selectQuizQuestions,
  escapeHtml,
  formatQuestionMessage,
  inlineKeyboardRows,
  choiceKeyboard,
  nextKeyboard,
  coursesKeyboard,
  lecturesKeyboard,
  inviteCoursesKeyboard,
  invitesListKeyboard,
  pageCaption,
  keyboardVariant,
  pageKeyboardVariant,
  navButtons,
  clampPageOffset,
  finalizePagedKeyboard,
  buildCoursesPage,
  buildLecturesPage,
  buildInviteCoursesPage,
  buildInvitesListPage,
  formatInviteListLines,
  explanationFor,
  formatFeedback,
  formatSummary,
  ratingForAnswer,
  computeResponseMs,
  parseStartPayload,
  inviteRefusal,
  inviteRefusalText,
  helpText,
  welcomeText,
  buildInviteLink,
  parseCallback,
  uniquePreserve,
  tallyAnswer,
};
