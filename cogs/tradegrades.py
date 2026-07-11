# Dependencies
import datetime
import json
from datetime import datetime as _dt
from pathlib import Path
from zoneinfo import ZoneInfo

# Files
import config

# Fantrax
from fantraxapi import FantraxAPI
from fantraxapi.api import Method, request as fantrax_request

# Trade analysis (project root, not a cog itself) — same "data/algorithm
# layer separate from Discord wiring" pattern as powerrankings.py.
import tradegrades as tg

# Discord
import discord
from discord.ext import commands, tasks
from discord import app_commands


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

# Shares the "espn" channel with weeklyrecap.py/powerrankings.py/
# dailytopperformer.py — periodic coverage/media content, as opposed to
# #news's real-time transaction/trade wire (where the trade itself was
# already announced by tradestracker.py — this is the follow-up
# analysis, deliberately separate in both timing and channel).
TRADE_GRADE_CHANNEL_ID = config.espnChannelId

# Flipped live 2026-07-11 — the underlying analysis pipeline (parse_trade_
# rows/analyze_trade/generate_trade_grade_writeup) was already verified
# against real trade history first (see CLAUDE.md), unlike
# powerrankings.py/dailytopperformer.py which have hard technical
# blockers until the season starts. posted_trade_grades.json was
# pre-seeded with this league's older trades so the first real run only
# grades the 2 most recent (not a 5-trade backlog dump) — see CLAUDE.md
# for exactly which txSetIds and why.
DRY_RUN = False

_TZ = ZoneInfo("America/Los_Angeles")
# 10am, deliberately after the 8am/9am cluster (daily top performer,
# weekly recap, power rankings) so a day with several of these doesn't
# stack everything at once.
TRADE_GRADE_POST_TIME = datetime.time(hour=10, minute=0, tzinfo=_TZ)

STATE_FILEPATH = Path(__file__).resolve().parent.parent / "posted_trade_grades.json"

# Same raw view tradestracker.py uses (getTransactionDetailsHistory,
# view="TRADE") — re-fetched independently here rather than sharing
# tradestracker.py's fetch, keeping the two cogs decoupled (same
# established preference as the rest of this codebase: duplicate a
# little fetch/parse logic rather than cross-import between cogs).
TRADE_VIEW = "TRADE"
TRADE_DATE_FORMAT = "%a %b %d, %Y, %I:%M%p"
TRADE_TZ = ZoneInfo("America/New_York")  # the "date" cell is always labeled EDT regardless of actual DST state


