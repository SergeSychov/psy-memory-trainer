'use strict';

/**
 * Fragment picking + thin-subtopic merge for psy_05.
 * Pure helpers — used from n8n Code (embedded) and node:test.
 */

const DEFAULTS = {
  konspektMaxChars: 5500,
  transcriptMaxChars: 7000,
  thinMinChars: 400,
  titleTokenMinLen: 5,
};

function norm(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/ё/g, 'е');
}

/** Significant tokens from a title/phrase — no short 4-char stems. */
function titleTokens(phrase, minLen = DEFAULTS.titleTokenMinLen) {
  return norm(phrase)
    .split(/[^а-яa-z0-9]+/)
    .filter((w) => w.length >= minLen);
}

function sectionList(konspekt) {
  const text = String(konspekt || '').trim();
  if (!text) return [];
  return text.split(/\n(?=##\s)/).map((s) => s.trim()).filter(Boolean);
}

function scoreSection(sectionText, title, terms) {
  const t = norm(sectionText);
  let score = 0;
  const tokens = titleTokens(title);
  for (const tok of tokens) {
    if (t.includes(tok)) score += 3;
  }
  // Prefer heading that starts with the title words
  const heading = (sectionText.split('\n')[0] || '');
  const h = norm(heading);
  if (tokens.length && tokens.every((tok) => h.includes(tok))) score += 8;
  for (const term of terms || []) {
    const names = [term.canonical].concat(term.aliases || []).map(norm).filter(Boolean);
    for (const name of names) {
      if (name.length >= 4 && t.includes(name)) score += 2;
    }
  }
  return score;
}

/**
 * Pick konspekt fragment for one subtopic: best matching ## section ± neighbour,
 * capped. Never falls back to the whole konspekt.
 */
function pickKonspektFragment(konspekt, title, terms, maxChars = DEFAULTS.konspektMaxChars) {
  const sections = sectionList(konspekt);
  if (!sections.length) {
    return { text: '', matched: false, score: 0 };
  }
  const scored = sections.map((text, idx) => ({
    idx,
    text,
    score: scoreSection(text, title, terms),
  }));
  scored.sort((a, b) => b.score - a.score || a.idx - b.idx);
  const best = scored[0];
  if (!best || best.score <= 0) {
    // No match: take a short window around the ordinal-ish position, not the whole doc.
    const approx = Math.min(
      sections.length - 1,
      Math.max(0, Math.floor(sections.length / 2)),
    );
    const window = [sections[approx]];
    if (approx > 0) window.unshift(sections[approx - 1]);
    if (approx + 1 < sections.length) window.push(sections[approx + 1]);
    let text = window.join('\n\n');
    if (text.length > maxChars) text = text.slice(0, maxChars);
    return { text, matched: false, score: 0 };
  }
  const idxs = new Set([best.idx]);
  if (best.idx > 0) idxs.add(best.idx - 1);
  if (best.idx + 1 < sections.length) idxs.add(best.idx + 1);
  let text = Array.from(idxs)
    .sort((a, b) => a - b)
    .map((i) => sections[i])
    .join('\n\n');
  if (text.length > maxChars) {
    // Prefer keeping the best section fully, then trim.
    const primary = sections[best.idx];
    if (primary.length >= maxChars) {
      text = primary.slice(0, maxChars);
    } else {
      text = text.slice(0, maxChars);
    }
  }
  return { text, matched: true, score: best.score };
}

function textMatches(haystack, title, terms) {
  const t = norm(haystack);
  if (!t) return false;
  const tokens = titleTokens(title);
  if (tokens.length && tokens.every((tok) => t.includes(tok))) return true;
  if (tokens.some((tok) => t.includes(tok))) return true;
  for (const term of terms || []) {
    const names = [term.canonical].concat(term.aliases || []).map(norm).filter(Boolean);
    for (const name of names) {
      if (name.length >= 5 && t.includes(name)) return true;
    }
  }
  return false;
}

/**
 * Transcript: only matching segments ±1 neighbour, capped.
 */
function pickTranscriptFragment(chunks, title, terms, maxChars = DEFAULTS.transcriptMaxChars) {
  let segs = (Array.isArray(chunks) ? chunks : [])
    .map((c) => String((c && c.text) || '').trim())
    .filter(Boolean);
  if (!segs.length) {
    return { text: '', matched: false };
  }
  const hit = new Set();
  segs.forEach((text, i) => {
    if (textMatches(text, title, terms)) {
      hit.add(i);
      if (i > 0) hit.add(i - 1);
      if (i + 1 < segs.length) hit.add(i + 1);
    }
  });
  const order = Array.from(hit).sort((a, b) => a - b);
  const parts = [];
  let used = 0;
  for (const i of order) {
    const text = segs[i];
    if (!text) continue;
    if (used + text.length > maxChars) break;
    parts.push(text);
    used += text.length + 1;
  }
  return {
    text: parts.length ? parts.join('\n') : '',
    matched: parts.length > 0,
  };
}

function termsForOrdinal(terms, ordinal) {
  return (terms || []).filter((t) => Number(t.subtopic_ordinal) === Number(ordinal));
}

function termsWithDefinition(terms) {
  return (terms || []).filter((t) => t && t.definition != null && String(t.definition).trim() !== '');
}

/**
 * Resolve term list for MCQ: prefer subtopic terms with definition;
 * else fall back to any lecture terms with definition (never empty skip).
 */
function resolveTermsForSubtopic(ordinal, allTerms) {
  const own = termsWithDefinition(termsForOrdinal(allTerms, ordinal));
  if (own.length) {
    return { terms: own, fallback: false };
  }
  const lecture = termsWithDefinition(allTerms);
  return { terms: lecture, fallback: lecture.length > 0 };
}

/**
 * Merge subtopics whose konspekt fragment is thinner than thinMinChars into a neighbour.
 * Re-numbers ordinals 1..N and remaps term.subtopic_ordinal.
 */
function mergeThinSubtopics(subtopics, terms, konspekt, opts = {}) {
  const thinMin = opts.thinMinChars != null ? opts.thinMinChars : DEFAULTS.thinMinChars;
  const konspektMax = opts.konspektMaxChars != null ? opts.konspektMaxChars : DEFAULTS.konspektMaxChars;
  let list = (subtopics || [])
    .map((s) => ({ ordinal: Number(s.ordinal), title: String(s.title || '').trim() }))
    .filter((s) => s.ordinal >= 1 && s.title)
    .sort((a, b) => a.ordinal - b.ordinal);

  if (list.length <= 1) {
    return { subtopics: list, terms: (terms || []).slice(), merges: [] };
  }

  const merges = [];
  let changed = true;
  while (changed && list.length > 1) {
    changed = false;
    for (let i = 0; i < list.length; i++) {
      const sub = list[i];
      const ownTerms = termsForOrdinal(terms, sub.ordinal);
      // Thinness = primary matched ## section alone (neighbours do not inflate).
      const sections = sectionList(konspekt);
      const scored = sections.map((text, idx) => ({
        idx,
        text,
        score: scoreSection(text, sub.title, ownTerms),
      }));
      scored.sort((a, b) => b.score - a.score || a.idx - b.idx);
      const primary = scored[0] && scored[0].score > 0 ? scored[0].text : '';
      const primaryChars = primary.length;
      if (primaryChars >= thinMin) continue;
      // Merge into neighbour with more primary content (prefer previous, else next).
      let targetIdx = i > 0 ? i - 1 : i + 1;
      if (i > 0 && i + 1 < list.length) {
        const prevOwn = termsForOrdinal(terms, list[i - 1].ordinal);
        const nextOwn = termsForOrdinal(terms, list[i + 1].ordinal);
        const prevPrimary = sections
          .map((text) => ({ text, score: scoreSection(text, list[i - 1].title, prevOwn) }))
          .sort((a, b) => b.score - a.score)[0];
        const nextPrimary = sections
          .map((text) => ({ text, score: scoreSection(text, list[i + 1].title, nextOwn) }))
          .sort((a, b) => b.score - a.score)[0];
        const prevLen = prevPrimary && prevPrimary.score > 0 ? prevPrimary.text.length : 0;
        const nextLen = nextPrimary && nextPrimary.score > 0 ? nextPrimary.text.length : 0;
        targetIdx = nextLen > prevLen ? i + 1 : i - 1;
      }
      const target = list[targetIdx];
      merges.push({
        from_ordinal: sub.ordinal,
        from_title: sub.title,
        into_ordinal: target.ordinal,
        into_title: target.title,
        fragment_chars: primaryChars,
      });
      target.title = target.title + ' / ' + sub.title;
      const fromOrd = sub.ordinal;
      const intoOrd = target.ordinal;
      terms = (terms || []).map((t) => {
        if (Number(t.subtopic_ordinal) === fromOrd) {
          return { ...t, subtopic_ordinal: intoOrd };
        }
        return t;
      });
      list.splice(i, 1);
      changed = true;
      break;
    }
  }

  // Re-number ordinals densely and remap terms.
  const ordMap = new Map();
  list = list.map((s, idx) => {
    const neu = idx + 1;
    ordMap.set(s.ordinal, neu);
    return { ordinal: neu, title: s.title };
  });
  const remappedTerms = (terms || []).map((t) => {
    const old = Number(t.subtopic_ordinal);
    const neu = ordMap.has(old) ? ordMap.get(old) : old;
    return { ...t, subtopic_ordinal: neu };
  });

  return { subtopics: list, terms: remappedTerms, merges };
}

/**
 * Build the questions-request payload fields (no LLM). Never skips for missing subtopic terms.
 */
function buildQuestionsRequestParts(input) {
  const {
    subtopic,
    allTerms,
    konspektMd,
    chunks,
    constants = {},
  } = input;
  const konspektMax = constants.questions_konspekt_max_chars || DEFAULTS.konspektMaxChars;
  const transcriptMax = constants.questions_transcript_max_chars || DEFAULTS.transcriptMaxChars;

  const resolved = resolveTermsForSubtopic(subtopic.ordinal, allTerms);
  const termCanonicals = resolved.terms.map((t) => t.canonical);
  const konspektFrag = pickKonspektFragment(
    konspektMd,
    subtopic.title,
    resolved.terms,
    konspektMax,
  );
  const transcriptFrag = pickTranscriptFragment(
    chunks,
    subtopic.title,
    resolved.terms,
    transcriptMax,
  );

  const termsBlock = resolved.terms.length
    ? resolved.terms.map((t) => '- ' + t.canonical + ': ' + t.definition).join('\n')
    : '- (нет терминов с определением — опирайся только на фрагмент конспекта; term_canonical не выдумывай)';

  const transcriptText = transcriptFrag.text
    || '[фрагмент не найден — опирайся только на конспект]';

  const user = [
    'ПОДТЕМА\n' + subtopic.ordinal + '. ' + subtopic.title,
    'ТЕРМИНЫ\n' + termsBlock,
    'ФРАГМЕНТ КОНСПЕКТА\n' + konspektFrag.text,
    'ФРАГМЕНТ ТРАНСКРИПТА\n' + transcriptText,
  ].join('\n\n');

  return {
    skip: false,
    skip_reason: null,
    terms_fallback: resolved.fallback,
    term_canonicals: termCanonicals,
    konspekt_fragment: konspektFrag.text,
    konspekt_matched: konspektFrag.matched,
    transcript_fragment: transcriptText,
    transcript_matched: transcriptFrag.matched,
    user_base: user,
  };
}

module.exports = {
  DEFAULTS,
  norm,
  titleTokens,
  sectionList,
  scoreSection,
  pickKonspektFragment,
  pickTranscriptFragment,
  termsForOrdinal,
  termsWithDefinition,
  resolveTermsForSubtopic,
  mergeThinSubtopics,
  buildQuestionsRequestParts,
};
