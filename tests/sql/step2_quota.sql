-- psy-memory-trainer / SQL-тест шага 2 (квота, заявки на лекции, identity)
--
-- Предпосылки: применены 0001..0008, существует роль psy_n8n (без USAGE на служебной схеме расширений).
-- Запуск: psql -v ON_ERROR_STOP=1 -f tests/sql/step2_quota.sql
-- Все данные синтетические; транзакция в конце откатывается (ROLLBACK), БД не меняется.
-- Первая же проваленная проверка бросает исключение и останавливает прогон.

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL ROLE psy_n8n;

-- ---------------------------------------------------------------------------
-- (1) Успех: ровно одна строка quota_ledger +1; повторный finalize не списывает второй раз
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    v_req uuid;
    v_lec uuid;
    c record;
    n integer;
BEGIN
    SELECT i.user_id, i.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000001', '9000000001', 'ci_synth_1') i;

    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 1') RETURNING id INTO v_course;

    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/a1.mp4', 'canonical', 'https://example.com/ci/a1.mp4',
                           'name', 'a1.mp4', 'size_bytes', 1000, 'duration_sec', 600, 'resolved_kind', 'video'),
        NULL);
    IF NOT c.ok THEN RAISE EXCEPTION '(1) create failed: %', c.reason; END IF;
    v_req := c.request_id;

    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
    IF NOT c.ok THEN RAISE EXCEPTION '(1) confirm failed: %', c.reason; END IF;
    v_lec := c.lecture_id;
    IF c.queue_position IS NULL OR c.queue_position < 0
       OR c.queue_position IS DISTINCT FROM psy.queue_position(v_req) THEN
        RAISE EXCEPTION '(1) bad queue_position: %', c.queue_position;
    END IF;

    -- постановка квоту не списывает
    IF psy.workspace_quota_remaining(v_ws) <> 2 THEN
        RAISE EXCEPTION '(1) quota must stay 2 after confirm, got %', psy.workspace_quota_remaining(v_ws);
    END IF;
    -- в job нет ссылки
    IF EXISTS (SELECT 1 FROM psy.jobs j WHERE j.lecture_id = v_lec AND j.payload::text LIKE '%example.com%') THEN
        RAISE EXCEPTION '(1) url leaked into jobs.payload';
    END IF;

    -- имитация успешного конвейера
    UPDATE psy.jobs SET status = 'done', finished_at = now() WHERE lecture_id = v_lec;
    INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key)
    VALUES (v_lec, 'terms', 'done', 'terms:' || v_lec::text);
    UPDATE psy.lectures SET status = 'questions_ready' WHERE id = v_lec;

    SELECT count(*) INTO n FROM psy.finalize_pending_requests() f
    WHERE f.request_id = v_req AND f.outcome = 'done';
    IF n <> 1 THEN RAISE EXCEPTION '(1) first finalize must report done once, got %', n; END IF;

    -- повторный finalize не меняет журнал
    PERFORM * FROM psy.finalize_pending_requests();
    PERFORM * FROM psy.finalize_pending_requests();

    SELECT count(*) INTO n FROM psy.quota_ledger q
    WHERE q.request_id = v_req AND q.reason = 'lecture_done' AND q.delta = 1;
    IF n <> 1 THEN RAISE EXCEPTION '(1) expected exactly one ledger +1, got %', n; END IF;

    SELECT count(*) INTO n FROM psy.quota_ledger q WHERE q.workspace_id = v_ws;
    IF n <> 1 THEN RAISE EXCEPTION '(1) expected exactly one ledger row for workspace, got %', n; END IF;

    IF psy.workspace_quota_remaining(v_ws) <> 1 THEN
        RAISE EXCEPTION '(1) remaining expected 1, got %', psy.workspace_quota_remaining(v_ws);
    END IF;

    SELECT count(*) INTO n FROM psy.outbox o
    WHERE o.dedupe_key = 'request_done:' || v_req::text AND o.kind = 'request_done' AND o.user_id = v_user;
    IF n <> 1 THEN RAISE EXCEPTION '(1) expected one outbox request_done, got %', n; END IF;

    IF (SELECT status FROM psy.lecture_requests WHERE id = v_req) <> 'done' THEN
        RAISE EXCEPTION '(1) request status must be done';
    END IF;

    RAISE NOTICE 'ok (1) success -> single ledger +1, finalize idempotent';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (2) Провал (job failed): списания нет, уведомление об ошибке есть
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    v_req uuid;
    v_lec uuid;
    c record;
    n integer;
    v_code text;
