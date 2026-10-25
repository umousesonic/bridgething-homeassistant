import unittest

from api.device import DeviceSession
from api.gateway import GatewaySession
from api.ha import HomeAssistant
from api.playback import brightness_commands, displayed_brightness, player_state_from_ha, transport_service
from api.server import Companion, route_post
from api.state import AppState


def _playing() -> dict:
    return {
        "state": "playing",
        "attributes": {
            "media_title": "Song",
            "media_artist": "Ada",
            "media_album_name": "Album",
            "media_duration": 200,
            "media_position": 12.5,
            "media_content_id": "spotify:track:abc",
            "entity_picture": "/api/media_player_proxy/media_player.spotify_kitchen",
            "shuffle": False,
            "repeat": "off",
        },
    }


class PlaybackTests(unittest.TestCase):
    def test_snapshot_shape_and_authority_claim_before_snapshot(self) -> None:
        state = AppState()
        state.set_playback(player_state_from_ha("media_player.spotify_kitchen", _playing()))
        session = GatewaySession(state, HomeAssistant(token=""))
        session.handle(
            {
                "id": "version-1",
                "meta": {"kind": "event"},
                "data": {
                    "type": "version",
                    "data": {"serialNumber": "ABC123", "nickname": "Dash", "imageVersion": "1.2.3"},
                },
            }
        )
        kinds = [(item["data"]["type"], (item["data"].get("data") or {}).get("event")) for item in session.sent]
        claim_at = kinds.index(("authority", "claim"))
        snapshot_at = kinds.index(("player", "snapshot"))
        self.assertLess(claim_at, snapshot_at)
        self.assertEqual(kinds[0], ("capabilities", "announce"))
        self.assertEqual(session.sent[0]["data"]["data"]["data"]["musicProvider"], "spotify")
        self.assertTrue(session.sent[0]["data"]["data"]["data"]["available"]["netFetch"])
        claims = [item for item in session.sent if item["data"]["data"]["event"] == "claim"]
        self.assertEqual(
            [item["data"]["data"]["data"]["scope"] for item in claims],
            ["nowPlayingMetadata", "nowPlayingPlayback", "volume"],
        )
        self.assertEqual(claims[0]["data"]["data"]["data"]["appBundle"], "com.spotify.client")
        snapshot = next(item for item in session.sent if item["data"]["data"]["event"] == "snapshot" and item["data"]["type"] == "player")
        track = snapshot["data"]["data"]["data"]["track"]
        self.assertEqual(track["title"], "Song")
        self.assertEqual(track["artist"], "Ada")
        self.assertEqual(track["artworkId"], "ha-cover")
        self.assertEqual(track["durationMs"], 200000)
        self.assertEqual(snapshot["data"]["data"]["data"]["playback"]["state"], "playing")
        self.assertEqual(snapshot["data"]["data"]["data"]["playback"]["positionMs"], 12500)
        self.assertEqual(state.get("device")["serialNumber"], "ABC123")

    def test_idle_player_does_not_claim_now_playing(self) -> None:
        state = AppState()
        session = GatewaySession(state, HomeAssistant(token=""))
        session.handle({"id": "v", "meta": {"kind": "event"}, "data": {"type": "version", "data": {"serialNumber": "S"}}})
        events = [item["data"]["data"]["event"] for item in session.sent if item["data"]["type"] in ("authority", "player")]
        self.assertNotIn("claim", events)
        self.assertEqual(events, ["snapshot"])
        snapshot = next(item for item in session.sent if item["data"]["type"] == "player")
        body = snapshot["data"]["data"]["data"]
        self.assertNotIn("track", body)
        self.assertNotIn("context", body)
        self.assertEqual(
            body["playback"],
            {
                "state": "paused",
                "positionMs": 0,
                "shuffle": False,
                "shuffleMode": "off",
                "repeat": "off",
                "setElapsedTimeAvailable": False,
            },
        )
        self.assertEqual(body["queue"], [])
        self.assertEqual(body["options"], {"speed": 1.0})

    def test_going_idle_releases_authority_before_the_paused_snapshot(self) -> None:
        state = AppState()
        state.set_playback(player_state_from_ha("media_player.spotify_kitchen", _playing()))
        session = GatewaySession(state, HomeAssistant(token=""))
        session.handle({"id": "v", "meta": {"kind": "event"}, "data": {"type": "version", "data": {"serialNumber": "S"}}})
        sent_before = len(session.sent)
        state.set_playback(player_state_from_ha("media_player.spotify_kitchen", None))
        session.sync_playback()
        follow = [(item["data"]["type"], item["data"]["data"]["event"]) for item in session.sent[sent_before:]]
        self.assertEqual(
            follow,
            [
                ("authority", "release"),
                ("authority", "release"),
                ("authority", "release"),
                ("player", "snapshot"),
            ],
        )
        released = [item["data"]["data"]["data"]["scope"] for item in session.sent[sent_before : sent_before + 3]]
        self.assertEqual(released, ["nowPlayingMetadata", "nowPlayingPlayback", "volume"])
        self.assertNotIn("track", session.sent[-1]["data"]["data"]["data"])
        self.assertEqual(session.sent[-1]["data"]["data"]["data"]["playback"]["state"], "paused")

    def test_device_transport_calls_the_source_player(self) -> None:
        state = AppState()
        state.update(sourceEntity="media_player.spotify_kitchen")
        ha = HomeAssistant(token="")
        session = GatewaySession(state, ha)
        session.handle({"id": "p", "meta": {"kind": "command"}, "data": {"type": "player", "data": {"event": "pause"}}})
        self.assertEqual(ha.calls[-1][1], "media_pause")
        session.handle(
            {
                "id": "s",
                "meta": {"kind": "command"},
                "data": {"type": "player", "data": {"event": "seekTo", "data": {"positionMs": 1500}}},
            }
        )
        self.assertEqual(ha.calls[-1], ("media_player", "media_seek", {"entity_id": "media_player.spotify_kitchen", "seek_position": 1.5}))
        session.handle(
            {
                "id": "r",
                "meta": {"kind": "request"},
                "data": {"type": "player", "data": {"event": "snapshotRequest"}},
            }
        )
        ack = session.sent[-1]
        self.assertEqual(ack["data"]["data"]["event"], "snapshotAck")
        self.assertEqual(ack["meta"]["data"]["requestId"], "r")

    def test_missing_source_is_play_failed(self) -> None:
        session = GatewaySession(AppState(), HomeAssistant(token=""))
        session.handle({"id": "p", "meta": {"kind": "command"}, "data": {"type": "player", "data": {"event": "skipNext"}}})
        error = session.sent[-1]["data"]["data"]["data"]["error"]
        self.assertEqual(error["type"], "playFailed")
        self.assertEqual(error["data"]["reason"], "no active transport")

    def test_shuffle_flag_and_repeat_pass_through(self) -> None:
        service = transport_service("setShuffle", {"on": True}, "media_player.kitchen")
        self.assertEqual(service[2]["shuffle"], True)
        repeat = transport_service("setRepeat", {"mode": "all"}, "media_player.kitchen")
        self.assertEqual(repeat[2]["repeat"], "all")

    def test_keepalive_and_unsupported_request(self) -> None:
        session = GatewaySession(AppState(), HomeAssistant(token=""))
        session.handle(
            {"id": "k", "meta": {"kind": "request"}, "data": {"type": "system", "data": {"event": "keepalive", "data": {"seq": 7}}}}
        )
        ack = session.sent[-1]
        self.assertEqual(ack["data"]["data"]["data"]["seq"], 7)
        self.assertEqual(ack["meta"]["data"]["requestId"], "k")
        session.handle({"id": "u", "meta": {"kind": "request"}, "data": {"type": "phone", "data": {"event": "ring"}}})
        self.assertEqual(session.sent[-1]["data"]["type"], "error")
        self.assertEqual(session.sent[-1]["data"]["data"]["type"], "unsupported")


