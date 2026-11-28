'use strict';

/**
 * Lecture intake dialog (step 2, §4.3): a pure, channel-agnostic reducer.
 *
 *   step(session, event, ctx)            -> { session', outgoing[], effects[] }
 *   onEffectResult(session, result, ctx) -> { session', outgoing[], effects[] }
 *
 * No side effects: SQL and HTTP (whisper probe / upload) are performed by n8n
 * from the returned `effects`; their outcome is fed back through
 * `onEffectResult`. `session' === null` means "delete the session row".
 *
 * Session:  { step, data, last_event_id, expires_at }
 *   step:   pick_course | new_course_title | await_source | confirm
 *   data:   { to, courses[{id,title}], offset, course_id, course_title,
 *             new_course_title, source{kind,url}, card, request_id, pending }
 *   The user's link lives only in session.data.source (and later in
 *   lecture_requests.source); it is never put into any message text.
 *
 * ctx:      { now, courses[{id,title}], quota_remaining (number|null=unlimited),
 *             active_request {request_id,status,queue_position}|null,
 *             to {channel, external_chat_id} }
 *
 * Effects:  create_request | probe_source | upload_inbox | confirm_request |
 *           cancel_request | show_status
 *
 * Button ids (opaque, <= 48 bytes, prefix `new:`):
 *   new:course:<idx>  new:newcourse  new:page:<offset>  new:ok  new:cancel
 *
 * All user-facing text is plain Russian text (no HTML — adapters escape).
 */

const { LIMITS, extractUrls, classifySource, checkLimits } = require('./source_links');
const { normalizeIncoming, validateOutgoing, truncateCodePoints, codePointLength } = require('./channel_core');

const STEPS = Object.freeze(['pick_course', 'new_course_title', 'await_source', 'confirm']);
const PREFIX = 'new:';
const BTN = Object.freeze({
  OK: 'new:ok',
  CANCEL: 'new:cancel',
  NEW_COURSE: 'new:newcourse',
  course: (idx) => `new:course:${idx}`,
  page: (offset) => `new:page:${offset}`,
});

const MAX_STORED_COURSES = 100;
const MEDIA_EXT = ['mp3', 'm4a', 'wav', 'ogg', 'oga', 'opus', 'aac', 'flac', 'wma', 'mp4', 'm4v', 'mov', 'mkv', 'webm', 'avi', 'mpeg', 'mpg', '3gp'];
const TEXT_EXT = ['txt', 'md', 'markdown'];
const TEXT_MIME = ['text/plain', 'text/markdown', 'text/x-markdown'];

// ---------------------------------------------------------------- formatting

function formatSize(bytes) {
  if (bytes == null || bytes === '') return null;
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return null;
  const KB = 1024;
  const MB = KB * 1024;
  const GB = MB * 1024;
  const dec = (v) => (Math.round(v * 10) / 10).toString().replace('.', ',');
  if (n >= GB) return `${dec(n / GB)} ГБ`;
  if (n >= MB) return `${dec(n / MB)} МБ`;
  if (n >= KB) return `${Math.round(n / KB)} КБ`;
  return `${Math.round(n)} Б`;
}

function formatDuration(sec) {
  if (sec == null || sec === '') return null;
  const n = Number(sec);
  if (!Number.isFinite(n) || n < 0) return null;
  const minutes = Math.round(n / 60);
  if (minutes < 1) return 'меньше минуты';
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h && m) return `${h} ч ${m} мин`;
  if (h) return `${h} ч`;
  return `${m} мин`;
}

/**
 * Make a user-supplied string safe to show: links are replaced with «…»,
 * angle brackets and control characters removed, whitespace collapsed.
 */
function sanitizeText(str, max) {
  let s = String(str == null ? '' : str)
    .replace(/[a-zA-Z][a-zA-Z0-9+.-]*:\/\/\S+/g, '…')
    .replace(/\b(?:www\.|(?:disk\.yandex\.(?:ru|com)|yadi\.sk|drive\.google\.com)\/)\S*/gi, '…')
    .replace(/\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\/\S*/gi, '…');
  // eslint-disable-next-line no-control-regex
  s = s.replace(/[\x00-\x1f\x7f]+/g, ' ').replace(/[<>]/g, '').replace(/\s+/g, ' ').trim();
  return max ? truncateCodePoints(s, max) : s;
}

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

// -------------------------------------------------------------------- texts

