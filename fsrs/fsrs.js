// Расчёт FSRS-5 для узла n8n Code.
// Зависимостей нет. Вставьте файл в узел и в конце вызовите fsrs.applyQuiz(...).
// Файл сам ничего не возвращает в n8n: return остаётся на совести узла.
//
// Рейтинги: 1 Again, 2 Hard, 3 Good, 4 Easy.
// Квиз MVP: неверно -> Again, верно -> Good.
// Hard/Easy и явный rating оставлены для голосовых ответов фазы 2.
//
// Формулы — FSRS-5 (19 весов, кривая FSRS-4.5, линейное демпфирование
// сложности, короткая память в те же сутки). Случайный разброс интервала выключен.

var fsrs = (function () {
  'use strict';

  var VERSION = 'fsrs-5';
  var DECAY = -0.5;
  var FACTOR = 19 / 81;
  var S_MIN = 0.001;
  var S_MAX = 36500;
  var INIT_S_MAX = 100;
  var DAY_MS = 86400000;
  var MIN_LEARNING_DAYS = 1 / 1440;

  var Rating = Object.freeze({ Again: 1, Hard: 2, Good: 3, Easy: 4 });
  var State = Object.freeze({ New: 0, Learning: 1, Review: 2, Relearning: 3 });
  var STATE_BY_NAME = Object.freeze({
    new: State.New,
    learning: State.Learning,
    review: State.Review,
    relearning: State.Relearning,
  });

  // Опубликованные дефолты FSRS-5. Их же кладут в settings.fsrs_weights.
  var DEFAULT_WEIGHTS = Object.freeze([
    0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046, 1.54575,
    0.1192, 1.01925, 1.9395, 0.11, 0.29605, 2.2698, 0.2315, 2.9898, 0.51655,
    0.6621,
  ]);

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function defaultWeights() {
    return DEFAULT_WEIGHTS.slice();
  }

  function parseNow(now) {
    if (now instanceof Date) {
      if (Number.isNaN(now.getTime())) throw new Error('now: некорректная дата');
      return now;
    }
    if (typeof now === 'number' || typeof now === 'string') {
      var date = new Date(now);
      if (Number.isNaN(date.getTime())) throw new Error('now: некорректная дата');
      return date;
    }
    throw new Error('now: Date, миллисекунды или ISO-строка');
  }

  function assertRating(rating) {
    if (
      rating !== Rating.Again &&
      rating !== Rating.Hard &&
      rating !== Rating.Good &&
      rating !== Rating.Easy
    ) {
      throw new Error('rating: 1 Again, 2 Hard, 3 Good, 4 Easy');
    }
    return rating;
  }

  function quizRating(correct) {
    if (typeof correct !== 'boolean') {
      throw new Error('quizRating: correct должен быть boolean');
    }
    return correct ? Rating.Good : Rating.Again;
  }

  function resolveRating(answer) {
    if (typeof answer === 'number' || typeof answer === 'string') {
      return assertRating(Number(answer));
    }
    if (!answer || typeof answer !== 'object') {
      throw new Error('ответ: { correct: boolean } или { rating: 1..4 }');
    }
    if (answer.rating != null && answer.rating !== '') {
      return assertRating(Number(answer.rating));
    }
    if (typeof answer.correct === 'boolean') return quizRating(answer.correct);
    throw new Error('ответ: нужно поле rating или correct');
  }

  function resolveWeights(weights) {
    if (weights == null || weights === '') return defaultWeights();
    var value = weights;
    if (typeof value === 'string') value = JSON.parse(value);
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      if (value.weights != null) value = value.weights;
      else if (value.w != null) value = value.w;
      else if (value.fsrs_weights != null) value = value.fsrs_weights;
      if (typeof value === 'string') value = JSON.parse(value);
    }
    if (!Array.isArray(value) || value.length !== 19) {
      throw new Error('fsrs_weights: нужны 19 чисел FSRS-5');
    }
    var parsed = value.map(Number);
    for (var i = 0; i < parsed.length; i += 1) {
      if (!Number.isFinite(parsed[i])) throw new Error('fsrs_weights: нечисловой вес');
    }
    return parsed;
  }

  function pick(options, camel, snake, fallback) {
    if (!options) return fallback;
    if (options[camel] != null && options[camel] !== '') return options[camel];
    if (options[snake] != null && options[snake] !== '') return options[snake];
    return fallback;
  }

  function asBool(value, fallback) {
    if (value == null || value === '') return fallback;
    if (typeof value === 'boolean') return value;
    if (value === 'true' || value === '1' || value === 1) return true;
    if (value === 'false' || value === '0' || value === 0) return false;
    throw new Error('enable_short_term: boolean');
  }

  function asRetention(value) {
    var retention = Number(value);
    if (!(retention > 0 && retention < 1)) {
      throw new Error('desired_retention должен быть между 0 и 1');
    }
    return retention;
  }

  function asMaximumInterval(value) {
    var maxInterval = Number(value);
    if (!Number.isFinite(maxInterval) || maxInterval < 1) {
      throw new Error('maximum_interval должен быть >= 1');
    }
    return maxInterval;
  }

  function normalizeState(value) {
    if (value == null || value === '') return State.New;
    if (typeof value === 'string') {
      var key = value.trim().toLowerCase();
      if (Object.prototype.hasOwnProperty.call(STATE_BY_NAME, key)) return STATE_BY_NAME[key];
      var parsed = Number(key);
      if (Number.isInteger(parsed) && parsed >= 0 && parsed <= 3) return parsed;
    }
    if (typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 3) {
      return value;
    }
    throw new Error('state: 0 new, 1 learning, 2 review, 3 relearning');
  }

  function wholeCount(value, name) {
    var n = value == null || value === '' ? 0 : Number(value);
    if (!Number.isInteger(n) || n < 0) throw new Error(name + ': целое число >= 0');
    return n;
  }

  function normalizeCard(card) {
    if (!card || typeof card !== 'object') throw new Error('card: объект строки cards');
    var state = normalizeState(card.state);
    var stability = card.stability == null || card.stability === '' ? 0 : Number(card.stability);
    var difficulty = card.difficulty == null || card.difficulty === '' ? 0 : Number(card.difficulty);
    if (!Number.isFinite(stability) || !Number.isFinite(difficulty)) {
      throw new Error('stability и difficulty должны быть числами');
    }
    if (state !== State.New) {
      if (!(stability >= S_MIN)) throw new Error('stability карточки слишком мала');
      if (!(difficulty >= 1 && difficulty <= 10)) throw new Error('difficulty вне [1, 10]');
    }
    return {
      state: state,
      stability: stability,
      difficulty: difficulty,
      reps: wholeCount(card.reps, 'reps'),
      lapses: wholeCount(card.lapses, 'lapses'),
      last_review_at: card.last_review_at == null || card.last_review_at === ''
        ? null
        : card.last_review_at,
    };
  }

  function initDifficulty(grade, w) {
    return clamp(w[4] - Math.exp(w[5] * (grade - 1)) + 1, 1, 10);
  }

  function initStability(grade, w) {
    return clamp(w[grade - 1], S_MIN, INIT_S_MAX);
  }

  function nextDifficulty(difficulty, grade, w) {
    var delta = -w[6] * (grade - 3);
    var damped = difficulty + delta * ((10 - difficulty) / 9);
    var target = initDifficulty(Rating.Easy, w);
    return clamp(w[7] * target + (1 - w[7]) * damped, 1, 10);
  }

  function retrievability(stability, elapsedDays) {
    if (!(stability >= S_MIN)) throw new Error('retrievability: stability слишком мала');
    var t = Number(elapsedDays);
    if (!Number.isFinite(t) || t < 0) throw new Error('retrievability: elapsed_days >= 0');
    return Math.pow(1 + (FACTOR * t) / stability, DECAY);
  }

  function intervalDays(stability, desiredRetention) {
    var retention = desiredRetention == null ? 0.9 : asRetention(desiredRetention);
    return (stability / FACTOR) * (Math.pow(retention, 1 / DECAY) - 1);
  }

  function shortTermStability(stability, grade, w) {
    return clamp(stability * Math.exp(w[17] * (grade - 3 + w[18])), S_MIN, S_MAX);
  }

  function recallStability(difficulty, stability, retained, grade, w) {
    var modifier = 1;
    if (grade === Rating.Hard) modifier = w[15];
    else if (grade === Rating.Easy) modifier = w[16];
    var increase =
      Math.exp(w[8]) *
      (11 - difficulty) *
      Math.pow(stability, -w[9]) *
      Math.expm1(w[10] * (1 - retained)) *
      modifier;
    return clamp(stability * (increase + 1), S_MIN, S_MAX);
  }

  function forgetStability(difficulty, stability, retained, w, enableShortTerm) {
    var raw =
      w[11] *
      Math.pow(difficulty, -w[12]) *
      (Math.pow(stability + 1, w[13]) - 1) *
      Math.exp(w[14] * (1 - retained));
    var cap = enableShortTerm ? stability * Math.exp(-w[17] * w[18]) : stability;
    return clamp(Math.min(raw, cap), S_MIN, S_MAX);
  }

  function elapsedDays(state, lastReviewAt, now) {
    if (state === State.New) return 0;
    if (lastReviewAt == null) {
      throw new Error('last_review_at пуст у карточки, которая уже не new');
    }
    var last = parseNow(lastReviewAt);
    var ms = now.getTime() - last.getTime();
    if (ms < 0) throw new Error('now раньше last_review_at');
    return Math.floor(ms / DAY_MS);
  }

  function nextState(state, grade) {
    if (grade === Rating.Again) {
      return state === State.Review || state === State.Relearning
        ? State.Relearning
        : State.Learning;
    }
    if (grade === Rating.Hard) {
      if (state === State.Review) return State.Review;
      if (state === State.Relearning) return State.Relearning;
      return State.Learning;
    }
    return State.Review;
  }

  function scheduledDays(state, stability, retention, maximumInterval) {
    var raw = intervalDays(stability, retention);
    if (state === State.Review) return clamp(Math.round(raw), 1, maximumInterval);
    return clamp(Math.max(raw, MIN_LEARNING_DAYS), MIN_LEARNING_DAYS, maximumInterval);
  }

  function createCard(now) {
    var at = parseNow(now == null ? new Date() : now);
    return {
      state: State.New,
      stability: 0,
      difficulty: 0,
      due: at.toISOString(),
      last_review_at: null,
      reps: 0,
      lapses: 0,
      elapsed_days: 0,
      scheduled_days: 0,
    };
  }

  function review(card, rating, now, options) {
    if (typeof rating === 'boolean') {
      throw new Error('review ждёт rating 1..4; для верно/неверно вызовите applyQuiz');
    }
    var grade = assertRating(typeof rating === 'string' ? Number(rating) : rating);
    var at = parseNow(now);
    var current = normalizeCard(card);
    var w = resolveWeights(pick(options, 'weights', 'fsrs_weights', null));
    var retention = asRetention(pick(options, 'desiredRetention', 'desired_retention', 0.9));
    var maximumInterval = asMaximumInterval(
      pick(options, 'maximumInterval', 'maximum_interval', 36500)
    );
    var enableShortTerm = asBool(
      pick(options, 'enableShortTerm', 'enable_short_term', true),
      true
    );
    var elapsed = elapsedDays(current.state, current.last_review_at, at);
    var memory;
    if (current.state === State.New) {
      memory = {
        stability: initStability(grade, w),
        difficulty: initDifficulty(grade, w),
      };
    } else if (elapsed === 0 && enableShortTerm) {
      memory = {
        stability: shortTermStability(current.stability, grade, w),
        difficulty: nextDifficulty(current.difficulty, grade, w),
      };
    } else {
      var retained = elapsed === 0 ? 1 : retrievability(current.stability, elapsed);
      memory = {
        stability:
          grade === Rating.Again
            ? forgetStability(current.difficulty, current.stability, retained, w, enableShortTerm)
            : recallStability(current.difficulty, current.stability, retained, grade, w),
        difficulty: nextDifficulty(current.difficulty, grade, w),
      };
    }
    var state = nextState(current.state, grade);
    var interval = scheduledDays(state, memory.stability, retention, maximumInterval);
    return {
      algorithm: VERSION,
      rating: grade,
      correct: grade !== Rating.Again,
      card: {
        state: state,
        stability: memory.stability,
        difficulty: memory.difficulty,
        due: new Date(at.getTime() + interval * DAY_MS).toISOString(),
        last_review_at: at.toISOString(),
        reps: current.reps + 1,
        lapses: current.lapses + (current.state === State.Review && grade === Rating.Again ? 1 : 0),
        elapsed_days: elapsed,
        scheduled_days: interval,
      },
    };
  }

  function applyQuiz(card, answer, now, options) {
    return review(card, resolveRating(answer), now, options);
  }

  return {
    VERSION: VERSION,
    DECAY: DECAY,
    FACTOR: FACTOR,
    Rating: Rating,
    State: State,
    defaultWeights: defaultWeights,
    createCard: createCard,
    review: review,
    applyQuiz: applyQuiz,
    quizRating: quizRating,
    retrievability: retrievability,
    intervalDays: intervalDays,
  };
})();

if (typeof module === 'object' && module.exports) {
  module.exports = fsrs;
}
