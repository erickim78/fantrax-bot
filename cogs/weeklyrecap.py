# Dependencies
import datetime
from zoneinfo import ZoneInfo

# Files
import config

# Fantrax
from fantraxapi import FantraxAPI
from fantraxapi.objs.standings import Record

# Discord
import discord
from discord.ext import commands, tasks
from discord import app_commands


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────

RECAP_CHANNEL_ID = config.espnChannelId

DRY_RUN = False

# Fantrax's scoring periods in this league run Monday-Sunday (confirmed
# live via league.scoring_periods — even the longer All-Star-break period
# still starts on a Monday), so "yesterday was a Sunday that ended a
# period" only ever lines up on a Monday. Posting daily at this time and
# checking that condition (see _find_completed_period) means the Monday
# restriction falls out naturally — no separate day-of-week check needed.
RECAP_POST_TIME = datetime.time(hour=9, minute=0, tzinfo=ZoneInfo("America/Los_Angeles"))
_TZ = ZoneInfo("America/Los_Angeles")


def _week_label(scoring_period) -> str:
    return f"Playoff Week {scoring_period.period.number}" if scoring_period.playoffs else f"Week {scoring_period.period.number}"


def _team_label(team, records_by_team: dict) -> str:
    # Matchup.away/.home fall back to a raw display-name string (not a
    # Team object) when the team lookup fails, per fantraxapi's own
    # NotTeamInLeague fallback — handle both.
    team_id = getattr(team, "id", None)
    name = getattr(team, "name", None) or str(team)
    record = records_by_team.get(team_id) if team_id else None
    if record:
        return f"**{name}** ({record.win}-{record.loss})"
    return f"**{name}**"


