"""
TPAP のログインに使う SPAKE2+ (P-256, RFC 9383 の M/N 点)。

FW 1.3.0 の Deco は TMP (SSH) を閉じ、tpapsvr (port 30001, TLS) の SPAKE2+ でオーナーの
TP-Link ID を認証する。SPAKE2+ は PAKE なので、正しい credential の verifier を持たない接続先は
`dev_confirm` を作れず、credential は偽の接続先に渡らない。

P-256 の演算・transcript・鍵スケジュールは、TP-Link の TPAP 実装を復元した
freeKC/tapo-v4-protocol (MIT) と JurajNyiri/pytapo (MIT) に合わせ、BE85 V2 FW 1.3.0 の
libtpap.so (M/N 点・context tag `PAKE V1`・HKDF の salt / info) と一致することを確認している。
"""

import hashlib
import hmac
import struct
from base64 import b64decode
from typing import Any

# P-256 (secp256r1)。
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
_Point = tuple[int, int] | None
# SEC1 の点の先頭 byte。
_SEC1_UNCOMPRESSED = 0x04
_SEC1_COMPRESSED = (0x02, 0x03)
# SEC1 の点の長さ (非圧縮 = 先頭 byte + x + y、圧縮 = 先頭 byte + x)。
_SEC1_UNCOMPRESSED_LEN = 65
_SEC1_COMPRESSED_LEN = 33


def _inv(x: int) -> int:
    return pow(x % _P, _P - 2, _P)


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
        m = (3 * x1 * x1 + _A) * _inv(2 * y1) % _P
    else:
        m = (y2 - y1) * _inv(x2 - x1) % _P
    x3 = (m * m - x1 - x2) % _P
    return (x3, (m * (x1 - x3) - y1) % _P)


# scalar 倍は Jacobian 座標 (X:Y:Z = X/Z^2, Y/Z^3) で計算する。affine で加算ごとに逆元を取ると、
# 1 回の倍で数百回の modular inverse が要り、ログインの timeout を超えるほど遅くなる。
_Jacobian = tuple[int, int, int]


def _jacobian_double(point: _Jacobian) -> _Jacobian:
    x, y, z = point
    if y == 0:
        return (0, 0, 0)
    # a = -3 なので m = 3*(x - z^2)*(x + z^2)。
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
    z_inv = _inv(z)
    z_inv2 = z_inv * z_inv % _P
    return (x * z_inv2 % _P, y * z_inv2 * z_inv % _P)


def _neg(p: _Point) -> _Point:
    return None if p is None else (p[0], (-p[1]) % _P)


def _on_curve(x: int, *, y: int) -> bool:
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x + _A * x + _B)) % _P == 0


