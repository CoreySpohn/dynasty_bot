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
from lib.roster_history import get_composition_as_of
from lib.roster_history import store_snapshot as store_roster_snapshot

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.trade_values")

# Suffixes KTC/Sleeper inconsistently include (e.g. "Kenneth Walker III").
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")

TREND_LOOKBACK_DAYS = 7

# KTC prices rookie draft picks as its own "RDP" position, named by season,
# tier and round - e.g. "2027 Early 1st". Only rounds 1-4 are published.
PICK_POSITION = "RDP"
PICK_TIERS = ("early", "mid", "late")
# Nobody says "my 2027 mid 1st" in a trade offer, they say "my 2027 1st".
# Mid is the neutral read on an unqualified pick; once the season is under
# way, standings tell you whether it's really an Early or a Late.
DEFAULT_PICK_TIER = "mid"
_PICK_ROUNDS = {
    "1": "1st", "1st": "1st", "first": "1st",
    "2": "2nd", "2nd": "2nd", "second": "2nd",
    "3": "3rd", "3rd": "3rd", "third": "3rd",
    "4": "4th", "4th": "4th", "fourth": "4th",
}
# Words people pad pick references with that carry no meaning here.
_PICK_NOISE = {"round", "rounder", "pick", "picks", "rd"}


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


def resolve_pick_name(query: str) -> Optional[str]:
    """Resolve a loose draft-pick reference to KTC's canonical RDP row name.

    Handles the phrasings people actually type into a trade offer - "2027
    1st", "27 2nd round pick", "2028 late 3rd" - and returns the KTC name
    for that pick (e.g. "2027 Mid 1st"). A pick with no stated tier
    resolves to DEFAULT_PICK_TIER.

    Deliberately strict: any token that isn't a season, tier, round or
    filler word means this isn't a pick reference, so ordinary player
    names (and owner-qualified picks like "Corey's 2027 1st", which we
    can't price without knowing whose pick it is) fall through to normal
    name matching instead of being silently mispriced.

    Returns:
        Canonical KTC pick name, or None if `query` isn't a pick reference.
    """
    season: Optional[int] = None
    tier: Optional[str] = None
    round_label: Optional[str] = None

    for token in normalize_name(query).split():
        if token in _PICK_NOISE:
            continue
        if token in PICK_TIERS:
            tier = token
        elif token in _PICK_ROUNDS:
            round_label = _PICK_ROUNDS[token]
        elif token.isdigit() and len(token) == 4 and token.startswith("20"):
            season = int(token)
        elif token.isdigit() and len(token) == 2:
            season = 2000 + int(token)
        else:
            return None

    if season is None or round_label is None:
        return None
    return f"{season} {(tier or DEFAULT_PICK_TIER).title()} {round_label}"


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


