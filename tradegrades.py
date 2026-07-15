# Dependencies
import re

# Fantrax
from fantraxapi import FantraxAPI

# Power rankings' reusable roster/lineup primitives — imported, not
# duplicated, since the min-cost-flow solver + injury/suspension
# filtering is substantial, stable code (unlike the small pick-text
# parsing below, which IS duplicated rather than cross-imported from
# tradestracker.py, per this codebase's established "keep root modules
# independent" preference — see CLAUDE.md).
import powerrankings as pr


# ─────────────────────────────────────────────────────────────────────────
# TRADE ROW PARSING
# ─────────────────────────────────────────────────────────────────────────
# Mirrors tradestracker.py's row-shape handling (same raw
# getTransactionDetailsHistory view="TRADE" rows).
#
# CONFIRMED LIVE (against this league's real trade history, not assumed):
# every row — including pick and FAAB-cash rows — carries a "scorer" key,
# but it's a thin placeholder dict ({"team": false, "rookie": false,
# "minorsEligible": false}) for non-player rows. draftPickDisplayParts/
# budgetAmountTradeObj must be checked FIRST; only treat a row as a
# player move once both are absent — matches tradestracker.py's existing
# check order exactly, confirmed necessary (not just copied blind) by
# dumping a real pick row and seeing it also had a "scorer" key. A
# genuine player row's "scorer" dict has a real "scorerId" — the same ID
# used as Player.id elsewhere in this codebase — so player identity is
# already known directly from the row, no name-matching needed.
PICK_ROUND_RE = re.compile(r"Round\s*<b>(\d+)</b>\s*\(([^)]+)\)")
PICK_YEAR_RE = re.compile(r"<b>(\d+)</b>")


def _ordinal(n) -> str:
    n = int(n)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _pick_description(pick: dict) -> dict:
    """Returns {"description": "a 2027 1st-round pick", "owner_name": str
    or None} — owner_name is the team whose original draft slot this is
    (Fantrax's own roundInfo label, e.g. "Round <b>1</b> (Horny
    Mushrooms)"), independent of who's actually sending/receiving it in
    THIS trade — a pick can change hands multiple times before it's
    actually used. None if the label didn't parse (same defensive
    fallback as the "?" round/year handling below). Resolved further in
    analyze_trade() into the owning team's current record — a real,
    already-available fact (no pick-value formula, see parse_trade_
    rows()'s docstring) that's still a meaningful proxy: a future 1st
    from a team currently near the bottom of the standings is worth more
    than one from a team near the top, since this league drafts in
    reverse-standings order."""
    round_match = PICK_ROUND_RE.search(pick.get("roundInfo", ""))
    year_match = PICK_YEAR_RE.search(pick.get("year", ""))
    round_num = round_match.group(1) if round_match else "?"
    owner_name = round_match.group(2) if round_match else None
    year = year_match.group(1) if year_match else "?"
    return {"description": f"a {year} {_ordinal(round_num)}-round pick", "owner_name": owner_name}


def parse_trade_rows(rows: list) -> dict:
    """rows: raw trade rows for one txSetId (same shape tradestracker.py's
    _fetch_trade_groups() returns — one group of rows sharing a txSetId).

    Returns {team_id: {"acquired_player_ids": [...],
    "given_up_player_ids": [...], "picks_acquired": [...],
    "picks_given_up": [...]}} — one entry per team involved. picks_*
    entries are _pick_description()'s dicts at this stage (resolved into
    final display strings by analyze_trade(), which is where the owning
    team's current record gets attached). Picks are tracked for
    narration context only, never resolved to a numeric VALUE — there's
    no clean deterministic pick-value source (see CLAUDE.md's dynasty-
    valuation discussion). FAAB cash rows are currently ignored entirely
    (not factored into the grade at all) — could be added later the same
    way picks are, if cash throw-ins turn out to matter enough to track."""
    def cell(row, key):
        return next((c for c in row["cells"] if c["key"] == key), None)

    by_team = {}

    def team_entry(team_id):
        return by_team.setdefault(team_id, {
            "acquired_player_ids": [], "given_up_player_ids": [],
            "picks_acquired": [], "picks_given_up": [],
        })

    for row in rows:
        from_id = cell(row, "from")["teamId"]
        to_id = cell(row, "to")["teamId"]

        pick = row.get("draftPickDisplayParts")
        if pick:
            desc = _pick_description(pick)
            team_entry(to_id)["picks_acquired"].append(desc)
            team_entry(from_id)["picks_given_up"].append(desc)
            continue
        if row.get("budgetAmountTradeObj"):
            continue  # FAAB cash — not currently factored into the grade

        scorer_id = row["scorer"]["scorerId"]
        team_entry(to_id)["acquired_player_ids"].append(scorer_id)
        team_entry(from_id)["given_up_player_ids"].append(scorer_id)

    return by_team


