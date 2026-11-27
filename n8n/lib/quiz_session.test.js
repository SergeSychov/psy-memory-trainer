'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  selectQuizQuestions,
  formatSummary,
  formatFeedback,
  formatQuestionMessage,
  lectureLabel,
  parseCallback,
  parseStartPayload,
  inviteRefusal,
  inviteRefusalText,
  welcomeText,
  helpText,
  buildInviteLink,
  buildInviteCoursesPage,
  buildInvitesListPage,
  keyboardVariant,
  pageKeyboardVariant,
  tallyAnswer,
  ratingForAnswer,
  computeResponseMs,
  coursesKeyboard,
  choiceKeyboard,
  buildCoursesPage,
  buildLecturesPage,
  COURSE_PAGE_SIZE,
  LECTURE_PAGE_SIZE,
  QUIZ_SIZE,
} = require('./quiz_session');

const UUID = '123e4567-e89b-12d3-a456-426614174000';

function q(id, subtopicId, answered) {
  return {
    id,
    subtopic_id: subtopicId,
    answered_before: !!answered,
    stem: `stem-${id}`,
    choice_a: 'a',
    choice_b: 'b',
    choice_c: 'c',
    choice_d: 'd',
    correct_choice: 'A',
    explanation: 'почему',
    source_kind: 'term',
    term_canonical: 'Термин',
    term_definition: 'Определение',
  };
}

test('selectQuizQuestions prefers unanswered and diversifies by subtopic', () => {
  const candidates = [];
  for (let s = 1; s <= 5; s += 1) {
    for (let i = 1; i <= 4; i += 1) {
      candidates.push(q(`u${s}-${i}`, `s${s}`, false));
    }
  }
  for (let i = 1; i <= 20; i += 1) {
    candidates.push(q(`a${i}`, 'sOld', true));
  }
  const picked = selectQuizQuestions(candidates, { limit: 10, seed: 'stable' });
  assert.equal(picked.length, 10);
  assert.ok(picked.every((item) => !item.answered_before));
  const subtopics = new Set(picked.map((item) => item.subtopic_id));
  assert.ok(subtopics.size >= 4, `expected broad subtopic mix, got ${subtopics.size}`);
});

test('selectQuizQuestions fills from answered when unanswered are short', () => {
  const candidates = [
    q('u1', 's1', false),
    q('u2', 's2', false),
    q('a1', 's3', true),
    q('a2', 's4', true),
    q('a3', 's5', true),
  ];
  const picked = selectQuizQuestions(candidates, { limit: 5, seed: 'fill' });
  assert.equal(picked.length, 5);
  assert.equal(picked.filter((item) => !item.answered_before).length, 2);
  assert.equal(picked.filter((item) => item.answered_before).length, 3);
});

test('selectQuizQuestions is deterministic for the same seed', () => {
  const candidates = [];
  for (let i = 0; i < 30; i += 1) {
    candidates.push(q(`id${i}`, `s${i % 6}`, i % 7 === 0));
  }
  const a = selectQuizQuestions(candidates, { limit: QUIZ_SIZE, seed: 'same' }).map((x) => x.id);
  const b = selectQuizQuestions(candidates, { limit: QUIZ_SIZE, seed: 'same' }).map((x) => x.id);
  assert.deepEqual(a, b);
});

test('formatSummary lists miss subtopics', () => {
  const text = formatSummary({
    correctCount: 7,
    total: 10,
    missSubtopics: ['Семья', 'Роли'],
  });
  assert.match(text, /7\/10/);
  assert.match(text, /Семья/);
  assert.match(text, /Роли/);
});

test('formatFeedback for wrong answer includes correct choice and term', () => {
  const text = formatFeedback({
    correct: false,
    chosen: 'B',
    question: {
      correct_choice: 'C',
      choice_c: 'верный ответ',
      explanation: 'краткое пояснение',
      source_kind: 'term',
      term_canonical: 'Аффект',
      term_definition: 'эмоциональная реакция',
    },
  });
  assert.match(text, /Неверно/);
  assert.match(text, /C/);
  assert.match(text, /верный ответ/);
  assert.match(text, /Аффект/);
  assert.match(text, /эмоциональная реакция/);
});

