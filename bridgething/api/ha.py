"""Calls into Home Assistant Core through the Supervisor API."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class HomeAssistant:
    def __init__(self, token: str | None = None, base: str | None = None) -> None:
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = (base or "http://supervisor/core").rstrip("/")
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.calls.append((domain, service, data))
        if not self.token:
            return
        self._request("POST", f"/api/services/{domain}/{service}", data)

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        if not self.token or not entity_id:
            return None
        try:
            return self._request("GET", f"/api/states/{urllib.request.quote(entity_id)}")
        except Exception:
            return None

    def fetch_picture(self, picture: str) -> tuple[bytes, str] | None:
        if not picture:
            return None
        url = picture
        headers = {}
        if picture.startswith("/"):
            url = f"{self.base}{picture}"
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                mime = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                return response.read(256 * 1024), mime or "application/octet-stream"
        except Exception:
            return None

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read()
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))
