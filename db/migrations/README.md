# db/migrations

SQL-миграции Postgres/Supabase (pgvector). В kickoff каталог назван `sql/migrations`; канонический путь в этом репозитории — `db/migrations/`.

Таблицы — в [docs/plan.md](../../docs/plan.md): `users`, `courses`, `lectures`, `lecture_texts`, `chunks` (эмбеддинг — отдельным решением), `glossary_terms`, `subtopics`, `questions`, `cards`, `reviews`, `settings`, `llm_usage`, `jobs`.

| Файл | Статус |
|---|---|
| [`0001_init.sql`](0001_init.sql) | схема **`psy`**, на проде |
| [`0002_llm_versions_and_term_examples.sql`](0002_llm_versions_and_term_examples.sql) | `llm_usage` версии/cache/job/step, `glossary_terms.life_example`, `questions.ordinal_in_subtopic` |
| [`0003_questions_superseded.sql`](0003_questions_superseded.sql) | `questions.superseded_at` + `generation`; unique только для active life_mcq |
| [`0004_questions_thesis_nullable_term.sql`](0004_questions_thesis_nullable_term.sql) | `questions.term_id` nullable + `source_kind`/`thesis`; `cards.term_id` nullable — **не применять без «да»** |
| [`0005_glossary_definition_source.sql`](0005_glossary_definition_source.sql) | `glossary_terms.definition_source` (`lecture`\|`textbook`\|NULL); непустые → `lecture` — **не применять без «да»** |
| [`0006_quiz_sessions.sql`](0006_quiz_sessions.sql) | `quiz_sessions` + `reviews.response_ms`/`session_id` |
| [`0007_workspaces_invites.sql`](0007_workspaces_invites.sql) | `workspaces`, `course_members`, `invites`, `users.is_system_admin`, `courses.workspace_id` + backfill — **не применять без ок**; применять один раз: `psql -v ON_ERROR_STOP=1 -f` |
| [`0008_lecture_sources_quota.sql`](0008_lecture_sources_quota.sql) | шаг 2: `user_identities` (+backfill, триггер, `resolve_identity`), `users.telegram_user_id` nullable, `lectures.media_kind` + `text`, `lecture_requests`, `quota_ledger`, `intake_sessions`, `outbox`, функции `create/confirm/cancel/refund_lecture_request`, `finalize_pending_requests`, `workspace_quota_remaining`, `queue_position` — **не применять без ок**; применять один раз: `psql -v ON_ERROR_STOP=1 -f`; SQL-тест: `tests/sql/step2_quota.sql` |

`CREATE EXTENSION` нет: `pgcrypto` / `uuid-ossp` / `vector` уже в `extensions`. Колонки embedding нет.

Запускать на проде — только после отдельного подтверждения владельца. Локальный файл сам по себе прод не трогает.
