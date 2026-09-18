# Dependencies
from collections import deque

# Fantrax
from fantraxapi import FantraxAPI
from fantraxapi import api as fantrax_api_module
from fantraxapi.objs.player import Player
from fantraxapi.objs.standings import Record


# ─────────────────────────────────────────────────────────────────────────
# LEAGUE ROSTER CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────
# Confirmed live via the raw getTeamRosterInfo(view="STATS") response
# (see CLAUDE.md gotcha) — statusTotals showed Active max=12, Reserve
# max=18, Injured Reserve max=4 (30-man roster, 12-man daily active
# lineup), and the 12 Active-status rows' posId sequence decoded to
# exactly this slot breakdown. This is a league-wide setting (identical
# for every team), confirmed against one team's roster — not something
# that needs re-deriving per team.
#
# Eligibility is checked directly against each Player's `all_positions`
# short names (e.g. a PG/SG dual-eligible player's all_positions already
# includes "G" and "Flx") — Fantrax pre-expands flex eligibility for us,
# so no hand-coded PG/SG→G, SF/PF→F hierarchy is needed.
ACTIVE_SLOT_TEMPLATE = [
    ("PG", 1), ("SG", 1), ("G", 2),
    ("SF", 1), ("PF", 1), ("F", 2),
    ("C", 1), ("Flx", 3),
]
ACTIVE_SLOT_COUNT = sum(count for _, count in ACTIVE_SLOT_TEMPLATE)  # 12


# ─────────────────────────────────────────────────────────────────────────
# MIN-COST FLOW — daily lineup optimizer
# ─────────────────────────────────────────────────────────────────────────
# Why min-cost flow instead of a greedy "fill the narrowest slot first"
# heuristic: the slot-eligibility structure isn't a clean nested hierarchy
# — e.g. a PG-only player and a SG-only player both feed the "G" flex
# slot without either containing the other. A greedy exchange argument
# can't be proven correct for that shape without real risk of a subtle,
# hard-to-notice ranking bug. Min-cost flow is guaranteed-optimal
# regardless of the eligibility structure. Graph size here is tiny (~45
# nodes, a few hundred edges, at most 12 augmentations), so a plain
# Bellman-Ford-based successive-shortest-paths solver is easily fast
# enough — no need for scipy (a container running at a 256MB memory
# limit is worth keeping light) or a more sophisticated algorithm.
class _MinCostFlow:
    def __init__(self, n: int):
        self.n = n
        self.graph = [[] for _ in range(n)]
        self.edges = []  # [to, cap, cost] per edge; edge i and i^1 are the forward/reverse pair

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> int:
        edge_id = len(self.edges)
        self.graph[u].append(edge_id)
        self.edges.append([v, cap, cost])
        self.graph[v].append(edge_id + 1)
        self.edges.append([u, 0, -cost])
        return edge_id

    def min_cost_flow(self, s: int, t: int, max_flow: int) -> tuple:
        total_flow = 0
        total_cost = 0
        while total_flow < max_flow:
            dist = [float("inf")] * self.n
            dist[s] = 0
            in_queue = [False] * self.n
            prev_edge = [-1] * self.n
            q = deque([s])
            in_queue[s] = True
            while q:
                u = q.popleft()
                in_queue[u] = False
                for edge_id in self.graph[u]:
                    v, cap, cost = self.edges[edge_id]
                    if cap > 0 and dist[u] + cost < dist[v]:
                        dist[v] = dist[u] + cost
                        prev_edge[v] = edge_id
                        if not in_queue[v]:
                            q.append(v)
                            in_queue[v] = True
            if dist[t] == float("inf"):
                break  # no more augmenting paths

            path_flow = max_flow - total_flow
            v = t
            while v != s:
                edge_id = prev_edge[v]
                path_flow = min(path_flow, self.edges[edge_id][1])
                v = self.edges[edge_id ^ 1][0]

            v = t
            while v != s:
                edge_id = prev_edge[v]
                self.edges[edge_id][1] -= path_flow
                self.edges[edge_id ^ 1][1] += path_flow
                v = self.edges[edge_id ^ 1][0]

            total_flow += path_flow
            total_cost += path_flow * dist[t]
        return total_flow, total_cost


def solve_daily_lineup(candidates: list) -> tuple:
    """candidates: list of (candidate_id, eligible_positions: set[str], fp_g: float)
    — one entry per player who has a game on this day. Returns
    (total_fp, assignments: dict[candidate_id, slot_position]) for the
    OPTIMAL lineup under ACTIVE_SLOT_TEMPLATE. Players with no eligible
    slot that day, or beyond what 12 slots can hold, are simply left out
    of `assignments` — this is expected, not an error (e.g. a low-game
    day where fewer than 12 rostered players even have games)."""
    if not candidates:
        return 0.0, {}

    SCALE = 100  # integer-scale fp_g to 2 decimal places for exact-integer flow costs

    num_slots = len(ACTIVE_SLOT_TEMPLATE)
    num_candidates = len(candidates)
    source = 0
    slot_base = 1
    cand_base = slot_base + num_slots
    sink = cand_base + num_candidates
    n = sink + 1

    mcf = _MinCostFlow(n)
    for i, (_pos, count) in enumerate(ACTIVE_SLOT_TEMPLATE):
        mcf.add_edge(source, slot_base + i, count, 0)
    for j, (_cand_id, _eligible, _fp_g) in enumerate(candidates):
        mcf.add_edge(cand_base + j, sink, 1, 0)

    slot_cand_edge = {}
    for i, (pos, _count) in enumerate(ACTIVE_SLOT_TEMPLATE):
        for j, (_cand_id, eligible, fp_g) in enumerate(candidates):
            if pos in eligible:
                cost = -round(fp_g * SCALE)
                slot_cand_edge[(i, j)] = mcf.add_edge(slot_base + i, cand_base + j, 1, cost)

    max_possible = min(ACTIVE_SLOT_COUNT, num_candidates)
    _flow, cost = mcf.min_cost_flow(source, sink, max_possible)

    assignments = {}
    for (i, j), edge_id in slot_cand_edge.items():
        if mcf.edges[edge_id][1] == 0:  # forward capacity dropped from 1 to 0 => this edge carried flow
            assignments[candidates[j][0]] = ACTIVE_SLOT_TEMPLATE[i][0]

    total_fp = -cost / SCALE
    return total_fp, assignments