const MAX_MIN = Math.round(LIMITS.max_duration_sec / 3600);
const CHAT_MB = Math.round(LIMITS.chat_file_max_bytes / (1024 * 1024));
const LINK_GB = Math.round(LIMITS.link_max_bytes / (1024 * 1024 * 1024));

const ERROR_TEXTS = Object.freeze({
  not_public:
    'Не получилось открыть файл: похоже, доступ по ссылке закрыт. Откройте доступ «всем, у кого есть ссылка» и пришлите ссылку ещё раз.',
  unsupported_link:
    'Эта ссылка не подходит. Нужна ссылка на один файл: публичная ссылка Яндекс Диска или Google Диска («доступ по ссылке») либо прямая https-ссылка на аудио или видео. Папки и документы Google пока не поддерживаются.',
  too_large: `Файл слишком большой: по ссылке можно до ${LINK_GB} ГБ, из чата — до ${CHAT_MB} МБ.`,
  too_long: `Запись длиннее ${MAX_MIN} ${plural(MAX_MIN, 'час', 'часа', 'часов')}: такие пока не обрабатываем. Разделите запись на части и добавьте их по отдельности.`,
  download_failed:
    'Не удалось скачать файл по ссылке. Проверьте, что ссылка открывается без входа в аккаунт, и попробуйте ещё раз чуть позже.',
  no_speech: 'В записи не нашлось речи, которую можно расшифровать.',
  empty_transcript: 'В записи не нашлось речи, которую можно расшифровать.',
  budget: 'Достигнут месячный лимит сервиса. Попробуйте добавить лекцию в начале следующего месяца.',
  timeout: 'Обработка заняла слишком много времени и была остановлена.',
  pipeline_failed: 'При обработке лекции что-то пошло не так.',
  internal: 'Произошла внутренняя ошибка. Попробуйте ещё раз чуть позже.',
  text_too_short: `Текста маловато: для лекции нужно не меньше ${LIMITS.text_min_chars} символов.`,
});

const CONFIRM_FAIL_TEXTS = Object.freeze({
  bad_state: 'Эта заявка уже недоступна. Чтобы начать заново, отправьте /new.',
  not_owner: 'Эта заявка недоступна. Чтобы начать заново, отправьте /new.',
  no_quota: 'Бесплатные лекции в квоте закончились, добавить новую пока нельзя.',
  busy_user: 'У вас уже есть лекция в обработке. Новую можно добавить, когда она будет готова (/status).',
  budget_stop: 'Достигнут месячный лимит сервиса. Попробуйте добавить лекцию в начале следующего месяца.',
  daily_cap: 'Сегодня сервис принял максимум лекций. Попробуйте завтра.',
  too_many_failures:
    'За последние сутки слишком много неудачных попыток. Попробуйте позже или напишите владельцу сервиса.',
});

const CREATE_FAIL_TEXTS = Object.freeze({
  duplicate: 'Такая лекция у вас уже есть (в очереди или обработана). Пришлите другой источник.',
  no_quota: 'Бесплатные лекции в квоте закончились, добавить новую пока нельзя.',
  busy_user: 'У вас уже есть лекция в обработке. Новую можно добавить, когда она будет готова (/status).',
  bad_course: 'Курс не найден. Чтобы начать заново, отправьте /new.',
});

function errorText(code) {
  return ERROR_TEXTS[code] || ERROR_TEXTS.pipeline_failed;
}

