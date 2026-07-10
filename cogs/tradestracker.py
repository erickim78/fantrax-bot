# Dependencies
import json
import re
from pathlib import Path

# Files
import config

# Fantrax
from fantraxapi import FantraxAPI
from fantraxapi.api import Method, request as fantrax_request

# Discord
import discord
from discord.ext import commands, tasks
from discord import app_commands


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

# Shares #news with transactionstracker.py — user explicitly wants trades
# and transactions in the same channel, not split out (see CLAUDE.md).
TRADE_CHANNEL_ID = config.transactionChannelId

STATE_FILEPATH = Path(__file__).resolve().parent.parent / "posted_trades.json"

DRY_RUN = False  # keep True until /tradedebug output looks right

# Total players+picks moved across the whole trade (both sides combined)
# at/above which a trade gets the "🚨 BREAKING:" prefix — regular trades
# post with no siren/prefix at all. No real editorial judgment here, just
# a size proxy: 4+ separates the one genuine outlier in this league's
# trade history so far (a 6-asset blockbuster) from everything else
# (2-3 assets).
BREAKING_ASSET_THRESHOLD = 4

# ─────────────────────────────────────────────────────────────────────────
# fantraxapi's League.transactions() only ever requests the site's default
# "Claim/Drop" view of getTransactionDetailsHistory — it hardcodes no
# `view` param at all, so completed trades never show up there. But that
# view is just a `view` kwarg on the SAME public, unauthenticated
# endpoint: probing the raw response's displayedLists.tabs showed a
# {"name": "Trade", "id": "TRADE"} tab alongside Claim/Drop. Requesting
# view="TRADE" returns fully EXECUTED trades with NO login needed —
# despite CLAUDE.md's original "Next steps" notes assuming a cookie/login
# flow was required (that's only true for League.pending_trades(), a
# different endpoint we don't need here). We bypass League.transactions()
# entirely and call the raw Method/request functions, because the
# TRADE-view row shape is different from CLAIM_DROP's — no
# "transactionCode" key at all, and draft-pick rows carry a
# "draftPickDisplayParts" dict instead of a usable "scorer" — so feeding
# these rows into the library's Transaction() parser would just crash.
# ─────────────────────────────────────────────────────────────────────────
TRADE_VIEW = "TRADE"

# draftPickDisplayParts looks like:
#   roundInfo: "Round <b>1</b> (Horny Mushrooms)"
#   year:      "<b>2029</b> Draft Pick"
# — confirmed via direct API probing, not documented anywhere.
PICK_ROUND_RE = re.compile(r"Round\s*<b>(\d+)</b>\s*\(([^)]+)\)")
PICK_YEAR_RE = re.compile(r"<b>(\d+)</b>")


def role_tag(team) -> str:
    team_id = getattr(team, "id", None)
    role_id = config.teamRoleIds.get(team_id) if team_id else None
    return f"<@&{role_id}>" if role_id else f"**{team.name}**"


def _ordinal(n: str) -> str:
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _english_list(items: list) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