# ─────────────────────────────────────────────────────────────────────────
# ROSTER DATA — bypasses the library's Roster() wrapper
# ─────────────────────────────────────────────────────────────────────────
# fantraxapi's League.team_roster() crashes (KeyError) any time
# league.scoring_dates is empty, which is the case for this whole league
# every off-season — Roster.__init__ unconditionally looks up
# league.scoring_dates[self.period_number]. We only need the roster's
# player list, position eligibility, FP/G, and (for a specific period)
# day-by-day game schedule here — not the wrapper's own date handling —
# so we call the raw API function directly and parse both the STATS and
# SCHEDULE_FULL views ourselves, sidestepping that crash entirely, same
# workaround pattern as the Standings.ranks tie-collision bug.
def fetch_team_period_data(api: FantraxAPI, team_id: str, period_number: int = None) -> tuple:
    """Returns (players, day_schedules).

    players: list of (Player, fantasy_points_per_game, fpts_total, gp,
    age, status) for every non-empty roster row, regardless of season
    state. status is "Active"/"Reserve"/"Inj Res" per Fantrax's own
    designation. fpts_total (season total fantasy points, STATS column
    sortKey "SCORE") and gp (games played, STATS column shortName "GP" —
    its own sortKey is a league-scoring-category composite, e.g.
    "SCORING_CATEGORY_3010#1350#-1", not a portable constant, so match by
    shortName instead) are kept alongside fp_g so callers can derive a
    genuine trailing-window scoring rate later (see
    compute_player_trends()) by diffing two weeks' totals, instead of
    only ever seeing the season-cumulative average. Confirmed live that
    fp_g == fpts_total / gp exactly (e.g. 3773 / 68 = 55.49) — these are
    the same underlying numbers Fantrax computes its own FP/G column
    from, not a separate estimate. age (STATS column sortKey "AGE") is a
    real player attribute (not derived, not blended), added for
    tradegrades.py's dynasty-timeline narration — irrelevant to the
    power-rankings projection itself, just riding along on the same
    already-fetched row.

    day_schedules: list of sets, one set per date column in whatever
    window SCHEDULE_FULL actually returns (see below — NOT necessarily
    scoped to just `period_number`), each containing the player_ids with
    a game that day. Built by matching STATS rows to SCHEDULE_FULL rows
    position-by-position, same zip-based approach fantraxapi's own
    Roster.__init__ uses internally.

    UPDATED (2026-09-17, supersedes an earlier "untestable until the
    season starts" note): confirmed live that SCHEDULE_FULL now DOES
    return real per-date columns pre-season — real per-player, per-date
    game data going out roughly a month (e.g. confirmed live: Oct 20
    through Nov 19 as of today). This flips only once the calendar gets
    close enough to the season for Fantrax to have published real
    schedule data; before that point it genuinely still returns only
    season-total columns (Age/FPts/FP-G), same empty-list-not-a-crash
    behavior as before — still no callable way to distinguish "too early
    in the offseason" from "no games this specific day" other than an
    empty day_schedules list either way.

    IMPORTANT: confirmed live that `period_number` does NOT scope which
    date columns come back right now — periods 1, 3, 10, and 19 all
    return the byte-identical ~30-day window. It's echoed back in the
    response's displayedSelections.displayedPeriod field without
    actually filtering anything server-side (same pattern already
    confirmed for the STATS view's season selector below). Whether real
    per-period scoping starts working once the season is actually live
    is genuinely unverifiable until that happens. compute_power_rankings()
    already accounts for this — see its module-level note — by making a
    single request and using every date column that comes back, rather
    than assuming distinct period_number values yield distinct windows.

    Separately confirmed live that the STATS view's fp_g/fpts_total/gp
    columns silently transition over the course of the offseason from
    real last-completed-season stats to Fantrax's own "Projected -
    Season" values (an opaque third-party projection) — see
    current_stats_source() and CLAUDE.md's write-up. Also still true:
    passing a raw "goBackDays" param (mirroring the site's window-
    selector UI) has no effect on the STATS view — the real windowed-
    stats mechanism needs the same kind of structured selector object
    that doesn't respond to simple params through this API path.
    """
    responses = fantrax_api_module.get_team_roster_info(api, team_id, period_number=period_number)
    stats_resp, sched_resp = responses

    status_by_id = {s["id"]: s["name"] for s in stats_resp["miscData"]["statusTotals"]}
    stats_header = stats_resp["tables"][0]["header"]["cells"]
    fpg_index = next((i for i, c in enumerate(stats_header) if c.get("sortKey") == "FPTS_PER_GAME"), None)
    fpts_index = next((i for i, c in enumerate(stats_header) if c.get("sortKey") == "SCORE"), None)
    gp_index = next((i for i, c in enumerate(stats_header) if c.get("shortName") == "GP"), None)
    age_index = next((i for i, c in enumerate(stats_header) if c.get("sortKey") == "AGE"), None)
    stats_rows = stats_resp["tables"][0]["rows"]

    sched_header = sched_resp["tables"][0]["header"]["cells"]
    sched_rows = sched_resp["tables"][0]["rows"]
    date_column_indices = [i for i, h in enumerate(sched_header) if h.get("eventStr")]

    players = []
    day_schedules = [set() for _ in date_column_indices]

    def _cell_float(cells: list, index) -> float:
        if index is None or index >= len(cells) or not cells[index].get("content"):
            return 0.0
        try:
            return float(cells[index]["content"])
        except ValueError:
            return 0.0

    for stats_row, sched_row in zip(stats_rows, sched_rows):
        if "scorer" not in stats_row:
            continue  # empty roster slot
        player = Player(api, stats_row["scorer"])
        cells = stats_row.get("cells", [])
        fp_g = _cell_float(cells, fpg_index)
        fpts_total = _cell_float(cells, fpts_index)
        gp = _cell_float(cells, gp_index)
        age = _cell_float(cells, age_index)
        status = status_by_id.get(stats_row.get("statusId"), "Unknown")
        players.append((player, fp_g, fpts_total, gp, age, status))

        sched_cells = sched_row.get("cells", [])
        for day_idx, col_idx in enumerate(date_column_indices):
            if col_idx < len(sched_cells) and sched_cells[col_idx].get("content"):
                day_schedules[day_idx].add(player.id)

    return players, day_schedules


def fetch_roster_players(api: FantraxAPI, team_id: str) -> list:
    """Roster only, no schedule — thin wrapper for callers (e.g. debug
    commands) that don't need a specific period's day-by-day data."""
    players, _day_schedules = fetch_team_period_data(api, team_id, period_number=None)
    return players


def current_stats_source(api: FantraxAPI) -> dict:
    """Returns {"is_projection": bool, "label": str} describing whatever
    season/projection view the STATS endpoint is CURRENTLY defaulting to.

    Confirmed live that this default silently shifts over the course of
    the offseason — real last-season "...Reg Season - YTD" stats right
    after the season ends (verified 2026-07: e.g. Shai Gilgeous-
    Alexander fp_g=55.49 over 68 real games — this is what tradegrades.py
    and the offseason power rankings were originally built/verified
    against), switching at some point to "Projected - Season" — an
    opaque third-party projection for the UPCOMING season (confirmed
    2026-09: same player showing fp_g=55.42 over a suspiciously round
    gp=70, and previously-zero-GP rookies/injured vets suddenly showing
    full projected lines). Fantrax controls exactly when that switch
    happens, not this codebase — and per 3 separate confirmed-failed
    attempts (see CLAUDE.md), there's no way to force one or the other
    through this API. There's no gap where NEITHER is available — the
    default is always one or the other, so no code change is needed to
    "ride" the transition; the only thing that goes stale without this
    detection is any hardcoded "based on last season's stats" caveat
    text, which becomes a false claim the moment the switch happens.

    Detects via timeframeTypeCode ("YEAR_TO_DATE" vs "PROJECTED_SEASON")
    rather than pattern-matching the "code" field, which embeds internal
    IDs (e.g. "41n") that likely shift every year. Makes one lightweight
    extra API call (any single team's roster info) — negligible given
    this is only ever called once per check cycle, not per-team."""
    any_team_id = next(iter(api.teams)).id
    responses = fantrax_api_module.get_team_roster_info(api, any_team_id, period_number=None)
    stats_resp = responses[0]
    selection = stats_resp["displayedSelections"]["displayedSeasonOrProjection"]
    return {
        "is_projection": selection.get("timeframeTypeCode") == "PROJECTED_SEASON",
        "label": selection.get("name", "unknown"),
    }


