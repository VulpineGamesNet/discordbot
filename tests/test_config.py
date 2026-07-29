"""Tests for the TOML configuration loader."""

import pytest

from config import (
    BotConfig,
    DatabaseConfig,
    DiscordConfig,
    MinecraftConfig,
    ServerConfig,
    Settings,
    load_config,
)

TWO_SERVERS = """
[discord]
token = "${TEST_TOKEN}"
guild_id = 987654321

[defaults]
stats_check_interval = 7
offline_threshold = 15
icon_url = "https://example.com/default.png"

[[servers]]
name = "Alpha"
channel_id = 111
rcon_host = "alpha-host"
rcon_port = 25576
rcon_password = "${ALPHA_RCON}"
database_url = "jdbc:mysql://u:p@localhost:3306/alpha"

[[servers]]
name = "Beta"
channel_id = 222
rcon_password = "beta-pass"
database_url = "mysql://u:p@localhost:3306/beta"
offline_threshold = 30
icon_url = "https://example.com/beta.png"
"""


@pytest.fixture
def write_config(tmp_path):
    """Write a TOML string to a temp file and return its path."""

    def _write(text: str) -> str:
        path = tmp_path / "config.toml"
        path.write_text(text)
        return str(path)

    return _write


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "token-from-env")
    monkeypatch.setenv("ALPHA_RCON", "alpha-secret")


class TestLoadConfig:
    """Tests for load_config()."""

    def test_loads_two_servers(self, write_config):
        config = load_config(write_config(TWO_SERVERS), env_file=None)

        assert isinstance(config, BotConfig)
        assert config.token == "token-from-env"
        assert config.guild_id == 987654321
        assert [s.minecraft.server_name for s in config.servers] == ["Alpha", "Beta"]

    def test_expands_env_placeholders(self, write_config):
        config = load_config(write_config(TWO_SERVERS), env_file=None)

        assert config.token == "token-from-env"
        assert config.servers[0].minecraft.rcon_password == "alpha-secret"

    def test_missing_env_var_raises(self, write_config, monkeypatch):
        monkeypatch.delenv("ALPHA_RCON", raising=False)

        with pytest.raises(ValueError, match="ALPHA_RCON"):
            load_config(write_config(TWO_SERVERS), env_file=None)

    def test_defaults_applied_to_every_server(self, write_config):
        config = load_config(write_config(TWO_SERVERS), env_file=None)

        assert config.servers[0].settings.stats_check_interval == 7
        assert config.servers[1].settings.stats_check_interval == 7

    def test_per_server_override_wins(self, write_config):
        config = load_config(write_config(TWO_SERVERS), env_file=None)

        assert config.servers[0].settings.offline_threshold == 15  # from [defaults]
        assert config.servers[1].settings.offline_threshold == 30  # overridden

    def test_unset_setting_falls_back_to_dataclass_default(self, write_config):
        config = load_config(write_config(TWO_SERVERS), env_file=None)

        assert config.servers[0].settings.max_message_length == 256

    def test_icon_url_default_and_override(self, write_config):
        config = load_config(write_config(TWO_SERVERS), env_file=None)

        assert config.servers[0].discord.icon_url == "https://example.com/default.png"
        assert config.servers[1].discord.icon_url == "https://example.com/beta.png"

    def test_connection_details_parsed(self, write_config):
        config = load_config(write_config(TWO_SERVERS), env_file=None)
        alpha, beta = config.servers

        assert alpha.discord.channel_id == 111
        assert alpha.minecraft.rcon_host == "alpha-host"
        assert alpha.minecraft.rcon_port == 25576
        assert alpha.database.url == "jdbc:mysql://u:p@localhost:3306/alpha"
        # Unset host/port fall back to defaults
        assert beta.minecraft.rcon_host == "localhost"
        assert beta.minecraft.rcon_port == 25575

    def test_duplicate_channel_id_raises(self, write_config):
        text = TWO_SERVERS.replace("channel_id = 222", "channel_id = 111")

        with pytest.raises(ValueError, match="share channel_id"):
            load_config(write_config(text), env_file=None)

    def test_missing_servers_raises(self, write_config):
        with pytest.raises(ValueError, match="at least one"):
            load_config(write_config('[discord]\ntoken = "t"\n'), env_file=None)

    def test_missing_token_raises(self, write_config):
        text = '[[servers]]\nname = "A"\nchannel_id = 1\nrcon_password = "p"\ndatabase_url = "u"\n'

        with pytest.raises(ValueError, match="token"):
            load_config(write_config(text), env_file=None)

    def test_missing_required_server_key_raises(self, write_config):
        text = '[discord]\ntoken = "t"\n\n[[servers]]\nname = "A"\nchannel_id = 1\n'

        with pytest.raises(ValueError, match="rcon_password"):
            load_config(write_config(text), env_file=None)

    def test_unnamed_server_raises(self, write_config):
        text = '[discord]\ntoken = "t"\n\n[[servers]]\nchannel_id = 1\n'

        with pytest.raises(ValueError, match="name"):
            load_config(write_config(text), env_file=None)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            load_config(str(tmp_path / "nope.toml"), env_file=None)

    def test_invalid_toml_raises(self, write_config):
        with pytest.raises(ValueError, match="not valid TOML"):
            load_config(write_config("this is not = = toml"), env_file=None)

    def test_stale_env_era_key_raises_naming_it(self, write_config):
        """topic_update_interval was dropped; migrating configs should say so."""
        text = TWO_SERVERS.replace(
            "stats_check_interval = 7", "stats_check_interval = 7\ntopic_update_interval = 60"
        )

        with pytest.raises(TypeError, match="topic_update_interval"):
            load_config(write_config(text), env_file=None)

    def test_guild_id_optional(self, write_config):
        text = TWO_SERVERS.replace("guild_id = 987654321\n", "")

        assert load_config(write_config(text), env_file=None).guild_id is None


