"""
PurpleAssetOne Discord Bot
Creates repair tickets, syncs thread comments as work log entries.
All slash command names and behavior are configurable via config.yaml.
Communicates with the PA1 API — no direct database access.
"""
import os
import json
import logging
from pathlib import Path
from copy import deepcopy

import yaml
import discord
from discord import app_commands
from discord.ext import tasks

from pa1_api import PA1Client

# ─────────────────────────────────────────
# ENV CONFIG
# ─────────────────────────────────────────
DISCORD_TOKEN     = os.getenv("DISCORD_BOT_TOKEN", "")
PA1_API_URL       = os.getenv("PA1_API_URL", "http://backend:8000")
PA1_BOT_USER      = os.getenv("PA1_BOT_USER", "")
PA1_BOT_PASSWORD  = os.getenv("PA1_BOT_PASSWORD", "")
DATA_DIR          = os.getenv("BOT_DATA_DIR", "/data")
GUILD_IDS_STR     = os.getenv("DISCORD_GUILD_IDS", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("pa1bot")

# ─────────────────────────────────────────
# YAML CONFIG LOADING
# ─────────────────────────────────────────
DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"
USER_CONFIG_PATH    = Path(DATA_DIR) / "config.yaml"


def load_config() -> dict:
    """Load config: start with bundled defaults, overlay user overrides."""
    cfg = {}
    # Load bundled defaults
    if DEFAULT_CONFIG_PATH.exists():
        try:
            cfg = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
        except Exception as e:
            log.warning(f"Failed to load default config: {e}")

    # Overlay user config from data volume
    if USER_CONFIG_PATH.exists():
        try:
            user_cfg = yaml.safe_load(USER_CONFIG_PATH.read_text()) or {}
            cfg = _deep_merge(cfg, user_cfg)
            log.info(f"User config loaded from {USER_CONFIG_PATH}")
        except Exception as e:
            log.warning(f"Failed to load user config: {e}")

    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = deepcopy(val)
    return result


# ── Config accessors ──────────────────────────────────────────────
CFG: dict = {}


def cmd_cfg(key: str) -> dict:
    """Get command config by internal key (e.g. 'create_ticket')."""
    return CFG.get("commands", {}).get(key, {})


def cmd_name(key: str, fallback: str = "") -> str:
    return cmd_cfg(key).get("name", fallback)


def hex_color(hex_str: str) -> int:
    """Convert a hex color string (with or without 0x/#) to int."""
    return int(str(hex_str).lstrip("#").lstrip("0x"), 16)


def priority_color(priority: str) -> int:
    colors = CFG.get("embeds", {}).get("priority_colors", {})
    return hex_color(colors.get(priority, "6C757D"))


def status_color(status: str) -> int:
    colors = CFG.get("embeds", {}).get("status_colors", {})
    return hex_color(colors.get(status, "6C757D"))


def priority_emoji(priority: str) -> str:
    emojis = CFG.get("embeds", {}).get("priority_emoji", {})
    return emojis.get(priority, "")


def equip_status_emoji(status: str) -> str:
    emojis = CFG.get("embeds", {}).get("equipment_status_emoji", {})
    return emojis.get(status, "⚪")


def footer_text() -> str:
    return CFG.get("embeds", {}).get("footer_text", "PurpleAssetOne")


# ─────────────────────────────────────────
# THREAD → TICKET MAPPING (persisted)
# ─────────────────────────────────────────
THREAD_MAP_FILE = Path(DATA_DIR) / "thread_map.json"

def load_thread_map() -> dict:
    if THREAD_MAP_FILE.exists():
        try: return json.loads(THREAD_MAP_FILE.read_text())
        except Exception: pass
    return {}

def save_thread_map(data: dict):
    THREAD_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    THREAD_MAP_FILE.write_text(json.dumps(data, indent=2))

thread_map: dict = {}


# ─────────────────────────────────────────
# BOT CLASS
# ─────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True


class PA1Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.api = PA1Client(PA1_API_URL, PA1_BOT_USER, PA1_BOT_PASSWORD)
        self._equipment_cache: list = []

    async def setup_hook(self):
        # Register all commands dynamically from config
        _register_commands(self)

        guild_ids = [int(g.strip()) for g in GUILD_IDS_STR.split(",") if g.strip()]
        for gid in guild_ids:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Commands synced to guild {gid}")
        if not guild_ids:
            await self.tree.sync()
            log.info("Commands synced globally (may take up to 1 hour)")

    async def on_ready(self):
        global thread_map
        thread_map = load_thread_map()
        log.info(f"Logged in as {self.user} | Tracking {len(thread_map)} threads")
        if not self.refresh_equipment_cache.is_running():
            self.refresh_equipment_cache.start()

    async def close(self):
        await self.api.close()
        await super().close()

    @tasks.loop(minutes=10)
    async def refresh_equipment_cache(self):
        try:
            cache_cfg = CFG.get("equipment_cache", {})
            max_items = cache_cfg.get("max_items", 200)
            self._equipment_cache = await self.api.search_equipment("", limit=max_items)
            log.info(f"Equipment cache refreshed: {len(self._equipment_cache)} items")
        except Exception as e:
            log.warning(f"Equipment cache refresh failed: {e}")

    def get_equipment_choices(self, query: str) -> list[app_commands.Choice]:
        query_lower = query.lower()
        matches = []
        for eq in self._equipment_cache:
            name = eq.get("common_name") or f"{eq.get('make','')} {eq.get('model','')}"
            search_str = f"{name} {eq.get('make','')} {eq.get('model','')} {eq.get('serial_number','')}".lower()
            if not query_lower or query_lower in search_str:
                label = f"{name} ({eq.get('serial_number', '?')})"
                matches.append(app_commands.Choice(name=label[:100], value=eq["id"]))
            if len(matches) >= 25:
                break
        return matches


bot = PA1Bot()


# ─────────────────────────────────────────
# TICKET CREATION MODAL
# ─────────────────────────────────────────
class TicketModal(discord.ui.Modal):
    def __init__(self, equipment_id: str, equipment_name: str):
        modal_cfg = CFG.get("modal", {})
        super().__init__(title=modal_cfg.get("title", "Create Repair Ticket"))
        self.equipment_id = equipment_id
        self.equipment_name = equipment_name

        self.ticket_title = discord.ui.TextInput(
            label=modal_cfg.get("field_title_label", "Title"),
            placeholder=modal_cfg.get("field_title_placeholder", "Brief description of the issue"),
            max_length=200, required=True,
        )
        self.description = discord.ui.TextInput(
            label=modal_cfg.get("field_description_label", "Description"),
            style=discord.TextStyle.paragraph,
            placeholder=modal_cfg.get("field_description_placeholder", "Detailed description (optional)"),
            max_length=2000, required=False,
        )
        self.priority = discord.ui.TextInput(
            label=modal_cfg.get("field_priority_label", "Priority (low / normal / high / critical)"),
            placeholder=modal_cfg.get("field_priority_default", "normal"),
            default=modal_cfg.get("field_priority_default", "normal"),
            max_length=10, required=False,
        )
        self.add_item(self.ticket_title)
        self.add_item(self.description)
        self.add_item(self.priority)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        prio = (self.priority.value or "normal").strip().lower()
        if prio not in ("low", "normal", "high", "critical"):
            prio = "normal"

        try:
            ticket = await bot.api.create_ticket(
                equipment_id=self.equipment_id,
                title=self.ticket_title.value.strip(),
                description=self.description.value.strip() or "",
                priority=prio,
                metadata={"discord_channel_id": str(interaction.channel_id), "created_via": "discord"},
            )

            embed = _ticket_embed(ticket, self.equipment_name, "New Repair Ticket Created")
            msg = await interaction.followup.send(embed=embed)

            # Auto-thread
            thread_cfg = CFG.get("auto_thread", {})
            if thread_cfg.get("enabled", True):
                archive = thread_cfg.get("archive_duration", 4320)
                thread = await msg.create_thread(
                    name=f"{ticket.get('ticket_number', 'Ticket')} — {self.ticket_title.value[:80]}",
                    auto_archive_duration=archive,
                )
                thread_map[str(thread.id)] = {
                    "ticket_id": ticket["id"],
                    "ticket_number": ticket.get("ticket_number", ""),
                }
                save_thread_map(thread_map)

                sync_cfg = CFG.get("thread_sync", {})
                welcome = sync_cfg.get("welcome_message",
                    "🎫 **Ticket {ticket_number}** is now linked to this thread.\n"
                    "Messages posted here will be automatically added as work log entries.\n"
                    "Use `/{addnote_cmd}` to add structured notes from any channel."
                )
                welcome = welcome.replace("{ticket_number}", ticket.get("ticket_number", "?"))
                welcome = welcome.replace("{addnote_cmd}", cmd_name("add_note", "addnote"))
                await thread.send(welcome)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to create ticket: {e}", ephemeral=True)


# ─────────────────────────────────────────
# COMMAND HANDLERS (config-independent logic)
# ─────────────────────────────────────────

async def _handle_create_ticket(interaction: discord.Interaction, equipment: str):
    eq_name = equipment
    for eq in bot._equipment_cache:
        if eq["id"] == equipment:
            eq_name = eq.get("common_name") or f"{eq.get('make','')} {eq.get('model','')}"
            break
    modal = TicketModal(equipment_id=equipment, equipment_name=eq_name)
    await interaction.response.send_modal(modal)


async def _handle_add_note(interaction: discord.Interaction, ticket_number: str, note: str):
    await interaction.response.defer(ephemeral=True)
    try:
        tickets = await bot.api.list_tickets(limit=500)
        ticket = next((t for t in tickets if t.get("ticket_number", "").upper() == ticket_number.upper()), None)
        if not ticket:
            await interaction.followup.send(f"❌ Ticket `{ticket_number}` not found.", ephemeral=True)
            return
        await bot.api.add_worklog(ticket["id"], action=f"Discord note from @{interaction.user.display_name}", notes=note)
        await interaction.followup.send(f"✅ Note added to **{ticket_number}**:\n> {note[:200]}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)


async def _handle_list_tickets(interaction: discord.Interaction, status: str = ""):
    default_status = cmd_cfg("list_tickets").get("default_status", "open")
    status = status or default_status
    await interaction.response.defer()
    try:
        tickets = await bot.api.list_tickets(status=status, limit=15)
        if not tickets:
            await interaction.followup.send(f"No tickets found with status **{status}**.")
            return
        embed = discord.Embed(title=f"📋 Repair Tickets — {status.replace('_', ' ').title()}", color=status_color(status))
        for t in tickets[:15]:
            pe = priority_emoji(t.get("priority", "normal"))
            equip = t.get("equipment_name", "Unknown")
            embed.add_field(
                name=f"{pe} {t.get('ticket_number', '?')} — {t.get('title', '?')[:60]}",
                value=f"`{equip}` · {t.get('status', '?').replace('_', ' ')} · {t.get('assigned_to_name') or 'Unassigned'}",
                inline=False,
            )
        embed.set_footer(text=f"Showing {len(tickets)} ticket{'s' if len(tickets) != 1 else ''}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: {e}")


async def _handle_search_equipment(interaction: discord.Interaction, query: str = ""):
    await interaction.response.defer()
    try:
        items = await bot.api.search_equipment(query, limit=15)
        if not items:
            await interaction.followup.send(f"No equipment found for **{query}**." if query else "No equipment found.")
            return
        embed = discord.Embed(title=f"🔧 Equipment{f' — \"{query}\"' if query else ''}", color=0x5865F2)
        for eq in items[:15]:
            name = eq.get("common_name") or f"{eq.get('make','')} {eq.get('model','')}"
            se = equip_status_emoji(eq.get("status", ""))
            embed.add_field(
                name=f"{se} {name}",
                value=f"`{eq.get('serial_number', '?')}` · {eq.get('area_name', 'No area')} · {eq.get('status', '?').replace('_', ' ')}",
                inline=False,
            )
        embed.set_footer(text=f"Showing {len(items)} result{'s' if len(items) != 1 else ''}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: {e}")


async def _handle_ticket_info(interaction: discord.Interaction, ticket_number: str):
    await interaction.response.defer()
    try:
        tickets = await bot.api.list_tickets(limit=500)
        ticket = next((t for t in tickets if t.get("ticket_number", "").upper() == ticket_number.upper()), None)
        if not ticket:
            await interaction.followup.send(f"❌ Ticket `{ticket_number}` not found.")
            return
        equip_name = ticket.get("equipment_name", "Unknown")
        embed = _ticket_embed(ticket, equip_name, f"Ticket {ticket.get('ticket_number', '?')}")
        work_log = ticket.get("work_log") or []
        if work_log:
            recent = work_log[-5:]
            log_text = "\n".join(
                f"• **{e.get('user_name', '?')}** — {e.get('action', '?')}"
                + (f"\n  _{e.get('notes', '')}_" if e.get("notes") else "")
                for e in recent
            )
            embed.add_field(name=f"📝 Work Log (last {len(recent)})", value=log_text[:1024], inline=False)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: {e}")


# ─────────────────────────────────────────
# DYNAMIC COMMAND REGISTRATION
# ─────────────────────────────────────────

def _register_commands(client: PA1Bot):
    """Build and register all slash commands from config at startup."""
    tree = client.tree

    # ── /repair-ticket (create) ───────────────────────────────────
    cc = cmd_cfg("create_ticket")
    described_create = app_commands.describe(
        equipment=cc.get("equipment_param", "Search and select the equipment"),
    )(_handle_create_ticket)
    create_cmd = app_commands.Command(
        name=cc.get("name", "repair-ticket"),
        description=cc.get("description", "Create a new repair ticket"),
        callback=described_create,
    )
    @create_cmd.autocomplete("equipment")
    async def _equip_ac(interaction: discord.Interaction, current: str):
        return client.get_equipment_choices(current)
    tree.add_command(create_cmd)

    # ── /addnote ──────────────────────────────────────────────────
    nc = cmd_cfg("add_note")
    described_note = app_commands.describe(
        ticket_number=nc.get("ticket_param", "Ticket number (e.g. TKT-001000)"),
        note=nc.get("note_param", "The note to add to the work log"),
    )(_handle_add_note)
    tree.add_command(app_commands.Command(
        name=nc.get("name", "addnote"),
        description=nc.get("description", "Add a work log note to a ticket"),
        callback=described_note,
    ))

    # ── /tickets ──────────────────────────────────────────────────
    lc = cmd_cfg("list_tickets")
    described_list = app_commands.describe(
        status=lc.get("status_param", "Filter by status"),
    )(_handle_list_tickets)
    tree.add_command(app_commands.Command(
        name=lc.get("name", "tickets"),
        description=lc.get("description", "List recent open repair tickets"),
        callback=described_list,
    ))

    # ── /equipment ────────────────────────────────────────────────
    ec = cmd_cfg("search_equipment")
    described_equip = app_commands.describe(
        query=ec.get("query_param", "Search by name, make, model, or serial number"),
    )(_handle_search_equipment)
    tree.add_command(app_commands.Command(
        name=ec.get("name", "equipment"),
        description=ec.get("description", "Search equipment"),
        callback=described_equip,
    ))

    # ── /ticketinfo ───────────────────────────────────────────────
    ic = cmd_cfg("ticket_info")
    described_info = app_commands.describe(
        ticket_number=ic.get("ticket_param", "Ticket number (e.g. TKT-001000)"),
    )(_handle_ticket_info)
    tree.add_command(app_commands.Command(
        name=ic.get("name", "ticketinfo"),
        description=ic.get("description", "View details for a specific ticket"),
        callback=described_info,
    ))

    names = [
        cc.get("name", "repair-ticket"), nc.get("name", "addnote"),
        lc.get("name", "tickets"), ec.get("name", "equipment"),
        ic.get("name", "ticketinfo"),
    ]
    log.info(f"Registered commands: /{', /'.join(names)}")


# ─────────────────────────────────────────
# THREAD MESSAGE SYNC
# ─────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    sync_cfg = CFG.get("thread_sync", {})
    if not sync_cfg.get("enabled", True):
        return

    thread_id = str(message.channel.id)
    mapping = thread_map.get(thread_id)
    if not mapping:
        return

    ticket_id = mapping["ticket_id"]
    ticket_number = mapping.get("ticket_number", "?")
    content = message.content.strip()

    min_len = sync_cfg.get("min_message_length", 2)
    if len(content) < min_len or content.startswith("/"):
        return

    try:
        await bot.api.add_worklog(
            ticket_id,
            action=f"Discord message from @{message.author.display_name}",
            notes=content[:2000],
        )
        confirm = sync_cfg.get("confirm_reaction", "📝")
        if confirm:
            await message.add_reaction(confirm)
    except Exception as e:
        log.warning(f"Failed to sync thread message to {ticket_number}: {e}")
        error_react = sync_cfg.get("error_reaction", "❌")
        if error_react:
            try:
                await message.add_reaction(error_react)
            except Exception:
                pass


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def _ticket_embed(ticket: dict, equipment_name: str, title: str) -> discord.Embed:
    prio = ticket.get("priority", "normal")
    status = ticket.get("status", "open")
    embed = discord.Embed(title=title, color=priority_color(prio))
    embed.add_field(name="Ticket #", value=ticket.get("ticket_number", "?"), inline=True)
    embed.add_field(name="Equipment", value=equipment_name, inline=True)
    embed.add_field(name="Priority", value=f"{priority_emoji(prio)} {prio.title()}", inline=True)
    embed.add_field(name="Status", value=status.replace("_", " ").title(), inline=True)
    if ticket.get("assigned_to_name"):
        embed.add_field(name="Assigned To", value=ticket["assigned_to_name"], inline=True)
    if ticket.get("area_name"):
        embed.add_field(name="Area", value=ticket["area_name"], inline=True)
    if ticket.get("description"):
        embed.add_field(name="Description", value=ticket["description"][:1024], inline=False)
    if ticket.get("category") == "maintenance":
        embed.add_field(name="Category", value="🛠️ Maintenance", inline=True)
    embed.set_footer(text=footer_text())
    return embed


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────
def main():
    global CFG
    CFG = load_config()

    if not DISCORD_TOKEN:
        log.error("DISCORD_BOT_TOKEN environment variable is not set")
        return
    if not PA1_BOT_USER or not PA1_BOT_PASSWORD:
        log.error("PA1_BOT_USER and PA1_BOT_PASSWORD must be set")
        return

    log.info("Starting PurpleAssetOne Discord Bot...")
    log.info(f"PA1 API: {PA1_API_URL}")

    cmds = CFG.get("commands", {})
    log.info(f"Command names: {', '.join('/' + v.get('name', k) for k, v in cmds.items())}")

    # Update cache refresh interval from config
    cache_cfg = CFG.get("equipment_cache", {})
    interval = cache_cfg.get("refresh_interval_minutes", 10)
    bot.refresh_equipment_cache.change_interval(minutes=max(1, interval))

    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