class BrightnessTests(unittest.TestCase):
    def test_slider_sets_manual_then_level(self) -> None:
        commands = brightness_commands(40)
        self.assertEqual(commands[0], ("hardware", "displaySetMode", {"mode": "manual"}, "command"))
        self.assertEqual(commands[1][2]["level"], 0.4)
        state = AppState()
        state.update(clientConnected=True)
        companion = Companion(state, HomeAssistant(token=""))
        status, _payload = route_post(companion, "/brightness", {"value": 40})
        self.assertEqual(status, 200)
        events = [item["data"]["data"]["event"] for item in companion.device.sent]
        self.assertEqual(events[:2], ["displaySetMode", "displaySetLevel"])
        self.assertEqual(companion.device.sent[1]["data"]["data"]["data"]["level"], 0.4)

    def test_state_reply_updates_hardware(self) -> None:
        session = DeviceSession(AppState())
        session.handle(
            {
                "id": "h",
                "meta": {"kind": "response", "data": {"requestId": "q"}},
                "data": {
                    "type": "hardware",
                    "data": {
                        "event": "stateReply",
                        "data": {
                            "state": {
                                "brightness": {"mode": "auto", "level": 0.2, "effectiveLevel": 0.55},
                                "ambientLevel": 30,
                            }
                        },
                    },
                },
            }
        )
        hardware = session.state.get("hardware")
        self.assertEqual(hardware["brightness"]["effectiveLevel"], 0.55)
        self.assertEqual(hardware["ambientLevel"], 30)

    def test_auto_effective_level_does_not_move_the_set_brightness(self) -> None:
        hardware = {"brightness": {"mode": "auto", "level": 0.2, "effectiveLevel": 0.55}}
        self.assertEqual(displayed_brightness(hardware), 20)

    def test_webapp_list_and_activate_stay_on_the_client_socket(self) -> None:
        session = DeviceSession(AppState())
        session.handle(
            {
                "id": "w",
                "meta": {"kind": "response"},
                "data": {
                    "type": "webapp",
                    "data": {"event": "listReply", "data": {"webapps": [{"id": "app-1", "name": "Now Playing"}]}},
                },
            }
        )
        session.activate_webapp("app-1")
        self.assertEqual(session.state.get("webapps")[0]["name"], "Now Playing")
        sent = session.sent[-1]
        self.assertEqual(sent["data"]["type"], "webapp")
        self.assertEqual(sent["data"]["data"]["event"], "activate")
        self.assertEqual(sent["data"]["data"]["data"]["id"], "app-1")
        self.assertNotIn("\xde\xad", __import__("json").dumps(sent))
