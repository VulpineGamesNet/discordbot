"""Tests for the Discord bot functionality."""

import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from PIL import Image

from bot import DiscordMCBot, MinecraftBridge, ServerCommands
from config import (
    BotConfig,
    DatabaseConfig,
    DiscordConfig,
    MinecraftConfig,
    ServerConfig,
    Settings,
)
from models import DiscordEvent


def make_server_config(
    name: str = "Test Server",
    channel_id: int = 123456789,
    stats_check_interval: int = 5,
) -> ServerConfig:
    """Build a ServerConfig for tests, varying only what a test cares about."""
    return ServerConfig(
        discord=DiscordConfig(
            channel_id=channel_id,
            webhook_url="https://discord.com/api/webhooks/test",
            icon_url="https://example.com/icon.png",
        ),
        minecraft=MinecraftConfig(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test_password",
            server_name=name,
        ),
        database=DatabaseConfig(
            url="jdbc:mysql://test_user:test_pass@localhost:3306/test_minecraft",
        ),
        settings=Settings(
            stats_check_interval=stats_check_interval,
            max_message_length=256,
            events_poll_interval=2,
        ),
    )


def stub_db(bridge):
    """Replace a bridge's DatabaseManager with a mock."""
    bridge.db_manager = MagicMock()
    bridge.db_manager.is_initialized = True
    bridge.db_manager.initialize = AsyncMock(return_value=True)
    bridge.db_manager.close = AsyncMock()
    bridge.db_manager.get_unprocessed_events = AsyncMock(return_value=[])
    bridge.db_manager.mark_events_processed = AsyncMock(return_value=True)
    return bridge


@pytest.fixture
def config():
    """Create a test configuration for one server."""
    return make_server_config()


@pytest.fixture
def bot_config(config):
    """Create a bot-level configuration wrapping one server."""
    return BotConfig(token="test_token", servers=[config], guild_id=987654321)


@pytest.fixture
def mock_bot(bot_config):
    """Create a mock bot instance."""
    bot = MagicMock(spec=DiscordMCBot)
    bot.config = bot_config
    bot.bridges = {}
    bot.wait_until_ready = AsyncMock()
    bot.get_channel = MagicMock(return_value=None)
    return bot


@pytest.fixture
def bridge(mock_bot, config):
    """Create a MinecraftBridge instance for testing."""
    return stub_db(MinecraftBridge(mock_bot, config))


class TestMessageSanitization:
    """Tests for Discord message sanitization."""

    def test_sanitize_removes_user_mentions(self, bridge):
        """Test that user mentions are replaced."""
        result = bridge.sanitize_discord_message("Hello <@123456789>!")
        assert result == "Hello [mention]!"

    def test_sanitize_removes_user_mentions_with_exclamation(self, bridge):
        """Test that user mentions with ! are replaced."""
        result = bridge.sanitize_discord_message("Hello <@!123456789>!")
        assert result == "Hello [mention]!"

    def test_sanitize_removes_channel_mentions(self, bridge):
        """Test that channel mentions are replaced."""
        result = bridge.sanitize_discord_message("Check out <#123456789>")
        assert result == "Check out [channel]"

    def test_sanitize_removes_role_mentions(self, bridge):
        """Test that role mentions are replaced."""
        result = bridge.sanitize_discord_message("Hey <@&123456789>!")
        assert result == "Hey [role]!"

    def test_sanitize_converts_custom_emojis(self, bridge):
        """Test that custom emojis are converted to text."""
        result = bridge.sanitize_discord_message("Nice <:thumbsup:123456>")
        assert result == "Nice :thumbsup:"

    def test_sanitize_converts_animated_emojis(self, bridge):
        """Test that animated emojis are converted to text."""
        result = bridge.sanitize_discord_message("Cool <a:dance:123456>")
        assert result == "Cool :dance:"

    def test_sanitize_replaces_quotes(self, bridge):
        """Test that double quotes are replaced with single quotes."""
        result = bridge.sanitize_discord_message('He said "hello"')
        assert result == "He said 'hello'"

    def test_sanitize_removes_backslashes(self, bridge):
        """Test that backslashes are removed."""
        result = bridge.sanitize_discord_message("Path\\to\\file")
        assert result == "Pathtofile"

    def test_sanitize_replaces_newlines(self, bridge):
        """Test that newlines are replaced with spaces."""
        result = bridge.sanitize_discord_message("Line1\nLine2\rLine3")
        assert result == "Line1 Line2 Line3"

    def test_sanitize_collapses_multiple_spaces(self, bridge):
        """Test that multiple spaces are collapsed."""
        result = bridge.sanitize_discord_message("Too    many   spaces")
        assert result == "Too many spaces"

    def test_sanitize_truncates_long_messages(self, bridge):
        """Test that long messages are truncated."""
        long_message = "x" * 300
        result = bridge.sanitize_discord_message(long_message)
        assert len(result) == 256
        assert result.endswith("...")

    def test_sanitize_preserves_normal_text(self, bridge):
        """Test that normal text is preserved."""
        result = bridge.sanitize_discord_message("Hello, world!")
        assert result == "Hello, world!"

    def test_sanitize_strips_whitespace(self, bridge):
        """Test that leading/trailing whitespace is stripped."""
        result = bridge.sanitize_discord_message("  Hello  ")
        assert result == "Hello"

    def test_sanitize_complex_message(self, bridge):
        """Test sanitization of a complex message."""
        message = 'Hey <@123> check <#456>! <:emoji:789> said "test"\nNew line'
        result = bridge.sanitize_discord_message(message)
        assert result == "Hey [mention] check [channel]! :emoji: said 'test' New line"