BEGIN
    SELECT i.user_id, i.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000002', '9000000002', 'ci_synth_2') i;

    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 2') RETURNING id INTO v_course;

    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/a2.mp4', 'canonical', 'https://example.com/ci/a2.mp4'),
        NULL);
    v_req := c.request_id;
    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
    IF NOT c.ok THEN RAISE EXCEPTION '(2) confirm failed: %', c.reason; END IF;
    v_lec := c.lecture_id;

    -- psy_04/05 помечают job failed, а статус лекции не меняют
    UPDATE psy.lectures SET status = 'transcribed' WHERE id = v_lec;
    UPDATE psy.jobs SET status = 'failed', attempts = 3, finished_at = now(),
           error = 'empty_transcript at https://example.com/secret?token=x'
    WHERE lecture_id = v_lec;

    SELECT count(*) INTO n FROM psy.finalize_pending_requests() f
    WHERE f.request_id = v_req AND f.outcome = 'failed' AND f.error_code = 'no_speech';
    IF n <> 1 THEN RAISE EXCEPTION '(2) finalize must report failed/no_speech once, got %', n; END IF;

    SELECT count(*) INTO n FROM psy.quota_ledger q WHERE q.workspace_id = v_ws;
    IF n <> 0 THEN RAISE EXCEPTION '(2) ledger must stay empty on failure, got %', n; END IF;
    IF psy.workspace_quota_remaining(v_ws) <> 2 THEN
        RAISE EXCEPTION '(2) quota must stay 2, got %', psy.workspace_quota_remaining(v_ws);
    END IF;

    SELECT lr.error_code INTO v_code FROM psy.lecture_requests lr WHERE lr.id = v_req;
    IF v_code <> 'no_speech' THEN RAISE EXCEPTION '(2) error_code expected no_speech, got %', v_code; END IF;
    IF (SELECT error_detail FROM psy.lecture_requests WHERE id = v_req) LIKE '%example.com%' THEN
        RAISE EXCEPTION '(2) url leaked into error_detail';
    END IF;

    SELECT count(*) INTO n FROM psy.outbox o
    WHERE o.dedupe_key = 'request_failed:' || v_req::text AND o.kind = 'request_failed';
    IF n <> 1 THEN RAISE EXCEPTION '(2) expected one outbox request_failed, got %', n; END IF;

    -- повторный вызов ничего не дублирует
    PERFORM * FROM psy.finalize_pending_requests();
    SELECT count(*) INTO n FROM psy.outbox o WHERE o.user_id = v_user;
    IF n <> 1 THEN RAISE EXCEPTION '(2) outbox must stay at 1 row, got %', n; END IF;

    RAISE NOTICE 'ok (2) failure -> no ledger spend';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (3) NULL-квота = безлимит
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    v_lec uuid;
    c record;
    i integer;
    v_req uuid;
BEGIN
    SELECT x.user_id, x.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000003', '9000000003', 'ci_synth_3') x;

    UPDATE psy.workspaces SET lecture_quota = NULL WHERE id = v_ws;
    IF psy.workspace_quota_remaining(v_ws) IS NOT NULL THEN
        RAISE EXCEPTION '(3) remaining must be NULL for unlimited workspace';
    END IF;

    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 3') RETURNING id INTO v_course;

    FOR i IN 1..4 LOOP
        SELECT * INTO c FROM psy.create_lecture_request(
            v_user, 'telegram', v_course, NULL, 'direct_url',
            jsonb_build_object('url', 'https://example.com/ci/u' || i, 'canonical', 'https://example.com/ci/u' || i),
            NULL);
        IF NOT c.ok THEN RAISE EXCEPTION '(3) create % failed: %', i, c.reason; END IF;
        v_req := c.request_id;

        SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
        IF NOT c.ok THEN RAISE EXCEPTION '(3) confirm % failed: %', i, c.reason; END IF;
        v_lec := c.lecture_id;

        UPDATE psy.jobs SET status = 'done', finished_at = now() WHERE lecture_id = v_lec;
        INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key)
        VALUES (v_lec, 'terms', 'done', 'terms:' || v_lec::text);
        UPDATE psy.lectures SET status = 'questions_ready' WHERE id = v_lec;
        PERFORM * FROM psy.finalize_pending_requests();
    END LOOP;

    IF psy.workspace_quota_remaining(v_ws) IS NOT NULL THEN
        RAISE EXCEPTION '(3) remaining must stay NULL';
    END IF;
    IF (SELECT count(*) FROM psy.lecture_requests WHERE user_id = v_user AND status = 'done') <> 4 THEN
        RAISE EXCEPTION '(3) all 4 requests must be done for unlimited quota';
    END IF;

    RAISE NOTICE 'ok (3) NULL quota = unlimited';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (4) Квота 2: после двух успехов третье подтверждение -> no_quota
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    v_lec uuid;
    c record;
    i integer;
    v_req uuid;
