-- psy-memory-trainer / шаг 2: загрузка источника лекции по ссылке + учёт квоты
-- Каналонезависимое ядро: идентичности пользователей, заявки на лекции, журнал квоты,
-- диалоговые сессии, outbox уведомлений и SQL-функции постановки/завершения.
--
-- Применять один раз: psql -v ON_ERROR_STOP=1 -f db/migrations/0008_lecture_sources_quota.sql
-- Только после явного «ок» владельца (прогон на проде — отдельное подтверждение).
--
-- Правила файла:
--   * одна транзакция; аддитивно (IF NOT EXISTS / OR REPLACE / ON CONFLICT), данные не удаляются;
--   * только встроенные функции PG15 (gen_random_uuid, sha256, encode, convert_to);
--     роль psy_n8n не имеет USAGE на служебной схеме расширений, поэтому ссылок на неё здесь нет;
--   * никаких имён, названий курсов, id, ссылок и текстов лекций в файле нет;
--   * квота списывается только по успеху (см. psy.finalize_pending_requests);
--   * существующие psy_01..psy_08 не меняются: users.telegram_user_id остаётся (psy_08 на нём работает).

BEGIN;

-- ---------------------------------------------------------------------------
-- 5.1 users и идентичности каналов
-- ---------------------------------------------------------------------------

-- У пользователей других каналов (MAX/веб/приложение) Telegram id нет; UNIQUE остаётся.
ALTER TABLE psy.users
    ALTER COLUMN telegram_user_id DROP NOT NULL;

CREATE TABLE IF NOT EXISTS psy.user_identities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES psy.users (id) ON DELETE CASCADE,
    -- без жёсткого списка: новый канал не требует миграции
    channel text NOT NULL,
    external_id text NOT NULL,
    -- куда слать уведомления; для личного чата Telegram = external_id
    external_chat_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    CONSTRAINT user_identities_channel_check CHECK (channel ~ '^[a-z][a-z0-9_]{1,15}$'),
    CONSTRAINT user_identities_channel_external_key UNIQUE (channel, external_id)
);

CREATE INDEX IF NOT EXISTS user_identities_user_id_idx
    ON psy.user_identities (user_id);

COMMENT ON TABLE psy.user_identities IS
    'Соответствие (channel, external_id) -> users.id. Новый канал = новые строки без миграции.';

-- Backfill: уже существующие Telegram-пользователи.
INSERT INTO psy.user_identities (user_id, channel, external_id, external_chat_id)
SELECT u.id, 'telegram', u.telegram_user_id::text, u.telegram_user_id::text
FROM psy.users u
WHERE u.telegram_user_id IS NOT NULL
ON CONFLICT ON CONSTRAINT user_identities_channel_external_key DO NOTHING;

-- Триггер: psy_08 создаёт пользователей по-старому (ON CONFLICT (telegram_user_id)),
-- identity telegram появляется сама.
CREATE OR REPLACE FUNCTION psy.users_sync_telegram_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF NEW.telegram_user_id IS NOT NULL THEN
        INSERT INTO psy.user_identities (user_id, channel, external_id, external_chat_id)
        VALUES (NEW.id, 'telegram', NEW.telegram_user_id::text, NEW.telegram_user_id::text)
        ON CONFLICT ON CONSTRAINT user_identities_channel_external_key DO NOTHING;
    END IF;
    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS users_sync_telegram_identity ON psy.users;
CREATE TRIGGER users_sync_telegram_identity
    AFTER INSERT OR UPDATE OF telegram_user_id ON psy.users
    FOR EACH ROW
    EXECUTE FUNCTION psy.users_sync_telegram_identity();

-- resolve_identity: находит/создаёт users (+ workspaces с квотой по умолчанию) + identity.
-- Идемпотентна. Для telegram заполняет users.telegram_user_id.
CREATE OR REPLACE FUNCTION psy.resolve_identity(
    p_channel text,
    p_external_id text,
    p_chat_id text DEFAULT NULL,
    p_display_name text DEFAULT NULL
)
RETURNS TABLE (user_id uuid, workspace_id uuid, is_new boolean)
LANGUAGE plpgsql
AS $fn$
#variable_conflict use_column
DECLARE
    v_user uuid;
    v_ws uuid;
    v_new boolean := false;
    v_tg bigint;
    v_chat text;