class TestUsernameSanitization:
    """Tests for Discord username sanitization."""

    def test_sanitize_username_normal(self, bridge):
        """Test that normal usernames are preserved."""
        result = bridge.sanitize_username("TestUser")
        assert result == "TestUser"

    def test_sanitize_username_with_spaces(self, bridge):
        """Test that spaces are preserved."""
        result = bridge.sanitize_username("Test User")
        assert result == "Test User"

    def test_sanitize_username_with_special_chars(self, bridge):
        """Test that special characters are removed."""
        result = bridge.sanitize_username("Test@User#123!")
        assert result == "TestUser123"

    def test_sanitize_username_truncates(self, bridge):
        """Test that long usernames are truncated to 16 characters."""
        result = bridge.sanitize_username("VeryLongUsernameHere")
        assert result == "VeryLongUsername"
        assert len(result) == 16

    def test_sanitize_username_empty_returns_default(self, bridge):
        """Test that empty username returns 'Discord'."""
        result = bridge.sanitize_username("")
        assert result == "Discord"

    def test_sanitize_username_only_special_chars(self, bridge):
        """Test that username with only special chars returns default."""
        result = bridge.sanitize_username("@#$%^&*()")
        assert result == "Discord"

    def test_sanitize_username_with_dashes_underscores(self, bridge):
        """Test that dashes and underscores are preserved."""
        result = bridge.sanitize_username("Test-User_123")
        assert result == "Test-User_123"


class TestRCONStats:
    """Tests for RCON-based stats retrieval."""

    @pytest.mark.asyncio
    async def test_get_stats_via_rcon_success(self, bridge):
        """Test successful stats retrieval via RCON."""
        stats_json = json.dumps({
            "tps": 19.5,
            "playerCount": 5,
            "players": ["Player1", "Player2"],
            "uptime": "2h 30m",
            "messages": [],
        })
        with patch.object(bridge, "send_rcon_command", new_callable=AsyncMock, return_value=stats_json):
            result = await bridge.get_stats_via_rcon()

        assert result is not None
        assert result["tps"] == 19.5
        assert result["playerCount"] == 5

    @pytest.mark.asyncio
    async def test_get_stats_via_rcon_failure(self, bridge):
        """Test stats retrieval when RCON fails."""
        with patch.object(bridge, "send_rcon_command", new_callable=AsyncMock, return_value=None):
            result = await bridge.get_stats_via_rcon()

        assert result is None

    @pytest.mark.asyncio
    async def test_get_stats_via_rcon_invalid_json(self, bridge):
        """Test stats retrieval with invalid JSON response."""
        with patch.object(bridge, "send_rcon_command", new_callable=AsyncMock, return_value="not valid json"):
            result = await bridge.get_stats_via_rcon()

        assert result is None


class TestRCON:
    """Tests for RCON functionality."""

    @pytest.mark.asyncio
    async def test_send_rcon_command_success(self, bridge):
        """Test successful RCON command."""
        with patch.object(bridge, "_rcon_sync", return_value="Command executed"):
            result = await bridge.send_rcon_command("test command")

        assert result == "Command executed"

    @pytest.mark.asyncio
    async def test_send_rcon_command_failure(self, bridge):
        """Test RCON command failure."""
        with patch.object(bridge, "_rcon_sync", side_effect=Exception("Connection failed")):
            result = await bridge.send_rcon_command("test command")

        assert result is None


