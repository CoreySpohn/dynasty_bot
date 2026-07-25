"""Tests for the ESPN preseason client."""

from datetime import date

import pytest
from aioresponses import aioresponses

from clients.espn import SCOREBOARD_URL, ESPNClient


def _url(season: int, week: int) -> str:
    return (
        f"{SCOREBOARD_URL}?dates={season}&seasontype=1&week={week}"
    )


def _payload(week: int, dates: list[str]) -> dict:
    return {
        "week": {"number": week},
        "events": [{"date": f"{d}T23:00Z"} for d in dates],
    }


class TestESPNClient:
    @pytest.fixture
    def client(self, aiohttp_session):
        return ESPNClient(aiohttp_session)

    async def test_fetches_every_preseason_week(self, client, mock_aioresponses):
        mock_aioresponses.get(_url(2026, 1), payload=_payload(1, ["2026-08-07"]))
        mock_aioresponses.get(
            _url(2026, 2), payload=_payload(2, ["2026-08-13", "2026-08-16"])
        )
        mock_aioresponses.get(_url(2026, 3), payload=_payload(3, ["2026-08-20"]))
        mock_aioresponses.get(_url(2026, 4), payload=_payload(4, ["2026-08-27"]))

        weeks = await client.get_preseason_weeks(2026)

        assert [w.week for w in weeks] == [1, 2, 3, 4]
        assert weeks[1].last_game == date(2026, 8, 16)

    async def test_drops_empty_weeks(self, client, mock_aioresponses):
        mock_aioresponses.get(_url(2026, 1), payload=_payload(1, ["2026-08-07"]))
        for week in (2, 3, 4):
            mock_aioresponses.get(
                _url(2026, week), payload={"week": {"number": week}, "events": []}
            )

        weeks = await client.get_preseason_weeks(2026)

        assert [w.week for w in weeks] == [1]

    async def test_survives_a_failed_week(self, client, mock_aioresponses):
        """One bad request shouldn't lose the weeks that did come back."""
        mock_aioresponses.get(_url(2026, 1), status=500)
        mock_aioresponses.get(
            _url(2026, 2), payload=_payload(2, ["2026-08-13", "2026-08-16"])
        )
        for week in (3, 4):
            mock_aioresponses.get(
                _url(2026, week), payload={"week": {"number": week}, "events": []}
            )

        weeks = await client.get_preseason_weeks(2026)

        assert [w.week for w in weeks] == [2]

    async def test_schedule_not_published_yet(self, client, mock_aioresponses):
        """Empty must read as 'unknown', never as 'there is no preseason'."""
        for week in (1, 2, 3, 4):
            mock_aioresponses.get(
                _url(2026, week), payload={"week": {"number": week}, "events": []}
            )

        assert await client.get_preseason_weeks(2026) == []
