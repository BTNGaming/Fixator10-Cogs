import math
import operator
import random
import time

import discord
from redbot.core import bank, commands
from redbot.core.utils import AsyncIter

from .abc import MixinMeta


class XP(MixinMeta):
    """XP/levels handling"""

    # calculates required exp for next level
    async def _required_exp(self, level: int):
        if level < 0:
            return 0
        return 139 * level + 65

    async def _level_exp(self, level: int):
        return level * 65 + 139 * level * (level - 1) // 2

    async def _find_level(self, total_exp):
        # this is specific to the function above
        return int((1 / 278) * (9 + math.sqrt(81 + 1112 * total_exp)))

    async def _give_chat_credit(self, user, server):
        msg_credits = await self.config.guild(server).msg_credits()
        if msg_credits and not await bank.is_global():
            await bank.deposit_credits(user, msg_credits)

    @commands.Cog.listener("on_message_without_command")
    async def _handle_on_message(self, message):
        if not self._db_ready:
            return
        server = message.guild
        user = message.author
        xp = await self.config.xp()
        # creates user if doesn't exist, bots are not logged.
        await self._create_user(user, server)
        curr_time = time.time()
        userinfo = await self.db.users.find_one({"user_id": str(user.id)})

        if not server or await self.config.guild(server).disabled():
            return
        if user.bot:
            return

        # check if chat_block exists
        if "chat_block" not in userinfo:
            userinfo["chat_block"] = 0

        if "last_message" not in userinfo:
            userinfo["last_message"] = 0
        if all(
            [
                float(curr_time) - float(userinfo["chat_block"]) >= 120,
                len(message.content) > await self.config.message_length() or message.attachments,
                message.content != userinfo["last_message"],
                message.channel.id not in await self.config.guild(server).ignored_channels(),
            ]
        ):
            await self._process_exp(message, userinfo, random.randint(xp[0], xp[1]))
            await self._give_chat_credit(user, server)

    async def _process_exp(self, message, userinfo, exp: int):
        server = message.guild
        channel = message.channel
        user = message.author
        # add to total exp
        required = await self._required_exp(userinfo["servers"][str(server.id)]["level"])
        try:
            await self.db.users.update_one(
                {"user_id": str(user.id)}, {"$set": {"total_exp": userinfo["total_exp"] + exp}},
            )
            self.bot.dispatch("leveler_process_exp", message, exp)
        except Exception as exc:
            self.log.error(f"Unable to process xp for {user.id}: {exc}")
        if userinfo["servers"][str(server.id)]["current_exp"] + exp >= required:
            userinfo["servers"][str(server.id)]["level"] += 1
            await self.db.users.update_one(
                {"user_id": str(user.id)},
                {
                    "$set": {
                        "servers.{}.level".format(server.id): userinfo["servers"][str(server.id)][
                            "level"
                        ],
                        "servers.{}.current_exp".format(server.id): userinfo["servers"][
                            str(server.id)
                        ]["current_exp"]
                        + exp
                        - required,
                        "chat_block": time.time(),
                        "last_message": message.content,
                    }
                },
            )
            await self._handle_levelup(user, userinfo, server, channel)
        else:
            await self.db.users.update_one(
                {"user_id": str(user.id)},
                {
                    "$set": {
                        "servers.{}.current_exp".format(server.id): userinfo["servers"][
                            str(server.id)
                        ]["current_exp"]
                        + exp,
                        "chat_block": time.time(),
                        "last_message": message.content,
                    }
                },
            )

    async def _handle_levelup(self, user, userinfo, server, channel):
        # channel lock implementation
        channel_id = await self.config.guild(server).lvl_msg_lock()
        if channel_id:
            channel = discord.utils.find(lambda m: m.id == channel_id, server.channels)

        server_identifier = ""  # super hacky
        name = await self._is_mention(user)  # also super hacky
        # private message takes precedent, of course
        if await self.config.guild(server).private_lvl_message():
            server_identifier = f" on {server.name}"
            channel = user
            name = "You"

        new_level = str(userinfo["servers"][str(server.id)]["level"])
        self.bot.dispatch("leveler_levelup", user, new_level)
        # add to appropriate role if necessary
        # try:
        server_roles = await self.db.roles.find_one({"server_id": str(server.id)})
        if server_roles is not None:
            for role in server_roles["roles"].keys():
                if int(server_roles["roles"][role]["level"]) == int(new_level):
                    add_role = discord.utils.get(server.roles, name=role)
                    if add_role is not None:
                        try:
                            await user.add_roles(add_role, reason="Levelup")
                        except discord.Forbidden:
                            await channel.send("Levelup role adding failed: Missing Permissions")
                        except discord.HTTPException:
                            await channel.send("Levelup role adding failed")
                    remove_role = discord.utils.get(
                        server.roles, name=server_roles["roles"][role]["remove_role"]
                    )
                    if remove_role is not None:
                        try:
                            await user.remove_roles(remove_role, reason="Levelup")
                        except discord.Forbidden:
                            await channel.send("Levelup role removal failed: Missing Permissions")
                        except discord.HTTPException:
                            await channel.send("Levelup role removal failed")
        try:
            server_linked_badges = await self.db.badgelinks.find_one({"server_id": str(server.id)})
            if server_linked_badges is not None:
                for badge_name in server_linked_badges["badges"]:
                    if int(server_linked_badges["badges"][badge_name]) == int(new_level):
                        server_badges = await self.db.badges.find_one(
                            {"server_id": str(server.id)}
                        )
                        if (
                            server_badges is not None
                            and badge_name in server_badges["badges"].keys()
                        ):
                            userinfo_db = await self.db.users.find_one({"user_id": str(user.id)})
                            new_badge_name = "{}_{}".format(badge_name, server.id)
                            userinfo_db["badges"][new_badge_name] = server_badges["badges"][
                                badge_name
                            ]
                            await self.db.users.update_one(
                                {"user_id": str(user.id)},
                                {"$set": {"badges": userinfo_db["badges"]}},
                            )
        except Exception as exc:
            await channel.send(f"Error. Badge was not given: {exc}")

        if await self.config.guild(server).lvl_msg():  # if lvl msg is enabled
            if await self.config.guild(server).text_only():
                async with channel.typing():
                    em = discord.Embed(
                        description="**{} just gained a level{}! (LEVEL {})**".format(
                            name, server_identifier, new_level
                        ),
                        colour=user.colour,
                    )
                    await channel.send(embed=em)
            else:
                async with channel.typing():
                    levelup = await self.draw_levelup(user, server)
                    file = discord.File(levelup, filename="levelup.png")
                    await channel.send(
                        "**{} just gained a level{}!**".format(name, server_identifier), file=file,
                    )

    async def _find_server_rank(self, user, server):
        targetid = str(user.id)
        users = []

        async for userinfo in self.db.users.find({}):
            try:
                server_exp = 0
                userid = userinfo["user_id"]
                for i in range(userinfo["servers"][str(server.id)]["level"]):
                    server_exp += await self._required_exp(i)
                server_exp += userinfo["servers"][str(server.id)]["current_exp"]
                users.append((userid, server_exp))
            except KeyError:
                pass

        sorted_list = sorted(users, key=operator.itemgetter(1), reverse=True)

        rank = 1
        async for a_user in AsyncIter(sorted_list):
            if a_user[0] == targetid:
                return rank
            rank += 1

    async def _find_server_rep_rank(self, user, server):
        targetid = str(user.id)
        users = []
        async for userinfo in self.db.users.find({}):
            if "servers" in userinfo and str(server.id) in userinfo["servers"]:
                users.append((userinfo["user_id"], userinfo["rep"]))

        sorted_list = sorted(users, key=operator.itemgetter(1), reverse=True)

        rank = 1
        async for a_user in AsyncIter(sorted_list):
            if a_user[0] == targetid:
                return rank
            rank += 1

    async def _find_server_exp(self, user, server):
        server_exp = 0
        userinfo = await self.db.users.find_one({"user_id": str(user.id)})

        try:
            for i in range(userinfo["servers"][str(server.id)]["level"]):
                server_exp += await self._required_exp(i)
            server_exp += userinfo["servers"][str(server.id)]["current_exp"]
            return server_exp
        except KeyError:
            return server_exp

    async def _find_global_rank(self, user):
        users = []

        async for userinfo in self.db.users.find({}):
            try:
                userid = userinfo["user_id"]
                users.append((userid, userinfo["total_exp"]))
            except KeyError:
                pass
        sorted_list = sorted(users, key=operator.itemgetter(1), reverse=True)

        rank = 1
        async for stats in AsyncIter(sorted_list):
            if stats[0] == str(user.id):
                return rank
            rank += 1

    async def _find_global_rep_rank(self, user):
        users = []

        async for userinfo in self.db.users.find({}):
            try:
                userid = userinfo["user_id"]
                users.append((userid, userinfo["rep"]))
            except KeyError:
                pass
        sorted_list = sorted(users, key=operator.itemgetter(1), reverse=True)

        rank = 1
        async for stats in AsyncIter(sorted_list):
            if stats[0] == str(user.id):
                return rank
            rank += 1
