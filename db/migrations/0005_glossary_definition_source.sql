-- psy-memory-trainer / glossary definition provenance
-- definition_source: lecture | textbook | NULL
-- Прогон на проде — только после отдельного подтверждения владельца (эта задача = «да» на 0005).

BEGIN;

ALTER TABLE psy.glossary_terms
    ADD COLUMN IF NOT EXISTS definition_source text;

COMMENT ON COLUMN psy.glossary_terms.definition_source IS
    'lecture = filled from this lecture; textbook = curated from textbook; NULL = unknown/empty.';

ALTER TABLE psy.glossary_terms
    DROP CONSTRAINT IF EXISTS glossary_terms_definition_source_check;
ALTER TABLE psy.glossary_terms
    ADD CONSTRAINT glossary_terms_definition_source_check
        CHECK (
            definition_source IS NULL
            OR definition_source IN ('lecture', 'textbook')
        );

-- Existing non-empty definitions came from lecture fill / extract.
UPDATE psy.glossary_terms
SET definition_source = 'lecture'
WHERE definition_source IS NULL
  AND definition IS NOT NULL
  AND length(btrim(definition)) > 0;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
