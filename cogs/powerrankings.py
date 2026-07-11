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

# Shares the "espn" channel with weeklyrecap.py — both are periodic
# summary content, as opposed to #news's real-time individual events.
RANKINGS_CHANNEL_ID = config.espnChannelId

# Was False (live) before the 2026-07-10 session — temporarily reverted
# to True because that session layered substantial new logic on top that
# has ONLY been verified with synthetic/mocked data, never real season
# numbers: gap-based tiers, the 4th "Favorites" tier + config-driven
# bottom-tier name, player-trend narration, recent-form blending in the
# simulation, and injury/suspension exclusion. Flip back to False once
# you've run /rankingsdebug (non-synthetic) against real in-season data
# and the tier grouping + blurb + player trends all look right — see
# CLAUDE.md's "Media ecosystem" section for the full verification-timing
# breakdown per piece.
DRY_RUN = True

_TZ = ZoneInfo("America/Los_Angeles")
# Midweek, deliberately NOT the Monday recap/standings/preview slot —
# spreads periodic content across the week instead of bunching it all on
# Monday.
RANKINGS_POST_TIME = datetime.time(hour=9, minute=0, tzinfo=_TZ)
RANKINGS_WEEKDAY = 2  # Wednesday (Monday=0)

# {team_id: rank} from the last time rankings were actually posted (not
# updated by debug runs — see _save_current_ranks). Used to compute
# week-over-week movement, the "notable changes" the blurb focuses on.
RANK_STATE_FILEPATH = Path(__file__).resolve().parent.parent / "posted_power_rankings.json"

# {player_id: {"fpts": total, "gp": games_played}} from the last time
# rankings were actually posted (not updated by debug runs — see
# _save_current_player_stats). Used by pr.compute_player_trends() to
# derive each player's trailing-window scoring rate since the last post.
PLAYER_STATS_STATE_FILEPATH = Path(__file__).resolve().parent.parent / "posted_player_stats.json"