class WeeklyRecap(commands.Cog):
    def __init__(self, bot):
        print("Init Function of WeeklyRecap Cog")
        self.bot = bot
        self.api = FantraxAPI(config.leagueId)
        self.check_recap.start()

    def cog_unload(self):
        self.check_recap.cancel()

    # ── data ─────────────────────────────────────────────────────────
    # fantraxapi's Standings.ranks is a dict[int, Record] keyed by rank
    # number — when multiple teams TIE at the same rank (guaranteed
    # pre-season when everyone's 0-0, but a real possibility mid-season
    # too), each team silently overwrites the previous one in that dict
    # slot, since dict keys can't hold duplicates. Confirmed live: with
    # all 10 teams tied at rank 1 pre-season, .ranks ended up with only
    # 1 entry. Rebuild the full list directly from the raw rows instead
    # of trusting .ranks, using the same Record construction the library
    # itself uses internally.
    def _all_records(self, standings) -> list:
        fields = {c["key"]: i for i, c in enumerate(standings._data["header"]["cells"])}
        return [
            Record(standings, row["fixedCells"][1]["teamId"], int(row["fixedCells"][0]["content"]), fields, row["cells"])
            for row in standings._data["rows"]
        ]

    def _record_lookup(self, standings) -> dict:
        return {r.team.id: r for r in self._all_records(standings)}

    def _find_completed_period(self):
        """Returns the ScoringPeriodResult whose range ended yesterday, or
        None if no period just wrapped up. NOTE: deliberately not using
        the library's own .complete/.current flags — they're computed as
        `now > end + 1 day`, which is still False on the Monday right
        after a Sunday-ending period (an off-by-one in the library, not
        this code). Matching .end == yesterday directly sidesteps that."""
        yesterday = datetime.datetime.now(_TZ).date() - datetime.timedelta(days=1)
        results = self.api.scoring_period_results(season=True, playoffs=True)
        return next((sp for sp in results.values() if sp.end == yesterday), None)

    def _next_period(self, completed_period):
        results = self.api.scoring_period_results(season=True, playoffs=True)
        return results.get(completed_period.period.number + 1)

    # ── formatting ──────────────────────────────────────────────────
    def format_recap_embed(self, period, records_by_team: dict) -> discord.Embed:
        embed = discord.Embed(title=f"📊 {_week_label(period)} Recap", color=discord.Color.blue())
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)

        lines = []
        for m in period.matchups:
            winner, winner_score, loser, loser_score = m.winner()
            if winner is None:
                lines.append(f"🏀 {_team_label(m.away, records_by_team)} tied {_team_label(m.home, records_by_team)} — {m.away_score} to {m.home_score}")
            else:
                lines.append(f"🏀 {_team_label(winner, records_by_team)} def. {_team_label(loser, records_by_team)} — {winner_score} to {loser_score}")
        embed.description = "\n".join(lines) if lines else "No matchups found for this period."
        return embed

    def format_standings_embed(self, standings) -> discord.Embed:
        embed = discord.Embed(title="🏆 Standings", color=discord.Color.gold())
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)

        records = sorted(self._all_records(standings), key=lambda r: (r.rank, r.team.name))
        lines = []
        for record in records:
            streak = f", {record.streak}" if record.streak else ""
            lines.append(f"{record.rank}. **{record.team.name}** — {record.win}-{record.loss}{streak}")
        embed.description = "\n".join(lines) if lines else "No standings data."
        return embed

    def format_next_week_embed(self, next_period, records_by_team: dict) -> discord.Embed:
        embed = discord.Embed(title=f"📅 {_week_label(next_period)} Matchups", color=discord.Color.purple())
        embed.set_author(name="Shams-kun", icon_url=self.bot.user.display_avatar.url)

        lines = [f"{_team_label(m.away, records_by_team)} vs {_team_label(m.home, records_by_team)}" for m in next_period.matchups]
        embed.description = "\n".join(lines) if lines else "Matchups not yet set."
        return embed

    # ── shared fetch/format/post logic ──────────────────────────────
    async def _check_and_post(self):
        completed = self._find_completed_period()
        if completed is None:
            return  # no period ended yesterday — nothing to report today

        standings = self.api.standings()
        records_by_team = self._record_lookup(standings)
        next_period = self._next_period(completed)

        if DRY_RUN:
            print(f"[WeeklyRecap DRY RUN] Would post recap for {_week_label(completed)}")
            return

        channel = self.bot.get_channel(RECAP_CHANNEL_ID)
        if channel is None:
            print(f"[WeeklyRecap] Channel {RECAP_CHANNEL_ID} not found.")
            return

        # Three separate posts (recap, then standings, then next week's
        # matchups) rather than one combined embed — keeps each piece
        # short and scannable, reads like distinct broadcast segments.
        await channel.send(embed=self.format_recap_embed(completed, records_by_team))
        await channel.send(embed=self.format_standings_embed(standings))
        if next_period:
            await channel.send(embed=self.format_next_week_embed(next_period, records_by_team))

    # ── background loop ─────────────────────────────────────────────
    @tasks.loop(time=RECAP_POST_TIME)
    async def check_recap(self):
        await self._check_and_post()

    @check_recap.before_loop
    async def before_check_recap(self):
        await self.bot.wait_until_ready()

    # Commands
    @app_commands.command(name='recapdebug', description='(Debug) Preview the weekly recap')
    @app_commands.default_permissions(manage_guild=True)
    async def recapDebug(self, interaction: discord.Interaction) -> None:
        # Try the exact same logic the real Monday trigger uses first —
        # if today genuinely is the day after a period ended, this IS
        # what the next scheduled post will show. Only fall back to a
        # looser preview otherwise (mid-week, or pre-season).
        period = self._find_completed_period()
        is_live_match = period is not None

        if period is None:
            results = self.api.scoring_period_results(season=True, playoffs=True)
            today = datetime.datetime.now(_TZ).date()
            # Strictly "ended before today" — NOT "not future", which
            # would also match the current in-progress period (its .end
            # always sorts later, so it'd always win over a truly
            # finished one). That was a real bug: running this mid-week
            # used to grab the ongoing, still-0-0 period instead of last
            # week's finished results.
            truly_completed = [sp for sp in results.values() if sp.end < today]
            period = max(truly_completed, key=lambda sp: sp.end) if truly_completed else min(results.values(), key=lambda sp: sp.start)

        standings = self.api.standings()
        records_by_team = self._record_lookup(standings)
        next_period = self._next_period(period)

        note = (
            "✅ This is exactly what the next scheduled Monday post will show."
            if is_live_match else
            "ℹ️ Preview only — today isn't the day after a period ended, so nothing will actually post right now. Showing the most relevant available period instead."
        )
        await interaction.response.send_message(note)
        await interaction.followup.send(embed=self.format_recap_embed(period, records_by_team))
        await interaction.followup.send(embed=self.format_standings_embed(standings))
        if next_period:
            await interaction.followup.send(embed=self.format_next_week_embed(next_period, records_by_team))


async def setup(bot):
    await bot.add_cog(WeeklyRecap(bot), guilds=[config.myGuild])
