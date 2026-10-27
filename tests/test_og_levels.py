"""Offline checks for eight continuous stages with OG names after Level 5."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot import VoiceLevelBot
from config import ConfigError, achieved_level, level_name, load_settings, role_changes
from storage import Store


HOURS = (1, 5, 15, 40, 100, 200, 350, 500)
NAMES = ("Level 1", "Level 2", "Level 3", "Level 4", "Level 5",
         "OG Level 1", "OG Level 2", "OG Level 3")


def eight_level_env():
    env = {"DISCORD_TOKEN": "test.bot.token", "GUILD_ID": "1", "LEVEL_COUNT": "8",
           "AUTO_ROLE_ID": "200", "WELCOME_ENABLED": "false"}
    for number, hours in enumerate(HOURS, start=1):
        env[f"LEVEL_{number}_ROLE_ID"] = str(100 + number)
        env[f"LEVEL_{number}_HOURS"] = str(hours)
    return env


class OGConfigurationTests(unittest.TestCase):
    def test_eight_stage_names_and_numbers(self):
        settings = load_settings(eight_level_env())
        self.assertEqual(tuple(level.number for level in settings.levels), tuple(range(1, 9)))
        self.assertEqual(tuple(level_name(level.number) for level in settings.levels), NAMES)
        self.assertEqual(level_name(0), "Level 0")

    def test_existing_five_level_setup_still_loads(self):
        env = eight_level_env()
        del env["LEVEL_COUNT"]
        for number in (6, 7, 8):
            del env[f"LEVEL_{number}_ROLE_ID"]
            del env[f"LEVEL_{number}_HOURS"]
        settings = load_settings(env)
        self.assertEqual(len(settings.levels), 5)
        self.assertEqual(tuple(level.hours for level in settings.levels), HOURS[:5])

    def test_og_hours_are_required_and_identified_by_display_name(self):
        for stage, name in ((6, "OG Level 1"), (7, "OG Level 2"), (8, "OG Level 3")):
            with self.subTest(stage=stage):
                env = eight_level_env()
                del env[f"LEVEL_{stage}_HOURS"]
                with self.assertRaises(ConfigError) as error:
                    load_settings(env)
                self.assertIn(f"LEVEL_{stage}_HOURS", str(error.exception))
                self.assertIn(name, str(error.exception))

    def test_custom_og_hours_and_exact_promotion_boundaries(self):
        env = eight_level_env()
        for stage, hours in ((6, 125), (7, 150), (8, 175)):
            env[f"LEVEL_{stage}_HOURS"] = str(hours)
        settings = load_settings(env)
        for stage, hours in ((6, 125), (7, 150), (8, 175)):
            with self.subTest(stage=stage):
                self.assertEqual(achieved_level(hours * 3600 - 1, settings.levels), stage - 1)
                self.assertEqual(achieved_level(hours * 3600, settings.levels), stage)
                add, remove, reached = role_changes(hours * 3600, settings.levels, "highest",
                                                    {100 + stage - 1, 200, 999})
                self.assertEqual((add, remove, reached), ({100 + stage}, {100 + stage - 1}, stage))

    def test_cumulative_mode_keeps_regular_and_join_roles(self):
        settings = load_settings(eight_level_env())
        regular_and_other = {101, 102, 103, 104, 105, 200, 999}
        add, remove, reached = role_changes(500 * 3600, settings.levels, "cumulative",
                                            regular_and_other)
        self.assertEqual((add, remove, reached), ({106, 107, 108}, set(), 8))


class OGDiscordOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "voice_levels.sqlite3")
        self.client = VoiceLevelBot(load_settings(eight_level_env()), self.store)
        self.guild = SimpleNamespace(id=1, name="Test Server")

        def role(role_id, position):
            return discord.Role(guild=self.guild, state=None, data={
                "id": str(role_id), "name": f"Role {role_id}", "position": position,
                "permissions": "0", "color": 0, "hoist": False,
                "managed": False, "mentionable": False,
            })

        self.roles = {1: role(1, 0), 200: role(200, 1), 300: role(300, 10)}
        self.roles.update({100 + stage: role(100 + stage, stage + 1) for stage in range(1, 9)})
        self.guild.get_role = self.roles.get
        self.guild.me = SimpleNamespace(top_role=self.roles[300],
                                        guild_permissions=discord.Permissions(manage_roles=True))
        self.member = SimpleNamespace(id=500, guild=self.guild, bot=False, display_name="Test Member",
                                      mention="<@500>", roles=[self.roles[1], self.roles[200]],
                                      add_roles=AsyncMock(), remove_roles=AsyncMock())
        self.guild.get_member = lambda user_id: self.member if user_id == self.member.id else None
        self.client.target_guild = lambda: self.guild
        self.client.online = True

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def command_embed(self, command_name):
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(user=self.member, guild=self.guild, response=response)
        command = self.client.tree.get_command(command_name, guild=discord.Object(id=self.guild.id))
        await command.callback(interaction)
        return response.send_message.await_args.kwargs["embed"]

    async def test_rank_crosses_all_og_tiers_and_stops_at_stage_eight(self):
        for stage, hours, current, next_name in (
                (5, 100, "Level 5", "Next: OG Level 1"),
                (6, 200, "OG Level 1", "Next: OG Level 2"),
                (7, 350, "OG Level 2", "Next: OG Level 3"),
                (8, 500, "OG Level 3", None)):
            with self.subTest(stage=stage):
                self.member.id = 500 + stage
                self.store.add_seconds(self.guild.id, {self.member.id: hours * 3600})
                embed = await self.command_embed("rank")
                fields = {field.name: field.value for field in embed.fields}
                self.assertEqual(fields["Current Level"], f"{current} · <@&{100 + stage}>")
                if next_name:
                    self.assertIn(next_name, fields)
                else:
                    self.assertEqual(fields["Progress"], "Highest configured level reached!")
                    self.assertFalse(any(name.startswith("Next:") for name in fields))

    async def test_levels_and_status_show_the_eight_level_setup(self):
        embed = await self.command_embed("levels")
        lines = embed.description.splitlines()
        self.assertEqual(len(lines), 8)
        for stage, (line, name, hours) in enumerate(zip(lines, NAMES, HOURS), start=1):
            self.assertIn(f"**{name}**", line)
            self.assertIn(f"<@&{100 + stage}>", line)
            self.assertIn(f"{hours} total hours", line)
        status = await self.command_embed("botstatus")
        fields = {field.name: field.value for field in status.fields}
        self.assertEqual(fields["Configured Levels"], "8")
        self.assertEqual(fields["Roles"], "All roles can be managed.")

    async def test_leaderboard_uses_og_names(self):
        self.store.add_seconds(self.guild.id, {501: 100 * 3600, 502: 200 * 3600,
                                               503: 350 * 3600, 504: 500 * 3600})
        lines = (await self.command_embed("leaderboard")).description.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].endswith("OG Level 3"))
        self.assertTrue(lines[1].endswith("OG Level 2"))
        self.assertTrue(lines[2].endswith("OG Level 1"))
        self.assertTrue(lines[3].endswith("Level 5"))

    async def test_saved_level_five_progress_promotes_without_resetting_time(self):
        self.member.roles.append(self.roles[105])
        self.store.add_seconds(self.guild.id, {self.member.id: 250 * 3600})
        self.store.mark_awarded(self.guild.id, self.member.id, 5)
        self.client.announce = AsyncMock()
        await self.client.sync_member(self.member.id)
        self.assertEqual(self.member.add_roles.await_args.args, (self.roles[106],))
        self.assertEqual(self.member.remove_roles.await_args.args, (self.roles[105],))
        self.assertEqual(self.store.total(self.guild.id, self.member.id), 250 * 3600)
        self.assertEqual(self.store.last_awarded(self.guild.id, self.member.id), 6)
        self.client.announce.assert_awaited_once_with(self.member, 6)

    async def test_level_up_announcement_uses_og_name(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        self.guild.get_channel = lambda channel_id: channel
        self.client.settings = replace(self.client.settings, level_up_channel_id=600)
        await self.client.announce(self.member, 6)
        embed = channel.send.await_args.kwargs["embed"]
        self.assertIn("reached **OG Level 1**!", embed.description)
        self.assertIn(self.roles[106].mention, embed.description)


if __name__ == "__main__":
    unittest.main()
