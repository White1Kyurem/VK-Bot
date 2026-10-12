"""Discord voice-time level roles. Run with: python bot.py"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from config import (ConfigError, Settings, achieved_level, load_settings, role_changes,
                    normalize_discord_token, token_fingerprint)
from storage import Store
from tracking import Tracker, VoiceParticipant, eligible_users
from welcomer import send_welcome, welcome_problems

log = logging.getLogger("voice-level-bot")
PURPLE = discord.Colour(0x8B5CF6)
START_VERSION = "2026-10-12-english-auto-role"


def log_token_check(raw: str, token: str) -> None:
    log.info("Build: %s | DISCORD_TOKEN: %s characters | Token fingerprint: %s",
             START_VERSION, len(token), token_fingerprint(token))
    if raw.strip() != token:
        log.warning("Token formatting was cleaned up automatically "
                    "(for example, quotes, a Bot prefix, or whitespace).")
    if token.count(".") != 2:
        log.warning("Unusual token format. Use the token from the Bot page in the Developer Portal. "
                    "Do not use the Client Secret or Public Key as DISCORD_TOKEN.")


async def check_token(token: str) -> None:
    """A REST login check; no Gateway, guild commands, or database are involved."""
    client = discord.Client(intents=discord.Intents.none())
    async with client:
        await client.login(token)
        log.info("Token accepted by Discord: Bot=%s | Bot ID=%s | Application ID=%s",
                 client.user, client.user.id, client.application_id)


def duration(seconds: float) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours} h {minutes:02d} min {secs:02d} s"


class LevelCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client: VoiceLevelBot = self.client
        if interaction.guild_id != client.settings.guild_id:
            await interaction.response.send_message("This bot is configured for a different server.",
                                                    ephemeral=True)
            return False
        if not client.online:
            await interaction.response.send_message("The bot is reconnecting. "
                                                    "Please try again shortly.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction,
                       error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, sqlite3.Error):
            await self.client.fail(original)
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need the Manage Server permission to use this command."
        else:
            log.error("Slash command failed", exc_info=(type(error), error, error.__traceback__))
            message = "The command failed. Details are available in the Railway logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class VoiceLevelBot(discord.Client):
    def __init__(self, settings: Settings, store: Store):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.members = True
        super().__init__(intents=intents, chunk_guilds_at_startup=False,
                         allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store
        self.tracker = Tracker(store, settings.guild_id)
        self.tree = LevelCommandTree(self)
        self.online = False
        self.gateway_connected = False
        self.online_event = asyncio.Event()
        self.closing = False
        self.fatal_error: Exception | None = None
        self.background: list[asyncio.Task] = []
        self.role_queue: asyncio.Queue[int] = asyncio.Queue()
        self.queued: set[int] = set()
        self.last_problems: tuple[str, ...] | None = None
        self.last_full_sync = time.monotonic()
        self.welcome_slots = asyncio.Semaphore(2)
        self.add_commands()

    def target_guild(self) -> discord.Guild | None:
        guild = self.get_guild(self.settings.guild_id)
        return guild if guild and not guild.unavailable else None

    def eligible_snapshot(self, guild: discord.Guild) -> set[int]:
        people = []
        # The public VoiceChannel.voice_states API also covers quiet/hidden users.
        # Stage audiences are not ordinary voice calls and are not counted.
        for channel in guild.voice_channels:
            for user_id, state in channel.voice_states.items():
                member = guild.get_member(user_id)
                if member is not None:
                    people.append(VoiceParticipant(user_id, channel.id, member.bot,
                                                   state.deaf or state.self_deaf))
        return eligible_users(people, self.settings, guild.afk_channel.id if guild.afk_channel else None)

    def enqueue(self, user_id: int) -> None:
        if user_id not in self.queued and not self.closing:
            self.queued.add(user_id)
            self.role_queue.put_nowait(user_id)

    def enqueue_all(self) -> None:
        users = self.store.user_ids(self.settings.guild_id)
        guild = self.target_guild()
        if guild:
            role_ids = {level.role_id for level in self.settings.levels}
            users.update(member.id for member in guild.members if not member.bot
                         and any(role.id in role_ids for role in member.roles))
        for user_id in users:
            self.enqueue(user_id)

    def refresh_tracking(self) -> None:
        if self.online and (guild := self.target_guild()):
            for user_id in self.tracker.refresh(self.eligible_snapshot(guild)):
                self.enqueue(user_id)

    def pause_tracking(self) -> None:
        self.online = False
        self.online_event.clear()
        self.tracker.abandon_unconfirmed()

    def resume_tracking(self) -> None:
        if self.closing or self.target_guild() is None:
            return
        self.online = True
        self.online_event.set()
        self.refresh_tracking()
        self.enqueue_all()

    def role_problems(self, guild: discord.Guild) -> list[str]:
        problems = []
        me = guild.me
        if me is None:
            return ["The bot's server member could not be loaded."]
        if not me.guild_permissions.manage_roles:
            problems.append("The bot is missing the Manage Roles permission.")
        for level in self.settings.levels:
            role = guild.get_role(level.role_id)
            if role is None:
                problems.append(f"Level {level.number}: Role ID {level.role_id} does not exist.")
            elif role.is_default() or role.managed:
                problems.append(f"Level {level.number}: @everyone and managed roles are not allowed.")
            elif role >= me.top_role:
                problems.append(f"Level {level.number}: The bot's highest role must be above '{role.name}'.")
        return problems

    def report_problems(self, guild: discord.Guild) -> list[str]:
        problems = self.role_problems(guild)
        signature = tuple(problems)
        if signature != self.last_problems:
            self.last_problems = signature
            for problem in problems:
                log.warning(problem)
            if not problems:
                log.info("All %s level roles exist and can be managed.", len(self.settings.levels))
        return problems

    def auto_role_problems(self, guild: discord.Guild) -> list[str]:
        role_id = self.settings.auto_role_id
        if role_id is None:
            return []
        role = guild.get_role(role_id)
        if role is None:
            return [f"AUTO_ROLE_ID: Role {role_id} does not exist on this server."]
        if role.is_default() or role.managed:
            return ["AUTO_ROLE_ID: @everyone and managed roles cannot be assigned."]
        me = guild.me
        if me is None:
            return ["Join role: The bot's server member could not be loaded."]
        problems = []
        if not me.guild_permissions.manage_roles:
            problems.append("Join role: The bot is missing the Manage Roles permission.")
        if role >= me.top_role:
            problems.append(f"Join role: The bot's highest role must be above '{role.name}'.")
        return problems

    async def assign_auto_role(self, member: discord.Member) -> bool:
        if (self.settings.auto_role_id is None or member.bot or self.closing
                or member.guild.id != self.settings.guild_id):
            return False
        if problems := self.auto_role_problems(member.guild):
            for problem in problems:
                log.warning("%s | User ID=%s", problem, member.id)
            return False
        role = member.guild.get_role(self.settings.auto_role_id)
        if role is None:
            return False  # The role may have been deleted during a guild update.
        if any(current.id == role.id for current in member.roles):
            return True
        try:
            await member.add_roles(role, reason="Voice Level Bot: automatic role on server join",
                                   atomic=True)
        except discord.NotFound:
            log.info("Join role not assigned: The member or role no longer exists. "
                     "User ID=%s | Role ID=%s", member.id, role.id)
            return False
        except discord.Forbidden:
            log.warning("Join role assignment denied. Check Manage Roles and the role hierarchy. "
                        "User ID=%s | Role ID=%s", member.id, role.id)
            return False
        except discord.HTTPException as error:
            log.warning("Join role could not be assigned: %s | User ID=%s | Role ID=%s",
                        error, member.id, role.id)
            return False
        log.info("Join role assigned: %s | User ID=%s | Role ID=%s", role.name, member.id, role.id)
        return True

    async def setup_hook(self) -> None:
        # REST login succeeded here, even if Discord subsequently rejects the Gateway intents.
        log.info("Discord login successful: Bot=%s | Bot ID=%s | Application ID=%s",
                 self.user, self.user.id, self.application_id)
        log.info("Intent settings for this bot: "
                 "https://discord.com/developers/applications/%s/bot", self.application_id)
        commands = await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))
        log.info("Registered %s slash commands for server %s.", len(commands), self.settings.guild_id)
        self.background = [
            asyncio.create_task(self.save_loop(), name="voice-checkpoints"),
            asyncio.create_task(self.role_worker(), name="level-roles"),
        ]

    async def on_ready(self) -> None:
        self.gateway_connected = True
        guild = self.target_guild()
        if guild is None:
            if self.get_guild(self.settings.guild_id) is not None:
                self.pause_tracking()
                return  # Discord will emit guild_available when its outage ends.
            await self.fail(RuntimeError("Check GUILD_ID: The bot cannot find the configured "
                                         "server, or the server is unavailable."))
            return
        self.resume_tracking()
        self.report_problems(guild)
        for problem in self.auto_role_problems(guild):
            log.warning(problem)
        if self.settings.auto_role_id is not None:
            log.info("Join role configured for new members: %s", self.settings.auto_role_id)
        for problem in welcome_problems(guild, self.settings.welcome):
            log.warning(problem)
        log.info("Bot online: %s | Server: %s | Active voice timers: %s",
                 self.user, guild.name, len(self.tracker.active))

    async def on_disconnect(self) -> None:
        self.gateway_connected = False
        if not self.closing:
            self.pause_tracking()
            log.warning("Discord connection lost. Voice time tracking is paused until reconnection.")

    async def on_resumed(self) -> None:
        self.gateway_connected = True
        self.resume_tracking()
        log.info("Discord connection restored; current voice sessions are being tracked again.")

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        if guild.id == self.settings.guild_id:
            self.pause_tracking()

    async def on_guild_available(self, guild: discord.Guild) -> None:
        if guild.id == self.settings.guild_id and self.gateway_connected and self.is_ready():
            self.resume_tracking()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if guild.id == self.settings.guild_id:
            self.pause_tracking()
            await self.fail(RuntimeError("The bot was removed from the configured server."))

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                     after: discord.VoiceState) -> None:
        if member.guild.id == self.settings.guild_id and not self.closing:
            # A join/leave also affects everybody else when min_humans > 1.
            self.refresh_tracking()
            if not member.bot and self.online:
                self.enqueue(member.id)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id == self.settings.guild_id and not self.closing:
            await self.assign_auto_role(member)
            if not member.bot:
                self.store.ensure_users(self.settings.guild_id, {member.id})
                self.enqueue(member.id)
            await self.welcome_member(member)

    async def welcome_member(self, member: discord.Member) -> bool:
        async with self.welcome_slots:
            if self.closing:
                return False
            return await send_welcome(member, self.settings)

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id == self.settings.guild_id and not self.closing:
            self.refresh_tracking()

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.guild.id != self.settings.guild_id or self.closing:
            return
        if before.roles != after.roles:
            if self.user and after.id == self.user.id:
                self.enqueue_all()
            elif not after.bot:
                configured = {level.role_id for level in self.settings.levels}
                old = {role.id for role in before.roles} & configured
                new = {role.id for role in after.roles} & configured
                if old != new:
                    self.enqueue(after.id)

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if after.id == self.settings.guild_id and not self.closing:
            self.refresh_tracking()

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.guild.id == self.settings.guild_id and not self.closing:
            self.refresh_tracking()

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if after.guild.id == self.settings.guild_id and not self.closing:
            self.enqueue_all()

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if role.guild.id == self.settings.guild_id and not self.closing:
            self.enqueue_all()

    async def on_guild_role_create(self, role: discord.Role) -> None:
        if role.guild.id == self.settings.guild_id and not self.closing:
            self.enqueue_all()

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        original = sys.exc_info()[1]
        log.exception("Error in event %s", event_method)
        if isinstance(original, sqlite3.Error):
            await self.fail(original)

    async def fail(self, error: Exception) -> None:
        self.fatal_error = error
        log.error("Bot is shutting down: %s", error)
        await self.close()

    async def save_loop(self) -> None:
        try:
            while not self.closing:
                await asyncio.sleep(self.settings.save_interval)
                if not self.online:
                    continue
                if self.target_guild() is None:
                    self.pause_tracking()
                    continue
                self.refresh_tracking()
                for user_id in self.tracker.checkpoint():
                    self.enqueue(user_id)
                if time.monotonic() - self.last_full_sync >= 300:
                    self.enqueue_all()
                    self.last_full_sync = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("Failed to save voice time.")
            await self.fail(error)

    async def role_worker(self) -> None:
        while not self.closing:
            user_id = await self.role_queue.get()
            try:
                await self.online_event.wait()
                self.queued.discard(user_id)
                await self.sync_member(user_id)
            except asyncio.CancelledError:
                raise
            except sqlite3.Error as error:
                await self.fail(error)
                return
            except Exception:
                # API failures are retried by the next voice event / periodic resync.
                log.exception("Role synchronization failed for user %s.", user_id)
            finally:
                self.role_queue.task_done()

    async def sync_member(self, user_id: int) -> None:
        guild = self.target_guild()
        if not self.online or guild is None or self.report_problems(guild):
            return
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return  # Keep past time if somebody leaves and later rejoins.
        if member.bot:
            return
        # Awards must be backed by saved time, including checks triggered by /rank.
        self.tracker.checkpoint({user_id})
        confirmed_total = self.store.total(guild.id, user_id)
        add, remove, reached = role_changes(confirmed_total, self.settings.levels,
                                            self.settings.role_mode, {role.id for role in member.roles})
        reason = "Voice Level Bot: role based on total voice time"
        # Add the new role first so a transient error never removes the old level early.
        if add:
            await member.add_roles(*(guild.get_role(role_id) for role_id in sorted(add)),
                                   reason=reason, atomic=True)
        if remove:
            await member.remove_roles(*(guild.get_role(role_id) for role_id in sorted(remove)),
                                      reason=reason, atomic=True)
        previous = self.store.last_awarded(guild.id, user_id)
        if reached > previous:
            # Persist before notifying: restarts do not re-announce old awards.
            self.store.mark_awarded(guild.id, user_id, reached)
            log.info("User %s reached Level %s (%s).", user_id, reached,
                     duration(self.tracker.total(user_id)))
            await self.announce(member, reached)

    async def announce(self, member: discord.Member, level_number: int) -> None:
        channel_id = self.settings.level_up_channel_id
        if channel_id is None or not self.online:
            return
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.warning("LEVEL_UP_CHANNEL_ID must point to a text channel on this server.")
            return
        role = member.guild.get_role(self.settings.levels[level_number - 1].role_id)
        embed = discord.Embed(title="New Voice Level!", colour=PURPLE,
                              description=f"{member.mention} reached **Level {level_number}**!\n"
                                          f"Role: {role.mention}\n"
                                          f"Total voice time: **{duration(self.tracker.total(member.id))}**")
        try:
            await channel.send(content=member.mention, embed=embed,
                               allowed_mentions=discord.AllowedMentions(users=[member],
                                                                        roles=False, everyone=False))
        except discord.HTTPException:
            log.warning("The level-up message could not be sent; the role was assigned.",
                        exc_info=True)

    def add_commands(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)

        @self.tree.command(name="rank", description="Show voice time, level, and the next goal.", guild=guild)
        @app_commands.guild_only()
        @app_commands.describe(member="Optional: Show another member's voice time.")
        async def rank(interaction: discord.Interaction, member: discord.Member | None = None):
            member = member or interaction.user
            if member.bot:
                await interaction.response.send_message("Bots do not earn voice time.", ephemeral=True)
                return
            total = self.tracker.total(member.id)
            reached = achieved_level(total, self.settings.levels)
            embed = discord.Embed(title=f"Voice Rank · {member.display_name}", colour=PURPLE)
            embed.add_field(name="Total Voice Time", value=duration(total), inline=False)
            role_text = self.settings.levels[reached - 1].role_id if reached else None
            embed.add_field(name="Current Level",
                            value=f"Level {reached}" + (f" · <@&{role_text}>" if role_text else ""))
            embed.add_field(name="Time Tracking",
                            value="Active" if member.id in self.tracker.active else "Paused")
            if reached < len(self.settings.levels):
                goal = self.settings.levels[reached]
                embed.add_field(name=f"Next: Level {goal.number}",
                                value=f"**{duration(goal.seconds - total)}** of voice time remaining", inline=False)
            else:
                embed.add_field(name="Progress", value="Highest configured level reached!", inline=False)
            embed.set_footer(text=f"Roles are checked every {self.settings.save_interval} seconds; "
                                  "Discord may delay role assignments.")
            self.enqueue(member.id)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="leaderboard", description="Top 10 members by total voice time.", guild=guild)
        @app_commands.guild_only()
        async def leaderboard(interaction: discord.Interaction):
            top = sorted(self.tracker.totals().items(), key=lambda row: (-row[1], row[0]))[:10]
            lines = [f"**{index}.** <@{user_id}> — {duration(total)} "
                     f"· Level {achieved_level(total, self.settings.levels)}"
                     for index, (user_id, total) in enumerate(top, start=1) if total > 0]
            embed = discord.Embed(title="Voice Leaderboard", colour=PURPLE,
                                  description="\n".join(lines) or "No voice time recorded yet.")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="levels", description="Show all level roles and required voice hours.", guild=guild)
        @app_commands.guild_only()
        async def levels(interaction: discord.Interaction):
            text = "\n".join(f"**Level {level.number}** · <@&{level.role_id}> · "
                             f"from **{level.hours:g} total hours**" for level in self.settings.levels)
            embed = discord.Embed(title="Voice Levels", description=text, colour=PURPLE)
            embed.set_footer(text="Each level requires a total amount of voice time, not extra hours per level.")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="botstatus", description="Admin: Check roles and voice time tracking.", guild=guild)
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def botstatus(interaction: discord.Interaction):
            problems = self.role_problems(interaction.guild)
            embed = discord.Embed(title="Voice Level Bot · Status", colour=PURPLE)
            embed.add_field(name="Roles", value="\n".join(problems)[:1024] or "All roles can be managed.",
                            inline=False)
            embed.add_field(name="Active Voice Timers", value=str(len(self.tracker.active)))
            embed.add_field(name="Role Mode", value=self.settings.role_mode)
            embed.add_field(name="Save Interval", value=f"{self.settings.save_interval} seconds")
            embed.add_field(name="Persistent Storage", value="Railway Volume" if self.settings.railway
                            else "Local Data Folder")
            embed.add_field(name="Minimum People per Channel", value=str(self.settings.min_humans))
            auto_role_status = "Disabled (AUTO_ROLE_ID is empty)"
            if self.settings.auto_role_id is not None:
                auto_role_status = "\n".join(self.auto_role_problems(interaction.guild)) or \
                                   f"Enabled: <@&{self.settings.auto_role_id}> on new server joins"
            embed.add_field(name="Join Role", value=auto_role_status[:1024], inline=False)
            welcome = self.settings.welcome
            welcome_status = "Disabled"
            if welcome.enabled:
                welcome_status = "\n".join(welcome_problems(interaction.guild, welcome)) or \
                                 f"Enabled in <#{welcome.channel_id}> · Unturned background"
            embed.add_field(name="Welcomer", value=welcome_status[:1024], inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="levels-sync", description="Admin: Sync roles with saved voice time.", guild=guild)
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def levels_sync(interaction: discord.Interaction):
            self.enqueue_all()
            await interaction.response.send_message("Role synchronization has started. "
                                                    "Use /botstatus to check for issues.", ephemeral=True)

        @self.tree.command(name="welcome-test", description="Admin: Send a test card in the welcome channel.", guild=guild)
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def welcome_test(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            sent = await self.welcome_member(interaction.user)
            message = "A welcome test was sent in the welcome channel." if sent else \
                      "The welcome message could not be sent. Check WELCOME_ENABLED, WELCOME_CHANNEL_ID " \
                      "and channel permissions using /botstatus."
            await interaction.followup.send(message, ephemeral=True)

    async def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        try:
            self.tracker.stop()
        except Exception as error:
            self.fatal_error = self.fatal_error or error
            log.exception("Failed to save the final voice times.")
        self.online = False
        self.online_event.clear()
        current = asyncio.current_task()
        other_tasks = [task for task in self.background if task is not current]
        for task in other_tasks:
            task.cancel()
        if other_tasks:
            await asyncio.gather(*other_tasks, return_exceptions=True)
        try:
            await super().close()
        finally:
            self.store.close()
            log.info("Bot stopped; confirmed voice times have been saved.")


async def run(settings: Settings) -> None:
    store = Store(settings.data_dir / "voice_levels.sqlite3")
    client = VoiceLevelBot(settings, store)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(client.close()))
        except (NotImplementedError, RuntimeError):
            pass  # Windows uses the normal KeyboardInterrupt path.
    try:
        async with client:
            await client.start(settings.token, reconnect=True)
    finally:
        await client.close()
    if client.fatal_error is not None:
        raise client.fatal_error


def main() -> int:
    parser = argparse.ArgumentParser(description="Discord Voice Level Bot")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check-config", action="store_true", help="Check variables without logging in to Discord")
    checks.add_argument("--check-token", action="store_true",
                        help="Check only the bot token, without a server connection or database")
    args = parser.parse_args()
    load_dotenv(override=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        raw_token = os.environ.get("DISCORD_TOKEN", "")
        token = normalize_discord_token(raw_token)
        log_token_check(raw_token, token)
        if args.check_token:
            asyncio.run(check_token(token))
            return 0
        settings = load_settings()
        if args.check_config:
            print(f"Configuration is valid: {len(settings.levels)} levels, "
                  f"mode {settings.role_mode}, saving every {settings.save_interval}s.")
            print("Discord IDs, role permissions, and the token are checked when logging in.")
            return 0
        asyncio.run(run(settings))
    except ConfigError as error:
        log.error("Configuration error: %s", error)
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error("Discord rejected the SERVER MEMBERS INTENT. Open the portal link logged above, "
                  "enable Server Members Intent on the Bot page for that exact application, "
                  "click Save Changes, and restart the Railway service. "
                  "The welcomer and join role both require this intent.")
        return 1
    except discord.LoginFailure as error:
        status = getattr(error.__cause__, "status", None)
        log.error("Discord login rejected%s. Check DISCORD_TOKEN in this Railway service "
                  "and deploy the variable changes.", f" (HTTP {status})" if status else "")
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception("The bot could not continue running.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