class TestPersistentRCON:
    """Tests for persistent RCON connection functionality."""

    def test_rcon_initial_state(self, bridge):
        """Test that RCON starts disconnected."""
        assert bridge._rcon_socket is None
        assert bridge._rcon_connected is False

    def test_rcon_disconnect_no_socket(self, bridge):
        """Test disconnect when no socket exists."""
        bridge._rcon_disconnect()
        assert bridge._rcon_socket is None
        assert bridge._rcon_connected is False

    def test_rcon_disconnect_with_socket(self, bridge):
        """Test disconnect cleans up socket."""
        mock_socket = MagicMock()
        bridge._rcon_socket = mock_socket
        bridge._rcon_connected = True

        bridge._rcon_disconnect()

        mock_socket.close.assert_called_once()
        assert bridge._rcon_socket is None
        assert bridge._rcon_connected is False

    def test_rcon_disconnect_socket_error(self, bridge):
        """Test disconnect handles socket close error gracefully."""
        mock_socket = MagicMock()
        mock_socket.close.side_effect = OSError("Socket error")
        bridge._rcon_socket = mock_socket
        bridge._rcon_connected = True

        bridge._rcon_disconnect()

        assert bridge._rcon_socket is None
        assert bridge._rcon_connected is False

    def test_rcon_connect_success(self, bridge):
        """Test successful RCON connection."""
        mock_socket = MagicMock()

        with patch("socket.socket", return_value=mock_socket):
            with patch.object(bridge, "_rcon_recv_packet", return_value=(1, 0, "")):
                result = bridge._rcon_connect()

        assert result is True
        assert bridge._rcon_connected is True
        assert bridge._rcon_socket == mock_socket
        mock_socket.connect.assert_called_once()

    def test_rcon_connect_auth_failure(self, bridge):
        """Test RCON connection with authentication failure."""
        mock_socket = MagicMock()

        with patch("socket.socket", return_value=mock_socket):
            with patch.object(bridge, "_rcon_recv_packet", return_value=(-1, 0, "")):
                result = bridge._rcon_connect()

        assert result is False
        assert bridge._rcon_connected is False
        mock_socket.close.assert_called()

    def test_rcon_connect_refused(self, bridge):
        """Test RCON connection refused."""
        mock_socket = MagicMock()
        mock_socket.connect.side_effect = ConnectionRefusedError()

        with patch("socket.socket", return_value=mock_socket):
            result = bridge._rcon_connect()

        assert result is False
        assert bridge._rcon_connected is False

    def test_rcon_connect_timeout(self, bridge):
        """Test RCON connection timeout."""
        import socket as socket_module
        mock_socket = MagicMock()
        mock_socket.connect.side_effect = socket_module.timeout()

        with patch("socket.socket", return_value=mock_socket):
            result = bridge._rcon_connect()

        assert result is False
        assert bridge._rcon_connected is False

    def test_rcon_connect_os_error(self, bridge):
        """Test RCON connection with OS error."""
        mock_socket = MagicMock()
        mock_socket.connect.side_effect = OSError("Network unreachable")

        with patch("socket.socket", return_value=mock_socket):
            result = bridge._rcon_connect()

        assert result is False
        assert bridge._rcon_connected is False

    def test_rcon_sync_connects_if_disconnected(self, bridge):
        """Test that _rcon_sync connects if not connected."""
        mock_socket = MagicMock()

        with patch.object(bridge, "_rcon_connect", return_value=True) as mock_connect:
            bridge._rcon_socket = mock_socket
            bridge._rcon_connected = True
            with patch.object(bridge, "_rcon_recv_packet", return_value=(2, 0, "response")):
                result = bridge._rcon_sync("test")

        assert result == "response"

    def test_rcon_sync_fails_if_cannot_connect(self, bridge):
        """Test that _rcon_sync raises if connection fails."""
        with patch.object(bridge, "_rcon_connect", return_value=False):
            with pytest.raises(ConnectionError, match="Not connected"):
                bridge._rcon_sync("test")

    def test_rcon_sync_disconnects_on_error(self, bridge):
        """Test that _rcon_sync disconnects on socket error."""
        import socket as socket_module
        mock_socket = MagicMock()
        bridge._rcon_socket = mock_socket
        bridge._rcon_connected = True

        with patch.object(bridge, "_rcon_send_packet", side_effect=socket_module.error("Connection reset")):
            with pytest.raises(ConnectionError):
                bridge._rcon_sync("test")

        assert bridge._rcon_connected is False
        assert bridge._rcon_socket is None

    def test_rcon_sync_disconnects_on_broken_pipe(self, bridge):
        """Test that _rcon_sync disconnects on BrokenPipeError."""
        mock_socket = MagicMock()
        bridge._rcon_socket = mock_socket
        bridge._rcon_connected = True

        with patch.object(bridge, "_rcon_send_packet", side_effect=BrokenPipeError()):
            with pytest.raises(ConnectionError):
                bridge._rcon_sync("test")

        assert bridge._rcon_connected is False
        assert bridge._rcon_socket is None

    def test_rcon_sync_auto_reconnect_on_next_call(self, bridge):
        """Test that after disconnect, next call attempts reconnect."""
        bridge._rcon_connected = False
        bridge._rcon_socket = None

        with patch.object(bridge, "_rcon_connect", return_value=False) as mock_connect:
            with pytest.raises(ConnectionError):
                bridge._rcon_sync("test")

        mock_connect.assert_called_once()

    def test_cog_unload_disconnects_rcon(self, bridge):
        """Test that cog_unload disconnects RCON and closes database."""
        mock_socket = MagicMock()
        bridge._rcon_socket = mock_socket
        bridge._rcon_connected = True
        bridge.http_session = MagicMock()
        bridge.http_session.close = AsyncMock()
        bridge.poll_server_stats = MagicMock()
        bridge.update_channel_topic = MagicMock()
        bridge.poll_discord_events = MagicMock()
        bridge.poll_discord_events.is_running.return_value = True

        import asyncio
        asyncio.get_event_loop().run_until_complete(bridge.cog_unload())

        assert bridge._rcon_connected is False
        bridge.db_manager.close.assert_called_once()

    def test_polling_uses_config_interval(self, bridge):
        """Test that polling interval is set from config."""
        bridge.config.settings.stats_check_interval = 10

        import asyncio
        asyncio.get_event_loop().run_until_complete(bridge.before_poll_stats())

        assert bridge.poll_server_stats.seconds == 10


