# PurpleAssetOne

A self-hosted workshop asset and repair management system. Track equipment, manage repair tickets, schedule machine time, control authorizations, run maintenance schedules, configure notifications with Discord and webhook delivery, and administer users -- all from a single responsive web portal with granular role and permission controls. Includes an optional Discord bot for creating and managing repair tickets directly from Discord.

---

## Table of Contents

- [Stack](#stack)
- [Quick Start](#quick-start)
- [Initial Login](#initial-login)
- [Roles & Permissions](#roles--permissions)
- [Features](#features)
- [Discord Bot](#discord-bot)
- [Notifications](#notifications)
- [Environment Variables](#environment-variables)
- [Persistent Storage](#persistent-storage)
- [Updating an Existing Installation](#updating-an-existing-installation)
- [Reverse Proxy](#reverse-proxy)
- [Authentication Providers](#authentication-providers)
- [File Uploads](#file-uploads)
- [Security Architecture](#security-architecture)
- [API Overview](#api-overview)
- [Data Model](#data-model)
- [Development](#development)
- [Project Structure](#project-structure)

---

## Stack

| Container | Image | Purpose |
|---|---|---|
| `purpleassetone_db` | `postgres:16-alpine` | Primary database |
| `purpleassetone_api` | Custom (Python 3.12 + FastAPI) | REST API backend |
| `purpleassetone_nginx` | Custom (Nginx alpine) | Frontend portal + reverse proxy |
| `purpleassetone_minio` | `minio/minio:latest` | Local S3-compatible file storage |
| `purpleassetone_discord` | Custom (Python 3.12 + discord.py) | Discord bot (optional, via `--profile discord`) |

**Default port map:**

| Port | Service |
|---|---|
| `8080` | Web portal (nginx) |
| `8000` | API (direct access, optional) |
| `9000` | MinIO S3 API |
| `9001` | MinIO admin console |

---

## Quick Start

### 1. Clone and configure

```bash
git clone <your-repo-url>
cd PurpleAssetOne
cp example.env .env
nano .env   # Set STORAGE, DB_PASSWORD, DB_APP_PASSWORD, SECRET_KEY, INIT_SUPERADMIN_PASSWORD
```

### 2. Create persistent data directory

```bash
mkdir -p /path/to/your/storage
```

Set the `STORAGE` variable in `.env` to this path. All container data (database, MinIO files, bot state) is stored under this directory.

### 3. Build and start

```bash
docker compose up -d --build
```

The portal is available at `http://<host-ip>:8080`.

### 4. (Optional) Start the Discord bot

```bash
docker compose --profile discord up -d --build
```

See the [Discord Bot](#discord-bot) section for setup details.

---

## Initial Login

No users are seeded in the database. On first startup, the backend automatically creates a single superadmin account from environment variables:

```env
INIT_SUPERADMIN_USER=superadmin
INIT_SUPERADMIN_PASSWORD=change_this_immediately
```

Set these in `.env` before first boot. After logging in, change the password via the web UI and create additional users as needed.

---

## Roles & Permissions

### Role Hierarchy

Roles are ordered from least to most privileged:

```
Viewer  <  Member  <  Authorizer  <  Technician  <  Area Host  <  Admin  <  Super Admin
```

### 40 Named Permissions

All access control -- both API endpoints and UI elements -- is gated by named permissions. Super Admin always has all permissions.

| Permission | Description |
|---|---|
| `equipment.view` | View equipment list and details |
| `equipment.create` | Add new equipment |
| `equipment.edit` | Edit existing equipment |
| `equipment.delete` | Delete equipment |
| `equipment.export` | Export equipment data (CSV/JSON) |
| `tickets.view` | View repair tickets |
| `tickets.create` | Create new tickets |
| `tickets.edit` | Edit ticket details and status |
| `tickets.worklog` | Add work log entries |
| `tickets.delete` | Delete tickets (destructive) |
| `areas.view` | View areas |
| `areas.create` | Create new areas |
| `areas.edit` | Edit area info |
| `areas.delete` | Delete areas |
| `scheduling.view` | View schedule and calendar |
| `scheduling.book` | Create own bookings |
| `scheduling.manage` | Manage all bookings (cancel, override) |
| `auth_sessions.view` | View authorization sessions |
| `auth_sessions.create` | Create auth sessions |
| `auth_sessions.manage` | Manage sessions and enrollments |
| `groups.view` | View equipment groups |
| `groups.manage` | Create, edit, and delete groups |
| `maintenance.view` | View maintenance schedules and events |
| `maintenance.create` | Create maintenance schedules |
| `maintenance.edit` | Edit maintenance schedules |
| `maintenance.complete` | Start and complete maintenance events |
| `maintenance.manage` | Full maintenance management (delete schedules) |
| `users.view` | View user list |
| `users.create` | Create new users |
| `users.edit` | Edit user profiles and roles |
| `users.delete` | Delete users (destructive) |
| `system.settings` | Access system settings menu |
| `system.users` | Manage users panel |
| `system.modules` | Toggle modules on/off |
| `system.templates` | Edit field templates |
| `system.dashboard` | Customize dashboard |
| `system.branding` | Edit branding and theme |
| `system.export` | Export and import data |
| `system.notifications` | Configure notification channels and events |
| `system.permissions` | Manage permissions (superadmin only) |
| `system.auth_config` | Configure authentication providers |

### Default Role Capabilities

| Permission | Viewer | Member | Authorizer | Technician | Area Host | Admin | Super Admin |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| View equipment | x | x | x | x | x | x | x |
| Create equipment | | | | x | x | x | x |
| Edit equipment | | | | x | x | x | x |
| Delete equipment | | | | | | x | x |
| View tickets | x | x | x | x | x | x | x |
| Create tickets | | x | x | x | x | x | x |
| Edit tickets | | | | x | x | x | x |
| Add work log entries | | | | x | x | x | x |
| Delete tickets | | | | | | | x |
| View areas | x | x | x | x | x | x | x |
| Create / edit areas | | | | x | x | x | x |
| Delete areas | | | | | | x | x |
| View schedule | x | x | x | x | x | x | x |
| Book own time slots | | x | x | x | x | x | x |
| Manage all bookings | | | | x | x | x | x |
| View auth sessions | x | x | x | x | x | x | x |
| Create auth sessions | | | x | x | x | x | x |
| Manage auth sessions | | | x | | x | x | x |
| View equipment groups | x | x | x | x | x | x | x |
| Manage equipment groups | | | | x | x | x | x |
| View maintenance | | | | x | x | x | x |
| Create maintenance | | | | | x | x | x |
| Complete maintenance | | | | x | x | x | x |
| Manage maintenance | | | | | x | x | x |
| View users | | | | | x | x | x |
| Create / edit users | | | | | | x | x |
| Delete users | | | | | | | x |
| Access system settings | | | | x | x | x | x |
| Users / Modules / Templates panels | | | | | | x | x |
| Dashboard / Branding / Export panels | | | | | | x | x |
| Notifications panel | | | | | x | x | x |
| Permissions panel | | | | | | | x |
| Authentication config panel | | | | | | | x |

### Customizing Permissions

The default table above is a starting point. A **Super Admin** can:

- **Adjust any role's permissions** via Settings > Permissions > Role Permissions. Changes are saved to the database and take effect immediately without a restart.
- **Grant or deny individual permissions per user** via Settings > Permissions > User Overrides. User-level overrides stack on top of their role's grants -- useful for exceptions in either direction.
- **Reset any role** back to its built-in defaults at any time.

Permission changes are enforced on both the API (every endpoint is gated by a named permission) and the UI (buttons, panels, and menu items are shown or hidden accordingly).

---

## Features

### Equipment Management
- Full equipment inventory with make, model, serial number, build date, and status tracking
- Area assignment, location description, and location image attachment
- Custom per-equipment attributes (key/value pairs)
- File attachments (images, PDFs, documents) stored in MinIO/S3
- Equipment groups for organizing related machines
- Optimistic locking (version field) to prevent conflicting concurrent edits
- Grid and list views; filter by area, status, or search
- Inline upcoming maintenance display and quick-schedule from the equipment detail panel

### Repair Tickets
- Linked to equipment; ticket numbers auto-generated
- Priority levels: critical, high, normal, low
- Status workflow: open > in-progress > on-hold > closed
- Work log entries with action, notes, parts used, and per-entry attachments
- Assignee field (any technician-or-above user)
- Ticket attachments separate from work log attachments
- Category tracking: repair (manual) or maintenance (auto-created from maintenance events)
- Full row-clickable ticket table

### Maintenance Calendar
- Recurring maintenance schedules tied to individual equipment or equipment groups
- Recurrence types: days, weeks, months, or years
- Completion-based recurrence -- next event is created only when the current one is completed or skipped
- Five view modes: Day, Week, Month, List, and Schedules (management)
- Status tracking: pending, in-progress, completed, skipped, overdue
- Auto-ticket integration: starting a maintenance event creates a linked repair ticket with category "maintenance"; completing the event auto-closes the ticket and restores equipment status
- Checklist support, priority levels, estimated time, and assignee per schedule
- Schedule maintenance directly from the equipment detail panel

### Scheduling
- Shared calendar engine with Day, Week, and Month views
- Per-equipment bookings with time slots and conflict prevention (GIST exclusion constraint)
- Current-time indicator, business-hours focus, mobile-first auto-switching
- Members can book their own time; technicians and above can manage all bookings

### Authorization Sessions
- Authorizer-led sessions for approving equipment use
- Sign-up slots with enrollment and unenrollment
- Session management by authorizers and admins

### Areas
- Named areas (rooms, zones, buildings) with description, contact, website, Discord channel
- Equipment filtered by area in sidebar and area detail panel
- Area-level ticket summary view

### Dashboard
- Configurable stat tiles (total equipment, active, in repair, open tickets, critical tickets, areas)
- Custom tiles: stat counter, raw HTML, Markdown, or sandboxed JavaScript
- Each custom tile has an independent size (small, medium, large, full-width)
- Configurable sections: area breakdown table, open tickets preview

### User Profiles
- Each user has a profile page with full name, email, Discord handle, and notes
- Users can edit their own profile and change their own password
- Admins can edit any profile and reset passwords

### System Settings (admin panel)

| Panel | Who can access | What it does |
|---|---|---|
| Users | Admin+ | Create, edit, enable/disable, and delete users |
| Modules | Admin+ | Toggle entire feature modules (equipment, scheduling, authorizations) on/off globally |
| Notifications | Admin+ | Configure notification delivery channels, per-event toggles, webhooks, and role routing |
| Export & Import | Admin+ | Full JSON snapshot export; per-entity CSV export/import; users JSON export/import |
| JSON Templates | Admin+ | Customize field labels and visibility for equipment, tickets, areas, users, and profile forms |
| Dashboard | Admin+ | Reorder/show/hide stat tiles; add custom content tiles |
| Branding | Admin+ | App name, icon, accent colors, favicon, GitHub link visibility |
| Permissions | Super Admin | Role permission matrix; per-user grant/deny overrides |
| Authentication | Super Admin | Configure SSO and external auth providers |
| API Documentation | Super Admin | Interactive reference for all 77+ API endpoints |

### Branding & Theming
- Customizable app name, Bootstrap icon, and favicon (dynamically rendered from icon + accent color)
- Full color scheme: primary, accent, header, sidebar, background, text, button text
- Font family selection
- GitHub repository link visibility toggle (configurable URL)
- All settings persisted in database; applied on load without a restart

### Mobile Support
- Fully responsive layout down to 320 px wide
- Offcanvas panels go full-width on phones
- Modals go full-screen on phones
- iOS safe-area insets (notch and home indicator) handled
- PWA meta tags: installable as a home screen app on iOS and Android
- Dynamic `theme-color` meta tag follows the configured header color

---

## Discord Bot

PurpleAssetOne includes an optional Discord bot that runs in its own container and communicates with the PA1 API. It has no direct database access, making it portable and safe to deploy independently.

### Capabilities

| Command | Description |
|---|---|
| `/repair-ticket` | Create a repair ticket. Equipment is selected via live autocomplete, then a modal collects title, description, and priority. |
| `/addnote` | Add a work log entry to any ticket by ticket number. |
| `/tickets` | List recent tickets filtered by status, with priority, equipment, and assignee. |
| `/ticketinfo` | View full ticket details including the last five work log entries. |
| `/equipment` | Search equipment by name, make, model, or serial number. |

### Ticket Creation Flow

1. User types `/repair-ticket` and selects equipment from the autocomplete dropdown.
2. A modal collects the title, description, and priority.
3. The bot creates the ticket via the PA1 API and posts a rich embed in the channel.
4. A discussion thread is automatically created on the embed message.
5. Any message posted in the thread is synced as a work log entry on the linked ticket.

### Thread Sync

Messages posted in a linked thread are automatically forwarded to the repair ticket's work log. The bot adds a reaction to each synced message to confirm delivery. Very short messages and slash commands are ignored.

Thread-to-ticket mappings are persisted to a JSON file in the bot's data directory and survive restarts.

### Setup

**1. Create a Discord Application**

Go to https://discord.com/developers/applications and create a new application. Under the Bot section, enable the **MESSAGE CONTENT INTENT** under Privileged Gateway Intents.

**2. Invite the Bot**

Generate an OAuth2 invite URL with:
- Scopes: `bot`, `applications.commands`
- Permissions: View Channels, Send Messages, Create Public Threads, Send Messages in Threads, Read Message History, Add Reactions, Use Slash Commands

**3. Create a PA1 Service Account**

In the PurpleAssetOne web UI, create a user for the bot (for example, username `discord-bot` with role `technician` or higher). This account is used by the bot to authenticate with the API.

**4. Configure Environment Variables**

Add the following to your `.env` file:

```env
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_GUILD_IDS=123456789012345678
PA1_BOT_USER=discord-bot
PA1_BOT_PASSWORD=your_bot_account_password
```

Set `DISCORD_GUILD_IDS` to your server ID for instant slash command registration. Multiple IDs can be comma-separated. Leave blank for global registration (takes up to one hour to propagate).

**5. Start the Bot**

```bash
docker compose --profile discord up -d --build
```

The bot only starts when the `discord` profile is active. The core stack is unaffected without it.

### Configuration

All slash command names, descriptions, modal labels, thread behavior, embed appearance, and cache settings are customizable via a YAML configuration file.

The bot ships with a bundled `config.yaml` containing sensible defaults. To customize, create a `config.yaml` in the bot's data directory:

```bash
nano $STORAGE/discord-bot/config.yaml
```

Only the keys you want to override need to be present -- missing keys fall back to the bundled defaults. After editing, restart the bot:

```bash
docker compose --profile discord restart discord-bot
```

**Example: rename the ticket creation command**

```yaml
commands:
  create_ticket:
    name: new-ticket
    description: Open a new repair ticket
```

**Configurable sections:**

| Section | What it controls |
|---|---|
| `commands` | Slash command names, descriptions, and parameter labels |
| `modal` | Ticket creation modal title, field labels, and placeholders |
| `thread_sync` | Enable/disable, minimum message length, reaction emoji, welcome message template |
| `auto_thread` | Enable/disable thread creation, archive duration |
| `equipment_cache` | Refresh interval and maximum items |
| `embeds` | Footer text, priority colors and emoji, status colors, equipment status emoji |

---

## Notifications

PurpleAssetOne includes a server-side notification system that fires events when data changes and delivers them to configured channels. Configuration is managed via Settings > Notifications and stored in the `app_config` database table under the key `"notifications"`.

### Notification Events

Events are fired automatically by the API when actions occur:

| Event | Trigger |
|---|---|
| `equipment.created` | New equipment added |
| `equipment.modified` | Equipment record updated |
| `area.created` | New area added |
| `area.modified` | Area record updated |
| `ticket.created` | New repair ticket opened |
| `ticket.modified` | Ticket updated (status, assignee, etc.) |
| `ticket.closed` | Ticket status set to closed |
| `schedule.booked` | New time slot booking created |
| `schedule.reminder` | Reminder before a scheduled booking (configurable lead time) |
| `auth_session.created` | New authorization session posted |
| `auth_session.modified` | Authorization session updated |
| `auth_session.enrollment` | User enrolled in an authorization session |
| `auth_session.fill_alert` | Fill-rate alert for sessions with open slots approaching start time |
| `auth_session.reminder` | Reminder before an authorization session (configurable lead time) |
| `maintenance.created` | New maintenance schedule created |
| `maintenance.due` | Maintenance event started (moved to in-progress) |
| `maintenance.completed` | Maintenance event completed |
| `maintenance.overdue` | Maintenance event past due |
| `maintenance.summary` | Periodic maintenance summary |

### Delivery Channels

| Channel | Description | Status |
|---|---|---|
| **Email** | SMTP-based email delivery (host, port, TLS, credentials) | Config UI ready -- delivery integration pending |
| **Push** | Provider-based push notifications (ntfy, pushover, gotify) | Config UI ready -- delivery integration pending |
| **Webhooks** | HTTP POST to configured URLs with event payloads | Live delivery via httpx |

### Webhooks

Webhooks are the primary active delivery channel. Each webhook has:

- **Type** -- Generic or Discord
- **URL** -- the endpoint to POST to
- **Enabled toggle** -- active or paused
- **Event filter** -- all events or a specific subset
- **Per-webhook test button** -- send a test payload to verify delivery before saving

#### Generic Webhooks

Generic webhooks POST a JSON payload:

```json
{
  "event": "ticket.created",
  "timestamp": "2026-03-12T18:00:00+00:00",
  "data": {
    "ticket_id": "abc-123",
    "title": "Motor overheating",
    "priority": "high",
    "by": "tech1"
  }
}
```

If a **signing secret** is configured, the payload is signed with HMAC-SHA256 and the signature is sent in the `X-Signature` header as `sha256=<hex-digest>`. Use this to verify the authenticity of incoming payloads in your receiving service.

#### Discord Webhooks

Discord webhooks send rich embeds directly to a Discord channel using the Discord webhook API. Features:

- **Color-coded embeds** by event group -- blurple for Equipment, green for Scheduling, yellow for Authorizations, orange for Maintenance, fuchsia for test events
- **Emoji icons** per event type
- **Structured fields** -- payload data rendered as inline embed fields
- **Custom bot identity** -- override the bot username and avatar URL per webhook
- **Real delivery** -- posts to the Discord webhook URL with `?wait=true` for error feedback

**Discord webhook setup:**

1. In your Discord server, go to **Channel Settings > Integrations > Webhooks > New Webhook**
2. Copy the webhook URL (format: `https://discord.com/api/webhooks/<id>/<token>`)
3. In PurpleAssetOne, go to Settings > Notifications > Webhooks > **Add Discord Webhook**
4. Paste the URL, optionally set the bot username and avatar
5. Select which events to deliver, then **Save Webhooks**
6. Click the test button on the webhook card to verify delivery

### Events Matrix

The **Events** tab provides a matrix of all notification events by channel (email, push, webhook). Toggle individual cells to control which channels fire for each event type.

### Role Routing

The **Role Routing** tab controls which roles receive each notification type. This determines the audience for email and push delivery. Webhooks fire regardless of role routing. Super Admin always receives all notifications.

### Notification Config Structure

All notification config is stored as a single JSONB document in `app_config` under the key `"notifications"`:

```json
{
  "channels": {
    "email":   { "enabled": false, "config": { "smtp_host": "...", "..." : "..." } },
    "push":    { "enabled": false, "config": { "provider": "ntfy", "..." : "..." } },
    "webhook": { "enabled": true,  "config": {} }
  },
  "webhooks": [
    {
      "name": "My Discord Channel",
      "url": "https://discord.com/api/webhooks/...",
      "type": "discord",
      "enabled": true,
      "events": ["*"],
      "discord_username": "PurpleAssetOne",
      "discord_avatar_url": ""
    },
    {
      "name": "Ops Webhook",
      "url": "https://ops.example.com/hooks/pa1",
      "type": "generic",
      "enabled": true,
      "events": ["ticket.created", "ticket.closed"],
      "secret": "my-signing-secret"
    }
  ],
  "events": {
    "equipment.created": { "email": false, "push": false, "webhook": true },
    "ticket.created":    { "email": true,  "push": true,  "webhook": true }
  },
  "role_routing": {
    "ticket.created": ["technician", "admin"],
    "equipment.created": ["admin"]
  }
}
```

---

## Environment Variables

Copy `example.env` to `.env` and configure:

```env
# Base directory for all persistent data
STORAGE=/path/to/your/storage

# Database owner password (used for migrations and schema management)
DB_PASSWORD=your_secure_password

# App user password (least-privilege -- used for all API operations)
DB_APP_PASSWORD=your_app_password

# Initial superadmin credentials (created on first startup if no users exist)
INIT_SUPERADMIN_USER=superadmin
INIT_SUPERADMIN_PASSWORD=change_this_immediately

# API JWT secret -- generate with: openssl rand -hex 32
SECRET_KEY=your_secret_key

# S3 / File Storage (default: local MinIO)
S3_ENDPOINT_URL=http://minio:9000
S3_ACCESS_KEY_ID=purpleassetone
S3_SECRET_ACCESS_KEY=your_minio_password
S3_BUCKET=purpleassetone
S3_PUBLIC_URL=        # Optional: CDN or public-facing URL prefix

# Discord Bot (optional -- only needed with --profile discord)
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_GUILD_IDS=123456789012345678
PA1_BOT_USER=discord-bot
PA1_BOT_PASSWORD=your_bot_account_password
```

### Using AWS S3 instead of MinIO

Set the following in `.env` and remove the `minio` service from `docker-compose.yml` (also remove its `depends_on` from the backend service):

```env
S3_ENDPOINT_URL=
S3_ACCESS_KEY_ID=your_aws_access_key
S3_SECRET_ACCESS_KEY=your_aws_secret_key
S3_BUCKET=your_bucket_name
S3_PUBLIC_URL=https://your_bucket_name.s3.amazonaws.com
```

---

## Persistent Storage

All data survives container rebuilds via bind mounts under the `STORAGE` path:

| Host path | Container path | Contents |
|---|---|---|
| `$STORAGE/postgres` | `/var/lib/postgresql/data` | Database |
| `$STORAGE/minio` | `/data` | Uploaded files |
| `$STORAGE` | `/appdata` | Backend application data |
| `$STORAGE/discord-bot` | `/data` | Bot state (thread map, user config) |

---

## Updating an Existing Installation

Run the migration script against the live database before restarting. This is safe to run multiple times -- all statements are idempotent.

```bash
cat postgres/migrate.sql | docker exec -i purpleassetone_db psql -U purpleassetone purpleassetone
docker compose build --no-cache backend nginx && docker compose up -d
```

### What the latest migration adds

- Maintenance tables (`maintenance_schedules`, `maintenance_events`)
- Area Host role in the role constraint
- Ticket `category` column (`repair` or `maintenance`)
- Ticket-to-maintenance-event linking (`ticket_id` on `maintenance_events`)
- Audit log table and `SECURITY DEFINER` triggers on all data tables
- Least-privilege `pa1_app` database role with DML-only grants
- Row-Level Security on `users` and `app_config` tables
- `auth_provider` and `external_id` columns on `users` for SSO support

---

## Reverse Proxy

### SWAG / nginx

```nginx
server {
    listen 443 ssl;
    server_name purpleassetone.yourdomain.com;

    include /config/nginx/ssl.conf;

    location / {
        proxy_pass http://purpleassetone_nginx:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 110m;
    }
}
```

Ensure SWAG and `purpleassetone_nginx` share a Docker network.

### Egress for Webhook Delivery

If your Docker network uses an egress proxy or firewall, ensure the backend container can reach outbound destinations for webhook delivery. For Discord webhooks, `discord.com` must be reachable on port 443.

### Trusted Header Auth (Authelia / Authentik forward-auth)

If you use Authelia or Authentik in front of PurpleAssetOne and want it to accept the authenticated user from a header:

1. Go to Settings > Authentication > select **Trusted Header Auth**
2. Configure `Remote-User` as the username header and set trusted proxy CIDRs
3. Optionally map groups (from `Remote-Groups`) to PA1 roles
4. Enable **Auto-provision** to create local user records on first login
5. Save, then restart the backend

> The auth provider UI stores configuration in the database. The actual middleware that reads the headers and issues a session token requires a backend restart to activate, and is designed as an integration point for a future backend middleware sprint.

---

## Authentication Providers

PurpleAssetOne is designed to support multiple authentication backends. The configuration UI and database schema are fully in place. The following providers are configurable via Settings > Authentication:

| Provider | Description | Status |
|---|---|---|
| **Local** | Built-in username/password | Active |
| **OIDC / OAuth2** | Authentik, Authelia, Azure B2C, Okta, any OIDC issuer | Config ready -- middleware integration pending |
| **LDAP / Active Directory** | Standard LDAP bind authentication with group-to-role mapping | Config ready -- middleware integration pending |
| **SAML 2.0** | Azure AD, ADFS, Okta | Config ready -- middleware integration pending |
| **Trusted Header** | Reverse-proxy forward-auth (Authelia, Authentik) | Config ready -- middleware integration pending |

All providers support:
- **Role mapping** -- map OIDC claims, LDAP groups, or SAML attributes to PA1 roles via a JSON map
- **Auto-provisioning** -- optionally create a local user record on first external login
- **External ID tracking** -- `external_id` stored per user to handle username changes at the IdP

---

## File Uploads

**Supported types:** images (JPEG, PNG, GIF, WebP, SVG), video (MP4, MOV, WebM), PDF, Word, Excel, plain text, CSV.

**Maximum size:** 100 MB per file.

Files are stored in MinIO (or S3) and proxied through nginx at `/files/`. Attachments can be added to:
- Equipment records
- Repair tickets
- Individual work log entries
- Area records

---

## Security Architecture

### Least-Privilege Database Role

The FastAPI backend connects to PostgreSQL as `pa1_app`, a restricted role with only SELECT, INSERT, UPDATE, and DELETE on data tables. It has no TRUNCATE, no DDL, and no direct write access to the audit log. A separate owner connection (`DATABASE_OWNER_URL`) is used only for the superadmin bootstrap on first startup.

### Row-Level Security

RLS is enabled and forced on sensitive tables:

- **users** -- superadmin rows can only be updated or deleted when the session role is superadmin
- **app_config** -- the `permissions` and `auth_config` keys can only be modified by superadmin sessions

RLS is enforced because the app connects as `pa1_app`, not the table owner.

### Audit Logging

A `SECURITY DEFINER` trigger on all 11 data tables records every INSERT, UPDATE, and DELETE operation. Each audit entry includes:

- The table name and record ID
- The operation type
- The user ID and role of the acting user
- Full JSONB snapshots of old and new data
- An array of changed field names (for updates)
- Timestamp

User context is set per-request via `set_config()` session variables, read by the trigger via `current_setting()`. The `password_hash` field is automatically stripped from audit snapshots. No-op updates (where nothing actually changed) are skipped.

The audit log is queryable via `GET /api/audit-log` (superadmin only) with filters for table, record, user, and operation type.

---

## API Overview

The API runs at port 8000 and is also accessible through nginx at `/api/`. All endpoints except `/api/auth/token`, `/api/config`, `/api/stats`, and `/health` require a Bearer token. Full interactive documentation is available in the web UI under Settings > API Documentation (superadmin only).

### Auth
| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/token` | Login; returns JWT + effective permissions list |
| `GET` | `/api/auth/me` | Current user info |

### Users
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/users` | `users.view` |
| `POST` | `/api/users` | `users.create` |
| `PATCH` | `/api/users/{id}` | `users.edit` |
| `DELETE` | `/api/users/{id}` | Superadmin only |
| `GET/PATCH` | `/api/users/me` | Authenticated |
| `PATCH` | `/api/users/me/password` | Authenticated |
| `GET/PATCH` | `/api/users/{id}/profile` | `users.view` / `users.edit` |
| `PATCH` | `/api/users/{id}/password` | `users.edit` |

### Equipment
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/equipment` | Public (filtered by `area_id`, `status`, `search`) |
| `POST` | `/api/equipment` | `equipment.create` |
| `GET` | `/api/equipment/{id}` | Public |
| `PATCH` | `/api/equipment/{id}` | `equipment.edit` (optimistic locking via `version`) |
| `DELETE` | `/api/equipment/{id}` | `equipment.delete` |

### Repair Tickets
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/tickets` | Public (filtered by `equipment_id`, `assigned_to`, `status`, `priority`) |
| `POST` | `/api/tickets` | `tickets.create` |
| `GET` | `/api/tickets/{id}` | Public |
| `PATCH` | `/api/tickets/{id}` | `tickets.edit` (optimistic locking via `version`) |
| `DELETE` | `/api/tickets/{id}` | `tickets.delete` |
| `POST` | `/api/tickets/{id}/worklog` | `tickets.worklog` |

### Areas
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/areas` | Public |
| `POST` | `/api/areas` | `areas.create` |
| `GET` | `/api/areas/{id}` | Public |
| `PATCH` | `/api/areas/{id}` | `areas.edit` |
| `DELETE` | `/api/areas/{id}` | `areas.delete` |

### Equipment Groups
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/equipment-groups` | Authenticated |
| `POST` | `/api/equipment-groups` | `groups.manage` |
| `PATCH` | `/api/equipment-groups/{id}` | `groups.manage` |
| `DELETE` | `/api/equipment-groups/{id}` | `groups.manage` |

### Scheduling
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/schedules` | Authenticated (filtered by `equipment_id`, `from_time`, `to_time`) |
| `POST` | `/api/schedules` | Authenticated (booking validation + conflict check) |
| `DELETE` | `/api/schedules/{id}` | Owner, admin, or superadmin |

### Authorization Sessions
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/auth-sessions` | Authenticated (filtered by `equipment_id`, `from_time`, `to_time`) |
| `POST` | `/api/auth-sessions` | Authorizer+ |
| `GET` | `/api/auth-sessions/{id}` | Authenticated |
| `PATCH` | `/api/auth-sessions/{id}` | Session authorizer, admin, or superadmin |
| `DELETE` | `/api/auth-sessions/{id}` | Session authorizer, admin, or superadmin |
| `POST` | `/api/auth-sessions/{id}/enroll` | Authenticated |
| `DELETE` | `/api/auth-sessions/{id}/enroll` | Authenticated (self-unenroll) |

### Maintenance
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/maintenance/schedules` | `maintenance.view` |
| `POST` | `/api/maintenance/schedules` | `maintenance.create` |
| `PATCH` | `/api/maintenance/schedules/{id}` | `maintenance.edit` |
| `DELETE` | `/api/maintenance/schedules/{id}` | `maintenance.manage` |
| `GET` | `/api/maintenance/events` | `maintenance.view` (filtered by `schedule_id`, `equipment_id`, `status`, date range) |
| `PATCH` | `/api/maintenance/events/{id}` | `maintenance.complete` |
| `GET` | `/api/maintenance/summary` | `maintenance.view` |

### Permissions
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/permissions/defs` | Authenticated |
| `PUT` | `/api/permissions/roles` | Superadmin |
| `GET/PUT` | `/api/permissions/users/{id}` | Superadmin |
| `POST` | `/api/permissions/reset-role/{role}` | Superadmin |

### Auth Configuration
| Method | Path | Permission |
|---|---|---|
| `GET/PUT` | `/api/auth-config` | Superadmin |

### Notifications
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/notifications-config` | `system.notifications` |
| `PUT` | `/api/notifications-config` | `system.notifications` |
| `POST` | `/api/notifications/test` | `system.notifications` -- fires a global test event |
| `POST` | `/api/notifications/test-webhook` | `system.notifications` -- sends test to a specific webhook URL |

### Audit Log
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/audit-log` | Superadmin (filtered by `table_name`, `record_id`, `user_id`, `operation`) |

### Config & Export
| Method | Path | Permission |
|---|---|---|
| `GET` | `/api/config` | Public |
| `GET` | `/api/config/{key}` | Public |
| `PUT` | `/api/config/{key}` | Superadmin (keys: `theme`, `dashboard`, `templates`, `modules`) |
| `GET` | `/api/stats` | Public |
| `GET` | `/api/export` | Superadmin |
| `GET` | `/api/export/csv/{entity}` | Superadmin |
| `GET` | `/api/export/csv-template/{entity}` | Superadmin |
| `POST` | `/api/import/csv/{entity}` | Superadmin |
| `GET` | `/api/export/users-json` | Superadmin |
| `GET` | `/api/export/profile-json` | Superadmin |
| `POST` | `/api/import/users-json` | Superadmin |
| `POST` | `/api/import/json/users` | Superadmin |

### File Uploads
| Method | Path | Permission |
|---|---|---|
| `POST` | `/api/upload` | Authenticated |
| `DELETE` | `/api/upload/{path}` | Authenticated |

### Health
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `GET` | `/api/health` | Same as above |

---

## Data Model

### Core Tables

| Table | Key Columns |
|---|---|
| `users` | id, username, password_hash, role, full_name, is_active, metadata (JSONB: email, discord, notes), auth_provider, external_id, created_at |
| `areas` | id, name, description, metadata (JSONB), created_at |
| `equipment` | id, area_id (FK), common_name, make, model, serial_number, build_date, status, schedulable, attributes (JSONB), attachments (JSONB), version |
| `repair_tickets` | id, equipment_id (FK), ticket_number, title, description, status, priority, category, opened_by, assigned_to, work_log (JSONB[]), attachments (JSONB[]), parts_used (JSONB[]), metadata (JSONB), version, closed_at |
| `schedules` | id, equipment_id (FK), user_id (FK), title, start_time, end_time, notes |
| `auth_sessions` | id, equipment_ids (UUID[]), authorizer_id (FK), title, description, start_time, end_time, total_slots |
| `auth_enrollments` | id, session_id (FK), user_id (FK), enrolled_at |
| `equipment_groups` | id, name, description, area_id |
| `equipment_group_members` | group_id, equipment_id |
| `maintenance_schedules` | id, title, description, equipment_id (FK), group_id (FK), recurrence_type, recurrence_interval, assigned_to, created_by, priority, estimated_minutes, checklist (JSONB), notify_roles (TEXT[]), is_active |
| `maintenance_events` | id, schedule_id (FK), equipment_id (FK), due_date, status, assigned_to, completed_by, completed_at, notes, checklist_state (JSONB), ticket_id (FK) |
| `app_config` | key (PK), value (JSONB), updated_at, updated_by |
| `audit_log` | id, table_name, record_id, operation, user_id, user_role, old_data (JSONB), new_data (JSONB), changed_fields (TEXT[]), created_at |

### Config Keys in `app_config`

| Key | Contents |
|---|---|
| `dashboard` | Dashboard tile config and ordering |
| `templates` | Field label/visibility templates for forms |
| `modules` | Module enable/disable state |
| `theme` | Branding and color scheme |
| `permissions` | Role grants + user-level grant/deny overrides |
| `auth_config` | Active auth provider + provider-specific config |
| `notifications` | Channel config, webhooks, event toggles, role routing |

---

## Development

```bash
# Rebuild only the backend after Python changes
docker compose build --no-cache backend && docker compose up -d

# Rebuild only the frontend after HTML changes
docker compose build --no-cache nginx && docker compose up -d

# Rebuild both (most common for feature work)
docker compose build --no-cache backend nginx && docker compose up -d

# Full rebuild (all containers)
docker compose down && docker compose build --no-cache && docker compose up -d

# Rebuild and restart the Discord bot
docker compose --profile discord build --no-cache discord-bot && docker compose --profile discord up -d

# View backend logs (includes notification dispatch and audit context logs)
docker logs purpleassetone_api -f

# View Discord bot logs
docker logs purpleassetone_discord -f

# Connect to the database directly
docker exec -it purpleassetone_db psql -U purpleassetone purpleassetone

# Run migrate.sql against the live DB
cat postgres/migrate.sql | docker exec -i purpleassetone_db psql -U purpleassetone purpleassetone
```

### Adding a new permission

1. Add an entry to `PERMISSION_DEFS` in `backend/main.py`
2. Add it to the appropriate role(s) in `DEFAULT_ROLE_PERMISSIONS`
3. Gate the relevant endpoint with `Depends(check_perm("your.permission"))`
4. Mirror the key and description in `PERMISSION_DEFS` and `DEFAULT_ROLE_PERMISSIONS` in `frontend-viewer/index.html`
5. Add it to the appropriate `PERM_GROUPS` array entry for the matrix UI
6. Use `can('your.permission')` in the frontend to show/hide the relevant UI element

### Adding a new notification event

1. Add the event key and metadata to `NOTIFICATION_EVENT_DEFS` in `backend/main.py`
2. Add an emoji to `DISCORD_EVENT_ICONS` for Discord embed formatting
3. Call `await fire_notification("your.event", {...payload})` in the relevant endpoint
4. The event automatically appears in the Events matrix and event filter dropdowns in the frontend

### Adding or modifying API endpoints

Whenever API endpoints are added, modified, or removed in `backend/main.py`, the `API_DOCS` constant in `frontend-viewer/index.html` must be updated in the same pass to keep the in-app documentation in sync.

---

## Project Structure

```
PurpleAssetOne/
|-- docker-compose.yml
|-- example.env
|-- README.md
|-- backend/
|   |-- Dockerfile
|   |-- main.py            # FastAPI app (~2820 lines)
|   +-- requirements.txt
|-- discord-bot/
|   |-- Dockerfile
|   |-- bot.py             # Discord bot (~550 lines)
|   |-- pa1_api.py         # PA1 API client wrapper (~115 lines)
|   |-- config.yaml        # Default command config
|   +-- requirements.txt
|-- frontend-viewer/
|   +-- index.html         # Single-file SPA (~6690 lines, Bootstrap 5)
|-- nginx/
|   |-- Dockerfile
|   +-- nginx.conf
+-- postgres/
    |-- init.sql           # Schema + default config (no seed users)
    |-- init-roles.sh      # Sets pa1_app password from env
    +-- migrate.sql        # Incremental migrations (safe to re-run)
```

### Key frontend globals

| Symbol | Purpose |
|---|---|
| `can(perm)` | Returns `true` if the current user has the named permission |
| `canAny(...perms)` | Returns `true` if the user has any of the listed permissions |
| `atLeastRole(role)` | Returns `true` if the user's role is at or above the given level |
| `appPermissions` | Array of effective permission strings for the current user |
| `currentUser` | Parsed user object from JWT / localStorage |
| `appConfig` | Merged config object (theme, dashboard, templates, modules) |
| `DEFAULT_CONFIG` | Built-in fallback config |
| `PERMISSION_DEFS` | Map of permission key to description |
| `DEFAULT_ROLE_PERMISSIONS` | Map of role to default permission list |
| `PERM_GROUPS` | Permission groups for the matrix UI |

### Key backend helpers

| Symbol | Purpose |
|---|---|
| `db_conn()` | Async context manager -- acquires pooled connection with audit context |
| `check_perm(perm)` | FastAPI dependency -- 403 if user lacks the permission |
| `require_role(*roles)` | Legacy shim -- checks minimum role level |
| `require_superadmin()` | Hard superadmin check (used for destructive ops) |
| `compute_permissions(role, user_id, config)` | Returns effective permission list for a user |
| `load_perm_config()` | Loads role_grants + user_grants from `app_config` |
| `load_notification_config()` | Loads notification config from `app_config` |
| `fire_notification(event, payload)` | Dispatches a notification event to all enabled channels |
| `_dispatch_webhook(wh, event, payload)` | POSTs to a single webhook (Discord or generic with HMAC) |
| `_build_discord_embed(event, payload)` | Builds a color-coded Discord rich embed |
| `_create_next_event(conn, schedule, from_date)` | Creates next maintenance event on completion |
| `parse_date(s)` | Safe ISO date string to `datetime.date` (handles string input from forms) |

### Navigation Structure

```
Sidebar: Dashboard | Equipment > (Repair Tickets, Groups, Areas, Maintenance) | Scheduling | Authorizations

Header: [avatar > My Profile] [role badge] [Settings dropdown] [Sign In]

Settings dropdown:
  System Settings: Users | Modules | Notifications
  Customization:   Export & Import | JSON Templates | Dashboard Customization | Branding Settings
  Permissions      (system.permissions -- superadmin only)
  Authentication   (system.auth_config -- superadmin only)
  API Documentation (superadmin only)
  About | My Profile | Sign Out
```
