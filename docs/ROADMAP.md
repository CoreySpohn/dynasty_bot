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

---

## 📊 Advanced Analytics

### Playoff Scenarios
Auto-calculated clinching/elimination scenarios each week.
- "Noah needs to win AND have Fuzzy lose to clinch playoffs"

### "What If" Machine
`/whatif @player trade` — simulates how your season would've gone with
different roster decisions.

---

## 🏆 Awards & Recognition

### Sacko Watch
Dramatic countdown tracking who's in danger of last place. Straightforward
on top of the results layer; mostly a question of how mean to be.

---

## 🎰 Fun & Games

### Weekly Predictions
Bot predicts each matchup winner with confidence %, tracks accuracy over the
season. **Needs a real table** — the bot's own predictions exist nowhere
else, so unlike the rest of this section they can't be recomputed.

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
- **`config/league_state.yaml` drifts.** It's manual, and a stale
  `current_state` silently gates the wrong set of deadline reminders.
- **Zero-point starters aren't provably byes.** The shame wall reports
  "started someone who scored nothing", which is honest, rather than
  claiming BYE — Sleeper's player payload doesn't carry bye weeks reliably.
- **Awards and the shame wall have never run on live data.** Everything is
  unit-tested against synthetic matchups, but the 2026 season hasn't started,
  so week 1 is the first real exercise. Expect to tune wording.
- **`/h2h` and `/rings` are API-heavy.** They walk every chained season.
  Fine for occasional use; if they get called often, cache per season the way
  `lib/results.py` caches completed weeks.

---

## Priority Guide

Timing matters more than complexity here: anything in-season has to land
before NFL Week 1 or it sits idle for a year.

| Priority | Feature | Complexity | Deadline |
|----------|---------|------------|----------|
| 🔴 High | Weekly Predictions | Medium | Before Week 1 |
| 🟡 Medium | Playoff Scenarios | Medium | Before playoffs |
| 🟡 Medium | Sacko Watch | Low (after layer) | Before playoffs |
| 🟡 Medium | Weekly Newsletter | Medium | Any time |
| 🟢 Low | Injury Roasts | Low | Any time |
| 🟢 Low | Player Birthday Alerts | Low | Any time |
| 🟢 Low | Trade Regret Tracker | Low (needs history depth) | Blocked ~months |
| 🟢 Low | "What If" Machine | High | Any time |
| 🟢 Low | Survivor Pool | High | Before Week 1 |
| 🟢 Low | Second value source (FantasyCalc) | Medium | Any time |

---

*Last updated: July 2026*
