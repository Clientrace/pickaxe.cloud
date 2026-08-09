"""Minecraft Server List Ping (the query the multiplayer menu makes).

Lets `pickaxe status` report players online without exposing RCON to the
internet -- it only needs the public game port, which is already open.
Protocol reference: https://minecraft.wiki/w/Java_Edition_protocol
"""

from __future__ import annotations

import json
import socket
import struct
import time
from dataclasses import dataclass

PROTOCOL_UNKNOWN = -1  # tells the server "just give me status, I'm not joining"


@dataclass
class ServerStatus:
    version: str
    players_online: int
    players_max: int
    sample: list[str]
    motd: str
    latency_ms: int


def _write_varint(value: int) -> bytes:
    # VarInts are two's-complement 32-bit. Masking matters: Python's >> on a
    # negative int never reaches zero, so -1 would loop forever.
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_exactly(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("connection closed mid-packet")
        buf.extend(chunk)
    return bytes(buf)


def _read_varint(sock: socket.socket) -> int:
    value = 0
    for shift in range(0, 35, 7):
        byte = _read_exactly(sock, 1)[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
    raise ValueError("varint longer than 5 bytes")


def _packet(packet_id: int, payload: bytes) -> bytes:
    body = _write_varint(packet_id) + payload
    return _write_varint(len(body)) + body


def _string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _write_varint(len(raw)) + raw


def _flatten_motd(node) -> str:
    """MOTD is either a legacy string or a chat component tree."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_motd(item) for item in node)
    if isinstance(node, dict):
        return _flatten_motd(node.get("text", "")) + _flatten_motd(node.get("extra", []))
    return ""


def _strip_formatting(text: str) -> str:
    out, skip = [], False
    for char in text:
        if skip:
            skip = False
            continue
        if char == "§":  # section sign introduces a colour code
            skip = True
            continue
        out.append(char)
    return "".join(out).strip()


def ping(host: str, port: int = 25565, timeout: float = 5.0) -> ServerStatus:
    """Query a Minecraft server. Raises OSError if it is not reachable."""
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        handshake = (
            _write_varint(PROTOCOL_UNKNOWN)
            + _string(host)
            + struct.pack(">H", port)
            + _write_varint(1)  # next state: status
        )
        sock.sendall(_packet(0x00, handshake))
        sock.sendall(_packet(0x00, b""))  # status request

        _read_varint(sock)  # total packet length
        if _read_varint(sock) != 0x00:
            raise ValueError("unexpected packet id in status response")
        payload = _read_exactly(sock, _read_varint(sock))

    latency_ms = int((time.monotonic() - started) * 1000)
    data = json.loads(payload.decode("utf-8"))
    players = data.get("players") or {}
    return ServerStatus(
        version=(data.get("version") or {}).get("name", "unknown"),
        players_online=players.get("online", 0),
        players_max=players.get("max", 0),
        sample=[p.get("name", "?") for p in (players.get("sample") or [])],
        motd=_strip_formatting(_flatten_motd(data.get("description", ""))),
        latency_ms=latency_ms,
    )


def wait_until_up(host: str, port: int, timeout: int = 600, on_tick=None) -> ServerStatus | None:
    """Poll until the server answers a status ping, or `timeout` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return ping(host, port, timeout=4.0)
        except (OSError, ValueError, json.JSONDecodeError):
            if on_tick:
                on_tick()
            time.sleep(5)
    return None
