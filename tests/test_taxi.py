"""Tests for the taxi raiding cog utilities."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import cogs.taxi as taxi_module
from cogs.taxi import ordinal, calculate_raid_cost, TaxiRaiding
from database import Database


class TestOrdinal:
    """Test suite for ordinal number formatting."""

    @pytest.mark.parametrize(
        "number,expected",
        [
            (1, "1st"),
            (2, "2nd"),
            (3, "3rd"),
            (4, "4th"),
            (5, "5th"),
            (10, "10th"),
            (11, "11th"),
            (12, "12th"),
            (13, "13th"),
            (21, "21st"),
            (22, "22nd"),
            (23, "23rd"),
            (100, "100th"),
            (101, "101st"),
            (111, "111th"),
            (112, "112th"),
        ],
    )
    def test_ordinal_numbers(self, number, expected):
        """Test ordinal formatting for various numbers."""
        assert ordinal(number) == expected


class TestCalculateRaidCost:
    """Test suite for raid cost calculation."""

    def test_round_1_cost(self):
        """Round 1 costs *two* 1sts, not one.

        The rules spell this case out because the general formula (an Nth plus
        an (N-1)th) has no round above 1 to step up to: "A taxi squad player
        drafted in the 1st round costs two 1st round picks in the next draft."
        """
        assert calculate_raid_cost(1) == "Two 1st Round Picks"

    def test_round_2_cost(self):
        """Round 2 picks cost a 1st and 2nd."""
        assert calculate_raid_cost(2) == "1st & 2nd Round Picks"

    def test_round_3_cost(self):
        """Round 3 picks cost a 2nd and 3rd."""
        assert calculate_raid_cost(3) == "2nd & 3rd Round Picks"

    def test_round_4_cost(self):
        """Round 4 picks cost a 3rd and 4th."""
        assert calculate_raid_cost(4) == "3rd & 4th Round Picks"

    def test_round_5_cost(self):
        """Round 5 picks cost a 4th and 5th."""
        assert calculate_raid_cost(5) == "4th & 5th Round Picks"

    def test_udfa_cost(self):
        """UDFA players cost a 4th round pick."""
        assert calculate_raid_cost("UDFA") == "4th Round Pick"

    def test_non_integer_returns_udfa_cost(self):
        """Non-integer values should be treated as UDFA."""
        assert calculate_raid_cost("unknown") == "4th Round Pick"


class TestCheckPendingRaids:
    """Test suite for the raid reminder loop's core check logic."""

    @pytest_asyncio.fixture
    async def test_db(self):
        """An in-memory database, patched in as the module-level `db` singleton."""
        database = Database(":memory:")
        await database.connect()
        with patch.object(taxi_module, "db", database):
            yield database
        await database.close()

    async def insert_raid(self, test_db, **overrides):
        raid = {
            "raider_user_id": "raider_discord_id",
            "raider_team_name": "Raider Team",
            "victim_user_id": "victim_sleeper_id",
            "victim_team_name": "Victim Team",
            "player_id": "p001",
            "player_name": "Test Player",
            "draft_round": "3",
            "cost_text": "2nd & 3rd Round Picks",
            "raid_date": "2026-07-22",
            "week": 1,
            "season": 2026,
            "status": "pending",
        }
        raid.update(overrides)
        async with test_db.execute(
            """
            INSERT INTO raids (
                raider_user_id, raider_team_name, victim_user_id, victim_team_name,
                player_id, player_name, draft_round, cost_text, raid_date,
                week, season, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(raid.values()),
        ):
            pass

    @pytest.fixture
    def taxi_cog(self, mock_bot):
        channel = MagicMock()
        channel.send = AsyncMock()
        mock_bot.get_channel = MagicMock(return_value=channel)
        return TaxiRaiding(mock_bot)

    async def test_returns_zero_with_no_pending_raids(self, taxi_cog, test_db):
        result = await taxi_cog._check_pending_raids()

        assert result == 0

    async def test_sends_reminder_for_raid_still_on_taxi(self, taxi_cog, test_db):
        await self.insert_raid(test_db)
        taxi_cog._find_taxi_player_owner = AsyncMock(return_value={"roster_id": 1})

        fake_member = SimpleNamespace(discord_id="victim_discord_id")
        with patch.object(
            taxi_module,
            "get_member_registry",
            return_value=MagicMock(find_by_sleeper_id=MagicMock(return_value=fake_member)),
        ):
            result = await taxi_cog._check_pending_raids()

        assert result == 1
        channel = taxi_cog.bot.get_channel.return_value
        channel.send.assert_awaited_once()
        message = channel.send.await_args.args[0]
        assert "Test Player" in message
        assert "<@victim_discord_id>" in message
        assert "<@raider_discord_id>" in message

    async def test_marks_raid_completed_when_player_left_taxi(self, taxi_cog, test_db):
        await self.insert_raid(test_db)
        taxi_cog._find_taxi_player_owner = AsyncMock(return_value=None)

        result = await taxi_cog._check_pending_raids()

        assert result == 0
        taxi_cog.bot.get_channel.return_value.send.assert_not_awaited()

        async with test_db.connection.execute(
            "SELECT status FROM raids WHERE player_id = ?", ("p001",)
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "completed"

    async def test_skips_reminder_when_alert_channel_not_configured(self, taxi_cog, test_db):
        await self.insert_raid(test_db)
        taxi_cog._find_taxi_player_owner = AsyncMock(return_value={"roster_id": 1})

        with patch.object(taxi_module, "ALERT_CHANNEL_ID", None):
            result = await taxi_cog._check_pending_raids()

        assert result == 0
        taxi_cog.bot.get_channel.assert_not_called()


class TestEligibilityAudit:
    """The daily audit that makes the taxi rules self-enforcing.

    Sleeper polices *when* a taxi move happens and *how long* a player may
    stay, but it cannot tell whether a stashed player was one of that owner's
    own rookie-draft picks. Until this loop existed the rule was only as real
    as the commissioner's memory to run /taxiaudit.
    """

    @pytest.fixture
    def audit_cog(self, mock_bot):
        channel = MagicMock()
        channel.send = AsyncMock()
        mock_bot.get_channel = MagicMock(return_value=channel)
        cog = TaxiRaiding(mock_bot)
        # Activations are on record, so the embed skips its "incomplete data"
        # caveat; that path is the command's, not the loop's.
        cog._ledger_size = AsyncMock(return_value=5)
        return cog

    @staticmethod
    def context(window_open=True):
        return {
            "season": 2026,
            "league_season": 2026,
            "window_open": window_open,
            "records": [],
            "rosters": [],
            "taxi_slots": 5,
            "owners": {1: "Owner One", 2: "Owner Two"},
            "players": {
                "p1": {"full_name": "Illegal Stash"},
                "p2": {"full_name": "Second Offender"},
            },
            "deadline": date(2026, 9, 8),
        }

    @staticmethod
    def violation(player_id="p1", roster_id=1):
        return SimpleNamespace(
            roster_id=roster_id,
            player_id=player_id,
            reason_text="not one of your own rookie-draft picks",
            draft_season=2024,
            draft_round=2,
            seasons_used=2,
        )

    def arrange(self, cog, violations, over=None, window_open=True):
        cog._taxi_context = AsyncMock(return_value=self.context(window_open))
        return patch.multiple(
            taxi_module,
            audit=MagicMock(return_value=violations),
            over_slot_limit=MagicMock(return_value=over or []),
        )

    @property
    def sent(self):
        return lambda cog: cog.bot.get_channel.return_value.send

    async def test_stays_quiet_after_the_deadline(self, audit_cog):
        """Rosters are frozen, so nobody can act on the alert."""
        with self.arrange(audit_cog, [self.violation()], window_open=False):
            found = await audit_cog._run_eligibility_audit()

        assert found == 0
        self.sent(audit_cog).assert_not_awaited()

    async def test_stays_quiet_when_every_squad_is_legal(self, audit_cog):
        with self.arrange(audit_cog, []):
            found = await audit_cog._run_eligibility_audit()

        assert found == 0
        self.sent(audit_cog).assert_not_awaited()

    async def test_alerts_on_a_violation(self, audit_cog):
        with self.arrange(audit_cog, [self.violation()]):
            found = await audit_cog._run_eligibility_audit()

        assert found == 1
        self.sent(audit_cog).assert_awaited_once()

    async def test_names_the_deadline_in_the_alert(self, audit_cog):
        with self.arrange(audit_cog, [self.violation()]):
            await audit_cog._run_eligibility_audit()

        embed = self.sent(audit_cog).await_args.kwargs["embed"]
        assert "September 8" in embed.description

    async def test_does_not_repeat_an_unchanged_finding(self, audit_cog):
        """A daily re-post of the same embed trains people to ignore it."""
        with self.arrange(audit_cog, [self.violation()]):
            await audit_cog._run_eligibility_audit()
            found = await audit_cog._run_eligibility_audit()

        assert found == 1
        self.sent(audit_cog).assert_awaited_once()

    async def test_speaks_up_again_when_a_new_violation_appears(self, audit_cog):
        with self.arrange(audit_cog, [self.violation()]):
            await audit_cog._run_eligibility_audit()
        with self.arrange(
            audit_cog, [self.violation(), self.violation("p2", roster_id=2)]
        ):
            await audit_cog._run_eligibility_audit()

        assert self.sent(audit_cog).await_count == 2

    async def test_a_fixed_then_repeated_violation_alerts_twice(self, audit_cog):
        """Going clean resets the memory, so a relapse is announced."""
        with self.arrange(audit_cog, [self.violation()]):
            await audit_cog._run_eligibility_audit()
        with self.arrange(audit_cog, []):
            await audit_cog._run_eligibility_audit()
        with self.arrange(audit_cog, [self.violation()]):
            await audit_cog._run_eligibility_audit()

        assert self.sent(audit_cog).await_count == 2

    async def test_alerts_on_an_over_full_taxi_squad(self, audit_cog):
        """Too many slots used is a violation Sleeper does allow to happen."""
        with self.arrange(audit_cog, [], over=[(1, 6)]):
            found = await audit_cog._run_eligibility_audit()

        assert found == 0
        self.sent(audit_cog).assert_awaited_once()

    async def test_skips_when_alert_channel_not_configured(self, audit_cog):
        with self.arrange(audit_cog, [self.violation()]):
            with patch.object(taxi_module, "ALERT_CHANNEL_ID", None):
                found = await audit_cog._run_eligibility_audit()

        assert found == 1
        audit_cog.bot.get_channel.assert_not_called()