def _decode_point(data: bytes) -> _Point:
    """
    SEC1 の点 (0x04 で非圧縮、0x02 / 0x03 で圧縮) を読む。

    信頼できない接続先が送る dev_share を秘密の演算に使う前に、長さと P-256 上の点であることを
    確かめる。満たさなければ拒否する。

    Raises:
        ValueError: 長さや符号化が不正、または曲線上の点でない。
    """
    if len(data) == _SEC1_UNCOMPRESSED_LEN and data[0] == _SEC1_UNCOMPRESSED:
        x, y = int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:65], "big")
        if _on_curve(x, y=y):
            return (x, y)
    elif len(data) == _SEC1_COMPRESSED_LEN and data[0] in _SEC1_COMPRESSED:
        x = int.from_bytes(data[1:33], "big")
        if 0 <= x < _P:
            y = pow((pow(x, 3, _P) + _A * x + _B) % _P, (_P + 1) // 4, _P)
            if (y & 1) != (data[0] & 1):
                y = _P - y
            if _on_curve(x, y=y):
                return (x, y)
    raise ValueError("SPAKE2+ dev_share is not a valid P-256 point")


def _encode_point(p: _Point) -> bytes:
    if p is None:
        raise ValueError("cannot encode the point at infinity")
    return b"\x04" + p[0].to_bytes(32, "big") + p[1].to_bytes(32, "big")


# RFC 9383 の P-256 用 M / N。
_M = _decode_point(
    bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f")
)
_N_POINT = _decode_point(
    bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49")
)
_CONTEXT_TAG = b"PAKE V1"
# PBKDF2 の反復回数の上限。接続先 (認証前) が返す値を無制限に渡すと、偽の接続先が巨大な値で
# worker thread と lock を占有できる (watchdog は I/O しか止められない)。実機は 100000 なので、
# 余裕を見た上限で頭打ちにする。
_MAX_ITERATIONS = 1_000_000


def _hkdf_sha256(ikm: bytes, *, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _length_prefixed(*chunks: bytes) -> bytes:
    return b"".join(struct.pack("<Q", len(chunk)) + chunk for chunk in chunks)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_credential(*, email: str, password: str) -> str:
    """
    device が SPAKE2+ の verifier を作るのに使う credential を組む。

    device は provisioning 時に `libtpap.generate_verify("<email>/<password>")` で verifier を
    作って保存するので (BE85 FW 1.3.0 の accountmgnt.lua)、同じ文字列を credential にする。
    """
    return f"{email}/{password}"


class Spake2pProver:
    """pake_register の応答から pake_share の材料を作り、応答を検証して session 鍵を導く。"""

    def __init__(
        self,
        register_result: dict[str, Any],
        *,
        user_random: str,
        credential: str,
        random_scalar: int,
    ) -> None:
        dev_salt = b64decode(register_result["dev_salt"])
        iterations = int(register_result["iterations"])
        if not 1 <= iterations <= _MAX_ITERATIONS:
            raise ValueError(f"SPAKE2+ iterations out of range: {iterations}")
        derived = hashlib.pbkdf2_hmac(
            "sha256", credential.encode(), dev_salt, iterations, 80
        )
        w0 = int.from_bytes(derived[:40], "big") % _N
        w1 = int.from_bytes(derived[40:], "big") % _N
        y_point = _decode_point(b64decode(register_result["dev_share"]))
        x = random_scalar % (_N - 1) + 1
        x_point = _add(_mul(x, point=_G), p2=_mul(w0, point=_M))
        h_point = _add(y_point, p2=_neg(_mul(w0, point=_N_POINT)))
        self.user_share = _encode_point(x_point)
        y_bytes = _encode_point(y_point)
        context = hashlib.sha256(
            _CONTEXT_TAG
            + b64decode(user_random)
            + b64decode(register_result["dev_random"])
        ).digest()
        transcript = _length_prefixed(
            context,
            b"",
            b"",
            _encode_point(_M),
            _encode_point(_N_POINT),
            self.user_share,
            y_bytes,
            _encode_point(_mul(x, point=h_point)),
            _encode_point(_mul(w1, point=h_point)),
            w0.to_bytes(32, "big"),
        )
        ke = hashlib.sha256(transcript).digest()
        confirm = _hkdf_sha256(
            ke, salt=b"\x00" * 32, info=b"ConfirmationKeys", length=64
        )
        self._kcb = confirm[32:]
        self.user_confirm = hmac.new(confirm[:32], y_bytes, hashlib.sha256).digest()
        self._shared_key = _hkdf_sha256(
            ke, salt=b"\x00" * 32, info=b"SharedKey", length=32
        )

    def device_confirm_ok(self, dev_confirm: bytes) -> bool:
        expected = hmac.new(self._kcb, self.user_share, hashlib.sha256).digest()
        return hmac.compare_digest(dev_confirm, expected)

    def session_key_and_nonce(self) -> tuple[bytes, bytes]:
        """AES-128-CCM の 16 byte の鍵と、packet ごとの nonce の先頭 8 byte。"""
        key = _hkdf_sha256(
            self._shared_key,
            salt=b"tp-kdf-salt-aes128-key",
            info=b"tp-kdf-info-aes128-key",
            length=32,
        )[:16]
        nonce_base = _hkdf_sha256(
            self._shared_key,
            salt=b"tp-kdf-salt-aes128-iv",
            info=b"tp-kdf-info-aes128-iv",
            length=32,
        )[:8]
        return key, nonce_base