BEGIN
    SELECT x.user_id, x.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000004', '9000000004', 'ci_synth_4') x;

    IF (SELECT lecture_quota FROM psy.workspaces WHERE id = v_ws) <> 2 THEN
        RAISE EXCEPTION '(4) default quota must be 2';
    END IF;

    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 4') RETURNING id INTO v_course;

    FOR i IN 1..2 LOOP
        SELECT * INTO c FROM psy.create_lecture_request(
            v_user, 'telegram', v_course, NULL, 'direct_url',
            jsonb_build_object('url', 'https://example.com/ci/q' || i, 'canonical', 'https://example.com/ci/q' || i),
            NULL);
        v_req := c.request_id;
        SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
        IF NOT c.ok THEN RAISE EXCEPTION '(4) confirm % failed: %', i, c.reason; END IF;
        v_lec := c.lecture_id;

        UPDATE psy.jobs SET status = 'done', finished_at = now() WHERE lecture_id = v_lec;
        INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key)
        VALUES (v_lec, 'terms', 'done', 'terms:' || v_lec::text);
        UPDATE psy.lectures SET status = 'questions_ready' WHERE id = v_lec;
        PERFORM * FROM psy.finalize_pending_requests();
    END LOOP;

    IF psy.workspace_quota_remaining(v_ws) <> 0 THEN
        RAISE EXCEPTION '(4) remaining expected 0, got %', psy.workspace_quota_remaining(v_ws);
    END IF;

    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/q3', 'canonical', 'https://example.com/ci/q3'),
        NULL);
    IF NOT c.ok THEN RAISE EXCEPTION '(4) create 3 failed: %', c.reason; END IF;
    v_req := c.request_id;

    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
    IF c.ok OR c.reason <> 'no_quota' THEN
        RAISE EXCEPTION '(4) third confirm must be no_quota, got ok=% reason=%', c.ok, c.reason;
    END IF;
    IF (SELECT status FROM psy.lecture_requests WHERE id = v_req) <> 'confirming' THEN
        RAISE EXCEPTION '(4) rejected request must stay confirming';
    END IF;

    RAISE NOTICE 'ok (4) quota 2 -> third confirm is no_quota';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (5) Второе подтверждение при активной заявке -> busy_user
--     (+ отмена активной из queued освобождает слот)
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    v_a uuid;
    v_b uuid;
    c record;
    d record;
