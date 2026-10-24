"""Map a Home Assistant media player into the now-playing snapshot the screen shows.

The Car Thing does not play the audio. Transport buttons on the screen become
Home Assistant service calls on the chosen media player.
"""

from __future__ import annotations

import time
from typing import Any

ARTWORK_ID = "ha-cover"


def music_provider(entity_id: str) -> str:
    if entity_id.startswith("media_player.spotify"):
        return "spotify"
    return "none"


def app_bundle(entity_id: str) -> str:
    if music_provider(entity_id) == "spotify":
        return "com.spotify.client"
    return "homeassistant"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def repeat_to_device(value: Any) -> str:
    if value in ("off", "all", "one"):
        return str(value)
    return "off"


def repeat_to_home_assistant(value: Any) -> str:
    return repeat_to_device(value)


def playback_state(state: str) -> str:
    if state == "playing":
        return "playing"
    if state == "paused":
        return "paused"
    return "stopped"


def idle_playback() -> dict[str, Any]:
    """No-track snapshot the stock companion sends. Playback stays paused, and the track is omitted."""
    return {
        "state": "paused",
        "positionMs": 0,
        "shuffle": False,
        "shuffleMode": "off",
        "repeat": "off",
        "setElapsedTimeAvailable": False,
    }


def player_state_from_ha(entity_id: str, ha_state: dict[str, Any] | None) -> dict[str, Any]:
    attributes = (ha_state or {}).get("attributes") or {}
    state = playback_state(str((ha_state or {}).get("state") or "idle"))
    title = _text(attributes.get("media_title"))
    if state == "stopped" or title is None:
        return {
            "track": None,
            "playback": idle_playback(),
            "queue": [],
            "options": {"speed": 1.0},
            "entityId": entity_id,
        }
    duration = _number(attributes.get("media_duration"))
    position = _number(attributes.get("media_position")) or 0.0
    uri = _text(attributes.get("media_content_id")) or f"homeassistant:{entity_id}"
    picture = _text(attributes.get("entity_picture"))
    track = {
        "uri": uri,
        "persistentId": uri,
        "title": title,
        "artist": _text(attributes.get("media_artist")) or "",
        "album": _text(attributes.get("media_album_name")) or "",
        "durationMs": int(duration * 1000) if duration is not None else None,
        "artworkId": ARTWORK_ID if picture else None,
    }
    track = {key: value for key, value in track.items() if value is not None}
    item = {
        "uri": uri,
        "title": title,
        "artist": track.get("artist") or "",
        "album": track.get("album") or "",
        "durationMs": track.get("durationMs"),
        "artworkId": track.get("artworkId"),
        "queued": False,
    }
    item = {key: value for key, value in item.items() if value is not None}
    return {
        "track": track,
        "playback": {
            "state": state,
            "positionMs": int(position * 1000),
            "positionAgeMs": 0,
            "shuffle": bool(attributes.get("shuffle")),
            "repeat": repeat_to_device(attributes.get("repeat")),
            "setElapsedTimeAvailable": True,
        },
        "queue": [item],
        "options": {"speed": 1.0},
        "picture": picture,
        "entityId": entity_id,
        "updatedAt": time.time(),
    }


def has_track(snapshot: dict[str, Any] | None) -> bool:
    return bool(snapshot and snapshot.get("track"))


def queue_snapshot(player: dict[str, Any]) -> dict[str, Any]:
    items = list(player.get("queue") or [])
    return {"order": [item["uri"] for item in items if item.get("uri")], "items": items}


def wire_player_state(player: dict[str, Any]) -> dict[str, Any]:
    track = player.get("track")
    if not isinstance(track, dict) or not track:
        playback = player.get("playback") if isinstance(player.get("playback"), dict) else {}
        shuffled = bool(playback.get("shuffle"))
        position = playback.get("positionMs")
        repeat = playback.get("repeat")
        return {
            "playback": {
                "state": "paused",
                "positionMs": int(position) if isinstance(position, (int, float)) and not isinstance(position, bool) else 0,
                "shuffle": shuffled,
                "shuffleMode": "songs" if shuffled else "off",
                "repeat": repeat if repeat in ("off", "all", "one") else "off",
                "setElapsedTimeAvailable": False,
            },
            "queue": [],
            "options": {"speed": 1.0},
        }
    payload = {
        "track": track,
        "playback": player.get("playback") or {"state": "paused", "positionMs": 0},
        "queue": player.get("queue") or [],
        "options": player.get("options") or {"speed": 1.0},
    }
    context = player.get("context")
    if isinstance(context, dict) and context.get("uri"):
        payload["context"] = {"uri": context["uri"], "name": context.get("name")}
    return payload


def brightness_commands(percent: float) -> list[tuple[str, str, dict[str, Any], str]]:
    level = max(0.0, min(1.0, float(percent) / 100.0))
    return [
        ("hardware", "displaySetMode", {"mode": "manual"}, "command"),
        ("hardware", "displaySetLevel", {"level": level}, "command"),
    ]


def displayed_brightness(hardware: dict[str, Any] | None) -> float | None:
    brightness = (hardware or {}).get("brightness") or {}
    mode = brightness.get("mode")
    if mode == "manual":
        value = brightness.get("level")
    else:
        value = brightness.get("effectiveLevel")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value) * 100, 1)
    return None


def transport_service(event: str, body: Any, entity_id: str) -> tuple[str, str, dict[str, Any]] | None:
    """Return domain, service, and data for a device player command."""
    payload = body if isinstance(body, dict) else {}
    base = {"entity_id": entity_id}
    if event in ("pause",):
        return ("media_player", "media_pause", base)
    if event in ("resume", "play") and not payload.get("uri"):
        return ("media_player", "media_play", base)
    if event == "play" and payload.get("uri"):
        return (
            "media_player",
            "media_play_media",
            {**base, "media_content_id": payload["uri"], "media_content_type": "music"},
        )
    if event == "queue" and payload.get("uri"):
        return (
            "media_player",
            "media_play_media",
            {**base, "media_content_id": payload["uri"], "media_content_type": "music"},
        )
    if event == "skipNext":
        return ("media_player", "media_next_track", base)
    if event == "skipPrev":
        return ("media_player", "media_previous_track", base)
    if event == "seekTo":
        position = payload.get("positionMs")
        if isinstance(position, (int, float)):
            return ("media_player", "media_seek", {**base, "seek_position": float(position) / 1000.0})
        return None
    if event == "setShuffle":
        return ("media_player", "shuffle_set", {**base, "shuffle": bool(payload.get("on"))})
    if event == "setRepeat":
        return ("media_player", "repeat_set", {**base, "repeat": repeat_to_home_assistant(payload.get("mode"))})
    return None
