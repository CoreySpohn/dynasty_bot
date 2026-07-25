"""Tests for the league taxi rules.

These rules exist precisely because Sleeper doesn't implement them, so there
is no upstream behaviour to fall back on — the tests are the specification.
"""

from datetime import date

import pytest

from lib.taxi_rules import (
    TAXI_MAX_SEASONS,
    Acquisition,
    Ineligible,
    TaxiRecord,
    addition_window_open,
    audit,
    build_draft_index,
    build_records,
    eligible_additions,
    evaluate,
    evaluate_addition,
    over_slot_limit,
    presumed_activated,
    slot_of,
    upcoming_season,
)


def _record(**kwargs):
    base = dict(
        player_id="p1",
        owner_id="userA",
        roster_id=1,
        acquisition=Acquisition.ROOKIE_DRAFT,
        draft_season=2025,
        draft_round=3,
        activated=False,
        on_taxi=True,
    )
    base.update(kwargs)
    return TaxiRecord(**base)


class TestUpcomingSeason:
    def test_uses_sleepers_season_while_it_is_live(self):
        for status in ("pre_draft", "drafting", "in_season", "post_season"):
            league = {"season": "2026", "status": status}

            assert upcoming_season(league, date(2026, 10, 1)) == 2026

    def test_rolls_forward_when_the_league_is_stale(self):
        """The case that made this necessary: it is mid-2026, the 2025 season
        finished, and nobody has created the 2026 league yet. Judging taxi
        squads against 2025 would be judging against a dead deadline."""
        league = {"season": "2025", "status": "complete"}

        assert upcoming_season(league, date(2026, 7, 25)) == 2026

    def test_rolls_forward_before_the_calendar_turns_over(self):
        league = {"season": "2025", "status": "complete"}

        assert upcoming_season(league, date(2025, 12, 31)) == 2026

    def test_handles_a_league_stale_by_more_than_a_year(self):
        league = {"season": "2024", "status": "complete"}

        assert upcoming_season(league, date(2026, 7, 25)) == 2026


class TestUpcomingSeasonFromNflState:
    """Sleeper's /state/nfl knows the season outright, so it beats guessing."""

    STALE_LEAGUE = {"season": "2025", "status": "complete"}

    def test_prefers_nfl_state_over_the_league(self):
        state = {"league_season": "2026", "season": "2026", "season_type": "off"}

        assert upcoming_season(self.STALE_LEAGUE, date(2026, 7, 25), state) == 2026

    def test_nfl_state_wins_even_against_the_calendar(self):
        """January 2027, but the NFL still considers it the 2026 season."""
        state = {"league_season": "2026", "season": "2026", "season_type": "post"}

        assert upcoming_season(self.STALE_LEAGUE, date(2027, 1, 10), state) == 2026

    def test_falls_back_to_season_when_league_season_missing(self):
        assert upcoming_season(self.STALE_LEAGUE, date(2026, 7, 25), {"season": "2026"}) == 2026

    def test_falls_back_to_the_league_when_state_is_unusable(self):
        for state in (None, {}, {"season_type": "off"}):
            assert upcoming_season(self.STALE_LEAGUE, date(2026, 7, 25), state) == 2026


class TestAdditionWindowOpen:
    """The real deadline is a preseason game time nothing exposes, but
    season_type at least brackets it."""

    def test_open_in_the_offseason_and_preseason(self):
        assert addition_window_open({"season_type": "off"}) is True
        assert addition_window_open({"season_type": "pre"}) is True

    def test_shut_once_the_season_starts(self):
        assert addition_window_open({"season_type": "regular"}) is False
        assert addition_window_open({"season_type": "post"}) is False

    def test_unknown_state_stays_open(self):
        """Permissive on missing data: showing a stale addition is recoverable,
        hiding a legal one during the real window is not."""
        for state in (None, {}, {"season_type": None}):
            assert addition_window_open(state) is True


