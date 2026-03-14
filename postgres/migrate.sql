-- PurpleAssetOne — Migration Script
-- Run this against your existing database to apply schema changes
-- without losing any data.
--
-- Usage:
--   cat postgres/migrate.sql | docker exec -i purpleassetone_db psql -U purpleassetone purpleassetone

-- ─────────────────────────────────────────
-- 1. Add common_name to equipment
-- ─────────────────────────────────────────
ALTER TABLE equipment ADD COLUMN IF NOT EXISTS common_name TEXT;

-- ─────────────────────────────────────────
-- 2. Add superadmin to role CHECK constraint
-- ─────────────────────────────────────────
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('superadmin', 'admin', 'technician', 'viewer'));

-- ─────────────────────────────────────────
-- 3. Ensure areas have metadata column with
--    the standard keys (backfill if empty)
-- ─────────────────────────────────────────
UPDATE areas
SET metadata = metadata ||
    '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'::jsonb
WHERE
    NOT (metadata ? 'website') OR
    NOT (metadata ? 'host_name') OR
    NOT (metadata ? 'host_contact') OR
    NOT (metadata ? 'email') OR
    NOT (metadata ? 'discord');

-- ─────────────────────────────────────────
-- 4. Initial superadmin is now created from environment variables
--    by the backend on first startup. No hardcoded credentials in SQL.
-- ─────────────────────────────────────────

-- ─────────────────────────────────────────
-- 5. App configuration table
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_by  TEXT
);

-- Default theme
INSERT INTO app_config (key, value) VALUES ('theme', '{
  "font": "system",
  "colors": {
    "accent":     "#f5a623",
    "primary":    "#0d6efd",
    "bg":         "#f0f2f5",
    "surface":    "#ffffff",
    "sidebar_bg": "#ffffff",
    "text":       "#212529",
    "success":    "#198754",
    "warning":    "#ffc107",
    "danger":     "#dc3545"
  }
}')
ON CONFLICT (key) DO NOTHING;

-- Default dashboard config
INSERT INTO app_config (key, value) VALUES ('dashboard', '{
  "tiles": [
    {"id":"total_equipment",  "label":"Total Equipment", "stat_key":"total_equipment",  "color":"#f5a623", "visible":true,  "custom":false},
    {"id":"active_equipment", "label":"Active",          "stat_key":"active_equipment", "color":"#198754", "visible":true,  "custom":false},
    {"id":"under_repair",     "label":"In Repair",       "stat_key":"under_repair",     "color":"#ffc107", "visible":true,  "custom":false},
    {"id":"open_tickets",     "label":"Open Tickets",    "stat_key":"open_tickets",     "color":"#dc3545", "visible":true,  "custom":false},
    {"id":"critical_tickets", "label":"Critical / High", "stat_key":"critical_tickets", "color":"#dc3545", "visible":true,  "custom":false},
    {"id":"total_areas",      "label":"Areas",           "stat_key":"total_areas",      "color":"#0d6efd", "visible":true,  "custom":false}
  ],
  "sections": {
    "area_breakdown":  {"visible":true,  "label":"Area Breakdown"},
    "open_tickets_tbl":{"visible":true,  "label":"Open Tickets"}
  }
}')
ON CONFLICT (key) DO NOTHING;

