'use strict';

/**
 * Validate gen_questions JSON for one subtopic (psy_05 / gen_questions_v5+).
 * Pure function — no I/O. Used from n8n Code node and node:test.
 */

const QUESTIONS_MIN = 8;
const QUESTIONS_MAX = 12;
const STEM_SIMILARITY_THRESHOLD = 0.72;
const FOCUS_ANSWER_SIMILARITY_THRESHOLD = 0.82;
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

function correctChoiceText(q) {
  const map = { A: q.choice_a, B: q.choice_b, C: q.choice_c, D: q.choice_d };
  return map[q.correct_choice] || '';
}

/** Focus fingerprint: thesis label or term canonical. */
function questionFocus(q) {
  const kind = q && q.source_kind === 'thesis' ? 'thesis' : 'term';
  if (kind === 'thesis') {
    return normalizeChoice(q.thesis || q.stem || '');
  }
  return normalizeChoice(q.term_canonical || '');
}

/**
 * Semantic-ish key: focus (term|thesis) + correct answer.
 * Used for cross-subtopic paraphrase detection without paid embeddings.
 */
function focusAnswerKey(q) {
  return questionFocus(q) + '||' + normalizeChoice(correctChoiceText(q));
}

/** @deprecated alias kept for callers */
function termAnswerKey(termCanonical, correctText) {
  return normalizeChoice(termCanonical) + '||' + normalizeChoice(correctText);
}

function normalizeQuestionShape(q) {
  const sourceKind = q.source_kind === 'thesis' ? 'thesis' : 'term';
  return {
    ...q,
    source_kind: sourceKind,
    term_canonical: sourceKind === 'term' ? q.term_canonical : null,
    thesis: sourceKind === 'thesis' ? String(q.thesis || '').trim() || null : null,
  };
}

/**
 * Drop near-duplicate stems and duplicate focus–answer pairs (incl. paraphrases).
 */
function dedupeQuestions(questions, opts = {}) {
  const threshold = opts.similarityThreshold != null ? opts.similarityThreshold : STEM_SIMILARITY_THRESHOLD;
  const focusThreshold =
    opts.focusAnswerSimilarity != null ? opts.focusAnswerSimilarity : FOCUS_ANSWER_SIMILARITY_THRESHOLD;
  const existingStems = opts.existingStems || [];
  const existingKeys = (opts.existingTermAnswers || []).map((k) => String(k));
  const kept = [];
  const dropped = [];

  function isParaphraseOf(q, otherKeyOrQ) {
    const key = typeof otherKeyOrQ === 'string' ? otherKeyOrQ : focusAnswerKey(otherKeyOrQ);
    const otherQ = typeof otherKeyOrQ === 'string' ? null : otherKeyOrQ;
    const mine = focusAnswerKey(q);
    if (mine === key) return true;
    const [f1, a1] = mine.split('||');
    const [f2, a2] = key.split('||');
    if (!f1 || !f2 || !a1 || !a2) return false;
    const focusSim = stemSimilarity(f1, f2);
    const ansSim = stemSimilarity(a1, a2);
    if (focusSim < focusThreshold || ansSim < focusThreshold) return false;
    // Fuzzy paraphrase: require stem overlap too, so different situations
    // with the same term but different answers/actions are not wiped.
    const stemSim = otherQ ? stemSimilarity(q.stem, otherQ.stem) : 0;
    return stemSim >= Math.min(threshold, 0.55);
  }

  for (const raw of questions || []) {
    const q = normalizeQuestionShape(raw);
    const stemNorm = normalizeStem(q.stem);
    let dup = false;
    let reason = null;

    if (existingKeys.some((k) => isParaphraseOf(q, k)) || kept.some((k) => isParaphraseOf(q, k))) {
      dup = true;
      reason = 'duplicate_focus_answer';
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
      existingKeys.push(focusAnswerKey(q));
    }
  }
  return { kept, dropped };
}

/**
 * Report-mode paraphrase pairs across a flat list (lecture-wide).
 * Does not mutate; returns pairs of indices with reasons.
 */
function findParaphrasePairs(questions, opts = {}) {
  const threshold = opts.similarityThreshold != null ? opts.similarityThreshold : STEM_SIMILARITY_THRESHOLD;
  const focusThreshold =
    opts.focusAnswerSimilarity != null ? opts.focusAnswerSimilarity : FOCUS_ANSWER_SIMILARITY_THRESHOLD;
  const list = (questions || []).map(normalizeQuestionShape);
  const pairs = [];
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      const a = list[i];
      const b = list[j];
      const stemSim = stemSimilarity(a.stem, b.stem);
      const focusSim = stemSimilarity(questionFocus(a), questionFocus(b));
      const ansSim = stemSimilarity(correctChoiceText(a), correctChoiceText(b));
      let reason = null;
      if (stemSim >= threshold) reason = 'similar_stem';
      else if (
        focusSim >= focusThreshold &&
        ansSim >= focusThreshold &&
        stemSim >= Math.min(threshold, 0.45)
      ) {
        reason = 'duplicate_focus_answer';
      }
      if (reason) {
        pairs.push({
          i,
          j,
          reason,
          stem_similarity: Number(stemSim.toFixed(3)),
          focus_similarity: Number(focusSim.toFixed(3)),
          answer_similarity: Number(ansSim.toFixed(3)),
          a: {
            id: a.id,
            source_kind: a.source_kind,
            focus: questionFocus(a),
            stem: a.stem,
            correct: correctChoiceText(a),
          },
          b: {
            id: b.id,
            source_kind: b.source_kind,
            focus: questionFocus(b),
            stem: b.stem,
            correct: correctChoiceText(b),
          },
        });
      }
    }
  }
  return pairs;
}

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
  return list.filter(isCaseOrActionQuestion).length / list.length;
}

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

  const rng = mulberry32(hashSeed(seedKey || q.stem || ''));
  const order = [0, 1, 2, 3];
  for (let i = order.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rng() * (i + 1));
    const tmp = order[i];
    order[i] = order[j];
    order[j] = tmp;
  }

  const total = letters.reduce((s, L) => s + (letterCounts[L] || 0), 0);
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