class TestDataclasses:
    """Tests for configuration dataclasses."""

    def test_discord_config(self):
        config = DiscordConfig(channel_id=123, icon_url="https://x/y.png")
        assert config.channel_id == 123
        assert config.icon_url == "https://x/y.png"
        assert config.webhook_url is None

    def test_minecraft_config(self):
        config = MinecraftConfig(
            rcon_host="host",
            rcon_port=25575,
            rcon_password="pass",
            server_name="Server",
        )
        assert config.rcon_host == "host"
        assert config.rcon_port == 25575
        assert config.rcon_password == "pass"
        assert config.server_name == "Server"

    def test_settings_defaults(self):
        settings = Settings()
        assert settings.stats_check_interval == 5
        assert settings.max_message_length == 256
        assert settings.events_poll_interval == 2
        assert settings.offline_threshold == 12
        assert settings.status_cooldown == 30

    def test_database_config_defaults(self):
        assert DatabaseConfig().url == ""

    def test_database_config_async_url_jdbc(self):
        config = DatabaseConfig(
            url="jdbc:mysql://testuser:testpass@db.example.com:3307/testdb"
        )
        assert config.async_url == "mysql+asyncmy://testuser:testpass@db.example.com:3307/testdb"

    def test_database_config_async_url_mysql(self):
        config = DatabaseConfig(url="mysql://user:pass@localhost:3306/db")
        assert config.async_url == "mysql+asyncmy://user:pass@localhost:3306/db"

    def test_database_config_async_url_already_asyncmy(self):
        config = DatabaseConfig(url="mysql+asyncmy://user:pass@localhost:3306/db")
        assert config.async_url == "mysql+asyncmy://user:pass@localhost:3306/db"

    def test_server_config_container(self):
        config = ServerConfig(
            discord=DiscordConfig(channel_id=123),
            minecraft=MinecraftConfig(rcon_host="host", rcon_port=25575, rcon_password="pass"),
            database=DatabaseConfig(url="jdbc:mysql://user:pass@localhost:3306/db"),
        )

        assert config.discord.channel_id == 123
        assert config.minecraft.rcon_host == "host"
        assert config.database.url == "jdbc:mysql://user:pass@localhost:3306/db"
        assert config.settings.stats_check_interval == 5

    def test_bot_config_container(self):
        server = ServerConfig(
            discord=DiscordConfig(channel_id=1),
            minecraft=MinecraftConfig(rcon_host="h", rcon_port=1, rcon_password="p"),
            database=DatabaseConfig(url="mysql://u:p@h:3306/d"),
        )
        config = BotConfig(token="t", servers=[server])

        assert config.token == "t"
        assert config.guild_id is None
        assert len(config.servers) == 1
