# fsrs

Чистый расчёт FSRS-5 на JavaScript без зависимостей. Сети, базы и Telegram внутри нет. Файл `fsrs.js` вставляется в узел n8n Code целиком. Узел сам обязан сделать `return`: библиотека только объявляет `fsrs`.

Тесты: `node --test fsrs/fsrs.test.js`.

## Что это за алгоритм

FSRS-5, 19 опубликованных весов, кривая забывания FSRS-4.5 (`decay = -0.5`, `factor = 19/81`). При целевой вспоминаемости 0.9 интервал в днях равен stability. Сложность после первого ответа — `D0`, дальше линейное демпфирование и возврат к сложности первого Easy. Отзыв в те же сутки (меньше 24 часов с прошлого ответа) обновляет stability короткой формулой. Случайного разброса интервала нет: один и тот же вход даёт тот же `due`.

Веса по умолчанию — массив, который кладётся в `settings.fsrs_weights`:

```json
[0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046, 1.54575, 0.1192, 1.01925, 1.9395, 0.11, 0.29605, 2.2698, 0.2315, 2.9898, 0.51655, 0.6621]
```

`settings.desired_retention` по плану стартует с `0.9` и в расчёт передаётся отдельно. Объект `{ "weights": [ ... ] }` тоже принимается.

FSRS-6 (21 вес и обучаемый decay) сюда не входит. Колонка `fsrs_weights` — jsonb, смена версии не требует новой колонки, но требует новой функции.

## Состояние карточки

Поля те же, что у строки `cards`:

| Поле | Смысл |
|---|---|
| `state` | `0` new, `1` learning, `2` review, `3` relearning. Принимаются и имена `new` / `learning` / `review` / `relearning` |
| `stability`, `difficulty` | Память FSRS. У новой карточки оба нуля |
| `due`, `last_review_at` | ISO-время |
| `reps`, `lapses` | Повторы и провалы уже выученной карточки |
| `elapsed_days` | Целые сутки с прошлого ответа, которыми пользовался этот вызов |
| `scheduled_days` | Назначенный интервал. У `review` это целое число дней, минимум 1. У `learning` и `relearning` может быть дробь суток |

`fsrs.createCard(now)` даёт посев `psy_06`: `state = 0`, `due = now`.

Минутных шагов Anki (1 минута, 10 минут) нет: в схеме нет счётчика шага. Пока карточка в learning или relearning, срок берётся из stability и может быть меньше суток. Утренний cron заберёт её, когда `due <= now`. Again по карточке в `review` увеличивает `lapses` на 1 и переводит в `relearning`. Again по новой карточке lapse не считает.

## Рейтинги и квиз

`1` Again, `2` Hard, `3` Good, `4` Easy.

Квиз MVP: неверно → Again, верно → Good.

```javascript
fsrs.quizRating(false); // 1
fsrs.quizRating(true);  // 3
```

`applyQuiz` принимает `{ correct: true/false }` или `{ rating: 1..4 }`. Если переданы оба, побеждает `rating`: так фаза 2 сможет прислать Hard/Easy или оценку голосового ответа, не меняя функцию. `correct` в результате — это `rating !== Again`. Hard для FSRS успешное воспоминание, поэтому у него `correct: true`. Квиз фазы 1 Hard не шлёт.

Вставка в узел Code, имена соседних узлов — для примера:

```javascript
const card = $('cards').item.json;
const settings = $('settings').item.json;
const correct = $json.chosen_choice === $json.correct_choice;
const result = fsrs.applyQuiz(card, { correct: correct }, new Date(), {
  desired_retention: settings.desired_retention,
  fsrs_weights: settings.fsrs_weights,
});
return {
  json: {
    card: result.card,
    rating: result.rating,
    correct: result.correct,
    channel: 'telegram',
  },
};
```

`result.card` — поля для `UPDATE cards`. `rating` и `correct` — для `INSERT` в `reviews`. Канал (`telegram`, позже `car_audio`) функция не хранит: его пишет workflow. Входная карточка не изменяется.

`review(card, rating, now, options)` — то же самое, если рейтинг уже числом.
