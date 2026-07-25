"""Tests for deriving the league state instead of setting it by hand."""

from datetime import date

import pytest

from lib.league_state import (
    IN_SEASON,
    OFF_SEASON,
    PRE_SEASON,
    DerivedState,
    derive_state,
    is_forward,
    should_apply,
)


class TestDeriveState:
    def test_regular_season_is_in_season(self):
        assert derive_state({"season_type": "regular"}).state == IN_SEASON

    def test_playoffs_are_in_season(self):
        assert derive_state({"season_type": "post"}).state == IN_SEASON

    def test_preseason_is_pre_season(self):
        assert derive_state({"season_type": "pre"}).state == PRE_SEASON

    def test_offseason_before_the_draft_is_off_season(self):
        derived = derive_state({"season_type": "off"}, rookie_draft_complete=False)

        assert derived.state == OFF_SEASON

    def test_completed_draft_moves_to_pre_season(self):
        derived = derive_state({"season_type": "off"}, rookie_draft_complete=True)

        assert derived.state == PRE_SEASON
        assert "rookie draft" in derived.reason

    def test_flips_the_day_before_the_opener(self):
        """The rule the config comments give: in_season starts the day before
        NFL week 1, which is still preseason by NFL reckoning."""
        opener = date(2026, 9, 9)

        assert derive_state(
            {"season_type": "pre"},
            regular_season_start=opener,
            today=date(2026, 9, 8),
        ).state == IN_SEASON
        assert derive_state(
            {"season_type": "pre"},
            regular_season_start=opener,
            today=date(2026, 9, 7),
        ).state == PRE_SEASON

    def test_unusable_state_derives_nothing(self):
        """Never guess - the caller must leave the state alone."""
        assert derive_state(None) is None
        assert derive_state({}) is None
        assert derive_state({"season_type": None}) is None

    def test_every_state_explains_itself(self):
        for state in ({"season_type": "regular"}, {"season_type": "pre"},
                      {"season_type": "off"}):
            assert derive_state(state).reason


class TestIsForward:
    def test_orders_the_season(self):
        assert is_forward(OFF_SEASON, PRE_SEASON) is True
        assert is_forward(PRE_SEASON, IN_SEASON) is True
        assert is_forward(OFF_SEASON, IN_SEASON) is True

    def test_backwards_is_not_forward(self):
        assert is_forward(IN_SEASON, OFF_SEASON) is False
        assert is_forward(PRE_SEASON, OFF_SEASON) is False

    def test_same_state_is_not_forward(self):
        assert is_forward(PRE_SEASON, PRE_SEASON) is False

    def test_unknown_states_never_trigger_a_change(self):
        assert is_forward("pre_draft", IN_SEASON) is False
        assert is_forward(OFF_SEASON, "nonsense") is False


class TestShouldApply:
    def test_applies_a_forward_move(self):
        assert should_apply(OFF_SEASON, DerivedState(PRE_SEASON, "why")) is True

    def test_refuses_the_wrap_back_to_offseason(self):
        """in_season -> off_season coincides with a human planning the
        offseason calendar, so it's suggested rather than applied."""
        assert should_apply(IN_SEASON, DerivedState(OFF_SEASON, "why")) is False

    def test_nothing_derived_means_nothing_applied(self):
        assert should_apply(OFF_SEASON, None) is False
