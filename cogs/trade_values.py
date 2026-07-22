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
from config import SLEEPER_LEAGUE_ID
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


# =========================================================================
# Shared data access (importable by other cogs, e.g. analytics power rankings)
# =========================================================================

async def get_latest_snapshot() -> list[dict[str, Any]]:
    """Return every player's most recent KTC value snapshot as row dicts."""
    async with db.execute(
        """
        SELECT * FROM ktc_values
        WHERE recorded_date = (SELECT MAX(recorded_date) FROM ktc_values)
        """
    ) as cursor:
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def index_by_sleeper_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index snapshot rows by sleeper_id, dropping players with no match."""
    return {r["sleeper_id"]: r for r in rows if r.get("sleeper_id")}


async def _value_as_of(ktc_id: int, cutoff_date: str) -> Optional[int]:
    """Most recent recorded superflex value for a player at or before a date."""
    async with db.execute(
        """
        SELECT value_sf FROM ktc_values
        WHERE ktc_id = ? AND recorded_date <= ?
        ORDER BY recorded_date DESC LIMIT 1
        """,
        (ktc_id, cutoff_date),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else None


async def get_team_dynasty_values(rosters: list[dict[str, Any]]) -> dict[int, int]:
    """Sum each roster's current superflex KTC value across all rostered
    players (starters, bench, taxi, IR). Players with no KTC match yet
    (e.g. deep practice squad guys) simply contribute 0.
    """
    by_sleeper_id = index_by_sleeper_id(await get_latest_snapshot())

    values: dict[int, int] = {}
    for roster in rosters:
        total = 0
        for player_id in roster.get("players") or []:
            row = by_sleeper_id.get(player_id)
            if row:
                total += row["value_sf"] or 0
        values[roster["roster_id"]] = total
    return values


async def get_team_dynasty_value_trends(
    rosters: list[dict[str, Any]], lookback_days: int = TREND_LOOKBACK_DAYS
) -> dict[int, tuple[int, int]]:
    """For each roster, return (current_total, prior_total) superflex value.

    prior_total re-prices the SAME currently-owned players using values
    from ~lookback_days ago, isolating market movement of a team's current
    assets from roster churn (trades/adds/drops changing who they own).
    """
    snapshot = await get_latest_snapshot()
    if not snapshot:
        return {roster["roster_id"]: (0, 0) for roster in rosters}

    latest_date = snapshot[0]["recorded_date"]
    cutoff = (
        datetime.fromisoformat(latest_date) - timedelta(days=lookback_days)
    ).date().isoformat()
    by_sleeper_id = index_by_sleeper_id(snapshot)

    trends: dict[int, tuple[int, int]] = {}
    for roster in rosters:
        current_total = 0
        prior_total = 0
        for player_id in roster.get("players") or []:
            row = by_sleeper_id.get(player_id)
            if not row:
                continue
            current_value = row["value_sf"] or 0
            current_total += current_value
            prior_value = await _value_as_of(row["ktc_id"], cutoff)
            prior_total += prior_value if prior_value is not None else current_value
        trends[roster["roster_id"]] = (current_total, prior_total)
    return trends


class TradeValues(commands.Cog):
    """Tracks dynasty player trade values sourced from KeepTradeCut."""

    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID

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
        candidates = await get_latest_snapshot()

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
        prior_value = await _value_as_of(ktc_id, cutoff)
        if prior_value is None or current_value is None:
            return None
        return current_value - prior_value

    # =========================================================================
    # Team Value Commands
    # =========================================================================

    @app_commands.command(
        name="teamvalues",
        description="Rank owners by total dynasty roster value (KeepTradeCut, Superflex)",
    )
    async def team_values(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users_list = await self.bot.sleeper.get_users(self.league_id)
            users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}

            values = await get_team_dynasty_values(rosters)
            if not any(values.values()):
                await interaction.followup.send(
                    "❌ No trade value data yet. Try `/synctradevalues` first."
                )
                return

            standings = sorted(
                (
                    {
                        "owner": users.get(r.get("owner_id", ""), f"Team {r['roster_id']}"),
                        "value": values.get(r["roster_id"], 0),
                    }
                    for r in rosters
                ),
                key=lambda t: t["value"],
                reverse=True,
            )

            table = "```\n"
            table += f"{'#':<3} {'Team':<18} {'Value':>10}\n"
            table += "-" * 34 + "\n"
            for idx, team in enumerate(standings, 1):
                table += f"{idx:<3} {team['owner'][:17]:<18} {team['value']:>10,}\n"
            table += "```"

            embed = discord.Embed(
                title="💰 Dynasty Team Values",
                description=table,
                color=discord.Color.gold(),
            )
            embed.set_footer(text="Total roster value (Superflex) • Source: KeepTradeCut")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"teamvalues failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error calculating team values: {e}")

    @app_commands.command(
        name="valuemovers",
        description="Winners and losers: biggest dynasty value swings this week",
    )
    async def value_movers(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users_list = await self.bot.sleeper.get_users(self.league_id)
            users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}

            trends = await get_team_dynasty_value_trends(rosters)
            if not trends or not any(current for current, _ in trends.values()):
                await interaction.followup.send(
                    "❌ Not enough trade value history yet. Check back after "
                    f"`/synctradevalues` has run for a few days."
                )
                return

            movers = [
                {
                    "owner": users.get(r.get("owner_id", ""), f"Team {r['roster_id']}"),
                    "delta": trends.get(r["roster_id"], (0, 0))[0]
                    - trends.get(r["roster_id"], (0, 0))[1],
                }
                for r in rosters
            ]
            movers.sort(key=lambda m: m["delta"], reverse=True)

            def fmt_list(entries: list[dict[str, Any]]) -> str:
                if not entries:
                    return "*No data*"
                return "\n".join(
                    f"{'📈' if m['delta'] > 0 else '📉' if m['delta'] < 0 else '➡️'} "
                    f"**{m['owner']}** {m['delta']:+,}"
                    for m in entries
                )

            embed = discord.Embed(
                title="📊 Dynasty Value: Winners & Losers",
                description=(
                    f"Change in total roster value (Superflex) over the last "
                    f"{TREND_LOOKBACK_DAYS} days"
                ),
                color=discord.Color.blue(),
            )
            embed.add_field(name="🔥 Biggest Gainers", value=fmt_list(movers[:3]), inline=True)
            embed.add_field(
                name="🥶 Biggest Fallers", value=fmt_list(movers[-3:][::-1]), inline=True
            )
            embed.set_footer(text="Source: KeepTradeCut")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"valuemovers failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error calculating value movers: {e}")

    @app_commands.command(
        name="tradecalc",
        description="Compare KeepTradeCut dynasty value of two sides of a proposed trade",
    )
    @app_commands.describe(
        side_a="Comma-separated players on side A (e.g. 'Justin Jefferson, Bijan Robinson')",
        side_b="Comma-separated players on side B",
    )
    async def trade_calc(
        self, interaction: discord.Interaction, side_a: str, side_b: str
    ):
        await interaction.response.defer()
        try:
            a_total, a_rows, a_missing = await self._evaluate_side(side_a)
            b_total, b_rows, b_missing = await self._evaluate_side(side_b)

            def fmt_side(rows: list[dict[str, Any]], missing: list[str]) -> str:
                lines = [f"**{r['player_name']}** — {_fmt(r['value_sf'])}" for r in rows]
                lines += [f"*{name} — not found*" for name in missing]
                return "\n".join(lines) or "*Nothing*"

            embed = discord.Embed(
                title="⚖️ Trade Value Calculator (Superflex)",
                color=discord.Color.purple(),
            )
            embed.add_field(
                name=f"Side A ({a_total:,} pts)", value=fmt_side(a_rows, a_missing), inline=True
            )
            embed.add_field(
                name=f"Side B ({b_total:,} pts)", value=fmt_side(b_rows, b_missing), inline=True
            )

            gap = a_total - b_total
            if gap == 0:
                verdict = "🤝 Dead even"
            else:
                winner = "Side A" if gap > 0 else "Side B"
                verdict = f"**{winner}** gets the better end by **{abs(gap):,}** pts"
            embed.add_field(name="Verdict", value=verdict, inline=False)
            embed.set_footer(text="Source: KeepTradeCut • Players only, picks not yet supported")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"tradecalc failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error calculating trade value: {e}")

    async def _evaluate_side(
        self, csv_names: str
    ) -> tuple[int, list[dict[str, Any]], list[str]]:
        """Look up each comma-separated player name and total their SF value."""
        rows: list[dict[str, Any]] = []
        missing: list[str] = []
        for raw_name in csv_names.split(","):
            name = raw_name.strip()
            if not name:
                continue
            row = await self._get_latest_value(name)
            if row is None:
                missing.append(name)
            else:
                rows.append(row)
        total = sum(r["value_sf"] or 0 for r in rows)
        return total, rows, missing


async def setup(bot: "DynastyBot") -> None:
    """Load the TradeValues cog."""
    await bot.add_cog(TradeValues(bot))