BEGIN
    IF p_channel IS NULL OR p_channel !~ '^[a-z][a-z0-9_]{1,15}$' THEN
        RAISE EXCEPTION 'resolve_identity: bad channel' USING ERRCODE = '22023';
    END IF;
    IF p_external_id IS NULL OR btrim(p_external_id) = '' OR length(p_external_id) > 128 THEN
        RAISE EXCEPTION 'resolve_identity: bad external_id' USING ERRCODE = '22023';
    END IF;

    v_chat := NULLIF(btrim(COALESCE(p_chat_id, '')), '');
    IF v_chat IS NULL AND p_channel = 'telegram' THEN
        v_chat := p_external_id;
    END IF;

    -- сериализуем одновременные первые обращения одного и того же внешнего пользователя
    PERFORM pg_advisory_xact_lock(hashtext('psy.identity:' || p_channel || ':' || p_external_id));

    SELECT ui.user_id INTO v_user
    FROM psy.user_identities ui
    WHERE ui.channel = p_channel AND ui.external_id = p_external_id;

    IF v_user IS NULL THEN
        IF p_channel = 'telegram' THEN
            IF p_external_id !~ '^[0-9]{1,18}$' THEN
                RAISE EXCEPTION 'resolve_identity: bad telegram id' USING ERRCODE = '22023';
            END IF;
            v_tg := p_external_id::bigint;
            INSERT INTO psy.users (telegram_user_id, display_name)
            VALUES (v_tg, NULLIF(btrim(COALESCE(p_display_name, '')), ''))
            ON CONFLICT ON CONSTRAINT users_telegram_user_id_key DO NOTHING
            RETURNING id INTO v_user;
            IF v_user IS NULL THEN
                SELECT u.id INTO v_user FROM psy.users u WHERE u.telegram_user_id = v_tg;
            ELSE
                v_new := true;
            END IF;
        ELSE
            INSERT INTO psy.users (display_name)
            VALUES (NULLIF(btrim(COALESCE(p_display_name, '')), ''))
            RETURNING id INTO v_user;
            v_new := true;
        END IF;

        INSERT INTO psy.user_identities (user_id, channel, external_id, external_chat_id, last_seen_at)
        VALUES (v_user, p_channel, p_external_id, v_chat, now())
        ON CONFLICT ON CONSTRAINT user_identities_channel_external_key DO UPDATE
        SET external_chat_id = COALESCE(EXCLUDED.external_chat_id, psy.user_identities.external_chat_id),
            last_seen_at = now();
    ELSE
        UPDATE psy.user_identities ui
        SET last_seen_at = now(),
            external_chat_id = COALESCE(v_chat, ui.external_chat_id)
        WHERE ui.channel = p_channel AND ui.external_id = p_external_id;
    END IF;

    UPDATE psy.users u
    SET display_name = NULLIF(btrim(COALESCE(p_display_name, '')), '')
    WHERE u.id = v_user
      AND u.display_name IS NULL
      AND NULLIF(btrim(COALESCE(p_display_name, '')), '') IS NOT NULL;

    INSERT INTO psy.workspaces (owner_user_id)
    VALUES (v_user)
    ON CONFLICT ON CONSTRAINT workspaces_owner_user_id_key DO NOTHING;

    SELECT w.id INTO v_ws FROM psy.workspaces w WHERE w.owner_user_id = v_user;

    user_id := v_user;
    workspace_id := v_ws;
    is_new := v_new;
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION psy.resolve_identity(text, text, text, text) IS
    'Идемпотентно: (channel, external_id) -> user_id + workspace_id; создаёт users/workspaces/identity при первом обращении.';

-- ---------------------------------------------------------------------------
-- 5.2 lectures: источник «текст» идёт мимо скачивания/ASR
-- ---------------------------------------------------------------------------

ALTER TABLE psy.lectures
    DROP CONSTRAINT IF EXISTS lectures_media_kind_check;
ALTER TABLE psy.lectures
    ADD CONSTRAINT lectures_media_kind_check CHECK (media_kind IN ('video', 'audio', 'text'));

-- ---------------------------------------------------------------------------
-- 5.3 lecture_requests: заявки пользователей на лекции
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS psy.lecture_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES psy.users (id),
    workspace_id uuid NOT NULL REFERENCES psy.workspaces (id),
    -- NULL, пока курс только создаётся (new_course_title)
    course_id uuid REFERENCES psy.courses (id),
    new_course_title text,
    -- NULL до постановки в очередь
    lecture_id uuid REFERENCES psy.lectures (id) ON DELETE SET NULL,
    channel text NOT NULL,
    source_kind text NOT NULL,
    -- url/ref, name, size_bytes, duration_sec, mime, inbox_path, text.
    -- URL хранится только здесь: в логи, ошибки, payload jobs и outbox не попадает.
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- sha256 канонической ссылки/файла/текста (дедупликация)
    source_hash text,
    status text NOT NULL DEFAULT 'confirming',
    error_code text,
    -- без URL
    error_detail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    notified_at timestamptz,
    -- dispatcher проставляет после /files/delete для inbox_path отменённых/просроченных заявок
    inbox_cleaned_at timestamptz,
    CONSTRAINT lecture_requests_channel_check CHECK (channel ~ '^[a-z][a-z0-9_]{1,15}$'),
    CONSTRAINT lecture_requests_source_kind_check CHECK (
        source_kind IN ('yandex_public', 'gdrive_public', 'direct_url', 'chat_file', 'text')
    ),
    CONSTRAINT lecture_requests_status_check CHECK (
        status IN ('confirming', 'queued', 'processing', 'done', 'failed', 'cancelled', 'expired')
    ),
    CONSTRAINT lecture_requests_source_object_check CHECK (jsonb_typeof(source) = 'object'),
    CONSTRAINT lecture_requests_course_or_title_check CHECK (
        course_id IS NOT NULL OR new_course_title IS NOT NULL
    )
);

-- одна лекция в обработке на пользователя
CREATE UNIQUE INDEX IF NOT EXISTS lecture_requests_one_active_per_user
    ON psy.lecture_requests (user_id)
    WHERE status IN ('queued', 'processing');

-- одна заявка «в подтверждении» на пользователя
CREATE UNIQUE INDEX IF NOT EXISTS lecture_requests_one_confirming_per_user
    ON psy.lecture_requests (user_id)
    WHERE status = 'confirming';

-- дедупликация одинаковых источников в рамках workspace
CREATE UNIQUE INDEX IF NOT EXISTS lecture_requests_dedupe_source
    ON psy.lecture_requests (workspace_id, source_hash)
    WHERE status IN ('queued', 'processing', 'done');

CREATE INDEX IF NOT EXISTS lecture_requests_status_created_idx
    ON psy.lecture_requests (status, created_at);

