'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  validateQuestionsPayload,
  dedupeQuestions,
  finalizeQuestions,
  shuffleQuestionChoices,
  stemSimilarity,
  termAnswerKey,
  focusAnswerKey,
  findParaphrasePairs,
  QUESTIONS_MIN,
} = require('./validate_questions.js');

const SCENES = [
  'Анна на кухне забыла список покупок и выбирает, куда поставить напоминание.',
  'Борис в метро смотрит в телефон и решает, прервать ли чужой разговор.',
  'Вера на работе получила срочное письмо и думает, ответить ли сразу.',
  'Глеб перед сном листает новости и выбирает, оставить ли телефон в другой комнате.',
  'Дина в очереди замечает спор соседей и решает, вмешаться ли.',
  'Егор после тренировки планирует завтрашний день и выбирает формат списка.',
  'Жанна ведёт совещание и думает, как зафиксировать договорённости.',
  'Игорь чинит полку дома и выбирает, звать ли родственников на помощь.',
  'Кира в кафе ждёт друга и решает, чем занять десять минут ожидания.',
  'Лев переезжает и раскладывает коробки, выбирая порядок комнат.',
  'Мария готовит ужин для гостей и думает, как распределить задачи.',
  'Никита готовится к экзамену и выбирает, повторять ли вслух или письменно.',
];

const baseQ = (n, opts = {}) => ({
  term_canonical: opts.term || 'термин',
  kind: 'life_mcq',
  stem: opts.stem || `${SCENES[(n - 1) % SCENES.length]} Что уместнее по лекции?`,
  choice_a: opts.a || `действие альфа-${n}`,
  choice_b: opts.b || `действие бета-${n}`,
  choice_c: opts.c || `действие гамма-${n}`,
  choice_d: opts.d || `действие дельта-${n}`,
  correct_choice: opts.correct || 'A',
  explanation: 'Потому что так сказано в лекции.',
  explanation_a: 'Верно по лекции.',
  explanation_b: 'Путает с другим механизмом.',
  explanation_c: 'Не следует из фрагмента.',
  explanation_d: 'Слишком общий вывод.',
});

function eightQuestions() {
  return Array.from({ length: 8 }, (_, i) => baseQ(i + 1));
}

const ctx = { termCanonicals: ['термин'], minQuestions: 8, maxQuestions: 12 };

test('ok: 8 valid questions (new min)', () => {
  const r = validateQuestionsPayload(
    { subtopic_ordinal: 1, shortfall: false, questions: eightQuestions() },
    ctx,
  );
  assert.equal(r.ok, true);
  assert.equal(r.validQuestions.length, 8);
});

test('lt_min_questions soft shortfall', () => {
  const r = validateQuestionsPayload(
    { subtopic_ordinal: 1, shortfall: false, questions: eightQuestions().slice(0, 5) },
    ctx,
  );
  assert.equal(r.ok, false);
  assert.ok(r.errors.includes('lt_min_questions'));
  assert.equal(r.shortfall, true);
  assert.equal(r.validQuestions.length, 5);
});

test('duplicate choices', () => {
  const qs = eightQuestions();
  qs[0].choice_b = qs[0].choice_a;
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('duplicate_choices')));
});

test('unknown term skipped from validQuestions', () => {
  const qs = eightQuestions();
  qs[0].term_canonical = 'чужой';
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('unknown_term')));
  assert.equal(r.validQuestions.length, 7);
});

test('dedupeQuestions drops similar stems and term-answer pairs', () => {
  const a = baseQ(1, {
    stem: 'Маша дома вечером выбирает, куда поставить заметку о встрече.',
    a: 'повесить на дверь',
  });
  const b = baseQ(2, {
    stem: 'Маша дома вечером выбирает куда поставить заметку о встрече завтра',
    a: 'другой ответ',
  });
  const c = baseQ(3, {
    stem: 'Совершенно другая бытовая сцена про автобус и билет.',
    a: 'выйти на нужной остановке',
  });
  const d = baseQ(4, {
    stem: 'Третья уникальная сцена на работе с коллегой про дедлайн.',
    a: 'повесить на дверь', // same term + same correct as a
  });
  assert.ok(stemSimilarity(a.stem, b.stem) >= 0.65);
  const { kept, dropped } = dedupeQuestions([a, b, c, d], { existingStems: [], existingTermAnswers: [] });
  assert.equal(kept.length, 2);
  assert.ok(dropped.some((x) => x.reason === 'similar_stem'));
  assert.ok(dropped.some((x) => x.reason === 'duplicate_focus_answer' || x.reason === 'duplicate_term_answer'));
});

