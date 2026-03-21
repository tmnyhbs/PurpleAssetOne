"""
PurpleAssetOne API Client
Handles authentication, token refresh, and all API calls for the Discord bot.
"""
import httpx
import time
import logging

log = logging.getLogger("pa1_api")


class PA1Client:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token: str | None = None
        self.token_expiry: float = 0
        self.user: dict = {}
        self._client = httpx.AsyncClient(timeout=15)

    async def close(self):
        await self._client.aclose()

    async def _ensure_token(self):
        """Authenticate or re-authenticate if token is expired/missing."""
        if self.token and time.time() < self.token_expiry - 60:
            return
        log.info(f"Authenticating as {self.username}...")
        resp = await self._client.post(
            f"{self.base_url}/api/auth/token",
            data={"username": self.username, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.user = data.get("user", {})
        # JWT expires in 12 hours; refresh at 11 hours
        self.token_expiry = time.time() + 11 * 3600
        log.info(f"Authenticated as {self.user.get('username', '?')} (role: {self.user.get('role', '?')})")

    async def _headers(self) -> dict:
        await self._ensure_token()
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def get(self, path: str, params: dict | None = None) -> dict | list:
        headers = await self._headers()
        resp = await self._client.get(f"{self.base_url}{path}", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, json_data: dict | None = None) -> dict:
        headers = await self._headers()
        resp = await self._client.post(f"{self.base_url}{path}", headers=headers, json=json_data)
        resp.raise_for_status()
        return resp.json()

    async def patch(self, path: str, json_data: dict | None = None) -> dict:
        headers = await self._headers()
        resp = await self._client.patch(f"{self.base_url}{path}", headers=headers, json=json_data)
        resp.raise_for_status()
        return resp.json()

    # ── Convenience methods ──────────────────────────────────────

    async def search_equipment(self, query: str = "", limit: int = 25) -> list:
        """Search equipment by name/make/model."""
        params = {}
        if query:
            params["search"] = query
        items = await self.get("/api/equipment", params=params)
        return items[:limit] if isinstance(items, list) else []

    async def get_equipment(self, equipment_id: str) -> dict:
        return await self.get(f"/api/equipment/{equipment_id}")

    async def create_ticket(self, equipment_id: str, title: str,
                            description: str = "", priority: str = "normal",
                            metadata: dict | None = None) -> dict:
        payload = {
            "equipment_id": equipment_id,
            "title": title,
            "description": description,
            "priority": priority,
            "metadata": metadata or {},
        }
        return await self.post("/api/tickets", payload)

    async def add_worklog(self, ticket_id: str, action: str,
                          notes: str = "") -> dict:
        payload = {
            "action": action,
            "notes": notes,
            "parts_used": [],
            "attachments": [],
        }
        return await self.post(f"/api/tickets/{ticket_id}/worklog", payload)

    async def get_ticket(self, ticket_id: str) -> dict:
        return await self.get(f"/api/tickets/{ticket_id}")

    async def list_tickets(self, status: str | None = None,
                           equipment_id: str | None = None,
                           limit: int = 10) -> list:
        params = {}
        if status:
            params["status"] = status
        if equipment_id:
            params["equipment_id"] = equipment_id
        items = await self.get("/api/tickets", params=params)
        return items[:limit] if isinstance(items, list) else []
