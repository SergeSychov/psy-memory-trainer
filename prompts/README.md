# prompts

Системные промпты дешёвой LLM. В n8n уходит текст раздела «Текст system prompt», без шапки и без JSON Schema под ним.

| Файл | Куда |
|---|---|
| [konspekt_v1.md](konspekt_v1.md) | Полный конспект (эталон; psy_04 не использует) |
| [konspekt_v2.md](konspekt_v2.md) | **psy_04** — короткий конспект, `prompt_version=konspekt_v2` |
| [konspekt_reduce_v1.md](konspekt_reduce_v1.md) | **psy_04** map-reduce склейка частей |
| [extract_terms.md](extract_terms.md) | v1 без `life_example` (не трогать) |
| [extract_terms_v2.md](extract_terms_v2.md) | **psy_05** термины + `life_example` |
| [gen_questions.md](gen_questions.md) | v1 (не трогать) |
| [gen_questions_v2.md](gen_questions_v2.md) | **psy_05** MCQ; один добор при shortfall, частичный успех |
| [explain_miss.md](explain_miss.md) | объяснение при пустом пуле |
| [consolidate_quiz.md](consolidate_quiz.md) | фаза 2 |

Проверка схем: `node --test prompts/prompts.test.js`.  
Валидатор MCQ: `node --test n8n/lib/validate_questions.test.js`.
