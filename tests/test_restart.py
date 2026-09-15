"""Tests for the /restart module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot import MinecraftBridge
from config import BotConfig, PterodactylConfig
from restart import RestartCommands, _duration
from tests.test_bot import make_server_config, stub_db

BOT_USER_ID = 99


def make_bot(*bridges):
    bot = MagicMock()
    bot.config = BotConfig(
        token="t",
        servers=[b.config for b in bridges],
        pterodactyl=PterodactylConfig(url="https://panel.test", api_key="k"),
    )
    bot.bridges = {b.config.discord.channel_id: b for b in bridges}
    bot.user.id = BOT_USER_ID
    return bot


def make_bridge(bot_stub, name="Vulpine ATM10", channel_id=1):
    bridge = stub_db(MinecraftBridge(bot_stub, make_server_config(name, channel_id=channel_id)))
    bridge.send_rcon_command = AsyncMock(return_value="")
    bridge.send_webhook_embed = AsyncMock(return_value=True)
    bridge.dm_owner = AsyncMock()
    return bridge


class FakePtero:
    """Scripted panel: `states` is consumed one per state() call, the last
    value repeats forever. Every power signal is recorded."""

    def __init__(self, states):
        self.states = list(states)
        self.signals: list[str] = []

    async def state(self, server):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def power(self, server, signal):
        self.signals.append(signal)


@pytest.fixture
def cog():
    bot_stub = MagicMock()
    bot_stub.wait_until_ready = AsyncMock()
    bridge = make_bridge(bot_stub)
    bot = make_bot(bridge)
    c = RestartCommands(bot)
    c.POLL = 0
    c.ptero = FakePtero(["running"])
    return c


@pytest.fixture
def bridge(cog):
    return next(iter(cog.bot.bridges.values()))


def make_interaction(channel_id=1, owner=False, roles=()):
    i = MagicMock()
    i.channel_id = channel_id
    i.guild = MagicMock()
    i.guild.owner_id = 42
    i.user = MagicMock(spec=discord.Member)
    i.user.id = 42 if owner else 7
    i.user.display_name = "Fox"
    i.user.roles = []
    for name in roles:
        r = MagicMock()
        r.name = name
        i.user.roles.append(r)
    i.response.send_message = AsyncMock()
    i.response.is_done = MagicMock(return_value=False)

    async def defer(**kwargs):  # after a defer only followups are allowed
        i.response.is_done.return_value = True

    i.response.defer = AsyncMock(side_effect=defer)
    i.followup.send = AsyncMock()
    return i


def sent_text(interaction) -> str:
    """Text of the one ephemeral reply, wherever the interaction state put it."""
    calls = interaction.followup.send.call_args_list + interaction.response.send_message.call_args_list
    assert len(calls) == 1, calls
    kwargs = calls[0].kwargs
    assert kwargs["ephemeral"] is True
    return kwargs["embed"].description


def said(bridge) -> list[str]:
    """Plain text of every tellraw broadcast, prefix stripped."""
    out = []
    for call in bridge.send_rcon_command.call_args_list:
        cmd = call.args[0]
        if cmd.startswith("tellraw @a "):
            parts = json.loads(cmd[len("tellraw @a "):])
            out.append("".join(p["text"] for p in parts if p)[len("[Server] "):])
    return out


class TestAccess:
    def test_role_name_derives_from_server_name(self, cog, bridge):
        assert cog.role_name(bridge) == "Vulpine ATM10 Admin"

    def test_owner_bypasses_role(self, cog, bridge):
        assert cog.allowed(make_interaction(owner=True), bridge)

    def test_role_holder_allowed(self, cog, bridge):
        assert cog.allowed(make_interaction(roles=["Vulpine ATM10 Admin"]), bridge)

    def test_other_server_role_denied(self, cog, bridge):
        assert not cog.allowed(make_interaction(roles=["Vulpine DC Admin"]), bridge)

    @pytest.mark.asyncio
    async def test_denied_user_gets_ephemeral_and_no_job(self, cog, bridge):
        i = make_interaction()
        await cog.restart_command.callback(cog, i, 5)
        assert "Vulpine ATM10 Admin" in sent_text(i)
        assert cog.jobs == {}

    @pytest.mark.asyncio
    async def test_unbridged_channel_rejected(self, cog):
        i = make_interaction(channel_id=999, owner=True)
        await cog.restart_command.callback(cog, i, 5)
        assert "<#1>" in sent_text(i)


class TestScheduling:
    @pytest.mark.asyncio
    async def test_schedule_announces_and_dms(self, cog, bridge):
        i = make_interaction(owner=True)
        with patch.object(cog, "_run", new=AsyncMock()):
            await cog.restart_command.callback(cog, i, 3)
        assert 1 in cog.jobs
        assert "3 minutes" in sent_text(i)
        assert "Fox" in bridge.send_webhook_embed.call_args.args[0]
        dm = bridge.dm_owner.call_args.args[0]
        assert "Fox" in dm and "Vulpine ATM10" in dm and "3 minutes" in dm
        cog.jobs[1].task.cancel()

    @pytest.mark.asyncio
    async def test_second_restart_rejected(self, cog, bridge):
        with patch.object(cog, "_run", new=AsyncMock()):
            await cog.restart_command.callback(cog, make_interaction(owner=True), 5)
            second = make_interaction(owner=True)
            await cog.restart_command.callback(cog, second, 5)
        assert "/restart-cancel" in sent_text(second)
        assert len(cog.jobs) == 1
        cog.jobs[1].task.cancel()

    @pytest.mark.asyncio
    async def test_panel_silence_rejected_before_countdown(self, cog, bridge):
        """A wrong id or key must surface here, not after five minutes of
        players being told the server is going down."""
        cog.ptero = FakePtero([None])
        i = make_interaction(owner=True)
        await cog.restart_command.callback(cog, i, 5)
        assert "panel did not answer" in sent_text(i)
        assert cog.jobs == {}
        assert bridge.send_rcon_command.await_count == 0

    @pytest.mark.asyncio
    async def test_not_running_rejected(self, cog, bridge):
        cog.ptero = FakePtero(["offline"])
        i = make_interaction(owner=True)
        await cog.restart_command.callback(cog, i, 5)
        assert "is offline" in sent_text(i)
        assert cog.jobs == {}

    @pytest.mark.asyncio
    async def test_cancel_before_signal(self, cog, bridge):
        with patch.object(cog, "_run", new=AsyncMock()):
            await cog.restart_command.callback(cog, make_interaction(owner=True), 5)
        i = make_interaction(owner=True)
        await cog.cancel_command.callback(cog, i)
        assert cog.jobs == {}
        assert "cancelled" in sent_text(i)
        assert said(bridge)[-1] == "Server restart has been cancelled"

    @pytest.mark.asyncio
    async def test_cancel_after_signal_too_late(self, cog, bridge):
        with patch.object(cog, "_run", new=AsyncMock()):
            await cog.restart_command.callback(cog, make_interaction(owner=True), 5)
        cog.jobs[1].signalled = True
        i = make_interaction(owner=True)
        await cog.cancel_command.callback(cog, i)
        assert "Too late" in sent_text(i)
        assert 1 in cog.jobs
        cog.jobs[1].task.cancel()

    @pytest.mark.asyncio
    async def test_cancel_nothing_scheduled(self, cog):
        i = make_interaction(owner=True)
        await cog.cancel_command.callback(cog, i)
        assert "No restart" in sent_text(i)


class TestCountdown:
    @pytest.mark.asyncio
    async def test_one_minute_sequence(self, cog, bridge):
        with patch("restart.asyncio.sleep", new=AsyncMock()):
            await cog._countdown(bridge, 1)
        assert said(bridge) == [
            "Server restart scheduled in 1 minute",
            "Restarting in 30 seconds",
            "10", "9", "8", "7", "6", "5", "4", "3", "2", "1",
            "Restarting now!",
        ]

    @pytest.mark.asyncio
    async def test_five_minutes_skips_the_duplicate_five(self, cog, bridge):
        with patch("restart.asyncio.sleep", new=AsyncMock()):
            await cog._countdown(bridge, 5)
        msgs = said(bridge)
        assert msgs[0] == "Server restart scheduled in 5 minutes"
        assert msgs[1] == "Restarting in 2 minutes"
        assert "Restarting in 5 minutes" not in msgs

    @pytest.mark.asyncio
    async def test_tellraw_is_valid_json_with_neutral_root(self, cog, bridge):
        await cog._say(bridge, ("Restarting in ", "yellow"), ("1 minute", "red"))
        cmd = bridge.send_rcon_command.call_args.args[0]
        parts = json.loads(cmd.removeprefix("tellraw @a "))
        assert parts[0] == ""
        assert parts[1] == {"text": "[Server] ", "color": "red", "bold": True}
        assert parts[2]["bold"] is False and parts[3]["bold"] is True


class TestBounce:
    """The watch after the restart signal, with a scripted panel."""

    @pytest.mark.asyncio
    async def test_happy_path(self, cog, bridge):
        cog.ptero = FakePtero(["running", "stopping", "offline", "starting", "running"])
        bridge.send_rcon_command = AsyncMock(side_effect=[None, None, "players"])
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart"]
        dm = bridge.dm_owner.call_args.args[0]
        assert "back up" in dm

    @pytest.mark.asyncio
    async def test_saving_server_is_not_back_up(self, cog, bridge):
        """2026-09-14: DC reported stopping and still answered RCON, and the
        watch called that 'back up' 150ms after the signal. RCON must not
        even be asked until the panel says the old process is gone."""
        cog.ptero = FakePtero(["stopping", "stopping", "offline", "running"])

        async def rcon(cmd):
            assert cog.ptero.states == ["running"], "RCON polled while still stopping"
            return "ok"

        bridge.send_rcon_command = AsyncMock(side_effect=rcon)
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart"]
        assert "back up" in bridge.dm_owner.call_args.args[0]

    @pytest.mark.asyncio
    async def test_fast_boot_between_polls_counts_as_down(self, cog, bridge):
        """stopping then running again without an offline/starting poll in
        between is a server that restarted quickly, not one that never stopped."""
        cog.ptero = FakePtero(["stopping", "running"])
        bridge.send_rcon_command = AsyncMock(return_value="ok")
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart"]

    @pytest.mark.asyncio
    async def test_kill_when_wings_never_acts(self, cog, bridge):
        cog.STOP_TIMEOUT = 0
        cog.ptero = FakePtero(["running", "running", "offline", "starting"])
        bridge.send_rcon_command = AsyncMock(side_effect=[None, None, "ok"])
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart", "kill"]

    @pytest.mark.asyncio
    async def test_slow_save_is_not_killed(self, cog, bridge):
        """Once the panel says stopping, the stop is wings' to finish: the
        short STOP_TIMEOUT no longer applies, a save must never be killed."""
        cog.STOP_TIMEOUT = 0
        cog.STOPPING_TIMEOUT = 60
        cog.ptero = FakePtero(["stopping", "stopping", "stopping", "offline", "running"])
        bridge.send_rcon_command = AsyncMock(return_value="ok")
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart"]

    @pytest.mark.asyncio
    async def test_stuck_stopping_is_killed_after_wings_limit(self, cog, bridge):
        cog.STOP_TIMEOUT = cog.STOPPING_TIMEOUT = 0
        cog.ptero = FakePtero(["stopping", "stopping", "offline", "running"])
        bridge.send_rcon_command = AsyncMock(return_value="ok")
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart", "kill"]

    @pytest.mark.asyncio
    async def test_give_up_after_kill(self, cog, bridge):
        cog.STOP_TIMEOUT = cog.KILL_TIMEOUT = 0
        cog.ptero = FakePtero(["running"])
        bridge.send_rcon_command = AsyncMock(return_value="ok")
        with pytest.raises(RuntimeError, match="after kill"):
            await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart", "kill"]

    @pytest.mark.asyncio
    async def test_start_when_wings_leaves_it_offline(self, cog, bridge):
        n = cog.OFFLINE_POLLS_BEFORE_START
        # one offline poll ends the going-down phase, n more trigger the start
        cog.ptero = FakePtero(["offline"] * (n + 1) + ["starting", "running"])
        bridge.send_rcon_command = AsyncMock(side_effect=[None] * (n + 2) + ["ok"])
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart", "start"]

    @pytest.mark.asyncio
    async def test_long_boot_is_left_alone(self, cog, bridge):
        """A pack that takes longer than START_TIMEOUT to boot is still
        booting, not stuck: no start signal, no failure, just patience."""
        cog.START_TIMEOUT = 0
        cog.ptero = FakePtero(["offline", "starting", "starting", "starting", "running"])
        bridge.send_rcon_command = AsyncMock(side_effect=[None, None, None, "ok"])
        await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart"]

    @pytest.mark.asyncio
    async def test_boot_that_never_ends_fails(self, cog, bridge):
        cog.BOOT_TIMEOUT = 0
        cog.ptero = FakePtero(["offline", "starting"])
        bridge.send_rcon_command = AsyncMock(return_value=None)
        with pytest.raises(RuntimeError, match="still starting"):
            await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart"]

    @pytest.mark.asyncio
    async def test_give_up_after_second_start(self, cog, bridge):
        cog.START_TIMEOUT = 0
        cog.ptero = FakePtero(["offline"])
        bridge.send_rcon_command = AsyncMock(return_value=None)
        with pytest.raises(RuntimeError, match="second start"):
            await cog._bounce(bridge)
        assert cog.ptero.signals == ["restart", "start"]

    @pytest.mark.asyncio
    async def test_failure_is_dmed_and_job_cleared(self, cog, bridge):
        cog.ptero = MagicMock()
        cog.ptero.power = AsyncMock(side_effect=RuntimeError("HTTP 401"))
        job = MagicMock(minutes=1)
        cog.jobs[1] = job
        with patch.object(cog, "_countdown", new=AsyncMock()):
            await cog._run(bridge, job)
        assert job.signalled is True
        assert "HTTP 401" in bridge.dm_owner.call_args.args[0]
        assert cog.jobs == {}


class TestRoleSync:
    def _guild(self, *roles, audit=(), audit_forbidden=False):
        """roles: (name, permission bits). audit: (role name, creator id)."""
        guild = MagicMock()
        guild.name = "Vulpine"
        guild.me.top_role = MagicMock()
        guild.roles = []
        by_name = {}
        for name, perms in roles:
            r = MagicMock(spec=discord.Role)
            r.id = 1000 + len(guild.roles)
            r.name = name
            r.managed = False
            r.hoist = False
            r.permissions = MagicMock(value=perms)
            r.guild = guild
            r.__lt__ = lambda self, other: True
            r.delete = AsyncMock()
            guild.roles.append(r)
            by_name[name] = r
        guild.create_role = AsyncMock()

        entries = []
        for name, creator in audit:
            e = MagicMock()
            e.target.id = by_name[name].id
            e.user.id = creator
            entries.append(e)

        def audit_logs(**kwargs):
            async def gen():
                if audit_forbidden:
                    raise discord.Forbidden(MagicMock(status=403), "no")
                for e in entries:
                    yield e

            return gen()

        guild.audit_logs = audit_logs
        return guild

    def _point_at(self, cog, guild):
        channel = MagicMock()
        channel.guild = guild
        cog.bot.get_channel = MagicMock(return_value=channel)

    @pytest.mark.asyncio
    async def test_creates_missing_and_deletes_own_stale(self, cog, bridge):
        guild = self._guild(
            ("Vulpine ATM9 Admin", 0), ("Head Admin", 8),
            audit=[("Vulpine ATM9 Admin", BOT_USER_ID)],
        )
        self._point_at(cog, guild)

        assert await cog.sync_roles() is True

        guild.create_role.assert_awaited_once()
        assert guild.create_role.call_args.kwargs["name"] == "Vulpine ATM10 Admin"
        stale, human = guild.roles
        stale.delete.assert_awaited_once()
        human.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_human_role_without_permissions_is_kept(self, cog, bridge):
        """Looks like ours (no guild permissions) but the audit log says a
        person made it - channel-overwrite-only roles look exactly like this."""
        guild = self._guild(("Build Admin", 0), audit=[("Build Admin", 7)])
        self._point_at(cog, guild)
        await cog.sync_roles()
        guild.roles[0].delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_audit_log_access_deletes_nothing(self, cog, bridge):
        guild = self._guild(("Vulpine ATM9 Admin", 0), audit_forbidden=True)
        self._point_at(cog, guild)
        await cog.sync_roles()
        guild.roles[0].delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_role_left_alone(self, cog, bridge):
        guild = self._guild(("Vulpine ATM10 Admin", 0))
        self._point_at(cog, guild)
        assert await cog.sync_roles() is True
        guild.create_role.assert_not_awaited()
        guild.roles[0].delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guild_not_visible_is_retried_later(self, cog):
        cog.bot.get_channel = MagicMock(return_value=None)
        assert await cog.sync_roles() is False
        await cog.on_ready()
        assert cog._roles_synced is False

    @pytest.mark.asyncio
    async def test_missing_manage_roles_is_retried_later(self, cog):
        guild = self._guild()
        guild.create_role = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
        self._point_at(cog, guild)
        await cog.on_ready()
        assert cog._roles_synced is False


def test_duration():
    assert _duration(60) == "1 minute"
    assert _duration(300) == "5 minutes"
    assert _duration(30) == "30 seconds"
    assert _duration(137) == "2 minutes 17 seconds"
    assert _duration(0) == "0 seconds"