# ─────────────────────────────────────────────────────────────────────────
# MARGINAL VALUE + REAL FACTS
# ─────────────────────────────────────────────────────────────────────────
def analyze_trade(api: FantraxAPI, trade_by_team: dict) -> dict:
    """trade_by_team: parse_trade_rows()'s output. Returns {team_id: {
    "team_name", "marginal_value_delta", "acquired_names",
    "acquired_ages", "acquired_rookies", "given_up_names", "given_up_ages",
    "given_up_rookies", "picks_acquired", "picks_given_up",
    "roster_avg_age", "record"
    }} for every team involved.

    acquired_rookies/given_up_rookies: parallel bool lists to
    acquired_names/given_up_names — Fantrax's own rookie designation
    (Player._data["rookie"], not a parsed Player attribute — confirmed
    live present on every scorer dict, real vs. inferred), given to the
    LLM as another real dynasty-timeline anchor alongside age.

    marginal_value_delta: pr.lineup_ceiling(current post-trade roster) -
    pr.lineup_ceiling(hypothetical roster with the trade undone) — the
    TRUE lineup-value impact of the trade, positional-logjam-aware by
    construction (see lineup_ceiling()'s docstring — a redundant player
    at an already-crowded position contributes less than raw production
    would suggest, no hand-written positional-scarcity rule needed).

    Resolving "what a team gave up": every player in a trade is, right
    now, on SOMEBODY's current roster (whoever received them) — so full
    stats (name, age) for a given-up player are looked up on the
    RECEIVING team's current roster, not historical roster data we don't
    have and can't get (see CLAUDE.md's daily-top-performer/GM-grade
    notes on why historical rosters aren't reliably queryable).

    Deliberately does NOT blend recent form into the lineup-ceiling
    computation (unlike simulate_team_period()) — trade grades are about
    roster construction as it stands, not a forward projection; keeping
    it to plain season fp_g avoids coupling this to previous_player_
    stats for no clear benefit. Easy to add later if wanted.

    picks_acquired/picks_given_up are resolved here (via _resolve_picks())
    into display strings that include the pick's ORIGINAL owner and that
    team's current record when it resolves — e.g. "a 2027 1st-round pick
    (Horny Mushrooms, currently 2-8)". The owning team need not be a
    participant in this trade at all (a pick can already have changed
    hands once). This is a real fact, not a computed pick value (see
    parse_trade_rows()'s docstring) — still a meaningful proxy since this
    league drafts in reverse-standings order, so a bad team's future 1st
    is worth more than a good team's."""
    team_ids = list(trade_by_team.keys())
    current_rosters = {tid: pr.fetch_roster_players(api, tid) for tid in team_ids}
    records = pr.record_lookup(api)  # {team_id: Record} for EVERY team in the league,
                                      # not just this trade's participants — needed below to
                                      # resolve a pick's ORIGINAL owner, who may not be
                                      # involved in this trade at all (a pick can already
                                      # have changed hands once before this deal).
    team_id_by_name = {t.name: t.id for t in api.teams}

    def _resolve_picks(picks):
        """picks: a list of _pick_description() dicts. Returns display
        strings, e.g. "a 2027 1st-round pick (Horny Mushrooms, currently
        2-8)" — attaches the owning team's current record when it
        resolves AND is actually informative (real fact, not a computed
        pick VALUE; see parse_trade_rows()'s docstring for why no value
        is computed). A 0-0 record (preseason — confirmed live, every
        team is 0-0 right now) carries no differentiating signal at all,
        so it's deliberately suppressed rather than shown as dead noise;
        this starts showing real records automatically once the season
        actually begins, no further code change needed. Falls back
        gracefully otherwise: owner name with no record if the team name
        doesn't resolve (e.g. renamed since) or hasn't played yet, bare
        description if there's no owner name at all (regex miss)."""
        resolved = []
        for pick in picks:
            desc = pick["description"]
            owner_name = pick.get("owner_name")
            if owner_name:
                owner_record = records.get(team_id_by_name.get(owner_name))
                record_str = None
                if owner_record and (owner_record.win + owner_record.loss) > 0:
                    record_str = f"{owner_record.win}-{owner_record.loss}"
                    if owner_record.streak:
                        record_str += f", {owner_record.streak}"
                desc += f" ({owner_name}, currently {record_str})" if record_str else f" ({owner_name})"
            resolved.append(desc)
        return resolved

    global_lookup = {}  # player_id -> (Player, fp_g, fpts_total, gp, age, status)
    for roster in current_rosters.values():
        for entry in roster:
            global_lookup[entry[0].id] = entry

    def _resolve(ids):
        names, ages, rookies = [], [], []
        for pid in ids:
            entry = global_lookup.get(pid)
            if entry is None:
                continue  # every traded player should be on someone's roster; don't crash if not
            names.append(entry[0].name)
            ages.append(entry[4])
            rookies.append(bool(entry[0]._data.get("rookie", False)))
        return names, ages, rookies

    results = {}
    for team_id in team_ids:
        info = trade_by_team[team_id]
        current_roster = current_rosters[team_id]
        acquired_ids = set(info["acquired_player_ids"])
        given_up_ids = set(info["given_up_player_ids"])

        hypothetical_roster = [e for e in current_roster if e[0].id not in acquired_ids]
        hypothetical_roster += [global_lookup[pid] for pid in given_up_ids if pid in global_lookup]

        # availability_filter="none" explicit here (matches lineup_ceiling's
        # own default, but spelled out since it's load-bearing): confirmed
        # live that ~29% of this league's rostered players are currently
        # flagged injured/suspended, almost all a meaningless day_to_day
        # "game-time decision" tag with no game to decide about during the
        # offseason — trade value must never be silently zeroed by that.
        ceiling_after = pr.lineup_ceiling(current_roster, availability_filter="none")
        ceiling_before = pr.lineup_ceiling(hypothetical_roster, availability_filter="none")

        acquired_names, acquired_ages, acquired_rookies = _resolve(info["acquired_player_ids"])
        given_up_names, given_up_ages, given_up_rookies = _resolve(info["given_up_player_ids"])
        roster_ages = [e[4] for e in current_roster if e[4]]

        results[team_id] = {
            "team_name": api.team(team_id).name,
            "marginal_value_delta": ceiling_after - ceiling_before,
            "acquired_names": acquired_names,
            "acquired_ages": acquired_ages,
            "acquired_rookies": acquired_rookies,
            "given_up_names": given_up_names,
            "given_up_ages": given_up_ages,
            "given_up_rookies": given_up_rookies,
            "picks_acquired": _resolve_picks(info["picks_acquired"]),
            "picks_given_up": _resolve_picks(info["picks_given_up"]),
            "roster_avg_age": (sum(roster_ages) / len(roster_ages)) if roster_ages else None,
            "record": records.get(team_id),
        }
    return results


