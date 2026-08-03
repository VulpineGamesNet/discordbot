"""Configuration loader for the Discord-Minecraft bridge bot.

Structural config lives in a TOML file (see config.example.toml). Secrets are
kept out of it via ``${VAR}`` placeholders that are expanded from the
environment (a ``.env`` file is loaded first, if present).
"""

import os
import tomllib
from dataclasses import dataclass, field, fields
from string import Template
from typing import Any, Optional

from dotenv import load_dotenv


@dataclass
class DiscordConfig:
    """Discord-side configuration for a single Minecraft server."""

    channel_id: int
    webhook_url: Optional[str] = None
    icon_url: str = ""


@dataclass
class MinecraftConfig:
    """Minecraft server configuration."""

    rcon_host: str
    rcon_port: int
    rcon_password: str
    server_name: str = "Minecraft Server"
    # Optional path to the server's latest.log, as seen from inside this
    # container. When set, the bot watches it for crash markers, which surface
    # a crash the moment it is written rather than waiting for RCON to fail -
    # and carry the reason with them. Mount the log *directory*, not the file:
    # a single-file bind mount pins the inode, so the container would keep
    # reading the pre-rotation file forever.
    log_path: str = ""


@dataclass
class DatabaseConfig:
    """Database configuration for MySQL."""

    url: str = ""

    @property
    def async_url(self) -> str:
        """Get SQLAlchemy async connection URL."""
        # Convert jdbc:mysql:// to mysql+asyncmy://
        url = self.url
        if url.startswith("jdbc:mysql://"):
            url = url.replace("jdbc:mysql://", "mysql+asyncmy://", 1)
        elif url.startswith("mysql://"):
            url = url.replace("mysql://", "mysql+asyncmy://", 1)
        return url


@dataclass
class Settings:
    """Per-server tunables. Defaults come from the TOML [defaults] table."""

    stats_check_interval: int = 5  # seconds between RCON getstats polls
    max_message_length: int = 256  # truncation before relaying to Minecraft
    events_poll_interval: int = 2  # seconds between discord_events DB polls
    offline_threshold: int = 12  # consecutive failed stats checks before marking offline
    status_cooldown: int = 30  # seconds between online/offline status notifications


@dataclass
class ServerConfig:
    """Everything the bot needs to bridge one Minecraft server."""

    discord: DiscordConfig
    minecraft: MinecraftConfig
    database: DatabaseConfig
    settings: Settings = field(default_factory=Settings)


@dataclass
class BotConfig:
    """Bot-global configuration plus the list of bridged servers."""

    token: str
    servers: list[ServerConfig]
    guild_id: Optional[int] = None


# Keys of Settings, used to split per-server tunables from connection details.
_SETTINGS_FIELDS = {f.name for f in fields(Settings)}


def _expand(value: Any) -> Any:
    """Recursively expand ``${VAR}`` placeholders from the environment.

    A missing variable is a hard error - silently substituting an empty string
    would produce an empty RCON password or a broken database URL.
    """
    if isinstance(value, str):
        try:
            return Template(value).substitute(os.environ)
        except KeyError as e:
            raise ValueError(
                f"config references ${{{e.args[0]}}} but that environment variable is not set"
            ) from None
        except ValueError as e:
            raise ValueError(f"invalid placeholder in config value {value!r}: {e}") from None
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _require(raw: dict, key: str, name: str) -> Any:
    """Fetch a required key from a [[servers]] table or raise."""
    value = raw.get(key)
    if value in (None, ""):
        raise ValueError(f"server '{name}': '{key}' is required")
    return value


def _server(raw: dict, defaults: dict) -> ServerConfig:
    """Build a ServerConfig from one [[servers]] table."""
    name = raw.get("name")
    if not name:
        raise ValueError("every [[servers]] entry needs a 'name'")

    overrides = {k: raw[k] for k in _SETTINGS_FIELDS & raw.keys()}
    settings = Settings(**{**defaults, **overrides})

    return ServerConfig(
        discord=DiscordConfig(
            channel_id=int(_require(raw, "channel_id", name)),
            webhook_url=raw.get("webhook_url") or None,
            icon_url=raw.get("icon_url", ""),
        ),
        minecraft=MinecraftConfig(
            rcon_host=raw.get("rcon_host", "localhost"),
            rcon_port=int(raw.get("rcon_port", 25575)),
            rcon_password=_require(raw, "rcon_password", name),
            server_name=name,
            log_path=raw.get("log_path", ""),
        ),
        database=DatabaseConfig(url=_require(raw, "database_url", name)),
        settings=settings,
    )


def load_config(path: str = "config.toml", env_file: Optional[str] = ".env") -> BotConfig:
    """
    Load configuration from a TOML file, expanding ``${VAR}`` from the environment.

    Args:
        path: Path to the TOML config file
        env_file: Optional .env file loaded before expansion (env wins over file)

    Returns:
        BotConfig with one ServerConfig per [[servers]] entry

    Raises:
        ValueError: If the config is missing, malformed, or incomplete
    """
    if env_file:
        load_dotenv(env_file)

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ValueError(
            f"config file '{path}' not found - copy config.example.toml and fill it in"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"config file '{path}' is not valid TOML: {e}") from None

    raw = _expand(raw)

    discord = raw.get("discord", {})
    token = discord.get("token")
    if not token:
        raise ValueError("[discord].token is required")

    # icon_url is a per-server display value, not a Settings field, so it is
    # pulled out before the rest of [defaults] is passed to Settings(**...).
    # An unknown key left in there raises TypeError naming the key, which is
    # exactly the error someone migrating from the old .env wants to see.
    defaults = dict(raw.get("defaults", {}))
    icon_default = defaults.pop("icon_url", "")

    servers_raw = raw.get("servers") or []
    if not servers_raw:
        raise ValueError("at least one [[servers]] entry is required")

    servers = []
    for entry in servers_raw:
        entry.setdefault("icon_url", icon_default)
        servers.append(_server(entry, defaults))

    seen: dict[int, str] = {}
    for server in servers:
        channel_id = server.discord.channel_id
        if channel_id in seen:
            raise ValueError(
                f"servers '{seen[channel_id]}' and '{server.minecraft.server_name}' "
                f"share channel_id {channel_id} - every server needs its own channel"
            )
        seen[channel_id] = server.minecraft.server_name

    return BotConfig(
        token=token,
        servers=servers,
        guild_id=int(discord["guild_id"]) if discord.get("guild_id") else None,
    )
