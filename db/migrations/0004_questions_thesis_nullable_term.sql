-- psy-memory-trainer / psy_05 thesis MCQ
-- questions.term_id → nullable; source_kind term|thesis + thesis text.
-- cards.term_id → nullable so FSRS insert from thesis questions does not fail.
-- Прогон на проде — только после отдельного подтверждения владельца (эта задача = «да» на 0004).

BEGIN;

ALTER TABLE psy.questions
    ALTER COLUMN term_id DROP NOT NULL;

ALTER TABLE psy.questions
    ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'term';

ALTER TABLE psy.questions
    ADD COLUMN IF NOT EXISTS thesis text;

COMMENT ON COLUMN psy.questions.source_kind IS
    'term = MCQ anchored to glossary_terms; thesis = MCQ from subtopic thesis, term_id may be NULL.';
COMMENT ON COLUMN psy.questions.thesis IS
    'Short thesis fingerprint when source_kind=thesis; NULL for term questions.';

ALTER TABLE psy.questions
    DROP CONSTRAINT IF EXISTS questions_source_kind_check;
ALTER TABLE psy.questions
    ADD CONSTRAINT questions_source_kind_check
        CHECK (source_kind IN ('term', 'thesis'));

ALTER TABLE psy.questions
    DROP CONSTRAINT IF EXISTS questions_term_or_thesis_check;
ALTER TABLE psy.questions
    ADD CONSTRAINT questions_term_or_thesis_check
        CHECK (
            (source_kind = 'term' AND term_id IS NOT NULL)
            OR (
                source_kind = 'thesis'
                AND thesis IS NOT NULL
                AND length(trim(thesis)) > 0
            )
        );

-- Backfill: existing rows stay source_kind=term (default) with non-null term_id.

ALTER TABLE psy.cards
    ALTER COLUMN term_id DROP NOT NULL;

COMMENT ON COLUMN psy.cards.term_id IS
    'Nullable when the linked question is thesis-based (questions.term_id IS NULL).';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