# ─────────────────────────────────────────────────────────────────────────
# LLM NARRATION
# ─────────────────────────────────────────────────────────────────────────
# Same "code computes real numbers, Claude only narrates judgment"
# principle as powerrankings.py — the marginal-value delta is a real,
# trusted computation (reusing the same optimizer the power rankings
# simulation already relies on), never invented by the LLM. Unlike
# power rankings, there's no clean deterministic way to value dynasty
# assets (picks, age-adjusted timeline fit) — that's an inherent
# judgment call even for human analysts, so Claude is explicitly asked
# to weigh the real computed value against real timeline facts and make
# a qualitative call, not to invent a fake objective dynasty formula.
_SYSTEM_PROMPT = """You are Shams-kun, the persona behind this Discord bot's automated \
sports-insider posts for a 10-team fantasy basketball dynasty league — the same voice \
used for its trade/transaction breaking-news posts and weekly power rankings (Woj/Shams \
style: punchy, no hedging, no disclaimers).

You are given a trade that was executed in this league, along with pre-computed REAL facts \
about its impact — NOT something you compute yourself:
- Each team's TRUE marginal lineup-value change from this trade. This already accounts for \
roster construction and positional depth — a player who can't crack an already-crowded \
position's lineup contributes less than their raw production would suggest, and this number \
reflects that correctly. Treat it as ground truth for "who actually gained more value", but \
NEVER state the number itself (see Rules).
- Real ages of every player involved, and each team's current roster-wide average age (a \
rough proxy for competitive timeline — a young roster suggests building for the future, an \
older one suggests playing to win now).
- Whether any acquired/given-up player is a rookie (Fantrax's own designation, not a guess).
- Each team's current win-loss record/streak (another timeline signal — very different \
context for a 9-1 team than a 2-8 team).
- Any draft picks involved.

Your job: call the emit_trade_grade tool with two things — a discrete verdict (which team \
this trade favors, if either, and by how much) and a narrative reaction explaining why (see \
Rules for length/structure). Weigh BOTH the computed lineup-value impact AND the real timeline signals \
(ages, rookie status, record) the way a real dynasty fantasy analyst would — a team can come \
out behind on pure computed value and still have made a defensible move if the return clearly \
fits their timeline (e.g. young assets for a rebuilding team, proven production for a team in \
win-now mode), and the verdict should reflect that (pulled toward "Even" or even the other \
way) even when the computed value alone points elsewhere. But magnitude matters: a large \
computed value gap needs a genuinely commensurate return to be called close to "Even" — \
timeline fit can soften a verdict, it shouldn't erase a large gap on its own, especially when \
the return is just one modest asset (e.g. a single pick) against a clearly superior current \
player. Reserve "Even" for a real-but-modest gap, or a return substantial enough (multiple \
valuable assets, real draft capital) to plausibly close a larger one. Do not invent fantasy-team context, \
needs, or motivations beyond what's given to you. The verdict and the narrative MUST agree — \
never pick a lopsided verdict and then write a narrative that reads as roughly even, or vice \
versa.

You MAY also draw on your own general knowledge of a player's real-world standing, skill \
trajectory, or prospect outlook to enrich the dynasty read — a real dynasty analyst wouldn't \
limit themselves to a spreadsheet either. Hedge this the way a real analyst hedges a scouting \
opinion ("has looked like an ascending piece," "carries real bust risk," "the tools are still \
there") rather than stating it as settled fact. But your own knowledge has a training cutoff, \
and anything concrete and situational — current NBA team, coaching staff, depth chart, \
teammates, contract status, or similar — can already be stale and flatly wrong by the time \
this posts, since that kind of thing can change within weeks or months. Never state or imply a \
specific detail like that. Stick to commentary that stays true regardless of the exact \
situational specifics underneath it: whether a player's role seems to be trending up or down, \
whether perceived consensus on them (scouting/fantasy-community sentiment) has been rising or \
cooling, general development or decline.

Rules:
- verdict: must be EXACTLY one of the enum options given in the tool schema — do not invent \
your own wording for it, do not add extra text to it.
- narrative: 1-3 short paragraphs (separated by a blank line), scaling with the trade's size \
and complexity — a simple 1-for-1 swap might only need one tight paragraph; a bigger, multi- \
asset trade (multiple players, picks on both sides) can spread across 2-3 shorter paragraphs \
instead of one dense block. Each paragraph should be a natural unit of thought (e.g. the \
immediate on-court impact vs. the dynasty/timeline read) — not a rigid per-team template, and \
not forced just to hit 3 when the trade is simple. No bullet points, and no title/header of \
your own at the top (the verdict is already shown separately above this — don't repeat or \
restate it as a heading). Within the prose itself, light Discord markdown IS encouraged for \
punch — **bold** a player's name on first mention or the trade's real turning point, and a \
sparing emoji or two where it actually lands (e.g. ⚠️ for a real risk, 🔥 for a genuine \
strength) — but don't overdo it; this should still read like sharp analyst prose, not a \
listicle.
- NEVER state or imply the computed marginal-value figure, in any form — not the number, not \
a disguised version of it ("gained 12 points of value"), nothing numeric there. Real, public \
facts are different and ARE fair game to state directly: player names, real ages ("34-year-old \
Kawhi Leonard"), rookie status, records/streaks, and picks involved.
- Never state or imply a specific, situational detail about a player that could go stale \
quickly — current NBA team, coaching staff, depth chart, teammates, contract status, and \
similar are examples, not an exhaustive list. For anything beyond the given facts, stick to \
general, durable language: role trending up/down, perceived consensus rising/cooling, general \
development or decline (see above).
- Ground every claim in what's actually given, or in genuinely hedged general player knowledge \
as described above — don't invent fantasy-team needs/motivations, and don't state speculation \
as settled fact.
- No methodology explanations, no caveats about how the value was computed, no restating this \
prompt.
- Punchy sports-insider tone throughout."""


