-- psy-memory-trainer / фаза 1
-- Схема продукта: psy (общая Supabase-база, чужой public не трогаем).
-- Эмбеддинги / vector(N) в эту миграцию не входят — отдельный 0002 после выбора модели Ollama.
--
-- Требуются уже установленные на инстансе расширения (аудит 2026-11-10):
--   pgcrypto 1.3, uuid-ossp 1.1, vector 0.8.0 — все в schema extensions.
-- CREATE EXTENSION здесь намеренно нет.
--
-- Роль n8n:
--   Существующий credential «Postgres account» → user pharmacy_user @ pharmacy_ai
--   (контейнер pharmacypostgres-zdl18c) — НЕ эта база. Для продукта нужен отдельный
--   credential на Supabase. Целевая роль приложения: psy_n8n (LOGIN, пароль вне git).
--   GRANT ниже срабатывает только если роль уже создана.
--
-- Прогон на проде — только после отдельного подтверждения владельца.

BEGIN;

CREATE SCHEMA IF NOT EXISTS psy;

CREATE TABLE psy.users (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    telegram_user_id bigint NOT NULL,
    display_name text,
    timezone text NOT NULL DEFAULT 'Europe/Moscow',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_telegram_user_id_key UNIQUE (telegram_user_id)
);

CREATE TABLE psy.courses (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES psy.users (id),
    title text NOT NULL,
    drive_folder_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE psy.lectures (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    course_id uuid NOT NULL REFERENCES psy.courses (id),
    external_file_id text NOT NULL,
    title text,
    media_kind text NOT NULL,
    duration_sec integer,
    status text NOT NULL DEFAULT 'queued',
    lecturer_name text,
    source_meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT lectures_course_external_file_key UNIQUE (course_id, external_file_id),
    CONSTRAINT lectures_media_kind_check CHECK (media_kind IN ('video', 'audio')),
    CONSTRAINT lectures_status_check CHECK (
        status IN (
            'queued',
            'audio_ready',
            'transcribed',
            'konspekt_ready',
            'questions_ready',
            'cards_ready',
            'failed'
        )
    )
);

CREATE TABLE psy.lecture_texts (
    lecture_id uuid PRIMARY KEY REFERENCES psy.lectures (id) ON DELETE CASCADE,
    transcript text,
    konspekt_md text,
    whisper_model text,
    llm_model text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE psy.chunks (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    lecture_id uuid NOT NULL REFERENCES psy.lectures (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    start_ms integer,
    end_ms integer,
    text text NOT NULL,
    CONSTRAINT chunks_lecture_ordinal_key UNIQUE (lecture_id, ordinal)
);

CREATE TABLE psy.glossary_terms (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    course_id uuid NOT NULL REFERENCES psy.courses (id),
    lecture_id uuid REFERENCES psy.lectures (id) ON DELETE SET NULL,
    canonical text NOT NULL,
    aliases text[] NOT NULL DEFAULT '{}'::text[],
    definition text,
    CONSTRAINT glossary_terms_course_canonical_key UNIQUE (course_id, canonical)
);

CREATE TABLE psy.subtopics (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    lecture_id uuid NOT NULL REFERENCES psy.lectures (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    title text NOT NULL,
    CONSTRAINT subtopics_lecture_ordinal_key UNIQUE (lecture_id, ordinal)
);

CREATE TABLE psy.questions (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    lecture_id uuid NOT NULL REFERENCES psy.lectures (id) ON DELETE CASCADE,
    subtopic_id uuid NOT NULL REFERENCES psy.subtopics (id) ON DELETE CASCADE,
    term_id uuid NOT NULL REFERENCES psy.glossary_terms (id),
    parent_question_id uuid REFERENCES psy.questions (id) ON DELETE SET NULL,
    stem text NOT NULL,
    choice_a text NOT NULL,
    choice_b text NOT NULL,
    choice_c text NOT NULL,
    choice_d text NOT NULL,
    correct_choice char(1) NOT NULL,
    explanation text,
    choice_explanations jsonb,
    kind text NOT NULL DEFAULT 'life_mcq',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT questions_correct_choice_check CHECK (correct_choice IN ('A', 'B', 'C', 'D')),
    CONSTRAINT questions_kind_check CHECK (kind IN ('life_mcq', 'followup', 'consolidation'))
);

CREATE TABLE psy.cards (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES psy.users (id),
    question_id uuid NOT NULL REFERENCES psy.questions (id) ON DELETE CASCADE,
    term_id uuid NOT NULL REFERENCES psy.glossary_terms (id),
    state smallint NOT NULL DEFAULT 0,
    stability double precision,
    difficulty double precision,
    due timestamptz NOT NULL DEFAULT now(),
    last_review_at timestamptz,
    reps integer NOT NULL DEFAULT 0,
    lapses integer NOT NULL DEFAULT 0,
    elapsed_days double precision,
    scheduled_days double precision,
    CONSTRAINT cards_user_question_key UNIQUE (user_id, question_id),
    CONSTRAINT cards_state_check CHECK (state IN (0, 1, 2, 3))
);

CREATE TABLE psy.reviews (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    card_id uuid NOT NULL REFERENCES psy.cards (id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES psy.users (id),
    rating smallint NOT NULL,
    correct boolean NOT NULL,
    chosen_choice char(1),
    channel text NOT NULL DEFAULT 'telegram',
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT reviews_rating_check CHECK (rating BETWEEN 1 AND 4),
    CONSTRAINT reviews_chosen_choice_check CHECK (
        chosen_choice IS NULL OR chosen_choice IN ('A', 'B', 'C', 'D')
    ),
    CONSTRAINT reviews_channel_check CHECK (channel IN ('telegram', 'car_audio'))
);

CREATE TABLE psy.settings (
    user_id uuid PRIMARY KEY REFERENCES psy.users (id) ON DELETE CASCADE,
    morning_hour smallint NOT NULL DEFAULT 8,
    evening_hour smallint NOT NULL DEFAULT 21,
    desired_retention double precision NOT NULL DEFAULT 0.9,
    fsrs_weights jsonb,
    whisper_model text NOT NULL DEFAULT 'large-v3-turbo',
    llm_model text,
    CONSTRAINT settings_morning_hour_check CHECK (morning_hour BETWEEN 0 AND 23),
    CONSTRAINT settings_evening_hour_check CHECK (evening_hour BETWEEN 0 AND 23)
);

CREATE TABLE psy.llm_usage (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    lecture_id uuid REFERENCES psy.lectures (id) ON DELETE SET NULL,
    workflow_name text NOT NULL,
    model text NOT NULL,
    prompt_tokens integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    cost_usd numeric(10, 5),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT llm_usage_tokens_check CHECK (
        prompt_tokens >= 0 AND completion_tokens >= 0
    ),
    CONSTRAINT llm_usage_cost_usd_check CHECK (
        cost_usd IS NULL OR cost_usd >= 0
    )
);

CREATE TABLE psy.jobs (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    lecture_id uuid NOT NULL REFERENCES psy.lectures (id) ON DELETE CASCADE,
    step text NOT NULL,
    status text NOT NULL,
    idempotency_key text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_idempotency_key_key UNIQUE (idempotency_key),
    CONSTRAINT jobs_status_check CHECK (
        status IN ('queued', 'running', 'done', 'failed')
    ),
    CONSTRAINT jobs_attempts_check CHECK (attempts >= 0)
);

CREATE INDEX cards_user_id_due_idx ON psy.cards (user_id, due);
CREATE INDEX cards_term_id_idx ON psy.cards (term_id);
CREATE INDEX questions_term_id_idx ON psy.questions (term_id);
CREATE INDEX questions_subtopic_id_idx ON psy.questions (subtopic_id);
CREATE INDEX lectures_status_idx ON psy.lectures (status);
CREATE INDEX glossary_terms_course_id_idx ON psy.glossary_terms (course_id);
CREATE INDEX reviews_card_id_reviewed_at_idx ON psy.reviews (card_id, reviewed_at);
CREATE INDEX jobs_status_step_idx ON psy.jobs (status, step);
CREATE INDEX llm_usage_created_at_idx ON psy.llm_usage (created_at);

-- updated_at: триггер, чтобы n8n не забывал now() на каждом UPDATE.
CREATE OR REPLACE FUNCTION psy.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER lectures_set_updated_at
    BEFORE UPDATE ON psy.lectures
    FOR EACH ROW
    EXECUTE FUNCTION psy.set_updated_at();

CREATE TRIGGER lecture_texts_set_updated_at
    BEFORE UPDATE ON psy.lecture_texts
    FOR EACH ROW
    EXECUTE FUNCTION psy.set_updated_at();

-- Права для будущей роли n8n на этой базе (не pharmacy_user).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT USAGE ON SCHEMA psy TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA psy TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.set_updated_at() TO psy_n8n;
        ALTER DEFAULT PRIVILEGES IN SCHEMA psy
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO psy_n8n;
        ALTER DEFAULT PRIVILEGES IN SCHEMA psy
            GRANT USAGE, SELECT ON SEQUENCES TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
