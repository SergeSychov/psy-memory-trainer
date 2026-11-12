'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  titleTokens,
  pickKonspektFragment,
  pickTranscriptFragment,
  resolveTermsForSubtopic,
  mergeThinSubtopics,
  buildQuestionsRequestParts,
  DEFAULTS,
} = require('./subtopic_questions');

const KONSPEKT = `# Тема

## 1. Окно внимания

Текст про **окно внимания** и фокус. Достаточно длинный абзац про удержание задачи без подсказки. Ещё предложения, чтобы набрать объём раздела номер один про окно.

## 2. Якорь переключения

Текст про **якорь переключения** — короткий жест на границе задач. Примеры кружки и щелчка ручки. Ещё абзац про дешёвый переход.

## 3. Бытовой ритуал

Текст про **бытовой ритуал закрепления** после серии окон. Три шага закрытия.

## 4. Связь трёх узлов

Коротко.
`;

test('titleTokens rejects short 4-char stems', () => {
  const toks = titleTokens('Связь трёх узлов');
  assert.ok(toks.every((t) => t.length >= 5));
  assert.ok(!toks.includes('трё'));
});

test('pickKonspektFragment caps and prefers matching section', () => {
  const frag = pickKonspektFragment(KONSPEKT, 'Якорь переключения', [
    { canonical: 'якорь переключения', aliases: [], definition: 'жест' },
  ], 200);
  assert.ok(frag.matched);
  assert.ok(frag.text.includes('Якорь'));
  assert.ok(!frag.text.includes('Бытовой ритуал') || frag.text.length <= 200);
  assert.ok(frag.text.length <= 200);
});

test('pickKonspektFragment does not dump whole konspekt on miss', () => {
  const frag = pickKonspektFragment(KONSPEKT, 'Несуществующая подтема xyz', [], 500);
  assert.equal(frag.matched, false);
  assert.ok(frag.text.length <= 500);
  assert.ok(frag.text.length < KONSPEKT.length);
});

test('pickTranscriptFragment only ±1 around hits', () => {
  const chunks = [
    { text: 'вступление без терминов' },
    { text: 'здесь якорь переключения в быту' },
    { text: 'соседний сегмент' },
    { text: 'далёкий сегмент про другое' },
  ];
  const frag = pickTranscriptFragment(
    chunks,
    'Якорь переключения',
    [{ canonical: 'якорь переключения', aliases: [] }],
    7000,
  );
  assert.ok(frag.matched);
  assert.ok(frag.text.includes('якорь'));
  assert.ok(frag.text.includes('соседний'));
  assert.ok(!frag.text.includes('далёкий'));
});

test('resolveTermsForSubtopic falls back to lecture terms', () => {
  const terms = [
    { canonical: 'окно внимания', definition: 'отрезок фокуса', subtopic_ordinal: 1 },
    { canonical: 'якорь', definition: null, subtopic_ordinal: 2 },
  ];
  const own = resolveTermsForSubtopic(1, terms);
  assert.equal(own.fallback, false);
  assert.equal(own.terms.length, 1);

  const thin = resolveTermsForSubtopic(2, terms);
  assert.equal(thin.fallback, true);
  assert.equal(thin.terms.length, 1);
  assert.equal(thin.terms[0].canonical, 'окно внимания');

  const empty = resolveTermsForSubtopic(9, []);
  assert.equal(empty.fallback, false);
  assert.equal(empty.terms.length, 0);
});

test('buildQuestionsRequestParts never skips without subtopic definitions', () => {
  const parts = buildQuestionsRequestParts({
    subtopic: { ordinal: 4, title: 'Связь трёх узлов' },
    allTerms: [
      { canonical: 'окно внимания', definition: 'отрезок', subtopic_ordinal: 1, aliases: [] },
    ],
    konspektMd: KONSPEKT,
    chunks: [{ text: 'окно внимания в быту' }],
    constants: {
      questions_konspekt_max_chars: DEFAULTS.konspektMaxChars,
      questions_transcript_max_chars: DEFAULTS.transcriptMaxChars,
    },
  });
  assert.equal(parts.skip, false);
  assert.equal(parts.skip_reason, null);
  assert.equal(parts.terms_fallback, true);
  assert.deepEqual(parts.term_canonicals, ['окно внимания']);
  assert.ok(parts.user_base.includes('ПОДТЕМА'));
  assert.ok(parts.user_base.includes('ФРАГМЕНТ КОНСПЕКТА'));
  assert.ok(parts.konspekt_fragment.length <= DEFAULTS.konspektMaxChars);
});

test('mergeThinSubtopics folds short section into neighbour', () => {
  const subtopics = [
    { ordinal: 1, title: 'Окно внимания' },
    { ordinal: 2, title: 'Якорь переключения' },
    { ordinal: 3, title: 'Бытовой ритуал' },
    { ordinal: 4, title: 'Связь трёх узлов' },
  ];
  const terms = [
    { canonical: 'окно внимания', definition: 'x', subtopic_ordinal: 1 },
    { canonical: 'якорь переключения', definition: 'y', subtopic_ordinal: 2 },
    { canonical: 'бытовой ритуал закрепления', definition: 'z', subtopic_ordinal: 3 },
  ];
  const out = mergeThinSubtopics(subtopics, terms, KONSPEKT, { thinMinChars: 80 });
  assert.ok(out.subtopics.length < 4);
  assert.ok(out.merges.length >= 1);
  assert.ok(out.subtopics.every((s, i) => s.ordinal === i + 1));
  assert.ok(out.terms.every((t) => t.subtopic_ordinal >= 1 && t.subtopic_ordinal <= out.subtopics.length));
});