BEGIN
    SELECT x.user_id, x.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000005', '9000000005', 'ci_synth_5') x;

    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 5') RETURNING id INTO v_course;

    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/b1', 'canonical', 'https://example.com/ci/b1'),
        NULL);
    v_a := c.request_id;
    SELECT * INTO c FROM psy.confirm_lecture_request(v_a, v_user, 1000000, 0.8, 1000000);
    IF NOT c.ok THEN RAISE EXCEPTION '(5) first confirm failed: %', c.reason; END IF;

    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/b2', 'canonical', 'https://example.com/ci/b2'),
        NULL);
    IF NOT c.ok THEN RAISE EXCEPTION '(5) second create failed: %', c.reason; END IF;
    v_b := c.request_id;

    SELECT * INTO c FROM psy.confirm_lecture_request(v_b, v_user, 1000000, 0.8, 1000000);
    IF c.ok OR c.reason <> 'busy_user' THEN
        RAISE EXCEPTION '(5) expected busy_user, got ok=% reason=%', c.ok, c.reason;
    END IF;

    -- чужой пользователь подтвердить заявку не может
    SELECT * INTO d FROM psy.confirm_lecture_request(v_b, gen_random_uuid(), 1000000, 0.8, 1000000);
    IF d.ok OR d.reason <> 'not_owner' THEN
        RAISE EXCEPTION '(5) expected not_owner, got ok=% reason=%', d.ok, d.reason;
    END IF;

    -- отмена queued-заявки (job ещё не running) освобождает слот
    SELECT * INTO c FROM psy.cancel_lecture_request(v_a, v_user);
    IF NOT c.ok THEN RAISE EXCEPTION '(5) cancel queued failed: %', c.reason; END IF;
    IF (SELECT count(*) FROM psy.jobs j JOIN psy.lecture_requests lr ON lr.lecture_id = j.lecture_id
        WHERE lr.id = v_a AND j.status = 'queued') <> 0 THEN
        RAISE EXCEPTION '(5) cancelled request must not leave queued jobs';
    END IF;

    SELECT * INTO c FROM psy.confirm_lecture_request(v_b, v_user, 1000000, 0.8, 1000000);
    IF NOT c.ok THEN RAISE EXCEPTION '(5) confirm after cancel failed: %', c.reason; END IF;

    -- из processing отмена отказывает
    UPDATE psy.jobs SET status = 'running', started_at = now() WHERE lecture_id = c.lecture_id;
    SELECT * INTO c FROM psy.cancel_lecture_request(v_b, v_user);
    IF c.ok OR c.reason <> 'processing' THEN
        RAISE EXCEPTION '(5) cancel while running must be refused, got ok=% reason=%', c.ok, c.reason;
    END IF;

    PERFORM * FROM psy.finalize_pending_requests();
    IF (SELECT status FROM psy.lecture_requests WHERE id = v_b) <> 'processing' THEN
        RAISE EXCEPTION '(5) finalize must mark running request as processing';
    END IF;

    RAISE NOTICE 'ok (5) busy_user / not_owner / cancel rules';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (6) Дубликат источника -> отказ (на create и на confirm)
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    v_a uuid;
    v_b uuid;
    v_lec uuid;
    c record;
BEGIN
    SELECT x.user_id, x.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000006', '9000000006', 'ci_synth_6') x;

    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 6') RETURNING id INTO v_course;

    -- A: первая заявка, ставим в очередь
    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/d1', 'canonical', 'https://example.com/ci/d1'),
        NULL);
    v_a := c.request_id;
    SELECT * INTO c FROM psy.confirm_lecture_request(v_a, v_user, 1000000, 0.8, 1000000);
    IF NOT c.ok THEN RAISE EXCEPTION '(6) confirm A failed: %', c.reason; END IF;
    v_lec := c.lecture_id;

    -- дубликат на create (A в queued)
    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/d1', 'canonical', 'https://example.com/ci/d1'),
        NULL);
    IF c.ok OR c.reason <> 'duplicate_source' THEN
        RAISE EXCEPTION '(6) create duplicate must be refused, got ok=% reason=%', c.ok, c.reason;
    END IF;

    -- A завершена (done) — дубликат всё равно запрещён
    UPDATE psy.jobs SET status = 'done', finished_at = now() WHERE lecture_id = v_lec;
    INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key)
    VALUES (v_lec, 'terms', 'done', 'terms:' || v_lec::text);
    UPDATE psy.lectures SET status = 'questions_ready' WHERE id = v_lec;
    PERFORM * FROM psy.finalize_pending_requests();

    -- защита на confirm: B с чужим hash, подменённым на hash A
    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/d2', 'canonical', 'https://example.com/ci/d2'),
        NULL);
    IF NOT c.ok THEN RAISE EXCEPTION '(6) create B failed: %', c.reason; END IF;
    v_b := c.request_id;
    UPDATE psy.lecture_requests
    SET source_hash = (SELECT source_hash FROM psy.lecture_requests WHERE id = v_a)
    WHERE id = v_b;

    SELECT * INTO c FROM psy.confirm_lecture_request(v_b, v_user, 1000000, 0.8, 1000000);
    IF c.ok OR c.reason <> 'duplicate_source' THEN
        RAISE EXCEPTION '(6) confirm duplicate must be refused, got ok=% reason=%', c.ok, c.reason;
    END IF;

    RAISE NOTICE 'ok (6) duplicate source refused';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (7) refund идемпотентен
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    v_req uuid;
    v_lec uuid;
    c record;
    n integer;