function finalizeQuestions(list, cap, opts = {}) {
  const maxCap = cap != null ? cap : QUESTIONS_MAX;
  const deduped = dedupeQuestions(list, {
    existingStems: opts.existingStems || [],
    existingTermAnswers: opts.existingTermAnswers || [],
    similarityThreshold: opts.similarityThreshold,
    focusAnswerSimilarity: opts.focusAnswerSimilarity,
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
    const shaped = normalizeQuestionShape(shuffled);
    return {
      ordinal: i + 1,
      source_kind: shaped.source_kind,
      term_canonical: shaped.term_canonical,
      thesis: shaped.thesis,
      stem: shaped.stem,
      choice_a: shaped.choice_a,
      choice_b: shaped.choice_b,
      choice_c: shaped.choice_c,
      choice_d: shaped.choice_d,
      correct_choice: shaped.correct_choice,
      explanation: shaped.explanation,
      choice_explanations: {
        A: shaped.explanation_a,
        B: shaped.explanation_b,
        C: shaped.explanation_c,
        D: shaped.explanation_d,
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

function validateQuestionsPayload(raw, ctx) {
  const errors = [];
  const termSet = new Set((ctx.termCanonicals || []).map((t) => String(t)));
  const allowThesis = ctx.allowThesis !== false;
  const thesisOnly = Boolean(ctx.thesisOnly);
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
    'source_kind',
    'term_canonical',
    'thesis',
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
  const keysInBatch = [];
  const structurallyValid = [];

  data.questions.forEach((rawQ, idx) => {
    const prefix = `q${idx}`;
    if (!rawQ || typeof rawQ !== 'object' || Array.isArray(rawQ)) {
      errors.push(`${prefix}:not_object`);
      return;
    }
    for (const key of Object.keys(rawQ)) {
      if (!allowedQ.has(key)) errors.push(`${prefix}:extra_field:${key}`);
    }

    // Backward compat: missing source_kind → term (v3/v4 payloads).
    const q = normalizeQuestionShape({
      ...rawQ,
      source_kind: rawQ.source_kind || (rawQ.term_canonical ? 'term' : 'thesis'),
    });

    const requiredBase = [
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
    ];
    for (const key of requiredBase) {
      if (rawQ[key] === undefined || rawQ[key] === null || rawQ[key] === '') {
        errors.push(`${prefix}:missing:${key}`);
      }
    }

    if (q.kind !== 'life_mcq') errors.push(`${prefix}:bad_kind`);
    if (!['A', 'B', 'C', 'D'].includes(q.correct_choice)) {
      errors.push(`${prefix}:bad_correct_choice`);
    }
    if (!['term', 'thesis'].includes(q.source_kind)) {
      errors.push(`${prefix}:bad_source_kind`);
    }

    if (q.source_kind === 'term') {
      if (!q.term_canonical) errors.push(`${prefix}:missing:term_canonical`);
      else if (!termSet.has(String(q.term_canonical))) {
        errors.push(`${prefix}:unknown_term:${q.term_canonical}`);
      }
      if (thesisOnly) errors.push(`${prefix}:term_not_allowed_in_thesis_only`);
    } else {
      if (!allowThesis) errors.push(`${prefix}:thesis_not_allowed`);
      if (!q.thesis) errors.push(`${prefix}:missing:thesis`);
      if (q.term_canonical) errors.push(`${prefix}:term_canonical_must_be_null_for_thesis`);
    }

    const choices = [q.choice_a, q.choice_b, q.choice_c, q.choice_d].map(normalizeChoice);
    if (choices.some((c) => !c)) errors.push(`${prefix}:empty_choice`);
    if (new Set(choices).size !== 4) errors.push(`${prefix}:duplicate_choices`);

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

    const key = focusAnswerKey(q);
    if (keysInBatch.includes(key) || existingTermAnswers.includes(key)) {
      errors.push(`${prefix}:duplicate_term_answer`);
      errors.push(`${prefix}:duplicate_focus_answer`);
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
      keysInBatch.push(key);
    }
  });

  if (data.questions.length > maxQ) errors.push('gt_max_questions');
  if (structurallyValid.length < minQ) errors.push('lt_min_questions');
  if (data.shortfall === true) errors.push('shortfall_true');

  const hardFail = errors.some(
    (e) =>
      e === 'invalid_json' ||
      e === 'not_object' ||
      e === 'questions_not_array' ||
      e.startsWith('extra_field:') ||
      e === 'bad_subtopic_ordinal' ||
      e === 'bad_shortfall',
  );

  const soft = new Set(['lt_min_questions', 'lt_10_questions', 'shortfall_true', 'gt_max_questions']);
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
  FOCUS_ANSWER_SIMILARITY_THRESHOLD,
  MAX_LETTER_SHARE,
  validateQuestionsPayload,
  normalizeChoice,
  normalizeStem,
  tokenize,
  stemSimilarity,
  termAnswerKey,
  focusAnswerKey,
  questionFocus,
  correctChoiceText,
  dedupeQuestions,
  findParaphrasePairs,
  isCaseOrActionQuestion,
  caseActionShare,
  hashSeed,
  shuffleQuestionChoices,
  finalizeQuestions,
  normalizeQuestionShape,
};
