#!/usr/bin/env python3
"""Minimal RCON client used on the instance.

Usage: mcrcon.py <command> [args...]

Reads the password from /var/lib/pickaxe/rcon.pass and talks to localhost.
Prints the server's reply on stdout. Exit codes: 0 ok, 1 error.
"""

import socket
import struct
import sys
from pathlib import Path

HOST = "127.0.0.1"
PORT = 25575
PASS_FILE = Path("/var/lib/pickaxe/rcon.pass")

TYPE_RESPONSE = 0
TYPE_COMMAND = 2
TYPE_LOGIN = 3

ID_COMMAND = 1
ID_SENTINEL = 2


class RconError(Exception):
    pass


def _encode(request_id: int, packet_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise RconError("RCON connection closed unexpectedly")
        buf.extend(chunk)
    return bytes(buf)


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    (length,) = struct.unpack("<i", _recv_exactly(sock, 4))
    payload = _recv_exactly(sock, length)
    request_id, packet_type = struct.unpack("<ii", payload[:8])
    return request_id, packet_type, payload[8:-2].decode("utf-8", errors="replace")


def execute(command: str, timeout: float = 10.0) -> str:
    if not PASS_FILE.exists():
        raise RconError(f"{PASS_FILE} not found -- has install.sh run?")
    password = PASS_FILE.read_text().strip()

    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(_encode(ID_COMMAND, TYPE_LOGIN, password))
        request_id, _, _ = _read_packet(sock)
        if request_id == -1:
            raise RconError("RCON authentication failed")

        sock.sendall(_encode(ID_COMMAND, TYPE_COMMAND, command))
        # Responses over 4096 bytes arrive split. A second, cheap packet acts as
        # a sentinel: once its reply comes back, the real reply is complete.
        sock.sendall(_encode(ID_SENTINEL, TYPE_RESPONSE, ""))

        chunks: list[str] = []
        while True:
            request_id, _, body = _read_packet(sock)
            if request_id == ID_SENTINEL:
                break
            chunks.append(body)
        return "".join(chunks).strip()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        reply = execute(" ".join(argv[1:]))
    except (RconError, OSError, struct.error) as exc:
        print(f"rcon: {exc}", file=sys.stderr)
        return 1
    if reply:
        print(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