test('dedupe catches stem~0.71 paraphrase that old 0.72 threshold missed', () => {
  // Jaccard on tokens len>=3: share 10, A-only 2, B-only 2 → inter=10 union=14 → ≈0.714.
  // Different correct answers so focus||answer key does not short-circuit.
  const shared =
    'консультации супруги рассказывают решения принимают совместно власть обязанности делят поровну';
  const a = baseQ(1, {
    stem: `${shared} семья вместе`,
    a: 'первый ответ про равные доли',
    term: 'эгалитарная семья',
  });
  const b = baseQ(2, {
    stem: `${shared} пара равные`,
    a: 'второй ответ про совместные решения',
    term: 'эгалитарная семья',
  });
  const sim = stemSimilarity(a.stem, b.stem);
  assert.ok(sim >= 0.65 && sim < 0.72, `expected ~0.65–0.72 got ${sim}`);
  const { kept, dropped } = dedupeQuestions([a, b], { existingStems: [], existingTermAnswers: [] });
  assert.equal(kept.length, 1);
  assert.ok(dropped.some((x) => x.reason === 'similar_stem' || x.reason === 'similar_stem_same_answer'));
  const pairs = findParaphrasePairs([a, b]);
  assert.ok(pairs.length >= 1);
  assert.equal(pairs[0].reason, 'similar_stem');
});

test('shuffleQuestionChoices is deterministic by seed', () => {
  const q = baseQ(1, { correct: 'A' });
  const s1 = shuffleQuestionChoices(q, 'seed-alpha');
  const s2 = shuffleQuestionChoices(q, 'seed-alpha');
  const s3 = shuffleQuestionChoices(q, 'seed-beta');
  assert.equal(s1.correct_choice, s2.correct_choice);
  assert.equal(s1.choice_a, s2.choice_a);
  // Different seed usually moves the correct slot (not guaranteed but very likely).
  const same =
    s1.correct_choice === s3.correct_choice &&
    s1.choice_a === s3.choice_a &&
    s1.choice_b === s3.choice_b;
  assert.equal(same, false);
});

test('finalizeQuestions caps, shuffles, balances letters under 35%', () => {
  const qs = Array.from({ length: 12 }, (_, i) => baseQ(i + 1, { correct: 'A' }));
  const fin = finalizeQuestions(qs, 12, {
    letterCounts: { A: 0, B: 0, C: 0, D: 0 },
    maxLetterShare: 0.35,
    seedPrefix: 'sub-1',
  });
  assert.equal(fin.questions.length, 12);
  const counts = { A: 0, B: 0, C: 0, D: 0 };
  for (const q of fin.questions) counts[q.correct_choice] += 1;
  const total = fin.questions.length;
  for (const L of ['A', 'B', 'C', 'D']) {
    assert.ok(counts[L] / total <= 0.35 + 1e-9, `letter ${L} share ${counts[L] / total}`);
  }
});

test('validate rejects near-synonym choices', () => {
  const qs = eightQuestions();
  qs[0].choice_b = 'вариант а 1 почти';
  qs[0].choice_a = 'вариант а 1';
  // Force high similarity
  qs[0].choice_a = 'быстрый переход внимания к задаче';
  qs[0].choice_b = 'быстрый переход внимания к задаче же';
  const r = validateQuestionsPayload({ subtopic_ordinal: 1, shortfall: false, questions: qs }, ctx);
  assert.ok(r.errors.some((e) => e.includes('synonym_choices')));
});

test('existing term-answer keys reject duplicates across subtopics', () => {
  const qs = eightQuestions();
  const key = termAnswerKey(qs[0].term_canonical, qs[0].choice_a);
  const r = validateQuestionsPayload(
    { subtopic_ordinal: 2, shortfall: false, questions: qs },
    { ...ctx, existingTermAnswers: [key] },
  );
  assert.ok(r.errors.some((e) => e.includes('duplicate_term_answer')));
  assert.ok(r.validQuestions.length < QUESTIONS_MIN);
});

test('invalid json string', () => {
  const r = validateQuestionsPayload('{nope', ctx);
  assert.equal(r.ok, false);
  assert.ok(r.errors.includes('invalid_json'));
});

test('thesis questions ok without term', () => {
  const qs = eightQuestions().map((q, i) => ({
    ...q,
    source_kind: 'thesis',
    term_canonical: null,
    thesis: `тезис номер ${i + 1} про бытовой ритуал`,
  }));
  const r = validateQuestionsPayload(
    { subtopic_ordinal: 1, shortfall: false, questions: qs },
    { termCanonicals: [], allowThesis: true, thesisOnly: true, minQuestions: 8, maxQuestions: 12 },
  );
  assert.equal(r.ok, true);
  assert.equal(r.validQuestions.length, 8);
});

test('findParaphrasePairs catches focus+answer rewrite', () => {
  const a = baseQ(1, {
    stem: 'Анна дома с ребёнком зовёт бабушку жить вместе — это три поколения?',
    a: 'да, три поколения одной семьи',
  });
  a.source_kind = 'thesis';
  a.thesis = 'три поколения межпоколенная';
  a.term_canonical = null;
  const b = baseQ(2, {
    stem: 'Анна дома с ребёнком зовёт бабушку жить вместе — межпоколенная ли семья?',
    a: 'да три поколения одной семьи',
  });
  b.source_kind = 'thesis';
  b.thesis = 'три поколения межпоколенная';
  b.term_canonical = null;
  const pairs = findParaphrasePairs([a, b], { focusAnswerSimilarity: 0.5, similarityThreshold: 0.5 });
  assert.ok(pairs.length >= 1);
  assert.ok(focusAnswerKey(a).includes('||'));
});
