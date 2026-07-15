# Dependencies
import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

# Files
import config

# Fantrax
from fantraxapi import FantraxAPI

# Power rankings data/algorithm/LLM layer (project root, not a cog itself)
import powerrankings as pr

# Discord
import discord
from discord.ext import commands, tasks
from discord import app_commands


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

# Shares the "espn" media channel, same as the real power rankings —
# this is a separate POST (own embed, own schedule), not a variant of
# the same one, so it gets its own DRY_RUN/state rather than branching
# cogs/powerrankings.py.
RANKINGS_CHANNEL_ID = config.espnChannelId

# Flipped live 2026-07-15 — user reviewed the embed (tiers, narration,
# role tags) via /offseasonrankingsdebug and confirmed it looks right.
# The real first post establishes the rankings baseline; every post
# after that only fires on genuine rank movement (see _ranks_changed()).
DRY_RUN = False

_TZ = ZoneInfo("America/Los_Angeles")
# Checked DAILY (not weekly) — but see _ranks_changed() below, an actual
# post only fires when a team's rank has genuinely moved since last time,
# so a quiet stretch with no trades produces zero posts, not silence-
# breaking noise. 11am, deliberately right after trade grades' 10am daily
# check — if a trade shifts the rankings, its trade-grade writeup lands
# first, and this "state of the league" update follows shortly after,
# showing the ripple effect in a sensible order. Checking daily (vs. a
# fixed weekly/monthly interval) costs nothing extra by itself — only an
# actual POST costs anything, and that's gated on real movement either
# way; measured real cost is $0.0134/post, trivial even in an
# unrealistic worst-case week of a trade landing every single day
# (~$0.09/week) — see CLAUDE.md for the full reasoning.
RANKINGS_POST_TIME = datetime.time(hour=11, minute=0, tzinfo=_TZ)

# {team_id: rank} from the last time offseason rankings were actually
# posted (not updated by debug runs). Deliberately a SEPARATE file from
# posted_power_rankings.json — these are two different ranking
# mechanisms (lineup_ceiling snapshot vs. real day-by-day simulation),
# so their rank histories shouldn't be conflated once the season starts
# and the real cog's state resumes from a blank slate.
RANK_STATE_FILEPATH = Path(__file__).resolve().parent.parent / "posted_offseason_power_rankings.json"