async def get_snapshot_as_of(cutoff_date: str) -> list[dict[str, Any]]:
    """Every player's value row from the newest KTC snapshot on or before a date.

    Returns an empty list if we have no snapshot that old - KTC has no
    historical API, so value history only reaches back to the first sync.
    """
    async with db.execute(
        """
        SELECT * FROM ktc_values
        WHERE recorded_date = (
            SELECT MAX(recorded_date) FROM ktc_values WHERE recorded_date <= ?
        )
        """,
        (cutoff_date,),
    ) as cursor:
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def index_by_sleeper_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index snapshot rows by sleeper_id, dropping players with no match."""
    return {r["sleeper_id"]: r for r in rows if r.get("sleeper_id")}


async def get_value_as_of(ktc_id: int, cutoff_date: str) -> Optional[int]:
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


async def get_value_trend(
    ktc_id: int,
    current_value: Optional[int],
    current_date: str,
    lookback_days: int = TREND_LOOKBACK_DAYS,
) -> Optional[int]:
    """Change in superflex value over the trailing lookback window, if we
    have a snapshot from around then."""
    if current_value is None:
        return None
    cutoff = (
        datetime.fromisoformat(current_date) - timedelta(days=lookback_days)
    ).date().isoformat()
    prior_value = await get_value_as_of(ktc_id, cutoff)
    if prior_value is None:
        return None
    return current_value - prior_value


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
            prior_value = await get_value_as_of(row["ktc_id"], cutoff)
            prior_total += prior_value if prior_value is not None else current_value
        trends[roster["roster_id"]] = (current_total, prior_total)
    return trends


async def get_team_dynasty_values_as_of(cutoff_date: str) -> dict[int, int]:
    """Total superflex value of each roster *as it stood* on a past date.

    Prices the roster recorded in the newest roster snapshot on or before
    `cutoff_date` using the KTC values from that same as-of date. Unlike
    get_team_dynasty_value_trends, this reflects roster churn: a team that
    traded a star away really is worth less afterwards.

    Returns:
        {roster_id: total_sf_value}, empty if we have no roster snapshot
        that far back (roster history only starts when snapshotting began).
    """
    composition = await get_composition_as_of(cutoff_date)
    if not composition:
        return {}

    by_sleeper_id = index_by_sleeper_id(await get_snapshot_as_of(cutoff_date))
    return {
        roster_id: sum(
            (by_sleeper_id.get(player_id) or {}).get("value_sf") or 0
            for player_id in player_ids
        )
        for roster_id, player_ids in composition.items()
    }


async def get_team_value_changes(
    rosters: list[dict[str, Any]], lookback_days: int = TREND_LOOKBACK_DAYS
) -> dict[int, tuple[int, Optional[int]]]:
    """For each roster, (current_total, prior_total) including roster churn.

    The companion to get_team_dynasty_value_trends, answering the other
    half of "how has my team's value moved":

    - get_team_dynasty_value_trends re-prices *today's* players at old
      values, isolating pure market movement.
    - this prices *the roster you actually had* then against the roster you
      have now, so trades, adds and drops all count.

    Returns:
        {roster_id: (current_total, prior_total_or_None)}. prior_total is
        None where no roster snapshot reaches back that far.
    """
    current = await get_team_dynasty_values(rosters)

    cutoff = (
        datetime.now() - timedelta(days=lookback_days)
    ).date().isoformat()
    prior = await get_team_dynasty_values_as_of(cutoff)

    return {
        roster["roster_id"]: (
            current.get(roster["roster_id"], 0),
            prior.get(roster["roster_id"]),
        )
        for roster in rosters
    }


# =========================================================================
# Trade grading
#
# Sleeper records what moved and when; ktc_values records what those assets
# were worth on that date. Together that grades a trade at the moment it
# happened, and re-pricing the same assets at today's values is what turns
# it into a regret tracker.
# =========================================================================

def trade_sides(transaction: dict[str, Any]) -> dict[int, dict[str, list]]:
    """Split a Sleeper trade into what each roster *received*.

    Sleeper's `adds` maps player_id to the roster that got them, and each
    entry in `draft_picks` names the receiving roster in `owner_id`.

    Returns:
        {roster_id: {"players": [player_id], "picks": [pick_dict]}}
    """
    roster_ids = transaction.get("roster_ids") or []
    sides: dict[int, dict[str, list]] = {
        roster_id: {"players": [], "picks": []} for roster_id in roster_ids
    }

    for player_id, roster_id in (transaction.get("adds") or {}).items():
        if roster_id in sides:
            sides[roster_id]["players"].append(player_id)

    for pick in transaction.get("draft_picks") or []:
        owner_id = pick.get("owner_id")
        if owner_id in sides:
            sides[owner_id]["picks"].append(pick)

    return sides


def trade_date(transaction: dict[str, Any]) -> Optional[str]:
    """ISO date a trade completed, from Sleeper's epoch-millisecond stamp."""
    timestamp = transaction.get("status_updated") or transaction.get("created")
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp / 1000).date().isoformat()


