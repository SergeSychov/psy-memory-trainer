-- psy-memory-trainer / Telegram quiz sessions + review timing
-- quiz_sessions: one open session per user; reviews get response_ms + session_id for FSRS later.
-- Прогон на проде — только после подтверждения владельца (эта задача = «да» на 0006).

BEGIN;

CREATE TABLE IF NOT EXISTS psy.quiz_sessions (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES psy.users (id),
    lecture_id uuid REFERENCES psy.lectures (id),
    status text NOT NULL,
    question_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
    cursor integer NOT NULL DEFAULT 0,
    current_question_id uuid REFERENCES psy.questions (id),
    asked_at timestamptz,
    correct_count integer NOT NULL DEFAULT 0,
    answered_count integer NOT NULL DEFAULT 0,
    telegram_chat_id bigint NOT NULL,
    last_callback_id text,
    miss_subtopics text[] NOT NULL DEFAULT '{}'::text[],
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT quiz_sessions_status_check CHECK (
        status IN (
            'picking_course',
            'picking_lecture',
            'active',
            'awaiting_next',
            'completed',
            'stopped'
        )
    ),
    CONSTRAINT quiz_sessions_cursor_check CHECK (cursor >= 0),
    CONSTRAINT quiz_sessions_counts_check CHECK (
        correct_count >= 0
        AND answered_count >= 0
        AND correct_count <= answered_count
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS quiz_sessions_one_open_per_user
    ON psy.quiz_sessions (user_id)
    WHERE status IN (
        'picking_course',
        'picking_lecture',
        'active',
        'awaiting_next'
    );

CREATE INDEX IF NOT EXISTS quiz_sessions_user_id_created_at_idx
    ON psy.quiz_sessions (user_id, created_at DESC);

COMMENT ON TABLE psy.quiz_sessions IS
    'Telegram MCQ quiz state. At most one open session per user (partial unique index).';

DROP TRIGGER IF EXISTS quiz_sessions_set_updated_at ON psy.quiz_sessions;
CREATE TRIGGER quiz_sessions_set_updated_at
    BEFORE UPDATE ON psy.quiz_sessions
    FOR EACH ROW
    EXECUTE FUNCTION psy.set_updated_at();

ALTER TABLE psy.reviews
    ADD COLUMN IF NOT EXISTS response_ms integer;

ALTER TABLE psy.reviews
    ADD COLUMN IF NOT EXISTS session_id uuid REFERENCES psy.quiz_sessions (id);

COMMENT ON COLUMN psy.reviews.response_ms IS
    'Milliseconds from question shown (quiz_sessions.asked_at) to answer.';
COMMENT ON COLUMN psy.reviews.session_id IS
    'Quiz session that produced this review; NULL for non-quiz channels later.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA psy TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
