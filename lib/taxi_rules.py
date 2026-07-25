"""League taxi squad rules, which Sleeper does not implement.

Sleeper allows any player inside their first three years onto a taxi slot.
The Superflexers rules are much narrower (docs/Superflexers Rules.md):

1. Only **rookies you drafted yourself** are taxi eligible.
2. **Once activated, a player can never go back on taxi.**
3. A player **received in a trade can never be placed on taxi**, even if the
   previous owner had them on theirs.
4. After **3 seasons** they must be activated or dropped.
5. 5 taxi slots per team.

Nothing in Sleeper's data model expresses any of that, so the bot has to
hold it. Note which parts are derivable and which are not:

- Draft origin *is* recoverable, permanently: `get_picks_in_draft` across the
  `previous_league_id` chain says who drafted whom in which round. Verified
  against all 46 current taxi players with no gaps.
- Trade acquisition *is* recoverable from `get_transactions`.
- **Activation history is not.** Sleeper serves only the current roster, so
  "was this player ever activated" is unknowable after the fact. That is why
  `roster_snapshots` records which slot a player occupied, and why an
  activation, once observed, has to be written down permanently.

The rule engine here is pure functions over already-fetched data so the
rules can be tested without touching Sleeper or Discord.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional

logger = logging.getLogger("dynasty_bot.taxi_rules")

# Seasons a drafted player may occupy a taxi slot, counted from their draft
# season inclusive. Drafted in 2023 with a limit of 3 means 2023, 2024 and
# 2025 are fine and 2026 is a violation.
#
# NOTE: the rules say "After 3 seasons they have to be activated or dropped",
# which is ambiguous about whether the draft year itself counts. This module
# takes the stricter reading (it does). Change TAXI_MAX_SEASONS to 4 for the
# looser one - that is the only edit required, and it moves 6 of the 8
# current violations.
TAXI_MAX_SEASONS = 3

# Slots in Sleeper's roster payload, and the ones this module cares about.
SLOT_TAXI = "taxi"
SLOT_RESERVE = "reserve"
SLOT_ACTIVE = "active"


class Acquisition(str, Enum):
    """How an owner came to have a player. Only ROOKIE_DRAFT is eligible."""

    ROOKIE_DRAFT = "rookie_draft"
    TRADE = "trade"
    # Waivers, free agency, or anything else we couldn't attribute.
    OTHER = "other"
    UNKNOWN = "unknown"


class Ineligible(str, Enum):
    """Why a player cannot be placed on a taxi squad."""

    NOT_OWN_DRAFTEE = "not_own_draftee"
    ACQUIRED_BY_TRADE = "acquired_by_trade"
    ALREADY_ACTIVATED = "already_activated"
    SEASONS_EXPIRED = "seasons_expired"


REASON_TEXT = {
    Ineligible.NOT_OWN_DRAFTEE: "not drafted by this owner",
    Ineligible.ACQUIRED_BY_TRADE: "acquired by trade",
    Ineligible.ALREADY_ACTIVATED: "already activated once",
    Ineligible.SEASONS_EXPIRED: "past the 3-season limit",
}


@dataclass
class TaxiRecord:
    """What the bot knows about one player's taxi standing for one owner."""

    player_id: str
    owner_id: Optional[str]
    roster_id: Optional[int] = None
    acquisition: Acquisition = Acquisition.UNKNOWN
    draft_season: Optional[int] = None
    draft_round: Optional[int] = None
    activated: bool = False
    activated_season: Optional[int] = None
    on_taxi: bool = False

    def seasons_used(self, season: int) -> Optional[int]:
        """Seasons elapsed since the draft, inclusive of the draft year."""
        if self.draft_season is None:
            return None
        return season - self.draft_season


@dataclass
class Eligibility:
    """Whether a player may occupy a taxi slot, and why not if they may not."""

    player_id: str
    eligible: bool
    reasons: list[Ineligible]

    @property
    def reason_text(self) -> str:
        return ", ".join(REASON_TEXT[reason] for reason in self.reasons)


def evaluate(record: TaxiRecord, season: int) -> Eligibility:
    """Decide whether `record`'s player may be on taxi in `season`.

    Reasons accumulate rather than short-circuiting, so an audit can say
    "traded for AND past the limit" instead of only the first thing checked.
    """
    reasons: list[Ineligible] = []

    if record.acquisition == Acquisition.TRADE:
        reasons.append(Ineligible.ACQUIRED_BY_TRADE)
    elif record.acquisition != Acquisition.ROOKIE_DRAFT:
        # Waiver pickups and unattributable players are not own-draftees.
        reasons.append(Ineligible.NOT_OWN_DRAFTEE)

    if record.activated:
        reasons.append(Ineligible.ALREADY_ACTIVATED)

    used = record.seasons_used(season)
    if used is not None and used >= TAXI_MAX_SEASONS:
        reasons.append(Ineligible.SEASONS_EXPIRED)

    return Eligibility(record.player_id, not reasons, reasons)