# ─────────────────────────────────────────────────────────────────────────
# WINDOW SELECTION & SIMULATION
# ─────────────────────────────────────────────────────────────────────────
# Single-window design (NOT the originally-planned staggered two-week
# design — see the superseded rationale this replaced, below the
# history note). This was ORIGINALLY built as two non-adjacent weeks (skip
# one in between) via two separate SCHEDULE_FULL requests with different
# period_number values, specifically to decorrelate one team's schedule-
# density luck (back-to-back clusters, road trips run in multi-week
# phases) from another's.
#
# CONFIRMED LIVE (2026-09-17) that this design was silently broken: the
# `period` parameter sent to getTeamRosterInfo(view="SCHEDULE_FULL") is
# ECHOED BACK in the response's displayedSelections.displayedPeriod
# field, but does NOT actually scope which date columns come back —
# tested period=1, 3, 10, and 19 and got the byte-identical date range
# every time (Oct 20 – Nov 19, a fixed ~30-day window). Same client-
# echoed-without-server-effect pattern already confirmed for the STATS
# view's displayedSeasonOrProjection selector (see CLAUDE.md's season-
# selector investigation) — a parameter the UI clearly uses for
# server-side filtering somewhere is either not wired the same way
# through this raw API path, or genuinely doesn't apply until the
# season is actually live and there's a real "current period" for the
# server to reason about relative offsets from (untestable until then).
#
# Practical effect: requesting "period_a" and "period_b" was silently
# fetching and simulating the EXACT SAME ~30 real days twice and summing
# them — not a crash, not obviously-wrong-looking output (points_per_day
# still landed in a sane range, since both total_points and total_days
# were inflated by the same 2x factor), but not what the two-request
# design claimed to be doing, and wasted an API call. Fixed by making a
# SINGLE request and using every date column that naturally comes back —
# which, at ~30 days, already covers a wider and more representative
# window than the original 2×7-day stagger was reaching for, so the
# "decorrelate schedule luck" goal is still met, just via a bigger single
# sample instead of two small stitched-together ones. period_number is
# still passed through (rather than removed) in case real per-period
# scoping starts working correctly once the season is actually live —
# genuinely unverifiable until that happens, same as several other
# pre-season assumptions in this file.
def _upcoming_period_number(api: FantraxAPI, today):
    """Returns the scoring period number containing or immediately after
    today, or None if there are no remaining periods this season. See
    the module-level note above for why this is passed through to
    fetch_team_period_data()/simulate_team_period() despite not
    currently affecting what SCHEDULE_FULL returns."""
    upcoming = sorted(
        (sp for sp in api.scoring_periods.values() if sp.end >= today),
        key=lambda sp: sp.start,
    )
    return upcoming[0].number if upcoming else None


# Recent-form blending — how much a player's simulated value leans on
# their recent scoring rate (since the last real post) vs. their season-
# to-date average. 0.35 means the season average still dominates (season
# sample size is much larger and more stable), but a real recent
# breakout/decline/return-from-injury moves the projection meaningfully
# instead of waiting weeks for the cumulative average to catch up.
# Tunable here directly (not config.py — this is an algorithm knob, not
# a league fact like the tier name).
RECENT_FORM_WEIGHT = 0.35
MIN_RECENT_GAMES = 1  # below this many games since the last snapshot, the recent rate isn't trusted


def _recent_fpg(fp_g: float, fpts_total: float, gp: float, player_id,
                 previous_player_stats: dict, min_recent_games: int = MIN_RECENT_GAMES):
    """Returns this player's fantasy points/game over just the games
    played SINCE previous_player_stats' snapshot (fpts_total/gp diffed
    against it), or None if there's no prior snapshot for this player or
    too few recent games to trust a rate. Shared by compute_player_
    trends() (blurb narration) and _build_player_lookup() (projection
    value blending) so both draw the "how has this player played lately"
    signal from the exact same derivation — see compute_power_rankings'
    docstring for why fpts_total/gp (not just fp_g) are needed for this."""
    if not previous_player_stats:
        return None
    prev = previous_player_stats.get(str(player_id))
    if prev is None:
        return None
    recent_gp = gp - prev["gp"]
    if recent_gp < min_recent_games:
        return None
    return (fpts_total - prev["fpts"]) / recent_gp


def _build_player_lookup(players: list, previous_player_stats: dict = None,
                          recent_weight: float = RECENT_FORM_WEIGHT,
                          availability_filter: str = "strict") -> dict:
    """players: fetch_team_period_data()'s raw (Player, fp_g, fpts_total,
    gp, status) tuples. Returns {player_id: (Player, value)} for every
    player eligible to be simulated.

    availability_filter controls which unavailability signals zero a
    player out entirely. Confirmed live (2026-07-11) that Fantrax's own
    icons distinguish real severity, which an earlier version of this
    function ignored (single injured-or-suspended check, day-to-day
    treated identically to out-indefinitely):
      - "strict" (default): excludes Inj Res roster-slot status, plus
        Player.out/injured_reserve/suspended — the real near-term-
        unavailability signals, appropriate for a production FORECAST
        (the real day-by-day power-rankings simulation below).
        Deliberately does NOT exclude pure day_to_day ("game-time
        decision") status on its own: confirmed live this tag is
        meaningless without an actual game to be a decision about (94 of
        98 currently-flagged players league-wide are day_to_day-only,
        almost all tied to real NBA news that won't matter again until
        the season starts), and even in-season it's too short-term/noisy
        by itself to justify zeroing a player's whole simulated value.
      - "none": excludes nobody on availability grounds at all — for
        trade grading (see lineup_ceiling()), where value should reflect
        a player's underlying asset quality/track record, not their
        current health status. An "out indefinitely" star still has real
        trade value once healthy.
      - "offseason" (added 2026-07-16): excludes Player.out/injured_
        reserve/suspended — Fantrax's own REAL status designations —
        but deliberately does NOT exclude on status == "Inj Res", unlike
        "strict". Confirmed live this matters: "Inj Res" is a FANTASY
        roster-SLOT placement the manager chooses (often just to free up
        an Active/Reserve spot, since Inj Res slots don't count against
        those caps) — a completely separate signal from Player.
        injured_reserve, Fantrax's own real "this player is actually on
        the NBA team's injured list" flag. Found a real case: a player
        sitting on his fantasy team's Inj Res slot with
        Player.injured_reserve == False (Fantrax itself doesn't
        currently flag him injured at all) — being zeroed out purely by
        a roster-management choice, not any real signal about his
        actual status heading into next season. Used by
        compute_offseason_power_rankings() instead of "strict" for
        exactly this reason — that ranking should reflect a player's
        actual real-world status, not how their fantasy manager happens
        to be using roster slots for cap-space reasons.

    value blends each player's season-average fp_g with their recent-
    form rate (see _recent_fpg()), weighted by recent_weight, falling
    back to pure fp_g when there's no recent rate to blend in — so the
    simulation responds to current form instead of only a slow-moving
    season average. This is SEPARATE from roster_players' fp_g field
    (compute_power_rankings' output used for narration/trend detection),
    which stays the pure season average on purpose — compute_player_
    trends() computes its own "recent vs. season baseline" comparison,
    and blending the baseline itself would double-count recent
    performance and dampen the signal it's trying to detect."""
    lookup = {}
    for p, fp_g, fpts_total, gp, _age, status in players:
        if availability_filter == "strict":
            if status == "Inj Res" or p.out or p.injured_reserve or p.suspended:
                continue
        elif availability_filter == "offseason":
            if p.out or p.injured_reserve or p.suspended:
                continue
        recent_fpg = _recent_fpg(fp_g, fpts_total, gp, p.id, previous_player_stats)
        value = fp_g if recent_fpg is None else (1 - recent_weight) * fp_g + recent_weight * recent_fpg
        lookup[p.id] = (p, value)
    return lookup


