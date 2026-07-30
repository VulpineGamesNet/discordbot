# Minecraft Discord Bot

Two-way Discord ↔ Minecraft bridge. One bot process serves **any number of
Minecraft servers**, each with its own Discord channel, RCON endpoint and MySQL
database.

Currently bridging:

| Server | Modpack | Minecraft | Loader |
|---|---|---|---|
| ATM10 | All The Mods 10 | 1.21.1 | NeoForge |
| DeceasedCraft | DeceasedCraft — Urban Zombie Apocalypse (Beta 5.10.x) | 1.20.1 | Forge |

## How it works

```
Minecraft ──chat/join/leave──> MySQL discord_events ──poll──> bot ──webhook──> Discord
Discord   ──message──────────> bot ──RCON /discordmsg──────> Minecraft
                               bot ──RCON /getstats───────> TPS, players, uptime
```

The Minecraft half is a KubeJS script — see [`minecraft/`](minecraft/). Both
halves live in this repo so they version together.

## Features

Per server:
- Chat relay both directions, with player skin avatars on the Discord side
- Join / leave embeds
- Channel topic showing TPS, player count and uptime
- Server online / restarting notifications (with debounce so a restart isn't spammy)
- `/players` slash command — resolves which server from the channel it was run in

If one server's RCON or database is down, the others keep working.

## Setup

1. **Minecraft side** — install the KubeJS script on each server first, per
   [`minecraft/README.md`](minecraft/README.md). Without it the bot has no
   `getstats` / `discordmsg` to call.

2. **Config**
   ```bash
   cp config.example.toml config.toml   # channels, RCON hosts, server names
   cp .env.example .env                 # token, RCON passwords, database URLs
   ```
   `config.toml` holds one `[[servers]]` block per Minecraft server. Secrets stay
   in `.env` and are pulled in via `${VAR}` placeholders — a missing variable is
   a startup error, never a silent empty password.

3. **Run**
   ```bash
   docker compose up -d --build
   ```
   Look for `Loaded bridge <name> -> channel <id>` per server in the logs.

   Or without Docker: `uv run python bot.py` (set `BOT_CONFIG` to use a config
   path other than `./config.toml`).

## Sending RCON commands

```bash
uv run python rcon.py "Vulpine ATM10" getstats
uv run python rcon.py "Vulpine ATM10" kubejs reload server-scripts
```

Reloading re-runs the KubeJS script bodies, but **does not** rebuild the command
tree — edits to `getstats` / `discordmsg` / `discordstatus` need a full Minecraft
server restart to take effect.

## Adding a server

Append a `[[servers]]` block to `config.toml`, add its two secrets to `.env`,
install the KubeJS script on that server, `docker compose restart`. No code
change and no slash-command re-sync.

## Discord bot permissions

Send Messages, Embed Links, Attach Files, Read Message History, Manage
Webhooks, Manage Channels (for the topic). Enable the **Message Content
Intent** in the Developer Portal. Without Manage Webhooks, create a webhook by
hand and set `webhook_url` in that server's block.

## Development

```bash
uv sync
uv run pytest
```

Requires Python 3.11+ (config parsing uses stdlib `tomllib`).
