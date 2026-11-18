'use strict';

/**
 * Validate gen_questions JSON for one subtopic (psy_05 / gen_questions_v4+).
 * Pure function — no I/O. Used from n8n Code node and node:test.
 */

const QUESTIONS_MIN = 8;
const QUESTIONS_MAX = 12;
const STEM_SIMILARITY_THRESHOLD = 0.72;
const MAX_LETTER_SHARE = 0.35;

function normalizeChoice(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/ё/g, 'е')
    .replace(/[\s\u00a0]+/g, ' ')
    .replace(/[.,;:!?…«»"'`()\[\]{}]+/g, '')
    .trim();
}

function normalizeStem(text) {
  return normalizeChoice(text);
}

function tokenize(text) {
  return normalizeStem(text)
    .split(/[^а-яa-z0-9]+/)
    .filter((w) => w.length >= 3);
}

/** Jaccard similarity on tokens; 1 = identical bag of words. */
function stemSimilarity(a, b) {
  const ta = new Set(tokenize(a));
  const tb = new Set(tokenize(b));
  if (!ta.size && !tb.size) return 1;
  if (!ta.size || !tb.size) return 0;
  let inter = 0;
  for (const t of ta) if (tb.has(t)) inter += 1;
  const union = ta.size + tb.size - inter;
  return union ? inter / union : 0;
}

function termAnswerKey(termCanonical, correctText) {
  return normalizeChoice(termCanonical) + '||' + normalizeChoice(correctText);
}

function correctChoiceText(q) {
  const map = { A: q.choice_a, B: q.choice_b, C: q.choice_c, D: q.choice_d };
  return map[q.correct_choice] || '';
}

/**
 * Drop near-duplicate stems and duplicate term–answer pairs.
 * Prefers earlier items. Returns { kept, dropped }.
 */
function dedupeQuestions(questions, opts = {}) {
  const threshold = opts.similarityThreshold != null ? opts.similarityThreshold : STEM_SIMILARITY_THRESHOLD;
  const existingStems = opts.existingStems || [];
  const existingKeys = new Set((opts.existingTermAnswers || []).map((k) => String(k)));
  const kept = [];
  const dropped = [];

  for (const q of questions || []) {
    const stemNorm = normalizeStem(q.stem);
    const key = termAnswerKey(q.term_canonical, correctChoiceText(q));
    let dup = false;
    let reason = null;

    if (existingKeys.has(key) || kept.some((k) => termAnswerKey(k.term_canonical, correctChoiceText(k)) === key)) {
      dup = true;
      reason = 'duplicate_term_answer';
    }
    if (!dup) {
      for (const prev of existingStems.concat(kept.map((k) => k.stem))) {
        if (stemSimilarity(stemNorm, prev) >= threshold) {
          dup = true;
          reason = 'similar_stem';
          break;
        }
      }
    }
    if (dup) dropped.push({ question: q, reason });
    else {
      kept.push(q);
      existingKeys.add(key);
    }
  }
  return { kept, dropped };
}

/**
 * Heuristic: case/action question vs term-naming.
 * Term-naming ≈ asks «как называется» / «какой термин» without a concrete situation verb.
 */
function isCaseOrActionQuestion(q) {
  const stem = normalizeStem(q.stem);
  const naming =
    /как называется|какой термин|термин для|название (этого|данного)|что это за (понятие|термин)/.test(
      stem,
    );
  if (naming) return false;
  const situation =
    /она|он|они|друг|мама|папа|коллега|студент|клиент|на работе|дома|вечером|утром|ситуаци|если |когда |выбер|сдела|ответ|стоит ли|лучше/.test(
      stem,
    );
  return situation || !naming;
}

function caseActionShare(questions) {
  const list = questions || [];
  if (!list.length) return 0;
  const n = list.filter(isCaseOrActionQuestion).length;
  return n / list.length;
}

/** FNV-1a 32-bit → unsigned int seed. */
function hashSeed(str) {
  let h = 2166136261;
  const s = String(str || '');
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Deterministically permute A–D so correct_choice is not left to the LLM.
 * Optional letterCounts: prefer a letter whose current share stays ≤ maxShare.
 */
function shuffleQuestionChoices(q, seedKey, opts = {}) {
  const letterCounts = opts.letterCounts || { A: 0, B: 0, C: 0, D: 0 };
  const maxShare = opts.maxLetterShare != null ? opts.maxLetterShare : MAX_LETTER_SHARE;
  const letters = ['A', 'B', 'C', 'D'];
  const slots = letters.map((L) => ({
    text: q[`choice_${L.toLowerCase()}`],
    expl: q[`explanation_${L.toLowerCase()}`],
  }));
  const correctIdx = { A: 0, B: 1, C: 2, D: 3 }[q.correct_choice];
  if (correctIdx == null) return { ...q };
  const correctSlot = slots[correctIdx];

  const rng = mulberry32(hashSeed(seedKey || q.stem || ''));
  const order = [0, 1, 2, 3];
  for (let i = order.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rng() * (i + 1));
    const tmp = order[i];
    order[i] = order[j];
    order[j] = tmp;
  }

  const total = letters.reduce((s, L) => s + (letterCounts[L] || 0), 0);
  // Bias only after we have lecture-wide counts; otherwise keep pure seeded shuffle.
  if (total > 0) {
    const ranked = letters
      .map((L, i) => ({ L, i, count: letterCounts[L] || 0, share: (letterCounts[L] || 0) / total }))
      .sort((a, b) => a.count - b.count || a.i - b.i);
    const under = ranked.filter((r) => r.share < maxShare || total < 4);
    const pick = (under.length ? under : ranked)[0];
    if (pick) {
      const currentCorrectPos = order.indexOf(correctIdx);
      const desiredPos = pick.i;
      if (currentCorrectPos !== desiredPos) {
        const atDesired = order[desiredPos];
        order[desiredPos] = correctIdx;
        order[currentCorrectPos] = atDesired;
      }
    }
  }

  const out = { ...q };
  order.forEach((srcIdx, destIdx) => {
    const L = letters[destIdx];
    out[`choice_${L.toLowerCase()}`] = slots[srcIdx].text;
    out[`explanation_${L.toLowerCase()}`] = slots[srcIdx].expl;
  });
  out.correct_choice = letters[order.indexOf(correctIdx)];
  return out;
}

/**
 * Finalize list: dedupe → cap → shuffle with lecture letter balance.
 */
function finalizeQuestions(list, cap, opts = {}) {
  const maxCap = cap != null ? cap : QUESTIONS_MAX;
  const deduped = dedupeQuestions(list, {
    existingStems: opts.existingStems || [],
    existingTermAnswers: opts.existingTermAnswers || [],
    similarityThreshold: opts.similarityThreshold,
  });
  const letterCounts = { A: 0, B: 0, C: 0, D: 0, ...(opts.letterCounts || {}) };
  const sliced = deduped.kept.slice(0, maxCap);
  const out = sliced.map((q, i) => {
    const seed = String(opts.seedPrefix || '') + '|' + String(q.stem || '') + '|' + i;
    const shuffled = shuffleQuestionChoices(q, seed, {
      letterCounts,
      maxLetterShare: opts.maxLetterShare,
    });
    letterCounts[shuffled.correct_choice] = (letterCounts[shuffled.correct_choice] || 0) + 1;
    return {
      ordinal: i + 1,
      term_canonical: shuffled.term_canonical,
      stem: shuffled.stem,
      choice_a: shuffled.choice_a,
      choice_b: shuffled.choice_b,
      choice_c: shuffled.choice_c,
      choice_d: shuffled.choice_d,
      correct_choice: shuffled.correct_choice,
      explanation: shuffled.explanation,
      choice_explanations: {
        A: shuffled.explanation_a,
        B: shuffled.explanation_b,
        C: shuffled.explanation_c,
        D: shuffled.explanation_d,
      },
    };
  });
  return {
    questions: out,
    dropped: deduped.dropped,
    letterCounts,
    case_action_share: caseActionShare(sliced),
  };
}

/**
 * @param {unknown} raw parsed JSON (object) or string
 * @param {{
 *   termCanonicals: string[],
 *   existingStems?: string[],
 *   existingTermAnswers?: string[],
 *   minQuestions?: number,
 *   maxQuestions?: number,
 *   similarityThreshold?: number,
 * }} ctx
 */
function validateQuestionsPayload(raw, ctx) {
  const errors = [];
  const termSet = new Set((ctx.termCanonicals || []).map((t) => String(t)));
  const existingStems = ctx.existingStems || [];
  const existingTermAnswers = ctx.existingTermAnswers || [];
  const minQ = ctx.minQuestions != null ? ctx.minQuestions : QUESTIONS_MIN;
  const maxQ = ctx.maxQuestions != null ? ctx.maxQuestions : QUESTIONS_MAX;

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

  const stemsInBatch = [];
  const keysInBatch = new Set();
  const structurallyValid = [];

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

    // Near-synonym distractors: any two choices with very high similarity
    for (let i = 0; i < choices.length; i += 1) {
      for (let j = i + 1; j < choices.length; j += 1) {
        if (stemSimilarity(choices[i], choices[j]) >= 0.9) {
          errors.push(`${prefix}:synonym_choices`);
        }
      }
    }

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

    const key = termAnswerKey(q.term_canonical, correctChoiceText(q));
    if (keysInBatch.has(key) || existingTermAnswers.includes(key)) {
      errors.push(`${prefix}:duplicate_term_answer`);
    }

    let similar = false;
    for (const prev of stemsInBatch.concat(existingStems)) {
      if (stemSimilarity(stemNorm, prev) >= (ctx.similarityThreshold || STEM_SIMILARITY_THRESHOLD)) {
        similar = true;
        break;
      }
    }
    if (similar) errors.push(`${prefix}:duplicate_stem`);

    const qErrors = errors.filter((e) => e.startsWith(`${prefix}:`));
    if (qErrors.length === 0) {
      structurallyValid.push(q);
      stemsInBatch.push(stemNorm);
      keysInBatch.add(key);
    }
  });

  if (data.questions.length > maxQ) {
    errors.push('gt_max_questions');
  }
  if (structurallyValid.length < minQ) {
    errors.push('lt_min_questions');
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

  // Soft: lt_min / shortfall / per-question → keep validQuestions for partial accept.
  const soft = new Set(['lt_min_questions', 'shortfall_true', 'gt_max_questions']);
  const ok =
    !hardFail &&
    errors.filter((e) => !soft.has(e) && !e.startsWith('q')).length === 0 &&
    structurallyValid.length === data.questions.length &&
    structurallyValid.length >= minQ &&
    structurallyValid.length <= maxQ &&
    data.shortfall === false;

  return {
    ok,
    errors,
    data,
    validQuestions: structurallyValid.slice(0, maxQ),
    shortfall: data.shortfall === true || structurallyValid.length < minQ,
    case_action_share: caseActionShare(structurallyValid),
  };
}

module.exports = {
  QUESTIONS_MIN,
  QUESTIONS_MAX,
  STEM_SIMILARITY_THRESHOLD,
  MAX_LETTER_SHARE,
  validateQuestionsPayload,
  normalizeChoice,
  normalizeStem,
  tokenize,
  stemSimilarity,
  termAnswerKey,
  correctChoiceText,
  dedupeQuestions,
  isCaseOrActionQuestion,
  caseActionShare,
  hashSeed,
  shuffleQuestionChoices,
  finalizeQuestions,
};
