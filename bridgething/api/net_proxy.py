"""Proxy the Car Thing's net and tunnel requests through the Home Assistant host.

TLS ends here. The device receives response bodies, not a default route.
"""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from .protocol import as_bytes

MAX_INLINE = 256 * 1024
STREAM_CHUNK = 16 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _headers(pairs: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(pairs, list):
        for item in pairs:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = str(item.get("value") or "")
    return result


def _header_list(headers: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if headers is None:
        return items
    raw = getattr(headers, "items", None)
    if raw is None:
        return items
    for name, value in raw():
        items.append({"name": str(name), "value": str(value)})
    return items


def fetch(request: dict[str, Any], opener: Callable[..., Any] | None = None, max_bytes: int = MAX_INLINE) -> dict[str, Any]:
    url = str(request.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": {"type": "requestFailed", "data": {"reason": "only http and https are proxied"}}}
    method = str(request.get("method") or "GET").upper()
    body = as_bytes(request.get("body")) or None
    timeout = request.get("timeoutMs")
    timeout_s = 30.0
    if isinstance(timeout, (int, float)) and timeout > 0:
        timeout_s = min(float(timeout) / 1000.0, 60.0)
    policy = str(request.get("redirect") or "follow")
    req = urllib.request.Request(url, data=body, headers=_headers(request.get("headers")), method=method)
    if opener is None:
        handlers: list[Any] = []
        if policy in ("manual", "error"):
            handlers.append(_NoRedirect())
        urlopen = urllib.request.build_opener(*handlers).open
    else:
        urlopen = opener
    try:
        with urlopen(req, timeout=timeout_s) as response:
            payload = response.read(max_bytes + 1)
            status = getattr(response, "status", None) or getattr(response, "code", 200)
            return {
                "ok": True,
                "status": int(status),
                "headers": _header_list(getattr(response, "headers", None)),
                "body": payload[:max_bytes],
                "truncated": len(payload) > max_bytes,
            }
    except urllib.error.HTTPError as exc:
        if policy == "error" and exc.code in (301, 302, 303, 307, 308):
            return {"ok": False, "error": {"type": "requestFailed", "data": {"reason": f"redirect {exc.code}"}}}
        payload = exc.read(max_bytes + 1) if exc.fp is not None else b""
        return {
            "ok": True,
            "status": int(exc.code),
            "headers": _header_list(exc.headers),
            "body": payload[:max_bytes],
            "truncated": len(payload) > max_bytes,
        }
    except TimeoutError:
        return {"ok": False, "error": {"type": "timeout"}}
    except Exception as exc:  # noqa: BLE001 - the device needs a wire error, not a traceback
        text = str(exc).lower()
        if "timed out" in text:
            return {"ok": False, "error": {"type": "timeout"}}
        return {"ok": False, "error": {"type": "requestFailed", "data": {"reason": str(exc)}}}


def open_stream(request: dict[str, Any], send: Callable[[str, dict[str, Any]], None], stream_id: str) -> None:
    result = fetch(request, max_bytes=1024 * 1024)
    if not result.get("ok") or result.get("truncated"):
        error = result.get("error") or {"type": "requestFailed", "data": {"reason": "response too large"}}
        send("streamError", {"streamId": stream_id, "error": error})
        return
    body = as_bytes(result.get("body"))
    send(
        "streamBegin",
        {
            "streamId": stream_id,
            "status": result.get("status") or 200,
            "headers": result.get("headers") or [],
            "totalSize": len(body),
        },
    )
    offset = 0
    while offset < len(body):
        chunk = body[offset : offset + STREAM_CHUNK]
        send("streamChunk", {"streamId": stream_id, "offset": offset, "bytes": chunk})
        offset += len(chunk)
    send("streamEnd", {"streamId": stream_id})


class Tunnel:
    def __init__(self, tunnel_id: str, sock: socket.socket, send: Callable[[str, dict[str, Any]], None]) -> None:
        self.tunnel_id = tunnel_id
        self.sock = sock
        self.send = send
        self.closed = False
        self._thread = threading.Thread(target=self._read, name=f"tunnel-{tunnel_id[:8]}", daemon=True)
        self._thread.start()

    def write(self, payload: bytes) -> None:
        try:
            self.sock.sendall(payload)
            self.send("ack", {"tunnelId": self.tunnel_id, "consumed": len(payload)})
        except OSError as exc:
            self.close(str(exc))

    def close(self, reason: str = "closed") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.send("closed", {"tunnelId": self.tunnel_id, "reason": reason})

    def _read(self) -> None:
        try:
            while not self.closed:
                chunk = self.sock.recv(STREAM_CHUNK)
                if not chunk:
                    self.close("eof")
                    return
                self.send("data", {"tunnelId": self.tunnel_id, "bytes": chunk})
        except OSError as exc:
            self.close(str(exc))


def connect_tunnel(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    if port < 1 or port > 65535:
        raise OSError("port out of range")
    return socket.create_connection((host, port), timeout)
