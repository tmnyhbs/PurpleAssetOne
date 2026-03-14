-- PurpleAssetOne Database Schema
-- Security-hardened: no seed users, RLS enabled, audit logging, least-privilege app role
-- Users are bootstrapped from environment variables by the backend on first startup.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "btree_gist";

-- ─────────────────────────────────────────
-- USERS & ROLES
-- ─────────────────────────────────────────
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username    TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('superadmin', 'admin', 'area_host', 'technician', 'authorizer', 'viewer', 'member')),
    full_name   TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    is_active   BOOLEAN DEFAULT TRUE,
    metadata    JSONB DEFAULT '{}',
    auth_provider VARCHAR(50) DEFAULT 'local',
    external_id   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS users_provider_external
    ON users(auth_provider, external_id) WHERE external_id IS NOT NULL;

-- ─────────────────────────────────────────
-- AREAS
-- ─────────────────────────────────────────
CREATE TABLE areas (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT UNIQUE NOT NULL,
    description TEXT,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- EQUIPMENT
-- ─────────────────────────────────────────
CREATE TABLE equipment (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    area_id         UUID REFERENCES areas(id) ON DELETE SET NULL,
    common_name     TEXT,
    make            TEXT NOT NULL,
    model           TEXT NOT NULL,
    serial_number   TEXT UNIQUE NOT NULL,
    build_date      DATE,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'under_repair', 'decommissioned')),
    schedulable     BOOLEAN DEFAULT false,
    attributes      JSONB DEFAULT '{}',
    attachments     JSONB DEFAULT '[]',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    version         INTEGER DEFAULT 1
);
CREATE INDEX idx_equipment_area ON equipment(area_id);
CREATE INDEX idx_equipment_status ON equipment(status);
CREATE INDEX idx_equipment_attributes ON equipment USING GIN(attributes);

-- ─────────────────────────────────────────
-- REPAIR TICKETS
-- ─────────────────────────────────────────
CREATE TABLE repair_tickets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    equipment_id    UUID NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    ticket_number   TEXT UNIQUE NOT NULL,
    opened_by       UUID REFERENCES users(id),
    assigned_to     UUID REFERENCES users(id),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'on_hold', 'closed')),
    priority        TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'critical')),
    title           TEXT NOT NULL,
    description     TEXT,
    opened_at       TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    work_log        JSONB DEFAULT '[]',
    parts_used      JSONB DEFAULT '[]',
    attachments     JSONB DEFAULT '[]',
    metadata        JSONB DEFAULT '{}',
    category        TEXT NOT NULL DEFAULT 'repair' CHECK (category IN ('repair', 'maintenance')),
    version         INTEGER DEFAULT 1
);
CREATE INDEX idx_tickets_equipment ON repair_tickets(equipment_id);
CREATE INDEX idx_tickets_status ON repair_tickets(status);
CREATE INDEX idx_tickets_assigned ON repair_tickets(assigned_to);
CREATE INDEX idx_tickets_category ON repair_tickets(category);

-- ─────────────────────────────────────────
-- SCHEDULING
-- ─────────────────────────────────────────
CREATE TABLE schedules (
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
CREATE INDEX idx_schedules_equipment ON schedules(equipment_id);
CREATE INDEX idx_schedules_time ON schedules(start_time, end_time);

-- ─────────────────────────────────────────
-- AUTH SESSIONS & ENROLLMENTS
-- ─────────────────────────────────────────
CREATE TABLE auth_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    equipment_ids   UUID[] DEFAULT '{}',
    authorizer_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    total_slots     INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_auth_sessions_time ON auth_sessions(start_time);

CREATE TABLE auth_enrollments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES auth_sessions(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    enrolled_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, user_id)
);

