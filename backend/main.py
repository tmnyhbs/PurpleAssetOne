"""
PurpleAssetOne — FastAPI Backend
"""
import os
import json
import yaml
import asyncpg
import bcrypt
from typing import Optional, List
from datetime import datetime, timedelta, timezone, date as date_type
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File, Form
from fastapi.responses import JSONResponse
import boto3
from botocore.exceptions import ClientError
import uuid as uuid_module
import mimetypes
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from pydantic import BaseModel, Field

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
DB_URL = os.getenv("DATABASE_URL", "postgresql://purpleassetone:purpleassetone@db:5432/purpleassetone")
DB_OWNER_URL = os.getenv("DATABASE_OWNER_URL", DB_URL)  # Owner connection for bootstrap only
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12
INIT_SUPERADMIN_USER = os.getenv("INIT_SUPERADMIN_USER", "superadmin")
INIT_SUPERADMIN_PASSWORD = os.getenv("INIT_SUPERADMIN_PASSWORD", "admin123")

pool: asyncpg.Pool = None

import contextvars
_ctx_user_id = contextvars.ContextVar('audit_user_id', default='')
_ctx_role = contextvars.ContextVar('audit_role', default='')

# ─────────────────────────────────────────
# S3 / STORAGE CONFIG
# ─────────────────────────────────────────
S3_ENDPOINT_URL    = os.getenv("S3_ENDPOINT_URL", "")       # blank = AWS S3
S3_ACCESS_KEY_ID   = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET          = os.getenv("S3_BUCKET", "purpleassetone")
S3_PUBLIC_URL      = os.getenv("S3_PUBLIC_URL", "")         # optional CDN/proxy prefix

def get_s3_client():
    kwargs = dict(
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    )
    if S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
    return boto3.client("s3", **kwargs)

