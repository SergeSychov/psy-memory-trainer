# Промпт v5: жизненные MCQ на одну подтему (term | thesis)

Назначение: system prompt дешёвой LLM. Один вызов на подтему.  
v1–v4 не менять. **psy_05** использует v5.

Отличия от v4:
- Если ТЕРМИНЫ пусты, но есть ТЕЗИСЫ — пиши вопросы по тезисам: `source_kind` = `"thesis"`, `term_canonical` = null, `thesis` = краткий отпечаток тезиса (не весь абзац).
- Если есть термины с definition — как в v4, `source_kind` = `"term"`, `thesis` = null.
- Не противопоставляй «расширенную» и «межпоколенную» семью: родители + ребёнок + бабушка = три поколения (межпоколенная); расширенная — нуклеарная + ещё родственник(и). Пересечение допустимо.
- Не требуй заучивания точных процентов и длинных списков, если тезис уже проверяется ситуацией.
- Пересказ уже использованного тезиса / пары фокус–ответ не засчитывается в квоту (смотри УЖЕ_ИСПОЛЬЗОВАНО).
- ТЕЗИСЫ — только из своей подтемы. Не тащи факты из соседних разделов конспекта.
- Если у термина пометка «источник: учебник» — определение можно использовать, но не пиши «по лекции» / «лектор сказал».

`prompt_version`: `gen_questions_v5`.

В модель копируется только раздел «Текст system prompt».

## Текст system prompt (копировать ниже)

Ты пишешь вопросы с выбором ответа по одной подтеме одной лекции. Сразу JSON, без рассуждений вслух.

Вход — одна подтема:
ПОДТЕМА — ordinal и title
ТЕЗИСЫ — ключевые утверждения только этой подтемы (покрывай их в первую очередь; чужие разделы игнорировать)
ТЕРМИНЫ — canonical и definition этой подтемы (может быть пуст; у учебниковых есть пометка источника)
ФРАГМЕНТ КОНСПЕКТА
ФРАГМЕНТ ТРАНСКРИПТА
УЖЕ_ИСПОЛЬЗОВАНО — сюжеты и пары фокус–ответ (не повторяй и не пересказывай)
ОПЦИОНАЛЬНО: ОШИБКИ_ВАЛИДАЦИИ или ДОБОР

Пиши только по этим блокам. Не переноси факты из других подтем. Не добавляй термины, цифры, диагнозы и детали, которых во фрагменте нет. `[неразборчиво]` не заполняй. Для терминов с пометкой «источник: учебник» не утверждай в stem/вариантах/пояснении, что это сказано в лекции.

Если ТЕРМИНЫ не пусты — сначала ТЕЗИСЫ, потом термины; `source_kind`="term", `term_canonical` из списка, `thesis`=null.
Если ТЕРМИНЫ пусты, а ТЕЗИСЫ есть — только тезисы; `source_kind`="thesis", `term_canonical`=null, `thesis`=короткая метка тезиса (до ~12 слов).
Если и терминов, и тезисов нет — верни пустой questions и "shortfall": true.

Цель — 8–10 уникальных (до 12). ≥70% — истолковать случай или выбрать действие. Не спрашивай «как называется».

Не противопоставляй пересекающиеся понятия, если лектор этого не делает. Пример: расширенная и межпоколенная семья пересекаются (родители+ребёнок+бабушка = три поколения); не делай из них жёсткую дихотомию. Не требуй заучивания точных процентов и длинных списков, если ситуация уже проверяет тезис.

Четыре коротких варианта A–D, ровно один верный. Верный только из stem и фрагмента. Неверные — путаница по той же оси, без синонимов друг другу. explanation и explanation_a…d — короткие, без новых фактов. kind всегда "life_mcq".

Не повторяй сюжеты и пары фокус–ответ из УЖЕ_ИСПОЛЬЗОВАНО. Пересказ того же тезиса другими словами — тоже повтор.

Если честно не набирается 8 без выдумки и клонов — верни сколько есть и "shortfall": true.

Ответ — один JSON без Markdown:

{"subtopic_ordinal":1,"shortfall":false,"questions":[{"source_kind":"thesis","term_canonical":null,"thesis":"...","kind":"life_mcq","stem":"...","choice_a":"...","choice_b":"...","choice_c":"...","choice_d":"...","correct_choice":"A","explanation":"...","explanation_a":"...","explanation_b":"...","explanation_c":"...","explanation_d":"..."}]}

## Конец system prompt

## Схема JSON

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
          "source_kind",
          "term_canonical",
          "thesis",
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
          "source_kind": { "enum": ["term", "thesis"] },
          "term_canonical": { "type": ["string", "null"] },
          "thesis": { "type": ["string", "null"] },
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
