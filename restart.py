"""Scheduled restarts from Discord: /restart and /restart-cancel.

Kept apart from bot.py on purpose. The bridge relays chat and *watches* a
server; this module *changes* one. It uses the bridge only for RCON, the
channel webhook and the owner DM, and the panel for the actual power cycle.

/restart N in a bridged channel:

1. Countdown in game over RCON ``tellraw``, on the cadence of the old KubeJS
   /srestart: 10m 5m 2m 1m 30s, then 10..1. Vanilla command, so it works on
   any pack.
2. Pterodactyl ``restart`` power signal. Wings runs that server's own stop
   command (``sstop now`` on ATM10, which pauses the RCON pollers and drains
   before ``stop``; SIGINT on DeceasedCraft), kills it if it will not go, then
   starts it again. The bot never sends a bare ``stop`` itself, so it needs
   to know nothing about the pack, and wings sees an expected restart rather
   than a crash.
3. Watch. Panel never leaves ``running``: ``kill`` after STOP_TIMEOUT. Stuck
   in ``stopping``: ``kill`` only after STOPPING_TIMEOUT, wings' own limit -
   a save in progress must never be killed early. Down but wings leaves it
   offline, or not answering RCON after START_TIMEOUT: ``start``. A boot the
   panel reports as ``starting`` gets BOOT_TIMEOUT. Past that: give up.

The owner is DMed when a restart is scheduled, cancelled, back up, or given
up on. A bot restart forgets a countdown in progress - nothing is persisted.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("discord_mc_bot.restart")


class Pterodactyl:
    """Just enough of the panel client API: power signals and current state."""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str):
        self.session = session
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self.timeout = aiohttp.ClientTimeout(total=15)

    async def power(self, server: str, signal: str) -> None:
        """Send start / stop / restart / kill. Raises on anything but 204."""
        async with self.session.post(
            f"{self.url}/api/client/servers/{server}/power",
            json={"signal": signal},
            headers=self.headers,
            timeout=self.timeout,
        ) as resp:
            if resp.status != 204:
                body = (await resp.text())[:200]
                raise RuntimeError(f"power {signal} -> HTTP {resp.status}: {body}")

    async def state(self, server: str) -> Optional[str]:
        """running / starting / stopping / offline, or None if the panel did not say."""
        try:
            async with self.session.get(
                f"{self.url}/api/client/servers/{server}/resources",
                headers=self.headers,
                timeout=self.timeout,
            ) as resp:
                if resp.status != 200:
                    return None
                return (await resp.json())["attributes"]["current_state"]
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
            return None


@dataclass
class RestartJob:
    requester: str
    minutes: int
    due_at: float  # time.time() of the restart signal
    task: Optional[asyncio.Task] = None
    signalled: bool = False  # past the point where cancel can still help


class RestartCommands(commands.Cog):
    """/restart and /restart-cancel, one job at a time per server."""

    # Seconds before the restart at which players are warned. Exact-total
    # matches are skipped because the scheduling announcement already says it.
    WARNINGS = (600, 300, 120, 60, 30, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)
    POLL: float = 5.0
    STOP_TIMEOUT: float = 90.0  # panel never left `running` -> kill
    STOPPING_TIMEOUT: float = 600.0  # panel stuck in `stopping` -> kill; wings' own limit
    KILL_TIMEOUT: float = 60.0  # after kill, before giving up
    START_TIMEOUT: float = 300.0  # not `starting`, no RCON -> start, then give up
    BOOT_TIMEOUT: float = 900.0  # panel says `starting` this long -> give up
    # Panel says offline this many polls in a row while we wait for it to come
    # up: wings is not going to start it, so we do. Wings normally starts it
    # within seconds of the stop, so 30s of offline is already abnormal.
    OFFLINE_POLLS_BEFORE_START = 6

    EMBED_COLOR_ORANGE = 0xE67E22
    EMBED_COLOR_GREEN = 0x57F287

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.jobs: dict[int, RestartJob] = {}  # channel_id -> job
        self.session: Optional[aiohttp.ClientSession] = None
        self.ptero: Optional[Pterodactyl] = None
        self._roles_synced = False

    async def cog_load(self) -> None:
        cfg = self.bot.config.pterodactyl
        self.session = aiohttp.ClientSession()
        self.ptero = Pterodactyl(self.session, cfg.url, cfg.api_key)
        logger.info("Restart module loaded, panel %s", cfg.url)

    async def cog_unload(self) -> None:
        for job in self.jobs.values():
            job.task.cancel()
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------------ roles

    @staticmethod
    def role_name(bridge) -> str:
        return f"{bridge.config.minecraft.server_name} Admin"

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready fires again after every gateway reconnect; a sync that did
        # not get through (guild not yet available, missing permission) is
        # retried then, a complete one is not repeated.
        if self._roles_synced:
            return
        try:
            self._roles_synced = await self.sync_roles()
        except Exception:
            logger.exception("Role sync failed")

    @staticmethod
    def _looks_ours(role: discord.Role) -> bool:
        """Cheap pre-filter: '<name> Admin', no guild permissions, not hoisted,
        not an integration role, and below the bot so it can be deleted."""
        return (
            role.name.endswith(" Admin")
            and not role.managed
            and not role.hoist
            and role.permissions.value == 0
            and role < role.guild.me.top_role
        )

    async def _created_by_bot(self, role: discord.Role) -> bool:
        """The audit log is the only record of who made a role. It keeps 45
        days, so an older stale role is reported and left, never deleted.
        Without View Audit Log nothing is ever deleted either."""
        try:
            async for entry in role.guild.audit_logs(
                limit=200, action=discord.AuditLogAction.role_create
            ):
                if entry.target is not None and entry.target.id == role.id:
                    return entry.user is not None and entry.user.id == self.bot.user.id
        except discord.Forbidden:
            logger.warning("Cannot read the audit log (View Audit Log) - stale roles are left alone")
        return False

    async def sync_roles(self) -> bool:
        """Create the '<server> Admin' role per bridged server, drop stale ones
        this bot created earlier. False if it could not do the whole job."""
        wanted: dict[discord.Guild, set[str]] = {}
        for bridge in self.bot.bridges.values():
            channel = self.bot.get_channel(bridge.config.discord.channel_id)
            guild = getattr(channel, "guild", None)
            if guild is not None:
                wanted.setdefault(guild, set()).add(self.role_name(bridge))
        if not wanted:
            logger.warning("No bridged channel visible - roles not synced yet")
            return False

        for guild, names in wanted.items():
            have = {r.name: r for r in guild.roles}
            for name in sorted(names - have.keys()):
                try:
                    await guild.create_role(name=name, reason="/restart access")
                    logger.info("Created role %r in %s", name, guild.name)
                except discord.Forbidden:
                    logger.error("Cannot create role %r - the bot needs Manage Roles", name)
                    return False
            for name, role in have.items():
                if name in names or not self._looks_ours(role):
                    continue
                if not await self._created_by_bot(role):
                    logger.warning("Role %r matches no server but was not created by this bot - left alone", name)
                    continue
                try:
                    await role.delete(reason="no bridged server with this name")
                    logger.warning("Deleted stale role %r from %s", name, guild.name)
                except discord.HTTPException as e:
                    logger.error("Deleting stale role %r failed: %s", name, e)
        return True

    def allowed(self, interaction: discord.Interaction, bridge) -> bool:
        """Guild owner, or a member holding that server's Admin role."""
        user, guild = interaction.user, interaction.guild
        if guild is None or not isinstance(user, discord.Member):
            return False
        if user.id == guild.owner_id:
            return True
        name = self.role_name(bridge)
        return any(r.name == name for r in user.roles)

    # --------------------------------------------------------------- commands

    async def _bridge_for(self, interaction: discord.Interaction):
        """The bridge behind the channel the command was run in, or None after
        telling the user where it works."""
        bridge = self.bot.bridges.get(interaction.channel_id)
        if bridge is None:
            where = ", ".join(f"<#{cid}>" for cid in self.bot.bridges) or "none configured"
            await self._ephemeral(interaction, f"Run this in a Minecraft chat channel: {where}")
        elif not self.allowed(interaction, bridge):
            await self._ephemeral(
                interaction,
                f"You need the **{self.role_name(bridge)}** role to do that.",
            )
            return None
        return bridge

    @staticmethod
    async def _ephemeral(interaction: discord.Interaction, text: str, color: int = 0xE67E22) -> None:
        embed = discord.Embed(description=text, color=color)
        if interaction.response.is_done():  # deferred while the panel was asked
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="restart",
        description="Restart this channel's Minecraft server after an in-game countdown",
    )
    @app_commands.describe(minutes="Countdown before the restart, 1-60 (default 5)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def restart_command(
        self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 60] = 5
    ) -> None:
        bridge = await self._bridge_for(interaction)
        if bridge is None:
            return
        cid = bridge.config.discord.channel_id
        name = bridge.config.minecraft.server_name

        job = self.jobs.get(cid)
        if job is not None:
            left = max(0, int(job.due_at - time.time()))
            await self._ephemeral(
                interaction,
                f"A restart is already scheduled by **{job.requester}** "
                f"({_duration(left)} left). Run `/restart-cancel` first.",
            )
            return

        # Ask the panel before promising players anything: this is the one
        # place a wrong id, a bad key or an unreachable panel can be caught
        # before, rather than after, a five minute countdown.
        await interaction.response.defer(ephemeral=True)
        state = await self.ptero.state(bridge.config.minecraft.pterodactyl_id)
        if state is None:
            await self._ephemeral(
                interaction,
                f"The panel did not answer for **{name}** - check `[pterodactyl]` "
                f"and this server's `pterodactyl_id`.",
            )
            return
        if state != "running":
            await self._ephemeral(interaction, f"**{name}** is {state} - nothing to restart.")
            return

        who = interaction.user.display_name
        job = RestartJob(requester=who, minutes=minutes, due_at=time.time() + minutes * 60)
        job.task = asyncio.create_task(self._run(bridge, job), name=f"restart:{name}")
        self.jobs[cid] = job
        bridge.log.info("Restart scheduled by %s in %d minutes", who, minutes)

        await self._ephemeral(
            interaction,
            f"Restart of **{name}** scheduled in {_duration(minutes * 60)}.",
            self.EMBED_COLOR_GREEN,
        )
        await bridge.send_webhook_embed(
            f"**{who}** scheduled a restart in {_duration(minutes * 60)}",
            self.EMBED_COLOR_ORANGE,
        )
        await bridge.dm_owner(
            f"\U0001f501 **{who}** scheduled a restart of **{name}** in {_duration(minutes * 60)}."
        )

    @app_commands.command(
        name="restart-cancel",
        description="Cancel the restart countdown on this channel's Minecraft server",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def cancel_command(self, interaction: discord.Interaction) -> None:
        bridge = await self._bridge_for(interaction)
        if bridge is None:
            return
        cid = bridge.config.discord.channel_id
        name = bridge.config.minecraft.server_name

        job = self.jobs.get(cid)
        if job is None:
            await self._ephemeral(interaction, f"No restart is scheduled for **{name}**.")
            return
        if job.signalled:
            await self._ephemeral(
                interaction, f"Too late - the restart signal for **{name}** is already sent."
            )
            return

        del self.jobs[cid]
        job.task.cancel()
        who = interaction.user.display_name
        bridge.log.info("Restart cancelled by %s", who)

        await self._say(bridge, ("Server restart has been ", "yellow"), ("cancelled", "green"))
        await self._ephemeral(
            interaction, f"Restart of **{name}** cancelled.", self.EMBED_COLOR_GREEN
        )
        await bridge.send_webhook_embed(
            f"**{who}** cancelled the restart", self.EMBED_COLOR_GREEN
        )
        await bridge.dm_owner(f"⏹️ **{who}** cancelled the restart of **{name}**.")

    # -------------------------------------------------------------- the job

    async def _run(self, bridge, job: RestartJob) -> None:
        cid = bridge.config.discord.channel_id
        name = bridge.config.minecraft.server_name
        try:
            await self._countdown(bridge, job.minutes)
            job.signalled = True
            await self._bounce(bridge)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            bridge.log.exception("Restart failed")
            await bridge.dm_owner(f"\U0001f6a8 Restart of **{name}** failed: {e}. Check the panel.")
        finally:
            if self.jobs.get(cid) is job:
                del self.jobs[cid]

    async def _countdown(self, bridge, minutes: int) -> None:
        total = minutes * 60
        loop = asyncio.get_running_loop()
        deadline = loop.time() + total

        await self._say(
            bridge, ("Server restart scheduled in ", "yellow"), (_duration(total), "green")
        )
        for warn in self.WARNINGS:
            if warn >= total:
                continue
            await asyncio.sleep(max(0.0, deadline - warn - loop.time()))
            if warn <= 10:
                await self._say(bridge, (str(warn), "red"))
            else:
                await self._say(bridge, ("Restarting in ", "yellow"), (_duration(warn), "red"))
        await asyncio.sleep(max(0.0, deadline - loop.time()))
        await self._say(bridge, ("Restarting now!", "red"))

    async def _say(self, bridge, *segments: tuple[str, str]) -> None:
        """Broadcast '[Server] ...' to every player. Segments are (text, colour);
        red is bold, like the old script. Vanilla tellraw, so it works on any
        pack. Leading "" keeps later parts from inheriting the prefix style."""
        parts = [{"text": "[Server] ", "color": "red", "bold": True}]
        parts += [{"text": t, "color": c, "bold": c == "red"} for t, c in segments]
        cmd = "tellraw @a " + json.dumps([""] + parts, separators=(",", ":"))
        if await bridge.send_rcon_command(cmd) is None:
            bridge.log.warning(
                "Countdown message not delivered: %s", "".join(t for t, _ in segments)
            )

    async def _bounce(self, bridge) -> None:
        """Restart through the panel and watch it come back, escalating if not."""
        sid = bridge.config.minecraft.pterodactyl_id
        name = bridge.config.minecraft.server_name
        started = time.time()

        await self.ptero.power(sid, "restart")
        bridge.log.info("Pterodactyl restart signal sent")

        # Going down. Wings kills on its own once its stop timeout passes; the
        # kill here is for a wings that never acted (still `running`) or one
        # that is stuck (`stopping` past wings' own limit) - never for a save
        # that is merely slow.
        if not await self._wait_down(sid, self.STOP_TIMEOUT, self.STOPPING_TIMEOUT):
            bridge.log.warning("Not down after the restart signal - sending kill")
            await self._power_quietly(bridge, sid, "kill")
            if not await self._wait_down(sid, self.KILL_TIMEOUT):
                raise RuntimeError(f"still not down {self.KILL_TIMEOUT:.0f}s after kill")
        bridge.log.info("Server is down, waiting for it to come back")

        # Coming up. Ready means RCON answers a vanilla command: on ATM10 that
        # also implies the pause flag was cleared by the new boot.
        started_again = False
        phase_start = time.time()
        offline_polls = 0
        while True:
            if await bridge.send_rcon_command("list") is not None:
                took = time.time() - started
                bridge.log.info("Server back up %.0fs after the restart signal", took)
                await bridge.dm_owner(
                    f"✅ **{name}** is back up, {_duration(took)} after the restart signal."
                )
                return

            state = await self.ptero.state(sid)
            offline_polls = offline_polls + 1 if state == "offline" else 0
            waited = time.time() - phase_start
            if state == "starting":
                # Booting. A heavy pack takes minutes, the first boot after a
                # pack update longer still; only a boot that never ends fails.
                if waited >= self.BOOT_TIMEOUT:
                    raise RuntimeError(f"still starting {waited:.0f}s after going down")
            elif not started_again and (
                offline_polls >= self.OFFLINE_POLLS_BEFORE_START or waited >= self.START_TIMEOUT
            ):
                bridge.log.warning("Not coming up on its own (state %s) - sending start", state)
                started_again = True
                phase_start = time.time()
                await self._power_quietly(bridge, sid, "start")
            elif started_again and waited >= self.START_TIMEOUT:
                raise RuntimeError(
                    f"not answering RCON {self.START_TIMEOUT:.0f}s after a second start "
                    f"(panel state: {state})"
                )
            await asyncio.sleep(self.POLL)

    async def _wait_down(
        self, sid: str, timeout: float, stopping_timeout: Optional[float] = None
    ) -> bool:
        """Poll the panel until the old process is gone, or the timeout.

        Down means past `stopping`: the panel says offline or starting, or it
        said stopping and now says running again, which is a server that
        booted between two polls (DC does the whole cycle in ~35s). Neither
        "not running" nor an RCON reply is enough on its own - a server that
        is saving reports stopping and keeps answering RCON, and on 2026-09-14
        that had DC declared back up 150ms after the signal.

        Once `stopping` is seen the budget becomes stopping_timeout, if given:
        a stop that has started is wings' to finish or kill.
        """
        # ponytail: one 5s sample must land in stopping/offline/starting. A
        # server whose whole stop+boot fits inside one poll gap is never seen
        # going down; poll faster here if that ever exists.
        began = time.time()
        seen_stopping = False
        while True:
            state = await self.ptero.state(sid)
            if state == "stopping" and not seen_stopping:
                seen_stopping = True
                timeout = stopping_timeout or timeout
            if state in ("offline", "starting") or (seen_stopping and state == "running"):
                return True
            if time.time() - began >= timeout:
                return False
            await asyncio.sleep(self.POLL)

    async def _power_quietly(self, bridge, sid: str, signal: str) -> None:
        """Escalation signals are best effort: wings refuses one while it is
        busy with another (HTTP 409), and the watch loop copes either way."""
        try:
            await self.ptero.power(sid, signal)
        except Exception as e:
            bridge.log.warning("Pterodactyl %s refused: %s", signal, e)


def _duration(seconds: float) -> str:
    """'5 minutes', '1 minute', '30 seconds', '2 minutes 17 seconds'."""
    m, s = divmod(int(seconds), 60)
    parts = []
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s or not m:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts)