BEGIN
    SELECT x.user_id, x.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000007', '9000000007', 'ci_synth_7') x;

    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 7') RETURNING id INTO v_course;

    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/r1', 'canonical', 'https://example.com/ci/r1'),
        NULL);
    v_req := c.request_id;

    -- до списания возвращать нечего
    IF psy.refund_lecture_request(v_req, 'ci') THEN
        RAISE EXCEPTION '(7) refund without charge must be a no-op';
    END IF;

    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
    v_lec := c.lecture_id;
    UPDATE psy.jobs SET status = 'done', finished_at = now() WHERE lecture_id = v_lec;
    INSERT INTO psy.jobs (lecture_id, step, status, idempotency_key)
    VALUES (v_lec, 'terms', 'done', 'terms:' || v_lec::text);
    UPDATE psy.lectures SET status = 'questions_ready' WHERE id = v_lec;
    PERFORM * FROM psy.finalize_pending_requests();

    IF psy.workspace_quota_remaining(v_ws) <> 1 THEN
        RAISE EXCEPTION '(7) remaining expected 1 before refund, got %', psy.workspace_quota_remaining(v_ws);
    END IF;

    IF NOT psy.refund_lecture_request(v_req, 'ci manual refund') THEN
        RAISE EXCEPTION '(7) first refund must be recorded';
    END IF;
    IF psy.refund_lecture_request(v_req, 'ci manual refund') THEN
        RAISE EXCEPTION '(7) second refund must be a no-op';
    END IF;
    PERFORM psy.refund_lecture_request(v_req, NULL);

    SELECT count(*) INTO n FROM psy.quota_ledger q WHERE q.request_id = v_req AND q.reason = 'refund';
    IF n <> 1 THEN RAISE EXCEPTION '(7) expected one refund row, got %', n; END IF;
    IF psy.workspace_quota_remaining(v_ws) <> 2 THEN
        RAISE EXCEPTION '(7) remaining expected 2 after refund, got %', psy.workspace_quota_remaining(v_ws);
    END IF;

    -- UNIQUE (request_id, reason) держит инвариант и на уровне таблицы
    BEGIN
        INSERT INTO psy.quota_ledger (workspace_id, request_id, delta, reason)
        VALUES (v_ws, v_req, -1, 'refund');
        RAISE EXCEPTION '(7) duplicate refund insert must violate unique';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    RAISE NOTICE 'ok (7) refund idempotent';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (8) Триггер identity: INSERT INTO users(telegram_user_id) создаёт user_identities
--     + resolve_identity идемпотентна, другой канал работает без telegram_user_id
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_user2 uuid;
    v_ws uuid;
    r1 record;
    r2 record;
    n integer;
