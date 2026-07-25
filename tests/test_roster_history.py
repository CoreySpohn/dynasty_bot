"""Tests for roster composition snapshots.

Sleeper only serves current rosters, so these snapshots are the only record
of who owned whom on a past date. Nothing can backfill them, which makes
the skip-if-unchanged and refuse-if-empty behaviours load-bearing rather
than cosmetic.
"""

from unittest.mock import patch

import pytest
import pytest_asyncio

import lib.roster_history as roster_history_module
from database import Database
from lib.roster_history import (
    get_composition_as_of,
    get_owner_map_as_of,
    get_snapshot_date_as_of,
    get_snapshot_dates,
    store_snapshot,
)


@pytest_asyncio.fixture
async def test_db():
    """In-memory database patched in as lib.roster_history's `db` singleton."""
    database = Database(":memory:")
    await database.connect()
    with patch.object(roster_history_module, "db", database):
        yield database
    await database.close()


def _rosters(*specs):
    """Build Sleeper-shaped rosters from (roster_id, owner_id, players)."""
    return [
        {"roster_id": rid, "owner_id": owner, "players": list(players)}
        for rid, owner, players in specs
    ]


class TestStoreSnapshot:
    async def test_stores_composition_for_a_date(self, test_db):
        written = await store_snapshot(
            _rosters((1, "u1", ["p1", "p2"]), (2, "u2", ["p3"])),
            recorded_date="2026-07-25",
        )

        assert written is True
        assert await get_composition_as_of("2026-07-25") == {
            1: ["p1", "p2"],
            2: ["p3"],
        }

    async def test_skips_write_when_composition_unchanged(self, test_db):
        rosters = _rosters((1, "u1", ["p1", "p2"]))
        await store_snapshot(rosters, recorded_date="2026-07-25")

        written = await store_snapshot(rosters, recorded_date="2026-07-26")

        assert written is False
        assert await get_snapshot_dates() == ["2026-07-25"]

    async def test_writes_again_once_composition_changes(self, test_db):
        await store_snapshot(_rosters((1, "u1", ["p1"])), recorded_date="2026-07-25")

        written = await store_snapshot(
            _rosters((1, "u1", ["p1", "p2"])), recorded_date="2026-07-27"
        )

        assert written is True
        assert await get_snapshot_dates() == ["2026-07-27", "2026-07-25"]

    async def test_rerunning_same_day_replaces_rather_than_duplicates(self, test_db):
        await store_snapshot(_rosters((1, "u1", ["p1"])), recorded_date="2026-07-25")
        await store_snapshot(
            _rosters((1, "u1", ["p1", "p2"])), recorded_date="2026-07-25"
        )

        assert await get_snapshot_dates() == ["2026-07-25"]
        assert await get_composition_as_of("2026-07-25") == {1: ["p1", "p2"]}

    async def test_refuses_empty_rosters(self, test_db):
        # A failed Sleeper fetch must not be recorded as everyone dropping
        # everyone - that would be indistinguishable from a real wipe.
        assert await store_snapshot([]) is False
        assert await store_snapshot(_rosters((1, "u1", []))) is False
        assert await get_snapshot_dates() == []

    async def test_records_owner_ids(self, test_db):
        await store_snapshot(
            _rosters((1, "u1", ["p1"]), (2, "u2", ["p2"])),
            recorded_date="2026-07-25",
        )

        assert await get_owner_map_as_of("2026-07-25") == {1: "u1", 2: "u2"}


class TestAsOfReads:
    @pytest_asyncio.fixture(autouse=True)
    async def _history(self, test_db):
        await store_snapshot(_rosters((1, "u1", ["p1"])), recorded_date="2026-07-01")
        await store_snapshot(
            _rosters((1, "u1", ["p1", "p2"])), recorded_date="2026-07-10"
        )

    async def test_uses_newest_snapshot_on_or_before_date(self):
        assert await get_composition_as_of("2026-07-05") == {1: ["p1"]}
        assert await get_composition_as_of("2026-07-10") == {1: ["p1", "p2"]}
        assert await get_composition_as_of("2026-07-31") == {1: ["p1", "p2"]}

    async def test_returns_empty_before_any_snapshot_exists(self):
        assert await get_composition_as_of("2026-06-30") == {}
        assert await get_snapshot_date_as_of("2026-06-30") is None

    async def test_snapshot_date_resolution(self):
        assert await get_snapshot_date_as_of("2026-07-09") == "2026-07-01"
        assert await get_snapshot_date_as_of("2026-07-10") == "2026-07-10"
