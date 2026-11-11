const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const FILES = [
  'extract_terms.md',
  'extract_terms_v2.md',
  'gen_questions.md',
  'gen_questions_v2.md',
  'explain_miss.md',
  'consolidate_quiz.md',
];

function systemPrompt(text) {
  const start = text.indexOf('## Текст system prompt');
  const end = text.indexOf('## Конец system prompt');
  assert.ok(start !== -1 && end > start, 'нет границ system prompt');
  return text.slice(start, end);
}

for (const name of FILES) {
  test(name + ': system prompt короткий, схема — валидный JSON', () => {
    const text = fs.readFileSync(path.join(__dirname, name), 'utf8');
    const prompt = systemPrompt(text);
    assert.ok(prompt.length < 2500, name + ' system prompt ' + prompt.length + ' знаков');
    assert.match(prompt, /JSON/);
    assert.match(prompt, /не /);
    const blocks = [...text.matchAll(/```json\n([\s\S]*?)```/g)];
    assert.ok(blocks.length >= 1, name + ' без схемы');
    for (const block of blocks) JSON.parse(block[1]);
  });
}

test('контракт полей, которые пишет n8n', () => {
  const terms = fs.readFileSync(path.join(__dirname, 'extract_terms.md'), 'utf8');
  const termsV2 = fs.readFileSync(path.join(__dirname, 'extract_terms_v2.md'), 'utf8');
  const questions = fs.readFileSync(path.join(__dirname, 'gen_questions.md'), 'utf8');
  const questionsV2 = fs.readFileSync(path.join(__dirname, 'gen_questions_v2.md'), 'utf8');
  const konspektV2 = fs.readFileSync(path.join(__dirname, 'konspekt_v2.md'), 'utf8');
  const miss = fs.readFileSync(path.join(__dirname, 'explain_miss.md'), 'utf8');
  const quiz = fs.readFileSync(path.join(__dirname, 'consolidate_quiz.md'), 'utf8');
  assert.match(terms, /glossary_terms/);
  assert.match(terms, /subtopics/);
  assert.match(termsV2, /life_example/);
  assert.match(konspektV2, /Краткий конспект/);
  assert.match(questions, /life_mcq/);
  assert.match(questions, /explanation_a/);
  assert.match(questions, /shortfall/);
  assert.match(questionsV2, /ДОБОР/);
  assert.match(questionsV2, /shortfall/);
  assert.match(miss, /term_id/);
  assert.match(miss, /llm_usage/);
  assert.match(quiz, /need 3-4 topics/);
  assert.match(quiz, /"minItems": 8/);
});

test('konspekt_v2 имеет system prompt', () => {
  const text = fs.readFileSync(path.join(__dirname, 'konspekt_v2.md'), 'utf8');
  assert.match(systemPrompt(text), /короткий/i);
});