const T = Object.freeze({
  pickCourse: 'Выберите курс, в который добавить лекцию, или создайте новый.',
  askCourseTitle: `Как назвать новый курс? Напишите название сообщением (от ${LIMITS.course_title_min} до ${LIMITS.course_title_max} символов, без ссылок).`,
  noCoursesYet: 'У вас пока нет курсов — создадим первый.',
  courseTitleBad: `Название должно быть от ${LIMITS.course_title_min} до ${LIMITS.course_title_max} символов и без ссылок. Попробуйте ещё раз.`,
  noQuota: 'Бесплатные лекции в квоте закончились, добавить новую пока нельзя.',
  nothingToCancel: 'Сейчас нечего отменять.',
  cancelled: 'Добавление лекции отменено. Квота не затронута.',
  cannotCancelProcessing: 'Эту лекцию уже обрабатывают — отменить нельзя. Я напишу, когда она будет готова.',
  cancelRequestOk: 'Заявка отменена. Квота не списана.',
  cancelRequestNo: 'Не получилось отменить: лекция уже обрабатывается. Я напишу, когда она будет готова.',
  sessionExpired: `Время ожидания (${LIMITS.session_ttl_min} мин) вышло. Чтобы начать заново, отправьте /new.`,
  noSession: 'Чтобы добавить лекцию, отправьте /new.',
  staleButton: 'Эта кнопка устарела. Чтобы начать заново, отправьте /new.',
  unknownCommand: 'Не знаю такую команду. Добавить лекцию — /new, отменить — /cancel, статус — /status.',
  pressButtons: 'Выберите вариант кнопками ниже или отмените: /cancel.',
  pressConfirm: 'Нажмите «Начать», чтобы поставить лекцию в очередь, или «Отмена».',
  checkingLink: 'Проверяю ссылку, это займёт несколько секунд…',
  multipleLinks: 'В сообщении несколько ссылок — беру первую, остальные пропускаю.',
  uploadingFile: 'Принимаю файл, это займёт несколько секунд…',
  fileTooBig: `Файл больше ${CHAT_MB} МБ — через чат такой не отправить. Пришлите ссылку на файл (Яндекс Диск, Google Диск или прямую ссылку).`,
  fileUnsupported: `Такой файл не подходит. Пришлите аудио или видео (до ${CHAT_MB} МБ), текстовый файл .txt или .md, ссылку или сам текст лекции.`,
  needSource: `Не вижу ссылки. Пришлите ссылку на файл, небольшой файл (аудио или видео до ${CHAT_MB} МБ) или текст лекции — не короче ${LIMITS.text_min_chars} символов.`,
  textTooShort: (n) =>
    `Текста маловато: ${n} ${plural(n, 'символ', 'символа', 'символов')}, а для лекции нужно не меньше ${LIMITS.text_min_chars}. Пришлите ссылку, файл или текст подлиннее.`,
  textTooLong: `Текст слишком длинный: максимум ${LIMITS.text_max_chars} символов.`,
  queued: (pos) =>
    `Лекция принята и поставлена в очередь.${pos > 0 ? ` Перед вами: ${pos}.` : ''} Я напишу, когда она будет готова. Квота спишется, только если обработка завершится успешно. Статус — /status.`,
  statusNone: (quota) =>
    `Сейчас у вас нет лекций в обработке.${quotaLine(quota)} Добавить лекцию — /new.`,
  statusQueued: (pos) =>
    `Лекция в очереди.${pos > 0 ? ` Перед вами: ${pos}.` : ' Скоро начнём.'} Я напишу, когда она будет готова. Отменить — /cancel.`,
  statusProcessing: 'Лекция обрабатывается: это может занять от нескольких минут до часа. Я напишу, когда она будет готова.',
  busyNew: 'Новую лекцию можно добавить, когда текущая будет готова.',
});

function quotaLine(quota) {
  if (quota === undefined) return '';
  if (quota === null) return ' Квота без ограничений.';
  return ` Осталось лекций в квоте: ${quota}.`;
}

// ---------------------------------------------------------------- messages

function mk(to, text, opts = {}) {
  const m = { to: { channel: to.channel, external_chat_id: to.external_chat_id }, text };
  if (opts.buttons) m.buttons = opts.buttons;
  if (opts.expect !== undefined) m.expect = opts.expect;
  return m;
}

function cancelRow() {
  return [{ id: BTN.CANCEL, label: 'Отмена' }];
}

function courseLabel(title) {
  const t = String(title);
  return codePointLength(t) > LIMITS.button_label_max ? `${truncateCodePoints(t, LIMITS.button_label_max - 1)}…` : t;
}

function courseButtons(courses, offset) {
  const size = LIMITS.course_page_size;
  const start = Math.max(0, Math.min(Number(offset) || 0, Math.max(0, courses.length - 1)));
  const rows = [];
  for (let i = start; i < Math.min(courses.length, start + size); i += 1) {
    rows.push([{ id: BTN.course(i), label: courseLabel(courses[i].title) }]);
  }
  const nav = [];
  if (start > 0) nav.push({ id: BTN.page(Math.max(0, start - size)), label: '◀ Назад' });
  if (start + size < courses.length) nav.push({ id: BTN.page(start + size), label: 'Ещё ▶' });
  if (nav.length) rows.push(nav);
  rows.push([{ id: BTN.NEW_COURSE, label: '➕ Новый курс' }, ...cancelRow()]);
  return rows;
}

function pickCourseMessage(to, courses, offset) {
  return mk(to, T.pickCourse, { buttons: courseButtons(courses, offset), expect: null });
}