CREATE INDEX IF NOT EXISTS lecture_requests_user_id_idx
    ON psy.lecture_requests (user_id, created_at DESC);

COMMENT ON TABLE psy.lecture_requests IS
    'Заявки на добавление лекции (канал-независимые). Источник правды об очереди пользователя; квота списывается через quota_ledger по успеху.';
COMMENT ON COLUMN psy.lecture_requests.source IS
    'Ссылка/файл/текст источника. Содержит URL: не копировать в логи, error_detail, jobs.payload и outbox.';
COMMENT ON COLUMN psy.lecture_requests.error_code IS
    'not_public | unsupported_link | too_large | too_long | download_failed | no_speech | budget | timeout | pipeline_failed | internal';

-- ---------------------------------------------------------------------------
-- 5.4 quota_ledger: журнал квоты (источник правды о расходе)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS psy.quota_ledger (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES psy.workspaces (id),
    -- NULL допустим только для admin_adjust
    request_id uuid REFERENCES psy.lecture_requests (id),
    -- +1 = списание, -1 = возврат
    delta integer NOT NULL,
    reason text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- повторный вызов не спишет дважды (идемпотентность)
    CONSTRAINT quota_ledger_request_reason_key UNIQUE (request_id, reason),
    CONSTRAINT quota_ledger_reason_check CHECK (reason IN ('lecture_done', 'refund', 'admin_adjust')),
    CONSTRAINT quota_ledger_delta_check CHECK (
        (reason = 'lecture_done' AND delta = 1)
        OR (reason = 'refund' AND delta = -1)
        OR (reason = 'admin_adjust' AND delta <> 0)
    ),
    CONSTRAINT quota_ledger_request_required_check CHECK (
        request_id IS NOT NULL OR reason = 'admin_adjust'
    )
);

CREATE INDEX IF NOT EXISTS quota_ledger_workspace_id_idx
    ON psy.quota_ledger (workspace_id, created_at);

COMMENT ON TABLE psy.quota_ledger IS
    'Журнал расхода квоты лекций: +1 списание (lecture_done), -1 возврат (refund), admin_adjust. Остаток = workspaces.lecture_quota - SUM(delta).';

-- NULL = безлимит (lecture_quota IS NULL); неизвестный workspace = 0 (не безлимит).
CREATE OR REPLACE FUNCTION psy.workspace_quota_remaining(p_workspace_id uuid)
RETURNS integer
LANGUAGE plpgsql
STABLE
AS $fn$
DECLARE
    v_quota integer;
    v_spent bigint;
BEGIN
    SELECT w.lecture_quota INTO v_quota
    FROM psy.workspaces w
    WHERE w.id = p_workspace_id;

    IF NOT FOUND THEN
        RETURN 0;
    END IF;
    IF v_quota IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(q.delta), 0) INTO v_spent
    FROM psy.quota_ledger q
    WHERE q.workspace_id = p_workspace_id;

    RETURN v_quota - v_spent::integer;
END;
$fn$;

COMMENT ON FUNCTION psy.workspace_quota_remaining(uuid) IS
    'Остаток квоты лекций: NULL = безлимит, иначе lecture_quota - SUM(quota_ledger.delta).';

-- ---------------------------------------------------------------------------
-- 5.5 intake_sessions: состояние диалога /new (канал-независимое)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS psy.intake_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES psy.users (id) ON DELETE CASCADE,
    step text NOT NULL,
    -- список курсов, выбранный курс, request_id
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- идемпотентность вебхуков
    last_event_id text,
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '30 minutes'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT intake_sessions_user_id_key UNIQUE (user_id),
    CONSTRAINT intake_sessions_step_check CHECK (
        step IN ('pick_course', 'new_course_title', 'await_source', 'confirm')
    )
);

COMMENT ON TABLE psy.intake_sessions IS
    'Диалог добавления лекции: одна сессия на пользователя, новая /new перезаписывает.';

DROP TRIGGER IF EXISTS intake_sessions_set_updated_at ON psy.intake_sessions;
CREATE TRIGGER intake_sessions_set_updated_at
    BEFORE UPDATE ON psy.intake_sessions
    FOR EACH ROW
    EXECUTE FUNCTION psy.set_updated_at();

-- ---------------------------------------------------------------------------
-- 5.6 outbox: исходящие уведомления
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS psy.outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES psy.users (id) ON DELETE CASCADE,
    kind text NOT NULL,
    -- структурные данные, не готовый текст: текст рендерит lib под канал
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    CONSTRAINT outbox_dedupe_key_key UNIQUE (dedupe_key),
    CONSTRAINT outbox_kind_check CHECK (
        kind IN ('request_done', 'request_failed', 'request_expired')
    ),
    CONSTRAINT outbox_status_check CHECK (status IN ('queued', 'sent', 'failed')),
    CONSTRAINT outbox_attempts_check CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS outbox_queued_created_idx
    ON psy.outbox (created_at)
    WHERE status = 'queued';

COMMENT ON TABLE psy.outbox IS
    'Очередь исходящих уведомлений пользователю; dedupe_key вида request_done:<request_id> защищает от дублей.';

-- ---------------------------------------------------------------------------
-- 5.7 SQL-функции
-- ---------------------------------------------------------------------------

-- Номер в очереди: сколько заявок перед этой («перед вами N»). NULL, если заявка не в очереди.
CREATE OR REPLACE FUNCTION psy.queue_position(p_request_id uuid)
RETURNS integer
LANGUAGE sql
STABLE
AS $fn$
    SELECT CASE
        WHEN me.status IN ('queued', 'processing') THEN (
            SELECT count(*)::integer
            FROM psy.lecture_requests o
            WHERE o.status IN ('queued', 'processing')
              AND o.id <> me.id
              AND (o.confirmed_at, o.id) < (me.confirmed_at, me.id)
        )
        ELSE NULL
    END
    FROM psy.lecture_requests me
    WHERE me.id = p_request_id;