-- Default templates
INSERT INTO app_config (key, value) VALUES ('templates', '{
  "equipment": {
    "fields": [
      {"key":"common_name",  "label":"Common Name",         "visible":true,  "required":false},
      {"key":"make",         "label":"Make",                "visible":true,  "required":true},
      {"key":"model",        "label":"Model",               "visible":true,  "required":true},
      {"key":"serial_number","label":"Serial Number",       "visible":true,  "required":true},
      {"key":"build_date",   "label":"Build Date",          "visible":true,  "required":false},
      {"key":"area_id",      "label":"Area",                "visible":true,  "required":false},
      {"key":"status",       "label":"Status",              "visible":true,  "required":false},
      {"key":"attributes",   "label":"Additional Attributes","visible":true, "required":false}
    ],
    "default_attributes": []
  },
  "tickets": {
    "fields": [
      {"key":"equipment_id", "label":"Equipment",   "visible":true, "required":true},
      {"key":"title",        "label":"Title",       "visible":true, "required":true},
      {"key":"description",  "label":"Description", "visible":true, "required":false},
      {"key":"priority",     "label":"Priority",    "visible":true, "required":false},
      {"key":"assigned_to",  "label":"Assigned To", "visible":true, "required":false},
      {"key":"status",       "label":"Status",      "visible":true, "required":false},
      {"key":"work_log",     "label":"Work Log",    "visible":true, "required":false}
    ]
  },
  "areas": {
    "fields": [
      {"key":"name",         "label":"Area Name",      "visible":true, "required":true},
      {"key":"description",  "label":"Description",    "visible":true, "required":false},
      {"key":"website",      "label":"Website",        "visible":true, "required":false},
      {"key":"host_name",    "label":"Area Host",      "visible":true, "required":false},
      {"key":"host_contact", "label":"Host Contact",   "visible":true, "required":false},
      {"key":"email",        "label":"Email",          "visible":true, "required":false},
      {"key":"discord",      "label":"Discord Channel","visible":true, "required":false}
    ]
  },
  "users": {
    "fields": [
      {"key":"username",  "label":"Username",    "visible":true, "required":true},
      {"key":"full_name", "label":"Full Name",   "visible":true, "required":false},
      {"key":"password",  "label":"Password",    "visible":true, "required":true},
      {"key":"role",      "label":"Role",        "visible":true, "required":false},
      {"key":"is_active", "label":"Active Status","visible":true, "required":false}
    ]
  }
}')
ON CONFLICT (key) DO NOTHING;

-- Add attachments to equipment and repair_tickets (2026-03-11)
ALTER TABLE equipment ADD COLUMN IF NOT EXISTS attachments JSONB DEFAULT '[]';
ALTER TABLE repair_tickets ADD COLUMN IF NOT EXISTS attachments JSONB DEFAULT '[]';

-- ─── Scheduling & Authorization (2026-03-11) ────────────────────
-- Expand role CHECK to include member and authorizer
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
  CHECK (role IN ('superadmin', 'admin', 'technician', 'authorizer', 'viewer', 'member'));

-- Add schedulable flag to equipment
ALTER TABLE equipment ADD COLUMN IF NOT EXISTS schedulable BOOLEAN DEFAULT false;

-- Enable btree_gist for exclusion constraint
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Schedules table
CREATE TABLE IF NOT EXISTS schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    equipment_id    UUID NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT schedules_no_overlap EXCLUDE USING GIST (
        equipment_id WITH =,
        tstzrange(start_time, end_time, '[)') WITH &&
    )
);
CREATE INDEX IF NOT EXISTS idx_schedules_equipment ON schedules(equipment_id);
CREATE INDEX IF NOT EXISTS idx_schedules_user ON schedules(user_id);
CREATE INDEX IF NOT EXISTS idx_schedules_time ON schedules(start_time, end_time);

