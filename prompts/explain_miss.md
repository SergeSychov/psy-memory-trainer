# Промпт: ошибка и новый вопрос на тот же термин

Назначение: system prompt дешёвой LLM. Запасной путь, не основной.

Сначала бот берёт другой уже сохранённый вопрос того же `term_id` и показывает `explanation` выбранного варианта. Этот промпт вызывается, только если пул по термину пуст. Вызов пишется в `llm_usage`. Новый вопрос сохраняется в `questions` с тем же `term_id` и `subtopic_id`, чтобы следующий промах снова обошёлся без LLM.

В модель копируется только раздел «Текст system prompt».

## Текст system prompt (копировать ниже)

Ученик ошибся, а другого вопроса на этот термин нет. Дай короткое объяснение и один новый вопрос на тот же термин. Сразу JSON.

Вход:
ТЕРМИН — canonical и definition
СТАРЫЙ ВОПРОС — stem и четыре варианта
ВЫБРАННАЯ БУКВА
ВЕРНАЯ БУКВА
ФРАГМЕНТ — конспект или транскрипт только про этот термин

explanation — почему выбранная буква не следует из фрагмента и что сказано вместо этого. Без оценки ученика и без новых фактов. `[неразборчиво]` не раскрывай.

Новый вопрос — другая жизненная ситуация на тот же term_canonical, не перефраз старого stem. Четыре коротких варианта, ровно один верный. Неверные — близкая путаница по этому фрагменту. К каждому варианту одно короткое пояснение. kind — "life_mcq". Не добавляй термины и детали, которых во фрагменте нет.

Ответ — один JSON без Markdown:

{"explanation":"...","question":{"term_canonical":"...","kind":"life_mcq","stem":"...","choice_a":"...","choice_b":"...","choice_c":"...","choice_d":"...","correct_choice":"A","explanation":"...","explanation_a":"...","explanation_b":"...","explanation_c":"...","explanation_d":"..."}}

## Конец system prompt

## Схема JSON

Поля `question` совпадают с элементом `questions` в `gen_questions.md`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["explanation", "question"],
  "properties": {
    "explanation": { "type": "string", "minLength": 1 },
    "question": {
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
```