BEGIN
    -- как psy_08: создание по-старому
    INSERT INTO psy.users (telegram_user_id, display_name)
    VALUES (9000000008, 'ci_synth_8')
    ON CONFLICT (telegram_user_id) DO UPDATE SET display_name = EXCLUDED.display_name
    RETURNING id INTO v_user;

    SELECT count(*) INTO n FROM psy.user_identities ui
    WHERE ui.user_id = v_user AND ui.channel = 'telegram'
      AND ui.external_id = '9000000008' AND ui.external_chat_id = '9000000008';
    IF n <> 1 THEN RAISE EXCEPTION '(8) trigger must create telegram identity, got %', n; END IF;

    -- повторный upsert (как psy_08) не ломается и не дублирует
    INSERT INTO psy.users (telegram_user_id, display_name)
    VALUES (9000000008, 'ci_synth_8')
    ON CONFLICT (telegram_user_id) DO UPDATE SET display_name = EXCLUDED.display_name;
    SELECT count(*) INTO n FROM psy.user_identities ui WHERE ui.user_id = v_user;
    IF n <> 1 THEN RAISE EXCEPTION '(8) identity must not be duplicated, got %', n; END IF;

    -- telegram_user_id теперь может быть NULL; UPDATE telegram_user_id тоже создаёт identity
    INSERT INTO psy.users (display_name) VALUES ('ci_synth_8b') RETURNING id INTO v_user2;
    SELECT count(*) INTO n FROM psy.user_identities ui WHERE ui.user_id = v_user2;
    IF n <> 0 THEN RAISE EXCEPTION '(8) user without telegram id must have no identity'; END IF;
    UPDATE psy.users SET telegram_user_id = 9000000018 WHERE id = v_user2;
    SELECT count(*) INTO n FROM psy.user_identities ui
    WHERE ui.user_id = v_user2 AND ui.channel = 'telegram' AND ui.external_id = '9000000018';
    IF n <> 1 THEN RAISE EXCEPTION '(8) UPDATE OF telegram_user_id must create identity'; END IF;

    -- resolve_identity: находит существующего пользователя psy_08 и создаёт workspace
    SELECT * INTO r1 FROM psy.resolve_identity('telegram', '9000000008', '9000000008', 'ci_synth_8');
    IF r1.user_id <> v_user OR r1.is_new THEN
        RAISE EXCEPTION '(8) resolve_identity must find existing user';
    END IF;
    IF r1.workspace_id IS NULL
       OR (SELECT lecture_quota FROM psy.workspaces WHERE id = r1.workspace_id) <> 2 THEN
        RAISE EXCEPTION '(8) resolve_identity must ensure workspace with default quota';
    END IF;

    -- новый telegram-пользователь, повтор — идемпотентно
    SELECT * INTO r1 FROM psy.resolve_identity('telegram', '9000000028', '9000000028', 'ci_synth_8c');
    SELECT * INTO r2 FROM psy.resolve_identity('telegram', '9000000028', '9000000028', 'ci_synth_8c');
    IF NOT r1.is_new OR r2.is_new OR r1.user_id <> r2.user_id OR r1.workspace_id <> r2.workspace_id THEN
        RAISE EXCEPTION '(8) resolve_identity must be idempotent';
    END IF;
    IF (SELECT telegram_user_id FROM psy.users WHERE id = r1.user_id) IS DISTINCT FROM 9000000028 THEN
        RAISE EXCEPTION '(8) telegram_user_id must be filled for telegram channel';
    END IF;

    -- другой канал: users.telegram_user_id = NULL
    SELECT * INTO r1 FROM psy.resolve_identity('max', 'ci-ext-1', 'ci-chat-1', 'ci_synth_8d');
    SELECT * INTO r2 FROM psy.resolve_identity('max', 'ci-ext-1', 'ci-chat-1b', NULL);
    IF NOT r1.is_new OR r2.is_new OR r1.user_id <> r2.user_id THEN
        RAISE EXCEPTION '(8) non-telegram resolve_identity must be idempotent';
    END IF;
    IF (SELECT telegram_user_id FROM psy.users WHERE id = r1.user_id) IS NOT NULL THEN
        RAISE EXCEPTION '(8) non-telegram user must have NULL telegram_user_id';
    END IF;
    IF (SELECT external_chat_id FROM psy.user_identities
        WHERE channel = 'max' AND external_id = 'ci-ext-1') <> 'ci-chat-1b' THEN
        RAISE EXCEPTION '(8) external_chat_id must be refreshed';
    END IF;

    -- плохой канал
    BEGIN
        PERFORM * FROM psy.resolve_identity('Bad Channel!', 'x', NULL, NULL);
        RAISE EXCEPTION '(8) bad channel must raise';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    SELECT w.id INTO v_ws FROM psy.workspaces w WHERE w.owner_user_id = v_user;
    IF v_ws IS NULL THEN RAISE EXCEPTION '(8) workspace missing'; END IF;

    RAISE NOTICE 'ok (8) identity trigger + resolve_identity';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (9) Под psy_n8n нет обращений к служебной схеме расширений
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_bad text;
    v_ok boolean := false;
