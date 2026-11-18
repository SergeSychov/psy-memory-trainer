-- psy-memory-trainer / psy_05 quality
-- Soft-archive for life_mcq: keep old rows, free (subtopic_id, ordinal) for a new generation.
-- Прогон на проде — только после отдельного подтверждения владельца.

BEGIN;

ALTER TABLE psy.questions
    ADD COLUMN IF NOT EXISTS superseded_at timestamptz;

ALTER TABLE psy.questions
    ADD COLUMN IF NOT EXISTS generation smallint NOT NULL DEFAULT 1;

COMMENT ON COLUMN psy.questions.superseded_at IS
    'NULL = active life_mcq; set when replaced by a newer generation. Never DELETE for regen.';
COMMENT ON COLUMN psy.questions.generation IS
    'Monotonic generation within lecture/subtopic; bumped when superseding.';

-- Old unique covered all life_mcq; active-only unique allows archived ordinals to coexist.
DROP INDEX IF EXISTS psy.questions_subtopic_ordinal_life_mcq_key;
CREATE UNIQUE INDEX questions_subtopic_ordinal_life_mcq_key
    ON psy.questions (subtopic_id, ordinal_in_subtopic)
    WHERE kind = 'life_mcq' AND superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS questions_lecture_active_life_mcq_idx
    ON psy.questions (lecture_id)
    WHERE kind = 'life_mcq' AND superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS questions_superseded_at_idx
    ON psy.questions (superseded_at)
    WHERE superseded_at IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
