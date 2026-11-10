const test = require('node:test');
const assert = require('node:assert/strict');
const fsrs = require('./fsrs.js');

const { Rating, State, review, applyQuiz, createCard, quizRating } = fsrs;

const NOW = new Date('2026-03-01T08:00:00.000Z');
const DAY = 86400000;

function close(actual, expected, epsilon) {
  assert.ok(
    Math.abs(actual - expected) < (epsilon || 1e-6),
    actual + ' != ' + expected
  );
}

function daysUntil(card, from) {
  return (Date.parse(card.due) - from.getTime()) / DAY;
}

test('кривая: через S дней вспоминаемость 0.9, интервал при 0.9 равен S', () => {
  close(fsrs.retrievability(10, 10), 0.9, 1e-12);
  close(fsrs.intervalDays(3.173, 0.9), 3.173, 1e-12);
  assert.equal(fsrs.FACTOR, 19 / 81);
  assert.equal(fsrs.defaultWeights().length, 19);
});

test('новая карточка: state new и due = сейчас', () => {
  const card = createCard(NOW);
  assert.equal(card.state, State.New);
  assert.equal(card.stability, 0);
  assert.equal(card.difficulty, 0);
  assert.equal(card.due, NOW.toISOString());
  assert.equal(card.last_review_at, null);
  assert.equal(card.reps, 0);
  assert.equal(card.lapses, 0);
});

test('первый ответ не мутирует вход и раскладывает рейтинги по убыванию лёгкости', () => {
  const card = createCard(NOW);
  const snapshot = JSON.stringify(card);
  const again = review(card, Rating.Again, NOW);
  const hard = review(card, Rating.Hard, NOW);
  const good = review(card, Rating.Good, NOW);
  const easy = review(card, Rating.Easy, NOW);
  assert.equal(JSON.stringify(card), snapshot);

  assert.equal(again.card.state, State.Learning);
  assert.equal(hard.card.state, State.Learning);
  assert.equal(good.card.state, State.Review);
  assert.equal(easy.card.state, State.Review);

  close(again.card.stability, 0.40255);
  close(hard.card.stability, 1.18385);
  close(good.card.stability, 3.173);
  close(easy.card.stability, 15.69105);
  assert.ok(easy.card.stability > good.card.stability);
  assert.ok(good.card.stability > hard.card.stability);
  assert.ok(hard.card.stability > again.card.stability);

  close(again.card.difficulty, 7.1949);
  close(hard.card.difficulty, 6.48830527);
  close(good.card.difficulty, 5.28243442);
  close(easy.card.difficulty, 3.22450159);
  assert.ok(again.card.difficulty > hard.card.difficulty);
  assert.ok(hard.card.difficulty > good.card.difficulty);
  assert.ok(good.card.difficulty > easy.card.difficulty);

  assert.equal(good.card.scheduled_days, 3);
  assert.equal(good.card.due, new Date(NOW.getTime() + 3 * DAY).toISOString());
  assert.equal(easy.card.scheduled_days, 16);
  close(again.card.scheduled_days, 0.40255);
  close(daysUntil(again.card, NOW), 0.40255);
  close(hard.card.scheduled_days, 1.18385);

  for (const result of [again, hard, good, easy]) {
    assert.equal(result.card.reps, 1);
    assert.equal(result.card.lapses, 0);
    assert.equal(result.card.elapsed_days, 0);
    assert.equal(result.card.last_review_at, NOW.toISOString());
    assert.equal(result.algorithm, 'fsrs-5');
  }
  assert.equal(again.correct, false);
  assert.equal(good.correct, true);
  assert.equal(hard.correct, true);
});

test('квиз: неверно -> Again, верно -> Good; явный rating не затирается', () => {
  assert.equal(quizRating(false), Rating.Again);
  assert.equal(quizRating(true), Rating.Good);
  const card = createCard(NOW);
  const wrong = applyQuiz(card, { correct: false }, NOW);
  const right = applyQuiz(card, { correct: true }, NOW);
  assert.equal(wrong.rating, Rating.Again);
  assert.equal(right.rating, Rating.Good);
  assert.equal(right.card.scheduled_days, 3);

  const voiced = applyQuiz(card, { rating: Rating.Hard, correct: false }, NOW);
  assert.equal(voiced.rating, Rating.Hard);
  assert.equal(voiced.card.state, State.Learning);

  const easy = applyQuiz(card, Rating.Easy, NOW);
  assert.equal(easy.rating, Rating.Easy);
});

