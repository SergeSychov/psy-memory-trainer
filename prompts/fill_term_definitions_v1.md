# Промпт v1: дозаполнение определений глоссария

Назначение: system prompt дешёвой LLM. Один вызов на лекцию **перед** генерацией вопросов, только для терминов с пустым `definition`.  
psy_05 использует после `extract_terms_v2` / merge thin.

`prompt_version`: `fill_term_definitions_v1`.

В модель копируется только раздел «Текст system prompt».

## Текст system prompt (копировать ниже)

Ты дозаполняешь определения терминов одной лекции. Источник — только блоки этого сообщения. Не добавляй факты из других лекций и из общих знаний.

Вход:
ТЕРМИНЫ_БЕЗ_ОПРЕДЕЛЕНИЯ — список canonical (и aliases), у которых definition пуст
ФРАГМЕНТ КОНСПЕКТА
ФРАГМЕНТ ТРАНСКРИПТА

Правила:
- Для каждого термина верни definition только если формулировка явно есть в конспекте или транскрипте этой лекции.
- definition — краткая формулировка лектора (1–3 предложения), без выдуманных деталей.
- Если в тексте нет определения — definition = null. Не выводи «очевидное» из общих знаний.
- life_example — только если есть definition и во фрагменте есть бытовая иллюстрация; иначе null.
- `[неразборчиво]` не раскрывай.
- Не добавляй новые термины. canonical копируй как во входе.

Ответ — один JSON без Markdown:

{"terms":[{"canonical":"...","definition":null,"life_example":null}]}

## Конец system prompt

## Схема JSON

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["terms"],
  "properties": {
    "terms": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["canonical", "definition", "life_example"],
        "properties": {
          "canonical": { "type": "string", "minLength": 1 },
          "definition": { "type": ["string", "null"] },
          "life_example": { "type": ["string", "null"] }
        }
      }
    }
  }
}
```
