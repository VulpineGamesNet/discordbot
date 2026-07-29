# Minecraft side

The KubeJS half of the bridge. It writes chat / join / leave into a MySQL
`discord_events` table (which the bot polls), and registers the RCON commands
the bot calls: `getstats` and `discordmsg`.

Ships alongside the bot so both halves version together.

## Which folder

| Folder | Minecraft | Loader | KubeJS | Used by |
|---|---|---|---|---|
| `1.21.1-neoforge/` | 1.21.1 | NeoForge | `2101.7.x` | ATM10 |
| `1.20.1-forge/` | 1.20.1 | Forge | `2001.6.x` | DeceasedCraft Beta 5.10.x |

The two scripts are identical except for the TPS call (`getAverageTickTime()`
was removed in 1.20.5+).

## Prerequisites

**1. KubeJS**
```bash
ls /path/to/server/mods | grep -i kubejs
```
Expect `kubejs-neoforge-2101.7.x` or `kubejs-forge-2001.6.x`. Both packs bundle it.

**2. MySQL Connector/J — the #1 cause of this script failing to load.**

Neither ATM10 nor DeceasedCraft ships it. Check:
```bash
grep -rl 'com/mysql/cj/jdbc/Driver' /path/to/server --include='*.jar'
```
Nothing found? Confirm at runtime — drop `kubejs/server_scripts/zz_mysql_probe.js`:
```js
console.info('[Probe] mysql driver = ' + Java.tryLoadClass('com.mysql.cj.jdbc.Driver'))
```
run `/kubejs reload server_scripts`, read the log (`null` = missing), then delete the file.

If missing, install [Minecraft MySQL JDBC](https://modrinth.com/mod/minecraft-mysql-jdbc)
into `mods/` — one jar covers Forge 1.20.1 and NeoForge 1.21.x, with
`com.mysql.cj.*` left unrelocated.

Do **not** drop a bare Oracle connector jar into `mods/` — Forge rejects jars
without `META-INF/mods.toml`.

**3. A database per server.** Never share one between servers — the
`discord_events` table has no server column by design.
```sql
CREATE DATABASE deceasedcraft CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mc_deceasedcraft'@'%' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON deceasedcraft.* TO 'mc_deceasedcraft'@'%';
FLUSH PRIVILEGES;
```
The `discord_events` table itself is created automatically on script load, so
the user needs `CREATE` on that schema.

## Install

**Merge into the existing `kubejs/` folder — do not replace it.** The modpack
has its own scripts in there. The trailing `/.` is what makes `cp` merge:

```bash
cp -r minecraft/1.20.1-forge/kubejs/. /path/to/server/kubejs/
cp /path/to/server/kubejs/config/discord.example.json \
   /path/to/server/kubejs/config/discord.json
```

Then edit `kubejs/config/discord.json`:

```json
{
  "host": "127.0.0.1",
  "port": 3306,
  "database": "deceasedcraft",
  "user": "mc_deceasedcraft",
  "password": "change-me",
  "server_name": "DeceasedCraft"
}
```

`server_name` is what `/getstats` reports — keep it matching the server's
`name` in the bot's `config.toml`.

## `server.properties`

```properties
enable-rcon=true
rcon.port=25575
rcon.password=<long-random-password>
broadcast-rcon-to-ops=false
```

RCON must be reachable from wherever the bot runs — the bot polls `getstats`
every few seconds.

## Apply and verify

```
/kubejs reload server_scripts
/discordstatus
```

Expect `Database: Connected`, and this in the server log:
```
[DiscordChat] Database table initialized successfully
```

## Commands

| Command | Permission | Purpose |
|---|---|---|
| `getstats` | 4 (console/RCON) | TPS, players, uptime as JSON |
| `discordmsg <user> <message>` | 4 (console/RCON) | Broadcast a Discord message in game |
| `discordstatus` | 2 (op) | Health readout |

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Failed to load Java class 'com.mysql.cj.jdbc.Driver'` | Connector/J missing — see Prerequisites |
| `FAILED TO CONNECT TO DATABASE` block in the log | Wrong host/credentials, or the user lacks grants on that schema |
| Chat reaches Discord but not the reverse | RCON unreachable from the bot, or wrong `rcon_password` in `.env` |
| Nothing in either direction | `/discordstatus` first; if the database is fine, check the bot logs for that server's name |
| Messages arrive with a rank prefix (`[Survivor] `) | The pack decorates chat components. `stripColorCodes` removes `§` codes but not literal text — strip it in `PlayerEvents.chat` if it shows up |
