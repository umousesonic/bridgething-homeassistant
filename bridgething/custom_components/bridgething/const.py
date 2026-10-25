"""Constants for the Bridgething integration."""

DOMAIN = "bridgething"
DEFAULT_URL = "http://172.30.32.1:8770"
API_PORT = 8770
CONF_URL = "url"
CONF_SOURCE = "source"

PLATFORMS = [
    "binary_sensor",
    "sensor",
    "number",
    "select",
    "button",
    "switch",
    "text",
    "media_player",
]


def _percent(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value) * 100, 1)
    return None


def set_brightness(hardware: dict | None) -> float | None:
    """Last brightness the user set. Auto mode does not move this."""
    brightness = (hardware or {}).get("brightness") or {}
    return _percent(brightness.get("level"))


def current_brightness(hardware: dict | None) -> float | None:
    """Brightness the panel is using now."""
    brightness = (hardware or {}).get("brightness") or {}
    value = brightness.get("effectiveLevel")
    if value is None:
        value = brightness.get("level")
    return _percent(value)