class PowerRankings(commands.Cog):
    def __init__(self, bot):
        print("Init Function of PowerRankings Cog")
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

    def _load_previous_player_stats(self) -> dict:
        if PLAYER_STATS_STATE_FILEPATH.exists():
            return json.loads(PLAYER_STATS_STATE_FILEPATH.read_text())
        return {}

    def _save_current_player_stats(self, rankings):
        stats = {}
        for team, _total, _days, _per_day, roster_players in rankings:
            for player_id, _name, _fp_g, fpts_total, gp, _age in roster_players:
                stats[str(player_id)] = {"fpts": fpts_total, "gp": gp}
        PLAYER_STATS_STATE_FILEPATH.write_text(json.dumps(stats))

    # ── core ─────────────────────────────────────────────────────────
    def _compute(self, previous_player_stats: dict = None):
        """Returns (rankings, records), or (None, None) if there's
        nothing meaningful to rank yet — pre-season (every team's
        points_per_day is exactly 0.0, confirmed live) or no periods
        left to simulate (end of season, see CLAUDE.md's playoff-window
        note). previous_player_stats: optional {player_id: {"fpts",
        "gp"}} — passed straight through to compute_power_rankings() so
        the SIMULATION itself (not just the narration) can blend toward
        each player's recent form; caller loads it via
        _load_previous_player_stats() before calling this, since it's
        needed here now, not just later for _build_embed()."""
        rankings = pr.compute_power_rankings(self.api, previous_player_stats=previous_player_stats)
        if not rankings or all(r[3] == 0.0 for r in rankings):
            return None, None
        records = pr.record_lookup(self.api)
        return rankings, records

    def _build_embed(self, rankings, records, previous_ranks: dict = None,
                      previous_player_stats: dict = None) -> discord.Embed:
        # Rank list is pure structure (code-only, no LLM) — the LLM's
        # only job is the short "notable changes" summary appended below
        # it, not a per-team breakdown.
        # Override just the bottom tier's name/emoji from config (tied to
        # whatever the league's current last-place punishment is) — the
        # top 3 tier names stay as pr.TIER_NAMES' code defaults. Must be
        # passed to BOTH calls below so the LLM's tier references match
        # what's actually printed in the rank list above it.
        tier_names = pr.TIER_NAMES[:-1] + (config.lastPlaceTierName,)
        tier_list = pr.format_tier_list(rankings, records, tier_names=tier_names)
        summary = pr.generate_power_rankings_writeup(
            rankings, config.anthropicApiKey, records=records, previous_ranks=previous_ranks,
            previous_player_stats=previous_player_stats, tier_names=tier_names,
        )
        description = f"{tier_list}\n\n{summary}"
        embed = discord.Embed(title="📊 Power Rankings", color=discord.Color.teal(), description=description)
        # Bot's own avatar as the branding icon, matching every other
        # tracker's embeds — not the big conch image from /askshams,
        # which isn't sized for this.
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _check_and_post(self):
        # Loaded up front now (not just before _build_embed as before) —
        # previous_player_stats feeds the SIMULATION via _compute() too,
        # not only the narration.
        previous_ranks = self._load_previous_ranks()
        previous_player_stats = self._load_previous_player_stats()

        rankings, records = self._compute(previous_player_stats)
        if rankings is None:
            return  # nothing to rank yet

        if DRY_RUN:
            print("[PowerRankings DRY RUN] Would post power rankings:")
            for rank, (team, _total, _days, _per_day, _roster_players) in enumerate(rankings, start=1):
                print(f"  {rank}. {team.name}")
            return

        channel = self.bot.get_channel(RANKINGS_CHANNEL_ID)
        if channel is None:
            print(f"[PowerRankings] Channel {RANKINGS_CHANNEL_ID} not found.")
            return

        await channel.send(embed=self._build_embed(rankings, records, previous_ranks, previous_player_stats))
        # Only a real post updates the "last week" baseline — a debug
        # run must never silently shift what counts as "no change".
        self._save_current_ranks(rankings)
        self._save_current_player_stats(rankings)

    # ── background loop ─────────────────────────────────────────────
    @tasks.loop(time=RANKINGS_POST_TIME)
    async def check_rankings(self):
        if datetime.datetime.now(_TZ).weekday() != RANKINGS_WEEKDAY:
            return
        await self._check_and_post()

    @check_rankings.before_loop
    async def before_check_rankings(self):
        await self.bot.wait_until_ready()

    # ── synthetic test data ──────────────────────────────────────────
    # Real team objects, made-up scores/records/top-players — lets
    # /rankingsdebug exercise the full pipeline (embed rendering + real
    # LLM narration) without waiting for the season to start, when real
    # scores are all exactly 0.0. Clearly labeled wherever it's used —
    # this must never be mistaken for a real projection.
    def _synthetic_rankings(self):
        teams = list(self.api.teams)
        scores = [312.4, 298.1, 287.6, 275.0, 268.9, 250.3, 241.7, 230.2, 218.5, 195.0][: len(teams)]
        # Distinct fake rosters per team (not the same 6 names repeated for
        # everyone) — a real /rankingsdebug run needs this to meaningfully
        # test that the LLM grounds each team's commentary in ITS OWN
        # players, not a shared sample set.
        real_players = [
            "Shai Gilgeous-Alexander", "Nikola Jokic", "Luka Doncic", "Giannis Antetokounmpo",
            "Tyrese Maxey", "Anthony Davis", "Donovan Mitchell", "Jalen Suggs",
            "Victor Wembanyama", "Anthony Edwards", "Devin Booker", "Kevin Durant",
            "LeBron James", "Karl-Anthony Towns", "Jayson Tatum", "Trae Young",
            "Ja Morant", "Domantas Sabonis", "De'Aaron Fox", "Paolo Banchero",
            "Cade Cunningham", "Franz Wagner", "Alperen Sengun", "Jalen Brunson",
            "Jaylen Brown", "Zion Williamson", "Jalen Williams", "Scottie Barnes",
            "Chet Holmgren", "Bam Adebayo", "Kawhi Leonard", "Darius Garland",
            "Evan Mobley", "James Harden", "Tyler Herro", "Desmond Bane",
            "Coby White", "OG Anunoby", "Julius Randle", "Damian Lillard",
            "Jamal Murray", "Michael Porter Jr.", "Brandon Ingram", "Zach LaVine",
            "Fred VanVleet", "Jalen Green", "Cam Thomas", "Deni Avdija",
            "Norman Powell", "Jaren Jackson Jr.", "Rudy Gobert", "Ivica Zubac",
            "Onyeka Okongwu", "Karl Towns", "Nikola Vucevic", "Walker Kessler",
            "Myles Turner", "Mark Williams",
        ]
        rankings = []
        previous_player_stats = {}
        for i, s in enumerate(scores):
            team_players = []
            for j, name in enumerate(real_players[i * 6:i * 6 + 6]):
                player_id = f"synthetic-{i}-{j}"
                fp_g = round(s / 5.5 + (5 - j) * 2.5, 1)
                gp = 40  # plausible games-played-so-far for a synthetic mid-season snapshot
                fpts_total = round(fp_g * gp, 1)
                age = 21 + (i + j * 2) % 16  # varied, plausible-looking spread, not meant to be realistic
                team_players.append((player_id, name, fp_g, fpts_total, gp, age))
                previous_player_stats[player_id] = {"fpts": fpts_total, "gp": gp}  # baseline == this week
            rankings.append((teams[i], s * 3, 3, s, team_players))

        class _FakeRecord:
            def __init__(self, win, loss, streak):
                self.win, self.loss, self.streak = win, loss, streak

        # A W-L/streak for EVERY team (not just the top/bottom one) — a
        # real /rankingsdebug run needs this so format_tier_list() always
        # takes its "record available" rendering path instead of falling
        # back to the no-record blank case for 8 of 10 teams, which made
        # the preview look inconsistent with what a real post shows once
        # the season's live (record_lookup() always has every team).
        # Deliberately monotonic with rank (best record at rank 1,
        # descending) — not meant to be realistic head-to-head math, just
        # plausible-looking and varied enough to preview real formatting.
        records = {}
        for i, team in enumerate(teams):
            win = max(0, 9 - i)
            loss = 10 - win
            streak_len = max(1, min(abs(win - loss), 6))
            streak = f"W{streak_len}" if win >= loss else f"L{streak_len}"
            records[team.id] = _FakeRecord(win, loss, streak)
        # Made-up "last week" ranks too, so synthetic mode also exercises
        # the movement/"notable changes" logic — team[1] jumped from #5,
        # team[3] fell from #1, team[-2] is brand new (no previous entry).
        previous_ranks = {t.id: i + 1 for i, t in enumerate(teams)}
        if len(teams) > 4:
            previous_ranks[teams[1].id] = 5
            previous_ranks[teams[3].id] = 1
            previous_ranks.pop(teams[-2].id, None)

        # Nudge two players' PREVIOUS snapshots so pr.compute_player_trends()
        # has something to find, mirroring how previous_ranks above
        # fabricates a riser/faller at the team level — subtract a few
        # games' worth of gp/fpts from the previous snapshot so the implied
        # recent_fpg comes out well above/below that player's season fp_g.
        if len(rankings) > 1:
            riser_id, _name, riser_fpg, riser_fpts, riser_gp, _age = rankings[0][4][0]  # team[0]'s best player
            previous_player_stats[riser_id] = {"fpts": riser_fpts - 3 * (riser_fpg + 15), "gp": riser_gp - 3}
            faller_id, _name, faller_fpg, faller_fpts, faller_gp, _age = rankings[1][4][0]  # team[1]'s best player
            previous_player_stats[faller_id] = {"fpts": faller_fpts - 3 * (faller_fpg - 15), "gp": faller_gp - 3}

        return rankings, records, previous_ranks, previous_player_stats

    # Commands
    @app_commands.command(name='rankingsdebug', description='(Debug) Preview the current-season power rankings')
    @app_commands.describe(synthetic="Use made-up placeholder numbers instead of real data (useful pre-season, when real scores are all 0)")
    @app_commands.default_permissions(manage_guild=True)
    async def rankingsDebug(self, interaction: discord.Interaction, synthetic: bool = False) -> None:
        await interaction.response.defer()  # the LLM call can take a few seconds

        if synthetic:
            rankings, records, previous_ranks, previous_player_stats = self._synthetic_rankings()
            header = "⚠️ SYNTHETIC TEST DATA — not real projections.\n\n"
        else:
            previous_ranks = self._load_previous_ranks()
            previous_player_stats = self._load_previous_player_stats()
            rankings, records = self._compute(previous_player_stats)
            header = ""
            if rankings is None:
                await interaction.followup.send(
                    "No rankings available yet — either the season hasn't started, "
                    "or there aren't enough periods left in the season to simulate. "
                    "Run again with `synthetic: True` to preview with placeholder data instead."
                )
                return

        # Raw numbers for verification — deliberately NOT what gets posted
        # for real (the embed below has no numbers, per design), just a
        # diagnostic dump so the underlying computation can be checked.
        # roster_players is the FULL roster now (not just a top-6 slice),
        # so slice explicitly here to keep this dump at its original
        # top-6-only verbosity instead of printing every rostered player.
        lines = [
            f"{rank}. {team.name} | per_day={per_day:.2f} | total={total:.1f} | days_simulated={days} "
            f"| top_players={', '.join(name for _pid, name, _fp_g, _fpts, _gp, _age in roster_players[:6])}"
            for rank, (team, total, days, per_day, roster_players) in enumerate(rankings, start=1)
        ]
        text = header + "\n".join(lines)
        await interaction.followup.send(f"```\n{text[:1900]}\n```")

        embed = self._build_embed(rankings, records, previous_ranks, previous_player_stats)
        if synthetic:
            embed.set_footer(text="⚠️ Synthetic test data — not a real projection.")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(PowerRankings(bot), guilds=[config.myGuild])
