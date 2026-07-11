# Dependencies
import json
import re
from datetime import datetime as _dt
from pathlib import Path
from zoneinfo import ZoneInfo

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
TRADE_CHANNEL_ID = config.newsChannelId

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

# The "date" cell's header literally says "Date Processed (EDT)" — always
# labeled EDT regardless of actual DST state, so we use the America/New_York
# zone (not a fixed UTC-4 offset) to get DST transitions right automatically.
TRADE_DATE_FORMAT = "%a %b %d, %Y, %I:%M%p"
TRADE_TZ = ZoneInfo("America/New_York")


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

    def _row_item_text(self, row) -> str:
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
            return f"a {year} {_ordinal(round_num)}-round pick{owner_note}"

        # FAAB cash throw-in — confirmed via /tradedebug: has neither
        # draftPickDisplayParts nor a usable scorer.name, just this dict.
        budget = row.get("budgetAmountTradeObj")
        if budget:
            return f"{budget.get('budget', '?')} in cash considerations"

        return f"**{row['scorer']['name']}**"

    def _trade_timestamp(self, rows: list):
        date_cell = self._cell(rows[0], "date")
        if date_cell and date_cell.get("content"):
            try:
                return _dt.strptime(date_cell["content"], TRADE_DATE_FORMAT).replace(tzinfo=TRADE_TZ)
            except ValueError:
                pass
        return discord.utils.utcnow()

    def format_trade_embed(self, rows: list) -> discord.Embed:
        # Group moves by the team receiving them, generically rather than
        # assuming exactly two sides (a trade could involve more) — each
        # team gets its own field, so this needs no "who's acquiring"
        # narrative logic the way the old plain-text version did.
        by_team = {}
        for row in rows:
            to_team = self.api.team(self._cell(row, "to")["teamId"])
            entry = by_team.setdefault(to_team.id, {"team": to_team, "items": []})
            entry["items"].append(self._row_item_text(row))

        # Only big trades get the siren treatment — everything else posts
        # with a calmer title/color, no callout.
        is_big = len(rows) >= BREAKING_ASSET_THRESHOLD
        embed = discord.Embed(
            title="🚨 BREAKING TRADE" if is_big else "🔄 Trade Executed",
            color=discord.Color.red() if is_big else discord.Color.blue(),
        )
        # Bot's own avatar as the branding icon — matches
        # transactionstracker.py's embeds, and avoids reusing the big
        # conch illustration from /askshams which isn't sized for this.
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)
        embed.timestamp = self._trade_timestamp(rows)

        # NOTE: mentions only get parsed by Discord in an embed's
        # description/field VALUE, not in field names — a role tag placed
        # in the name renders as literal `<@&ID>` text instead of the
        # colored mention. So "receive" goes right after the mention
        # inside the value, not in the field name (which would either
        # repeat the team name or fail to render the mention at all).
        # Field name itself is left blank (Discord requires non-empty,
        # hence the zero-width space).
        for entry in by_team.values():
            items_block = "\n".join(f"• {item}" for item in entry["items"])
            embed.add_field(
                name="​",
                value=f"{role_tag(entry['team'])} receive:\n{items_block}",
                inline=True,
            )

        return embed

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
                embed = self.format_trade_embed(groups[tid])
                print(f"  [{embed.title}]")
                for f in embed.fields:
                    print(f"    {f.name}: {f.value}")
            return

        channel = self.bot.get_channel(TRADE_CHANNEL_ID)
        if channel is None:
            print(f"[TradesTracker] Channel {TRADE_CHANNEL_ID} not found.")
            return

        # Oldest-first so the feed reads chronologically, same convention
        # as transactionstracker.py. One message (embed) per trade — save
        # immediately after each send so a mid-loop crash can't leave an
        # already-posted trade unrecorded.
        for tid in reversed(new_ids):
            await channel.send(embed=self.format_trade_embed(groups[tid]))
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
        text = "\n".join(lines)

        await interaction.response.send_message(f"```\n{text[:1900]}\n```")
        for chunk_start in range(1900, len(text), 1900):
            await interaction.followup.send(f"```\n{text[chunk_start:chunk_start+1900]}\n```")

        # Also send real rendered embeds (capped) so the actual visual
        # output can be checked, not just raw metadata.
        for tid in order[:5]:
            await interaction.followup.send(embed=self.format_trade_embed(groups[tid]))


async def setup(bot):
    await bot.add_cog(TradesTracker(bot), guilds=[config.myGuild])
