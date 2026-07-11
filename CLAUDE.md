# Fantrax Discord Bot — Project Context

Discord bot for a 10-team dynasty fantasy basketball league (Quail Hill
Invitational) on Fantrax, using discord.py cogs + the unofficial
`fantraxapi` Python wrapper.

## ⚠️ Season-start checklist (season begins 2026-10-20)

Everything below was built/modified pre-season and could only be verified
against synthetic or mocked data — real verification was impossible before
real games exist. `DRY_RUN = True` on both affected cogs specifically so
nothing posts for real until each item below is checked off by hand. Full
reasoning for every "why" here lives in the "Media ecosystem" section
further down — this is just the actionable summary.

- [ ] **Day 1 (Oct 20) or as soon as real per-day data appears**: run
      `/rankingsdebug` (non-synthetic) and confirm:
  - `SCHEDULE_FULL` is actually returning real day-by-day columns (not
    still falling back to season-totals — gotcha #12)
  - Gap-based tier boundaries (`👑 Favorites` / `🏆 Contenders` / `⚔️ In
    the Hunt` / bottom tier) look like reasonable groupings of the real
    `points_per_day` spread, not a degenerate split
  - `config.lastPlaceTierName` still reflects this season's actual
    last-place punishment (update it if not — it's a config value, no
    code change needed)
  - Injury/suspension exclusion is correctly dropping any real
    `day_to_day`/`out`/`injured_reserve`/`suspended` player from the
    simulation (may need to wait for a real case to actually observe)
- [ ] **Oct 21 (morning after Day 1's games) or the first real
      `/topperformerdebug` (non-synthetic) run**: confirm
  - Requesting a specific date via the raw `getLiveScoringStats` bypass
    actually returns THAT date's data (this was the open, unverified
    question the whole daily-top-performer feature was built to answer)
  - The non-`ACTIVE` status bucket(s) parse correctly with real bench-
    player data (only ever tested against a fabricated response — see
    the bug-fix note in "Media ecosystem" below)
  - The embed reads correctly with a real player/team/score
- [ ] **Second real Wednesday post (one full week-over-week cycle after
      the first real post)**: confirm player-trend narration and
      recent-form blending actually activate (both are silently inert on
      the very first real post — no prior `posted_player_stats.json` to
      diff against yet, by design) and that `RECENT_FORM_WEIGHT = 0.35`
      feels reasonable against real week-to-week swings
- [ ] Once the power-rankings items above all check out: flip
      `DRY_RUN = False` in `cogs/powerrankings.py`
- [ ] Once the daily-top-performer items above check out: flip
      `DRY_RUN = False` in `cogs/dailytopperformer.py`
- [x] ~~First real trade... flip DRY_RUN~~ — **done 2026-07-11, ahead of
      season start.** Unlike power rankings/daily top performer, trade
      grades had no hard technical blocker — `lineup_ceiling()` only
      needs STATS-view data, which already falls back to real last-
      season stats pre-season. `DRY_RUN = False` in `cogs/tradegrades.py`
      now; `posted_trade_grades.json` was pre-seeded with this league's
      3 older trades (`3rgg2qblmr032wol`, `fxlczs0fmr59gwn6`,
      `ifz6mxonmrcr9744`) so the next real 10am-Pacific check grades only
      the 2 most recent (`rwsmwi3amrd643yj` — Homoerotic Knights/Horny
      Mushrooms, the Kawhi/AD-for-picks-and-youth trade — and
      `m6dg2647mrcs6521` — Goat James/Homoerotic Knights), not a 5-trade
      backlog dump. Still worth eyeballing the first couple of real posts
      once they land: does `config.espnChannelId` feel right for this
      content, does the narrative read well using last-season stats
      rather than current-season ones.
- [ ] Also worth a one-time check regardless of season timing:
      `config.mediaChannelId` no longer exists (consolidated into
      `config.espnChannelId` on 2026-07-11) — if you're reading old notes
      or an old branch that still references `mediaChannelId`, that's
      stale, not a bug to fix.

## Structure

```
main.py              — bot entrypoint, loads all cogs from cogs/
config.py             — plain module-level config (botToken, clientID,
                         myGuild, leagueId, conchResponses, teamRoleIds,
                         transactionChannelId). No dotenv, no env vars —
                         just hardcoded values imported as config.xyz.
cogs/commands.py      — misc slash commands (/askshams only — /scoreboard
                         and /standings were removed, unfinished/broken
                         and limited use case). Stateless, no local file I/O.
cogs/transactionstracker.py — auto-posts adds/drops/waivers to #news as
                         Discord embeds. Local file state
                         (posted_transactions.json), polls every 5 min
                         plus a midnight-PST waiver-batch check.
cogs/tradestracker.py — auto-posts executed trades to the same #news
                         channel as embeds, separate cog from
                         transactionstracker.py for modularity. Local
                         file state (posted_trades.json), polls every
                         5 min.
```

Both trackers' debug slash commands (`/transactiondebug`, `/faabdebug`,
`/teamsdebug`, `/biddebug`, `/tradedebug`) are gated with
`@app_commands.default_permissions(manage_guild=True)` — Discord hides
them from the slash-command picker entirely for regular league members,
not just a runtime permission check.

Deployed via Docker Compose on a Synology NAS (`/docker/fantrax/`),
separate project from an existing arr-stack/Plex Compose stack on the
same NAS. Bot source is bind-mounted (not baked into the image), so
editing `.py` files + `docker compose restart fantrax-bot` picks up
changes — no rebuild needed unless `requirements.txt` changes.

## Fantrax league specifics

- League is set to **public** (Commissioner → League Setup → Misc →
  "Allow public to view league"). This means `FantraxAPI(config.leagueId)`
  works with **no login/session** for most read endpoints
  (`transactions()`, `standings()`, `teams`, etc.) — being public only
  exposes read-only viewing, it does NOT let outsiders join, claim, trade,
  or otherwise modify anything.
- **Exception:** `League.pending_trades()` (returns `Trade` objects)
  explicitly requires an authenticated session and raises `NotLoggedIn`
  otherwise — this is true regardless of the league's public/private
  setting. **We don't use this method** — the trade poster (see gotcha #9
  below) reads *executed* trades from the same public endpoint
  `transactions()` uses, under a different `view` param, which needs no
  login at all. `pending_trades()` remains true and undocumented-gotcha
  material for whoever eventually wants pre-approval trade previews, but
  it's not on the critical path for anything currently built.
- FAAB: $1,500 annual rollover cap, redistributed equally each year.
  `fantraxapi` does **not** expose a "FAAB remaining" field anywhere
  (checked `Team`, `League`, and `Standings`/`Record` objects directly
  via debug commands — none of them have it). Decided not to try to
  track/display a running balance ourselves given the non-trivial
  redistribution rules make a self-maintained tracker risky to get
  wrong silently.

## fantraxapi library gotchas (important — don't relitigate these)

1. **`Transaction.date` is broken for this league.** The library hardcodes
   `cells[1]` as the date cell (`Transaction.__init__` in
   `fantraxapi/objs/transaction.py`), but for every transaction in this
   league, `cells[1]` actually holds something else (a FAAB bid amount
   for add-type rows, or empty for others) — the real date is at
   `cells[3]`. We monkeypatch `fantraxapi.objs.transaction.datetime` with
   a tolerant parser (`_SafeDatetimeParser`) that catches any parse
   failure and returns a `SENTINEL_DATE` (1970-01-01) instead of
   crashing. **We don't fix/use the real date** — nothing in this bot's
   output needs a timestamp, so the sentinel is just "don't crash,"
   not "get the date right." If a future feature needs real dates,
   switch to reading `cells[3]` directly instead of trusting
   `Transaction.date`.
2. **FAAB bid amount lives at `cells[1]`** on `txn._data[0]` for
   add-type transactions (FA/WW/CLAIM) — confirmed via manual dump, not
   documented anywhere. Extracted via `_get_bid_amount()` static method
   in `transactionstracker.py`. Format is a plain decimal string like
   `'10.00'`.
3. **This league's transaction `type` values are `FA`, `WW`, `DROP`** —
   never `CLAIM`, despite that being the "obvious" name you'd expect for
   a bid-based waiver claim. `ADD_TYPES = {"FA", "WW", "CLAIM"}` covers
   all cases defensively but only FA/WW have actually been observed.
