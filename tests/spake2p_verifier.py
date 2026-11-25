"""
fake の Deco が TPAP のログインを受けるための SPAKE2+ verifier (P-256)。

本番のクライアント (src/deco/spake2p.py の prover) とは別に組み、両者が噛み合うことで実装を
相互に検証する。EC の演算・transcript・鍵スケジュールは RFC 9383 の P-256 に従う。
"""

import hashlib
import hmac
import struct
from base64 import b64decode, b64encode
from secrets import randbits, token_bytes

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
_Point = tuple[int, int] | None


def _add(p1: _Point, *, p2: _Point) -> _Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        m = (3 * x1 * x1 + _A) * pow(2 * y1, _P - 2, _P) % _P
    else:
        m = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    return (x3, (m * (x1 - x3) - y1) % _P)


_Jacobian = tuple[int, int, int]


def _jacobian_double(point: _Jacobian) -> _Jacobian:
    x, y, z = point
    if y == 0:
        return (0, 0, 0)
    z2 = z * z % _P
    m = 3 * (x - z2) * (x + z2) % _P
    s = 4 * x * y * y % _P
    nx = (m * m - 2 * s) % _P
    y2 = y * y % _P
    ny = (m * (s - nx) - 8 * y2 * y2) % _P
    nz = 2 * y * z % _P
    return (nx, ny, nz)


def _jacobian_add(p1: _Jacobian, *, p2: _Jacobian) -> _Jacobian:
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    if z1 == 0:
        return p2
    if z2 == 0:
        return p1
    z1s = z1 * z1 % _P
    z2s = z2 * z2 % _P
    u1 = x1 * z2s % _P
    u2 = x2 * z1s % _P
    s1 = y1 * z2s * z2 % _P
    s2 = y2 * z1s * z1 % _P
    if u1 == u2:
        if s1 != s2:
            return (0, 0, 0)
        return _jacobian_double(p1)
    h = (u2 - u1) % _P
    r = (s2 - s1) % _P
    h2 = h * h % _P
    h3 = h2 * h % _P
    u1h2 = u1 * h2 % _P
    nx = (r * r - h3 - 2 * u1h2) % _P
    ny = (r * (u1h2 - nx) - s1 * h3) % _P
    nz = h * z1 * z2 % _P
    return (nx, ny, nz)


def _mul(k: int, *, point: _Point) -> _Point:
    k %= _N
    if k == 0 or point is None:
        return None
    result: _Jacobian = (0, 0, 0)
    addend: _Jacobian = (point[0], point[1], 1)
    while k:
        if k & 1:
            result = _jacobian_add(result, p2=addend)
        addend = _jacobian_double(addend)
        k >>= 1
    x, y, z = result
    if z == 0:
        return None
    z_inv = pow(z, _P - 2, _P)
    z_inv2 = z_inv * z_inv % _P
    return (x * z_inv2 % _P, y * z_inv2 * z_inv % _P)


def _neg(p: _Point) -> _Point:
    return None if p is None else (p[0], (-p[1]) % _P)


def _decode(data: bytes) -> _Point:
    if data[0] == 0x04:
        return (int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:65], "big"))
    x = int.from_bytes(data[1:33], "big")
    y = pow((pow(x, 3, _P) + _A * x + _B) % _P, (_P + 1) // 4, _P)
    if (y & 1) != (data[0] & 1):
        y = _P - y
    return (x, y)


def _encode(p: _Point) -> bytes:
    assert p is not None
    return b"\x04" + p[0].to_bytes(32, "big") + p[1].to_bytes(32, "big")


_M = _decode(
    bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f")
)
_N_POINT = _decode(
    bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49")
)
_CONTEXT_TAG = b"PAKE V1"
_ITERATIONS = 100000


def _hkdf(ikm: bytes, *, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _length_prefixed(*chunks: bytes) -> bytes:
    return b"".join(struct.pack("<Q", len(chunk)) + chunk for chunk in chunks)


class Spake2pVerifier:
    """1 回の pake_register → pake_share を受ける verifier。"""

    def __init__(self, credential: str) -> None:
        # device と同じく、credential ("<email>/<password>") から直接 verifier を作る。
        self._dev_salt = token_bytes(32)
        self._dev_random = token_bytes(32)
        derived = hashlib.pbkdf2_hmac(
            "sha256", credential.encode(), self._dev_salt, _ITERATIONS, 80
        )
        self._w0 = int.from_bytes(derived[:40], "big") % _N
        w1 = int.from_bytes(derived[40:], "big") % _N
        self._l = _mul(w1, point=_G)
        self._y = randbits(256) % (_N - 1) + 1
        self._y_point = _add(_mul(self._y, point=_G), p2=_mul(self._w0, point=_N_POINT))

    def register_result(self) -> dict[str, str | int]:
        """pake_register の応答 result。device は credential を変換しない (extra_crypt 無し)。"""
        return {
            "dev_salt": b64encode(self._dev_salt).decode(),
            "dev_random": b64encode(self._dev_random).decode(),
            "dev_share": b64encode(_encode(self._y_point)).decode(),
            "iterations": _ITERATIONS,
        }

    def share_response(
        self, *, user_random: str, user_share: bytes, user_confirm: bytes
    ) -> tuple[bytes, bytes, bytes] | None:
        """
        pake_share を検証する。

        user_confirm が合えば (dev_confirm, session 鍵, nonce の先頭 8 byte) を返し、合わなければ
        None を返す。
        """
        x_point = _decode(user_share)
        t = _add(x_point, p2=_neg(_mul(self._w0, point=_M)))
        z = _mul(self._y, point=t)
        v = _mul(self._y, point=self._l)
        context = hashlib.sha256(
            _CONTEXT_TAG + b64decode(user_random) + self._dev_random
        ).digest()
        transcript = _length_prefixed(
            context,
            b"",
            b"",
            _encode(_M),
            _encode(_N_POINT),
            user_share,
            _encode(self._y_point),
            _encode(z),
            _encode(v),
            self._w0.to_bytes(32, "big"),
        )
        ke = hashlib.sha256(transcript).digest()
        confirm = _hkdf(ke, salt=b"\x00" * 32, info=b"ConfirmationKeys", length=64)
        expected_user = hmac.new(
            confirm[:32], _encode(self._y_point), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(user_confirm, expected_user):
            return None
        dev_confirm = hmac.new(confirm[32:], user_share, hashlib.sha256).digest()
        shared = _hkdf(ke, salt=b"\x00" * 32, info=b"SharedKey", length=32)
        key = _hkdf(
            shared,
            salt=b"tp-kdf-salt-aes128-key",
            info=b"tp-kdf-info-aes128-key",
            length=32,
        )
        nonce_base = _hkdf(
            shared,
            salt=b"tp-kdf-salt-aes128-iv",
            info=b"tp-kdf-info-aes128-iv",
            length=32,
        )[:8]
        return dev_confirm, key[:16], nonce_base
