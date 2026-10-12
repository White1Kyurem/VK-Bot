"""Discord voice-time level roles. Run with: python bot.py"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sqlite3
import sys
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from config import ConfigError, Settings, achieved_level, load_settings, role_changes
from storage import Store
from tracking import Tracker, VoiceParticipant, eligible_users
from welcomer import send_welcome, welcome_problems

log = logging.getLogger("voice-level-bot")
PURPLE = discord.Colour(0x8B5CF6)


def duration(seconds: float) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours} h {minutes:02d} min {secs:02d} s"


class LevelCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        client: VoiceLevelBot = self.client
        if interaction.guild_id != client.settings.guild_id:
            await interaction.response.send_message("Dieser Bot ist für einen anderen Server "
                                                    "konfiguriert.", ephemeral=True)
            return False
        if not client.online:
            await interaction.response.send_message("Der Bot verbindet sich gerade neu. "
                                                    "Bitte gleich nochmals versuchen.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction,
                       error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, sqlite3.Error):
            await self.client.fail(original)
        if isinstance(error, app_commands.MissingPermissions):
            message = "Dafür brauchst du die Server-Berechtigung ‚Server verwalten‘."
        else:
            log.error("Slash-Command fehlgeschlagen", exc_info=(type(error), error, error.__traceback__))
            message = "Der Befehl ist fehlgeschlagen. Details stehen in den Railway-Logs."
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
            return ["Bot-Mitglied konnte nicht geladen werden."]
        if not me.guild_permissions.manage_roles:
            problems.append("Dem Bot fehlt die Berechtigung ‚Rollen verwalten‘.")
        for level in self.settings.levels:
            role = guild.get_role(level.role_id)
            if role is None:
                problems.append(f"Level {level.number}: Rollen-ID {level.role_id} existiert nicht.")
            elif role.is_default() or role.managed:
                problems.append(f"Level {level.number}: @everyone/Integrationsrollen sind nicht erlaubt.")
            elif role >= me.top_role:
                problems.append(f"Level {level.number}: Die Bot-Rolle muss über ‚{role.name}‘ stehen.")
        return problems

    def report_problems(self, guild: discord.Guild) -> list[str]:
        problems = self.role_problems(guild)
        signature = tuple(problems)
        if signature != self.last_problems:
            self.last_problems = signature
            for problem in problems:
                log.warning(problem)
            if not problems:
                log.info("Alle %s Level-Rollen sind vorhanden und verwaltbar.", len(self.settings.levels))
        return problems

    async def setup_hook(self) -> None:
        commands = await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))
        log.info("%s Slash-Commands für Server %s registriert.", len(commands), self.settings.guild_id)
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
            await self.fail(RuntimeError("GUILD_ID prüfen: Der Bot findet den konfigurierten "
                                         "Server nicht oder der Server ist nicht verfügbar."))
            return
        self.resume_tracking()
        self.report_problems(guild)
        for problem in welcome_problems(guild, self.settings.welcome):
            log.warning(problem)
        log.info("Bot online: %s | Server: %s | Aktive Zeitmessungen: %s",
                 self.user, guild.name, len(self.tracker.active))

    async def on_disconnect(self) -> None:
        self.gateway_connected = False
        if not self.closing:
            self.pause_tracking()
            log.warning("Discord-Verbindung unterbrochen. Call-Zeit pausiert bis zum Reconnect.")

    async def on_resumed(self) -> None:
        self.gateway_connected = True
        self.resume_tracking()
        log.info("Discord-Verbindung wiederhergestellt; aktuelle Calls werden neu erfasst.")

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        if guild.id == self.settings.guild_id:
            self.pause_tracking()

    async def on_guild_available(self, guild: discord.Guild) -> None:
        if guild.id == self.settings.guild_id and self.gateway_connected and self.is_ready():
            self.resume_tracking()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if guild.id == self.settings.guild_id:
            self.pause_tracking()
            await self.fail(RuntimeError("Der Bot wurde vom konfigurierten Server entfernt."))

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                     after: discord.VoiceState) -> None:
        if member.guild.id == self.settings.guild_id and not self.closing:
            # A join/leave also affects everybody else when min_humans > 1.
            self.refresh_tracking()
            if not member.bot and self.online:
                self.enqueue(member.id)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id == self.settings.guild_id and not self.closing:
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
        log.exception("Fehler im Event %s", event_method)
        if isinstance(original, sqlite3.Error):
            await self.fail(original)

    async def fail(self, error: Exception) -> None:
        self.fatal_error = error
        log.error("Bot wird beendet: %s", error)
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
            log.exception("Speichern der Call-Zeit fehlgeschlagen.")
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
                log.exception("Rollen-Synchronisierung für Nutzer %s fehlgeschlagen.", user_id)
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
        reason = "Voice-Level-Bot: Rolle anhand der gesamten Call-Zeit"
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
            log.info("Nutzer %s hat Level %s erreicht (%s).", user_id, reached,
                     duration(self.tracker.total(user_id)))
            await self.announce(member, reached)

    async def announce(self, member: discord.Member, level_number: int) -> None:
        channel_id = self.settings.level_up_channel_id
        if channel_id is None or not self.online:
            return
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.warning("LEVEL_UP_CHANNEL_ID muss auf einen Text-Channel in diesem Server zeigen.")
            return
        role = member.guild.get_role(self.settings.levels[level_number - 1].role_id)
        embed = discord.Embed(title="Neues Voice-Level!", colour=PURPLE,
                              description=f"{member.mention} hat **Level {level_number}** erreicht!\n"
                                          f"Rolle: {role.mention}\n"
                                          f"Gesamte Call-Zeit: **{duration(self.tracker.total(member.id))}**")
        try:
            await channel.send(content=member.mention, embed=embed,
                               allowed_mentions=discord.AllowedMentions(users=[member],
                                                                        roles=False, everyone=False))
        except discord.HTTPException:
            log.warning("Level-Up-Nachricht konnte nicht gesendet werden; Rolle wurde vergeben.",
                        exc_info=True)

    def add_commands(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)

        @self.tree.command(name="rank", description="Zeigt Call-Zeit, Level und nächstes Ziel.", guild=guild)
        @app_commands.guild_only()
        @app_commands.describe(mitglied="Optional: Call-Zeit eines anderen Mitglieds anzeigen.")
        async def rank(interaction: discord.Interaction, mitglied: discord.Member | None = None):
            member = mitglied or interaction.user
            if member.bot:
                await interaction.response.send_message("Bots sammeln keine Call-Zeit.", ephemeral=True)
                return
            total = self.tracker.total(member.id)
            reached = achieved_level(total, self.settings.levels)
            embed = discord.Embed(title=f"Voice-Rang · {member.display_name}", colour=PURPLE)
            embed.add_field(name="Gesamte Call-Zeit", value=duration(total), inline=False)
            role_text = self.settings.levels[reached - 1].role_id if reached else None
            embed.add_field(name="Erreichtes Level",
                            value=f"Level {reached}" + (f" · <@&{role_text}>" if role_text else ""))
            embed.add_field(name="Zeitmessung",
                            value="Läuft gerade" if member.id in self.tracker.active else "Pausiert")
            if reached < len(self.settings.levels):
                goal = self.settings.levels[reached]
                embed.add_field(name=f"Bis Level {goal.number}",
                                value=f"Noch **{duration(goal.seconds - total)}** Call-Zeit", inline=False)
            else:
                embed.add_field(name="Fortschritt", value="Höchstes konfiguriertes Level erreicht!", inline=False)
            embed.set_footer(text=f"Rollenprüfung alle {self.settings.save_interval} Sekunden; "
                                  "Discord kann die Vergabe verzögern.")
            self.enqueue(member.id)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="leaderboard", description="Top 10 nach gesamter Call-Zeit.", guild=guild)
        @app_commands.guild_only()
        async def leaderboard(interaction: discord.Interaction):
            top = sorted(self.tracker.totals().items(), key=lambda row: (-row[1], row[0]))[:10]
            lines = [f"**{index}.** <@{user_id}> — {duration(total)} "
                     f"· Level {achieved_level(total, self.settings.levels)}"
                     for index, (user_id, total) in enumerate(top, start=1) if total > 0]
            embed = discord.Embed(title="Voice-Leaderboard", colour=PURPLE,
                                  description="\n".join(lines) or "Noch keine Call-Zeit gesammelt.")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="levels", description="Zeigt alle Level-Rollen und Stunden-Grenzen.", guild=guild)
        @app_commands.guild_only()
        async def levels(interaction: discord.Interaction):
            text = "\n".join(f"**Level {level.number}** · <@&{level.role_id}> · "
                             f"ab **{level.hours:g} Stunden gesamt**" for level in self.settings.levels)
            embed = discord.Embed(title="Voice-Level", description=text, colour=PURPLE)
            embed.set_footer(text="Es zählt die gesamte gesammelte Zeit, nicht zusätzliche Zeit pro Stufe.")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="botstatus", description="Admin: Prüft Rollen und Zeitmessung.", guild=guild)
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def botstatus(interaction: discord.Interaction):
            problems = self.role_problems(interaction.guild)
            embed = discord.Embed(title="Voice-Level-Bot · Status", colour=PURPLE)
            embed.add_field(name="Rollen", value="\n".join(problems)[:1024] or "Alle Rollen sind verwaltbar.",
                            inline=False)
            embed.add_field(name="Aktive Zeitmessungen", value=str(len(self.tracker.active)))
            embed.add_field(name="Rollenmodus", value=self.settings.role_mode)
            embed.add_field(name="Speicherintervall", value=f"{self.settings.save_interval} Sekunden")
            embed.add_field(name="Dauerhafte Daten", value="Railway-Volume" if self.settings.railway
                            else "Lokaler Datenordner")
            embed.add_field(name="Mindestanzahl Menschen", value=str(self.settings.min_humans))
            welcome = self.settings.welcome
            welcome_status = "Ausgeschaltet"
            if welcome.enabled:
                welcome_status = "\n".join(welcome_problems(interaction.guild, welcome)) or \
                                 f"Aktiv in <#{welcome.channel_id}> · Unturned-Hintergrund"
            embed.add_field(name="Welcomer", value=welcome_status[:1024], inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="levels-sync", description="Admin: Gleicht gespeicherte Zeiten mit Rollen ab.", guild=guild)
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def levels_sync(interaction: discord.Interaction):
            self.enqueue_all()
            await interaction.response.send_message("Rollen-Abgleich wurde gestartet. "
                                                    "Fehler kannst du mit /botstatus prüfen.", ephemeral=True)

        @self.tree.command(name="welcome-test", description="Admin: Sendet eine Welcome-Karte im Welcome-Channel.", guild=guild)
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def welcome_test(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            sent = await self.welcome_member(interaction.user)
            message = "Welcome-Test wurde im Welcome-Channel gesendet." if sent else \
                      "Welcome konnte nicht gesendet werden. WELCOME_ENABLED, WELCOME_CHANNEL_ID " \
                      "und die Channel-Berechtigungen mit /botstatus prüfen."
            await interaction.followup.send(message, ephemeral=True)

    async def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        try:
            self.tracker.stop()
        except Exception as error:
            self.fatal_error = self.fatal_error or error
            log.exception("Letzte Call-Zeiten konnten nicht gespeichert werden.")
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
            log.info("Bot beendet; bestätigte Call-Zeiten sind gespeichert.")


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
    parser = argparse.ArgumentParser(description="Discord Voice-Level-Bot")
    parser.add_argument("--check-config", action="store_true", help="Variablen prüfen, ohne Discord-Login")
    args = parser.parse_args()
    load_dotenv(override=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
        if args.check_config:
            print(f"Konfiguration gültig: {len(settings.levels)} Levels, "
                  f"Modus {settings.role_mode}, Speichern alle {settings.save_interval}s.")
            print("Discord-IDs, Rollen-Berechtigungen und Token werden erst beim Login geprüft.")
            return 0
        asyncio.run(run(settings))
    except ConfigError as error:
        log.error("Konfigurationsfehler: %s", error)
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error("Im Discord Developer Portal bitte den SERVER MEMBERS INTENT aktivieren.")
        return 1
    except discord.LoginFailure:
        log.error("Discord-Login fehlgeschlagen. DISCORD_TOKEN prüfen.")
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception("Bot konnte nicht weiterlaufen.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
