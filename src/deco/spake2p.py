"""
TPAP のログインに使う SPAKE2+ (P-256、RFC 9383) の prover。

transcript・確認の MAC・SharedKey は RFC 9383 に従う。TP-Link 固有の値 (context tag `PAKE V1` と、
SharedKey から AES-128-CCM の鍵・nonce を導く HKDF の salt / info) は、BE85 V2 FW 1.3.0 の
libtpap.so と一致することを確かめている。
"""

import hashlib
import hmac
import struct
from secrets import randbelow
from typing import NamedTuple

from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC

# pycryptodome は EccPoint を ECC の公開 API として文書化しているが、型の上では re-export していない (4.4.0)。
from Crypto.PublicKey.ECC import EccPoint  # pyright: ignore[reportPrivateImportUsage]

_CURVE = "p256"
# P-256 の位数。scalar はこの値を法とする。
_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = EccPoint(
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
    curve=_CURVE,
)
_SEC1_PREFIXES = (b"\x02", b"\x03", b"\x04")
_CONTEXT_TAG = b"PAKE V1"


def _decode_point(data: bytes) -> EccPoint:
    """
    SEC1 の点 (0x04 で非圧縮、0x02 / 0x03 で圧縮) を読む。

    Raises:
        ValueError: SEC1 の点でない、P-256 の曲線上にない、または無限遠点。
    """
    # import_key は DER・PEM 等も受け付けるので、SEC1 の先頭 byte のものだけを渡す。
    if data[:1] not in _SEC1_PREFIXES:
        raise ValueError("not a SEC1 point")
    point = ECC.import_key(data, curve_name=_CURVE).pointQ
    if point.is_point_at_infinity():
        raise ValueError("the point at infinity is not a valid share")
    return point


def _encode_point(point: EccPoint) -> bytes:
    """
    非圧縮の SEC1 にする。

    Raises:
        ValueError: 無限遠点 (pycryptodome は (0, 0) として返すので、そのまま符号化しない)。
    """
    if point.is_point_at_infinity():
        raise ValueError("cannot encode the point at infinity")
    x, y = point.xy
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


# RFC 9383 の P-256 用 M / N。
_M = _decode_point(
    bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f")
)
_N = _decode_point(
    bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49")
)


def _hkdf(ikm: bytes, *, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256。salt が空なら、RFC 5869 の既定 (hash 長の 0 byte) を使う。"""
    return HKDF(ikm, length, salt, SHA256, context=info)


def _length_prefixed(*chunks: bytes) -> bytes:
    return b"".join(struct.pack("<Q", len(chunk)) + chunk for chunk in chunks)


class RegisterResult(NamedTuple):
    """pake_register の応答のうち、prover が使う値 (base64 を復号したもの)。"""

    dev_random: bytes
    dev_salt: bytes
    iterations: int
    dev_share: bytes


class Spake2pProver:
    """pake_register の応答から pake_share の値を作り、pake_share の応答を検証して session 鍵を導く。"""

    def __init__(
        self, register: RegisterResult, *, credential: str, user_random: bytes
    ) -> None:
        """
        pake_register の応答から、pake_share で送る user_share と user_confirm を作る。

        Raises:
            ValueError: dev_share が P-256 の有効な点でない、または演算の結果が無限遠点になった。
        """
        derived = hashlib.pbkdf2_hmac(
            "sha256", credential.encode(), register.dev_salt, register.iterations, 80
        )
        w0 = int.from_bytes(derived[:40], "big") % _ORDER
        w1 = int.from_bytes(derived[40:], "big") % _ORDER
        y_point = _decode_point(register.dev_share)
        x = randbelow(_ORDER - 1) + 1
        x_point = _G * x + _M * w0
        h_point = y_point + (-(_N * w0))
        self.user_share = _encode_point(x_point)
        y_bytes = _encode_point(y_point)
        context = hashlib.sha256(
            _CONTEXT_TAG + user_random + register.dev_random
        ).digest()
        transcript = _length_prefixed(
            context,
            b"",
            b"",
            _encode_point(_M),
            _encode_point(_N),
            self.user_share,
            y_bytes,
            _encode_point(h_point * x),
            _encode_point(h_point * w1),
            w0.to_bytes(32, "big"),
        )
        main_key = hashlib.sha256(transcript).digest()
        confirm = _hkdf(main_key, salt=b"", info=b"ConfirmationKeys", length=64)
        self._device_confirm_key = confirm[32:]
        self.user_confirm = hmac.new(confirm[:32], y_bytes, hashlib.sha256).digest()
        self._shared_key = _hkdf(main_key, salt=b"", info=b"SharedKey", length=32)

    def device_confirm_ok(self, dev_confirm: bytes) -> bool:
        expected = hmac.new(
            self._device_confirm_key, self.user_share, hashlib.sha256
        ).digest()
        return hmac.compare_digest(dev_confirm, expected)

    def session_key_and_nonce(self) -> tuple[bytes, bytes]:
        """AES-128-CCM の 16 byte の鍵と、packet ごとの nonce の先頭 8 byte。"""
        key = _hkdf(
            self._shared_key,
            salt=b"tp-kdf-salt-aes128-key",
            info=b"tp-kdf-info-aes128-key",
            length=32,
        )[:16]
        nonce_base = _hkdf(
            self._shared_key,
            salt=b"tp-kdf-salt-aes128-iv",
            info=b"tp-kdf-info-aes128-iv",
            length=32,
        )[:8]
        return key, nonce_base
