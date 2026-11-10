# db/migrations

SQL-миграции Postgres/Supabase (pgvector). В kickoff каталог назван `sql/migrations`; канонический путь в этом репозитории — `db/migrations/`.

Таблицы — в [docs/plan.md](../../docs/plan.md): `users`, `courses`, `lectures`, `lecture_texts`, `chunks` (эмбеддинг — отдельным решением), `glossary_terms`, `subtopics`, `questions`, `cards`, `reviews`, `settings`, `llm_usage`, `jobs`.

Первая миграция: [`0001_init.sql`](0001_init.sql) — схема **`psy`** (аудит: общая Supabase, `public` занят). `CREATE EXTENSION` нет: `pgcrypto` / `uuid-ossp` / `vector` уже в `extensions`. Колонки embedding нет.

Запускать на проде — только после отдельного подтверждения владельца. Локальный файл сам по себе прод не трогает.