def pick_ktc_name(pick: dict[str, Any]) -> Optional[str]:
    """KTC row name for a Sleeper draft pick, at the neutral tier.

    Sleeper only records season and round. KTC prices Early/Mid/Late
    separately, and which one a future pick becomes isn't knowable until
    that season's standings exist - so these price at DEFAULT_PICK_TIER,
    the same assumption /tradecalc makes for an unqualified pick.
    """
    season, round_no = pick.get("season"), pick.get("round")
    if season is None or round_no is None:
        return None
    return resolve_pick_name(f"{season} {round_no}")


def _index_by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index value rows by normalized name (how pick rows have to be found,
    since KTC's RDP entries have no Sleeper ID)."""
    return {normalize_name(r["player_name"]): r for r in rows}


def _price_items(
    received: dict[str, list],
    by_sleeper_id: dict[str, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    player_names: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Label and price everything one roster received in a trade."""
    items: list[dict[str, Any]] = []

    for player_id in received["players"]:
        row = by_sleeper_id.get(player_id)
        label = (row or {}).get("player_name")
        if not label and player_names:
            label = (player_names.get(player_id) or {}).get("full_name")
        items.append(
            {
                "label": label or f"Player {player_id}",
                "sleeper_id": player_id,
                "value": (row or {}).get("value_sf") or 0,
                "priced": row is not None,
            }
        )

    for pick in received["picks"]:
        name = pick_ktc_name(pick)
        row = by_name.get(normalize_name(name)) if name else None
        items.append(
            {
                "label": name or "Unknown pick",
                "sleeper_id": None,
                "value": (row or {}).get("value_sf") or 0,
                "priced": row is not None,
            }
        )

    return items


async def grade_trade(
    transaction: dict[str, Any],
    player_names: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """Price both sides of a completed trade, then and now.

    Args:
        transaction: A Sleeper transaction dict.
        player_names: Optional Sleeper players map, used only to label
            players KTC doesn't rank.

    Returns:
        {
          "trade_date": ISO date,
          "sides": {roster_id: {"items": [...], "then": int, "now": int}},
        }
        or None if this isn't a completed trade, has no usable timestamp,
        or predates our first KTC snapshot (KTC has no historical API, so
        older trades simply cannot be priced at the time they happened).
    """
    if transaction.get("type") != "trade":
        return None
    if transaction.get("status") not in (None, "complete"):
        return None

    as_of = trade_date(transaction)
    if not as_of:
        return None

    then_snapshot = await get_snapshot_as_of(as_of)
    if not then_snapshot:
        return None
    now_snapshot = await get_latest_snapshot()

    then_by_id = index_by_sleeper_id(then_snapshot)
    then_by_name = _index_by_name(then_snapshot)
    now_by_id = index_by_sleeper_id(now_snapshot)
    now_by_name = _index_by_name(now_snapshot)

    sides: dict[int, dict[str, Any]] = {}
    for roster_id, received in trade_sides(transaction).items():
        then_items = _price_items(received, then_by_id, then_by_name, player_names)
        now_items = _price_items(received, now_by_id, now_by_name, player_names)
        sides[roster_id] = {
            "items": then_items,
            "then": sum(item["value"] for item in then_items),
            "now": sum(item["value"] for item in now_items),
        }

    return {"trade_date": as_of, "sides": sides}


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
        """Fetch and store today's KeepTradeCut values and roster composition."""
        try:
            count = await self._sync_values()
            logger.info(f"Synced {count} KeepTradeCut player values")
        except Exception as e:
            logger.error(f"KeepTradeCut sync failed: {e}", exc_info=True)

        # Separate try block: a Sleeper hiccup shouldn't cost us the KTC
        # snapshot (or vice versa). Both are unrecoverable if missed.
        try:
            await self._snapshot_rosters()
        except Exception as e:
            logger.error(f"Roster snapshot failed: {e}", exc_info=True)

    async def _snapshot_rosters(self) -> bool:
        """Record today's roster composition, skipping unchanged days."""
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        return await store_roster_snapshot(rosters)

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

            snapshotted = await self._snapshot_rosters()
            roster_note = (
                "Roster composition snapshotted."
                if snapshotted
                else "Roster composition unchanged since the last snapshot."
            )
            await interaction.followup.send(
                f"✅ Synced trade values for **{count}** players from "
                f"KeepTradeCut. {roster_note}"
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
        """Find the most recent snapshot row for a player or draft pick.

        Tries an exact name match, then a draft-pick interpretation (so
        "2027 1st" finds KTC's "2027 Mid 1st" row), then a fuzzy substring
        match.
        """
        query_key = normalize_name(player_query)
        candidates = await get_latest_snapshot()

        exact = [c for c in candidates if normalize_name(c["player_name"]) == query_key]
        if exact:
            return exact[0]

        pick_name = resolve_pick_name(player_query)
        if pick_name:
            pick_key = normalize_name(pick_name)
            picks = [
                c for c in candidates if normalize_name(c["player_name"]) == pick_key
            ]
            if picks:
                return picks[0]

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
        return await get_value_trend(ktc_id, current_value, current_date)

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
        name="tradegrades",
        description="Grade a week's trades using the values from the day they happened",
    )
    @app_commands.describe(week="NFL week to check (defaults to the current week)")
    async def trade_grades(
        self, interaction: discord.Interaction, week: Optional[int] = None
    ):
        await interaction.response.defer()
        try:
            league = await self.bot.sleeper.get_league(self.league_id)
            target_week = week or league.get("settings", {}).get("leg", 1)

            transactions = await self.bot.sleeper.get_transactions(
                self.league_id, target_week
            )
            trades = [t for t in (transactions or []) if t.get("type") == "trade"]
            if not trades:
                await interaction.followup.send(
                    f"📭 No trades found in week {target_week}."
                )
                return

            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users_list = await self.bot.sleeper.get_users(self.league_id)
            users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}
            owner_by_roster = {
                r["roster_id"]: users.get(r.get("owner_id", ""), f"Team {r['roster_id']}")
                for r in rosters
            }
            players = await self.bot.sleeper.get_all_players()

            embeds: list[discord.Embed] = []
            unpriceable = 0
            for trade in trades:
                graded = await grade_trade(trade, player_names=players)
                if graded is None:
                    unpriceable += 1
                    continue
                embeds.append(self._build_trade_grade_embed(graded, owner_by_roster))

            if not embeds:
                await interaction.followup.send(
                    f"❌ Found {len(trades)} trade(s) in week {target_week}, but none "
                    "could be priced - KeepTradeCut has no historical API, so trades "
                    "from before the first value sync can't be graded at the time "
                    "they happened."
                )
                return

            # Discord caps a single message at 10 embeds.
            await interaction.followup.send(embeds=embeds[:10])
            if unpriceable or len(embeds) > 10:
                notes = []
                if len(embeds) > 10:
                    notes.append(f"{len(embeds) - 10} more trade(s) not shown")
                if unpriceable:
                    notes.append(
                        f"{unpriceable} trade(s) predate our value history"
                    )
                await interaction.followup.send(f"ℹ️ {'; '.join(notes)}.")

        except Exception as e:
            logger.error(f"tradegrades failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error grading trades: {e}")

    def _build_trade_grade_embed(
        self, graded: dict[str, Any], owner_by_roster: dict[int, str]
    ) -> discord.Embed:
        """Render one graded trade as then-vs-now value per side."""
        embed = discord.Embed(
            title=f"⚖️ Trade from {graded['trade_date']}",
            color=discord.Color.purple(),
        )

        for roster_id, side in graded["sides"].items():
            lines = [
                f"**{item['label']}** — {_fmt(item['value'])}"
                + ("" if item["priced"] else " *(unranked)*")
                for item in side["items"]
            ] or ["*Nothing*"]

            drift = side["now"] - side["then"]
            arrow = "📈" if drift > 0 else "📉" if drift < 0 else "➡️"
            lines.append(
                f"\nThen: **{side['then']:,}** → Now: **{side['now']:,}** "
                f"{arrow} {drift:+,}"
            )

            embed.add_field(
                name=f"{owner_by_roster.get(roster_id, f'Team {roster_id}')} receives",
                value="\n".join(lines),
                inline=True,
            )

        totals = {rid: side["then"] for rid, side in graded["sides"].items()}
        if len(totals) == 2:
            (a_id, a_total), (b_id, b_total) = totals.items()
            gap = a_total - b_total
            if gap == 0:
                verdict = "🤝 Dead even at the time"
            else:
                winner = a_id if gap > 0 else b_id
                verdict = (
                    f"**{owner_by_roster.get(winner, f'Team {winner}')}** won it on "
                    f"paper by **{abs(gap):,}** pts"
                )
            embed.add_field(name="Verdict (at trade time)", value=verdict, inline=False)

        embed.set_footer(
            text=(
                "Values as of the trade date • Picks priced as "
                f"{DEFAULT_PICK_TIER.title()} • Source: KeepTradeCut"
            )
        )
        return embed

    @app_commands.command(
        name="valuehistory",
        description="How each team's dynasty value has changed, trades included",
    )
    @app_commands.describe(days="How far back to compare (default 7, max 365)")
    async def value_history(
        self, interaction: discord.Interaction, days: Optional[int] = None
    ):
        await interaction.response.defer()
        try:
            lookback = max(1, min(days or TREND_LOOKBACK_DAYS, 365))

            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users_list = await self.bot.sleeper.get_users(self.league_id)
            users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}

            changes = await get_team_value_changes(rosters, lookback_days=lookback)
            if not any(prior is not None for _, prior in changes.values()):
                await interaction.followup.send(
                    f"❌ No roster snapshot from {lookback} days ago yet. Roster "
                    "history only reaches back to when snapshotting started, "
                    "and neither Sleeper nor KeepTradeCut can backfill it - so "
                    "this fills in as the daily sync runs. Try a smaller "
                    "`days`, or `/valuemovers` for pure market movement."
                )
                return

            rows = sorted(
                (
                    {
                        "owner": users.get(
                            r.get("owner_id", ""), f"Team {r['roster_id']}"
                        ),
                        "current": changes[r["roster_id"]][0],
                        "prior": changes[r["roster_id"]][1],
                    }
                    for r in rosters
                ),
                key=lambda t: (
                    (t["current"] - t["prior"]) if t["prior"] is not None else 0
                ),
                reverse=True,
            )

            table = "```\n"
            table += f"{'Team':<18} {'Then':>9} {'Now':>9} {'Change':>9}\n"
            table += "-" * 48 + "\n"
            for row in rows:
                if row["prior"] is None:
                    table += (
                        f"{row['owner'][:17]:<18} {'--':>9} "
                        f"{row['current']:>9,} {'n/a':>9}\n"
                    )
                else:
                    delta = row["current"] - row["prior"]
                    table += (
                        f"{row['owner'][:17]:<18} {row['prior']:>9,} "
                        f"{row['current']:>9,} {delta:>+9,}\n"
                    )
            table += "```"

            embed = discord.Embed(
                title="📈 Dynasty Value History",
                description=(
                    f"Total roster value (Superflex) now vs **{lookback} days "
                    "ago**, priced against the roster each team actually had "
                    "then - so trades, adds and drops all count.\n"
                    "For market movement on current players only, use "
                    "`/valuemovers`."
                )
                + f"\n{table}",
                color=discord.Color.blue(),
            )
            embed.set_footer(text="Source: KeepTradeCut + roster snapshots")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"valuehistory failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building value history: {e}")

    @app_commands.command(
        name="tradecalc",
        description="Compare KeepTradeCut dynasty value of two sides of a proposed trade",
    )
    @app_commands.describe(
        side_a="Comma-separated players and/or picks (e.g. 'Bijan Robinson, 2027 1st')",
        side_b="Comma-separated players and/or picks on the other side",
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
            embed.set_footer(
                text=(
                    "Source: KeepTradeCut • Picks without a stated tier are "
                    f"priced as {DEFAULT_PICK_TIER.title()}"
                )
            )

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
