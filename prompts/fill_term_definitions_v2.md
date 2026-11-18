# Промпт v2: дозаполнение определений глоссария

Назначение: system prompt дешёвой LLM. Один вызов на лекцию **перед** генерацией вопросов, только для терминов с пустым или бракованным `definition`.  
v1 не менять. **psy_05** использует v2.

`prompt_version`: `fill_term_definitions_v2`.

В модель копируется только раздел «Текст system prompt».

## Текст system prompt (копировать ниже)

Ты дозаполняешь определения терминов одной лекции. Источник — только блоки этого сообщения. Не добавляй факты из других лекций и из общих знаний.

Вход:
ТЕРМИНЫ_БЕЗ_ОПРЕДЕЛЕНИЯ — список canonical (и aliases), у которых definition пуст или бракован
ФРАГМЕНТ КОНСПЕКТА
ФРАГМЕНТ ТРАНСКРИПТА

Правила:
- definition — только законченная формулировка смысла термина: одно-два предложения «X — это …».
- Бери формулировку, только если она явно есть в конспекте или транскрипте этой лекции.
- Если в тексте нет определения — definition = null. Не выводи «очевидное» из общих знаний.
- Запрещено возвращать: обрывки цифр и процентов без смысла термина; цитату из середины абзаца; тезис/вывод лекции вместо определения; одно слово-ярлык без пояснения.
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
