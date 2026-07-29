// Discord Chat Sync - Minecraft <-> Discord Integration
// Target: Minecraft 1.20.1 / Forge / KubeJS 2001.6.x
//
// Minecraft -> Discord: events written to the MySQL discord_events table, polled by the bot
// Discord -> Minecraft: via RCON /discordmsg, called by the bot
//
// Everything is wrapped in an IIFE because all server_scripts/*.js share one
// global scope - the modpack's own scripts must not collide with these names.

;(function () {

  // ==========================================================================
  // CONFIGURATION - kubejs/config/discord.json
  // ==========================================================================

  let discordDbConfig = {}
  try {
    discordDbConfig = JsonIO.read('kubejs/config/discord.json') || {}
  } catch (e) {
    console.warn('[DiscordChat] Could not load config file, using defaults: ' + e)
  }

  const DISCORD_CONFIG = {
    // Enable/disable features
    enableChatSync: true,
    enableJoinLeave: true,

    // Reported by /getstats; must match the server's `name` in the bot's config.toml
    serverName: String(discordDbConfig.server_name || 'Minecraft Server'),

    // Maximum message length
    maxMessageLength: 1900
  }

  const DISCORD_DB_HOST = discordDbConfig.host || 'localhost'
  const DISCORD_DB_PORT = parseInt(discordDbConfig.port || '3306')
  const DISCORD_DB_NAME = discordDbConfig.database || 'minecraft'
  const DISCORD_DB_USER = discordDbConfig.user || 'root'
  const DISCORD_DB_PASS = discordDbConfig.password || ''

  // ==========================================================================
  // MYSQL CONNECTION
  // ==========================================================================

  // Requires MySQL Connector/J on the server classpath - neither ATM10 nor
  // DeceasedCraft ship it. See minecraft/README.md.
  let DiscordMysqlDriver = Java.loadClass('com.mysql.cj.jdbc.Driver')
  let discordMysqlDriver = new DiscordMysqlDriver()
  let DiscordSqlTypes = Java.loadClass('java.sql.Types')

  let discordDatabaseAvailable = false

  function getDiscordConnection() {
    let url = 'jdbc:mysql://' + DISCORD_DB_HOST + ':' + DISCORD_DB_PORT + '/' + DISCORD_DB_NAME +
      '?user=' + encodeURIComponent(DISCORD_DB_USER) +
      '&password=' + encodeURIComponent(DISCORD_DB_PASS) +
      '&autoReconnect=true'
    return discordMysqlDriver.connect(url, null)
  }

  function closeDiscordQuietly(resource) {
    if (resource) {
      try { resource.close() } catch(e) {}
    }
  }

  function initDiscordDatabase() {
    let conn = null
    let stmt = null
    try {
      console.info('[DiscordChat] Connecting to database at ' + DISCORD_DB_HOST + ':' + DISCORD_DB_PORT + '/' + DISCORD_DB_NAME + '...')
      conn = getDiscordConnection()
      stmt = conn.createStatement()

      // Create discord_events table
      stmt.executeUpdate(
        'CREATE TABLE IF NOT EXISTS discord_events (' +
        '  id BIGINT AUTO_INCREMENT PRIMARY KEY,' +
        '  event_type VARCHAR(20) NOT NULL,' +
        '  player_name VARCHAR(64) NOT NULL,' +
        '  player_uuid VARCHAR(36) NOT NULL,' +
        '  message TEXT,' +
        '  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,' +
        '  processed_at TIMESTAMP NULL,' +
        '  INDEX idx_unprocessed (processed_at, created_at)' +
        ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
      )

      discordDatabaseAvailable = true
      console.info('[DiscordChat] Database table initialized successfully')
    } catch(e) {
      discordDatabaseAvailable = false
      console.error('[DiscordChat] ========================================')
      console.error('[DiscordChat] FAILED TO CONNECT TO DATABASE!')
      console.error('[DiscordChat] Error: ' + e)
      console.error('[DiscordChat] Host: ' + DISCORD_DB_HOST + ':' + DISCORD_DB_PORT + '/' + DISCORD_DB_NAME)
      console.error('[DiscordChat] User: ' + DISCORD_DB_USER)
      console.error('[DiscordChat] Discord chat sync features will be DISABLED')
      console.error('[DiscordChat] ========================================')
    } finally {
      closeDiscordQuietly(stmt)
      closeDiscordQuietly(conn)
    }
  }

  // Initialize database on script load
  initDiscordDatabase()

  // ==========================================================================
  // STATE VARIABLES
  // ==========================================================================

  let serverStartTime = Date.now()

  // ==========================================================================
  // UTILITY FUNCTIONS
  // ==========================================================================

  function escapeJson(str) {
    if (!str) return ""
    let result = ""
    for (let i = 0; i < str.length; i++) {
      let c = str.charAt(i)
      if (c === '"') result += '\\"'
      else if (c === '\\') result += '\\\\'
      else if (c === '\n') result += '\\n'
      else if (c === '\r') result += '\\r'
      else if (c === '\t') result += '\\t'
      else result += c
    }
    return result
  }

  function stripColorCodes(str) {
    if (!str) return ""
    let result = ""
    let i = 0
    while (i < str.length) {
      let c = str.charAt(i)
      if (c === '\u00A7' && i + 1 < str.length) {
        i += 2
      } else {
        result += c
        i++
      }
    }
    return result
  }

  function truncateMessage(str, maxLen) {
    if (!str) return ""
    if (str.length <= maxLen) return str
    return str.substring(0, maxLen - 3) + "..."
  }

  // ==========================================================================
  // SERVER STATS FUNCTIONS
  // ==========================================================================

  function getUptime() {
    let uptimeMs = Date.now() - serverStartTime
    let seconds = Math.floor(uptimeMs / 1000)
    let minutes = Math.floor(seconds / 60)
    let hours = Math.floor(minutes / 60)
    let days = Math.floor(hours / 24)

    hours = hours % 24
    minutes = minutes % 60

    if (days > 0) {
      return days + "d " + hours + "h " + minutes + "m"
    } else {
      return hours + "h " + minutes + "m"
    }
  }

  function getTPS(server) {
    try {
      // 1.20.1 still has getAverageTickTime(); it was removed in 1.20.5+.
      let avgTickMs = server.getAverageTickTime()
      let tps = Math.min(20.0, 1000.0 / avgTickMs)
      return Math.round(tps * 100) / 100
    } catch (e) {
      return 20.00
    }
  }

  function getPlayerCount(server) {
    try {
      let count = 0
      server.getPlayers().forEach(function (p) {
        count++
      })
      return count
    } catch (e) {
      return 0
    }
  }

  function getPlayerList(server) {
    let players = []
    try {
      server.getPlayers().forEach(function (p) {
        players.push({
          name: String(p.getName().getString()),
          uuid: String(p.getStringUuid())
        })
      })
    } catch (e) {}
    return players
  }

  // Build JSON response for /getstats command (chat lives in MySQL, not here)
  function buildStatsJson(server) {
    let tps = getTPS(server)
    let playerCount = getPlayerCount(server)
    let players = getPlayerList(server)
    let uptime = getUptime()

    // Build players array JSON (objects with name and uuid)
    let playersJson = '['
    for (let i = 0; i < players.length; i++) {
      if (i > 0) playersJson += ','
      playersJson += '{"name":"' + escapeJson(players[i].name) + '","uuid":"' + escapeJson(players[i].uuid) + '"}'
    }
    playersJson += ']'

    let jsonStr = '{'
    jsonStr += '"tps":' + tps + ','
    jsonStr += '"playerCount":' + playerCount + ','
    jsonStr += '"players":' + playersJson + ','
    jsonStr += '"uptime":"' + escapeJson(uptime) + '",'
    jsonStr += '"serverName":"' + escapeJson(DISCORD_CONFIG.serverName) + '"'
    jsonStr += '}'

    return jsonStr
  }

  // ==========================================================================
  // DATABASE EVENT INSERTION
  // ==========================================================================

  function insertEvent(eventType, playerName, playerUuid, message) {
    if (!discordDatabaseAvailable) {
      console.warn('[DiscordChat] Database not available, event not recorded')
      return false
    }

    let conn = null
    let stmt = null
    try {
      conn = getDiscordConnection()
      stmt = conn.prepareStatement(
        'INSERT INTO discord_events (event_type, player_name, player_uuid, message) VALUES (?, ?, ?, ?)'
      )
      stmt.setString(1, eventType)
      stmt.setString(2, playerName)
      stmt.setString(3, playerUuid)
      if (message) {
        stmt.setString(4, message)
      } else {
        stmt.setNull(4, DiscordSqlTypes.VARCHAR)
      }
      stmt.executeUpdate()
      console.info('[DiscordChat] Event inserted: type=' + eventType + ', player=' + playerName)
      return true
    } catch(e) {
      console.error('[DiscordChat] Failed to insert event: ' + e)
      return false
    } finally {
      closeDiscordQuietly(stmt)
      closeDiscordQuietly(conn)
    }
  }

  // ==========================================================================
  // EVENT HANDLERS
  // ==========================================================================

  ServerEvents.loaded(event => {
    serverStartTime = Date.now()
    console.info("[DiscordChat] Discord chat sync initialized")
  })

  // Player chat messages.
  // Never `return false` here - the event has a result, and a false result is
  // translated into setCanceled(true), which kills chat server-wide.
  PlayerEvents.chat(event => {
    if (!DISCORD_CONFIG.enableChatSync) {
      return
    }

    let messageStr = stripColorCodes(String(event.message || ''))
    messageStr = truncateMessage(messageStr, DISCORD_CONFIG.maxMessageLength)

    if (!messageStr || messageStr.length === 0) {
      return
    }

    let player = event.player
    insertEvent("chat", player.getName().getString(), player.getStringUuid(), messageStr)
  })

  // Player join
  PlayerEvents.loggedIn(event => {
    if (!DISCORD_CONFIG.enableJoinLeave) return

    let player = event.player
    insertEvent("join", player.getName().getString(), player.getStringUuid(), null)
  })

  // Player leave
  PlayerEvents.loggedOut(event => {
    if (!DISCORD_CONFIG.enableJoinLeave) return

    let player = event.player
    insertEvent("leave", player.getName().getString(), player.getStringUuid(), null)
  })

  // ==========================================================================
  // COMMANDS
  // ==========================================================================

  ServerEvents.commandRegistry(event => {
    let Commands = event.getCommands()
    let Arguments = event.getArguments()

    // /getstats - server stats as JSON, read off the RCON response by the bot
    event.register(
      Commands.literal("getstats")
        .requires(function (src) {
          // Only allow from console/RCON (permission level 4)
          return src.hasPermission(4)
        })
        .executes(function (ctx) {
          let server = ctx.getSource().getServer()
          let jsonResponse = buildStatsJson(server)

          // Return JSON as command feedback
          ctx.getSource().sendSystemMessage(Component.literal(jsonResponse))

          return 1
        })
    )

    // /discordmsg <username> <message> - relay a message from Discord to Minecraft
    event.register(
      Commands.literal("discordmsg")
        .requires(function (src) {
          return src.hasPermission(4)
        })
        .then(
          Commands.argument("username", Arguments.STRING.create(event))
            .then(
              Commands.argument("message", Arguments.GREEDY_STRING.create(event))
                .executes(function (ctx) {
                  let username = Arguments.STRING.getResult(ctx, "username")
                  let message = Arguments.GREEDY_STRING.getResult(ctx, "message")
                  let server = ctx.getSource().getServer()

                  let chatMsg = Component.empty()
                    .append(Component.gray("["))
                    .append(Component.blue("Discord"))
                    .append(Component.gray("] "))
                    .append(Component.aqua(username))
                    .append(Component.white(": "))
                    .append(Component.white(message))

                  server.getPlayers().forEach(function (player) {
                    player.sendSystemMessage(chatMsg)
                  })

                  console.info("[Discord] " + username + ": " + message)
                  return 1
                })
            )
        )
    )

    // /discordstatus - check Discord integration status
    event.register(
      Commands.literal("discordstatus")
        .requires(function (src) {
          return src.hasPermission(2)
        })
        .executes(function (ctx) {
          let src = ctx.getSource()

          src.sendSystemMessage(Component.gold("=== Discord Chat Status ==="))
          src.sendSystemMessage(
            Component.yellow("Server: ").append(Component.white(DISCORD_CONFIG.serverName))
          )
          src.sendSystemMessage(
            Component.yellow("Chat Sync: ").append(
              DISCORD_CONFIG.enableChatSync ? Component.green("Enabled") : Component.red("Disabled")
            )
          )
          src.sendSystemMessage(
            Component.yellow("Join/Leave: ").append(
              DISCORD_CONFIG.enableJoinLeave ? Component.green("Enabled") : Component.red("Disabled")
            )
          )
          src.sendSystemMessage(
            Component.yellow("Database: ").append(
              discordDatabaseAvailable ? Component.green("Connected") : Component.red("Disconnected")
            )
          )
          src.sendSystemMessage(
            Component.yellow("Uptime: ").append(
              Component.white(getUptime())
            )
          )

          return 1
        })
    )
  })

  console.info("[DiscordChat] Discord chat sync script loaded (" + DISCORD_CONFIG.serverName + ")")

})()
