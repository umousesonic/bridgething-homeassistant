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


def displayed_brightness(hardware: dict | None) -> float | None:
    """Backlight slider value. Auto mode shows the level the panel is actually using."""
    brightness = (hardware or {}).get("brightness") or {}
    if brightness.get("mode") == "manual":
        value = brightness.get("level")
    else:
        value = brightness.get("effectiveLevel")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value) * 100, 1)
    return None
