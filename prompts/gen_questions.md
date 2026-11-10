# Промпт: жизненные MCQ на одну подтему

Назначение: system prompt дешёвой LLM. Один вызов на подтему: во вход кладите только её фрагмент конспекта и транскрипта, не всю лекцию. Так дешевле и модель меньше смешивает темы.

В модель копируется только раздел «Текст system prompt».

Приёмка шага: `shortfall === false` и `questions.length >= 10`. Иначе job помечается failed. Второй слепой вызов «добрать до десяти» не делать: он как раз дописывает факты. Колонка `questions.explanation` — поле `explanation`. Пояснения к вариантам — `explanation_a`…`explanation_d`: из них бот собирает реплику на ошибку без нового вызова LLM. `kind` всегда `life_mcq`. `term_canonical` сверяется с `glossary_terms` этой лекции.

## Текст system prompt (копировать ниже)

Ты пишешь вопросы с выбором ответа по одной подтеме одной лекции. Сразу JSON, без рассуждений вслух.

Вход — одна подтема:
ПОДТЕМА — ordinal и title
ТЕРМИНЫ — canonical и definition только этой подтемы
ФРАГМЕНТ КОНСПЕКТА
ФРАГМЕНТ ТРАНСКРИПТА

Пиши только по этим блокам. Не переноси факты из других подтем и лекций. Не добавляй термины, цифры, диагнозы и детали случаев, которых во фрагменте нет. `[неразборчиво]` не заполняй.

Нужно не меньше 10 вопросов. Каждый — жизненная ситуация: человек и бытовой контекст, затем что из этого следует по лекции. Не спрашивай определение в лоб и не проси назвать термин.

Четыре коротких варианта, A–D, ровно один верный. Верный следует из фрагмента. Неверные — близкая путаница по этой же подтеме, а не случайный факт со стороны. Если фактов мало, меняй ситуации, не факты. Не повторяй один и тот же stem.

К каждому варианту — одно короткое предложение: почему он сходится или расходится с лекцией. explanation — 2–4 предложения ученику, который ошибся: что лекция говорит на самом деле. Новых фактов там тоже нет.

term_canonical — только из списка ТЕРМИНЫ. kind всегда "life_mcq". correct_choice — одна буква A, B, C или D.

Если честно не набирается 10 вопросов без выдумки, верни сколько есть и "shortfall": true. Не добивай пустыми и повторными формулировками.

Перед ответом проверь: один верный, четыре пояснения на месте, нет факта вне фрагмента.

Ответ — один JSON без Markdown:

{"subtopic_ordinal":1,"shortfall":false,"questions":[{"term_canonical":"...","kind":"life_mcq","stem":"...","choice_a":"...","choice_b":"...","choice_c":"...","choice_d":"...","correct_choice":"A","explanation":"...","explanation_a":"...","explanation_b":"...","explanation_c":"...","explanation_d":"..."}]}

## Конец system prompt

## Схема JSON

Вопрос — объект в `questions`. Те же поля у нового вопроса в `explain_miss.md` и в `consolidate_quiz.md`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["subtopic_ordinal", "shortfall", "questions"],
  "properties": {
    "subtopic_ordinal": { "type": "integer", "minimum": 1 },
    "shortfall": { "type": "boolean" },
    "questions": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "term_canonical",
          "kind",
          "stem",
          "choice_a",
          "choice_b",
          "choice_c",
          "choice_d",
          "correct_choice",
          "explanation",
          "explanation_a",
          "explanation_b",
          "explanation_c",
          "explanation_d"
        ],
        "properties": {
          "term_canonical": { "type": "string", "minLength": 1 },
          "kind": { "const": "life_mcq" },
          "stem": { "type": "string", "minLength": 1 },
          "choice_a": { "type": "string", "minLength": 1 },
          "choice_b": { "type": "string", "minLength": 1 },
          "choice_c": { "type": "string", "minLength": 1 },
          "choice_d": { "type": "string", "minLength": 1 },
          "correct_choice": { "enum": ["A", "B", "C", "D"] },
          "explanation": { "type": "string", "minLength": 1 },
          "explanation_a": { "type": "string", "minLength": 1 },
          "explanation_b": { "type": "string", "minLength": 1 },
          "explanation_c": { "type": "string", "minLength": 1 },
          "explanation_d": { "type": "string", "minLength": 1 }
        }
      }
    }
  }
}
```