class TestEvaluate:
    def test_own_recent_draftee_is_eligible(self):
        assert evaluate(_record(), season=2025).eligible is True

    def test_player_drafted_by_someone_else_is_not(self):
        result = evaluate(_record(acquisition=Acquisition.OTHER), season=2025)

        assert result.eligible is False
        assert Ineligible.NOT_OWN_DRAFTEE in result.reasons

    def test_traded_for_player_is_not_eligible(self):
        """Explicit league rule: a player received in a trade can never go on
        taxi, even though they may have been on the other owner's."""
        result = evaluate(_record(acquisition=Acquisition.TRADE), season=2025)

        assert result.eligible is False
        assert Ineligible.ACQUIRED_BY_TRADE in result.reasons

    def test_activated_player_can_never_return(self):
        result = evaluate(_record(activated=True), season=2025)

        assert result.eligible is False
        assert Ineligible.ALREADY_ACTIVATED in result.reasons

    def test_expires_after_the_season_limit(self):
        # Drafted 2023 with a 3-season limit: 2023-2025 fine, 2026 expired.
        assert evaluate(_record(draft_season=2023), season=2025).eligible is True
        result = evaluate(_record(draft_season=2023), season=2026)

        assert result.eligible is False
        assert Ineligible.SEASONS_EXPIRED in result.reasons

    def test_limit_boundary_matches_the_constant(self):
        record = _record(draft_season=2020)

        assert evaluate(record, season=2020 + TAXI_MAX_SEASONS - 1).eligible is True
        assert evaluate(record, season=2020 + TAXI_MAX_SEASONS).eligible is False

    def test_reasons_accumulate(self):
        """An audit should be able to say both things, not just the first."""
        result = evaluate(
            _record(acquisition=Acquisition.TRADE, activated=True, draft_season=2020),
            season=2026,
        )

        assert set(result.reasons) == {
            Ineligible.ACQUIRED_BY_TRADE,
            Ineligible.ALREADY_ACTIVATED,
            Ineligible.SEASONS_EXPIRED,
        }
        assert "acquired by trade" in result.reason_text

    def test_undrafted_player_with_no_draft_season_never_expires(self):
        # No draft record means no clock, but also not an own-draftee.
        result = evaluate(
            _record(acquisition=Acquisition.OTHER, draft_season=None), season=2030
        )

        assert Ineligible.SEASONS_EXPIRED not in result.reasons
        assert Ineligible.NOT_OWN_DRAFTEE in result.reasons


class TestEvaluateAddition:
    """The addition window: "the only players you can put on your taxi during
    an off-season are players you took in the rookie draft that off-season"."""

    def test_this_years_draftee_can_be_added(self):
        assert evaluate_addition(_record(draft_season=2025), season=2025).eligible

    def test_last_years_draftee_cannot_be_added(self):
        """Still legal to *keep* on a slot, but the window has closed."""
        record = _record(draft_season=2024)

        assert evaluate(record, season=2025).eligible is True
        result = evaluate_addition(record, season=2025)
        assert result.eligible is False
        assert result.reasons == [Ineligible.OUTSIDE_ADDITION_WINDOW]

    def test_undrafted_player_can_never_be_added(self):
        result = evaluate_addition(
            _record(acquisition=Acquisition.OTHER, draft_season=None), season=2025
        )

        assert result.eligible is False
        assert Ineligible.OUTSIDE_ADDITION_WINDOW in result.reasons
        assert Ineligible.NOT_OWN_DRAFTEE in result.reasons

    def test_still_applies_the_other_rules(self):
        result = evaluate_addition(
            _record(draft_season=2025, activated=True), season=2025
        )

        assert result.eligible is False
        assert result.reasons == [Ineligible.ALREADY_ACTIVATED]


class TestAudit:
    def test_only_flags_players_actually_on_taxi(self):
        # Ineligible but on the active roster is just a normal player.
        records = [_record(acquisition=Acquisition.TRADE, on_taxi=False)]

        assert audit(records, 2025) == []

    def test_flags_an_illegal_taxi_player(self):
        records = [_record(acquisition=Acquisition.TRADE, on_taxi=True)]

        violations = audit(records, 2025)

        assert len(violations) == 1
        assert violations[0].player_id == "p1"
        assert Ineligible.ACQUIRED_BY_TRADE in violations[0].reasons

    def test_reports_seasons_used(self):
        records = [_record(draft_season=2022, on_taxi=True)]

        assert audit(records, 2026)[0].seasons_used == 4

    def test_legal_squad_produces_no_violations(self):
        assert audit([_record()], 2025) == []


class TestSlotOf:
    def test_identifies_each_slot(self):
        roster = {
            "players": ["a", "b", "c"],
            "taxi": ["a"],
            "reserve": ["b"],
        }

        assert slot_of(roster, "a") == "taxi"
        assert slot_of(roster, "b") == "reserve"
        assert slot_of(roster, "c") == "active"

    def test_handles_missing_arrays(self):
        assert slot_of({"players": ["a"]}, "a") == "active"


class TestOverSlotLimit:
    def test_flags_only_rosters_above_the_limit(self):
        rosters = [
            {"roster_id": 1, "taxi": ["a", "b", "c", "d", "e", "f"]},
            {"roster_id": 2, "taxi": ["a"]},
            {"roster_id": 3},
        ]

        assert over_slot_limit(rosters, 5) == [(1, 6)]


class TestBuildDraftIndex:
    def test_indexes_players_to_their_draft(self):
        index = build_draft_index([
            (2025, [{"player_id": "p1", "round": 2, "picked_by": "userA"}]),
        ])

        assert index["p1"] == (2025, 2, "userA")

    def test_earliest_draft_wins(self):
        """A later supplemental appearance must not reset the 3-season clock."""
        index = build_draft_index([
            (2026, [{"player_id": "p1", "round": 1, "picked_by": "userB"}]),
            (2024, [{"player_id": "p1", "round": 4, "picked_by": "userA"}]),
        ])

        assert index["p1"] == (2024, 4, "userA")

    def test_skips_picks_with_no_player(self):
        assert build_draft_index([(2025, [{"round": 1}])]) == {}


