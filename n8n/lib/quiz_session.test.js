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
  tallyAnswer,
  ratingForAnswer,
  computeResponseMs,
  coursesKeyboard,
  choiceKeyboard,
  QUIZ_SIZE,
} = require('./quiz_session');

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

test('parseCallback covers course/lecture/answer/next', () => {
  assert.deepEqual(parseCallback('course:abc'), { kind: 'course', courseId: 'abc' });
  assert.deepEqual(parseCallback('lecture:xyz'), { kind: 'lecture', lectureId: 'xyz' });
  assert.deepEqual(parseCallback('ans:sid:B'), {
    kind: 'answer',
    sessionId: 'sid',
    choice: 'B',
  });
  assert.deepEqual(parseCallback('next:sid'), { kind: 'next', sessionId: 'sid' });
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