def lineup_ceiling(roster: list, previous_player_stats: dict = None,
                    availability_filter: str = "none") -> float:
    """roster: fetch_team_period_data()/fetch_roster_players()-shaped
    list of (Player, fp_g, fpts_total, gp, age, status) tuples. Returns
    the optimal lineup's total value treating EVERY eligible player as
    if they had a game today — NOT a real day's simulation (no
    SCHEDULE_FULL/day_schedules dependency), so this works even pre-
    season/offseason. Just "what's this exact roster's active-lineup
    ceiling right now."

    Built for tradegrades.py: the delta between this computed on a
    team's roster before vs. after a trade is the TRUE lineup-value
    gain/loss, and it accounts for positional depth/logjam automatically
    — a redundant player at an already-crowded position barely moves the
    ceiling, because the optimizer only has so many slots to fill,
    regardless of raw fp_g. No hand-written positional-scarcity rule
    needed; this is the same reason the real day-by-day power-rankings
    simulation doesn't need one either.

    availability_filter: see _build_player_lookup() — defaults to "none"
    here (unlike simulate_team_period()'s "strict") since lineup_ceiling
    was built for trade grading, an asset-VALUE question, not a near-
    term production FORECAST; a player's current health status shouldn't
    zero out their trade value. Only trade grading uses this function
    now — compute_offseason_power_rankings() originally used this as a
    schedule-independent stand-in for a real forecast, but was upgraded
    (2026-09-17) to simulate_team_period() once real day-by-day schedule
    data became available pre-season; a ~30-deep roster's ceiling only
    ever credits the best 12, leaving real bench depth invisible to a
    team-strength ranking specifically."""
    lookup = _build_player_lookup(roster, previous_player_stats, availability_filter=availability_filter)
    candidates = [
        (player_id, {pos.short_name for pos in player.all_positions}, value)
        for player_id, (player, value) in lookup.items()
    ]
    total, _assignments = solve_daily_lineup(candidates)
    return total


def simulate_team_period(api: FantraxAPI, team_id: str, period_number: int,
                          previous_player_stats: dict = None,
                          availability_filter: str = "strict",
                          manual_stat_overrides: dict = None) -> list:
    """Returns a list of that day's optimal-lineup total_fp, one entry
    per date column in whatever window SCHEDULE_FULL actually returns
    (empty list pre-season, or before real per-date schedule data
    exists — see fetch_team_period_data()). previous_player_stats:
    optional {player_id: {"fpts", "gp"}} — when given, blends each
    player's simulated value toward their recent form; see
    _build_player_lookup().

    availability_filter: see _build_player_lookup() — defaults to
    "strict" (real near-term unavailability should count against a team
    in a genuine production forecast; a fantasy manager's Inj Res SLOT
    placement counts too, since here it's plausibly a real signal about
    who's actually playing). compute_offseason_power_rankings() passes
    "offseason" instead — same simulation mechanism, different
    unavailability semantics (roster-slot placement alone shouldn't
    zero a player out there — see that tier's docstring).

    manual_stat_overrides: optional {player_name: fp_g} — see config.
    manualStatOverrides' docstring. Applied the same way compute_
    offseason_power_rankings() applied it directly to lineup_ceiling()
    before this function took over that job: patches in the real fp_g
    (and status, to bypass "strict"'s Inj Res gate if that ever matters
    for an override candidate) for any gp==0 player whose name is in the
    dict, before building the day's lineup candidates. Not used by the
    real in-season compute_power_rankings() — that path always has a
    real current-season fp_g source, nothing to override."""
    players, day_schedules = fetch_team_period_data(api, team_id, period_number=period_number)
    if manual_stat_overrides:
        players = [
            (p, manual_stat_overrides[p.name], fpts_total, gp, age, "Active")
            if gp == 0 and p.name in manual_stat_overrides
            else (p, fp_g, fpts_total, gp, age, status)
            for p, fp_g, fpts_total, gp, age, status in players
        ]
    player_lookup = _build_player_lookup(players, previous_player_stats, availability_filter=availability_filter)

    day_totals = []
    for available_ids in day_schedules:
        candidates = []
        for player_id in available_ids:
            if player_id not in player_lookup:
                continue
            player, value = player_lookup[player_id]
            eligible = {pos.short_name for pos in player.all_positions}
            candidates.append((player_id, eligible, value))
        total, _assignments = solve_daily_lineup(candidates)
        day_totals.append(total)
    return day_totals


def compute_power_rankings(api: FantraxAPI, today=None, previous_player_stats: dict = None) -> list:
    """Returns [(team, total_points, total_days, points_per_day,
    roster_players), ...] for every team in the league, sorted descending
    by points_per_day (not raw total — normalizes away any difference in
    how many real games each team happens to have in the simulated
    window, e.g. a team whose players' NBA teams play more games in that
    stretch than another's). Empty per-team results (points_per_day=0.0)
    are expected, not an error, until the season starts.

    previous_player_stats: optional {player_id: {"fpts", "gp"}} from
    last week's REAL post (see cogs/powerrankings.py's
    _load_previous_player_stats()). When given, each player's simulated
    value blends toward their recent form instead of relying solely on
    season-average fp_g — see simulate_team_period()/_build_player_
    lookup() for the exact derivation. Omitting it (the default)
    simulates on pure season-average fp_g, same as before this feature —
    not required for a working ranking, just a more responsive one.

    roster_players: list of (player_id, player_name, fp_g, fpts_total,
    gp, age) for EVERY active-eligible player on that team's roster (Inj
    Res excluded), sorted descending by fp_g — not sliced to a top-N; callers
    wanting "top players for context" (e.g. generate_power_rankings_
    writeup()) slice roster_players[:6] themselves. Fetched via a
    separate fetch_roster_players() call per team (one extra API call
    each, negligible given this only runs ~weekly) rather than threading
    it through simulate_team_period(), to keep that function's job (one
    thing: sum optimal daily lineups) unchanged. fp_g here is ALWAYS the
    pure season-to-date average, never the recent-form-blended value
    used internally by the simulation above — this field feeds narration
    and compute_player_trends()'s own "recent vs. season baseline"
    comparison, which would be distorted by a baseline that's already
    partly blended toward recent. fpts_total/gp are the raw season
    totals fp_g is computed from, kept alongside it so compute_player_
    trends() can derive its own trailing-window rate. player_id
    (Player.id) is used instead of name for stable cross-week identity.
    This exists so the LLM narration can reference real players instead
    of inventing plausible-sounding-but-fabricated commentary from the
    aggregate score alone — see generate_power_rankings_writeup()."""
    import datetime
    if today is None:
        today = datetime.date.today()

    period_number = _upcoming_period_number(api, today)
    if period_number is None:
        return []

    results = []
    for team in api.teams:
        day_totals = simulate_team_period(
            api, team.id, period_number, previous_player_stats=previous_player_stats
        )
        total_points = sum(day_totals)
        total_days = len(day_totals)
        per_day = total_points / total_days if total_days else 0.0

        roster = fetch_roster_players(api, team.id)
        roster_players = _build_roster_players_field(roster)

        results.append((team, total_points, total_days, per_day, roster_players))

    results.sort(key=lambda r: r[3], reverse=True)
    return results