BEGIN
    IF current_user <> 'psy_n8n' THEN
        RAISE EXCEPTION '(9) test must run as psy_n8n, got %', current_user;
    END IF;

    -- у роли нет USAGE на схеме (если схема есть)
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'extensions')
       AND has_schema_privilege('psy_n8n', 'extensions', 'USAGE') THEN
        RAISE EXCEPTION '(9) psy_n8n must not have USAGE on the extensions schema';
    END IF;

    -- прямое обращение действительно недоступно этой роли
    BEGIN
        EXECUTE 'SELECT extensions.gen_random_uuid()';
    EXCEPTION
        WHEN OTHERS THEN v_ok := true;
    END;
    IF NOT v_ok THEN
        RAISE EXCEPTION '(9) extensions.* must be unavailable for psy_n8n';
    END IF;

    -- ни одна функция 0008 не ссылается на неё в теле
    SELECT string_agg(p.proname, ', ') INTO v_bad
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'psy'
      AND p.proname IN (
          'users_sync_telegram_identity', 'resolve_identity', 'workspace_quota_remaining',
          'queue_position', 'lecture_request_error_code', 'create_lecture_request',
          'confirm_lecture_request', 'cancel_lecture_request', 'refund_lecture_request',
          'finalize_pending_requests'
      )
      AND p.prosrc ~* 'extensions\s*\.';
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '(9) functions reference the extensions schema: %', v_bad;
    END IF;

    -- и у роли есть EXECUTE на каждую из новых функций
    SELECT string_agg(p.proname, ', ') INTO v_bad
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'psy'
      AND p.proname IN (
          'users_sync_telegram_identity', 'resolve_identity', 'workspace_quota_remaining',
          'queue_position', 'lecture_request_error_code', 'create_lecture_request',
          'confirm_lecture_request', 'cancel_lecture_request', 'refund_lecture_request',
          'finalize_pending_requests'
      )
      AND NOT has_function_privilege('psy_n8n', p.oid, 'EXECUTE');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION '(9) psy_n8n lacks EXECUTE on: %', v_bad;
    END IF;

    RAISE NOTICE 'ok (9) no extensions.* access under psy_n8n (all functions ran above as this role)';
END;
$t$;

-- ---------------------------------------------------------------------------
-- (дополнительно) Текстовый источник идёт мимо ASR; лимиты и антиабьюз-отказы
-- ---------------------------------------------------------------------------
DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_req uuid;
    v_lec uuid;
    v_course uuid;
    c record;
BEGIN
    SELECT x.user_id, x.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000010', '9000000010', 'ci_synth_10') x;

    -- новый курс создаётся при подтверждении
    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', NULL, 'ci synthetic new course', 'text',
        jsonb_build_object('text', repeat('синтетический текст ', 200)),
        NULL);
    IF NOT c.ok THEN RAISE EXCEPTION '(x) create text failed: %', c.reason; END IF;
    v_req := c.request_id;

    -- слишком много проваленных заявок за 24 ч -> too_many_failures
    -- (проверяем на отдельном пользователе ниже), здесь — бюджетный и дневной стоп
    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 0, 0.8, 1000000);
    IF c.ok OR c.reason <> 'budget_stop' THEN
        RAISE EXCEPTION '(x) zero budget must stop: ok=% reason=%', c.ok, c.reason;
    END IF;
    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 0);
    IF c.ok OR c.reason <> 'daily_cap' THEN
        RAISE EXCEPTION '(x) zero daily cap must stop: ok=% reason=%', c.ok, c.reason;
    END IF;

    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
    IF NOT c.ok THEN RAISE EXCEPTION '(x) text confirm failed: %', c.reason; END IF;
    v_lec := c.lecture_id;

    IF (SELECT media_kind FROM psy.lectures WHERE id = v_lec) <> 'text'
       OR (SELECT status FROM psy.lectures WHERE id = v_lec) <> 'transcribed' THEN
        RAISE EXCEPTION '(x) text lecture must be media_kind=text, status=transcribed';
    END IF;
    IF (SELECT external_file_id FROM psy.lectures WHERE id = v_lec) <> 'req:' || v_req::text THEN
        RAISE EXCEPTION '(x) external_file_id must be req:<request_id>';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM psy.lecture_texts WHERE lecture_id = v_lec AND char_length(transcript) >= 2000) THEN
        RAISE EXCEPTION '(x) transcript must be stored';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM psy.jobs WHERE lecture_id = v_lec AND step = 'konspekt' AND status = 'queued') THEN
        RAISE EXCEPTION '(x) konspekt job must be queued';
    END IF;
    IF EXISTS (SELECT 1 FROM psy.jobs WHERE lecture_id = v_lec AND step = 'extract_audio') THEN
        RAISE EXCEPTION '(x) text source must skip extract_audio';
    END IF;
    IF (SELECT source ? 'text' FROM psy.lecture_requests WHERE id = v_req) THEN
        RAISE EXCEPTION '(x) text must be dropped from request after copy';
    END IF;
    SELECT lr.course_id INTO v_course FROM psy.lecture_requests lr WHERE lr.id = v_req;
    IF v_course IS NULL OR NOT EXISTS (
        SELECT 1 FROM psy.courses c2
        WHERE c2.id = v_course AND c2.workspace_id = v_ws AND c2.drive_folder_id IS NULL
    ) THEN
        RAISE EXCEPTION '(x) course must be created in the user workspace without drive_folder_id';
    END IF;

    -- чужой курс использовать нельзя
    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', (SELECT id FROM psy.courses WHERE workspace_id <> v_ws LIMIT 1), NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/x1', 'canonical', 'https://example.com/ci/x1'),
        NULL);
    IF c.ok OR c.reason <> 'bad_course' THEN
        RAISE EXCEPTION '(x) foreign course must be refused: ok=% reason=%', c.ok, c.reason;
    END IF;

    RAISE NOTICE 'ok (x) text source, budget/daily stops, foreign course';
