"""Shannon stream cipher used by Spotify's access-point handshake.

The register update matches the reference implementation Spotify's own clients use.
"""

from __future__ import annotations

_N = 16
_FOLD = _N
_INITKONST = 0x6996C53A
_KEYP = 13


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _rotl(word: int, bits: int) -> int:
    word = _u32(word)
    return _u32((word << bits) | (word >> (32 - bits)))


def _sbox1(word: int) -> int:
    word = _u32(word)
    word = _u32(word ^ (_rotl(word, 5) | _rotl(word, 7)))
    return _u32(word ^ (_rotl(word, 19) | _rotl(word, 22)))


def _sbox2(word: int) -> int:
    word = _u32(word)
    word = _u32(word ^ (_rotl(word, 7) | _rotl(word, 22)))
    return _u32(word ^ (_rotl(word, 5) | _rotl(word, 19)))


class Shannon:
    def __init__(self, key: bytes) -> None:
        self.R = [0] * _N
        self.CRC = [0] * _N
        self.initR = [0] * _N
        self.konst = _INITKONST
        self.sbuf = 0
        self.mbuf = 0
        self.nbuf = 0
        self.R[0] = 1
        self.R[1] = 1
        for index in range(2, _N):
            self.R[index] = _u32(self.R[index - 1] + self.R[index - 2])
        self._loadkey(key)
        self.konst = self.R[0]
        self.initR = list(self.R)

    def _cycle(self) -> None:
        mixed = _u32(self.R[12] ^ self.R[13] ^ self.konst)
        mixed = _u32(_sbox1(mixed) ^ _rotl(self.R[0], 1))
        self.R[:-1] = self.R[1:]
        self.R[-1] = mixed
        mixed = _sbox2(_u32(self.R[2] ^ self.R[15]))
        self.R[0] = _u32(self.R[0] ^ mixed)
        self.sbuf = _u32(mixed ^ self.R[8] ^ self.R[12])

    def _diffuse(self) -> None:
        for _ in range(_FOLD):
            self._cycle()

    def _loadkey(self, key: bytes) -> None:
        for start in range(0, len(key), 4):
            word = key[start : start + 4]
            if len(word) < 4:
                word = word + bytes(4 - len(word))
            self.R[_KEYP] = _u32(self.R[_KEYP] ^ int.from_bytes(word, "little"))
            self._cycle()
        self.R[_KEYP] = _u32(self.R[_KEYP] ^ len(key))
        self._cycle()
        self.CRC = list(self.R)
        self._diffuse()
        for index in range(_N):
            self.R[index] = _u32(self.R[index] ^ self.CRC[index])

    def nonce(self, nonce: bytes) -> None:
        self.R = list(self.initR)
        self.konst = _INITKONST
        self._loadkey(nonce)
        self.konst = self.R[0]
        self.nbuf = 0

    def nonce_u32(self, nonce: int) -> None:
        self.nonce(int(nonce).to_bytes(4, "big"))

    def _crcfunc(self, word: int) -> None:
        mixed = _u32(self.CRC[0] ^ self.CRC[2] ^ self.CRC[15] ^ word)
        self.CRC[:-1] = self.CRC[1:]
        self.CRC[-1] = mixed

    def _macfunc(self, word: int) -> None:
        self._crcfunc(word)
        self.R[_KEYP] = _u32(self.R[_KEYP] ^ word)

    def _consume_partial(self, buf: bytearray, index: int, *, decrypt: bool) -> None:
        if decrypt:
            buf[index] ^= (self.sbuf >> (32 - self.nbuf)) & 0xFF
            self.mbuf = _u32(self.mbuf ^ (buf[index] << (32 - self.nbuf)))
        else:
            self.mbuf = _u32(self.mbuf ^ (buf[index] << (32 - self.nbuf)))
            buf[index] ^= (self.sbuf >> (32 - self.nbuf)) & 0xFF

    def _process(self, buf: bytearray, *, decrypt: bool) -> None:
        index = 0
        size = len(buf)
        if self.nbuf:
            while self.nbuf and index < size:
                self._consume_partial(buf, index, decrypt=decrypt)
                self.nbuf -= 8
                index += 1
                if self.nbuf == 0:
                    self._macfunc(self.mbuf)
            if index == size:
                return
        while index + 4 <= size:
            self._cycle()
            word = int.from_bytes(buf[index : index + 4], "little")
            if decrypt:
                word = _u32(word ^ self.sbuf)
                self._macfunc(word)
            else:
                self._macfunc(word)
                word = _u32(word ^ self.sbuf)
            buf[index : index + 4] = word.to_bytes(4, "little")
            index += 4
        if index < size:
            self._cycle()
            self.mbuf = 0
            self.nbuf = 32
            while index < size:
                self._consume_partial(buf, index, decrypt=decrypt)
                self.nbuf -= 8
                index += 1

    def encrypt(self, buf: bytearray) -> None:
        self._process(buf, decrypt=False)

    def decrypt(self, buf: bytearray) -> None:
        self._process(buf, decrypt=True)

    def finish(self, size: int) -> bytes:
        if self.nbuf:
            self._macfunc(self.mbuf)
        self._cycle()
        self.R[_KEYP] = _u32(self.R[_KEYP] ^ _INITKONST ^ (self.nbuf << 3))
        self.nbuf = 0
        for index in range(_N):
            self.R[index] = _u32(self.R[index] ^ self.CRC[index])
        self._diffuse()
        out = bytearray()
        while len(out) < size:
            self._cycle()
            out += self.sbuf.to_bytes(4, "little")
        return bytes(out[:size])

    def check_mac(self, expected: bytes) -> None:
        actual = self.finish(len(expected))
        if actual != expected:
            raise ValueError("MAC mismatch")
