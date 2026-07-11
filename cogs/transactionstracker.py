# Dependencies
import datetime
import json
from datetime import datetime as _real_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Files
import config

# Fantrax
from fantraxapi import FantraxAPI
from fantraxapi.objs import transaction as _fantrax_transaction_module

# Discord
import discord
from discord.ext import commands, tasks
from discord import app_commands


# ─────────────────────────────────────────────────────────────────────────
# PATCH — fantraxapi's Transaction.__init__ assumes every transaction row
# has a valid date string in a fixed cell position and crashes with a bare
# ValueError when that assumption doesn't hold (confirmed: every row in
# this league fails it — the date cell position the library expects isn't
# where it thinks it is for this league/sport). This swaps in a tolerant
# parser: anything that fails to parse as a date becomes a Jan 1 1970
# sentinel instead of throwing, so a bad cell doesn't kill the whole
# transactions() call. Remove this if/when upstream fixes it:
# https://github.com/meisnate12/FantraxAPI
# ─────────────────────────────────────────────────────────────────────────

SENTINEL_DATE = _real_datetime(1970, 1, 1)


class _SafeDatetimeParser:
    @staticmethod
    def strptime(date_string, fmt):
        try:
            return _real_datetime.strptime(date_string, fmt)
        except (ValueError, TypeError):
            return SENTINEL_DATE


_fantrax_transaction_module.datetime = _SafeDatetimeParser


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

TRANSACTION_CHANNEL_ID = config.newsChannelId

# Anchored to the project root (one level up from cogs/) rather than a bare
# relative path, so it lands in the same place regardless of what directory
# the bot process happens to be launched from.
STATE_FILEPATH = Path(__file__).resolve().parent.parent / "posted_transactions.json"

DRY_RUN = False  # ← keep True until /transactiondebug output looks right

# Fantrax processes all waiver claims at midnight Pacific. zoneinfo handles
# PST/PDT (daylight saving) automatically, so this stays correct year-round
# without manual adjustment. Small 5-min buffer so processing has finished.
WAIVER_PROCESS_TIME = datetime.time(hour=0, minute=5, tzinfo=ZoneInfo("America/Los_Angeles"))

def role_tag(team) -> str:
    team_id = getattr(team, "id", None)
    role_id = config.teamRoleIds.get(team_id) if team_id else None
    return f"<@&{role_id}>" if role_id else f"**{team.name}**"


# Transaction type buckets — ADJUST ME if /transactiondebug shows other
# type strings we haven't accounted for (e.g. a real 'TRADE' type, or a
# 'CLAIM' distinct from 'WW').
ADD_TYPES = {"FA", "WW", "CLAIM"}
DROP_TYPES = {"DROP"}


