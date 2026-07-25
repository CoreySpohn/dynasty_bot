"""Tests for the scheduler cog's draft-date anchors and deadline resolution.

Focused on the path that decides *when* the rookie draft reminders fire.
That date used to live in deadlines.yaml and was written only by someone
remembering to run /sync_sleeper; when nobody did, the draft reminder and the
three deadlines chained to it silently never fired.

The cog is built with `object.__new__` rather than its constructor on purpose:
`__init__` starts two `tasks.loop`s and reads the real config files, none of
which these tests want.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

import cogs.scheduler as scheduler_module
from cogs.scheduler import SchedulerCog
from lib.nfl_calendar import ANCHOR_ROOKIE_DRAFT_END, ANCHOR_ROOKIE_DRAFT_START

TZ = pytz.timezone("America/New_York")


def draft_start_ms(dt: datetime) -> int:
    """Sleeper's millisecond epoch for a wall-clock time in league timezone."""
    return int(TZ.localize(dt).timestamp() * 1000)


def make_cog(bot, anchors=None, deadlines=None):
    cog = object.__new__(SchedulerCog)
    cog.bot = bot
    cog.timezone = TZ
    cog.nfl_anchors = anchors if anchors is not None else {}
    cog.deadlines_config = {"deadlines": deadlines or []}
    return cog


class TestDraftDates:
    """_draft_dates reads both dates out of one get_drafts call."""

    def bot_with_drafts(self, drafts):
        bot = MagicMock()
        bot.league_id = "123456789"
        bot.sleeper = MagicMock()
        bot.sleeper.get_drafts = AsyncMock(return_value=drafts)
        return bot

    async def test_scheduled_draft_has_a_start_but_no_end(self):
        """A pre_draft draft is dated but unfinished.

        This is the case that matters in July: the commissioner has set the
        date, nobody has picked, and the reminders still need to count down
        to it.
        """
        bot = self.bot_with_drafts([
            {
                "season": "2026",
                "status": "pre_draft",
                "start_time": draft_start_ms(datetime(2026, 8, 8, 23, 30)),
                "last_picked": None,
            }
        ])
        start, end = await make_cog(bot)._draft_dates(2026)

        assert start == date(2026, 8, 8)
        assert end is None

    async def test_start_uses_league_timezone_not_utc(self):
        """A late-evening draft must not roll into the next day.

        Aug 8 at 11:30pm Eastern is Aug 9 in UTC. Reading it as UTC would
        advertise the draft, and every reminder chained to it, a day late.
        """
        bot = self.bot_with_drafts([
            {
                "season": "2026",
                "status": "pre_draft",
                "start_time": draft_start_ms(datetime(2026, 8, 8, 23, 30)),
            }
        ])
        start, _ = await make_cog(bot)._draft_dates(2026)

        assert start == date(2026, 8, 8)

    async def test_completed_draft_ends_on_last_pick(self):
        """Owners get 24 hours a pick, so the draft ends days after it starts."""
        bot = self.bot_with_drafts([
            {
                "season": "2026",
                "status": "complete",
                "start_time": draft_start_ms(datetime(2026, 8, 8, 23, 30)),
                "last_picked": draft_start_ms(datetime(2026, 8, 21, 12, 0)),
            }
        ])
        start, end = await make_cog(bot)._draft_dates(2026)

        assert start == date(2026, 8, 8)
        assert end == date(2026, 8, 21)

    async def test_picks_the_draft_for_the_requested_season(self):
        """Sleeper keeps a draft per season; ordering is not a selector."""
        bot = self.bot_with_drafts([
            {
                "season": "2025",
                "status": "complete",
                "start_time": draft_start_ms(datetime(2025, 8, 9, 20, 0)),
                "last_picked": draft_start_ms(datetime(2025, 8, 20, 20, 0)),
            },
            {
                "season": "2026",
                "status": "pre_draft",
                "start_time": draft_start_ms(datetime(2026, 8, 8, 23, 30)),
            },
        ])
        start, end = await make_cog(bot)._draft_dates(2026)

        assert start == date(2026, 8, 8)
        assert end is None

    async def test_no_draft_for_the_season_is_not_an_error(self):
        bot = self.bot_with_drafts([{"season": "2025", "status": "complete"}])

        assert await make_cog(bot)._draft_dates(2026) == (None, None)

    async def test_sleeper_failure_degrades_to_unknown(self):
        bot = MagicMock()
        bot.league_id = "123456789"
        bot.sleeper = MagicMock()
        bot.sleeper.get_drafts = AsyncMock(side_effect=RuntimeError("502"))

        assert await make_cog(bot)._draft_dates(2026) == (None, None)


