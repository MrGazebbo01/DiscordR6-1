"""
Discord bot for Rainbow Six Siege marketplace price tracking and player statistics.
Features
--------
/ profile <nickname>
    • Shows the current ranked statistics for a player via R6Tracker API.

/ market search [weapon] [event] [type] [min_price] [max_price]
    • Searches R6 Marketplace (stats.cc endpoints) with optional filters.

/ alert add <skin_id_or_name>
    • Subscribes the invoking user to price‑change pings for the chosen skin.

/ alert remove <skin_id_or_name>
/ alert list
    • Manage existing alerts.

Implementation notes
--------------------
• Requires Python ≥ 3.10 and discord.py ≥ 2.3 (for built‑in slash‑commands).
• External API keys are read from environment variables (see README section).
• Alerts are persisted in SQLite (sqlite3) – thread‑safe with `aiosqlite`.
• Periodic price polling uses `discord.ext.tasks.loop`.
• Error handling and rate‑limits are kept minimal for clarity – extend in prod.
"""

from __future__ import annotations

import asyncio
import os
import logging
import re
from typing import Any, Optional, Dict, List

import aiosqlite
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
R6TRACKER_KEY = os.getenv("R6TRACKER_API_KEY")  # create at https://tracker.gg/developers
USER_AGENT = "R6MarketBot (+https://github.com/yourname/r6-market-bot)"

DB_PATH = "r6bot.db"
POLL_INTERVAL = 10 * 60  # seconds between marketplace polls

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("r6bot")