test('formatQuestionMessage shows A-D texts', () => {
  const text = formatQuestionMessage(
    {
      stem: 'Что сделать?',
      choice_a: 'один',
      choice_b: 'два',
      choice_c: 'три',
      choice_d: 'четыре',
    },
    0,
    10,
  );
  assert.match(text, /Вопрос 1 из 10/);
  assert.match(text, /A\..*один/);
  assert.match(text, /D\..*четыре/);
});

test('lectureLabel uses drive folder_path', () => {
  assert.equal(
    lectureLabel({
      title: 'audio_16k.opus',
      source_meta: { drive: { folder_path: ['2026-01-01 Лекция тест'] } },
    }),
    '2026-01-01 Лекция тест',
  );
});

test('parseCallback covers course/lecture/answer/next/page/noop', () => {
  assert.deepEqual(parseCallback('course:abc'), { kind: 'course', courseId: 'abc' });
  assert.deepEqual(parseCallback('lecture:xyz'), { kind: 'lecture', lectureId: 'xyz' });
  assert.deepEqual(parseCallback('ans:sid:B'), {
    kind: 'answer',
    sessionId: 'sid',
    choice: 'B',
  });
  assert.deepEqual(parseCallback('next:sid'), { kind: 'next', sessionId: 'sid' });
  assert.deepEqual(parseCallback('noop'), { kind: 'noop' });
  assert.deepEqual(parseCallback('page:courses:2'), { kind: 'page_courses', offset: 2 });
  assert.deepEqual(parseCallback(`page:lectures:${UUID}:4`), {
    kind: 'page_lectures',
    courseId: UUID,
    offset: 4,
  });
});

test('keyboardVariant keys itemsOnPage × nav_variant without pads', () => {
  assert.equal(keyboardVariant(2, 0, 4, 2).key, '2/none');
  assert.equal(keyboardVariant(1, 4, 4, 5).key, '1/prev');
  assert.equal(keyboardVariant(4, 0, 4, 9).key, '4/next');
  assert.equal(keyboardVariant(4, 4, 4, 9).key, '4/both');
  assert.equal(keyboardVariant(1, 0, 2, 1).key, '1/none');
  assert.equal(keyboardVariant(2, 0, 2, 5).key, '2/next');
  assert.equal(keyboardVariant(2, 2, 2, 5).key, '2/both');
  assert.equal(keyboardVariant(1, 4, 2, 5).key, '1/prev');
  assert.equal(keyboardVariant(2, 0, 2, 2).has_nav, false);
  assert.equal(keyboardVariant(2, 0, 2, 5).kb_rows, 3);
  assert.equal(keyboardVariant(2, 0, 2, 2).kb_rows, 2);
  assert.equal(pageKeyboardVariant(2, 0, 2, 5).nav_variant, 'next');
});

