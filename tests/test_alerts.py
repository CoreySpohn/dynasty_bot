"""Tests for the lineup Alerts cog."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.alerts import Alerts


@pytest.fixture
def alerts_cog(mock_bot):
    """Create an Alerts cog wired to a mock bot.sleeper client."""
    mock_bot.sleeper = MagicMock()
    mock_bot.sleeper.get_nfl_state = AsyncMock(
        return_value={"week": 5, "season_type": "regular"}
    )
    mock_bot.sleeper.get_all_players = AsyncMock(return_value={})
    mock_bot.sleeper.get_matchups = AsyncMock(return_value=[])
    mock_bot.sleeper.get_rosters = AsyncMock(return_value=[])

    channel = MagicMock()
    channel.send = AsyncMock()
    mock_bot.get_channel = MagicMock(return_value=channel)

    return Alerts(mock_bot)


class TestCheckStarters:
    """Test suite for the check_starters background task logic."""

    async def test_uses_real_bot_sleeper_client(self, alerts_cog):
        """Regression test: the cog must call bot.sleeper, not a separate client.

        Previously the cog was constructed with an unrelated SleeperClient
        (utils/sleeper.py) that had incompatible method signatures and was
        never actually wired to the bot, so every run threw AttributeError.
        """
        await alerts_cog.check_starters.coro(alerts_cog)

        alerts_cog.bot.sleeper.get_nfl_state.assert_awaited_once()
        alerts_cog.bot.sleeper.get_all_players.assert_awaited_once()
        alerts_cog.bot.sleeper.get_matchups.assert_awaited_once_with(
            alerts_cog.league_id, 5
        )
        alerts_cog.bot.sleeper.get_rosters.assert_awaited_once_with(
            alerts_cog.league_id
        )

    @pytest.mark.parametrize("season_type", ["off", "pre", None])
    async def test_skips_outside_regular_or_post_season(self, alerts_cog, season_type):
        """No alerts should be checked outside the regular season or playoffs."""
        alerts_cog.bot.sleeper.get_nfl_state = AsyncMock(
            return_value={"week": 1, "season_type": season_type}
        )

        await alerts_cog.check_starters.coro(alerts_cog)

        alerts_cog.bot.sleeper.get_matchups.assert_not_awaited()

    async def test_alerts_inactive_and_out_starters(self, alerts_cog):
        """Inactive/out starters should generate an alert sent to the channel."""
        alerts_cog.bot.sleeper.get_all_players = AsyncMock(
            return_value={
                "p1": {"full_name": "Injured Guy", "position": "WR", "status": "Inactive"},
                "p2": {"full_name": "Hurt Guy", "position": "RB", "injury_status": "Out"},
                "p3": {"full_name": "Healthy Guy", "position": "QB", "status": "Active"},
            }
        )
        alerts_cog.bot.sleeper.get_matchups = AsyncMock(
            return_value=[{"roster_id": 1, "starters": ["p1", "p2", "p3"]}]
        )
        alerts_cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[{"roster_id": 1, "metadata": {"team_name": "Test Team"}}]
        )

        await alerts_cog.check_starters.coro(alerts_cog)

        channel = alerts_cog.bot.get_channel.return_value
        assert channel.send.await_count == 2
        sent_messages = [call.args[0] for call in channel.send.await_args_list]
        assert any("Injured Guy" in m and "INACTIVE" in m for m in sent_messages)
        assert any("Hurt Guy" in m and "OUT" in m for m in sent_messages)
        assert not any("Healthy Guy" in m for m in sent_messages)

    async def test_team_name_resolved_despite_int_roster_id(self, alerts_cog):
        """Regression test: roster_id from Sleeper is an int, matchup roster_id
        gets stringified for the sent_alerts key. The roster lookup must not
        silently miss and fall back to the generic 'Team {id}' name.
        """
        alerts_cog.bot.sleeper.get_all_players = AsyncMock(
            return_value={"p1": {"full_name": "Someone", "position": "WR", "status": "Inactive"}}
        )
        alerts_cog.bot.sleeper.get_matchups = AsyncMock(
            return_value=[{"roster_id": 1, "starters": ["p1"]}]
        )
        alerts_cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[{"roster_id": 1, "metadata": {"team_name": "Real Team Name"}}]
        )

        await alerts_cog.check_starters.coro(alerts_cog)

        channel = alerts_cog.bot.get_channel.return_value
        sent = channel.send.await_args_list[0].args[0]
        assert "Real Team Name" in sent
        assert "Team 1" not in sent

    async def test_does_not_resend_same_alert(self, alerts_cog):
        """Once an alert has been sent for a (week, player, roster), it's not repeated."""
        alerts_cog.bot.sleeper.get_all_players = AsyncMock(
            return_value={"p1": {"full_name": "Someone", "position": "WR", "status": "Inactive"}}
        )
        alerts_cog.bot.sleeper.get_matchups = AsyncMock(
            return_value=[{"roster_id": 1, "starters": ["p1"]}]
        )
        alerts_cog.bot.sleeper.get_rosters = AsyncMock(return_value=[])

        await alerts_cog.check_starters.coro(alerts_cog)
        await alerts_cog.check_starters.coro(alerts_cog)

        channel = alerts_cog.bot.get_channel.return_value
        assert channel.send.await_count == 1

    async def test_skips_sending_when_alert_channel_not_configured(self, alerts_cog):
        """If ALERT_CHANNEL_ID isn't set, the task should skip sending rather
        than crash trying to fetch a None channel."""
        alerts_cog.bot.sleeper.get_all_players = AsyncMock(
            return_value={"p1": {"full_name": "Someone", "position": "WR", "status": "Inactive"}}
        )
        alerts_cog.bot.sleeper.get_matchups = AsyncMock(
            return_value=[{"roster_id": 1, "starters": ["p1"]}]
        )
        alerts_cog.bot.sleeper.get_rosters = AsyncMock(return_value=[])

        with patch("cogs.alerts.ALERT_CHANNEL_ID", None):
            await alerts_cog.check_starters.coro(alerts_cog)

        alerts_cog.bot.get_channel.assert_not_called()

    async def test_exceptions_are_caught_and_logged(self, alerts_cog):
        """A failure mid-check (e.g. a Sleeper API error) shouldn't crash the loop."""
        alerts_cog.bot.sleeper.get_nfl_state = AsyncMock(side_effect=RuntimeError("boom"))

        # Should not raise.
        await alerts_cog.check_starters.coro(alerts_cog)


