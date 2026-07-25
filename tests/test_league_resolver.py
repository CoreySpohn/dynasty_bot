"""Tests for following the league across renewals.

The guards matter more than the happy path here. Resolving to the *wrong* league
would serve confident, plausible data about strangers; failing to resolve just
serves stale data from a frozen league. So every ambiguous case must decline.
"""

from unittest.mock import AsyncMock

import pytest

from lib.league_resolver import pick_league, resolve_league_id

OLD_ID = "1231652068087844864"
NEW_ID = "1329282772417671168"

# The configured user really is in these three leagues for 2025, which is why
# name matching exists at all.
REAL_2025_LEAGUES = [
    {
        "league_id": "1267592261261078528",
        "name": "🪓 2025 Epsteins Island Was Never Real League",
        "total_rosters": 10,
        "season": "2025",
    },
    {
        "league_id": "1254970896590839808",
        "name": "2025 Epsteins Island Was Never Real League",
        "total_rosters": 10,
        "season": "2025",
    },
    {
        "league_id": OLD_ID,
        "name": "The Superflexers",
        "total_rosters": 12,
        "season": "2025",
    },
]


class TestPickLeague:
    def test_picks_the_named_league_out_of_several(self):
        picked = pick_league(REAL_2025_LEAGUES, "The Superflexers")

        assert picked["league_id"] == OLD_ID

    def test_ignores_case_and_whitespace(self):
        picked = pick_league(REAL_2025_LEAGUES, "  the   superflexers ")

        assert picked["league_id"] == OLD_ID

    def test_no_match_is_none(self):
        assert pick_league(REAL_2025_LEAGUES, "Some Other League") is None

    def test_empty_candidates_is_none(self):
        assert pick_league([], "The Superflexers") is None

    def test_blank_name_never_matches(self):
        """Otherwise a league with no name set would match everything."""
        assert pick_league([{"league_id": "x", "name": ""}], "") is None

    def test_refuses_to_guess_between_duplicates(self):
        leagues = [
            {"league_id": "a", "name": "The Superflexers", "total_rosters": 12},
            {"league_id": "b", "name": "The Superflexers", "total_rosters": 12},
        ]

        assert pick_league(leagues, "The Superflexers") is None

    def test_roster_count_disambiguates(self):
        leagues = [
            {"league_id": "a", "name": "The Superflexers", "total_rosters": 10},
            {"league_id": "b", "name": "The Superflexers", "total_rosters": 12},
        ]

        assert pick_league(leagues, "The Superflexers", 12)["league_id"] == "b"

    def test_rejects_a_name_match_of_the_wrong_size(self):
        """A same-named league with a different team count is a different league."""
        leagues = [
            {"league_id": "a", "name": "The Superflexers", "total_rosters": 10}
        ]

        assert pick_league(leagues, "The Superflexers", 12) is None


_UNSET = object()


def _client(*, state=_UNSET, league=_UNSET, user_leagues=None, **overrides):
    """Build a stub client.

    `state` and `league` use a sentinel rather than `or`, so a test can pass an
    empty dict to mean "the API returned nothing useful" - which is a case worth
    covering and one that `or` would silently replace with the default.
    """
    client = AsyncMock()
    client.get_nfl_state.return_value = (
        {"league_season": "2026", "season": "2026"} if state is _UNSET else state
    )
    client.get_league.return_value = (
        {"name": "The Superflexers", "season": "2025", "total_rosters": 12}
        if league is _UNSET
        else league
    )
    client.get_user_leagues.return_value = (
        user_leagues if user_leagues is not None else [
            {
                "league_id": NEW_ID,
                "name": "The Superflexers",
                "total_rosters": 12,
                "season": "2026",
            }
        ]
    )
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class TestResolveLeagueId:
    async def test_follows_a_renewal(self):
        resolved = await resolve_league_id(_client(), "user1", OLD_ID)

        assert resolved == NEW_ID

    async def test_no_user_id_disables_resolution(self):
        client = _client()

        assert await resolve_league_id(client, None, OLD_ID) == OLD_ID
        client.get_user_leagues.assert_not_called()

    async def test_skips_lookup_when_already_current(self):
        """The common case every startup after the first - don't pay for it."""
        client = _client(
            league={
                "name": "The Superflexers",
                "season": "2026",
                "total_rosters": 12,
            }
        )

        assert await resolve_league_id(client, "user1", NEW_ID) == NEW_ID
        client.get_user_leagues.assert_not_called()

    async def test_keeps_configured_id_when_no_league_matches(self):
        """Early in a year, before the renewal exists."""
        client = _client(user_leagues=[])

        assert await resolve_league_id(client, "user1", OLD_ID) == OLD_ID

    async def test_keeps_configured_id_on_wrong_named_leagues(self):
        client = _client(user_leagues=REAL_2025_LEAGUES[:2])

        assert await resolve_league_id(client, "user1", OLD_ID) == OLD_ID

    async def test_explicit_name_override_wins(self):
        client = _client(
            league={"name": "Renamed", "season": "2025", "total_rosters": 12}
        )

        resolved = await resolve_league_id(
            client, "user1", OLD_ID, league_name="The Superflexers"
        )

        assert resolved == NEW_ID

    async def test_survives_an_api_failure(self):
        client = _client(get_nfl_state=AsyncMock(side_effect=RuntimeError("boom")))

        assert await resolve_league_id(client, "user1", OLD_ID) == OLD_ID

    async def test_survives_a_user_leagues_failure(self):
        client = _client(
            get_user_leagues=AsyncMock(side_effect=RuntimeError("boom"))
        )

        assert await resolve_league_id(client, "user1", OLD_ID) == OLD_ID

    async def test_missing_season_keeps_configured_id(self):
        client = _client(state={})

        assert await resolve_league_id(client, "user1", OLD_ID) == OLD_ID

    async def test_falls_back_to_season_when_league_season_absent(self):
        client = _client(state={"season": "2026"})

        assert await resolve_league_id(client, "user1", OLD_ID) == NEW_ID