class TransactionTracker(commands.Cog):
    def __init__(self, bot):
        print("Init Function of TransactionTracker Cog")
        self.bot = bot

        # Init Fantrax api instance
        self.api = FantraxAPI(config.leagueId)

        self.posted_ids = self._load_posted_ids()
        self.check_transactions.start()
        self.check_waiver_batch.start()

    def cog_unload(self):
        self.check_transactions.cancel()
        self.check_waiver_batch.cancel()

    # ── persistence ────────────────────────────────────────────────
    def _load_posted_ids(self) -> set:
        if STATE_FILEPATH.exists():
            return set(json.loads(STATE_FILEPATH.read_text()))
        return set()

    def _save_posted_ids(self):
        STATE_FILEPATH.write_text(json.dumps(sorted(self.posted_ids)))

    # ── formatting ──────────────────────────────────────────────────
    # NOTE: the date field is unreliable for this league (confirmed via
    # /transactiondebug — every single transaction fails to parse a real
    # date, not just occasional bad rows). We don't use dates anywhere in
    # the announcement format, so we no longer gate on date validity at
    # all — dedup is handled entirely by posted_ids/txn.id below.
    def _classify_transaction(self, txn) -> tuple:
        """Returns (line_text, color) — shared by the live embed, the
        DRY_RUN console preview, and /transactiondebug."""
        adds = [p.name for p in txn.players if getattr(p, "type", "").upper() in ADD_TYPES]
        drops = [p.name for p in txn.players if getattr(p, "type", "").upper() in DROP_TYPES]
        other = [
            p.name for p in txn.players
            if getattr(p, "type", "").upper() not in ADD_TYPES | DROP_TYPES
        ]

        team_tag = role_tag(txn.team)
        adds_bold = ", ".join(f"**{n}**" for n in adds)
        drops_bold = ", ".join(f"**{n}**" for n in drops)
        other_bold = ", ".join(f"**{n}**" for n in other)

        # FAAB bid amount — confirmed via /biddebug to live at cells[1] on
        # the first raw row of an add-type transaction (this is also the
        # cell the library's buggy date parser was misreading as a date).
        # Only meaningful when there's an actual add in this transaction.
        bid_phrase = ""
        if adds:
            bid = self._get_bid_amount(txn)
            if bid is not None:
                bid_phrase = f" for ${bid}"

        # Tweet-style phrasing: "adding X for $Y, and dropping Z to make
        # room" reads closer to how FAAB pickups actually get reported
        # than a more formal "waiving" construction. Color loosely
        # matches the move type: green = any add (including a sign-and-
        # drop, since the headline is still "acquired a player"), red =
        # pure cut, blurple = anything else.
        if adds and drops:
            text = (
                f"📝 {team_tag} are adding {adds_bold}{bid_phrase}, "
                f"and dropping {drops_bold} to make room."
            )
            color = discord.Color.green()
        elif adds:
            text = f"📝 {team_tag} are adding {adds_bold}{bid_phrase} off waivers."
            color = discord.Color.green()
        elif drops:
            text = f"✂️ {team_tag} are dropping {drops_bold}."
            color = discord.Color.red()
        elif other:
            text = f"🔄 {team_tag} are moving {other_bold}."
            color = discord.Color.blurple()
        else:
            text = f"🔄 {team_tag} made a roster move."
            color = discord.Color.blurple()

        return text, color

    def format_transaction_embed(self, txn) -> discord.Embed:
        text, color = self._classify_transaction(txn)
        embed = discord.Embed(description=text, color=color)
        # Bot's own avatar as the branding icon — already square/sized
        # correctly for this, unlike the big conch image used in
        # /askshams which is a full illustration, not an icon.
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()
        return embed

    @staticmethod
    def _get_bid_amount(txn):
        """Extract the FAAB bid amount from the raw transaction data.
        Confirmed via /biddebug: row[0].cells[1] holds it as a string like
        '10.00' for FA/WW/CLAIM-type transactions. Returns None if it's
        not present or doesn't look like a plausible bid amount (guards
        against DROP-only transactions, which don't have this cell in the
        same position, or any future format we haven't seen yet)."""
        try:
            cells = txn._data[0].get("cells", [])
            raw = cells[1].get("content", "") if len(cells) > 1 else ""
            # Bid amounts look like '10.00', '0.00', '24.00' — a plain
            # non-negative decimal number.
            if raw and raw.replace(".", "", 1).isdigit():
                return raw
        except (IndexError, KeyError, AttributeError, TypeError):
            pass
        return None

    # ── shared fetch/format/post logic (used by both loops) ──────────
    async def _check_and_post(self, source: str):
        txns = self.api.transactions(count=100)

        new_entries = []  # list of (txn.id, txn)
        for txn in txns:
            if txn.id in self.posted_ids:
                continue
            new_entries.append((txn.id, txn))

        if not new_entries:
            return

        if DRY_RUN:
            print(f"[TransactionTracker DRY RUN — {source}] Would post:")
            for _, txn in new_entries:
                text, _ = self._classify_transaction(txn)
                print("  ", text)
            return

        channel = self.bot.get_channel(TRANSACTION_CHANNEL_ID)
        if channel is None:
            print(f"[TransactionTracker] Channel {TRANSACTION_CHANNEL_ID} not found.")
            return

        # Post oldest-first so the feed reads chronologically. One message
        # (embed) per transaction — save immediately after each send so a
        # mid-loop crash can't leave an already-posted transaction
        # unrecorded (matches tradestracker.py's convention). Role
        # mentions inside embeds still render as the colored role name,
        # they just don't trigger a ping — acceptable since Fantrax's own
        # site already notifies everyone for transactions/trades.
        for txn_id, txn in reversed(new_entries):
            await channel.send(embed=self.format_transaction_embed(txn))
            self.posted_ids.add(txn_id)
            self._save_posted_ids()

    # ── background loops ──────────────────────────────────────────────
    # General sweep — mainly for drops, which can happen anytime. Every
    # 5 min for near-immediate reporting — still a trivial request volume
    # (public, unauthenticated endpoint, same call the Fantrax website
    # itself makes on page load).
    @tasks.loop(minutes=5)
    async def check_transactions(self):
        await self._check_and_post(source="5-min sweep")

    @check_transactions.before_loop
    async def before_check_transactions(self):
        await self.bot.wait_until_ready()

    # Waiver batch — Fantrax processes all claims at midnight Pacific
    # (PST/PDT, DST-aware via zoneinfo), so this fires shortly after that
    # to report the whole wave promptly instead of waiting for the next
    # hourly sweep.
    @tasks.loop(time=WAIVER_PROCESS_TIME)
    async def check_waiver_batch(self):
        await self._check_and_post(source="midnight waiver batch")

    @check_waiver_batch.before_loop
    async def before_check_waiver_batch(self):
        await self.bot.wait_until_ready()

    # Commands — debug commands are gated to manage_guild so Discord hides
    # them from the slash-command picker for regular league members
    # entirely, not just blocked-on-run.
    @app_commands.command(name='faabdebug', description='(Debug) Inspect standings data for FAAB remaining info')
    @app_commands.default_permissions(manage_guild=True)
    async def faabDebug(self, interaction: discord.Interaction) -> None:
        standings = self.api.standings()
        lines = []
        for record in standings.records.values() if hasattr(standings, "records") else []:
            lines.append(f"team={getattr(record, 'team', None)} | vars={vars(record)}")
        if not lines:
            # Fallback in case the attribute name isn't "records" — dump
            # whatever the Standings object itself looks like instead.
            lines.append(f"standings_vars={vars(standings)}")
        text = "\n".join(lines) or "No data."
        await interaction.response.send_message(f"```\n{text[:1900]}\n```")
        for chunk_start in range(1900, len(text), 1900):
            await interaction.followup.send(f"```\n{text[chunk_start:chunk_start+1900]}\n```")

    @app_commands.command(name='teamsdebug', description='(Debug) List all league teams with their Fantrax IDs')
    @app_commands.default_permissions(manage_guild=True)
    async def teamsDebug(self, interaction: discord.Interaction) -> None:
        lines = [f"{t.name} (short={t.short}) | id={t.id}" for t in self.api.teams]
        text = "\n".join(lines) or "No teams returned."
        await interaction.response.send_message(f"```\n{text[:1900]}\n```")

    @app_commands.command(name='biddebug', description='(Debug) Dump full raw row/cell data to locate FAAB bid amounts')
    @app_commands.default_permissions(manage_guild=True)
    async def bidDebug(self, interaction: discord.Interaction) -> None:
        txns = self.api.transactions(count=30)
        # No CLAIM-type rows exist in this league's feed — widen to WW/FA,
        # since the bid amount (if present at all) likely lives on one of
        # those instead.
        candidate_txns = [
            t for t in txns
            if any(getattr(p, "type", "").upper() in ("CLAIM", "WW", "FA") for p in t.players)
        ]

        if not candidate_txns:
            await interaction.response.send_message("No FA/WW/CLAIM transactions found in the last 30.")
            return

        lines = []
        # Dump every row (not just row 0) and every cell in each row, so we
        # can see the full raw structure and spot where a bid number sits.
        for t in candidate_txns[:5]:  # a handful of examples is enough
            player_types = [(p.name, getattr(p, "type", None)) for p in t.players]
            lines.append(f"=== txn {t.id} | team={t.team.name} | players={player_types} ===")
            for row_idx, row in enumerate(t._data):
                cell_dump = " || ".join(
                    f"[{i}]={c.get('content', '')!r}"
                    for i, c in enumerate(row.get("cells", []))
                )
                lines.append(f"  row[{row_idx}]: {cell_dump}")

        text = "\n".join(lines) or "No data."
        await interaction.response.send_message(f"```\n{text[:1900]}\n```")
        for chunk_start in range(1900, len(text), 1900):
            await interaction.followup.send(f"```\n{text[chunk_start:chunk_start+1900]}\n```")

    @app_commands.command(name='transactiondebug', description='(Debug) Dump raw recent Fantrax transactions')
    @app_commands.default_permissions(manage_guild=True)
    async def transactionDebug(self, interaction: discord.Interaction) -> None:
        txns = self.api.transactions(count=20)
        lines = []
        for t in txns:
            players = [(p.name, getattr(p, "type", None)) for p in t.players]
            flag = " ⚠️ MALFORMED DATE (date unreliable, harmless — still posts)" if t.date == SENTINEL_DATE else ""
            preview, _ = self._classify_transaction(t)
            lines.append(f"{t.id} | {t.date} | {t.team.name} | team_vars={vars(t.team)} | {players}{flag}")
            lines.append(f"    → {preview}")
        text = "\n".join(lines) or "No transactions returned."

        await interaction.response.send_message(f"```\n{text[:1900]}\n```")
        for chunk_start in range(1900, len(text), 1900):
            await interaction.followup.send(f"```\n{text[chunk_start:chunk_start+1900]}\n```")

        # Also send real rendered embeds (capped) so the actual visual
        # output can be checked, not just raw metadata.
        for t in txns[:5]:
            await interaction.followup.send(embed=self.format_transaction_embed(t))


async def setup(bot):
    await bot.add_cog(TransactionTracker(bot), guilds=[config.myGuild])