$fn$;

COMMENT ON FUNCTION psy.queue_position(uuid) IS
    'Сколько заявок queued/processing стоит перед данной (0 = первая); NULL, если заявка не в очереди.';

-- Код ошибки по тексту ошибки job/лекции (psy_04/05 не меняют статус лекции, а job.error содержит причину).
CREATE OR REPLACE FUNCTION psy.lecture_request_error_code(p_error text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $fn$
    SELECT CASE
        WHEN position('budget_exceeded' IN lower(COALESCE(p_error, ''))) > 0 THEN 'budget'
        WHEN position('empty_transcript' IN lower(COALESCE(p_error, ''))) > 0
          OR position('no_speech' IN lower(COALESCE(p_error, ''))) > 0 THEN 'no_speech'
        WHEN position('too_long' IN lower(COALESCE(p_error, ''))) > 0 THEN 'too_long'
        WHEN position('too_large' IN lower(COALESCE(p_error, ''))) > 0 THEN 'too_large'
        WHEN position('not_public' IN lower(COALESCE(p_error, ''))) > 0 THEN 'not_public'
        WHEN position('download_failed' IN lower(COALESCE(p_error, ''))) > 0 THEN 'download_failed'
        ELSE 'pipeline_failed'
    END;
$fn$;

-- create_lecture_request: заявка в статусе confirming.
-- Проверки: workspace пользователя, курс принадлежит этому workspace (или задано название нового),
-- источник не дубликат. Прежняя confirming-заявка пользователя закрывается как cancelled.
-- Если p_source_hash не передан, считается sha256 от canonical/url/text из p_source.
CREATE OR REPLACE FUNCTION psy.create_lecture_request(
    p_user_id uuid,
    p_channel text,
    p_course_id uuid,
    p_new_course_title text,
    p_source_kind text,
    p_source jsonb,
    p_source_hash text DEFAULT NULL
)
RETURNS TABLE (ok boolean, reason text, request_id uuid)
LANGUAGE plpgsql
AS $fn$
#variable_conflict use_column
DECLARE
    v_ws uuid;
    v_title text;
    v_hash text;
    v_basis text;
    v_id uuid;
BEGIN
    IF p_channel IS NULL OR p_channel !~ '^[a-z][a-z0-9_]{1,15}$'
       OR p_source_kind IS NULL
       OR p_source_kind NOT IN ('yandex_public', 'gdrive_public', 'direct_url', 'chat_file', 'text')
       OR p_source IS NULL OR jsonb_typeof(p_source) <> 'object' THEN
        ok := false; reason := 'bad_input'; request_id := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    SELECT w.id INTO v_ws FROM psy.workspaces w WHERE w.owner_user_id = p_user_id;
    IF v_ws IS NULL THEN
        ok := false; reason := 'no_workspace'; request_id := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    -- сериализуем заявки одного пользователя
    PERFORM 1 FROM psy.workspaces w WHERE w.id = v_ws FOR UPDATE;

    v_title := NULL;
    IF p_course_id IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM psy.courses c WHERE c.id = p_course_id AND c.workspace_id = v_ws
        ) THEN
            ok := false; reason := 'bad_course'; request_id := NULL;
            RETURN NEXT;
            RETURN;
        END IF;
    ELSE
        v_title := NULLIF(btrim(COALESCE(p_new_course_title, '')), '');
        IF v_title IS NULL OR char_length(v_title) > 200 THEN
            ok := false; reason := 'bad_course'; request_id := NULL;
            RETURN NEXT;
            RETURN;
        END IF;
    END IF;

    v_hash := NULLIF(btrim(COALESCE(p_source_hash, '')), '');
    IF v_hash IS NULL THEN
        v_basis := CASE p_source_kind
            WHEN 'text' THEN p_source ->> 'text'
            ELSE COALESCE(p_source ->> 'canonical', p_source ->> 'url')
        END;
        IF v_basis IS NOT NULL AND v_basis <> '' THEN
            v_hash := encode(sha256(convert_to(v_basis, 'UTF8')), 'hex');
        END IF;
    END IF;

    IF v_hash IS NOT NULL AND EXISTS (
        SELECT 1
        FROM psy.lecture_requests x
        WHERE x.workspace_id = v_ws
          AND x.source_hash = v_hash
          AND x.status IN ('queued', 'processing', 'done')
    ) THEN
        ok := false; reason := 'duplicate_source'; request_id := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    UPDATE psy.lecture_requests x
    SET status = 'cancelled', finished_at = now()
    WHERE x.user_id = p_user_id AND x.status = 'confirming';

    INSERT INTO psy.lecture_requests (
        user_id, workspace_id, course_id, new_course_title, channel,
        source_kind, source, source_hash, status
    )
    VALUES (
        p_user_id, v_ws, p_course_id, v_title, p_channel,
        p_source_kind, p_source, v_hash, 'confirming'
    )
    RETURNING id INTO v_id;

    ok := true; reason := NULL; request_id := v_id;
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION psy.create_lecture_request(uuid, text, uuid, text, text, jsonb, text) IS
    'Создаёт заявку confirming; reason при отказе: bad_input | no_workspace | bad_course | duplicate_source.';

