"""Home Assistant app process: USB-NCM, both device sockets, and the local API."""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .device import DeviceSession
from .gateway import GatewaySession, client_port_open, find_gateway
from .ha import HomeAssistant
from .ncm import (
    bring_up,
    candidate_gateways,
    cdc_interfaces,
    ensure_profile,
    install_dhclient_hook,
    local_ncm_addresses,
    read_sysfs_interfaces,
)
from .playback import player_state_from_ha, transport_service
from .protocol import decode_frames
from .state import AppState

log = logging.getLogger("bridgething")
API_PORT = 8770
SOURCE_PATH = "/data/source_entity"


class Companion:
    def __init__(self, state: AppState | None = None, ha: HomeAssistant | None = None) -> None:
        self.state = state or AppState()
        self.ha = ha or HomeAssistant()
        self.gateway = GatewaySession(self.state, self.ha)
        self.device = DeviceSession(self.state)
        self.stop = threading.Event()

    def apply_source(self, entity_id: str) -> None:
        self.state.set_source(entity_id)
        try:
            os.makedirs(os.path.dirname(SOURCE_PATH), exist_ok=True)
            with open(SOURCE_PATH, "w", encoding="utf-8") as handle:
                handle.write(entity_id.strip())
        except OSError:
            return

    def apply_ha_state(self, body: dict[str, Any]) -> None:
        entity_id = str(body.get("entity_id") or self.state.get("sourceEntity") or "")
        if entity_id:
            self.state.update(sourceEntity=entity_id)
        player = player_state_from_ha(
            entity_id,
            {"state": body.get("state"), "attributes": body.get("attributes") or {}},
        )
        self.state.set_playback(player)

    def transport(self, event: str, body: dict[str, Any] | None = None) -> str | None:
        entity = str(self.state.get("sourceEntity") or "")
        if not entity:
            return "no active transport"
        service = transport_service(event, body or {}, entity)
        if service is None:
            return "unsupported"
        self.ha.call_service(*service)
        return None


def load_source() -> str:
    for path in ("/data/options.json",):
        try:
            with open(path, encoding="utf-8") as handle:
                options = json.load(handle)
        except (OSError, json.JSONDecodeError):
            options = None
        if isinstance(options, dict) and str(options.get("source_entity") or "").strip():
            return str(options["source_entity"]).strip()
    try:
        with open(SOURCE_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def make_handler(companion: Companion) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http " + fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/state", "/status"):
                self._send(200, companion.state.snapshot())
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid json"})
                return
            if not isinstance(body, dict):
                body = {}
            try:
                status, payload = route_post(companion, path, body)
            except TimeoutError as exc:
                self._send(504, {"error": str(exc)})
                return
            except OSError as exc:
                self._send(503, {"error": str(exc)})
                return
            self._send(status, payload)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def _need(companion: Companion, key: str, message: str) -> None:
    if not companion.state.get(key):
        raise OSError(message)