class TestMessageHandler:
    """Tests for Discord message handling."""

    @pytest.fixture
    def mock_message(self, config):
        """Create a mock Discord message."""
        message = MagicMock(spec=discord.Message)
        message.author = MagicMock()
        message.author.bot = False
        message.author.display_name = "TestUser"
        message.channel = MagicMock()
        message.channel.id = config.discord.channel_id
        message.content = "Hello from Discord!"
        message.attachments = []
        message.stickers = []
        message.add_reaction = AsyncMock()
        return message

    @pytest.mark.asyncio
    async def test_on_message_ignores_bots(self, bridge, mock_message):
        """Test that bot messages are ignored."""
        mock_message.author.bot = True

        with patch.object(bridge, "send_rcon_command", new_callable=AsyncMock) as mock_rcon:
            await bridge.on_message(mock_message)

        mock_rcon.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_message_ignores_wrong_channel(self, bridge, mock_message):
        """Test that messages from wrong channel are ignored."""
        mock_message.channel.id = 999999999  # Different channel

        with patch.object(bridge, "send_rcon_command", new_callable=AsyncMock) as mock_rcon:
            await bridge.on_message(mock_message)

        mock_rcon.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_message_relays_to_minecraft(self, bridge, mock_message):
        """Test that valid messages are relayed to Minecraft."""
        with patch.object(
            bridge, "send_rcon_command", new_callable=AsyncMock, return_value="OK"
        ) as mock_rcon:
            await bridge.on_message(mock_message)

        mock_rcon.assert_called_once()
        call_args = mock_rcon.call_args[0][0]
        assert "discordmsg" in call_args
        assert "TestUser" in call_args
        assert "Hello from Discord!" in call_args

    @pytest.mark.asyncio
    async def test_on_message_handles_attachments(self, bridge, mock_message):
        """Test that attachment-only messages are handled."""
        mock_message.content = ""
        mock_message.attachments = [MagicMock()]

        with patch.object(
            bridge, "send_rcon_command", new_callable=AsyncMock, return_value="OK"
        ) as mock_rcon:
            await bridge.on_message(mock_message)

        mock_rcon.assert_called_once()
        call_args = mock_rcon.call_args[0][0]
        assert "[attachment]" in call_args

    @pytest.mark.asyncio
    async def test_on_message_handles_stickers(self, bridge, mock_message):
        """Test that sticker-only messages are handled."""
        mock_message.content = ""
        mock_message.stickers = [MagicMock()]

        with patch.object(
            bridge, "send_rcon_command", new_callable=AsyncMock, return_value="OK"
        ) as mock_rcon:
            await bridge.on_message(mock_message)

        mock_rcon.assert_called_once()
        call_args = mock_rcon.call_args[0][0]
        assert "[sticker]" in call_args

    @pytest.mark.asyncio
    async def test_on_message_ignores_empty(self, bridge, mock_message):
        """Test that empty messages without attachments are ignored."""
        mock_message.content = ""
        mock_message.attachments = []
        mock_message.stickers = []

        with patch.object(bridge, "send_rcon_command", new_callable=AsyncMock) as mock_rcon:
            await bridge.on_message(mock_message)

        mock_rcon.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_message_no_reaction_on_success(self, bridge, mock_message):
        """Test that no reaction is added on successful relay."""
        with patch.object(
            bridge, "send_rcon_command", new_callable=AsyncMock, return_value="OK"
        ):
            await bridge.on_message(mock_message)

        mock_message.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_message_replies_on_failure(self, bridge, mock_message):
        """Test that failure reply is sent when relay fails."""
        mock_message.reply = AsyncMock()
        with patch.object(
            bridge, "send_rcon_command", new_callable=AsyncMock, return_value=None
        ):
            await bridge.on_message(mock_message)

        mock_message.reply.assert_called_once()
        call_kwargs = mock_message.reply.call_args.kwargs
        assert call_kwargs["mention_author"] is False
        embed = call_kwargs["embed"]
        assert "not delivered" in embed.description.lower()