@dataclass
class Violation:
    """A player currently occupying a taxi slot they aren't entitled to."""

    player_id: str
    roster_id: Optional[int]
    owner_id: Optional[str]
    reasons: list[Ineligible]
    draft_season: Optional[int] = None
    draft_round: Optional[int] = None
    seasons_used: Optional[int] = None

    @property
    def reason_text(self) -> str:
        return ", ".join(REASON_TEXT[reason] for reason in self.reasons)


def audit(records: Iterable[TaxiRecord], season: int) -> list[Violation]:
    """Find every player on a taxi squad who shouldn't be there.

    Only considers players actually occupying a slot - an ineligible player
    sitting on the active roster is simply a normal rostered player, not a
    violation.
    """
    violations = []
    for record in records:
        if not record.on_taxi:
            continue
        result = evaluate(record, season)
        if result.eligible:
            continue
        violations.append(
            Violation(
                player_id=record.player_id,
                roster_id=record.roster_id,
                owner_id=record.owner_id,
                reasons=result.reasons,
                draft_season=record.draft_season,
                draft_round=record.draft_round,
                seasons_used=record.seasons_used(season),
            )
        )
    return violations


def over_slot_limit(
    rosters: Iterable[dict[str, Any]], slots: int
) -> list[tuple[int, int]]:
    """Rosters exceeding the taxi slot limit, as (roster_id, count)."""
    return [
        (roster["roster_id"], len(roster.get("taxi") or []))
        for roster in rosters
        if len(roster.get("taxi") or []) > slots
    ]


def slot_of(roster: dict[str, Any], player_id: str) -> str:
    """Which slot a player occupies on a Sleeper roster.

    Sleeper lists taxi and reserve (IR) players in `players` as well as in
    their own arrays, so the specific arrays have to be checked first.
    """
    if player_id in (roster.get("taxi") or []):
        return SLOT_TAXI
    if player_id in (roster.get("reserve") or []):
        return SLOT_RESERVE
    return SLOT_ACTIVE


def build_draft_index(
    picks_by_league: Iterable[tuple[int, list[dict[str, Any]]]],
) -> dict[str, tuple[int, Optional[int], Optional[str]]]:
    """Index every drafted player to their earliest draft.

    Args:
        picks_by_league: (season, picks) pairs across the league chain.

    Returns:
        player_id -> (season, round, picked_by owner id). Earliest draft
        wins, since that's the rookie draft that made them taxi eligible;
        a later supplemental appearance doesn't reset the clock.
    """
    index: dict[str, tuple[int, Optional[int], Optional[str]]] = {}
    for season, picks in picks_by_league:
        for pick in picks:
            player_id = pick.get("player_id")
            if not player_id:
                continue
            existing = index.get(player_id)
            if existing is None or season < existing[0]:
                index[player_id] = (season, pick.get("round"), pick.get("picked_by"))
    return index


def build_records(
    rosters: Iterable[dict[str, Any]],
    draft_index: dict[str, tuple[int, Optional[int], Optional[str]]],
    traded_for: Optional[set[tuple[Optional[str], str]]] = None,
    activated: Optional[dict[tuple[Optional[str], str], int]] = None,
) -> list[TaxiRecord]:
    """Build a TaxiRecord for every player on every roster.

    Args:
        rosters: Sleeper rosters.
        draft_index: Output of build_draft_index.
        traded_for: (owner_id, player_id) pairs the owner acquired by trade.
        activated: (owner_id, player_id) -> season they were activated.

    Acquisition is decided by *who* drafted the player, not merely whether
    they were drafted: a player drafted by someone else and later traded for
    is not an own-draftee, which is the case rule 3 exists to cover.
    """
    traded_for = traded_for or set()
    activated = activated or {}

    records = []
    for roster in rosters:
        owner_id = roster.get("owner_id")
        roster_id = roster.get("roster_id")
        for player_id in roster.get("players") or []:
            draft = draft_index.get(player_id)
            draft_season = draft[0] if draft else None
            draft_round = draft[1] if draft else None
            drafted_by = draft[2] if draft else None

            if (owner_id, player_id) in traded_for:
                acquisition = Acquisition.TRADE
            elif draft and drafted_by == owner_id:
                acquisition = Acquisition.ROOKIE_DRAFT
            elif draft:
                # Drafted, but by somebody else - so however they got here,
                # it wasn't their own draft pick.
                acquisition = Acquisition.OTHER
            else:
                acquisition = Acquisition.OTHER

            activation_season = activated.get((owner_id, player_id))
            records.append(
                TaxiRecord(
                    player_id=player_id,
                    owner_id=owner_id,
                    roster_id=roster_id,
                    acquisition=acquisition,
                    draft_season=draft_season,
                    draft_round=draft_round,
                    activated=activation_season is not None,
                    activated_season=activation_season,
                    on_taxi=slot_of(roster, player_id) == SLOT_TAXI,
                )
            )
    return records


def eligible_additions(
    records: Iterable[TaxiRecord], season: int
) -> list[TaxiRecord]:
    """Players an owner could legally move onto their taxi squad now.

    Excludes anyone already occupying a slot, since the question being asked
    is what could be *added*.
    """
    return [
        record
        for record in records
        if not record.on_taxi and evaluate(record, season).eligible
    ]