def route_post(companion: Companion, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    device = companion.device
    gateway = companion.gateway
    if path == "/playback/source":
        companion.apply_source(str(body.get("entity_id") or ""))
        return 200, companion.state.snapshot()
    if path == "/playback/state":
        companion.apply_ha_state(body)
        return 200, {"ok": True}
    if path == "/playback/command":
        error = companion.transport(str(body.get("event") or ""), body.get("data") if isinstance(body.get("data"), dict) else body)
        if error:
            return 409, {"error": error}
        return 200, {"ok": True}
    if path == "/brightness":
        _need(companion, "clientConnected", "Car Thing client socket is not connected")
        device.set_brightness(float(body.get("value") or 0))
        return 200, companion.state.snapshot()
    if path == "/brightness/mode":
        _need(companion, "clientConnected", "Car Thing client socket is not connected")
        device.set_brightness_mode(str(body.get("mode") or "auto"))
        return 200, companion.state.snapshot()
    if path == "/webapps/active":
        webapp_id = str(body.get("id") or "")
        if companion.state.get("clientConnected"):
            device.activate_webapp(webapp_id)
        elif companion.state.get("gatewayConnected"):
            gateway.switch_webapp(webapp_id)
        else:
            raise OSError("Car Thing is not connected")
        return 200, {"ok": True}
    if path in ("/reboot", "/power_off", "/factory_reset", "/launcher_gesture", "/companion_launch", "/bluetooth/connect", "/bluetooth/forget", "/bluetooth/discoverable", "/bluetooth/alias"):
        _need(companion, "clientConnected", "Car Thing client socket is not connected")
    if path == "/reboot":
        device.reboot()
        return 200, {"ok": True}
    if path == "/power_off":
        device.power_off()
        return 200, {"ok": True}
    if path == "/factory_reset":
        device.factory_reset()
        return 200, {"ok": True}
    if path == "/nickname":
        _need(companion, "gatewayConnected", "Car Thing gateway is not connected")
        gateway.set_nickname(str(body.get("nickname") or ""))
        companion.state.update(nickname=str(body.get("nickname") or ""))
        return 200, {"ok": True}
    if path == "/navigate/url":
        companion.state.update(navigateUrl=str(body.get("url") or ""))
        return 200, {"ok": True}
    if path == "/navigate":
        _need(companion, "gatewayConnected", "Car Thing gateway is not connected")
        url = str(body.get("url") or companion.state.get("navigateUrl") or "")
        gateway.navigate(url)
        return 200, {"ok": True}
    if path == "/launcher_gesture":
        device.set_gesture(str(body.get("gesture") or "fivePress"))
        return 200, {"ok": True}
    if path == "/companion_launch":
        device.set_companion_launch(bool(body.get("enabled")))
        return 200, {"ok": True}
    if path == "/bluetooth/select":
        device.select_bluetooth(str(body.get("mac") or ""))
        return 200, {"ok": True}
    if path == "/bluetooth/connect":
        device.connect_bluetooth(str(body.get("mac") or "") or None)
        return 200, {"ok": True}
    if path == "/bluetooth/forget":
        device.forget_bluetooth(str(body.get("mac") or "") or None)
        return 200, {"ok": True}
    if path == "/bluetooth/discoverable":
        device.set_discoverable(bool(body.get("enabled")))
        return 200, {"ok": True}
    if path == "/bluetooth/alias":
        device.set_alias(str(body.get("name") or ""))
        return 200, {"ok": True}
    return 404, {"error": "not found"}


def ncm_loop(companion: Companion) -> None:
    install_dhclient_hook()
    companion.state.update(profile=ensure_profile())
    seen: set[str] = set()
    while not companion.stop.is_set():
        names = set(cdc_interfaces(read_sysfs_interfaces()))
        for name in names - seen:
            log.info("cdc_ncm interface %s appeared", name)
            bring_up(name)
        for name in seen - names:
            log.info("cdc_ncm interface %s disappeared", name)
            subprocess.run(["dhclient", "-r", name], capture_output=True)
            subprocess.run(["nmcli", "device", "disconnect", name], capture_output=True)
        seen = names
        try:
            addr_text = subprocess.check_output(["ip", "-4", "addr"], text=True)
        except (OSError, subprocess.CalledProcessError):
            addr_text = ""
        addresses = local_ncm_addresses(addr_text)
        host = find_gateway(candidate_gateways(addresses)) if addresses else None
        client_open = bool(host and client_port_open(host))
        companion.state.update(
            ncm=bool(addresses),
            addresses=[item[0] for item in addresses],
            gatewayHost=host,
            clientPortOpen=client_open,
        )
        companion.stop.wait(2)


def gateway_loop(companion: Companion) -> None:
    session = companion.gateway
    buffer = bytearray()
    while not companion.stop.is_set():
        host = companion.state.get("gatewayHost")
        if not host:
            companion.stop.wait(1)
            continue
        try:
            log.info("connecting gateway %s", host)
            session.connect(str(host))
            buffer.clear()
            while not companion.stop.is_set() and companion.state.get("gatewayHost") == host:
                try:
                    assert session.ws is not None
                    opcode, payload = session.ws.recv()
                except (TimeoutError, OSError) as exc:
                    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                        session.sync_playback()
                        continue
                    raise
                if opcode != 2:
                    continue
                buffer.extend(payload)
                for message in decode_frames(buffer):
                    session.handle(message)
                session.sync_playback()
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway session ended: %s", exc)
        finally:
            session.close()
        companion.stop.wait(2)


def device_loop(companion: Companion) -> None:
    session = companion.device
    while not companion.stop.is_set():
        host = companion.state.get("gatewayHost")
        if not host:
            companion.stop.wait(1)
            continue
        try:
            log.info("connecting device client %s", host)
            session.connect(str(host))
            while not companion.stop.is_set() and companion.state.get("gatewayHost") == host:
                try:
                    assert session.ws is not None
                    opcode, payload = session.ws.recv()
                except (TimeoutError, OSError) as exc:
                    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                        session.poll()
                        continue
                    raise
                session.handle_payload(opcode, payload)
                session.poll()
        except Exception as exc:  # noqa: BLE001
            log.warning("device session ended: %s", exc)
        finally:
            session.close()
        companion.stop.wait(2)


def playback_loop(companion: Companion) -> None:
    while not companion.stop.is_set():
        entity = str(companion.state.get("sourceEntity") or "")
        ha_state = companion.ha.get_state(entity) if entity else None
        if isinstance(ha_state, dict):
            companion.state.set_playback(player_state_from_ha(entity, ha_state))
        companion.stop.wait(2)


class ReuseHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def serve(companion: Companion) -> None:
    try:
        server = ReuseHTTPServer(("0.0.0.0", API_PORT), make_handler(companion))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            log.error("port %s is already in use", API_PORT)
        raise
    log.info("api listening on %s", API_PORT)
    try:
        server.serve_forever()
    finally:
        companion.stop.set()
        server.server_close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    companion = Companion()
    source = load_source()
    if source:
        companion.state.update(sourceEntity=source)
    threads = [
        threading.Thread(target=ncm_loop, args=(companion,), name="ncm", daemon=True),
        threading.Thread(target=gateway_loop, args=(companion,), name="gateway", daemon=True),
        threading.Thread(target=device_loop, args=(companion,), name="device", daemon=True),
        threading.Thread(target=playback_loop, args=(companion,), name="playback", daemon=True),
    ]
    for thread in threads:
        thread.start()
    serve(companion)


if __name__ == "__main__":
    main()
