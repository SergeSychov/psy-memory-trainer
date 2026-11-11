'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { validateQuestionsPayload } = require('./validate_questions.js');

const baseQ = (n) => ({
  term_canonical: 'термин',
  kind: 'life_mcq',
  stem: `Ситуация номер ${n} про память на кухне?`,
  choice_a: `вариант а ${n}`,
  choice_b: `вариант б ${n}`,
  choice_c: `вариант в ${n}`,
  choice_d: `вариант г ${n}`,
  correct_choice: 'A',
  explanation: 'Потому что так сказано в лекции.',
  explanation_a: 'Верно по лекции.',
  explanation_b: 'Путает с другим механизмом.',
  explanation_c: 'Не следует из фрагмента.',
  explanation_d: 'Слишком общий вывод.',
});

function tenQuestions() {
  return Array.from({ length: 10 }, (_, i) => baseQ(i + 1));
}

const ctx = { termCanonicals: ['термин'] };

test('ok: 10 valid questions', () => {
  const r = validateQuestionsPayload(
    { subtopic_ordinal: 1, shortfall: false, questions: tenQuestions() },
    ctx,
  );
  assert.equal(r.ok, true);
  assert.equal(r.validQuestions.length, 10);
});

test('duplicate choices', () => {
  const qs = tenQuestions();
  qs[0].choice_b = qs[0].choice_a;
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('duplicate_choices')));
});

test('three choices missing one', () => {
  const qs = tenQuestions();
  qs[0].choice_d = '';
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('missing:choice_d') || e.includes('empty_choice')));
});

test('missing explanation', () => {
  const qs = tenQuestions();
  qs[0].explanation = '';
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('missing_explanation')));
});

test('extra field', () => {
  const qs = tenQuestions();
  qs[0].foo = 'bar';
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('extra_field:foo')));
});

test('unknown term', () => {
  const qs = tenQuestions();
  qs[0].term_canonical = 'чужой';
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('unknown_term')));
});

test('lt 10 questions', () => {
  const r = validateQuestionsPayload(
    { subtopic_ordinal: 1, shortfall: false, questions: tenQuestions().slice(0, 5) },
    ctx,
  );
  assert.equal(r.ok, false);
  assert.ok(r.errors.includes('lt_10_questions'));
  assert.equal(r.shortfall, true);
});

test('shortfall true', () => {
  const r = validateQuestionsPayload(
    { subtopic_ordinal: 1, shortfall: true, questions: tenQuestions() },
    ctx,
  );
  assert.equal(r.ok, false);
  assert.ok(r.errors.includes('shortfall_true'));
});

test('invalid json string', () => {
  const r = validateQuestionsPayload('{nope', ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.includes('invalid_json'));
});