4. **`Team` objects have `.id`, not `.team_id`** (despite some older
   docs/forks showing `team_id` — that's from a different version of
   this library). Always match teams by `.id` (Fantrax's stable internal
   ID), never by `.name` — display names change on renames (e.g. "IHOP
   Dreams" was briefly "Lomboy Elite Dynasty"; "Two Balls and a Dream"
   currently displays as "Slaw Bunnies"). The full team.id → Discord
   role ID mapping lives in `config.teamRoleIds`.
5. **Discord does not parse `<@&ID>` mention syntax inside ``` code
   fences.** Live-posted messages must NOT be wrapped in a code block or
   role tags render as literal text instead of pinging. (Debug commands
   still use code blocks intentionally — that's for raw-data readability,
   not for mentions to render.)
6. **`python:slim` Docker images lack `tzdata`** — needed for
   `zoneinfo.ZoneInfo("America/Los_Angeles")` (used for the midnight-PST
   waiver-processing check). Installed via `apt-get install tzdata` in
   the Dockerfile.
7. **Synology DSM's kernel commonly lacks CFS bandwidth control** — a
   `cpus:`/`NanoCPUs` Docker Compose limit fails at container creation
   with `NanoCPUs can not be set...`. Only `mem_limit` is used, not a CPU
   limit, in `docker-compose.yml`.
8. **Python print() output is buffered by default in containers** — set
   `ENV PYTHONUNBUFFERED=1` in the Dockerfile or `docker logs` won't show
   output in real time (or possibly at all until buffer flushes).
9. **Executed trades are retrievable with NO login**, via the same public
   `getTransactionDetailsHistory` endpoint `League.transactions()` uses —
   just pass `view="TRADE"` instead of the library's hardcoded default
   (`CLAIM_DROP`). Confirmed by probing the raw response: the default
   view's `displayedLists.tabs` lists `{"name": "Trade", "id": "TRADE"}`
   alongside Claim/Drop. `League.transactions()` never exposes a `view`
   kwarg, so `cogs/tradestracker.py` bypasses it and calls
   `fantraxapi.api.Method`/`request` directly. The row shape under
   `view="TRADE"` is also different from Claim/Drop's and will crash the
   library's `Transaction()` parser if you try to feed it in — no
   `transactionCode` key at all; instead each row is one asset with `key:
   "from"`/`"to"` cells (both have `.teamId`, use that not `.content`,
   per gotcha #4) and one of three shapes:
   - player: `row["scorer"]["name"]`
   - draft pick: `row["draftPickDisplayParts"]` — `roundInfo`/`year` are
     HTML fragments (e.g. `"Round <b>1</b> (Horny Mushrooms)"`,
     `"<b>2029</b> Draft Pick"`), parsed via regex in
     `tradestracker.py`'s `PICK_ROUND_RE`/`PICK_YEAR_RE`.
   - FAAB cash throw-in: `row["budgetAmountTradeObj"]["budget"]` (e.g.
     `"$100.00"`) — has neither of the above, discovered when it crashed
     the initial player-only parser on a real trade.
   All rows sharing a `txSetId` belong to the same trade; rows come back
   newest-first, same as the Claim/Drop feed.
10. **Discord only parses `<@&ID>` mention syntax in an embed's
    `description` or field `value` — NOT in field `name`, `title`,
    `author.name`, or `footer.text`.** A role tag placed in a field name
    renders as literal `<@&ID>` text instead of the colored mention
    (discovered live in `#news` — see `tradestracker.py`'s
    `format_trade_embed`). Same underlying reason as gotcha #5 (Discord's
    mention parser doesn't run everywhere plain text can go), different
    trigger. Mentions inside embeds also never trigger a ping
    notification regardless of placement — acceptable here since
    Fantrax's own site already notifies everyone for
    transactions/trades, so the bot doesn't need to duplicate that.
11. **`Standings.ranks` (a `dict[int, Record]` keyed by rank number)
    silently drops teams that tie at the same rank** — dict keys can't
    hold duplicates, so each tied team overwrites the previous one in
    that slot, leaving only the last one processed. Confirmed live: all
    10 teams tied at rank 1 pre-season (everyone 0-0) collapsed
    `.ranks` down to a single entry. `cogs/weeklyrecap.py`'s
    `_all_records()` rebuilds the full list directly from
    `standings._data["rows"]` using the same `Record` constructor the
    library uses internally, instead of trusting `.ranks`. Use
    `_all_records()`/`_record_lookup()` anywhere standings data is
    needed — don't reach for `.ranks` directly.
12. **The SCHEDULE_FULL view's day-by-day date columns don't exist until
    the season actually starts** — confirmed live, including with an
    explicit future `period_number` — it falls back to season-total
    columns (Age/FPts/FP-G) instead, same root cause as gotcha #9's
    `scoring_dates` emptiness. This means `powerrankings.py`'s
    `fetch_team_period_data()` schedule half, and therefore the whole
    daily-simulation pipeline's actual numbers, is **unverified until
    2026-10-20** — the optimizer and roster/FP-G extraction are
    real-data-tested today, but "does player X have a game on date Y"
    parsing is written to match `fantraxapi`'s own known-working
    `Roster.__init__` zip-based pattern (stats rows zipped with schedule
    rows), not yet exercised against a real response. Check this first
    once the season starts, before trusting any ranking output.
13. **`docker compose restart` does NOT pick up a `requirements.txt`
    change — only `docker compose up -d --build` does.** Hit this for
    real: added `anthropic` to `requirements.txt`, restarted, and
    `/rankingsdebug` crashed with `ModuleNotFoundError: No module named
    'anthropic'` — the running container was still on an image built
    before the dependency was added, since `.py` files are bind-mounted
    (restart picks those up fine) but installed pip packages are baked
    into the image at build time (restart does NOT re-run `pip
    install`). Already stated in the Dockerfile's own header comment
    ("Only rebuild this image if requirements.txt changes") — this
    gotcha is the reminder that it's easy to miss in practice, not new
    information. Symptom to watch for: a slash command's first message
    (if it sends one before the part that imports the new dependency)
    goes through fine, then the rest of the command silently never
    arrives — looks like "isn't working" or "not sending an embed", not
    obviously an import error, unless `docker compose logs` is checked.

## Discord formatting conventions

- Both trackers post as **Discord embeds** (not plain text/code blocks)
  to `#news`, branded with a "Shams-kun" author name + the bot's own
  Discord avatar as the icon (deliberately not the big conch image from
  `/askshams` — that's a full illustration, not sized for a small embed
  icon).
