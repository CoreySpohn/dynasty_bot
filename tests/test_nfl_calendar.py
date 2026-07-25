"""Tests for the NFL preseason calendar.

The week-numbering quirk these encode is real and stable: ESPN calls the Hall of
Fame Game preseason week 1 on its own, then the full 16-game weeks 2-4. Pinning
a league-wide roster deadline to a single exhibition game would cost owners
nine days, so `first_full_week` skips it.
"""

from datetime import date

import pytest
import yaml

from lib.nfl_calendar import (
    ANCHOR_REGULAR_SEASON_START,
    ANCHOR_ROOKIE_DRAFT_END,
    ANCHOR_TAXI_DEADLINE,
    PreseasonWeek,
    effective_taxi_deadline,
    first_full_week,
    load_anchors,
    parse_preseason_week,
    parse_preseason_weeks,
    preseason_bounds,
    save_anchors,
    stored_taxi_deadline,
    taxi_deadline,
)


def _payload(week: int, dates: list[str]) -> dict:
    return {
        "week": {"number": week},
        "events": [{"date": f"{d}T23:00Z"} for d in dates],
    }


# The shape ESPN actually returned for 2026.
HOF_WEEK = _payload(1, ["2026-08-07"])
FULL_WEEK_2 = _payload(
    2, ["2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16"]
)
FULL_WEEK_3 = _payload(3, ["2026-08-20", "2026-08-22"])


class TestParsePreseasonWeek:
    def test_parses_a_week(self):
        week = parse_preseason_week(FULL_WEEK_2)

        assert week.week == 2
        assert week.first_game == date(2026, 8, 13)
        assert week.last_game == date(2026, 8, 16)
        assert week.game_count == 4

    def test_empty_week_is_none(self):
        """How ESPN reports a week that doesn't exist in a given year."""
        assert parse_preseason_week({"week": {"number": 5}, "events": []}) is None

    def test_missing_events_key_is_none(self):
        assert parse_preseason_week({}) is None

    def test_tolerates_a_plain_date(self):
        week = parse_preseason_week(
            {"week": {"number": 1}, "events": [{"date": "2026-08-07"}]}
        )

        assert week.last_game == date(2026, 8, 7)

    def test_skips_unparseable_dates(self):
        week = parse_preseason_week(
            {
                "week": {"number": 2},
                "events": [{"date": "not-a-date"}, {"date": "2026-08-13T23:00Z"}],
            }
        )

        assert week.game_count == 1
        assert week.last_game == date(2026, 8, 13)

    def test_orders_weeks(self):
        weeks = parse_preseason_weeks([FULL_WEEK_3, HOF_WEEK, FULL_WEEK_2])

        assert [w.week for w in weeks] == [1, 2, 3]


class TestFirstFullWeek:
    def test_skips_the_hall_of_fame_game(self):
        weeks = parse_preseason_weeks([HOF_WEEK, FULL_WEEK_2, FULL_WEEK_3])

        assert first_full_week(weeks).week == 2

    def test_uses_week_one_when_it_is_already_league_wide(self):
        """Years with no Hall of Fame game (2020, 2021) need no special case."""
        weeks = parse_preseason_weeks([FULL_WEEK_2, FULL_WEEK_3])

        assert first_full_week(weeks).week == 2

    def test_falls_back_to_the_earliest_single_game_week(self):
        weeks = parse_preseason_weeks([HOF_WEEK])

        assert first_full_week(weeks).week == 1

    def test_no_weeks_is_none(self):
        assert first_full_week([]) is None


class TestTaxiDeadline:
    def test_is_the_last_game_of_the_first_full_week(self):
        weeks = parse_preseason_weeks([HOF_WEEK, FULL_WEEK_2, FULL_WEEK_3])

        assert taxi_deadline(weeks) == date(2026, 8, 16)

    def test_unknown_when_the_schedule_is_not_out(self):
        assert taxi_deadline([]) is None


class TestPreseasonBounds:
    def test_spans_every_week(self):
        weeks = parse_preseason_weeks([HOF_WEEK, FULL_WEEK_2, FULL_WEEK_3])

        assert preseason_bounds(weeks) == (date(2026, 8, 7), date(2026, 8, 22))

    def test_empty(self):
        assert preseason_bounds([]) == (None, None)


class TestEffectiveTaxiDeadline:
    """Reconciling the written deadline with a draft that floats past it.

    The seasons here are real: the league's rookie draft ran to Aug 18 in 2023
    and Aug 19 in 2025, while the first full preseason week ended Aug 13 and
    Aug 10.
    """

    def test_written_deadline_holds_when_the_draft_finishes_in_time(self):
        # 2024: draft ended Aug 6, written deadline Aug 11.
        assert effective_taxi_deadline(
            date(2024, 8, 11), date(2024, 8, 6), date(2024, 9, 5)
        ) == date(2024, 8, 11)

    def test_extends_when_the_draft_overtakes_it(self):
        # 2025: draft ended Aug 19, so Aug 10 is unusable.
        assert effective_taxi_deadline(
            date(2025, 8, 10), date(2025, 8, 19), date(2025, 9, 4)
        ) == date(2025, 8, 22)

    def test_extends_for_2023_too(self):
        assert effective_taxi_deadline(
            date(2023, 8, 13), date(2023, 8, 18), date(2023, 9, 7)
        ) == date(2023, 8, 21)

    def test_grace_is_overridable(self):
        assert effective_taxi_deadline(
            date(2025, 8, 10), date(2025, 8, 19), grace_days=7
        ) == date(2025, 8, 26)

    def test_never_runs_past_the_opener(self):
        """Taxi decisions can't still be open once games count."""
        assert effective_taxi_deadline(
            date(2026, 8, 16), date(2026, 9, 6), date(2026, 9, 9)
        ) == date(2026, 9, 8)

    def test_clamp_yields_when_it_would_precede_the_draft(self):
        """A draft finishing after kickoff means the calendar is already broken;
        shortening the deadline below the draft's own end helps nobody."""
        assert effective_taxi_deadline(
            date(2026, 8, 16), date(2026, 9, 20), date(2026, 9, 9)
        ) == date(2026, 9, 23)

    def test_no_opener_means_no_clamp(self):
        assert effective_taxi_deadline(
            date(2026, 8, 16), date(2026, 8, 19)
        ) == date(2026, 8, 22)

    def test_unknown_inputs(self):
        assert effective_taxi_deadline(None, date(2026, 8, 19)) is None
        assert effective_taxi_deadline(date(2026, 8, 16), None) is None