class TestChannelTopicUpdate:
    """Tests for channel topic update functionality."""

    @pytest.mark.asyncio
    async def test_update_topic_no_stats(self, bridge):
        """Test that topic update is skipped when no stats available."""
        bridge.last_stats = None

        await bridge.update_channel_topic()

        bridge.bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_topic_channel_not_found(self, bridge):
        """Test handling when channel is not found."""
        bridge.last_stats = {"tps": 20.0, "playerCount": 5, "uptime": "1h 0m"}
        bridge.bot.get_channel.return_value = None

        # Should not raise
        await bridge.update_channel_topic()

    @pytest.mark.asyncio
    async def test_update_topic_formats_correctly(self, bridge):
        """Test that topic is formatted correctly."""
        bridge.last_stats = {
            "tps": 19.85,
            "playerCount": 42,
            "uptime": "21h 1m",
        }
        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.topic = None
        mock_channel.edit = AsyncMock()
        bridge.bot.get_channel.return_value = mock_channel

        await bridge.update_channel_topic()

        mock_channel.edit.assert_called_once()
        topic = mock_channel.edit.call_args.kwargs["topic"]
        assert topic == "TPS: 19.85 | Players: 42 | Uptime: 21h 0m"  # uptime floored to 10m by _round_uptime

    @pytest.mark.asyncio
    async def test_update_topic_skips_if_unchanged(self, bridge):
        """Test that topic update is skipped if topic hasn't changed."""
        bridge.last_stats = {"tps": 20.0, "playerCount": 5, "uptime": "1h 0m"}
        bridge.last_topic = "TPS: 20.00 | Players: 5 | Uptime: 1h 0m"
        mock_channel = MagicMock(spec=discord.TextChannel)
        mock_channel.edit = AsyncMock()
        bridge.bot.get_channel.return_value = mock_channel

        await bridge.update_channel_topic()

        mock_channel.edit.assert_not_called()


class TestServerStatusDetection:
    """Tests for server start/stop detection."""

    @pytest.mark.asyncio
    async def test_server_comes_online(self, bridge):
        """Test detection when server comes online."""
        bridge.server_online = False
        stats = {"tps": 20.0, "playerCount": 0, "messages": []}

        with patch.object(bridge, "get_stats_via_rcon", new_callable=AsyncMock, return_value=stats):
            with patch.object(bridge, "send_webhook_embed", new_callable=AsyncMock) as mock_embed:
                await bridge.poll_server_stats()

        assert bridge.server_online is True
        mock_embed.assert_called_once()
        # author_name is the 4th argument (index 3)
        assert "online" in mock_embed.call_args[0][3].lower()

    @pytest.mark.asyncio
    async def test_server_goes_offline(self, bridge):
        """Test detection when server goes offline after threshold consecutive failures."""
        bridge.server_online = True

        with patch.object(bridge, "get_stats_via_rcon", new_callable=AsyncMock, return_value=None):
            with patch.object(bridge, "send_webhook_embed", new_callable=AsyncMock) as mock_embed:
                # Need OFFLINE_THRESHOLD consecutive failures to trigger offline
                for _ in range(bridge.OFFLINE_THRESHOLD):
                    await bridge.poll_server_stats()

        assert bridge.server_online is False
        mock_embed.assert_called_once()
        # author_name is the 4th argument (index 3)
        assert "restarting" in mock_embed.call_args[0][3].lower()

    @pytest.mark.asyncio
    async def test_server_stays_online(self, bridge):
        """Test no notification when server stays online."""
        bridge.server_online = True
        stats = {"tps": 20.0, "playerCount": 0, "messages": []}

        with patch.object(bridge, "get_stats_via_rcon", new_callable=AsyncMock, return_value=stats):
            with patch.object(bridge, "send_webhook_embed", new_callable=AsyncMock) as mock_embed:
                await bridge.poll_server_stats()

        assert bridge.server_online is True
        mock_embed.assert_not_called()


class TestEventProcessing:
    """Tests for processing events from database."""

    def _create_event(self, event_type: str, player_name: str, player_uuid: str, message: str = None) -> DiscordEvent:
        """Helper to create a mock DiscordEvent."""
        event = MagicMock(spec=DiscordEvent)
        event.id = 1
        event.event_type = event_type
        event.player_name = player_name
        event.player_uuid = player_uuid
        event.message = message
        return event

    @pytest.mark.asyncio
    async def test_process_chat_event(self, bridge):
        """Test processing a chat event."""
        events = [self._create_event("chat", "Steve", "abc-123", "Hello!")]

        with patch.object(bridge, "send_webhook_message", new_callable=AsyncMock) as mock_webhook:
            await bridge.process_events(events)

        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][0] == "Hello!"
        assert mock_webhook.call_args[0][1] == "Steve"

    @pytest.mark.asyncio
    async def test_process_join_event(self, bridge):
        """Test processing a join event."""
        events = [self._create_event("join", "Steve", "abc-123")]

        with patch.object(bridge, "send_webhook_embed", new_callable=AsyncMock) as mock_embed:
            await bridge.process_events(events)

        mock_embed.assert_called_once()
        args = mock_embed.call_args[0]
        assert args[0] is None  # no description
        assert "Steve logged in" in args[3]  # author_name

    @pytest.mark.asyncio
    async def test_process_leave_event(self, bridge):
        """Test processing a leave event."""
        events = [self._create_event("leave", "Steve", "abc-123")]

        with patch.object(bridge, "send_webhook_embed", new_callable=AsyncMock) as mock_embed:
            await bridge.process_events(events)

        mock_embed.assert_called_once()
        args = mock_embed.call_args[0]
        assert args[0] is None  # no description
        assert "Steve logged out" in args[3]  # author_name