def _build_roster_players_field(roster: list, exclude_inj_res_slot: bool = True) -> list:
    """roster: fetch_roster_players()-shaped list of (Player, fp_g,
    fpts_total, gp, age, status) tuples. Returns [(player_id, name, fp_g,
    fpts_total, gp, age), ...] for every active-eligible player, sorted
    descending by fp_g — the roster_players field shared by compute_
    power_rankings() and compute_offseason_power_rankings(), so both
    feed narration/trend logic the exact same shape regardless of which
    ranking mechanism produced the rest of the row.

    exclude_inj_res_slot: True (default, matches compute_power_rankings'
    existing behavior) excludes players sitting on the fantasy roster's
    Inj Res SLOT. compute_offseason_power_rankings() passes False — see
    _build_player_lookup()'s "offseason" availability_filter docstring
    for why that roster-slot placement isn't a reliable real-status
    signal (a manager's roster-space choice, not necessarily reflecting
    actual injury status) — keeps this field consistent with which
    players actually count toward that ranking's ceiling."""
    active_eligible = sorted(
        (
            (p, fp_g, fpts_total, gp, age)
            for p, fp_g, fpts_total, gp, age, status in roster
            if not exclude_inj_res_slot or status != "Inj Res"
        ),
        key=lambda tup: tup[1],  # fp_g
        reverse=True,
    )
    return [
        (p.id, p.name, fp_g, fpts_total, gp, age)
        for p, fp_g, fpts_total, gp, age in active_eligible
    ]


# ─────────────────────────────────────────────────────────────────────────
# OFFSEASON MODE — same tier/format/narration machinery, different
# ranking mechanism (no live day-by-day simulation to lean on).
# ─────────────────────────────────────────────────────────────────────────
def compute_offseason_power_rankings(api: FantraxAPI, manual_stat_overrides: dict = None) -> list:
    """Returns [(team, total_points, total_days, points_per_day,
    roster_players), ...] for every team, sorted descending by
    points_per_day — SAME tuple shape as compute_power_rankings(), and
    (as of 2026-09-17) the SAME underlying mechanism: real day-by-day
    simulation via simulate_team_period() against whatever window
    SCHEDULE_FULL actually returns (see _upcoming_period_number()'s
    module note — currently ~30 real days regardless of which single
    period_number is requested). total_days now reflects that real day
    count, not a fixed 1.

    UPGRADED from a lineup_ceiling() snapshot to this real simulation —
    user's reasoning: rosters here run ~30 deep, and a lineup_ceiling()
    snapshot only ever credits whichever 12 players could theoretically
    fill the active slots if everyone had a game the same day, leaving
    18+ real rostered players completely invisible to the ranking
    regardless of how deep a team's bench actually is. The real
    simulation naturally values that depth instead — on any given real
    day, only players who actually have a game that day are even
    eligible for a slot, so a team with real bench depth gets real
    credit on nights its stars don't play, the same way the actual
    in-season version has always worked. This was only possible once
    real day-by-day schedule data became available pre-season (see the
    "Real day-by-day schedule data now exists" section of CLAUDE.md) —
    genuinely wasn't an option when this function was first built.

    availability_filter="offseason" (passed to simulate_team_period(),
    NOT its own "strict" default) — excludes real out/injured_reserve/
    suspended status but NOT a player sitting on their fantasy team's
    Inj Res roster SLOT. Confirmed live this matters: that slot is a
    manager's roster-space choice (frees up an Active/Reserve spot),
    NOT reliably a real injury signal — found a real case (a player with
    Player.injured_reserve == False, Fantrax's own data saying he's not
    actually hurt, zeroed out purely because his manager parked him on
    Inj Res). "strict" is right for the real in-season forecast (near-
    term unavailability should count there); this ranking is about
    actual real-world status heading into next season, which the roster
    slot doesn't reliably tell you. Still does NOT blend recent form (no
    previous_player_stats passed) — no meaningful 'recent' signal when
    real games for the CURRENT season haven't started yet.

    manual_stat_overrides: optional {player_name: fp_g} — see config.
    manualStatOverrides' docstring for the full reasoning. Passed
    straight through to simulate_team_period(), which now owns applying
    it (patches fp_g + status for any gp==0 player whose name is in the
    dict) — same effective behavior as when this function patched the
    roster itself before calling lineup_ceiling() directly. Still
    separately re-applied to the roster fetched for roster_players below
    — that's a SEPARATE fetch_roster_players() call for narration/debug-
    dump display purposes, not part of the simulation itself, so it
    needs its own patch to stay visually consistent with what the
    simulation actually counted."""
    import datetime
    today = datetime.date.today()
    period_number = _upcoming_period_number(api, today)
    if period_number is None:
        return []

    overrides = manual_stat_overrides or {}
    results = []
    for team in api.teams:
        day_totals = simulate_team_period(
            api, team.id, period_number,
            availability_filter="offseason", manual_stat_overrides=overrides,
        )
        total_points = sum(day_totals)
        total_days = len(day_totals)
        per_day = total_points / total_days if total_days else 0.0

        roster = fetch_roster_players(api, team.id)
        if overrides:
            roster = [
                (p, overrides[p.name], fpts_total, gp, age, "Active") if gp == 0 and p.name in overrides
                else (p, fp_g, fpts_total, gp, age, status)
                for p, fp_g, fpts_total, gp, age, status in roster
            ]
        roster_players = _build_roster_players_field(roster, exclude_inj_res_slot=False)

        results.append((team, total_points, total_days, per_day, roster_players))
    results.sort(key=lambda r: r[3], reverse=True)
    return results


# Data-driven tier split — deterministic, code-computed, NOT something the
# LLM decides (same "Claude narrates, never computes structure" principle as
# the ranking itself). Finds the N-1 largest consecutive-pair drops in the
# sorted points_per_day list and splits there, so tier boundaries reflect
# real clustering in the projections instead of a fixed index split.
#
# The bottom tier's name/emoji is deliberately NOT hardcoded below —
# cogs/powerrankings.py overrides it with config.lastPlaceTierName, since
# that name is tied to whatever the league's current last-place punishment
# actually is (changes season to season) rather than being a fixed joke
# baked into the code. This default is only used by standalone callers
# (tests, direct script use) that don't go through the cog.
TIER_NAMES = ("👑 Favorites", "🏆 Contenders", "🎲 Long Shots", "🥞 Pancake Contention")