def ensure_bucket():
    """Create the bucket if it doesn't exist (MinIO only)."""
    if not S3_ENDPOINT_URL:
        return  # AWS S3 bucket must exist already
    try:
        s3 = get_s3_client()
        s3.head_bucket(Bucket=S3_BUCKET)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            s3.create_bucket(Bucket=S3_BUCKET)
            # Set public-read policy so files are directly accessible
            policy = json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{S3_BUCKET}/*"]
                }]
            })
            s3.put_bucket_policy(Bucket=S3_BUCKET, Policy=policy)

def file_url(key: str) -> str:
    """Return the public URL for a stored file key."""
    if S3_PUBLIC_URL:
        return f"{S3_PUBLIC_URL.rstrip('/')}/{key}"
    if S3_ENDPOINT_URL:
        # Serve through nginx /files/ proxy so URLs work internally and via reverse proxies
        return f"/files/{S3_BUCKET}/{key}"
    return f"https://{S3_BUCKET}.s3.amazonaws.com/{key}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=5, max_size=20)
    ensure_bucket()
    # Bootstrap: create initial superadmin if no users exist (uses owner connection)
    await _bootstrap_superadmin()
    yield
    await pool.close()


async def _bootstrap_superadmin():
    """Create the initial superadmin user from env vars if the users table is empty."""
    try:
        owner_conn = await asyncpg.connect(DB_OWNER_URL)
        try:
            count = await owner_conn.fetchval("SELECT COUNT(*) FROM users")
            if count == 0:
                hashed = bcrypt.hashpw(INIT_SUPERADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
                await owner_conn.execute(
                    "INSERT INTO users (username, password_hash, role, full_name) VALUES ($1, $2, 'superadmin', 'Super Administrator')",
                    INIT_SUPERADMIN_USER, hashed
                )
                import logging
                logging.getLogger("startup").info(f"Created initial superadmin user: {INIT_SUPERADMIN_USER}")
        finally:
            await owner_conn.close()
    except Exception as e:
        import logging
        logging.getLogger("startup").warning(f"Superadmin bootstrap check failed: {e}")


@asynccontextmanager
async def db_conn():
    """Acquire a pooled connection with audit context (user_id + role) set from the request."""
    async with pool.acquire() as conn:
        uid = _ctx_user_id.get('')
        role = _ctx_role.get('')
        await conn.execute("SELECT set_config('app.current_user_id', $1, false)", uid or '')
        await conn.execute("SELECT set_config('app.session_role', $1, false)", role or '')
        try:
            yield conn
        finally:
            await conn.execute("SELECT set_config('app.current_user_id', '', false)")
            await conn.execute("SELECT set_config('app.session_role', '', false)")


app = FastAPI(title="PurpleAssetOne API", lifespan=lifespan)

import traceback

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": str(exc)})

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Audit context middleware ──────────────────────────────────────
@app.middleware("http")
async def audit_context_middleware(request: Request, call_next):
    """Extract user_id and role from JWT bearer token and store in contextvars
    so the DB audit trigger can record who performed each operation."""
    user_id = ''
    role = ''
    auth_header = request.headers.get('authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            payload = jwt.decode(auth_header[7:], SECRET_KEY, algorithms=[ALGORITHM])
            user_id = payload.get('sub', '')
            role = payload.get('role', '')
        except JWTError:
            pass
    _ctx_user_id.set(user_id)
    _ctx_role.set(role)
    response = await call_next(request)
    return response


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────
class Token(BaseModel):
    access_token: str
    token_type: str
    user: dict

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "technician"
    full_name: Optional[str] = None

class AreaCreate(BaseModel):
    name: str
    description: Optional[str] = None
    metadata: dict = {}

def parse_date(s):
    """Safely parse an ISO date string to a datetime.date, returning None on failure."""
    if not s: return None
    try:
        return date_type.fromisoformat(str(s).strip())
    except (ValueError, TypeError, AttributeError):
        return None


class EquipmentCreate(BaseModel):
    area_id: Optional[str] = None
    common_name: Optional[str] = None
    make: str
    model: str
    serial_number: str
    schedulable: bool = False
    build_date: Optional[str] = None
    status: str = "active"
    attributes: dict = {}

class EquipmentUpdate(BaseModel):
    area_id: Optional[str] = None
    common_name: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    schedulable: Optional[bool] = None
    build_date: Optional[str] = None
    status: Optional[str] = None
    attributes: Optional[dict] = None
    attachments: Optional[list] = None
    version: int  # required for optimistic locking

class TicketCreate(BaseModel):
    equipment_id: str
    title: str
    description: Optional[str] = None
    priority: str = "normal"
    assigned_to: Optional[str] = None
    metadata: dict = {}

class TicketUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assigned_to: Optional[str] = None
    metadata: Optional[dict] = None
    attachments: Optional[list] = None
    version: int

class WorkLogEntry(BaseModel):
    action: str
    notes: Optional[str] = None
    parts_used: list = []
    attachments: list = []

# ─────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    async with db_conn() as conn:
        user = await conn.fetchrow(
            "SELECT id, username, role, full_name, is_active FROM users WHERE id=$1",
            user_id
        )
    if not user or not user["is_active"]:
        raise credentials_exception
    return dict(user)

# ─────────────────────────────────────────
# PERMISSIONS ENGINE
# ─────────────────────────────────────────

# Fixed role hierarchy: viewer < member < authorizer < technician < area_host < admin < superadmin
ROLE_HIERARCHY = ["viewer", "member", "authorizer", "technician", "area_host", "admin", "superadmin"]

# All permission keys with descriptions
PERMISSION_DEFS = {
    # Equipment
    "equipment.view":     "View equipment list and details",
    "equipment.create":   "Add new equipment",
    "equipment.edit":     "Edit existing equipment",
    "equipment.delete":   "Delete equipment",
    "equipment.export":   "Export equipment data (CSV/JSON)",
    # Tickets
    "tickets.view":       "View repair tickets",
    "tickets.create":     "Create new tickets",
    "tickets.edit":       "Edit ticket details & status",
    "tickets.worklog":    "Add work log entries",
    "tickets.delete":     "Delete tickets (destructive)",
    # Areas
    "areas.view":         "View areas",
    "areas.create":       "Create new areas",
    "areas.edit":         "Edit area info",
    "areas.delete":       "Delete areas",
    # Scheduling
    "scheduling.view":    "View schedule / calendar",
    "scheduling.book":    "Create own bookings",
    "scheduling.manage":  "Manage all bookings (cancel, override)",
    # Authorizations
    "auth_sessions.view":   "View authorization sessions",
    "auth_sessions.create": "Create auth sessions",
    "auth_sessions.manage": "Manage sessions & enrollments",
    # Equipment Groups
    "groups.view":        "View equipment groups",
    "groups.manage":      "Create / edit / delete groups",
    # Maintenance
    "maintenance.view":     "View maintenance calendar and schedules",
    "maintenance.create":   "Create maintenance schedules",
    "maintenance.edit":     "Edit maintenance schedules",
    "maintenance.complete": "Complete or skip maintenance events",
    "maintenance.manage":   "Full maintenance management (delete, reassign, configure)",
    # Users
    "users.view":         "View user list",
    "users.create":       "Create new users",
    "users.edit":         "Edit user profiles & roles",
    "users.delete":       "Delete users (destructive)",
    # System settings (menu visibility also controlled by these)
    "system.settings":    "Access system settings menu",
    "system.users":       "Manage users panel",
    "system.modules":     "Toggle modules on/off",
    "system.templates":   "Edit field templates",
    "system.dashboard":   "Customize dashboard",
    "system.branding":    "Edit branding & theme",
    "system.export":      "Export & import data",
    "system.permissions": "Manage permissions (superadmin only)",
    "system.auth_config": "Configure authentication providers",
    "system.notifications": "Configure notification channels and events",
}

# Default permissions per role (cumulative — higher roles don't inherit lower unless listed)
DEFAULT_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "viewer": [
        "equipment.view", "tickets.view", "areas.view",
        "scheduling.view", "auth_sessions.view", "groups.view",
    ],
    "member": [
        "equipment.view", "tickets.view", "tickets.create",
        "areas.view", "scheduling.view", "scheduling.book",
        "auth_sessions.view", "groups.view",
    ],
    "authorizer": [
        "equipment.view", "tickets.view", "tickets.create",
        "areas.view", "scheduling.view", "scheduling.book",
        "auth_sessions.view", "auth_sessions.create", "auth_sessions.manage",
        "groups.view",
    ],
    "technician": [
        "equipment.view", "equipment.create", "equipment.edit", "equipment.export",
        "tickets.view", "tickets.create", "tickets.edit", "tickets.worklog",
        "areas.view", "areas.create", "areas.edit",
        "scheduling.view", "scheduling.book", "scheduling.manage",
        "auth_sessions.view", "auth_sessions.create",
        "groups.view", "groups.manage",
        "maintenance.view", "maintenance.create", "maintenance.complete",
        "system.settings",
    ],
    "area_host": [
        "equipment.view", "equipment.create", "equipment.edit", "equipment.export",
        "tickets.view", "tickets.create", "tickets.edit", "tickets.worklog",
        "areas.view", "areas.create", "areas.edit",
        "scheduling.view", "scheduling.book", "scheduling.manage",
        "auth_sessions.view", "auth_sessions.create", "auth_sessions.manage",
        "groups.view", "groups.manage",
        "maintenance.view", "maintenance.create", "maintenance.edit",
        "maintenance.complete", "maintenance.manage",
        "users.view",
        "system.settings", "system.notifications",
    ],
    "admin": [
        "equipment.view", "equipment.create", "equipment.edit", "equipment.delete", "equipment.export",
        "tickets.view", "tickets.create", "tickets.edit", "tickets.worklog",
        "areas.view", "areas.create", "areas.edit", "areas.delete",
        "scheduling.view", "scheduling.book", "scheduling.manage",
        "auth_sessions.view", "auth_sessions.create", "auth_sessions.manage",
        "groups.view", "groups.manage",
        "maintenance.view", "maintenance.create", "maintenance.edit",
        "maintenance.complete", "maintenance.manage",
        "users.view", "users.create", "users.edit",
        "system.settings", "system.users", "system.modules", "system.templates",
        "system.dashboard", "system.branding", "system.export", "system.notifications",
    ],
    "superadmin": ["*"],  # wildcard: all permissions
}

# ── Permission config caching ──────────────────────────────────────
_perm_config_cache: dict = {}

async def load_perm_config() -> dict:
    """Load permissions config from DB (role_grants + user_grants)."""
    try:
        async with db_conn() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM app_config WHERE key='permissions'"
            )
        if row:
            val = row["value"]
            if isinstance(val, str):
                val = json.loads(val)
            return val or {}
    except Exception:
        pass
    return {}

def compute_permissions(role: str, user_id: str, perm_config: dict) -> list[str]:
    """Compute effective permission list for a user."""
    if role == "superadmin":
        return list(PERMISSION_DEFS.keys())

    # Start from role defaults (can be overridden per-role in config)
    role_grants = perm_config.get("role_grants", {})
    base = set(role_grants.get(role, DEFAULT_ROLE_PERMISSIONS.get(role, [])))

    # Apply user-level overrides
    user_overrides = perm_config.get("user_grants", {}).get(str(user_id), {})
    base.update(user_overrides.get("grant", []))
    base.difference_update(user_overrides.get("deny", []))

    return sorted(base)


async def get_current_user_with_perms(token: str = Depends(oauth2_scheme)):
    """Extended get_current_user that loads effective permissions into the user dict."""
    user = await get_current_user(token)
    perm_cfg = await load_perm_config()
    user["permissions"] = compute_permissions(user["role"], user["id"], perm_cfg)
    return user

def check_perm(perm: str):
    """Dependency: require a specific permission (or superadmin wildcard)."""
    async def checker(current_user: dict = Depends(get_current_user_with_perms)):
        perms = current_user.get("permissions", [])
        if perm not in perms:
            raise HTTPException(status_code=403, detail=f"Permission required: {perm}")
        return current_user
    return checker

def require_role(*roles):
    """Legacy shim — maps to role hierarchy check. New code uses check_perm."""
    hierarchy_map = {r: i for i, r in enumerate(ROLE_HIERARCHY)}
    min_level = min((hierarchy_map.get(r, 0) for r in roles), default=0)
    async def checker(current_user: dict = Depends(get_current_user)):
        user_level = hierarchy_map.get(current_user["role"], -1)
        if user_level < min_level:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return checker

def require_superadmin():
    async def checker(current_user: dict = Depends(get_current_user)):
        if current_user["role"] != "superadmin":
            raise HTTPException(status_code=403, detail="Superadmin access required")
        return current_user
    return checker


def row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        # Convert UUID objects to strings
        if isinstance(v, uuid_module.UUID):
            d[k] = str(v)
        elif isinstance(v, list):
            d[k] = [str(x) if isinstance(x, uuid_module.UUID) else x for x in v]
        elif isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, (dict, list)):
                    d[k] = parsed
            except (json.JSONDecodeError, ValueError):
                pass
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, date_type):
            d[k] = v.isoformat()
    return d

def rows_to_list(rows):
    return [row_to_dict(r) for r in rows]

# ─────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────
@app.post("/api/auth/token", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    async with db_conn() as conn:
        user = await conn.fetchrow(
            "SELECT id, username, password_hash, role, full_name, is_active, COALESCE(auth_provider,'local') as auth_provider FROM users WHERE username=$1",
            form_data.username
        )
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not bcrypt.checkpw(form_data.password.encode(), user["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    perm_cfg = await load_perm_config()
    permissions = compute_permissions(user["role"], str(user["id"]), perm_cfg)
    # Include role and permissions in JWT for stateless frontend checks
    token = create_access_token({
        "sub": str(user["id"]),
        "role": user["role"],
        "auth_provider": user.get("auth_provider", "local"),
    })
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": str(user["id"]),
            "username": user["username"],
            "role": user["role"],
            "full_name": user["full_name"],
            "auth_provider": user.get("auth_provider", "local"),
            "permissions": permissions,
        }
    }

@app.get("/api/auth/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return current_user

# ─────────────────────────────────────────
# USER MANAGEMENT
# ─────────────────────────────────────────
@app.get("/api/users")
async def list_users(current_user=Depends(check_perm("users.view"))):
    async with db_conn() as conn:
        rows = await conn.fetch(
            "SELECT id, username, role, full_name, created_at, is_active FROM users ORDER BY username"
        )
    return rows_to_list(rows)

@app.post("/api/users", status_code=201)
async def create_user(data: UserCreate, current_user=Depends(check_perm("users.create"))):
    # Only superadmin can create admin or superadmin users
    if data.role in ("admin", "superadmin") and current_user["role"] != "superadmin":
        raise HTTPException(403, "Only superadmin can create admin or superadmin users")
    if data.role not in ("superadmin", "admin", "area_host", "technician", "authorizer", "viewer", "member"):
        raise HTTPException(400, "Invalid role")
    hashed = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt()).decode()
    async with db_conn() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO users (username, password_hash, role, full_name)
                   VALUES ($1, $2, $3, $4)
                   RETURNING id, username, role, full_name, created_at""",
                data.username, hashed, data.role, data.full_name
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(400, "Username already exists")
    return row_to_dict(row)

@app.patch("/api/users/{user_id}")
async def update_user(user_id: str, data: dict, current_user=Depends(check_perm("users.edit"))):
    allowed = {"role", "full_name", "is_active"}
    updates = {k: v for k, v in data.items() if k in allowed}
    # Only superadmin can set role to admin/superadmin
    if "role" in updates and updates["role"] in ("admin", "superadmin") and current_user["role"] != "superadmin":
        raise HTTPException(403, "Only superadmin can assign admin or superadmin roles")
    if not updates:
        raise HTTPException(400, "No valid fields to update")
    set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
    async with db_conn() as conn:
        row = await conn.fetchrow(
            f"UPDATE users SET {set_clauses} WHERE id=$1 RETURNING id, username, role, full_name, is_active",
            user_id, *updates.values()
        )
    if not row:
        raise HTTPException(404, "User not found")
    return row_to_dict(row)

@app.patch("/api/users/{user_id}/password")
async def change_password(user_id: str, data: dict, current_user=Depends(check_perm("users.edit"))):
    new_password = data.get("password")
    if not new_password or len(new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    async with db_conn() as conn:
        await conn.execute("UPDATE users SET password_hash=$1 WHERE id=$2", hashed, user_id)
    return {"ok": True}



# ─────────────────────────────────────────
# PROFILE (current user)
# ─────────────────────────────────────────
@app.get("/api/users/me")
async def get_me(current_user=Depends(get_current_user)):
    async with db_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, role, full_name, is_active, metadata, created_at FROM users WHERE id=$1",
            current_user["id"]
        )
    return row_to_dict(row) if row else HTTPException(404, "User not found")

@app.patch("/api/users/me")
async def update_me(data: dict, current_user=Depends(get_current_user)):
    allowed_meta = {"email", "discord", "notes"}
    meta_updates = {k: v for k, v in data.items() if k in allowed_meta}
    full_name = data.get("full_name")
    async with db_conn() as conn:
        if full_name is not None:
            await conn.execute("UPDATE users SET full_name=$1 WHERE id=$2", full_name, current_user["id"])
        if meta_updates:
            for k, v in meta_updates.items():
                await conn.execute(
                    "UPDATE users SET metadata = jsonb_set(COALESCE(metadata,'{}'), $1, $2::jsonb) WHERE id=$3",
                    [k], json.dumps(v), current_user["id"]
                )
    return {"ok": True}

@app.patch("/api/users/me/password")
async def change_my_password(data: dict, current_user=Depends(get_current_user)):
    current_pw = data.get("current_password", "")
    new_pw     = data.get("new_password", "")
    if not new_pw or len(new_pw) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    async with db_conn() as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE id=$1", current_user["id"])
    if not row or not bcrypt.checkpw(current_pw.encode(), row["password_hash"].encode()):
        raise HTTPException(400, "Current password is incorrect")
    hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    async with db_conn() as conn:
        await conn.execute("UPDATE users SET password_hash=$1 WHERE id=$2", hashed, current_user["id"])
    return {"ok": True}

@app.get("/api/users/{user_id}/profile")
async def get_user_profile(user_id: str, current_user=Depends(check_perm("users.view"))):
    async with db_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, role, full_name, is_active, metadata, created_at FROM users WHERE id=$1",
            user_id
        )
    if not row: raise HTTPException(404, "User not found")
    return row_to_dict(row)

@app.patch("/api/users/{user_id}/profile")
async def update_user_profile(user_id: str, data: dict, current_user=Depends(check_perm("users.edit"))):
    allowed_meta = {"email", "discord", "notes"}
    meta_updates = {k: v for k, v in data.items() if k in allowed_meta}
    full_name = data.get("full_name")
    async with db_conn() as conn:
        if full_name is not None:
            await conn.execute("UPDATE users SET full_name=$1 WHERE id=$2", full_name, user_id)
        if meta_updates:
            for k, v in meta_updates.items():
                await conn.execute(
                    "UPDATE users SET metadata = jsonb_set(COALESCE(metadata,'{}'), $1, $2::jsonb) WHERE id=$3",
                    [k], json.dumps(v), user_id
                )
    return {"ok": True}


@app.delete("/api/users/{user_id}", status_code=204)
async def delete_user(user_id: str, current_user=Depends(require_superadmin())):
    if user_id == current_user["id"]:
        raise HTTPException(400, "Cannot delete your own account")
    async with db_conn() as conn:
        result = await conn.execute("DELETE FROM users WHERE id=$1", user_id)
    if result == "DELETE 0":
        raise HTTPException(404, "User not found")
    return Response(status_code=204)

# ─────────────────────────────────────────
# AREAS
# ─────────────────────────────────────────
@app.get("/api/areas")
async def list_areas():
    async with db_conn() as conn:
        rows = await conn.fetch(
            """SELECT a.*, COUNT(e.id) as equipment_count
               FROM areas a
               LEFT JOIN equipment e ON e.area_id = a.id
               GROUP BY a.id ORDER BY a.name"""
        )
    return rows_to_list(rows)

@app.get("/api/areas/{area_id}")
async def get_area(area_id: str):
    async with db_conn() as conn:
        row = await conn.fetchrow(
            """SELECT a.*, COUNT(e.id) as equipment_count
               FROM areas a
               LEFT JOIN equipment e ON e.area_id = a.id
               WHERE a.id = $1
               GROUP BY a.id""",
            area_id
        )
    if not row:
        raise HTTPException(404, "Area not found")
    return row_to_dict(row)

@app.post("/api/areas", status_code=201)
async def create_area(data: AreaCreate, current_user=Depends(check_perm("areas.create"))):
    # Ensure standard metadata keys are present
    meta = {"website": "", "host_name": "", "host_contact": "", "email": "", "discord": ""}
    meta.update(data.metadata)
    async with db_conn() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO areas (name, description, metadata) VALUES ($1, $2, $3) RETURNING *",
                data.name, data.description, json.dumps(meta)
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(400, "Area name already exists")
    result = row_to_dict(row)
    await fire_notification("area.created", {"area_id": str(result.get("id")), "name": data.name, "by": current_user.get("username")})
    return result

@app.patch("/api/areas/{area_id}")
async def update_area(area_id: str, data: dict, current_user=Depends(check_perm("areas.edit"))):
    allowed = {"name", "description", "metadata"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if "metadata" in updates:
        updates["metadata"] = json.dumps(updates["metadata"])
    set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
    async with db_conn() as conn:
        row = await conn.fetchrow(
            f"UPDATE areas SET {set_clauses} WHERE id=$1 RETURNING *",
            area_id, *updates.values()
        )
    if not row:
        raise HTTPException(404, "Area not found")
    result = row_to_dict(row)
    await fire_notification("area.modified", {"area_id": area_id, "fields": list(updates.keys()), "by": current_user.get("username")})
    return result

@app.delete("/api/areas/{area_id}")
async def delete_area(area_id: str, current_user=Depends(check_perm("areas.delete"))):
    async with db_conn() as conn:
        result = await conn.execute("DELETE FROM areas WHERE id=$1", area_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Area not found")
    return {"ok": True}

# ─────────────────────────────────────────
# EQUIPMENT
# ─────────────────────────────────────────
@app.get("/api/equipment")
async def list_equipment(
    area_id: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
):
    conditions = ["1=1"]
    params = []
    i = 1

    if area_id:
        conditions.append(f"e.area_id = ${i}")
        params.append(area_id); i += 1
    if status:
        conditions.append(f"e.status = ${i}")
        params.append(status); i += 1
    if search:
        conditions.append(f"(e.make ILIKE ${i} OR e.model ILIKE ${i} OR e.serial_number ILIKE ${i} OR e.common_name ILIKE ${i})")
        params.append(f"%{search}%"); i += 1

    where = " AND ".join(conditions)
    async with db_conn() as conn:
        rows = await conn.fetch(
            f"""SELECT e.*, a.name as area_name,
                       COUNT(t.id) as open_tickets
                FROM equipment e
                LEFT JOIN areas a ON a.id = e.area_id
                LEFT JOIN repair_tickets t ON t.equipment_id = e.id AND t.status != 'closed'
                WHERE {where}
                GROUP BY e.id, a.name
                ORDER BY e.make, e.model""",
            *params
        )
    return rows_to_list(rows)

@app.get("/api/equipment/{equipment_id}")
async def get_equipment(equipment_id: str):
    async with db_conn() as conn:
        row = await conn.fetchrow(
            """SELECT e.*, a.name as area_name
               FROM equipment e
               LEFT JOIN areas a ON a.id = e.area_id
               WHERE e.id = $1""",
            equipment_id
        )
    if not row:
        raise HTTPException(404, "Equipment not found")
    return row_to_dict(row)

@app.post("/api/equipment", status_code=201)
async def create_equipment(data: EquipmentCreate, current_user=Depends(check_perm("equipment.create"))):
    async with db_conn() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO equipment (area_id, common_name, make, model, serial_number, build_date, status, attributes, schedulable)
                   VALUES ($1, $2, $3, $4, $5, $6::date, $7, $8, $9)
                   RETURNING *""",
                data.area_id, data.common_name, data.make, data.model, data.serial_number,
                parse_date(data.build_date),
                data.status, json.dumps(data.attributes), data.schedulable
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(400, "Serial number already exists")
    result = row_to_dict(row)
    await fire_notification("equipment.created", {"equipment_id": str(result.get("id")), "make": data.make, "model": data.model, "by": current_user.get("username")})
    return result

@app.patch("/api/equipment/{equipment_id}")
async def update_equipment(equipment_id: str, data: EquipmentUpdate, current_user=Depends(check_perm("equipment.edit"))):
    async with db_conn() as conn:
        current = await conn.fetchrow("SELECT version FROM equipment WHERE id=$1", equipment_id)
        if not current:
            raise HTTPException(404, "Equipment not found")
        if current["version"] != data.version:
            raise HTTPException(409, "Equipment was modified by another user. Please refresh and try again.")

        updates = {}
        if data.common_name is not None: updates["common_name"] = data.common_name
        if data.make is not None: updates["make"] = data.make
        if data.model is not None: updates["model"] = data.model
        if data.serial_number is not None: updates["serial_number"] = data.serial_number
        if data.build_date is not None: updates["build_date"] = parse_date(data.build_date)
        if data.status is not None: updates["status"] = data.status
        if data.area_id is not None: updates["area_id"] = data.area_id
        if data.attributes is not None: updates["attributes"] = json.dumps(data.attributes)
        if data.attachments is not None: updates["attachments"] = json.dumps(data.attachments)
        if data.schedulable is not None: updates["schedulable"] = data.schedulable

        if not updates:
            raise HTTPException(400, "No fields to update")

        _DATE_COLS = {"build_date"}
        set_clauses = ", ".join(
            f"{k}=${i+2}::date" if k in _DATE_COLS else f"{k}=${i+2}"
            for i, k in enumerate(updates)
        )
        row = await conn.fetchrow(
            f"UPDATE equipment SET {set_clauses} WHERE id=$1 RETURNING *",
            equipment_id, *updates.values()
        )
    result = row_to_dict(row)
    await fire_notification("equipment.modified", {"equipment_id": equipment_id, "fields": list(updates.keys()), "by": current_user.get("username")})
    return result

@app.delete("/api/equipment/{equipment_id}")
async def delete_equipment(equipment_id: str, current_user=Depends(check_perm("equipment.delete"))):
    async with db_conn() as conn:
        result = await conn.execute("DELETE FROM equipment WHERE id=$1", equipment_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Equipment not found")
    return {"ok": True}

# ─────────────────────────────────────────
# REPAIR TICKETS
# ─────────────────────────────────────────
@app.get("/api/tickets")
async def list_tickets(
    request: Request,
    equipment_id: Optional[str] = None,
    assigned_to: Optional[str] = None,
):
    # Support multiple status= and priority= query params
    qp = request.query_params
    statuses  = qp.getlist("status")  if hasattr(qp, "getlist") else [v for k,v in qp.multi_items() if k=="status"]
    priorities = qp.getlist("priority") if hasattr(qp, "getlist") else [v for k,v in qp.multi_items() if k=="priority"]
    # Also accept single status/priority for backward compat
    single_status   = qp.get("status")
    single_priority = qp.get("priority")
    if not statuses and single_status:   statuses   = [single_status]
    if not priorities and single_priority: priorities = [single_priority]

    conditions = ["1=1"]
    params = []
    i = 1

    if equipment_id:
        conditions.append(f"t.equipment_id = ${i}"); params.append(equipment_id); i += 1
    if statuses:
        placeholders = ",".join(f"${i+j}" for j in range(len(statuses)))
        conditions.append(f"t.status IN ({placeholders})"); params.extend(statuses); i += len(statuses)
    if assigned_to:
        conditions.append(f"t.assigned_to = ${i}"); params.append(assigned_to); i += 1
    if priorities:
        placeholders = ",".join(f"${i+j}" for j in range(len(priorities)))
        conditions.append(f"t.priority IN ({placeholders})"); params.extend(priorities); i += len(priorities)

    where = " AND ".join(conditions)
    async with db_conn() as conn:
        rows = await conn.fetch(
            f"""SELECT t.id, t.equipment_id, t.ticket_number, t.opened_by, t.assigned_to,
                       t.status, t.priority, t.title, t.description, t.work_log,
                       t.parts_used, t.metadata, t.opened_at, t.closed_at, t.category, t.version,
                       COALESCE(e.common_name, e.make || ' ' || e.model) as equipment_name,
                       e.serial_number,
                       a.name as area_name,
                       opener.full_name as opened_by_name,
                       assignee.full_name as assigned_to_name
                FROM repair_tickets t
                LEFT JOIN equipment e ON e.id = t.equipment_id
                LEFT JOIN areas a ON a.id = e.area_id
                LEFT JOIN users opener ON opener.id = t.opened_by
                LEFT JOIN users assignee ON assignee.id = t.assigned_to
                WHERE {where}
                ORDER BY
                  CASE t.priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'normal' THEN 3 ELSE 4 END,
                  t.opened_at DESC""",
            *params
        )
    return rows_to_list(rows)

@app.get("/api/tickets/{ticket_id}")
async def get_ticket(ticket_id: str):
    async with db_conn() as conn:
        row = await conn.fetchrow(
            """SELECT t.id, t.equipment_id, t.ticket_number, t.opened_by, t.assigned_to,
                      t.status, t.priority, t.title, t.description, t.work_log,
                      t.parts_used, t.metadata, t.opened_at, t.closed_at, t.category, t.version,
                      COALESCE(e.common_name, e.make || ' ' || e.model) as equipment_name,
                      e.serial_number,
                      a.name as area_name,
                      opener.full_name as opened_by_name,
                      assignee.full_name as assigned_to_name
               FROM repair_tickets t
               LEFT JOIN equipment e ON e.id = t.equipment_id
               LEFT JOIN areas a ON a.id = e.area_id
               LEFT JOIN users opener ON opener.id = t.opened_by
               LEFT JOIN users assignee ON assignee.id = t.assigned_to
               WHERE t.id = $1""",
            ticket_id
        )
    if not row:
        raise HTTPException(404, "Ticket not found")
    return row_to_dict(row)

@app.post("/api/tickets", status_code=201)
async def create_ticket(data: TicketCreate, current_user=Depends(check_perm("tickets.create"))):
    async with db_conn() as conn:
        ticket_number = await conn.fetchval("SELECT next_ticket_number()")
        row = await conn.fetchrow(
            """INSERT INTO repair_tickets
               (equipment_id, ticket_number, opened_by, assigned_to, title, description, priority, metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING *""",
            data.equipment_id, ticket_number, current_user["id"],
            data.assigned_to, data.title, data.description,
            data.priority, json.dumps(data.metadata)
        )
        await conn.execute(
            "UPDATE equipment SET status='under_repair' WHERE id=$1 AND status='active'",
            data.equipment_id
        )
    result = row_to_dict(row)
    await fire_notification("ticket.created", {"ticket_id": str(result.get("id")), "title": data.title, "priority": data.priority, "by": current_user.get("username")})
    return result

@app.patch("/api/tickets/{ticket_id}")
async def update_ticket(ticket_id: str, data: TicketUpdate, current_user=Depends(check_perm("tickets.edit"))):
    async with db_conn() as conn:
        current = await conn.fetchrow("SELECT version, status FROM repair_tickets WHERE id=$1", ticket_id)
        if not current:
            raise HTTPException(404, "Ticket not found")
        if current["version"] != data.version:
            raise HTTPException(409, "Ticket was modified by another user. Please refresh and try again.")

        updates = {}
        if data.title is not None: updates["title"] = data.title
        if data.description is not None: updates["description"] = data.description
        if data.status is not None: updates["status"] = data.status
        if data.priority is not None: updates["priority"] = data.priority
        if data.assigned_to is not None: updates["assigned_to"] = data.assigned_to
        if data.metadata is not None: updates["metadata"] = json.dumps(data.metadata)
        if data.attachments is not None: updates["attachments"] = json.dumps(data.attachments)
        if data.status == "closed": updates["closed_at"] = datetime.now(timezone.utc).isoformat()

        if not updates:
            raise HTTPException(400, "No fields to update")

        _DATE_COLS = {"build_date"}
        set_clauses = ", ".join(
            f"{k}=${i+2}::date" if k in _DATE_COLS else f"{k}=${i+2}"
            for i, k in enumerate(updates)
        )
        row = await conn.fetchrow(
            f"UPDATE repair_tickets SET {set_clauses} WHERE id=$1 RETURNING *",
            ticket_id, *updates.values()
        )

        if data.status == "closed":
            open_count = await conn.fetchval(
                "SELECT COUNT(*) FROM repair_tickets WHERE equipment_id=$1 AND status != 'closed'",
                row["equipment_id"]
            )
            if open_count == 0:
                await conn.execute(
                    "UPDATE equipment SET status='active' WHERE id=$1 AND status='under_repair'",
                    row["equipment_id"]
                )

    result = row_to_dict(row)
    if data.status == "closed":
        await fire_notification("ticket.closed", {"ticket_id": ticket_id, "by": current_user.get("username")})
    else:
        await fire_notification("ticket.modified", {"ticket_id": ticket_id, "fields": list(updates.keys()), "by": current_user.get("username")})
    return result


@app.delete("/api/tickets/{ticket_id}", status_code=204)
async def delete_ticket(ticket_id: str, current_user=Depends(check_perm("tickets.delete"))):
    async with db_conn() as conn:
        # If ticket is linked to equipment under_repair, revert status if no other open tickets
        ticket = await conn.fetchrow("SELECT equipment_id FROM repair_tickets WHERE id=$1", ticket_id)
        if not ticket:
            raise HTTPException(404, "Ticket not found")
        result = await conn.execute("DELETE FROM repair_tickets WHERE id=$1", ticket_id)
        if result == "DELETE 0":
            raise HTTPException(404, "Ticket not found")
        # Revert equipment status if no remaining open tickets
        if ticket["equipment_id"]:
            open_count = await conn.fetchval(
                "SELECT COUNT(*) FROM repair_tickets WHERE equipment_id=$1 AND status != 'closed'",
                ticket["equipment_id"]
            )
            if open_count == 0:
                await conn.execute(
                    "UPDATE equipment SET status='active' WHERE id=$1 AND status='under_repair'",
                    ticket["equipment_id"]
                )

@app.post("/api/tickets/{ticket_id}/worklog")
async def add_work_log(ticket_id: str, entry: WorkLogEntry, current_user=Depends(check_perm("tickets.worklog"))):
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": str(current_user["id"]),
        "user_name": current_user["full_name"] or current_user["username"],
        "action": entry.action,
        "notes": entry.notes,
        "parts_used": entry.parts_used,
        "attachments": entry.attachments,
    }
    async with db_conn() as conn:
        row = await conn.fetchrow(
            """UPDATE repair_tickets
               SET work_log = work_log || $2::jsonb,
                   status = CASE WHEN status = 'open' THEN 'in_progress' ELSE status END
               WHERE id = $1
               RETURNING *""",
            ticket_id, json.dumps([log_entry])
        )
    if not row:
        raise HTTPException(404, "Ticket not found")
    return row_to_dict(row)


# ─────────────────────────────────────────
# FILE UPLOADS
# ─────────────────────────────────────────
ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
    "video/mp4", "video/quicktime", "video/webm",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain", "text/csv",
}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    context: str = Form(default="general"),  # equipment/{id}, ticket/{id}, worklog
    current_user=Depends(check_perm("equipment.edit"))
):
    # Validate mime type
    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    if mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(400, f"File type '{mime}' is not allowed")

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(400, "File exceeds 100 MB limit")

    ext = (file.filename or "file").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    key = f"{context}/{uuid_module.uuid4()}.{ext}" if ext else f"{context}/{uuid_module.uuid4()}"

    try:
        s3 = get_s3_client()
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=data,
            ContentType=mime,
            ContentDisposition=f'inline; filename="{file.filename}"',
        )
    except Exception as e:
        raise HTTPException(500, f"Storage error: {e}")

    return {
        "key": key,
        "url": file_url(key),
        "filename": file.filename,
        "size": len(data),
        "mime": mime,
    }

@app.delete("/api/upload/{path:path}", status_code=204)
async def delete_file(path: str, current_user=Depends(check_perm("equipment.edit"))):
    try:
        s3 = get_s3_client()
        s3.delete_object(Bucket=S3_BUCKET, Key=path)
    except Exception as e:
        raise HTTPException(500, f"Storage error: {e}")


# ─────────────────────────────────────────
# SCHEDULING
# ─────────────────────────────────────────

class ScheduleCreate(BaseModel):
    equipment_id: str
    title: Optional[str] = None
    start_time: str   # ISO8601
    end_time: str
    notes: Optional[str] = None

class ScheduleUpdate(BaseModel):
    title: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    notes: Optional[str] = None

class AuthSessionCreate(BaseModel):
    equipment_ids: List[str] = []
    title: str
    description: Optional[str] = None
    start_time: str
    end_time: str
    total_slots: int = 1

class AuthSessionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    total_slots: Optional[int] = None
    equipment_ids: Optional[List[str]] = None

def require_member_or_above():
    """Allow any authenticated user (member and up)."""
    return require_role("member", "viewer", "authorizer", "technician", "admin")

def require_authorizer():
    return require_role("authorizer", "admin")

# ── Schedules ──

@app.get("/api/schedules")
async def list_schedules(
    equipment_id: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    current_user=Depends(get_current_user)
):
    conditions = ["1=1"]
    params = []
    i = 1
    if equipment_id:
        conditions.append(f"s.equipment_id = ${i}"); params.append(equipment_id); i += 1
    if from_time:
        conditions.append(f"s.end_time > ${i}"); params.append(from_time); i += 1
    if to_time:
        conditions.append(f"s.start_time < ${i}"); params.append(to_time); i += 1
    where = " AND ".join(conditions)
    async with db_conn() as conn:
        rows = await conn.fetch(
            f"""SELECT s.id, s.equipment_id, s.user_id, s.title, s.start_time, s.end_time,
                       s.notes, s.created_at,
                       COALESCE(e.common_name, e.make || ' ' || e.model) as equipment_name,
                       u.full_name as user_name, u.username
                FROM schedules s
                LEFT JOIN equipment e ON e.id = s.equipment_id
                LEFT JOIN users u ON u.id = s.user_id
                WHERE {where}
                ORDER BY s.start_time""",
            *params
        )
    return rows_to_list(rows)

@app.post("/api/schedules", status_code=201)
async def create_schedule(data: ScheduleCreate, current_user=Depends(get_current_user)):
    # Validate duration: min 15 min, max 24 hours
    try:
        start = datetime.fromisoformat(data.start_time.replace("Z", "+00:00"))
        end   = datetime.fromisoformat(data.end_time.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "Invalid datetime format")
    duration = (end - start).total_seconds()
    if duration < 900:
        raise HTTPException(400, "Minimum booking duration is 15 minutes")
    if duration > 86400:
        raise HTTPException(400, "Maximum booking duration is 24 hours")
    if end <= start:
        raise HTTPException(400, "End time must be after start time")

    async with db_conn() as conn:
        # Verify equipment is schedulable
        equip = await conn.fetchrow("SELECT schedulable FROM equipment WHERE id=$1", data.equipment_id)
        if not equip:
            raise HTTPException(404, "Equipment not found")
        if not equip["schedulable"]:
            raise HTTPException(400, "This equipment is not enabled for scheduling")
        try:
            row = await conn.fetchrow(
                """INSERT INTO schedules (equipment_id, user_id, title, start_time, end_time, notes)
                   VALUES ($1, $2, $3, $4, $5, $6) RETURNING *""",
                data.equipment_id, current_user["id"], data.title,
                start, end, data.notes
            )
        except asyncpg.ExclusionViolationError:
            raise HTTPException(409, "This time slot conflicts with an existing booking")
    result = row_to_dict(row)
    await fire_notification("schedule.booked", {"schedule_id": str(result.get("id")), "title": data.title, "equipment_id": data.equipment_id, "by": current_user.get("username")})
    return result

@app.delete("/api/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: str, current_user=Depends(get_current_user)):
    async with db_conn() as conn:
        row = await conn.fetchrow("SELECT user_id FROM schedules WHERE id=$1", schedule_id)
        if not row:
            raise HTTPException(404, "Schedule not found")
        # Only owner, admin, or superadmin can delete
        if str(row["user_id"]) != current_user["id"] and current_user["role"] not in ("admin", "superadmin"):
            raise HTTPException(403, "You can only cancel your own bookings")
        await conn.execute("DELETE FROM schedules WHERE id=$1", schedule_id)

# ── Auth Sessions ──

@app.get("/api/auth-sessions")
async def list_auth_sessions(
    equipment_id: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    current_user=Depends(get_current_user)
):
    conditions = ["1=1"]
    params = []
    i = 1
    if equipment_id:
        conditions.append(f"a.equipment_id = ${i}"); params.append(equipment_id); i += 1
    if from_time:
        conditions.append(f"a.end_time > ${i}"); params.append(from_time); i += 1
    if to_time:
        conditions.append(f"a.start_time < ${i}"); params.append(to_time); i += 1
    where = " AND ".join(conditions)
    async with db_conn() as conn:
        rows = await conn.fetch(
            f"""SELECT a.id, a.equipment_ids, a.authorizer_id, a.title, a.description,
                       a.start_time, a.end_time, a.total_slots, a.created_at,
                       u.full_name as authorizer_name, u.username as authorizer_username,
                       COUNT(en.id) as enrolled_count,
                       COALESCE(
                         json_agg(json_build_object('user_id',en.user_id::text,'enrolled_at',en.enrolled_at)
                           ORDER BY en.enrolled_at) FILTER (WHERE en.id IS NOT NULL),
                         '[]'::json
                       ) as enrollments
                FROM auth_sessions a
                LEFT JOIN users u ON u.id = a.authorizer_id
                LEFT JOIN auth_enrollments en ON en.session_id = a.id
                WHERE {where}
                GROUP BY a.id, u.id
                ORDER BY a.start_time""",
            *params
        )
    return rows_to_list(rows)

@app.get("/api/auth-sessions/{session_id}")
async def get_auth_session(session_id: str, current_user=Depends(get_current_user)):
    async with db_conn() as conn:
        row = await conn.fetchrow(
            """SELECT a.id, a.equipment_ids, a.authorizer_id, a.title, a.description,
                      a.start_time, a.end_time, a.total_slots, a.created_at,
                      u.full_name as authorizer_name,
                      COUNT(en.id) as enrolled_count
               FROM auth_sessions a
               LEFT JOIN users u ON u.id = a.authorizer_id
               LEFT JOIN auth_enrollments en ON en.session_id = a.id
               WHERE a.id = $1
               GROUP BY a.id, u.id""",
            session_id
        )
        if not row:
            raise HTTPException(404, "Session not found")
        enrollments = await conn.fetch(
            """SELECT en.id, en.user_id, en.enrolled_at, u.full_name, u.username
               FROM auth_enrollments en
               JOIN users u ON u.id = en.user_id
               WHERE en.session_id = $1
               ORDER BY en.enrolled_at""",
            session_id
        )
    result = row_to_dict(row)
    result["enrollments"] = rows_to_list(enrollments)
    return result

@app.post("/api/auth-sessions", status_code=201)
async def create_auth_session(data: AuthSessionCreate, current_user=Depends(require_authorizer())):
    try:
        start = datetime.fromisoformat(data.start_time.replace("Z", "+00:00"))
        end   = datetime.fromisoformat(data.end_time.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "Invalid datetime format")
    if end <= start:
        raise HTTPException(400, "End time must be after start time")
    if data.total_slots < 1:
        raise HTTPException(400, "Must have at least 1 slot")
    async with db_conn() as conn:
        row = await conn.fetchrow(
            """INSERT INTO auth_sessions (equipment_ids, authorizer_id, title, description, start_time, end_time, total_slots)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
            data.equipment_ids or [], current_user["id"], data.title, data.description,
            start, end, data.total_slots
        )
    result = row_to_dict(row)
    await fire_notification("auth_session.created", {"session_id": str(result.get("id")), "title": data.title, "by": current_user.get("username")})
    return result

@app.patch("/api/auth-sessions/{session_id}")
async def update_auth_session(session_id: str, data: AuthSessionUpdate, current_user=Depends(require_authorizer())):
    async with db_conn() as conn:
        existing = await conn.fetchrow("SELECT authorizer_id FROM auth_sessions WHERE id=$1", session_id)
        if not existing:
            raise HTTPException(404, "Session not found")
        if str(existing["authorizer_id"]) != current_user["id"] and current_user["role"] not in ("admin", "superadmin"):
            raise HTTPException(403, "Only the session authorizer can edit this session")
        updates = {}
        if data.title is not None: updates["title"] = data.title
        if data.description is not None: updates["description"] = data.description
        if data.equipment_ids is not None: updates["equipment_ids"] = data.equipment_ids
        if data.total_slots is not None: updates["total_slots"] = data.total_slots
        if data.start_time is not None:
            updates["start_time"] = datetime.fromisoformat(data.start_time.replace("Z", "+00:00"))
        if data.end_time is not None:
            updates["end_time"] = datetime.fromisoformat(data.end_time.replace("Z", "+00:00"))
        if not updates:
            raise HTTPException(400, "No fields to update")
        _DATE_COLS = {"build_date"}
        set_clauses = ", ".join(
            f"{k}=${i+2}::date" if k in _DATE_COLS else f"{k}=${i+2}"
            for i, k in enumerate(updates)
        )
        row = await conn.fetchrow(
            f"UPDATE auth_sessions SET {set_clauses} WHERE id=$1 RETURNING *",
            session_id, *updates.values()
        )
    result = row_to_dict(row)
    await fire_notification("auth_session.modified", {"session_id": session_id, "fields": list(updates.keys()), "by": current_user.get("username")})
    return result

@app.delete("/api/auth-sessions/{session_id}", status_code=204)
async def delete_auth_session(session_id: str, current_user=Depends(require_authorizer())):
    async with db_conn() as conn:
        existing = await conn.fetchrow("SELECT authorizer_id FROM auth_sessions WHERE id=$1", session_id)
        if not existing:
            raise HTTPException(404, "Session not found")
        if str(existing["authorizer_id"]) != current_user["id"] and current_user["role"] not in ("admin", "superadmin"):
            raise HTTPException(403, "Only the session authorizer can delete this session")
        await conn.execute("DELETE FROM auth_sessions WHERE id=$1", session_id)

@app.post("/api/auth-sessions/{session_id}/enroll", status_code=201)
async def enroll_in_session(session_id: str, current_user=Depends(get_current_user)):
    async with db_conn() as conn:
        session = await conn.fetchrow(
            """SELECT a.total_slots, COUNT(en.id) as enrolled_count
               FROM auth_sessions a
               LEFT JOIN auth_enrollments en ON en.session_id = a.id
               WHERE a.id = $1
               GROUP BY a.id""",
            session_id
        )
        if not session:
            raise HTTPException(404, "Session not found")
        if session["enrolled_count"] >= session["total_slots"]:
            raise HTTPException(409, "This session is full")
        try:
            row = await conn.fetchrow(
                "INSERT INTO auth_enrollments (session_id, user_id) VALUES ($1, $2) RETURNING *",
                session_id, current_user["id"]
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "You are already enrolled in this session")
    result = row_to_dict(row)
    await fire_notification("auth_session.enrollment", {"session_id": session_id, "user": current_user.get("username")})
    return result

@app.delete("/api/auth-sessions/{session_id}/enroll", status_code=204)
async def unenroll_from_session(session_id: str, current_user=Depends(get_current_user)):
    async with db_conn() as conn:
        result = await conn.execute(
            "DELETE FROM auth_enrollments WHERE session_id=$1 AND user_id=$2",
            session_id, current_user["id"]
        )
        if result == "DELETE 0":
            raise HTTPException(404, "Enrollment not found")


# ─────────────────────────────────────────
# EQUIPMENT GROUPS
# ─────────────────────────────────────────

class EquipGroupCreate(BaseModel):
    name: str
    description: Optional[str] = None
    area_id: Optional[str] = None
    equipment_ids: List[str] = []

class EquipGroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    area_id: Optional[str] = None
    equipment_ids: Optional[List[str]] = None

@app.get("/api/equipment-groups")
async def list_equipment_groups(current_user=Depends(get_current_user)):
    async with db_conn() as conn:
        rows = await conn.fetch(
            """SELECT g.id, g.name, g.description, g.area_id, g.created_at,
                      a.name as area_name,
                      COALESCE(
                        json_agg(json_build_object(
                          'id', e.id::text,
                          'common_name', e.common_name,
                          'make', e.make, 'model', e.model,
                          'status', e.status
                        ) ORDER BY gm.sort_order) FILTER (WHERE e.id IS NOT NULL),
                        '[]'::json
                      ) as equipment
               FROM equipment_groups g
               LEFT JOIN areas a ON a.id = g.area_id
               LEFT JOIN equipment_group_members gm ON gm.group_id = g.id
               LEFT JOIN equipment e ON e.id = gm.equipment_id
               GROUP BY g.id, a.id
               ORDER BY g.name"""
        )
    return rows_to_list(rows)

@app.post("/api/equipment-groups", status_code=201)
async def create_equipment_group(data: EquipGroupCreate, current_user=Depends(check_perm("groups.manage"))):
    async with db_conn() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO equipment_groups (name, description, area_id) VALUES ($1,$2,$3) RETURNING *",
                data.name, data.description, data.area_id
            )
            for i, eid in enumerate(data.equipment_ids):
                await conn.execute(
                    "INSERT INTO equipment_group_members (group_id, equipment_id, sort_order) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                    str(row["id"]), eid, i
                )
    return row_to_dict(row)

@app.patch("/api/equipment-groups/{group_id}")
async def update_equipment_group(group_id: str, data: EquipGroupUpdate, current_user=Depends(check_perm("groups.manage"))):
    async with db_conn() as conn:
        async with conn.transaction():
            updates = {}
            if data.name is not None: updates["name"] = data.name
            if data.description is not None: updates["description"] = data.description
            if data.area_id is not None: updates["area_id"] = data.area_id
            if updates:
                set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
                await conn.execute(f"UPDATE equipment_groups SET {set_clauses} WHERE id=$1", group_id, *updates.values())
            if data.equipment_ids is not None:
                await conn.execute("DELETE FROM equipment_group_members WHERE group_id=$1", group_id)
                for i, eid in enumerate(data.equipment_ids):
                    await conn.execute(
                        "INSERT INTO equipment_group_members (group_id, equipment_id, sort_order) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                        group_id, eid, i
                    )
    return await get_equipment_group_by_id(group_id, conn=None)

async def get_equipment_group_by_id(group_id, conn=None):
    async with db_conn() as c:
        row = await c.fetchrow(
            """SELECT g.id, g.name, g.description, g.area_id, g.created_at,
                      COALESCE(json_agg(json_build_object('id',e.id::text,'common_name',e.common_name,'make',e.make,'model',e.model,'status',e.status) ORDER BY gm.sort_order) FILTER (WHERE e.id IS NOT NULL),'[]'::json) as equipment
               FROM equipment_groups g
               LEFT JOIN equipment_group_members gm ON gm.group_id=g.id
               LEFT JOIN equipment e ON e.id=gm.equipment_id
               WHERE g.id=$1 GROUP BY g.id""", group_id)
    return row_to_dict(row)

@app.delete("/api/equipment-groups/{group_id}", status_code=204)
async def delete_equipment_group(group_id: str, current_user=Depends(check_perm("groups.manage"))):
    async with db_conn() as conn:
        await conn.execute("DELETE FROM equipment_groups WHERE id=$1", group_id)


# ─────────────────────────────────────────
# CSV EXPORT / IMPORT
# ─────────────────────────────────────────
import csv, io

@app.get("/api/export/csv/{entity}")
async def export_csv(entity: str, current_user=Depends(require_superadmin())):
    valid = {"areas", "equipment", "users", "tickets", "schedules", "auth_sessions"}
    if entity not in valid:
        raise HTTPException(400, f"Unknown entity. Valid: {', '.join(sorted(valid))}")
    async with db_conn() as conn:
        if entity == "areas":
            rows = await conn.fetch("SELECT id, name, description, created_at FROM areas ORDER BY name")
            fields = ["id","name","description","created_at"]
        elif entity == "equipment":
            rows = await conn.fetch("""SELECT e.id, e.common_name, e.make, e.model, e.serial_number,
                e.build_date, e.status, e.schedulable, a.name as area_name, e.created_at
                FROM equipment e LEFT JOIN areas a ON a.id=e.area_id ORDER BY e.make,e.model""")
            fields = ["id","common_name","make","model","serial_number","build_date","status","schedulable","area_name","created_at"]
        elif entity == "users":
            rows = await conn.fetch("SELECT id, username, full_name, role, is_active, created_at FROM users ORDER BY username")
            fields = ["id","username","full_name","role","is_active","created_at"]
        elif entity == "tickets":
            rows = await conn.fetch("""SELECT t.id, t.ticket_number, t.title, t.status, t.priority,
                t.opened_by, t.assigned_to, e.common_name as equipment_name, t.created_at, t.updated_at
                FROM repair_tickets t LEFT JOIN equipment e ON e.id=t.equipment_id ORDER BY t.created_at DESC""")
            fields = ["id","ticket_number","title","status","priority","opened_by","assigned_to","equipment_name","created_at","updated_at"]
        elif entity == "schedules":
            rows = await conn.fetch("""SELECT s.id, s.start_time, s.end_time, s.title, s.notes,
                COALESCE(e.common_name, e.make||' '||e.model) as equipment_name,
                u.username as booked_by, s.created_at
                FROM schedules s LEFT JOIN equipment e ON e.id=s.equipment_id
                LEFT JOIN users u ON u.id=s.user_id ORDER BY s.start_time""")
            fields = ["id","start_time","end_time","title","notes","equipment_name","booked_by","created_at"]
        elif entity == "auth_sessions":
            rows = await conn.fetch("""SELECT a.id, a.title, a.description, a.start_time, a.end_time,
                a.total_slots, u.username as authorizer, a.created_at
                FROM auth_sessions a LEFT JOIN users u ON u.id=a.authorizer_id ORDER BY a.start_time""")
            fields = ["id","title","description","start_time","end_time","total_slots","authorizer","created_at"]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        d = {}
        for f in fields:
            v = row[f]
            if hasattr(v, "isoformat"): v = v.isoformat()
            d[f] = v if v is not None else ""
        writer.writerow(d)
    content = output.getvalue()
    from fastapi.responses import Response
    return Response(content=content, media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={entity}-export.csv"})

@app.get("/api/export/csv-template/{entity}")
async def csv_template(entity: str, current_user=Depends(require_superadmin())):
    templates = {
        "areas":      ["name","description"],
        "equipment":  ["common_name","make","model","serial_number","build_date","status","area_name"],
        "users":      ["username","full_name","role","password"],
        "schedules":  ["equipment_name","start_time","end_time","title","notes","booked_by_username"],
    }
    if entity not in templates:
        raise HTTPException(400, "No template available for this entity")
    output = io.StringIO()
    csv.DictWriter(output, fieldnames=templates[entity]).writeheader()
    from fastapi.responses import Response
    return Response(content=output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={entity}-template.csv"})

@app.post("/api/import/csv/{entity}", status_code=200)
async def import_csv(entity: str, file: UploadFile = File(...), current_user=Depends(require_superadmin())):
    valid = {"areas", "equipment", "users"}
    if entity not in valid:
        raise HTTPException(400, f"Import supported for: {', '.join(sorted(valid))}")
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
        reader = list(csv.DictReader(io.StringIO(text)))
    except Exception as e:
        raise HTTPException(400, f"Could not parse CSV: {e}")
    if not reader:
        raise HTTPException(400, "CSV is empty or has no data rows")

    created = 0; skipped = 0; errors = []
    async with db_conn() as conn:
        if entity == "areas":
            for i, row in enumerate(reader, 2):
                name = (row.get("name") or "").strip()
                if not name: errors.append(f"Row {i}: name required"); skipped+=1; continue
                try:
                    await conn.execute("INSERT INTO areas (name, description) VALUES ($1,$2) ON CONFLICT (name) DO NOTHING",
                        name, (row.get("description") or "").strip() or None)
                    created += 1
                except Exception as e: errors.append(f"Row {i}: {e}"); skipped+=1

        elif entity == "equipment":
            area_map = {r["name"]: str(r["id"]) for r in await conn.fetch("SELECT id, name FROM areas")}
            for i, row in enumerate(reader, 2):
                make = (row.get("make") or "").strip()
                model = (row.get("model") or "").strip()
                serial = (row.get("serial_number") or "").strip()
                if not make or not model or not serial:
                    errors.append(f"Row {i}: make, model, serial_number required"); skipped+=1; continue
                area_id = area_map.get((row.get("area_name") or "").strip())
                status = (row.get("status") or "active").strip()
                if status not in ("active","inactive","under_repair","decommissioned"): status = "active"
                build_date_str = (row.get("build_date") or "").strip() or None
                build_date_val = None
                if build_date_str:
                    try: build_date_val = date_type.fromisoformat(build_date_str)
                    except: pass
                try:
                    await conn.execute(
                        """INSERT INTO equipment (common_name,make,model,serial_number,build_date,status,area_id)
                           VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING""",
                        (row.get("common_name") or "").strip() or None, make, model, serial,
                        build_date_val, status, area_id)
                    created += 1
                except Exception as e: errors.append(f"Row {i}: {e}"); skipped+=1

        elif entity == "users":
            for i, row in enumerate(reader, 2):
                username = (row.get("username") or "").strip()
                password = (row.get("password") or "").strip()
                if not username or not password:
                    errors.append(f"Row {i}: username and password required"); skipped+=1; continue
                role = (row.get("role") or "member").strip()
                if role not in ("superadmin","admin","area_host","technician","authorizer","viewer","member"): role = "member"
                pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                try:
                    await conn.execute(
                        "INSERT INTO users (username,full_name,role,password_hash) VALUES ($1,$2,$3,$4) ON CONFLICT (username) DO NOTHING",
                        username, (row.get("full_name") or "").strip() or None, role, pw_hash)
                    created += 1
                except Exception as e: errors.append(f"Row {i}: {e}"); skipped+=1

    return {"created": created, "skipped": skipped, "errors": errors[:20]}
# ─────────────────────────────────────────
# STATS / DASHBOARD
# ─────────────────────────────────────────
@app.get("/api/stats")
async def get_stats():
    async with db_conn() as conn:
        stats = await conn.fetchrow("""
            SELECT
              (SELECT COUNT(*) FROM equipment) as total_equipment,
              (SELECT COUNT(*) FROM equipment WHERE status = 'active') as active_equipment,
              (SELECT COUNT(*) FROM equipment WHERE status = 'under_repair') as under_repair,
              (SELECT COUNT(*) FROM repair_tickets WHERE status != 'closed') as open_tickets,
              (SELECT COUNT(*) FROM repair_tickets WHERE status != 'closed' AND priority IN ('high','critical')) as critical_tickets,
              (SELECT COUNT(*) FROM areas) as total_areas
        """)
        area_breakdown = await conn.fetch("""
            SELECT a.name, COUNT(e.id) as count,
                   SUM(CASE WHEN e.status = 'under_repair' THEN 1 ELSE 0 END) as in_repair
            FROM areas a
            LEFT JOIN equipment e ON e.area_id = a.id
            GROUP BY a.id, a.name ORDER BY a.name
        """)
    return {
        "summary": row_to_dict(stats),
        "areas": rows_to_list(area_breakdown)
    }

# ─────────────────────────────────────────

# ─────────────────────────────────────────
# PERMISSIONS MANAGEMENT
# ─────────────────────────────────────────

@app.get("/api/permissions/defs")
async def get_permission_defs(current_user=Depends(get_current_user)):
    perm_cfg = await load_perm_config()
    role_grants = {}
    for role in ROLE_HIERARCHY:
        role_grants[role] = perm_cfg.get("role_grants", {}).get(
            role, DEFAULT_ROLE_PERMISSIONS.get(role, [])
        )
    return {
        "defs": PERMISSION_DEFS,
        "roles": ROLE_HIERARCHY,
        "role_grants": role_grants,
        "user_grants": perm_cfg.get("user_grants", {}),
        "defaults": DEFAULT_ROLE_PERMISSIONS,
    }

@app.put("/api/permissions/roles")
async def update_role_permissions(data: dict, current_user=Depends(require_superadmin())):
    perm_cfg = await load_perm_config()
    perm_cfg["role_grants"] = data
    async with db_conn() as conn:
        await conn.execute(
            "INSERT INTO app_config(key,value) VALUES('permissions',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=$1::jsonb",
            json.dumps(perm_cfg)
        )
    return {"ok": True}

@app.put("/api/permissions/users/{user_id}")
async def update_user_perms(user_id: str, data: dict, current_user=Depends(require_superadmin())):
    perm_cfg = await load_perm_config()
    if "user_grants" not in perm_cfg:
        perm_cfg["user_grants"] = {}
    if data.get("grant") or data.get("deny"):
        perm_cfg["user_grants"][user_id] = {
            "grant": list(set(data.get("grant", []))),
            "deny":  list(set(data.get("deny", []))),
        }
    else:
        perm_cfg["user_grants"].pop(user_id, None)
    async with db_conn() as conn:
        await conn.execute(
            "INSERT INTO app_config(key,value) VALUES('permissions',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=$1::jsonb",
            json.dumps(perm_cfg)
        )
    return {"ok": True}

@app.get("/api/permissions/users/{user_id}")
async def get_user_perms(user_id: str, current_user=Depends(require_superadmin())):
    perm_cfg = await load_perm_config()
    overrides = perm_cfg.get("user_grants", {}).get(str(user_id), {"grant": [], "deny": []})
    async with db_conn() as conn:
        row = await conn.fetchrow("SELECT id, role FROM users WHERE id=$1", user_id)
    if not row:
        raise HTTPException(404, "User not found")
    effective = compute_permissions(row["role"], user_id, perm_cfg)
    return {"overrides": overrides, "effective": effective}

@app.post("/api/permissions/reset-role/{role}")
async def reset_role_defaults(role: str, current_user=Depends(require_superadmin())):
    if role not in ROLE_HIERARCHY:
        raise HTTPException(400, f"Unknown role: {role}")
    perm_cfg = await load_perm_config()
    perm_cfg.setdefault("role_grants", {}).pop(role, None)
    async with db_conn() as conn:
        await conn.execute(
            "INSERT INTO app_config(key,value) VALUES('permissions',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=$1::jsonb",
            json.dumps(perm_cfg)
        )
    return {"ok": True, "defaults": DEFAULT_ROLE_PERMISSIONS.get(role, [])}

# ─────────────────────────────────────────
# NOTIFICATIONS SYSTEM
# ─────────────────────────────────────────

NOTIFICATION_EVENT_DEFS = {
    # Equipment
    "equipment.created":       {"label": "Equipment created",       "group": "Equipment"},
    "equipment.modified":      {"label": "Equipment modified",      "group": "Equipment"},
    "area.created":            {"label": "Area created",            "group": "Equipment"},
    "area.modified":           {"label": "Area modified",           "group": "Equipment"},
    "ticket.created":          {"label": "Ticket created",          "group": "Equipment"},
    "ticket.modified":         {"label": "Ticket modified",         "group": "Equipment"},
    "ticket.closed":           {"label": "Ticket closed",           "group": "Equipment"},
    # Scheduling
    "schedule.booked":         {"label": "Time slot booked",        "group": "Scheduling"},
    "schedule.reminder":       {"label": "Schedule reminder",       "group": "Scheduling"},
    # Authorizations
    "auth_session.created":    {"label": "Auth session posted",     "group": "Authorizations"},
    "auth_session.modified":   {"label": "Auth session modified",   "group": "Authorizations"},
    "auth_session.enrollment": {"label": "User enrolled in session","group": "Authorizations"},
    "auth_session.fill_alert": {"label": "Fill-rate alert",         "group": "Authorizations"},
    "auth_session.reminder":   {"label": "Auth session reminder",   "group": "Authorizations"},
    # Maintenance
    "maintenance.created":     {"label": "Maintenance scheduled",   "group": "Maintenance"},
    "maintenance.due":         {"label": "Maintenance event due",   "group": "Maintenance"},
    "maintenance.completed":   {"label": "Maintenance completed",   "group": "Maintenance"},
    "maintenance.overdue":     {"label": "Maintenance overdue",     "group": "Maintenance"},
    "maintenance.summary":     {"label": "Maintenance summary digest","group": "Maintenance"},
}

NOTIFICATION_CHANNEL_DEFS = {
    "email": {
        "label": "Email (SMTP)",
        "fields": [
            {"key": "smtp_host", "label": "SMTP Host", "type": "text", "required": True, "hint": "e.g. smtp.gmail.com"},
            {"key": "smtp_port", "label": "SMTP Port", "type": "number", "required": True, "hint": "587 (TLS) or 465 (SSL)"},
            {"key": "smtp_user", "label": "SMTP Username", "type": "text", "required": True},
            {"key": "smtp_pass", "label": "SMTP Password", "type": "password", "required": True},
            {"key": "from_address", "label": "From Address", "type": "text", "required": True, "hint": "noreply@example.com"},
            {"key": "tls", "label": "Use TLS", "type": "boolean", "required": False},
        ],
    },
    "push": {
        "label": "Push Notifications",
        "fields": [
            {"key": "provider", "label": "Push Provider", "type": "text", "required": True, "hint": "e.g. ntfy, pushover, gotify"},
            {"key": "server_url", "label": "Server URL", "type": "url", "required": False, "hint": "e.g. https://ntfy.sh"},
            {"key": "api_key", "label": "API Key / Token", "type": "password", "required": True},
            {"key": "topic", "label": "Default Topic / Channel", "type": "text", "required": False},
        ],
    },
    "webhook": {
        "label": "Webhooks",
        "fields": [],
    },
}

DEFAULT_NOTIFICATION_CONFIG = {
    "channels": {
        "email":   {"enabled": False, "config": {}},
        "push":    {"enabled": False, "config": {}},
        "webhook": {"enabled": False, "config": {}},
    },
    "webhooks": [],
    "events": {},
    "role_routing": {},
}


async def load_notification_config() -> dict:
    """Load notification config from DB."""
    try:
        async with db_conn() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM app_config WHERE key='notifications'"
            )
        if row:
            val = row["value"]
            if isinstance(val, str):
                val = json.loads(val)
            return val or {}
    except Exception:
        pass
    return {}


async def fire_notification(event_type: str, payload: dict):
    """Notification dispatcher. Reads config, dispatches to enabled channels.
    Webhooks (generic + Discord) are delivered via httpx POST."""
    try:
        config = await load_notification_config()
        events_cfg = config.get("events", {})
        event_cfg = events_cfg.get(event_type, {})

        # Check if any channel is enabled for this event
        channels = config.get("channels", {})
        dispatched_to = []
        for ch_key in ("email", "push", "webhook"):
            ch = channels.get(ch_key, {})
            if ch.get("enabled") and event_cfg.get(ch_key):
                dispatched_to.append(ch_key)

        if dispatched_to:
            import logging
            logging.getLogger("notifications").info(
                f"NOTIFY [{event_type}] → {dispatched_to} | {json.dumps(payload, default=str)[:500]}"
            )

        # Webhook dispatch (generic + Discord)
        if "webhook" in dispatched_to:
            webhooks = config.get("webhooks", [])
            for wh in webhooks:
                if not wh.get("enabled"):
                    continue
                wh_events = wh.get("events", [])
                if wh_events and "*" not in wh_events and event_type not in wh_events:
                    continue
                await _dispatch_webhook(wh, event_type, payload)
    except Exception:
        import traceback
        traceback.print_exc()


# ── Discord embed colors per event group ──
DISCORD_EMBED_COLORS = {
    "Equipment":      0x5865F2,   # blurple
    "Scheduling":     0x57F287,   # green
    "Authorizations": 0xFEE75C,   # yellow
    "Maintenance":    0xE67E22,   # orange
    "test":           0xEB459E,   # fuchsia
}

DISCORD_EVENT_ICONS = {
    "equipment.created":       "🔧",
    "equipment.modified":      "✏️",
    "area.created":            "📍",
    "area.modified":           "📝",
    "ticket.created":          "🎫",
    "ticket.modified":         "🔄",
    "ticket.closed":           "✅",
    "schedule.booked":         "📅",
    "schedule.reminder":       "⏰",
    "auth_session.created":    "🔑",
    "auth_session.modified":   "🔑",
    "auth_session.enrollment": "👤",
    "auth_session.fill_alert": "⚠️",
    "auth_session.reminder":   "⏰",
    "maintenance.created":     "🛠️",
    "maintenance.due":         "📋",
    "maintenance.completed":   "✅",
    "maintenance.overdue":     "🚨",
    "maintenance.summary":     "📊",
    "test":                    "🧪",
}


def _build_discord_embed(event_type: str, payload: dict) -> dict:
    """Build a Discord-rich embed object for a notification event."""
    event_def = NOTIFICATION_EVENT_DEFS.get(event_type, {})
    group = event_def.get("group", "test")
    label = event_def.get("label", event_type)
    icon  = DISCORD_EVENT_ICONS.get(event_type, "📣")
    color = DISCORD_EMBED_COLORS.get(group, 0x95A5A6)

    # Build human-readable field list from payload
    fields = []
    skip_keys = {"event", "message"}
    for k, v in payload.items():
        if k in skip_keys:
            continue
        display_key = k.replace("_", " ").title()
        display_val = ", ".join(v) if isinstance(v, list) else str(v)
        if display_val:
            fields.append({"name": display_key, "value": display_val, "inline": True})

    embed = {
        "title": f"{icon}  {label}",
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "PurpleAssetOne Notifications"},
    }

    # Use payload "message" as description if present, otherwise build one
    if payload.get("message"):
        embed["description"] = payload["message"]
    elif payload.get("by"):
        embed["description"] = f"Triggered by **{payload['by']}**"

    if fields:
        embed["fields"] = fields[:25]  # Discord max 25 fields

    return embed


def _build_discord_payload(wh: dict, event_type: str, payload: dict) -> dict:
    """Build complete Discord webhook JSON payload."""
    embed = _build_discord_embed(event_type, payload)
    body = {"embeds": [embed]}
    if wh.get("discord_username"):
        body["username"] = wh["discord_username"]
    if wh.get("discord_avatar_url"):
        body["avatar_url"] = wh["discord_avatar_url"]
    return body


def _build_generic_payload(event_type: str, payload: dict) -> dict:
    """Build a generic webhook JSON payload."""
    return {
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }


async def _dispatch_webhook(wh: dict, event_type: str, payload: dict):
    """POST a notification to a single webhook endpoint (Discord or generic)."""
    import httpx
    import hmac
    import hashlib
    import logging
    log = logging.getLogger("notifications")

    url = wh.get("url", "").strip()
    if not url:
        return

    wh_type = wh.get("type", "generic")
    try:
        if wh_type == "discord":
            body = _build_discord_payload(wh, event_type, payload)
            # Discord expects ?wait=true for error feedback
            post_url = url.split("?")[0] + "?wait=true"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(post_url, json=body)
            if resp.status_code in (200, 204):
                log.info(f"  DISCORD → {wh.get('name','?')} OK ({resp.status_code})")
            else:
                log.warning(f"  DISCORD → {wh.get('name','?')} FAIL {resp.status_code}: {resp.text[:300]}")
        else:
            # Generic webhook with optional HMAC signing
            body = _build_generic_payload(event_type, payload)
            body_bytes = json.dumps(body, default=str).encode()
            headers = {"Content-Type": "application/json"}
            secret = wh.get("secret", "")
            if secret:
                sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
                headers["X-Signature"] = f"sha256={sig}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, content=body_bytes, headers=headers)
            if resp.status_code < 300:
                log.info(f"  WEBHOOK → {wh.get('name','?')} OK ({resp.status_code})")
            else:
                log.warning(f"  WEBHOOK → {wh.get('name','?')} FAIL {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        log.error(f"  WEBHOOK → {wh.get('name','?')} ERROR: {e}")


@app.get("/api/notifications-config")
async def get_notifications_config(current_user=Depends(check_perm("system.notifications"))):
    config = await load_notification_config()
    return {
        "event_defs": NOTIFICATION_EVENT_DEFS,
        "channel_defs": NOTIFICATION_CHANNEL_DEFS,
        "roles": ROLE_HIERARCHY,
        "channels": config.get("channels", DEFAULT_NOTIFICATION_CONFIG["channels"]),
        "webhooks": config.get("webhooks", []),
        "events": config.get("events", {}),
        "role_routing": config.get("role_routing", {}),
    }


@app.put("/api/notifications-config")
async def update_notifications_config(data: dict, current_user=Depends(check_perm("system.notifications"))):
    async with db_conn() as conn:
        await conn.execute(
            "INSERT INTO app_config(key,value) VALUES('notifications',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=$1::jsonb",
            json.dumps(data)
        )
    return {"ok": True}


@app.post("/api/notifications/test")
async def test_notification(data: dict, current_user=Depends(check_perm("system.notifications"))):
    """Send a test notification event for validating channel configuration."""
    channel = data.get("channel", "webhook")
    test_payload = {
        "event": "test",
        "message": f"Test notification from PurpleAssetOne sent by {current_user.get('username', '?')}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await fire_notification("test", test_payload)
    return {"ok": True, "channel": channel, "note": "Test event dispatched. Check server logs for delivery details."}


@app.post("/api/notifications/test-webhook")
async def test_single_webhook(data: dict, current_user=Depends(check_perm("system.notifications"))):
    """Send a test message directly to a single webhook URL for validation.
    Expects: {url, type, name?, discord_username?, discord_avatar_url?}"""
    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Webhook URL is required")
    wh = {
        "url": url,
        "type": data.get("type", "generic"),
        "name": data.get("name", "Test"),
        "secret": data.get("secret", ""),
        "discord_username": data.get("discord_username", ""),
        "discord_avatar_url": data.get("discord_avatar_url", ""),
        "enabled": True,
    }
    test_payload = {
        "event": "test",
        "message": f"Test notification from PurpleAssetOne sent by {current_user.get('username', '?')}",
        "by": current_user.get("username", "?"),
    }
    try:
        await _dispatch_webhook(wh, "test", test_payload)
        return {"ok": True, "note": f"Test message sent to {wh['type']} webhook."}
    except Exception as e:
        raise HTTPException(500, f"Webhook delivery failed: {e}")


# ─────────────────────────────────────────
# AUTH PROVIDER CONFIGURATION
# ─────────────────────────────────────────

AUTH_PROVIDER_DEFS = {
    "local": {"label":"Local (built-in)","description":"Username/password in PA1 database.","fields":[]},
    "oidc": {
        "label":"OpenID Connect / OAuth2",
        "description":"Authenticate via any OIDC provider (Authentik, Authelia, Azure B2C, Okta, etc.)",
        "fields":[
            {"key":"issuer_url","label":"Issuer URL","type":"url","required":True,"hint":"e.g. https://authentik.example.com/application/o/pa1/"},
            {"key":"client_id","label":"Client ID","type":"text","required":True},
            {"key":"client_secret","label":"Client Secret","type":"password","required":True},
            {"key":"scopes","label":"Scopes","type":"text","required":False,"hint":"Space-separated, default: openid email profile"},
            {"key":"username_claim","label":"Username Claim","type":"text","required":False,"hint":"JWT claim used as username (default: preferred_username)"},
            {"key":"role_claim","label":"Role Claim (optional)","type":"text","required":False},
            {"key":"role_map","label":"Role Mapping (JSON)","type":"json","required":False,"hint":"{\"oidc_group\": \"admin\"}"},
            {"key":"allow_signup","label":"Auto-provision users","type":"boolean","required":False},
        ],
    },
    "ldap": {
        "label":"LDAP / Active Directory",
        "description":"Authenticate against LDAP or Active Directory.",
        "fields":[
            {"key":"host","label":"LDAP Host","type":"text","required":True,"hint":"ldap://dc.example.com or ldaps://..."},
            {"key":"port","label":"Port","type":"number","required":False,"hint":"389 or 636"},
            {"key":"base_dn","label":"Base DN","type":"text","required":True,"hint":"dc=example,dc=com"},
            {"key":"bind_dn","label":"Bind DN","type":"text","required":True},
            {"key":"bind_password","label":"Bind Password","type":"password","required":True},
            {"key":"user_filter","label":"User Filter","type":"text","required":False,"hint":"(objectClass=user)"},
            {"key":"username_attr","label":"Username Attribute","type":"text","required":False,"hint":"sAMAccountName"},
            {"key":"group_dn","label":"Group Base DN","type":"text","required":False},
            {"key":"role_map","label":"Group → Role Mapping (JSON)","type":"json","required":False},
            {"key":"allow_signup","label":"Auto-provision users","type":"boolean","required":False},
        ],
    },
    "saml": {
        "label":"SAML 2.0",
        "description":"Enterprise SAML (Azure AD, ADFS, Okta, etc.)",
        "fields":[
            {"key":"idp_metadata_url","label":"IdP Metadata URL","type":"url","required":True},
            {"key":"sp_entity_id","label":"SP Entity ID","type":"text","required":True},
            {"key":"acs_url","label":"ACS URL","type":"url","required":True,"hint":"https://your-pa1-host/api/auth/saml/acs"},
            {"key":"username_attr","label":"Username Attribute","type":"text","required":False},
            {"key":"role_attr","label":"Role Attribute","type":"text","required":False},
            {"key":"role_map","label":"Role Mapping (JSON)","type":"json","required":False},
            {"key":"allow_signup","label":"Auto-provision users","type":"boolean","required":False},
        ],
    },
    "header": {
        "label":"Trusted Header Auth",
        "description":"Trust reverse-proxy headers (Authelia, Authentik forward-auth, nginx auth_request).",
        "fields":[
            {"key":"username_header","label":"Username Header","type":"text","required":True,"hint":"Remote-User"},
            {"key":"email_header","label":"Email Header","type":"text","required":False,"hint":"Remote-Email"},
            {"key":"groups_header","label":"Groups Header","type":"text","required":False,"hint":"Remote-Groups"},
            {"key":"trusted_ips","label":"Trusted Proxy CIDRs","type":"text","required":False},
            {"key":"default_role","label":"Default Role","type":"text","required":False},
            {"key":"role_map","label":"Group → Role Map (JSON)","type":"json","required":False},
            {"key":"allow_signup","label":"Auto-provision users","type":"boolean","required":False},
        ],
    },
}

@app.get("/api/auth-config")
async def get_auth_config(current_user=Depends(require_superadmin())):
    async with db_conn() as conn:
        row = await conn.fetchrow("SELECT value FROM app_config WHERE key='auth_config'")
    saved = {}
    if row:
        val = row["value"]
        saved = json.loads(val) if isinstance(val, str) else (val or {})
    return {
        "providers": AUTH_PROVIDER_DEFS,
        "active_provider": saved.get("active_provider", "local"),
        "provider_config": saved.get("provider_config", {}),
    }

@app.put("/api/auth-config")
async def update_auth_config(data: dict, current_user=Depends(require_superadmin())):
    async with db_conn() as conn:
        await conn.execute(
            "INSERT INTO app_config(key,value) VALUES('auth_config',$1::jsonb) ON CONFLICT(key) DO UPDATE SET value=$1::jsonb",
            json.dumps(data)
        )
    return {"ok": True, "note": "Restart backend to apply provider changes."}

# EXPORT (superadmin only)
# ─────────────────────────────────────────
@app.get("/api/export")
async def export_data(current_user=Depends(require_superadmin())):
    async with db_conn() as conn:
        areas = await conn.fetch(
            "SELECT id, name, description, metadata, created_at FROM areas ORDER BY name"
        )
        equipment = await conn.fetch(
            """SELECT e.id, e.common_name, e.make, e.model, e.serial_number,
                      e.build_date, e.status, e.attributes, e.created_at,
                      a.name as area_name
               FROM equipment e
               LEFT JOIN areas a ON a.id = e.area_id
               ORDER BY e.make, e.model"""
        )
        users_count = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE role='superadmin') as superadmins,
                      COUNT(*) FILTER (WHERE role='admin') as admins,
                      COUNT(*) FILTER (WHERE role='technician') as technicians,
                      COUNT(*) FILTER (WHERE role='viewer') as viewers
               FROM users WHERE is_active = true"""
        )
        ticket_stats = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status != 'closed') as open_tickets,
                      COUNT(*) as total_tickets
               FROM repair_tickets"""
        )

    return {
        "export_date": datetime.now(timezone.utc).isoformat(),
        "exported_by": current_user["username"],
        "summary": {
            "areas": len(areas),
            "equipment": len(equipment),
            "active_users": dict(users_count),
            "tickets": dict(ticket_stats),
        },
        "areas": [row_to_dict(r) for r in areas],
        "equipment": [row_to_dict(r) for r in equipment],
    }

# ─────────────────────────────────────────
# APP CONFIGURATION
# ─────────────────────────────────────────

# Theme is stored in a YAML file on the host bind mount.
# Dashboard and templates remain in the database.
APPDATA_DIR  = os.environ.get("APPDATA_DIR", "/appdata")
THEME_CONFIG = os.path.join(APPDATA_DIR, "config.yaml")

def read_theme_yaml() -> dict:
    """Read theme from YAML file. Returns empty dict if file missing or malformed."""
    try:
        with open(THEME_CONFIG, "r") as f:
            data = yaml.safe_load(f) or {}
            return data.get("theme", {})
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"Warning: could not read {THEME_CONFIG}: {e}")
        return {}

def write_theme_yaml(theme: dict, updated_by: str = "system"):
    """Write theme section to YAML file, preserving any other top-level keys."""
    os.makedirs(APPDATA_DIR, exist_ok=True)
    # Load existing file to preserve other keys
    existing = {}
    try:
        with open(THEME_CONFIG, "r") as f:
            existing = yaml.safe_load(f) or {}
    except FileNotFoundError:
        pass
    existing["theme"] = theme
    existing["_updated_by"] = updated_by
    existing["_updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(THEME_CONFIG, "w") as f:
        yaml.dump(existing, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

@app.get("/api/config")
async def get_all_config():
    """Public endpoint — returns all config keys merged into one object.
    Theme comes from YAML file; dashboard and templates come from DB."""
    async with db_conn() as conn:
        rows = await conn.fetch("SELECT key, value FROM app_config WHERE key != 'theme'")
    result = {}
    for row in rows:
        v = row["value"]
        result[row["key"]] = json.loads(v) if isinstance(v, str) else v
    # Theme from YAML overrides everything
    theme = read_theme_yaml()
    if theme:
        result["theme"] = theme
    return result

@app.get("/api/config/{key}")
async def get_config(key: str):
    if key == "theme":
        theme = read_theme_yaml()
        if not theme:
            raise HTTPException(404, "Theme config not found")
        return theme
    async with db_conn() as conn:
        row = await conn.fetchrow("SELECT value FROM app_config WHERE key=$1", key)
    if not row:
        raise HTTPException(404, f"Config key '{key}' not found")
    v = row["value"]
    return json.loads(v) if isinstance(v, str) else v

@app.put("/api/config/{key}")
async def set_config(key: str, request: Request, current_user=Depends(require_superadmin())):
    if key not in ("theme", "dashboard", "templates", "modules"):
        raise HTTPException(400, "Unknown config key")
    data = await request.json()
    if key == "theme":
        try:
            write_theme_yaml(data, updated_by=current_user["username"])
        except Exception as e:
            raise HTTPException(500, f"Failed to write theme config: {e}")
    else:
        async with db_conn() as conn:
            await conn.execute(
                """INSERT INTO app_config (key, value, updated_at, updated_by)
                   VALUES ($1, $2, NOW(), $3)
                   ON CONFLICT (key) DO UPDATE
                   SET value=$2, updated_at=NOW(), updated_by=$3""",
                key, json.dumps(data), current_user["username"]
            )
    return {"ok": True}


@app.post("/api/import/json/users")
async def import_json_users(request: Request, current_user=Depends(require_superadmin())):
    """Import users from JSON array. Skips existing usernames."""
    users_data = await request.json()
    if not isinstance(users_data, list):
        raise HTTPException(400, "Expected JSON array of user objects")
    created = skipped = 0
    errors = []
    async with db_conn() as conn:
        for u in users_data:
            try:
                username = (u.get("username") or "").strip()
                password = u.get("password") or "changeme123"
                role = u.get("role", "member")
                full_name = u.get("full_name") or None
                if not username:
                    errors.append("Skipped row with empty username")
                    skipped += 1
                    continue
                if role not in ("superadmin","admin","area_host","technician","authorizer","viewer","member"):
                    role = "member"
                hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                await conn.execute(
                    """INSERT INTO users (username, password_hash, role, full_name)
                       VALUES ($1, $2, $3, $4) ON CONFLICT (username) DO NOTHING""",
                    username, hashed, role, full_name
                )
                # Check if it was inserted
                row = await conn.fetchrow("SELECT id FROM users WHERE username=$1", username)
                if row:
                    created += 1
                else:
                    skipped += 1
            except Exception as e:
                errors.append(str(e))
    return {"created": created, "skipped": skipped, "errors": errors}

@app.get("/api/export/users-json")
async def export_users_json(current_user=Depends(require_superadmin())):
    async with db_conn() as conn:
        rows = await conn.fetch(
            "SELECT id, username, role, full_name, is_active, metadata, created_at FROM users ORDER BY username"
        )
    return rows_to_list(rows)

@app.get("/api/export/profile-json")
async def export_profile_json(current_user=Depends(require_superadmin())):
    """Export all user profile data (non-sensitive: no password hashes)."""
    async with db_conn() as conn:
        rows = await conn.fetch(
            "SELECT id, username, role, full_name, is_active, metadata, created_at FROM users ORDER BY username"
        )
    return [
        {
            "id": str(r["id"]),
            "username": r["username"],
            "role": r["role"],
            "full_name": r["full_name"] or "",
            "is_active": r["is_active"],
            "email":   (json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})).get("email", ""),
            "discord": (json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})).get("discord", ""),
            "notes":   (json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})).get("notes", ""),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]

@app.post("/api/import/users-json")
async def import_users_json(request: Request, current_user=Depends(require_superadmin())):
    data = await request.json()
    if not isinstance(data, list):
        raise HTTPException(400, "Expected a JSON array of users")
    created = skipped = 0
    errors = []
    async with db_conn() as conn:
        for i, u in enumerate(data):
            username = (u.get("username") or "").strip()
            role     = u.get("role", "member")
            if not username:
                errors.append(f"Item {i}: username required"); skipped += 1; continue
            if role not in ("superadmin","admin","area_host","technician","authorizer","viewer","member"):
                role = "member"
            # Use a placeholder hash if no password provided (user must reset)
            pw_hash = u.get("password_hash") or bcrypt.hashpw(b"changeme123", bcrypt.gensalt()).decode()
            meta = u.get("metadata") or {}
            try:
                await conn.execute(
                    """INSERT INTO users (username, password_hash, role, full_name, is_active, metadata)
                       VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (username) DO NOTHING""",
                    username, pw_hash, role,
                    u.get("full_name",""), u.get("is_active", True),
                    json.dumps(meta)
                )
                created += 1
            except Exception as e:
                errors.append(f"Item {i}: {e}"); skipped += 1
    return {"created": created, "skipped": skipped, "errors": errors}



# ─────────────────────────────────────────
# MAINTENANCE CALENDAR
# ─────────────────────────────────────────

class MaintenanceScheduleCreate(BaseModel):
    title: str
    description: Optional[str] = None
    equipment_id: Optional[str] = None
    group_id: Optional[str] = None
    recurrence_type: str = "days"
    recurrence_interval: int = 30
    assigned_to: Optional[str] = None
    priority: str = "normal"
    estimated_minutes: Optional[int] = None
    checklist: list = []
    notify_roles: list = []

class MaintenanceScheduleUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    equipment_id: Optional[str] = None
    group_id: Optional[str] = None
    recurrence_type: Optional[str] = None
    recurrence_interval: Optional[int] = None
    assigned_to: Optional[str] = None
    priority: Optional[str] = None
    estimated_minutes: Optional[int] = None
    checklist: Optional[list] = None
    notify_roles: Optional[list] = None
    is_active: Optional[bool] = None

class MaintenanceEventUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    assigned_to: Optional[str] = None
    checklist_state: Optional[list] = None


def _next_due_date(from_date, rec_type: str, rec_interval: int):
    """Calculate the next due date from a given date based on recurrence settings."""
    from dateutil.relativedelta import relativedelta
    base = from_date if isinstance(from_date, datetime) else datetime.fromisoformat(str(from_date).replace("Z","+00:00"))
    if rec_type == "days":
        return base + timedelta(days=rec_interval)
    elif rec_type == "weeks":
        return base + timedelta(weeks=rec_interval)
    elif rec_type == "months":
        return base + relativedelta(months=rec_interval)
    elif rec_type == "years":
        return base + relativedelta(years=rec_interval)
    return base + timedelta(days=rec_interval)


async def _create_next_event(conn, schedule, from_date=None):
    """Create the next pending maintenance event for a schedule.
    Only creates if no pending/in_progress event already exists."""
    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM maintenance_events WHERE schedule_id=$1 AND status IN ('pending','in_progress')",
        schedule["id"]
    )
    if existing > 0:
        return None  # Already has an active event

    if from_date:
        due = _next_due_date(from_date, schedule["recurrence_type"], schedule["recurrence_interval"])
    else:
        due = datetime.now(timezone.utc) + timedelta(days=1)  # First event: tomorrow

    # For group schedules, create events for each equipment in the group
    equip_id = schedule.get("equipment_id")
    if not equip_id and schedule.get("group_id"):
        equip_id = None  # group-level event

    row = await conn.fetchrow(
        """INSERT INTO maintenance_events (schedule_id, equipment_id, due_date, assigned_to, checklist_state)
           VALUES ($1, $2, $3, $4, $5) RETURNING *""",
        schedule["id"], equip_id, due,
        schedule.get("assigned_to"), json.dumps(schedule.get("checklist") or [])
    )
    return row


# ── Maintenance Schedules CRUD ────────────────────────────────────

@app.get("/api/maintenance/schedules")
async def list_maintenance_schedules(
    equipment_id: Optional[str] = None,
    group_id: Optional[str] = None,
    current_user=Depends(check_perm("maintenance.view"))
):
    conditions = ["1=1"]
    params = []
    i = 1
    if equipment_id:
        conditions.append(f"ms.equipment_id = ${i}"); params.append(equipment_id); i += 1
    if group_id:
        conditions.append(f"ms.group_id = ${i}"); params.append(group_id); i += 1
    where = " AND ".join(conditions)
    async with db_conn() as conn:
        rows = await conn.fetch(f"""
            SELECT ms.*, e.common_name as equipment_name, e.make as equipment_make, e.model as equipment_model,
                   eg.name as group_name, u.full_name as assigned_name, u.username as assigned_username,
                   c.full_name as creator_name
            FROM maintenance_schedules ms
            LEFT JOIN equipment e ON e.id = ms.equipment_id
            LEFT JOIN equipment_groups eg ON eg.id = ms.group_id
            LEFT JOIN users u ON u.id = ms.assigned_to
            LEFT JOIN users c ON c.id = ms.created_by
            WHERE {where}
            ORDER BY ms.created_at DESC
        """, *params)
    return rows_to_list(rows)


@app.post("/api/maintenance/schedules", status_code=201)
async def create_maintenance_schedule(data: MaintenanceScheduleCreate, current_user=Depends(check_perm("maintenance.create"))):
    if not data.equipment_id and not data.group_id:
        raise HTTPException(400, "Either equipment_id or group_id is required")
    if data.recurrence_type not in ("days", "weeks", "months", "years"):
        raise HTTPException(400, "Invalid recurrence_type")
    if data.recurrence_interval < 1:
        raise HTTPException(400, "recurrence_interval must be >= 1")

    async with db_conn() as conn:
        row = await conn.fetchrow(
            """INSERT INTO maintenance_schedules
               (title, description, equipment_id, group_id, recurrence_type, recurrence_interval,
                assigned_to, created_by, priority, estimated_minutes, checklist, notify_roles)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               RETURNING *""",
            data.title, data.description,
            data.equipment_id or None, data.group_id or None,
            data.recurrence_type, data.recurrence_interval,
            data.assigned_to or None, current_user["id"],
            data.priority, data.estimated_minutes,
            json.dumps(data.checklist), data.notify_roles or []
        )
        schedule = dict(row)
        # Create the first pending event
        evt = await _create_next_event(conn, schedule)

    result = row_to_dict(row)
    await fire_notification("maintenance.created", {
        "schedule_id": str(result.get("id")), "title": data.title,
        "recurrence": f"Every {data.recurrence_interval} {data.recurrence_type}",
        "by": current_user.get("username"),
    })
    return result


@app.patch("/api/maintenance/schedules/{schedule_id}")
async def update_maintenance_schedule(schedule_id: str, data: MaintenanceScheduleUpdate, current_user=Depends(check_perm("maintenance.edit"))):
    async with db_conn() as conn:
        existing = await conn.fetchrow("SELECT * FROM maintenance_schedules WHERE id=$1", schedule_id)
        if not existing:
            raise HTTPException(404, "Schedule not found")
        updates = {}
        for field in ("title","description","equipment_id","group_id","recurrence_type",
                       "recurrence_interval","assigned_to","priority","estimated_minutes","is_active"):
            val = getattr(data, field, None)
            if val is not None:
                updates[field] = val
        if data.checklist is not None: updates["checklist"] = json.dumps(data.checklist)
        if data.notify_roles is not None: updates["notify_roles"] = data.notify_roles
        if not updates:
            raise HTTPException(400, "No fields to update")
        set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
        row = await conn.fetchrow(
            f"UPDATE maintenance_schedules SET {set_clauses} WHERE id=$1 RETURNING *",
            schedule_id, *updates.values()
        )
    return row_to_dict(row)


@app.delete("/api/maintenance/schedules/{schedule_id}", status_code=204)
async def delete_maintenance_schedule(schedule_id: str, current_user=Depends(check_perm("maintenance.manage"))):
    async with db_conn() as conn:
        result = await conn.execute("DELETE FROM maintenance_schedules WHERE id=$1", schedule_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Schedule not found")


# ── Maintenance Events CRUD ───────────────────────────────────────

@app.get("/api/maintenance/events")
async def list_maintenance_events(
    schedule_id: Optional[str] = None,
    equipment_id: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    include_active: Optional[bool] = False,
    current_user=Depends(check_perm("maintenance.view"))
):
    conditions = ["1=1"]
    params = []
    i = 1
    if schedule_id:
        conditions.append(f"me.schedule_id = ${i}"); params.append(schedule_id); i += 1
    if equipment_id:
        conditions.append(f"(me.equipment_id = ${i} OR ms.equipment_id = ${i})"); params.append(equipment_id); i += 1
    if status:
        conditions.append(f"me.status = ${i}"); params.append(status); i += 1
    # Date filtering: if include_active, also include any pending/in_progress/overdue regardless of date
    if from_date or to_date:
        date_parts = []
        if from_date and to_date:
            date_parts.append(f"(me.due_date >= ${i}::timestamptz AND me.due_date <= ${i+1}::timestamptz)")
            params.extend([from_date, to_date]); i += 2
        elif from_date:
            date_parts.append(f"me.due_date >= ${i}::timestamptz"); params.append(from_date); i += 1
        elif to_date:
            date_parts.append(f"me.due_date <= ${i}::timestamptz"); params.append(to_date); i += 1
        if include_active:
            date_parts.append("me.status IN ('pending','in_progress','overdue')")
        conditions.append(f"({' OR '.join(date_parts)})")
    where = " AND ".join(conditions)
    async with db_conn() as conn:
        rows = await conn.fetch(f"""
            SELECT me.*, ms.title, ms.description as schedule_description, ms.priority,
                   ms.estimated_minutes, ms.recurrence_type, ms.recurrence_interval,
                   ms.group_id, ms.equipment_id as schedule_equipment_id,
                   e.common_name as equipment_name, e.make as equipment_make, e.model as equipment_model,
                   eg.name as group_name,
                   u.full_name as assigned_name, u.username as assigned_username,
                   cb.full_name as completed_by_name,
                   rt.ticket_number as ticket_number
            FROM maintenance_events me
            JOIN maintenance_schedules ms ON ms.id = me.schedule_id
            LEFT JOIN equipment e ON e.id = COALESCE(me.equipment_id, ms.equipment_id)
            LEFT JOIN equipment_groups eg ON eg.id = ms.group_id
            LEFT JOIN users u ON u.id = me.assigned_to
            LEFT JOIN users cb ON cb.id = me.completed_by
            LEFT JOIN repair_tickets rt ON rt.id = me.ticket_id
            WHERE {where}
            ORDER BY me.due_date ASC
        """, *params)
    return rows_to_list(rows)


@app.patch("/api/maintenance/events/{event_id}")
async def update_maintenance_event(event_id: str, data: MaintenanceEventUpdate, current_user=Depends(check_perm("maintenance.complete"))):
    async with db_conn() as conn:
        existing = await conn.fetchrow(
            """SELECT me.*, ms.recurrence_type, ms.recurrence_interval, ms.is_active as schedule_active,
                      ms.id as sid, ms.title as sched_title, ms.description as sched_desc, ms.priority as sched_priority,
                      ms.assigned_to as sched_assigned, ms.equipment_id as sched_equip_id
               FROM maintenance_events me JOIN maintenance_schedules ms ON ms.id = me.schedule_id WHERE me.id=$1""",
            event_id
        )
        if not existing:
            raise HTTPException(404, "Event not found")

        updates = {}
        if data.status is not None: updates["status"] = data.status
        if data.notes is not None: updates["notes"] = data.notes
        if data.assigned_to is not None: updates["assigned_to"] = data.assigned_to
        if data.checklist_state is not None: updates["checklist_state"] = json.dumps(data.checklist_state)

        # ── On START (in_progress): auto-create a linked maintenance ticket ──
        if data.status == "in_progress" and not existing.get("ticket_id"):
            equip_id = existing.get("equipment_id") or existing.get("sched_equip_id")
            if equip_id:
                ticket_number = await conn.fetchval("SELECT next_ticket_number()")
                ticket_row = await conn.fetchrow(
                    """INSERT INTO repair_tickets
                       (equipment_id, ticket_number, opened_by, assigned_to, title, description,
                        priority, status, category, metadata)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, 'in_progress', 'maintenance', $8)
                       RETURNING id""",
                    equip_id, ticket_number, current_user["id"],
                    existing.get("assigned_to") or existing.get("sched_assigned"),
                    f"[Maintenance] {existing.get('sched_title', 'Scheduled Maintenance')}",
                    existing.get("sched_desc") or f"Auto-created from maintenance schedule. Event ID: {event_id}",
                    existing.get("sched_priority", "normal"),
                    json.dumps({"maintenance_event_id": str(event_id), "auto_created": True})
                )
                if ticket_row:
                    updates["ticket_id"] = str(ticket_row["id"])
                    # Set equipment to under_repair
                    await conn.execute(
                        "UPDATE equipment SET status='under_repair' WHERE id=$1 AND status='active'",
                        equip_id
                    )

        # Handle completion — mark completed_by and completed_at
        if data.status in ("completed", "skipped"):
            updates["completed_by"] = current_user["id"]
            updates["completed_at"] = datetime.now(timezone.utc).isoformat()

        if not updates:
            raise HTTPException(400, "No fields to update")

        set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
        row = await conn.fetchrow(
            f"UPDATE maintenance_events SET {set_clauses} WHERE id=$1 RETURNING *",
            event_id, *updates.values()
        )

        # ── On COMPLETION: auto-close the linked ticket ──
        if data.status == "completed":
            ticket_id = row.get("ticket_id") or existing.get("ticket_id")
            if ticket_id:
                await conn.execute(
                    "UPDATE repair_tickets SET status='closed', closed_at=NOW() WHERE id=$1 AND status != 'closed'",
                    ticket_id
                )
                equip_id = existing.get("equipment_id") or existing.get("sched_equip_id")
                if equip_id:
                    open_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM repair_tickets WHERE equipment_id=$1 AND status != 'closed'",
                        equip_id
                    )
                    if open_count == 0:
                        await conn.execute(
                            "UPDATE equipment SET status='active' WHERE id=$1 AND status='under_repair'",
                            equip_id
                        )

        # On completion/skip: create the NEXT event (only if schedule is still active)
        if data.status in ("completed", "skipped") and existing["schedule_active"]:
            schedule = await conn.fetchrow("SELECT * FROM maintenance_schedules WHERE id=$1", existing["sid"])
            if schedule:
                await _create_next_event(conn, dict(schedule), from_date=datetime.now(timezone.utc))

    result = row_to_dict(row)

    if data.status == "completed":
        await fire_notification("maintenance.completed", {
            "event_id": event_id, "title": existing.get("sched_title", ""),
            "by": current_user.get("username"),
        })
    elif data.status == "in_progress":
        await fire_notification("maintenance.due", {
            "event_id": event_id, "title": existing.get("sched_title", ""),
            "by": current_user.get("username"),
            "ticket": updates.get("ticket_id", ""),
        })
    return result


@app.get("/api/maintenance/summary")
async def maintenance_summary(current_user=Depends(check_perm("maintenance.view"))):
    """Summary stats for the maintenance dashboard."""
    async with db_conn() as conn:
        now = datetime.now(timezone.utc)
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') as pending,
                COUNT(*) FILTER (WHERE status = 'in_progress') as in_progress,
                COUNT(*) FILTER (WHERE status = 'overdue') as overdue,
                COUNT(*) FILTER (WHERE status = 'completed' AND completed_at > $1) as completed_this_month,
                COUNT(*) FILTER (WHERE status = 'pending' AND due_date < $2) as past_due
            FROM maintenance_events
        """, now.replace(day=1, hour=0, minute=0, second=0), now)
        upcoming = await conn.fetch("""
            SELECT me.id, me.due_date, me.status, ms.title, ms.priority,
                   e.common_name as equipment_name, eg.name as group_name
            FROM maintenance_events me
            JOIN maintenance_schedules ms ON ms.id = me.schedule_id
            LEFT JOIN equipment e ON e.id = COALESCE(me.equipment_id, ms.equipment_id)
            LEFT JOIN equipment_groups eg ON eg.id = ms.group_id
            WHERE me.status IN ('pending','in_progress','overdue')
            ORDER BY me.due_date ASC LIMIT 20
        """)
        # Auto-mark overdue events
        await conn.execute(
            "UPDATE maintenance_events SET status='overdue' WHERE status='pending' AND due_date < $1",
            now
        )
    return {
        "stats": dict(stats) if stats else {},
        "upcoming": rows_to_list(upcoming),
    }



# ─────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────
@app.get("/api/audit-log")
async def get_audit_log(
    table_name: Optional[str] = None,
    record_id: Optional[str] = None,
    user_id: Optional[str] = None,
    operation: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    current_user=Depends(require_superadmin())
):
    """Query the audit log. Superadmin only."""
    conditions = ["1=1"]
    params = []
    i = 1
    if table_name:
        conditions.append(f"table_name = ${i}"); params.append(table_name); i += 1
    if record_id:
        conditions.append(f"record_id = ${i}"); params.append(record_id); i += 1
    if user_id:
        conditions.append(f"user_id = ${i}"); params.append(user_id); i += 1
    if operation:
        conditions.append(f"operation = ${i}"); params.append(operation); i += 1
    where = " AND ".join(conditions)
    async with db_conn() as conn:
        rows = await conn.fetch(
            f"""SELECT id, table_name, record_id, operation, user_id, user_role,
                       old_data, new_data, changed_fields, created_at
                FROM audit_log WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${i} OFFSET ${i+1}""",
            *params, min(limit, 500), offset
        )
        total = await conn.fetchval(f"SELECT COUNT(*) FROM audit_log WHERE {where}", *params)
    return {"total": total, "offset": offset, "limit": limit, "entries": rows_to_list(rows)}


# ─────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────
@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