function askTitleMessage(to, hasCourses) {
  const text = hasCourses ? T.askCourseTitle : `${T.noCoursesYet}\n${T.askCourseTitle}`;
  return mk(to, text, { buttons: [cancelRow()], expect: 'text' });
}

function sourcePromptMessage(to, courseTitle, isNew) {
  const lines = [
    `Курс: «${courseTitle}»${isNew ? ' (новый)' : ''}.`,
    'Пришлите лекцию одним из способов:',
    '• ссылкой на файл: публичная ссылка Яндекс Диска или Google Диска («доступ по ссылке») либо прямая https-ссылка;',
    `• небольшим файлом: аудио или видео до ${CHAT_MB} МБ;`,
    `• текстом лекции (от ${LIMITS.text_min_chars} символов): сообщением или файлом .txt/.md.`,
    `Длительность записи — не больше ${MAX_MIN} ${plural(MAX_MIN, 'час', 'часа', 'часов')}.`,
  ];
  return mk(to, lines.join('\n'), { buttons: [cancelRow()], expect: 'link' });
}

const SOURCE_LABELS = Object.freeze({
  yandex_public: 'ссылка на Яндекс Диск',
  gdrive_public: 'ссылка на Google Диск',
  direct_url: 'прямая ссылка',
  chat_file: 'файл из чата',
  text: 'текст лекции',
});

/** Confirmation card: course, source summary, remaining quota, "charged only on success". */
function confirmationText(card, quotaRemaining) {
  const c = card || {};
  const lines = ['Всё проверено. Вот что получится:'];
  const course = c.course_title ? `«${c.course_title}»${c.is_new_course ? ' (новый курс)' : ''}` : null;
  if (course) lines.push(`Курс: ${course}`);
  lines.push(`Источник: ${SOURCE_LABELS[c.source_kind] || 'лекция'}`);
  if (c.name) lines.push(`Название: ${c.name}`);
  if (c.chars != null) lines.push(`Объём: ${c.chars} ${plural(c.chars, 'символ', 'символа', 'символов')}`);
  const size = formatSize(c.size_bytes);
  if (size) lines.push(`Размер: ${size}`);
  const dur = formatDuration(c.duration_sec);
  if (dur) lines.push(`Длительность: ${dur}`);
  if (quotaRemaining !== undefined) {
    lines.push(quotaRemaining === null ? 'Квота: без ограничений.' : `Осталось лекций в квоте: ${quotaRemaining}.`);
  }
  lines.push('Квота списывается только если лекция успешно обработана. Если не получится — квота останется.');
  lines.push('Нажмите «Начать», чтобы поставить лекцию в очередь.');
  return lines.join('\n');
}

function confirmationMessage(to, card, quotaRemaining) {
  return mk(to, confirmationText(card, quotaRemaining), {
    buttons: [[{ id: BTN.OK, label: '▶ Начать' }, ...cancelRow()]],
    expect: null,
  });
}

function statusMessage(to, active, quota) {
  if (active && active.status === 'processing') return mk(to, T.statusProcessing);
  if (active && active.status === 'queued') return mk(to, T.statusQueued(Number(active.queue_position) || 0));
  return mk(to, T.statusNone(quota));
}

// ------------------------------------------------------------------ session

function toMs(value) {
  if (value instanceof Date) return value.getTime();
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const t = Date.parse(value);
    if (!Number.isNaN(t)) return t;
  }
  return null;
}

function ctxNow(ctx) {
  const t = ctx ? toMs(ctx.now) : null;
  return t == null ? Date.now() : t;
}

function expiresIso(nowMs) {
  return new Date(nowMs + LIMITS.session_ttl_min * 60 * 1000).toISOString();
}

function makeSession(stepName, data, eventId, nowMs) {
  return { step: stepName, data, last_event_id: eventId == null ? null : String(eventId), expires_at: expiresIso(nowMs) };
}

function result(session, outgoing, effects) {
  const out = (outgoing || []).map((m) => validateOutgoing(m));
  return { session: session || null, outgoing: out, effects: effects || [] };
}

function cancelEffect(requestId, notify) {
  return { type: 'cancel_request', request_id: requestId, notify: !!notify };
}