-- Auth sessions table
CREATE TABLE IF NOT EXISTS auth_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    equipment_id    UUID REFERENCES equipment(id) ON DELETE SET NULL,
    authorizer_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    total_slots     INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_equipment ON auth_sessions(equipment_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_authorizer ON auth_sessions(authorizer_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_time ON auth_sessions(start_time);

-- Auth enrollments table
CREATE TABLE IF NOT EXISTS auth_enrollments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES auth_sessions(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    enrolled_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_auth_enrollments_session ON auth_enrollments(session_id);
CREATE INDEX IF NOT EXISTS idx_auth_enrollments_user ON auth_enrollments(user_id);

-- ─── Multi-equipment auth sessions + Equipment groups (2026-03-11b) ─

-- Replace single equipment_id with array on auth_sessions
ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS equipment_ids UUID[] DEFAULT '{}';
-- Migrate existing single FK to array
UPDATE auth_sessions SET equipment_ids = ARRAY[equipment_id] WHERE equipment_id IS NOT NULL AND array_length(equipment_ids,1) IS NULL;
ALTER TABLE auth_sessions DROP COLUMN IF EXISTS equipment_id;

-- Equipment groups
CREATE TABLE IF NOT EXISTS equipment_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    area_id     UUID REFERENCES areas(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS equipment_group_members (
    group_id    UUID NOT NULL REFERENCES equipment_groups(id) ON DELETE CASCADE,
    equipment_id UUID NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    sort_order  INTEGER DEFAULT 0,
    PRIMARY KEY (group_id, equipment_id)
);

-- ─────────────────────────────────────────
-- SESSION 3: Permissions & Auth Providers
-- ─────────────────────────────────────────

-- 1. Expand role constraint to include authorizer + member
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('superadmin','admin','technician','authorizer','member','viewer'));

-- 2. Add auth provider tracking to users
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider  VARCHAR(50) DEFAULT 'local';
ALTER TABLE users ADD COLUMN IF NOT EXISTS external_id    TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_provider_external
    ON users(auth_provider, external_id)
    WHERE external_id IS NOT NULL;

-- 3. Add permissions and auth_config to app_config
-- (rows inserted by the API on first save — no data migration needed)

-- 4. Ensure all existing users are marked as 'local' provider
UPDATE users SET auth_provider = 'local' WHERE auth_provider IS NULL;

-- ─────────────────────────────────────────
-- SESSION 4: Maintenance Calendar + Area Host role
-- ─────────────────────────────────────────

-- 1. Expand role constraint to include area_host
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('superadmin','admin','area_host','technician','authorizer','member','viewer'));

-- 2. Maintenance schedule definitions (recurring)
CREATE TABLE IF NOT EXISTS maintenance_schedules (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title             TEXT NOT NULL,
    description       TEXT,
    equipment_id      UUID REFERENCES equipment(id) ON DELETE CASCADE,
    group_id          UUID REFERENCES equipment_groups(id) ON DELETE CASCADE,
    recurrence_type   TEXT NOT NULL DEFAULT 'days' CHECK (recurrence_type IN ('days','weeks','months','years')),
    recurrence_interval INTEGER NOT NULL DEFAULT 30,
    assigned_to       UUID REFERENCES users(id) ON DELETE SET NULL,
    created_by        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    priority          TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high','critical')),
    estimated_minutes INTEGER,
    checklist         JSONB DEFAULT '[]',
    notify_roles      TEXT[] DEFAULT '{}',
    is_active         BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT maint_sched_target CHECK (equipment_id IS NOT NULL OR group_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_maint_sched_equip ON maintenance_schedules(equipment_id);
CREATE INDEX IF NOT EXISTS idx_maint_sched_group ON maintenance_schedules(group_id);
CREATE INDEX IF NOT EXISTS idx_maint_sched_active ON maintenance_schedules(is_active);

-- 3. Maintenance events (individual occurrences)
CREATE TABLE IF NOT EXISTS maintenance_events (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id       UUID NOT NULL REFERENCES maintenance_schedules(id) ON DELETE CASCADE,
    equipment_id      UUID REFERENCES equipment(id) ON DELETE CASCADE,
    due_date          TIMESTAMPTZ NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_progress','completed','skipped','overdue')),
    assigned_to       UUID REFERENCES users(id) ON DELETE SET NULL,
    completed_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    completed_at      TIMESTAMPTZ,
    notes             TEXT,
    checklist_state   JSONB DEFAULT '[]',
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_maint_events_sched ON maintenance_events(schedule_id);
CREATE INDEX IF NOT EXISTS idx_maint_events_equip ON maintenance_events(equipment_id);
CREATE INDEX IF NOT EXISTS idx_maint_events_due ON maintenance_events(due_date);
CREATE INDEX IF NOT EXISTS idx_maint_events_status ON maintenance_events(status);

-- (Seed users removed — initial superadmin is created from environment variables on first startup)

-- 5. Add category to repair_tickets (repair vs maintenance)
ALTER TABLE repair_tickets ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'repair';
ALTER TABLE repair_tickets DROP CONSTRAINT IF EXISTS repair_tickets_category_check;
DO $$ BEGIN
  ALTER TABLE repair_tickets ADD CONSTRAINT repair_tickets_category_check CHECK (category IN ('repair', 'maintenance'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE INDEX IF NOT EXISTS idx_tickets_category ON repair_tickets(category);

-- 6. Add ticket_id link to maintenance_events
ALTER TABLE maintenance_events ADD COLUMN IF NOT EXISTS ticket_id UUID REFERENCES repair_tickets(id) ON DELETE SET NULL;

-- ─────────────────────────────────────────
-- SESSION 5: Security Hardening
-- ─────────────────────────────────────────

-- 1. Audit log table
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    table_name  TEXT NOT NULL,
    record_id   TEXT,
    operation   TEXT NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    user_id     TEXT,
    user_role   TEXT,
    old_data    JSONB,
    new_data    JSONB,
    changed_fields TEXT[],
    ip_address  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_table ON audit_log(table_name);
CREATE INDEX IF NOT EXISTS idx_audit_log_record ON audit_log(record_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_time ON audit_log(created_at);

-- 2. Audit trigger function
CREATE OR REPLACE FUNCTION audit_trigger_fn()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    _user_id  TEXT;
    _role     TEXT;
    _record_id TEXT;
    _old      JSONB;
    _new      JSONB;
    _changed  TEXT[];
BEGIN
    _user_id := current_setting('app.current_user_id', true);
    _role    := current_setting('app.session_role', true);
    IF TG_OP = 'DELETE' THEN
        _old := to_jsonb(OLD); _record_id := COALESCE(_old->>'id', _old->>'key', '');
        INSERT INTO audit_log (table_name, record_id, operation, user_id, user_role, old_data)
        VALUES (TG_TABLE_NAME, _record_id, 'DELETE', _user_id, _role, _old);
        RETURN OLD;
    ELSIF TG_OP = 'INSERT' THEN
        _new := to_jsonb(NEW); _record_id := COALESCE(_new->>'id', _new->>'key', '');
        INSERT INTO audit_log (table_name, record_id, operation, user_id, user_role, new_data)
        VALUES (TG_TABLE_NAME, _record_id, 'INSERT', _user_id, _role, _new);
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        _old := to_jsonb(OLD); _new := to_jsonb(NEW); _record_id := COALESCE(_new->>'id', _new->>'key', '');
        _changed := ARRAY(SELECT key FROM jsonb_each(_new) WHERE NOT (_old ? key AND _old->key = _new->key));
        IF array_length(_changed, 1) IS NULL THEN RETURN NEW; END IF;
        IF _old ? 'password_hash' THEN _old := _old - 'password_hash'; _new := _new - 'password_hash'; END IF;
        INSERT INTO audit_log (table_name, record_id, operation, user_id, user_role, old_data, new_data, changed_fields)
        VALUES (TG_TABLE_NAME, _record_id, 'UPDATE', _user_id, _role, _old, _new, _changed);
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- 3. Attach audit triggers (safe: DROP IF EXISTS + CREATE)
DO $$ DECLARE t TEXT; tbl TEXT[] := ARRAY['users','areas','equipment','repair_tickets','schedules','auth_sessions','auth_enrollments','equipment_groups','maintenance_schedules','maintenance_events','app_config'];
BEGIN FOREACH t IN ARRAY tbl LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS audit_%s ON %I', replace(t,'.','_'), t);
    EXECUTE format('CREATE TRIGGER audit_%s AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn()', replace(t,'.','_'), t);
END LOOP; END $$;

-- 4. Least-privilege app role
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pa1_app') THEN
        CREATE ROLE pa1_app LOGIN PASSWORD 'changeme_in_env';
    END IF;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    users, areas, equipment, repair_tickets, schedules,
    auth_sessions, auth_enrollments, equipment_groups, equipment_group_members,
    maintenance_schedules, maintenance_events, app_config
TO pa1_app;
GRANT SELECT ON audit_log TO pa1_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pa1_app;

-- 5. Row-Level Security on users
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS users_select ON users; CREATE POLICY users_select ON users FOR SELECT USING (true);
DROP POLICY IF EXISTS users_insert ON users; CREATE POLICY users_insert ON users FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS users_update ON users; CREATE POLICY users_update ON users FOR UPDATE USING (
    role != 'superadmin'
    OR current_setting('app.session_role', true) = 'superadmin'
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);
DROP POLICY IF EXISTS users_delete ON users; CREATE POLICY users_delete ON users FOR DELETE USING (
    current_setting('app.session_role', true) = 'superadmin'
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);

-- 6. Row-Level Security on app_config
ALTER TABLE app_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_config FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS config_select ON app_config; CREATE POLICY config_select ON app_config FOR SELECT USING (true);
DROP POLICY IF EXISTS config_insert ON app_config; CREATE POLICY config_insert ON app_config FOR INSERT WITH CHECK (
    key NOT IN ('permissions', 'auth_config')
    OR current_setting('app.session_role', true) = 'superadmin'
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);
DROP POLICY IF EXISTS config_update ON app_config; CREATE POLICY config_update ON app_config FOR UPDATE USING (
    key NOT IN ('permissions', 'auth_config')
    OR current_setting('app.session_role', true) = 'superadmin'
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);
