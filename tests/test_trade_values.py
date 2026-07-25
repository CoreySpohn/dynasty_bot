"""Tests for the TradeValues cog."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio

import cogs.trade_values as trade_values_module
import lib.roster_history as roster_history_module
from cogs.trade_values import (
    TradeValues,
    get_team_dynasty_value_trends,
    get_team_dynasty_values,
    get_team_dynasty_values_as_of,
    get_team_value_changes,
    grade_trade,
    normalize_name,
    pick_ktc_name,
    resolve_pick_name,
    trade_date,
    trade_sides,
)
from database import Database
from lib.roster_history import store_snapshot


class TestNormalizeName:
    def test_strips_suffix(self):
        assert normalize_name("Kenneth Walker III") == normalize_name("Kenneth Walker")

    def test_strips_punctuation_and_case(self):
        assert normalize_name("Ja'Marr Chase") == normalize_name("JAMARR CHASE")

    def test_hyphenated_names(self):
        assert normalize_name("Amon-Ra St. Brown") == normalize_name("amon ra st brown")


class TestResolvePickName:
    """KTC stores picks as RDP rows named '2027 Early 1st', but nobody types
    the tier, so loose references have to resolve onto those names."""

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("2027 1st", "2027 Mid 1st"),
            ("2027 1st round pick", "2027 Mid 1st"),
            ("27 2nd", "2027 Mid 2nd"),
            ("2028 late 3rd", "2028 Late 3rd"),
            ("2026 EARLY 4th", "2026 Early 4th"),
            ("2027 first rounder", "2027 Mid 1st"),
            ("2027 rd 2", "2027 Mid 2nd"),
        ],
    )
    def test_resolves_natural_phrasings(self, query, expected):
        assert resolve_pick_name(query) == expected

    def test_already_canonical_name_round_trips(self):
        assert resolve_pick_name("2027 Early 1st") == "2027 Early 1st"

    @pytest.mark.parametrize(
        "query",
        [
            "Bijan Robinson",
            "Ja'Marr Chase",
            "Kenneth Walker III",
            "2027",             # season with no round
            "1st",              # round with no season
            "Corey's 2027 1st",  # owner-qualified: can't price without the owner
        ],
    )
    def test_returns_none_for_non_pick_references(self, query):
        assert resolve_pick_name(query) is None


@pytest.fixture
def trade_values_cog(mock_bot):
    return TradeValues(mock_bot)


class TestMatchSleeperId:
    async def test_matches_on_name_and_position(self, trade_values_cog):
        index = {normalize_name("Ja'Marr Chase"): [("101", "WR")]}
        ktc_player = {"name": "Ja'Marr Chase", "position": "WR"}

        assert trade_values_cog._match_sleeper_id(ktc_player, index) == "101"

    async def test_prefers_position_match_among_duplicates(self, trade_values_cog):
        index = {
            normalize_name("Josh Allen"): [("999", "LB"), ("101", "QB")],
        }
        ktc_player = {"name": "Josh Allen", "position": "QB"}

        assert trade_values_cog._match_sleeper_id(ktc_player, index) == "101"

    async def test_returns_none_when_no_candidate(self, trade_values_cog):
        assert trade_values_cog._match_sleeper_id(
            {"name": "Nobody Special", "position": "WR"}, {}
        ) is None


@pytest_asyncio.fixture
async def test_db():
    """An in-memory database, patched in as the module-level `db` singleton.

    Patched into lib.roster_history too: cogs.trade_values imports
    get_composition_as_of from there, and that closes over roster_history's
    own `db` global, so both have to point at the same database.
    """
    database = Database(":memory:")
    await database.connect()
    with patch.object(trade_values_module, "db", database), patch.object(
        roster_history_module, "db", database
    ):
        yield database
    await database.close()


class TestGetLatestValueAndTrend:
    async def test_exact_and_trend_lookup(self, trade_values_cog, test_db):
        async with test_db.execute(
            """
            INSERT INTO ktc_values (
                ktc_id, sleeper_id, player_name, position, team, is_rookie,
                value_1qb, rank_1qb, positional_rank_1qb,
                value_sf, rank_sf, positional_rank_sf, recorded_date
            ) VALUES (1004, '13524', "Ja'Marr Chase", 'WR', 'CIN', 0,
                      9999, 1, 1, 9999, 1, 1, '2026-07-15')
            """
        ):
            pass
        async with test_db.execute(
            """
            INSERT INTO ktc_values (
                ktc_id, sleeper_id, player_name, position, team, is_rookie,
                value_1qb, rank_1qb, positional_rank_1qb,
                value_sf, rank_sf, positional_rank_sf, recorded_date
            ) VALUES (1004, '13524', "Ja'Marr Chase", 'WR', 'CIN', 0,
                      9950, 1, 1, 9960, 1, 1, '2026-07-22')
            """
        ):
            pass

        row = await trade_values_cog._get_latest_value("chase")
        assert row is not None
        assert row["player_name"] == "Ja'Marr Chase"
        assert row["recorded_date"] == "2026-07-22"

        trend = await trade_values_cog._get_trend(1004, row["value_sf"], row["recorded_date"])
        assert trend == 9960 - 9999

    async def test_returns_none_for_unknown_player(self, trade_values_cog, test_db):
        row = await trade_values_cog._get_latest_value("totally unknown player xyz")
        assert row is None


async def _insert_snapshot(
    test_db, ktc_id, sleeper_id, name, value_sf, recorded_date
):
    async with test_db.execute(
        """
        INSERT INTO ktc_values (
            ktc_id, sleeper_id, player_name, position, team, is_rookie,
            value_1qb, rank_1qb, positional_rank_1qb,
            value_sf, rank_sf, positional_rank_sf, recorded_date
        ) VALUES (?, ?, ?, 'WR', 'CIN', 0, ?, 1, 1, ?, 1, 1, ?)
        """,
        (ktc_id, sleeper_id, name, value_sf, value_sf, recorded_date),
    ):
        pass


class TestTeamDynastyValues:
    async def test_sums_current_roster_value_ignoring_unmatched_players(self, test_db):
        await _insert_snapshot(test_db, 1, "p1", "Player One", 5000, "2026-07-22")
        await _insert_snapshot(test_db, 2, "p2", "Player Two", 3000, "2026-07-22")

        rosters = [{"roster_id": 1, "players": ["p1", "p2", "p_unmatched"]}]
        values = await get_team_dynasty_values(rosters)

        assert values[1] == 8000

    async def test_missing_roster_gets_zero(self, test_db):
        rosters = [{"roster_id": 99, "players": []}]
        values = await get_team_dynasty_values(rosters)

        assert values[99] == 0


class TestTeamDynastyValueTrends:
    async def test_isolates_market_movement_from_roster_churn(self, test_db):
        # Same player, currently owned both times: value rose from 4000 to 5000.
        await _insert_snapshot(test_db, 1, "p1", "Player One", 4000, "2026-07-15")
        await _insert_snapshot(test_db, 1, "p1", "Player One", 5000, "2026-07-22")

        rosters = [{"roster_id": 1, "players": ["p1"]}]
        trends = await get_team_dynasty_value_trends(rosters)

        current, prior = trends[1]
        assert current == 5000
        assert prior == 4000

    async def test_falls_back_to_current_value_when_no_history(self, test_db):
        await _insert_snapshot(test_db, 1, "p1", "Player One", 5000, "2026-07-22")

        rosters = [{"roster_id": 1, "players": ["p1"]}]
        trends = await get_team_dynasty_value_trends(rosters)

        current, prior = trends[1]
        assert current == prior == 5000


class TestTeamValuesWithRosterChurn:
    """The churn-inclusive counterpart to TestTeamDynastyValueTrends.

    get_team_dynasty_value_trends deliberately re-prices only currently
    owned players, so it cannot see a trade. These functions price the
    roster a team actually had, which is the whole point of snapshotting
    composition.
    """

    @staticmethod
    def _days_ago(n: int) -> str:
        return (datetime.now() - timedelta(days=n)).date().isoformat()

    async def test_prices_the_roster_as_it_stood(self, test_db):
        old_date = self._days_ago(10)
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, old_date)
        await _insert_snapshot(test_db, 2, "scrub", "Scrub Player", 1000, old_date)
        await store_snapshot(
            [{"roster_id": 1, "owner_id": "u1", "players": ["star", "scrub"]}],
            recorded_date=old_date,
        )

        values = await get_team_dynasty_values_as_of(self._days_ago(7))

        assert values == {1: 9000}

    async def test_returns_empty_without_a_snapshot_that_old(self, test_db):
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, self._days_ago(1))
        await store_snapshot(
            [{"roster_id": 1, "owner_id": "u1", "players": ["star"]}],
            recorded_date=self._days_ago(1),
        )

        assert await get_team_dynasty_values_as_of(self._days_ago(30)) == {}

    async def test_change_reflects_a_trade_away(self, test_db):
        old_date = self._days_ago(10)
        today = datetime.now().date().isoformat()

        # Values flat across both dates, so any change is pure churn.
        for date in (old_date, today):
            await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, date)
            await _insert_snapshot(test_db, 2, "scrub", "Scrub Player", 1000, date)

        await store_snapshot(
            [{"roster_id": 1, "owner_id": "u1", "players": ["star", "scrub"]}],
            recorded_date=old_date,
        )

        # Today the star is gone.
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["scrub"]}]
        changes = await get_team_value_changes(rosters, lookback_days=7)

        current, prior = changes[1]
        assert prior == 9000
        assert current == 1000
        assert current - prior == -8000

    async def test_prior_is_none_when_history_is_too_shallow(self, test_db):
        today = datetime.now().date().isoformat()
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, today)

        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["star"]}]
        changes = await get_team_value_changes(rosters, lookback_days=7)

        current, prior = changes[1]
        assert current == 8000
        assert prior is None


async def _insert_pick(test_db, ktc_id, name, value_sf, recorded_date):
    """Insert a KTC rookie-draft-pick row (position 'RDP', no sleeper_id)."""
    async with test_db.execute(
        """
        INSERT INTO ktc_values (
            ktc_id, sleeper_id, player_name, position, team, is_rookie,
            value_1qb, rank_1qb, positional_rank_1qb,
            value_sf, rank_sf, positional_rank_sf, recorded_date
        ) VALUES (?, NULL, ?, 'RDP', NULL, 0, ?, 1, 1, ?, 1, 1, ?)
        """,
        (ktc_id, name, value_sf, value_sf, recorded_date),
    ):
        pass


class TestPickLookupThroughGetLatestValue:
    """/tradecalc has to price picks, not just players. The RDP rows were
    already being synced; only the loose-reference lookup was missing."""

    async def test_finds_pick_by_loose_reference(self, trade_values_cog, test_db):
        await _insert_pick(test_db, 9001, "2027 Mid 1st", 5591, "2026-07-24")

        row = await trade_values_cog._get_latest_value("2027 1st")

        assert row is not None
        assert row["player_name"] == "2027 Mid 1st"
        assert row["value_sf"] == 5591

    async def test_finds_pick_by_exact_tier(self, trade_values_cog, test_db):
        await _insert_pick(test_db, 9002, "2027 Early 1st", 7118, "2026-07-24")
        await _insert_pick(test_db, 9003, "2027 Mid 1st", 5591, "2026-07-24")

        row = await trade_values_cog._get_latest_value("2027 early 1st")

        assert row["value_sf"] == 7118

    async def test_player_lookup_still_wins_over_pick_parsing(
        self, trade_values_cog, test_db
    ):
        await _insert_snapshot(test_db, 1, "p1", "Bijan Robinson", 9993, "2026-07-24")
        await _insert_pick(test_db, 9004, "2027 Mid 1st", 5591, "2026-07-24")

        row = await trade_values_cog._get_latest_value("Bijan Robinson")

        assert row["player_name"] == "Bijan Robinson"

    async def test_tradecalc_side_totals_players_and_picks_together(
        self, trade_values_cog, test_db
    ):
        await _insert_snapshot(test_db, 1, "p1", "Bijan Robinson", 9993, "2026-07-24")
        await _insert_pick(test_db, 9005, "2027 Mid 1st", 5591, "2026-07-24")

        total, rows, missing = await trade_values_cog._evaluate_side(
            "Bijan Robinson, 2027 1st"
        )

        assert total == 9993 + 5591
        assert missing == []
        assert len(rows) == 2

    async def test_unpublished_pick_round_reported_missing(
        self, trade_values_cog, test_db
    ):
        # KTC only publishes rounds 1-4, so a 5th has no row to price.
        total, rows, missing = await trade_values_cog._evaluate_side("2027 5th")

        assert total == 0
        assert rows == []
        assert missing == ["2027 5th"]


# =========================================================================
# Trade grading
# =========================================================================

def _trade(*, roster_ids, adds=None, draft_picks=None, status_updated=None,
           status="complete", type_="trade"):
    """Build a Sleeper-shaped trade transaction."""
    return {
        "type": type_,
        "status": status,
        "status_updated": status_updated,
        "roster_ids": list(roster_ids),
        "adds": adds or {},
        "draft_picks": draft_picks or [],
    }


def _epoch_ms(date_str: str) -> int:
    return int(datetime.fromisoformat(date_str).timestamp() * 1000)


class TestTradeSides:
    def test_splits_players_and_picks_by_receiving_roster(self):
        sides = trade_sides(
            _trade(
                roster_ids=[1, 2],
                adds={"star": 1, "scrub": 2},
                draft_picks=[
                    {"season": "2027", "round": 1, "owner_id": 2,
                     "previous_owner_id": 1},
                ],
            )
        )

        assert sides[1]["players"] == ["star"]
        assert sides[2]["players"] == ["scrub"]
        assert sides[1]["picks"] == []
        assert len(sides[2]["picks"]) == 1

    def test_ignores_assets_for_rosters_not_in_the_trade(self):
        sides = trade_sides(_trade(roster_ids=[1, 2], adds={"x": 99}))

        assert sides[1]["players"] == []
        assert sides[2]["players"] == []


class TestTradeDateAndPickNaming:
    def test_converts_epoch_millis_to_iso_date(self):
        assert trade_date(
            _trade(roster_ids=[1], status_updated=_epoch_ms("2026-08-14"))
        ) == "2026-08-14"

    def test_returns_none_without_a_timestamp(self):
        assert trade_date(_trade(roster_ids=[1])) is None

    def test_pick_maps_to_neutral_ktc_tier(self):
        assert pick_ktc_name({"season": "2027", "round": 1}) == "2027 Mid 1st"
        assert pick_ktc_name({"season": 2028, "round": 3}) == "2028 Mid 3rd"

    def test_incomplete_pick_returns_none(self):
        assert pick_ktc_name({"season": "2027"}) is None
        assert pick_ktc_name({"round": 1}) is None


class TestGradeTrade:
    """Grades use the values from the trade date, which is the whole reason
    dated KTC snapshots are worth keeping - KTC can't be backfilled."""

    async def test_prices_both_sides_at_trade_date_values(self, test_db):
        # Star was worth 8000 on the trade date, 9000 today.
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, "2026-08-01")
        await _insert_snapshot(test_db, 2, "scrub", "Scrub Player", 1000, "2026-08-01")
        await _insert_snapshot(test_db, 1, "star", "Star Player", 9000, "2026-08-20")
        await _insert_snapshot(test_db, 2, "scrub", "Scrub Player", 900, "2026-08-20")

        graded = await grade_trade(
            _trade(
                roster_ids=[1, 2],
                adds={"star": 1, "scrub": 2},
                status_updated=_epoch_ms("2026-08-01"),
            )
        )

        assert graded["trade_date"] == "2026-08-01"
        assert graded["sides"][1]["then"] == 8000
        assert graded["sides"][2]["then"] == 1000
        # "Now" re-prices the same assets at the latest snapshot.
        assert graded["sides"][1]["now"] == 9000
        assert graded["sides"][2]["now"] == 900

    async def test_prices_draft_picks_via_rdp_rows(self, test_db):
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, "2026-08-01")
        await _insert_pick(test_db, 9001, "2027 Mid 1st", 5591, "2026-08-01")

        graded = await grade_trade(
            _trade(
                roster_ids=[1, 2],
                adds={"star": 2},
                draft_picks=[{"season": "2027", "round": 1, "owner_id": 1}],
                status_updated=_epoch_ms("2026-08-01"),
            )
        )

        assert graded["sides"][1]["then"] == 5591
        assert graded["sides"][2]["then"] == 8000

    async def test_uses_snapshot_from_before_the_trade_when_exact_date_missing(
        self, test_db
    ):
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, "2026-08-01")

        graded = await grade_trade(
            _trade(
                roster_ids=[1, 2],
                adds={"star": 1},
                status_updated=_epoch_ms("2026-08-05"),
            )
        )

        assert graded["sides"][1]["then"] == 8000

    async def test_returns_none_for_trades_predating_value_history(self, test_db):
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, "2026-08-01")

        graded = await grade_trade(
            _trade(
                roster_ids=[1, 2],
                adds={"star": 1},
                status_updated=_epoch_ms("2026-07-01"),
            )
        )

        assert graded is None

    async def test_ignores_non_trade_and_incomplete_transactions(self, test_db):
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, "2026-08-01")
        stamp = _epoch_ms("2026-08-02")

        assert await grade_trade(
            _trade(roster_ids=[1], status_updated=stamp, type_="waiver")
        ) is None
        assert await grade_trade(
            _trade(roster_ids=[1], status_updated=stamp, status="failed")
        ) is None

    async def test_unranked_player_is_labelled_and_flagged(self, test_db):
        await _insert_snapshot(test_db, 1, "star", "Star Player", 8000, "2026-08-01")

        graded = await grade_trade(
            _trade(
                roster_ids=[1, 2],
                adds={"deep_guy": 1},
                status_updated=_epoch_ms("2026-08-02"),
            ),
            player_names={"deep_guy": {"full_name": "Practice Squad Guy"}},
        )

        item = graded["sides"][1]["items"][0]
        assert item["label"] == "Practice Squad Guy"
        assert item["priced"] is False
        assert item["value"] == 0
