"""Tests for the league taxi rules.

These rules exist precisely because Sleeper doesn't implement them, so there
is no upstream behaviour to fall back on — the tests are the specification.
"""

import pytest

from lib.taxi_rules import (
    TAXI_MAX_SEASONS,
    Acquisition,
    Ineligible,
    TaxiRecord,
    audit,
    build_draft_index,
    build_records,
    eligible_additions,
    evaluate,
    over_slot_limit,
    slot_of,
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


class TestEligibleAdditions:
    def test_excludes_players_already_on_taxi(self):
        records = [_record(player_id="on", on_taxi=True)]

        assert eligible_additions(records, 2025) == []

    def test_includes_an_eligible_bench_draftee(self):
        records = [_record(player_id="bench", on_taxi=False)]

        assert [r.player_id for r in eligible_additions(records, 2025)] == ["bench"]

    def test_excludes_ineligible_bench_players(self):
        records = [
            _record(player_id="traded", on_taxi=False, acquisition=Acquisition.TRADE),
            _record(player_id="old", on_taxi=False, draft_season=2020),
            _record(player_id="used", on_taxi=False, activated=True),
        ]

        assert eligible_additions(records, 2026) == []