class TestDatabasePolling:
    """Tests for database event polling."""

    @pytest.mark.asyncio
    async def test_poll_discord_events_no_events(self, bridge):
        """Test polling when no events available."""
        bridge.db_manager.get_unprocessed_events.return_value = []

        with patch.object(bridge, "process_events", new_callable=AsyncMock) as mock_process:
            await bridge.poll_discord_events()

        mock_process.assert_not_called()
        bridge.db_manager.mark_events_processed.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_discord_events_with_events(self, bridge):
        """Test polling and processing events."""
        mock_event = MagicMock(spec=DiscordEvent)
        mock_event.id = 1
        mock_event.event_type = "chat"
        mock_event.player_name = "Steve"
        mock_event.player_uuid = "abc-123"
        mock_event.message = "Hello!"

        bridge.db_manager.get_unprocessed_events.return_value = [mock_event]

        with patch.object(bridge, "process_events", new_callable=AsyncMock) as mock_process:
            await bridge.poll_discord_events()

        mock_process.assert_called_once_with([mock_event])
        bridge.db_manager.mark_events_processed.assert_called_once_with([1])

    @pytest.mark.asyncio
    async def test_poll_discord_events_db_not_initialized(self, bridge):
        """Test polling when database not initialized."""
        bridge.db_manager.is_initialized = False

        with patch.object(bridge, "process_events", new_callable=AsyncMock) as mock_process:
            await bridge.poll_discord_events()

        mock_process.assert_not_called()
        bridge.db_manager.get_unprocessed_events.assert_not_called()


class TestDiscordMCBot:
    """Tests for the main bot class."""

    def test_bot_initialization(self, bot_config):
        """Test bot initialization with config."""
        bot = DiscordMCBot(bot_config)

        assert bot.config == bot_config
        assert bot.command_prefix == "!mc"
        assert bot.bridges == {}

    def test_bot_intents(self, bot_config):
        """Test that bot has correct intents."""
        bot = DiscordMCBot(bot_config)

        assert bot.intents.message_content is True
        assert bot.intents.guilds is True


class TestPlayerAvatarsImage:
    """Tests for player avatars image generation."""

    @pytest.mark.asyncio
    async def test_generate_avatars_empty_list(self, bridge):
        """Test that empty player list returns None."""
        bridge.http_session = MagicMock()
        result = await bridge.generate_player_avatars_image([])
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_avatars_no_session(self, bridge):
        """Test that missing http_session returns None."""
        bridge.http_session = None
        result = await bridge.generate_player_avatars_image([{"name": "Test", "uuid": "abc"}])
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_avatars_no_uuid(self, bridge):
        """Test that players without UUID are skipped."""
        bridge.http_session = MagicMock()
        result = await bridge.generate_player_avatars_image([{"name": "Test"}])
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_avatars_string_format(self, bridge):
        """Test that old string format players are skipped."""
        bridge.http_session = MagicMock()
        result = await bridge.generate_player_avatars_image(["Player1", "Player2"])
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_avatars_success(self, bridge):
        """Test successful avatar image generation."""
        # Create a test image
        test_image = Image.new('RGBA', (32, 32), (255, 0, 0, 255))
        img_buffer = io.BytesIO()
        test_image.save(img_buffer, format='PNG')
        img_bytes = img_buffer.getvalue()

        # Mock HTTP session
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=img_bytes)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))

        bridge.http_session = mock_session

        players = [
            {"name": "Player1", "uuid": "uuid-1"},
            {"name": "Player2", "uuid": "uuid-2"},
        ]

        result = await bridge.generate_player_avatars_image(players)

        assert result is not None
        assert isinstance(result, io.BytesIO)

        # Verify the image can be opened
        result.seek(0)
        combined_image = Image.open(result)
        assert combined_image.width == 32 * 2 + 4  # 2 avatars + 1 padding (single row)
        assert combined_image.height == 32  # single row

    @pytest.mark.asyncio
    async def test_generate_avatars_http_error(self, bridge):
        """Test handling of HTTP errors during avatar fetch."""
        mock_response = AsyncMock()
        mock_response.status = 404

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))

        bridge.http_session = mock_session

        players = [{"name": "Player1", "uuid": "uuid-1"}]
        result = await bridge.generate_player_avatars_image(players)

        assert result is None

    @pytest.mark.asyncio
    async def test_generate_avatars_limits_to_20(self, bridge):
        """Test that avatar generation is limited to 20 players with 5 per row."""
        test_image = Image.new('RGBA', (32, 32), (255, 0, 0, 255))
        img_buffer = io.BytesIO()
        test_image.save(img_buffer, format='PNG')
        img_bytes = img_buffer.getvalue()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=img_bytes)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))

        bridge.http_session = mock_session

        # Create 25 players
        players = [{"name": f"Player{i}", "uuid": f"uuid-{i}"} for i in range(25)]

        result = await bridge.generate_player_avatars_image(players)

        assert result is not None
        result.seek(0)
        combined_image = Image.open(result)
        # Should only have 20 avatars in 4 rows of 5
        # Width: 5 * 32 + 4 * 4 = 176
        # Height: 4 * 32 + 3 * 4 = 140
        assert combined_image.width == 5 * 32 + 4 * 4
        assert combined_image.height == 4 * 32 + 3 * 4

    @pytest.mark.asyncio
    async def test_generate_avatars_multiple_rows(self, bridge):
        """Test that 7 players creates 2 rows (5 + 2)."""
        test_image = Image.new('RGBA', (32, 32), (255, 0, 0, 255))
        img_buffer = io.BytesIO()
        test_image.save(img_buffer, format='PNG')
        img_bytes = img_buffer.getvalue()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=img_bytes)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))

        bridge.http_session = mock_session

        players = [{"name": f"Player{i}", "uuid": f"uuid-{i}"} for i in range(7)]

        result = await bridge.generate_player_avatars_image(players)

        assert result is not None
        result.seek(0)
        combined_image = Image.open(result)
        # 7 players = 2 rows (5 + 2), width based on 5 columns
        assert combined_image.width == 5 * 32 + 4 * 4  # 176
        assert combined_image.height == 2 * 32 + 1 * 4  # 68