-- confirm_lecture_request: постановка в очередь. Квоту НЕ списывает.
CREATE OR REPLACE FUNCTION psy.confirm_lecture_request(
    p_request_id uuid,
    p_user_id uuid,
    p_monthly_budget_usd numeric DEFAULT 5,
    p_stop_ratio numeric DEFAULT 0.8,
    p_daily_cap integer DEFAULT 10
)
RETURNS TABLE (ok boolean, reason text, lecture_id uuid, queue_position integer)
LANGUAGE plpgsql
AS $fn$
#variable_conflict use_column
DECLARE
    r psy.lecture_requests%ROWTYPE;
    v_remaining integer;
    v_month_start timestamptz;
    v_day_start timestamptz;
    v_spent numeric;
    v_course uuid;
    v_lecture uuid;
    v_kind text;
    v_dur integer;
    v_size bigint;
    v_max_bytes bigint;
    v_status text;
    v_text text;
    v_cons text;
BEGIN
    SELECT * INTO r FROM psy.lecture_requests x WHERE x.id = p_request_id;
    IF NOT FOUND THEN
        ok := false; reason := 'bad_state'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;
    IF r.user_id <> p_user_id THEN
        ok := false; reason := 'not_owner'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    -- сериализует подтверждения одного пользователя (один workspace на пользователя)
    PERFORM 1 FROM psy.workspaces w WHERE w.id = r.workspace_id FOR UPDATE;

    SELECT * INTO r FROM psy.lecture_requests x WHERE x.id = p_request_id FOR UPDATE;
    IF r.status <> 'confirming' THEN
        ok := false; reason := 'bad_state'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    v_remaining := psy.workspace_quota_remaining(r.workspace_id);
    IF v_remaining IS NOT NULL AND v_remaining <= 0 THEN
        ok := false; reason := 'no_quota'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1 FROM psy.lecture_requests x
        WHERE x.user_id = r.user_id AND x.status IN ('queued', 'processing')
    ) THEN
        ok := false; reason := 'busy_user'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    IF r.source_hash IS NOT NULL AND EXISTS (
        SELECT 1 FROM psy.lecture_requests x
        WHERE x.workspace_id = r.workspace_id
          AND x.source_hash = r.source_hash
          AND x.id <> r.id
          AND x.status IN ('queued', 'processing', 'done')
    ) THEN
        ok := false; reason := 'duplicate_source'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    -- месяц/сутки считаем по Москве (timezone по умолчанию в users)
    IF p_monthly_budget_usd IS NOT NULL THEN
        v_month_start := date_trunc('month', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow';
        SELECT COALESCE(SUM(lu.cost_usd), 0) INTO v_spent
        FROM psy.llm_usage lu
        WHERE lu.created_at >= v_month_start;
        IF v_spent >= p_monthly_budget_usd * COALESCE(p_stop_ratio, 0.8) THEN
            ok := false; reason := 'budget_stop'; lecture_id := NULL; queue_position := NULL;
            RETURN NEXT;
            RETURN;
        END IF;
    END IF;

    IF p_daily_cap IS NOT NULL THEN
        v_day_start := date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow';
        IF (
            SELECT count(*) FROM psy.lecture_requests x WHERE x.confirmed_at >= v_day_start
        ) >= p_daily_cap THEN
            ok := false; reason := 'daily_cap'; lecture_id := NULL; queue_position := NULL;
            RETURN NEXT;
            RETURN;
        END IF;
    END IF;

    IF (
        SELECT count(*) FROM psy.lecture_requests x
        WHERE x.user_id = r.user_id
          AND x.status = 'failed'
          AND x.finished_at >= now() - interval '24 hours'
    ) >= 5 THEN
        ok := false; reason := 'too_many_failures'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    -- минимальная проверка источника (полная валидация — в lib/probe)
    IF r.source_kind = 'text' THEN
        v_text := r.source ->> 'text';
        IF v_text IS NULL OR char_length(v_text) < 2000 OR char_length(v_text) > 250000 THEN
            ok := false; reason := 'bad_source'; lecture_id := NULL; queue_position := NULL;
            RETURN NEXT;
            RETURN;
        END IF;
    ELSIF r.source_kind = 'chat_file' THEN
        IF COALESCE(r.source ->> 'inbox_path', '') = '' THEN
            ok := false; reason := 'bad_source'; lecture_id := NULL; queue_position := NULL;
            RETURN NEXT;
            RETURN;
        END IF;
    ELSIF COALESCE(r.source ->> 'url', '') = '' THEN
        ok := false; reason := 'bad_source'; lecture_id := NULL; queue_position := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    BEGIN
        -- курс
        IF r.course_id IS NULL THEN
            INSERT INTO psy.courses (user_id, workspace_id, title, drive_folder_id)
            VALUES (r.user_id, r.workspace_id, r.new_course_title, NULL)
            RETURNING id INTO v_course;
        ELSE
            SELECT c.id INTO v_course
            FROM psy.courses c
            WHERE c.id = r.course_id AND c.workspace_id = r.workspace_id;
            IF v_course IS NULL THEN
                ok := false; reason := 'bad_course'; lecture_id := NULL; queue_position := NULL;
                RETURN NEXT;
                RETURN;
            END IF;
        END IF;

        -- лекция
        v_kind := CASE
            WHEN r.source_kind = 'text' THEN 'text'
            WHEN r.source ->> 'resolved_kind' = 'audio' THEN 'audio'
            ELSE 'video'
        END;
        v_dur := CASE
            WHEN (r.source ->> 'duration_sec') ~ '^[0-9]{1,6}(\.[0-9]+)?$'
                THEN round((r.source ->> 'duration_sec')::numeric)::integer
            ELSE NULL
        END;
        v_size := CASE
            WHEN (r.source ->> 'size_bytes') ~ '^[0-9]{1,15}$'
                THEN (r.source ->> 'size_bytes')::bigint
            ELSE NULL
        END;
        v_status := CASE WHEN r.source_kind = 'text' THEN 'transcribed' ELSE 'queued' END;
        v_max_bytes := CASE WHEN r.source_kind = 'chat_file' THEN 20971520 ELSE 4294967296 END;

        INSERT INTO psy.lectures (
            course_id, external_file_id, title, media_kind, duration_sec, status, source_meta
        )
        VALUES (
            v_course,
            'req:' || r.id::text,
            NULLIF(left(COALESCE(r.source ->> 'name', ''), 200), ''),
            v_kind,
            v_dur,
            v_status,
            jsonb_strip_nulls(jsonb_build_object(
                'request_id', r.id,
                'source_kind', r.source_kind,
                'size_bytes', v_size,
                'duration_sec', v_dur
            ))
        )
        RETURNING id INTO v_lecture;

        IF r.source_kind = 'text' THEN
            -- текст идёт мимо скачивания/ASR: сразу в транскрипт и в конспект
            INSERT INTO psy.lecture_texts (lecture_id, transcript)
            VALUES (v_lecture, v_text);

            INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
            VALUES (
                v_lecture, 'konspekt', 'queued', 'konspekt:' || v_lecture::text,
                jsonb_build_object('source', 'lecture_request', 'request_id', r.id)
            );
        ELSE
            -- ссылка берётся из lecture_requests.source, в payload её нет
            INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key, payload)
            VALUES (
                v_lecture, 'extract_audio', 'queued', 'extract_audio:' || v_lecture::text,
                jsonb_build_object(
                    'source_kind', r.source_kind,
                    'inbox_path', CASE WHEN r.source_kind = 'chat_file' THEN r.source ->> 'inbox_path' ELSE NULL END,
                    'max_bytes', v_max_bytes,
                    'max_duration_sec', 7200,
                    'duration_tolerance_sec', 60,
                    'request_id', r.id
                )
            );
        END IF;

        UPDATE psy.lecture_requests x
        SET status = 'queued',
            confirmed_at = now(),
            course_id = v_course,
            lecture_id = v_lecture,
            -- текст уже скопирован в lecture_texts
            source = x.source - 'text'
        WHERE x.id = r.id;
    EXCEPTION
        WHEN unique_violation THEN
            GET STACKED DIAGNOSTICS v_cons = CONSTRAINT_NAME;
            ok := false;
            reason := CASE v_cons
                WHEN 'lecture_requests_one_active_per_user' THEN 'busy_user'
                WHEN 'lecture_requests_dedupe_source' THEN 'duplicate_source'
                ELSE 'bad_state'
            END;
            lecture_id := NULL;
            queue_position := NULL;
            RETURN NEXT;
            RETURN;
    END;

    ok := true;
    reason := NULL;
    lecture_id := v_lecture;
    queue_position := psy.queue_position(r.id);
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION psy.confirm_lecture_request(uuid, uuid, numeric, numeric, integer) IS
    'Постановка заявки в очередь (квоту не списывает). reason при отказе: bad_state | not_owner | no_quota | busy_user | duplicate_source | budget_stop | daily_cap | too_many_failures | bad_source | bad_course.';