def assign_tiers(rankings: list, tier_names: tuple = None) -> list:
    """Returns [(tier_name, [ranking_entries]), ...] preserving rank
    order, covering every entry in `rankings` exactly once.

    tier_names: names/emoji for each tier, top to bottom. Defaults to
    TIER_NAMES (4 tiers) if not given — pass a different-length tuple to
    get a different number of tiers; the split algorithm scales
    automatically (see below), no code change needed to add/remove a
    tier.

    Tier boundaries are picked by a "largest gap" heuristic: rankings is
    already sorted descending by points_per_day, so we look at the
    len(tier_names)-1 largest consecutive-pair drops in that sorted list
    and split there — tiers reflect real clustering in the projections
    (e.g. a runaway leader, a cluster of also-rans) instead of a fixed
    index split that never looked at the scores at all.

    This is a simple largest-gap split, NOT full Jenks natural-breaks
    optimization (which minimizes within-tier variance across every
    possible partition) — deliberately: 10 data points don't justify a
    heavier statistical method, and this module avoids numpy/scipy on
    purpose (256MB container memory limit — same reasoning as the
    from-scratch min-cost-flow solver above).

    For n>=len(tier_names) this always yields >=1 team per tier: the n-1
    consecutive gaps have distinct indices, so the top len(tier_names)-1
    by magnitude are always that many distinct split points. For smaller
    n there aren't enough teams to draw that many meaningful splits, so
    everyone goes in the middle-most tier rather than overclaiming a
    "Favorites"/"Pancake" signal that doesn't exist yet (effectively
    unreachable in this 10-team league)."""
    names = tier_names if tier_names is not None else TIER_NAMES
    num_tiers = len(names)
    n = len(rankings)
    if n == 0:
        return [(name, []) for name in names]
    if n < num_tiers:
        mid = num_tiers // 2
        return [(name, list(rankings) if i == mid else []) for i, name in enumerate(names)]

    scores = [r[3] for r in rankings]  # points_per_day, already sorted descending
    gaps = [(scores[i] - scores[i + 1], i) for i in range(n - 1)]
    k = num_tiers - 1
    top_k = sorted(gaps, key=lambda g: g[0], reverse=True)[:k]
    splits = sorted(i + 1 for _gap, i in top_k)  # split AFTER index i
    bounds = [0] + splits + [n]
    return [(names[i], rankings[bounds[i]:bounds[i + 1]]) for i in range(num_tiers)]


def format_tier_list(rankings: list, records: dict = None, tier_names: tuple = None,
                      role_ids: dict = None) -> str:
    """Deterministic, code-only rendering of the tier-grouped rank list
    — team names, rank numbers, and W-L record + streak (whenever a
    record is available). No LLM involvement here at all (unlike
    generate_power_rankings_writeup, which only writes the league-wide
    summary paragraph appended after this) — the rank list itself is
    pure structure, nothing to narrate. tier_names: see assign_tiers().

    role_ids: optional {team_id: discord_role_id} — when given, renders
    each team as a clickable role mention (<@&role_id>) instead of its
    plain name, falling back to the plain name for any team without a
    configured role. Kept as an explicit parameter rather than importing
    config.py directly — this module is deliberately Discord/config-
    independent (see CLAUDE.md); the calling cog supplies its own role
    mapping. Defaults to None (plain names, current behavior unchanged)
    so existing callers aren't affected unless they opt in."""
    tiers = assign_tiers(rankings, tier_names=tier_names)
    blocks = []
    rank = 1
    for tier_name, entries in tiers:
        lines = [f"**{tier_name}**"]
        for team, *_rest in entries:
            record = records.get(team.id) if records else None
            if record and record.streak:
                record_str = f" ({record.win}-{record.loss}, {record.streak})"
            elif record:
                record_str = f" ({record.win}-{record.loss})"
            else:
                record_str = ""
            role_id = role_ids.get(team.id) if role_ids else None
            team_display = f"<@&{role_id}>" if role_id else team.name
            lines.append(f"{rank}. {team_display}{record_str}")
            rank += 1
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ─────────────────────────────────────────────────────────────────────────
# STANDINGS ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────
# Same Standings.ranks tie-collision workaround as weeklyrecap.py (see
# CLAUDE.md gotcha #11) — duplicated here rather than imported from that
# cog to keep this module standalone/cog-independent.
def _all_records(standings) -> list:
    fields = {c["key"]: i for i, c in enumerate(standings._data["header"]["cells"])}
    return [
        Record(standings, row["fixedCells"][1]["teamId"], int(row["fixedCells"][0]["content"]), fields, row["cells"])
        for row in standings._data["rows"]
    ]


def record_lookup(api: FantraxAPI) -> dict:
    """Returns {team_id: Record} for every team's current standings —
    used to give the LLM narration real W-L/streak context per team."""
    standings = api.standings()
    return {r.team.id: r for r in _all_records(standings)}


# ─────────────────────────────────────────────────────────────────────────
# LLM NARRATION
# ─────────────────────────────────────────────────────────────────────────
# Claude only ever narrates the already-computed, already-ranked numbers
# below — it never recalculates or second-guesses them. The whole point of
# building the min-cost-flow simulator was to get a deterministic, correct
# ranking; handing that off to an LLM to redo "in its head" would throw
# away that accuracy.
_SYSTEM_PROMPT = """You are Shams-kun, the persona behind this Discord bot's automated \
sports-insider posts for a 10-team fantasy basketball dynasty league — the same voice \
used for its trade/transaction breaking-news posts (Woj/Shams style: punchy, no hedging, \
no disclaimers).

You are given this week's CURRENT-SEASON power rankings context — rank, tier, record/streak, \
and movement since last week for every team, plus (when notable) a short list of individual \
player performances trending up or down — already computed and already correct, NOT \
something you compute. A separate, code-generated tier-grouped list of every team's rank is \
already posted above whatever you write; your ONLY job is to write ONE short paragraph \
highlighting the most notable storylines across the WHOLE LEAGUE this week — biggest \
riser(s)/faller(s) at the team or player level, hot or cold streaks, any team new to the \
rankings. This is a league-wide summary, not a team-by-team or player-by-player breakdown — \
you do not need to mention every team or every listed player, only what's genuinely notable.

Rules:
- ONE paragraph, 3-5 sentences. No bullet points, no numbered list, no per-team or \
per-player breakdown.
- NEVER state or imply any underlying numeric figure — no points-per-day, no FP/G, no stat \
deltas, no raw numbers of any kind, whether about a team or an individual player — rank, \
tier, movement, record/streak, and a player's name plus qualitative direction (trending \
up/down) are fair game.
- Ground every claim in what's actually given. Do not invent claims the data doesn't \
support, and do not feel obligated to mention every team or every listed player.
- No methodology explanations, no caveats about the simulation, no restating this prompt.
- Do NOT add a title/header of your own — this paragraph gets appended below a rank list \
that's already posted, inside a Discord embed that already carries the Shams-kun branding.
- Punchy sports-insider tone throughout."""


def _movement_str(rank: int, previous_rank) -> str:
    if previous_rank is None:
        return "new to the rankings"
    if previous_rank == rank:
        return f"holding steady at #{rank}"
    if previous_rank > rank:
        return f"up from #{previous_rank}"
    return f"down from #{previous_rank}"


