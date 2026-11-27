-- psy-memory-trainer / workspaces, course members, invite links
-- Self-serve /start + owner invites. Progress tables untouched.
-- Прогон на проде — только после явного «ок» владельца.

BEGIN;

ALTER TABLE psy.users
    ADD COLUMN IF NOT EXISTS is_system_admin boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN psy.users.is_system_admin IS
    'Platform admin (not course owner). Set only in controlled backfill/ops.';

CREATE TABLE IF NOT EXISTS psy.workspaces (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    owner_user_id uuid NOT NULL REFERENCES psy.users (id),
    lecture_quota integer DEFAULT 2,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workspaces_owner_user_id_key UNIQUE (owner_user_id),
    CONSTRAINT workspaces_lecture_quota_check CHECK (
        lecture_quota IS NULL OR lecture_quota >= 0
    )
);

COMMENT ON TABLE psy.workspaces IS
    'One workspace per user. lecture_quota NULL = unlimited; spend counted in a later step.';
COMMENT ON COLUMN psy.workspaces.lecture_quota IS
    'Max lectures the owner may upload; NULL = no limit. Default 2 for new users.';

DROP TRIGGER IF EXISTS workspaces_set_updated_at ON psy.workspaces;
CREATE TRIGGER workspaces_set_updated_at
    BEFORE UPDATE ON psy.workspaces
    FOR EACH ROW
    EXECUTE FUNCTION psy.set_updated_at();

ALTER TABLE psy.courses
    ADD COLUMN IF NOT EXISTS workspace_id uuid REFERENCES psy.workspaces (id);

COMMENT ON COLUMN psy.courses.user_id IS
    'Legacy owner pointer; canonical owner = workspaces.owner_user_id via courses.workspace_id.';
COMMENT ON COLUMN psy.courses.workspace_id IS
    'Workspace that owns this course.';

CREATE TABLE IF NOT EXISTS psy.invites (
    id uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    course_id uuid NOT NULL REFERENCES psy.courses (id) ON DELETE CASCADE,
    created_by uuid NOT NULL REFERENCES psy.users (id),
    code_hash text NOT NULL,
    max_uses integer,
    uses_count integer NOT NULL DEFAULT 0,
    expires_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT invites_code_hash_key UNIQUE (code_hash),
    CONSTRAINT invites_max_uses_check CHECK (max_uses IS NULL OR max_uses > 0),
    CONSTRAINT invites_uses_count_check CHECK (uses_count >= 0),
    CONSTRAINT invites_uses_vs_max_check CHECK (
        max_uses IS NULL OR uses_count <= max_uses
    )
);

CREATE INDEX IF NOT EXISTS invites_course_id_active_idx
    ON psy.invites (course_id)
    WHERE revoked_at IS NULL;

COMMENT ON TABLE psy.invites IS
    'Invite links for a course. Store only sha256 hex of the code; plaintext never persisted.';

CREATE TABLE IF NOT EXISTS psy.course_members (
    course_id uuid NOT NULL REFERENCES psy.courses (id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES psy.users (id) ON DELETE CASCADE,
    role text NOT NULL DEFAULT 'student',
    invite_id uuid REFERENCES psy.invites (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (course_id, user_id),
    CONSTRAINT course_members_role_check CHECK (role IN ('student'))
);

CREATE INDEX IF NOT EXISTS course_members_user_id_idx
    ON psy.course_members (user_id);

COMMENT ON TABLE psy.course_members IS
    'Students added via invite (or backfill). Course owner is workspace owner, not a member row.';

-- Backfill: курсы сознательно привязаны к workspace Сергея независимо от courses.user_id (в проде = Наталья).
-- Require exactly one Сергей and one Наталья; no course titles/ids in this script.
DO $$
DECLARE
    serge_id uuid;
    natalya_id uuid;
    serge_ws uuid;
    n_serge int;
    n_natalya int;
BEGIN
    SELECT count(*) INTO n_serge FROM psy.users WHERE display_name = 'Сергей';
    SELECT count(*) INTO n_natalya FROM psy.users WHERE display_name = 'Наталья';
    IF n_serge <> 1 OR n_natalya <> 1 THEN
        RAISE EXCEPTION
            '0007 backfill expects exactly 1 user Сергей and 1 Наталья (got % / %)',
            n_serge, n_natalya;
    END IF;

    SELECT id INTO serge_id FROM psy.users WHERE display_name = 'Сергей';
    SELECT id INTO natalya_id FROM psy.users WHERE display_name = 'Наталья';

    UPDATE psy.users
    SET is_system_admin = true
    WHERE id = serge_id;

    INSERT INTO psy.workspaces (owner_user_id, lecture_quota)
    VALUES (serge_id, NULL)
    ON CONFLICT (owner_user_id) DO NOTHING;

    INSERT INTO psy.workspaces (owner_user_id)
    SELECT u.id
    FROM psy.users u
    WHERE u.id <> serge_id
    ON CONFLICT (owner_user_id) DO NOTHING;

    SELECT id INTO serge_ws
    FROM psy.workspaces
    WHERE owner_user_id = serge_id;

    IF serge_ws IS NULL THEN
        RAISE EXCEPTION '0007 backfill: workspace for Сергей missing';
    END IF;

    UPDATE psy.courses
    SET workspace_id = serge_ws
    WHERE workspace_id IS NULL;

    INSERT INTO psy.course_members (course_id, user_id, role)
    SELECT c.id, natalya_id, 'student'
    FROM psy.courses c
    JOIN psy.workspaces w ON w.id = c.workspace_id
    WHERE w.owner_user_id <> natalya_id
    ON CONFLICT (course_id, user_id) DO NOTHING;
END;
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM psy.courses
        WHERE workspace_id IS NULL
    ) THEN
        RAISE EXCEPTION '0007: courses.workspace_id still NULL after backfill';
    END IF;
END;
$$;

ALTER TABLE psy.courses
    ALTER COLUMN workspace_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS courses_workspace_id_idx
    ON psy.courses (workspace_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'psy_n8n') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.workspaces TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.invites TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE psy.course_members TO psy_n8n;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA psy TO psy_n8n;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA psy TO psy_n8n;
    END IF;
END;
$$;

COMMIT;
