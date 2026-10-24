"""Artwork ids the Car Thing actually requests.

Bridgething mints `spotify/img/{edge}/i{file}` for i.scdn.co images and
`spotify/img/{edge}/u{percent-encoded url}` for every other picture. The device
then rewrites the edge to 248 or 96 and asks the companion for that id.
"""

from __future__ import annotations

HERO_EDGE = 248
THUMB_EDGE = 96
SCDN_IMAGE_PREFIX = "https://i.scdn.co/image/"
LIKED_SONGS_ASSET = "builtin/img/liked-songs"

_UNRESERVED = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def asset_id_for(url: str, edge: int = HERO_EDGE) -> str | None:
    if not url:
        return None
    if url.startswith("builtin:"):
        name = url.split(":", 1)[1]
        return f"builtin/img/{name}" if name else None
    if url.startswith(SCDN_IMAGE_PREFIX):
        rest = url[len(SCDN_IMAGE_PREFIX) :]
        if not rest:
            return None
        return f"spotify/img/{edge}/i{rest}"
    return f"spotify/img/{edge}/u{percent_encode(url)}"


def art_url(asset_id: str) -> str | None:
    """The picture URL inside an id, ignoring which edge the device asked for."""
    prefix = "spotify/img/"
    if not asset_id.startswith(prefix):
        return None
    edge, _, tagged = asset_id[len(prefix) :].partition("/")
    if not edge.isdigit() or len(tagged) < 2:
        return None
    tag, body = tagged[0], tagged[1:]
    if tag == "i":
        return SCDN_IMAGE_PREFIX + body
    if tag == "u":
        return percent_decode(body)
    return None


def with_edge(asset_id: str, edge: int) -> str:
    """Same rewrite the device applies before it asks the companion for a cover."""
    parts = asset_id.split("/", 3)
    if len(parts) != 4 or parts[1] != "img" or not parts[2].isdigit():
        return asset_id
    return f"{parts[0]}/img/{edge}/{parts[3]}"


def percent_encode(raw: str) -> str:
    out: list[str] = []
    for byte in raw.encode("utf-8"):
        if byte in _UNRESERVED:
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def percent_decode(encoded: str) -> str | None:
    out = bytearray()
    index = 0
    while index < len(encoded):
        if encoded[index] == "%":
            hex_pair = encoded[index + 1 : index + 3]
            if len(hex_pair) != 2:
                return None
            try:
                out.append(int(hex_pair, 16))
            except ValueError:
                return None
            index += 3
            continue
        out.append(ord(encoded[index]) & 0xFF)
        index += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None