test('повтор через 3 дня: Good растит stability, Again — это lapse', () => {
  const first = review(createCard(NOW), Rating.Good, NOW);
  const later = new Date(NOW.getTime() + 3 * DAY);
  const good = review(first.card, Rating.Good, later);
  const lapse = review(first.card, Rating.Again, later);

  assert.equal(good.card.elapsed_days, 3);
  assert.equal(good.card.state, State.Review);
  assert.equal(good.card.reps, 2);
  assert.equal(good.card.lapses, 0);
  close(good.card.stability, 10.73892585);
  close(good.card.difficulty, 5.27296793);
  assert.equal(good.card.scheduled_days, 11);
  assert.ok(good.card.stability > first.card.stability);

  assert.equal(lapse.card.state, State.Relearning);
  assert.equal(lapse.card.lapses, 1);
  assert.equal(lapse.card.reps, 2);
  assert.equal(lapse.correct, false);
  close(lapse.card.stability, 1.05556109);
  close(lapse.card.difficulty, 6.79693258);
  close(lapse.card.scheduled_days, 1.05556109);
  assert.ok(lapse.card.stability < first.card.stability);
  assert.ok(lapse.card.stability < good.card.stability);
  const weights = fsrs.defaultWeights();
  const cap = first.card.stability * Math.exp(-weights[17] * weights[18]);
  assert.ok(lapse.card.stability <= cap + 1e-9);
});

test('меньше суток — короткая память, даже если прошло 20 часов', () => {
  const first = review(createCard(NOW), Rating.Again, NOW);
  const soon = new Date(NOW.getTime() + 20 * 60 * 60 * 1000);
  const next = review(first.card, Rating.Good, soon);
  assert.equal(next.card.elapsed_days, 0);
  assert.equal(next.card.state, State.Review);
  close(next.card.stability, 0.5666983);
  close(next.card.difficulty, 7.17663617);
  assert.equal(next.card.scheduled_days, 1);
  assert.equal(next.card.lapses, 0);
});

test('короткую память можно выключить, веса и retention берутся из settings', () => {
  const first = review(createCard(NOW), Rating.Again, NOW);
  const soon = new Date(NOW.getTime() + 20 * 60 * 60 * 1000);
  const off = review(first.card, Rating.Good, soon, { enable_short_term: false });
  close(off.card.stability, 0.40255);
  assert.equal(off.card.scheduled_days, 1);

  const weights = fsrs.defaultWeights();
  const fromRow = applyQuiz(createCard(NOW), { correct: true }, NOW, {
    desired_retention: 0.8,
    fsrs_weights: JSON.stringify(weights),
  });
  assert.equal(fromRow.card.scheduled_days, 8);

  const wrapped = applyQuiz(createCard(NOW), { correct: true }, NOW, {
    fsrs_weights: { version: 'fsrs-5', weights: weights },
    desiredRetention: 0.95,
  });
  assert.ok(wrapped.card.scheduled_days < 3);
  assert.equal(wrapped.card.scheduled_days, 1);
});

test('одинаковый вход даёт одинаковый due; потолок интервала режет Easy', () => {
  const card = createCard(NOW);
  const a = review(card, Rating.Good, NOW);
  const b = review(card, Rating.Good, NOW);
  assert.equal(a.card.due, b.card.due);
  const capped = review(card, Rating.Easy, NOW, { maximum_interval: 2 });
  assert.equal(capped.card.scheduled_days, 2);
});

test('строковые поля строки Postgres и имя состояния принимаются', () => {
  const card = createCard(NOW);
  card.state = '0';
  card.reps = '0';
  card.lapses = '0';
  const first = review(card, '3', NOW);
  first.card.state = 'review';
  first.card.stability = String(first.card.stability);
  first.card.difficulty = String(first.card.difficulty);
  const later = new Date(NOW.getTime() + 3 * DAY);
  const next = review(first.card, Rating.Good, later);
  assert.equal(next.card.reps, 2);
  close(next.card.stability, 10.73892585);
});

test('начальная stability выше 100 обрезается', () => {
  const weights = fsrs.defaultWeights();
  weights[2] = 150;
  const result = review(createCard(NOW), Rating.Good, NOW, { weights: weights });
  assert.equal(result.card.stability, 100);
});

test('мусорный вход отвергается', () => {
  const card = createCard(NOW);
  assert.throws(() => review(card, 0, NOW), /rating/);
  assert.throws(() => review(card, true, NOW), /applyQuiz/);
  assert.throws(() => quizRating('yes'), /boolean/);
  assert.throws(() => applyQuiz(card, {}, NOW), /rating или correct/);
  assert.throws(() => review(card, Rating.Good, NOW, { weights: [1, 2, 3] }), /19/);
  assert.throws(() => review(card, Rating.Good, NOW, { desired_retention: 1 }), /desired_retention/);
  const reviewed = review(card, Rating.Good, NOW);
  assert.throws(
    () => review(reviewed.card, Rating.Good, new Date(NOW.getTime() - DAY)),
    /раньше/
  );
  assert.throws(() => review({ state: 2, stability: 1, difficulty: 5, reps: 0, lapses: 0 }, Rating.Good, NOW), /last_review_at/);
  assert.throws(() => review(card, Rating.Good, 'не дата'), /now/);
});