class TestCheckStartersReturnValue:
    """The return value of _check_starters() drives the /checkalerts reply."""

    async def test_returns_none_when_season_is_off(self, alerts_cog):
        alerts_cog.bot.sleeper.get_nfl_state = AsyncMock(
            return_value={"week": 0, "season_type": "off"}
        )

        result = await alerts_cog._check_starters()

        assert result is None

    async def test_returns_zero_when_nothing_to_alert(self, alerts_cog):
        result = await alerts_cog._check_starters()

        assert result == 0

    async def test_returns_count_of_alerts_sent(self, alerts_cog):
        alerts_cog.bot.sleeper.get_all_players = AsyncMock(
            return_value={
                "p1": {"full_name": "Someone", "position": "WR", "status": "Inactive"},
            }
        )
        alerts_cog.bot.sleeper.get_matchups = AsyncMock(
            return_value=[{"roster_id": 1, "starters": ["p1"]}]
        )
        alerts_cog.bot.sleeper.get_rosters = AsyncMock(return_value=[])

        result = await alerts_cog._check_starters()

        assert result == 1


class TestCheckAlertsCommand:
    """Regression coverage for the /checkalerts manual trigger command."""

    async def test_reports_skip_reason_when_season_off(self, alerts_cog):
        alerts_cog.bot.sleeper.get_nfl_state = AsyncMock(
            return_value={"week": 0, "season_type": "off"}
        )
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await alerts_cog.check_alerts_now.callback(alerts_cog, interaction)

        interaction.followup.send.assert_awaited_once()
        message = interaction.followup.send.await_args.args[0]
        assert "offseason" in message.lower() or "regular season" in message.lower() or "playoffs" in message.lower()
