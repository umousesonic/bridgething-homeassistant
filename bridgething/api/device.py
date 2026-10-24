"""Unframed client session on port 8891.

Backlight, power, diagnostics, Bluetooth, and webapp switching live here.
The gateway socket cannot send them.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .ncm import CLIENT_PORT
from .playback import brightness_commands
from .protocol import dumps, envelope, inner_event, normalize_msgpack, outer_type, response_request_id, unpack_msgpack
from .state import AppState
from .ws import WebSocket, connect

log = logging.getLogger("bridgething.device")


class Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None


class DeviceSession:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.sent: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.waiting: dict[str, Pending] = {}
        self.ws: WebSocket | None = None
        self._poll_at = 0.0

    def connect(self, host: str) -> None:
        self.close()
        self.ws = connect(host, CLIENT_PORT)
        self.ws.sock.settimeout(1.0)
        self.state.update(clientConnected=True)
        self._hello()
        self._poll_at = time.monotonic()

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        self.state.update(clientConnected=False)

    def send(self, message: dict[str, Any]) -> None:
        payload = dumps(message)
        with self.lock:
            self.sent.append(message)
            if self.ws is not None:
                self.ws.send_text(payload)

    def command(self, surface: str, event: str, body: Any = None) -> None:
        self.send(envelope("command", surface, event, body))

    def request(self, surface: str, event: str, body: Any = None, timeout: float = 8.0) -> dict[str, Any] | None:
        message = envelope("request", surface, event, body)
        pending = Pending()
        with self.lock:
            self.waiting[message["id"]] = pending
        try:
            self.send(message)
        except OSError:
            with self.lock:
                self.waiting.pop(message["id"], None)
            raise
        if not pending.event.wait(timeout):
            with self.lock:
                self.waiting.pop(message["id"], None)
            raise TimeoutError(event)
        return pending.result

    def handle_payload(self, opcode: int, payload: bytes) -> None:
        if opcode == 1:
            message = json.loads(payload.decode("utf-8"))
        else:
            message = normalize_msgpack(unpack_msgpack(payload))
        if isinstance(message, dict):
            self.handle(message)

    def handle(self, message: dict[str, Any]) -> None:
        try:
            self._dispatch(message)
        finally:
            rid = response_request_id(message)
            if not rid:
                return
            with self.lock:
                pending = self.waiting.pop(rid, None)
            if pending is not None:
                pending.result = message
                pending.event.set()

    def set_brightness(self, percent: float) -> None:
        level = max(0.0, min(1.0, float(percent) / 100.0))
        hardware = dict(self.state.get("hardware") or {})
        brightness = dict(hardware.get("brightness") or {})
        brightness.update(mode="manual", level=level, effectiveLevel=level)
        hardware["brightness"] = brightness
        self.state.update(hardware=hardware)
        for surface, event, body, kind in brightness_commands(percent):
            self.send(envelope(kind, surface, event, body))
        self.command_refresh()

    def set_brightness_mode(self, mode: str) -> None:
        hardware = dict(self.state.get("hardware") or {})
        brightness = dict(hardware.get("brightness") or {})
        brightness["mode"] = mode
        hardware["brightness"] = brightness
        self.state.update(hardware=hardware)
        self.command("hardware", "displaySetMode", {"mode": mode})
        self.command_refresh()

    def command_refresh(self) -> None:
        self.send(envelope("request", "hardware", "stateGet"))

    def activate_webapp(self, webapp_id: str) -> None:
        self.send(envelope("request", "webapp", "activate", {"id": webapp_id}))

    def reboot(self) -> None:
        self.command("system", "reboot")

    def power_off(self) -> None:
        self.command("system", "powerOff")

    def factory_reset(self) -> None:
        self.send(envelope("request", "system", "factoryReset"))

    def set_gesture(self, gesture: str) -> None:
        self.command("system", "launcherGestureSet", {"gesture": gesture})

    def set_companion_launch(self, enabled: bool) -> None:
        self.command("system", "companionLaunchSet", {"enabled": bool(enabled)})
        self.state.update(companionLaunch=bool(enabled))

    def select_bluetooth(self, mac: str) -> None:
        self.state.update(bluetoothSelection=mac)

    def connect_bluetooth(self, mac: str | None = None) -> None:
        target = mac or self.state.get("bluetoothSelection")
        if target:
            self.command("bluetooth", "connect", {"mac": target})

    def forget_bluetooth(self, mac: str | None = None) -> None:
        target = mac or self.state.get("bluetoothSelection")
        if target:
            self.send(envelope("request", "bluetooth", "forget", {"mac": target}))

    def set_discoverable(self, enabled: bool) -> None:
        self.command("bluetooth", "enableDiscoverable" if enabled else "disableDiscoverable")
        self.state.update(discoverable=bool(enabled))

    def set_alias(self, name: str) -> None:
        self.command("bluetooth", "setAlias", {"name": name})
        self.state.update(bluetoothAlias=name)

    def poll(self) -> None:
        now = time.monotonic()
        if now - self._poll_at < 5:
            return
        self._poll_at = now
        self.send(envelope("request", "hardware", "stateGet"))
        self.send(envelope("request", "system", "diagnosticsGet"))

    def _hello(self) -> None:
        # This firmware accepts only versionRequest and diagnosticsGet on the system surface.
        self.send(envelope("request", "system", "versionRequest"))
        self.send(envelope("request", "hardware", "stateGet"))
        self.send(envelope("request", "system", "diagnosticsGet"))
        self.send(envelope("request", "webapp", "list"))
        self.send(envelope("request", "webapp", "current"))
        self.send(envelope("request", "bluetooth", "list"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        surface = outer_type(message)
        event, body = inner_event(message)
        payload = body if isinstance(body, dict) else {}
        if surface == "hardware":
            self._hardware(event, payload)
        elif surface == "webapp":
            self._webapp(event, payload)
        elif surface == "system":
            self._system(event, payload)
        elif surface == "bluetooth":
            self._bluetooth(event, payload if payload else body)

    def _hardware(self, event: str, body: dict[str, Any]) -> None:
        if event == "stateReply":
            state = body.get("state") if isinstance(body.get("state"), dict) else {}
            self.state.update(hardware=state)
        elif event == "brightnessChanged":
            hardware = dict(self.state.get("hardware") or {})
            hardware["brightness"] = body
            self.state.update(hardware=hardware)
        elif event == "ambientLightUpdate":
            hardware = dict(self.state.get("hardware") or {})
            if "ambientLevel" in body:
                hardware["ambientLevel"] = body.get("ambientLevel")
            self.state.update(hardware=hardware)

    def _webapp(self, event: str, body: dict[str, Any]) -> None:
        if event == "listReply" and isinstance(body.get("webapps"), list):
            self.state.update(webapps=body["webapps"])
        elif event in ("currentReply", "activeReply"):
            self.state.update(activeWebapp=body.get("id"), activeWebappName=body.get("name"))
        elif event == "activeChanged":
            self.state.update(activeWebappName=body.get("name"))
            self.send(envelope("request", "webapp", "current"))
        elif event in ("webappInstalled", "webappUninstalled"):
            self.send(envelope("request", "webapp", "list"))
            self.send(envelope("request", "webapp", "current"))

    def _system(self, event: str, body: dict[str, Any]) -> None:
        if event == "version":
            self.state.apply_meta(body)
        elif event == "diagnosticsReply":
            diagnostics = body.get("diagnostics") if isinstance(body.get("diagnostics"), dict) else body
            self.state.update(diagnostics=diagnostics)
        elif event in ("deviceNickname", "deviceNicknameChanged"):
            self.state.update(nickname=body.get("nickname"))
            if body.get("nickname"):
                self.state.apply_meta({"nickname": body.get("nickname")})
        elif event in ("launcherGestureReply", "launcherGestureChanged"):
            self.state.update(launcherGesture=body.get("gesture"))
        elif event in ("companionLaunchReply", "companionLaunchChanged"):
            self.state.update(companionLaunch=bool(body.get("enabled")))
        elif event == "logsTailReply":
            entries = body.get("entries") if isinstance(body.get("entries"), list) else []
            if entries and isinstance(entries[-1], dict):
                self.state.update(logLine=entries[-1].get("message"))
        elif event == "logEntry":
            if body.get("message"):
                self.state.update(logLine=body.get("message"))

    def _bluetooth(self, event: str, body: Any) -> None:
        if event in ("pairedDevices",) or (event == "" and isinstance(body, dict)):
            self.state.update(bluetoothDevices=_paired_devices(body))
        elif event == "connectedDevice" and isinstance(body, dict):
            self.state.update(bluetoothConnected=body.get("mac"))
        elif event == "interface" and isinstance(body, dict):
            self.state.apply_meta({"btMac": body.get("mac"), "bluetoothName": body.get("name")})
        elif event == "status" and isinstance(body, dict) and body.get("connected") is False:
            self.state.update(bluetoothConnected=None)


def _paired_devices(body: Any) -> list[dict[str, str]]:
    mapping = body
    if isinstance(body, dict) and isinstance(body.get("devices"), dict):
        mapping = body["devices"]
    if not isinstance(mapping, dict):
        return []
    devices: list[dict[str, str]] = []
    for mac, info in mapping.items():
        if not isinstance(mac, str) or mac in ("event", "devices"):
            continue
        name = mac
        if isinstance(info, dict) and info.get("name"):
            name = str(info["name"])
        devices.append({"mac": mac, "name": name})
    return devices
