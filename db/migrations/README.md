# db/migrations

SQL-миграции Postgres/Supabase (pgvector). В kickoff каталог назван `sql/migrations`; канонический путь в этом репозитории — `db/migrations/`.

Черновик таблиц — в [docs/plan.md](../../docs/plan.md): `users`, `courses`, `lectures`, `lecture_texts`, `chunks` (эмбеддинг — отдельным решением), `glossary_terms`, `subtopics`, `questions`, `cards`, `reviews`, `settings`, `llm_usage`, `jobs`.

Файлов миграций пока нет. Первая предлагаемая миграция — `0001_init.sql`. Запускать её, тем более на проде, можно только после отдельного подтверждения владельца.

Если база на Dokploy общая с другими проектами, таблицы продукта живут в отдельной схеме. Имя схемы фиксируется на аудите, до написания SQL.