class TestBuildRecords:
    ROSTERS = [
        {
            "roster_id": 1,
            "owner_id": "userA",
            "players": ["own_pick", "traded_in", "waiver_guy"],
            "taxi": ["own_pick"],
        }
    ]
    DRAFT_INDEX = {
        "own_pick": (2025, 3, "userA"),
        "traded_in": (2025, 1, "userB"),
    }

    def test_classifies_acquisition(self):
        records = {
            r.player_id: r
            for r in build_records(self.ROSTERS, self.DRAFT_INDEX)
        }

        assert records["own_pick"].acquisition == Acquisition.ROOKIE_DRAFT
        # Drafted by someone else, so not an own-draftee however they arrived.
        assert records["traded_in"].acquisition == Acquisition.OTHER
        assert records["waiver_guy"].acquisition == Acquisition.OTHER

    def test_explicit_trade_record_overrides_draft_origin(self):
        """A player you drafted, traded away, and reacquired is no longer an
        own-draftee — the trade is what matters."""
        records = {
            r.player_id: r
            for r in build_records(
                self.ROSTERS,
                self.DRAFT_INDEX,
                traded_for={("userA", "own_pick")},
            )
        }

        assert records["own_pick"].acquisition == Acquisition.TRADE

    def test_marks_taxi_occupancy(self):
        records = {
            r.player_id: r for r in build_records(self.ROSTERS, self.DRAFT_INDEX)
        }

        assert records["own_pick"].on_taxi is True
        assert records["waiver_guy"].on_taxi is False

    def test_applies_recorded_activations(self):
        records = {
            r.player_id: r
            for r in build_records(
                self.ROSTERS,
                self.DRAFT_INDEX,
                activated={("userA", "own_pick"): 2025},
            )
        }

        assert records["own_pick"].activated is True
        assert records["own_pick"].activated_season == 2025


class TestPresumedActivated:
    """Seeding the ledger has to guess at activation history, since Sleeper
    doesn't keep it. These are the guesses it is allowed to make."""

    def test_prior_class_on_active_roster_is_presumed_activated(self):
        record = _record(draft_season=2024, on_taxi=False)

        assert presumed_activated(record, 2026) is True

    def test_current_class_on_bench_is_not(self):
        """The bug this guards: rookies sit on the bench straight out of the
        draft. Presuming activation would close a slot they can still use."""
        record = _record(draft_season=2026, on_taxi=False)

        assert presumed_activated(record, 2026) is False

    def test_player_still_on_taxi_is_not(self):
        assert presumed_activated(_record(draft_season=2024, on_taxi=True), 2026) is False

    def test_non_draftee_is_not(self):
        record = _record(
            draft_season=2024, on_taxi=False, acquisition=Acquisition.OTHER
        )

        assert presumed_activated(record, 2026) is False

    def test_undrafted_player_is_not(self):
        record = _record(
            draft_season=None, on_taxi=False, acquisition=Acquisition.ROOKIE_DRAFT
        )

        assert presumed_activated(record, 2026) is False


class TestEligibleAdditions:
    def test_excludes_players_already_on_taxi(self):
        records = [_record(player_id="on", on_taxi=True)]

        assert eligible_additions(records, 2025) == []

    def test_includes_an_eligible_bench_draftee(self):
        records = [_record(player_id="bench", on_taxi=False)]

        assert [r.player_id for r in eligible_additions(records, 2025)] == ["bench"]

    def test_excludes_ineligible_bench_players(self):
        records = [
            _record(
                player_id="traded",
                on_taxi=False,
                acquisition=Acquisition.TRADE,
                draft_season=2026,
            ),
            _record(player_id="old", on_taxi=False, draft_season=2020),
            _record(
                player_id="used", on_taxi=False, activated=True, draft_season=2026
            ),
        ]

        assert eligible_additions(records, 2026) == []

    def test_excludes_earlier_draft_classes(self):
        """The rule that Sleeper's "first three years" check gets wrong: a
        second-year player is not an available addition even though he could
        legally still be sitting on a slot."""
        records = [_record(player_id="sophomore", on_taxi=False, draft_season=2025)]

        assert eligible_additions(records, 2026) == []

    def test_is_empty_outside_a_draft_off_season(self):
        records = [
            _record(player_id="a", on_taxi=False, draft_season=2024),
            _record(player_id="b", on_taxi=False, draft_season=2025),
        ]

        assert eligible_additions(records, 2026) == []

    def test_closed_window_excludes_even_this_years_class(self):
        """Mid-season: the draft class is right, but the deadline is long gone."""
        records = [_record(player_id="rookie", on_taxi=False, draft_season=2026)]

        assert len(eligible_additions(records, 2026, window_open=True)) == 1
        assert eligible_additions(records, 2026, window_open=False) == []