class TestSyncDraftAnchors:
    """The upkeep loop's cheap poll for the two draft dates."""

    def cog_for(self, drafts, anchors=None):
        bot = MagicMock()
        bot.league_id = "123456789"
        bot.sleeper = MagicMock()
        bot.sleeper.get_drafts = AsyncMock(return_value=drafts)
        return make_cog(bot, anchors=anchors)

    async def test_records_a_newly_scheduled_draft(self):
        cog = self.cog_for([
            {
                "season": "2026",
                "status": "pre_draft",
                "start_time": draft_start_ms(datetime(2026, 8, 8, 23, 30)),
            }
        ])

        with patch.object(scheduler_module, "save_anchors") as save:
            changed = await cog._sync_draft_anchors(2026)

        assert changed is True
        assert cog.nfl_anchors[ANCHOR_ROOKIE_DRAFT_START] == "2026-08-08"
        save.assert_called_once()

    async def test_unchanged_dates_write_nothing(self):
        """Twice a day forever - it must not rewrite the file each pass."""
        cog = self.cog_for(
            [
                {
                    "season": "2026",
                    "status": "pre_draft",
                    "start_time": draft_start_ms(datetime(2026, 8, 8, 23, 30)),
                }
            ],
            anchors={ANCHOR_ROOKIE_DRAFT_START: "2026-08-08"},
        )

        with patch.object(scheduler_module, "save_anchors") as save:
            changed = await cog._sync_draft_anchors(2026)

        assert changed is False
        save.assert_not_called()

    async def test_a_moved_draft_corrects_the_anchor(self):
        """The date moves to whatever weekend owners can make."""
        cog = self.cog_for(
            [
                {
                    "season": "2026",
                    "status": "pre_draft",
                    "start_time": draft_start_ms(datetime(2026, 8, 15, 20, 0)),
                }
            ],
            anchors={ANCHOR_ROOKIE_DRAFT_START: "2026-08-08"},
        )

        with patch.object(scheduler_module, "save_anchors"):
            assert await cog._sync_draft_anchors(2026) is True
        assert cog.nfl_anchors[ANCHOR_ROOKIE_DRAFT_START] == "2026-08-15"

    async def test_a_sleeper_outage_never_blanks_a_known_date(self):
        """Losing the API mid-countdown must not cancel the reminders."""
        bot = MagicMock()
        bot.league_id = "123456789"
        bot.sleeper = MagicMock()
        bot.sleeper.get_drafts = AsyncMock(side_effect=RuntimeError("502"))
        cog = make_cog(bot, anchors={ANCHOR_ROOKIE_DRAFT_START: "2026-08-08"})

        with patch.object(scheduler_module, "save_anchors") as save:
            assert await cog._sync_draft_anchors(2026) is False

        assert cog.nfl_anchors[ANCHOR_ROOKIE_DRAFT_START] == "2026-08-08"
        save.assert_not_called()

    async def test_records_completion_once_the_draft_finishes(self):
        cog = self.cog_for(
            [
                {
                    "season": "2026",
                    "status": "complete",
                    "start_time": draft_start_ms(datetime(2026, 8, 8, 23, 30)),
                    "last_picked": draft_start_ms(datetime(2026, 8, 21, 12, 0)),
                }
            ],
            anchors={ANCHOR_ROOKIE_DRAFT_START: "2026-08-08"},
        )

        with patch.object(scheduler_module, "save_anchors"):
            assert await cog._sync_draft_anchors(2026) is True
        assert cog.nfl_anchors[ANCHOR_ROOKIE_DRAFT_END] == "2026-08-21"


class TestOnlyDraftDatesMissing:
    """Which upkeep path runs: the cheap draft poll or a full re-sync."""

    def cog_with(self, anchors):
        return make_cog(MagicMock(), anchors=anchors)

    def test_true_while_the_start_is_unknown(self):
        cog = self.cog_with({
            "nfl_regular_season_start": "2026-09-09",
            "nfl_preseason_start": "2026-08-06",
        })

        assert cog._only_draft_dates_missing(2026) is True

    def test_true_while_the_draft_is_unfinished(self):
        cog = self.cog_with({
            "nfl_regular_season_start": "2026-09-09",
            "nfl_preseason_start": "2026-08-06",
            ANCHOR_ROOKIE_DRAFT_START: "2026-08-08",
        })

        assert cog._only_draft_dates_missing(2026) is True

    def test_false_once_both_dates_are_known(self):
        cog = self.cog_with({
            "nfl_regular_season_start": "2026-09-09",
            "nfl_preseason_start": "2026-08-06",
            ANCHOR_ROOKIE_DRAFT_START: "2026-08-08",
            ANCHOR_ROOKIE_DRAFT_END: "2026-08-21",
        })

        assert cog._only_draft_dates_missing(2026) is False

    def test_false_when_the_schedule_itself_is_stale(self):
        """A season rollover needs the full re-sync, not the draft shortcut."""
        cog = self.cog_with({
            "nfl_regular_season_start": "2025-09-04",
            "nfl_preseason_start": "2025-08-07",
        })

        assert cog._only_draft_dates_missing(2026) is False


