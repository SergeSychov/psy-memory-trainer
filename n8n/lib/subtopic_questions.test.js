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
  normalizeTermsExtract,
  finalizeTermsExtractGate,
  missWindowCenter,
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
  assert.ok(frag.text.length <= 200);
});

test('pickKonspektFragment does not dump whole konspekt on miss', () => {
  const frag = pickKonspektFragment(KONSPEKT, 'Несуществующая подтема xyz', [], 500);
  assert.equal(frag.matched, false);
  assert.ok(frag.text.length <= 500);
  assert.ok(frag.text.length < KONSPEKT.length);
});

test('miss window centers by subtopic position, not document middle', () => {
  assert.equal(missWindowCenter(4, { subtopicIndex: 0, subtopicCount: 3 }), 0);
  assert.equal(missWindowCenter(4, { subtopicIndex: 2, subtopicCount: 3 }), 3);
  const first = pickKonspektFragment(
    KONSPEKT,
    'Абсолютно чужой заголовок альфа',
    [],
    5000,
    { subtopicIndex: 0, subtopicCount: 3 },
  );
  const last = pickKonspektFragment(
    KONSPEKT,
    'Абсолютно чужой заголовок омега',
    [],
    5000,
    { subtopicIndex: 2, subtopicCount: 3 },
  );
  assert.ok(first.text.includes('Окно внимания'));
  assert.ok(last.text.includes('Связь трёх узлов') || last.text.includes('Бытовой ритуал'));
  assert.notEqual(first.text.slice(0, 40), last.text.slice(0, 40));
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

test('pickTranscriptFragment falls back to slicing raw transcript without chunks', () => {
  const long = 'А'.repeat(500) + ' якорь переключения ' + 'Б'.repeat(1500);
  const frag = pickTranscriptFragment([], 'Якорь переключения', [
    { canonical: 'якорь переключения', aliases: [] },
  ], 7000, long);
  assert.ok(frag.text.length > 0);
  assert.ok(frag.text.includes('якорь') || frag.text.length >= 1000);
});

test('transcript miss window centers by subtopic position, not document middle', () => {
  const chunks = [
    { text: 'сегмент-0-начало' },
    { text: 'сегмент-1' },
    { text: 'сегмент-2' },
    { text: 'сегмент-3-конец' },
  ];
  const first = pickTranscriptFragment(chunks, 'Чужой заголовок', [], 7000, '', {
    subtopicIndex: 0,
    subtopicCount: 3,
  });
  const last = pickTranscriptFragment(chunks, 'Чужой заголовок', [], 7000, '', {
    subtopicIndex: 2,
    subtopicCount: 3,
  });
  assert.ok(first.text.includes('сегмент-0'));
  assert.ok(last.text.includes('сегмент-3') || last.text.includes('сегмент-2'));
  assert.notEqual(first.text, last.text);
});

test('normalizeTermsExtract: empty subtopics + terms → synthesize, bind, no throw', () => {
  const out = normalizeTermsExtract(
    {
      subtopics: [],
      terms: [
        {
          canonical: 'окно внимания',
          aliases: ['окно'],
          definition: 'отрезок фокуса',
          life_example: 'на кухне',
          subtopic_ordinal: 3,
        },
      ],
    },
    { lectureTitle: 'Лекция без подтем' },
  );
  assert.equal(out.synthesizedSubtopic, true);
  assert.equal(out.subtopics.length, 1);
  assert.equal(out.subtopics[0].ordinal, 1);
  assert.equal(out.subtopics[0].title, 'Лекция без подтем');
  assert.equal(out.terms.length, 1);
  assert.equal(out.terms[0].subtopic_ordinal, 1);
  assert.equal(out.lecture_terms_empty, false);
});

test('normalizeTermsExtract: keeps real subtopics and does not synthesize', () => {
  const out = normalizeTermsExtract(
    {
      subtopics: [{ ordinal: 2, title: 'Якорь' }],
      terms: [{ canonical: 'якорь', definition: 'жест', subtopic_ordinal: 2 }],
    },
    { lectureTitle: 'Игнор' },
  );
  assert.equal(out.synthesizedSubtopic, false);
  assert.equal(out.subtopics.length, 1);
  assert.equal(out.subtopics[0].title, 'Якорь');
  assert.equal(out.terms[0].subtopic_ordinal, 2);
});

test('finalizeTermsExtractGate: soft empty terms + course → proceed with course_fallback', () => {
  const gate = finalizeTermsExtractGate({
    parseOk: true,
    errorMessage: 'terms: empty_terms',
    subtopics: [{ ordinal: 1, title: 'Окно' }, { ordinal: 2, title: 'Якорь' }],
    terms: [],
    courseTerms: [{ canonical: 'курс-термин', definition: 'из курса' }],
    lectureTitle: 'Лекция X',
  });
  assert.equal(gate.proceed, true);
  assert.equal(gate.course_fallback, true);
  assert.equal(gate.hard_parse_error, false);
  assert.equal(gate.subtopics.length, 2);
  assert.equal(gate.reason, 'course_terms_fallback');
});

test('finalizeTermsExtractGate: soft empty everywhere → synthetic subtopic, no_terms_available', () => {
  const gate = finalizeTermsExtractGate({
    parseOk: true,
    errorMessage: 'terms: empty_terms',
    subtopics: [],
    terms: [],
    courseTerms: [],
    lectureTitle: 'Синтетика',
  });
  assert.equal(gate.proceed, true);
  assert.equal(gate.course_fallback, false);
  assert.equal(gate.subtopics.length, 1);
  assert.equal(gate.subtopics[0].title, 'Синтетика');
  assert.equal(gate.reason, 'no_terms_available');
});

test('finalizeTermsExtractGate: hard parse → proceed=false, no synthetic, no course fallback', () => {
  const gate = finalizeTermsExtractGate({
    parseOk: false,
    errorMessage: 'terms: invalid_json',
    subtopics: [{ ordinal: 1, title: 'Не должны подставиться' }],
    terms: [],
    courseTerms: [{ canonical: 'курс-термин', definition: 'из курса' }],
    lectureTitle: 'Лекция X',
  });
  assert.equal(gate.proceed, false);
  assert.equal(gate.hard_parse_error, true);
  assert.equal(gate.course_fallback, false);
  assert.deepEqual(gate.subtopics, []);
  assert.match(String(gate.reason), /invalid_json/);
});

test('resolveTermsForSubtopic: subtopic → lecture → course', () => {
  const lectureTerms = [
    { canonical: 'окно внимания', definition: 'отрезок фокуса', subtopic_ordinal: 1 },
    { canonical: 'якорь', definition: null, subtopic_ordinal: 2 },
  ];
  const courseTerms = [
    { canonical: 'курс-термин', definition: 'из курса', aliases: [] },
  ];

  const own = resolveTermsForSubtopic(1, lectureTerms, courseTerms);
  assert.equal(own.source, 'subtopic');
  assert.equal(own.fallback, false);

  const lec = resolveTermsForSubtopic(2, lectureTerms, courseTerms);
  assert.equal(lec.source, 'lecture');
  assert.equal(lec.fallback, true);
  assert.equal(lec.terms[0].canonical, 'окно внимания');

  const course = resolveTermsForSubtopic(9, [], courseTerms);
  assert.equal(course.source, 'course');
  assert.equal(course.terms[0].canonical, 'курс-термин');

  const empty = resolveTermsForSubtopic(9, [], []);
  assert.equal(empty.source, null);
  assert.equal(empty.terms.length, 0);
});

test('buildQuestionsRequestParts skips LLM when no terms anywhere', () => {
  const parts = buildQuestionsRequestParts({
    subtopic: { ordinal: 1, title: 'Пусто' },
    allTerms: [{ canonical: 'x', definition: null, subtopic_ordinal: 1 }],
    courseTerms: [{ canonical: 'y', definition: '  ' }],
    konspektMd: KONSPEKT,
    chunks: [],
    transcript: 'текст',
  });
  assert.equal(parts.skip, true);
  assert.equal(parts.skip_reason, 'no_terms_available');
  assert.equal(parts.request_ready, false);
  assert.equal(parts.user_base, null);
  assert.deepEqual(parts.term_canonicals, []);
});

test('buildQuestionsRequestParts uses lecture fallback and builds request', () => {
  const parts = buildQuestionsRequestParts({
    subtopic: { ordinal: 4, title: 'Связь трёх узлов' },
    allTerms: [
      { canonical: 'окно внимания', definition: 'отрезок', subtopic_ordinal: 1, aliases: [] },
    ],
    courseTerms: [],
    konspektMd: KONSPEKT,
    chunks: [{ text: 'окно внимания в быту' }],
    constants: {
      questions_konspekt_max_chars: DEFAULTS.konspektMaxChars,
      questions_transcript_max_chars: DEFAULTS.transcriptMaxChars,
    },
  });
  assert.equal(parts.skip, false);
  assert.equal(parts.terms_source, 'lecture');
  assert.ok(parts.user_base.includes('ПОДТЕМА'));
});

test('mergeThinSubtopics folds matched short section into neighbour', () => {
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
});

test('mergeThinSubtopics keeps unmatched titles as separate subtopics', () => {
  const subtopics = [
    { ordinal: 1, title: 'Альфа без совпадения' },
    { ordinal: 2, title: 'Бета без совпадения' },
    { ordinal: 3, title: 'Гамма без совпадения' },
  ];
  const out = mergeThinSubtopics(subtopics, [], KONSPEKT, { thinMinChars: 400 });
  assert.equal(out.subtopics.length, 3);
  assert.equal(out.merges.length, 0);
  assert.deepEqual(
    out.subtopics.map((s) => s.title),
    ['Альфа без совпадения', 'Бета без совпадения', 'Гамма без совпадения'],
  );
});
