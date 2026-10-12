"""Offline checks for join roles; no Discord login or real role changes."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot import VoiceLevelBot
from config import ConfigError, load_settings, role_changes
from storage import Store


def settings_env(**overrides):
    return {
        "DISCORD_TOKEN": "test.bot.token",
        "GUILD_ID": "1",
        "LEVEL_COUNT": "1",
        "LEVEL_1_ROLE_ID": "101",
        "LEVEL_1_HOURS": "1",
        "WELCOME_ENABLED": "false",
        "AUTO_ROLE_ID": "200",
        **overrides,
    }


class AutoRoleConfigTests(unittest.TestCase):
    def test_existing_installation_without_auto_role_still_loads(self):
        env = settings_env()
        del env["AUTO_ROLE_ID"]
        self.assertIsNone(load_settings(env).auto_role_id)
        self.assertIsNone(load_settings(settings_env(AUTO_ROLE_ID="  ")).auto_role_id)

    def test_invalid_role_id_and_level_role_overlap_are_rejected(self):
        for invalid in ("Member", "0", "-1", str(2**64), "101"):
            with self.subTest(value=invalid):
                with self.assertRaises(ConfigError):
                    load_settings(settings_env(AUTO_ROLE_ID=invalid))

    def test_level_sync_keeps_the_join_role(self):
        settings = load_settings(settings_env(AUTO_ROLE_ID=" 200 "))
        self.assertEqual(settings.auto_role_id, 200)
        for total in (0, 7200):
            with self.subTest(seconds=total):
                add, remove, _ = role_changes(total, settings.levels, settings.role_mode,
                                              {1, 101, 200, 201})
                self.assertNotIn(200, add | remove)
                self.assertNotIn(201, add | remove)


class AutoRoleJoinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "voice_levels.sqlite3")
        self.client = VoiceLevelBot(load_settings(settings_env()), self.store)
        self.client.welcome_member = AsyncMock(return_value=True)
        self.guild = SimpleNamespace(id=1, name="Testserver")

        def role(role_id, position, managed=False):
            return discord.Role(guild=self.guild, state=None, data={
                "id": str(role_id), "name": f"Role {role_id}", "position": position,
                "permissions": "0", "color": 0, "hoist": False,
                "managed": managed, "mentionable": False,
            })

        self.everyone = role(1, 0)
        self.auto_role = role(200, 1)
        self.other_role = role(201, 2)
        self.bot_role = role(300, 10)
        self.roles = {r.id: r for r in (self.everyone, self.auto_role, self.other_role, self.bot_role)}
        self.guild.get_role = self.roles.get
        self.guild.me = SimpleNamespace(top_role=self.bot_role,
                                        guild_permissions=discord.Permissions(manage_roles=True))
        self.member = SimpleNamespace(id=500, guild=self.guild, bot=False,
                                      roles=[self.everyone, self.other_role], add_roles=AsyncMock())

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_join_assigns_only_the_configured_role_before_welcome(self):
        order = []
        self.member.add_roles.side_effect = lambda *args, **kwargs: order.append("role")
        self.client.welcome_member.side_effect = lambda *args: order.append("welcome")
        await self.client.on_member_join(self.member)
        self.assertEqual(order, ["role", "welcome"])
        call = self.member.add_roles.await_args
        self.assertEqual(call.args, (self.auto_role,))
        self.assertIs(call.kwargs["atomic"], True)
        self.assertEqual([r.id for r in self.member.roles], [1, 201])
        self.assertIn(self.member.id, self.store.user_ids(self.guild.id))

    async def test_existing_role_does_not_trigger_another_assignment(self):
        self.member.roles.append(self.auto_role)
        await self.client.on_member_join(self.member)
        self.member.add_roles.assert_not_awaited()
        self.client.welcome_member.assert_awaited_once_with(self.member)

    async def test_role_is_assigned_even_if_the_welcome_fails(self):
        self.client.welcome_member.side_effect = RuntimeError("Welcome image unavailable")
        with self.assertRaises(RuntimeError):
            await self.client.on_member_join(self.member)
        self.member.add_roles.assert_awaited_once()

    async def test_discord_role_errors_do_not_block_the_welcome(self):
        for exception, status in ((discord.Forbidden, 403), (discord.NotFound, 404),
                                  (discord.HTTPException, 503)):
            with self.subTest(status=status):
                self.member.add_roles.reset_mock()
                self.client.welcome_member.reset_mock()
                self.member.add_roles.side_effect = exception(
                    SimpleNamespace(status=status, reason="Mock response"), "Role request failed")
                await self.client.on_member_join(self.member)
                self.client.welcome_member.assert_awaited_once_with(self.member)

    async def test_unassignable_roles_and_permissions_do_not_send_role_requests(self):
        configurations = (
            ("missing", replace(self.client.settings, auto_role_id=999)),
            ("everyone", replace(self.client.settings, auto_role_id=1)),
            ("same bot role", replace(self.client.settings, auto_role_id=300)),
            ("managed", replace(self.client.settings, auto_role_id=200)),
            ("no permission", replace(self.client.settings, auto_role_id=200)),
        )
        for name, settings in configurations:
            with self.subTest(configuration=name):
                self.client.settings = settings
                self.auto_role.managed = name == "managed"
                self.guild.me.guild_permissions.manage_roles = name != "no permission"
                self.client.welcome_member.reset_mock()
                await self.client.on_member_join(self.member)
                self.member.add_roles.assert_not_awaited()
                self.client.welcome_member.assert_awaited_once_with(self.member)

    async def test_bots_do_not_receive_the_member_role(self):
        self.member.bot = True
        await self.client.on_member_join(self.member)
        self.member.add_roles.assert_not_awaited()
        self.client.welcome_member.assert_awaited_once_with(self.member)
        self.assertNotIn(self.member.id, self.store.user_ids(self.guild.id))

    async def test_disabled_auto_role_preserves_the_welcome(self):
        self.client.settings = replace(self.client.settings, auto_role_id=None)
        await self.client.on_member_join(self.member)
        self.member.add_roles.assert_not_awaited()
        self.client.welcome_member.assert_awaited_once_with(self.member)

    async def test_other_servers_and_shutdown_are_ignored(self):
        self.member.guild = SimpleNamespace(id=2)
        await self.client.on_member_join(self.member)
        self.member.guild = self.guild
        await self.client.close()
        await self.client.on_member_join(self.member)
        self.member.add_roles.assert_not_awaited()
        self.client.welcome_member.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