def compute_player_trends(rankings: list, previous_player_stats: dict,
                           n: int = 2, min_recent_games: int = MIN_RECENT_GAMES) -> list:
    """rankings: compute_power_rankings() output. previous_player_stats:
    {player_id: {"fpts": total, "gp": games_played}} from last week's
    REAL post (see cogs/powerrankings.py's _load_previous_player_stats();
    {} if there's no prior snapshot, e.g. the first post ever).

    Derives each player's TRUE recent-window scoring rate via the shared
    _recent_fpg() helper — fantasy points per game over just the games
    played SINCE the last snapshot — and compares it to their current
    season-long average (fp_g). The same helper also feeds
    _build_player_lookup()'s value blending for the projection itself
    (see simulate_team_period()) — one derivation of "recent form",
    used two ways: numeric blend for the ranking math, qualitative
    direction for the narration. This is a genuine "how have they played
    lately vs. their own baseline" signal, not an approximation from
    watching the cumulative average creep:
    confirmed via a live API check that FPts-total and GP columns
    already exist in the same STATS response fetch_team_period_data
    parses for fp_g, so this costs zero extra API calls. Also confirmed
    live that there's no native "recent window" stat column to use
    directly instead (no L7/L14/L15/L30-style column exists on this
    view), and that Fantrax's real windowed-stats mechanism (the site's
    "Dates" timeframe selector) needs a structured request shape this
    codebase's thin API wrapper isn't built to send — see
    fetch_team_period_data()'s docstring. Compares against the season
    average inclusive of the recent window itself (a standard
    simplification in "recent vs. season" fantasy analysis, not
    de-biased/excluded).

    Players with recent_gp < min_recent_games (no games since the last
    snapshot — bye week, injury — or a data anomaly) are skipped: not
    enough of a sample to say anything about recent form. Players absent
    from previous_player_stats entirely (new to a roster since last
    snapshot — trade, waiver pickup, return from Inj Res, or re-added
    after being dropped) are also skipped — no prior data point to
    compare against, and by design "new to roster" players are out of
    scope for this signal. A dropped player's stale entry naturally
    disappears from previous_player_stats the next time a real post
    fires (see _save_current_player_stats() — full overwrite, not a
    merge), so no explicit cleanup is needed here. A player traded
    between two of this league's teams compares correctly and is
    attributed to their CURRENT team, since fpts_total/gp are the
    player's own real-world NBA stats (not scoped to whichever fantasy
    team owns them) and team is read from this week's rankings, not the
    stale snapshot.

    Returns up to n risers + n fallers league-wide as [(team_name,
    player_name, direction), ...], direction in {"riser", "faller"} —
    never the underlying numbers, matching the same never-leak-raw-
    numbers contract as _movement_str()."""
    if not previous_player_stats:
        return []

    deltas = []  # (delta, team_name, player_name)
    for team, _total, _days, _per_day, roster_players in rankings:
        for player_id, name, fp_g, fpts_total, gp, _age in roster_players:
            recent_fpg = _recent_fpg(fp_g, fpts_total, gp, player_id, previous_player_stats, min_recent_games)
            if recent_fpg is None:
                continue
            delta = recent_fpg - fp_g
            if delta != 0:
                deltas.append((delta, team.name, name))

    risers = sorted((d for d in deltas if d[0] > 0), reverse=True)[:n]
    fallers = sorted((d for d in deltas if d[0] < 0))[:n]
    return [(team, name, "riser") for _d, team, name in risers] + \
           [(team, name, "faller") for _d, team, name in fallers]


def generate_power_rankings_writeup(rankings: list, api_key: str, records: dict = None,
                                     previous_ranks: dict = None, previous_player_stats: dict = None,
                                     tier_names: tuple = None) -> str:
    """rankings: the output of compute_power_rankings() — [(team,
    total_points, total_days, points_per_day, roster_players), ...].
    records: optional {team_id: Record} from record_lookup().
    previous_ranks: optional {team_id: last_rank}, used to compute
    week-over-week movement. previous_player_stats: optional {player_id:
    {"fpts", "gp"}}, used by compute_player_trends() to surface notable
    player-level performances. tier_names: see assign_tiers() — MUST
    match whatever was passed to format_tier_list() for the rank list
    posted above this paragraph, or the tier labels Claude sees here
    won't match what's actually on screen. Returns a SINGLE short
    paragraph (3-5 sentences) highlighting the most notable changes
    across the league this week — NOT a per-team breakdown, and NOT the
    rank list itself (that's format_tier_list(), built separately with
    no LLM involvement — this function's output is meant to be appended
    after that list)."""
    import anthropic

    tiers = assign_tiers(rankings, tier_names=tier_names)
    lines = []
    rank = 1
    for tier_name, entries in tiers:
        for team, *_rest in entries:
            record = records.get(team.id) if records else None
            if record and record.streak:
                record_str = f" ({record.win}-{record.loss}, {record.streak})"
            elif record:
                record_str = f" ({record.win}-{record.loss})"
            else:
                record_str = ""
            previous_rank = previous_ranks.get(team.id) if previous_ranks else None
            movement = _movement_str(rank, previous_rank)
            lines.append(f"{rank}. {team.name} [{tier_name}]{record_str} — {movement}")
            rank += 1
    user_message = "This week's power rankings context:\n\n" + "\n".join(lines)

    trends = compute_player_trends(rankings, previous_player_stats or {})
    if trends:
        trend_lines = [
            f"- {name} ({team}): trending {'up' if direction == 'riser' else 'down'}"
            for team, name, direction in trends
        ]
        user_message += "\n\nNotable player performances this week:\n" + "\n".join(trend_lines)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def compute_offseason_movers(rankings: list, previous_player_values: dict,
                              previous_ranks: dict = None, n: int = 3) -> dict:
    """rankings: compute_offseason_power_rankings()'s output.
    previous_player_values: {player_id: {"name": str, "fp_g": float,
    "team_id": str}} from the last REAL offseason post (see
    cogs/offseasonpowerrankings.py's _load_previous_player_values(); {}
    or None if there's no prior snapshot).
    previous_ranks: optional {team_id: last_rank} — used to pick which
    teams get detailed "why" treatment: the n teams with the largest
    |current_rank - previous_rank|. Without it, every team with a real
    driver is reported (used by /offseasonrankingsdebug-style previews
    that don't have real rank history to compare against).

    WHY this exists: confirmed live that a real rank swing can happen
    with ZERO actual roster moves — Fantrax's own "Projected - Season"
    values shift on their own (real-world injury news, its own
    recalculation), not just when a trade/waiver move happens. Handing
    the narration a bare rank delta risks it inventing a plausible-
    sounding but FALSE "why" (implying a trade that never happened) —
    this computes the real, distinguishable cause instead, so the
    narration only ever states something actually true.

    Returns {team_id: {"gained": [(name, source)], "lost": [(name,
    destination)], "shifted": [(name, direction)]}} for only the
    biggest-moving teams:
    - "gained": players newly on this roster since the last snapshot.
      source is "from <old team name>" if they were tracked on a
      DIFFERENT team_id last snapshot (a real trade/waiver move), or
      "new addition" if they weren't in previous_player_values at all
      (a free agent nobody had rostered before).
    - "lost": players who were on this roster last snapshot but aren't
      now. destination is "to <new team name>" if they're findable on
      another team's CURRENT roster, or "dropped" if they're not on any
      tracked roster right now.
    - "shifted": players who stayed on THIS roster both snapshots, but
      whose value changed — NOT a roster move, a value/projection
      change. Capped at the top 2 by |delta| per team; small/negligible
      shifts (<1.0 fp_g) are dropped as noise, not a real signal."""
    if not previous_player_values:
        return {}

    team_name_by_id = {team.id: team.name for team, *_rest in rankings}

    previous_by_team = {}
    for player_id, info in previous_player_values.items():
        previous_by_team.setdefault(info["team_id"], set()).add(player_id)

    current_owner_by_id = {}  # player_id -> team_id, across the WHOLE league right now
    current_roster_by_team = {}  # team_id -> {player_id: (name, fp_g)}
    for team, _total, _days, _per_day, roster_players in rankings:
        current_roster_by_team[team.id] = {}
        for player_id, name, fp_g, *_rest in roster_players:
            current_owner_by_id[player_id] = team.id
            current_roster_by_team[team.id][player_id] = (name, fp_g)

    if previous_ranks:
        current_ranks = {team.id: rank for rank, (team, *_rest) in enumerate(rankings, start=1)}
        deltas = sorted(
            (
                (abs(current_ranks[tid] - prev_rank), tid)
                for tid, prev_rank in previous_ranks.items()
                if tid in current_ranks
            ),
            reverse=True,
        )
        big_mover_ids = {tid for delta, tid in deltas[:n] if delta > 0}
    else:
        big_mover_ids = None  # report every team with a real driver

    results = {}
    for team_id, current_roster in current_roster_by_team.items():
        if big_mover_ids is not None and team_id not in big_mover_ids:
            continue

        gained, shifted = [], []
        for player_id, (name, fp_g) in current_roster.items():
            prev = previous_player_values.get(player_id)
            if prev is None:
                gained.append((name, "new addition"))
            elif prev["team_id"] != team_id:
                gained.append((name, f"from {team_name_by_id.get(prev['team_id'], 'a waiver pickup')}"))
            else:
                delta = fp_g - prev["fp_g"]
                if abs(delta) >= 1.0:
                    shifted.append((name, delta))

        lost = []
        for player_id in previous_by_team.get(team_id, set()) - current_roster.keys():
            name = previous_player_values[player_id]["name"]
            new_owner = current_owner_by_id.get(player_id)
            destination = f"to {team_name_by_id.get(new_owner, 'elsewhere')}" if new_owner else "dropped"
            lost.append((name, destination))

        shifted.sort(key=lambda x: -abs(x[1]))

        if gained or lost or shifted:
            results[team_id] = {
                "gained": gained[:3],
                "lost": lost[:3],
                "shifted": [(name, "up" if delta > 0 else "down") for name, delta in shifted[:2]],
            }
    return results


