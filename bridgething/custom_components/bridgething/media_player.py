"""The screen mirror. Transport buttons go to the media player that owns the sound."""

from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import BridgethingEntity

_STATES = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "stopped": MediaPlayerState.IDLE,
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([NowPlaying(hass.data["bridgething"][entry.entry_id])])


class NowPlaying(BridgethingEntity, MediaPlayerEntity):
    _attr_name = "Now playing"
    _attr_icon = "mdi:music"
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.SEEK
    )

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "now_playing")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool((self.coordinator.data or {}).get("gatewayConnected"))

    @property
    def state(self) -> MediaPlayerState:
        playback = self._playback()
        return _STATES.get(str(playback.get("state") or ""), MediaPlayerState.IDLE)

    @property
    def media_title(self) -> str | None:
        track = self._track()
        return track.get("title")

    @property
    def media_artist(self) -> str | None:
        track = self._track()
        return track.get("artist") or None

    @property
    def media_album_name(self) -> str | None:
        track = self._track()
        return track.get("album") or None

    @property
    def media_position(self) -> int | None:
        position = self._playback().get("positionMs")
        if isinstance(position, (int, float)):
            return int(position / 1000)
        return None

    @property
    def media_duration(self) -> int | None:
        duration = self._track().get("durationMs")
        if isinstance(duration, (int, float)):
            return int(duration / 1000)
        return None

    @property
    def shuffle(self) -> bool:
        return bool(self._playback().get("shuffle"))

    async def async_media_play(self) -> None:
        await self._command("resume")

    async def async_media_pause(self) -> None:
        await self._command("pause")

    async def async_media_next_track(self) -> None:
        await self._command("skipNext")

    async def async_media_previous_track(self) -> None:
        await self._command("skipPrev")

    async def async_media_seek(self, position: float) -> None:
        await self.coordinator.async_post("/playback/command", {"event": "seekTo", "data": {"positionMs": int(position * 1000)}})

    async def _command(self, event: str) -> None:
        await self.coordinator.async_post("/playback/command", {"event": event})

    def _player(self) -> dict:
        player = (self.coordinator.data or {}).get("playback") or {}
        return player if isinstance(player, dict) else {}

    def _track(self) -> dict:
        track = self._player().get("track") or {}
        return track if isinstance(track, dict) else {}

    def _playback(self) -> dict:
        playback = self._player().get("playback") or {}
        return playback if isinstance(playback, dict) else {}