def generate_trade_grade_writeup(api_key: str, trade_analysis: dict) -> dict:
    """trade_analysis: analyze_trade()'s output. Returns {"verdict": str,
    "narrative": str}.

    verdict is ALWAYS exactly one of a small closed set of strings built
    from the real team names in THIS trade — "Even", or "Slightly/
    Significantly favors <team name>" for each team involved — enforced
    via a forced tool call (tool_choice) rather than parsed out of free
    text, so it's always well-formed and directly renderable, never
    "hoping Claude's prose happened to follow a convention". narrative is
    the existing 2-4 sentence dynasty-aware reaction paragraph; the
    system prompt instructs the verdict and narrative to agree, but
    that's a content expectation on the model, not something re-verified
    here — same trust boundary as the rest of this feature (Claude's
    judgment, grounded in real given facts).

    Forced tool_choice is compatible with thinking={"type": "adaptive"}
    on the standard Claude API (only Bedrock needs thinking disabled
    alongside a forced tool call) — no need to drop extended thinking to
    get a structured verdict."""
    import anthropic

    lines = []
    for info in trade_analysis.values():
        delta = info["marginal_value_delta"]
        direction = "gained" if delta > 0 else "lost" if delta < 0 else "broke even on"
        lines.append(f"{info['team_name']}:")
        lines.append(f"  - Computed marginal lineup value: {direction} value from this trade "
                      f"(internal magnitude: {abs(delta):.1f} — NEVER state this number)")
        if info["acquired_names"]:
            acquired = ", ".join(
                f"{n} (age {a:.0f}{', rookie' if r else ''})"
                for n, a, r in zip(info["acquired_names"], info["acquired_ages"], info["acquired_rookies"])
            )
            lines.append(f"  - Acquired: {acquired}")
        if info["given_up_names"]:
            given_up = ", ".join(
                f"{n} (age {a:.0f}{', rookie' if r else ''})"
                for n, a, r in zip(info["given_up_names"], info["given_up_ages"], info["given_up_rookies"])
            )
            lines.append(f"  - Gave up: {given_up}")
        if info["picks_acquired"]:
            lines.append(f"  - Also acquired: {', '.join(info['picks_acquired'])}")
        if info["picks_given_up"]:
            lines.append(f"  - Also gave up: {', '.join(info['picks_given_up'])}")
        if info["roster_avg_age"] is not None:
            lines.append(f"  - Current roster average age (post-trade): {info['roster_avg_age']:.1f}")
        record = info["record"]
        if record:
            record_str = f"{record.win}-{record.loss}" + (f", {record.streak}" if record.streak else "")
            lines.append(f"  - Current record: {record_str}")

    user_message = "Trade to analyze:\n\n" + "\n".join(lines)

    # verdict_options built fresh per-trade from the REAL team names
    # involved — the enum IS the set of valid answers, so there's no
    # separate string-parsing/matching step on our end once the tool
    # call comes back. Untested past exactly 2 teams (every real trade
    # in this league's history so far has been 2-team — see CLAUDE.md),
    # but the shape here is N-team-general: one "favors <team>" pair per
    # team, whatever N is.
    team_names = [info["team_name"] for info in trade_analysis.values()]
    verdict_options = ["Even"]
    for name in team_names:
        verdict_options.append(f"Slightly favors {name}")
        verdict_options.append(f"Significantly favors {name}")

    tool = {
        "name": "emit_trade_grade",
        "description": "Emit the final trade grade for this trade: a discrete verdict plus "
                        "the narrative reaction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": verdict_options,
                    "description": "The overall dynasty-aware verdict on who this trade "
                                    "favors, if anyone. Must agree with `narrative`.",
                },
                "narrative": {
                    "type": "string",
                    "description": "The dynasty-aware reaction — 1-3 short paragraphs "
                                    "(blank-line separated), scaling with the trade's size/"
                                    "complexity. See the system prompt for the exact rules.",
                },
            },
            "required": ["verdict", "narrative"],
            "additionalProperties": False,
        },
    }

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,  # bumped from 1024 — narrative can now run up to 3 paragraphs on a
                          # big trade, plus adaptive thinking's own token spend; leaves headroom
                          # so neither gets cut off (stop_reason: max_tokens) before the tool call
        thinking={"type": "adaptive"},
        system=_SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": "emit_trade_grade"},
        messages=[{"role": "user", "content": user_message}],
    )
    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        return {"verdict": "Even", "narrative": ""}

    verdict = tool_use.input.get("verdict")
    if verdict not in verdict_options:  # defensive — schema should already guarantee this
        verdict = "Even"
    return {"verdict": verdict, "narrative": tool_use.input.get("narrative", "")}