# ---------------------------------------------------------------------------
# Helper HTTP client
# ---------------------------------------------------------------------------
class HTTPClient:
    """Lightweight wrapper around aiohttp.ClientSession with JSON convenience."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_json(self, url: str, **params) -> Any:
        assert self.session, "HTTPClient not initialized via async context manager"
        async with self.session.get(url, params=params, timeout=30) as resp:
            resp.raise_for_status()
            return await resp.json()

# ---------------------------------------------------------------------------
# R6 Marketplace & Tracker API wrappers
# ---------------------------------------------------------------------------
class MarketplaceAPI:
    BASE = "https://stats.cc/api/siege/marketplace/v1"

    async def search(self, *, weapon: str | None = None, event: str | None = None,
                     type_: str | None = None, min_price: int | None = None,
                     max_price: int | None = None) -> list[dict[str, Any]]:
        """Search marketplace items with optional filters."""
        params: Dict[str, Any] = {}
        if weapon:
            params["weapon"] = weapon
        if event:
            params["event"] = event
        if type_:
            params["type"] = type_
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price

        async with HTTPClient() as http:
            data = await http.get_json(f"{self.BASE}/search", **params)
            return data.get("items", [])

    async def item(self, item_id: str | int) -> dict[str, Any]:
        async with HTTPClient() as http:
            return await http.get_json(f"{self.BASE}/item/{item_id}")

class R6TrackerAPI:
    BASE = "https://api.tracker.gg/api/v2/r6/standard/profile/ubi"

    async def profile(self, nickname: str) -> dict[str, Any]:
        headers = {"TRN-Api-Key": R6TRACKER_KEY}
        async with HTTPClient() as http:
            http.session.headers.update(headers)  # type: ignore
            return await http.get_json(f"{self.BASE}/{nickname}")

# ---------------------------------------------------------------------------
# Persistence layer for alerts
# ---------------------------------------------------------------------------
class AlertRepo:
    """Stores price alerts in SQLite.

    Schema:
        CREATE TABLE IF NOT EXISTS alerts (
            guild_id   INTEGER,
            user_id    INTEGER,
            item_id    TEXT,
            last_price INTEGER,
            PRIMARY KEY (guild_id, user_id, item_id)
        );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def setup(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    guild_id   INTEGER,
                    user_id    INTEGER,
                    item_id    TEXT,
                    last_price INTEGER,
                    PRIMARY KEY (guild_id, user_id, item_id)
                );
                """
            )
            await db.commit()

    async def add_alert(self, guild_id: int, user_id: int, item_id: str, last_price: int | None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO alerts VALUES (?, ?, ?, ?)",
                (guild_id, user_id, item_id, last_price),
            )
            await db.commit()

    async def remove_alert(self, guild_id: int, user_id: int, item_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM alerts WHERE guild_id=? AND user_id=? AND item_id=?",
                (guild_id, user_id, item_id),
            )
            await db.commit()

    async def list_alerts(self, guild_id: int, user_id: int) -> list[tuple[str, int | None]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT item_id, last_price FROM alerts WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ) as cursor:
                return await cursor.fetchall()

    async def all_alerts(self) -> list[tuple[int, int, str, int | None]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT guild_id, user_id, item_id, last_price FROM alerts"
            ) as cursor:
                return await cursor.fetchall()

    async def update_price(self, guild_id: int, user_id: int, item_id: str, price: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE alerts SET last_price=? WHERE guild_id=? AND user_id=? AND item_id=?",
                (price, guild_id, user_id, item_id),
            )
            await db.commit()

# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------
class R6Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("/"),
            intents=discord.Intents.default(),
            application_id=os.getenv("APPLICATION_ID"),
        )

        self.market_api = MarketplaceAPI()
        self.tracker_api = R6TrackerAPI()
        self.alert_repo = AlertRepo(DB_PATH)

        self.tree.add_command(self.profile_command)
        self.tree.add_command(self.market_group)
        self.tree.add_command(self.alert_group)

        self.poll_marketplaces.start()

    # ------------------------- Slash Commands -------------------------
    @app_commands.command(name="profile", description="Mostra le statistiche ranked attuali di un giocatore")
    @app_commands.describe(nickname="Nickname Uplay / Ubisoft Connect")
    async def profile_command(self, interaction: discord.Interaction, nickname: str):
        await interaction.response.defer(thinking=True)
        try:
            data = await self.tracker_api.profile(nickname)
            segments = data.get("data", {}).get("segments", [])
            if not segments:
                await interaction.followup.send(f"❌ Nessun profilo trovato per **{nickname}**")
                return

            rank_info = next((s for s in segments if s.get("type") == "overview"), segments[0])
            stats = rank_info.get("stats", {})
            mmr = stats.get("mmr", {}).get("displayValue", "N/A")
            rank = stats.get("rank", {}).get("metadata", {}).get("name", "?")
            kd = stats.get("kd", {}).get("displayValue", "N/A")
            wl = stats.get("wlPercentage", {}).get("displayValue", "N/A")

            embed = discord.Embed(title=f"{nickname} — Ranked Stats", color=discord.Color.blue())
            embed.add_field(name="Rank", value=f"{rank} ({mmr} MMR)")
            embed.add_field(name="K/D", value=kd)
            embed.add_field(name="Win %", value=wl)
            embed.set_footer(text="Dati da R6Tracker.com")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            log.exception("Errore in /profile")
            await interaction.followup.send("❌ Errore nel recuperare le statistiche. Riprova più tardi.")

    # ------------------------- /market -------------------------
    market_group = app_commands.Group(name="market", description="Cerca skin nel marketplace")

    @market_group.command(name="search", description="Ricerca skin per arma, evento, tipo, prezzo…")
    async def market_search(
        self,
        interaction: discord.Interaction,
        weapon: Optional[str] = None,
        event: Optional[str] = None,
        type_: Optional[str] = app_commands.Param(name="type", default=None),
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
    ):
        await interaction.response.defer(thinking=True)
        items = await self.market_api.search(
            weapon=weapon,
            event=event,
            type_=type_,
            min_price=min_price,
            max_price=max_price,
        )
        if not items:
            await interaction.followup.send("❌ Nessun risultato trovato.")
            return

        # Limit to first 10 results to avoid spam
        embed = discord.Embed(title="Risultati Marketplace", color=discord.Color.green())
        for it in items[:10]:
            price = it.get("price", {}).get("latest", "–")
            embed.add_field(
                name=f"{it['name']} (ID {it['id']})",
                value=f"Weapon: {it.get('weapon')}\nEvent: {it.get('event')}\nTipo: {it.get('type')}\nPrezzo: {price} 🪙",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    # ------------------------- /alert -------------------------
    alert_group = app_commands.Group(name="alert", description="Gestisci gli alert di prezzo per le skin")

    @alert_group.command(name="add", description="Ricevi un ping quando cambia il prezzo di una skin")
    @app_commands.describe(item_id="ID oppure nome preciso della skin")
    async def alert_add(self, interaction: discord.Interaction, item_id: str):
        await interaction.response.defer(ephemeral=True)
        # Resolve ID if user passed name
        item_id_resolved = await self._resolve_item_id(item_id)
        if item_id_resolved is None:
            await interaction.followup.send("❌ Skin non trovata.")
            return
        item = await self.market_api.item(item_id_resolved)
        last_price = item.get("price", {}).get("latest")
        await self.alert_repo.add_alert(interaction.guild.id, interaction.user.id, str(item_id_resolved), last_price)
        await interaction.followup.send(f"🔔 Alert impostato per **{item.get('name')}** (prezzo attuale: {last_price})")

    @alert_group.command(name="remove", description="Elimina l’alert per una skin")
    async def alert_remove(self, interaction: discord.Interaction, item_id: str):
        await interaction.response.defer(ephemeral=True)
        await self.alert_repo.remove_alert(interaction.guild.id, interaction.user.id, item_id)
        await interaction.followup.send("🗑️ Alert rimosso, se esisteva.")

    @alert_group.command(name="list", description="Mostra i tuoi alert attivi")
    async def alert_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await self.alert_repo.list_alerts(interaction.guild.id, interaction.user.id)
        if not rows:
            await interaction.followup.send("(nessun alert impostato)")
            return
        msg = "\n".join([f"• {item_id} (ultimo prezzo: {price})" for item_id, price in rows])
        await interaction.followup.send(f"I tuoi alert:\n{msg}")

    # -------------------------- Background polling --------------------------
    @tasks.loop(seconds=POLL_INTERVAL)
    async def poll_marketplaces(self):
        await self.wait_until_ready()
        log.info("Esecuzione polling marketplace…")
        alerts = await self.alert_repo.all_alerts()
        for guild_id, user_id, item_id, last_price in alerts:
            try:
                item = await self.market_api.item(item_id)
                price = item.get("price", {}).get("latest")
                if price is None:
                    continue
                if last_price is None or price != last_price:
                    await self._notify_price_change(guild_id, user_id, item, price, last_price)
                    await self.alert_repo.update_price(guild_id, user_id, item_id, price)
            except Exception:
                log.exception("Errore durante polling di item %s", item_id)

    async def _notify_price_change(self, guild_id: int, user_id: int, item: dict[str, Any], new_price: int, old_price: Optional[int]):
        guild = self.get_guild(guild_id)
        if guild is None:
            return
        member = guild.get_member(user_id)
        if member is None:
            return
        try:
            await member.send(f"💸 Prezzo aggiornato per **{item['name']}**: {old_price or 'N/A'} → {new_price} 🪙")
        except discord.Forbidden:
            log.warning("Impossibile mandare messaggio privato a %s", member)

    async def _resolve_item_id(self, query: str) -> Optional[str]:
        """If user passed a name instead of ID, attempt to resolve via search."""
        if re.fullmatch(r"\d+", query):
            return query  # Already an ID
        results = await self.market_api.search()
        for it in results:
            if it["name"].lower() == query.lower():
                return str(it["id"])
        return None

    # -------------------------- Events --------------------------
    async def setup_hook(self):
        # Ensure DB
        await self.alert_repo.setup()
        # Sync commands to all guilds at startup
        await self.tree.sync()

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        raise RuntimeError("Set the DISCORD_TOKEN environment variable")

    bot = R6Bot()
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
