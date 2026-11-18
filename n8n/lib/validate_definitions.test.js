'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { isValidDefinition, applyFilledDefinitions } = require('./subtopic_questions.js');

test('isValidDefinition accepts finished meaning', () => {
  const r = isValidDefinition(
    'матриархальная семья — семья, где старшая женщина обладает основной властью.',
    'матриархальная семья',
  );
  assert.equal(r.ok, true);
});

test('isValidDefinition rejects percent scrap', () => {
  const r = isValidDefinition('на 26%, при нехватке социальных связей — на 29%', 'одиночество');
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'stats_fragment');
});

test('isValidDefinition rejects short label', () => {
  const r = isValidDefinition('фундамент', 'сексуально-эротическая функция');
  assert.equal(r.ok, false);
});

test('isValidDefinition rejects semicolon summary scrap', () => {
  const r = isValidDefinition(
    'фундамент; супружеские отношения первичны',
    'сексуально-эротическая функция',
  );
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'label_scrap');
});

test('textbook definitions are not overwritten by fill', () => {
  const { fillDefinitionsFromText, applyFilledDefinitions, isTextbookLocked } = require('./subtopic_questions.js');
  const locked = {
    canonical: 'одиночество',
    definition: 'Неприятное субъективное переживание при нехватке связей.',
    definition_source: 'textbook',
    aliases: [],
  };
  assert.equal(isTextbookLocked(locked), true);
  const heur = fillDefinitionsFromText(
    [locked],
    'одиночество — другое определение из лекции про цифры на 26%.',
    '',
  );
  assert.equal(heur.filled.length, 0);
  assert.equal(heur.terms[0].definition, locked.definition);
  const llm = applyFilledDefinitions([locked], {
    terms: [{ canonical: 'одиночество', definition: 'одиночество — выдумка модели из воздуха.', life_example: null }],
  });
  assert.equal(llm.applied.length, 0);
  assert.equal(llm.terms[0].definition, locked.definition);
});

test('applyFilledDefinitions drops invalid LLM defs', () => {
  const { terms, applied, rejected } = applyFilledDefinitions(
    [{ canonical: 'одиночество', definition: null, aliases: [] }],
    {
      terms: [
        { canonical: 'одиночество', definition: 'на 26% выше риск', life_example: null },
        {
          canonical: 'одиночество',
          definition: 'одиночество — субъективное переживание нехватки значимых связей.',
          life_example: null,
        },
      ],
    },
  );
  // second valid would overwrite if same key — Map keeps last valid only if we set once.
  // Our loop: invalid rejected, then valid applied.
  assert.ok(rejected.some((x) => x.reason === 'stats_fragment' || x.reason === 'digit_heavy' || x.reason === 'too_few_letters' || x.ok === false || x.reason));
  // After processing, if a valid one was in payload last for same canonical, applied.
  // With Map, last valid wins: the second definition is valid.
  assert.equal(terms[0].definition.includes('переживание') || applied.includes('одиночество'), true);
});
