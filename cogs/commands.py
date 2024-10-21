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
        self.api = FantraxAPI()


    # Commands 
    # Magic Conch Filler Command
    @app_commands.command(name='conch', description='Ask the Magic Conch for an answer.')
    async def conch(self, interaction: discord.Interaction, question: str) -> None:
        rand = random.randint(0,17)
    
        responses = config.responses

        # Send Embed with Reply
        imgURL = "https://i.imgur.com/RLsojmN.jpg"
        embed=discord.Embed(color=0xf1d3ed)
        embed.set_image( url = imgURL )
        embed.add_field(name="Magic Conch", value=question, inline=False)
        embed.add_field(name=responses[rand], value='\u200b', inline=False)
        await interaction.response.send_message(embed=embed)


    @app_commands.command(name='standings',description='Display Current Standings')
    async def standings(self, interaction: discord.Interaction) -> None:
        return


async def setup(bot):
    await bot.add_cog(Commands(bot))
