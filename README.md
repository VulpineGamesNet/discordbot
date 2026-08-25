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
- `@name` typed in game pings that Discord member (`@everyone` and roles never ping)
- Join / leave embeds
- Channel topic showing TPS, player count and uptime
- Server online / restarting notifications (with debounce so a restart isn't spammy)
- DM to the owner when a server stays offline past `down_alert_after` (default
  5 min) and an all-clear when it comes back - see [Down alerts](#down-alerts)
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

The reload argument differs per KubeJS major version — ATM10 (KubeJS 7) wants
`server-scripts` with a hyphen, DeceasedCraft (KubeJS 6) wants `server_scripts`
with an underscore.

Reloading re-runs the KubeJS script bodies, but **does not** rebuild the command
tree — edits to `getstats` / `discordmsg` / `discordstatus` need a full Minecraft
server restart to take effect.

## Down alerts

The channel embeds (`is restarting...` / `is now online`) are for players and
fire on every bounce. The down alert is a different audience: a DM to one
person, sent only when an outage needs a human.

```toml
[discord]
alert_user_id = 123456789012345678   # omit to disable

[defaults]
down_alert_after = 300               # seconds offline before the DM
```

`down_alert_after` is counted **from the moment the server is marked offline**,
not from the first failed RCON check. That is deliberate: wings restarts a
crashed server by itself in roughly three minutes, so the window is a wait for
that automatic recovery to fail. Anchoring on the first failure would spend the
grace on detection instead — a wedged server answers the TCP connect and then
times out, so one check can cost 30s and detection lands well after the outage
began.

- Recovery inside the window: no DM at all, nobody is woken up.
- Recovery after the DM: an all-clear DM follows, with how long it was down.
- One DM per outage, however long it lasts.
- A bot restarted *during* an outage still alerts — the offline mark does not
  depend on having watched the server go down.

The bot can only DM someone who shares a server with it. If the DM fails
(closed DMs, wrong id) it is logged and the bridge keeps running; it is never
retried, because neither cause is fixable at runtime. With no `alert_user_id`
set, the alert is written to the log instead.

## Adding a server

Append a `[[servers]]` block to `config.toml`, add its two secrets to `.env`,
install the KubeJS script on that server, `docker compose restart`. No code
change and no slash-command re-sync.

## Discord bot permissions

Per bridged channel: **View Channel**, Send Messages, Embed Links, Attach
Files, Read Message History, Manage Webhooks, Manage Channels (for the topic).
Enable the **Message Content Intent** in the Developer Portal. Without Manage
Webhooks, create a webhook by hand and set `webhook_url` in that server's block.

**If Minecraft chat reaches Discord but Discord chat never reaches Minecraft,
check View Channel first.** Posting happens through a webhook, which keeps
working without it, so join/leave embeds and relayed chat all look healthy —
but Discord only delivers message events for channels the bot can see, so
`on_message` never fires and the bot logs nothing at all. A total absence of
`Relaying message from ...` lines for one channel is the signature.

## Development

```bash
uv sync
uv run pytest
```

Requires Python 3.11+ (config parsing uses stdlib `tomllib`).
