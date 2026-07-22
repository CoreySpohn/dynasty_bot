"""Trade Values Cog for Dynasty Bot.

Syncs dynasty player trade values from KeepTradeCut on a daily schedule
and stores a dated snapshot of every player's value so trends can be
tracked over time. Also exposes a lookup command for the latest value.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from clients.keeptradecut import KeepTradeCutClient
from database import db

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.trade_values")

# Suffixes KTC/Sleeper inconsistently include (e.g. "Kenneth Walker III").
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")

TREND_LOOKBACK_DAYS = 7


def _fmt(value: Optional[int]) -> str:
    """Format a possibly-missing numeric value for embed display."""
    return f"{value:,}" if value is not None else "N/A"


def normalize_name(name: str) -> str:
    """Normalize a player name for cross-source matching.

    Lowercases, strips punctuation, and drops trailing generational
    suffixes so e.g. "Kenneth Walker III" and "Kenneth Walker" match.
    """
    cleaned = _NON_ALNUM_RE.sub("", name.lower().replace("-", " "))
    parts = [p for p in cleaned.split() if p not in _NAME_SUFFIXES]
    return " ".join(parts)


class TradeValues(commands.Cog):
    """Tracks dynasty player trade values sourced from KeepTradeCut."""

    def __init__(self, bot: "DynastyBot"):
        self.bot = bot

    def cog_load(self) -> None:
        self.sync_task.start()

    def cog_unload(self) -> None:
        self.sync_task.cancel()

    # =========================================================================
    # Daily Sync
    # =========================================================================

    @tasks.loop(hours=24)
    async def sync_task(self):
        """Fetch and store today's KeepTradeCut value snapshot."""
        try:
            count = await self._sync_values()
            logger.info(f"Synced {count} KeepTradeCut player values")
        except Exception as e:
            logger.error(f"KeepTradeCut sync failed: {e}", exc_info=True)

    @sync_task.before_loop
    async def before_sync_task(self):
        await self.bot.wait_until_ready()

    async def _sync_values(self) -> int:
        """Fetch current KTC values, match them to Sleeper IDs, and store
        today's snapshot. Re-running on the same day overwrites that day's
        rows rather than duplicating them.

        Returns:
            Number of player rows written.
        """
        async with aiohttp.ClientSession() as session:
            players = await KeepTradeCutClient(session).get_player_values()
        if not players:
            return 0

        sleeper_players = await self.bot.sleeper.get_all_players()
        name_index: dict[str, list[tuple[str, str]]] = {}
        for sleeper_id, data in sleeper_players.items():
            full_name = data.get("full_name")
            if not full_name:
                continue
            key = normalize_name(full_name)
            name_index.setdefault(key, []).append((sleeper_id, data.get("position") or ""))

        today = datetime.now().date().isoformat()

        for p in players:
            sleeper_id = self._match_sleeper_id(p, name_index)
            async with db.execute(
                """
                INSERT INTO ktc_values (
                    ktc_id, sleeper_id, player_name, position, team, is_rookie,
                    value_1qb, rank_1qb, positional_rank_1qb,
                    value_sf, rank_sf, positional_rank_sf, recorded_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ktc_id, recorded_date) DO UPDATE SET
                    sleeper_id=excluded.sleeper_id,
                    team=excluded.team,
                    value_1qb=excluded.value_1qb,
                    rank_1qb=excluded.rank_1qb,
                    positional_rank_1qb=excluded.positional_rank_1qb,
                    value_sf=excluded.value_sf,
                    rank_sf=excluded.rank_sf,
                    positional_rank_sf=excluded.positional_rank_sf
                """,
                (
                    p["ktc_id"], sleeper_id, p["name"], p["position"], p["team"],
                    p["rookie"], p["value_1qb"], p["rank_1qb"], p["positional_rank_1qb"],
                    p["value_sf"], p["rank_sf"], p["positional_rank_sf"], today,
                ),
            ):
                pass

        return len(players)

    def _match_sleeper_id(
        self, ktc_player: dict[str, Any], name_index: dict[str, list[tuple[str, str]]]
    ) -> Optional[str]:
        """Best-effort match of a KTC player to a Sleeper player_id by
        normalized name, preferring a position match when a name is shared
        by multiple Sleeper entries (e.g. retired players still in the DB).
        """
        candidates = name_index.get(normalize_name(ktc_player["name"]))
        if not candidates:
            return None
        for sleeper_id, position in candidates:
            if position == ktc_player["position"]:
                return sleeper_id
        return candidates[0][0]

    # =========================================================================
    # Commands
    # =========================================================================

    @app_commands.command(
        name="synctradevalues",
        description="Manually refresh dynasty trade values from KeepTradeCut",
    )
    async def sync_trade_values(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            count = await self._sync_values()
            if count == 0:
                await interaction.followup.send(
                    "❌ Failed to fetch values from KeepTradeCut. It may be down or blocking requests."
                )
                return
            await interaction.followup.send(
                f"✅ Synced trade values for **{count}** players from KeepTradeCut."
            )
        except Exception as e:
            logger.error(f"synctradevalues failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error syncing trade values: {e}")

    @app_commands.command(
        name="tradevalue",
        description="Look up a player's current KeepTradeCut dynasty trade value",
    )
    @app_commands.describe(player="Player name to search for")
    async def trade_value(self, interaction: discord.Interaction, player: str):
        await interaction.response.defer()

        try:
            row = await self._get_latest_value(player)
            if row is None:
                await interaction.followup.send(
                    f"❌ No trade value found for \"{player}\". "
                    "Try `/synctradevalues` if this player was recently added."
                )
                return

            trend = await self._get_trend(row["ktc_id"], row["value_sf"], row["recorded_date"])

            embed = discord.Embed(
                title=f"💰 {row['player_name']} ({row['position']}{' - ' + row['team'] if row['team'] else ''})",
                color=discord.Color.gold(),
            )
            embed.add_field(
                name="Superflex",
                value=(
                    f"**{_fmt(row['value_sf'])}** pts\n"
                    f"Rank #{_fmt(row['rank_sf'])} (#{_fmt(row['positional_rank_sf'])} {row['position']})"
                ),
                inline=True,
            )
            embed.add_field(
                name="1QB",
                value=(
                    f"**{_fmt(row['value_1qb'])}** pts\n"
                    f"Rank #{_fmt(row['rank_1qb'])} (#{_fmt(row['positional_rank_1qb'])} {row['position']})"
                ),
                inline=True,
            )
            if trend is not None:
                arrow = "📈" if trend > 0 else "📉" if trend < 0 else "➡️"
                embed.add_field(
                    name=f"{TREND_LOOKBACK_DAYS}-Day Trend (SF)",
                    value=f"{arrow} {trend:+,}",
                    inline=True,
                )
            embed.set_footer(text=f"Values as of {row['recorded_date']} • Source: KeepTradeCut")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"tradevalue failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error looking up trade value: {e}")

    async def _get_latest_value(self, player_query: str) -> Optional[dict[str, Any]]:
        """Find the most recent snapshot row for a player by fuzzy name match."""
        query_key = normalize_name(player_query)
        async with db.execute(
            """
            SELECT * FROM ktc_values
            WHERE recorded_date = (SELECT MAX(recorded_date) FROM ktc_values)
            """
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]

        candidates = [dict(zip(columns, row)) for row in rows]

        exact = [c for c in candidates if normalize_name(c["player_name"]) == query_key]
        if exact:
            return exact[0]

        partial = [c for c in candidates if query_key in normalize_name(c["player_name"])]
        if partial:
            partial.sort(key=lambda c: c["value_sf"] or 0, reverse=True)
            return partial[0]

        return None

    async def _get_trend(
        self, ktc_id: int, current_value: int, current_date: str
    ) -> Optional[int]:
        """Return the change in superflex value over the last week, if we
        have a snapshot from around then."""
        cutoff = (
            datetime.fromisoformat(current_date) - timedelta(days=TREND_LOOKBACK_DAYS)
        ).date().isoformat()

        async with db.execute(
            """
            SELECT value_sf FROM ktc_values
            WHERE ktc_id = ? AND recorded_date <= ?
            ORDER BY recorded_date DESC LIMIT 1
            """,
            (ktc_id, cutoff),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None or row[0] is None or current_value is None:
            return None
        return current_value - row[0]


async def setup(bot: "DynastyBot") -> None:
    """Load the TradeValues cog."""
    await bot.add_cog(TradeValues(bot))