function parseButton(id) {
  if (typeof id !== 'string' || !id.startsWith(PREFIX)) return null;
  if (id === BTN.OK) return { action: 'ok' };
  if (id === BTN.CANCEL) return { action: 'cancel' };
  if (id === BTN.NEW_COURSE) return { action: 'newcourse' };
  const m = /^new:(course|page):(\d{1,6})$/.exec(id);
  if (!m) return null;
  return m[1] === 'course' ? { action: 'course', idx: Number(m[2]) } : { action: 'page', offset: Number(m[2]) };
}

function normalizeCourses(list) {
  const out = [];
  for (const c of Array.isArray(list) ? list : []) {
    if (!c || c.id == null) continue;
    const title = sanitizeText(c.title, LIMITS.course_title_max) || 'Курс';
    out.push({ id: String(c.id), title });
    if (out.length >= MAX_STORED_COURSES) break;
  }
  return out;
}

function fileClass(file) {
  const name = String(file.name || '').toLowerCase();
  const ext = name.includes('.') ? name.split('.').pop() : '';
  const mime = String(file.mime || '').toLowerCase();
  if (TEXT_EXT.includes(ext) || TEXT_MIME.includes(mime)) return 'text';
  if (file.kind === 'audio' || file.kind === 'video') return 'media';
  if (mime.startsWith('audio/') || mime.startsWith('video/') || MEDIA_EXT.includes(ext)) return 'media';
  return null;
}

// ---------------------------------------------------------------- the reducer

function courseRef(data) {
  return { course_id: data.course_id || null, new_course_title: data.new_course_title || null };
}

function cardBase(data, extra) {
  return {
    course_title: data.course_title || data.new_course_title || null,
    is_new_course: !data.course_id,
    ...extra,
  };
}

function startNew(env) {
  const { to, ctx, event, now, session } = env;
  const active = ctx.active_request;
  if (active && (active.status === 'queued' || active.status === 'processing')) {
    return result(session, [statusMessage(to, active, ctx.quota_remaining), mk(to, T.busyNew)], []);
  }
  if (typeof ctx.quota_remaining === 'number' && ctx.quota_remaining <= 0) {
    return result(session, [mk(to, T.noQuota)], []);
  }
  const effects = [];
  if (session && session.data && session.data.request_id) effects.push(cancelEffect(session.data.request_id, false));
  const courses = normalizeCourses(ctx.courses);
  const data = { to, courses, offset: 0, pending: null };
  if (!courses.length) {
    return result(makeSession('new_course_title', data, event.event_id, now), [askTitleMessage(to, false)], effects);
  }
  return result(makeSession('pick_course', data, event.event_id, now), [pickCourseMessage(to, courses, 0)], effects);
}

function cancelFlow(env, { announce = true } = {}) {
  const { to, session } = env;
  const effects = [];
  if (session && session.data && session.data.request_id) effects.push(cancelEffect(session.data.request_id, false));
  return result(null, announce ? [mk(to, T.cancelled)] : [], effects);
}

function handleCommand(env) {
  const { event, to, ctx, session } = env;
  switch (event.command) {
    case 'new':
      return startNew(env);
    case 'cancel': {
      if (session) return cancelFlow(env);
      const active = ctx.active_request;
      if (active && active.status === 'queued' && active.request_id) {
        return result(null, [], [cancelEffect(active.request_id, true)]);
      }
      if (active && active.status === 'processing') return result(null, [mk(to, T.cannotCancelProcessing)], []);
      return result(null, [mk(to, T.nothingToCancel)], []);
    }
    case 'status': {
      const effects = ctx.active_request === undefined ? [{ type: 'show_status' }] : [];
      const outgoing = ctx.active_request === undefined ? [] : [statusMessage(to, ctx.active_request, ctx.quota_remaining)];
      return result(session, outgoing, effects);
    }
    default:
      return result(session, [mk(to, T.unknownCommand)], []);
  }
}

function sessionBump(env, patchData, stepName) {
  const { session, event, now } = env;
  return makeSession(stepName || session.step, { ...session.data, ...(patchData || {}) }, event.event_id, now);
}

