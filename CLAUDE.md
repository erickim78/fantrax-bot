# Fantrax Discord Bot — Project Context

Discord bot for a 10-team dynasty fantasy basketball league (Quail Hill
Invitational) on Fantrax, using discord.py cogs + the unofficial
`fantraxapi` Python wrapper.

## Structure

```
main.py              — bot entrypoint, loads all cogs from cogs/
config.py             — plain module-level config (botToken, clientID,
                         myGuild, leagueId, conchResponses, teamRoleIds,
                         transactionChannelId). No dotenv, no env vars —
                         just hardcoded values imported as config.xyz.
cogs/commands.py      — misc slash commands (/askshams, /scoreboard,
                         /standings). Stateless, no local file I/O.
cogs/transactionstracker.py — auto-posts adds/drops/waivers to Discord.
                         Has local file state (posted_transactions.json)
                         and several /xdebug slash commands used to
                         reverse-engineer the Fantrax API's undocumented
                         raw data structure (see below).
```

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

## Discord formatting conventions

- Trade announcements (manual, for now): Shams/Woj breaking-news style,
  `🚨 BREAKING:` / `🚨🚨 BLOCKBUSTER:` for bigger ones, role-tagged teams,
  bolded player/pick names, one line per trade, no analysis.
- Transaction announcements (automated, `transactionstracker.py`): same
  tweet-style brevity. `📝` for any add (whether or not paired with a
  drop), `✂️` for drop-only. FAAB bid shown as `for $X.00` inline, not as
  a running balance. NOT wrapped in code blocks (see gotcha #5).
- User explicitly wants trades and transactions to share the SAME
  `#news` channel (not split into separate channels) — emphasis for
  trades will come from the `🚨`/`🚨🚨` escalation and (agreed but not yet
  built) a `▬▬▬` divider around trade posts, not from channel separation
  or role pings (explicitly declined pinging `General Manager` role).

## Next steps (where we left off)

**Trade poster status:** built — `cogs/tradestracker.py`, a separate cog
from `transactionstracker.py` for modularity (own hourly loop, own
`posted_trades.json` state file, dedup by `txSetId`). Reads *executed*
trades off the public unauthenticated endpoint (see gotcha #9) rather
than `pending_trades()`, so no login/cookie flow was needed after all —
an earlier version of this plan added `config.fantraxCookies` and a
`requests.Session`-based login for `pending_trades()`, but that was
scrapped once the no-login `view="TRADE"` path was found; don't
reintroduce it without a real need. Formatting matches the
`▬▬▬`-divider/`#news`-sharing conventions above, escalating `🚨`→`🚨🚨`
at 3+ combined players+picks moved (a size heuristic, not real editorial
judgment). `DRY_RUN = True` by default — flip once `/tradedebug` output
looks right, same two-step rollout `transactionstracker.py` used.
Not yet verified against a live unattended run on the NAS.

**Transaction announcer status:** fully built and formatting-complete
(tweet-style, bid amounts included). Was mid-way through final NAS
verification (confirm state persists across container restart, confirm
no double-posting, let it run one real unattended cycle) before
switching `config.transactionChannelId` from test channel to `#news`
to go live — check whether that's been completed before assuming it's
live in production.
