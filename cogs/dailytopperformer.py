# Dependencies
import datetime
from zoneinfo import ZoneInfo

# Files
import config

# Fantrax
from fantraxapi import FantraxAPI
from fantraxapi import api as fantrax_api_module
from fantraxapi.objs.player import LivePlayer

# Discord
import discord
from discord.ext import commands, tasks
from discord import app_commands


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

# Shares the "espn" channel with weeklyrecap.py/powerrankings.py — all
# periodic coverage/media content, as opposed to #news's real-time
# transaction/trade events.
TOP_PERFORMER_CHANNEL_ID = config.espnChannelId

# New, unverified-against-real-data feature (see CLAUDE.md) — starts
# cautious. Flip once you've watched it run for real for a bit; nothing
# can post for real before 2026-10-20 regardless (see _find_top_performer
# — DateNotInSeason is unconditionally raised until real scoring dates
# exist), so this only matters once the season is actually live.
DRY_RUN = True

_TZ = ZoneInfo("America/Los_Angeles")
# 8am, deliberately ahead of the 9am weekly recap (Monday) / power
# rankings (Wednesday) slot so a day that has all three doesn't post them
# back-to-back-to-back at the exact same minute.
TOP_PERFORMER_POST_TIME = datetime.time(hour=8, minute=0, tzinfo=_TZ)