# ─────────────────────────────────────────────────────────────────────────
# OFFSEASON NARRATION — separate system prompt rather than branching the
# real one: the framing genuinely differs (no live games, so "movement"
# means roster changes via trades/waivers, not a hot/cold streak), and
# this codebase's established preference is a separate purpose-built
# prompt over one prompt with conditional branches for two different jobs.
# ─────────────────────────────────────────────────────────────────────────
_OFFSEASON_SYSTEM_PROMPT = """You are Shams-kun, the persona behind this Discord bot's automated \
sports-insider posts for a 10-team fantasy basketball dynasty league — the same voice used for \
its trade/transaction breaking-news posts (Woj/Shams style: punchy, no hedging, no disclaimers).

This is an OFFSEASON power rankings post — there are no NBA games being played right now, so \
this is NOT a live simulation of the CURRENT season's results like the in-season version. It \
ranks each team's roster strength via a real day-by-day simulation against actual NBA schedule \
data for the upcoming season, using each player's current per-game value (last season's real \
stats or Fantrax's own preseason projections, whichever is currently available — not something \
you need to track). Positional depth and real bench depth are already accounted for correctly. \
Already computed and already correct, NOT something you compute yourself. A separate, code-\
generated tier-grouped list of every team's rank is already posted above whatever you write; \
your ONLY job is to write ONE short paragraph highlighting the most notable storylines across \
the WHOLE LEAGUE — biggest riser(s)/faller(s), any team new to the rankings. This is a league-\
wide summary, not a team-by-team breakdown — you do not need to mention every team, only what's \
genuinely notable.

Since there are no games happening, movement since the last check-in is NEVER a hot or cold \
streak — but it is also NOT always a roster move. Two different, real causes exist: (1) an \
actual roster change (a trade, a waiver pickup/drop), or (2) a player who stayed on the exact \
same roster the whole time, but whose underlying value shifted (real-world injury news, or \
Fantrax's own projection recalculating on its own). For the biggest-moving teams, you'll be \
given real, computed context showing exactly which of these actually happened — which players \
were gained/lost (a real roster move) vs. which player's value shifted while staying put (not a \
roster move at all). Use THAT to explain WHY a team moved. Do not default to "made a move" or \
"improved their roster" language when the given context says a value shifted instead — that \
would be stating something false. If no driver context is given for a team, just state its \
rank/tier plainly and move on — do not guess at or invent a reason.

Only the teams with real driver context given need this kind of detailed "why" treatment — \
everyone else should get at most a brief rank/tier mention, not padded-out speculation.

Rules:
- ONE paragraph, 3-5 sentences. No bullet points, no numbered list, no per-team breakdown.
- NEVER state or imply any underlying numeric figure — no points-per-day, no FP/G, no raw \
numbers of any kind. Rank, tier, movement, and named players/teams from the given driver \
context are fair game.
- Ground every claim in what's actually given. Do not invent claims the data doesn't support, \
and do not feel obligated to mention every team.
- No methodology explanations, no caveats about how this was computed, no restating this prompt \
— the embed this appends to already carries its own "offseason snapshot" disclaimer.
- Do NOT add a title/header of your own — this paragraph gets appended below a rank list that's \
already posted, inside a Discord embed that already carries the Shams-kun branding.
- Punchy sports-insider tone throughout."""


def generate_offseason_power_rankings_writeup(rankings: list, api_key: str,
                                                previous_ranks: dict = None,
                                                previous_player_values: dict = None,
                                                tier_names: tuple = None) -> str:
    """rankings: compute_offseason_power_rankings()'s output — same tuple
    shape as compute_power_rankings() (see that function's docstring),
    just with points_per_day being a single current-roster lineup_
    ceiling() snapshot instead of a real simulated average.

    previous_ranks: optional {team_id: last_rank}, used to compute
    movement since the last post, and to pick which teams get detailed
    "why" treatment (see compute_offseason_movers()).

    previous_player_values: optional {player_id: {"name", "fp_g",
    "team_id"}} from the last real post (see cogs/offseasonpowerrankings.py's
    _load_previous_player_values()). Feeds compute_offseason_movers() —
    the REAL, computed reason the biggest-moving teams moved (a roster
    change vs. a value/projection shift for a player who stayed put),
    so the narration explains WHY instead of just restating the rank
    delta, without risking an invented explanation for teams where no
    real driver is given.

    Deliberately no records/streaks param (every team is 0-0 offseason —
    confirmed live, zero informational value, see CLAUDE.md's pick-
    owner-record note for the same reasoning) and no player-trend block
    (compute_player_trends() would always return [] here anyway — a
    player's games-played can't change between two offseason snapshots,
    so every recent_fpg computation is undefined by construction; simpler
    to not call it at all than carry an always-empty codepath).

    Returns a SINGLE short paragraph, same never-leak-raw-numbers
    contract as generate_power_rankings_writeup()."""
    import anthropic

    tiers = assign_tiers(rankings, tier_names=tier_names)
    lines = []
    rank = 1
    for tier_name, entries in tiers:
        for team, *_rest in entries:
            previous_rank = previous_ranks.get(team.id) if previous_ranks else None
            movement = _movement_str(rank, previous_rank)
            lines.append(f"{rank}. {team.name} [{tier_name}] — {movement}")
            rank += 1
    user_message = "Current offseason power rankings context:\n\n" + "\n".join(lines)

    movers = compute_offseason_movers(rankings, previous_player_values or {}, previous_ranks=previous_ranks)
    if movers:
        team_name_by_id = {team.id: team.name for team, *_rest in rankings}
        mover_lines = []
        for team_id, info in movers.items():
            parts = []
            if info["gained"]:
                parts.append("gained " + ", ".join(f"{name} ({source})" for name, source in info["gained"]))
            if info["lost"]:
                parts.append("lost " + ", ".join(f"{name} ({dest})" for name, dest in info["lost"]))
            if info["shifted"]:
                parts.append("saw " + ", ".join(f"{name} trend {direction}" for name, direction in info["shifted"]))
            mover_lines.append(f"- {team_name_by_id.get(team_id, '?')}: {'; '.join(parts)}")
        user_message += (
            "\n\nWhat's actually driving the biggest movers (use this to explain WHY, not just that "
            "they moved):\n" + "\n".join(mover_lines)
        )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=_OFFSEASON_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")