class TradeGrades(commands.Cog):
    def __init__(self, bot):
        print("Init Function of TradeGrades Cog")
        self.bot = bot
        self.api = FantraxAPI(config.leagueId)
        self.posted_ids = self._load_posted_ids()
        self.check_trade_grades.start()

    def cog_unload(self):
        self.check_trade_grades.cancel()

    # ── persistence ────────────────────────────────────────────────
    def _load_posted_ids(self) -> set:
        if STATE_FILEPATH.exists():
            return set(json.loads(STATE_FILEPATH.read_text()))
        return set()

    def _save_posted_ids(self):
        STATE_FILEPATH.write_text(json.dumps(sorted(self.posted_ids)))

    # ── raw fetch/group (mirrors tradestracker.py's _fetch_trade_groups) ──
    @staticmethod
    def _cell(row, key):
        return next((c for c in row["cells"] if c["key"] == key), None)

    def _fetch_trade_groups(self):
        """Returns (groups, order): groups maps txSetId -> list of raw
        row dicts, order is txSetIds newest-first (same convention
        tradestracker.py's own fetch already confirmed live)."""
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

    def _trade_timestamp(self, rows: list) -> datetime.datetime:
        date_cell = self._cell(rows[0], "date")
        if date_cell and date_cell.get("content"):
            try:
                return _dt.strptime(date_cell["content"], TRADE_DATE_FORMAT).replace(tzinfo=TRADE_TZ)
            except ValueError:
                pass
        return discord.utils.utcnow()

    # ── core ─────────────────────────────────────────────────────────
    @staticmethod
    def _team_tag(team_id: str, team_name: str) -> str:
        role_id = config.teamRoleIds.get(team_id)
        return f"<@&{role_id}>" if role_id else f"**{team_name}**"

    @staticmethod
    def _chunk_field_value(text: str, limit: int = 1024) -> list:
        """Splits text into <=limit-char chunks for use as Discord field
        values, preferring to break at paragraph boundaries (blank lines)
        so a multi-paragraph narrative (see tradegrades.py's
        _SYSTEM_PROMPT — now 1-3 paragraphs) doesn't get cut off mid-
        sentence on a bigger trade. In practice this almost always
        returns a single chunk (measured real narratives run ~600-900
        characters); the multi-chunk path is a rare safety net, not the
        common case."""
        if len(text) <= limit:
            return [text]
        chunks, current = [], ""
        for para in text.split("\n\n"):
            candidate = f"{current}\n\n{para}" if current else para
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
            if len(para) <= limit:
                current = para
            else:  # a single paragraph longer than the limit on its own — hard-split as a last resort
                for i in range(0, len(para), limit):
                    chunks.append(para[i:i + limit])
                current = ""
        if current:
            chunks.append(current)
        return chunks

    def _build_embed(self, trade_analysis: dict, verdict: str, narrative: str) -> discord.Embed:
        embed = discord.Embed(title="📊 Trade Analysis", color=discord.Color.gold())
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()

        # Trade summary — one inline field per team, same "who received
        # what" shape as tradestracker.py's original announcement embed
        # (built from trade_analysis's already-resolved acquired_names/
        # ages/rookies/picks_acquired, not a re-fetch of the raw rows) —
        # so the grade post is self-contained and doesn't require
        # scrolling back to #news to see what was actually traded.
        for team_id, info in trade_analysis.items():
            items = [
                f"• **{name}** (age {age:.0f}{', Rookie' if rookie else ''})"
                for name, age, rookie in zip(info["acquired_names"], info["acquired_ages"], info["acquired_rookies"])
            ]
            items += [f"• {pick}" for pick in info["picks_acquired"]]
            items_block = "\n".join(items) if items else "*(picks/cash only — no players)*"
            embed.add_field(
                name="​",
                value=f"{self._team_tag(team_id, info['team_name'])} receive:\n{items_block}",
                inline=True,
            )

        embed.add_field(name="📈 Verdict", value=f"**{verdict}**"[:1024], inline=False)

        # Chunked (not truncated) in case a real narrative ever exceeds a
        # single field's 1024-character cap — see _chunk_field_value().
        # Continuation chunks reuse the same blank-name-field convention
        # as the team fields above, rather than repeating "📝 Analysis".
        narrative_chunks = self._chunk_field_value(narrative) if narrative else ["*(no narrative generated)*"]
        for i, chunk in enumerate(narrative_chunks):
            embed.add_field(name="📝 Analysis" if i == 0 else "​", value=chunk, inline=False)
        return embed

    def _grade_trade(self, rows: list) -> discord.Embed:
        """rows: one trade's raw rows. Runs the full parse -> analyze ->
        narrate -> embed pipeline. Makes a real (paid) LLM call — do not
        call this in a loop without the DRY_RUN/dedup gating below."""
        parsed = tg.parse_trade_rows(rows)
        analysis = tg.analyze_trade(self.api, parsed)
        grade = tg.generate_trade_grade_writeup(config.anthropicApiKey, analysis)
        return self._build_embed(analysis, grade["verdict"], grade["narrative"])

    async def _check_and_post(self):
        try:
            groups, order = self._fetch_trade_groups()
        except Exception as e:
            print(f"[TradeGrades] Failed to fetch trade history: {e}")
            return

        today = datetime.datetime.now(_TZ).date()
        # Only grade trades from a full day ago or earlier — "analysis
        # happens the next day", not immediately (explicit design
        # decision, not just a scheduling accident). Any backlog (bot
        # was down a few days, several trades queued) all gets graded in
        # one pass — dedup via posted_ids prevents double-posting, same
        # pattern as every other tracker's catch-up behavior.
        pending = [
            tid for tid in order
            if tid not in self.posted_ids and self._trade_timestamp(groups[tid]).astimezone(_TZ).date() < today
        ]
        if not pending:
            return

        if DRY_RUN:
            print(f"[TradeGrades DRY RUN] Would grade {len(pending)} trade(s): {pending}")
            return

        channel = self.bot.get_channel(TRADE_GRADE_CHANNEL_ID)
        if channel is None:
            print(f"[TradeGrades] Channel {TRADE_GRADE_CHANNEL_ID} not found.")
            return

        # Oldest-first, same chronological-feed convention as
        # tradestracker.py — save immediately after each post so a
        # mid-loop crash can't leave a graded trade unrecorded.
        for tid in reversed(pending):
            try:
                embed = self._grade_trade(groups[tid])
            except Exception as e:
                print(f"[TradeGrades] Failed to grade trade {tid}: {e}")
                continue
            await channel.send(embed=embed)
            self.posted_ids.add(tid)
            self._save_posted_ids()

    # ── background loop ─────────────────────────────────────────────
    @tasks.loop(time=TRADE_GRADE_POST_TIME)
    async def check_trade_grades(self):
        await self._check_and_post()

    @check_trade_grades.before_loop
    async def before_check_trade_grades(self):
        await self.bot.wait_until_ready()

    # ── synthetic test data ──────────────────────────────────────────
    def _synthetic_analysis(self) -> dict:
        """Fake trade analysis (real team objects, made-up players/ages/
        value) so /tradegradedebug can preview the embed without a real
        trade to grade — same pattern as every other tracker's synthetic
        mode. Deliberately mirrors a real trade shape seen in this
        league's actual history: a win-now team trading picks + youth
        for proven current production."""
        teams = list(self.api.teams)
        team_a, team_b = teams[0], teams[1]

        class _FakeRecord:
            def __init__(self, win, loss, streak):
                self.win, self.loss, self.streak = win, loss, streak

        return {
            team_a.id: {
                "team_name": team_a.name,
                "marginal_value_delta": 22.5,
                "acquired_names": ["Kevin Durant", "Anthony Davis"],
                "acquired_ages": [37.0, 33.0],
                "acquired_rookies": [False, False],
                "given_up_names": ["Rookie Guard", "Young Wing"],
                "given_up_ages": [21.0, 22.0],
                "given_up_rookies": [True, False],
                "picks_acquired": [],
                "picks_given_up": ["a 2028 1st-round pick", "a 2029 2nd-round pick"],
                "roster_avg_age": 28.4,
                "record": _FakeRecord(9, 1, "W6"),
            },
            team_b.id: {
                "team_name": team_b.name,
                "marginal_value_delta": -22.3,
                "acquired_names": ["Rookie Guard", "Young Wing"],
                "acquired_ages": [21.0, 22.0],
                "acquired_rookies": [True, False],
                "given_up_names": ["Kevin Durant", "Anthony Davis"],
                "given_up_ages": [37.0, 33.0],
                "given_up_rookies": [False, False],
                "picks_acquired": ["a 2028 1st-round pick", "a 2029 2nd-round pick"],
                "picks_given_up": [],
                "roster_avg_age": 23.1,
                "record": _FakeRecord(2, 8, "L4"),
            },
        }

    # Commands
    @app_commands.command(name='tradegradedebug', description='(Debug) Preview a trade analysis')
    @app_commands.describe(synthetic="Use made-up placeholder data instead of a real trade (useful pre-season)")
    @app_commands.default_permissions(manage_guild=True)
    async def tradeGradeDebug(self, interaction: discord.Interaction, synthetic: bool = False) -> None:
        await interaction.response.defer()  # the LLM call can take a few seconds

        if synthetic:
            analysis = self._synthetic_analysis()
            header = "⚠️ SYNTHETIC TEST DATA — not a real trade.\n\n"
        else:
            try:
                groups, order = self._fetch_trade_groups()
            except Exception as e:
                await interaction.followup.send(f"Failed to fetch trade history: {e}")
                return
            if not order:
                await interaction.followup.send(
                    "No trades found. Run again with `synthetic: True` to preview with placeholder data instead."
                )
                return
            # Debug grades the most recent trade regardless of age/graded
            # status — intentionally bypasses the "wait a day" + dedup
            # gating in _check_and_post(), same "debug never touches or
            # is blocked by persisted state" principle as every other
            # tracker's debug command.
            analysis = tg.analyze_trade(self.api, tg.parse_trade_rows(groups[order[0]]))
            header = ""

        grade = tg.generate_trade_grade_writeup(config.anthropicApiKey, analysis)

        lines = [header, f"verdict={grade['verdict']!r}"]
        for info in analysis.values():
            lines.append(
                f"{info['team_name']}: marginal_value_delta={info['marginal_value_delta']:.2f} "
                f"| acquired={info['acquired_names']} | given_up={info['given_up_names']}"
            )
        await interaction.followup.send(f"```\n{chr(10).join(lines)[:1900]}\n```")

        embed = self._build_embed(analysis, grade["verdict"], grade["narrative"])
        if synthetic:
            embed.set_footer(text="⚠️ Synthetic test data — not a real trade.")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(TradeGrades(bot), guilds=[config.myGuild])