END;
$t$;

DO $t$
DECLARE
    v_user uuid;
    v_ws uuid;
    v_course uuid;
    c record;
    i integer;
    v_req uuid;
BEGIN
    SELECT x.user_id, x.workspace_id INTO v_user, v_ws
    FROM psy.resolve_identity('telegram', '9000000011', '9000000011', 'ci_synth_11') x;
    INSERT INTO psy.courses (user_id, workspace_id, title)
    VALUES (v_user, v_ws, 'ci synthetic course 11') RETURNING id INTO v_course;

    -- пять проваленных заявок за сутки
    FOR i IN 1..5 LOOP
        INSERT INTO psy.lecture_requests (
            user_id, workspace_id, course_id, channel, source_kind, source, status, finished_at
        )
        VALUES (v_user, v_ws, v_course, 'telegram', 'direct_url', '{}'::jsonb, 'failed', now());
    END LOOP;

    SELECT * INTO c FROM psy.create_lecture_request(
        v_user, 'telegram', v_course, NULL, 'direct_url',
        jsonb_build_object('url', 'https://example.com/ci/f1', 'canonical', 'https://example.com/ci/f1'),
        NULL);
    v_req := c.request_id;
    SELECT * INTO c FROM psy.confirm_lecture_request(v_req, v_user, 1000000, 0.8, 1000000);
    IF c.ok OR c.reason <> 'too_many_failures' THEN
        RAISE EXCEPTION '(x) expected too_many_failures, got ok=% reason=%', c.ok, c.reason;
    END IF;

    -- просроченное подтверждение -> expired, inbox_path попадает в cleanup
    UPDATE psy.lecture_requests
    SET created_at = now() - interval '2 hours',
        source = jsonb_build_object('inbox_path', 'inbox/ci-synthetic.bin')
    WHERE id = v_req;
    IF NOT EXISTS (
        SELECT 1 FROM psy.finalize_pending_requests() f WHERE f.request_id = v_req AND f.outcome = 'expired'
    ) THEN
        RAISE EXCEPTION '(x) stale confirming must become expired';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM psy.finalize_pending_requests() f
        WHERE f.request_id = v_req AND f.outcome = 'cleanup' AND f.inbox_path = 'inbox/ci-synthetic.bin'
    ) THEN
        RAISE EXCEPTION '(x) expired request must be returned for inbox cleanup';
    END IF;
    UPDATE psy.lecture_requests SET inbox_cleaned_at = now() WHERE id = v_req;
    IF EXISTS (
        SELECT 1 FROM psy.finalize_pending_requests() f WHERE f.request_id = v_req AND f.outcome = 'cleanup'
    ) THEN
        RAISE EXCEPTION '(x) cleaned request must not be returned again';
    END IF;

    RAISE NOTICE 'ok (x) too_many_failures, expiry + cleanup';
END;
$t$;

ROLLBACK;
