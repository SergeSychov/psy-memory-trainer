# Промпт v2: термины и подтемы (с life_example)

Назначение: system prompt дешёвой LLM. Один вызов на лекцию после конспекта.  
v1 (`extract_terms.md`) не менять. psy_05 использует v2.

`prompt_version`: `extract_terms_v2`.

В модель копируется только раздел «Текст system prompt».

Куда пишет n8n: `subtopics`, `glossary_terms` (+ `life_example`).

## Текст system prompt (копировать ниже)

Ты выделяешь подтемы и термины одной лекции. Источник — только блоки этого сообщения. Не добавляй понятия, определения и примеры из других лекций и из общих знаний.

Вход:
МЕТАДАННЫЕ
ГЛОССАРИЙ — уже известные написания, может быть пуст
КОНСПЕКТ
ТРАНСКРИПТ

Правила:
- Подтема — отдельный учебный узел этой лекции, в порядке изложения. ordinal с 1. Не дроби лекцию на одну подтему и не копируй оглавление чужого курса.
- Термин — имя понятия, которое лектор здесь употребляет. Бытовые слова, имена людей и разовый пример термином не являются.
- definition — только формулировка из этой лекции. Нет формулировки — null. Дыру `[неразборчиво]` не раскрывай.
- life_example — 1–2 предложения бытовой ситуации, которая иллюстрирует definition из этой лекции. Не вводит новых фактов. Если definition = null, то life_example = null.
- canonical — написание из ГЛОССАРИЙ, если термин там есть; иначе как в конспекте.
- aliases — только другие написания из глоссария или явная оговорка лектора. Не выдумывай варианты.
- subtopic_ordinal — подтема, в которой термин разбирается. Нет явной подтемы — ближайшая по тексту.

Ответ — один JSON-объект без Markdown и без текста вокруг:

{"subtopics":[{"ordinal":1,"title":"..."}],"terms":[{"canonical":"...","aliases":[],"definition":null,"life_example":null,"subtopic_ordinal":1}]}

Пустая лекция — пустые массивы. Не объясняй ответ.

## Конец system prompt

## Схема JSON

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["subtopics", "terms"],
  "properties": {
    "subtopics": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["ordinal", "title"],
        "properties": {
          "ordinal": { "type": "integer", "minimum": 1 },
          "title": { "type": "string", "minLength": 1 }
        }
      }
    },
    "terms": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["canonical", "aliases", "definition", "life_example", "subtopic_ordinal"],
        "properties": {
          "canonical": { "type": "string", "minLength": 1 },
          "aliases": { "type": "array", "items": { "type": "string" } },
          "definition": { "type": ["string", "null"] },
          "life_example": { "type": ["string", "null"] },
          "subtopic_ordinal": { "type": "integer", "minimum": 1 }
        }
      }
    }
  }
}
```