test('buildCoursesPage paginates without noop item pads', () => {
  const many = Array.from({ length: 5 }, (_, i) => ({
    id: `c${i}`,
    title: `Курс ${i}`,
  }));
  const page0 = buildCoursesPage(many, 0);
  assert.equal(page0.keyboard_variant, '2/next');
  assert.equal(page0.nav_variant, 'next');
  assert.equal(page0.kb_rows, COURSE_PAGE_SIZE + 1);
  assert.equal(page0.keyboard.inline_keyboard.length, COURSE_PAGE_SIZE + 1);
  assert.equal(page0.keyboard.inline_keyboard[0][0].callback_data, 'course:c0');
  assert.equal(page0.keyboard.inline_keyboard[1][0].callback_data, 'course:c1');
  assert.match(page0.out_text, /1–2 из 5/);
  const nav0 = page0.keyboard.inline_keyboard[COURSE_PAGE_SIZE];
  assert.equal(nav0.length, 1);
  assert.equal(nav0[0].callback_data, 'page:courses:2');
  assert.equal(nav0[0].text, 'Ещё');

  const page1 = buildCoursesPage(many, 2);
  assert.equal(page1.keyboard_variant, '2/both');
  assert.equal(page1.nav_variant, 'both');
  assert.equal(page1.keyboard.inline_keyboard[0][0].callback_data, 'course:c2');
  assert.equal(page1.keyboard.inline_keyboard[COURSE_PAGE_SIZE][0].callback_data, 'page:courses:0');
  assert.equal(page1.keyboard.inline_keyboard[COURSE_PAGE_SIZE][0].text, 'Назад');
  assert.equal(page1.keyboard.inline_keyboard[COURSE_PAGE_SIZE][1].callback_data, 'page:courses:4');

  const page2 = buildCoursesPage(many, 4);
  assert.equal(page2.keyboard_variant, '1/prev');
  assert.equal(page2.nav_variant, 'prev');
  assert.equal(page2.shown, 1);
  assert.equal(page2.keyboard.inline_keyboard.length, 2);
  assert.equal(page2.keyboard.inline_keyboard[0][0].callback_data, 'course:c4');
  assert.equal(page2.keyboard.inline_keyboard[1].length, 1);
  assert.equal(page2.keyboard.inline_keyboard[1][0].text, 'Назад');
  assert.ok(!page2.keyboard.inline_keyboard.some((row) => row[0]?.callback_data === 'noop'));
  assert.match(page2.out_text, /5–5 из 5/);

  const short = buildCoursesPage([{ id: 'only', title: 'Один' }], 0);
  assert.equal(short.keyboard_variant, '1/none');
  assert.equal(short.nav_variant, 'none');
  assert.equal(short.out_text, 'Выберите курс:');
  assert.equal(short.keyboard.inline_keyboard.length, 1);
  assert.equal(short.keyboard.inline_keyboard[0][0].callback_data, 'course:only');
});

test('buildLecturesPage paginates with course id in nav callbacks', () => {
  const lectures = Array.from({ length: 9 }, (_, i) => ({
    id: `l${i}`,
    title: `Лекция ${i}`,
  }));
  const page0 = buildLecturesPage(lectures, 0, UUID);
  assert.equal(page0.keyboard_variant, '4/next');
  assert.equal(page0.nav_variant, 'next');
  assert.equal(page0.kb_rows, LECTURE_PAGE_SIZE + 1);
  assert.equal(page0.keyboard.inline_keyboard.length, LECTURE_PAGE_SIZE + 1);
  assert.match(page0.out_text, /1–4 из 9/);
  const nextCb = page0.keyboard.inline_keyboard[LECTURE_PAGE_SIZE][0].callback_data;
  assert.equal(nextCb, `page:lectures:${UUID}:4`);
  assert.ok(Buffer.byteLength(nextCb, 'utf8') <= 64);

  const page1 = buildLecturesPage(lectures, 4, UUID);
  assert.equal(page1.keyboard_variant, '4/both');
  assert.equal(page1.nav_variant, 'both');
  assert.equal(page1.keyboard.inline_keyboard[0][0].callback_data, 'lecture:l4');
  assert.equal(
    page1.keyboard.inline_keyboard[LECTURE_PAGE_SIZE][0].callback_data,
    `page:lectures:${UUID}:0`,
  );
  assert.equal(
    page1.keyboard.inline_keyboard[LECTURE_PAGE_SIZE][1].callback_data,
    `page:lectures:${UUID}:8`,
  );

  const page2 = buildLecturesPage(lectures, 8, UUID);
  assert.equal(page2.keyboard_variant, '1/prev');
  assert.equal(page2.nav_variant, 'prev');
  assert.equal(page2.shown, 1);
  assert.equal(page2.keyboard.inline_keyboard.length, 2);
  assert.match(page2.out_text, /9–9 из 9/);
  assert.equal(page2.keyboard.inline_keyboard[1][0].text, 'Назад');
  assert.ok(!page2.keyboard.inline_keyboard.some((row) => row[0]?.text === '·'));

  const two = buildLecturesPage(lectures.slice(0, 2), 0, UUID);
  assert.equal(two.keyboard_variant, '2/none');
  assert.equal(two.keyboard.inline_keyboard.length, 2);
  assert.ok(!two.keyboard.inline_keyboard.some((row) => row[0]?.callback_data === 'noop'));
});