class TestStoredTaxiDeadline:
    """Every path here exists to avoid enforcing a deadline that would close
    the window when it shouldn't be closed."""

    def test_returns_a_consistent_deadline(self):
        anchors = {
            ANCHOR_TAXI_DEADLINE: "2026-08-16",
            ANCHOR_ROOKIE_DRAFT_END: "2026-08-10",
        }

        assert stored_taxi_deadline(2026, anchors) == date(2026, 8, 16)

    def test_ignores_a_deadline_from_another_season(self):
        anchors = {
            ANCHOR_TAXI_DEADLINE: "2025-08-10",
            ANCHOR_ROOKIE_DRAFT_END: "2025-08-19",
        }

        assert stored_taxi_deadline(2026, anchors) is None

    def test_not_enforced_until_the_draft_is_complete(self):
        """The window can't close before the draft that fills it, and the draft
        floats to whatever weekend owners can manage."""
        anchors = {ANCHOR_TAXI_DEADLINE: "2026-08-16"}

        assert stored_taxi_deadline(2026, anchors) is None

    def test_moves_the_deadline_when_the_draft_ran_past_it(self):
        """The 2025 case: applied literally the deadline expired before anyone
        drafted, so it shifts to a few days after the final pick."""
        anchors = {
            ANCHOR_TAXI_DEADLINE: "2026-08-16",
            ANCHOR_ROOKIE_DRAFT_END: "2026-08-19",
            ANCHOR_REGULAR_SEASON_START: "2026-09-09",
        }

        assert stored_taxi_deadline(2026, anchors) == date(2026, 8, 22)

    def test_respects_the_opener_clamp_from_anchors(self):
        anchors = {
            ANCHOR_TAXI_DEADLINE: "2026-08-16",
            ANCHOR_ROOKIE_DRAFT_END: "2026-09-06",
            ANCHOR_REGULAR_SEASON_START: "2026-09-09",
        }

        assert stored_taxi_deadline(2026, anchors) == date(2026, 9, 8)

    def test_missing_and_malformed_anchors(self):
        assert stored_taxi_deadline(2026, {}) is None
        assert stored_taxi_deadline(2026, {ANCHOR_TAXI_DEADLINE: "nonsense"}) is None
        assert stored_taxi_deadline(
            2026,
            {ANCHOR_TAXI_DEADLINE: "2026-08-16", ANCHOR_ROOKIE_DRAFT_END: "??"},
        ) is None


class TestLoadAnchors:
    def test_reads_a_bare_mapping(self, tmp_path):
        path = tmp_path / "nfl_anchors.yaml"
        path.write_text(yaml.dump({"a": "2026-01-01"}))

        assert load_anchors(path) == {"a": "2026-01-01"}

    def test_reads_a_nested_block(self, tmp_path):
        """Tolerates the legacy shape in case anchors are pasted across."""
        path = tmp_path / "nfl_anchors.yaml"
        path.write_text(yaml.dump({"nfl_anchors": {"a": "2026-01-01"}}))

        assert load_anchors(path) == {"a": "2026-01-01"}

    def test_missing_file_is_empty(self, tmp_path):
        assert load_anchors(tmp_path / "nope.yaml") == {}

    def test_malformed_file_is_empty(self, tmp_path):
        path = tmp_path / "nfl_anchors.yaml"
        path.write_text("{[not valid yaml")

        assert load_anchors(path) == {}


class TestSaveAnchors:
    ANCHORS = {"nfl_taxi_deadline": "2026-08-16", "rookie_draft_end": None}

    def test_writes_and_round_trips(self, tmp_path):
        path = tmp_path / "nfl_anchors.yaml"

        assert save_anchors(self.ANCHORS, path) is True
        assert load_anchors(path) == self.ANCHORS

    def test_skips_an_identical_write(self, tmp_path):
        """The upkeep loop re-checks every 12 hours for the weeks it can take
        the draft to finish; rewriting an unchanged file each time is waste."""
        path = tmp_path / "nfl_anchors.yaml"
        save_anchors(self.ANCHORS, path)
        before = path.stat().st_mtime_ns

        assert save_anchors(self.ANCHORS, path) is False
        assert path.stat().st_mtime_ns == before

    def test_writes_when_a_value_changes(self, tmp_path):
        path = tmp_path / "nfl_anchors.yaml"
        save_anchors(self.ANCHORS, path)
        changed = {**self.ANCHORS, "rookie_draft_end": "2026-08-19"}

        assert save_anchors(changed, path) is True
        assert load_anchors(path)["rookie_draft_end"] == "2026-08-19"

    def test_keeps_a_generated_header(self, tmp_path):
        path = tmp_path / "nfl_anchors.yaml"
        save_anchors(self.ANCHORS, path)

        assert path.read_text().startswith("#")
