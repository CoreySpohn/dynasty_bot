# NathanPeterman Roadmap 🏈

Feature ideas for the Dynasty Fantasy Football Discord Bot.

---

## Guiding principle: persist only what upstream won't give back

Worth stating up front, because it decides how most features below get built.

Sleeper keeps league history **forever** — matchups, per-team scores,
`starters`, `players_points`, transactions, playoff brackets — and prior
seasons chain backwards through `previous_league_id`. Anything derivable from
that should be **computed live**, not copied into our own tables. Copying it
buys a second source of truth to keep in sync, a backfill to get right, and
staleness bugs, in exchange for nothing. `power_rankings` is the cautionary
tale: it's a stored derivation of Sleeper data and it has sat empty.

The opposite is true for KeepTradeCut (no API, no history) and roster
composition (Sleeper serves *current* rosters only). Miss a day there and
it's gone permanently, which is why both are snapshotted daily.

So when a feature below wants "history," ask which kind it needs. Most want
a shared **derivation layer**, not a table.

---

## ✅ Shipped

- **Auto-rumor generation** — reworked and re-enabled: grounded in real
  events, table-driven seeds, weighted context modules
- **Custom reporters + prompts** — `/rumor` and `/randomrumor` take a custom
  personality, parsed into name/emoji/style
- **AI backend rotation + failover** — rotates Gemini/Claude/OpenAI for
  stylistic variety and fails over when one is rate limited or down.
  Backends used to swallow their own errors and return placeholder text
  (`*X hears whispers*: ...`), so an outage was indistinguishable from a
  successful rewrite and nothing ever failed over
- **Dynasty value tracking** — daily KTC sync into dated `ktc_values`
  snapshots, with `/tradevalue`, `/teamvalues`, `/valuemovers`
- **Team dynasty value in power rankings** — 15% of the `/rankings` Power
  Level formula
- **Rumor flavor from value trends** — one weighted context module per
  rumor, chained onto the owner/player the rumor is actually about
- **Picks in `/tradecalc`** — KTC's `RDP` rows were already being synced;
  loose phrasings (`2027 1st`, `27 2nd round pick`) now resolve onto them,
  defaulting to the Mid tier
- **Roster composition snapshots** — `roster_snapshots`, written daily
  alongside the KTC sync, skipping unchanged days
- **Team value over time including churn** — `/valuehistory` prices each
  team against the roster it *actually had* N days ago, so trades count.
  (`/valuemovers` remains the pure-market-movement view)
- **Trade grading** — `/tradegrades` prices both sides of a completed trade
  at trade-date values, plus how each side has aged since
- **CI** — `uv run pytest` on every push and PR (workflow file needs a token
  with `workflow` scope; verification is local for now)
- **Weekly results derivation layer** — `lib/results.py`, the single place
  Sleeper matchups become per-team weekly results. `/rankings` reads it
  instead of its own loop; `calculate_optimal_lineup` and `FLEX_POSITIONS`
  moved here and are re-exported from `cogs/analytics.py` for existing
  importers. Completed weeks cached in-process, current week never
- **Weekly Awards** — `/awards`, auto-posting once per completed week.
  "Biggest upset" uses season-to-date averages as the expectation, since the
  bot makes no predictions
- **Shame Wall** — `/shamewall`: losses the optimal lineup would have won,
  points left on the bench, zero-point starters
- **Luck Index** — `/luckindex`, built on all-play expected wins. Lineup
  efficiency is reported separately, because leaving points on the bench is
  a decision, not luck
- **Trash Talk** — `/trashtalk @owner`, grounded in that owner's real record,
  bench waste, luck score and dynasty value rank
- **Historical Matchups** — `/h2h`, attributed by owner rather than roster so
  a league renewal doesn't credit the wrong person
- **Championship Rings** — `/rings`, read from Sleeper's winners bracket
  rather than guessed from a championship-week matchup id
- **Projection model** — `lib/projections.py`: per-team scoring
  distributions shrunk toward the league mean (dynasty value as the week-1
  prior), pairwise win probability, and a Monte Carlo season simulation.
  One model behind predictions, playoff odds and sacko watch, so they can't
  disagree
- **Weekly Predictions** — `/predictions` and `/predictionrecord`, recorded
  before kickoff and never rewritten (`INSERT OR IGNORE`, not upsert), graded
  automatically once a week finishes. Backtested at 46/78 (59.0%) on 2025