class TestRookieDraftDeadlineResolution:
    """The payoff: reminders resolve off the anchor, with no hand-entered date."""

    DEADLINES = [
        {
            "id": "rookie_draft",
            "date": None,
            "relative_to": ANCHOR_ROOKIE_DRAFT_START,
            "offset_days": 0,
        },
        {
            "id": "draft_preview",
            "date": None,
            "relative_to": "rookie_draft",
            "offset_days": -7,
        },
        {
            "id": "faab_opens",
            "date": None,
            "relative_to": "rookie_draft",
            "offset_days": 2,
        },
    ]

    def cog_with_anchor(self, anchor_value):
        return make_cog(
            MagicMock(),
            anchors={ANCHOR_ROOKIE_DRAFT_START: anchor_value},
            deadlines=self.DEADLINES,
        )

    def resolve(self, cog, deadline_id):
        for deadline in cog.deadlines_config["deadlines"]:
            if deadline["id"] == deadline_id:
                return cog._resolve_deadline_date(deadline)
        raise AssertionError(f"no deadline {deadline_id}")

    def test_draft_resolves_from_the_anchor(self):
        cog = self.cog_with_anchor("2026-08-08")

        assert self.resolve(cog, "rookie_draft") == date(2026, 8, 8)

    @pytest.mark.parametrize(
        "deadline_id,expected",
        [("draft_preview", date(2026, 8, 1)), ("faab_opens", date(2026, 8, 10))],
    )
    def test_dependents_chain_off_it(self, deadline_id, expected):
        """These three were stranded whenever the draft date went unwritten."""
        cog = self.cog_with_anchor("2026-08-08")

        assert self.resolve(cog, deadline_id) == expected

    def test_unscheduled_draft_resolves_to_nothing(self):
        """No date yet is 'don't fire', not a crash or a bogus date."""
        cog = self.cog_with_anchor(None)

        assert self.resolve(cog, "rookie_draft") is None
        assert self.resolve(cog, "draft_preview") is None


class TestRecurrence:
    """Fixed-calendar deadlines, which nothing resolved before.

    Only 'weekly_in_season' had a handler, so a `recurring: yearly` entry fell
    through to its stored date - the date of its *last* occurrence. That made
    draft_house dead outright and gave rule_voting_ends a one-year life for
    whichever year somebody had typed in by hand.
    """

    def resolve(self, **deadline):
        cog = make_cog(MagicMock())
        deadline.setdefault("id", "test_deadline")
        return cog._resolve_recurrence(deadline)

    def test_yearly_lands_on_its_month_and_day(self):
        resolved = self.resolve(recurring="yearly", month=3, day=1)

        assert (resolved.month, resolved.day) == (3, 1)

    def test_yearly_is_never_in_the_past(self):
        """The whole point: a passed occurrence rolls to the next one."""
        today = datetime.now(TZ).date()
        resolved = self.resolve(recurring="yearly", month=3, day=1)

        assert resolved >= today
        assert resolved.year in (today.year, today.year + 1)

    def test_recurrence_beats_a_stale_stored_date(self):
        """rule_voting_ends carries both; the typed-in year must not win."""
        today = datetime.now(TZ).date()
        resolved = self.resolve(
            recurring="yearly", month=3, day=1, date="2026-03-01"
        )

        assert resolved >= today

    def test_month_and_day_can_come_from_the_stored_date(self):
        """rivalries_realignment configures its date and nothing else."""
        resolved = self.resolve(
            recurring="every_5_years", date="2025-02-01", base_year=2025
        )

        assert (resolved.month, resolved.day) == (2, 1)

    def test_five_year_cycle_stays_on_its_base_year(self):
        """2025 + 5n, never an off-cycle year."""
        today = datetime.now(TZ).date()
        resolved = self.resolve(
            recurring="every_5_years", date="2025-02-01", base_year=2025
        )

        assert (resolved.year - 2025) % 5 == 0
        assert resolved >= today

    def test_weekly_is_left_to_its_own_handler(self):
        assert self.resolve(recurring="weekly_in_season", day_of_week="thursday") is None

    def test_a_yearly_deadline_with_no_fixed_date_resolves_elsewhere(self):
        """rookie_draft was one of these before it anchored itself."""
        assert self.resolve(recurring="yearly") is None

    def test_an_impossible_date_is_reported_not_crashed(self):
        assert self.resolve(recurring="yearly", month=2, day=30) is None

    def test_non_recurring_deadlines_are_untouched(self):
        assert self.resolve(date="2026-08-08") is None
