# Dependencies
import random

# Files
import config

# Fantrax
from fantraxapi import FantraxAPI

# Discord
import discord
from discord.ext import commands
from discord import app_commands

class Commands(commands.Cog):
    def __init__(self, bot):
        print("Init Function of Commands Cog")
        self.bot = bot

        # Init Fantrax api instance
        self.api = FantraxAPI(config.leagueId)


    # Commands 
    # Magic Conch Filler Command
    @app_commands.command(name='askshams', description='Ask Shams a question and he will provide his wisdom.')
    async def askShams(self, interaction: discord.Interaction, question: str) -> None:
        rand = random.randint(0,17)
        imgURL = "https://i.imgur.com/ZC44rDY.jpeg"
        embed=discord.Embed(color=0xf1d3ed)
        embed.set_image( url = imgURL )
        embed.add_field(name="You have asked Shams-kun:", value=question, inline=False)
        embed.add_field(name=config.conchResponses[rand], value='\u200b', inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Commands(bot), guilds=[config.myGuild])
