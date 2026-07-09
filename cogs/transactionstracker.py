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

TRANSACTION_CHANNEL_ID = config.transactionChannelId  # ← add this to config.py

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
    def format_transaction_line(self, txn) -> str:
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

        # Insider-style anchors, mirroring the trade-blurb pattern:
        # short emoji tag + one clean sentence, no analysis/headers.
        if adds and drops:
            return (
                f"🔁 WAIVER MOVE: {team_tag} are adding {adds_bold}, "
                f"waiving {drops_bold} in a corresponding move."
            )
        elif adds:
            return f"📝 SIGNING: {team_tag} are adding {adds_bold} off waivers."
        elif drops:
            return f"✂️ CUT: {team_tag} are waiving {drops_bold}."
        elif other:
            return f"🔄 ROSTER MOVE: {team_tag} are moving {other_bold}."
        else:
            return f"🔄 ROSTER MOVE: {team_tag} made a roster move."

    # ── shared fetch/format/post logic (used by both loops) ──────────
    async def _check_and_post(self, source: str):
        txns = self.api.transactions(count=100)

        new_lines = []
        newly_posted = set()

        for txn in txns:
            if txn.id in self.posted_ids:
                continue
            new_lines.append(self.format_transaction_line(txn))
            newly_posted.add(txn.id)

        if not new_lines:
            return

        if DRY_RUN:
            print(f"[TransactionTracker DRY RUN — {source}] Would post:")
            for line in new_lines:
                print("  ", line)
            return

        channel = self.bot.get_channel(TRANSACTION_CHANNEL_ID)
        if channel is None:
            print(f"[TransactionTracker] Channel {TRANSACTION_CHANNEL_ID} not found.")
            return

        # Post oldest-first so the feed reads chronologically. Chunk into
        # multiple messages if needed — midnight PST waiver processing can
        # dump many claims at once, and Discord caps messages at 2000 chars.
        # NOTE: deliberately NOT wrapped in a ``` code block — Discord does
        # not parse <@&ID> mention syntax inside code fences, which would
        # make every role tag render as literal text instead of a mention.
        ordered_lines = list(reversed(new_lines))
        chunk = []
        chunk_len = 0
        for line in ordered_lines:
            if chunk_len + len(line) + 1 > 1900 and chunk:
                await channel.send("\n".join(chunk))
                chunk = []
                chunk_len = 0
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            await channel.send("\n".join(chunk))

        self.posted_ids |= newly_posted
        self._save_posted_ids()

    # ── background loops ──────────────────────────────────────────────
    # General sweep — mainly for drops, which can happen anytime and
    # aren't time-sensitive enough to need frequent polling. Runs hourly
    # instead of every 20 min to keep API/consumption load down.
    @tasks.loop(hours=1)
    async def check_transactions(self):
        await self._check_and_post(source="hourly sweep")

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

    # Commands
    @app_commands.command(name='teamsdebug', description='(Debug) List all league teams with their Fantrax IDs')
    async def teamsDebug(self, interaction: discord.Interaction) -> None:
        lines = [f"{t.name} (short={t.short}) | id={t.id}" for t in self.api.teams]
        text = "\n".join(lines) or "No teams returned."
        await interaction.response.send_message(f"```\n{text[:1900]}\n```")

    @app_commands.command(name='transactiondebug', description='(Debug) Dump raw recent Fantrax transactions')
    async def transactionDebug(self, interaction: discord.Interaction) -> None:
        txns = self.api.transactions(count=20)
        lines = []
        for t in txns:
            players = [(p.name, getattr(p, "type", None)) for p in t.players]
            flag = " ⚠️ MALFORMED DATE (date unreliable, harmless — still posts)" if t.date == SENTINEL_DATE else ""
            preview = self.format_transaction_line(t)
            lines.append(f"{t.id} | {t.date} | {t.team.name} | team_vars={vars(t.team)} | {players}{flag}")
            lines.append(f"    → {preview}")
        text = "\n".join(lines) or "No transactions returned."

        await interaction.response.send_message(f"```\n{text[:1900]}\n```")
        for chunk_start in range(1900, len(text), 1900):
            await interaction.followup.send(f"```\n{text[chunk_start:chunk_start+1900]}\n```")


async def setup(bot):
    await bot.add_cog(TransactionTracker(bot), guilds=[config.myGuild])