class TestPlayersCommand:
    """Tests for /players slash command."""

    @pytest.mark.asyncio
    async def test_players_command_no_stats(self, bridge):
        """Test /players when no stats available."""
        bridge.last_stats = None

        mock_interaction = MagicMock()
        mock_interaction.response = MagicMock()
        mock_interaction.response.send_message = AsyncMock()

        # Call the underlying callback directly
        await bridge.send_players_embed(mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        call_kwargs = mock_interaction.response.send_message.call_args.kwargs
        assert call_kwargs["ephemeral"] is True
        assert "offline" in call_kwargs["embed"].description.lower()

    @pytest.mark.asyncio
    async def test_players_command_server_offline(self, bridge):
        """Test /players when server is offline."""
        bridge.last_stats = {"tps": 20.0}
        bridge.server_online = False

        mock_interaction = MagicMock()
        mock_interaction.response = MagicMock()
        mock_interaction.response.send_message = AsyncMock()

        await bridge.send_players_embed(mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        call_kwargs = mock_interaction.response.send_message.call_args.kwargs
        assert call_kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_players_command_no_players(self, bridge):
        """Test /players when no players online."""
        bridge.last_stats = {
            "tps": 20.0,
            "playerCount": 0,
            "players": [],
            "uptime": "1h 0m",
        }
        bridge.server_online = True

        mock_interaction = MagicMock()
        mock_interaction.response = MagicMock()
        mock_interaction.response.send_message = AsyncMock()

        await bridge.send_players_embed(mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        call_kwargs = mock_interaction.response.send_message.call_args.kwargs
        assert "ephemeral" not in call_kwargs or call_kwargs.get("ephemeral") is not True
        assert "no players" in call_kwargs["embed"].description.lower()

    @pytest.mark.asyncio
    async def test_players_command_with_players(self, bridge):
        """Test /players with online players."""
        bridge.last_stats = {
            "tps": 19.5,
            "playerCount": 2,
            "players": [
                {"name": "Player1", "uuid": "uuid-1"},
                {"name": "Player2", "uuid": "uuid-2"},
            ],
            "uptime": "2h 30m",
        }
        bridge.server_online = True

        mock_interaction = MagicMock()
        mock_interaction.response = MagicMock()
        mock_interaction.response.send_message = AsyncMock()

        # Mock image generation to return None (no image)
        with patch.object(bridge, "generate_player_avatars_image", new_callable=AsyncMock, return_value=None):
            await bridge.send_players_embed(mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        call_kwargs = mock_interaction.response.send_message.call_args.kwargs
        embed = call_kwargs["embed"]
        assert "Players Online (2)" in embed.title
        assert "Player1" in embed.description
        assert "Player2" in embed.description

    @pytest.mark.asyncio
    async def test_players_command_with_image(self, bridge):
        """Test /players with avatar image."""
        bridge.last_stats = {
            "tps": 20.0,
            "playerCount": 1,
            "players": [{"name": "Player1", "uuid": "uuid-1"}],
            "uptime": "1h 0m",
        }
        bridge.server_online = True

        mock_interaction = MagicMock()
        mock_interaction.response = MagicMock()
        mock_interaction.response.send_message = AsyncMock()

        # Mock image generation to return a buffer
        mock_buffer = io.BytesIO(b"fake image data")
        with patch.object(bridge, "generate_player_avatars_image", new_callable=AsyncMock, return_value=mock_buffer):
            await bridge.send_players_embed(mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        call_kwargs = mock_interaction.response.send_message.call_args.kwargs
        assert "file" in call_kwargs
        assert call_kwargs["embed"].image.url == "attachment://players.png"


class TestMultipleBridges:
    """The load-bearing assertions for running N servers on one bot."""

    def test_two_bridges_have_unique_cog_names(self, mock_bot):
        """Cog names must differ, or the second add_cog raises ClientException."""
        a = MinecraftBridge(mock_bot, make_server_config("Alpha", channel_id=1))
        b = MinecraftBridge(mock_bot, make_server_config("Beta", channel_id=2))

        assert a.qualified_name != b.qualified_name
        assert "Alpha" in a.qualified_name
        assert "Beta" in b.qualified_name

    @pytest.mark.asyncio
    async def test_two_bridges_have_independent_loops(self, mock_bot):
        """Each cog instance must get its own tasks.Loop, with its own interval.

        discord.py's Loop.__get__ returns a per-instance copy; if that ever
        changed, both bridges would share one poll loop and one server would
        silently stop being monitored.
        """
        a = MinecraftBridge(mock_bot, make_server_config("Alpha", 1, stats_check_interval=5))
        b = MinecraftBridge(mock_bot, make_server_config("Beta", 2, stats_check_interval=30))

        assert a.poll_server_stats is not b.poll_server_stats
        assert a.poll_discord_events is not b.poll_discord_events
        assert a.update_channel_topic is not b.update_channel_topic

        await a.before_poll_stats()
        await b.before_poll_stats()

        assert a.poll_server_stats.seconds == 5
        assert b.poll_server_stats.seconds == 30

    def test_bridges_do_not_share_state(self, mock_bot):
        """Per-server state must not leak between bridges."""
        a = MinecraftBridge(mock_bot, make_server_config("Alpha", channel_id=1))
        b = MinecraftBridge(mock_bot, make_server_config("Beta", channel_id=2))

        a.last_stats = {"tps": 19.0}
        a.server_online = True
        a.consecutive_offline_checks = 7

        assert b.last_stats is None
        assert b.server_online is False
        assert b.consecutive_offline_checks == 0
        assert a.rcon_lock is not b.rcon_lock

    @pytest.mark.asyncio
    async def test_add_two_bridges_to_real_bot(self, bot_config):
        """Two bridges must coexist on a real Bot - catches cog/command collisions."""
        bot = DiscordMCBot(bot_config)

        a = stub_db(MinecraftBridge(bot, make_server_config("Alpha", channel_id=1)))
        b = stub_db(MinecraftBridge(bot, make_server_config("Beta", channel_id=2)))
        a.db_manager.initialize = AsyncMock(return_value=False)
        b.db_manager.initialize = AsyncMock(return_value=False)

        try:
            await bot.add_cog(a)
            await bot.add_cog(b)
            await bot.add_cog(ServerCommands(bot))

            assert bot.get_cog(a.qualified_name) is a
            assert bot.get_cog(b.qualified_name) is b
            # Exactly one /players in the tree regardless of server count
            assert [c.name for c in bot.tree.get_commands()] == ["players"]
        finally:
            await bot.remove_cog(a.qualified_name)
            await bot.remove_cog(b.qualified_name)
            await bot.close()

    @pytest.mark.asyncio
    async def test_setup_hook_survives_failing_server(self):
        """A server that fails to construct must not stop the others loading."""
        config = BotConfig(
            token="test_token",
            servers=[
                make_server_config("Good", channel_id=1),
                make_server_config("Broken", channel_id=2),
            ],
        )
        bot = DiscordMCBot(config)

        good_db = MagicMock()
        good_db.initialize = AsyncMock(return_value=False)
        good_db.close = AsyncMock()

        with patch("bot.DatabaseManager", side_effect=[good_db, RuntimeError("bad url")]), \
             patch.object(bot.tree, "sync", new_callable=AsyncMock):
            try:
                await bot.setup_hook()

                assert list(bot.bridges) == [1]
                assert bot.bridges[1].config.minecraft.server_name == "Good"
                assert bot.get_cog("ServerCommands") is not None
            finally:
                await bot.remove_cog(bot.bridges[1].qualified_name)
                await bot.close()


class TestServerCommands:
    """/players resolves its target server from the invoking channel."""

    @pytest.mark.asyncio
    async def test_players_resolves_bridge_by_channel(self, mock_bot):
        a = MinecraftBridge(mock_bot, make_server_config("Alpha", channel_id=1))
        b = MinecraftBridge(mock_bot, make_server_config("Beta", channel_id=2))
        a.send_players_embed = AsyncMock()
        b.send_players_embed = AsyncMock()
        mock_bot.bridges = {1: a, 2: b}

        cog = ServerCommands(mock_bot)
        interaction = MagicMock()
        interaction.channel_id = 2

        await cog.players_command.callback(cog, interaction)

        b.send_players_embed.assert_awaited_once_with(interaction)
        a.send_players_embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_players_unknown_channel_replies_ephemeral(self, mock_bot):
        bridge = MinecraftBridge(mock_bot, make_server_config("Alpha", channel_id=1))
        bridge.send_players_embed = AsyncMock()
        mock_bot.bridges = {1: bridge}

        cog = ServerCommands(mock_bot)
        interaction = MagicMock()
        interaction.channel_id = 999
        interaction.response.send_message = AsyncMock()

        await cog.players_command.callback(cog, interaction)

        bridge.send_players_embed.assert_not_awaited()
        kwargs = interaction.response.send_message.call_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "<#1>" in kwargs["embed"].description
