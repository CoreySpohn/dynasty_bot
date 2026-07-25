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
- **CI** — `uv run pytest` on every push and PR

---

## 📊 Advanced Analytics

### Weekly results derivation layer
The prerequisite for most of the section below, and it's a **module, not a
table**. `get_matchups(league_id, week)` already returns `starters`,
`players`, and `players_points` per team — everything needed for optimal
lineup, bench points, and margins. `cogs/analytics.py` reads
`players_points` for power rankings and throws the intermediate results
away; `calculate_optimal_lineup` is right there too.

Build `lib/results.py` returning computed per-team weekly results (score,
opponent, W/L, optimal points, bench points), chaining `previous_league_id`
for prior seasons. Have `/rankings` use it instead of its own loop. If
latency bites, add an in-process TTL cache — finalized weeks are immutable,
so caching is trivially safe.

### Luck Index
Points Against vs league average, close wins/losses, optimal lineup vs
actual. All computable from the results layer.

### Playoff Scenarios
Auto-calculated clinching/elimination scenarios each week.
- "Noah needs to win AND have Fuzzy lose to clinch playoffs"

### Historical Matchups
All-time head-to-head records — a `GROUP BY` over the results layer once it
chains prior seasons.
- "Corey is 12-5 all-time vs Fuzzy"

### "What If" Machine
`/whatif @player trade` — simulates how your season would've gone with
different roster decisions.

---

## 🏆 Awards & Recognition

### Weekly Awards
Auto-posts after each week. Cheap once the results layer exists; needs one
small table for "already posted this award" idempotency (follow the existing
`reminder_history` pattern rather than inventing a new one).
- 👑 **Highest Scorer**
- 😱 **Biggest Upset**
- 📈 **Best Bench** (most points left on bench)
- 💔 **Worst Beat** (highest-scoring loser)

### Shame Wall
Worst starts/sits of the week — optimal-lineup gap and BYE/injured starters,
straight off the results layer.
- "DaFuzz started a player on BYE! 🤦"
- "Rob Jr. left 40 points on the bench!"

### Sacko Watch
Dramatic countdown tracking who's in danger of last place.

### Championship Ring Counter
Historical champions with emoji rings — derivable from playoff brackets
across the `previous_league_id` chain.
- 🏆🏆🏆 Fuzzy (3x Champion)

---

## 🎰 Fun & Games

### Trash Talk Generator
`/trashtalk @opponent` — AI-powered smack talk for your weekly matchup. The
reporter-persona and failover machinery already exists, so this is mostly
prompt work. Good quick win.

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

---

## Priority Guide

Timing matters more than complexity here: anything in-season has to land
before NFL Week 1 or it sits idle for a year.

| Priority | Feature | Complexity | Deadline |
|----------|---------|------------|----------|
| 🔴 High | Weekly results derivation layer | Medium | Before Week 1 |
| 🔴 High | Weekly Awards | Low (after layer) | Before Week 1 |
| 🔴 High | Shame Wall | Low (after layer) | Before Week 1 |
| 🔴 High | Luck Index | Low (after layer) | Before Week 1 |
| 🟡 Medium | Trash Talk Generator | Low | Any time |
| 🟡 Medium | Historical Matchups | Low (after layer) | Any time |
| 🟡 Medium | Playoff Scenarios | Medium | Before playoffs |
| 🟡 Medium | Weekly Predictions | Medium | Before Week 1 |
| 🟢 Low | Championship Rings | Low | Any time |
| 🟢 Low | Trade Regret Tracker | Low (needs history depth) | Blocked ~months |
| 🟢 Low | Weekly Newsletter | Medium | After awards |
| 🟢 Low | Survivor Pool | High | Before Week 1 |

---

*Last updated: July 2026*