-- cancel_lecture_request: из confirming и из queued (пока ни один job не running).
CREATE OR REPLACE FUNCTION psy.cancel_lecture_request(
    p_request_id uuid,
    p_user_id uuid
)
RETURNS TABLE (ok boolean, reason text)
LANGUAGE plpgsql
AS $fn$
#variable_conflict use_column
DECLARE
    r psy.lecture_requests%ROWTYPE;
BEGIN
    SELECT * INTO r FROM psy.lecture_requests x WHERE x.id = p_request_id FOR UPDATE;
    IF NOT FOUND THEN
        ok := false; reason := 'not_found';
        RETURN NEXT;
        RETURN;
    END IF;
    IF r.user_id <> p_user_id THEN
        ok := false; reason := 'not_owner';
        RETURN NEXT;
        RETURN;
    END IF;

    IF r.status = 'confirming' THEN
        UPDATE psy.lecture_requests x
        SET status = 'cancelled', finished_at = now()
        WHERE x.id = r.id;
        ok := true; reason := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    IF r.status = 'processing' THEN
        ok := false; reason := 'processing';
        RETURN NEXT;
        RETURN;
    END IF;

    IF r.status = 'queued' THEN
        -- ждём возможного claim (psy_02..05 берут job через FOR UPDATE SKIP LOCKED)
        PERFORM 1 FROM psy.jobs j WHERE j.lecture_id = r.lecture_id FOR UPDATE;
        IF EXISTS (
            SELECT 1 FROM psy.jobs j WHERE j.lecture_id = r.lecture_id AND j.status = 'running'
        ) THEN
            ok := false; reason := 'processing';
            RETURN NEXT;
            RETURN;
        END IF;

        -- чтобы конвейер не подобрал job отменённой заявки
        UPDATE psy.jobs j
        SET status = 'failed', attempts = 3, error = 'cancelled', finished_at = now()
        WHERE j.lecture_id = r.lecture_id AND j.status = 'queued';

        UPDATE psy.lectures l
        SET status = 'failed', error = 'cancelled'
        WHERE l.id = r.lecture_id AND l.status <> 'failed';

        UPDATE psy.lecture_requests x
        SET status = 'cancelled', finished_at = now()
        WHERE x.id = r.id;

        ok := true; reason := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    ok := false; reason := 'bad_state';
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION psy.cancel_lecture_request(uuid, uuid) IS
    'Отмена из confirming/queued. reason при отказе: not_found | not_owner | processing | bad_state.';

