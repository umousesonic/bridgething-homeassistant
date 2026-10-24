"""A small WebSocket client. The gateway wants binary frames. The device client wants text."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct

from .protocol import WS_GUID


class WebSocket:
    def __init__(self, sock: socket.socket, leftover: bytes = b"", protocol: str | None = None) -> None:
        self.sock = sock
        self.buffer = bytearray(leftover)
        self.protocol = protocol

    def send(self, payload: bytes, opcode: int = 0x2) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self.send(text.encode("utf-8"), opcode=0x1)

    def recv(self) -> tuple[int, bytes]:
        while True:
            opcode, payload = self._read_frame()
            if opcode in (0x1, 0x2):
                return opcode, payload
            if opcode == 0x8:
                raise ConnectionError("websocket closed")
            if opcode == 0x9:
                self.send(payload, opcode=0xA)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buffer += chunk
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def _read_frame(self) -> tuple[int, bytes]:
        first = self._read_exact(2)
        opcode = first[0] & 0x0F
        if not first[0] & 0x80:
            raise ConnectionError("fragmented websocket frames are not supported")
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if length > 2 * 1024 * 1024:
            raise ConnectionError("websocket frame is too large")
        if first[1] & 0x80:
            mask = self._read_exact(4)
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self._read_exact(length)))
        else:
            payload = self._read_exact(length)
        return opcode, payload


def connect(
    host: str,
    port: int,
    timeout: float = 5.0,
    *,
    tls: bool = False,
    path: str = "/",
    headers: dict[str, str] | None = None,
    protocols: list[str] | None = None,
) -> WebSocket:
    sock = socket.create_connection((host, port), timeout)
    if tls:
        import ssl

        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    if not path.startswith("/"):
        path = "/" + path
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if protocols:
        lines.append("Sec-WebSocket-Protocol: " + ", ".join(protocols))
    for name, value in (headers or {}).items():
        if name.lower() not in {"host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol"}:
            lines.append(f"{name}: {value}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket handshake closed")
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise ConnectionError(status.decode("utf-8", "replace"))
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest())
    if accept not in head:
        raise ConnectionError("websocket accept header did not match")
    chosen = None
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"sec-websocket-protocol:"):
            chosen = line.split(b":", 1)[1].strip().decode("utf-8", "replace")
    return WebSocket(sock, rest, chosen)
