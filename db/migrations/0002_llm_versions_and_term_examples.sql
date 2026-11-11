-- psy-memory-trainer / фаза 1, шаг psy_04–05
-- Аддитивно к 0001: версии LLM, life_example терминов, идемпотентность вопросов.
-- Эмбеддинги / vector(N) по-прежнему не создаём.
--
-- Прогон на проде — только после отдельного подтверждения владельца.

BEGIN;

-- --- llm_usage: версии промпта/workflow, связь с job, cache hit, шаг ---
ALTER TABLE psy.llm_usage
    ADD COLUMN IF NOT EXISTS prompt_version text,
    ADD COLUMN IF NOT EXISTS workflow_version text,
    ADD COLUMN IF NOT EXISTS job_id uuid REFERENCES psy.jobs (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS prompt_cache_hit_tokens integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS step text;

ALTER TABLE psy.llm_usage
    DROP CONSTRAINT IF EXISTS llm_usage_prompt_cache_hit_tokens_check;

ALTER TABLE psy.llm_usage
    ADD CONSTRAINT llm_usage_prompt_cache_hit_tokens_check
        CHECK (prompt_cache_hit_tokens >= 0);

CREATE INDEX IF NOT EXISTS llm_usage_job_id_idx ON psy.llm_usage (job_id);
CREATE INDEX IF NOT EXISTS llm_usage_lecture_id_created_at_idx
    ON psy.llm_usage (lecture_id, created_at);

-- --- glossary_terms: бытовая иллюстрация определения из этой лекции ---
ALTER TABLE psy.glossary_terms
    ADD COLUMN IF NOT EXISTS life_example text;

-- --- questions: порядковый номер в подтеме для идемпотентной перегенерации ---
ALTER TABLE psy.questions
    ADD COLUMN IF NOT EXISTS ordinal_in_subtopic smallint;

-- Номер обязателен по смыслу только для основных вопросов (life_mcq);
-- followup/consolidation создаются на лету и номера не получают (NULL).
-- Заполнить существующие life_mcq стабильным порядком, чтобы UNIQUE прошёл.
WITH numbered AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY subtopic_id
            ORDER BY created_at, id
        )::smallint AS rn
    FROM psy.questions
    WHERE ordinal_in_subtopic IS NULL AND kind = 'life_mcq'
)
UPDATE psy.questions q
SET ordinal_in_subtopic = numbered.rn
FROM numbered
WHERE q.id = numbered.id;

DROP INDEX IF EXISTS psy.questions_subtopic_ordinal_life_mcq_key;
CREATE UNIQUE INDEX questions_subtopic_ordinal_life_mcq_key
    ON psy.questions (subtopic_id, ordinal_in_subtopic)
    WHERE kind = 'life_mcq';

ALTER TABLE psy.questions
    DROP CONSTRAINT IF EXISTS questions_life_mcq_ordinal_required;
ALTER TABLE psy.questions
    ADD CONSTRAINT questions_life_mcq_ordinal_required
        CHECK (kind <> 'life_mcq' OR ordinal_in_subtopic IS NOT NULL);

ALTER TABLE psy.questions
    DROP CONSTRAINT IF EXISTS questions_ordinal_in_subtopic_check;

ALTER TABLE psy.questions
    ADD CONSTRAINT questions_ordinal_in_subtopic_check
        CHECK (ordinal_in_subtopic >= 1);

-- Права для роли n8n (как в 0001: только если роль уже есть).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT USAGE ON SCHEMA psy TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA psy TO psy_n8n;
        ALTER DEFAULT PRIVILEGES IN SCHEMA psy
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO psy_n8n;
        ALTER DEFAULT PRIVILEGES IN SCHEMA psy
            GRANT USAGE, SELECT ON SEQUENCES TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
