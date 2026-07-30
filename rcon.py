"""Send one RCON command to a server defined in config.toml.

Handy for `kubejs reload server-scripts`, `getstats` and `discordstatus` without
needing a shell inside the Minecraft container.

    uv run python rcon.py "Vulpine ATM10" getstats
    uv run python rcon.py "Vulpine ATM10" kubejs reload server-scripts

Note: KubeJS spells it `server-scripts` here (hyphen), and reloading re-runs the
script bodies but does NOT rebuild the Brigadier command tree - changes to the
registered commands need a full server restart.
"""

import socket
import struct
import sys

from config import load_config

RCON_AUTH = 3
RCON_EXEC = 2


def _send(sock: socket.socket, packet_id: int, packet_type: int, payload: str) -> None:
    body = struct.pack("<ii", packet_id, packet_type) + payload.encode() + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(body)) + body)


def _recv(sock: socket.socket) -> tuple[int, str]:
    length = struct.unpack("<i", sock.recv(4))[0]
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("connection closed by server")
        data += chunk
    return struct.unpack("<i", data[0:4])[0], data[8:-2].decode(errors="replace")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)

    name, command = sys.argv[1], " ".join(sys.argv[2:])
    servers = load_config().servers
    match = [s for s in servers if s.minecraft.server_name.lower() == name.lower()]
    if not match:
        sys.exit(
            "no server named %r in config.toml - have: %s"
            % (name, ", ".join(s.minecraft.server_name for s in servers))
        )

    mc = match[0].minecraft
    with socket.create_connection((mc.rcon_host, mc.rcon_port), timeout=15) as sock:
        _send(sock, 1, RCON_AUTH, mc.rcon_password)
        if _recv(sock)[0] == -1:
            sys.exit("RCON auth failed for %s" % mc.server_name)
        _send(sock, 2, RCON_EXEC, command)
        print(_recv(sock)[1])


if __name__ == "__main__":
    main()
