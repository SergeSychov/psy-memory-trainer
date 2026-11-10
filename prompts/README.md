# prompts

Системные промпты дешёвой LLM (DeepSeek flash / gpt-4o-mini и аналоги). В n8n уходит текст раздела «Текст system prompt», без шапки и без JSON Schema под ним. Ответ модели — один JSON-объект. Схема в файле нужна, чтобы проверить ответ до записи в Postgres.

Промпты не добавляют фактов, которых нет во входных блоках. Конспекты и транскрипты лекций в этот каталог не кладутся.

| Файл | Куда попадает |
|---|---|
| [konspekt_v1.md](konspekt_v1.md) | System prompt конспекта. В модель копируется раздел «Текст system prompt» |
| [extract_terms.md](extract_terms.md) | Один вызов на лекцию → `subtopics` и `glossary_terms` |
| [gen_questions.md](gen_questions.md) | Один вызов на подтему, во вход только её фрагмент. Не меньше 10 жизненных MCQ, либо `shortfall: true` и job failed |
| [explain_miss.md](explain_miss.md) | Только если по `term_id` не осталось сохранённых вопросов. Объяснение и один новый вопрос на тот же термин. Вызов пишется в `llm_usage` |
| [consolidate_quiz.md](consolidate_quiz.md) | Смешанный квиз 3–4 тем. Текст готов. Workflow — фаза 2, в утренний квиз фазы 1 не подмешивать |

Общие поля вопроса: `stem`, `choice_a`…`choice_d`, `correct_choice` (`A`–`D`), `explanation` (колонка `questions.explanation`), `explanation_a`…`explanation_d`, `kind` = `life_mcq`, `term_canonical`.

В черновике схемы одна текстовая колонка `explanation`. Четыре пояснения к вариантам схема не хранит. Пока колонок нет, в `questions.explanation` пишите поле `explanation` и строку пояснения выбранной буквы: бот показывает это без нового вызова LLM. Отдельные колонки под `explanation_a`…`explanation_d` — вопрос к миграции, этот каталог схему не меняет.

Приёмка `gen_questions`: `shortfall === false` и `questions.length >= 10`. Недостачу не добивать вторым слепым вызовом.

Проверка, что схемы в файлах парсятся: `node --test prompts/prompts.test.js`.
