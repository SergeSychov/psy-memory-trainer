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
  transcriptChunkChars: 1000,
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

function missWindowCenter(sectionsLength, opts = {}) {
  if (sectionsLength <= 0) return 0;
  const idx = opts.subtopicIndex;
  const count = opts.subtopicCount;
  if (idx == null || count == null || count < 1) {
    return Math.min(sectionsLength - 1, Math.max(0, Math.floor(sectionsLength / 2)));
  }
  const i = Math.min(Math.max(Number(idx) || 0, 0), Math.max(count - 1, 0));
  if (count <= 1 || sectionsLength <= 1) return 0;
  return Math.min(
    sectionsLength - 1,
    Math.max(0, Math.round((i / (count - 1)) * (sectionsLength - 1))),
  );
}

/**
 * Pick konspekt fragment for one subtopic: best matching ## section ± neighbour,
 * capped. Never falls back to the whole konspekt.
 * opts: { subtopicIndex, subtopicCount } — center miss-window by subtopic position.
 */
function pickKonspektFragment(konspekt, title, terms, maxChars = DEFAULTS.konspektMaxChars, opts = {}) {
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
    const approx = missWindowCenter(sections.length, opts);
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

function transcriptToSegments(chunks, transcript, chunkChars = DEFAULTS.transcriptChunkChars) {
  let segs = (Array.isArray(chunks) ? chunks : [])
    .map((c) => String((c && c.text) || '').trim())
    .filter(Boolean);
  if (segs.length) return segs;
  const tr = String(transcript || '');
  if (!tr.trim()) return [];
  segs = [];
  for (let i = 0; i < tr.length; i += chunkChars) {
    segs.push(tr.slice(i, i + chunkChars));
  }
  return segs;
}

/**
 * Transcript: only matching segments ±1 neighbour, capped.
 * If chunks empty, slice raw transcript into ~1000-char pieces first.
 */
function pickTranscriptFragment(
  chunks,
  title,
  terms,
  maxChars = DEFAULTS.transcriptMaxChars,
  transcript = '',
  opts = {},
) {
  const segs = transcriptToSegments(chunks, transcript);
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
  // No lexical hit: positional window by subtopic index (same idea as konspekt miss).
  let order = Array.from(hit).sort((a, b) => a - b);
  if (!order.length && segs.length) {
    const mid = missWindowCenter(segs.length, opts);
    order = [mid];
    if (mid > 0) order.unshift(mid - 1);
    if (mid + 1 < segs.length) order.push(mid + 1);
    order.sort((a, b) => a - b);
  }
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
    matched: hit.size > 0,
  };
}

function termsForOrdinal(terms, ordinal) {
  return (terms || []).filter((t) => Number(t.subtopic_ordinal) === Number(ordinal));
}

function termsWithDefinition(terms) {
  return (terms || []).filter((t) => t && t.definition != null && String(t.definition).trim() !== '');
}

/**
 * Resolve term list for MCQ.
 * Default (ownOnly=true, gen_questions_v4): only this subtopic's terms with definition.
 * Legacy fallback (ownOnly=false): subtopic → lecture → course.
 */
function resolveTermsForSubtopic(ordinal, lectureTerms, courseTerms = [], opts = {}) {
  const ownOnly = opts.ownOnly !== false;
  const own = termsWithDefinition(termsForOrdinal(lectureTerms, ordinal));
  if (own.length) {
    return { terms: own, fallback: false, source: 'subtopic' };
  }
  if (ownOnly) {
    return { terms: [], fallback: false, source: null };
  }
  const lecture = termsWithDefinition(lectureTerms);
  if (lecture.length) {
    return { terms: lecture, fallback: true, source: 'lecture' };
  }
  const course = termsWithDefinition(courseTerms);
  if (course.length) {
    return { terms: course, fallback: true, source: 'course' };
  }
  return { terms: [], fallback: false, source: null };
}

/**
 * Pull short thesis lines from a konspekt/transcript fragment for the MCQ prompt.
 * Heuristic only — synthetic-safe, no course content in repo.
 */