-- refund_lecture_request: ручной возврат квоты (только владелец продукта; вызывать из служебных путей).
-- Возвращает true, если возврат записан сейчас; false — нет списания или возврат уже был (безопасно повторять).
CREATE OR REPLACE FUNCTION psy.refund_lecture_request(
    p_request_id uuid,
    p_reason text DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_ws uuid;
    v_inserted uuid;
BEGIN
    SELECT x.workspace_id INTO v_ws
    FROM psy.lecture_requests x
    WHERE x.id = p_request_id;
    IF v_ws IS NULL THEN
        RETURN false;
    END IF;

    PERFORM 1 FROM psy.workspaces w WHERE w.id = v_ws FOR UPDATE;

    -- возвращать можно только то, что было списано
    IF NOT EXISTS (
        SELECT 1 FROM psy.quota_ledger q
        WHERE q.request_id = p_request_id AND q.reason = 'lecture_done'
    ) THEN
        RETURN false;
    END IF;

    INSERT INTO psy.quota_ledger (workspace_id, request_id, delta, reason, note)
    VALUES (v_ws, p_request_id, -1, 'refund', NULLIF(left(COALESCE(p_reason, ''), 200), ''))
    ON CONFLICT ON CONSTRAINT quota_ledger_request_reason_key DO NOTHING
    RETURNING id INTO v_inserted;

    RETURN v_inserted IS NOT NULL;
END;
$fn$;

COMMENT ON FUNCTION psy.refund_lecture_request(uuid, text) IS
    'Ручной возврат квоты (-1, reason=refund); идемпотентен благодаря UNIQUE (request_id, reason).';

-- finalize_pending_requests: исход queued/processing заявок по jobs и статусу лекции.
-- outcome: processing | done | failed | expired | cleanup.
--   cleanup — заявки cancelled/expired/failed с inbox_path, файл которых ещё не убран (inbox_cleaned_at IS NULL):
--   вызывающий удаляет файл (/files/delete) и проставляет lecture_requests.inbox_cleaned_at.
-- Квота списывается ТОЛЬКО при успехе (questions_ready/cards_ready и job terms = done), идемпотентно.
CREATE OR REPLACE FUNCTION psy.finalize_pending_requests()
RETURNS TABLE (
    request_id uuid,
    user_id uuid,
    outcome text,
    error_code text,
    inbox_path text
)
LANGUAGE plpgsql
AS $fn$
#variable_conflict use_column
DECLARE
    r record;
    v_code text;
    v_err text;
BEGIN
    FOR r IN
        SELECT
            lr.id,
            lr.user_id,
            lr.workspace_id,
            lr.lecture_id,
            lr.status,
            lr.channel,
            lr.source_kind,
            lr.course_id,
            lr.started_at,
            l.status AS lec_status,
            l.error AS lec_error,
            EXISTS (
                SELECT 1 FROM psy.jobs j WHERE j.lecture_id = lr.lecture_id AND j.status = 'running'
            ) AS has_running,
            EXISTS (
                SELECT 1 FROM psy.jobs j WHERE j.lecture_id = lr.lecture_id AND j.status = 'failed'
            ) AS has_failed,
            EXISTS (
                SELECT 1 FROM psy.jobs j
                WHERE j.lecture_id = lr.lecture_id AND j.step = 'terms' AND j.status = 'done'
            ) AS terms_done,
            (
                SELECT j.error FROM psy.jobs j
                WHERE j.lecture_id = lr.lecture_id AND j.status = 'failed'
                ORDER BY COALESCE(j.finished_at, j.created_at) DESC
                LIMIT 1
            ) AS job_error,
            GREATEST(
                lr.confirmed_at,
                l.updated_at,
                (
                    SELECT max(GREATEST(j.created_at, j.started_at, j.finished_at))
                    FROM psy.jobs j WHERE j.lecture_id = lr.lecture_id
                )
            ) AS last_move
        FROM psy.lecture_requests lr
        LEFT JOIN psy.lectures l ON l.id = lr.lecture_id
        WHERE lr.status IN ('queued', 'processing')
        ORDER BY lr.confirmed_at
        FOR UPDATE OF lr SKIP LOCKED
    LOOP
        IF r.lec_status IN ('questions_ready', 'cards_ready') AND r.terms_done THEN
            -- успех: списание + уведомление в одной транзакции
            UPDATE psy.lecture_requests x
            SET status = 'done', finished_at = now(), error_code = NULL, error_detail = NULL
            WHERE x.id = r.id;

            INSERT INTO psy.quota_ledger (workspace_id, request_id, delta, reason)
            VALUES (r.workspace_id, r.id, 1, 'lecture_done')
            ON CONFLICT ON CONSTRAINT quota_ledger_request_reason_key DO NOTHING;

            INSERT INTO psy.outbox (user_id, kind, payload, dedupe_key)
            VALUES (
                r.user_id,
                'request_done',
                jsonb_build_object(
                    'request_id', r.id,
                    'lecture_id', r.lecture_id,
                    'course_id', r.course_id,
                    'channel', r.channel,
                    'source_kind', r.source_kind,
                    'quota_remaining', psy.workspace_quota_remaining(r.workspace_id)
                ),
                'request_done:' || r.id::text
            )
            ON CONFLICT ON CONSTRAINT outbox_dedupe_key_key DO NOTHING;

            request_id := r.id; user_id := r.user_id; outcome := 'done';
            error_code := NULL; inbox_path := NULL;
            RETURN NEXT;

        ELSIF r.has_failed OR r.lec_status = 'failed' THEN
            -- провал: job failed (psy_04/05 статус лекции не меняют) или лекция failed; квота не списывается
            v_err := COALESCE(r.job_error, r.lec_error);
            v_code := psy.lecture_request_error_code(v_err);

            UPDATE psy.lecture_requests x
            SET status = 'failed',
                finished_at = now(),
                error_code = v_code,
                error_detail = NULLIF(left(regexp_replace(COALESCE(v_err, ''), 'https?://[^[:space:]]+', '[url]', 'gi'), 300), '')
            WHERE x.id = r.id;

            INSERT INTO psy.outbox (user_id, kind, payload, dedupe_key)
            VALUES (
                r.user_id,
                'request_failed',
                jsonb_build_object(
                    'request_id', r.id,
                    'lecture_id', r.lecture_id,
                    'channel', r.channel,
                    'source_kind', r.source_kind,
                    'error_code', v_code
                ),
                'request_failed:' || r.id::text
            )
            ON CONFLICT ON CONSTRAINT outbox_dedupe_key_key DO NOTHING;

            request_id := r.id; user_id := r.user_id; outcome := 'failed';
            error_code := v_code; inbox_path := NULL;
            RETURN NEXT;

        ELSIF r.last_move IS NOT NULL AND r.last_move < now() - interval '8 hours' THEN
            -- таймаут: без движения > 8 ч; снимаем хвосты, чтобы конвейер не тратил бюджет на брошенную заявку
            UPDATE psy.jobs j
            SET status = 'failed', attempts = 3, error = 'timeout', finished_at = now()
            WHERE j.lecture_id = r.lecture_id AND j.status IN ('queued', 'running');

            UPDATE psy.lectures l
            SET status = 'failed', error = 'timeout'
            WHERE l.id = r.lecture_id AND l.status <> 'failed';

            UPDATE psy.lecture_requests x
            SET status = 'failed', finished_at = now(), error_code = 'timeout'
            WHERE x.id = r.id;

            INSERT INTO psy.outbox (user_id, kind, payload, dedupe_key)
            VALUES (
                r.user_id,
                'request_failed',
                jsonb_build_object(
                    'request_id', r.id,
                    'lecture_id', r.lecture_id,
                    'channel', r.channel,
                    'source_kind', r.source_kind,
                    'error_code', 'timeout'
                ),
                'request_failed:' || r.id::text
            )
            ON CONFLICT ON CONSTRAINT outbox_dedupe_key_key DO NOTHING;

            request_id := r.id; user_id := r.user_id; outcome := 'failed';
            error_code := 'timeout'; inbox_path := NULL;
            RETURN NEXT;

        ELSIF r.has_running AND r.status = 'queued' THEN
            UPDATE psy.lecture_requests x
            SET status = 'processing', started_at = COALESCE(x.started_at, now())
            WHERE x.id = r.id;

            request_id := r.id; user_id := r.user_id; outcome := 'processing';
            error_code := NULL; inbox_path := NULL;
            RETURN NEXT;
        END IF;
    END LOOP;

    -- confirming старше TTL (30 мин) -> expired; outbox не нужен
    FOR r IN
        WITH u AS (
            UPDATE psy.lecture_requests x
            SET status = 'expired', finished_at = now()
            WHERE x.status = 'confirming'
              AND x.created_at < now() - interval '30 minutes'
            RETURNING x.id, x.user_id
        )
        SELECT u.id, u.user_id FROM u
    LOOP
        request_id := r.id; user_id := r.user_id; outcome := 'expired';
        error_code := NULL; inbox_path := NULL;
        RETURN NEXT;
    END LOOP;

    -- уборка inbox у отменённых/просроченных/проваленных заявок
    FOR r IN
        SELECT lr.id, lr.user_id, lr.source ->> 'inbox_path' AS ip
        FROM psy.lecture_requests lr
        WHERE lr.status IN ('cancelled', 'expired', 'failed')
          AND lr.inbox_cleaned_at IS NULL
          AND COALESCE(lr.source ->> 'inbox_path', '') <> ''
        ORDER BY lr.finished_at
        LIMIT 50
    LOOP
        request_id := r.id; user_id := r.user_id; outcome := 'cleanup';
        error_code := NULL; inbox_path := r.ip;
        RETURN NEXT;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION psy.finalize_pending_requests() IS
    'Определяет исход queued/processing заявок по jobs и статусу лекции; списывает квоту только по успеху (идемпотентно); пишет outbox; возвращает строки processing/done/failed/expired/cleanup.';

-- ---------------------------------------------------------------------------
-- Права роли n8n (как в 0006/0007)
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.user_identities TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.lecture_requests TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.quota_ledger TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.intake_sessions TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.outbox TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA psy TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.users_sync_telegram_identity() TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.resolve_identity(text, text, text, text) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.workspace_quota_remaining(uuid) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.queue_position(uuid) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.lecture_request_error_code(text) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.create_lecture_request(uuid, text, uuid, text, text, jsonb, text) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.confirm_lecture_request(uuid, uuid, numeric, numeric, integer) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.cancel_lecture_request(uuid, uuid) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.refund_lecture_request(uuid, text) TO psy_n8n;
        GRANT EXECUTE ON FUNCTION psy.finalize_pending_requests() TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
