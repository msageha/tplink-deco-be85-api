"""
fake の Deco が TPAP のログインを受けるための SPAKE2+ verifier (P-256)。

本番のクライアント (src/deco/spake2p.py の prover) とは別に RFC 9383 の verifier 側を組み、両者が
噛み合うことで transcript と鍵スケジュールを相互に検証する。点の演算は pycryptodome に任せる。
"""

import hashlib
import hmac
import struct
from base64 import b64encode
from secrets import randbelow, token_bytes

from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.PublicKey.ECC import EccPoint  # pyright: ignore[reportPrivateImportUsage]

_CURVE = "p256"
_G = ECC.construct(curve=_CURVE, d=1).pointQ
_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_M = ECC.import_key(
    bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"),
    curve_name=_CURVE,
).pointQ
_N = ECC.import_key(
    bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"),
    curve_name=_CURVE,
).pointQ
_CONTEXT_TAG = b"PAKE V1"
_ITERATIONS = 100000


def _encode(point: EccPoint) -> bytes:
    x, y = point.xy
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _hkdf(ikm: bytes, *, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(ikm, length, salt, SHA256, context=info)


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
        self._w0 = int.from_bytes(derived[:40], "big") % _ORDER
        w1 = int.from_bytes(derived[40:], "big") % _ORDER
        self._l = _G * w1
        self._y = randbelow(_ORDER - 1) + 1
        self._y_point = _G * self._y + _N * self._w0

    def register_result(self) -> dict[str, str | int]:
        """pake_register の応答 result。device は credential を変換しない (extra_crypt 無し)。"""
        return {
            "dev_salt": b64encode(self._dev_salt).decode(),
            "dev_random": b64encode(self._dev_random).decode(),
            "dev_share": b64encode(_encode(self._y_point)).decode(),
            "iterations": _ITERATIONS,
        }

    def share_response(
        self, *, user_random: bytes, user_share: bytes, user_confirm: bytes
    ) -> tuple[bytes, bytes, bytes] | None:
        """
        pake_share を検証する。

        user_confirm が合えば (dev_confirm, session 鍵, nonce の先頭 8 byte) を返し、合わなければ
        None を返す。
        """
        x_point = ECC.import_key(user_share, curve_name=_CURVE).pointQ
        t = x_point + (-(_M * self._w0))
        context = hashlib.sha256(_CONTEXT_TAG + user_random + self._dev_random).digest()
        transcript = _length_prefixed(
            context,
            b"",
            b"",
            _encode(_M),
            _encode(_N),
            user_share,
            _encode(self._y_point),
            _encode(t * self._y),
            _encode(self._l * self._y),
            self._w0.to_bytes(32, "big"),
        )
        main_key = hashlib.sha256(transcript).digest()
        confirm = _hkdf(main_key, salt=b"", info=b"ConfirmationKeys", length=64)
        expected_user = hmac.new(
            confirm[:32], _encode(self._y_point), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(user_confirm, expected_user):
            return None
        dev_confirm = hmac.new(confirm[32:], user_share, hashlib.sha256).digest()
        shared = _hkdf(main_key, salt=b"", info=b"SharedKey", length=32)
        key = _hkdf(
            shared,
            salt=b"tp-kdf-salt-aes128-key",
            info=b"tp-kdf-info-aes128-key",
            length=32,
        )[:16]
        nonce_base = _hkdf(
            shared,
            salt=b"tp-kdf-salt-aes128-iv",
            info=b"tp-kdf-info-aes128-iv",
            length=32,
        )[:8]
        return dev_confirm, key, nonce_base
