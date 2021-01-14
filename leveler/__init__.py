from .levelers import Levelers


async def setup(bot):
    cog = Levelers(bot)
    bot.add_cog(cog)
    await cog.initialize()
