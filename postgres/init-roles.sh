#!/bin/bash
# PurpleAssetOne — Post-init role configuration
# This script runs after init.sql and sets the pa1_app password from environment variables.

set -e

APP_PW="${DB_APP_PASSWORD:-pa1_app_changeme}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Set the app user password from environment
    ALTER ROLE pa1_app PASSWORD '${APP_PW}';

    -- Ensure grants are in place (idempotent)
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
        users, areas, equipment, repair_tickets, schedules,
        auth_sessions, auth_enrollments, equipment_groups, equipment_group_members,
        maintenance_schedules, maintenance_events, app_config
    TO pa1_app;
    GRANT SELECT ON audit_log TO pa1_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pa1_app;
EOSQL

echo "PurpleAssetOne: pa1_app role configured successfully"
