# Generic dependencies
import os

# Files
import config

# Discord dependencies
import discord
from discord.ext import commands
from discord import app_commands


class MyBot(commands.Bot):
    #Init
    def __init__(self):
        return



    # Setup hook
    async def setup_hook(self):
        return
        # None yet
        # await load_extensions()


    # On ready
    async def on_ready(self):
        print(f'Logged in as {self.user}')



# execute
if __name__ == '__main__':
    bot = MyBot()
    bot.run(config.botToken)