- **Playoff Scenarios** — `/playoffodds`, with clinched/eliminated proved by
  exhaustive enumeration rather than inferred from simulation
- **Sacko Watch** — `/sacko`, last-place odds off the same simulation
- **Taxi squad rule tracking** — `lib/taxi_rules.py` plus `/taxiaudit`,
  `/taxieligible` and an admin `/taxibackfill`. Sleeper enforces none of the
  league's taxi rules, so the bot holds them. Draft origin is derived from
  the draft endpoints (verified complete for all 46 current taxi players);
  activation history can't be, so `roster_snapshots` now records slots and
  `taxi_ledger` keeps observed activations. Two rules the commissioner's
  Discord ruling settled (quoted in `docs/Superflexers Rules.md`): the
  addition window is **one off-season wide**, so only that year's own draftees
  may be *added* even though earlier ones may *stay*; and the draft year
  counts toward the 3-season limit. `upcoming_season` picks the season to
  judge against, since a completed Sleeper league keeps reporting the season
  that just ended until the next one is created

---

## 📊 Advanced Analytics

### "What If" Machine
`/whatif @player trade` — simulates how your season would've gone with
different roster decisions.

---

## 🎰 Fun & Games

### Trade Regret Tracker
Revisit old trades after N weeks and declare a winner. `/tradegrades`
already does then-vs-now, so this is largely a scheduled wrapper around it.
Blocked in practice by history depth, not code: KTC snapshots only start
2026-07-22 and can't be backfilled, so meaningful verdicts need months of
accumulation.

### Fantasy Roulette
Random start/sit advice when you can't decide (with a disclaimer!).

---

## 🎭 Social & Engagement

### Player Birthday Alerts
"🎂 Happy Birthday to YOUR player, Patrick Mahomes!" — Sleeper's player data
carries birthdates.

### Injury Roasts
When your star gets hurt, bot sends condolences in a reporter's voice.
- "BREAKING: Sources say Corey is 'devastated' after losing Ja'Marr Chase"

---

## 🎲 Mini-Games

### Survivor Pool
Weekly pick'em — pick one NFL team to win, can't reuse teams.

### League Prop Bets
Polls/bets on league events, distinct from the NFL-based props in Kohl's
Cash.
- "Will Corey trade his 1st round pick before the deadline?"

### Caption Contest
Bot posts a meme, league votes on best caption.

---

## 🔧 Utility Features

### Proper tagging on Discord
Make sure alerts (taxi raids, lineup alerts) tag the right users, and that
the bot follows up when a deadline is approaching or missed. Taxi raiding
does this; the rest is uneven.

### Waiver Wire Alerts
Notify when specific players are dropped to waivers.

### Draft Recap
Auto-generate draft grades and hot takes after rookie drafts. Season-bound —
misses its window if not ready before the rookie draft.

### Weekly Newsletter
Auto-generated league newsletter with recaps, standings, and drama. Best
built last, on top of the results layer and Weekly Awards.

### Second value source (FantasyCalc)
Cross-check KTC values. Would also give pick tiers a sanity check.

---

## Open follow-ups on shipped work

- **Pick tiers are assumed, not known.** Sleeper records a traded pick as
  season + round; KTC prices Early/Mid/Late separately, and which one a
  future pick becomes isn't knowable until that season's standings exist.
  Both `/tradecalc` and `/tradegrades` price untiered picks as **Mid**.
  Refine from standings once a season is underway.
- **Owner-qualified picks don't resolve.** "Corey's 2027 1st" deliberately
  falls through rather than being mispriced, since pricing it needs to know
  whose pick it is.
- **Rumor entity extraction is regex/substring based** (first name for
  owners, last name for players), not real NLP — fine for flavor, not
  bulletproof against short or common names.
- **`config/league_state.yaml`'s `pre_draft` state is still manual.** The other
  three are now derived and applied by `SchedulerCog.upkeep_loop` (see
  `lib/league_state.py`), and NFL anchors re-sync themselves. `pre_draft`
  can't be: the config describes it as "after rules voted", and a rules vote
  has no API. It also isn't in `VALID_STATES`, so nothing sets it today.
  The `transitions` block is likewise still `null` and unread by anything —
  the derivation uses live signals instead.
- **Zero-point starters aren't provably byes.** The shame wall reports
  "started someone who scored nothing", which is honest, rather than
  claiming BYE — Sleeper's player payload doesn't carry bye weeks reliably.
- **Awards and the shame wall have never run on live data.** Everything is
  unit-tested against synthetic matchups, but the 2026 season hasn't started,
  so week 1 is the first real exercise. Expect to tune wording.
