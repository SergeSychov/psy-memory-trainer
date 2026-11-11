'use strict';

/**
 * Validate gen_questions_v2 JSON for one subtopic.
 * Pure function — no I/O. Used from n8n Code node and node:test.
 */

function normalizeChoice(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[\s\u00a0]+/g, ' ')
    .replace(/[.,;:!?…]+$/g, '')
    .trim();
}

function normalizeStem(text) {
  return normalizeChoice(text);
}

/**
 * @param {unknown} raw parsed JSON (object) or string
 * @param {{ termCanonicals: string[], existingStems?: string[] }} ctx
 * @returns {{ ok: boolean, errors: string[], data?: object, validQuestions?: object[] }}
 */
function validateQuestionsPayload(raw, ctx) {
  const errors = [];
  const termSet = new Set((ctx.termCanonicals || []).map((t) => String(t)));
  const existingStems = new Set((ctx.existingStems || []).map(normalizeStem));

  let data = raw;
  if (typeof raw === 'string') {
    try {
      data = JSON.parse(raw);
    } catch {
      return { ok: false, errors: ['invalid_json'] };
    }
  }

  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return { ok: false, errors: ['not_object'] };
  }

  const allowedTop = new Set(['subtopic_ordinal', 'shortfall', 'questions']);
  for (const key of Object.keys(data)) {
    if (!allowedTop.has(key)) errors.push(`extra_field:${key}`);
  }

  if (typeof data.subtopic_ordinal !== 'number' || data.subtopic_ordinal < 1) {
    errors.push('bad_subtopic_ordinal');
  }
  if (typeof data.shortfall !== 'boolean') {
    errors.push('bad_shortfall');
  }
  if (!Array.isArray(data.questions)) {
    errors.push('questions_not_array');
    return { ok: false, errors };
  }

  const allowedQ = new Set([
    'term_canonical',
    'kind',
    'stem',
    'choice_a',
    'choice_b',
    'choice_c',
    'choice_d',
    'correct_choice',
    'explanation',
    'explanation_a',
    'explanation_b',
    'explanation_c',
    'explanation_d',
  ]);

  const stemsInBatch = new Set();
  const validQuestions = [];

  data.questions.forEach((q, idx) => {
    const prefix = `q${idx}`;
    if (!q || typeof q !== 'object' || Array.isArray(q)) {
      errors.push(`${prefix}:not_object`);
      return;
    }
    for (const key of Object.keys(q)) {
      if (!allowedQ.has(key)) errors.push(`${prefix}:extra_field:${key}`);
    }

    const required = [...allowedQ];
    for (const key of required) {
      if (q[key] === undefined || q[key] === null || q[key] === '') {
        errors.push(`${prefix}:missing:${key}`);
      }
    }

    if (q.kind !== 'life_mcq') errors.push(`${prefix}:bad_kind`);
    if (!['A', 'B', 'C', 'D'].includes(q.correct_choice)) {
      errors.push(`${prefix}:bad_correct_choice`);
    }
    if (!termSet.has(String(q.term_canonical || ''))) {
      errors.push(`${prefix}:unknown_term:${q.term_canonical}`);
    }

    const choices = [q.choice_a, q.choice_b, q.choice_c, q.choice_d].map(normalizeChoice);
    if (choices.some((c) => !c)) errors.push(`${prefix}:empty_choice`);
    if (new Set(choices).size !== 4) errors.push(`${prefix}:duplicate_choices`);

    const correctIdx = { A: 0, B: 1, C: 2, D: 3 }[q.correct_choice];
    if (correctIdx != null) {
      const correctText = choices[correctIdx];
      choices.forEach((c, i) => {
        if (i !== correctIdx && c === correctText) {
          errors.push(`${prefix}:correct_duplicated_in_other`);
        }
      });
    }

    for (const letter of ['a', 'b', 'c', 'd']) {
      if (!String(q[`explanation_${letter}`] || '').trim()) {
        errors.push(`${prefix}:missing_explanation_${letter}`);
      }
    }
    if (!String(q.explanation || '').trim()) errors.push(`${prefix}:missing_explanation`);

    const stemNorm = normalizeStem(q.stem);
    if (!stemNorm) errors.push(`${prefix}:empty_stem`);
    if (stemsInBatch.has(stemNorm) || existingStems.has(stemNorm)) {
      errors.push(`${prefix}:duplicate_stem`);
    }
    stemsInBatch.add(stemNorm);

    const qErrors = errors.filter((e) => e.startsWith(`${prefix}:`));
    if (qErrors.length === 0) validQuestions.push(q);
  });

  if (data.questions.length < 10) {
    errors.push('lt_10_questions');
  }
  if (data.shortfall === true) {
    errors.push('shortfall_true');
  }

  const hardFail = errors.some(
    (e) =>
      e === 'invalid_json' ||
      e === 'not_object' ||
      e === 'questions_not_array' ||
      e.startsWith('extra_field:') ||
      e === 'bad_subtopic_ordinal' ||
      e === 'bad_shortfall',
  );

  // Schema/JSON hard errors → not ok. Soft: lt_10 / shortfall / per-question → ok=false but validQuestions kept.
  const ok =
    !hardFail &&
    errors.filter((e) => e !== 'lt_10_questions' && e !== 'shortfall_true' && !e.startsWith('q')).length === 0 &&
    validQuestions.length === data.questions.length &&
    data.questions.length >= 10 &&
    data.shortfall === false;

  return {
    ok,
    errors,
    data,
    validQuestions,
    shortfall: data.shortfall === true || validQuestions.length < 10,
  };
}

module.exports = {
  validateQuestionsPayload,
  normalizeChoice,
  normalizeStem,
};