function handleButton(env) {
  const { event, to, session } = env;
  const b = parseButton(event.button_id);
  if (!b) return result(session, [mk(to, T.staleButton)], []);
  if (b.action === 'cancel') return cancelFlow(env);

  const data = session.data || {};
  if (session.step === 'pick_course') {
    const courses = data.courses || [];
    if (b.action === 'page') {
      const offset = Math.min(b.offset, Math.max(0, courses.length - 1));
      return result(sessionBump(env, { offset }), [pickCourseMessage(to, courses, offset)], []);
    }
    if (b.action === 'newcourse') {
      return result(sessionBump(env, {}, 'new_course_title'), [askTitleMessage(to, true)], []);
    }
    if (b.action === 'course') {
      const course = courses[b.idx];
      if (!course) return result(sessionBump(env, {}), [mk(to, T.staleButton)], []);
      const next = sessionBump(env, { course_id: course.id, course_title: course.title, new_course_title: null }, 'await_source');
      return result(next, [sourcePromptMessage(to, course.title, false)], []);
    }
  }
  if (session.step === 'confirm' && b.action === 'ok') {
    if (!data.request_id) return result(sessionBump(env, {}), [mk(to, T.staleButton)], []);
    if (data.pending === 'confirm') return result(session, [], []); // double tap
    return result(sessionBump(env, { pending: 'confirm' }), [], [{ type: 'confirm_request', request_id: data.request_id }]);
  }
  return result(session, [mk(to, T.staleButton)], []);
}

function handleCourseTitle(env) {
  const { event, to } = env;
  const raw = String(event.text || '');
  const hadUrl = extractUrls(raw).length > 0;
  const title = sanitizeText(raw, LIMITS.course_title_max + 1);
  const len = codePointLength(title);
  if (hadUrl || title.includes('…') || len < LIMITS.course_title_min || len > LIMITS.course_title_max) {
    return result(sessionBump(env, {}), [mk(to, T.courseTitleBad, { buttons: [cancelRow()], expect: 'text' })], []);
  }
  const next = sessionBump(env, { new_course_title: title, course_id: null, course_title: title }, 'await_source');
  return result(next, [sourcePromptMessage(to, title, true)], []);
}

function startCreate(env, sourceKind, source, card) {
  const { session } = env;
  const data = session.data;
  const effect = { type: 'create_request', source_kind: sourceKind, source, ...courseRef(data) };
  const next = sessionBump(env, { pending: 'create', card: cardBase(data, { source_kind: sourceKind, ...card }) });
  return result(next, [], [effect]);
}

function handleSourceText(env) {
  const { event, to, session } = env;
  const text = String(event.text || '');
  const chars = codePointLength(text);

  if (chars >= LIMITS.text_min_chars) {
    const lim = checkLimits({ kind: 'text', text_chars: chars });
    if (!lim.ok) return result(sessionBump(env, {}), [mk(to, T.textTooLong, { buttons: [cancelRow()], expect: 'link' })], []);
    return startCreate(env, 'text', { text, chars }, { chars });
  }

  const urls = extractUrls(text);
  if (urls.length) {
    const cls = classifySource(urls[0]);
    if (cls.error) {
      return result(sessionBump(env, {}), [mk(to, errorText(cls.error), { buttons: [cancelRow()], expect: 'link' })], []);
    }
    const outgoing = [mk(to, T.checkingLink)];
    if (urls.length > 1) outgoing.unshift(mk(to, T.multipleLinks));
    const next = sessionBump(env, { pending: 'probe', source: { kind: cls.kind, url: cls.canonical } });
    return result(next, outgoing, [{ type: 'probe_source', source_kind: cls.kind, url: cls.canonical, maybe_folder: !!cls.maybe_folder }]);
  }

  const hint = chars >= 200 ? T.textTooShort(chars) : T.needSource;
  return result(sessionBump(env, {}), [mk(to, hint, { buttons: [cancelRow()], expect: 'link' })], []);
}

function handleSourceFile(env) {
  const { event, to } = env;
  const file = event.file;
  const retry = (text) => result(sessionBump(env, {}), [mk(to, text, { buttons: [cancelRow()], expect: 'link' })], []);
  const cls = fileClass(file);
  if (!cls) return retry(T.fileUnsupported);
  if (!checkLimits({ kind: 'chat_file', size_bytes: file.size_bytes }).ok) return retry(T.fileTooBig);
  const asText = cls === 'text';
  const next = sessionBump(env, { pending: 'upload', upload_as_text: asText });
  return result(next, [mk(to, T.uploadingFile)], [{ type: 'upload_inbox', file, as_text: asText }]);
}

/**
 * Reduce one incoming event.
 * @param {object|null} session
 * @param {object} rawEvent IncomingEvent (see channel_core)
 * @param {object} [ctx]
 */
