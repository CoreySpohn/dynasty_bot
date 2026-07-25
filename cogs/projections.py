"""Projections cog: weekly predictions, playoff odds, and sacko watch.

All three read one model (`lib/projections.py`) built on one results layer
(`lib/results.py`), so they can't contradict each other.

The only thing persisted is the bot's own predictions. That's not derived
data - a prediction only means anything if it was recorded *before* the
games, so it can never be recomputed after the fact. Everything else here
is calculated on demand from Sleeper.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.trade_values import get_team_dynasty_values
from config import SLEEPER_LEAGUE_ID
from database import db
from lib.projections import (
    DEFAULT_SIMULATIONS,
    MAX_ENUMERABLE_GAMES,
    FinishOdds,
    MatchupPrediction,
    SimulationInput,
    build_strengths,
    predict_week,
    simulate_finishes,
    split_schedule,
    week_schedule,
)
from lib.results import get_season_results, get_week_results, played

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.projections")

# Check hourly for weeks needing predictions made or resolved. Cheap, and
# the DB constraints make repeated ticks harmless.
UPKEEP_INTERVAL_HOURS = 1


class Projections(commands.Cog):
    """Weekly predictions, playoff odds, and last-place watch."""

    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID

    async def cog_load(self) -> None:
        self.upkeep_task.start()
        logger.info("Projections cog loaded")

    def cog_unload(self) -> None:
        self.upkeep_task.cancel()

    # =====================================================================
    # Shared plumbing
    # =====================================================================

    async def _context(self) -> dict[str, Any]:
        """League, owner names, players, rosters, and season results."""
        league = await self.bot.sleeper.get_league(self.league_id)
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        users_list = await self.bot.sleeper.get_users(self.league_id)
        players = await self.bot.sleeper.get_all_players()

        users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}
        return {
            "league": league,
            "rosters": rosters,
            "players": players,
            "owners": {
                r["roster_id"]: users.get(
                    r.get("owner_id", ""), f"Team {r['roster_id']}"
                )
                for r in rosters
            },
            "season": int(league.get("season") or 0),
            "current_week": league.get("settings", {}).get("leg", 1) or 1,
            "playoff_teams": league.get("settings", {}).get("playoff_teams", 6),
            "playoff_week_start": league.get("settings", {}).get(
                "playoff_week_start", 15
            ),
        }

    @staticmethod
    def _is_live_season(league: dict[str, Any]) -> bool:
        """Sleeper leaves `leg` at the final week after a season ends, so
        `status` is the only reliable in-season signal."""
        return league.get("status") == "in_season"

    async def _strengths_for(self, ctx: dict[str, Any], results):
        """Build team strengths, with dynasty value as the early prior."""
        dynasty_values = {}
        try:
            dynasty_values = await get_team_dynasty_values(ctx["rosters"])
        except Exception as e:
            logger.warning(f"No dynasty values for projections: {e}")
        return build_strengths(
            results,
            [r["roster_id"] for r in ctx["rosters"]],
            dynasty_values,
        )

    # =====================================================================
    # Prediction storage
    # =====================================================================

    async def _store_predictions(
        self, season: int, predictions: list[MatchupPrediction]
    ) -> int:
        """Record predictions, ignoring any matchup already predicted.

        INSERT OR IGNORE rather than upsert on purpose: a prediction must
        not change after the fact, or the accuracy record is worthless.
        """
        written = 0
        for prediction in predictions:
            low = min(prediction.roster_id, prediction.opponent_roster_id)
            high = max(prediction.roster_id, prediction.opponent_roster_id)
            async with db.execute(
                """
                INSERT OR IGNORE INTO predictions (
                    season, week, roster_id, opponent_roster_id,
                    predicted_winner_roster_id, confidence,
                    projected_points, opponent_projected_points
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    season,
                    prediction.week,
                    low,
                    high,
                    prediction.favorite_roster_id,
                    prediction.confidence,
                    prediction.projected_points,
                    prediction.opponent_projected_points,
                ),
            ) as cursor:
                written += cursor.rowcount or 0
        return written

    async def _stored_predictions(
        self, season: int, week: int
    ) -> list[dict[str, Any]]:
        async with db.execute(
            "SELECT * FROM predictions WHERE season = ? AND week = ? "
            "ORDER BY confidence DESC",
            (season, week),
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    async def _resolve_week(self, ctx: dict[str, Any], week: int) -> int:
        """Grade any unresolved predictions for a completed week."""
        pending = [
            row
            for row in await self._stored_predictions(ctx["season"], week)
            if row["correct"] is None
        ]
        if not pending:
            return 0

        results = played(
            await get_week_results(
                self.bot.sleeper,
                self.league_id,
                week,
                players=ctx["players"],
                league=ctx["league"],
            )
        )
        if not results:
            return 0

        winners = {}
        for result in results:
            if result.result == "W":
                key = (
                    min(result.roster_id, result.opponent_roster_id),
                    max(result.roster_id, result.opponent_roster_id),
                )
                winners[key] = result.roster_id

        resolved = 0
        for row in pending:
            key = (row["roster_id"], row["opponent_roster_id"])
            if key not in winners:
                continue  # tie, or not actually finished
            actual = winners[key]
            correct = int(actual == row["predicted_winner_roster_id"])
            async with db.execute(
                "UPDATE predictions SET actual_winner_roster_id = ?, "
                "correct = ?, resolved_at = ? WHERE id = ?",
                (actual, correct, datetime.now().isoformat(), row["id"]),
            ):
                pass
            resolved += 1
        return resolved

    # =====================================================================
    # Upkeep loop
    # =====================================================================

    @tasks.loop(hours=UPKEEP_INTERVAL_HOURS)
    async def upkeep_task(self) -> None:
        """Record predictions for the current week; grade finished weeks.

        Predictions have to be written before the games are played, so this
        can't be done lazily when someone runs /predictions.
        """
        try:
            ctx = await self._context()
            if not self._is_live_season(ctx["league"]):
                return

            week = ctx["current_week"]
            if week <= ctx["playoff_week_start"] - 1:
                results = await get_season_results(
                    self.bot.sleeper,
                    self.league_id,
                    players=ctx["players"],
                    league=ctx["league"],
                    through_week=week,
                )
                strengths = await self._strengths_for(ctx, results)
                schedule = week_schedule(
                    [r for r in results if r.week == week]
                )
                if schedule:
                    written = await self._store_predictions(
                        ctx["season"], predict_week(schedule, strengths, week)
                    )
                    if written:
                        logger.info(
                            f"Recorded {written} prediction(s) for week {week}"
                        )

            for past_week in range(1, week):
                resolved = await self._resolve_week(ctx, past_week)
                if resolved:
                    logger.info(
                        f"Graded {resolved} prediction(s) for week {past_week}"
                    )

        except Exception as e:
            logger.error(f"Prediction upkeep failed: {e}", exc_info=True)

    @upkeep_task.before_loop
    async def before_upkeep(self) -> None:
        await self.bot.wait_until_ready()

    # =====================================================================
    # /predictions
    # =====================================================================

    @app_commands.command(
        name="predictions",
        description="The bot's matchup predictions for a week",
    )
    @app_commands.describe(week="Week to show (defaults to the current week)")
    async def predictions(
        self, interaction: discord.Interaction, week: Optional[int] = None
    ):
        await interaction.response.defer()
        try:
            ctx = await self._context()
            target = week or ctx["current_week"]

            stored = await self._stored_predictions(ctx["season"], target)
            if stored:
                embed = self._stored_predictions_embed(ctx, target, stored)
                await interaction.followup.send(embed=embed)
                return

            # Nothing recorded yet (a future week, or before the loop has
            # run). Project it live, and say so - an unrecorded projection
            # doesn't count toward the accuracy record.
            results = await get_season_results(
                self.bot.sleeper,
                self.league_id,
                players=ctx["players"],
                league=ctx["league"],
                through_week=max(target, ctx["current_week"]),
            )
            schedule = week_schedule([r for r in results if r.week == target])
            if not schedule:
                await interaction.followup.send(
                    f"📭 No schedule found for week {target}."
                )
                return

            strengths = await self._strengths_for(ctx, results)
            predictions = predict_week(schedule, strengths, target)
            await interaction.followup.send(
                embed=self._live_predictions_embed(ctx, target, predictions)
            )

        except Exception as e:
            logger.error(f"predictions failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building predictions: {e}")

    def _prediction_line(
        self,
        owners: dict[int, str],
        favorite: int,
        underdog: int,
        confidence: float,
        correct: Optional[int] = None,
    ) -> str:
        mark = "" if correct is None else (" ✅" if correct else " ❌")
        cointoss = " *(coin toss)*" if confidence < 0.55 else ""
        return (
            f"**{owners.get(favorite, favorite)}** over "
            f"{owners.get(underdog, underdog)} — {confidence * 100:.0f}%"
            f"{cointoss}{mark}"
        )

    def _stored_predictions_embed(
        self, ctx: dict[str, Any], week: int, stored: list[dict[str, Any]]
    ) -> discord.Embed:
        owners = ctx["owners"]
        lines = []
        for row in stored:
            favorite = row["predicted_winner_roster_id"]
            underdog = (
                row["opponent_roster_id"]
                if favorite == row["roster_id"]
                else row["roster_id"]
            )
            lines.append(
                self._prediction_line(
                    owners, favorite, underdog, row["confidence"], row["correct"]
                )
            )

        graded = [row for row in stored if row["correct"] is not None]
        embed = discord.Embed(
            title=f"🔮 Week {week} Predictions",
            description="\n".join(lines),
            color=discord.Color.teal(),
        )
        if graded:
            hits = sum(row["correct"] for row in graded)
            embed.add_field(
                name="Result",
                value=f"**{hits}/{len(graded)}** correct",
                inline=False,
            )
        embed.set_footer(text="Locked in before kickoff • /predictionrecord for the season")
        return embed

    def _live_predictions_embed(
        self, ctx: dict[str, Any], week: int, predictions: list[MatchupPrediction]
    ) -> discord.Embed:
        owners = ctx["owners"]
        lines = [
            self._prediction_line(
                owners,
                prediction.favorite_roster_id,
                prediction.underdog_roster_id,
                prediction.confidence,
            )
            for prediction in sorted(
                predictions, key=lambda p: p.confidence, reverse=True
            )
        ]
        embed = discord.Embed(
            title=f"🔮 Week {week} Projection",
            description="\n".join(lines) or "*No matchups*",
            color=discord.Color.teal(),
        )
        embed.set_footer(
            text=(
                "Not yet locked in — projected live, so it doesn't count "
                "toward the accuracy record"
            )
        )
        return embed

    # =====================================================================
    # /predictionrecord
    # =====================================================================

    @app_commands.command(
        name="predictionrecord",
        description="How accurate the bot's predictions have been",
    )
    async def predictionrecord(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            ctx = await self._context()

            async with db.execute(
                "SELECT week, confidence, correct FROM predictions "
                "WHERE season = ? AND correct IS NOT NULL ORDER BY week",
                (ctx["season"],),
            ) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                await interaction.followup.send(
                    "📭 No graded predictions yet this season. They're recorded "
                    "before each week and graded once it finishes."
                )
                return

            total = len(rows)
            hits = sum(row[2] for row in rows)

            # Calibration: when the bot says 70%, does it win ~70%? This is
            # the part that says whether the confidence number means
            # anything, as opposed to raw hit rate.
            confident = [row for row in rows if row[1] >= 0.65]
            cointoss = [row for row in rows if row[1] < 0.55]

            by_week: dict[int, list[int]] = {}
            for week, _, correct in rows:
                by_week.setdefault(week, []).append(correct)

            table = "```\n"
            table += f"{'Week':<6} {'Record':>8} {'Hit%':>6}\n"
            table += "-" * 22 + "\n"
            for week in sorted(by_week):
                marks = by_week[week]
                table += (
                    f"{week:<6} {sum(marks)}/{len(marks):<6} "
                    f"{sum(marks) / len(marks) * 100:>5.0f}%\n"
                )
            table += "```"

            embed = discord.Embed(
                title="🎯 Prediction Record",
                description=(
                    f"**{hits}/{total}** correct "
                    f"({hits / total * 100:.1f}%)\n{table}"
                ),
                color=discord.Color.teal(),
            )
            mean_confidence = sum(row[1] for row in rows) / total
            embed.add_field(
                name="Calibration",
                value=(
                    f"Average stated confidence **{mean_confidence * 100:.0f}%** "
                    f"vs **{hits / total * 100:.0f}%** actual.\n"
                    + (
                        f"Confident picks (≥65%): "
                        f"{sum(r[2] for r in confident)}/{len(confident)}\n"
                        if confident
                        else ""
                    )
                    + (
                        f"Coin tosses (<55%): "
                        f"{sum(r[2] for r in cointoss)}/{len(cointoss)}"
                        if cointoss
                        else ""
                    )
                ),
                inline=False,
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"predictionrecord failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building record: {e}")

    # =====================================================================
    # /playoffodds and /sacko
    # =====================================================================

    async def _simulate(self, ctx: dict[str, Any]) -> tuple[dict[int, FinishOdds], int]:
        """Run the season simulation. Returns (odds, games_remaining)."""
        regular_season_end = ctx["playoff_week_start"] - 1
        results = await get_season_results(
            self.bot.sleeper,
            self.league_id,
            players=ctx["players"],
            league=ctx["league"],
            through_week=regular_season_end,
        )
        completed, remaining = split_schedule(results, regular_season_end)
        strengths = await self._strengths_for(ctx, completed)

        inputs = SimulationInput(
            strengths=strengths,
            completed=completed,
            remaining=remaining,
            playoff_teams=ctx["playoff_teams"],
        )
        return simulate_finishes(inputs), inputs.games_remaining

    @app_commands.command(
        name="playoffodds",
        description="Simulated playoff odds for every team",
    )
    async def playoffodds(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            ctx = await self._context()
            odds, games_remaining = await self._simulate(ctx)
            if not odds:
                await interaction.followup.send("📭 Not enough data to simulate.")
                return

            owners = ctx["owners"]
            ranked = sorted(
                odds.values(),
                key=lambda o: (o.playoff_odds, o.expected_wins),
                reverse=True,
            )

            table = "```\n"
            table += f"{'Team':<16} {'Odds':>7} {'ExpW':>6} {'Seed':>5}\n"
            table += "-" * 37 + "\n"
            for entry in ranked:
                name = owners.get(entry.roster_id, f"Team {entry.roster_id}")[:15]
                flag = (
                    " z"
                    if entry.clinched
                    else (" x" if entry.eliminated else "")
                )
                table += (
                    f"{name:<16} {entry.playoff_odds * 100:>6.1f}% "
                    f"{entry.expected_wins:>6.1f} {entry.mean_seed:>5.1f}{flag}\n"
                )
            table += "```"

            notes = [f"{ctx['playoff_teams']} playoff spots"]
            if games_remaining:
                notes.append(f"{games_remaining} games left")
            else:
                notes.append("regular season complete")

            embed = discord.Embed(
                title="📈 Playoff Odds",
                description=(
                    f"{DEFAULT_SIMULATIONS:,} simulated seasons • "
                    f"{' • '.join(notes)}\n{table}"
                ),
                color=discord.Color.blue(),
            )
            if any(o.clinched or o.eliminated for o in odds.values()):
                embed.add_field(
                    name="Legend",
                    value="`z` clinched · `x` eliminated",
                    inline=False,
                )
            elif games_remaining > MAX_ENUMERABLE_GAMES:
                embed.add_field(
                    name="Note",
                    value=(
                        f"With {games_remaining} games left there are too many "
                        "outcomes to check every one, so nothing is labelled "
                        "clinched or eliminated — 0% and 100% here mean "
                        "*no simulation found it*, not *impossible*."
                    ),
                    inline=False,
                )
            embed.add_field(
                name="Seeding",
                value="Wins, then total points — Sleeper's default tiebreak.",
                inline=False,
            )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"playoffodds failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error simulating playoffs: {e}")

    @app_commands.command(
        name="sacko",
        description="Sacko watch — who's headed for the toilet bowl",
    )
    async def sacko(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            ctx = await self._context()
            odds, games_remaining = await self._simulate(ctx)
            if not odds:
                await interaction.followup.send("📭 Not enough data to simulate.")
                return

            owners = ctx["owners"]
            ranked = sorted(
                odds.values(), key=lambda o: o.last_place_odds, reverse=True
            )
            contenders = [entry for entry in ranked if entry.last_place_odds > 0.005]

            if not contenders:
                await interaction.followup.send(
                    "🤷 Nobody's close enough to last place to be worth mocking yet."
                )
                return

            leader = contenders[0]
            leader_name = owners.get(leader.roster_id, f"Team {leader.roster_id}")

            if leader.sacko_clinched:
                headline = (
                    f"🚽 **It's official: {leader_name} is the Sacko.** "
                    "Mathematically eliminated from not being last."
                )
            elif leader.last_place_odds >= 0.6:
                headline = (
                    f"🚽 **{leader_name}** is {leader.last_place_odds * 100:.0f}% "
                    "of the way to the toilet bowl. Start practicing the speech."
                )
            else:
                headline = (
                    f"🚽 **{leader_name}** leads the race to the bottom at "
                    f"{leader.last_place_odds * 100:.0f}%, but it's still a "
                    "genuine contest. Inspiring stuff."
                )

            table = "```\n"
            table += f"{'Team':<16} {'Sacko%':>8} {'ExpW':>6}\n"
            table += "-" * 32 + "\n"
            for entry in contenders:
                name = owners.get(entry.roster_id, f"Team {entry.roster_id}")[:15]
                table += (
                    f"{name:<16} {entry.last_place_odds * 100:>7.1f}% "
                    f"{entry.expected_wins:>6.1f}\n"
                )
            table += "```"

            embed = discord.Embed(
                title="🚽 Sacko Watch",
                description=f"{headline}\n{table}",
                color=discord.Color.dark_gold(),
            )
            embed.set_footer(
                text=(
                    f"{DEFAULT_SIMULATIONS:,} simulated seasons • "
                    + (
                        f"{games_remaining} games left"
                        if games_remaining
                        else "regular season complete"
                    )
                )
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"sacko failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error running sacko watch: {e}")


async def setup(bot: "DynastyBot") -> None:
    """Load the Projections cog."""
    await bot.add_cog(Projections(bot))