-- ─────────────────────────────────────────
-- EQUIPMENT GROUPS
-- ─────────────────────────────────────────
CREATE TABLE equipment_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    area_id     UUID REFERENCES areas(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE equipment_group_members (
    group_id    UUID NOT NULL REFERENCES equipment_groups(id) ON DELETE CASCADE,
    equipment_id UUID NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    sort_order  INTEGER DEFAULT 0,
    PRIMARY KEY (group_id, equipment_id)
);

-- ─────────────────────────────────────────
-- MAINTENANCE
-- ─────────────────────────────────────────
CREATE TABLE maintenance_schedules (
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

CREATE TABLE maintenance_events (
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
    ticket_id         UUID REFERENCES repair_tickets(id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_maint_events_due ON maintenance_events(due_date);
CREATE INDEX idx_maint_events_status ON maintenance_events(status);

-- ─────────────────────────────────────────
-- APP CONFIGURATION
-- ─────────────────────────────────────────
CREATE TABLE app_config (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_by  TEXT
);

-- ─────────────────────────────────────────
-- TRIGGERS: auto-update version + updated_at
-- ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    NEW.version = OLD.version + 1;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_equipment_updated BEFORE UPDATE ON equipment FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_tickets_updated   BEFORE UPDATE ON repair_tickets FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─────────────────────────────────────────
-- TICKET NUMBER SEQUENCE
-- ─────────────────────────────────────────
CREATE SEQUENCE ticket_seq START 1000;
CREATE OR REPLACE FUNCTION next_ticket_number()
RETURNS TEXT AS $$
BEGIN
    RETURN 'TKT-' || LPAD(nextval('ticket_seq')::TEXT, 6, '0');
END;
$$ LANGUAGE plpgsql;

-- ═════════════════════════════════════════
-- AUDIT LOG
-- ═════════════════════════════════════════
CREATE TABLE audit_log (
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
CREATE INDEX idx_audit_log_table ON audit_log(table_name);
CREATE INDEX idx_audit_log_record ON audit_log(record_id);
CREATE INDEX idx_audit_log_user ON audit_log(user_id);
CREATE INDEX idx_audit_log_time ON audit_log(created_at);

-- Audit trigger function (SECURITY DEFINER so app user can't write to audit_log directly)
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
    _key      TEXT;
BEGIN
    -- Read session context (set by the application per-request)
    _user_id := current_setting('app.current_user_id', true);
    _role    := current_setting('app.session_role', true);

    IF TG_OP = 'DELETE' THEN
        _old := to_jsonb(OLD);
        _record_id := COALESCE(_old->>'id', _old->>'key', '');
        INSERT INTO audit_log (table_name, record_id, operation, user_id, user_role, old_data)
        VALUES (TG_TABLE_NAME, _record_id, 'DELETE', _user_id, _role, _old);
        RETURN OLD;
    ELSIF TG_OP = 'INSERT' THEN
        _new := to_jsonb(NEW);
        _record_id := COALESCE(_new->>'id', _new->>'key', '');
        INSERT INTO audit_log (table_name, record_id, operation, user_id, user_role, new_data)
        VALUES (TG_TABLE_NAME, _record_id, 'INSERT', _user_id, _role, _new);
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        _old := to_jsonb(OLD);
        _new := to_jsonb(NEW);
        _record_id := COALESCE(_new->>'id', _new->>'key', '');
        -- Detect which fields changed
        _changed := ARRAY(
            SELECT key FROM jsonb_each(_new)
            WHERE NOT (_old ? key AND _old->key = _new->key)
        );
        -- Skip audit if nothing actually changed
        IF array_length(_changed, 1) IS NULL THEN RETURN NEW; END IF;
        -- Strip password_hash from audit data
        IF _old ? 'password_hash' THEN
            _old := _old - 'password_hash';
            _new := _new - 'password_hash';
        END IF;
        INSERT INTO audit_log (table_name, record_id, operation, user_id, user_role, old_data, new_data, changed_fields)
        VALUES (TG_TABLE_NAME, _record_id, 'UPDATE', _user_id, _role, _old, _new, _changed);
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Attach audit triggers to all data tables
CREATE TRIGGER audit_users               AFTER INSERT OR UPDATE OR DELETE ON users                FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_areas               AFTER INSERT OR UPDATE OR DELETE ON areas                FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_equipment           AFTER INSERT OR UPDATE OR DELETE ON equipment            FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_repair_tickets      AFTER INSERT OR UPDATE OR DELETE ON repair_tickets       FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_schedules           AFTER INSERT OR UPDATE OR DELETE ON schedules            FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_auth_sessions       AFTER INSERT OR UPDATE OR DELETE ON auth_sessions        FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_auth_enrollments    AFTER INSERT OR UPDATE OR DELETE ON auth_enrollments     FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_equipment_groups    AFTER INSERT OR UPDATE OR DELETE ON equipment_groups     FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_maint_schedules     AFTER INSERT OR UPDATE OR DELETE ON maintenance_schedules FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_maint_events        AFTER INSERT OR UPDATE OR DELETE ON maintenance_events   FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
CREATE TRIGGER audit_app_config          AFTER INSERT OR UPDATE OR DELETE ON app_config           FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();

-- ═════════════════════════════════════════
-- ROW-LEVEL SECURITY
-- ═════════════════════════════════════════

-- Users table: app user can read all (needed for login), but cannot
-- modify superadmin password_hash unless the current session role is superadmin
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;

CREATE POLICY users_select ON users FOR SELECT USING (true);
CREATE POLICY users_insert ON users FOR INSERT WITH CHECK (true);
CREATE POLICY users_update ON users FOR UPDATE USING (
    -- Superadmin rows can only be updated if current session is superadmin
    role != 'superadmin'
    OR current_setting('app.session_role', true) = 'superadmin'
    -- Also allow the bootstrap process (no role set yet)
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);
CREATE POLICY users_delete ON users FOR DELETE USING (
    current_setting('app.session_role', true) = 'superadmin'
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);

-- App config: protect permissions and auth_config from non-superadmin modification
ALTER TABLE app_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_config FORCE ROW LEVEL SECURITY;

CREATE POLICY config_select ON app_config FOR SELECT USING (true);
CREATE POLICY config_insert ON app_config FOR INSERT WITH CHECK (
    key NOT IN ('permissions', 'auth_config')
    OR current_setting('app.session_role', true) = 'superadmin'
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);
CREATE POLICY config_update ON app_config FOR UPDATE USING (
    key NOT IN ('permissions', 'auth_config')
    OR current_setting('app.session_role', true) = 'superadmin'
    OR COALESCE(current_setting('app.session_role', true), '') = ''
);

-- ═════════════════════════════════════════
-- LEAST-PRIVILEGE APP ROLE
-- ═════════════════════════════════════════
-- The pa1_app role is created by init-roles.sh with the password from env vars.
-- These grants give it data access but not schema modification or audit tampering.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pa1_app') THEN
        CREATE ROLE pa1_app LOGIN PASSWORD 'changeme_in_env';
    END IF;
END $$;

-- Data tables: full DML
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    users, areas, equipment, repair_tickets, schedules,
    auth_sessions, auth_enrollments, equipment_groups, equipment_group_members,
    maintenance_schedules, maintenance_events, app_config
TO pa1_app;

-- Audit log: read-only (writes happen via SECURITY DEFINER trigger)
GRANT SELECT ON audit_log TO pa1_app;

-- Sequences
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pa1_app;

-- No TRUNCATE, no DDL, no direct audit_log writes
-- RLS is enforced because pa1_app is not the table owner

-- ═════════════════════════════════════════
-- DEFAULT CONFIGURATION (no user data)
-- ═════════════════════════════════════════

-- Seed areas
INSERT INTO areas (id, name, description, metadata) VALUES
    ('2f4e26d2-1fae-40a1-841c-00e5043bb363', '2D Printing',   '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('5945e850-fbf2-4b6a-96c8-abf559e95970', '3D Printing',   '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('408a6c9e-152b-4e91-a06f-7e1c785f5217', '3D SLA Printing','', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('2c72f0f5-8dfc-450e-9810-a1273fb4acb5', 'Arts',          '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('74eb8072-0114-4d38-96a4-0c5e2ccc344e', 'CNC',           '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('2df0660f-3ffc-48ee-931f-af1f8e1bea9f', 'Cold Metals',   '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('589894fa-c44c-4fce-97bc-b4babf5f4f24', 'Electronics',   '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('951f8eb1-4815-4c48-8b8f-642d79281566', 'Hot Metals',    '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('c85d30de-deb7-402a-9974-bc2f22d0f8fb', 'Small Metals',  '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}'),
    ('abd1c2e4-e232-4ffd-8459-6102e4741d6b', 'Woodshop',      '', '{"website":"","host_name":"","host_contact":"","email":"","discord":""}');

INSERT INTO app_config (key, value) VALUES
('theme', '{
  "font": "system",
  "colors": {
    "accent":"#f5a623","primary":"#0d6efd","bg":"#f0f2f5",
    "surface":"#ffffff","sidebar_bg":"#ffffff","text":"#212529",
    "success":"#198754","warning":"#ffc107","danger":"#dc3545"
  }
}'),
('dashboard', '{
  "tiles": [
    {"id":"total_equipment",  "label":"Total Equipment","stat_key":"total_equipment", "color":"#f5a623","visible":true, "custom":false},
    {"id":"active_equipment", "label":"Active",         "stat_key":"active_equipment","color":"#198754","visible":true, "custom":false},
    {"id":"under_repair",     "label":"In Repair",      "stat_key":"under_repair",    "color":"#ffc107","visible":true, "custom":false},
    {"id":"open_tickets",     "label":"Open Tickets",   "stat_key":"open_tickets",    "color":"#dc3545","visible":true, "custom":false},
    {"id":"critical_tickets", "label":"Critical / High","stat_key":"critical_tickets","color":"#dc3545","visible":true, "custom":false},
    {"id":"total_areas",      "label":"Areas",          "stat_key":"total_areas",     "color":"#0d6efd","visible":true, "custom":false}
  ],
  "sections": {
    "area_breakdown":   {"visible":true,"label":"Area Breakdown"},
    "open_tickets_tbl": {"visible":true,"label":"Open Tickets"}
  }
}'),
('templates', '{
  "equipment":{"fields":[
    {"key":"common_name",  "label":"Common Name",          "visible":true,"required":false},
    {"key":"make",         "label":"Make",                 "visible":true,"required":true},
    {"key":"model",        "label":"Model",                "visible":true,"required":true},
    {"key":"serial_number","label":"Serial Number",        "visible":true,"required":true},
    {"key":"build_date",   "label":"Build Date",           "visible":true,"required":false},
    {"key":"area_id",      "label":"Area",                 "visible":true,"required":false},
    {"key":"status",       "label":"Status",               "visible":true,"required":false},
    {"key":"attributes",   "label":"Additional Attributes","visible":true,"required":false}
  ],"default_attributes":[]},
  "tickets":{"fields":[
    {"key":"equipment_id","label":"Equipment",  "visible":true,"required":true},
    {"key":"title",       "label":"Title",      "visible":true,"required":true},
    {"key":"description", "label":"Description","visible":true,"required":false},
    {"key":"priority",    "label":"Priority",   "visible":true,"required":false},
    {"key":"assigned_to", "label":"Assigned To","visible":true,"required":false},
    {"key":"status",      "label":"Status",     "visible":true,"required":false},
    {"key":"work_log",    "label":"Work Log",   "visible":true,"required":false}
  ]},
  "areas":{"fields":[
    {"key":"name",        "label":"Area Name",      "visible":true,"required":true},
    {"key":"description", "label":"Description",    "visible":true,"required":false},
    {"key":"website",     "label":"Website",        "visible":true,"required":false},
    {"key":"host_name",   "label":"Area Host",      "visible":true,"required":false},
    {"key":"host_contact","label":"Host Contact",   "visible":true,"required":false},
    {"key":"email",       "label":"Email",          "visible":true,"required":false},
    {"key":"discord",     "label":"Discord Channel","visible":true,"required":false}
  ]},
  "users":{"fields":[
    {"key":"username", "label":"Username",    "visible":true,"required":true},
    {"key":"full_name","label":"Full Name",   "visible":true,"required":false},
    {"key":"password", "label":"Password",    "visible":true,"required":true},
    {"key":"role",     "label":"Role",        "visible":true,"required":false},
    {"key":"is_active","label":"Active Status","visible":true,"required":false}
  ]}
}');