function step(session, rawEvent, ctx = {}) {
  const event = normalizeIncoming(rawEvent);
  const to = { channel: event.channel, external_chat_id: event.external_chat_id };
  const now = ctxNow(ctx);

  // webhook retry: same event already applied to this session
  if (session && session.last_event_id != null && String(session.last_event_id) === event.event_id) {
    return result(session, [], []);
  }

  const expiresAt = session ? toMs(session.expires_at) : null;
  const expired = !!session && expiresAt != null && expiresAt <= now;
  const live = session && !expired ? session : null;
  const env = { event, to, ctx, now, session: live };

  if (event.kind === 'command') {
    if (expired && event.command !== 'new') {
      const effects = session.data && session.data.request_id ? [cancelEffect(session.data.request_id, false)] : [];
      if (event.command === 'cancel') return result(null, [mk(to, T.nothingToCancel)], effects);
      return handleCommand({ ...env, session: null });
    }
    return handleCommand(env);
  }

  if (!live) {
    const effects = expired && session.data && session.data.request_id ? [cancelEffect(session.data.request_id, false)] : [];
    return result(null, [mk(to, expired ? T.sessionExpired : event.kind === 'button' ? T.staleButton : T.noSession)], effects);
  }

  if (event.kind === 'button') return handleButton(env);

  const bump = (text, opts) => result(sessionBump(env, {}), [mk(to, text, opts)], []);
  switch (live.step) {
    case 'pick_course':
      return bump(T.pressButtons, { buttons: courseButtons(live.data.courses || [], live.data.offset || 0), expect: null });
    case 'new_course_title':
      return event.kind === 'text' ? handleCourseTitle(env) : bump(T.courseTitleBad, { buttons: [cancelRow()], expect: 'text' });
    case 'await_source':
      if (live.data.pending) return result(live, [], []); // probe/upload in flight
      return event.kind === 'file' ? handleSourceFile(env) : handleSourceText(env);
    case 'confirm':
      return bump(T.pressConfirm, { buttons: [[{ id: BTN.OK, label: '▶ Начать' }, ...cancelRow()]], expect: null });
    default:
      return result(null, [mk(to, T.noSession)], []);
  }
}

// ------------------------------------------------------- effect results

function stray(session, res) {
  // user cancelled while a request was being created: release it
  if (res && res.type === 'create_request' && res.ok && res.request_id) {
    return result(session, [], [cancelEffect(res.request_id, false)]);
  }
  return result(session, [], []);
}

function backToSource(session, extra) {
  const data = { ...session.data, pending: null, source: null, ...(extra || {}) };
  return { ...session, step: 'await_source', data };
}

function sourceError(session, to, text) {
  return result(backToSource(session), [mk(to, text, { buttons: [cancelRow()], expect: 'link' })], []);
}

function onProbe(session, res, to) {
  if (res.ok === false || res.ok == null) {
    return sourceError(session, to, errorText(res.error || 'download_failed'));
  }
  const kind = session.data.source && session.data.source.kind;
  const lim = checkLimits({ kind, size_bytes: res.size_bytes, duration_sec: res.duration_sec });
  if (!lim.ok) return sourceError(session, to, errorText(lim.error));
  const url = session.data.source.url;
  const source = {
    url,
    name: res.name ? sanitizeText(res.name, 120) : null,
    size_bytes: res.size_bytes == null ? null : res.size_bytes,
    duration_sec: res.duration_sec == null ? null : res.duration_sec,
    mime: res.mime || null,
  };
  const data = session.data;
  const effect = { type: 'create_request', source_kind: kind, source, ...courseRef(data) };
  const card = cardBase(data, {
    source_kind: kind,
    name: source.name,
    size_bytes: source.size_bytes,
    duration_sec: source.duration_sec,
  });
  return result({ ...session, data: { ...data, pending: 'create', card } }, [], [effect]);
}

function onUpload(session, res, to) {
  if (!res.ok) return sourceError(session, to, errorText(res.error || 'internal'));
  const data = session.data;
  const name = res.name ? sanitizeText(res.name, 120) : null;
  if (data.upload_as_text || res.as_text) {
    const text = String(res.text == null ? '' : res.text);
    const chars = codePointLength(text);
    const lim = checkLimits({ kind: 'text', text_chars: chars });
    if (!lim.ok) {
      return sourceError(session, to, lim.error === 'text_too_short' ? T.textTooShort(chars) : T.textTooLong);
    }
    const effect = { type: 'create_request', source_kind: 'text', source: { text, chars, name }, ...courseRef(data) };
    const card = cardBase(data, { source_kind: 'text', name, chars });
    return result({ ...session, data: { ...data, pending: 'create', card, upload_as_text: false } }, [], [effect]);
  }
  const source = {
    inbox_path: res.inbox_path || null,
    name,
    size_bytes: res.size_bytes == null ? null : res.size_bytes,
    mime: res.mime || null,
  };
  const effect = { type: 'create_request', source_kind: 'chat_file', source, ...courseRef(data) };
  const card = cardBase(data, { source_kind: 'chat_file', name, size_bytes: source.size_bytes });
  return result({ ...session, data: { ...data, pending: 'create', card } }, [], [effect]);
}