class TradesTracker(commands.Cog):
    def __init__(self, bot):
        print("Init Function of TradesTracker Cog")
        self.bot = bot

        # Public league — same as transactionstracker.py, no session/login
        # needed for the executed-trades view.
        self.api = FantraxAPI(config.leagueId)

        self.posted_ids = self._load_posted_ids()
        self.check_trades.start()

    def cog_unload(self):
        self.check_trades.cancel()

    # ── persistence ────────────────────────────────────────────────
    def _load_posted_ids(self) -> set:
        if STATE_FILEPATH.exists():
            return set(json.loads(STATE_FILEPATH.read_text()))
        return set()

    def _save_posted_ids(self):
        STATE_FILEPATH.write_text(json.dumps(sorted(self.posted_ids)))

    # ── raw fetch/group ──────────────────────────────────────────────
    def _fetch_trade_groups(self):
        """Returns (groups, order): groups maps txSetId -> list of raw row
        dicts belonging to that trade, order is the list of txSetIds in
        the order the API returned them (newest-first, confirmed via
        probing — same convention as the Claim/Drop feed)."""
        raw = fantrax_request(
            self.api, Method("getTransactionDetailsHistory", maxResultsPerPage="300", view=TRADE_VIEW)
        )
        groups = {}
        order = []
        for row in raw["table"]["rows"]:
            if not row.get("executed"):
                continue
            tx_id = row["txSetId"]
            if tx_id not in groups:
                groups[tx_id] = []
                order.append(tx_id)
            groups[tx_id].append(row)
        return groups, order

    # ── formatting ──────────────────────────────────────────────────
    # Dedup is by txSetId (the trade's grouping key), so a trade is only
    # ever posted once, the first time it shows up as executed.
    @staticmethod
    def _cell(row, key):
        return next((c for c in row["cells"] if c["key"] == key), None)

    def _row_item_text(self, row) -> tuple:
        """Returns (text, is_player). is_player drives the "who's
        acquiring" heuristic in format_trade below."""
        pick = row.get("draftPickDisplayParts")
        if pick:
            round_match = PICK_ROUND_RE.search(pick.get("roundInfo", ""))
            year_match = PICK_YEAR_RE.search(pick.get("year", ""))
            round_num = round_match.group(1) if round_match else "?"
            owner_name = round_match.group(2) if round_match else ""
            year = year_match.group(1) if year_match else "?"
            # Always show the original-owner label Fantrax itself attaches
            # to the pick, even when it's the sending team's own pick —
            # matches how these have been written manually (e.g. "a 2029
            # 1st-round pick (Horny Mushrooms)" posted even when Horny
            # Mushrooms was the one sending it).
            owner_note = f" ({owner_name})" if owner_name else ""
            return f"a {year} {_ordinal(round_num)}-round pick{owner_note}", False

        # FAAB cash throw-in — confirmed via /tradedebug: has neither
        # draftPickDisplayParts nor a usable scorer.name, just this dict.
        budget = row.get("budgetAmountTradeObj")
        if budget:
            return f"{budget.get('budget', '?')} in cash considerations", False

        return f"**{row['scorer']['name']}**", True

    def format_trade(self, rows: list) -> str:
        # Group moves by the team receiving them, generically rather than
        # assuming exactly two sides (a trade could involve more).
        by_team = {}
        for row in rows:
            to_team = self.api.team(self._cell(row, "to")["teamId"])
            entry = by_team.setdefault(to_team.id, {"team": to_team, "items": [], "got_player": False})
            text, is_player = self._row_item_text(row)
            entry["items"].append(text)
            entry["got_player"] = entry["got_player"] or is_player

        # Only big trades get called out — everything else just posts
        # plain, no siren/prefix at all.
        prefix = "🚨 BREAKING: " if len(rows) >= BREAKING_ASSET_THRESHOLD else ""

        if len(by_team) != 2:
            # 3+ team trade (rare in a 10-team league, but possible) — the
            # "X are acquiring ... from Y in exchange for ..." phrasing
            # only reads naturally for a two-sided trade, so fall back to
            # a flat per-team breakdown instead of guessing at a headline.
            sides = "; ".join(
                f"{role_tag(entry['team'])} get {_english_list(entry['items'])}"
                for entry in by_team.values()
            )
            return f"{prefix}{sides}."

        # Lead with whichever side landed a player — a pick/FAAB-only
        # return never leads the headline, matching how these have always
        # been written manually (e.g. "Goat James are acquiring Maluach
        # and Ighodaro... in exchange for a 2nd" — Goat James leads
        # despite Homoerotic Knights' side being the one with row 0's
        # asset). When both sides get a player (a straight swap), that
        # signal ties, so fall back to whichever team the API's first row
        # sent its asset to, for a stable/deterministic pick either way.
        entries = list(by_team.values())
        player_sides = [e for e in entries if e["got_player"]]
        if len(player_sides) == 1:
            acquiring, other = player_sides[0], next(e for e in entries if e is not player_sides[0])
        else:
            first_to_id = self.api.team(self._cell(rows[0], "to")["teamId"]).id
            acquiring = by_team[first_to_id]
            other = next(e for e in entries if e is not acquiring)

        return (
            f"{prefix}{role_tag(acquiring['team'])} are acquiring "
            f"{_english_list(acquiring['items'])} from {role_tag(other['team'])} "
            f"in exchange for {_english_list(other['items'])}."
        )

    # ── shared fetch/format/post logic ──────────────────────────────
    async def _check_and_post(self):
        try:
            groups, order = self._fetch_trade_groups()
        except Exception as e:
            print(f"[TradesTracker] Failed to fetch trade history: {e}")
            return

        new_ids = [tid for tid in order if tid not in self.posted_ids]
        if not new_ids:
            return

        if DRY_RUN:
            print("[TradesTracker DRY RUN] Would post:")
            for tid in reversed(new_ids):
                print("  ", self.format_trade(groups[tid]))
            return

        channel = self.bot.get_channel(TRADE_CHANNEL_ID)
        if channel is None:
            print(f"[TradesTracker] Channel {TRADE_CHANNEL_ID} not found.")
            return

        # Oldest-first so the feed reads chronologically, same convention
        # as transactionstracker.py. One message per trade — save
        # immediately after each send so a mid-loop crash can't leave an
        # already-posted trade unrecorded.
        for tid in reversed(new_ids):
            await channel.send(self.format_trade(groups[tid]))
            self.posted_ids.add(tid)
            self._save_posted_ids()

    # ── background loop ─────────────────────────────────────────────
    # Every 5 min rather than hourly for near-immediate reporting — still
    # a trivial request volume (public, unauthenticated endpoint, same
    # call the Fantrax website itself makes on page load).
    @tasks.loop(minutes=5)
    async def check_trades(self):
        await self._check_and_post()

    @check_trades.before_loop
    async def before_check_trades(self):
        await self.bot.wait_until_ready()

    # Commands — gated to manage_guild so Discord hides it from the
    # slash-command picker for regular league members entirely, not just
    # blocked-on-run (matches transactionstracker.py's debug commands).
    @app_commands.command(name='tradedebug', description='(Debug) Dump raw executed trades')
    @app_commands.default_permissions(manage_guild=True)
    async def tradeDebug(self, interaction: discord.Interaction) -> None:
        try:
            groups, order = self._fetch_trade_groups()
        except Exception as e:
            await interaction.response.send_message(f"Failed to fetch trade history: {e}")
            return

        if not order:
            await interaction.response.send_message("No trades found.")
            return

        lines = []
        for tid in order[:10]:
            rows = groups[tid]
            date_cell = self._cell(rows[0], "date")
            lines.append(f"{tid} | date={date_cell['content'] if date_cell else '?'} | items={len(rows)}")
            lines.append(f"    → {self.format_trade(rows)}")
        text = "\n".join(lines)

        await interaction.response.send_message(f"```\n{text[:1900]}\n```")
        for chunk_start in range(1900, len(text), 1900):
            await interaction.followup.send(f"```\n{text[chunk_start:chunk_start+1900]}\n```")


async def setup(bot):
    await bot.add_cog(TradesTracker(bot), guilds=[config.myGuild])