- **Transactions** (`transactionstracker.py`): one embed per transaction,
  `📝` for any add (whether or not paired with a drop) / `✂️` for
  drop-only, description-only body (no fields needed). Color-coded:
  green = any add including a sign-and-drop, red = pure drop, blurple =
  anything else. FAAB bid shown as `for $X.00` inline, not a running
  balance (see "FAAB" note above — no balance tracking at all). Embed
  timestamp is post time (transaction dates aren't reliable — see
  gotcha #1).
- **Trades** (`tradestracker.py`): one embed per trade, one field per
  team (`@Team receive:` + a bulleted list of players/picks/cash — see
  gotcha #10 for why the mention has to live in the field *value*, not
  the field name). Title/color escalate to `🚨 BREAKING TRADE` in red at
  `BREAKING_ASSET_THRESHOLD` (currently 4) combined players+picks moved
  across the whole trade; otherwise `🔄 Trade Executed` in blue — no
  siren/prefix distinction below that, this is a size heuristic not real
  editorial judgment. Embed timestamp uses the *real* trade-processed
  date from Fantrax (parsed as `America/New_York`, DST-safe via
  `zoneinfo`) rather than post time, since that data is actually
  reliable for trades (unlike transactions).
- No more `▬▬▬` divider — an earlier plan used one in plain text, dropped
  once everything moved to embeds (the embed's colored left border does
  that visual-separation job now).
- Both trackers poll **every 5 minutes** (not hourly) — deliberately
  lowered for near-real-time reporting; still a trivial request volume
  on a public, unauthenticated endpoint (confirmed no rate-limit issues
  during testing). `transactionstracker.py` additionally has the
  midnight-PST waiver-batch check for prompt overnight reporting.
- User explicitly wants trades and transactions to share the SAME
  `#news` channel (not split into separate channels) — emphasis comes
  from the embed title/color escalation, not channel separation or role
  pings (explicitly declined pinging a `General Manager`-style role;
  mentions render but don't ping regardless, see gotcha #10).
- **Weekly recap** (`weeklyrecap.py`): three SEPARATE embeds posted in
  sequence (recap → standings → next week's matchups), not one combined
  embed — each stays short/scannable, reads like distinct broadcast
  segments. Recap lines bake each team's updated W-L record into the
  score line (`**Team** (6-2) def. **Team** (3-5) — 145.2 to 138.7`)
  rather than a separate standings table in that embed; the dedicated
  standings embed right after covers the full-table view. Colors: blue
  (recap) → gold (standings) → purple (next week).

## Next steps (where we left off)

**Trade poster and transaction announcer are both live in production**
in `#news`, `DRY_RUN = False` on both. Formatting/embeds verified
against real trade and transaction data multiple times this session,
including the mention-in-field-name bug (gotcha #10) and a color/wording
pass based on live screenshots. Not yet verified against a full
unattended overnight cycle on the NAS post-embed-conversion — worth a
final check if issues come up (same "confirm state persists across
restart, confirm no double-posting" rigor used for the original
plain-text rollout, now needs re-confirming for the embed version).

**Weekly recap status:** built and live — `cogs/weeklyrecap.py`,
`DRY_RUN = False`, `config.weeklyRecapChannelId` points at the real
recap channel. Posts Mondays at 9:00 AM Pacific (`RECAP_POST_TIME`, easy
to change) IF a scoring period's `.end` was yesterday — see gotcha #11's
sibling issue, the date-match approach (not the library's
`.complete`/`.current` flags) that makes this reliable. Flipping
`DRY_RUN` off before the season starts (2026-10-20) was safe since that
date-match logic means nothing can actually post before the first real
Monday-after-a-period-end anyway. `/recapdebug` was fixed mid-session
(was grabbing the in-progress current period instead of the last
truly-finished one on non-Monday days) — it now tells you explicitly
whether you're seeing a live match (exactly what the next scheduled post
will show) or a preview. Not yet verified against a real week of actual
(non-zero) scores, since the season hasn't started — worth a sanity
check via `/recapdebug` once week 1 concludes.

**Power rankings will stay a manual/Claude-assisted process**, not
bot-automated — user explicitly doesn't want the bot generating rankings
opinions. A possible future help: a bot command that dumps the
underlying stats (standings, streaks, point differentials, recent moves)
as a starting point to paste into a Claude session — floated as an idea,
not committed/built.

FAAB-spent leaderboard and draft-pick-ownership tracker were both
considered and explicitly declined — user pointed out both are already
visible on the Fantrax site itself, so not worth building.

Season doesn't start until 2026-10-20, so none of the recap's matchup-
result formatting has been tested against real (non-zero) scores yet —
worth a sanity check once the season actually begins.

**Current-season power rankings — in progress.** `powerrankings.py`
(project root, NOT in `cogs/` — it's data/algorithm only so far, no
Discord cog wiring yet, so it must not be auto-loaded by main.py's
`load_cogs()`). Goal: accurately project each team's true depth-adjusted
strength — not a naive top-12 fp/g average — by simulating the league's
actual 12-slot daily lineup lock against the real NBA schedule. Dynasty
rankings are explicitly out of scope for this — user will do those
manually/with Claude chat, since dynasty asset valuation (picks, age,
timeline) has no clean deterministic data source the way current-season
performance does.

Built and verified so far:
- `ACTIVE_SLOT_TEMPLATE`: this league's exact daily lineup slots — 1 PG,
  1 SG, 2 G, 1 SF, 1 PF, 2 F, 1 C, 3 Flx = 12 — confirmed live from the
  raw roster response's Active-status row `posId` sequence (see gotcha
  below). Eligibility for each slot is checked directly against a
  `Player.all_positions` short name (e.g. a PG/SG-eligible player's
  `all_positions` already includes `"G"` and `"Flx"` — Fantrax
  pre-expands flex eligibility, no hand-coded PG/SG→G hierarchy needed).
- `solve_daily_lineup()`: a from-scratch min-cost-flow solver (Bellman-
  Ford-based successive shortest paths, no scipy dependency — kept the
  container light at its 256MB `mem_limit`) that finds the
  *provably optimal* 12-man lineup for a given day's available players.
  Deliberately NOT a greedy "fill narrowest slot first" heuristic — the
  eligibility structure has overlapping-but-not-nested cases (a PG-only
  and a SG-only player both feed the shared "G" flex slot without either
  containing the other) where greedy correctness can't be proven for
  every case. Validated against adversarial synthetic test cases
  (including one hand-verified case with forced position conflicts),
  not just spot-checked.
- `fetch_team_period_data()`: roster + per-day schedule for one team/
  period, bypassing `League.team_roster()`'s crash the same way
  `weeklyrecap.py`'s standings fix does — calls
  `fantraxapi.api.get_team_roster_info` directly and parses both the
  STATS and SCHEDULE_FULL raw responses itself. Verified live: roster/
  FP-G extraction works correctly today (Fantrax falls back to last
  season's stats pre-season, which is fine for testing the plumbing —
  confirmed real player data flows through correctly, e.g. the optimizer
  correctly pulled bench players over lower-scoring "Active"-tagged ones
  when re-solving from the full 30-man roster).
- `staggered_period_numbers()` / `compute_power_rankings()`: two
  non-adjacent weekly periods (current + one 2 periods out, skipping the
  one in between) rather than one continuous 14-day block — NBA
  schedule density runs in multi-week phases per team (road trips,
  back-to-back clusters), so a continuous block can land entirely inside
  one team's light/heavy stretch; staggering decorrelates that noise at
  the same total simulated-day count while staying close enough to "now"
  (~3 weeks out) that injuries/recent trades stay relevant. Reuses the
  league's own weekly period boundaries (same `scoring_periods` data
  `weeklyrecap.py` already uses).
  **Known, accepted edge case:** periods 17-19 (the playoff weeks,
  confirmed via gotcha #9's investigation) are 14 days long, not 7 —
  walked every period as "today" and confirmed no crash anywhere, but
  the last few weeks of the season use a different window shape than
  the rest (period 15 pairs with 14-day period 17 instead of another
  7-day period; periods 18-19 have no period far enough ahead to pair
  with, so they fall back to a single 14-day window instead of two
  staggered ones). User explicitly decided this is fine to leave as-is
  rather than special-case playoff periods — don't "fix" this later
  without checking back, it's a deliberate choice, not an oversight.

**LLM narration — built and validated.** `config.anthropicApiKey` holds a
separate Anthropic API key (pay-per-token billing via the Console, not
the user's claude.ai subscription — prepaid credits, auto-reload
recommended since this runs unattended on a schedule). `requirements.txt`
now includes `anthropic`. `generate_power_rankings_writeup()` in
`powerrankings.py` calls `claude-opus-4-8` with adaptive thinking, given
the ALREADY-COMPUTED/ALREADY-RANKED numbers from `compute_power_rankings()`
— Claude only narrates, it never recalculates or reorders (explicit
system-prompt instruction). Tested with a real API call against
synthetic-but-realistic data (real team names, made-up scores/records,
since real scores are all zero pre-season) — output correctly held rank
order, matched the established Shams-kun voice, used real record/streak
context where given without hallucinating it where absent, and
translated scores into narrative instead of quoting raw numbers, per
the system prompt. `record_lookup()` reuses the same `Standings.ranks`
tie-collision workaround as `weeklyrecap.py` (gotcha #11), duplicated
rather than cross-imported to keep cogs independent/modular (established
preference from the trade/transaction tracker split).

**Discord cog — built and live, `DRY_RUN = False`.** *(Update 2026-07-10:
`DRY_RUN` was reverted to `True` at the end of that day's session —
see the "Media ecosystem" section further down for why. This paragraph
describes the cog as it was verified BEFORE that session's additions;
check the `DRY_RUN` line in the file itself for the current real state
rather than trusting this sentence.)*
`cogs/powerrankings.py` posts to the same channel as `weeklyrecap.py`
(`config.weeklyRecapChannelId`) — both are periodic digest content, as
opposed to `#news`'s real-time individual events. Cadence is
**Wednesday 9am Pacific**, deliberately separate from the Monday
recap/standings/preview slot so periodic content spreads across the
week instead of bunching up on Monday. `_compute()` treats "every
team's points_per_day is exactly 0.0" (the confirmed pre-season state)
or an empty ranking list (no periods left — see the playoff-window note
above) as "nothing to post yet", not an error — same graceful-
degradation pattern as the other two trackers. `/rankingsdebug`
(permission-gated to `manage_guild`, matching every other debug
command) dumps the raw per-team numbers in a code block first, then
sends the actual rendered embed with real LLM narration — uses
`interaction.response.defer()` since the LLM call can take a few
seconds, longer than Discord's 3-second initial-response window.

**`/rankingsdebug synthetic: True`** — a `synthetic` bool parameter
runs the full pipeline (real LLM call, real embed rendering) against
made-up-but-clearly-labeled placeholder data (`_synthetic_rankings()`
in the cog) instead of real Fantrax data, specifically so the debug
command is testable pre-season when real scores are all 0.0. Both the
text dump and the embed footer are explicitly marked
"⚠️ SYNTHETIC TEST DATA" so it can never be mistaken for a real
projection. Gives each team a genuinely distinct fake 6-player roster
(not the same shared sample list repeated for every team) — an earlier
version of this reused one roster for all 10 teams, which the LLM
correctly picked up on and echoed the same 6 names across multiple
teams' commentary, exposing that the test data itself was unrealistic
rather than any pipeline bug.

**LLM narration grounded in real roster data, not just the aggregate
score.** `compute_power_rankings()` returns a 5th element per team,
`top_players: [(name, fp_g), ...]` (that team's top 6 active-eligible
players, fetched via `fetch_roster_players()` — one extra API call per
team, negligible at ~weekly cadence). Verified via a real API call with
per-team-distinct synthetic rosters: commentary consistently references
the actual players given for each team, no invented claims.

**Redesigned into tiers + a single league-wide "notable changes"
paragraph** (two rounds of post-launch user feedback):
- `assign_tiers()` in `powerrankings.py` splits the ranked list into
  three deterministic, code-computed tiers (🏆 Contenders / ⚔️ In the
  Hunt / 🥞 Pancake Contention) using a **largest-gap heuristic** — finds
  the two biggest consecutive-pair drops in the sorted `points_per_day`
  list and splits there, so boundaries reflect real clustering in the
  projections instead of a fixed index split. (Superseded the earlier
  fixed 30/40/30 positional split, which never looked at the scores at
  all — see git history if the old behavior is ever needed for
  comparison.) Deliberately not full Jenks natural-breaks optimization —
  10 data points don't justify it, and this module avoids numpy/scipy on
  purpose (256MB container memory limit).
- **The rank list itself is 100% code-generated, zero LLM
  involvement** — `format_tier_list()` builds it directly (team name,
  rank, always-shown W-L record + streak whenever a `Record` is
  available — no "only if notable" filtering, user explicitly wants it
  always shown). Claude's only job is a SINGLE 3-5 sentence paragraph
  appended below that list, covering league-wide storylines (biggest
  riser/faller, hot/cold streaks, new arrivals) — explicitly NOT a
  per-team breakdown anymore (the first version had a blurb per team;
  user asked to collapse that into one summary paragraph instead).
- **No numeric projections anywhere in the post** — system prompt
  forbids stating/implying the points-per-day figure; verified via a
  regex sweep over real LLM output finding zero leaked numbers, not
  just eyeballed.
- `posted_power_rankings.json` (gitignored like the other tracker state
  files) stores `{team_id: last_rank}` for week-over-week movement —
  only written after a REAL post (`_check_and_post`), never by a debug
  run, so testing can't corrupt the "what changed" baseline.
- `/rankingsdebug synthetic: True` fabricates plausible previous-rank
  data (a riser, a faller, a brand-new team) so movement logic is
  exercisable without waiting for a real second week of data.
- Verified end-to-end with a real API call after each redesign round:
  correct tier grouping, zero leaked numbers, accurate movement-focused
  paragraph.
- Real per-call cost measured directly from `response.usage` (not
  estimated): **~$0.015/generation** (1,292 input + 352 output tokens)
  — actually cheaper than the original per-team-blurb version's
  estimate, since less data goes in and the output shrank to one
  paragraph. **Predates the player-trends addition below** — see that
  section for the (currently estimated, not yet live-measured) updated
  figure.

**Gap-based tiers + real week-over-week player-trend narration** (2026-07-10):
- `assign_tiers()`'s largest-gap split (described above) replaced the
  old fixed 30/40/30 positional split.
- New `compute_player_trends()` in `powerrankings.py` lets the weekly
  blurb name standout/slumping *players*, not just teams — grounded in
  a genuine trailing-window signal, not just a slow-moving cumulative
  average. Design history worth keeping, since it wasn't the first idea
  tried:
  - First draft compared this week's season-cumulative FP/G to last
    week's season-cumulative FP/G. Rejected — too weak/noisy a signal,
    since a cumulative average barely moves once a player has a large
    game sample.
  - Checked live whether Fantrax's API exposes a native trailing-window
    stat (the site has a "Dates"/goBackDays window selector) before
    building anything custom. Confirmed via direct API calls: passing a
    raw `goBackDays` kwarg through `Method(**kwargs)` does nothing (byte-
    identical response) — the real mechanism needs a structured
    `displayedSeasonOrProjection` selector (`timeframeTypeCode:
    "BY_DATE"` + explicit start/end dates) that the codebase's thin
    `Method(**kwargs)` wrapper isn't built to send (it flat-stringifies
    kwargs, no nested-object support), and — like `SCHEDULE_FULL`'s
    per-date columns (gotcha #12) — couldn't be verified correct until
    the season is actually live anyway. `get_live_scoring_stats()` (a
    real per-day endpoint) was also considered but would cost one extra
    API call *per day* in the window, i.e. more load, not less.
  - Landed on deriving a true trailing-window rate from data already
    being fetched: `fetch_team_period_data()` now also extracts each
    player's season `FPts` total (STATS column `sortKey == "SCORE"`)
    and `GP` (games played — matched by `shortName == "GP"`, since this
    column's own `sortKey` is a league-scoring-category composite, e.g.
    `SCORING_CATEGORY_3010#1350#-1`, not a portable constant). Confirmed
    live that `FP/G == FPts / GP` exactly (e.g. Shai Gilgeous-Alexander:
    3773 / 68 = 55.49, matching Fantrax's own FP/G column). Persisting
    `(FPts, GP)` week over week and diffing gives
    `recent_fpg = (fpts_now - fpts_prev) / (gp_now - gp_prev)` — a
    genuine "how have they played since the last post" rate, compared
    against the player's current season average — at **zero extra API
    calls**, since it reuses the exact same STATS response already
    parsed for `fp_g`.
  - `roster_players` (renamed from `top_players`) now holds every
    active-eligible player on a team's roster, not just a top-6 slice —
    needed so trend detection isn't blind to players outside the
    current top 6. Callers wanting "top players for context" (e.g. the
    LLM-facing grounding, `/rankingsdebug`'s dump) slice
    `roster_players[:6]` themselves. Each entry is now `(player_id,
    name, fp_g, fpts_total, gp)` — `player_id` (`Player.id`) instead of
    just name, for stable cross-week identity.
  - `posted_player_stats.json` (gitignored, same pattern as
    `posted_power_rankings.json`) stores `{player_id: {"fpts", "gp"}}`
    for the whole league — flat, not nested per team, written wholesale
    (full overwrite, not merge) only on a REAL post. This flat/overwrite
    shape handles trades and drops for free, with no special-case code:
    a **traded** player's real-world FPts/GP aren't scoped to whichever
    fantasy team owns them, and this week's `rankings` always attributes
    them to their *current* team (rebuilt from live rosters every run),
    so a trade doesn't misattribute anything. A **dropped** player
    simply doesn't appear in any team's `roster_players` the week
    they're unrostered, so they're silently absent from trend detection;
    their stale entry then vanishes on the next real post because the
    save is a full rebuild off that week's `rankings`, not a merge — no
    explicit cleanup/pruning logic needed anywhere.
  - New-to-roster players (trade, waiver pickup, return from Inj Res, or
    re-added after a drop cycle already purged their old snapshot) have
    no prior data point and are skipped by design — explicitly confirmed
    out of scope; would need a different signal (roster-add detection,
    not an FP/G delta) to flag as their own storyline.
  - `_SYSTEM_PROMPT`'s numeric-leak rule was broadened to explicitly
    cover player-level figures too (not just points-per-day) — a
    player's name plus qualitative direction ("trending up/down") is
    fair game, the underlying delta never is, same contract as the
    existing team-level `_movement_str()`.
  - Estimated cost impact (not yet live-measured — see the $0.015/gen
    figure above for the last real measurement, which predates this
    change): +$0.0012-0.0015/generation (~8-10%), landing around
    $0.0165-0.017/generation, i.e. roughly +$0.005-0.0065/month at the
    existing Wednesday-only cadence. The trend computation itself is
    $0 in tokens — pure Python before the API call; only a handful of
    code-selected "trending up/down" lines ever reach the LLM.

**4th tier ("Favorites") + config-driven bottom tier name** (2026-07-10,
same session as the gap-based tiers above): `assign_tiers()` generalized
from always-2-gaps/3-tiers to `len(tier_names)-1` gaps/`len(tier_names)`
tiers — adding/removing a tier is now just a `tier_names` tuple-length
change, no algorithm change. `TIER_NAMES` is `(👑 Favorites, 🏆
Contenders, ⚔️ In the Hunt, 🥞 Pancake Contention)`. The bottom tier's
name/emoji is overridden from `config.lastPlaceTierName` in
`cogs/powerrankings.py`'s `_build_embed()` (top 3 stay the code
defaults) — deliberately NOT hardcoded, since it's tied to whatever the
league's current last-place punishment actually is, which changes
season to season; editable without a code change. Must be passed to
BOTH `format_tier_list()` and `generate_power_rankings_writeup()` or the
LLM's tier references stop matching what's printed above it.
`_synthetic_rankings()` also fixed to fabricate a W-L/streak for all 10
teams (was only 2 before) — `/rankingsdebug synthetic:True` previews now
match what a real post's formatting actually looks like instead of
mostly showing the no-record fallback path.

**Recent form now feeds the projection itself, not just the blurb**
(2026-07-10, later same day): previously `recent_fpg` (the trailing-
window rate `compute_player_trends()` derives) only ever reached the
narration — the actual `points_per_day` ranking was pure season-average
`fp_g`, so a real breakout/decline/return-from-injury didn't move the
number until enough weeks passed for the cumulative average to catch
up. Explicitly requested: "we should definitely rate players on current
or potential future performance more."
- New `_recent_fpg()` — factored out of `compute_player_trends()`'s
  inline math, now the single shared derivation of "how has this player
  played since the last snapshot," used two ways: `compute_player_
  trends()` for the qualitative narration signal (unchanged behavior,
  confirmed via regression test — same inputs produce identical
  risers/fallers as before the refactor), and `_build_player_lookup()`
  for the new numeric blend below.
- New `_build_player_lookup()` — the actual value fed into
  `solve_daily_lineup()`'s min-cost-flow now blends season fp_g with
  `_recent_fpg()`, weighted by `RECENT_FORM_WEIGHT` (0.35, a plain
  module constant in `powerrankings.py` — not `config.py`, since this is
  an algorithm tuning knob, not a league fact like the tier name).
  Deliberately does NOT touch `roster_players`' own `fp_g` field (stays
  pure season average) — `compute_player_trends()` computes its own
  "recent vs. season baseline" delta from that field, and blending the
  baseline itself would double-count recent performance and dampen the
  exact signal that comparison is trying to surface.
- `previous_player_stats` now threads all the way down to
  `simulate_team_period()`, not just to the narration step — `cogs/
  powerrankings.py`'s `_compute()` takes it as a param and
  `_check_and_post()`/`rankingsDebug()` both load it *before* calling
  `_compute()` now (previously loaded only afterward, for
  `_build_embed()`). Omitting it (the default, `None`) simulates on
  pure season-average `fp_g` — same behavior as before this change, so
  nothing breaks for any caller that doesn't pass it.
- **Injury/suspension exclusion added to the simulation** — separate ask
  in the same request ("if injuries are currently being included in
  then we should def remove that too"). `Inj Res` roster-SLOT status was
  already excluded; what wasn't caught is a player Fantrax's own
  real-time designation flags as `day_to_day`, `out`, or
  `injured_reserve` (the `Player.injured` property — confirmed exact
  field names by reading `fantraxapi/objs/player.py` directly rather
  than guessing) or `Player.suspended`, while still sitting in an
  Active/Reserve roster slot (manager hasn't moved them, or has no open
  IR slot). `_build_player_lookup()` now excludes those too. Day-to-day
  is treated identically to fully out — full exclusion, no partial-
  availability weighting — a deliberate simplification flagged as
  possibly worth revisiting once real in-season data shows how often
  day-to-day players actually end up playing anyway.
- Verified via unit tests (fabricated `Player` stand-ins + fabricated
  previous-snapshot dicts, since real day-by-day schedule data is still
  gotcha #12-blocked pre-season): injury/suspension exclusion, blend-
  value arithmetic, and the `compute_player_trends()` regression all
  pass. Also confirmed live against the real API that `compute_power_
  rankings()` doesn't crash with or without a `previous_player_stats`
  argument (still returns all-zero `points_per_day` pre-season, as
  expected — this change doesn't touch the "nothing to rank yet" gate).

**Still blocked on gotcha #12** (real schedule data) for actual numeric
validation once the season starts — the LLM narration piece is
validated independently of that, since it only cares about the shape of
`compute_power_rankings()`'s output, not whether the numbers are
pre-season zeros or real projections. The recent-form blending above is
in the same boat: the blend *math* is unit-tested and correct, but
whether `RECENT_FORM_WEIGHT = 0.35` actually "feels right" against real
players' real week-to-week swings is unverifiable until real games are
being played.

## Media ecosystem — daily/weekly content beyond power rankings

Goal (user's framing): build out content around the league similar to how
real NBA media covers the league — beyond the existing trade/transaction
breaking news and weekly recap/power-rankings digests. Brainstormed and
prioritized by "buildable + testable with real data today" vs. "needs the
season live": MVP race and trade grades (both usable now, season-to-date
`FPts`-based, no day-level data needed) were the two strongest "buildable
now" candidates; GM lineup grades (actual lineup vs. hindsight-optimal,
using `solve_daily_lineup()` fed real scores instead of projected `fp_g`)
was explicitly **deferred** rather than built speculatively — the
data-fetching piece depends on `League.live_scores(date)` /
`getLiveScoringStats`, whose date-scoping behavior is unverifiable
pre-season (see below), and there's no calendar-time cost to waiting since
a week-1 grade can't exist before week 1 concludes anyway. Neither MVP
race nor trade grades has been built yet — only the daily top-performer
piece below has, chosen specifically because it validates the same
uncertain endpoint sooner (1 day in) than GM grades would (7 days in).

**`getLiveScoringStats` investigation** (2026-07-10): confirmed via direct
raw-API calls (bypassing `League.live_scores()`'s wrapper, which requires
`api.scoring_dates` to be populated — empty pre-season, same root cause as
gotcha #12) that the per-day response IS shaped the way we'd want:
`statsPerTeam.allTeamsStats[team_id]` is keyed by roster-slot status for
that specific date (`"ACTIVE"` confirmed; a `"RESERVE"`-style bucket is
strongly implied but unconfirmed — pre-season data only ever showed the
`ACTIVE` key, all zeros, so nothing to bench-test against yet). **However**
the `scoring_date` parameter appears to be fully ignored right now —
requesting three different dates (a future in-season date, the season
opener, and today) all echoed back the exact same default date instead of
what was requested. This isn't new/worse news, just another instance of
the already-known pre-season dead zone (no valid scoring context exists
for ANY date until real games are played) — but it means whether
per-*day* querying actually works once real dates exist is still an open
question, not a confirmed one. Also considered: `Team.roster()`/
`getTeamRosterInfo` reflecting a *historical* roster (needed so a GM grade
doesn't credit/blame a team for a lineup decision using players they
didn't actually own that day) — `live_scores(date)` sidesteps this problem
entirely by construction (it's tied to that day's real matchup, not a
live-refetched roster), which is part of why it's the right foundation for
this whole feature family, unlike re-simulating a past `SCHEDULE_FULL`
period against today's roster (the "before/current/after" idea considered
and rejected earlier for power rankings, for exactly this contamination
reason).

**`cogs/dailytopperformer.py`** — built and DRY_RUN=True (new,
unverified-against-real-data feature; flip once you've watched it run for
real — see the file's own comment). Posts daily at **8am Pacific**
(deliberately ahead of the Monday/Wednesday 9am recap/rankings slot so
those days don't stack three posts at the same minute) to the same digest
channel as `weeklyrecap.py`/`powerrankings.py` — flagged as possibly
wanting a dedicated channel later given it posts 7x more often than those.
Finds the single highest real fantasy-point performance league-wide for
*yesterday*, across every team and EVERY rostered player regardless of
active/reserve slot — deliberately "who had the best fantasy day in the
league" (simple, fun, no optimizer needed), not "whose manager benefited
from starting them" (that's the deferred GM-grade framing, a different and
harder question).

**Bug caught and fixed same session, before real-world testing was even
possible:** first draft called the wrapped `self.api.live_scores(date)`
(`FantraxAPI` **is** `League` — same class, aliased in
`fantraxapi/__init__.py`, confirmed by reading the package init). Reading
that wrapper's source (`fantraxapi/objs/league.py`) afterward, while
answering a question about verification timing, showed it only extracts
the `"ACTIVE"` bucket from `statsPerTeam.allTeamsStats[team_id]`, silently
dropping every other status bucket (`"RESERVE"`, etc.) — meaning it was
actually active-slot-only, contradicting the explicit "any rostered
player regardless of slot" requirement above. Fixed by bypassing the
wrapper entirely: calls `fantrax_api_module.get_live_scoring_stats()`
raw, replicates the wrapper's own `scorer_map`/`active_teams` construction
logic, then iterates ALL status buckets in each team's data (not just
`ACTIVE`) to find the true league-wide max — same bypass pattern
`fetch_team_period_data()`/`tradestracker.py` already use elsewhere for
other wrapper limitations in this codebase. The season-validity guard
(`DateNotInSeason`, raised internally by the wrapper) is now a direct
`scoring_date not in self.api.scoring_dates.values()` check instead,
since bypassing the wrapper means bypassing its guard too. Verified with
a fabricated raw response where the RESERVE bucket's score beats the
ACTIVE bucket's — confirms the fixed version correctly picks the bench
performance, where the original wrapper-based version would have
silently returned the wrong (lower, active-only) answer. Real numbers ARE
shown in the embed (`**{points:.1f}** fantasy points`) — unlike power
rankings' projections, this is reporting an already-happened real result,
so hiding the number would be inconsistent with how the rest of the bot
already treats real, settled numbers (FAAB bids, matchup scores, W-L
records). `/topperformerdebug synthetic:True` previews the embed
pre-season with a fake performance (real team object, made-up player/
points), same pattern as every other tracker's synthetic debug mode.
Verified offline: graceful `None` on the pre-season path (no crash),
correct max-finding across a fabricated multi-team/multi-player
`live_scores()`-shaped response, correct `None` on an empty-scoring-day
response, and correct embed rendering from the synthetic path.

**Channel rename (2026-07-11):** the two existing config channel
variables were renamed to match the actual Discord channel names rather
than generic labels, to avoid confusion as more content types share
them: `transactionChannelId` → `newsChannelId` (the transaction/trade
wire — `transactionstracker.py`, `tradestracker.py`), `weeklyRecapChannelId`
→ `espnChannelId` (periodic game coverage/media content). The distinction
between "news" and "espn" isn't "does an LLM narrate it" (recap has zero
LLM involvement, same as transactions) — it's *subject matter*: news is
specifically the roster-move transaction wire, espn is coverage of the
actual competition (results, standings, storylines, analysis). Under that
framing, `weeklyrecap.py` belongs on `espnChannelId`, which it already
shared with `powerrankings.py` before this distinction was even named — a
pre-existing decision this rename made explicit rather than changed. A
short-lived `mediaChannelId` placeholder (introduced earlier the same
session for `dailytopperformer.py`) was immediately consolidated into
`espnChannelId` once it turned out to be the same real channel — don't
resurrect `mediaChannelId`, it never had a real ID and no longer exists in
config.py.

## Trade grades (`tradegrades.py` + `cogs/tradegrades.py`)

**Goal** (user's framing): a trade reaction/grade that's genuinely
dynasty-aware, not just a current-value comparison — "most trades aren't
just in-season value" in a dynasty league. Specifically requested:
positional roster construction should factor in (a player doesn't help a
team that's already deep at his position, because of the 12-active-slot
lineup constraint), timeline fit should factor in (age vs. team
competitive window), and the system should stay open to "other dynasty
considerations" without needing every assumption hand-coded.

**Design principle — same one power rankings is built on**: code computes
real, deterministic numbers; Claude narrates the judgment; the LLM never
invents facts or formulas. This mattered more here than anywhere else in
the bot, because a "correct" dynasty valuation formula doesn't exist —
not just for us, for anyone. Pick-value charts and age curves are
opinions, not lookups (this was already explicitly decided out of scope
once before, for exactly this reason — see the dynasty-rankings note
elsewhere in this file). The design threads the needle by finding the ONE
piece of this that *is* objectively computable, and leaning on real
public facts + LLM judgment for the rest.

**The computable piece — positional fit via marginal lineup value, not a
heuristic penalty.** `powerrankings.lineup_ceiling(roster)` (new,
reused from the existing min-cost-flow optimizer) treats every rostered
player as if they had a game today and returns the optimal lineup's
total value under the real 12-slot template — a "what's this roster's
active-lineup ceiling right now" number that needs no `SCHEDULE_FULL`/
day-by-day data at all, so (unlike the actual power-rankings simulation)
it's fully usable in the offseason. For a trade, computing this on a
team's current (post-trade) roster vs. a reconstructed hypothetical
roster with the trade undone gives the TRUE marginal value gained/lost —
positional logjam falls out automatically, because the optimizer only
has so many slots regardless of raw `fp_g`. Verified with a deliberately
constructed test (4 PG-eligible players competing for 3 PG/G slots plus
3 shared Flex slots): losing the 4th, buried PG cost only 15.0 marginal
value even though his raw `fp_g` was 35.0 — because he was still winning
a Flex slot over a weaker player, just not a "true" PG/G slot — while
losing an actual starter cost 25.0, not his full 45.0 raw value, because
the next-best bench player cascaded up to partially backfill. That
cascading, partial-credit behavior is exactly right and is NOT something
a hand-written "if a team has N+ players at position X, discount by Y%"
rule could reproduce — it falls out of reusing the real optimizer, for
free.

**Resolving "what a team gave up" without historical roster data**: every
player in a trade is, right now, sitting on *somebody's* current roster —
whoever received them. So `tradegrades.analyze_trade()` fetches every
involved team's CURRENT roster, builds one global `player_id -> full
roster entry` lookup across all of them, and resolves a given-up player's
stats (name, age, `fp_g`) from wherever they landed. No historical/
point-in-time roster query needed — which is good, because one doesn't
reliably exist (same family of problem as the "before" week idea rejected
for power rankings, and the untested past-date query for
`getLiveScoringStats`).

**Real facts gathered for the LLM, not computed by it**: player ages (the
`fetch_team_period_data()` tuple grew from 5 to 6 elements — `(Player,
fp_g, fpts_total, gp, age, status)` — same STATS column extraction
pattern as `fpts_total`/`gp` before it; touched `simulate_team_period()`,
`_build_player_lookup()`, `compute_power_rankings()`, and every
`roster_players` consumer in both `powerrankings.py` and
`cogs/powerrankings.py`), each team's current roster-wide average age,
each team's current W-L record/streak (`record_lookup()`, reused), and
any draft picks involved (parsed from the same `draftPickDisplayParts`
shape `tradestracker.py` already handles — **confirmed live, not
assumed**, that every trade row carries a `"scorer"` key even for pick/
cash rows, just a thin placeholder dict, so `draftPickDisplayParts`/
`budgetAmountTradeObj` must be checked FIRST, exactly mirroring
`tradestracker.py`'s existing check order — an earlier draft of this
investigation's own throwaway test script got this wrong by checking
`"scorer" in row` first, which is why this got verified against real
trade history before being written into the real parser, not after).
Per explicit product decisions: no contract/keeper rules exist in this
league (pure dynasty, simplifies the model), and both record and roster
age are handed to Claude with no code-side weighting — Claude judgment-
calls which matters more per trade, rather than the code picking one
proxy.

**What stays hidden vs. what's shown**: the computed marginal-value
number itself is NEVER stated in the output (same "Claude narrates,
never leaks the underlying figure" contract as power rankings) — but
real public facts (player ages, records, picks) ARE said directly, since
hiding e.g. "Kawhi Leonard is 35" would be bizarre (it's real, public,
verifiable information, not a proprietary computed number). The
narration prompt gets the real computed direction and magnitude as
grounding so its qualitative call ("clearly favors Team A") is accurate,
even though the number itself never appears in the output.

**Verified against REAL trade history, not just synthetic data** — this
league has 16 real executed trades already (offseason dynasty activity).
`parse_trade_rows()` + `analyze_trade()` + `generate_trade_grade_writeup()`
(LLM call mocked to avoid real spend) were run end-to-end on an actual
trade: Horny Mushrooms sent two future 1st-round picks (2027, 2029) plus
two young players (Carlton Carrington, 20; Max Christie, 23) to
Homoerotic Knights for Kawhi Leonard (35) and Anthony Davis (33) — a
textbook win-now-vets-for-picks-and-youth dynasty trade. Computed
marginal value: Horny Mushrooms +25.3, Homoerotic Knights -25.2 —
directionally exactly right (immediate production clearly favors the
team that got Kawhi/AD), while the real facts fed to the narration
(ages, two future 1sts, the youth) carry everything needed for a
genuinely dynasty-aware read on the other side of the deal. This is
about as good a real-world validation as this feature could get before
the season starts.

**`cogs/tradegrades.py`** — built and `DRY_RUN=True` (new, unverified-
against-real-*posting* feature, though the analysis pipeline itself is
now real-data-verified per above). Runs once daily at **10am Pacific**
(after the 8am/9am cluster) and grades any trade whose real processed
date is before today and not yet in `posted_trade_grades.json` (own
dedup state file, gitignored, same flat/full-overwrite-on-save pattern
as the other trackers' state — mirrors `tradestracker.py`'s
`posted_trades.json` pattern, not merged/shared with it, keeping the two
cogs decoupled). Deliberately "grades the next day", not immediately —
explicit product decision, not a scheduling accident — so if the bot's
been down a few days, any backlog of ungraded trades all gets graded in
one pass (dedup prevents double-posting, same catch-up behavior as every
other tracker). Posts to `config.espnChannelId`, a separate embed from
the original trade announcement in `#news`/`newsChannelId`. Re-fetches
trade history independently rather than sharing `tradestracker.py`'s
fetch (same "duplicate small fetch/parse logic, keep cogs decoupled"
preference already established in this codebase). `/tradegradedebug
synthetic:True` fabricates a plausible trade (mirroring the real
Kawhi/AD-for-picks-and-youth shape found in this league's actual
history) for pre-season preview; the non-synthetic path grades the most
recent real trade regardless of age/graded-status, bypassing the "wait a
day"/dedup gating — debug commands never touch or are blocked by
persisted state, same principle as every other tracker's debug command.
Verified offline: date-filtering logic (a trade dated today is excluded,
yesterday-or-earlier is included, already-graded is excluded via dedup —
caught and fixed a timezone bug in the *test itself*, not the code, from
generating fake timestamps in the wrong timezone before comparing against
the Eastern-labeled convention real trade rows use), the full grade
pipeline against real trade data end-to-end with a mocked LLM call, and
the synthetic preview path.

**Making the "dynasty" part actually mean something (2026-07-11).** The
design above gives Claude real timeline signals (age, record) but
originally restricted it to zero speculation beyond given facts — on
reflection this was too conservative: most real trades in this league
involve picks and rebuild-vs-contend timing, and a system that can only
comment on immediate lineup impact isn't really doing dynasty analysis at
all. Revised `_SYSTEM_PROMPT` to explicitly permit Claude to draw on its
own general knowledge of a player's real-world standing/trajectory/
prospect outlook, hedged like a real scouting opinion rather than stated
as fact ("has looked like an ascending piece," not "is definitely a star
in the making").

The one constraint added on top, per explicit user request: **never let
that general-knowledge commentary name or imply a specific current NBA
team, coaching situation, or teammate/depth-chart context.** Reasoning:
we don't feed Claude any player's actual current NBA team in this prompt,
so any team-specific claim would necessarily come from Claude's own
training knowledge — which can go stale the moment a real-world trade,
signing, or coaching change happens, and would read as an obviously wrong,
dated take to league members who follow the NBA. Team-agnostic trajectory
language ("trending up," "role has been expanding," "still developing")
doesn't have that failure mode — it ages far more gracefully than a claim
tied to a specific roster spot. This mirrors the same instinct behind
rejecting the earlier "week-over-week cumulative average" power-rankings
design: prefer a signal that's actually correct over one that just sounds
more specific.

Also wired in each player's `rookie` flag (Fantrax's own designation —
`Player._data["rookie"]`, not a parsed `Player` attribute, so accessed via
`._data.get("rookie", False)`) as a real anchor fact alongside age, since
it's exactly the kind of hard, unambiguous dynasty-relevant fact (unlike
speculative trajectory commentary) that's free to state directly.

Verified via the same mocked-`anthropic.Anthropic`-client pattern used
elsewhere in this file: confirmed the rookie flag reaches the user
message (`", rookie"` appended for a fabricated rookie given-up player)
and that the new system prompt contains both the general-knowledge
permission and the team-avoidance rule. Also re-ran `analyze_trade()`
against the same real Kawhi/AD trade used for the original validation —
rookie extraction didn't crash on real `Player` objects (all four players
in that trade are veterans, so `rookie=False` for all, as expected), and
the marginal-value split reproduced the original +25.3/-25.2 result
exactly, confirming this change didn't disturb the existing computation.
Caught and fixed one regression before it could break the debug command:
`cogs/tradegrades.py`'s `_synthetic_analysis()` (used by `/tradegradedebug
synthetic:True`) didn't have the new `acquired_rookies`/`given_up_rookies`
keys the narration prompt-builder now requires — would have raised a
`KeyError` the next time someone ran the synthetic debug path.

**Still on the table, not yet built** — raised during the same holistic-
dynasty-analysis discussion, deliberately not implemented yet (each has an
open question or needs more verification first, not just "do it later"
busywork):
- **Injury/suspension tooltip context.** Confirmed feasible: the real
  human-readable injury detail (e.g. "Finger - Game-time decision") lives
  in the `tooltip` text on each entry of `player._data["icons"]`
  (`typeId` 1/2/6/30 = day-to-day/injured-reserve/suspended/out — see
  `fantraxapi/objs/player.py`), not currently extracted by this codebase.
  Bare injury/suspension flags with no context were judged "just noise" —
  the tooltip text is the part worth surfacing.
- **Roster-crunch VOR** (a trade pushing a team over its total roster cap,
  forcing a drop of a player with real value). **Roster cap now confirmed
  live** (2026-07-11, via `miscData.statusTotals` on a real
  `getTeamRosterInfo` response): 12 Active + 18 Reserve + 4 Injured
  Reserve = **34 total roster spots**, all currently at max for at least
  one real team checked — supersedes the earlier "30-man roster" note
  elsewhere in this file, which was stale/wrong. Not yet implemented:
  `lineup_ceiling()` only scores the active-12 optimal lineup and is
  blind to bench depth beyond that, so a trade that pushes a team over 34
  total players (forcing a drop of a real bench asset) currently costs
  nothing in the computed marginal value — a real gap, worth fixing by
  detecting an over-cap hypothetical roster in `analyze_trade()` and
  computing the value of the forced drop(s) as a separate real fact.
- **`games_back`** (from `Record`) as an additional timeline signal — easy
  to add, but flagged as a genuinely open question: could conflict with
  the roster-age signal for a team that's young but already contending,
  and it's unclear whether Claude would reliably reconcile two
  disagreeing signals correctly without an explicit prompt instruction
  telling it how. Would need that instruction written deliberately, not
  just handed two more numbers and hoped for the best.
- Reusing `_recent_fpg()`/player-trend infra for trade grades, and using a
  team's power-rankings tier as an input signal — both gated on having
  real season data to compute either from (currently offseason-only,
  last-season stats).

**Actually showing the trade + an actual grade (2026-07-11).** User
feedback on the first live posts: the embed only ever showed the
narrative paragraph — no visible record of what was actually traded, and
no scannable grade at all (a wall of prose isn't a "grade"). Two fixes,
both in `cogs/tradegrades.py`'s `_build_embed()`:
- **Trade summary fields**: one inline field per team, "`<Team> receive:`"
  followed by a bullet list of acquired players (name, age, rookie tag)
  and picks — same shape as `tradestracker.py`'s original announcement
  embed, but built from `trade_analysis`'s already-resolved data (no
  re-fetch of the raw rows needed, since `analyze_trade()` already
  carries acquired_names/ages/rookies/picks_acquired per team). Reuses
  the same team-role-ping convention as `tradestracker.py`'s
  `role_tag()` (falls back to a bolded team name if no role is
  configured for that team) — a local `_team_tag(team_id, team_name)`,
  not a cross-cog import, since it only needs `config.teamRoleIds` and
  this codebase's established preference is duplicating a little glue
  rather than coupling cogs together.
- **An actual verdict, not just prose.** `generate_trade_grade_writeup()`
  now returns `{"verdict": str, "narrative": str}` instead of a bare
  string. The user considered two formats — per-team letter grades
  (A–F, like ESPN/Bleacher Report trade columns) vs. a single directional
  favor-scale — and picked the favor-scale: an absolute letter grade
  needs its own external calibration ("what does a B mean on its own?"),
  while a relative scale directly answers what trade-grade content is
  actually for and is naturally grounded by the real computed
  `marginal_value_delta` this feature already produces. Implemented via a
  **forced tool call** (`tool_choice: {"type": "tool", "name":
  "emit_trade_grade"}`), not text parsing — `verdict` is a JSON-schema
  enum built fresh per-trade from the real team names involved ("Even",
  "Slightly/Significantly favors <team>" ×2), so the model is
  structurally constrained to a well-formed, directly-renderable answer
  rather than us hoping its prose happened to follow a convention.
  Confirmed forced `tool_choice` is compatible with `thinking: {"type":
  "adaptive"}` on the standard Claude API (the only restriction —
  `thinking: {"type": "disabled"}` required alongside a forced tool
  call — is Bedrock-specific, not applicable here). Designed for the
  2-team case (matches this league's real trade history — verified live,
  all 5 real trades so far are 2-team) but the enum-building loop is
  N-team-general; genuinely untested past 2.

  Verified via the same mocked-`anthropic.Anthropic`-client pattern used
  throughout this file: confirmed the tool schema's `verdict` enum
  contains the real team names, that `tool_choice` forces the
  `emit_trade_grade` call, and that the returned dict's verdict is always
  one of the enum options. Also directly exercised the new embed-building
  logic standalone (no live bot needed) to confirm the field layout reads
  correctly: team summary fields first (mirroring the announcement),
  then a bolded Verdict field, then the Analysis paragraph — in that
  order, matching how Discord renders fields (in add-order, independent
  of the embed's description).

  **Measured real cost, not estimated** (2026-07-11): a real generation
  against the real Kawhi/AD trade cost **$0.0213/call** ($0.013 input +
  $0.008 output) — input tokens roughly doubled vs. before this session's
  changes (1,190 → 2,596), almost entirely from the tool schema itself
  (the verdict enum spells out every team name twice). Real output
  quality checked at the same time: verdict ("Slightly favors Horny
  Mushrooms") genuinely agreed with the narrative's own reasoning, never
  leaked the hidden number, correctly stuck to given facts. In absolute
  terms this is a non-issue regardless — trade grades fire per-trade, not
  on a schedule, and this league's had 5 trades total since the offseason
  restart, so even a 2-3x per-call increase is a few cents a month.

  **Widened the "team avoidance" framing** (2026-07-11, user feedback on
  first draft): the earlier version of the general-knowledge rule named
  the forbidden category too narrowly ("current NBA team, coaching
  staff, teammate/depth-chart context"). Reframed around the actual
  underlying principle instead — anything concrete and situational that
  could go stale within weeks or months, with team/coach/depth-chart/
  contract-status as EXAMPLES, not the exhaustive rule — and explicitly
  added "perceived consensus rising/cooling" alongside role-trend and
  general development/decline as durable, always-fair-game commentary.

  **Pick-owner attribution restored + enriched with real record data**
  (2026-07-11, direct follow-up to the "would an objective pick-value
  formula help" question). `tradegrades.py`'s pick parser had silently
  dropped whose pick it originally is — `tradestracker.py`'s announcement
  embed shows "a 2027 1st-round pick (Horny Mushrooms)"; the trade-grade
  version had stripped that down to a bare "a 2027 1st-round pick".
  Decided AGAINST building an objective pick-value formula (no real
  market exists for a homegrown dynasty rookie pool the way one exists
  for the actual NBA draft — a formula would just be an invented opinion
  wearing a computed-number costume, exactly what "pick-value charts are
  opinions, not lookups" already ruled out). Instead restored the owner
  name (`_pick_description()` now returns `{"description", "owner_name"}`
  instead of a bare string) and went one step further: `analyze_trade()`
  resolves the owning team's CURRENT record too (`_resolve_picks()`),
  since this league drafts in reverse-standings order — a real fact, not
  a formula, that's still a meaningful proxy (a bad team's future 1st is
  worth more than a good team's). `record_lookup()` already covers every
  team in the league, not just trade participants, since a pick's
  original owner need not be part of this trade at all (picks change
  hands more than once) — no extra API call needed.

  Caught a real issue verifying this against ALL of this league's actual
  trades with picks (not just the one already-used Kawhi/AD example):
  every team's record currently reads 0-0 (confirmed live — genuinely
  the offseason, nobody's played yet), which would have made "currently
  0-0" dead noise shown identically on every pick. Suppressed the record
  tag specifically when win+loss == 0 (falls back to just the owner name)
  — this starts showing real, differentiating records automatically once
  the season begins, no further code change needed, same "verify against
  real data before shipping" catch as several other features this
  session.

**Multi-paragraph narrative + description-based layout (2026-07-11).**
User feedback after seeing the first real output: liked the content, but
a single 2-4 sentence paragraph read as a dense block of text, especially
undesirable for a bigger trade where there's more to actually say.
Relaxed `_SYSTEM_PROMPT`'s narrative rule from "ONE paragraph, 2-4
sentences" to "1-3 short paragraphs, scaling with the trade's size/
complexity" — no rigid per-team template, just organic paragraph breaks
(e.g. immediate impact / risk factors / other side's timeline), left to
Claude's judgment on how many. Bumped `max_tokens` 1024→2048 to give the
now-longer output (plus adaptive thinking's own spend) headroom.

This broke the embed layout, though: Discord field VALUES cap at 1024
characters, and a real 2-3 paragraph narrative comfortably exceeds that
(measured: a real 3-paragraph output ran ~900 characters on its own,
before the verdict). Moved verdict+narrative out of a field and into the
embed's DESCRIPTION instead (4096-character budget, no realistic
overflow risk) — `f"**{verdict}**\n\n{narrative}"`. Tradeoff: Discord
always renders description before fields regardless of add-order, so
this flips the visual order from the original design (trade-recap-first,
analysis-after) to take-first/trade-recap-below. Judged this an
improvement, not just a compromise — real trade-reaction content usually
leads with the take anyway (bold headline reaction up top, details as a
secondary recap below), and it robustly solves the length problem
instead of hoping paragraphs stay under ~900 characters.

Verified with a real (paid) generation against the real Kawhi/AD trade:
genuinely used 3 natural paragraphs (on-court impact → age/durability
risk → the other side's timeline payoff), verdict ("Slightly favors
Horny Mushrooms") agreed with the narrative's own conclusion, no leaked
numbers, no stale team-specific claims — a real, hedged "durability
questions... value curve pointed down" aging-stars read without naming
any specific NBA team. Real cost this call: $0.0244 (2,805 input +
414 output tokens) — up slightly from the $0.0213 baseline measured
right after the tool-call rewrite, expected given the longer allowed
output, still trivial at this league's real trade frequency.

**Reverted to fields-first layout, added field-chunking + inline
markdown (2026-07-11, same conversation).** User liked the multi-
paragraph content but preferred the ORIGINAL field-based layout (trade
assets on top, 📈/📝 emoji section headers) over the description-based
one from the previous change — and asked for bold/emoji usage within the
paragraphs themselves, not just as section labels.

Moved verdict+narrative back into `embed.add_field()` calls (trade-
summary fields render first again), but this reintroduces the exact
1024-character field cap problem the description move had sidestepped —
so rather than accept truncation risk, added `_chunk_field_value()`
(`cogs/tradegrades.py`): splits a too-long narrative at paragraph
boundaries into multiple "📝 Analysis" fields (continuation fields reuse
the blank-name-field convention already used for team fields), hard-
splitting a single over-limit paragraph only as a last resort. This
wasn't just theoretical — verified with the exact real narrative from
the previous real call: it was 1,075 characters, genuinely over the
1024 cap, and the chunker split it cleanly at the paragraph boundary
between the risk paragraph and the Knights' side rather than cutting off
mid-sentence.

Also relaxed `_SYSTEM_PROMPT`'s narrative rule to explicitly encourage
light inline Discord markdown — **bold** on a player's name at first
mention or the trade's real turning point, a sparing emoji or two where
it actually lands (⚠️ for risk, 🔥 for strength) — while still banning a
Claude-authored heading/label (redundant with the code-added "📝
Analysis" field name). Verified with a real generation: bolded player
names correctly, used exactly one ⚠️ in the risk paragraph (not spammed),
and separately used *italics* for emphasis unprompted — reads sharp, not
listicle-y.
