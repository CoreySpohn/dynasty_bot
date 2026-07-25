"""Tests for the NFL calendar dates league deadlines depend on.

The taxi deadline is now the start of the regular season. It used to be derived
from the preseason schedule, which was changed because the rookie draft floats
to whatever weekend owners can make and kept finishing *after* that date - in
2023 and 2025 the old deadline had expired before anyone could draft.
"""

from datetime import date

import pytest
import yaml

from lib.nfl_calendar import (
    ANCHOR_REGULAR_SEASON_START,
    ANCHOR_ROOKIE_DRAFT_END,
    ANCHOR_TAXI_DEADLINE,
    TAXI_DEADLINE_DAYS_BEFORE_OPENER,
    load_anchors,
    parse_preseason_week,
    parse_preseason_weeks,
    preseason_bounds,
    save_anchors,
    stored_taxi_deadline,
    taxi_deadline_from_opener,
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

    def test_identifies_a_single_game_week(self):
        """ESPN numbers the Hall of Fame Game as preseason week 1 by itself."""
        weeks = parse_preseason_weeks([HOF_WEEK, FULL_WEEK_2])

        assert weeks[0].is_league_wide is False
        assert weeks[1].is_league_wide is True


class TestPreseasonBounds:
    def test_spans_every_week(self):
        weeks = parse_preseason_weeks([HOF_WEEK, FULL_WEEK_2, FULL_WEEK_3])

        assert preseason_bounds(weeks) == (date(2026, 8, 7), date(2026, 8, 22))

    def test_empty(self):
        assert preseason_bounds([]) == (None, None)


class TestTaxiDeadlineFromOpener:
    def test_is_the_day_before_kickoff(self):
        assert taxi_deadline_from_opener(date(2026, 9, 9)) == date(2026, 9, 8)

    def test_matches_the_constant(self):
        opener = date(2026, 9, 9)
        deadline = taxi_deadline_from_opener(opener)

        assert (opener - deadline).days == TAXI_DEADLINE_DAYS_BEFORE_OPENER

    def test_unknown_opener(self):
        assert taxi_deadline_from_opener(None) is None


class TestStoredTaxiDeadline:
    """Always derived from the stored opener, never stored itself.

    Storing it duplicated the opener and the copy went stale: the first cut of
    the season-start change left a deadline computed under the old preseason
    rule in the anchors file, and the upkeep loop's cheap draft-only path would
    never have recomputed it.
    """

    def test_derives_from_the_opener(self):
        anchors = {ANCHOR_REGULAR_SEASON_START: "2026-09-09"}

        assert stored_taxi_deadline(2026, anchors) == date(2026, 9, 8)

    def test_ignores_a_stored_deadline_entirely(self):
        """The stale-value regression: an old preseason-derived date must not
        override what the opener says."""
        anchors = {
            ANCHOR_REGULAR_SEASON_START: "2026-09-09",
            ANCHOR_TAXI_DEADLINE: "2026-08-16",
        }

        assert stored_taxi_deadline(2026, anchors) == date(2026, 9, 8)

    def test_enforced_even_before_the_draft_finishes(self):
        """Unlike the old preseason rule, a season-start deadline can't precede
        the draft that fills the slots, so there's no reason to withhold it."""
        anchors = {ANCHOR_REGULAR_SEASON_START: "2026-09-09"}

        assert stored_taxi_deadline(2026, anchors) == date(2026, 9, 8)

    def test_ignores_an_opener_from_another_season(self):
        """A year-old anchors file is normal until the upkeep loop re-syncs."""
        anchors = {ANCHOR_REGULAR_SEASON_START: "2025-09-04"}

        assert stored_taxi_deadline(2026, anchors) is None

    def test_defers_to_a_draft_that_ran_past_it(self):
        """Only reachable if the calendar is already broken, but closing the
        window before the draft that fills it would be worse."""
        anchors = {
            ANCHOR_REGULAR_SEASON_START: "2026-09-09",
            ANCHOR_ROOKIE_DRAFT_END: "2026-09-20",
        }

        assert stored_taxi_deadline(2026, anchors) == date(2026, 9, 20)

    def test_no_anchors_at_all(self):
        assert stored_taxi_deadline(2026, {}) is None

    def test_malformed_opener(self):
        anchors = {ANCHOR_REGULAR_SEASON_START: "junk"}

        assert stored_taxi_deadline(2026, anchors) is None


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
    ANCHORS = {"nfl_taxi_deadline": "2026-09-08", "rookie_draft_end": None}

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