class OffseasonPowerRankings(commands.Cog):
    def __init__(self, bot):
        print("Init Function of OffseasonPowerRankings Cog")
        self.bot = bot
        self.api = FantraxAPI(config.leagueId)
        self.check_rankings.start()

    def cog_unload(self):
        self.check_rankings.cancel()

    # ── persistence ────────────────────────────────────────────────
    def _load_previous_ranks(self) -> dict:
        if RANK_STATE_FILEPATH.exists():
            return json.loads(RANK_STATE_FILEPATH.read_text())
        return {}

    def _save_current_ranks(self, rankings):
        ranks = {team.id: rank for rank, (team, *_rest) in enumerate(rankings, start=1)}
        RANK_STATE_FILEPATH.write_text(json.dumps(ranks))

    @staticmethod
    def _ranks_changed(rankings, previous_ranks: dict) -> bool:
        """True if at least one team's rank differs from the last real
        post, OR there's no previous post to compare against (first-ever
        run). This is the actual posting gate for the daily check below
        — checking is free (pure computation, no LLM call), so checking
        daily costs nothing by itself; only an actual post costs
        anything, and this keeps that gated on genuine movement instead
        of a fixed time interval, so a quiet stretch with no trades
        produces zero posts rather than repeating an identical list."""
        if not previous_ranks:
            return True
        current_ranks = {team.id: rank for rank, (team, *_rest) in enumerate(rankings, start=1)}
        return current_ranks != previous_ranks

    # ── core ─────────────────────────────────────────────────────────
    def _build_embed(self, rankings, previous_ranks: dict = None) -> discord.Embed:
        # No records/streaks passed to format_tier_list (every team is
        # 0-0 offseason — zero informational value, see CLAUDE.md) and no
        # player-trend block in the narration (see pr.generate_offseason_
        # power_rankings_writeup()'s docstring for why that'd always be
        # empty here anyway). Tier names reuse the exact same config-
        # driven bottom tier as the real power rankings, for brand
        # consistency — the distinction from the real thing is the title/
        # footer caveat below, not different tier names.
        tier_names = pr.TIER_NAMES[:-1] + (config.lastPlaceTierName,)
        # role_ids=config.teamRoleIds — tags team roles instead of plain
        # names in the rank list, same convention as tradestracker.py/
        # tradegrades.py. Scoped to just this cog for now (not also
        # applied to the real cogs/powerrankings.py) — pings all 10 teams
        # on every post here, which is a different tradeoff than pinging
        # only the 2 teams involved in a specific trade.
        tier_list = pr.format_tier_list(rankings, records=None, tier_names=tier_names, role_ids=config.teamRoleIds)
        summary = pr.generate_offseason_power_rankings_writeup(
            rankings, config.anthropicApiKey, previous_ranks=previous_ranks, tier_names=tier_names,
        )
        description = f"{tier_list}\n\n{summary}"
        # dark_teal (not the real power rankings' teal()) — visually
        # similar family (still reads as "power rankings"), distinguishable
        # at a glance so the two posts don't get confused with each other.
        embed = discord.Embed(title="📐 Offseason Power Rankings Update", color=discord.Color.dark_teal(), description=description)
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Based on current rosters + last season's per-game stats — not a live simulation.")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _check_and_post(self):
        previous_ranks = self._load_previous_ranks()
        try:
            rankings = pr.compute_offseason_power_rankings(self.api, config.manualStatOverrides)
        except Exception as e:
            print(f"[OffseasonPowerRankings] Failed to compute rankings: {e}")
            return

        # The real posting gate — see _ranks_changed()'s docstring. Applied
        # BEFORE the DRY_RUN branch so a dry run previews exactly what the
        # real path would do (including staying silent on a quiet day),
        # not a looser approximation of it.
        if not self._ranks_changed(rankings, previous_ranks):
            return

        if DRY_RUN:
            print("[OffseasonPowerRankings DRY RUN] Ranks changed — would post:")
            for rank, (team, _total, _days, _per_day, _roster_players) in enumerate(rankings, start=1):
                print(f"  {rank}. {team.name}")
            return

        channel = self.bot.get_channel(RANKINGS_CHANNEL_ID)
        if channel is None:
            print(f"[OffseasonPowerRankings] Channel {RANKINGS_CHANNEL_ID} not found.")
            return

        await channel.send(embed=self._build_embed(rankings, previous_ranks))
        # Only a real post updates the "last check-in" baseline — a debug
        # run must never silently shift what counts as "no change".
        self._save_current_ranks(rankings)

    # ── background loop ─────────────────────────────────────────────
    @tasks.loop(time=RANKINGS_POST_TIME)
    async def check_rankings(self):
        # Daily, no weekday gate — see RANKINGS_POST_TIME's comment for
        # why checking daily is free and the real cost gate is
        # _ranks_changed(), not this schedule.
        await self._check_and_post()

    @check_rankings.before_loop
    async def before_check_rankings(self):
        await self.bot.wait_until_ready()

    # Commands — no synthetic mode here (unlike the real power rankings'
    # /rankingsdebug): this whole feature exists BECAUSE real data already
    # works offseason (last season's real stats), so there's no "can't
    # test until later" gap to fill with placeholder data.
    @app_commands.command(name='offseasonrankingsdebug', description='(Debug) Preview the offseason power rankings')
    @app_commands.default_permissions(manage_guild=True)
    async def offseasonRankingsDebug(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # the LLM call can take a few seconds

        previous_ranks = self._load_previous_ranks()
        try:
            rankings = pr.compute_offseason_power_rankings(self.api, config.manualStatOverrides)
        except Exception as e:
            await interaction.followup.send(f"Failed to compute rankings: {e}")
            return

        # Raw numbers for verification — deliberately NOT what gets posted
        # for real (the embed has no numbers, per design), just a
        # diagnostic dump so the underlying computation can be checked.
        lines = [
            f"{rank}. {team.name} | ceiling={per_day:.2f} "
            f"| top_players={', '.join(name for _pid, name, _fp_g, _fpts, _gp, _age in roster_players[:6])}"
            for rank, (team, _total, _days, per_day, roster_players) in enumerate(rankings, start=1)
        ]
        text = "\n".join(lines)
        await interaction.followup.send(f"```\n{text[:1900]}\n```")

        await interaction.followup.send(embed=self._build_embed(rankings, previous_ranks))


async def setup(bot):
    await bot.add_cog(OffseasonPowerRankings(bot), guilds=[config.myGuild])