function extractTheses(fragment, maxTheses = 12) {
  const text = String(fragment || '').trim();
  if (!text) return [];
  const lines = [];
  for (const raw of text.split(/\n+/)) {
    let line = raw.replace(/^#+\s*/, '').trim();
    line = line.replace(/^[-*•]\s+/, '').trim();
    line = line.replace(/^\d+[.)]\s+/, '').trim();
    if (!line || line.length < 24) continue;
    if (/^#{1,6}\s/.test(raw)) continue;
    // Prefer assertive / causal / research-like lines.
    const score =
      (/(поэтому|потому|значит|следует|если|когда|исследован|показал|причин)/i.test(line) ? 2 : 0) +
      (line.length >= 40 && line.length <= 220 ? 1 : 0);
    if (score > 0 || /^[«"]?[А-ЯA-Z]/.test(line)) {
      lines.push({ line: line.slice(0, 280), score: score || 1 });
    }
  }
  lines.sort((a, b) => b.score - a.score);
  const seen = new Set();
  const out = [];
  for (const item of lines) {
    const key = norm(item.line).slice(0, 80);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item.line);
    if (out.length >= maxTheses) break;
  }
  return out;
}

/**
 * Accept only a finished 1–2 sentence definition of meaning.
 * Reject: digit/percent scraps, mid-paragraph quotes, thesis-as-definition, one-word labels.
 */
function isValidDefinition(text, canonical) {
  const def = String(text || '').trim();
  if (!def) return { ok: false, reason: 'empty' };
  if (def.length < 12) return { ok: false, reason: 'too_short' };
  if (def.length > 400) return { ok: false, reason: 'too_long' };
  // Bare stats / percent scraps without a copula about the term.
  if (/^\s*(на\s+)?\d+([.,]\d+)?\s*%/.test(def) || /^[\d\s%,.;–—+-]+$/.test(def)) {
    return { ok: false, reason: 'stats_fragment' };
  }
  const letters = (def.match(/[а-яa-z]/gi) || []).length;
  const digits = (def.match(/\d/g) || []).length;
  if (letters < 8) return { ok: false, reason: 'too_few_letters' };
  if (digits > letters) return { ok: false, reason: 'digit_heavy' };
  // One-word / two-token label without explanation.
  const words = def.split(/\s+/).filter(Boolean);
  if (words.length < 3) return { ok: false, reason: 'label_only' };
  // Starts mid-thought (lowercase continuation / bare conjunction).
  if (/^(и|а|но|или|на|при|для|что|как|это\s+на\s+\d)/i.test(def) && !/^[А-ЯA-Z«"]/.test(def)) {
    // allow "это …" as definition pattern
    if (!/^это\s+\S+/i.test(def)) return { ok: false, reason: 'mid_paragraph' };
  }
  // Thesis-like causal claim without defining the term (heuristic).
  const name = String(canonical || '').trim().toLowerCase();
  const lower = def.toLowerCase();
  const looksLikeThesis =
    /(поэтому|значит|следует|исследование показало|по данным)/i.test(def) &&
    name &&
    !lower.includes(name.split(/\s+/)[0]) &&
    !/^(это|—|–|-)/.test(lower) &&
    !lower.includes(' — ') &&
    !lower.includes(' – ');
  if (looksLikeThesis) return { ok: false, reason: 'thesis_not_definition' };
  return { ok: true, reason: null };
}

function needsDefinitionRefill(definition, canonical) {
  if (definition == null || !String(definition).trim()) return true;
  return !isValidDefinition(definition, canonical).ok;
}

/**
 * Deterministic fill of empty/broken definitions from konspekt/transcript patterns.
 * Only keeps candidates that pass isValidDefinition.
 */
function fillDefinitionsFromText(terms, konspekt, transcript) {
  const hay = `${String(konspekt || '')}\n${String(transcript || '')}`;
  if (!hay.trim()) {
    return { terms: (terms || []).map((t) => ({ ...t })), filled: [], rejected: [] };
  }
  const filled = [];
  const rejected = [];
  const next = (terms || []).map((t) => {
    if (!needsDefinitionRefill(t.definition, t.canonical)) return { ...t };
    const names = [t.canonical].concat(t.aliases || []).map((n) => String(n || '').trim()).filter(Boolean);
    let definition = null;
    for (const name of names) {
      if (name.length < 3) continue;
      const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const patterns = [
        new RegExp(`\\*\\*${esc}\\*\\*\\s*[—:\\-]\\s*([^\\n.]{12,220})`, 'i'),
        new RegExp(`(?:^|\\n)\\s*${esc}\\s*[—:\\-]\\s*([^\\n.]{12,220})`, 'i'),
        new RegExp(`${esc}\\s+(?:—|–|-)\\s+([^\\n.]{12,220})`, 'i'),
        new RegExp(`${esc}\\s+[—–-]\\s*это\\s+([^\\n.]{12,220})`, 'i'),
      ];
      for (const re of patterns) {
        const m = hay.match(re);
        if (m && m[1]) {
          const candidate = m[1].trim().replace(/[;,.]+$/, '');
          const check = isValidDefinition(candidate, t.canonical);
          if (check.ok) {
            definition = candidate;
            break;
          }
          rejected.push({ canonical: t.canonical, reason: check.reason, sample: candidate.slice(0, 80) });
        }
      }
      if (definition) break;
    }
    if (!definition) return { ...t, definition: needsDefinitionRefill(t.definition, t.canonical) ? null : t.definition };
    filled.push(t.canonical);
    return { ...t, definition };
  });
  return { terms: next, filled, rejected };
}

/**
 * Merge LLM fill_term_definitions payload into terms (only empty/broken → valid).
 */
function applyFilledDefinitions(terms, filledPayload) {
  const byCanon = new Map();
  const rejected = [];
  for (const row of (filledPayload && filledPayload.terms) || []) {
    const c = String((row && row.canonical) || '').trim().toLowerCase();
    if (!c) continue;
    const def = row.definition == null ? null : String(row.definition).trim() || null;
    if (!def) continue;
    const check = isValidDefinition(def, c);
    if (!check.ok) {
      rejected.push({ canonical: c, reason: check.reason, sample: def.slice(0, 80) });
      continue;
    }
    const life =
      def && row.life_example != null ? String(row.life_example).trim() || null : null;
    byCanon.set(c, { definition: def, life_example: life });
  }
  const applied = [];
  const next = (terms || []).map((t) => {
    if (!needsDefinitionRefill(t.definition, t.canonical)) return { ...t };
    const hit = byCanon.get(String(t.canonical || '').trim().toLowerCase());
    if (!hit) {
      // Drop broken existing definition so it stays empty rather than garbage.
      if (needsDefinitionRefill(t.definition, t.canonical) && t.definition) {
        return { ...t, definition: null, life_example: null };
      }
      return { ...t };
    }
    applied.push(t.canonical);
    return {
      ...t,
      definition: hit.definition,
      life_example: hit.life_example || t.life_example || null,
    };
  });
  return { terms: next, applied, rejected };
}

/**
 * Merge only when a matched primary ## section exists and its length (no neighbours) < thinMin.
 * score 0 (no match) ≠ thin — leave the subtopic alone.
 */
function mergeThinSubtopics(subtopics, terms, konspekt, opts = {}) {
  const thinMin = opts.thinMinChars != null ? opts.thinMinChars : DEFAULTS.thinMinChars;
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
      const sections = sectionList(konspekt);
      const scored = sections.map((text, idx) => ({
        idx,
        text,
        score: scoreSection(text, sub.title, ownTerms),
      }));
      scored.sort((a, b) => b.score - a.score || a.idx - b.idx);
      // No matched section → not thin; do not merge.
      if (!scored[0] || scored[0].score <= 0) continue;
      const primaryChars = scored[0].text.length;
      if (primaryChars >= thinMin) continue;

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
 * Normalize extract_terms JSON payload (subtopics + terms).
 * If LLM returned no valid subtopics, synthesize one from lectureTitle
 * BEFORE binding terms — otherwise `subs[0]` throws and the job stays running.
 *
 * Does not set parseOk/fail: caller still owns schema/json errors.
 */
function normalizeTermsExtract(data, opts) {
  const lectureTitle = String((opts && opts.lectureTitle) || '').trim() || 'Лекция';
  const subs = [];
  const seen = new Set();
  for (const s of (data && data.subtopics) || []) {
    const ordinal = Number(s && s.ordinal);
    const title = String((s && s.title) || '').trim();
    if (!Number.isInteger(ordinal) || ordinal < 1 || !title || seen.has(ordinal)) continue;
    seen.add(ordinal);
    subs.push({ ordinal, title });
  }
  subs.sort((a, b) => a.ordinal - b.ordinal);

  let synthesizedSubtopic = false;
  if (!subs.length) {
    subs.push({ ordinal: 1, title: lectureTitle });
    seen.add(1);
    synthesizedSubtopic = true;
  }

  const byKey = new Map();
  for (const t of (data && data.terms) || []) {
    const canonical = String((t && t.canonical) || '').trim();
    if (!canonical) continue;
    const aliases = Array.isArray(t.aliases)
      ? t.aliases.map((a) => String(a || '').trim()).filter((a) => a && a !== canonical)
      : [];
    let definition = t.definition == null ? null : String(t.definition).trim() || null;
    let lifeExample = definition && t.life_example != null ? String(t.life_example).trim() || null : null;
    let so = Number(t.subtopic_ordinal);
    if (!seen.has(so)) {
      const lower = subs.filter((s) => s.ordinal <= so).pop();
      so = lower ? lower.ordinal : subs[0].ordinal;
    }
    const key = canonical.toLowerCase();
    if (byKey.has(key)) {
      const prev = byKey.get(key);
      prev.aliases = Array.from(new Set(prev.aliases.concat(aliases)));
      if (!prev.definition && definition) {
        prev.definition = definition;
        prev.life_example = lifeExample;
      }
      continue;
    }
    byKey.set(key, {
      canonical,
      aliases: Array.from(new Set(aliases)),
      definition,
      life_example: lifeExample,
      subtopic_ordinal: so,
    });
  }
  const terms = Array.from(byKey.values());
  return {
    subtopics: subs,
    terms,
    synthesizedSubtopic,
    lecture_terms_empty: terms.length === 0,
  };
}

/**
 * After extract_terms: decide whether to enter the subtopic loop.
 *
 * Soft (proceed=true): parseOk=true with empty lecture terms and/or empty subtopics.
 *   - course glossary has definitions → keep/synthesize subtopics, course_fallback.
 *   - nowhere defined → synthesize one subtopic only if list empty → no_terms_available.
 *
 * Hard (proceed=false): parseOk=false (invalid_json / llm_truncated / empty / schema).
 *   → Fail Context (retryable). No synthetic subtopic, no course MCQ.
 */
function finalizeTermsExtractGate(input) {
  const {
    parseOk,
    errorMessage = null,
    subtopics = [],
    terms = [],
    courseTerms = [],
    lectureTitle = '',
  } = input || {};
  const courseDefined = termsWithDefinition(courseTerms);
  const title = String(lectureTitle || '').trim() || 'Лекция';
  let subs = (subtopics || [])
    .map((s) => ({ ordinal: Number(s.ordinal), title: String(s.title || '').trim() }))
    .filter((s) => s.ordinal >= 1 && s.title)
    .sort((a, b) => a.ordinal - b.ordinal);

  // Hard parse failure → Fail Context (retryable). Do not invent a subtopic
  // (would wipe real ones on regenerate) and do not hide the error behind course MCQ.
  if (!parseOk) {
    return {
      proceed: false,
      subtopics: [],
      terms: [],
      reason: errorMessage || 'terms_extract_failed',
      lecture_terms_empty: true,
      course_fallback: false,
      hard_parse_error: true,
    };
  }

  const lectureTermsEmpty = !(terms || []).length;
  let reason = null;
  let courseFallback = false;

  if (!subs.length) {
    subs = [{ ordinal: 1, title }];
  }

  if (lectureTermsEmpty && courseDefined.length) {
    courseFallback = true;
    reason = 'course_terms_fallback';
  } else if (lectureTermsEmpty && !courseDefined.length) {
    reason = 'no_terms_available';
  }

  return {
    proceed: true,
    subtopics: subs,
    terms: terms || [],
    reason,
    lecture_terms_empty: lectureTermsEmpty,
    course_fallback: courseFallback,
    hard_parse_error: false,
  };
}

/**
 * Build questions-request fields (no LLM).
 * skip=true + skip_reason=no_terms_available when no defined terms in this subtopic
 * (ownOnly default — no lecture/course clone fallback).
 */
function buildQuestionsRequestParts(input) {
  const {
    subtopic,
    allTerms,
    courseTerms = [],
    konspektMd,
    chunks,
    transcript = '',
    constants = {},
    subtopicIndex = null,
    subtopicCount = null,
    usedPlots = [],
    usedTermAnswers = [],
    ownOnly = true,
  } = input;
  const konspektMax = constants.questions_konspekt_max_chars || DEFAULTS.konspektMaxChars;
  const transcriptMax = constants.questions_transcript_max_chars || DEFAULTS.transcriptMaxChars;

  const resolved = resolveTermsForSubtopic(subtopic.ordinal, allTerms, courseTerms, { ownOnly });
  // Fragment by title (+ own terms if any). Never dump whole konspekt / clone foreign terms.
  const konspektFrag = pickKonspektFragment(
    konspektMd,
    subtopic.title,
    resolved.terms,
    konspektMax,
    { subtopicIndex, subtopicCount },
  );
  const transcriptFrag = pickTranscriptFragment(
    chunks,
    subtopic.title,
    resolved.terms,
    transcriptMax,
    transcript,
    { subtopicIndex, subtopicCount },
  );
  const theses = extractTheses(konspektFrag.text, 12);
  const thesisMode = !resolved.terms.length && theses.length > 0;

  if (!resolved.terms.length && !theses.length) {
    return {
      skip: true,
      skip_reason: 'no_terms_or_theses',
      terms_fallback: false,
      terms_source: null,
      term_canonicals: [],
      theses: [],
      thesis_mode: false,
      konspekt_fragment: konspektFrag.text || '',
      konspekt_matched: konspektFrag.matched,
      transcript_fragment: '',
      transcript_matched: false,
      user_base: null,
      request_ready: false,
    };
  }

  const termCanonicals = resolved.terms.map((t) => t.canonical);
  const termsBlock = resolved.terms.length
    ? resolved.terms.map((t) => '- ' + t.canonical + ': ' + t.definition).join('\n')
    : '[пусто — пиши только по ТЕЗИСАМ; source_kind=thesis, term_canonical=null]';
  const thesesBlock = theses.length
    ? theses.map((t, i) => (i + 1) + '. ' + t).join('\n')
    : '[тезисы не извлечены — опирайся на фрагмент конспекта]';
  const transcriptText = transcriptFrag.text
    || '[фрагмент не найден — опирайся только на конспект]';
  const usedPlotsBlock = (usedPlots || []).length
    ? (usedPlots || []).slice(0, 40).map((s) => '- ' + s).join('\n')
    : '[пусто]';
  const usedAnswersBlock = (usedTermAnswers || []).length
    ? (usedTermAnswers || []).slice(0, 40).map((s) => '- ' + s).join('\n')
    : '[пусто]';

  const user = [
    'ПОДТЕМА\n' + subtopic.ordinal + '. ' + subtopic.title,
    'ТЕЗИСЫ\n' + thesesBlock,
    'ТЕРМИНЫ\n' + termsBlock,
    'ФРАГМЕНТ КОНСПЕКТА\n' + konspektFrag.text,
    'ФРАГМЕНТ ТРАНСКРИПТА\n' + transcriptText,
    'УЖЕ_ИСПОЛЬЗОВАНО\nСюжеты:\n' + usedPlotsBlock + '\nПары фокус–ответ:\n' + usedAnswersBlock,
  ].join('\n\n');

  return {
    skip: false,
    skip_reason: null,
    terms_fallback: resolved.fallback,
    terms_source: resolved.source,
    term_canonicals: termCanonicals,
    theses,
    thesis_mode: thesisMode,
    konspekt_fragment: konspektFrag.text,
    konspekt_matched: konspektFrag.matched,
    transcript_fragment: transcriptText,
    transcript_matched: transcriptFrag.matched,
    user_base: user,
    request_ready: true,
  };
}

module.exports = {
  DEFAULTS,
  norm,
  titleTokens,
  sectionList,
  scoreSection,
  missWindowCenter,
  pickKonspektFragment,
  transcriptToSegments,
  pickTranscriptFragment,
  termsForOrdinal,
  termsWithDefinition,
  resolveTermsForSubtopic,
  extractTheses,
  isValidDefinition,
  needsDefinitionRefill,
  fillDefinitionsFromText,
  applyFilledDefinitions,
  mergeThinSubtopics,
  normalizeTermsExtract,
  finalizeTermsExtractGate,
  buildQuestionsRequestParts,
};