- **`/h2h` and `/rings` are API-heavy.** They walk every chained season.
  Fine for occasional use; if they get called often, cache per season the way
  `lib/results.py` caches completed weeks.
- **Rumor cadence is ~2.1/week against a 2.0 target.** The minimum-spacing
  dead time is corrected for only to first order, since how much of a
  spacing window overlaps the quiet hours depends on when the last rumor
  posted. Pinned by a simulation test; not worth solving exactly.
- **The bot needs Send Messages on the announcements channel.** The recap
  auto-post got `403 Missing Permissions` on its first run. Recaps fall back
  to `ALERT_CHANNEL_ID`, but the intended target is `#announcements`.
- **Predictions are only ~59% accurate, on one season of backtest.** Mean
  stated confidence was 61.8% against 59.0% actual, so the model is mildly
  overconfident, and 46/78 is p~0.06 one-sided against a coin flip. Left
  untuned on purpose: fitting the shrinkage constants to 78 games would be
  overfitting. Revisit once `predictions` has two real seasons in it.
- **`/playoffodds` reports odds, not scenarios.** It doesn't yet say "Noah
  needs to win AND have Fuzzy lose" - the enumeration that proves
  clinched/eliminated could produce those sentences but doesn't.
- **The simulation ignores the playoff bracket itself.** Odds are for
  *making* the playoffs, not winning them.
- **The written taxi deadline contradicts the draft calendar, and needs a
  ruling.** The rules put it at "the end of the last game of the first week of
  NFL preseason games". That date is now fetched exactly (ESPN, via
  `/sync_nfl` — nflverse publishes no preseason games at all), but the rookie
  draft floats to whatever weekend owners can manage and then takes days to
  run, 24 hours per pick. So it has repeatedly finished *after* the deadline:

  | Season | Rookie draft ended | First full preseason week ended |
  |--------|--------------------|---------------------------------|
  | 2023   | Aug 18             | Aug 13                          |
  | 2024   | Aug 6              | Aug 11                          |
  | 2025   | Aug 19             | Aug 10                          |

  Two of the last three years the deadline had expired before anyone could
  draft, which cannot be what's enforced. `stored_taxi_deadline` therefore
  refuses to apply a deadline the draft has overtaken, falling back to
  `season_type` bracketing, and logs why. The real fix is a rules decision:
  most likely re-anchoring the deadline to the draft's completion rather than
  to the NFL preseason.
- **Taxi activations before 2026-07-25 are assumed, not observed.** Sleeper
  can't tell us who was activated historically, so `/taxibackfill` records
  every own-draftee from a *past* draft class who is currently off taxi as
  already activated. Conservative - they'd be ineligible either way - but it
  isn't evidence, and the `taxi_ledger` notes say so. The current draft class
  is deliberately exempt: they sit on the bench straight out of the draft, so
  presuming activation would close a slot they can still legally fill.
- **Trade-acquired players aren't auto-detected yet.** Rule 3 (a player
  received in a trade can never go on taxi) is implemented in the engine and
  read from `taxi_ledger`, but nothing yet walks `get_transactions` to
  populate it. Currently no violations depend on it, since every taxi player
  is their own owner's draftee.
- **Nothing enforces taxi rules at the moment of the move.** `/taxiaudit`
  reports after the fact; it doesn't stop an illegal Sleeper move.
- **Cadence and quiet hours aren't state-aware.** `RUMORS_PER_WEEK` is one
  constant, so it can't yet be busier in-season or around the trade deadline
  than it is in the dead of the offseason.

---

## Priority Guide

Timing matters more than complexity here: anything in-season has to land
before NFL Week 1 or it sits idle for a year.

| Priority | Feature | Complexity | Deadline |
|----------|---------|------------|----------|
| 🟡 Medium | Weekly Newsletter | Medium | Any time |
| 🟡 Medium | Per-matchup "needs to win" scenarios | Medium | Before playoffs |
| 🟢 Low | Injury Roasts | Low | Any time |
| 🟢 Low | Player Birthday Alerts | Low | Any time |
| 🟢 Low | Trade Regret Tracker | Low (needs history depth) | Blocked ~months |
| 🟢 Low | "What If" Machine | High | Any time |
| 🟢 Low | Survivor Pool | High | Before Week 1 |
| 🟢 Low | Second value source (FantasyCalc) | Medium | Any time |

---

*Last updated: July 2026*
