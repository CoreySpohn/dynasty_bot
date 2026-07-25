"""Roster composition history.

Sleeper only exposes *current* rosters, so who owned whom on a past date is
unrecoverable once that date passes. That makes this different from matchup
results, which Sleeper keeps forever per league (and chains backwards via
`previous_league_id`) and which we therefore compute live rather than
duplicate into our own tables.

Pricing a team - or a trade - as of some earlier date needs the roster as it
stood then, so we snapshot composition daily alongside the KeepTradeCut
value sync. Together those two dated tables are what make value history
mean anything: `ktc_values` says what a player was worth on a date,
`roster_snapshots` says who owned them.

A snapshot is the set of rows sharing a `recorded_date`. Days where nothing
changed are skipped entirely, so "the roster on date D" means "the newest
snapshot on or before D" - the same as-of idiom `ktc_values` already uses
via `get_value_as_of`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from database import db

logger = logging.getLogger("dynasty_bot.roster_history")


def _composition(rosters: list[dict[str, Any]]) -> dict[int, frozenset[str]]:
    """Reduce Sleeper rosters to {roster_id: frozenset(player_ids)}.

    Uses the `players` list, which covers starters, bench, taxi and IR.
    """
    return {
        roster["roster_id"]: frozenset(roster.get("players") or [])
        for roster in rosters
        if roster.get("roster_id") is not None
    }


async def get_snapshot_dates() -> list[str]:
    """Every date we hold a roster snapshot for, newest first."""
    async with db.execute(
        "SELECT DISTINCT recorded_date FROM roster_snapshots "
        "ORDER BY recorded_date DESC"
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_snapshot_date_as_of(cutoff_date: str) -> Optional[str]:
    """Date of the newest snapshot on or before `cutoff_date`, if any."""
    async with db.execute(
        "SELECT MAX(recorded_date) FROM roster_snapshots WHERE recorded_date <= ?",
        (cutoff_date,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def get_composition_as_of(cutoff_date: str) -> dict[int, list[str]]:
    """Roster composition from the newest snapshot on or before a date.

    Args:
        cutoff_date: ISO date (YYYY-MM-DD).

    Returns:
        {roster_id: [player_id, ...]}, empty if we have no snapshot that
        old - callers should treat empty as "no history yet" rather than
        "everyone had an empty roster".
    """
    snapshot_date = await get_snapshot_date_as_of(cutoff_date)
    if not snapshot_date:
        return {}

    async with db.execute(
        "SELECT roster_id, player_id FROM roster_snapshots WHERE recorded_date = ?",
        (snapshot_date,),
    ) as cursor:
        rows = await cursor.fetchall()

    composition: dict[int, list[str]] = {}
    for roster_id, player_id in rows:
        composition.setdefault(roster_id, []).append(player_id)
    return composition


async def get_owner_map_as_of(cutoff_date: str) -> dict[int, Optional[str]]:
    """{roster_id: owner_id} as recorded in the newest snapshot on or before
    a date, so historical teams can be labelled with who owned them then."""
    snapshot_date = await get_snapshot_date_as_of(cutoff_date)
    if not snapshot_date:
        return {}

    async with db.execute(
        "SELECT DISTINCT roster_id, owner_id FROM roster_snapshots "
        "WHERE recorded_date = ?",
        (snapshot_date,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {roster_id: owner_id for roster_id, owner_id in rows}


async def store_snapshot(
    rosters: list[dict[str, Any]], recorded_date: Optional[str] = None
) -> bool:
    """Persist roster composition for a date, skipping unchanged days.

    Args:
        rosters: Sleeper roster dicts.
        recorded_date: ISO date to record under; defaults to today.

    Returns:
        True if a snapshot was written, False if it was skipped because
        composition is identical to the most recent stored snapshot (or
        because `rosters` was empty, e.g. a failed Sleeper fetch - never
        record that as "everyone dropped everyone").
    """
    composition = _composition(rosters)
    if not composition or not any(composition.values()):
        logger.warning("Refusing to store an empty roster snapshot")
        return False

    recorded_date = recorded_date or datetime.now().date().isoformat()

    latest_date = await get_snapshot_date_as_of(recorded_date)
    if latest_date:
        previous = await get_composition_as_of(latest_date)
        if {rid: frozenset(pids) for rid, pids in previous.items()} == composition:
            logger.info(
                f"Roster composition unchanged since {latest_date}; "
                "skipping snapshot"
            )
            return False

    # Replace any rows already recorded for this date so a re-run (manual
    # sync, restart) updates the day rather than duplicating it.
    async with db.execute(
        "DELETE FROM roster_snapshots WHERE recorded_date = ?", (recorded_date,)
    ):
        pass

    owners = {
        roster["roster_id"]: roster.get("owner_id")
        for roster in rosters
        if roster.get("roster_id") is not None
    }
    rows = [
        (recorded_date, roster_id, owners.get(roster_id), player_id)
        for roster_id, player_ids in composition.items()
        for player_id in sorted(player_ids)
    ]
    for row in rows:
        async with db.execute(
            """
            INSERT INTO roster_snapshots
                (recorded_date, roster_id, owner_id, player_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(recorded_date, roster_id, player_id) DO NOTHING
            """,
            row,
        ):
            pass

    logger.info(
        f"Stored roster snapshot for {recorded_date}: "
        f"{len(rows)} player-rows across {len(composition)} rosters"
    )
    return True
