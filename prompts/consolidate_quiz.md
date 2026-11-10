# Промпт: смешанный квиз 3–4 тем

Назначение: system prompt дешёвой LLM для консолидации. Текст промпта готов сейчас. Workflow, который его вызывает, — фаза 2: пауза после учёбы и смешанный квиз. В утренний FSRS-квиз фазы 1 его не подмешивать.

Вход короткий: 3 или 4 подтемы, у каждой title и термины с одной строкой definition. Полный транскрипт не передавать — это лишние токены и соблазн дописать лекцию.

В модель копируется только раздел «Текст system prompt».

## Текст system prompt (копировать ниже)

Собери смешанный квиз по подтемам, которые ученик уже проходил по отдельности. Сразу JSON.

Вход — список из 3 или 4 подтем. У каждой есть title и термины: canonical плюс одна строка definition. Другого текста нет.

Не выходи за эти строки. Не связывай подтемы выводом, которого во входе нет. Не добавляй термины.

Если подтем меньше 3 или больше 4, верни только {"error":"need 3-4 topics"}.

Иначе 8 вопросов. В каждом одна подтема: поля subtopic_title и term_canonical. Подтемы чередуются, ни одна не занимает больше трёх вопросов подряд. Вопрос — жизненная ситуация, не определение в лоб. Четыре коротких варианта, ровно один верный. Неверные — близкая путаница по переданным строкам. К каждому варианту одно короткое пояснение. explanation — 2–4 предложения, если ученик ошибся. kind — "life_mcq".

Ответ — один JSON без Markdown:

{"topics":["..."],"questions":[{"subtopic_title":"...","term_canonical":"...","kind":"life_mcq","stem":"...","choice_a":"...","choice_b":"...","choice_c":"...","choice_d":"...","correct_choice":"A","explanation":"...","explanation_a":"...","explanation_b":"...","explanation_c":"...","explanation_d":"..."}]}

## Конец system prompt

## Схема JSON

Либо отказ из-за числа тем, либо квиз. Поля вопроса те же, что в `gen_questions.md`, плюс `subtopic_title`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["error"],
      "properties": { "error": { "const": "need 3-4 topics" } }
    },
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["topics", "questions"],
      "properties": {
        "topics": {
          "type": "array",
          "minItems": 3,
          "maxItems": 4,
          "items": { "type": "string", "minLength": 1 }
        },
        "questions": {
          "type": "array",
          "minItems": 8,
          "maxItems": 8,
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "subtopic_title",
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
              "subtopic_title": { "type": "string", "minLength": 1 },
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
  ]
}
```