test('UUID course/lecture callback_data stay within Telegram 64-byte limit', () => {
  const courseCb = `course:${UUID}`;
  const lectureCb = `lecture:${UUID}`;
  const pageLectures = `page:lectures:${UUID}:999`;
  assert.ok(Buffer.byteLength(courseCb, 'utf8') <= 64);
  assert.ok(Buffer.byteLength(lectureCb, 'utf8') <= 64);
  assert.ok(Buffer.byteLength(pageLectures, 'utf8') <= 64);
  const kb = coursesKeyboard([{ id: UUID, title: 'Курс' }]);
  assert.equal(kb.inline_keyboard[0][0].callback_data, courseCb);
});

test('empty courses page helper still reports zero total (format node shows empty text)', () => {
  const empty = buildCoursesPage([], 0);
  assert.equal(empty.total, 0);
  assert.equal(empty.shown, 0);
  assert.equal(empty.keyboard_variant, '0/none');
  assert.equal(empty.out_text, 'Выберите курс:');
});

test('tallyAnswer and rating helpers', () => {
  const t = tallyAnswer({
    session: { correct_count: 2, answered_count: 3, miss_subtopics: ['A'] },
    correct: false,
    subtopicTitle: 'B',
  });
  assert.deepEqual(t, {
    correctCount: 2,
    answeredCount: 4,
    missSubtopics: ['A', 'B'],
  });
  assert.equal(ratingForAnswer(true), 3);
  assert.equal(ratingForAnswer(false), 1);
  assert.equal(
    computeResponseMs('2026-11-19T10:00:00.000Z', '2026-11-19T10:00:01.500Z'),
    1500,
  );
});

test('coursesKeyboard / choiceKeyboard use Telegram inline_keyboard shape', () => {
  const courses = coursesKeyboard([
    { id: 'c1', title: 'Курс один' },
    { id: 'c2', title: 'Курс два' },
  ]);
  assert.ok(Array.isArray(courses.inline_keyboard));
  assert.equal(courses.inline_keyboard.length, 2);
  assert.equal(courses.inline_keyboard[0][0].callback_data, 'course:c1');
  assert.equal(courses.rows, undefined);

  const choices = choiceKeyboard('sid');
  assert.equal(choices.inline_keyboard.length, 1);
  assert.deepEqual(
    choices.inline_keyboard[0].map((b) => b.text),
    ['A', 'B', 'C', 'D'],
  );
  assert.equal(choices.inline_keyboard[0][1].callback_data, 'ans:sid:B');
});

test('parseStartPayload handles plain, invite, bot suffix, bad code', () => {
  assert.deepEqual(parseStartPayload('/start'), { kind: 'plain' });
  assert.deepEqual(parseStartPayload('/start@natasha_study_bot'), { kind: 'plain' });
  assert.deepEqual(parseStartPayload('/start Abcd_1234'), {
    kind: 'invite',
    code: 'Abcd_1234',
  });
  assert.deepEqual(parseStartPayload('/start@bot Abcd_1234'), {
    kind: 'invite',
    code: 'Abcd_1234',
  });
  assert.equal(parseStartPayload('/start bad!').kind, 'bad_code');
  assert.equal(parseStartPayload('/start short').kind, 'bad_code');
  assert.equal(parseStartPayload('').kind, 'empty');
  assert.equal(parseStartPayload(`/start ${'a'.repeat(65)}`).kind, 'bad_code');
  assert.equal(parseStartPayload('/START').kind, 'plain');
  assert.equal(parseStartPayload('/start a b').kind, 'bad_code');
});

test('inviteRefusal and texts cover lifecycle', () => {
  assert.equal(inviteRefusal(null), 'not_found');
  assert.equal(inviteRefusal({ id: '1', revoked_at: '2026-01-01' }), 'revoked');
  assert.equal(
    inviteRefusal({ id: '1', expires_at: '2020-01-01T00:00:00.000Z' }, '2026-01-01'),
    'expired',
  );
  assert.equal(
    inviteRefusal({ id: '1', expires_at: '2099-01-01T00:00:00.000Z' }, '2026-01-01'),
    null,
  );
  assert.equal(inviteRefusal({ id: '1', max_uses: null, uses_count: 99 }), null);
  assert.equal(
    inviteRefusal({
      id: '1',
      revoked_at: '2026-01-01',
      expires_at: '2020-01-01T00:00:00.000Z',
    }),
    'revoked',
  );
  assert.equal(inviteRefusal({ id: '1', max_uses: 2, uses_count: 2 }), 'exhausted');
  assert.equal(inviteRefusal({ id: '1', max_uses: 2, uses_count: 1 }), null);
  assert.match(inviteRefusalText('revoked'), /отозвано/i);
  assert.match(inviteRefusalText('expired'), /истёк/i);
  assert.match(inviteRefusalText('exhausted'), /максимальное/i);
  assert.match(inviteRefusalText('not_found'), /не найдено/i);
  assert.match(inviteRefusalText('bad_code'), /Некорректный/i);
});