function onCreate(session, res, to, ctx) {
  const data = session.data;
  if (!res.ok) {
    const text = CREATE_FAIL_TEXTS[res.error] || errorText('internal');
    if (res.error === 'no_quota' || res.error === 'busy_user' || res.error === 'bad_course') {
      return result(null, [mk(to, text)], []);
    }
    return sourceError(session, to, text);
  }
  if (!res.request_id) return sourceError(session, to, errorText('internal'));
  const quota = res.quota_remaining !== undefined ? res.quota_remaining : ctx.quota_remaining;
  const next = { ...session, step: 'confirm', data: { ...data, pending: null, request_id: res.request_id, source: null } };
  return result(next, [confirmationMessage(to, data.card, quota)], []);
}

function onConfirm(session, res, to) {
  const data = session.data;
  if (res.ok) return result(null, [mk(to, T.queued(Number(res.queue_position) || 0))], []);
  const reason = res.reason;
  const text = CONFIRM_FAIL_TEXTS[reason] || errorText('internal');
  const effects = reason === 'bad_state' || reason === 'not_owner' ? [] : [cancelEffect(data.request_id, false)];
  return result(null, [mk(to, text)], effects);
}

/**
 * Feed the outcome of an effect back into the dialog.
 * @param {object|null} session
 * @param {{type: string, ok?: boolean}} res
 * @param {object} [ctx] { to, quota_remaining }
 */
function onEffectResult(session, res, ctx = {}) {
  if (!res || typeof res !== 'object') return result(session, [], []);
  const to = ctx.to || (session && session.data && session.data.to);

  if (res.type === 'cancel_request') {
    if (!res.notify || !to) return result(session, [], []);
    return result(session, [mk(to, res.ok ? T.cancelRequestOk : T.cancelRequestNo)], []);
  }
  if (!session || !session.data || !to) return stray(session, res);

  const pending = session.data.pending;
  if (res.type === 'probe_source' && pending === 'probe') return onProbe(session, res, to);
  if (res.type === 'upload_inbox' && pending === 'upload') return onUpload(session, res, to);
  if (res.type === 'create_request' && pending === 'create') return onCreate(session, res, to, ctx);
  if (res.type === 'confirm_request' && pending === 'confirm') return onConfirm(session, res, to);
  return stray(session, res);
}

// ------------------------------------------------------------ notifications

/**
 * Render an outbox notification into plain Russian text (no HTML, no URLs).
 * @param {'request_done'|'request_failed'|'request_expired'} kind
 * @param {object} [payload] { lecture_title, course_title, error_code, quota_remaining }
 * @returns {string}
 */
function renderNotification(kind, payload) {
  const p = payload && typeof payload === 'object' ? payload : {};
  const title = p.lecture_title ? sanitizeText(p.lecture_title, 120) : '';
  const named = title ? ` «${title}»` : '';
  switch (kind) {
    case 'request_done': {
      const q = quotaLine(p.quota_remaining);
      return `Лекция${named} готова! Можно проходить квиз: /quiz.${q}`;
    }
    case 'request_failed':
      return `Не получилось обработать лекцию${named}. ${errorText(p.error_code)} Квота не списана. Попробовать снова — /new.`;
    case 'request_expired':
      return 'Заявка на лекцию ждала подтверждения слишком долго и закрыта. Квота не списана. Чтобы начать заново, отправьте /new.';
    default: {
      const err = new Error('unknown_notification_kind');
      err.code = 'unknown_notification_kind';
      throw err;
    }
  }
}

module.exports = {
  STEPS,
  BTN,
  ERROR_TEXTS,
  CONFIRM_FAIL_TEXTS,
  step,
  onEffectResult,
  renderNotification,
  errorText,
  parseButton,
  confirmationText,
  statusMessage,
  sanitizeText,
  formatSize,
  formatDuration,
};
