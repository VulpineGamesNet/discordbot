"""
Discord-Minecraft Chat Sync Bot

One process bridges N Minecraft servers. Each server gets its own Discord
channel, RCON endpoint and MySQL database, defined by a [[servers]] block in
config.toml. Per server it handles:
- Discord -> Minecraft chat relay via RCON
- Minecraft -> Discord chat relay via webhook (polling the discord_events table)
- Channel topic updates with server stats (TPS, players, uptime)
- Server start/stop notifications (via RCON connectivity monitoring)
"""

import asyncio
import io
import json
import logging
import os
import re
import signal
import socket
import struct
import time
import traceback
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image

from config import BotConfig, ServerConfig, load_config
from database import DatabaseManager
from models import DiscordEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("discord_mc_bot")


class MinecraftBridge(commands.Cog):
    """Bridges one Minecraft server to one Discord channel.

    Instantiated once per [[servers]] entry. Everything stateful lives on the
    instance - including the three tasks.loop attributes, which discord.py
    copies per instance via Loop.__get__.
    """

    EMBED_COLOR_GREEN = 0x57F287
    EMBED_COLOR_RED = 0xED4245
    EMBED_COLOR_ORANGE = 0xE67E22
    EMBED_COLOR_BLUE = 0x3498DB
    EMBED_COLOR_PURPLE = 0x9B59B6  # For bot status notifications

    WEBHOOK_NAME = "Minecraft Bridge"

    RCON_SERVERDATA_AUTH = 3
    RCON_SERVERDATA_EXECCOMMAND = 2

    def __init__(self, bot: "DiscordMCBot", config: ServerConfig):
        self.bot = bot
        self.config = config
        server_name = config.minecraft.server_name
        # discord.py keys cogs by __cog_name__, which CogMeta sets as a *class*
        # attribute - shadowing it per instance is what lets N copies of this
        # cog coexist on one bot. Dunder on both ends, so no name mangling.
        self.__cog_name__ = f"MinecraftBridge[{server_name}]"
        # Every log line from this bridge is tagged with the server name, so a
        # flapping server is identifiable when several are running.
        self.log = logger.getChild(server_name)
        self.icon_url = config.discord.icon_url
        # Debounce settings (config-driven via config.settings)
        self.STATUS_COOLDOWN: int = config.settings.status_cooldown
        self.OFFLINE_THRESHOLD: int = config.settings.offline_threshold
        # How long the server must be continuously unreachable before it is
        # called offline. Expressed in seconds rather than a count of checks:
        # a sick server makes each check take far longer than the poll
        # interval (a hung RCON read costs 30s+), so counting checks silently
        # stretches the window exactly when it matters. ATM10 was down four
        # minutes on 2026-08-03 and only completed eight checks against a
        # threshold of twelve, so nothing was ever announced.
        self.OFFLINE_AFTER_SECONDS: float = (
            config.settings.offline_threshold * config.settings.stats_check_interval
        )
        # A refused connection is not ambiguous: nothing is listening on the
        # port, so the process is gone rather than merely slow. That needs no
        # patience, unlike a timeout which may just be a struggling server.
        self.OFFLINE_REFUSED_AFTER_SECONDS: float = 2 * config.settings.stats_check_interval
        self.last_stats: Optional[dict] = None
        self.rcon_lock = asyncio.Lock()
        self.last_topic: Optional[str] = None
        self.server_online: bool = False
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.managed_webhook: Optional[discord.Webhook] = None
        # Debounce state
        self.last_status_notification: float = 0
        self.consecutive_offline_checks: int = 0
        # Timestamp of the first failed check in the current run of failures,
        # None while the server is answering. This is what the offline decision
        # is actually based on.
        self.first_failed_check: Optional[float] = None
        # False until the first poll has told us what the server is doing. The
        # bot starts assuming offline, so without this the first successful
        # poll looks like a transition and announces "is now online" for a
        # server that never went anywhere - only the bot restarted.
        self._status_known: bool = False
        # True once an outage has been announced and not yet resolved. A
        # recovery that answers an announced outage always gets posted, even
        # inside the cooldown: throttling repeat alerts is worth doing,
        # silently withholding the all-clear is not - it leaves the channel
        # claiming the server is still down.
        self._offline_announced: bool = False
        # Persistent RCON connection state
        self._rcon_socket: Optional[socket.socket] = None
        self._rcon_connected: bool = False
        # True when the last attempt was refused outright, i.e. nothing is
        # listening. Distinguishes "process is gone" from "process is wedged".
        self._rcon_refused: bool = False
        # Server log watching (optional, only when log_path is configured)
        self.log_path: str = config.minecraft.log_path
        self._log_pos: int = 0
        self._log_inode: Optional[int] = None
        # Set once a crash marker has been announced, cleared when the server
        # answers again, so one incident produces one message.
        self._crash_announced: bool = False
        # RCON survives the start of a shutdown - the server keeps answering
        # while it saves. Until a poll has actually failed, a success after a
        # log warning means "still going down", not "back up", and must not be
        # allowed to post an "is now online" in the middle of the outage.
        self._down_confirmed: bool = False
        self._log_down_at: float = 0.0
        # Database manager for Discord events
        self.db_manager: DatabaseManager = DatabaseManager(config.database, server_name)
        # name (lowercased) -> (member id or None, when the answer expires)
        self._mention_cache: dict[str, tuple[Optional[int], float]] = {}

    async def cog_load(self) -> None:
        """Called when cog is loaded."""
        self.http_session = aiohttp.ClientSession()
        # Initialize database connection
        if await self.db_manager.initialize():
            self.poll_discord_events.start()
        else:
            self.log.warning("Database not available - discord events polling disabled")
        self.poll_server_stats.start()
        self.update_channel_topic.start()
        if self.log_path:
            self.watch_server_log.start()
            self.log.info("Watching server log at %s", self.log_path)
        self.log.info("MinecraftBridge cog loaded")

    async def cog_unload(self) -> None:
        """Called when cog is unloaded."""
        self.poll_server_stats.cancel()
        self.update_channel_topic.cancel()
        if self.watch_server_log.is_running():
            self.watch_server_log.cancel()
        if self.poll_discord_events.is_running():
            self.poll_discord_events.cancel()
        if self.http_session:
            await self.http_session.close()
        self._rcon_disconnect()
        await self.db_manager.close()

    async def setup_webhook(self) -> None:
        """Get or create a webhook for the configured channel."""
        # Skip if manual webhook URL is configured
        if self.config.discord.webhook_url:
            self.log.info("Using manually configured webhook URL")
            return

        channel = self.bot.get_channel(self.config.discord.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            self.log.warning("Could not find text channel for webhook setup")
            return

        try:
            webhooks = await channel.webhooks()
            # Look for existing webhook created by this bot
            for webhook in webhooks:
                if webhook.name == self.WEBHOOK_NAME:
                    self.managed_webhook = webhook
                    self.log.info(f"Found existing webhook: {webhook.name}")
                    return

            # Create new webhook
            self.managed_webhook = await channel.create_webhook(
                name=self.WEBHOOK_NAME,
                reason="Minecraft chat bridge webhook",
            )
            self.log.info(f"Created new webhook: {self.managed_webhook.name}")

        except discord.Forbidden:
            self.log.warning("Missing MANAGE_WEBHOOKS permission - configure webhook_url manually")
        except Exception as e:
            self.log.error(f"Failed to setup webhook: {e}")

    def _rcon_send_packet(
        self, sock: socket.socket, packet_id: int, packet_type: int, payload: str
    ) -> None:
        """Send an RCON packet."""
        payload_bytes = payload.encode("utf-8") + b"\x00\x00"
        packet = struct.pack("<ii", packet_id, packet_type) + payload_bytes
        packet = struct.pack("<i", len(packet)) + packet
        sock.sendall(packet)

    # A whole packet must arrive within this long. The socket timeout alone is
    # not enough: it applies per recv() call, so a server dribbling bytes
    # refreshes it indefinitely and one read can block for minutes. Seen on
    # 2026-08-03, when a single call held the stats loop for 167 seconds.
    RCON_READ_DEADLINE: float = 15.0

    def _rcon_recv_packet(self, sock: socket.socket) -> tuple[int, int, str]:
        """Receive an RCON packet, bounded by RCON_READ_DEADLINE overall."""
        deadline = time.monotonic() + self.RCON_READ_DEADLINE

        length_data = sock.recv(4)
        if len(length_data) < 4:
            raise ConnectionError("Failed to read packet length")
        length = struct.unpack("<i", length_data)[0]

        data = b""
        while len(data) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionError(
                    f"Timed out after {self.RCON_READ_DEADLINE}s with "
                    f"{len(data)}/{length} bytes of the packet"
                )
            sock.settimeout(min(remaining, 30.0))
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Connection closed by server")
            data += chunk

        packet_id = struct.unpack("<i", data[0:4])[0]
        packet_type = struct.unpack("<i", data[4:8])[0]
        payload = data[8:-2].decode("utf-8")

        return packet_id, packet_type, payload

    def _rcon_connect(self) -> bool:
        """Establish persistent RCON connection with authentication."""
        self._rcon_disconnect()
        self._rcon_refused = False

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect(
                (self.config.minecraft.rcon_host, self.config.minecraft.rcon_port)
            )

            self._rcon_send_packet(
                sock, 1, self.RCON_SERVERDATA_AUTH, self.config.minecraft.rcon_password
            )
            packet_id, _, _ = self._rcon_recv_packet(sock)

            if packet_id == -1:
                self.log.error("RCON authentication failed - check password")
                sock.close()
                return False

            sock.settimeout(30.0)
            self._rcon_socket = sock
            self._rcon_connected = True
            self._rcon_refused = False
            self.log.info("RCON connection established")
            return True

        except ConnectionRefusedError:
            # Nothing is listening. Either the server has not started yet or
            # the process is gone; poll_server_stats treats it as gone.
            self._rcon_refused = True
            self.log.debug("RCON connection refused - nothing listening on the port")
            return False
        except socket.timeout:
            self.log.warning("RCON connection timed out")
            return False
        except OSError as e:
            self.log.debug(f"RCON connection error: {e}")
            return False

    def _rcon_disconnect(self) -> None:
        """Close RCON connection cleanly."""
        if self._rcon_socket:
            try:
                self._rcon_socket.close()
            except OSError:
                pass
            self._rcon_socket = None
        self._rcon_connected = False

    def _rcon_sync(self, command: str) -> str:
        """Send command using persistent RCON connection with auto-reconnect."""
        if not self._rcon_connected:
            if not self._rcon_connect():
                raise ConnectionError("Not connected to RCON")

        try:
            self._rcon_send_packet(
                self._rcon_socket, 2, self.RCON_SERVERDATA_EXECCOMMAND, command
            )
            _, _, response = self._rcon_recv_packet(self._rcon_socket)
            return response

        except (socket.timeout, socket.error, ConnectionError, BrokenPipeError, OSError) as e:
            self.log.warning(f"RCON command failed, connection lost: {e}")
            self._rcon_disconnect()
            raise ConnectionError(f"RCON disconnected: {e}")

    async def send_rcon_command(self, command: str) -> Optional[str]:
        """Send a command to Minecraft server via RCON."""
        async with self.rcon_lock:
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, self._rcon_sync, command)
                return result
            except Exception as e:
                self.log.debug(f"RCON error: {e}")
                return None

    async def get_stats_via_rcon(self) -> Optional[dict]:
        """Get server stats via RCON /getstats command."""
        response = await self.send_rcon_command("getstats")
        if response is None:
            return None

        try:
            return json.loads(response)
        except json.JSONDecodeError as e:
            self.log.warning(f"Invalid JSON from /getstats: {e}")
            return None

    async def send_webhook_message(
        self,
        content: str,
        player_name: Optional[str] = None,
        player_uuid: Optional[str] = None,
    ) -> bool:
        """Send a message via Discord webhook."""
        # Chat carries "@name" pings resolved by resolve_mentions, so user
        # mentions must parse - but a webhook may ping @everyone and every role
        # by default, and nobody in game gets to do that.
        allowed = discord.AllowedMentions(everyone=False, roles=False, users=True)

        # Use managed webhook if available
        if self.managed_webhook:
            try:
                avatar_url = f"https://mc-heads.net/avatar/{player_uuid}/128" if player_uuid else self.icon_url
                await self.managed_webhook.send(
                    content=content,
                    username=player_name or self.config.minecraft.server_name,
                    avatar_url=avatar_url,
                    allowed_mentions=allowed,
                )
                return True
            except discord.HTTPException as e:
                self.log.error(f"Managed webhook error: {e}")
                return False

        # Fall back to manual webhook URL
        if not self.config.discord.webhook_url:
            self.log.debug("No webhook available")
            return False

        if not self.http_session:
            return False

        avatar_url = f"https://mc-heads.net/avatar/{player_uuid}/128" if player_uuid else self.icon_url
        payload = {
            "content": content,
            "avatar_url": avatar_url,
            "allowed_mentions": {"parse": ["users"]},
        }
        if player_name:
            payload["username"] = player_name

        try:
            async with self.http_session.post(
                self.config.discord.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    return True
                elif resp.status == 429:
                    self.log.warning("Webhook rate limited")
                    return False
                else:
                    self.log.warning(f"Webhook returned status: {resp.status}")
                    return False
        except Exception as e:
            self.log.error(f"Webhook error: {e}")
            return False

    async def send_webhook_embed(
        self,
        description: str,
        color: int,
        icon_url: Optional[str] = None,
        author_name: Optional[str] = None,
    ) -> bool:
        """Send an embed via Discord webhook (for system messages like join/leave/status)."""
        # Build embed
        embed = discord.Embed(color=color)
        if description:
            embed.description = description
        if icon_url and author_name:
            embed.set_author(name=author_name, icon_url=icon_url)

        # Use managed webhook if available
        if self.managed_webhook:
            try:
                await self.managed_webhook.send(
                    embed=embed,
                    username=self.config.minecraft.server_name,
                    avatar_url=self.icon_url,
                )
                return True
            except discord.HTTPException as e:
                self.log.error(f"Managed webhook embed error: {e}")
                return False

        # Fall back to manual webhook URL
        if not self.config.discord.webhook_url:
            self.log.debug("No webhook available")
            return False

        if not self.http_session:
            return False

        payload = {
            "embeds": [{"color": color, "description": description if description else None,
                       "author": {"name": author_name, "icon_url": icon_url} if icon_url and author_name else None}],
            "username": self.config.minecraft.server_name,
            "avatar_url": self.icon_url,
        }

        try:
            async with self.http_session.post(
                self.config.discord.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    return True
                elif resp.status == 429:
                    self.log.warning("Webhook rate limited")
                    return False
                else:
                    self.log.warning(f"Webhook returned status: {resp.status}")
                    return False
        except Exception as e:
            self.log.error(f"Webhook embed error: {e}")
            return False

    async def send_bot_status(self, status: str, color: int) -> bool:
        """Send bot status notification to Discord."""
        return await self.send_webhook_embed(
            None,
            color,
            self.icon_url,
            status,
        )

    async def generate_player_avatars_image(self, players: list) -> Optional[io.BytesIO]:
        """Generate a combined image of player avatars (max 5 per row)."""
        if not players or not self.http_session:
            return None

        avatar_size = 32
        padding = 4
        max_per_row = 5
        avatars = []

        # Fetch all player avatars
        for player in players[:20]:  # Limit to 20 players
            if isinstance(player, dict):
                uuid = player.get("uuid", "")
            else:
                continue

            if not uuid:
                continue

            try:
                url = f"https://mc-heads.net/avatar/{uuid}/{avatar_size}"
                async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        img = Image.open(io.BytesIO(data))
                        avatars.append(img)
            except Exception as e:
                self.log.debug(f"Failed to fetch avatar for {uuid}: {e}")
                continue

        if not avatars:
            return None

        # Calculate dimensions (max 5 per row, multiple rows if needed)
        cols = min(len(avatars), max_per_row)
        rows = (len(avatars) + max_per_row - 1) // max_per_row

        total_width = cols * avatar_size + (cols - 1) * padding
        total_height = rows * avatar_size + (rows - 1) * padding

        combined = Image.new('RGBA', (total_width, total_height), (0, 0, 0, 0))

        for i, avatar in enumerate(avatars):
            row = i // max_per_row
            col = i % max_per_row
            x = col * (avatar_size + padding)
            y = row * (avatar_size + padding)
            combined.paste(avatar, (x, y))

        # Save to buffer
        buffer = io.BytesIO()
        combined.save(buffer, format='PNG')
        buffer.seek(0)

        return buffer

    # "@name" typed in game. Dots are allowed inside a Discord username but not
    # at the end, so "@fox." looks up "fox" and keeps the full stop.
    MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)")
    MENTION_CACHE_TTL: float = 600.0  # also how long a rename stays wrong
    MENTION_MAX_PER_MESSAGE = 5  # cap the gateway lookups one message can cost

    async def _lookup_member_id(self, name: str) -> Optional[int]:
        """Find the Discord member behind a name, cached for MENTION_CACHE_TTL."""
        key = name.lower()
        cached = self._mention_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        channel = self.bot.get_channel(self.config.discord.channel_id)
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None

        try:
            # Gateway member query. A non-empty query needs no privileged
            # members intent, unlike fetching the whole member list.
            members = await guild.query_members(query=name, limit=10)
        except (discord.HTTPException, asyncio.TimeoutError) as e:
            # Not cached: a transient failure must not stick for ten minutes.
            self.log.debug(f"Member lookup for @{name} failed: {e}")
            return None

        member = discord.utils.find(
            lambda m: key in (m.name.lower(), m.display_name.lower()), members
        )
        member_id = member.id if member else None
        self._mention_cache[key] = (member_id, time.monotonic() + self.MENTION_CACHE_TTL)
        return member_id

    async def resolve_mentions(self, content: str) -> str:
        """Turn "@name" from in-game chat into a real Discord ping."""
        if "@" not in content:
            return content

        names = {m.group(1) for m in self.MENTION_RE.finditer(content)}
        # Longest first: replacing "@fox" before "@foxtrot" would maul the latter.
        for name in sorted(names, key=len, reverse=True)[: self.MENTION_MAX_PER_MESSAGE]:
            member_id = await self._lookup_member_id(name)
            if member_id:
                content = content.replace(f"@{name}", f"<@{member_id}>")
        return content

    async def process_events(self, events: list[DiscordEvent]) -> None:
        """Process events from database (chat, join, leave)."""
        for event in events:
            msg_type = event.event_type
            player = event.player_name
            uuid = event.player_uuid

            if msg_type == "chat":
                content = await self.resolve_mentions(event.message or "")
                await self.send_webhook_message(content, player, uuid)
                self.log.info(f"Relayed chat from {player}: {content[:50]}...")

            elif msg_type == "join":
                icon_url = f"https://mc-heads.net/avatar/{uuid}/32"
                await self.send_webhook_embed(
                    None,
                    self.EMBED_COLOR_GREEN,
                    icon_url,
                    f"{player} logged in",
                )
                self.log.info(f"Sent join notification for {player}")

            elif msg_type == "leave":
                icon_url = f"https://mc-heads.net/avatar/{uuid}/32"
                await self.send_webhook_embed(
                    None,
                    self.EMBED_COLOR_RED,
                    icon_url,
                    f"{player} logged out",
                )
                self.log.info(f"Sent leave notification for {player}")

    @tasks.loop(seconds=2)
    async def poll_discord_events(self) -> None:
        """Poll database for unprocessed Discord events."""
        if not self.db_manager.is_initialized:
            return

        events = await self.db_manager.get_unprocessed_events(limit=10)
        if not events:
            return

        # Process events
        await self.process_events(events)

        # Mark as processed
        event_ids = [e.id for e in events]
        await self.db_manager.mark_events_processed(event_ids)

    @poll_discord_events.before_loop
    async def before_poll_events(self) -> None:
        self.poll_discord_events.change_interval(seconds=self.config.settings.events_poll_interval)
        await self.bot.wait_until_ready()

    @poll_discord_events.error
    async def poll_discord_events_error(self, error: BaseException) -> None:
        self.log.error(
            "poll_discord_events loop crashed:\n%s",
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        )
        await asyncio.sleep(10)
        try:
            self.poll_discord_events.restart()
            self.log.info("poll_discord_events loop restarted after crash")
        except Exception as e:
            self.log.error(f"Failed to restart poll_discord_events loop: {e}")

    @tasks.loop(seconds=5)
    async def poll_server_stats(self) -> None:
        """Poll server stats via RCON for status monitoring."""
        stats = await self.get_stats_via_rcon()
        now = time.time()

        if stats:
            # A server that is shutting down answers RCON until the save
            # finishes, so a success here does not undo a log warning until a
            # poll has actually failed. Without the grace cap a log line that
            # turns out to be survivable would wedge the bridge as offline.
            if self._crash_announced and not self._down_confirmed:
                if now - self._log_down_at < self.LOG_DOWN_GRACE:
                    self.log.debug("RCON still answering after the log said the server is going down")
                    return
                self.log.info(
                    "Server still answering %.0fs after the log warning - treating it as survived",
                    now - self._log_down_at,
                )

            self.last_stats = stats
            self.consecutive_offline_checks = 0  # Reset offline counter
            self.first_failed_check = None
            self._crash_announced = False
            self._down_confirmed = False

            if not self.server_online:
                self.server_online = True
                if not self._status_known:
                    # First look after startup: adopt the state, announce
                    # nothing. Only transitions this bot actually watched
                    # happen are worth reporting.
                    self._status_known = True
                    self.log.info("Server already up at startup - no notification")
                elif (
                    self._offline_announced
                    or now - self.last_status_notification >= self.STATUS_COOLDOWN
                ):
                    server_name = self.config.minecraft.server_name
                    await self._send_status_embed_with_retry(
                        self.EMBED_COLOR_BLUE,
                        f"{server_name} is now online!",
                        log_label="Server came online",
                    )
                    self.last_status_notification = now
                    self._offline_announced = False
                else:
                    self.log.info("Server came online - notification skipped (cooldown)")
        else:
            self.consecutive_offline_checks += 1
            self._status_known = True
            self._down_confirmed = True
            if self.first_failed_check is None:
                self.first_failed_check = now
            unreachable_for = now - self.first_failed_check

            # Mark offline once the server has been unreachable for the whole
            # window. Deliberately not a count of checks - see
            # OFFLINE_AFTER_SECONDS. A refused port needs far less patience
            # than a timeout, because it is not ambiguous.
            window = (
                self.OFFLINE_REFUSED_AFTER_SECONDS
                if self._rcon_refused
                else self.OFFLINE_AFTER_SECONDS
            )
            if self.server_online and unreachable_for >= window:
                self.server_online = False
                # Only notify if cooldown has passed
                if now - self.last_status_notification >= self.STATUS_COOLDOWN:
                    server_name = self.config.minecraft.server_name
                    await self._send_status_embed_with_retry(
                        self.EMBED_COLOR_ORANGE,
                        f"{server_name} is restarting...",
                        log_label="Server went offline",
                    )
                    self.last_status_notification = now
                    self._offline_announced = True
                else:
                    self.log.info("Server went offline - notification skipped (cooldown)")

    STATUS_RETRY_DELAY: float = 2.0  # seconds before retrying a failed status webhook

    async def _send_status_embed_with_retry(
        self,
        color: int,
        author_name: str,
        log_label: str,
    ) -> bool:
        """Send a status author-only embed; on False, log a warning and retry once."""
        for attempt in (1, 2):
            try:
                ok = await self.send_webhook_embed(
                    None,
                    color,
                    self.icon_url,
                    author_name,
                )
            except Exception as e:
                self.log.warning(f"{log_label} - webhook send raised on attempt {attempt}: {e}")
                ok = False
            if ok:
                if attempt == 1:
                    self.log.info(f"{log_label} - sent notification")
                else:
                    self.log.info(f"{log_label} - sent notification on retry")
                return True
            if attempt == 1:
                self.log.warning(f"{log_label} - webhook send returned False, retrying in {self.STATUS_RETRY_DELAY}s")
                await asyncio.sleep(self.STATUS_RETRY_DELAY)
        self.log.error(f"{log_label} - webhook send failed after retry")
        return False

    @poll_server_stats.before_loop
    async def before_poll_stats(self) -> None:
        self.poll_server_stats.change_interval(seconds=self.config.settings.stats_check_interval)
        await self.bot.wait_until_ready()

    @poll_server_stats.error
    async def poll_server_stats_error(self, error: BaseException) -> None:
        self.log.error(
            "poll_server_stats loop crashed:\n%s",
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        )
        await asyncio.sleep(10)
        try:
            self.poll_server_stats.restart()
            self.log.info("poll_server_stats loop restarted after crash")
        except Exception as e:
            self.log.error(f"Failed to restart poll_server_stats loop: {e}")

    # Lines that mean the server is dying or gone. Ordered most specific
    # first; the first match wins, so a crash report beats a plain stop.
    # Strings checked against the live logs of both packs, not guessed: the
    # watchdog class is ServerWatchdog on 1.20.1 and 1.21.1 alike (the old
    # ServerHangWatchdog name went away with the MCP mappings), and vanilla
    # logs "Stopping server", never "Stopping the server".
    LOG_CRASH_MARKERS = (
        ("OutOfMemoryError", "ran out of memory"),
        ("ServerWatchdog", "stopped responding (watchdog)"),
        ("Preparing crash report", "crashed"),
        ("This crash report has been saved", "crashed"),
        ("Stopping server", "is stopping"),
    )
    # How long RCON may keep answering after a log warning before the warning
    # is judged wrong. Longer than any save; shorter than a restart.
    LOG_DOWN_GRACE: float = 60.0
    # Only inspect the tail of a burst; a crashing server can emit a very
    # large report and none of it changes the verdict.
    LOG_MAX_LINES_PER_TICK = 400

    def _read_new_log_lines(self) -> list[str]:
        """Return log lines written since the last read.

        Handles rotation: Minecraft replaces latest.log on each boot, so a new
        inode or a file shorter than our offset means start again from zero.
        Blocking file IO, so callers run it in an executor.
        """
        try:
            stat = os.stat(self.log_path)
        except OSError:
            return []

        if self._log_inode is None:
            # First look: start at the end so a restart does not replay history
            self._log_inode = stat.st_ino
            self._log_pos = stat.st_size
            return []

        if stat.st_ino != self._log_inode or stat.st_size < self._log_pos:
            self.log.info("Server log rotated, following the new file")
            self._log_inode = stat.st_ino
            self._log_pos = 0

        if stat.st_size == self._log_pos:
            return []

        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._log_pos)
                data = f.read()
                self._log_pos = f.tell()
        except OSError as e:
            self.log.debug(f"Could not read server log: {e}")
            return []

        return data.splitlines()[-self.LOG_MAX_LINES_PER_TICK:]

    @tasks.loop(seconds=5)
    async def watch_server_log(self) -> None:
        """Announce a crash the moment the server writes about it.

        RCON only reveals a problem once it stops answering, which during the
        2026-08-03 out-of-memory crash took minutes. The log says so
        immediately, and says why.
        """
        if not self.log_path or self._crash_announced:
            return

        loop = asyncio.get_event_loop()
        lines = await loop.run_in_executor(None, self._read_new_log_lines)

        for line in lines:
            for marker, reason in self.LOG_CRASH_MARKERS:
                if marker in line:
                    await self._announce_from_log(reason, line)
                    return

    async def _announce_from_log(self, reason: str, line: str) -> None:
        """Post a crash notice found in the log and suppress the RCON one."""
        self._crash_announced = True
        self._down_confirmed = False
        self._log_down_at = time.time()
        # The log is as good a source of state as a poll, so the recovery that
        # follows counts as a transition rather than a startup observation.
        self._status_known = True
        server_name = self.config.minecraft.server_name
        self.log.warning("Server log reports it %s: %s", reason, line.strip()[:200])

        # Claim the status transition so poll_server_stats does not repeat it
        was_online = self.server_online
        self.server_online = False
        self.first_failed_check = time.time()
        if not was_online:
            return

        await self._send_status_embed_with_retry(
            self.EMBED_COLOR_ORANGE,
            f"{server_name} {reason}",
            log_label="Crash detected in server log",
        )
        self.last_status_notification = time.time()
        self._offline_announced = True

    @watch_server_log.before_loop
    async def before_watch_log(self) -> None:
        await self.bot.wait_until_ready()

    @watch_server_log.error
    async def watch_server_log_error(self, error: BaseException) -> None:
        self.log.error(
            "watch_server_log loop crashed:\n%s",
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        )
        await asyncio.sleep(30)
        try:
            self.watch_server_log.restart()
            self.log.info("watch_server_log loop restarted after crash")
        except Exception as e:
            self.log.error(f"Failed to restart watch_server_log loop: {e}")

    def sanitize_discord_message(self, content: str) -> str:
        """Sanitize Discord message for Minecraft."""
        content = re.sub(r"<@!?(\d+)>", "[mention]", content)
        content = re.sub(r"<#(\d+)>", "[channel]", content)
        content = re.sub(r"<@&(\d+)>", "[role]", content)
        content = re.sub(r"<a?:(\w+):\d+>", r":\1:", content)
        content = content.replace('"', "'")
        content = content.replace("\\", "")
        content = content.replace("\n", " ").replace("\r", " ")
        content = re.sub(r"\s+", " ", content).strip()

        max_len = self.config.settings.max_message_length
        if len(content) > max_len:
            content = content[: max_len - 3] + "..."

        return content

    def sanitize_username(self, username: str) -> str:
        """Sanitize Discord username for Minecraft command."""
        username = re.sub(r"[^\w\s\-_]", "", username)
        username = username[:16]
        username = username.strip()
        if not username:
            username = "Discord"
        return username

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle Discord messages and relay to Minecraft.

        Every bridge sees every message; the channel check below is what makes
        each one only handle its own server.
        """
        if message.author.bot:
            return

        if message.channel.id != self.config.discord.channel_id:
            return

        content = message.content
        if not content:
            if message.attachments:
                content = "[attachment]"
            elif message.stickers:
                content = "[sticker]"
            else:
                return

        content = self.sanitize_discord_message(content)
        if not content:
            return

        username = self.sanitize_username(message.author.display_name)
        command = f'discordmsg "{username}" {content}'

        self.log.info(f"Relaying message from {username}: {content[:50]}...")

        result = await self.send_rcon_command(command)
        if result is None:
            self.log.warning(f"Failed to relay message from {username}")
            try:
                embed = discord.Embed(
                    description=f"**Message was not delivered**\n> {message.content}",
                    color=0xED4245,  # Red
                )
                await message.reply(embed=embed, mention_author=False)
            except discord.Forbidden:
                pass

    TOPIC_BASE_INTERVAL: int = 600  # 10 min — Discord limit is 2 edits / 10 min per channel
    TOPIC_MAX_BACKOFF: int = 3600   # cap retry-after at 1 hour

    @staticmethod
    def _round_uptime(uptime: str) -> str:
        """Round uptime down to nearest 10 minutes to reduce topic churn."""
        import re
        m = re.match(r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m)?", uptime.strip())
        if not m:
            return uptime
        d = int(m.group(1) or 0)
        h = int(m.group(2) or 0)
        mins = (int(m.group(3) or 0) // 10) * 10
        parts = []
        if d: parts.append(f"{d}d")
        if h or d: parts.append(f"{h}h")
        parts.append(f"{mins}m")
        return " ".join(parts)

    @tasks.loop(seconds=600)
    async def update_channel_topic(self) -> None:
        """Update Discord channel topic with server stats."""
        self.log.info(
            f"Topic update tick: last_stats={'set' if self.last_stats else 'None'} "
            f"last_topic={self.last_topic!r}"
        )
        if not self.last_stats:
            self.log.warning("No stats available for topic update (RCON returning nothing)")
            return

        try:
            channel = self.bot.get_channel(self.config.discord.channel_id)
            if not channel:
                self.log.warning("Could not find configured channel")
                return

            if not isinstance(channel, discord.TextChannel):
                self.log.warning("Configured channel is not a text channel")
                return

            tps = self.last_stats.get("tps", 20.0)
            player_count = self.last_stats.get("playerCount", 0)
            uptime = self._round_uptime(self.last_stats.get("uptime", "0h 0m"))

            topic = f"TPS: {tps:.2f} | Players: {player_count} | Uptime: {uptime}"

            if self.last_topic == topic:
                return

            await channel.edit(topic=topic)
            self.last_topic = topic
            self.log.info(f"Updated channel topic: {topic}")
            # Restore base cadence after a successful edit
            if self.update_channel_topic.seconds != self.TOPIC_BASE_INTERVAL:
                self.update_channel_topic.change_interval(seconds=self.TOPIC_BASE_INTERVAL)

        except discord.Forbidden:
            self.log.error("Bot lacks permission to edit channel topic")
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = float(e.response.headers.get("Retry-After", "60"))
                backoff = min(int(retry_after) + 30, self.TOPIC_MAX_BACKOFF)
                self.log.warning(f"Topic edit rate limited. Backing off {backoff}s (retry-after={retry_after}s)")
                self.update_channel_topic.change_interval(seconds=backoff)
                await asyncio.sleep(retry_after)
            else:
                self.log.error(f"HTTP error updating channel topic: {e}")
        except Exception as e:
            self.log.error(f"Error updating channel topic: {e}")

    @update_channel_topic.before_loop
    async def before_update_topic(self) -> None:
        await self.bot.wait_until_ready()

    @update_channel_topic.error
    async def update_channel_topic_error(self, error: BaseException) -> None:
        self.log.error(
            "update_channel_topic loop crashed:\n%s",
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        )
        await asyncio.sleep(30)
        try:
            self.update_channel_topic.restart()
            self.log.info("update_channel_topic loop restarted after crash")
        except Exception as e:
            self.log.error(f"Failed to restart update_channel_topic loop: {e}")

    async def send_players_embed(self, interaction: discord.Interaction) -> None:
        """Show online players with avatars in an embed."""
        if not self.last_stats:
            embed = discord.Embed(
                description="Unable to fetch server data. Server may be offline.",
                color=self.EMBED_COLOR_ORANGE,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not self.server_online:
            embed = discord.Embed(
                description="Server is currently offline or restarting.",
                color=self.EMBED_COLOR_ORANGE,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        players = self.last_stats.get("players", [])
        player_count = self.last_stats.get("playerCount", 0)
        server_name = self.config.minecraft.server_name
        tps = self.last_stats.get("tps", 20.0)
        uptime = self.last_stats.get("uptime", "0h 0m")

        # Build embed
        embed = discord.Embed(
            title=f"Players Online ({player_count})",
            color=self.EMBED_COLOR_GREEN if player_count > 0 else self.EMBED_COLOR_BLUE,
        )
        embed.set_author(name=server_name, icon_url=self.icon_url)
        embed.set_footer(text=f"TPS: {tps:.2f} | Uptime: {uptime}")

        if player_count == 0:
            embed.description = "No players online"
            await interaction.response.send_message(embed=embed)
        else:
            # Build player list
            player_lines = []
            for player in players:
                if isinstance(player, dict):
                    name = player.get("name", "Unknown")
                else:
                    name = str(player)
                player_lines.append(f"• **{name}**")

            embed.description = "\n".join(player_lines)

            # Generate combined avatar image
            avatar_buffer = await self.generate_player_avatars_image(players)

            if avatar_buffer:
                file = discord.File(avatar_buffer, filename="players.png")
                embed.set_image(url="attachment://players.png")
                await interaction.response.send_message(embed=embed, file=file)
            else:
                await interaction.response.send_message(embed=embed)


class ServerCommands(commands.Cog):
    """Global slash commands, added exactly once.

    Lives outside MinecraftBridge because app commands are registered on the
    tree by name - N bridges each carrying a /players would collide. Adding a
    server therefore never requires a slash-command re-sync.
    """

    def __init__(self, bot: "DiscordMCBot"):
        self.bot = bot

    @app_commands.command(
        name="players",
        description="Show online players on this channel's Minecraft server",
    )
    async def players_command(self, interaction: discord.Interaction) -> None:
        bridge = self.bot.bridges.get(interaction.channel_id)
        if bridge is None:
            where = ", ".join(f"<#{cid}>" for cid in self.bot.bridges) or "none configured"
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"Run this in a Minecraft chat channel: {where}",
                    color=MinecraftBridge.EMBED_COLOR_ORANGE,
                ),
                ephemeral=True,
            )
            return
        await bridge.send_players_embed(interaction)


class DiscordMCBot(commands.Bot):
    """Main bot class."""

    def __init__(self, config: BotConfig):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        super().__init__(
            command_prefix="!mc",
            intents=intents,
            help_command=None,
        )

        self.config = config
        self.bridges: dict[int, MinecraftBridge] = {}  # channel_id -> bridge
        self._shutdown_notification_sent = False
        self._startup_notification_sent = False

    async def setup_hook(self) -> None:
        """Called when bot is starting up."""
        for server in self.config.servers:
            name = server.minecraft.server_name
            bridge = None
            try:
                bridge = MinecraftBridge(self, server)
                await self.add_cog(bridge)
            except Exception:
                logger.exception("Bridge for %s failed to load - continuing without it", name)
                if bridge is not None:
                    try:
                        await bridge.cog_unload()
                    except Exception:
                        pass
                continue
            self.bridges[server.discord.channel_id] = bridge
            logger.info("Loaded bridge %s -> channel %s", name, server.discord.channel_id)

        await self.add_cog(ServerCommands(self))

        # Sync slash commands
        try:
            if self.config.guild_id:
                guild = discord.Object(id=self.config.guild_id)
                # Copy commands to guild and sync
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info(f"Synced commands to guild {self.config.guild_id}")
                # Clear stale global commands after guild sync
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                logger.info("Cleared stale global commands")
            else:
                # Global sync (can take up to an hour to propagate)
                await self.tree.sync()
                logger.info("Synced commands globally")
        except discord.Forbidden as e:
            logger.warning(f"Missing access to sync commands: {e.status} {e.code} - {e.text}")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        logger.info("Bot setup complete")

    async def on_ready(self) -> None:
        """Called when bot is connected and ready."""
        logger.info(f"Logged in as {self.user.name} ({self.user.id})")

        names = [b.config.minecraft.server_name for b in self.bridges.values()]
        activity_name = names[0] if len(names) == 1 else f"{len(names)} Minecraft servers"
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=activity_name)
        )

        # Setup webhooks (must be done after bot is ready to access channels)
        for bridge in self.bridges.values():
            try:
                await bridge.setup_webhook()
            except Exception:
                logger.exception("Webhook setup failed for %s", bridge.config.minecraft.server_name)

        # on_ready fires again after a gateway reconnect - only announce once
        if not self._startup_notification_sent:
            self._startup_notification_sent = True
            for bridge in self.bridges.values():
                try:
                    await bridge.send_bot_status(
                        "Discord bot started",
                        MinecraftBridge.EMBED_COLOR_PURPLE,
                    )
                except Exception:
                    logger.exception(
                        "Startup notification failed for %s", bridge.config.minecraft.server_name
                    )
            logger.info("Sent bot startup notifications")

    async def close(self) -> None:
        """Called when bot is shutting down."""
        if not self._shutdown_notification_sent:
            self._shutdown_notification_sent = True
            for bridge in self.bridges.values():
                try:
                    await bridge.send_bot_status(
                        "Discord bot stopped",
                        MinecraftBridge.EMBED_COLOR_RED,
                    )
                except Exception:
                    logger.exception(
                        "Shutdown notification failed for %s", bridge.config.minecraft.server_name
                    )
            logger.info("Sent bot shutdown notifications")
        await super().close()


def main() -> None:
    """Main entry point."""
    config_path = os.environ.get("BOT_CONFIG", "config.toml")
    logger.info(f"Loading configuration from {config_path}...")

    try:
        config = load_config(config_path)
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Copy config.example.toml to config.toml and fill in your values")
        return
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return

    for server in config.servers:
        logger.info(
            "%s: RCON %s:%s -> channel %s",
            server.minecraft.server_name,
            server.minecraft.rcon_host,
            server.minecraft.rcon_port,
            server.discord.channel_id,
        )

    bot = DiscordMCBot(config)

    # Set up signal handlers for graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def shutdown() -> None:
        """Graceful shutdown coroutine."""
        logger.info("Shutting down bot...")
        await bot.close()
        logger.info("Bot shutdown complete")

    def handle_signal(sig: signal.Signals) -> None:
        logger.info(f"Received signal {sig.name}, initiating shutdown...")
        # Stop the bot by cancelling the main task
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    try:
        loop.run_until_complete(bot.start(config.token))
    except asyncio.CancelledError:
        logger.info("Main task cancelled, running shutdown...")
        loop.run_until_complete(shutdown())
    except discord.LoginFailure:
        logger.error("Invalid Discord token")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