class DailyTopPerformer(commands.Cog):
    def __init__(self, bot):
        print("Init Function of DailyTopPerformer Cog")
        self.bot = bot
        self.api = FantraxAPI(config.leagueId)
        self.check_top_performer.start()

    def cog_unload(self):
        self.check_top_performer.cancel()

    # ── core ─────────────────────────────────────────────────────────
    def _find_top_performer(self, scoring_date: datetime.date):
        """Returns (player, team, points) for the single highest real
        fantasy-point performance league-wide on scoring_date, or None
        if there's nothing to report (scoring_date isn't a valid date
        for this league yet — always true pre-season, since
        api.scoring_dates is empty until the season starts, same root
        cause as gotcha #12 — or no one scored at all that day).

        Considers EVERY rostered player regardless of active/reserve
        slot — this is "who had the best fantasy day in the league",
        not "whose manager actually benefited from starting them". The
        latter (crediting only active-slot performances, or grading a
        lineup decision against the hindsight-optimal one) is a
        different, deliberately deferred feature — see CLAUDE.md.

        Bypasses League.live_scores() deliberately: that wrapper only
        extracts the "ACTIVE" status bucket from the raw response
        (confirmed by reading fantraxapi/objs/league.py directly),
        silently dropping reserve/bench performances — which would have
        made this active-slot-only despite the "any rostered player"
        requirement above. Parses every status bucket in the raw
        response ourselves instead, same bypass pattern
        fetch_team_period_data()/tradestracker.py already use elsewhere
        in this codebase for other wrapper limitations."""
        if scoring_date not in self.api.scoring_dates.values():
            return None
        response = fantrax_api_module.get_live_scoring_stats(self.api, scoring_date=scoring_date)

        scorer_map = {}
        for _, data in response["scorerMap"].items():
            for _, data2 in data.items():
                for _, data3 in data2.items():
                    for entry in data3:
                        scorer_id = entry["scorer"]["scorerId"]
                        if scorer_id not in scorer_map:
                            scorer_map[scorer_id] = entry["scorer"]

        active_teams = set()
        for matchup in response["matchups"]:
            team1, team2 = matchup.split("_")
            active_teams.add(team1)
            active_teams.add(team2)

        best = None  # (scorer_id, team_id, points)
        for team_id, data in response["statsPerTeam"]["allTeamsStats"].items():
            if team_id not in active_teams:
                continue
            for _status, bucket in data.items():
                for scorer_id, pts in bucket.get("statsMap", {}).items():
                    if scorer_id.startswith("_") or scorer_id not in scorer_map:
                        continue
                    points = pts["object1"]
                    if best is None or points > best[2]:
                        best = (scorer_id, team_id, points)

        if best is None:
            return None
        scorer_id, team_id, points = best
        player = LivePlayer(self.api, scorer_map[scorer_id], team_id, points, scoring_date)
        return player, player.team, points

    def _build_embed(self, player, team, points: float, scoring_date: datetime.date) -> discord.Embed:
        description = (
            f"**{player.name}** ({team.name}) put up **{points:.1f}** fantasy points "
            f"on {scoring_date.strftime('%A, %B %d')} — the best individual performance "
            f"in the league that day."
        )
        embed = discord.Embed(title="🔥 Top Performer", color=discord.Color.orange(), description=description)
        # Bot's own avatar as the branding icon, matching every other
        # tracker's embeds — not the big conch image from /askshams,
        # which isn't sized for this.
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _check_and_post(self):
        yesterday = datetime.datetime.now(_TZ).date() - datetime.timedelta(days=1)
        result = self._find_top_performer(yesterday)
        if result is None:
            return  # nothing to report yet
        player, team, points = result

        if DRY_RUN:
            print(f"[DailyTopPerformer DRY RUN] Would post: {player.name} ({team.name}) — {points:.1f} pts on {yesterday}")
            return

        channel = self.bot.get_channel(TOP_PERFORMER_CHANNEL_ID)
        if channel is None:
            print(f"[DailyTopPerformer] Channel {TOP_PERFORMER_CHANNEL_ID} not found.")
            return

        await channel.send(embed=self._build_embed(player, team, points, yesterday))

    # ── background loop ─────────────────────────────────────────────
    @tasks.loop(time=TOP_PERFORMER_POST_TIME)
    async def check_top_performer(self):
        await self._check_and_post()

    @check_top_performer.before_loop
    async def before_check_top_performer(self):
        await self.bot.wait_until_ready()

    # ── synthetic test data ──────────────────────────────────────────
    # Real team object, made-up player/points — lets /topperformerdebug
    # exercise the full embed-rendering pipeline pre-season, when
    # live_scores() always raises DateNotInSeason (no scoring_dates exist
    # yet — see _find_top_performer's docstring).
    def _synthetic_top_performer(self):
        team = list(self.api.teams)[0]

        class _FakeLivePlayer:
            def __init__(self, name, points, team):
                self.name = name
                self.points = points
                self.team = team

        player = _FakeLivePlayer("Shai Gilgeous-Alexander", 78.4, team)
        return player, team, player.points

    # Commands
    @app_commands.command(name='topperformerdebug', description='(Debug) Preview the daily top-performer post')
    @app_commands.describe(synthetic="Use made-up placeholder data instead of real data (useful pre-season)")
    @app_commands.default_permissions(manage_guild=True)
    async def topPerformerDebug(self, interaction: discord.Interaction, synthetic: bool = False) -> None:
        await interaction.response.defer()

        yesterday = datetime.datetime.now(_TZ).date() - datetime.timedelta(days=1)
        if synthetic:
            player, team, points = self._synthetic_top_performer()
            scoring_date = yesterday
            header = "⚠️ SYNTHETIC TEST DATA — not a real performance.\n\n"
        else:
            result = self._find_top_performer(yesterday)
            if result is None:
                await interaction.followup.send(
                    "No top performer available yet — either the season hasn't started, "
                    "or no games were played yesterday. Run again with `synthetic: True` "
                    "to preview with placeholder data instead."
                )
                return
            player, team, points = result
            scoring_date = yesterday
            header = ""

        # Raw text dump first, matching every other debug command's
        # pattern — a quick diagnostic line before the actual embed.
        await interaction.followup.send(f"```\n{header}{player.name} ({team.name}) -- {points:.1f} pts on {scoring_date}\n```")

        embed = self._build_embed(player, team, points, scoring_date)
        if synthetic:
            embed.set_footer(text="⚠️ Synthetic test data — not a real performance.")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DailyTopPerformer(bot), guilds=[config.myGuild])
