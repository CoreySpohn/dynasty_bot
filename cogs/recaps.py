"""Weekly recap cog: awards, shame wall, and the luck index.

All three read `lib/results.py` rather than touching Sleeper payloads
directly, so their numbers agree with `/rankings` by construction.

Nothing here stores results - Sleeper keeps those forever. The one thing
persisted is *whether a recap was already posted* (`posted_recaps`), which
exists nowhere else and is the difference between posting awards once and
posting them again on every restart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks

from config import ALERT_CHANNEL_ID, SLEEPER_LEAGUE_ID
from database import db
from lib.results import (
    WeekResult,
    build_records,
    compute_luck,
    get_season_results,
    get_week_results,
    played,
)

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.recaps")

LEAGUE_STATE_PATH = Path(__file__).parent.parent / "config" / "league_state.yaml"

# How often to check whether the last completed week still needs its recap.
# Generous because posted_recaps makes a missed tick harmless - the next one
# picks it up - while a tight loop would just hammer Sleeper.
AUTOPOST_INTERVAL_HOURS = 6

# A starter scoring exactly this is the observable signal for "shouldn't
# have been in your lineup" - on a bye, inactive, or benched by their NFL
# team. Sleeper's player payload doesn't carry bye weeks reliably, so this
# is stated as zero-point starters rather than claimed as a BYE.
ZERO = 0.0


class Recaps(commands.Cog):
    """Weekly awards, shame wall, and luck index."""

    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID

    async def cog_load(self) -> None:
        # Started here rather than in __init__ so constructing the cog
        # doesn't require a running event loop.
        self.autopost_task.start()
        logger.info("Recaps cog loaded")

    def cog_unload(self) -> None:
        self.autopost_task.cancel()

    # =====================================================================
    # Auto-post
    # =====================================================================

    @property
    def recap_channel_id(self) -> Optional[int]:
        """Where recaps go: the announcements channel, else the alert channel."""
        try:
            with open(LEAGUE_STATE_PATH) as f:
                state = yaml.safe_load(f) or {}
        except FileNotFoundError:
            state = {}
        return (
            state.get("announcements", {}).get("channel_id") or ALERT_CHANNEL_ID
        )

    @tasks.loop(hours=AUTOPOST_INTERVAL_HOURS)
    async def autopost_task(self) -> None:
        """Post the last completed week's awards and shame wall, once.

        `posted_recaps` is what makes this safe to run on a loop and across
        restarts - without it, every tick would re-post the same week.
        """
        try:
            league, owners, players = await self._league_context()
            if not self._should_autopost(league):
                return

            season = int(league.get("season") or 0)
            week = self._resolve_week(league, None)

            channel_id = self.recap_channel_id
            if not channel_id:
                logger.debug("No recap channel configured; skipping auto-post")
                return
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                logger.warning(f"Recap channel {channel_id} not found")
                return

            week_results = played(
                await get_week_results(
                    self.bot.sleeper, self.league_id, week,
                    players=players, league=league,
                )
            )
            if not week_results:
                return

            if not await self._already_posted(season, week, "awards"):
                season_results = await get_season_results(
                    self.bot.sleeper, self.league_id,
                    players=players, league=league, through_week=week,
                )
                embed = discord.Embed(
                    title=f"🏆 Week {week} Awards",
                    description=f"{season} season",
                    color=discord.Color.gold(),
                )
                for name, value in self._award_fields(
                    week_results, owners, self._season_averages(season_results)
                ):
                    embed.add_field(name=name, value=value, inline=False)
                message = await channel.send(embed=embed)
                await self._mark_posted(season, week, "awards", str(message.id))
                logger.info(f"Auto-posted week {week} awards")

            if not await self._already_posted(season, week, "shamewall"):
                embed = self._build_shamewall_embed(week, week_results, owners, players)
                if embed.fields:
                    message = await channel.send(embed=embed)
                    await self._mark_posted(
                        season, week, "shamewall", str(message.id)
                    )
                    logger.info(f"Auto-posted week {week} shame wall")
                else:
                    # Nothing shameful happened; record it so we stop looking.
                    await self._mark_posted(season, week, "shamewall")

        except Exception as e:
            logger.error(f"Recap auto-post failed: {e}", exc_info=True)

    @autopost_task.before_loop
    async def before_autopost(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _should_autopost(league: dict[str, Any]) -> bool:
        """Whether there's a live season worth recapping.

        `settings.leg` is NOT sufficient on its own: Sleeper leaves it at the
        final week once a season completes, so a leg-only check stays true
        all offseason and the loop tries to re-post last season's week 17
        awards as though they were new. `status` is the reliable signal -
        it goes "pre_draft" / "drafting" / "in_season" / "complete".
        """
        if league.get("status") != "in_season":
            return False
        return (league.get("settings", {}).get("leg", 0) or 0) > 1

    # =====================================================================
    # Shared plumbing
    # =====================================================================

    async def _league_context(self) -> tuple[dict[str, Any], dict[int, str], dict]:
        """League dict, roster_id -> owner display name, and the player map."""
        league = await self.bot.sleeper.get_league(self.league_id)
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        users_list = await self.bot.sleeper.get_users(self.league_id)
        players = await self.bot.sleeper.get_all_players()

        users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}
        owner_by_roster = {
            r["roster_id"]: users.get(r.get("owner_id", ""), f"Team {r['roster_id']}")
            for r in rosters
        }
        return league, owner_by_roster, players

    def _resolve_week(self, league: dict[str, Any], week: Optional[int]) -> int:
        """Requested week, else the most recently *completed* week.

        Defaults to current_week - 1 because the in-progress week has
        partial scores, and awarding "highest scorer" mid-Sunday is noise.
        """
        current = league.get("settings", {}).get("leg", 1) or 1
        return week if week is not None else max(1, current - 1)

    def _player_name(self, players: dict, player_id: str) -> str:
        data = players.get(player_id) or {}
        return data.get("full_name") or f"Player {player_id}"

    async def _already_posted(self, season: int, week: int, recap_type: str) -> bool:
        async with db.execute(
            "SELECT 1 FROM posted_recaps WHERE season = ? AND week = ? "
            "AND recap_type = ?",
            (season, week, recap_type),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _mark_posted(
        self,
        season: int,
        week: int,
        recap_type: str,
        message_id: Optional[str] = None,
    ) -> None:
        async with db.execute(
            "INSERT OR IGNORE INTO posted_recaps "
            "(season, week, recap_type, message_id) VALUES (?, ?, ?, ?)",
            (season, week, recap_type, message_id),
        ):
            pass

    # =====================================================================
    # /awards
    # =====================================================================

    @app_commands.command(
        name="awards",
        description="Weekly awards: highest scorer, biggest upset, best bench, worst beat",
    )
    @app_commands.describe(week="Week to award (defaults to the last completed week)")
    async def awards(
        self, interaction: discord.Interaction, week: Optional[int] = None
    ):
        await interaction.response.defer()
        try:
            league, owners, players = await self._league_context()
            target_week = self._resolve_week(league, week)
            season = int(league.get("season") or 0)

            week_results = played(
                await get_week_results(
                    self.bot.sleeper,
                    self.league_id,
                    target_week,
                    players=players,
                    league=league,
                )
            )
            if not week_results:
                await interaction.followup.send(
                    f"📭 No completed results for week {target_week} yet."
                )
                return

            # Season-to-date averages give "biggest upset" an expectation to
            # measure against, rather than guessing at one.
            season_results = await get_season_results(
                self.bot.sleeper,
                self.league_id,
                players=players,
                league=league,
                through_week=target_week,
            )
            averages = self._season_averages(season_results)

            embed = discord.Embed(
                title=f"🏆 Week {target_week} Awards",
                description=f"{season} season",
                color=discord.Color.gold(),
            )

            for name, value in self._award_fields(week_results, owners, averages):
                embed.add_field(name=name, value=value, inline=False)

            await interaction.followup.send(embed=embed)
            await self._mark_posted(season, target_week, "awards")

        except Exception as e:
            logger.error(f"awards failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building awards: {e}")

    @staticmethod
    def _season_averages(season_results: list[WeekResult]) -> dict[int, float]:
        """Season-to-date average points per roster."""
        records = build_records(played(season_results))
        return {
            roster_id: (record.points_for / record.games if record.games else 0.0)
            for roster_id, record in records.items()
        }

    def _award_fields(
        self,
        week_results: list[WeekResult],
        owners: dict[int, str],
        averages: dict[int, float],
    ) -> list[tuple[str, str]]:
        """Build the four award lines for a week."""
        def owner(result: WeekResult) -> str:
            return owners.get(result.roster_id, f"Team {result.roster_id}")

        fields: list[tuple[str, str]] = []

        top = max(week_results, key=lambda r: r.points)
        fields.append(
            ("👑 Highest Scorer", f"**{owner(top)}** — {top.points:,.1f} pts")
        )

        # Biggest upset: the winner who beat a team averaging the most more
        # than them per week. Uses season-to-date averages as the
        # expectation, since the bot doesn't make predictions.
        upsets = [
            (r, averages.get(r.opponent_roster_id, 0.0) - averages.get(r.roster_id, 0.0))
            for r in week_results
            if r.result == "W" and r.opponent_roster_id is not None
        ]
        upsets = [(r, gap) for r, gap in upsets if gap > 0]
        if upsets:
            winner, gap = max(upsets, key=lambda pair: pair[1])
            fields.append(
                (
                    "😱 Biggest Upset",
                    f"**{owner(winner)}** beat "
                    f"**{owners.get(winner.opponent_roster_id, 'their opponent')}**, "
                    f"who averages {gap:,.1f} more per week "
                    f"({winner.points:,.1f}–{winner.opponent_points:,.1f})",
                )
            )
        else:
            fields.append(
                ("😱 Biggest Upset", "*Chalk week — every favorite held serve*")
            )

        bench = max(week_results, key=lambda r: r.points_left_on_bench)
        if bench.points_left_on_bench > 0:
            fields.append(
                (
                    "📈 Best Bench",
                    f"**{owner(bench)}** left "
                    f"**{bench.points_left_on_bench:,.1f}** on the bench "
                    f"({bench.points:,.1f} of a possible {bench.optimal_points:,.1f})",
                )
            )

        losers = [r for r in week_results if r.result == "L"]
        if losers:
            worst = max(losers, key=lambda r: r.points)
            fields.append(
                (
                    "💔 Worst Beat",
                    f"**{owner(worst)}** scored {worst.points:,.1f} and *still lost* "
                    f"to {owners.get(worst.opponent_roster_id, 'their opponent')} "
                    f"({worst.opponent_points:,.1f})",
                )
            )

        return fields

    # =====================================================================
    # /shamewall
    # =====================================================================

    @app_commands.command(
        name="shamewall",
        description="The week's worst lineup decisions",
    )
    @app_commands.describe(week="Week to shame (defaults to the last completed week)")
    async def shamewall(
        self, interaction: discord.Interaction, week: Optional[int] = None
    ):
        await interaction.response.defer()
        try:
            league, owners, players = await self._league_context()
            target_week = self._resolve_week(league, week)
            season = int(league.get("season") or 0)

            week_results = played(
                await get_week_results(
                    self.bot.sleeper,
                    self.league_id,
                    target_week,
                    players=players,
                    league=league,
                )
            )
            if not week_results:
                await interaction.followup.send(
                    f"📭 No completed results for week {target_week} yet."
                )
                return

            embed = self._build_shamewall_embed(
                target_week, week_results, owners, players
            )
            if not embed.fields:
                embed.description = (
                    "Nobody embarrassed themselves this week. Suspicious."
                )

            await interaction.followup.send(embed=embed)
            await self._mark_posted(season, target_week, "shamewall")

        except Exception as e:
            logger.error(f"shamewall failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building shame wall: {e}")

    def _build_shamewall_embed(
        self,
        week: int,
        week_results: list[WeekResult],
        owners: dict[int, str],
        players: dict,
    ) -> discord.Embed:
        """Render the shame wall. Shared by the command and the auto-post."""
        embed = discord.Embed(
            title=f"🤦 Week {week} Shame Wall",
            color=discord.Color.dark_red(),
        )

        def owner(result: WeekResult) -> str:
            return owners.get(result.roster_id, f"Team {result.roster_id}")

        # Losses the optimal lineup would have won - the precise definition
        # of a self-inflicted loss.
        self_inflicted = sorted(
            (r for r in week_results if r.would_have_won),
            key=lambda r: r.points_left_on_bench,
            reverse=True,
        )
        if self_inflicted:
            embed.add_field(
                name="⚰️ Lost a game they'd already won",
                value="\n".join(
                    f"**{owner(r)}** lost {r.points:,.1f}–{r.opponent_points:,.1f}, "
                    f"but their best lineup scored {r.optimal_points:,.1f}"
                    for r in self_inflicted
                ),
                inline=False,
            )

        leavers = sorted(
            (r for r in week_results if r.points_left_on_bench > 0),
            key=lambda r: r.points_left_on_bench,
            reverse=True,
        )[:3]
        if leavers:
            embed.add_field(
                name="🪑 Most points left on the bench",
                value="\n".join(
                    f"**{owner(r)}** — {r.points_left_on_bench:,.1f} "
                    f"({r.points:,.1f} of {r.optimal_points:,.1f})"
                    for r in leavers
                ),
                inline=False,
            )

        zero_lines = [
            f"**{owner(result)}** started "
            f"{', '.join(self._player_name(players, pid) for pid in zeros)} for zero"
            for result, zeros in (
                (
                    result,
                    [pid for pid, pts in result.starter_points.items() if pts <= ZERO],
                )
                for result in week_results
            )
            if zeros
        ]
        if zero_lines:
            embed.add_field(
                name="🕳️ Started someone who scored nothing",
                value="\n".join(zero_lines),
                inline=False,
            )

        return embed

    # =====================================================================
    # /luckindex
    # =====================================================================

    @app_commands.command(
        name="luckindex",
        description="Who's been lucky: record vs what all-play scoring deserved",
    )
    async def luckindex(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            league, owners, players = await self._league_context()

            season_results = await get_season_results(
                self.bot.sleeper, self.league_id, players=players, league=league
            )
            luck = compute_luck(season_results)
            if not luck or not any(index.games for index in luck.values()):
                await interaction.followup.send(
                    "📭 No completed games this season yet."
                )
                return

            ranked = sorted(
                luck.values(), key=lambda index: index.luck_score, reverse=True
            )

            table = "```\n"
            table += f"{'Team':<16} {'Rec':>6} {'Exp':>6} {'Luck':>6} {'Eff':>5}\n"
            table += "-" * 43 + "\n"
            for index in ranked:
                name = owners.get(index.roster_id, f"Team {index.roster_id}")[:15]
                table += (
                    f"{name:<16} {index.actual_wins:>6.1f} "
                    f"{index.expected_wins:>6.1f} {index.luck_score:>+6.1f} "
                    f"{index.efficiency * 100:>4.0f}%\n"
                )
            table += "```"

            luckiest, unluckiest = ranked[0], ranked[-1]

            embed = discord.Embed(
                title="🍀 Luck Index",
                description=(
                    "**Exp** is all-play expected wins — what your record would be "
                    "if you played every team every week. **Luck** is actual minus "
                    "expected: the part of your record the schedule handed you.\n"
                    "**Eff** is the share of your optimal lineup you actually "
                    "started — a decision, not luck, so it's kept separate."
                    f"\n{table}"
                ),
                color=discord.Color.green(),
            )
            embed.add_field(
                name="🍀 Luckiest",
                value=(
                    f"**{owners.get(luckiest.roster_id, 'Unknown')}** "
                    f"({luckiest.luck_score:+.1f} wins, "
                    f"{luckiest.close_wins}-{luckiest.close_losses} in close games)"
                ),
                inline=True,
            )
            embed.add_field(
                name="🐍 Unluckiest",
                value=(
                    f"**{owners.get(unluckiest.roster_id, 'Unknown')}** "
                    f"({unluckiest.luck_score:+.1f} wins, "
                    f"{unluckiest.points_against_per_game:,.1f} allowed/game)"
                ),
                inline=True,
            )
            embed.set_footer(text="Derived live from Sleeper matchup data")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"luckindex failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building luck index: {e}")


async def setup(bot: "DynastyBot") -> None:
    """Load the Recaps cog."""
    await bot.add_cog(Recaps(bot))