test('welcomeText and helpText owner lines depend on ownsCourses', () => {
  assert.match(welcomeText({ isNew: true }), /пространство/i);
  assert.match(welcomeText({ isNew: false, ownsCourses: false }), /Снова/i);
  assert.doesNotMatch(welcomeText({ isNew: false, ownsCourses: false }), /\/invite/);
  assert.match(welcomeText({ isNew: false, ownsCourses: true }), /\/invite, \/invites/);
  assert.match(
    welcomeText({ joinedCourse: true, courseTitle: 'Тест' }),
    /подключены к курсу «Тест»/,
  );
  const escaped = welcomeText({ joinedCourse: true, courseTitle: 'A<b>&' });
  assert.match(escaped, /A&lt;b&gt;&amp;/);
  assert.doesNotMatch(escaped, /<b>/);
  assert.doesNotMatch(helpText({ ownsCourses: false }), /\/invite/);
  assert.match(helpText({ ownsCourses: true }), /\/invite —/);
  assert.match(helpText({ ownsCourses: true }), /\/invites —/);
  const link = buildInviteLink('natasha_study_bot', 'Abcd1234_xy');
  assert.equal(link, 'https://t.me/natasha_study_bot?start=Abcd1234_xy');
  assert.equal(buildInviteLink('bot', 'short'), null);
  assert.ok(Buffer.byteLength('Abcd1234_xy', 'utf8') <= 64);
});

test('invite course/revoke callbacks stay within 64 bytes', () => {
  assert.deepEqual(parseCallback(`inv:course:${UUID}`), {
    kind: 'inv_course',
    courseId: UUID,
  });
  assert.deepEqual(parseCallback(`inv:revoke:${UUID}`), {
    kind: 'inv_revoke',
    inviteId: UUID,
  });
  assert.ok(Buffer.byteLength(`inv:course:${UUID}`, 'utf8') <= 64);
  assert.ok(Buffer.byteLength(`inv:revoke:${UUID}`, 'utf8') <= 64);

  const page = buildInviteCoursesPage(
    [
      { id: 'c1', title: 'A' },
      { id: 'c2', title: 'B' },
      { id: 'c3', title: 'C' },
    ],
    0,
  );
  assert.equal(page.keyboard.inline_keyboard[0][0].callback_data, 'inv:course:c1');
  assert.equal(page.keyboard_variant, '2/next');
  assert.equal(page.nav_variant, 'next');
  assert.equal(page.keyboard.inline_keyboard[COURSE_PAGE_SIZE][0].callback_data, 'page:invcourses:2');

  const list = buildInvitesListPage(
    [
      { id: UUID, course_title: 'A<b>&', uses_count: 1, max_uses: null, expires_at: null },
      {
        id: '223e4567-e89b-12d3-a456-426614174000',
        course_title: 'B',
        uses_count: 0,
        max_uses: 3,
        expires_at: null,
      },
    ],
    0,
  );
  assert.equal(list.keyboard_variant, '2/none');
  assert.equal(list.keyboard.inline_keyboard.length, 2);
  assert.equal(list.keyboard.inline_keyboard[0][0].text, 'Отозвать 1');
  assert.equal(list.keyboard.inline_keyboard[1][0].text, 'Отозвать 2');
  assert.equal(list.keyboard.inline_keyboard[0][0].callback_data, `inv:revoke:${UUID}`);
  assert.ok(!list.keyboard.inline_keyboard.some((row) => row[0]?.callback_data === 'noop'));
  assert.match(list.out_text, /Активные приглашения/);
  assert.match(list.out_text, /A&lt;b&gt;&amp;/);
  assert.doesNotMatch(list.out_text, /<b>/);
});
